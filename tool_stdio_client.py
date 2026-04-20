from __future__ import annotations

import asyncio
import json
import os
from itertools import count
from pathlib import Path
from typing import Any


class ToolProcessError(RuntimeError):
    pass


def extract_text_content(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part for part in parts if part).strip()


class ToolProcessClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = count(1)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=self.env or os.environ.copy(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def initialize(self) -> dict[str, Any]:
        await self.start()
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ghost-cli", "version": "0.2.0"},
            },
        )
        await self.notify("notifications/initialized", {})
        if not isinstance(result, dict):
            raise ToolProcessError("Invalid initialize result from tool process.")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {})
        if not isinstance(result, dict):
            raise ToolProcessError("Invalid tools/list result from tool process.")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ToolProcessError("Tool process returned malformed tool list.")
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> str:
        result = await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, dict):
            raise ToolProcessError("Invalid tools/call result from tool process.")
        if result.get("isError"):
            message = extract_text_content(result) or "Tool call failed."
            raise ToolProcessError(message)
        return extract_text_content(result)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> Any:
        await self.start()
        if self._proc is None or self._proc.stdin is None:
            raise ToolProcessError("Tool process is not running.")

        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        try:
            await self._write_message(message)
            return await asyncio.wait_for(future, timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ToolProcessError(f"Timed out waiting for {method}.") from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ToolProcessError("Tool process stdin is unavailable.")
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + raw)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break

                request_id = message.get("id")
                if request_id is None:
                    continue

                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue

                if "error" in message:
                    error = message["error"]
                    if isinstance(error, dict):
                        future.set_exception(ToolProcessError(str(error.get("message", error))))
                    else:
                        future.set_exception(ToolProcessError(str(error)))
                else:
                    future.set_result(message.get("result"))
        except Exception as exc:
            self._fail_pending(exc)
        finally:
            self._fail_pending(ToolProcessError("Tool process disconnected."))

    async def _read_message(self) -> dict[str, Any] | None:
        assert self._proc is not None and self._proc.stdout is not None
        headers: dict[str, str] = {}

        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("utf-8").strip()
            if not decoded:
                break
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return {}
        body = await self._proc.stdout.readexactly(length)
        return json.loads(body.decode("utf-8"))

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return

    def _fail_pending(self, exc: BaseException) -> None:
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        if self._proc is not None:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), 5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()

        self._proc = None
        self._reader_task = None
        self._stderr_task = None
