from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


INSTANCE_ID_PROPERTY = {
    "type": "string",
    "description": (
        "Named Ghost browser instance. Omit to use the shared default instance. "
        "Use different instance IDs for independent Chrome sessions."
    ),
}


def get_ghost_tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="ghost_instance_create",
            description=(
                "Create or reuse a named Ghost browser instance backed by its own "
                "independent Chrome profile. Optionally open the browser immediately "
                "and navigate to a URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": {
                        "type": "string",
                        "description": (
                            "Optional instance name. If omitted, Ghost generates one. "
                            "Reusing the same name attaches to the same independent Chrome session."
                        ),
                    },
                    "open_browser": {
                        "type": "boolean",
                        "description": "Open the browser window immediately (default: true).",
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional URL to open immediately after creation.",
                    },
                    "cdp_url": {
                        "type": "string",
                        "description": (
                            "CDP endpoint to attach to an external browser (e.g. "
                            "\"http://localhost:9222\" for Liquid's embedded Chrome). "
                            "When set, Ghost controls the external browser instead of launching its own. "
                            "Use the special value \"live-chrome\" to attach to the user's currently open "
                            "Chrome session through the live Chrome auto-connect flow."
                        ),
                    },
                    "playwright_session": {
                        "type": "string",
                        "description": (
                            "Attach Ghost to a managed Playwright CLI session instead of a Chrome/CDP target. "
                            "Currently supported values: 'linkedin_auth_a' and 'linkedin_auth_b'."
                        ),
                    },
                    "reuse_only": {
                        "type": "boolean",
                        "description": (
                            "If true, fail with an error if the instance does not already exist instead "
                            "of creating a new one. Use this when you want to operate on an existing tab "
                            "rather than accidentally opening a new browser session. "
                            "Call ghost_instance_list first to see what instances are available."
                        ),
                    },
                },
            },
        ),
        ToolDef(
            name="ghost_instance_list",
            description="List all known Ghost browser instances and their current page state.",
            input_schema={"type": "object", "properties": {}},
        ),
        ToolDef(
            name="ghost_instance_close",
            description=(
                "Close a named Ghost browser instance and unregister it from the local "
                "Ghost runtime. This does not delete its profile directory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": {
                        "type": "string",
                        "description": "The named instance to close.",
                    },
                },
                "required": ["instance_id"],
            },
        ),
        ToolDef(
            name="ghost_vacuum",
            description=(
                "Vacuum the current browser page into a numbered text menu. "
                "Returns ONLY interactive elements (buttons, links, inputs) "
                "grouped by page region. Optionally navigate to a URL first. "
                "Shows first N elements (default 50); use ghost_more for next page. "
                "Use this instead of reading raw page HTML/DOM."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                    "url": {
                        "type": "string",
                        "description": "Optional URL to navigate to before vacuuming. Omit to vacuum the current page.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max elements to show per page (default 50). Use ghost_more for next pages.",
                    },
                },
            },
        ),
        ToolDef(
            name="ghost_more",
            description=(
                "Get the next page of elements from the last vacuum result. "
                "Does NOT re-vacuum the page - uses the cached element list. "
                "Element numbers stay globally consistent (element [75] is always [75])."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                    "offset": {
                        "type": "integer",
                        "description": "Start from this element offset. Omit to continue from where the last page ended.",
                    },
                },
            },
        ),
        ToolDef(
            name="ghost_click",
            description=(
                "Execute an action from the Ghost menu by number. "
                "Clicks buttons/links, fills inputs, toggles checkboxes. "
                "After executing, automatically re-vacuums and returns the updated menu. "
                "Works with ANY element number, even if not shown on the current page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                    "choice": {
                        "type": "integer",
                        "description": "The menu number to execute (e.g., 7 to click element [7]).",
                    },
                    "value": {
                        "type": "string",
                        "description": "Text value for input/search fields. Required for textbox/searchbox elements.",
                    },
                },
                "required": ["choice"],
            },
        ),
        ToolDef(
            name="ghost_status",
            description=(
                "Show current Ghost state for one instance: cached page URL, element count, "
                "browser connection status, and runtime session count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                },
            },
        ),
        ToolDef(
            name="ghost_save_auth",
            description=(
                "Export browser auth (cookies, localStorage) from one instance to linkedin_auth.json. "
                "Call after logging into a site to persist the session across restarts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                },
            },
        ),
        ToolDef(
            name="ghost_eval",
            description=(
                "Run a JavaScript function on the current page and return the result. "
                "Use this to extract elements that the accessibility tree misses (e.g. WhatsApp chat list, "
                "LinkedIn feed items, SPAs with custom renders). "
                "Pass a JS arrow function string: '() => document.title' or "
                "'() => [...document.querySelectorAll(\"span[title]\")].map(e=>e.title).join(\"\\\\n\")'"
            ),
            input_schema={
                "type": "object",
                "required": ["script"],
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                    "script": {
                        "type": "string",
                        "description": "JavaScript arrow function to evaluate, e.g. '() => document.title'",
                    },
                },
            },
        ),
        ToolDef(
            name="ghost_screenshot",
            description=(
                "Take a screenshot of the current browser page. "
                "Optionally scroll to a specific menu element first. "
                "Returns the file path - use Read tool to view the image."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": INSTANCE_ID_PROPERTY,
                    "element": {
                        "type": "integer",
                        "description": "Menu element number to scroll to before taking screenshot. Omit for current viewport.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture entire page (default: false, captures only visible viewport).",
                    },
                },
            },
        ),
    ]
