"""Server MCP per leggere report e gestire campagne Google Ads (account singolo, no MCC),
tramite la Google Ads API (REST).

L'autenticazione è OAuth 2.0: il token non è una credenziale statica, ma letto dal file
salvato dal flusso di autorizzazione dell'hub (app/google_ads_oauth.py, pulsante "Autorizza
con Google" nel form del server) — DATA_DIR/creds/<server_id>-google_ads.json.

IMPORTANTE — Developer Token: ogni chiamata richiede anche l'header 'developer-token'
(DATA_DIR/creds non c'entra, è una credenziale a parte, non OAuth). Un token appena creato ha
livello **Basic**: funziona SOLO su account Google Ads di test (creati da Google Ads →
Strumenti → Centro API → "Crea account di test"), non sul vostro account vero. Per l'account
reale serve il livello **Standard**, che richiede una richiesta approvata da Google (giorni,
non immediata) — vedi sezione dedicata nel README.

Questo server esegue anche operazioni di scrittura (pausa/attiva campagna, cambio budget):
spendono o risparmiano soldi veri sull'account collegato. Non c'è una doppia conferma
applicativa in questi tool: la cautela va esercitata da chi li invoca (conferma con l'utente
prima di eseguirli), come per qualunque azione che muove denaro reale.

Configurazione (env):
    GOOGLE_ADS_CLIENT_ID        obbligatoria: OAuth client ID (Google Cloud Console)
    GOOGLE_ADS_CLIENT_SECRET    obbligatoria: OAuth client secret
    GOOGLE_ADS_DEVELOPER_TOKEN  obbligatoria: Developer Token (Google Ads → Centro API)
    GOOGLE_ADS_CUSTOMER_ID      obbligatoria: ID account Google Ads, 10 cifre senza trattini
    GOOGLE_ADS_API_VERSION      opzionale: versione API (es. 'v23'), default in questo file
    DATA_DIR                    ereditata dall'hub, usata per individuare il file token
Argomenti:
    --server-id <id>      obbligatorio: ID del server nell'hub, per il nome del file token

Avvio (stdio transport):
    python3 -m app.mcp_servers.google_ads_server --server-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time

import httpx

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app import google_ads_oauth

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s googleads-mcp: %(message)s")
logger = logging.getLogger("googleads-mcp")

# Versione "vNN": Google Ads API rilascia una versione al mese e ritira le vecchie dopo circa
# un anno. Sovrascrivibile via env GOOGLE_ADS_API_VERSION senza toccare il codice quando questa
# default va in dismissione (stesso pattern già adottato per LinkedIn e Meta in questo hub).
API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "").strip() or "v22"
API_BASE = f"https://googleads.googleapis.com/{API_VERSION}"
MICROS_PER_UNIT = 1_000_000
_TIMEOUT = 30.0

SERVER_ID: str = ""  # impostato in main() da --server-id


class ConfigError(Exception):
    """Configurazione mancante o autorizzazione OAuth non ancora completata."""


class AuthExpiredError(Exception):
    """Il refresh_token non è più valido: va rifatta l'autorizzazione."""


def _customer_id() -> str:
    raw = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "").strip()
    customer_id = re.sub(r"[^0-9]", "", raw)  # tollera 'XXX-XXX-XXXX' oltre a 'XXXXXXXXXX'
    if not customer_id:
        raise ConfigError("Configurazione incompleta: manca GOOGLE_ADS_CUSTOMER_ID.")
    return customer_id


def _developer_token() -> str:
    token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if not token:
        raise ConfigError("Configurazione incompleta: manca GOOGLE_ADS_DEVELOPER_TOKEN.")
    return token


async def _get_access_token() -> str:
    tokens = google_ads_oauth.load_tokens(SERVER_ID)
    if not tokens or not tokens.get("refresh_token"):
        raise ConfigError(
            "Nessuna autorizzazione Google Ads trovata per questo server: completa il "
            "consenso OAuth dal pulsante 'Autorizza con Google' nel form di modifica."
        )
    if time.time() >= (tokens.get("expires_at") or 0) - 60:
        from app.models import MCPServer
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server is None:
            raise ConfigError("Server non trovato nell'hub (id non valido).")
        try:
            payload = await google_ads_oauth.refresh_access_token(server, tokens["refresh_token"])
        except google_ads_oauth.AuthExpiredError as exc:
            raise AuthExpiredError(str(exc)) from None
        except Exception as exc:
            raise AuthExpiredError(f"Rinnovo del token fallito: {exc}") from None
        payload.setdefault("refresh_token", tokens["refresh_token"])
        google_ads_oauth.save_tokens(SERVER_ID, payload)
        tokens = google_ads_oauth.load_tokens(SERVER_ID)
    return tokens["access_token"]


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "developer-token": _developer_token(),
        "Content-Type": "application/json",
    }


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    error = body.get("error") or {}
    for detail in error.get("details", []):
        for ads_error in detail.get("errors", []):
            msg = ads_error.get("message")
            if msg:
                return msg  # il dettaglio specifico di Google Ads batte il messaggio generico
    return error.get("message") or resp.text[:300]


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    token = await _get_access_token()
    url = f"{API_BASE}/{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=_headers(token), **kwargs)
    if resp.status_code == 401:
        raise AuthExpiredError("Google ha rifiutato il token: l'autorizzazione potrebbe essere revocata, rifai il consenso OAuth.")
    return resp


