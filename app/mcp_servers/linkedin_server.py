"""Server MCP per pubblicare e gestire post sulla Pagina aziendale LinkedIn.

Usa la Community Management API di LinkedIn (namespace /rest/posts). L'autenticazione è
OAuth 2.0: l'access token non è passato come credenziale statica, ma letto dal file salvato
dal flusso di autorizzazione dell'hub (app/linkedin_oauth.py, pulsante "Autorizza con
LinkedIn" nel form del server) — DATA_DIR/creds/<server_id>-linkedin.json.

IMPORTANTE: le chiamate a /rest/posts richiedono che LinkedIn abbia approvato per l'app
l'accesso al prodotto "Community Management API" (processo di review lato LinkedIn, non
immediato). Il codice è scritto secondo la documentazione ufficiale ma NON è stato
verificato con una chiamata reale approvata: alcuni nomi di campo (in particolare per
'statistiche_post', area meno stabile delle API LinkedIn) potrebbero richiedere aggiustamenti
al primo test dal vivo.

Configurazione (env):
    LINKEDIN_ORG_ID       obbligatoria: ID numerico della Pagina aziendale
                          (da un URN tipo 'urn:li:organization:12345678' -> solo '12345678')
    DATA_DIR              ereditata dall'hub, usata per individuare il file token
Argomenti:
    --server-id <id>      obbligatorio: ID del server nell'hub, per il nome del file token

Avvio (stdio transport):
    python3 -m app.mcp_servers.linkedin_server --server-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import urllib.parse

import httpx

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app import linkedin_oauth

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s linkedin-mcp: %(message)s")
logger = logging.getLogger("linkedin-mcp")

API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_VERSION = "202401"  # versione API "YYYYMM"; LinkedIn la aggiorna periodicamente
_TIMEOUT = 30.0

ORG_ID = os.environ.get("LINKEDIN_ORG_ID", "")
SERVER_ID: str = ""  # impostato in main() da --server-id


class ConfigError(Exception):
    """Configurazione mancante o autorizzazione OAuth non ancora completata."""


class AuthExpiredError(Exception):
    """Il token è scaduto e non è stato possibile rinnovarlo automaticamente."""


def _author_urn() -> str:
    if not ORG_ID:
        raise ConfigError("Configurazione incompleta: manca LINKEDIN_ORG_ID.")
    return f"urn:li:organization:{ORG_ID}"


async def _get_access_token() -> str:
    """Legge il token salvato; se scaduto (o quasi) prova a rinnovarlo al volo, come rete di
    sicurezza oltre allo scheduler periodico dell'hub."""
    tokens = linkedin_oauth.load_tokens(SERVER_ID)
    if not tokens or not tokens.get("access_token"):
        raise ConfigError(
            "Nessuna autorizzazione LinkedIn trovata per questo server: completa il consenso "
            "OAuth dal pulsante 'Autorizza con LinkedIn' nel form di modifica."
        )
    import time
    if time.time() >= (tokens.get("expires_at") or 0) - 60:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise AuthExpiredError("Il token è scaduto e non è disponibile un refresh token: rifai l'autorizzazione.")
        # Serve un oggetto MCPServer per leggere client_id/secret dalle sue env: qui basta un
        # oggetto minimale, i tool non hanno altro accesso allo store dell'hub.
        from app.models import MCPServer
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server is None:
            raise ConfigError("Server non trovato nell'hub (id non valido).")
        try:
            payload = await linkedin_oauth.refresh_access_token(server, refresh_token)
        except Exception as exc:
            raise AuthExpiredError(f"Rinnovo del token fallito: {exc}") from None
        payload.setdefault("refresh_token", refresh_token)
        linkedin_oauth.save_tokens(SERVER_ID, payload)
        tokens = linkedin_oauth.load_tokens(SERVER_ID)
    return tokens["access_token"]


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    token = await _get_access_token()
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=_headers(token), **kwargs)
    if resp.status_code == 401:
        raise AuthExpiredError("LinkedIn ha rifiutato il token (401): l'autorizzazione potrebbe essere revocata, rifai il consenso OAuth.")
    if resp.status_code == 403:
        raise ConfigError(
            "LinkedIn ha risposto 403: probabile mancanza dell'accesso approvato al prodotto "
            "'Community Management API' per questa app, o scope insufficienti."
        )
    return resp


def _urn_encode(urn: str) -> str:
    return urllib.parse.quote(urn, safe="")


async def _crea_post(testo: str, visibilita: str) -> dict:
    if not testo or not testo.strip():
        raise ValueError("crea_post richiede il parametro 'testo'.")
    visibilita = visibilita if visibilita in {"PUBLIC", "LOGGED_IN"} else "PUBLIC"
    body = {
        "author": _author_urn(),
        "commentary": testo,
        "visibility": visibilita,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    resp = await _request("POST", "/posts", json=body)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"LinkedIn ha risposto {resp.status_code} creando il post: {resp.text[:300]}")
    post_id = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
    return {"ok": True, "post_id": post_id, "visibilita": visibilita}


