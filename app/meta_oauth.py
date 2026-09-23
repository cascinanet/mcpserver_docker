"""OAuth 2.0 con Meta (Graph API) per il tipo di server 'meta' (Facebook + Instagram).

A differenza di LinkedIn, Meta non usa un refresh_token separato: il consenso dà un token
utente di breve durata, che va subito scambiato per uno di lunga durata (~60 giorni). Da
quel token si derivano i "page access token" (uno per ogni Pagina Facebook amministrata,
via /me/accounts) che di norma non scadono finché il token utente da cui derivano resta
valido. Per ogni Pagina con un account Instagram Business/Creator collegato, viene letto
anche l'id dell'account Instagram (campo 'instagram_business_account'): un solo consenso
OAuth basta per pubblicare sia su Facebook sia su Instagram.

Flusso: l'admin autorizza una volta tramite browser (pulsante nel form → redirect a Meta →
consenso → callback), l'hub scambia il 'code' per il token utente, lo estende a lunga durata,
poi interroga /me/accounts per Pagine + Instagram collegati. Tutto salvato su file
(DATA_DIR/creds/<id>-meta.json, chmod 600, fuori da git). Un task in background rinnova il
token utente prima che scada (~60 giorni) e ri-legge Pagine/Instagram.

Nota: le chiamate API vere e proprie (in app/mcp_servers/meta_server.py) richiedono che Meta
abbia approvato per l'app i permessi di pubblicazione (pages_manage_posts,
instagram_content_publish, ecc. — App Review, non immediata). Il flusso OAuth qui sotto è
verificabile anche prima della review con un account aggiunto come Tester/Amministratore
dell'app (funziona in modalità Sviluppo senza review).
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

logger = logging.getLogger("mcp.meta_oauth")

# Versione Graph API "vNN.0": Meta ne rilascia una nuova ogni pochi mesi e ne ritira le vecchie
# dopo circa 2 anni. Sovrascrivibile via env 'META_API_VERSION' senza toccare il codice.
GRAPH_VERSION = os.environ.get("META_API_VERSION", "").strip() or "v23.0"
AUTH_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
TOKEN_URL = f"{GRAPH_BASE}/oauth/access_token"

# Permessi del prodotto "Facebook Login per Business": pubblicazione su Pagine + Instagram
# collegato. 'business_management' serve per vedere le Pagine gestite tramite Business Manager.
DEFAULT_SCOPES = (
    "pages_show_list pages_read_engagement pages_manage_posts business_management "
    "instagram_basic instagram_content_publish"
)

REFRESH_MARGIN_SECONDS = 7 * 24 * 3600  # rinnova quando mancano meno di 7 giorni alla scadenza
SCHEDULER_CHECK_INTERVAL = 3600  # ogni ora
_STATE_TTL = 600  # 10 minuti per completare il consenso su Meta
_TIMEOUT = 30.0

_pending_states: dict[str, dict] = {}


class TokenExchangeError(Exception):
    """Meta ha rifiutato uno scambio di token (code->token, estensione a lunga durata, ecc.)."""


def _creds_dir() -> Path:
    path = get_settings().data_dir / "creds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path(server_id: str) -> Path:
    return _creds_dir() / f"{server_id}-meta.json"


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
    client_id = _env(server, "META_APP_ID")
    scope = _env(server, "META_SCOPES") or DEFAULT_SCOPES
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
    }
    return f"{AUTH_URL}?{httpx.QueryParams(params)}"


async def _get(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400:
        detail = (payload.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise TokenExchangeError(f"Meta ha rifiutato la richiesta: {detail}")
    return payload


async def exchange_code(server: MCPServer, code: str, redirect_uri: str) -> dict:
    """Code di autorizzazione -> token utente di breve durata (poche ore)."""
    return await _get(TOKEN_URL, {
        "client_id": _env(server, "META_APP_ID"),
        "client_secret": _env(server, "META_APP_SECRET"),
        "redirect_uri": redirect_uri,
        "code": code,
    })


async def extend_token(server: MCPServer, short_lived_token: str) -> dict:
    """Token di breve durata (o un token di lunga durata ancora valido) -> token utente di
    lunga durata (~60 giorni). Meta permette di ri-estendere un token già a lunga durata
    finché non è scaduto: è così che avviene il rinnovo periodico, senza un refresh_token
    separato come in altri provider."""
    return await _get(TOKEN_URL, {
        "grant_type": "fb_exchange_token",
        "client_id": _env(server, "META_APP_ID"),
        "client_secret": _env(server, "META_APP_SECRET"),
        "fb_exchange_token": short_lived_token,
    })


async def fetch_pages(user_access_token: str) -> dict[str, dict]:
    """Pagine Facebook amministrate dall'utente autorizzato, col relativo page access token
    (derivato dal token utente: valido finché lo è quello da cui deriva) e, se collegato,
    l'account Instagram Business/Creator della Pagina."""
    data = await _get(f"{GRAPH_BASE}/me/accounts", {
        "fields": "id,name,access_token",
        "access_token": user_access_token,
        "limit": "200",
    })
    pages: dict[str, dict] = {}
    for item in data.get("data", []):
        page_id = item.get("id")
        page_token = item.get("access_token")
        if not page_id or not page_token:
            continue
        entry = {"name": item.get("name"), "access_token": page_token,
                  "instagram_id": None, "instagram_username": None}
        try:
            ig = await _get(f"{GRAPH_BASE}/{page_id}", {
                "fields": "instagram_business_account{id,username}",
                "access_token": page_token,
            })
            account = ig.get("instagram_business_account")
            if account:
                entry["instagram_id"] = account.get("id")
                entry["instagram_username"] = account.get("username")
        except TokenExchangeError:
            # Pagina senza Instagram collegato, o permesso instagram_basic non ancora
            # approvato: la Pagina resta comunque utilizzabile per Facebook.
            pass
        pages[page_id] = entry
    return pages


