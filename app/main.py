"""Entry point FastAPI dell'MCP Hub."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.admin import routes as admin_routes
from app.auth import routes as auth_routes
from app.auth.security import ensure_bootstrap_admin
from app.config import get_settings
from app import runtime
from app.mcp import routes as mcp_routes
from app.mcp.manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_bootstrap_admin()
    runtime.load()
    # Pre-avvia i processi MCP così il primo initialize dei client è immediato.
    try:
        await manager.prewarm_all()
    except Exception:  # noqa: BLE001
        logging.getLogger("mcp").exception("Prewarm iniziale fallito")
    # Reaper: chiude le sessioni inattive per evitare l'accumulo di subprocess (OOM).
    reaper = asyncio.create_task(manager.reap_loop())
    yield
    reaper.cancel()
    await manager.close_all()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="MCP Hub", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=False,  # su Azure il TLS è terminato dal frontend; lascia False dietro proxy
    )

    # CORS: i client MCP web (es. claude.ai) girano nel browser e fanno richieste
    # cross-origin con preflight OPTIONS. Va esposto Mcp-Session-Id per leggere la sessione.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
        max_age=86400,
    )

    # Diagnostica temporanea: registra le richieste a /mcp su file (per capire i client).
    import json as _json
    import time as _time

    @app.middleware("http")
    async def _log_mcp(request, call_next):
        path = request.url.path
        if path == "/healthz" or not runtime.request_logging_enabled():
            return await call_next(request)
        h = request.headers
        entry = {
            "method": request.method,
            "path": path,
            "qs_keys": list(request.query_params.keys()),
            "accept": h.get("accept"),
            "user_agent": h.get("user-agent"),
            "origin": h.get("origin"),
            "mcp_session_id": h.get("mcp-session-id"),
            "has_authorization": "authorization" in h,
        }
        try:
            response = await call_next(request)
            entry["status"] = response.status_code
            return response
        except Exception as exc:  # noqa: BLE001 - registra sempre uno status anche sugli errori non gestiti
            entry["status"] = 500
            entry["error"] = str(exc)
            raise
        finally:
            try:
                with open(settings.data_dir / "reqlog.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(_json.dumps(entry) + "\n")
            except OSError:
                pass

    app.include_router(auth_routes.router)
    app.include_router(mcp_routes.router)   # /mcp/... (consumato dai client MCP)
    app.include_router(admin_routes.router)  # admin UI protetta (registrata per ultima)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
