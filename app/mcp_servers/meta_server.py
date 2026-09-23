"""Server MCP per pubblicare e gestire post su Pagine Facebook e account Instagram Business
collegati, tramite la Graph API di Meta.

L'autenticazione è OAuth 2.0: il token non è una credenziale statica, ma letto dal file
salvato dal flusso di autorizzazione dell'hub (app/meta_oauth.py, pulsante "Autorizza con
Meta" nel form del server) — DATA_DIR/creds/<server_id>-meta.json. Un solo consenso OAuth
vale per tutte le Pagine Facebook che l'utente amministra: l'account Instagram collegato a
ogni Pagina viene scoperto in automatico (campo 'instagram_business_account'), non va
configurato a mano.

IMPORTANTE: le chiamate di pubblicazione richiedono che Meta abbia approvato per l'app i
permessi 'pages_manage_posts' e 'instagram_content_publish' (App Review, non immediata). Il
codice è scritto secondo la documentazione ufficiale della Graph API ma NON è stato
verificato con una chiamata reale approvata: in particolare la pubblicazione video su
Instagram (elaborazione asincrona lato Meta) e 'statistiche_post' andrebbero controllati al
primo uso reale.

Configurazione (env):
    META_APP_ID           obbligatoria: ID app da Meta for Developers (scheda Impostazioni di base)
    META_APP_SECRET        obbligatoria: Chiave segreta app
    META_PAGES             opzionale: etichette per le Pagine, 'cascinanet=<page_id>, bman=<page_id>'.
                            Senza etichette si usa direttamente l'ID della Pagina Facebook
                            restituito da elenco_pagine (scoperto in automatico dal consenso OAuth,
                            non va inserito manualmente prima di autorizzare).
    META_API_VERSION       opzionale: versione Graph API (YYYY vNN.0), default in app/meta_oauth.py
    DATA_DIR                ereditata dall'hub, usata per individuare il file token
Argomenti:
    --server-id <id>      obbligatorio: ID del server nell'hub, per il nome del file token

Avvio (stdio transport):
    python3 -m app.mcp_servers.meta_server --server-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time

import httpx

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app import meta_oauth

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s meta-mcp: %(message)s")
logger = logging.getLogger("meta-mcp")

GRAPH_BASE = meta_oauth.GRAPH_BASE
_TIMEOUT = 30.0

SERVER_ID: str = ""  # impostato in main() da --server-id


class ConfigError(Exception):
    """Configurazione mancante o autorizzazione OAuth non ancora completata."""


class AuthExpiredError(Exception):
    """Il token utente è scaduto e non è stato possibile rinnovarlo automaticamente."""


def _parse_labels(raw: str) -> dict[str, str]:
    """'a=1, b=2' -> {'a': '1', 'b': '2'} (etichette in minuscolo). Stringa vuota -> {}."""
    labels: dict[str, str] = {}
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        label, page_id = item.split("=", 1)
        labels[label.strip().lower()] = page_id.strip()
    return labels


LABELS = _parse_labels(os.environ.get("META_PAGES", ""))


async def _current_tokens() -> dict:
    tokens = meta_oauth.load_tokens(SERVER_ID)
    if not tokens or not tokens.get("user_access_token"):
        raise ConfigError(
            "Nessuna autorizzazione Meta trovata per questo server: completa il consenso "
            "OAuth dal pulsante 'Autorizza con Meta' nel form di modifica."
        )
    if time.time() >= (tokens.get("expires_at") or 0) - 60:
        # Rete di sicurezza oltre allo scheduler periodico dell'hub: il token utente Meta non
        # ha un refresh_token separato, si ri-estende quello corrente finché è ancora valido.
        from app.models import MCPServer
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server is None:
            raise ConfigError("Server non trovato nell'hub (id non valido).")
        try:
            long_lived = await meta_oauth.extend_token(server, tokens["user_access_token"])
            pages = await meta_oauth.fetch_pages(long_lived["access_token"])
            meta_oauth.save_tokens(SERVER_ID, long_lived["access_token"], long_lived.get("expires_in", 0), pages)
        except Exception as exc:
            raise AuthExpiredError(
                f"Il token è scaduto e non è stato possibile rinnovarlo: {exc}. Rifai l'autorizzazione."
            ) from None
        tokens = meta_oauth.load_tokens(SERVER_ID)
    return tokens


def _pages_hint(pages: dict) -> str:
    named = [f"{label} ({pid})" for label, pid in LABELS.items() if pid in pages]
    unnamed = [pid for pid in pages if pid not in LABELS.values()]
    parts = named + unnamed
    return ", ".join(parts) or "nessuna (autorizza prima con il pulsante 'Autorizza con Meta')"


async def _resolve_page(pagina: str | None) -> tuple[str, dict]:
    """(page_id, dati pagina) su cui operare. Con più Pagine autorizzate 'pagina' è
    obbligatoria: meglio un errore che un post pubblicato sulla Pagina sbagliata."""
    tokens = await _current_tokens()
    pages = tokens.get("pages") or {}
    if not pages:
        raise ConfigError("Nessuna Pagina Facebook trovata per l'utente autorizzato.")
    pagina = (pagina or "").strip()
    if not pagina:
        if len(pages) > 1:
            raise ConfigError(f"Sono configurate più Pagine: indica il parametro 'pagina'. Disponibili: {_pages_hint(pages)}.")
        page_id = next(iter(pages))
    else:
        page_id = LABELS.get(pagina.lower(), pagina)
        if page_id not in pages:
            raise ConfigError(f"Pagina '{pagina}' non trovata tra quelle autorizzate. Disponibili: {_pages_hint(pages)}.")
    return page_id, pages[page_id]


async def _resolve_instagram(pagina: str | None) -> tuple[str, dict]:
    page_id, page = await _resolve_page(pagina)
    ig_id = page.get("instagram_id")
    if not ig_id:
        raise ConfigError(
            f"La Pagina '{page.get('name') or page_id}' non ha un account Instagram Business/Creator "
            "collegato (o il permesso 'instagram_basic' non è ancora approvato per l'app)."
        )
    return ig_id, page


async def _graph_request(method: str, path: str, access_token: str, **kwargs) -> httpx.Response:
    params = kwargs.pop("params", {}) or {}
    params["access_token"] = access_token
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, f"{GRAPH_BASE}/{path}", params=params, **kwargs)
    if resp.status_code == 401 or (resp.status_code == 400 and "OAuthException" in resp.text):
        raise AuthExpiredError("Meta ha rifiutato il token: l'autorizzazione potrebbe essere revocata, rifai il consenso OAuth.")
    return resp


def _raise_for_graph_error(resp: httpx.Response, azione: str) -> dict:
    if resp.status_code >= 400:
        try:
            detail = (resp.json().get("error") or {}).get("message") or resp.text[:300]
        except ValueError:
            detail = resp.text[:300]
        raise RuntimeError(f"Meta ha risposto {resp.status_code} {azione}: {detail}")
    try:
        return resp.json()
    except ValueError:
        return {}


async def _elenco_pagine() -> dict:
    tokens = await _current_tokens()
    pages = tokens.get("pages") or {}
    labels_by_id = {pid: label for label, pid in LABELS.items()}
    return {
        "pagine": [
            {
                "id": pid,
                "etichetta": labels_by_id.get(pid),
                "nome": p.get("name"),
                "instagram_id": p.get("instagram_id"),
                "instagram_username": p.get("instagram_username"),
            }
            for pid, p in pages.items()
        ],
        "token_scade_il": tokens.get("expires_at"),
    }


async def _crea_post_facebook(testo: str, pagina: str | None, link: str | None, immagine_url: str | None) -> dict:
    if not testo or not testo.strip():
        raise ValueError("crea_post_facebook richiede il parametro 'testo'.")
    page_id, page = await _resolve_page(pagina)
    token = page["access_token"]
    if immagine_url:
        resp = await _graph_request("POST", f"{page_id}/photos", token,
                                     data={"caption": testo, "url": immagine_url, "published": "true"})
        data = _raise_for_graph_error(resp, "pubblicando la foto")
        post_id = data.get("post_id") or data.get("id")
    else:
        body = {"message": testo}
        if link:
            body["link"] = link
        resp = await _graph_request("POST", f"{page_id}/feed", token, data=body)
        data = _raise_for_graph_error(resp, "creando il post")
        post_id = data.get("id")
    return {"ok": True, "post_id": post_id, "pagina": page.get("name") or page_id}


async def _elenco_post_facebook(pagina: str | None, limite: int) -> dict:
    limite = max(1, min(limite or 20, 100))
    page_id, page = await _resolve_page(pagina)
    resp = await _graph_request("GET", f"{page_id}/posts", page["access_token"],
                                 params={"fields": "id,message,created_time,permalink_url", "limit": limite})
    data = _raise_for_graph_error(resp, "elencando i post")
    return {
        "pagina": page.get("name") or page_id,
        "post": [
            {"id": e.get("id"), "testo": e.get("message"), "creato_il": e.get("created_time"),
             "url": e.get("permalink_url")}
            for e in data.get("data", [])
        ],
    }


async def _crea_post_instagram(didascalia: str, pagina: str | None, immagine_url: str | None, video_url: str | None) -> dict:
    if not immagine_url and not video_url:
        raise ValueError("crea_post_instagram richiede 'immagine_url' o 'video_url' (Instagram non pubblica post di solo testo).")
    ig_id, page = await _resolve_instagram(pagina)
    token = page["access_token"]
    body: dict = {"caption": didascalia or ""}
    if video_url:
        body["video_url"] = video_url
        body["media_type"] = "REELS"
    else:
        body["image_url"] = immagine_url
    resp = await _graph_request("POST", f"{ig_id}/media", token, data=body)
    data = _raise_for_graph_error(resp, "caricando il media")
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError("Meta non ha restituito un creation_id per il media caricato.")

    if video_url:
        # I video (Reels compresi) vengono elaborati in modo asincrono lato Meta: bisogna
        # attendere status_code=FINISHED prima di poter pubblicare. Non verificato dal vivo:
        # per video di grandi dimensioni l'elaborazione può superare questo timeout, nel qual
        # caso il tool restituisce un errore invece di pubblicare un video non pronto.
        for _ in range(10):
            status_resp = await _graph_request("GET", creation_id, token, params={"fields": "status_code"})
            status = _raise_for_graph_error(status_resp, "controllando lo stato del video").get("status_code")
            if status == "FINISHED":
                break
            if status == "ERROR":
                raise RuntimeError("Meta ha segnalato un errore elaborando il video caricato.")
            await asyncio.sleep(3)
        else:
            raise RuntimeError(
                "Il video non è ancora pronto dopo l'attesa massima: riprova più tardi con "
                "'elenco_post_instagram', il media potrebbe comunque completarsi ed essere pubblicabile."
            )

    publish_resp = await _graph_request("POST", f"{ig_id}/media_publish", token, data={"creation_id": creation_id})
    published = _raise_for_graph_error(publish_resp, "pubblicando il media")
    return {"ok": True, "post_id": published.get("id"), "pagina": page.get("name")}


async def _elenco_post_instagram(pagina: str | None, limite: int) -> dict:
    limite = max(1, min(limite or 20, 100))
    ig_id, page = await _resolve_instagram(pagina)
    resp = await _graph_request("GET", f"{ig_id}/media", page["access_token"],
                                 params={"fields": "id,caption,media_type,media_url,permalink,timestamp", "limit": limite})
    data = _raise_for_graph_error(resp, "elencando i media")
    return {
        "pagina": page.get("name"),
        "instagram_username": page.get("instagram_username"),
        "post": [
            {"id": e.get("id"), "didascalia": e.get("caption"), "tipo": e.get("media_type"),
             "url": e.get("permalink"), "pubblicato_il": e.get("timestamp")}
            for e in data.get("data", [])
        ],
    }


async def _elimina_post(post_id: str, pagina: str | None) -> dict:
    # DELETE /{id} è generico nella Graph API: serve solo un access_token con i permessi
    # giusti. Per un post Facebook basta il page access token; per un media Instagram
    # servirebbe il permesso 'instagram_manage_contents' — non verificato dal vivo, la
    # documentazione ufficiale di Meta su questo punto è meno chiara che per la pubblicazione.
    if not post_id:
        raise ValueError("elimina_post richiede il parametro 'post_id'.")
    _page_id, page = await _resolve_page(pagina)
    resp = await _graph_request("DELETE", post_id, page["access_token"])
    data = _raise_for_graph_error(resp, "eliminando il post")
    if data.get("success") is False:
        raise RuntimeError("Meta ha rifiutato l'eliminazione del post (permessi insufficienti o post non eliminabile via API).")
    return {"ok": True, "post_id": post_id}


async def _statistiche_post(post_id: str, pagina: str | None) -> dict:
    # NOTA: come per elimina_post, non verificato dal vivo. Prova prima i campi per un post
    # Facebook; se falliscono (es. è un media Instagram), riprova con i campi Instagram.
    if not post_id:
        raise ValueError("statistiche_post richiede il parametro 'post_id'.")
    _page_id, page = await _resolve_page(pagina)
    token = page["access_token"]
    resp = await _graph_request("GET", post_id, token,
                                 params={"fields": "reactions.summary(true),comments.summary(true),shares"})
    if resp.status_code < 400:
        data = resp.json()
        return {
            "post_id": post_id,
            "mi_piace_totali": (data.get("reactions", {}).get("summary") or {}).get("total_count"),
            "commenti_totali": (data.get("comments", {}).get("summary") or {}).get("total_count"),
            "condivisioni": (data.get("shares") or {}).get("count"),
        }
    ig_resp = await _graph_request("GET", post_id, token, params={"fields": "like_count,comments_count"})
    data = _raise_for_graph_error(ig_resp, "leggendo le statistiche")
    return {
        "post_id": post_id,
        "mi_piace_totali": data.get("like_count"),
        "commenti_totali": data.get("comments_count"),
        "avviso": "Endpoint statistiche non ancora verificato con un'app approvata: se i "
                  "numeri sembrano sbagliati, segnalalo per un aggiustamento.",
    }


TOOLS = [
    types.Tool(
        name="crea_post_facebook",
        description="Pubblica un nuovo post su una Pagina Facebook (testo, opzionalmente con link o immagine).",
        inputSchema={
            "type": "object",
            "required": ["testo"],
            "properties": {
                "testo": {"type": "string", "description": "Testo del post."},
                "link": {"type": "string", "description": "URL da allegare (Facebook genera l'anteprima). Ignorato se 'immagine_url' è impostato."},
                "immagine_url": {"type": "string", "description": "URL pubblico di un'immagine da pubblicare col testo come didascalia."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina (vedi elenco_pagine). Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
    types.Tool(
        name="elenco_post_facebook",
        description="Elenca gli ultimi post pubblicati su una Pagina Facebook, più recenti prima.",
        inputSchema={
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "description": "Numero massimo di post (default 20, max 100)."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina. Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
    types.Tool(
        name="crea_post_instagram",
        description="Pubblica un nuovo post sull'account Instagram Business collegato a una Pagina Facebook. "
                    "Richiede un'immagine o un video (Instagram non supporta post di solo testo).",
        inputSchema={
            "type": "object",
            "properties": {
                "didascalia": {"type": "string", "description": "Testo della didascalia (supporta hashtag)."},
                "immagine_url": {"type": "string", "description": "URL pubblico dell'immagine da pubblicare."},
                "video_url": {"type": "string", "description": "URL pubblico del video da pubblicare (Reel). Alternativo a immagine_url."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina Facebook collegata. Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
    types.Tool(
        name="elenco_post_instagram",
        description="Elenca gli ultimi media pubblicati sull'account Instagram collegato a una Pagina, più recenti prima.",
        inputSchema={
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "description": "Numero massimo di post (default 20, max 100)."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina Facebook collegata. Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
    types.Tool(
        name="elenco_pagine",
        description="Elenca le Pagine Facebook autorizzate (con etichetta, se configurata) e l'account Instagram "
                    "collegato a ciascuna, se presente.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="elimina_post",
        description="Elimina un post/media Facebook o Instagram dato il suo ID.",
        inputSchema={
            "type": "object",
            "required": ["post_id"],
            "properties": {
                "post_id": {"type": "string", "description": "ID del post/media."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina proprietaria (per il token da usare). Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
    types.Tool(
        name="statistiche_post",
        description="Reazioni/commenti (Facebook) o like/commenti (Instagram) di un post dato il suo ID.",
        inputSchema={
            "type": "object",
            "required": ["post_id"],
            "properties": {
                "post_id": {"type": "string", "description": "ID del post/media."},
                "pagina": {"type": "string", "description": "Etichetta o ID della Pagina proprietaria (per il token da usare). Obbligatoria se sono configurate più Pagine."},
            },
        },
    ),
]

_DISPATCH = {
    "crea_post_facebook": lambda a: _crea_post_facebook(a.get("testo", ""), a.get("pagina"), a.get("link"), a.get("immagine_url")),
    "elenco_post_facebook": lambda a: _elenco_post_facebook(a.get("pagina"), a.get("limite")),
    "crea_post_instagram": lambda a: _crea_post_instagram(a.get("didascalia", ""), a.get("pagina"), a.get("immagine_url"), a.get("video_url")),
    "elenco_post_instagram": lambda a: _elenco_post_instagram(a.get("pagina"), a.get("limite")),
    "elenco_pagine": lambda a: _elenco_pagine(),
    "elimina_post": lambda a: _elimina_post(a.get("post_id", ""), a.get("pagina")),
    "statistiche_post": lambda a: _statistiche_post(a.get("post_id", ""), a.get("pagina")),
}


def _sanitize(message: str) -> str:
    """Rete di sicurezza: se un access token o il client secret finissero in un messaggio
    d'errore (es. echeggiati da una risposta di Meta), li maschera."""
    secrets_to_mask = []
    tokens = meta_oauth.load_tokens(SERVER_ID) if SERVER_ID else None
    if tokens:
        secrets_to_mask.append(tokens.get("user_access_token"))
        for page in (tokens.get("pages") or {}).values():
            secrets_to_mask.append(page.get("access_token"))
    if SERVER_ID:
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server:
            secrets_to_mask.append(server.env.get("META_APP_SECRET"))
    for secret in secrets_to_mask:
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


app = Server("meta")


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
        logger.warning("Tool '%s': Meta irraggiungibile: %s", name, safe)
        return [types.TextContent(type="text", text=f"Impossibile raggiungere Meta: {safe}")]
    except Exception as exc:  # noqa: BLE001
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s' errore: %s", name, safe)
        return [types.TextContent(type="text", text=f"Errore: {safe}")]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    global SERVER_ID
    parser = argparse.ArgumentParser(description="Server MCP Meta (Facebook Pages + Instagram Business, Graph API)")
    parser.add_argument("--server-id", required=True, help="ID del server nell'hub (per individuare il file token)")
    args = parser.parse_args()
    SERVER_ID = args.server_id
    asyncio.run(_run())


if __name__ == "__main__":
    main()
