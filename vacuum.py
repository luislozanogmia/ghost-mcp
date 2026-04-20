"""
VACUUM — Ghost Browser v2 core module.

Reads a live web page via Playwright's accessibility tree and returns
a clean, numbered text menu of all interactive elements.

Transpiles the raw a11y tree into a minimal, AI-friendly text menu.

Usage:
    # From a Playwright page object
    result = vacuum(page)

    # From a raw accessibility tree dict
    result = vacuum_from_tree(tree_dict)

    # From raw page snapshot text
    result = vacuum_from_snapshot_text(snapshot_text)

    # CLI: vacuum a URL
    python vacuum.py https://example.com
"""

from __future__ import annotations

import re
import sys
import json
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "combobox", "checkbox",
    "radio", "slider", "switch", "menuitem", "tab",
    "searchbox", "spinbutton", "option", "menuitemcheckbox",
    "menuitemradio", "treeitem",
})

LANDMARK_MAP = {
    "banner": "NAV",
    "navigation": "NAV",
    "main": "MAIN",
    "contentinfo": "FOOTER",
    "complementary": "ASIDE",
    "form": "FORM",
    "search": "SEARCH",
    "region": "SECTION",
}

# Roles that map to a nice display label
ROLE_DISPLAY = {
    "button": "button",
    "link": "link",
    "textbox": "input",
    "combobox": "dropdown",
    "checkbox": "checkbox",
    "radio": "radio",
    "slider": "slider",
    "switch": "switch",
    "menuitem": "menu",
    "menuitemcheckbox": "menu",
    "menuitemradio": "menu",
    "tab": "tab",
    "searchbox": "search",
    "spinbutton": "spin",
    "option": "option",
    "treeitem": "tree",
}

MAX_NAME_LEN = 50
ROLE_COL_WIDTH = 12  # width for right-aligned role column


# ---------------------------------------------------------------------------
# JS Supplement Registry
# ---------------------------------------------------------------------------

