"""
EXECUTE -- Ghost Browser v2 action module.

Takes a user's numeric choice from the vacuum menu and executes the
corresponding action on the page (Playwright) or prepares a structured action payload.

Usage:
    # Direct Playwright execution
    result = execute(choice=3, vacuum_result=vr, page=page)
    result = execute(choice=7, vacuum_result=vr, page=page, value="search query")

    # Structured action output for external runtimes
    payload = build_action_payload(choice=3, vacuum_result=vr)

    # Describe what an action would do
    desc = describe_action(element)

    # CLI: test with vacuum JSON
    python execute.py vacuum_output.json 3
    python execute.py vacuum_output.json 7 --value "AI research"
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Optional

from vacuum import VacuumResult, find_element


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExecuteResult:
    """Result of executing an action on the page."""
    success: bool
    action: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

_CLICK_ROLES = frozenset({
    "button", "menuitem", "switch", "tab",
    "menuitemcheckbox", "menuitemradio", "treeitem", "option",
})

_LINK_ROLES = frozenset({"link"})

_FILL_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})

_TOGGLE_ROLES = frozenset({"checkbox", "radio"})

_SLIDER_ROLES = frozenset({"slider"})

# Human-friendly role labels for action descriptions
_ROLE_LABELS = {
    "button": "button",
    "link": "link",
    "textbox": "text field",
    "combobox": "dropdown",
    "checkbox": "checkbox",
    "radio": "radio button",
    "slider": "slider",
    "switch": "switch",
    "menuitem": "menu item",
    "menuitemcheckbox": "menu checkbox",
    "menuitemradio": "menu radio",
    "tab": "tab",
    "searchbox": "search field",
    "spinbutton": "spin button",
    "option": "option",
    "treeitem": "tree item",
}


# ---------------------------------------------------------------------------
# Action description
# ---------------------------------------------------------------------------

def describe_action(element: dict, value: Optional[str] = None) -> str:
    """
    Return a human-readable description of what will happen when
    this element is activated.

    Examples:
        "Click 'Accept Jesus Garcia' button"
        "Fill 'Search' search field"
        "Toggle 'Remember me' checkbox"
        "Navigate to 'Home' link"
    """
    role = element.get("role", "unknown")
    name = element.get("name") or role
    label = _ROLE_LABELS.get(role, role)

    if role in _CLICK_ROLES:
        desc = f"Click '{name}' {label}"
    elif role in _LINK_ROLES:
        desc = f"Navigate to '{name}' {label}"
    elif role in _FILL_ROLES:
        desc = f"Fill '{name}' {label}"
        if value:
            desc += f" with '{value}'"
    elif role in _TOGGLE_ROLES:
        desc = f"Toggle '{name}' {label}"
    elif role in _SLIDER_ROLES:
        desc = f"Set '{name}' {label}"
        if value:
            desc += f" to '{value}'"
    else:
        desc = f"Activate '{name}' {label}"
    return desc


# ---------------------------------------------------------------------------
# Direct Playwright execution
# ---------------------------------------------------------------------------

def execute(
    choice: int,
    vacuum_result: VacuumResult,
    page,
    value: Optional[str] = None,
) -> ExecuteResult:
    """
    Execute a numbered action from the vacuum menu on a Playwright page.

    Args:
        choice: The element number from the vacuum menu.
        vacuum_result: A VacuumResult from the vacuum module.
        page: A Playwright sync Page object.
        value: Text to fill (required for textbox/searchbox/combobox roles).

    Returns:
        ExecuteResult with success status, action description, and any error.
    """
    element = find_element(vacuum_result, choice)
    if element is None:
        return ExecuteResult(
            success=False,
            action="",
            error=f"Element [{choice}] not found in vacuum result.",
        )

    role = element["role"]
    name = element["name"]
    action_desc = describe_action(element, value)

    try:
        # --- Click actions (button, menuitem, switch, tab, etc.) ---
        if role in _CLICK_ROLES:
            page.get_by_role(role, name=name).click()

        # --- Link actions (click + expect navigation) ---
        elif role in _LINK_ROLES:
            try:
                with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=15000
                ):
                    page.get_by_role("link", name=name).click()
            except Exception:
                # Navigation may not trigger (SPA, JS links, anchors).
                # The click itself likely succeeded.
                pass

        # --- Fill actions (textbox, searchbox, combobox, spinbutton) ---
        elif role in _FILL_ROLES:
            locator = page.get_by_role(role, name=name)
            fill_value = value or ""
            locator.fill(fill_value)
            # Press Enter for search boxes to submit
            if role == "searchbox" and fill_value:
                locator.press("Enter")

        # --- Toggle actions (checkbox, radio) ---
        elif role in _TOGGLE_ROLES:
            page.get_by_role(role, name=name).click()

        # --- Slider actions ---
        elif role in _SLIDER_ROLES:
            locator = page.get_by_role("slider", name=name)
            if value:
                locator.fill(value)
            else:
                locator.click()

        # --- Unknown role fallback: try click ---
        else:
            page.get_by_role(role, name=name).click()

        return ExecuteResult(success=True, action=action_desc)

    except Exception as exc:
        return ExecuteResult(
            success=False,
            action=action_desc,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Structured external-runtime execution path
# ---------------------------------------------------------------------------

def build_action_payload(
    choice: int,
    vacuum_result: VacuumResult,
    value: Optional[str] = None,
) -> dict:
    """
    Build a structured action dict for a numbered vacuum menu element.

    Instead of executing directly via Playwright, this returns the
    information needed for the caller to invoke the appropriate browser action.

    Args:
        choice: The element number from the vacuum menu.
        vacuum_result: A VacuumResult from the vacuum module.
        value: Text value for fill/type actions.

    Returns:
        dict with keys:
            ref_id: str or None
            action_type: "click" | "fill" | "type"
            value: str or None
            description: str
        On error, includes an "error" key.
    """
    element = find_element(vacuum_result, choice)
    if element is None:
        return {
            "ref_id": None,
            "action_type": None,
            "value": None,
            "description": "",
            "error": f"Element [{choice}] not found in vacuum result.",
        }

    role = element["role"]
    ref_id = element.get("ref")
    description = describe_action(element, value)

    # Determine action type for the external runtime
    if role in _FILL_ROLES:
        action_type = "fill"
    elif role in _SLIDER_ROLES:
        action_type = "fill" if value else "click"
    else:
        # click, link, toggle, unknown -- all map to click
        action_type = "click"

    return {
        "ref_id": ref_id,
        "action_type": action_type,
        "value": value,
        "description": description,
    }


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

def _cli_main():
    """
    Test execute from CLI with a vacuum JSON file.

    Usage:
        python execute.py vacuum_output.json 3
        python execute.py vacuum_output.json 7 --value "AI research"
        python execute.py vacuum_output.json 5 --payload
    """
    if len(sys.argv) < 3:
        print("Usage: python execute.py <vacuum_json> <choice> [--value TEXT] [--payload]")
        print()
        print("Arguments:")
        print("  vacuum_json  Path to vacuum output JSON")
        print("  choice       Element number to execute")
        print("  --value TEXT  Value for fill/set actions")
        print("  --payload    Show structured action dict instead of Playwright")
        sys.exit(1)

    json_path = sys.argv[1]
    choice = int(sys.argv[2])

    # Parse optional flags
    value = None
    use_payload = "--payload" in sys.argv
    if "--value" in sys.argv:
        vi = sys.argv.index("--value")
        if vi + 1 < len(sys.argv):
            value = sys.argv[vi + 1]

    # Load vacuum JSON
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR loading vacuum JSON: {e}")
        sys.exit(1)

    # Build a minimal VacuumResult from JSON
    vr = VacuumResult(
        menu_text=data.get("menu_text", ""),
        elements=data.get("elements", []),
        page_url=data.get("page_url", ""),
        page_title=data.get("page_title", ""),
        element_count=data.get("element_count", 0),
    )

    # Look up and describe
    element = find_element(vr, choice)
    if element is None:
        print(f"ERROR: Element [{choice}] not found (valid: 1-{vr.element_count})")
        sys.exit(1)

    print(f"Page: {vr.page_title}")
    print(f"URL:  {vr.page_url}")
    print(f"Element [{choice}]: {element['name']} ({element['role']})")
    print(f"Description: {describe_action(element, value)}")
    print()

    if use_payload:
        result = build_action_payload(choice, vr, value=value)
        print("Action Dict:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("[DRY RUN] No live Playwright page provided.")
        print(f"Would execute: {describe_action(element, value)}")
        if value:
            print(f"With value: '{value}'")


if __name__ == "__main__":
    _cli_main()
