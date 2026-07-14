"""Admin UI: dashboard e CRUD dei server MCP. Protetta da login di sessione."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import runtime
from app.auth import security
from app.auth.dependencies import require_login
from app.config import get_settings
from app.mcp import catalog
from app.models import MCPServer
from app.storage import store
from app.templating import templates

router = APIRouter(tags=["admin"], dependencies=[Depends(require_login)])


def _form_context(request: Request, user: str, server: MCPServer | None, error: str | None = None) -> dict:
    return {
        "request": request,
        "user": user,
        "server": server,
        "error": error,
        "server_types": [t.model_dump() for t in catalog.SERVER_TYPES],
        "default_type": catalog.default_type().key,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_login)):
    servers = store.list_servers()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"user": user, "servers": servers,
         "type_labels": {t.key: t.label for t in catalog.SERVER_TYPES},
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
