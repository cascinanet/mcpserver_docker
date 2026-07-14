"""Dipendenze FastAPI per proteggere le rotte della admin UI."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException


def current_username(request: Request) -> str | None:
    return request.session.get("user")


class RequireLogin:
    """Redirige a /login se la sessione non è autenticata (per le pagine HTML)."""

    def __call__(self, request: Request) -> str:
        user = current_username(request)
        if not user:
            raise HTTPException(
                status_code=307,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        return user


require_login = RequireLogin()
