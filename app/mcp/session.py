"""Sessione MCP: un subprocess stdio per ogni connessione SSE client.

Il protocollo stdio di MCP usa messaggi JSON-RPC delimitati da newline.
Questa classe avvia il processo, instrada stdin/stdout e mette i messaggi
in uscita (server -> client) in una coda consumata dallo stream SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from app.models import MCPServer

logger = logging.getLogger("mcp.session")


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Risolve i valori 'env:NOME' leggendo dall'ambiente dell'hub."""
    resolved: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(value, str) and value.startswith("env:"):
            resolved[key] = os.environ.get(value[4:], "")
        else:
            resolved[key] = value
    return resolved


class MCPSession:
    def __init__(self, session_id: str, server: MCPServer):
        self.id = session_id
        self.server = server
        self.outbound: asyncio.Queue[str] = asyncio.Queue()
        self.proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._closed = asyncio.Event()
        # Futures in attesa di risposta, indicizzati per id JSON-RPC (transport Streamable HTTP).
        self._pending: dict[object, asyncio.Future] = {}
        # Timestamp ultimo utilizzo (per il reaper delle sessioni inattive).
        self.last_activity: float = time.monotonic()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def start(self) -> None:
        env = {**os.environ, **_resolve_env(self.server.env)}
        logger.info("Avvio server MCP '%s': %s %s", self.server.id, self.server.command, self.server.args)
        self.proc = await asyncio.create_subprocess_exec(
            self.server.command,
            *self.server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # I messaggi MCP sono JSON su una sola riga: alcune risposte (report grandi)
            # superano il limite di default di 64KB del lettore asyncio. Lo alziamo a 32MB.
            limit=32 * 1024 * 1024,
        )
        self._tasks.append(asyncio.create_task(self._pump_stdout()))
        self._tasks.append(asyncio.create_task(self._pump_stderr()))

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # Se la riga è una risposta a una richiesta pendente (per id), risolvi il future;
                # altrimenti (notifiche, messaggi server->client) finisce nella coda outbound.
                fut = None
                try:
                    msg = json.loads(line)
                    if isinstance(msg, dict) and msg.get("id") is not None:
                        fut = self._pending.pop(msg["id"], None)
                except (ValueError, TypeError):
                    pass
                if fut is not None and not fut.done():
                    fut.set_result(line)
                else:
                    await self.outbound.put(line)
        except Exception:  # noqa: BLE001
            logger.exception("Errore lettura stdout (%s)", self.server.id)
        finally:
            self._closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Sessione MCP terminata"))
            self._pending.clear()

    async def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            logger.debug("[%s stderr] %s", self.server.id, raw.decode(errors="replace").rstrip())

    async def send(self, message: str) -> None:
        """Inoltra un messaggio JSON-RPC dal client al processo (stdin)."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Sessione non avviata")
        self.touch()
        self.proc.stdin.write((message.rstrip("\n") + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, message: dict, timeout: float = 120.0) -> str:
        """Invia una richiesta JSON-RPC e attende la risposta con lo stesso id."""
        msg_id = message.get("id")
        if msg_id is None:
            raise ValueError("request() richiede un messaggio con 'id'")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self.send(json.dumps(message))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self.proc.kill()
        self._closed.set()
