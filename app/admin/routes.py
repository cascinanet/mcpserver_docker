"""Admin UI: dashboard e CRUD dei server MCP. Protetta da login di sessione."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import backup as backup_mod
from app import linkedin_oauth
from app import meta_oauth
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
    # Sostituisce i placeholder dei template del catalogo con i valori reali di questo
    # deployment/server, così il form pre-compila comando/argomenti già pronti all'uso:
    #   <DATA_DIR>   -> cartella dati reale (Lightsail, Azure, Docker hanno DATA_DIR diversi)
    #   <SERVER_ID>  -> id del server in modifica (solo se già esistente/noto)
    data_dir = str(get_settings().data_dir)
    server_types = []
    for t in catalog.SERVER_TYPES:
        data = t.model_dump()
        data["args"] = [a.replace("<DATA_DIR>", data_dir) for a in data["args"]]
        if server:
            data["args"] = [a.replace("<SERVER_ID>", server.id) for a in data["args"]]
        server_types.append(data)
    linkedin_status = linkedin_oauth.status(server.id) if server and server.type == "linkedin" else None
    meta_status = meta_oauth.status(server.id) if server and server.type == "meta" else None
    return {
        "request": request,
        "user": user,
        "server": server,
        "error": error,
        "server_types": server_types,
        "default_type": catalog.default_type().key,
        "linkedin_status": linkedin_status,
        "linkedin_redirect_uri": f"{_public_base_url(request)}/oauth/linkedin/callback",
        "meta_status": meta_status,
        "meta_redirect_uri": f"{_public_base_url(request)}/oauth/meta/callback",
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
    linkedin_client_id: str = Form(""),
    linkedin_client_secret: str = Form(""),
    linkedin_org_id: str = Form(""),
    meta_app_id: str = Form(""),
    meta_app_secret: str = Form(""),
    meta_pages: str = Form(""),
    user: str = Depends(require_login),
):
    server_id = id.strip()
    existing = store.get_server(server_id)
    server_type = catalog.get_type(type) or catalog.default_type()

    def build(env_dict: dict, has_credentials: bool) -> MCPServer:
        # Rete di sicurezza server-side per i placeholder del catalogo: se il client non li ha
        # sostituiti (es. tipo cambiato senza toccare Argomenti), non deve finire nel comando
        # avviato un letterale '<SERVER_ID>' invece del vero id.
        data_dir = str(get_settings().data_dir)
        real_args = [a.replace("<DATA_DIR>", data_dir).replace("<SERVER_ID>", server_id) for a in args.split() if a]
        return MCPServer(
            id=server_id, name=name.strip(), type=server_type.key,
            description=description.strip(), command=command.strip(),
            args=real_args, env=env_dict,
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

    # Campi dedicati LinkedIn: uniti nell'Env così l'admin non deve scrivere il JSON a mano.
    # A differenza del box credenziali Google (sempre vuoto per design), questi campi sono
    # pre-compilati con il valore attuale ad ogni apertura del form: se l'admin non li tocca
    # il valore corrente viene ri-salvato invariato; se li svuota intenzionalmente, vengono
    # cancellati (stesso comportamento dei campi Nome/Comando, non serve logica speciale).
    if server_type.key == "linkedin":
        env_dict["LINKEDIN_CLIENT_ID"] = linkedin_client_id.strip()
        env_dict["LINKEDIN_CLIENT_SECRET"] = linkedin_client_secret.strip()
        env_dict["LINKEDIN_ORG_ID"] = linkedin_org_id.strip()
    if server_type.key == "meta":
        env_dict["META_APP_ID"] = meta_app_id.strip()
        env_dict["META_APP_SECRET"] = meta_app_secret.strip()
        env_dict["META_PAGES"] = meta_pages.strip()

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


def _public_base_url(request: Request) -> str:
    configured = get_settings().public_base_url
    if configured:
        return configured.rstrip("/")
    # Fallback: dedotto dalla richiesta in arrivo. Dietro un reverse proxy che non inoltra
    # correttamente lo schema originale, impostare PUBLIC_BASE_URL evita mismatch con il
    # redirect URI registrato in LinkedIn.
    return str(request.base_url).rstrip("/")


@router.get("/servers/{server_id}/linkedin/authorize")
async def linkedin_authorize(server_id: str, request: Request, user: str = Depends(require_login)):
    """Avvia il consenso OAuth: redirige il browser dell'admin a LinkedIn."""
    server = store.get_server(server_id)
    if not server or server.type != "linkedin":
        raise HTTPException(status_code=404, detail="Server LinkedIn non trovato.")
    if not server.env.get("LINKEDIN_CLIENT_ID"):
        raise HTTPException(status_code=400, detail="Imposta prima LINKEDIN_CLIENT_ID nelle Env e salva.")
    state = linkedin_oauth.make_state(server_id)
    redirect_uri = f"{_public_base_url(request)}/oauth/linkedin/callback"
    return RedirectResponse(linkedin_oauth.authorize_url(server, redirect_uri, state))


