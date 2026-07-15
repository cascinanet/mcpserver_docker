"""Server MCP per database SQLite cifrati con SQLCipher.

A differenza del server sqlite standard, la passphrase di cifratura NON è nota né al
server né all'hub: viene passata come parametro obbligatorio `key` a OGNI tool call,
usata solo per la durata della singola richiesta (connect → PRAGMA key → verifica →
operazione → close) e mai memorizzata, messa in cache o loggata.

Avvio (stdio transport):
    python3 -m app.mcp_servers.sqlcipher_server --db-path /percorso/al/file.db

Tool esposti (ognuno richiede `key`):
    read_query, write_query, create_table, list_tables, describe_table
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import sqlcipher3

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# Logging volutamente scarno: NON logghiamo mai gli argomenti dei tool (contengono `key`).
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s sqlcipher-mcp: %(message)s")
logger = logging.getLogger("sqlcipher-mcp")

_PLAINTEXT_MAGIC = b"SQLite format 3\x00"
_KEY_ERROR = "chiave mancante o non valida"
_PLAINTEXT_ERROR = "il file esistente non è un database cifrato valido"

# Impostato in main() dall'argomento --db-path. È l'unico stato del server; la chiave no.
DB_PATH: str = ""


class KeyRejected(Exception):
    """Chiave mancante/errata. Messaggio volutamente generico (nessun dettaglio sul file)."""


class PlaintextRejected(Exception):
    """Il file esiste ma è un DB SQLite in chiaro: rifiutato (accettiamo solo file cifrati)."""


def _escape(key: str) -> str:
    # PRAGMA key non accetta parametri bindati: la chiave va nel testo SQL come stringa
    # letterale. Raddoppiamo gli apici singoli per evitare qualunque quote-injection.
    return key.replace("'", "''")


def _open(key: object) -> sqlcipher3.Connection:
    """Apre la connessione, applica la chiave e ne verifica la validità. La chiave resta
    solo in questa funzione; il chiamante deve chiudere la connessione in un finally."""
    if not isinstance(key, str) or not key:
        raise KeyRejected(_KEY_ERROR)
    # Rifiuta i DB in chiaro: se il file esiste e ha l'header SQLite non cifrato, stop.
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) >= 16:
        with open(DB_PATH, "rb") as fh:
            if fh.read(16) == _PLAINTEXT_MAGIC:
                raise PlaintextRejected(_PLAINTEXT_ERROR)
    con = sqlcipher3.connect(DB_PATH)
    try:
        con.execute("PRAGMA key = '%s'" % _escape(key))
        # Una lettura di sqlite_master fallisce se la chiave è errata/assente.
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception:
        con.close()
        # `from None`: scarta il traceback originale, che potrebbe contenere lo statement PRAGMA.
        raise KeyRejected(_KEY_ERROR) from None
    con.row_factory = sqlcipher3.Row
    return con


def _rows_to_json(cursor) -> str:
    rows = [dict(r) for r in cursor.fetchall()]
    return json.dumps(rows, ensure_ascii=False, default=str)


def _do_read_query(key: object, query: str) -> str:
    if not query or not query.strip().lower().startswith("select"):
        raise ValueError("read_query accetta solo istruzioni SELECT")
    con = _open(key)
    try:
        return _rows_to_json(con.execute(query))
    finally:
        con.close()


def _do_write_query(key: object, query: str) -> str:
    stripped = (query or "").strip().lower()
    if not stripped or stripped.startswith("select"):
        raise ValueError("write_query accetta INSERT/UPDATE/DELETE, non SELECT")
    con = _open(key)
    try:
        cur = con.execute(query)
        con.commit()
        return json.dumps({"affected_rows": cur.rowcount})
    finally:
        con.close()


def _do_create_table(key: object, query: str) -> str:
    if not query or not query.strip().lower().startswith("create table"):
        raise ValueError("create_table accetta solo istruzioni CREATE TABLE")
    con = _open(key)
    try:
        con.execute(query)
        con.commit()
        return json.dumps({"ok": True})
    finally:
        con.close()


def _do_list_tables(key: object) -> str:
    con = _open(key)
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return json.dumps([r[0] for r in cur.fetchall()], ensure_ascii=False)
    finally:
        con.close()


def _do_describe_table(key: object, table_name: str) -> str:
    if not table_name or not isinstance(table_name, str):
        raise ValueError("table_name mancante")
    con = _open(key)
    try:
        # pragma_table_info come funzione tabellare accetta un parametro bindato: niente injection.
        cur = con.execute("SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info(?)", (table_name,))
        cols = [{"name": r[0], "type": r[1], "notnull": bool(r[2]), "default": r[3], "pk": bool(r[4])}
                for r in cur.fetchall()]
        if not cols:
            raise ValueError("tabella non trovata")
        return json.dumps(cols, ensure_ascii=False, default=str)
    finally:
        con.close()


_KEY_PROP = {"type": "string", "description": "Passphrase di cifratura SQLCipher (obbligatoria, usata solo per questa richiesta)."}

TOOLS = [
    types.Tool(
        name="read_query", description="Esegue una query SELECT sul database cifrato.",
        inputSchema={"type": "object", "required": ["key", "query"],
                     "properties": {"key": _KEY_PROP, "query": {"type": "string", "description": "Istruzione SELECT."}}},
    ),
    types.Tool(
        name="write_query", description="Esegue INSERT/UPDATE/DELETE sul database cifrato.",
        inputSchema={"type": "object", "required": ["key", "query"],
                     "properties": {"key": _KEY_PROP, "query": {"type": "string", "description": "Istruzione INSERT/UPDATE/DELETE."}}},
    ),
    types.Tool(
        name="create_table", description="Crea una tabella nel database cifrato.",
        inputSchema={"type": "object", "required": ["key", "query"],
                     "properties": {"key": _KEY_PROP, "query": {"type": "string", "description": "Istruzione CREATE TABLE."}}},
    ),
    types.Tool(
        name="list_tables", description="Elenca le tabelle del database cifrato.",
        inputSchema={"type": "object", "required": ["key"], "properties": {"key": _KEY_PROP}},
    ),
    types.Tool(
        name="describe_table", description="Mostra le colonne di una tabella del database cifrato.",
        inputSchema={"type": "object", "required": ["key", "table_name"],
                     "properties": {"key": _KEY_PROP, "table_name": {"type": "string", "description": "Nome della tabella."}}},
    ),
]

_DISPATCH = {
    "read_query": lambda a: _do_read_query(a.get("key"), a.get("query", "")),
    "write_query": lambda a: _do_write_query(a.get("key"), a.get("query", "")),
    "create_table": lambda a: _do_create_table(a.get("key"), a.get("query", "")),
    "list_tables": lambda a: _do_list_tables(a.get("key")),
    "describe_table": lambda a: _do_describe_table(a.get("key"), a.get("table_name", "")),
}


def _sanitize(message: str, key: object) -> str:
    """Ultima rete di sicurezza: se per qualche motivo la chiave comparisse in un messaggio
    d'errore, la sostituisce con '***' prima di restituirlo/loggarlo."""
    if isinstance(key, str) and key and key in message:
        message = message.replace(key, "***")
    return message


app = Server("sqlcipher")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=f"Tool sconosciuto: {name}")]
    key = arguments.get("key") if isinstance(arguments, dict) else None
    try:
        result = handler(arguments or {})
        return [types.TextContent(type="text", text=result)]
    except (KeyRejected, PlaintextRejected) as exc:
        # Errori legati alla chiave/al file: messaggio generico, nessun dettaglio strutturale.
        logger.warning("Tool '%s' rifiutato (chiave/file).", name)
        return [types.TextContent(type="text", text=str(exc))]
    except Exception as exc:  # noqa: BLE001
        safe = _sanitize(str(exc), key)
        logger.warning("Tool '%s' errore: %s", name, safe)
        return [types.TextContent(type="text", text=f"Errore: {safe}")]
    finally:
        # La chiave non deve sopravvivere alla richiesta.
        key = None
        if isinstance(arguments, dict):
            arguments.pop("key", None)


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    global DB_PATH
    parser = argparse.ArgumentParser(description="Server MCP SQLite cifrato (SQLCipher)")
    parser.add_argument("--db-path", required=True, help="Percorso del file DB cifrato")
    args = parser.parse_args()
    DB_PATH = args.db_path
    asyncio.run(_run())


if __name__ == "__main__":
    main()
