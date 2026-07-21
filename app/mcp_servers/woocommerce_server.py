"""Server MCP per il reporting WooCommerce (vendite, prodotti, coupon, clienti, ordini).

Due namespace REST distinti, due credenziali distinte:

  wc/v3        -> Basic Auth con consumer key/secret (WooCommerce -> Impostazioni ->
                  Avanzate -> REST API). Copre ordini, vendite totali, top seller per
                  quantità: nessun fatturato per prodotto, nessun dettaglio coupon.
  wc-analytics -> Basic Auth con un utente WordPress + Application Password (WordPress
                  -> Utenti -> Profilo -> "Password per le applicazioni"). Sblocca
                  fatturato per prodotto, coupon dettagliati, spesa per cliente.

Se manca la seconda credenziale i tool che ne hanno bisogno non falliscono: tornano un
dato ridotto (es. solo quantità, non fatturato) con scritto chiaramente cosa manca.

Configurazione (env):
    WC_SITE_URL       obbligatoria, es. https://esempio.it (senza slash finale)
    WC_CONSUMER_KEY   obbligatoria
    WC_CONSUMER_SECRET obbligatoria
    WC_APP_USER       opzionale, utente WordPress amministratore
    WC_APP_PASSWORD   opzionale, Application Password dello stesso utente

Avvio (stdio transport):
    python3 -m app.mcp_servers.woocommerce_server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, timedelta

import httpx

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s woocommerce-mcp: %(message)s")
logger = logging.getLogger("woocommerce-mcp")

SITE_URL = os.environ.get("WC_SITE_URL", "").rstrip("/")
CONSUMER_KEY = os.environ.get("WC_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("WC_CONSUMER_SECRET", "")
APP_USER = os.environ.get("WC_APP_USER", "")
APP_PASSWORD = os.environ.get("WC_APP_PASSWORD", "")

_TIMEOUT = 30.0
_ANALYTICS_HINT = (
    "Dato ridotto: manca WC_APP_PASSWORD (Application Password di un utente Amministratore "
    "WordPress). Con solo consumer key/secret il namespace wc-analytics non è raggiungibile."
)


class ConfigError(Exception):
    """Configurazione mancante (sito o credenziali)."""


def _has_analytics_auth() -> bool:
    return bool(APP_USER and APP_PASSWORD)


def _require_base_config() -> None:
    if not SITE_URL or not CONSUMER_KEY or not CONSUMER_SECRET:
        raise ConfigError("Configurazione incompleta: servono WC_SITE_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET.")


def _parse(resp: httpx.Response) -> object:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        detail = data.get("message", "") if isinstance(data, dict) else ""
        raise RuntimeError(f"WooCommerce ha risposto {resp.status_code}{': ' + detail if detail else ''}")
    return data


async def _get_v3(path: str, params: dict | None = None) -> object:
    """GET autenticato su wc/v3 con consumer key/secret."""
    _require_base_config()
    url = f"{SITE_URL}/wp-json/wc/v3/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=(CONSUMER_KEY, CONSUMER_SECRET)) as client:
        resp = await client.get(url, params=params or {})
    return _parse(resp)


async def _get_analytics(path: str, params: dict | None = None) -> object:
    """GET autenticato su wc-analytics con Application Password (utente WP)."""
    _require_base_config()
    if not _has_analytics_auth():
        raise ConfigError(_ANALYTICS_HINT)
    url = f"{SITE_URL}/wp-json/wc-analytics/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=(APP_USER, APP_PASSWORD)) as client:
        resp = await client.get(url, params=params or {})
    return _parse(resp)


def _period_to_range(periodo: str) -> tuple[str, str]:
    """Converte un periodo (week/month/last_month/year) in after/before ISO per wc-analytics,
    che a differenza dei report legacy non accetta la scorciatoia 'period'."""
    today = date.today()
    if periodo == "month":
        start = today.replace(day=1)
    elif periodo == "last_month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        start = last_month_end.replace(day=1)
        today = last_month_end
    elif periodo == "year":
        start = today.replace(month=1, day=1)
    else:
        start = today - timedelta(days=today.weekday())
    return f"{start.isoformat()}T00:00:00", f"{today.isoformat()}T23:59:59"


# --- implementazione dei 6 tool -------------------------------------------------


async def _panoramica_vendite(periodo: str, data_inizio: str, data_fine: str) -> dict:
    params: dict = {}
    if data_inizio or data_fine:
        if data_inizio:
            params["date_min"] = data_inizio
        if data_fine:
            params["date_max"] = data_fine
    else:
        params["period"] = periodo or "week"
    data = await _get_v3("reports/sales", params)
    rows = data if isinstance(data, list) else []
    row = rows[0] if rows else {}
    return {
        "periodo": params.get("period", f"{data_inizio or '?'} -> {data_fine or '?'}"),
        "fatturato_totale": row.get("total_sales"),
        "fatturato_netto": row.get("net_sales"),
        "tasse_totali": row.get("total_tax"),
        "sconti_totali": row.get("total_discount"),
        "ordini": row.get("total_orders"),
        "prodotti_venduti": row.get("total_items"),
        "clienti": row.get("total_customers"),
    }


async def _top_prodotti(periodo: str, data_inizio: str, data_fine: str, limite: int) -> dict:
    limite = max(1, min(limite or 10, 50))
    if _has_analytics_auth():
        params: dict = {"orderby": "items_sold", "order": "desc", "per_page": limite}
        if data_inizio or data_fine:
            if data_inizio:
                params["after"] = f"{data_inizio}T00:00:00"
            if data_fine:
                params["before"] = f"{data_fine}T23:59:59"
        else:
            params["after"], params["before"] = _period_to_range(periodo or "week")
        # extended_info=1: senza questo l'API non include nome/SKU, solo gli ID.
        params["extended_info"] = "1"
        data = await _get_analytics("reports/products", params)
        items = data if isinstance(data, list) else []
        return {
            "fonte": "wc-analytics (con fatturato)",
            "prodotti": [
                {
                    "id": i.get("product_id"),
                    "nome": (i.get("extended_info") or {}).get("name"),
                    "sku": (i.get("extended_info") or {}).get("sku"),
                    "quantita_venduta": i.get("items_sold"),
                    "fatturato": i.get("net_revenue"),
                }
                for i in items
            ],
        }
    params = {}
    if data_inizio or data_fine:
        if data_inizio:
            params["date_min"] = data_inizio
        if data_fine:
            params["date_max"] = data_fine
    else:
        params["period"] = periodo or "week"
    data = await _get_v3("reports/top_sellers", params)
    items = data if isinstance(data, list) else []
    return {
        "fonte": "wc/v3 legacy (solo quantità)",
        "avviso": _ANALYTICS_HINT,
        # Il report legacy usa 'title', non 'name'.
        "prodotti": [{"id": i.get("product_id"), "nome": i.get("title"), "quantita_venduta": i.get("quantity")} for i in items],
    }


async def _report_coupon(data_inizio: str, data_fine: str, limite: int) -> dict:
    limite = max(1, min(limite or 20, 100))
    if _has_analytics_auth():
        params: dict = {"per_page": limite}
        if data_inizio:
            params["after"] = f"{data_inizio}T00:00:00"
        if data_fine:
            params["before"] = f"{data_fine}T23:59:59"
        # extended_info=1: senza questo l'API non include il codice coupon, solo l'ID.
        params["extended_info"] = "1"
        data = await _get_analytics("reports/coupons", params)
        items = data if isinstance(data, list) else []
        return {
            "fonte": "wc-analytics (dettaglio per coupon)",
            "coupon": [
                {
                    "id": i.get("coupon_id"),
                    "codice": (i.get("extended_info") or {}).get("code"),
                    "ordini": i.get("orders_count"),
                    "sconto_totale": i.get("amount"),
                }
                for i in items
            ],
        }
    data = await _get_v3("reports/coupons/totals")
    items = data if isinstance(data, list) else []
    return {
        "fonte": "wc/v3 legacy (aggregato, poco dettagliato)",
        "avviso": _ANALYTICS_HINT,
        "coupon": items,
    }


async def _andamento_temporale(data_inizio: str, data_fine: str, intervallo: str) -> dict:
    if not data_inizio or not data_fine:
        raise ValueError("andamento_temporale richiede sia data_inizio che data_fine (formato AAAA-MM-GG).")
    intervallo = intervallo if intervallo in {"day", "week", "month", "quarter", "year"} else "day"
    if _has_analytics_auth():
        params = {"after": f"{data_inizio}T00:00:00", "before": f"{data_fine}T23:59:59", "interval": intervallo}
        data = await _get_analytics("reports/revenue/stats", params)
        intervals = (data or {}).get("intervals", []) if isinstance(data, dict) else []
        return {
            "fonte": "wc-analytics (serie storica)",
            "intervallo": intervallo,
            "punti": [
                {
                    "periodo": i.get("date_start"),
                    "fatturato": (i.get("subtotals") or {}).get("total_sales"),
                    "ordini": (i.get("subtotals") or {}).get("orders_count"),
                }
                for i in intervals
            ],
        }
    data = await _get_v3("reports/sales", {"date_min": data_inizio, "date_max": data_fine})
    rows = data if isinstance(data, list) else []
    row = rows[0] if rows else {}
    return {
        "fonte": "wc/v3 legacy (solo totale, nessuna serie storica)",
        "avviso": _ANALYTICS_HINT,
        "totale_periodo": {"fatturato": row.get("total_sales"), "ordini": row.get("total_orders")},
    }


async def _report_clienti(data_inizio: str, data_fine: str, limite: int) -> dict:
    if not _has_analytics_auth():
        raise ConfigError(
            _ANALYTICS_HINT + " Questo report non ha un fallback su wc/v3: nessun endpoint legacy "
            "espone dati clienti aggregati."
        )
    limite = max(1, min(limite or 20, 100))
    params = {"per_page": limite, "orderby": "total_spend", "order": "desc"}
    if data_inizio:
        params["after"] = f"{data_inizio}T00:00:00"
    if data_fine:
        params["before"] = f"{data_fine}T23:59:59"
    data = await _get_analytics("reports/customers", params)
    items = data if isinstance(data, list) else []
    return {
        "avviso_privacy": "Questi dati includono informazioni personali (nome, email): tratta l'output di conseguenza.",
        "clienti": [
            {
                "nome": i.get("name"),
                "email": i.get("email"),
                "ordini": i.get("orders_count"),
                "spesa_totale": i.get("total_spend"),
            }
            for i in items
        ],
    }


async def _elenco_ordini(data_inizio: str, data_fine: str, stato: str, limite: int) -> dict:
    limite = max(1, min(limite or 20, 100))
    params: dict = {"per_page": limite}
    if data_inizio:
        params["after"] = f"{data_inizio}T00:00:00"
    if data_fine:
        params["before"] = f"{data_fine}T23:59:59"
    if stato:
        params["status"] = stato
    data = await _get_v3("orders", params)
    items = data if isinstance(data, list) else []
    return {
        "ordini": [
            {
                "id": o.get("id"),
                "numero": o.get("number"),
                "stato": o.get("status"),
                "data": o.get("date_created"),
                "totale": o.get("total"),
                "valuta": o.get("currency"),
                "cliente_email": (o.get("billing") or {}).get("email"),
            }
            for o in items
        ],
    }


async def _dettaglio_prodotto(id: int) -> dict:
    if not id:
        raise ValueError("dettaglio_prodotto richiede il parametro 'id'.")
    data = await _get_v3(f"products/{int(id)}")
    if not isinstance(data, dict):
        raise RuntimeError("Prodotto non trovato.")
    images = data.get("images") or []
    return {
        "id": data.get("id"),
        "nome": data.get("name"),
        "sku": data.get("sku"),
        "tipo": data.get("type"),
        "stato": data.get("status"),
        "prezzo": data.get("price"),
        "prezzo_regolare": data.get("regular_price"),
        "prezzo_scontato": data.get("sale_price"),
        "stock_status": data.get("stock_status"),
        "quantita_stock": data.get("stock_quantity"),
        "categorie": [c.get("name") for c in (data.get("categories") or [])],
        "permalink": data.get("permalink"),
        "immagine": images[0].get("src") if images else None,
        "totale_vendite": data.get("total_sales"),
    }


async def _ricerca_prodotti(query: str, limite: int) -> dict:
    if not query:
        raise ValueError("ricerca_prodotti richiede il parametro 'query'.")
    limite = max(1, min(limite or 10, 50))
    data = await _get_v3("products", {"search": query, "per_page": limite})
    items = data if isinstance(data, list) else []
    return {
        "prodotti": [
            {
                "id": p.get("id"),
                "nome": p.get("name"),
                "sku": p.get("sku"),
                "prezzo": p.get("price"),
                "stock_status": p.get("stock_status"),
            }
            for p in items
        ],
    }


async def _report_categorie(data_inizio: str, data_fine: str, limite: int) -> dict:
    if not _has_analytics_auth():
        raise ConfigError(
            _ANALYTICS_HINT + " Questo report non ha un fallback su wc/v3: nessun endpoint legacy "
            "espone vendite per categoria."
        )
    limite = max(1, min(limite or 20, 100))
    params: dict = {"per_page": limite, "orderby": "items_sold", "order": "desc", "extended_info": "1"}
    if data_inizio:
        params["after"] = f"{data_inizio}T00:00:00"
    if data_fine:
        params["before"] = f"{data_fine}T23:59:59"
    data = await _get_analytics("reports/categories", params)
    items = data if isinstance(data, list) else []
    return {
        "categorie": [
            {
                "id": i.get("category_id"),
                "nome": (i.get("extended_info") or {}).get("name"),
                "quantita_venduta": i.get("items_sold"),
                "fatturato": i.get("net_revenue"),
                "ordini": i.get("orders_count"),
            }
            for i in items
        ],
    }


async def _dettaglio_ordine(id: int) -> dict:
    if not id:
        raise ValueError("dettaglio_ordine richiede il parametro 'id'.")
    data = await _get_v3(f"orders/{int(id)}")
    if not isinstance(data, dict):
        raise RuntimeError("Ordine non trovato.")
    billing = data.get("billing") or {}
    shipping = data.get("shipping") or {}
    shipping_lines = data.get("shipping_lines") or []
    return {
        "id": data.get("id"),
        "numero": data.get("number"),
        "stato": data.get("status"),
        "data": data.get("date_created"),
        "totale": data.get("total"),
        "valuta": data.get("currency"),
        "metodo_pagamento": data.get("payment_method_title"),
        "metodo_spedizione": shipping_lines[0].get("method_title") if shipping_lines else None,
        "cliente": {
            "nome": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or None,
            "email": billing.get("email"),
            "telefono": billing.get("phone"),
            "indirizzo_fatturazione": ", ".join(filter(None, [billing.get("address_1"), billing.get("city"), billing.get("postcode")])) or None,
            "indirizzo_spedizione": ", ".join(filter(None, [shipping.get("address_1"), shipping.get("city"), shipping.get("postcode")])) or None,
        },
        "note_cliente": data.get("customer_note") or None,
        "articoli": [
            {
                "prodotto_id": li.get("product_id"),
                "nome": li.get("name"),
                "quantita": li.get("quantity"),
                "totale": li.get("total"),
            }
            for li in (data.get("line_items") or [])
        ],
    }


async def _clienti_nuovi_vs_ricorrenti(data_inizio: str, data_fine: str) -> dict:
    if not data_inizio or not data_fine:
        raise ValueError("clienti_nuovi_vs_ricorrenti richiede sia data_inizio che data_fine (formato AAAA-MM-GG).")
    if not _has_analytics_auth():
        raise ConfigError(_ANALYTICS_HINT + " Questo report non ha un fallback su wc/v3.")
    base = {"after": f"{data_inizio}T00:00:00", "before": f"{data_fine}T23:59:59"}

    async def _segment(customer_type: str) -> dict:
        data = await _get_analytics("reports/revenue/stats", {**base, "customer_type": customer_type})
        totals = (data or {}).get("totals", {}) if isinstance(data, dict) else {}
        ordini = totals.get("orders_count") or 0
        fatturato = totals.get("total_sales") or 0
        return {
            "ordini": ordini,
            "fatturato": fatturato,
            "valore_medio_ordine": round(fatturato / ordini, 2) if ordini else None,
        }

    return {
        "fonte": "wc-analytics (segmentazione customer_type)",
        "nota": "Il parametro customer_type non è documentato ufficialmente e WooCommerce lo modifica "
                "spesso: verifica i numeri su un intervallo già noto prima di fidartene.",
        "clienti_nuovi": await _segment("new"),
        "clienti_ricorrenti": await _segment("returning"),
    }


def _pct_change(old: float, new: float) -> float | None:
    if not old:
        return None
    return round((new - old) / old * 100, 2)


async def _confronto_periodi(data_inizio_a: str, data_fine_a: str, data_inizio_b: str, data_fine_b: str) -> dict:
    if not all([data_inizio_a, data_fine_a, data_inizio_b, data_fine_b]):
        raise ValueError(
            "confronto_periodi richiede data_inizio_a, data_fine_a, data_inizio_b, data_fine_b (formato AAAA-MM-GG)."
        )
    periodo_a = await _panoramica_vendite("", data_inizio_a, data_fine_a)
    periodo_b = await _panoramica_vendite("", data_inizio_b, data_fine_b)
    fatturato_a = periodo_a.get("fatturato_totale") or 0
    fatturato_b = periodo_b.get("fatturato_totale") or 0
    ordini_a = periodo_a.get("ordini") or 0
    ordini_b = periodo_b.get("ordini") or 0
    return {
        "periodo_a": periodo_a,
        "periodo_b": periodo_b,
        "variazione_b_su_a": {
            "fatturato_percento": _pct_change(fatturato_a, fatturato_b),
            "ordini_percento": _pct_change(ordini_a, ordini_b),
        },
    }


# --- schema tool MCP --------------------------------------------------------

_PERIODO_PROP = {
    "type": "string",
    "enum": ["week", "month", "last_month", "year"],
    "description": "Periodo predefinito. Ignorato se data_inizio/data_fine sono forniti.",
}
_DATA_INIZIO_PROP = {"type": "string", "description": "Data inizio (AAAA-MM-GG). Opzionale."}
_DATA_FINE_PROP = {"type": "string", "description": "Data fine (AAAA-MM-GG). Opzionale."}
_LIMITE_PROP = {"type": "integer", "description": "Numero massimo di risultati."}

TOOLS = [
    types.Tool(
        name="panoramica_vendite",
        description="Fatturato, tasse, sconti, ordini e clienti in un periodo (via wc/v3, sempre disponibile).",
        inputSchema={
            "type": "object",
            "properties": {"periodo": _PERIODO_PROP, "data_inizio": _DATA_INIZIO_PROP, "data_fine": _DATA_FINE_PROP},
        },
    ),
    types.Tool(
        name="top_prodotti",
        description="Prodotti più venduti nel periodo. Con Application Password include il fatturato per prodotto.",
        inputSchema={
            "type": "object",
            "properties": {
                "periodo": _PERIODO_PROP,
                "data_inizio": _DATA_INIZIO_PROP,
                "data_fine": _DATA_FINE_PROP,
                "limite": _LIMITE_PROP,
            },
        },
    ),
    types.Tool(
        name="report_coupon",
        description="Utilizzo coupon nel periodo. Con Application Password include il dettaglio per singolo coupon.",
        inputSchema={
            "type": "object",
            "properties": {"data_inizio": _DATA_INIZIO_PROP, "data_fine": _DATA_FINE_PROP, "limite": _LIMITE_PROP},
        },
    ),
    types.Tool(
        name="andamento_temporale",
        description="Serie storica del fatturato tra due date, suddivisa per intervallo (day/week/month/quarter/year). "
        "Richiede Application Password per la serie completa; senza, torna solo il totale del periodo.",
        inputSchema={
            "type": "object",
            "required": ["data_inizio", "data_fine"],
            "properties": {
                "data_inizio": _DATA_INIZIO_PROP,
                "data_fine": _DATA_FINE_PROP,
                "intervallo": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
            },
        },
    ),
    types.Tool(
        name="report_clienti",
        description="Spesa per cliente nel periodo (nome, email, ordini, spesa totale). Richiede Application Password.",
        inputSchema={
            "type": "object",
            "properties": {"data_inizio": _DATA_INIZIO_PROP, "data_fine": _DATA_FINE_PROP, "limite": _LIMITE_PROP},
        },
    ),
    types.Tool(
        name="elenco_ordini",
        description="Elenco ordini nel periodo, filtrabile per stato (es. processing, completed, cancelled).",
        inputSchema={
            "type": "object",
            "properties": {
                "data_inizio": _DATA_INIZIO_PROP,
                "data_fine": _DATA_FINE_PROP,
                "stato": {"type": "string", "description": "Stato ordine WooCommerce."},
                "limite": _LIMITE_PROP,
            },
        },
    ),
    types.Tool(
        name="dettaglio_prodotto",
        description="Dettaglio di un prodotto (prezzo, stock, categorie, immagine) dato il suo ID.",
        inputSchema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "ID prodotto WooCommerce."}},
        },
    ),
    types.Tool(
        name="ricerca_prodotti",
        description="Cerca prodotti per nome o SKU: utile per trovare l'ID di un prodotto partendo dal nome.",
        inputSchema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Testo da cercare nel nome/SKU del prodotto."},
                "limite": _LIMITE_PROP,
            },
        },
    ),
    types.Tool(
        name="report_categorie",
        description="Fatturato e quantità vendute per categoria di prodotto nel periodo. Richiede Application Password.",
        inputSchema={
            "type": "object",
            "properties": {"data_inizio": _DATA_INIZIO_PROP, "data_fine": _DATA_FINE_PROP, "limite": _LIMITE_PROP},
        },
    ),
    types.Tool(
        name="dettaglio_ordine",
        description="Dettaglio completo di un ordine (articoli acquistati, indirizzi, pagamento, note) dato il suo ID.",
        inputSchema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "ID ordine WooCommerce."}},
        },
    ),
    types.Tool(
        name="clienti_nuovi_vs_ricorrenti",
        description="Confronta ordini, fatturato e valore medio ordine tra clienti nuovi e ricorrenti nel periodo. "
        "Richiede Application Password.",
        inputSchema={
            "type": "object",
            "required": ["data_inizio", "data_fine"],
            "properties": {"data_inizio": _DATA_INIZIO_PROP, "data_fine": _DATA_FINE_PROP},
        },
    ),
    types.Tool(
        name="confronto_periodi",
        description="Confronta fatturato e ordini tra due periodi (es. questo mese vs mese scorso) e calcola la "
        "variazione percentuale.",
        inputSchema={
            "type": "object",
            "required": ["data_inizio_a", "data_fine_a", "data_inizio_b", "data_fine_b"],
            "properties": {
                "data_inizio_a": {"type": "string", "description": "Data inizio periodo A (AAAA-MM-GG)."},
                "data_fine_a": {"type": "string", "description": "Data fine periodo A (AAAA-MM-GG)."},
                "data_inizio_b": {"type": "string", "description": "Data inizio periodo B (AAAA-MM-GG)."},
                "data_fine_b": {"type": "string", "description": "Data fine periodo B (AAAA-MM-GG)."},
            },
        },
    ),
]

_DISPATCH = {
    "panoramica_vendite": lambda a: _panoramica_vendite(a.get("periodo", ""), a.get("data_inizio", ""), a.get("data_fine", "")),
    "top_prodotti": lambda a: _top_prodotti(a.get("periodo", ""), a.get("data_inizio", ""), a.get("data_fine", ""), a.get("limite")),
    "report_coupon": lambda a: _report_coupon(a.get("data_inizio", ""), a.get("data_fine", ""), a.get("limite")),
    "andamento_temporale": lambda a: _andamento_temporale(a.get("data_inizio", ""), a.get("data_fine", ""), a.get("intervallo", "day")),
    "report_clienti": lambda a: _report_clienti(a.get("data_inizio", ""), a.get("data_fine", ""), a.get("limite")),
    "elenco_ordini": lambda a: _elenco_ordini(a.get("data_inizio", ""), a.get("data_fine", ""), a.get("stato", ""), a.get("limite")),
    "dettaglio_prodotto": lambda a: _dettaglio_prodotto(a.get("id")),
    "ricerca_prodotti": lambda a: _ricerca_prodotti(a.get("query", ""), a.get("limite")),
    "report_categorie": lambda a: _report_categorie(a.get("data_inizio", ""), a.get("data_fine", ""), a.get("limite")),
    "dettaglio_ordine": lambda a: _dettaglio_ordine(a.get("id")),
    "clienti_nuovi_vs_ricorrenti": lambda a: _clienti_nuovi_vs_ricorrenti(a.get("data_inizio", ""), a.get("data_fine", "")),
    "confronto_periodi": lambda a: _confronto_periodi(
        a.get("data_inizio_a", ""), a.get("data_fine_a", ""), a.get("data_inizio_b", ""), a.get("data_fine_b", "")
    ),
}


def _sanitize(message: str) -> str:
    """Rete di sicurezza: se una credenziale finisse in un messaggio d'errore, la maschera."""
    for secret in (CONSUMER_SECRET, APP_PASSWORD):
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


app = Server("woocommerce")


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
    except ConfigError as exc:
        return [types.TextContent(type="text", text=str(exc))]
    except httpx.RequestError as exc:
        logger.warning("Tool '%s': sito irraggiungibile: %s", name, _sanitize(str(exc)))
        return [types.TextContent(type="text", text=f"Impossibile raggiungere {SITE_URL or 'il sito'}: {_sanitize(str(exc))}")]
    except Exception as exc:  # noqa: BLE001
        safe = _sanitize(str(exc))
        logger.warning("Tool '%s' errore: %s", name, safe)
        return [types.TextContent(type="text", text=f"Errore: {safe}")]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
