"""
Ghost runtime host.

This is the supported in-process runtime used by `ghost-cli`. The older server
entrypoints were archived under `deprecated/mcp/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets

_ghost_dir = str(Path(__file__).resolve().parent)
if _ghost_dir not in sys.path:
    sys.path.insert(0, _ghost_dir)

from execute import find_element
from ghost_tool_defs import get_ghost_tools
from chrome_transport import ChromeTransportRuntime
from shared_runtime import SERVER_LOG_FILE, pid_exists, setup_logging
from vacuum import VacuumResult, _build_result, paginate_result, vacuum_from_snapshot_text

LOGGER = setup_logging("ghost.runtime_host", SERVER_LOG_FILE)


def _install_exception_logging() -> None:
    def _log_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        LOGGER.exception("Ghost compatibility server crashed", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = _log_exception


_install_exception_logging()

DEFAULT_INSTANCE_ID = "default"
DEFAULT_LIMIT = 50
GHOST_DIR = Path(__file__).parent
AUTH_PATH = GHOST_DIR / "browser_context" / "linkedin_auth.json"
AUTOMATION_AUTH_PATH = GHOST_DIR / "automations" / "linkedin" / "playwright_auth.json"
LIQUID_CDP_CANDIDATES = (
    "http://127.0.0.1:9222",
    "http://localhost:9222",
)
LIVE_CHROME_SENTINELS = {"live-chrome", "current-chrome", "auto-connect"}
LIQUID_STATUS_CANDIDATES = (
    "http://127.0.0.1:8100/browser/status",
    "http://localhost:8100/browser/status",
)
CDP_WS_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_CONTEXT_DIR = GHOST_DIR / "browser_context"
NAMED_CONTEXT_ROOT = GHOST_DIR / "browser_context_instances"
_playwright = None
_playwright_lock = asyncio.Lock()
_instances_lock = asyncio.Lock()
_instances: dict[str, "GhostInstance"] = {}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _sanitize_error(msg: str) -> str:
    if not msg:
        return msg
    clean = re.sub(r"<[^>]+>", "", str(msg))
    clean = re.sub(r'class="[^"]*"', "", clean)
    clean = re.sub(r'href="[^"]*"', "", clean)
    clean = re.sub(r'aria-\w+="[^"]*"', "", clean)
    clean = re.sub(r'data-\w+="[^"]*"', "", clean)
    clean = re.sub(r'id="[^"]*"', "", clean)
    clean = re.sub(r'style="[^"]*"', "", clean)
    clean = re.sub(r'\w+="[^"]*"', "", clean)
    clean = re.sub(r"=\s*<.*", "", clean, flags=re.DOTALL)
    clean = re.sub(r"resolved to.*", "", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _normalize_instance_id(raw_instance_id: Any | None) -> str:
    if raw_instance_id is None or raw_instance_id == "":
        return DEFAULT_INSTANCE_ID
    if not isinstance(raw_instance_id, str):
        raise ValueError("instance_id must be a string")

    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_instance_id.strip())
    candidate = candidate.strip("._-")
    if not candidate:
        raise ValueError("instance_id cannot be empty after normalization")
    return candidate[:80]


def _generate_instance_id() -> str:
    return f"instance-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _context_dir_for(instance_id: str) -> Path:
    if instance_id == DEFAULT_INSTANCE_ID:
        return DEFAULT_CONTEXT_DIR
    return NAMED_CONTEXT_ROOT / instance_id


def _read_json(url: str, *, timeout: float = 1.0) -> Any | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _looks_like_liquid_cdp(base_url: str) -> bool:
    version_payload = _read_json(f"{base_url}/json/version")
    if not isinstance(version_payload, dict):
        return False

    target_payload = _read_json(f"{base_url}/json/list")
    if isinstance(target_payload, list):
        for target in target_payload:
            if not isinstance(target, dict):
                continue
            target_url = str(target.get("url", ""))
            if "localhost:3001" in target_url or "localhost:8100" in target_url:
                return True

    for status_url in LIQUID_STATUS_CANDIDATES:
        status_payload = _read_json(status_url)
        if isinstance(status_payload, dict) and status_payload.get("status") == "ok":
            return True
    return False


def _detect_liquid_cdp_url() -> Optional[str]:
    for candidate in LIQUID_CDP_CANDIDATES:
        if _looks_like_liquid_cdp(candidate):
            return candidate
    return None


def _is_live_chrome_attach(value: Any | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in LIVE_CHROME_SENTINELS


def _build_ax_tree_from_nodes(nodes: list[dict]) -> Optional[dict]:
    if not nodes:
        return None

    lookup = {}
    for node in nodes:
        node_id = node["nodeId"]
        role_obj = node.get("role", {})
        name_obj = node.get("name", {})
        desc_obj = node.get("description", {})

        converted = {
            "role": role_obj.get("value", ""),
            "name": name_obj.get("value", ""),
            "description": desc_obj.get("value", ""),
        }

        for prop in node.get("properties", []):
            pname = prop.get("name", "")
            pval = prop.get("value", {}).get("value", "")
            if pname in ("checked", "disabled", "expanded", "pressed", "selected"):
                converted[pname] = pval
            elif pname == "description" and pval and not converted["description"]:
                converted["description"] = pval

        if node.get("backendDOMNodeId") is not None:
            converted["backendDOMNodeId"] = node.get("backendDOMNodeId")

        lookup[node_id] = {
            "converted": converted,
            "childIds": node.get("childIds", []),
        }

    root_id = nodes[0]["nodeId"]

    def build_tree(node_id: int):
        entry = lookup.get(node_id)
        if not entry:
            return None
        result = dict(entry["converted"])
        children = []
        for child_id in entry["childIds"]:
            child = build_tree(child_id)
            if child:
                children.append(child)
        if children:
            result["children"] = children
        return result

    return build_tree(root_id)


def _liquid_cdp_targets(base_url: str) -> list[dict[str, Any]]:
    payload = _read_json(f"{base_url}/json/list")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _liquid_webview_target(base_url: str) -> Optional[dict[str, Any]]:
    targets = _liquid_cdp_targets(base_url)
    for target in targets:
        target_url = str(target.get("url", ""))
        if target.get("type") == "webview" and target_url not in ("", "about:blank"):
            return target
    return None


def _is_blocked_liquid_self_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return False

    port = parsed.port
    return port in {3000, 3001, 5173, 8100, 9222}


@asynccontextmanager
async def _cdp_target_connection(ws_url: str):
    async with websockets.connect(ws_url, max_size=CDP_WS_MAX_FRAME_BYTES) as socket:
        next_id = 0

        async def call(method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
            nonlocal next_id
            next_id += 1
            await socket.send(json.dumps({"id": next_id, "method": method, "params": params or {}}))
            while True:
                raw_message = await socket.recv()
                payload = json.loads(raw_message)
                if payload.get("id") != next_id:
                    continue
                if "error" in payload:
                    raise RuntimeError(f"{method} failed: {payload['error']}")
                return payload.get("result", {})

        yield call


async def _ensure_playwright():
    global _playwright

    async with _playwright_lock:
        if _playwright is None:
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()
        return _playwright


async def _stop_playwright() -> None:
    global _playwright

    async with _playwright_lock:
        if _playwright is None:
            return
        playwright_instance = _playwright
        _playwright = None

    try:
        await playwright_instance.stop()
    except Exception:
        LOGGER.exception("Failed to stop Playwright cleanly")


async def _get_ax_tree_cdp(page) -> Optional[dict]:
    cdp = await page.context.new_cdp_session(page)
    try:
        result = await cdp.send("Accessibility.getFullAXTree")
        return _build_ax_tree_from_nodes(result.get("nodes", []))
    finally:
        await cdp.detach()


@dataclass
class GhostInstance:
    instance_id: str
    context_dir: Path
    created_at: str = field(default_factory=_now_iso)
    last_used_at: str = field(default_factory=_now_iso)
    page_limit: int = DEFAULT_LIMIT
    context: Any = None
    page: Any = None
    _browser: Any = None  # only set for CDP connections
    _chrome_transport: Optional[ChromeTransportRuntime] = None
    cdp_url: Optional[str] = None  # e.g. "http://localhost:9222" to attach to external browser
    vacuum_cache: Optional[VacuumResult] = None
    page_url: str = ""
    page_title: str = ""
    current_offset: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    @property
    def browser_connected(self) -> bool:
        return self.context is not None or (self._chrome_transport is not None and self._chrome_transport.connected)

    @property
    def transport_kind(self) -> str:
        if self._liquid_webview_target() is not None:
            return "liquid-cdp"
        if self._chrome_transport is not None and self._chrome_transport.connected:
            return "chrome-transport"
        if self.context is not None:
            return "playwright"
        return "disconnected"

    def _touch(self) -> None:
        self.last_used_at = _now_iso()

    def _reset_runtime_state(self) -> None:
        self.context = None
        self.page = None
        self._browser = None
        self._chrome_transport = None
        self.vacuum_cache = None
        self.page_url = ""
        self.page_title = ""
        self.current_offset = 0
        self.page_limit = DEFAULT_LIMIT

    async def _ensure_browser_locked(self) -> None:
        if self._liquid_webview_target() is None:
            if self._chrome_transport is None:
                attach_live_chrome = _is_live_chrome_attach(self.cdp_url)
                # When no explicit CDP URL is set, default to auto_connect (proxy) instead
                # of launching a fresh Chrome process.
                if not attach_live_chrome and not self.cdp_url:
                    attach_live_chrome = True
                self._chrome_transport = ChromeTransportRuntime(
                    instance_id=self.instance_id,
                    context_dir=self.context_dir,
                    browser_url=None if attach_live_chrome else self.cdp_url,
                    auto_connect=attach_live_chrome,
                    logger=LOGGER,
                )
            await self._chrome_transport.ensure_browser()
            self.context = None
            self.page = None
            self._browser = None
            return

        if self.context is not None:
            try:
                _ = self.context.pages
                return
            except Exception:
                LOGGER.warning("Ghost browser died; reinitializing instance=%s", self.instance_id)
                self._reset_runtime_state()

        playwright = await _ensure_playwright()

        if self.cdp_url:
            # Attach to external browser (e.g. Liquid's Electron) via CDP
            self._browser = await playwright.chromium.connect_over_cdp(self.cdp_url)

            # Search ALL contexts for the webview page (Electron puts webviews
            # in a separate context from the main renderer)
            webview_page = None
            webview_context = None
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    url = page.url
                    # Skip Liquid's own UI, devtools, and blank pages
                    if (url.startswith("devtools://") or
                            "localhost:8100" in url or
                            "localhost:3001" in url or
                            url.startswith("file://") or
                            url in ("about:blank", "")):
                        continue
                    webview_page = page
                    webview_context = ctx
                    break
                if webview_page:
                    break

            if webview_context:
                self.context = webview_context
                self.page = webview_page
            else:
                # Fallback: use first context
                self.context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()

            total_pages = sum(len(ctx.pages) for ctx in self._browser.contexts)
            LOGGER.info("Ghost CDP-attached instance=%s url=%s contexts=%d pages=%d webview=%s",
                        self.instance_id, self.cdp_url, len(self._browser.contexts),
                        total_pages, self.page.url if self.page else "none")
        else:
            # Launch own Chrome with persistent context
            self.context_dir.mkdir(parents=True, exist_ok=True)
            self.context = await playwright.chromium.launch_persistent_context(
                str(self.context_dir),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--remote-debugging-port=0",
                ],
                no_viewport=True,
            )

            if AUTH_PATH.exists():
                try:
                    auth_data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
                    cookies = auth_data.get("cookies", [])
                    if cookies:
                        await self.context.add_cookies(cookies)
                except Exception:
                    LOGGER.exception("Failed to preload saved auth for instance=%s", self.instance_id)

        def _on_close() -> None:
            LOGGER.info("Ghost browser context closed instance=%s", self.instance_id)
            self._reset_runtime_state()

        self.context.on("close", _on_close)
        LOGGER.info("Ghost browser ready instance=%s context=%s", self.instance_id,
                     self.cdp_url or self.context_dir.resolve())

    async def ensure_browser(self) -> None:
        async with self.lock:
            await self._ensure_browser_locked()
            self._touch()

    async def open_new_tab(self, url: Optional[str] = None) -> None:
        """Open a new Chrome tab in the shared browser and pin this instance to it."""
        async with self.lock:
            await self._ensure_browser_locked()
            if self._chrome_transport is not None:
                await self._chrome_transport.create_tab(url)
            self._touch()

    async def _get_active_page_locked(self):
        await self._ensure_browser_locked()
        pages = self.context.pages
        if pages:
            self.page = pages[-1]
        else:
            self.page = await self.context.new_page()
        return self.page

    def _liquid_webview_target(self) -> Optional[dict[str, Any]]:
        if not self.cdp_url or self.cdp_url not in LIQUID_CDP_CANDIDATES:
            return None
        return _liquid_webview_target(self.cdp_url)

    async def _liquid_webview_snapshot_locked(self, url: Optional[str] = None) -> Optional[tuple[dict, str, str]]:
        target = self._liquid_webview_target()
        if not target:
            return None

        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url:
            return None

        async with _cdp_target_connection(ws_url) as call:
            await call("Page.enable")
            await call("Runtime.enable")
            await call("DOM.enable")
            await call("Accessibility.enable")
            if url:
                await call("Page.navigate", {"url": url})
                await asyncio.sleep(2)

            tree_result = await call("Accessibility.getFullAXTree")
            snapshot = _build_ax_tree_from_nodes(tree_result.get("nodes", []))
            if not snapshot:
                return None

            title_result = await call(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
            )
            url_result = await call(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True},
            )
            title = title_result.get("result", {}).get("value", "") or target.get("title", "")
            current_url = url_result.get("result", {}).get("value", "") or target.get("url", "")
            return snapshot, str(current_url), str(title)

    async def _act_on_liquid_webview_element_locked(self, element: dict[str, Any], value: Optional[str] = None) -> str:
        target = self._liquid_webview_target()
        if not target:
            return "Error: Liquid workspace browser is not available."

        backend_node_id = (element.get("node") or {}).get("backendDOMNodeId")
        if backend_node_id is None:
            return "Error: Selected element is missing a backend DOM node id."

        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url:
            return "Error: Liquid workspace browser target is missing a websocket endpoint."

        role = str(element.get("role", ""))
        name = str(element.get("name", ""))

        async with _cdp_target_connection(ws_url) as call:
            await call("Runtime.enable")
            await call("DOM.enable")
            resolved = await call("DOM.resolveNode", {"backendNodeId": int(backend_node_id)})
            object_id = resolved.get("object", {}).get("objectId")
            if not object_id:
                return f"Error: Could not resolve '{name}' in the workspace browser."

            if role in ("textbox", "searchbox", "combobox", "spinbutton"):
                if not value:
                    return f"Error: Element [{element.get('number')}] is a {role} - provide a value."
                function_declaration = """
                    function(value, role) {
                      this.scrollIntoView({ block: 'center', inline: 'center' });
                      this.focus();
                      if (this.tagName === 'SELECT') {
                        this.value = value;
                      } else if ('value' in this) {
                        this.value = value;
                      } else {
                        this.textContent = value;
                      }
                      this.dispatchEvent(new Event('input', { bubbles: true }));
                      this.dispatchEvent(new Event('change', { bubbles: true }));
                      if (role === 'searchbox' || role === 'textbox' || role === 'combobox') {
                        this.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
                        this.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
                        if (this.form && this.form.requestSubmit) {
                          this.form.requestSubmit();
                        }
                      }
                      return true;
                    }
                """
                await call(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": function_declaration,
                        "arguments": [{"value": value}, {"value": role}],
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                action_desc = f"Filled '{name}' with '{value}'"
            else:
                await call(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": """
                            function() {
                              this.scrollIntoView({ block: 'center', inline: 'center' });
                              this.click();
                              return true;
                            }
                        """,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                if role == "link":
                    action_desc = f"Clicked link '{name}'"
                elif role in ("checkbox", "radio"):
                    action_desc = f"Toggled '{name}'"
                else:
                    action_desc = f"Clicked '{name}'"

        await asyncio.sleep(1)
        return action_desc

    async def _screenshot_liquid_webview_locked(self, full_page: bool) -> str:
        target = self._liquid_webview_target()
        if not target:
            return "Error: Liquid workspace browser is not available."

        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url:
            return "Error: Liquid workspace browser target is missing a websocket endpoint."

        screenshots_dir = GHOST_DIR / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        safe_instance_id = re.sub(r"[^A-Za-z0-9._-]+", "-", self.instance_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = screenshots_dir / f"ghost_{safe_instance_id}_{timestamp}.png"

        async with _cdp_target_connection(ws_url) as call:
            await call("Page.enable")
            if full_page:
                await call("Emulation.setDeviceMetricsOverride", {
                    "width": 1440,
                    "height": 2400,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                })
            result = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": bool(full_page)})

        screenshot_bytes = base64.b64decode(result.get("data", ""))
        screenshot_path.write_bytes(screenshot_bytes)
        self._touch()
        return (
            f"Screenshot saved: {screenshot_path.resolve()}\n"
            f"Instance: {self.instance_id}\n"
            f"Page: {self.page_url or target.get('url', 'unknown')}\n"
            "Element: workspace browser viewport"
        )

    async def _vacuum_page_locked(self, url: Optional[str] = None, limit: Optional[int] = None) -> str:
        if self._liquid_webview_target() is not None and url and _is_blocked_liquid_self_url(url):
            return (
                "Error: Refusing to navigate the Liquid-bound Ghost session to a local Liquid/dev URL "
                f"({url}). This session is already attached to the workspace browser. "
                "Use ghost_vacuum with no URL to inspect the current page, or choose an external site."
            )

        await self._ensure_browser_locked()

        liquid_snapshot = await self._liquid_webview_snapshot_locked(url=url)
        if liquid_snapshot is not None:
            snapshot, current_url, title = liquid_snapshot
            self.page_url = current_url
            self.page_title = title

            use_limit = limit if (limit and int(limit) > 0) else DEFAULT_LIMIT
            self.page_limit = int(use_limit)
            self.current_offset = 0

            result = _build_result(snapshot, self.page_url, self.page_title, limit=self.page_limit, offset=0)
            self.vacuum_cache = result
            self.current_offset = min(self.page_limit, result.total_count)
            self._touch()
            return result.menu_text

        if self._chrome_transport is not None:
            page = await self._chrome_transport.ensure_page(url=url)
            self.page_url = str(page.get("url", ""))
            self.page_title = self.page_url or "Chrome Page"

            # Write snapshot to a temp file to avoid transferring the full AX tree
            # as a response payload (which times out on heavy SPAs like WhatsApp).
            # This mirrors the Playwright path which reads the AX tree directly.
            import re as _re, tempfile, os as _os

            async def _take_snapshot_to_file() -> str:
                snap_fd, snap_path = tempfile.mkstemp(prefix="ghost_snap_", suffix=".txt")
                _os.close(snap_fd)
                try:
                    await self._chrome_transport.take_snapshot(file_path=snap_path)
                    with open(snap_path, "r", encoding="utf-8") as fh:
                        return fh.read()
                finally:
                    try:
                        _os.unlink(snap_path)
                    except OSError:
                        pass

            def _use_here_uid(snap: str) -> Optional[str]:
                """Return the uid of the 'Use here' button if the dialog is the only content."""
                m = _re.search(r'(uid=\S+)\s+button\s+"Use here"', snap)
                return m.group(1) if m else None

            snapshot = await _take_snapshot_to_file()

            # Auto-dismiss "WhatsApp is open in another window" dialog.
            # Having two CDP connections (proxy + Claude Code) triggers it on every select_page.
            # Retry up to 3 times: click "Use here" and re-snapshot.
            for _attempt in range(3):
                uid = _use_here_uid(snapshot)
                if uid is None:
                    break
                LOGGER.info("Ghost: dismissing 'Use here' dialog (attempt %d), uid=%s", _attempt + 1, uid)
                await self._chrome_transport.call_tool("click", {"uid": uid, "includeSnapshot": False}, timeout_seconds=10.0)
                await asyncio.sleep(1.5)
                snapshot = await _take_snapshot_to_file()

            if not snapshot:
                return "Error: Could not get browser snapshot."

            use_limit = limit if (limit and int(limit) > 0) else DEFAULT_LIMIT
            self.page_limit = int(use_limit)
            self.current_offset = 0

            result = vacuum_from_snapshot_text(snapshot, url=self.page_url, title=self.page_title)

            # Inject JS-supplemented clickable elements for known SPAs
            from vacuum import _JS_SUPPLEMENTS
            for domain_key, entry in _JS_SUPPLEMENTS.items():
                if domain_key in self.page_url:
                    try:
                        raw = await self._chrome_transport.call_tool(
                            "evaluate_script",
                            {"function": entry["script"]},
                            timeout_seconds=15.0,
                        )
                        import json as _json, re as _re2
                        # chrome-devtools transport wraps output: extract JSON from ```json ... ``` block
                        _m = _re2.search(r'```(?:json)?\s*([\s\S]*?)```', raw or "")
                        raw_json = _m.group(1).strip() if _m else (raw or "").strip()
                        items = _json.loads(raw_json)
                        # double-encoded: script returned JSON.stringify(...)
                        if isinstance(items, str):
                            items = _json.loads(items)
                        if isinstance(items, list) and items:
                            supp_label = entry["label"]
                            supp_elems = []
                            for i, item in enumerate(items):
                                if not isinstance(item, dict):
                                    continue
                                supp_elems.append({
                                    "number": i + 1,
                                    "role": "button",
                                    "name": item.get("name", f"{supp_label} {i+1}"),
                                    "ref": None,
                                    "node": None,
                                    "js_click": item.get("js_click"),
                                })
                            shift = len(supp_elems)
                            # Shift existing element numbers to make room
                            for e in result.elements:
                                e["number"] += shift
                            # Shift landmark_groups
                            new_groups: dict = {}
                            for region, nums in result._landmark_groups.items():
                                new_groups[region] = [n + shift for n in nums]
                            # Add supplement group under its label
                            new_groups[supp_label] = [e["number"] for e in supp_elems]
                            result._landmark_groups = new_groups
                            # Prepend supplement elements
                            result.elements = supp_elems + result.elements
                            result.total_count = len(result.elements)
                            LOGGER.info("Ghost JS supplement: injected %d %s items", shift, supp_label)
                    except Exception as _e:
                        LOGGER.warning("Ghost JS supplement failed for %s: %s", domain_key, _e)
                    break

            result.menu_text = paginate_result(result, 0, self.page_limit)
            self.vacuum_cache = result
            self.current_offset = min(self.page_limit, result.total_count)
            self._touch()
            return result.menu_text

        page = await self._get_active_page_locked()

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        self.page_url = page.url
        self.page_title = await page.title()

        snapshot = await _get_ax_tree_cdp(page)
        if not snapshot:
            return "Error: Could not get accessibility snapshot."

        use_limit = limit if (limit and int(limit) > 0) else DEFAULT_LIMIT
        self.page_limit = int(use_limit)
        self.current_offset = 0

        result = _build_result(snapshot, self.page_url, self.page_title, limit=self.page_limit, offset=0)
        self.vacuum_cache = result
        self.current_offset = min(self.page_limit, result.total_count)
        self._touch()
        return result.menu_text

    async def vacuum_page(self, url: Optional[str] = None, limit: Optional[int] = None) -> str:
        async with self.lock:
            return await self._vacuum_page_locked(url=url, limit=limit)

    async def more(self, offset: Optional[int] = None) -> str:
        async with self.lock:
            if self.vacuum_cache is None:
                return "Error: No page vacuumed yet. Call ghost_vacuum first."

            if offset is not None:
                self.current_offset = int(offset)

            menu_text = paginate_result(self.vacuum_cache, self.current_offset, self.page_limit)
            self.current_offset = min(self.current_offset + self.page_limit, self.vacuum_cache.total_count)
            self._touch()
            return menu_text

    async def click_element(self, choice: int, value: Optional[str] = None) -> str:
        async with self.lock:
            if self.vacuum_cache is None:
                return "Error: No page vacuumed yet. Call ghost_vacuum first."

            element = find_element(self.vacuum_cache, choice)
            if element is None:
                return f"Error: Element [{choice}] not found on page."
            role = element["role"]
            name = element["name"]

            if self._liquid_webview_target() is not None:
                action_desc = await self._act_on_liquid_webview_element_locked(element, value=value)
                if action_desc.startswith("Error:"):
                    return action_desc
                new_menu = await self._vacuum_page_locked()
                return f"Done: {action_desc}\n\n{new_menu}"

            if self._chrome_transport is not None:
                ref = element.get("ref")
                js_click = element.get("js_click")

                # Check if we have js_click (no ref) or ref (standard click)
                if js_click and not ref:
                    # Try to resolve to a real CDP uid via a11y snapshot first
                    # (JS synthetic events fail on React apps like WhatsApp Web)
                    resolved_uid = None
                    try:
                        snapshot_text = await self._chrome_transport.take_snapshot()
                        # Search for element by name in snapshot lines
                        import re as _re
                        name_escaped = _re.escape(name)
                        for line in snapshot_text.splitlines():
                            # Match: uid=<id> ... "name"
                            m = _re.search(r'uid=(\S+).*?"' + name_escaped + r'"', line)
                            if not m:
                                # Also match without quotes
                                m = _re.search(r'uid=(\S+)[^\n]*' + name_escaped, line)
                            if m:
                                resolved_uid = m.group(1)
                                break
                    except Exception:
                        pass

                    if resolved_uid:
                        try:
                            await self._chrome_transport.click(resolved_uid)
                            action_desc = f"Clicked '{name}' (CDP uid={resolved_uid})"
                        except Exception as e:
                            return f"Error: CDP click failed on [{choice}] uid={resolved_uid}: {e}"
                    else:
                        # Fallback: JS-based click for dynamic elements
                        try:
                            await self._chrome_transport.call_tool(
                                "evaluate_script",
                                {"function": f"() => {{ {js_click} }}"},
                                timeout_seconds=30.0,
                            )
                            action_desc = f"Clicked '{name}' (JS)"
                        except Exception as e:
                            return f"Error: Failed to execute JS click on [{choice}]: {e}"
                elif not ref and not js_click:
                    return f"Error: Element [{choice}] is missing a Chrome transport uid or JS click script. Re-vacuum to refresh the page state."
                else:
                    # Standard transport click with ref
                    if role in ("textbox", "searchbox", "combobox", "spinbutton"):
                        if not value:
                            return f"Error: Element [{choice}] is a {role} - provide a value."
                        await self._chrome_transport.fill(ref, value)
                        if role in ("searchbox", "textbox", "combobox"):
                            with suppress(Exception):
                                await self._chrome_transport.press_key("Enter")
                        action_desc = f"Filled '{name}' with '{value}'"
                    elif role in ("checkbox", "radio"):
                        await self._chrome_transport.click(ref)
                        action_desc = f"Toggled '{name}'"
                    elif role == "link":
                        await self._chrome_transport.click(ref)
                        action_desc = f"Clicked link '{name}'"
                    else:
                        await self._chrome_transport.click(ref)
                        action_desc = f"Clicked '{name}'"

                await asyncio.sleep(1)
                new_menu = await self._vacuum_page_locked()
                return f"Done: {action_desc}\n\n{new_menu}"

            page = await self._get_active_page_locked()

            occurrence = 0
            for elem in self.vacuum_cache.elements:
                if elem["number"] == choice:
                    break
                if elem["role"] == role and elem["name"] == name:
                    occurrence += 1

            try:
                if role in ("textbox", "searchbox", "combobox", "spinbutton"):
                    if not value:
                        return f"Error: Element [{choice}] is a {role} - provide a value."
                    locator = page.get_by_role(role, name=name).nth(occurrence)
                    await locator.fill(value, timeout=5000)
                    if role in ("searchbox", "textbox", "combobox"):
                        await locator.press("Enter")
                    action_desc = f"Filled '{name}' with '{value}'"
                elif role in ("checkbox", "radio"):
                    locator = page.get_by_role(role, name=name).nth(occurrence)
                    await locator.click(timeout=5000)
                    action_desc = f"Toggled '{name}'"
                elif role == "link":
                    locator = page.get_by_role("link", name=name).nth(occurrence)
                    url_before = page.url
                    await locator.click(timeout=5000)
                    await asyncio.sleep(1)
                    if page.url != url_before:
                        action_desc = f"Navigated to link '{name}'"
                    else:
                        action_desc = f"Clicked link '{name}'"
                else:
                    locator = page.get_by_role(role, name=name).nth(occurrence)
                    await locator.click(timeout=5000)
                    action_desc = f"Clicked '{name}'"
            except Exception as exc:
                err_str = str(exc)
                if "strict mode violation" in err_str.lower():
                    match_count = sum(
                        1 for elem in self.vacuum_cache.elements if elem["role"] == role and elem["name"] == name
                    )
                    return (
                        f"Error: Multiple elements match '{name}' ({match_count} matches). "
                        "Try a more specific element number."
                    )
                try:
                    await page.get_by_text(name, exact=True).nth(occurrence).click(timeout=5000)
                    action_desc = f"Clicked '{name}' (fallback)"
                except Exception as fallback_err:
                    fallback_str = str(fallback_err)
                    if "timeout" in fallback_str.lower():
                        return (
                            f"Error: Element [{choice}] '{name}' timed out. "
                            "Page may have changed - re-vacuum to get fresh menu."
                        )
                    return f"Error: Could not click [{choice}] '{name}'. Re-vacuum to get fresh menu."

            await asyncio.sleep(1)
            new_menu = await self._vacuum_page_locked()
            return f"Done: {action_desc}\n\n{new_menu}"

    async def screenshot(self, element_num: Optional[int], full_page: bool) -> str:
        async with self.lock:
            if self._liquid_webview_target() is not None:
                return await self._screenshot_liquid_webview_locked(full_page=full_page)

            if self._chrome_transport is not None:
                uid = None
                element_description = "viewport"
                if element_num is not None:
                    if self.vacuum_cache is None:
                        return "Error: No page vacuumed yet. Call ghost_vacuum first."
                    elem = find_element(self.vacuum_cache, int(element_num))
                    if elem is None:
                        return f"Error: Element [{element_num}] not found."
                    uid = elem.get("ref")
                    if not uid:
                        return f"Error: Element [{element_num}] is missing a Chrome transport uid."
                    element_description = f"[{element_num}] {elem['role']} '{elem['name']}'"

                screenshots_dir = GHOST_DIR / "screenshots"
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                safe_instance_id = re.sub(r"[^A-Za-z0-9._-]+", "-", self.instance_id)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = screenshots_dir / f"ghost_{safe_instance_id}_{timestamp}.png"
                await self._chrome_transport.take_screenshot(
                    file_path=str(screenshot_path),
                    uid=uid,
                    full_page=full_page,
                )
                self._touch()
                return (
                    f"Screenshot saved: {screenshot_path.resolve()}\n"
                    f"Instance: {self.instance_id}\n"
                    f"Page: {self.page_url or 'unknown'}\n"
                    f"Element: {element_description}"
                )

            page = await self._get_active_page_locked()
            element_description = "viewport"

            if element_num is not None:
                if self.vacuum_cache is None:
                    return "Error: No page vacuumed yet. Call ghost_vacuum first."

                elem = find_element(self.vacuum_cache, int(element_num))
                if elem is None:
                    return f"Error: Element [{element_num}] not found."

                role = elem["role"]
                name_val = elem["name"]
                element_description = f"[{element_num}] {role} '{name_val}'"

                occurrence = 0
                for cached_element in self.vacuum_cache.elements:
                    if cached_element["number"] == int(element_num):
                        break
                    if cached_element["role"] == role and cached_element["name"] == name_val:
                        occurrence += 1

                try:
                    locator = page.get_by_role(role, name=name_val).nth(occurrence)
                    await locator.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            screenshots_dir = GHOST_DIR / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            safe_instance_id = re.sub(r"[^A-Za-z0-9._-]+", "-", self.instance_id)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshots_dir / f"ghost_{safe_instance_id}_{timestamp}.png"

            await page.screenshot(path=str(screenshot_path), full_page=full_page)
            self._touch()
            return (
                f"Screenshot saved: {screenshot_path.resolve()}\n"
                f"Instance: {self.instance_id}\n"
                f"Page: {self.page_url or page.url}\n"
                f"Element: {element_description}"
            )

    async def save_auth(self) -> str:
        async with self.lock:
            if self._chrome_transport is not None:
                self._touch()
                return (
                    "Chrome transport persists auth in the browser profile automatically. "
                    f"Profile path: {self.context_dir.resolve()}"
                )

            if self.context is None:
                return "Error: No browser context. Call ghost_vacuum first."

            state = await self.context.storage_state()
            linkedin_cookies = [cookie for cookie in state.get("cookies", []) if "linkedin" in cookie.get("domain", "")]
            state["cookies"] = linkedin_cookies
            AUTH_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
            if AUTOMATION_AUTH_PATH.parent.exists():
                AUTOMATION_AUTH_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
            self._touch()
            return f"Saved {len(linkedin_cookies)} LinkedIn cookies to {AUTH_PATH}"

    async def status(self, active_http_sessions: Optional[int]) -> dict[str, Any]:
        async with self.lock:
            cached = self.vacuum_cache is not None
            return {
                "instance_id": self.instance_id,
                "created_at": self.created_at,
                "last_used_at": self.last_used_at,
                "transport": self.transport_kind,
                "browser_connected": self.browser_connected,
                "cdp_url": self.cdp_url or "none",
                "context_dir": str(self.context_dir.resolve()),
                "page_url": self.page_url or "none",
                "page_title": self.page_title or "none",
                "cached_elements": self.vacuum_cache.element_count if cached else 0,
                "total_elements": self.vacuum_cache.total_count if cached else 0,
                "current_offset": self.current_offset,
                "page_limit": self.page_limit,
                "has_more": self.vacuum_cache.has_more if cached else False,
                "active_http_sessions": active_http_sessions,
            }

    async def close_browser(self, reason: str) -> bool:
        async with self.lock:
            context = self.context
            browser = self._browser
            chrome_mcp = self._chrome_transport
            if context is None:
                if chrome_mcp is None:
                    return False

            LOGGER.info("Closing Ghost browser instance=%s: %s", self.instance_id, reason)
            self._reset_runtime_state()

        try:
            if chrome_mcp is not None:
                await chrome_mcp.close()
            elif browser is not None:
                # CDP connection — disconnect, don't close the host browser
                await browser.close()
            else:
                await context.close()
        except Exception:
            LOGGER.exception("Failed to close Ghost browser instance=%s cleanly", self.instance_id)
        return True


def _shared_session_count() -> Optional[int]:
    return None


async def _get_or_create_instance(
    requested_instance_id: Any | None,
    *,
    generate_if_missing: bool = False,
) -> tuple[GhostInstance, bool]:
    requested = requested_instance_id
    if generate_if_missing and (requested is None or requested == ""):
        requested = _generate_instance_id()

    instance_id = _normalize_instance_id(requested)

    async with _instances_lock:
        instance = _instances.get(instance_id)
        if instance is None:
            instance = GhostInstance(instance_id=instance_id, context_dir=_context_dir_for(instance_id))
            _instances[instance_id] = instance
            created = True
        else:
            created = False
    return instance, created


async def _get_instance(requested_instance_id: Any | None) -> GhostInstance | None:
    instance_id = _normalize_instance_id(requested_instance_id)
    async with _instances_lock:
        return _instances.get(instance_id)


async def _list_instances() -> list[GhostInstance]:
    async with _instances_lock:
        return [_instances[key] for key in sorted(_instances)]


async def _close_instance(instance_id: str, reason: str) -> GhostInstance | None:
    normalized_instance_id = _normalize_instance_id(instance_id)
    async with _instances_lock:
        instance = _instances.pop(normalized_instance_id, None)
    if instance is not None:
        await instance.close_browser(reason)
    return instance


async def _close_all_instance_browsers(reason: str) -> None:
    instances = await _list_instances()
    for instance in instances:
        await instance.close_browser(reason)


async def _maybe_attach_default_instance_to_liquid(instance: GhostInstance) -> bool:
    if instance.instance_id != DEFAULT_INSTANCE_ID:
        return False

    if instance.cdp_url and instance.cdp_url not in LIQUID_CDP_CANDIDATES:
        return False

    liquid_cdp_url = _detect_liquid_cdp_url()
    if not liquid_cdp_url:
        return False

    should_switch = False
    if instance.cdp_url != liquid_cdp_url:
        should_switch = True
    elif instance.context is not None and instance._browser is None:
        should_switch = True

    if should_switch and instance.context is not None:
        await instance.close_browser("switching default instance to Liquid CDP")

    instance.cdp_url = liquid_cdp_url
    return True


async def _has_open_browsers() -> bool:
    instances = await _list_instances()
    return any(instance.browser_connected for instance in instances)


async def list_tools():
    return get_ghost_tools()


async def call_tool(name: str, arguments: dict | None) -> str:
    arguments = arguments or {}

    try:
        if name == "ghost_instance_create":
            reuse_only = bool(arguments.get("reuse_only", False))
            instance, created = await _get_or_create_instance(
                arguments.get("instance_id"),
                generate_if_missing=True,
            )
            if reuse_only and created:
                # Immediately tear down the instance we just accidentally created
                await _close_instance(instance.instance_id, "reuse_only=True but instance did not exist")
                available = [i.instance_id for i in await _list_instances()]
                return (
                    f"Error: reuse_only=True but instance '{instance.instance_id}' does not exist. "
                    f"Call ghost_instance_list first to find an existing instance. "
                    f"Available instances: {available}"
                )
            # Set CDP URL if provided (attaches to external browser like Liquid)
            cdp_url = arguments.get("cdp_url")
            if cdp_url and isinstance(cdp_url, str):
                if instance.cdp_url != cdp_url and instance.browser_connected:
                    await instance.close_browser("switching browser attachment target")
                instance.cdp_url = cdp_url
            else:
                await _maybe_attach_default_instance_to_liquid(instance)

            open_browser = bool(arguments.get("open_browser", True))
            url = arguments.get("url")
            if url is not None and not isinstance(url, str):
                return "Error: 'url' must be a string."
            if url:
                if created:
                    # New instance → open a fresh Chrome tab instead of hijacking the current one
                    await instance.open_new_tab(url)
                else:
                    # Existing instance → navigate its pinned tab
                    await instance.vacuum_page(url=url, limit=1)
            elif open_browser:
                await instance.ensure_browser()

            payload = {
                "created": created,
                "instance_id": instance.instance_id,
                "status": await instance.status(_shared_session_count()),
            }
            return json.dumps(payload, indent=2)

        if name == "ghost_instance_list":
            statuses = []
            active_http_sessions = _shared_session_count()
            for instance in await _list_instances():
                statuses.append(await instance.status(active_http_sessions))
            payload = {
                "count": len(statuses),
                "instances": statuses,
            }
            return json.dumps(payload, indent=2)

        if name == "ghost_instance_close":
            requested_instance_id = arguments.get("instance_id")
            if requested_instance_id is None:
                return "Error: 'instance_id' is required."
            instance = await _get_instance(requested_instance_id)
            if instance is None:
                normalized = _normalize_instance_id(requested_instance_id)
                return f"Error: Instance '{normalized}' does not exist."

            previous_status = await instance.status(_shared_session_count())
            await _close_instance(instance.instance_id, "instance closed by tool call")
            payload = {
                "closed": True,
                "instance_id": instance.instance_id,
                "previous_status": previous_status,
            }
            return json.dumps(payload, indent=2)

        instance, _ = await _get_or_create_instance(arguments.get("instance_id"))
        await _maybe_attach_default_instance_to_liquid(instance)

        if name == "ghost_vacuum":
            url = arguments.get("url")
            if url is not None and not isinstance(url, str):
                return "Error: 'url' must be a string."
            limit = arguments.get("limit")
            result = await instance.vacuum_page(url=url, limit=limit)
            return result

        if name == "ghost_more":
            offset = arguments.get("offset")
            result = await instance.more(offset=offset)
            return result

        if name == "ghost_click":
            choice = arguments.get("choice")
            value = arguments.get("value")
            if choice is None:
                return "Error: 'choice' is required."
            result = await instance.click_element(int(choice), value=value)
            return result

        if name == "ghost_status":
            if instance.cdp_url and not instance.browser_connected:
                await instance.ensure_browser()
            status = await instance.status(_shared_session_count())
            return json.dumps(status, indent=2)

        if name == "ghost_screenshot":
            element_num = arguments.get("element")
            full_page = bool(arguments.get("full_page", False))
            result = await instance.screenshot(
                element_num=int(element_num) if element_num is not None else None,
                full_page=full_page,
            )
            return result

        if name == "ghost_eval":
            script = arguments.get("script", "").strip()
            if not script:
                return "Error: 'script' is required."
            if instance._chrome_transport is None:
                return "Error: No browser connected. Call ghost_vacuum first."
            result = await instance._chrome_transport.call_tool(
                "evaluate_script",
                {"function": script},
                timeout_seconds=30.0,
            )
            return result

        if name == "ghost_save_auth":
            result = await instance.save_auth()
            return result

        return f"Unknown tool: {name}"

    except Exception as exc:
        LOGGER.exception("Ghost tool call failed: %s", name)
        return f"Ghost error: {_sanitize_error(str(exc))}"