def _raise_for_error(resp: httpx.Response, azione: str) -> dict:
    if resp.status_code >= 400:
        detail = _extract_error_message(resp)
        if "DEVELOPER_TOKEN" in detail.upper() and "TEST" in detail.upper():
            detail += (
                " — probabile causa: il Developer Token è ancora al livello 'Basic' (funziona "
                "solo su account Google Ads di test). Per l'account reale serve il livello "
                "'Standard', da richiedere su Google Ads → Strumenti → Centro API."
            )
        raise RuntimeError(f"Google Ads ha risposto {resp.status_code} {azione}: {detail}")
    try:
        return resp.json()
    except ValueError:
        return {}


async def _search(query: str, page_size: int = 200) -> list[dict]:
    """Esegue una query GAQL (Google Ads Query Language) e restituisce i risultati della
    prima pagina. Non pagina oltre: adeguato per un uso conversazionale su un account singolo,
    non per estrarre l'intero storico di un account grande."""
    resp = await _request(
        "POST", f"customers/{_customer_id()}/googleAds:search",
        json={"query": query, "pageSize": page_size},
    )
    data = _raise_for_error(resp, "eseguendo la query")
    return data.get("results", [])


def _micros_to_unit(value) -> float | None:
    if value is None:
        return None
    return int(value) / MICROS_PER_UNIT


async def _resolve_campaign(campagna: str) -> dict:
    """'campagna' può essere l'ID numerico o il nome (anche parziale, case-insensitive). Più
    corrispondenze sul nome -> errore con l'elenco, per non agire sulla campagna sbagliata."""
    if not campagna or not campagna.strip():
        raise ValueError("Specifica il parametro 'campagna' (ID o nome).")
    campagna = campagna.strip()
    if campagna.isdigit():
        where = f"campaign.id = {campagna}"
    else:
        safe = campagna.replace('"', '\\"')
        where = f'campaign.name LIKE "%{safe}%"'
    rows = await _search(
        "SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget "
        f"FROM campaign WHERE {where}"
    )
    if not rows:
        raise ValueError(f"Nessuna campagna trovata per '{campagna}'.")
    if len(rows) > 1:
        nomi = ", ".join(f"{r['campaign']['name']} (id {r['campaign']['id']})" for r in rows)
        raise ValueError(f"Più campagne corrispondono a '{campagna}': {nomi}. Usa l'ID per essere univoco.")
    return rows[0]["campaign"]


async def _elenco_campagne(stato: str | None) -> dict:
    where = ""
    if stato:
        where = f" WHERE campaign.status = '{stato.strip().upper()}'"
    rows = await _search(
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign_budget.amount_micros, metrics.cost_micros, metrics.clicks, metrics.impressions, "
        "metrics.conversions FROM campaign" + where + " ORDER BY campaign.id"
    )
    return {
        "campagne": [
            {
                "id": r["campaign"]["id"],
                "nome": r["campaign"]["name"],
                "stato": r["campaign"]["status"],
                "tipo": r["campaign"].get("advertisingChannelType"),
                "budget_giornaliero": _micros_to_unit((r.get("campaignBudget") or {}).get("amountMicros")),
                "costo_ultimi_7gg": _micros_to_unit((r.get("metrics") or {}).get("costMicros")),
                "click_ultimi_7gg": (r.get("metrics") or {}).get("clicks"),
                "impression_ultimi_7gg": (r.get("metrics") or {}).get("impressions"),
                "conversioni_ultimi_7gg": (r.get("metrics") or {}).get("conversions"),
            }
            for r in rows
        ],
        "nota": "Le metriche si riferiscono agli ultimi 7 giorni (intervallo di default della "
                "Google Ads API quando la query non specifica un periodo).",
    }


_LIVELLI = {
    "campagna": ("campaign", "campaign.id, campaign.name"),
    "gruppo_annunci": ("ad_group", "ad_group.id, ad_group.name, campaign.name"),
    "parola_chiave": ("keyword_view", "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, campaign.name, ad_group.name"),
}