_JS_SUPPLEMENTS = {
    "web.whatsapp.com": {
        "label": "WhatsApp Chat",
        "script": """() => {
            const cells = document.querySelectorAll('div[role="row"]');
            return JSON.stringify([...cells].slice(0, 30).map((el, i) => ({
                name: el.querySelector('span[title]')?.title || el.querySelector('span[dir]')?.innerText || el.innerText.split('\\n')[0] || 'Chat ' + i,
                js_click: `document.querySelectorAll('div[role="row"]')[${i}].click()`
            })));
        }"""
    },
    "mail.google.com": {
        "label": "Gmail Email",
        "script": """() => {
            const rows = document.querySelectorAll('tr.zA');
            return [...rows].slice(0, 30).map((el, i) => ({
                name: (el.querySelector('.yX.xY span')?.getAttribute('name') || 'Sender') + ': ' + (el.querySelector('.y6 span')?.innerText || 'Subject'),
                js_click: `document.querySelectorAll('tr.zA')[${i}].click()`
            }));
        }"""
    },
    "linkedin.com": {
        "label": "LinkedIn Item",
        "script": """() => {
            const items = document.querySelectorAll('.scaffold-finite-scroll__content li');
            return [...items].slice(0, 20).map((el, i) => ({
                name: el.querySelector('span[aria-hidden="true"]')?.innerText || el.innerText.split('\\n')[0] || 'Item ' + i,
                js_click: `document.querySelectorAll('.scaffold-finite-scroll__content li')[${i}].click()`
            }));
        }"""
    }
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VacuumElement:
    """A single interactive element extracted from the page."""
    number: int
    role: str
    name: str
    ref: Optional[str] = None
    node: Optional[dict] = None


@dataclass
class VacuumResult:
    """Result of vacuuming a page."""
    menu_text: str
    elements: list[dict] = field(default_factory=list)
    page_url: str = ""
    page_title: str = ""
    element_count: int = 0
    total_count: int = 0       # total elements before pagination
    has_more: bool = False      # True if more elements exist beyond current page
    # Internal: landmark_groups preserved for re-pagination without re-vacuum
    _landmark_groups: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------

def _truncate(name: str, max_len: int = MAX_NAME_LEN) -> str:
    """Truncate a name and add ellipsis if too long."""
    if len(name) <= max_len:
        return name
    return name[: max_len - 3] + "..."


def _clean_name(name: Optional[str]) -> str:
    """Normalize whitespace in a name."""
    if not name:
        return ""
    return " ".join(name.split())


def _detect_landmark(node: dict) -> Optional[str]:
    """Detect ARIA landmark from a node's role."""
    role = (node.get("role") or "").lower()
    return LANDMARK_MAP.get(role)


def _collect_child_text(node: dict, max_depth: int = 3) -> str:
    """
    Recursively collect text from child nodes of an interactive element.

    When CDP returns a link with an empty 'name' but the link wraps text nodes
    (e.g., person names on LinkedIn), this gathers that text. Limited depth
    prevents runaway recursion on deep trees.
    """
    if max_depth <= 0:
        return ""
    parts = []
    for child in node.get("children", []):
        child_role = (child.get("role") or "").lower()
        child_name = _clean_name(child.get("name"))
        # Only collect text from non-interactive children (statictext, img alt, etc.)
        # Interactive children will be collected as their own elements
        if child_role not in INTERACTIVE_ROLES and child_name:
            parts.append(child_name)
        elif child_role not in INTERACTIVE_ROLES:
            deeper = _collect_child_text(child, max_depth - 1)
            if deeper:
                parts.append(deeper)
    return " ".join(parts)


def _walk_tree(
    node: dict,
    elements: list[VacuumElement],
    counter: list[int],
    current_landmark: Optional[str],
    landmark_groups: dict[str, list[int]],
    parent_name: str = "",
):
    """
    Recursively walk the accessibility tree.

    Collects interactive elements into `elements`, tracks which landmark
    region each element belongs to via `landmark_groups`.
    """
    role = (node.get("role") or "").lower()
    name = _clean_name(node.get("name"))

    # Check if this node defines a landmark region
    landmark = _detect_landmark(node)
    if landmark:
        current_landmark = landmark

    # Check if this is an interactive element we care about
    if role in INTERACTIVE_ROLES:
        # Build display name with multiple fallback sources
        display_name = name
        # Fallback 1: CDP description property (often has link text on dynamic pages)
        if not display_name:
            display_name = _clean_name(node.get("description"))
        # Fallback 2: concatenate text from child nodes (catches person names in links)
        if not display_name and role == "link":
            display_name = _collect_child_text(node)
        # Fallback 3: parent name
        if not display_name:
            display_name = parent_name
        # Fallback 4: just use the role
        if not display_name:
            display_name = role  # last resort: just use the role

        counter[0] += 1
        num = counter[0]

        elem = VacuumElement(
            number=num,
            role=role,
            name=_truncate(display_name),
            ref=node.get("ref", None),
            node=node,
        )
        elements.append(elem)

        # Track which landmark group this element belongs to
        region = current_landmark or "OTHER"
        if region not in landmark_groups:
            landmark_groups[region] = []
        landmark_groups[region].append(num)

    # Recurse into children
    children = node.get("children", [])
    # Use current node's name as parent context for unnamed children
    ctx_name = name if name else parent_name
    for child in children:
        _walk_tree(child, elements, counter, current_landmark, landmark_groups, ctx_name)


# ---------------------------------------------------------------------------
# Menu formatting
# ---------------------------------------------------------------------------

# Preferred display order for landmark groups
_REGION_ORDER = ["NAV", "SEARCH", "MAIN", "FORM", "SECTION", "ASIDE", "FOOTER", "OTHER"]


def _format_menu(
    elements: list[VacuumElement],
    landmark_groups: dict[str, list[int]],
    page_title: str,
    page_url: str,
    offset: int = 0,
    limit: int = 0,
    total: int = 0,
    is_continuation: bool = False,
) -> str:
    """
    Format the extracted elements into the text menu.

    If limit > 0, only elements with numbers in (offset, offset+limit] are shown.
    Element numbers stay globally consistent regardless of pagination.
    """
    lines: list[str] = []

    # Determine visible range
    if limit > 0:
        vis_start = offset + 1        # first visible number (1-based)
        vis_end = offset + limit       # last visible number (inclusive)
    else:
        vis_start = 1
        vis_end = float("inf")

    visible_nums = set()
    for e in elements:
        if vis_start <= e.number <= vis_end:
            visible_nums.add(e.number)

    # Header
    title_display = page_title or "Untitled"
    if is_continuation:
        lines.append(f"--- continued from {title_display} ---")
    else:
        lines.append(f"=== {title_display} ===")
        if page_url:
            lines.append(f"URL: {page_url}")
    lines.append("")

    # Build a lookup from element number to element
    elem_by_num: dict[int, VacuumElement] = {e.number: e for e in elements}

    # Determine the width needed for the number column (use global max, not page max)
    max_num = max((e.number for e in elements), default=0)
    num_width = len(str(max_num))

    # Track which elements have been printed (to avoid duplicates)
    printed: set[int] = set()

    # Print by region in preferred order
    for region in _REGION_ORDER:
        nums = landmark_groups.get(region, [])
        if not nums:
            continue

        # Only print region header if it has visible elements
        region_visible = [n for n in nums if n in visible_nums and n not in printed]
        if not region_visible:
            continue

        lines.append(f"[{region}]")
        for num in nums:
            if num in printed or num not in visible_nums:
                continue
            printed.add(num)
            elem = elem_by_num[num]
            role_label = ROLE_DISPLAY.get(elem.role, elem.role)
            # Format: [N] Name                           (role)
            num_str = f"[{elem.number}]".ljust(num_width + 2)
            role_str = f"({role_label})"
            name_part = f"  {num_str} {elem.name}"
            # Pad to align role labels
            pad_target = 52
            if len(name_part) < pad_target:
                name_part = name_part.ljust(pad_target)
            lines.append(f"{name_part} {role_str}")
        lines.append("")

    # Print any elements not yet covered (shouldn't happen, but safety net)
    remaining = [e for e in elements if e.number not in printed and e.number in visible_nums]
    if remaining:
        lines.append("[OTHER]")
        for elem in remaining:
            role_label = ROLE_DISPLAY.get(elem.role, elem.role)
            num_str = f"[{elem.number}]".ljust(num_width + 2)
            role_str = f"({role_label})"
            name_part = f"  {num_str} {elem.name}"
            pad_target = 52
            if len(name_part) < pad_target:
                name_part = name_part.ljust(pad_target)
            lines.append(f"{name_part} {role_str}")
        lines.append("")

    # Footer — show pagination info if paginated
    actual_total = total if total else len(elements)
    if limit > 0 and actual_total > limit:
        shown_start = offset + 1
        shown_end = min(offset + limit, actual_total)
        has_more = shown_end < actual_total
        more_hint = " (ghost_more for next)" if has_more else ""
        lines.append(f"--- showing {shown_start}-{shown_end} of {actual_total} actions{more_hint} ---")
    else:
        lines.append(f"--- {actual_total} actions available ---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _get_ax_tree_cdp_sync(page) -> Optional[dict]:
    """
    Get accessibility tree via CDP session (sync API).
    Returns {role, name, children} tree or None.
    """
    cdp = page.context.new_cdp_session(page)
    try:
        result = cdp.send("Accessibility.getFullAXTree")
        nodes = result.get("nodes", [])
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
                # Fallback: some CDP versions put description in properties
                elif pname == "description" and pval and not converted["description"]:
                    converted["description"] = pval
            lookup[node_id] = {
                "converted": converted,
                "childIds": node.get("childIds", []),
            }

        root_id = nodes[0]["nodeId"]

        def build_tree(nid):
            entry = lookup.get(nid)
            if not entry:
                return None
            result = dict(entry["converted"])
            children = []
            for cid in entry["childIds"]:
                child = build_tree(cid)
                if child:
                    children.append(child)
            if children:
                result["children"] = children
            return result

        return build_tree(root_id)
    finally:
        cdp.detach()


def vacuum(page, limit: int = 50, offset: int = 0) -> VacuumResult:
    """
    Vacuum a Playwright page (sync API).

    Takes a sync Playwright Page object, snapshots the accessibility tree,
    and returns a VacuumResult with the formatted menu and element list.
    All elements are collected; limit/offset control what appears in menu_text.
    """
    # Get accessibility tree via CDP (page.accessibility removed in Playwright 1.50+)
    tree = _get_ax_tree_cdp_sync(page)
    if tree is None:
        return VacuumResult(
            menu_text="=== Empty Page ===\n\n--- 0 actions available ---",
            elements=[],
            page_url=page.url,
            page_title=page.title(),
            element_count=0,
        )

    url = page.url
    title = page.title()

    return _build_result(tree, url, title, limit=limit, offset=offset)


def vacuum_from_tree(
    tree_dict: dict,
    url: str = "",
    title: str = "",
    limit: int = 50,
    offset: int = 0,
) -> VacuumResult:
    """
    Vacuum from a raw accessibility tree snapshot (dict).

    Accepts the same structure as Playwright's page.accessibility.snapshot().
    Works with data from any source: browser transport, CDP session, saved snapshot, etc.
    """
    if not tree_dict:
        return VacuumResult(
            menu_text="=== Empty Page ===\n\n--- 0 actions available ---",
            elements=[],
            page_url=url,
            page_title=title,
            element_count=0,
        )

    # Try to extract title from tree root if not provided
    if not title:
        title = _clean_name(tree_dict.get("name")) or "Untitled"

    return _build_result(tree_dict, url, title, limit=limit, offset=offset)


def vacuum_from_snapshot_text(snapshot_text: str, url: str = "", title: str = "") -> VacuumResult:
    """
    Parse raw page snapshot text into a VacuumResult.

    The parsed snapshot format looks like:
        - button "Sign In" [ref=e23]
        - link "Home" [ref=e45]
        - textbox "Search..." [ref=e67]

    Also handles formats like:
        button [ref_1]
        link "name" [ref_2]
        searchbox "Search" [ref_3]
    """
    elements: list[VacuumElement] = []
    landmark_groups: dict[str, list[int]] = {}
    counter = 0
    current_region = "MAIN"

    # Patterns for supported snapshot formats
    # Format 0: uid=1_3 link "Learn more" url="..."
    uid_named_pat = re.compile(
        r'uid=([^\s]+)\s+([A-Za-z][\w-]*)\s+"([^"]*)"',
        re.IGNORECASE,
    )
    # Format 0b: uid=1_7 button
    uid_unnamed_pat = re.compile(
        r'uid=([^\s]+)\s+([A-Za-z][\w-]*)\b',
        re.IGNORECASE,
    )
    # Format 1: - role "name" [ref=XXX]
    pat1 = re.compile(
        r'[-*]?\s*(\w+)\s+"([^"]*)"\s*\[ref[=_](\w+)\]',
        re.IGNORECASE,
    )
    # Format 2: role [ref_N]  (unnamed)
    pat2 = re.compile(
        r'[-*]?\s*(\w+)\s*\[ref[=_](\w+)\]',
        re.IGNORECASE,
    )
    # Format 3: role "name" (no ref)
    pat3 = re.compile(
        r'[-*]?\s*(\w+)\s+"([^"]*)"',
        re.IGNORECASE,
    )
    # Detect section headers like "Navigation:", "Main content:", etc.
    section_pat = re.compile(
        r'^(?:[-=#]*\s*)?(navigation|banner|main|content|footer|sidebar|header|complementary|search)\b',
        re.IGNORECASE,
    )

    section_to_region = {
        "navigation": "NAV",
        "banner": "NAV",
        "header": "NAV",
        "main": "MAIN",
        "content": "MAIN",
        "footer": "FOOTER",
        "sidebar": "ASIDE",
        "complementary": "ASIDE",
        "search": "SEARCH",
    }

    for line in snapshot_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Check for section headers
        sec_match = section_pat.match(stripped)
        if sec_match:
            sec_name = sec_match.group(1).lower()
            current_region = section_to_region.get(sec_name, "MAIN")
            continue

        # Chrome DevTools snapshot format: uid=<id> role "name"
        m = uid_named_pat.search(stripped)
        if m:
            ref = m.group(1)
            role = m.group(2).lower()
            name = m.group(3)
            if role not in INTERACTIVE_ROLES:
                landmark = LANDMARK_MAP.get(role)
                if landmark:
                    current_region = landmark
                continue
            counter += 1
            elem = VacuumElement(
                number=counter,
                role=role,
                name=_truncate(_clean_name(name)) or role,
                ref=ref,
            )
            elements.append(elem)
            landmark_groups.setdefault(current_region, []).append(counter)
            continue

        # Chrome DevTools snapshot format: uid=<id> role
        m = uid_unnamed_pat.search(stripped)
        if m:
            ref = m.group(1)
            role = m.group(2).lower()
            if role not in INTERACTIVE_ROLES:
                landmark = LANDMARK_MAP.get(role)
                if landmark:
                    current_region = landmark
                continue
            counter += 1
            elem = VacuumElement(
                number=counter,
                role=role,
                name=role,
                ref=ref,
            )
            elements.append(elem)
            landmark_groups.setdefault(current_region, []).append(counter)
            continue

        # Try format 1: role "name" [ref=X]
        m = pat1.search(stripped)
        if m:
            role = m.group(1).lower()
            name = m.group(2)
            ref_raw = m.group(3)
            # Ensure ref has ref_ prefix for structured-action compatibility
            ref = f"ref_{ref_raw}" if not ref_raw.startswith("ref_") else ref_raw
            # Non-interactive roles may indicate landmarks
            if role not in INTERACTIVE_ROLES:
                landmark = LANDMARK_MAP.get(role)
                if landmark:
                    current_region = landmark
                continue
            counter += 1
            elem = VacuumElement(
                number=counter,
                role=role,
                name=_truncate(_clean_name(name)) or role,
                ref=ref,
            )
            elements.append(elem)
            landmark_groups.setdefault(current_region, []).append(counter)
            continue

        # Try format 2: role [ref_N]
        m = pat2.search(stripped)
        if m:
            role = m.group(1).lower()
            ref_raw = m.group(2)
            ref = f"ref_{ref_raw}" if not ref_raw.startswith("ref_") else ref_raw
            # Non-interactive roles may indicate landmarks
            if role not in INTERACTIVE_ROLES:
                landmark = LANDMARK_MAP.get(role)
                if landmark:
                    current_region = landmark
                continue
            # Skip unnamed elements (noise like unnamed buttons)
            clean = _clean_name(None)
            counter += 1
            elem = VacuumElement(
                number=counter,
                role=role,
                name=role,
                ref=ref,
            )
            elements.append(elem)
            landmark_groups.setdefault(current_region, []).append(counter)
            continue

        # Try format 3: role "name" (no ref)
        m = pat3.search(stripped)
        if m:
            role = m.group(1).lower()
            name = m.group(2)
            if role in INTERACTIVE_ROLES:
                counter += 1
                elem = VacuumElement(
                    number=counter,
                    role=role,
                    name=_truncate(_clean_name(name)) or role,
                )
                elements.append(elem)
                landmark_groups.setdefault(current_region, []).append(counter)

    total = len(elements)
    menu_text = _format_menu(elements, landmark_groups, title or "Ghost Page", url)

    elem_dicts = [
        {"number": e.number, "role": e.role, "name": e.name, "ref": e.ref, "node": None, "js_click": None}
        for e in elements
    ]

    return VacuumResult(
        menu_text=menu_text,
        elements=elem_dicts,
        page_url=url,
        page_title=title or "Ghost Page",
        element_count=len(elements),
        total_count=total,
        has_more=False,
        _landmark_groups=landmark_groups,
    )


def _apply_js_supplements(
    url: str,
    elements: list[VacuumElement],
    elem_dicts: list[dict],
) -> None:
    """
    Check if the current URL has a JS supplement registry entry.
    If so, run the supplement script and inject results as additional elements.

    Mutates elem_dicts in place, appending new elements with js_click set.
    """
    # Find matching supplement by checking if URL contains the registry key
    registry_entry = None
    for domain_key, entry in _JS_SUPPLEMENTS.items():
        if domain_key in url:
            registry_entry = entry
            break

    if registry_entry is None:
        return

    # Note: This function is called from vacuum.py context.
    # In production use, the page object would be passed to execute the script.
    # For now, this is a placeholder that shows the structure.
    # The actual script execution happens in the runtime host when vacuum is called.
    pass


def _build_result(
    tree: dict,
    url: str,
    title: str,
    limit: int = 50,
    offset: int = 0,
) -> VacuumResult:
    """
    Shared builder: walk tree, format menu, return VacuumResult.

    Always collects ALL elements internally. If limit > 0, the menu_text
    only shows elements[offset:offset+limit] but the full element list is
    stored in VacuumResult.elements for click/lookup.
    """
    elements: list[VacuumElement] = []
    landmark_groups: dict[str, list[int]] = {}
    counter = [0]  # mutable for recursion

    _walk_tree(tree, elements, counter, None, landmark_groups)

    total = len(elements)

    menu_text = _format_menu(
        elements, landmark_groups, title, url,
        offset=offset, limit=limit, total=total,
    )

    # Convert elements to dicts for the result
    elem_dicts = [
        {
            "number": e.number,
            "role": e.role,
            "name": e.name,
            "ref": e.ref,
            "node": e.node,
            "js_click": None,
        }
        for e in elements
    ]

    # Apply JS supplements if URL matches a registry entry
    _apply_js_supplements(url, elements, elem_dicts)

    has_more = (limit > 0) and (offset + limit < total)

    return VacuumResult(
        menu_text=menu_text,
        elements=elem_dicts,
        page_url=url,
        page_title=title,
        element_count=len(elements),
        total_count=total,
        has_more=has_more,
        _landmark_groups=landmark_groups,
    )


def paginate_result(result: VacuumResult, offset: int, limit: int) -> str:
    """
    Re-format the menu_text for a different page of the SAME cached result.

    No re-vacuum needed — uses the stored elements and landmark_groups from
    the original VacuumResult. Returns formatted menu text for the requested
    page slice.
    """
    # Reconstruct VacuumElement objects from the stored element dicts
    elements = [
        VacuumElement(
            number=e["number"],
            role=e["role"],
            name=e["name"],
            ref=e.get("ref"),
            node=e.get("node"),
        )
        for e in result.elements
    ]

    total = result.total_count or len(elements)

    return _format_menu(
        elements,
        result._landmark_groups,
        result.page_title,
        result.page_url,
        offset=offset,
        limit=limit,
        total=total,
        is_continuation=True,
    )


# ---------------------------------------------------------------------------
# Element lookup
# ---------------------------------------------------------------------------

def find_element(result: VacuumResult, number: int) -> Optional[dict]:
    """Look up an element by its menu number. Returns the element dict or None."""
    for elem in result.elements:
        if elem["number"] == number:
            return elem
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main():
    """Launch headless Chrome, vacuum a URL, print the menu."""
    if len(sys.argv) < 2:
        print("Usage: python vacuum.py <URL> [--json]")
        print("       python vacuum.py https://example.com")
        print("       python vacuum.py https://example.com --json")
        sys.exit(1)

    url = sys.argv[1]
    output_json = "--json" in sys.argv

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed.")
        print("  pip install playwright")
        print("  playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"Loading: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Give dynamic content a moment to render
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"ERROR loading page: {e}")
            browser.close()
            sys.exit(1)

        result = vacuum(page)
        browser.close()

    if output_json:
        out = {
            "page_url": result.page_url,
            "page_title": result.page_title,
            "element_count": result.element_count,
            "elements": result.elements,
            "menu_text": result.menu_text,
        }
        # Strip node data from JSON output (too verbose)
        for elem in out["elements"]:
            elem.pop("node", None)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print()
        print(result.menu_text)


if __name__ == "__main__":
    _cli_main()