# Durata tipica di un token utente di lunga durata, usata come ripiego quando Meta non
# restituisce 'expires_in' nella risposta di fb_exchange_token (osservato dal vivo: capita
# ri-autorizzando un utente che ha già un token valido — la richiesta di estensione riesce
# ma il campo manca o vale 0). Senza questo ripiego 'expires_in' mancante/0 veniva
# interpretato come "scade adesso", segnalando falsamente il token come scaduto subito dopo
# averlo ottenuto con successo (e innescando un tentativo di rinnovo a ogni chiamata).
DEFAULT_LONG_LIVED_SECONDS = 60 * 24 * 3600


def save_tokens(server_id: str, user_access_token: str, expires_in: float, pages: dict[str, dict]) -> None:
    now = time.time()
    record = {
        "user_access_token": user_access_token,
        "expires_at": now + float(expires_in or DEFAULT_LONG_LIVED_SECONDS),
        "obtained_at": now,
        "pages": pages,
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
    if not tokens or not tokens.get("user_access_token"):
        return {"authorized": False}
    expires_at = tokens.get("expires_at") or 0
    pages = tokens.get("pages") or {}
    return {
        "authorized": True,
        "expires_at": expires_at,
        "expired": time.time() >= expires_at,
        "obtained_at": tokens.get("obtained_at"),
        "pagine": [
            {"id": pid, "nome": p.get("name"), "instagram": p.get("instagram_username")}
            for pid, p in pages.items()
        ],
    }


async def complete_authorization(server: MCPServer, server_id: str, code: str, redirect_uri: str) -> None:
    """Code -> token breve -> token lungo -> Pagine/Instagram -> salvataggio. Un'unica funzione
    così sia il callback OAuth sia lo scheduler di rinnovo condividono la stessa logica."""
    short = await exchange_code(server, code, redirect_uri)
    long_lived = await extend_token(server, short["access_token"])
    pages = await fetch_pages(long_lived["access_token"])
    save_tokens(server_id, long_lived["access_token"], long_lived.get("expires_in", 0), pages)


async def scheduler_loop() -> None:
    """Rinnova periodicamente il token utente (e ri-legge Pagine/Instagram) per tutti i
    server 'meta' autorizzati, prima che il token scada."""
    from app.storage import store  # import ritardato: store non deve dipendere da questo modulo

    while True:
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)
        for server in store.list_servers():
            if server.type != "meta":
                continue
            tokens = load_tokens(server.id)
            if not tokens or not tokens.get("user_access_token"):
                continue
            expires_at = tokens.get("expires_at") or 0
            if expires_at - time.time() > REFRESH_MARGIN_SECONDS:
                continue
            try:
                long_lived = await extend_token(server, tokens["user_access_token"])
                pages = await fetch_pages(long_lived["access_token"])
                save_tokens(server.id, long_lived["access_token"], long_lived.get("expires_in", 0), pages)
                logger.info("Token Meta rinnovato per '%s'", server.id)
            except Exception:  # noqa: BLE001
                logger.exception("Rinnovo token Meta fallito per '%s'", server.id)
