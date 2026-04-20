from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional


DEFAULT_CODEX_HOME = Path(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))
DEFAULT_PWCLI = DEFAULT_CODEX_HOME / "skills" / "playwright" / "scripts" / "playwright_cli.sh"
DEFAULT_PWMGR = DEFAULT_CODEX_HOME / "skills" / "playwright" / "scripts" / "playwright_manager.py"
DEFAULT_STATE_PRESET = "linkedin"
APPROVED_PLAYWRIGHT_SESSIONS = {"linkedin_auth_a", "linkedin_auth_b"}


class PlaywrightSessionTransport:
    def __init__(
        self,
        session_id: str,
        *,
        pwcli_path: Path | None = None,
        pw_manager_path: Path | None = None,
        logger: Any = None,
    ) -> None:
        if session_id not in APPROVED_PLAYWRIGHT_SESSIONS:
            raise RuntimeError(
                f"Unsupported Playwright session '{session_id}'. "
                f"Allowed: {sorted(APPROVED_PLAYWRIGHT_SESSIONS)}"
            )
        self.session_id = session_id
        self.pwcli_path = pwcli_path or DEFAULT_PWCLI
        self.pw_manager_path = pw_manager_path or DEFAULT_PWMGR
        self.logger = logger
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            self.logger.info(message, *args)

    async def _run(self, cmd: list[str]) -> str:
        def _invoke() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

        proc = await asyncio.to_thread(_invoke)
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise RuntimeError(output or f"command failed: {' '.join(cmd)}")
        return output

    async def _run_pwcli(self, *args: str, raw: bool = False) -> str:
        cmd = [str(self.pwcli_path), "--session", self.session_id]
        if raw:
            cmd.append("--raw")
        cmd.extend(args)
        return await self._run(cmd)

    async def _run_pw_manager(self, *args: str) -> str:
        cmd = ["python3", str(self.pw_manager_path), *args]
        return await self._run(cmd)

    async def ensure_browser(self) -> None:
        if not self.pwcli_path.exists():
            raise RuntimeError(f"pwcli not found: {self.pwcli_path}")
        if not self.pw_manager_path.exists():
            raise RuntimeError(f"playwright manager not found: {self.pw_manager_path}")

        await self._run_pw_manager(
            "--session",
            self.session_id,
            "--ensure-session",
            "--state-preset",
            DEFAULT_STATE_PRESET,
        )
        self._connected = True
        self._log("Playwright session transport ready session=%s", self.session_id)

    async def close(self) -> None:
        self._connected = False

    async def goto(self, url: str) -> None:
        await self.ensure_browser()
        await self._run_pwcli("goto", url)

    async def snapshot(self) -> str:
        await self.ensure_browser()
        return await self._run_pwcli("snapshot", raw=True)

    async def click(self, ref: str) -> None:
        await self.ensure_browser()
        await self._run_pwcli("click", ref, raw=True)

    async def fill(self, ref: str, value: str) -> None:
        await self.ensure_browser()
        await self._run_pwcli("fill", ref, value, raw=True)

    async def press_key(self, key: str) -> None:
        await self.ensure_browser()
        await self._run_pwcli("press", key, raw=True)

    async def evaluate_script(self, script: str) -> str:
        await self.ensure_browser()
        return await self._run_pwcli("eval", script, raw=True)

    async def page_info(self) -> dict[str, Any]:
        raw = await self.evaluate_script(
            "() => ({title: document.title, href: location.href})"
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"title": "", "href": ""}

    async def take_screenshot(self, file_path: str, *, full_page: bool = False) -> str:
        await self.ensure_browser()
        args = ["screenshot", "--filename", file_path]
        if full_page:
            args.append("--full-page")
        output = await self._run_pwcli(*args, raw=True)
        match = re.search(r"\(([^)]+\.(?:png|jpeg|jpg))\)", output)
        return match.group(1) if match else output
