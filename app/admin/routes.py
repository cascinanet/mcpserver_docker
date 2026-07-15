"""Admin UI: dashboard e CRUD dei server MCP. Protetta da login di sessione."""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import runtime
from app.auth import security
from app.auth.dependencies import require_login
from app.config import get_settings
from app.mcp import catalog
from app.mcp.manager import manager
from app.models import MCPServer
from app.storage import store
from app.templating import templates

router = APIRouter(tags=["admin"], dependencies=[Depends(require_login)])


def _form_context(request: Request, user: str, server: MCPServer | None, error: str | None = None) -> dict:
    # Sostituisce il placeholder <DATA_DIR> nei template del catalogo (es. percorso DB sqlite)
    # con la cartella dati reale di questo deployment, così il form pre-compila un percorso
    # che vive davvero sul disco/volume persistente (Lightsail, Azure, Docker hanno DATA_DIR diversi).
    data_dir = str(get_settings().data_dir)
    server_types = []
    for t in catalog.SERVER_TYPES:
        data = t.model_dump()
        data["args"] = [a.replace("<DATA_DIR>", data_dir) for a in data["args"]]
        server_types.append(data)
    return {
        "request": request,
        "user": user,
        "server": server,
        "error": error,
        "server_types": server_types,
        "default_type": catalog.default_type().key,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_login)):
    servers = store.list_servers()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"user": user, "servers": servers,
         "type_labels": {t.key: t.label for t in catalog.SERVER_TYPES},
         "health": {s.id: manager.health(s.id) for s in servers},
         "logging_enabled": runtime.request_logging_enabled()},
    )


@router.post("/logging/toggle")
async def toggle_logging(request: Request, user: str = Depends(require_login)):
    runtime.set_request_logging(not runtime.request_logging_enabled())
    return RedirectResponse("/", status_code=303)


@router.post("/logging/clear")
async def clear_logs(request: Request, user: str = Depends(require_login)):
    for name in ("reqlog.jsonl", "bodylog.jsonl"):
        path = get_settings().data_dir / name
        try:
            path.unlink()
        except OSError:
            pass
    return RedirectResponse("/logs", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, user: str = Depends(require_login)):
    def tail(name: str, n: int = 80) -> list[str]:
        path = get_settings().data_dir / name
        try:
            with path.open("r", encoding="utf-8") as fh:
                return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
        except OSError:
            return []
    return templates.TemplateResponse(
        request, "logs.html",
        {"user": user, "logging_enabled": runtime.request_logging_enabled(),
         "reqlog": tail("reqlog.jsonl"), "bodylog": tail("bodylog.jsonl")},
    )


@router.get("/servers/new", response_class=HTMLResponse)
async def new_server_form(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "server_form.html", _form_context(request, user, None))


@router.get("/servers/{server_id}/edit", response_class=HTMLResponse)
async def edit_server_form(server_id: str, request: Request, user: str = Depends(require_login)):
    server = store.get_server(server_id)
    return templates.TemplateResponse(request, "server_form.html", _form_context(request, user, server))


@router.post("/servers/save")
async def save_server(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    type: str = Form("custom"),
    description: str = Form(""),
    command: str = Form(...),
    args: str = Form(""),
    env: str = Form("{}"),
    credentials_json: str = Form(""),
    auth_token: str = Form(""),
    enabled: bool = Form(False),
    user: str = Depends(require_login),
):
    server_id = id.strip()
    existing = store.get_server(server_id)
    server_type = catalog.get_type(type) or catalog.default_type()

    def build(env_dict: dict, has_credentials: bool) -> MCPServer:
        return MCPServer(
            id=server_id, name=name.strip(), type=server_type.key,
            description=description.strip(), command=command.strip(),
            args=[a for a in args.split() if a], env=env_dict,
            auth_token=auth_token.strip() or None, enabled=enabled,
            has_credentials=has_credentials,
        )

    try:
        env_dict = json.loads(env or "{}")
        if not isinstance(env_dict, dict):
            raise ValueError("Il campo Env deve essere un oggetto JSON.")
    except (ValueError, json.JSONDecodeError) as exc:
        return _form_error(request, user, build({}, False), f"Env non valido: {exc}")

    # Credenziali: se incollate, salvale e collega in automatico GOOGLE_APPLICATION_CREDENTIALS.
    has_credentials = existing.has_credentials if existing else False
    if credentials_json.strip():
        try:
            path, project_id = store.save_credentials(server_id, credentials_json)
        except (ValueError, json.JSONDecodeError) as exc:
            return _form_error(request, user, build(env_dict, has_credentials), f"Credenziali non valide: {exc}")
        env_dict[server_type.credentials_env] = str(path.resolve())
        if project_id:
            env_dict.setdefault("GOOGLE_PROJECT_ID", project_id)
        has_credentials = True

    store.upsert_server(build(env_dict, has_credentials))
    return RedirectResponse("/", status_code=303)


