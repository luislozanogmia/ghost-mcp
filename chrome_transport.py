from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from tool_stdio_client import ToolProcessClient


_PAGE_LINE_RE = re.compile(r"^\s*(\d+):\s+(.*?)(\s+\[selected\])?\s*$")
_PROXY_URL = "http://127.0.0.1:8766"
_PROXY_SCRIPT = Path(__file__).parent / "chrome_transport_proxy.py"
_VENV_ROOT = Path(__file__).resolve().parent / ".venv"
_PROXY_PYTHON = (
    _VENV_ROOT / "Scripts" / "python.exe"
    if (_VENV_ROOT / "Scripts" / "python.exe").exists()
    else _VENV_ROOT / "bin" / "python"
)
_DEFAULT_CHROME_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path(os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local")) / "Google/Chrome/Application/chrome.exe",
)


def _parse_pages(text: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _PAGE_LINE_RE.match(line)
        if not match:
            continue
        pages.append(
            {
                "pageId": int(match.group(1)),
                "url": match.group(2).strip(),
                "selected": bool(match.group(3)),
            }
        )
    return pages


def _normalize_page_url(url: Optional[str]) -> str:
    if not url:
        return ""
    normalized = str(url).strip()
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _proxy_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{_PROXY_URL}/health", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("connected"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _resolve_chrome_path() -> str:
    for candidate in _DEFAULT_CHROME_PATHS:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("Could not locate chrome.exe for the Chrome transport.")


def _tool_command(browser_url: Optional[str], auto_connect: bool) -> tuple[str, list[str]]:
    if os.name == "nt":
        args = ["/c", "npx", "-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"]
        command = "cmd"
    else:
        args = ["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"]
        command = "npx"

    if browser_url:
        args.extend(["--browserUrl", browser_url])
    elif auto_connect:
        args.extend(["--autoConnect", "--channel=stable"])
    else:
        raise RuntimeError("Chrome transport has no browser target configured.")

    return command, args


@dataclass
class ChromeTransportRuntime:
    instance_id: str
    context_dir: Path
    browser_url: Optional[str] = None
    auto_connect: bool = False
    logger: Any = None
    _page_id: Optional[int] = None
    _browser_process: Optional[subprocess.Popen] = None
    _browser_debug_port: Optional[int] = None
    _client: Optional[ToolProcessClient] = None

    @property
    def connected(self) -> bool:
        if self.auto_connect:
            return _proxy_is_healthy()
        if self._client is not None and self._client.running:
            return True
        return self._browser_process is not None and self._browser_process.poll() is None

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            self.logger.info(message, *args)

    async def _wait_for_browser_port(self, timeout_seconds: float = 15.0) -> None:
        if not self._browser_debug_port:
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self._browser_debug_port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.25)
        raise RuntimeError(f"Chrome debug port {self._browser_debug_port} did not come up in time.")

    async def _ensure_proxy_running(self) -> None:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{_PROXY_URL}/health", timeout=3.0)
                if resp.status_code == 200:
                    return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass

        self._log("Starting Ghost Chrome Proxy...")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(
            [str(_PROXY_PYTHON), str(_PROXY_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{_PROXY_URL}/health", timeout=2.0)
                    if resp.status_code == 200:
                        self._log("Ghost Chrome Proxy is ready")
                        return
            except (httpx.ConnectError, httpx.TimeoutException):
                continue
        raise RuntimeError("Ghost Chrome Proxy did not start in time")

    async def ensure_browser(self) -> None:
        if self.auto_connect:
            await self._ensure_proxy_running()
            return
        if self.browser_url:
            return
        if self._browser_process is not None and self._browser_process.poll() is None:
            return

        self.context_dir.mkdir(parents=True, exist_ok=True)
        self._browser_debug_port = _find_free_port()
        chrome_path = _resolve_chrome_path()
        command = [
            chrome_path,
            f"--remote-debugging-port={self._browser_debug_port}",
            f"--user-data-dir={self.context_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._browser_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.browser_url = f"http://127.0.0.1:{self._browser_debug_port}"
        self._log(
            "Chrome transport browser launched instance=%s pid=%s browser_url=%s",
            self.instance_id,
            self._browser_process.pid,
            self.browser_url,
        )
        await self._wait_for_browser_port()

    async def _ensure_client(self) -> None:
        if self.auto_connect:
            await self._ensure_proxy_running()
            return

        await self.ensure_browser()
        if self._client is not None and self._client.running:
            return

        if self._client is not None:
            await self._client.close()

        command, args = _tool_command(self.browser_url, False)
        client = ToolProcessClient(command=command, args=args, cwd=Path(__file__).parent)
        await client.initialize()
        self._client = client

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> str:
        if self.auto_connect:
            await self._ensure_proxy_running()
            payload: dict[str, Any] = {"name": name, "arguments": arguments or {}, "timeout": timeout_seconds}
            if self._page_id is not None:
                payload["page_id"] = self._page_id
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{_PROXY_URL}/call",
                    json=payload,
                    timeout=timeout_seconds + 5,
                )
                data = response.json()
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                return str(data.get("result") or "")

        await self._ensure_client()
        assert self._client is not None
        return await self._client.call_tool(name, arguments or {}, timeout_seconds=timeout_seconds)

    async def list_pages(self) -> list[dict[str, Any]]:
        text = await self.call_tool("list_pages", {}, timeout_seconds=20.0)
        pages = _parse_pages(text)
        if self._page_id is not None and not any(page["pageId"] == self._page_id for page in pages):
            self._page_id = None
        return pages

    async def create_tab(self, url: Optional[str] = None) -> dict[str, Any]:
        target_url = url or "about:blank"
        text = await self.call_tool(
            "new_page",
            {"url": target_url, "background": False, "timeout": 15000},
            timeout_seconds=30.0,
        )
        pages = _parse_pages(text) or await self.list_pages()
        page = next((p for p in pages if p.get("selected")), None) or (pages[-1] if pages else None)
        if page is None:
            raise RuntimeError("Failed to open new Chrome tab")
        self._page_id = page["pageId"]
        return page

    async def _select_page(self, page_id: int) -> None:
        await self.call_tool("select_page", {"pageId": page_id, "bringToFront": True}, timeout_seconds=20.0)
        self._page_id = page_id

    async def ensure_page(self, url: Optional[str] = None) -> dict[str, Any]:
        pages = await self.list_pages()
        normalized_target = _normalize_page_url(url)

        if self._page_id is not None:
            page = next((page for page in pages if page["pageId"] == self._page_id), None)
            if page is not None:
                if url:
                    await self.call_tool(
                        "navigate_page",
                        {"type": "url", "url": url, "timeout": 30000},
                        timeout_seconds=45.0,
                    )
                    page["url"] = url
                return page

        if url:
            matching_page = next(
                (
                    page
                    for page in pages
                    if _normalize_page_url(page.get("url")) == normalized_target
                    or _normalize_page_url(page.get("url")).startswith(normalized_target)
                    or normalized_target.startswith(_normalize_page_url(page.get("url")))
                ),
                None,
            )
            if matching_page is not None:
                await self._select_page(matching_page["pageId"])
                matching_page["selected"] = True
                return matching_page

            current = next((page for page in pages if page.get("selected")), None) or (pages[0] if pages else None)
            if current is not None:
                self._page_id = current["pageId"]
                await self.call_tool(
                    "navigate_page",
                    {"type": "url", "url": url, "timeout": 30000},
                    timeout_seconds=45.0,
                )
                current["url"] = url
                return current

        if pages:
            page = next((item for item in pages if item.get("selected")), None) or pages[0]
            self._page_id = page["pageId"]
            return page

        text = await self.call_tool(
            "new_page",
            {"url": "about:blank", "background": False, "timeout": 15000},
            timeout_seconds=30.0,
        )
        pages = _parse_pages(text) or await self.list_pages()
        if not pages:
            raise RuntimeError("Chrome transport could not open an initial page.")
        page = next((item for item in pages if item.get("selected")), None) or pages[-1]
        self._page_id = page["pageId"]
        return page

    async def take_snapshot(self, *, file_path: Optional[str] = None) -> str:
        await self.ensure_page()
        args: dict[str, Any] = {}
        if file_path:
            args["filePath"] = file_path
        return await self.call_tool("take_snapshot", args, timeout_seconds=45.0)

    async def click(self, uid: str) -> str:
        await self.ensure_page()
        return await self.call_tool("click", {"uid": uid, "includeSnapshot": False}, timeout_seconds=30.0)

    async def fill(self, uid: str, value: str) -> str:
        await self.ensure_page()
        return await self.call_tool("fill", {"uid": uid, "value": value, "includeSnapshot": False}, timeout_seconds=30.0)

    async def press_key(self, key: str) -> str:
        await self.ensure_page()
        return await self.call_tool("press_key", {"key": key, "includeSnapshot": False}, timeout_seconds=20.0)

    async def take_screenshot(
        self,
        *,
        file_path: str,
        uid: Optional[str] = None,
        full_page: bool = False,
    ) -> str:
        await self.ensure_page()
        payload: dict[str, Any] = {"filePath": file_path, "format": "png"}
        if uid:
            payload["uid"] = uid
        elif full_page:
            payload["fullPage"] = True
        return await self.call_tool("take_screenshot", payload, timeout_seconds=45.0)

    async def close(self) -> None:
        self._page_id = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self.auto_connect:
            return
        if self._browser_process is not None and self._browser_process.poll() is None:
            self._browser_process.terminate()
            try:
                self._browser_process.wait(timeout=5)
            except Exception:
                self._browser_process.kill()
        self._browser_process = None
        self._browser_debug_port = None