async def _report_performance(livello: str, data_da: str | None, data_a: str | None, campagna: str | None, limite: int) -> dict:
    livello = (livello or "campagna").strip().lower()
    if livello not in _LIVELLI:
        raise ValueError(f"'livello' deve essere uno tra: {', '.join(_LIVELLI)}.")
    risorsa, campi_dimensione = _LIVELLI[livello]
    limite = max(1, min(limite or 50, 500))

    condizioni = []
    if data_da and data_a:
        condizioni.append(f"segments.date BETWEEN '{data_da}' AND '{data_a}'")
    elif data_da or data_a:
        raise ValueError("Specifica sia 'data_da' sia 'data_a' (formato YYYY-MM-DD), o nessuno dei due per gli ultimi 7 giorni.")
    if campagna:
        info = await _resolve_campaign(campagna)
        condizioni.append(f"campaign.id = {info['id']}")
    where = (" WHERE " + " AND ".join(condizioni)) if condizioni else ""

    rows = await _search(
        f"SELECT {campi_dimensione}, metrics.impressions, metrics.clicks, metrics.cost_micros, "
        f"metrics.conversions, metrics.conversions_value FROM {risorsa}{where} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limite}"
    )

    def _riga(r: dict) -> dict:
        m = r.get("metrics") or {}
        base = {
            "campagna": (r.get("campaign") or {}).get("name"),
            "impression": m.get("impressions"),
            "click": m.get("clicks"),
            "costo": _micros_to_unit(m.get("costMicros")),
            "conversioni": m.get("conversions"),
            "valore_conversioni": m.get("conversionsValue"),
        }
        if livello == "gruppo_annunci":
            base["gruppo_annunci"] = (r.get("adGroup") or {}).get("name")
        if livello == "parola_chiave":
            crit = (r.get("adGroupCriterion") or {}).get("keyword") or {}
            base["gruppo_annunci"] = (r.get("adGroup") or {}).get("name")
            base["parola_chiave"] = crit.get("text")
            base["tipo_corrispondenza"] = crit.get("matchType")
        return base

    return {
        "livello": livello,
        "periodo": f"{data_da} — {data_a}" if data_da else "ultimi 7 giorni",
        "righe": [_riga(r) for r in rows],
    }


async def _metti_in_pausa_campagna(campagna: str) -> dict:
    return await _cambia_stato_campagna(campagna, "PAUSED")


async def _attiva_campagna(campagna: str) -> dict:
    return await _cambia_stato_campagna(campagna, "ENABLED")


async def _cambia_stato_campagna(campagna: str, nuovo_stato: str) -> dict:
    info = await _resolve_campaign(campagna)
    if info["status"] == nuovo_stato:
        return {"ok": True, "campagna": info["name"], "stato": nuovo_stato, "nota": "Era già in questo stato, nessuna modifica inviata."}
    body = {
        "operations": [{
            "update": {"resourceName": f"customers/{_customer_id()}/campaigns/{info['id']}", "status": nuovo_stato},
            "updateMask": "status",
        }]
    }
    resp = await _request("POST", f"customers/{_customer_id()}/campaigns:mutate", json=body)
    _raise_for_error(resp, "cambiando lo stato della campagna")
    return {"ok": True, "campagna": info["name"], "stato_precedente": info["status"], "stato": nuovo_stato}


async def _modifica_budget_campagna(campagna: str, nuovo_budget_giornaliero: float) -> dict:
    if not nuovo_budget_giornaliero or nuovo_budget_giornaliero <= 0:
        raise ValueError("'nuovo_budget_giornaliero' deve essere un numero maggiore di zero (nella valuta dell'account).")
    info = await _resolve_campaign(campagna)
    budget_resource = info.get("campaignBudget")
    if not budget_resource:
        raise RuntimeError(f"La campagna '{info['name']}' non ha un budget associato leggibile (campagna condivisa o di tipo particolare?).")
    nuovo_micros = round(nuovo_budget_giornaliero * MICROS_PER_UNIT)
    body = {
        "operations": [{
            "update": {"resourceName": budget_resource, "amountMicros": str(nuovo_micros)},
            "updateMask": "amount_micros",
        }]
    }
    resp = await _request("POST", f"customers/{_customer_id()}/campaignBudgets:mutate", json=body)
    _raise_for_error(resp, "cambiando il budget della campagna")
    return {"ok": True, "campagna": info["name"], "nuovo_budget_giornaliero": nuovo_budget_giornaliero}