def _form_error(request: Request, user: str, server: MCPServer, error: str):
    return templates.TemplateResponse(
        request, "server_form.html", _form_context(request, user, server, error), status_code=400,
    )


@router.post("/servers/{server_id}/delete")
async def delete_server(server_id: str, user: str = Depends(require_login)):
    store.delete_server(server_id)
    return RedirectResponse("/", status_code=303)


# Tipi che gestiscono un file DB via --db-path (backup/download/restore valgono per tutti).
_SQLITE_TYPES = {"sqlite", "sqlite_encrypted"}


def _sqlite_db_path(server: MCPServer) -> Path | None:
    """Estrae il percorso del file DB dagli argomenti (--db-path <percorso>)."""
    args = server.args
    for i, arg in enumerate(args):
        if arg == "--db-path" and i + 1 < len(args):
            return Path(args[i + 1])
    return None


@router.get("/servers/{server_id}/download-db")
async def download_db(server_id: str, user: str = Depends(require_login)):
    """Scarica il file DB per un backup manuale. Ristretto ai server della famiglia sqlite
    e a percorsi dentro DATA_DIR, per evitare che un --db-path anomalo esponga file arbitrari.
    Per il tipo cifrato il file scaricato è già cifrato (SQLCipher), quindi sicuro da conservare."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    db_path = _sqlite_db_path(server)
    if not db_path:
        raise HTTPException(status_code=400, detail="Percorso del database non configurato.")
    resolved = db_path.resolve()
    if not resolved.is_relative_to(get_settings().data_dir.resolve()):
        raise HTTPException(status_code=400, detail="Percorso del database fuori dalla cartella dati.")
    if not resolved.is_file():
        raise HTTPException(
            status_code=404,
            detail="File database non ancora creato (avvia il server almeno una volta, es. con 'Testa connessione').",
        )
    return FileResponse(resolved, filename=f"{server_id}.db", media_type="application/octet-stream")


@router.post("/servers/{server_id}/restore-db")
async def restore_db(server_id: str, file: UploadFile = File(...), user: str = Depends(require_login)):
    """Sostituisce il file SQLite con uno caricato dall'admin (ripristino di un backup).
    Stessi controlli di sicurezza del download; risponde sempre 200 con {ok, detail} tranne
    che per ID/tipo non validi, così il pulsante nel form gestisce l'esito in modo uniforme."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        return JSONResponse({"ok": False, "detail": "Server SQLite non trovato."}, status_code=404)
    db_path = _sqlite_db_path(server)
    if not db_path:
        return JSONResponse({"ok": False, "detail": "Percorso del database non configurato."}, status_code=400)
    resolved = db_path.resolve()
    if not resolved.is_relative_to(get_settings().data_dir.resolve()):
        return JSONResponse({"ok": False, "detail": "Percorso del database fuori dalla cartella dati."}, status_code=400)

    content = await file.read()
    is_plaintext_sqlite = content.startswith(b"SQLite format 3\x00")
    if server.type == "sqlite_encrypted":
        # Il tipo cifrato deve ricevere un file cifrato: un DB SQLite in chiaro è chiaramente
        # sbagliato. Non possiamo validare oltre senza la chiave (che l'hub non conosce).
        if is_plaintext_sqlite:
            return JSONResponse(
                {"ok": False, "detail": "Il file caricato è un database SQLite in chiaro, non cifrato."},
                status_code=400,
            )
    elif not is_plaintext_sqlite:
        return JSONResponse(
            {"ok": False, "detail": "Il file caricato non è un database SQLite valido."}, status_code=400
        )

    backup_note = ""
    if resolved.is_file():
        backup_path = resolved.with_name(f"{resolved.name}.bak-{int(time.time())}")
        shutil.copy2(resolved, backup_path)
        backup_note = f" Backup del file precedente salvato come '{backup_path.name}'."

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(content)
    # Chiude eventuali processi caldi già in pool: leggerebbero ancora il file vecchio.
    await manager.close_server_pool(server_id)

    return JSONResponse({"ok": True, "detail": f"Database ripristinato ({len(content)} byte).{backup_note}"})


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def _backup_pattern(db_name: str) -> re.Pattern:
    return re.compile(r"^" + re.escape(db_name) + r"\.bak-(\d+)$")


