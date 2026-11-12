"""OAuth 2.0 con Google per il tipo di server 'google_ads' (Google Ads API).

A differenza di Meta, Google usa un refresh_token classico che non scade da solo (salvo
revoca, 6 mesi di inattività, o se l'app OAuth è ancora in stato "Testing" sulla schermata di
consenso — in quel caso Google lo invalida dopo 7 giorni, vedi avviso nel README). L'access
token di volta in volta ottenuto dal refresh_token dura ~1 ora.

Flusso: l'admin autorizza una volta tramite browser (pulsante nel form → redirect a Google →
consenso → callback), l'hub scambia il 'code' per access_token + refresh_token e li salva su
file (DATA_DIR/creds/<id>-google_ads.json, chmod 600, fuori da git). Un task in background
rinnova l'access token prima che scada; se il refresh_token stesso risulta non più valido
(revocato, o scaduto per il limite dei 7 giorni in modalità Testing), lo stato va a
'richiede nuova autorizzazione' invece di un errore silenzioso.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

from app.config import get_settings
from app.models import MCPServer

logger = logging.getLogger("mcp.google_ads_oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/adwords"

# Durata tipica di un access token Google quando la risposta non la specifica (non dovrebbe
# succedere con le API Google, ma è lo stesso ripiego difensivo adottato per Meta dopo aver
# trovato dal vivo una risposta priva di 'expires_in': meglio assumere una durata breve e
# rinnovare prima del dovuto, che segnare un token appena ottenuto come già scaduto).
DEFAULT_ACCESS_TOKEN_SECONDS = 3600

REFRESH_MARGIN_SECONDS = 300  # rinnova l'access token quando mancano meno di 5 minuti
SCHEDULER_CHECK_INTERVAL = 900  # ogni 15 minuti: l'access token dura solo ~1 ora
_STATE_TTL = 600  # 10 minuti per completare il consenso su Google
_TIMEOUT = 30.0

_pending_states: dict[str, dict] = {}


class ConfigError(Exception):
    """Configurazione mancante o autorizzazione OAuth non ancora completata."""


class AuthExpiredError(Exception):
    """Il refresh_token non è più valido (revocato, o scaduto per il limite dei 7 giorni in
    modalità Testing sulla schermata di consenso OAuth): va rifatta l'autorizzazione."""


def _creds_dir() -> Path:
    path = get_settings().data_dir / "creds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path(server_id: str) -> Path:
    return _creds_dir() / f"{server_id}-google_ads.json"


def _env(server: MCPServer, key: str) -> str:
    value = server.env.get(key, "")
    return value if isinstance(value, str) else ""


def make_state(server_id: str) -> str:
    now = time.time()
    expired = [s for s, v in _pending_states.items() if now - v["created_at"] > _STATE_TTL]
    for s in expired:
        _pending_states.pop(s, None)
    state = secrets.token_urlsafe(24)
    _pending_states[state] = {"server_id": server_id, "created_at": now}
    return state


def consume_state(state: str) -> str | None:
    entry = _pending_states.pop(state, None)
    if not entry or time.time() - entry["created_at"] > _STATE_TTL:
        return None
    return entry["server_id"]


def authorize_url(server: MCPServer, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": _env(server, "GOOGLE_ADS_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": SCOPE,
        # access_type=offline + prompt=consent: senza questi due Google spesso non restituisce
        # affatto un refresh_token (specie se l'utente ha già autorizzato questa app in passato).
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{httpx.QueryParams(params)}"


async def _post_token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400:
        error = payload.get("error")
        detail = payload.get("error_description") or error or f"HTTP {resp.status_code}"
        if error == "invalid_grant":
            # Codice specifico di Google per un code/refresh_token non più valido: a differenza
            # del generico 'OAuthException' di Meta, qui l'errore è già inequivocabile, niente
            # da classificare oltre.
            raise AuthExpiredError(f"Google ha rifiutato l'autorizzazione ({detail}): rifai il consenso OAuth.")
        raise RuntimeError(f"Google ha rifiutato la richiesta token: {detail}")
    return payload


async def exchange_code(server: MCPServer, code: str, redirect_uri: str) -> dict:
    """Code di autorizzazione -> access_token + refresh_token."""
    return await _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _env(server, "GOOGLE_ADS_CLIENT_ID"),
        "client_secret": _env(server, "GOOGLE_ADS_CLIENT_SECRET"),
    })


async def refresh_access_token(server: MCPServer, refresh_token: str) -> dict:
    """refresh_token -> nuovo access_token (~1 ora). Google di norma NON restituisce qui un
    nuovo refresh_token: va mantenuto quello già salvato."""
    return await _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _env(server, "GOOGLE_ADS_CLIENT_ID"),
        "client_secret": _env(server, "GOOGLE_ADS_CLIENT_SECRET"),
    })


def save_tokens(server_id: str, payload: dict) -> None:
    now = time.time()
    record = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": now + float(payload.get("expires_in") or DEFAULT_ACCESS_TOKEN_SECONDS),
        "obtained_at": now,
    }
    path = token_path(server_id)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_tokens(server_id: str) -> dict | None:
    path = token_path(server_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def delete_tokens(server_id: str) -> None:
    path = token_path(server_id)
    if path.exists():
        path.unlink()


def status(server_id: str) -> dict:
    tokens = load_tokens(server_id)
    if not tokens or not tokens.get("refresh_token"):
        return {"authorized": False}
    expires_at = tokens.get("expires_at") or 0
    return {
        "authorized": True,
        "expires_at": expires_at,
        "expired": time.time() >= expires_at,
        "obtained_at": tokens.get("obtained_at"),
    }


async def complete_authorization(server: MCPServer, server_id: str, code: str, redirect_uri: str) -> None:
    payload = await exchange_code(server, code, redirect_uri)
    if not payload.get("refresh_token"):
        # Capita se l'utente ha già autorizzato questa stessa app in passato e Google non
        # ripropone il refresh_token nonostante prompt=consent (raro ma possibile): senza,
        # l'hub non può rinnovare da solo l'accesso.
        raise RuntimeError(
            "Google non ha restituito un refresh_token. Prova a revocare l'accesso dell'app "
            "da https://myaccount.google.com/permissions e rifai il consenso da capo."
        )
    save_tokens(server_id, payload)


async def scheduler_loop() -> None:
    """Rinnova periodicamente l'access token per tutti i server 'google_ads' autorizzati."""
    from app.storage import store  # import ritardato: store non deve dipendere da questo modulo

    while True:
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)
        for server in store.list_servers():
            if server.type != "google_ads":
                continue
            tokens = load_tokens(server.id)
            if not tokens or not tokens.get("refresh_token"):
                continue
            expires_at = tokens.get("expires_at") or 0
            if expires_at - time.time() > REFRESH_MARGIN_SECONDS:
                continue
            try:
                payload = await refresh_access_token(server, tokens["refresh_token"])
                payload.setdefault("refresh_token", tokens["refresh_token"])
                save_tokens(server.id, payload)
                logger.info("Token Google Ads rinnovato per '%s'", server.id)
            except AuthExpiredError:
                logger.warning("Refresh token Google Ads non più valido per '%s': va rifatta l'autorizzazione.", server.id)
            except Exception:  # noqa: BLE001
                logger.exception("Rinnovo token Google Ads fallito per '%s'", server.id)