async def _elenco_post(limite: int) -> dict:
    limite = max(1, min(limite or 20, 100))
    params = {"author": _author_urn(), "q": "author", "count": limite, "sortBy": "LAST_MODIFIED"}
    resp = await _request("GET", "/posts", params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn ha risposto {resp.status_code} elencando i post: {resp.text[:300]}")
    data = resp.json()
    elements = data.get("elements", []) if isinstance(data, dict) else []
    return {
        "post": [
            {
                "id": e.get("id"),
                "testo": e.get("commentary"),
                "stato": e.get("lifecycleState"),
                "visibilita": e.get("visibility"),
                "creato_il": e.get("createdAt"),
                "modificato_il": e.get("lastModifiedAt"),
            }
            for e in elements
        ],
    }


async def _elimina_post(post_id: str) -> dict:
    if not post_id:
        raise ValueError("elimina_post richiede il parametro 'post_id' (URN del post).")
    resp = await _request("DELETE", f"/posts/{_urn_encode(post_id)}")
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"LinkedIn ha risposto {resp.status_code} eliminando il post: {resp.text[:300]}")
    return {"ok": True, "post_id": post_id}


async def _statistiche_post(post_id: str) -> dict:
    # NOTA: endpoint meno stabile delle altre API LinkedIn (area statistiche/social actions
    # soggetta a modifiche frequenti). Verificare i nomi dei campi al primo test reale.
    if not post_id:
        raise ValueError("statistiche_post richiede il parametro 'post_id' (URN del post).")
    resp = await _request("GET", f"/socialActions/{_urn_encode(post_id)}")
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn ha risposto {resp.status_code} leggendo le statistiche: {resp.text[:300]}")
    data = resp.json() if resp.status_code != 204 else {}
    likes = (data.get("likesSummary") or {}) if isinstance(data, dict) else {}
    comments = (data.get("commentsSummary") or {}) if isinstance(data, dict) else {}
    return {
        "post_id": post_id,
        "mi_piace_totali": likes.get("totalLikes"),
        "commenti_totali": comments.get("totalFirstLevelComments"),
        "avviso": "Endpoint statistiche non ancora verificato con un'app approvata: se i "
                  "numeri sembrano sbagliati, controlla la risposta grezza con 'Testa connessione' "
                  "o segnalalo per un aggiustamento.",
    }


TOOLS = [
    types.Tool(
        name="crea_post",
        description="Pubblica un nuovo post testuale sulla Pagina aziendale LinkedIn.",
        inputSchema={
            "type": "object",
            "required": ["testo"],
            "properties": {
                "testo": {"type": "string", "description": "Testo del post (supporta a-capo)."},
                "visibilita": {"type": "string", "enum": ["PUBLIC", "LOGGED_IN"], "description": "Default: PUBLIC."},
            },
        },
    ),
    types.Tool(
        name="elenco_post",
        description="Elenca gli ultimi post pubblicati dalla Pagina, più recenti prima.",
        inputSchema={
            "type": "object",
            "properties": {"limite": {"type": "integer", "description": "Numero massimo di post (default 20, max 100)."}},
        },
    ),
    types.Tool(
        name="elimina_post",
        description="Elimina un post dato il suo ID/URN (restituito da crea_post o elenco_post).",
        inputSchema={
            "type": "object",
            "required": ["post_id"],
            "properties": {"post_id": {"type": "string", "description": "URN del post, es. urn:li:share:12345."}},
        },
    ),
    types.Tool(
        name="statistiche_post",
        description="Conteggio like e commenti di un post dato il suo ID/URN.",
        inputSchema={
            "type": "object",
            "required": ["post_id"],
            "properties": {"post_id": {"type": "string", "description": "URN del post."}},
        },
    ),
]

_DISPATCH = {
    "crea_post": lambda a: _crea_post(a.get("testo", ""), a.get("visibilita", "PUBLIC")),
    "elenco_post": lambda a: _elenco_post(a.get("limite")),
    "elimina_post": lambda a: _elimina_post(a.get("post_id", "")),
    "statistiche_post": lambda a: _statistiche_post(a.get("post_id", "")),
}


def _sanitize(message: str) -> str:
    """Rete di sicurezza: se un access/refresh token o il client secret finissero in un
    messaggio d'errore (es. echeggiati da una risposta di LinkedIn), li maschera."""
    secrets_to_mask = []
    tokens = linkedin_oauth.load_tokens(SERVER_ID) if SERVER_ID else None
    if tokens:
        secrets_to_mask.extend([tokens.get("access_token"), tokens.get("refresh_token")])
    if SERVER_ID:
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server:
            secrets_to_mask.append(server.env.get("LINKEDIN_CLIENT_SECRET"))
    for secret in secrets_to_mask:
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


app = Server("linkedin")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=f"Tool sconosciuto: {name}")]
    try:
        result = await handler(arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except (ConfigError, AuthExpiredError) as exc:
        return [types.TextContent(type="text", text=str(exc))]
    except httpx.RequestError as exc:
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s': LinkedIn irraggiungibile: %s", name, safe)
        return [types.TextContent(type="text", text=f"Impossibile raggiungere LinkedIn: {safe}")]
    except Exception as exc:  # noqa: BLE001
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s' errore: %s", name, safe)
        return [types.TextContent(type="text", text=f"Errore: {safe}")]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    global SERVER_ID
    parser = argparse.ArgumentParser(description="Server MCP LinkedIn (Community Management API)")
    parser.add_argument("--server-id", required=True, help="ID del server nell'hub (per individuare il file token)")
    args = parser.parse_args()
    SERVER_ID = args.server_id
    asyncio.run(_run())


if __name__ == "__main__":
    main()