def _list_backups(server: MCPServer) -> list[dict]:
    db_path = _sqlite_db_path(server)
    if not db_path:
        return []
    resolved = db_path.resolve()
    if not resolved.is_relative_to(get_settings().data_dir.resolve()):
        return []
    pattern = _backup_pattern(resolved.name)
    backups = []
    for p in resolved.parent.glob(f"{resolved.name}.bak-*"):
        match = pattern.match(p.name)
        if not match:
            continue
        backups.append({
            "name": p.name,
            "size": _format_size(p.stat().st_size),
            "created_at": datetime.fromtimestamp(int(match.group(1))).strftime("%Y-%m-%d %H:%M:%S"),
            "sort_key": int(match.group(1)),
        })
    backups.sort(key=lambda b: b["sort_key"], reverse=True)
    return backups


@router.get("/servers/{server_id}/backups", response_class=HTMLResponse)
async def list_backups(server_id: str, request: Request, user: str = Depends(require_login)):
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    return templates.TemplateResponse(
        request, "backups.html", {"user": user, "server": server, "backups": _list_backups(server)},
    )


@router.post("/servers/{server_id}/backups/{filename}/delete")
async def delete_backup(server_id: str, filename: str, user: str = Depends(require_login)):
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    db_path = _sqlite_db_path(server)
    if not db_path:
        raise HTTPException(status_code=400, detail="Percorso del database non configurato.")
    resolved_db = db_path.resolve()
    if not resolved_db.is_relative_to(get_settings().data_dir.resolve()):
        raise HTTPException(status_code=400, detail="Percorso del database fuori dalla cartella dati.")
    # Nome file validato contro un pattern esatto (niente '..' o percorsi assoluti nel path param).
    if not _backup_pattern(resolved_db.name).match(filename):
        raise HTTPException(status_code=400, detail="Nome file di backup non valido.")
    backup_path = resolved_db.parent / filename
    if not backup_path.is_file():
        raise HTTPException(status_code=404, detail="Backup non trovato.")
    backup_path.unlink()
    return RedirectResponse(f"/servers/{server_id}/backups", status_code=303)


@router.post("/servers/{server_id}/test")
async def test_server(server_id: str, user: str = Depends(require_login)):
    """Handshake MCP minimo ('initialize') eseguito internamente, senza passare da un
    client esterno: usato dal pulsante 'Testa connessione' nel form di modifica server."""
    server = store.get_server(server_id)
    if not server:
        return JSONResponse({"ok": False, "detail": "Server non trovato."}, status_code=404)
    result = await manager.test_connection(server)
    return JSONResponse(result)


@router.get("/account/password", response_class=HTMLResponse)
async def password_form(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "change_password.html", {"user": user, "error": None, "ok": False}
    )


@router.post("/account/password", response_class=HTMLResponse)
async def password_change(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    user: str = Depends(require_login),
):
    def render(error=None, ok=False, code=200):
        return templates.TemplateResponse(
            request, "change_password.html", {"user": user, "error": error, "ok": ok}, status_code=code
        )

    account = store.get_user(user)
    if not account or not security.verify_password(current, account.password_hash):
        return render(error="Password attuale errata.", code=400)
    if len(new) < 8:
        return render(error="La nuova password deve avere almeno 8 caratteri.", code=400)
    if new != confirm:
        return render(error="Le due nuove password non coincidono.", code=400)
    account.password_hash = security.hash_password(new)
    store.upsert_user(account)
    return render(ok=True)
