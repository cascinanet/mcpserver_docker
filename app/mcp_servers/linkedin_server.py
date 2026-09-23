"""Server MCP per pubblicare e gestire post sulla Pagina aziendale LinkedIn.

Usa la Community Management API di LinkedIn (namespace /rest/posts). L'autenticazione è
OAuth 2.0: l'access token non è passato come credenziale statica, ma letto dal file salvato
dal flusso di autorizzazione dell'hub (app/linkedin_oauth.py, pulsante "Autorizza con
LinkedIn" nel form del server) — DATA_DIR/creds/<server_id>-linkedin.json.

IMPORTANTE: le chiamate a /rest/posts richiedono che LinkedIn abbia approvato per l'app
l'accesso al prodotto "Community Management API". Alcuni nomi di campo (in particolare per
'statistiche_post', area meno stabile delle API LinkedIn) potrebbero richiedere aggiustamenti
al primo test dal vivo.

Configurazione (env):
    LINKEDIN_ORG_ID       obbligatoria: Pagina/e aziendali. Una sola: l'ID numerico (da un URN
                          tipo 'urn:li:organization:12345678' -> '12345678'). Più pagine con
                          etichetta: 'cascinanet=12345678, pixelio=87654321'. Il token OAuth è
                          dell'utente, quindi vale per tutte le pagine che amministra.
    LINKEDIN_API_VERSION  opzionale: header LinkedIn-Version (YYYYMM), default in LINKEDIN_VERSION
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
# Versione API "YYYYMM": LinkedIn pubblica una versione al mese e ognuna resta attiva circa
# un anno, poi le chiamate falliscono con 426. Sovrascrivibile via env LINKEDIN_API_VERSION
# senza toccare il codice quando questa default va in dismissione.
LINKEDIN_VERSION = os.environ.get("LINKEDIN_API_VERSION", "").strip() or "202606"
_TIMEOUT = 30.0

SERVER_ID: str = ""  # impostato in main() da --server-id


class ConfigError(Exception):
    """Configurazione mancante o autorizzazione OAuth non ancora completata."""


class AuthExpiredError(Exception):
    """Il token è scaduto e non è stato possibile rinnovarlo automaticamente."""


_ORG_PREFIX = "urn:li:organization:"


def _clean_org_id(value: str) -> str:
    value = value.strip()
    return value[len(_ORG_PREFIX):] if value.startswith(_ORG_PREFIX) else value


def _parse_pages(raw: str) -> dict[str, str]:
    """'12345' -> {'principale': '12345'}; 'a=1, b=2' -> {'a': '1', 'b': '2'} (etichette in minuscolo)."""
    pages: dict[str, str] = {}
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            label, org_id = item.split("=", 1)
            pages[label.strip().lower()] = _clean_org_id(org_id)
        else:
            pages["principale" if not pages else _clean_org_id(item)] = _clean_org_id(item)
    return pages


PAGES = _parse_pages(os.environ.get("LINKEDIN_ORG_ID", ""))


def _pages_hint() -> str:
    return ", ".join(f"{label} ({org_id})" for label, org_id in PAGES.items()) or "nessuna"


def _author_urn(pagina: str | None) -> str:
    """URN della Pagina su cui operare. Con più pagine configurate 'pagina' è obbligatoria: meglio
    un errore che un post pubblicato sulla Pagina sbagliata."""
    pagina = (pagina or "").strip()
    if not pagina:
        if not PAGES:
            raise ConfigError("Configurazione incompleta: manca LINKEDIN_ORG_ID (ID della Pagina aziendale).")
        if len(PAGES) > 1:
            raise ConfigError(
                f"Sono configurate più Pagine: indica il parametro 'pagina'. Disponibili: {_pages_hint()}."
            )
        return _ORG_PREFIX + next(iter(PAGES.values()))
    org_id = PAGES.get(pagina.lower())
    if org_id is None:
        candidate = _clean_org_id(pagina)
        if not candidate.isdigit():
            raise ConfigError(f"Pagina '{pagina}' non trovata. Disponibili: {_pages_hint()}.")
        # ID numerico non configurato: consentito, i permessi li verifica comunque LinkedIn.
        org_id = candidate
    return _ORG_PREFIX + org_id


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


def _headers(token: str, extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


async def _request(method: str, path: str, headers: dict | None = None, **kwargs) -> httpx.Response:
    token = await _get_access_token()
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=_headers(token, headers), **kwargs)
    if resp.status_code == 426:
        raise ConfigError(
            f"LinkedIn ha risposto 426: la versione API '{LINKEDIN_VERSION}' non è più attiva. "
            "Imposta nelle Env del server LINKEDIN_API_VERSION con una versione recente (YYYYMM)."
        )
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


async def _crea_post(testo: str, visibilita: str, pagina: str | None) -> dict:
    if not testo or not testo.strip():
        raise ValueError("crea_post richiede il parametro 'testo'.")
    visibilita = visibilita if visibilita in {"PUBLIC", "LOGGED_IN"} else "PUBLIC"
    author = _author_urn(pagina)
    body = {
        "author": author,
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
    return {"ok": True, "post_id": post_id, "pagina": author, "visibilita": visibilita}


async def _elenco_post(limite: int, pagina: str | None) -> dict:
    limite = max(1, min(limite or 20, 100))
    # Rest.li 2.0 vuole l'URN codificato nella query (':' -> %3A): costruita a mano perché
    # httpx lascerebbe i ':' in chiaro.
    author = _author_urn(pagina)
    query = f"q=author&author={_urn_encode(author)}&count={limite}&sortBy=LAST_MODIFIED"
    resp = await _request("GET", f"/posts?{query}", headers={"X-RestLi-Method": "FINDER"})
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn ha risposto {resp.status_code} elencando i post: {resp.text[:300]}")
    data = resp.json()
    elements = data.get("elements", []) if isinstance(data, dict) else []
    return {
        "pagina": author,
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


async def _elenco_pagine() -> dict:
    """Pagine configurate + (se LinkedIn lo consente) tutte quelle che l'utente autorizzato
    amministra, così gli ID si scoprono senza cercarli a mano."""
    configured = [{"etichetta": label, "id": org_id} for label, org_id in PAGES.items()]
    result: dict = {"configurate": configured}
    resp = await _request(
        "GET", "/organizationAcls?q=roleAssignee&role=ADMINISTRATOR&state=APPROVED",
        headers={"X-RestLi-Method": "FINDER"},
    )
    if resp.status_code >= 400:
        result["nota"] = f"Elenco delle Pagine amministrate non disponibile (LinkedIn {resp.status_code})."
        return result
    labels_by_id = {org_id: label for label, org_id in PAGES.items()}
    amministrate = []
    for element in (resp.json() or {}).get("elements", []):
        urn = element.get("organization") or element.get("organizationalTarget") or ""
        org_id = _clean_org_id(urn)
        if not org_id:
            continue
        info = {"id": org_id, "etichetta": labels_by_id.get(org_id)}
        org_resp = await _request("GET", f"/organizations/{org_id}")
        if org_resp.status_code < 400:
            org = org_resp.json() or {}
            info["nome"] = org.get("localizedName")
            info["vanity_name"] = org.get("vanityName")
        amministrate.append(info)
    result["amministrate"] = amministrate
    return result


_PAGINA_PROP = {
    "type": "string",
    "description": "Etichetta o ID della Pagina (vedi elenco_pagine). Obbligatoria se sono configurate più Pagine.",
}

TOOLS = [
    types.Tool(
        name="crea_post",
        description="Pubblica un nuovo post testuale su una Pagina aziendale LinkedIn.",
        inputSchema={
            "type": "object",
            "required": ["testo"],
            "properties": {
                "testo": {"type": "string", "description": "Testo del post (supporta a-capo)."},
                "visibilita": {"type": "string", "enum": ["PUBLIC", "LOGGED_IN"], "description": "Default: PUBLIC."},
                "pagina": _PAGINA_PROP,
            },
        },
    ),
    types.Tool(
        name="elenco_post",
        description="Elenca gli ultimi post pubblicati dalla Pagina, più recenti prima.",
        inputSchema={
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "description": "Numero massimo di post (default 20, max 100)."},
                "pagina": _PAGINA_PROP,
            },
        },
    ),
    types.Tool(
        name="elenco_pagine",
        description="Elenca le Pagine aziendali configurate (con etichetta) e quelle che l'utente "
                    "autorizzato amministra su LinkedIn, con nome e ID.",
        inputSchema={"type": "object", "properties": {}},
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
    "crea_post": lambda a: _crea_post(a.get("testo", ""), a.get("visibilita", "PUBLIC"), a.get("pagina")),
    "elenco_post": lambda a: _elenco_post(a.get("limite"), a.get("pagina")),
    "elenco_pagine": lambda a: _elenco_pagine(),
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
