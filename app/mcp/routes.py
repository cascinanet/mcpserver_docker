"""Endpoint per i client MCP.

Transport supportati per ogni server abilitato:
  • Streamable HTTP (attuale, usato da Claude):
      POST   /mcp/{server_id}   -> invia messaggi JSON-RPC, riceve la risposta
      GET    /mcp/{server_id}   -> stream SSE per i messaggi server->client
      DELETE /mcp/{server_id}   -> termina la sessione
    La sessione è identificata dall'header 'Mcp-Session-Id' (assegnato alla initialize).
  • HTTP+SSE legacy (deprecato, retrocompatibilità):
      GET  /mcp/{server_id}/sse        -> stream SSE, emette l'evento 'endpoint'
      POST /mcp/{server_id}/messages   -> messaggi JSON-RPC client -> server

Auth: se il server ha un 'auth_token', va passato come Bearer header o ?token=.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException

from app.mcp.manager import manager
from app.models import MCPServer
from app.storage import store

logger = logging.getLogger("mcp.routes")
router = APIRouter(prefix="/mcp", tags=["mcp"])


def _check_access(request: Request, auth_token: str | None) -> None:
    if not auth_token:
        return
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token")
    if token != auth_token:
        raise HTTPException(status_code=401, detail="Token non valido")


def _resolve(server_id: str, request: Request) -> MCPServer:
    server = store.get_server(server_id)
    if not server or not server.enabled:
        raise HTTPException(status_code=404, detail="Server MCP non trovato o disabilitato")
    _check_access(request, server.auth_token)
    return server


@router.get("/{server_id}/sse")
async def sse(server_id: str, request: Request):
    server = store.get_server(server_id)
    if not server or not server.enabled:
        raise HTTPException(status_code=404, detail="Server MCP non trovato o disabilitato")
    _check_access(request, server.auth_token)

    session = await manager.create(server)
    post_url = f"{request.url.path.rsplit('/', 1)[0]}/messages?session_id={session.id}"

    async def event_stream():
        # 1) Comunica al client l'URL dove inviare i messaggi.
        yield f"event: endpoint\ndata: {post_url}\n\n"
        # 2) Inoltra i messaggi del server verso il client.
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(session.outbound.get(), timeout=15)
                    yield f"event: message\ndata: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # commento SSE per tenere viva la connessione
        finally:
            await manager.close(session.id)
            logger.info("Sessione SSE chiusa (%s)", session.id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Content-Type": "text/event-stream"},
    )


@router.post("/{server_id}/messages")
async def messages(server_id: str, session_id: str, request: Request):
    session = manager.get(session_id)
    if not session or session.server.id != server_id:
        raise HTTPException(status_code=404, detail="Sessione non trovata")
    body = await request.body()
    await session.send(body.decode("utf-8"))
    return Response(status_code=202)


# --- Transport Streamable HTTP (MCP 2025) ---

_SESSION_HEADER = "mcp-session-id"


def _is_request(msg: object) -> bool:
    return isinstance(msg, dict) and "method" in msg and msg.get("id") is not None


def _is_initialize(messages: list) -> bool:
    return any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)


@router.post("/{server_id}")
async def streamable_post(server_id: str, request: Request):
    server = _resolve(server_id, request)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Body JSON non valido")
    messages = body if isinstance(body, list) else [body]

    # Diagnostica: registra il body ricevuto (solo se il logging è attivo dalla admin UI).
    from app import runtime as _rt
    if _rt.request_logging_enabled():
        try:
            from app.config import get_settings as _gs
            with open(_gs().data_dir / "bodylog.jsonl", "a", encoding="utf-8") as _fh:
                _fh.write(json.dumps({"ua": request.headers.get("user-agent"), "body": body})[:800] + "\n")
        except OSError:
            pass

    session_id = request.headers.get(_SESSION_HEADER)
    init = _is_initialize(messages)
    if session_id:
        session = manager.get(session_id)
        if not session or session.server.id != server_id:
            raise HTTPException(status_code=404, detail="Sessione non trovata")
    elif init:
        session = await manager.create(server)
        session_id = session.id
    else:
        raise HTTPException(status_code=400, detail="Mcp-Session-Id mancante")

    # Inoltra ogni messaggio; per le richieste (con id) attende la risposta correlata.
    responses = []
    try:
        for msg in messages:
            if _is_request(msg):
                responses.append(json.loads(await session.request(msg)))
            elif isinstance(msg, dict):
                await session.send(json.dumps(msg))
    except (asyncio.TimeoutError, RuntimeError) as exc:
        await manager.close(session_id)
        raise HTTPException(status_code=502, detail=f"Errore dal server MCP: {exc}")

    headers = {_SESSION_HEADER: session_id} if init else {}
    if not responses:
        return Response(status_code=202, headers=headers)

    # Negoziazione: se il client accetta SSE (come Claude), rispondi in formato event-stream;
    # altrimenti JSON puro. Entrambi conformi alla spec Streamable HTTP.
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        async def gen():
            for resp in responses:
                yield f"event: message\ndata: {json.dumps(resp)}\n\n"
        sse_headers = {**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                       "Content-Type": "text/event-stream"}
        return StreamingResponse(gen(), media_type="text/event-stream", headers=sse_headers)

    payload = responses if isinstance(body, list) else responses[0]
    return JSONResponse(payload, headers=headers)


@router.get("/{server_id}")
async def streamable_get(server_id: str, request: Request):
    _resolve(server_id, request)
    session_id = request.headers.get(_SESSION_HEADER)
    session = manager.get(session_id) if session_id else None
    if not session or session.server.id != server_id:
        # Nessuno stream server->client senza sessione valida (conforme alla spec).
        raise HTTPException(status_code=405, detail="Sessione non valida per lo stream GET")

    async def event_stream():
        while True:
            if await request.is_disconnected():
                break
            try:
                message = await asyncio.wait_for(session.outbound.get(), timeout=15)
                yield f"data: {message}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Content-Type": "text/event-stream"},
    )


@router.delete("/{server_id}")
async def streamable_delete(server_id: str, request: Request):
    _resolve(server_id, request)
    session_id = request.headers.get(_SESSION_HEADER)
    if session_id:
        await manager.close(session_id)
    return Response(status_code=204)
