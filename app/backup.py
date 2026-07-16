"""Backup dei file DB per i server della famiglia sqlite (in chiaro e cifrati SQLCipher).

Logica condivisa tra le route admin (pulsanti Scarica/Ripristina/Backup ora/Elimina) e lo
scheduler in background (backup automatico pianificato + pulizia dei più vecchi per retention).
Un backup è una semplice copia del file: non richiede la passphrase SQLCipher, perché è
un'operazione a livello di byte sul file, non sul contenuto del database.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.models import MCPServer

logger = logging.getLogger("mcp.backup")

# Tipi che gestiscono un file DB via --db-path (backup/download/restore valgono per tutti).
SQLITE_TYPES = {"sqlite", "sqlite_encrypted"}

# Ogni quanto il loop in background controlla se qualche server ha bisogno di un backup
# automatico. Indipendente dall'intervallo scelto per ogni singolo server (che può essere
# più lungo): questo è solo il "tick" di verifica.
SCHEDULER_CHECK_INTERVAL = 900  # 15 minuti


def sqlite_db_path(server: MCPServer) -> Path | None:
    """Estrae il percorso del file DB dagli argomenti (--db-path <percorso>)."""
    args = server.args
    for i, arg in enumerate(args):
        if arg == "--db-path" and i + 1 < len(args):
            return Path(args[i + 1])
    return None


def resolve_db_path(server: MCPServer) -> Path | None:
    """Percorso del DB risolto e validato (dentro DATA_DIR). None se non configurato o
    se punta fuori dalla cartella dati (protezione contro un --db-path anomalo)."""
    db_path = sqlite_db_path(server)
    if not db_path:
        return None
    resolved = db_path.resolve()
    if not resolved.is_relative_to(get_settings().data_dir.resolve()):
        return None
    return resolved


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def backup_pattern(db_name: str) -> re.Pattern:
    return re.compile(r"^" + re.escape(db_name) + r"\.bak-(\d+)$")


def list_backups(server: MCPServer) -> list[dict]:
    """Backup esistenti per un server, più recente prima."""
    resolved = resolve_db_path(server)
    if not resolved:
        return []
    pattern = backup_pattern(resolved.name)
    backups = []
    for p in resolved.parent.glob(f"{resolved.name}.bak-*"):
        match = pattern.match(p.name)
        if not match:
            continue
        backups.append({
            "name": p.name,
            "size": format_size(p.stat().st_size),
            "created_at": datetime.fromtimestamp(int(match.group(1))).strftime("%Y-%m-%d %H:%M:%S"),
            "sort_key": int(match.group(1)),
        })
    backups.sort(key=lambda b: b["sort_key"], reverse=True)
    return backups


def apply_retention(server: MCPServer, resolved: Path) -> list[str]:
    """Elimina i backup più vecchi oltre il limite 'backup_retention' del server.
    Ritorna i nomi dei file eliminati."""
    if not server.backup_retention or server.backup_retention <= 0:
        return []
    backups = list_backups(server)
    removed = []
    for b in backups[server.backup_retention:]:
        path = resolved.parent / b["name"]
        try:
            path.unlink()
            removed.append(b["name"])
        except OSError:
            logger.warning("Impossibile eliminare il backup '%s' (retention) per %s", b["name"], server.id)
    return removed


def create_backup(server: MCPServer) -> dict:
    """Crea un backup (copia del file DB) e applica la retention configurata. Non richiede
    la passphrase: è una copia di byte, non un'operazione sul contenuto del database."""
    resolved = resolve_db_path(server)
    if not resolved:
        return {"ok": False, "detail": "Percorso del database non configurato o fuori dalla cartella dati."}
    if not resolved.is_file():
        return {"ok": False, "detail": "File database non ancora creato (nessun dato da salvare)."}
    backup_path = resolved.with_name(f"{resolved.name}.bak-{int(time.time())}")
    shutil.copy2(resolved, backup_path)
    removed = apply_retention(server, resolved)
    detail = f"Backup creato: {backup_path.name} ({format_size(backup_path.stat().st_size)})."
    if removed:
        detail += f" Rimossi {len(removed)} backup più vecchi (retention: {server.backup_retention})."
    return {"ok": True, "detail": detail}


async def scheduler_loop() -> None:
    """Controlla periodicamente tutti i server con backup automatico attivo e crea un
    nuovo backup quando è passato 'backup_interval_hours' dall'ultima esecuzione."""
    from app.storage import store  # import ritardato: store non deve dipendere da backup

    while True:
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)
        now = time.time()
        for server in store.list_servers():
            if server.type not in SQLITE_TYPES or not server.backup_interval_hours:
                continue
            due_since = server.backup_last_run_at or 0
            if now - due_since < server.backup_interval_hours * 3600:
                continue
            result = create_backup(server)
            server.backup_last_run_at = now
            store.upsert_server(server)
            if result["ok"]:
                logger.info("Backup automatico per '%s': %s", server.id, result["detail"])
            else:
                logger.warning("Backup automatico per '%s' saltato: %s", server.id, result["detail"])
