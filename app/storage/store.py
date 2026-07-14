"""Storage su file JSON (nessun database).

Su Azure App Service punta DATA_DIR a /home/data, che è persistente tra i riavvii.
La scrittura è atomica (write su file temporaneo + rename).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import MCPServer, User

_lock = threading.Lock()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- Server MCP ---

def list_servers() -> list[MCPServer]:
    raw = _read_json(get_settings().servers_file, [])
    return [MCPServer(**item) for item in raw]


def get_server(server_id: str) -> MCPServer | None:
    return next((s for s in list_servers() if s.id == server_id), None)


def save_servers(servers: list[MCPServer]) -> None:
    with _lock:
        _write_json(get_settings().servers_file, [s.model_dump() for s in servers])


def upsert_server(server: MCPServer) -> None:
    servers = list_servers()
    servers = [s for s in servers if s.id != server.id]
    servers.append(server)
    save_servers(servers)


def delete_server(server_id: str) -> None:
    save_servers([s for s in list_servers() if s.id != server_id])
    delete_credentials(server_id)


# --- Credenziali per-server (file JSON del service account) ---

def _creds_dir() -> Path:
    path = get_settings().data_dir / "creds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def credentials_path(server_id: str) -> Path:
    return _creds_dir() / f"{server_id}.json"


def save_credentials(server_id: str, raw_json: str) -> tuple[Path, str | None]:
    """Salva il JSON del service account su file dedicato.

    Ritorna (percorso, project_id) e valida che sia un service account.
    Solleva ValueError se il JSON non è valido o non è un service account.
    """
    data = json.loads(raw_json)
    if data.get("type") != "service_account":
        raise ValueError("Il JSON non sembra un service account (manca \"type\": \"service_account\").")
    path = credentials_path(server_id)
    with _lock:
        _write_json(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, data.get("project_id")


def delete_credentials(server_id: str) -> None:
    path = credentials_path(server_id)
    if path.exists():
        path.unlink()


# --- Utenti ---

def list_users() -> list[User]:
    raw = _read_json(get_settings().users_file, [])
    return [User(**item) for item in raw]


def get_user(username: str) -> User | None:
    return next((u for u in list_users() if u.username == username), None)


def save_users(users: list[User]) -> None:
    with _lock:
        _write_json(get_settings().users_file, [u.model_dump() for u in users])


def upsert_user(user: User) -> None:
    users = [u for u in list_users() if u.username != user.username]
    users.append(user)
    save_users(users)
