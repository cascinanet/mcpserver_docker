"""Registro in memoria delle sessioni MCP + pool di processi pre-avviati.

Avviare un server MCP (es. analytics-mcp) da zero richiede molti secondi (import
Python + librerie). Per rispondere subito all'initialize dei client (claude.ai ha
un timeout breve), teniamo un piccolo pool di processi già avviati ("caldi") per
ogni server: alla connessione ne assegniamo uno e ne avviamo un altro di scorta.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from app.mcp.session import MCPSession
from app.models import MCPServer

logger = logging.getLogger("mcp.manager")

POOL_SIZE = 1        # processi caldi per server (istanza con RAM limitata)
IDLE_TTL = 600       # secondi di inattività dopo cui una sessione viene chiusa
MAX_SESSIONS = 24    # tetto massimo sessioni attive (backstop anti-accumulo)
REAP_INTERVAL = 60   # ogni quanto gira il reaper


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, MCPSession] = {}
        self._pools: dict[str, list[MCPSession]] = {}

    # --- pool di processi caldi ---

    async def prewarm_all(self) -> None:
        from app.storage import store
        for server in store.list_servers():
            if server.enabled:
                try:
                    await self._refill(server)
                except Exception:  # noqa: BLE001
                    logger.exception("Prewarm fallito per %s", server.id)

    async def _refill(self, server: MCPServer) -> None:
        pool = self._pools.setdefault(server.id, [])
        while len(pool) < POOL_SIZE:
            session = MCPSession(secrets.token_urlsafe(24), server)
            await session.start()
            pool.append(session)
            logger.info("Processo caldo pronto per %s (pool=%d)", server.id, len(pool))

    def _refill_bg(self, server: MCPServer) -> None:
        async def _task():
            try:
                await self._refill(server)
            except Exception:  # noqa: BLE001
                logger.exception("Refill pool fallito per %s", server.id)
        asyncio.create_task(_task())

    # --- sessioni attive ---

    async def create(self, server: MCPServer) -> MCPSession:
        pool = self._pools.get(server.id, [])
        session = pool.pop(0) if pool else None
        if session is None:
            # Pool vuoto: avvio a freddo (lento). Capita solo sotto burst di connessioni.
            session = MCPSession(secrets.token_urlsafe(24), server)
            await session.start()
        # Backstop anti-accumulo: se troppe sessioni attive, chiudi le più vecchie.
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.last_activity)
            logger.warning("Tetto sessioni raggiunto: chiudo la più vecchia %s", oldest.id)
            await self.close(oldest.id)
        self._sessions[session.id] = session
        self._refill_bg(server)  # rimpiazza il processo caldo consumato
        return session

    def get(self, session_id: str) -> MCPSession | None:
        return self._sessions.get(session_id)

    async def reap_loop(self) -> None:
        """Chiude periodicamente le sessioni inattive (evita l'accumulo di subprocess)."""
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.monotonic()
            stale = [sid for sid, s in self._sessions.items() if now - s.last_activity > IDLE_TTL]
            for sid in stale:
                logger.info("Reaper: chiudo sessione inattiva %s", sid)
                await self.close(sid)
            if stale:
                logger.info("Reaper: chiuse %d sessioni; attive ora %d", len(stale), len(self._sessions))

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)
        for pool in self._pools.values():
            for session in pool:
                await session.close()
        self._pools.clear()


manager = SessionManager()