@router.get("/oauth/linkedin/callback", response_class=HTMLResponse)
async def linkedin_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    user: str = Depends(require_login),
):
    """Riceve il redirect da LinkedIn, scambia il code per i token e li salva."""

    def _bounce(message: str, server_id: str | None = None, code_status: int = 400):
        back = f"/servers/{server_id}/edit" if server_id else "/"
        return HTMLResponse(
            f"<p>{message}</p><p><a href='{back}'>Torna al pannello</a></p>", status_code=code_status
        )

    if error:
        return _bounce(f"LinkedIn ha rifiutato l'autorizzazione: {error} — {error_description}")

    server_id = linkedin_oauth.consume_state(state)
    if not server_id:
        return _bounce("Sessione di autorizzazione scaduta o non valida (hai impiegato più di 10 minuti, o la pagina è stata aperta due volte). Riprova dal pulsante 'Autorizza con LinkedIn'.")

    server = store.get_server(server_id)
    if not server or server.type != "linkedin":
        return _bounce("Server LinkedIn non trovato.", server_id)

    redirect_uri = f"{_public_base_url(request)}/oauth/linkedin/callback"
    try:
        payload = await linkedin_oauth.exchange_code(server, code, redirect_uri)
    except Exception as exc:  # noqa: BLE001
        return _bounce(f"Scambio del code fallito: {exc}", server_id)

    linkedin_oauth.save_tokens(server_id, payload)
    return RedirectResponse(f"/servers/{server_id}/edit?linkedin_authorized=1", status_code=303)


@router.post("/servers/{server_id}/linkedin/deauthorize")
async def linkedin_deauthorize(server_id: str, user: str = Depends(require_login)):
    """Elimina il token salvato (revoca locale): il server smetterà di funzionare finché non
    si rifà il consenso OAuth."""
    server = store.get_server(server_id)
    if not server or server.type != "linkedin":
        raise HTTPException(status_code=404, detail="Server LinkedIn non trovato.")
    linkedin_oauth.delete_tokens(server_id)
    return RedirectResponse(f"/servers/{server_id}/edit", status_code=303)


@router.get("/servers/{server_id}/meta/authorize")
async def meta_authorize(server_id: str, request: Request, user: str = Depends(require_login)):
    """Avvia il consenso OAuth: redirige il browser dell'admin a Meta."""
    server = store.get_server(server_id)
    if not server or server.type != "meta":
        raise HTTPException(status_code=404, detail="Server Meta non trovato.")
    if not server.env.get("META_APP_ID"):
        raise HTTPException(status_code=400, detail="Imposta prima META_APP_ID nelle Env e salva.")
    state = meta_oauth.make_state(server_id)
    redirect_uri = f"{_public_base_url(request)}/oauth/meta/callback"
    return RedirectResponse(meta_oauth.authorize_url(server, redirect_uri, state))


@router.get("/oauth/meta/callback", response_class=HTMLResponse)
async def meta_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    user: str = Depends(require_login),
):
    """Riceve il redirect da Meta, scambia il code per il token (esteso a lunga durata) e
    scopre Pagine/Instagram collegati."""

    def _bounce(message: str, server_id: str | None = None, code_status: int = 400):
        back = f"/servers/{server_id}/edit" if server_id else "/"
        return HTMLResponse(
            f"<p>{message}</p><p><a href='{back}'>Torna al pannello</a></p>", status_code=code_status
        )

    if error:
        return _bounce(f"Meta ha rifiutato l'autorizzazione: {error} — {error_description}")

    server_id = meta_oauth.consume_state(state)
    if not server_id:
        return _bounce("Sessione di autorizzazione scaduta o non valida (hai impiegato più di 10 minuti, o la pagina è stata aperta due volte). Riprova dal pulsante 'Autorizza con Meta'.")

    server = store.get_server(server_id)
    if not server or server.type != "meta":
        return _bounce("Server Meta non trovato.", server_id)

    redirect_uri = f"{_public_base_url(request)}/oauth/meta/callback"
    try:
        await meta_oauth.complete_authorization(server, server_id, code, redirect_uri)
    except Exception as exc:  # noqa: BLE001
        return _bounce(f"Scambio del code fallito: {exc}", server_id)

    return RedirectResponse(f"/servers/{server_id}/edit?meta_authorized=1", status_code=303)


