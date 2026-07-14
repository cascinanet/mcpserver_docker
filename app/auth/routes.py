"""Rotte di login/logout della admin UI (sessione su cookie firmato)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.security import authenticate
from app.templating import templates

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str | None = None):
    if request.session.get("user"):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Credenziali non valide."},
            status_code=401,
        )
    request.session["user"] = user.username
    return RedirectResponse(next or "/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