TOOLS = [
    types.Tool(
        name="elenco_campagne",
        description="Elenca le campagne dell'account con stato, budget giornaliero e metriche degli ultimi 7 giorni.",
        inputSchema={
            "type": "object",
            "properties": {
                "stato": {"type": "string", "description": "Filtra per stato: ENABLED, PAUSED o REMOVED. Default: tutte."},
            },
        },
    ),
    types.Tool(
        name="report_performance",
        description="Report di rendimento (impression, click, costo, conversioni) per campagna, gruppo di annunci o parola chiave.",
        inputSchema={
            "type": "object",
            "properties": {
                "livello": {"type": "string", "enum": ["campagna", "gruppo_annunci", "parola_chiave"], "description": "Dettaglio del report. Default: campagna."},
                "data_da": {"type": "string", "description": "Formato YYYY-MM-DD. Richiede anche 'data_a'. Default: ultimi 7 giorni."},
                "data_a": {"type": "string", "description": "Formato YYYY-MM-DD."},
                "campagna": {"type": "string", "description": "ID o nome (anche parziale) per limitare il report a una sola campagna."},
                "limite": {"type": "integer", "description": "Numero massimo di righe (default 50, max 500)."},
            },
        },
    ),
    types.Tool(
        name="metti_in_pausa_campagna",
        description="Mette in pausa una campagna. Azione reale e immediata sull'account: non chiede conferma, va richiesta da chi invoca il tool prima di eseguirla.",
        inputSchema={
            "type": "object",
            "required": ["campagna"],
            "properties": {"campagna": {"type": "string", "description": "ID o nome (anche parziale) della campagna."}},
        },
    ),
    types.Tool(
        name="attiva_campagna",
        description="Riattiva una campagna in pausa. Azione reale e immediata sull'account (riprende a spendere): non chiede conferma, va richiesta da chi invoca il tool prima di eseguirla.",
        inputSchema={
            "type": "object",
            "required": ["campagna"],
            "properties": {"campagna": {"type": "string", "description": "ID o nome (anche parziale) della campagna."}},
        },
    ),
    types.Tool(
        name="modifica_budget_campagna",
        description="Cambia il budget giornaliero di una campagna (nella valuta dell'account). Azione reale e immediata: non chiede conferma, va richiesta da chi invoca il tool prima di eseguirla.",
        inputSchema={
            "type": "object",
            "required": ["campagna", "nuovo_budget_giornaliero"],
            "properties": {
                "campagna": {"type": "string", "description": "ID o nome (anche parziale) della campagna."},
                "nuovo_budget_giornaliero": {"type": "number", "description": "Nuovo budget giornaliero, nella valuta dell'account (es. 25.50)."},
            },
        },
    ),
]

_DISPATCH = {
    "elenco_campagne": lambda a: _elenco_campagne(a.get("stato")),
    "report_performance": lambda a: _report_performance(a.get("livello", "campagna"), a.get("data_da"), a.get("data_a"), a.get("campagna"), a.get("limite")),
    "metti_in_pausa_campagna": lambda a: _metti_in_pausa_campagna(a.get("campagna", "")),
    "attiva_campagna": lambda a: _attiva_campagna(a.get("campagna", "")),
    "modifica_budget_campagna": lambda a: _modifica_budget_campagna(a.get("campagna", ""), a.get("nuovo_budget_giornaliero")),
}


def _sanitize(message: str) -> str:
    """Rete di sicurezza: se un access/refresh token o il client secret finissero in un
    messaggio d'errore, li maschera."""
    secrets_to_mask = []
    tokens = google_ads_oauth.load_tokens(SERVER_ID) if SERVER_ID else None
    if tokens:
        secrets_to_mask.extend([tokens.get("access_token"), tokens.get("refresh_token")])
    if SERVER_ID:
        from app.storage import store as _store
        server = _store.get_server(SERVER_ID)
        if server:
            secrets_to_mask.append(server.env.get("GOOGLE_ADS_CLIENT_SECRET"))
            secrets_to_mask.append(server.env.get("GOOGLE_ADS_DEVELOPER_TOKEN"))
    for secret in secrets_to_mask:
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


app = Server("google_ads")


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
    except (ConfigError, AuthExpiredError, ValueError) as exc:
        return [types.TextContent(type="text", text=str(exc))]
    except httpx.RequestError as exc:
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s': Google Ads irraggiungibile: %s", name, safe)
        return [types.TextContent(type="text", text=f"Impossibile raggiungere Google Ads: {safe}")]
    except Exception as exc:  # noqa: BLE001
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s' errore: %s", name, safe)
        return [types.TextContent(type="text", text=f"Errore: {safe}")]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    global SERVER_ID
    parser = argparse.ArgumentParser(description="Server MCP Google Ads (account singolo)")
    parser.add_argument("--server-id", required=True, help="ID del server nell'hub (per individuare il file token)")
    args = parser.parse_args()
    SERVER_ID = args.server_id
    asyncio.run(_run())


if __name__ == "__main__":
    main()
