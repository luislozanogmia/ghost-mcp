#!/usr/bin/env python3
"""Ghost Chrome Proxy — shared HTTP proxy wrapping the Chrome debug transport.

Chrome only allows one DevTools debugger connection. This proxy holds that
single connection and exposes it to any number of consumers via HTTP on port
8766. Auto-reconnects when the Chrome debug subprocess dies or goes stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from tool_stdio_client import ToolProcessClient


LOG = logging.getLogger("ghost.chrome_proxy")
PORT = 8766
PID_FILE = Path(__file__).parent / "chrome_transport_proxy.pid"
_RECONNECT_DELAY = 3.0
_MAX_RECONNECT_ATTEMPTS = 10


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _tool_command() -> tuple[str, list[str]]:
    if os.name == "nt":
        return (
            "cmd",
            ["/c", "npx", "-y", "chrome-devtools-mcp@latest", "--no-usage-statistics", "--autoConnect", "--channel=stable"],
        )
    return (
        "npx",
        ["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics", "--autoConnect", "--channel=stable"],
    )


class GhostChromeProxy:
    def __init__(self) -> None:
        self._client: ToolProcessClient | None = None
        self._session_healthy = False
        self._tools: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._session_cycle_event = asyncio.Event()
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/tools", self._handle_tools)
        self._app.router.add_post("/call", self._handle_call)

    def _mark_stale(self) -> None:
        if self._session_healthy:
            LOG.warning("Session marked stale -- scheduling reconnect")
        self._session_healthy = False
        self._session_cycle_event.set()

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "connected": self._session_healthy, "pid": os.getpid()})

    async def _handle_tools(self, _request: web.Request) -> web.Response:
        return web.json_response({"tools": self._tools})

    async def _handle_call(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"result": None, "error": "invalid JSON body"}, status=400)

        name = body.get("name", "")
        arguments = body.get("arguments", {})
        timeout = float(body.get("timeout", 60))
        page_id = body.get("page_id")

        if not name:
            return web.json_response({"result": None, "error": "missing 'name' field"}, status=400)
        if not self._session_healthy or self._client is None:
            return web.json_response(
                {"result": None, "error": "Chrome transport not connected (reconnecting)"},
                status=503,
            )

        no_preselect = {"select_page", "list_pages", "new_page"}
        async with self._lock:
            try:
                if page_id is not None and name not in no_preselect:
                    await self._client.call_tool(
                        "select_page",
                        {"pageId": page_id, "bringToFront": False},
                        timeout_seconds=10.0,
                    )
                text = await self._client.call_tool(
                    name,
                    arguments,
                    timeout_seconds=timeout,
                )
                if not text and name not in {"close_page"}:
                    LOG.warning("Tool %s returned empty result -- session may be stale", name)
                    self._mark_stale()
                    return web.json_response({"result": None, "error": "session stale, reconnecting"})
                return web.json_response({"result": text, "error": None})
            except Exception as exc:
                LOG.exception("Tool call %s failed", name)
                self._mark_stale()
                return web.json_response({"result": None, "error": str(exc)})

    async def _connect_once(self) -> bool:
        try:
            command, args = _tool_command()
            client = ToolProcessClient(command=command, args=args, cwd=Path(__file__).parent)
            await client.initialize()
            tools = await client.list_tools()
            self._tools = [
                {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", "") or "",
                    "inputSchema": tool.get("inputSchema", {}) or {},
                }
                for tool in tools
            ]
            self._client = client
            self._session_healthy = True
            self._session_cycle_event.clear()
            LOG.info("Connected -- %d tools cached", len(self._tools))
        except Exception as exc:
            LOG.warning("Connection attempt failed: %s", exc)
            self._mark_stale()
            await self._close_client()
            return False
        return True

    async def _reconnect_loop(self) -> None:
        current_task = asyncio.current_task()
        if self._reconnect_task and self._reconnect_task is not current_task and not self._reconnect_task.done():
            return
        self._reconnect_task = current_task

        try:
            while not self._stop_event.is_set():
                if not self._session_healthy:
                    connected = False
                    for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
                        if self._stop_event.is_set():
                            return
                        LOG.info("Reconnect attempt %d/%d ...", attempt, _MAX_RECONNECT_ATTEMPTS)
                        if await self._connect_once():
                            connected = True
                            break
                        await asyncio.sleep(_RECONNECT_DELAY)
                    if not connected:
                        LOG.error("All reconnect attempts exhausted.")
                        return

                stop_wait = asyncio.create_task(self._stop_event.wait())
                stale_wait = asyncio.create_task(self._session_cycle_event.wait())
                done, pending = await asyncio.wait(
                    {stop_wait, stale_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                if stop_wait in done and self._stop_event.is_set():
                    return
                if stale_wait in done:
                    await self._close_client()
                    continue
        finally:
            self._reconnect_task = None

    async def run(self) -> None:
        PID_FILE.write_text(str(os.getpid()))
        LOG.info("PID %d written to %s", os.getpid(), PID_FILE)

        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", PORT)
        await site.start()
        LOG.info("HTTP proxy listening on http://127.0.0.1:%d", PORT)

        await self._reconnect_loop()
        await self._close_client()
        await runner.cleanup()
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _maybe_exit_if_proxy_running() -> None:
    if _port_in_use(PORT):
        print("Chrome proxy already running.")
        raise SystemExit(0)


def main() -> int:
    _setup_logging()
    _maybe_exit_if_proxy_running()
    proxy = GhostChromeProxy()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_args: Any) -> None:
        proxy._stop_event.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(proxy.run())
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
