"""
Ghost AIfy — The glue between raw page snapshot text and Ghost vacuum/execute.

Two entry points:
    1. aify(snapshot_text, url, title) -> menu_text
       Parses raw page snapshot text and returns a clean numbered menu.

    2. action(choice, vacuum_json, value=None) -> dict
       Maps a menu number to a structured action payload.

Usage from Bash (called by the LLM agent):
    # Vacuum a page
    python aify.py vacuum --url "https://..." --title "Page Title" < page_snapshot.txt

    # Or with inline text
    python aify.py vacuum --url "https://..." --title "Page Title" --text "button [ref_1] ..."

    # Get action for a choice
    python aify.py action 12 --vacuum-json result.json
    python aify.py action 12 --vacuum-json result.json --value "search query"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_root_dir = str(Path(__file__).resolve().parent.parent)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from helpers.vacuum import vacuum_from_snapshot_text, VacuumResult
from helpers.execute import build_action_payload, find_element


def aify(snapshot_text: str, url: str = "", title: str = "") -> dict:
    """
    Parse raw page snapshot text and return structured result.

    Returns dict with:
        menu_text: str  -- the clean numbered menu (what the AI shows the user)
        vacuum_json: dict  -- serialized VacuumResult for action() calls
        element_count: int
    """
    result = vacuum_from_snapshot_text(snapshot_text, url=url, title=title)
    return {
        "menu_text": result.menu_text,
        "vacuum_json": {
            "elements": result.elements,
            "page_url": result.page_url,
            "page_title": result.page_title,
            "element_count": result.element_count,
        },
        "element_count": result.element_count,
    }


def action(choice: int, vacuum_json: dict, value: str = None) -> dict:
    """
    Map a menu number to a structured action payload.

    Returns dict with:
        ref: str  -- the ref_id to pass to chrome computer tool
        action: str  -- "click" or "fill"
        value: str or None  -- text for fill actions
        description: str  -- human-readable action description
    """
    # Reconstruct VacuumResult from JSON
    result = VacuumResult(
        menu_text="",
        elements=vacuum_json.get("elements", []),
        page_url=vacuum_json.get("page_url", ""),
        page_title=vacuum_json.get("page_title", ""),
        element_count=vacuum_json.get("element_count", 0),
    )

    action_payload = build_action_payload(choice, result, value=value)

    if action_payload.get("error"):
        return {"error": action_payload["error"]}

    return {
        "ref": action_payload["ref_id"],
        "action": action_payload["action_type"],
        "value": action_payload.get("value"),
        "description": action_payload["description"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ghost AIfy — page snapshot to text menu bridge")
    sub = parser.add_subparsers(dest="command")

    # vacuum command
    p_vac = sub.add_parser("vacuum", help="Parse raw page snapshot text into a text menu")
    p_vac.add_argument("--url", default="", help="Page URL")
    p_vac.add_argument("--title", default="", help="Page title")
    p_vac.add_argument("--text", default=None, help="Raw page snapshot text (reads stdin if omitted)")
    p_vac.add_argument("--json", action="store_true", help="Output full JSON (menu + vacuum data)")

    # action command
    p_act = sub.add_parser("action", help="Map menu number to a structured action")
    p_act.add_argument("choice", type=int, help="Menu number to execute")
    p_act.add_argument("--vacuum-json", required=True, help="Path to vacuum JSON file")
    p_act.add_argument("--value", default=None, help="Text value for fill actions")

    args = parser.parse_args()

    if args.command == "vacuum":
        snapshot_text = args.text if args.text else sys.stdin.read()
        result = aify(snapshot_text, url=args.url, title=args.title)

        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result["menu_text"])

    elif args.command == "action":
        vac_path = Path(args.vacuum_json)
        if not vac_path.exists():
            print(f"Error: {vac_path} not found", file=sys.stderr)
            sys.exit(1)
        vacuum_json = json.loads(vac_path.read_text(encoding="utf-8"))
        result = action(args.choice, vacuum_json, value=args.value)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
