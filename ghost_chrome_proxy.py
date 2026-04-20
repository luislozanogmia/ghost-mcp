#!/usr/bin/env python3
"""Ghost Chrome Proxy — shared HTTP proxy wrapping chrome-devtools-mcp.

Chrome only allows ONE CDP debugger connection. This proxy holds that single
connection and exposes it to any number of consumers via HTTP on port 8766.
Auto-reconnects when chrome-devtools-mcp subprocess dies or goes stale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

LOG = logging.getLogger("ghost.chrome_proxy")
PORT = 8766
PID_FILE = Path(__file__).parent / "ghost_chrome_proxy.pid"
_RECONNECT_DELAY = 3.0   # seconds between reconnect attempts
_MAX_RECONNECT_ATTEMPTS = 10


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _server_params() -> StdioServerParameters:
    if os.name == "nt":
        return StdioServerParameters(
            command="cmd",
            args=["/c", "npx", "-y", "chrome-devtools-mcp@latest",
                  "--no-usage-statistics", "--autoConnect", "--channel=stable"],
        )
    return StdioServerParameters(
        command="npx",
        args=["-y", "chrome-devtools-mcp@latest",
              "--no-usage-statistics", "--autoConnect", "--channel=stable"],
    )


def _tool_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


class GhostChromeProxy:
    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._session_healthy: bool = False
        self._tools: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/tools", self._handle_tools)
        self._app.router.add_post("/call", self._handle_call)

    def _mark_stale(self) -> None:
        if self._session_healthy:
            LOG.warning("Session marked stale -- scheduling reconnect")
        self._session_healthy = False
        self._session = None

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "connected": self._session_healthy,
            "pid": os.getpid(),
        })

    async def _handle_tools(self, _request: web.Request) -> web.Response:
        return web.json_response({"tools": self._tools})

    async def _handle_call(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"result": None, "error": "invalid JSON body"}, status=400
            )

        name = body.get("name", "")
        arguments = body.get("arguments", {})
        timeout = body.get("timeout", 60)
        page_id = body.get("page_id")

        if not name:
            return web.json_response(
                {"result": None, "error": "missing 'name' field"}, status=400
            )

        if not self._session_healthy or self._session is None:
            return web.json_response(
                {"result": None, "error": "MCP session not connected (reconnecting)"}, status=503
            )

        _NO_PRESELECT = {"select_page", "list_pages", "new_page"}

        async with self._lock:
            try:
                if page_id is not None and name not in _NO_PRESELECT:
                    await self._session.call_tool(
                        "select_page",
                        {"pageId": page_id, "bringToFront": False},
                        read_timeout_seconds=timedelta(seconds=10),
                    )
                result = await asyncio.wait_for(
                    self._session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=timedelta(seconds=timeout),
                    ),
                    timeout=timeout + 5,
                )
                text = _tool_text(result)
                # Empty result from a live session = subprocess went stale
                if not text and name not in {"close_page"}:
                    LOG.warning("Tool %s returned empty result -- session may be stale", name)
                    self._mark_stale()
                    asyncio.ensure_future(self._reconnect_loop())
                    return web.json_response(
                        {"result": None, "error": "session stale, reconnecting"}
                    )
                return web.json_response({"result": text, "error": None})
            except asyncio.TimeoutError:
                self._mark_stale()
                asyncio.ensure_future(self._reconnect_loop())
                return web.json_response(
                    {"result": None, "error": f"timeout after {timeout}s -- reconnecting"}
                )
            except Exception as exc:
                LOG.exception("Tool call %s failed", name)
                self._mark_stale()
                asyncio.ensure_future(self._reconnect_loop())
                return web.json_response({"result": None, "error": str(exc)})

    async def _connect_once(self) -> bool:
        """Attempt a single connection to chrome-devtools-mcp. Returns True on success."""
        try:
            params = _server_params()
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    self._tools = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                        }
                        for t in tools_result.tools
                    ]
                    LOG.info("Connected -- %d tools cached", len(self._tools))
                    self._session = session
                    self._session_healthy = True
                    # Hold until session dies or stop requested
                    await self._stop_event.wait()
        except Exception as exc:
            LOG.warning("Connection attempt failed: %s", exc)
            self._mark_stale()
            return False
        return True

    async def _reconnect_loop(self) -> None:
        """Keep trying to reconnect until successful or stop requested."""
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already reconnecting
        self._reconnect_task = asyncio.current_task()

        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            if self._stop_event.is_set():
                return
            if self._session_healthy:
                return  # already reconnected by another path
            LOG.info("Reconnect attempt %d/%d ...", attempt, _MAX_RECONNECT_ATTEMPTS)
            try:
                params = _server_params()
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
                        self._tools = [
                            {
                                "name": t.name,
                                "description": t.description or "",
                                "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                            }
                            for t in tools_result.tools
                        ]
                        LOG.info("Reconnected -- %d tools cached", len(self._tools))
                        self._session = session
                        self._session_healthy = True
                        await self._stop_event.wait()
                        return
            except Exception as exc:
                LOG.warning("Reconnect attempt %d failed: %s", attempt, exc)
                self._mark_stale()
                await asyncio.sleep(_RECONNECT_DELAY)

        LOG.error("All reconnect attempts exhausted.")

    async def run(self) -> None:
        PID_FILE.write_text(str(os.getpid()))
        LOG.info("PID %d written to %s", os.getpid(), PID_FILE)

        # Start HTTP server first so health checks work during initial connect
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", PORT)
        await site.start()
        LOG.info("Ghost Chrome Proxy HTTP ready on http://127.0.0.1:%d", PORT)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                pass

        # Initial connection with auto-reconnect on failure
        await self._reconnect_loop()

        try:
            await self._stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass

        LOG.info("Shutting down...")
        self._session = None
        self._session_healthy = False
        await runner.cleanup()

        if PID_FILE.exists():
            PID_FILE.unlink()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if _port_in_use(PORT):
        LOG.info("Port %d already in use — proxy already running", PORT)
        sys.exit(0)

    proxy = GhostChromeProxy()

    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        LOG.info("Interrupted")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