@router.post("/servers/{server_id}/meta/deauthorize")
async def meta_deauthorize(server_id: str, user: str = Depends(require_login)):
    """Elimina il token salvato (revoca locale): il server smetterà di funzionare finché non
    si rifà il consenso OAuth."""
    server = store.get_server(server_id)
    if not server or server.type != "meta":
        raise HTTPException(status_code=404, detail="Server Meta non trovato.")
    meta_oauth.delete_tokens(server_id)
    return RedirectResponse(f"/servers/{server_id}/edit", status_code=303)


# Tipi che gestiscono un file DB via --db-path (backup/download/restore valgono per tutti).
_SQLITE_TYPES = backup_mod.SQLITE_TYPES


@router.get("/servers/{server_id}/download-db")
async def download_db(server_id: str, user: str = Depends(require_login)):
    """Scarica il file DB per un backup manuale. Ristretto ai server della famiglia sqlite
    e a percorsi dentro DATA_DIR, per evitare che un --db-path anomalo esponga file arbitrari.
    Per il tipo cifrato il file scaricato è già cifrato (SQLCipher), quindi sicuro da conservare."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    resolved = backup_mod.resolve_db_path(server)
    if not resolved:
        raise HTTPException(status_code=400, detail="Percorso del database non configurato o fuori dalla cartella dati.")
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
    resolved = backup_mod.resolve_db_path(server)
    if not resolved:
        return JSONResponse(
            {"ok": False, "detail": "Percorso del database non configurato o fuori dalla cartella dati."},
            status_code=400,
        )

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
        result = backup_mod.create_backup(server)
        if result["ok"]:
            backup_note = f" Backup del file precedente creato prima della sostituzione ({result['detail']})"

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(content)
    # Chiude eventuali processi caldi già in pool: leggerebbero ancora il file vecchio.
    await manager.close_server_pool(server_id)

    return JSONResponse({"ok": True, "detail": f"Database ripristinato ({len(content)} byte).{backup_note}"})


@router.get("/servers/{server_id}/backups", response_class=HTMLResponse)
async def list_backups(server_id: str, request: Request, user: str = Depends(require_login)):
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    last_run = None
    if server.backup_last_run_at:
        last_run = datetime.fromtimestamp(server.backup_last_run_at).strftime("%Y-%m-%d %H:%M:%S")
    return templates.TemplateResponse(
        request, "backups.html",
        {"user": user, "server": server, "backups": backup_mod.list_backups(server), "last_run": last_run},
    )


@router.post("/servers/{server_id}/backups/create")
async def create_backup_now(server_id: str, user: str = Depends(require_login)):
    """Crea subito un backup (copia del file DB) fuori da qualunque pianificazione."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    backup_mod.create_backup(server)
    return RedirectResponse(f"/servers/{server_id}/backups", status_code=303)


@router.post("/servers/{server_id}/backups/settings")
async def save_backup_settings(
    server_id: str,
    backup_interval_hours: int = Form(0),
    backup_retention: int = Form(0),
    user: str = Depends(require_login),
):
    """Salva pianificazione (ogni N ore, 0 = disattivato) e retention (max backup da
    mantenere, 0 = illimitato) per il backup automatico di questo server."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    server.backup_interval_hours = backup_interval_hours or None
    server.backup_retention = backup_retention or None
    store.upsert_server(server)
    return RedirectResponse(f"/servers/{server_id}/backups", status_code=303)


def _resolve_backup_path(server_id: str, filename: str) -> tuple[MCPServer, Path]:
    """Helper comune a download/delete: valida server, tipo, percorso DB e nome file di
    backup (regex ancorata: niente '..' o percorsi assoluti nel path param)."""
    server = store.get_server(server_id)
    if not server or server.type not in _SQLITE_TYPES:
        raise HTTPException(status_code=404, detail="Server SQLite non trovato.")
    resolved_db = backup_mod.resolve_db_path(server)
    if not resolved_db:
        raise HTTPException(status_code=400, detail="Percorso del database non configurato o fuori dalla cartella dati.")
    if not backup_mod.backup_pattern(resolved_db.name).match(filename):
        raise HTTPException(status_code=400, detail="Nome file di backup non valido.")
    return server, resolved_db.parent / filename


@router.get("/servers/{server_id}/backups/{filename}/download")
async def download_backup(server_id: str, filename: str, user: str = Depends(require_login)):
    """Scarica un singolo file di backup (stessi controlli di sicurezza della cancellazione)."""
    _server, backup_path = _resolve_backup_path(server_id, filename)
    if not backup_path.is_file():
        raise HTTPException(status_code=404, detail="Backup non trovato.")
    return FileResponse(backup_path, filename=filename, media_type="application/octet-stream")


@router.post("/servers/{server_id}/backups/{filename}/delete")
async def delete_backup(server_id: str, filename: str, user: str = Depends(require_login)):
    _server, backup_path = _resolve_backup_path(server_id, filename)
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
