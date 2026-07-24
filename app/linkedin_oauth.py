"""OAuth 2.0 con LinkedIn (Community Management API) per il tipo di server 'linkedin'.

Flusso: l'admin autorizza una volta tramite browser (pulsante nel form → redirect a
LinkedIn → consenso → callback), l'hub scambia il 'code' per access_token + refresh_token
e li salva su file (DATA_DIR/creds/<id>-linkedin.json, chmod 600, fuori da git — stesso
trattamento delle credenziali service account Google). Un task in background rinnova il
token prima che scada (access token LinkedIn: ~60 giorni; refresh token: ~1 anno).

Nota: le chiamate API vere e proprie (in app/mcp_servers/linkedin_server.py) richiedono che
LinkedIn abbia approvato l'accesso al prodotto "Community Management API" per l'app; il
flusso OAuth qui sotto è verificabile/testabile anche prima dell'approvazione (con le API
di autenticazione di base), ma il consenso per gli scope di pubblicazione fallirà finché
il prodotto non è approvato.
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

logger = logging.getLogger("mcp.linkedin_oauth")

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# w_organization_social/r_organization_social/rw_organization_admin richiedono il prodotto
# "Community Management API" approvato; openid/profile funzionano anche senza (utili per
# verificare che il flusso OAuth di base sia cablato correttamente nel frattempo).
DEFAULT_SCOPES = "openid profile w_organization_social r_organization_social rw_organization_admin"

REFRESH_MARGIN_SECONDS = 3 * 24 * 3600  # rinnova quando mancano meno di 3 giorni alla scadenza
SCHEDULER_CHECK_INTERVAL = 3600  # ogni ora
_STATE_TTL = 600  # 10 minuti per completare il consenso su LinkedIn

# Stati OAuth pendenti (CSRF): in memoria, va bene perché il consenso si completa in pochi
# minuti nello stesso processo che l'ha generato (1 solo worker gunicorn, vedi README).
_pending_states: dict[str, dict] = {}


def _creds_dir() -> Path:
    path = get_settings().data_dir / "creds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path(server_id: str) -> Path:
    return _creds_dir() / f"{server_id}-linkedin.json"


def _env(server: MCPServer, key: str) -> str:
    value = server.env.get(key, "")
    return value if isinstance(value, str) else ""


def make_state(server_id: str) -> str:
    """Genera e registra uno state OAuth (protezione CSRF), scartando quelli scaduti."""
    now = time.time()
    expired = [s for s, v in _pending_states.items() if now - v["created_at"] > _STATE_TTL]
    for s in expired:
        _pending_states.pop(s, None)
    state = secrets.token_urlsafe(24)
    _pending_states[state] = {"server_id": server_id, "created_at": now}
    return state


def consume_state(state: str) -> str | None:
    """Convalida e consuma uno state (una sola volta): ritorna il server_id o None se non valido/scaduto."""
    entry = _pending_states.pop(state, None)
    if not entry or time.time() - entry["created_at"] > _STATE_TTL:
        return None
    return entry["server_id"]


def authorize_url(server: MCPServer, redirect_uri: str, state: str) -> str:
    client_id = _env(server, "LINKEDIN_CLIENT_ID")
    # Scope sovrascrivibili via env 'LINKEDIN_SCOPES' (spazio-separati): utile per verificare
    # il flusso OAuth con scope di base (es. 'openid profile', non gated da approvazione
    # prodotto) prima che LinkedIn approvi Community Management API, che sblocca gli scope
    # di pubblicazione in DEFAULT_SCOPES.
    scope = _env(server, "LINKEDIN_SCOPES") or DEFAULT_SCOPES
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
    }
    return f"{AUTH_URL}?{httpx.QueryParams(params)}"


async def exchange_code(server: MCPServer, code: str, redirect_uri: str) -> dict:
    """Scambia il code di autorizzazione per access_token/refresh_token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _env(server, "LINKEDIN_CLIENT_ID"),
        "client_secret": _env(server, "LINKEDIN_CLIENT_SECRET"),
    }
    return await _post_token(data)


async def refresh_access_token(server: MCPServer, refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _env(server, "LINKEDIN_CLIENT_ID"),
        "client_secret": _env(server, "LINKEDIN_CLIENT_SECRET"),
    }
    return await _post_token(data)


async def _post_token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400:
        detail = payload.get("error_description") or payload.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"LinkedIn ha rifiutato la richiesta token: {detail}")
    return payload


def save_tokens(server_id: str, payload: dict) -> None:
    now = time.time()
    record = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": now + float(payload.get("expires_in", 0) or 0),
        "refresh_token_expires_at": (
            now + float(payload["refresh_token_expires_in"]) if payload.get("refresh_token_expires_in") else None
        ),
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
    """Stato di autorizzazione per il pannello admin (non richiama LinkedIn, legge solo il file)."""
    tokens = load_tokens(server_id)
    if not tokens or not tokens.get("access_token"):
        return {"authorized": False}
    expires_at = tokens.get("expires_at") or 0
    return {
        "authorized": True,
        "expires_at": expires_at,
        "expired": time.time() >= expires_at,
        "obtained_at": tokens.get("obtained_at"),
    }


async def scheduler_loop() -> None:
    """Rinnova periodicamente i token in scadenza per tutti i server 'linkedin' autorizzati."""
    from app.storage import store  # import ritardato: store non deve dipendere da questo modulo

    while True:
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)
        for server in store.list_servers():
            if server.type != "linkedin":
                continue
            tokens = load_tokens(server.id)
            if not tokens or not tokens.get("refresh_token"):
                continue
            expires_at = tokens.get("expires_at") or 0
            if expires_at - time.time() > REFRESH_MARGIN_SECONDS:
                continue
            try:
                payload = await refresh_access_token(server, tokens["refresh_token"])
                # LinkedIn non sempre restituisce un nuovo refresh_token: mantieni quello vecchio.
                payload.setdefault("refresh_token", tokens["refresh_token"])
                save_tokens(server.id, payload)
                logger.info("Token LinkedIn rinnovato per '%s'", server.id)
            except Exception:  # noqa: BLE001
                logger.exception("Rinnovo token LinkedIn fallito per '%s'", server.id)
