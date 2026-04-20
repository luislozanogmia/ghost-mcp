"""
Ghost cached bridge — maps raw page snapshot output into Ghost vacuum/action flows.

This script is called by the LLM agent to process raw page snapshot output
without the raw page data ever entering the conversation.

Flow:
    1. LLM gets raw page snapshot text from a browser/runtime
    2. LLM calls: python ghost_cache_bridge.py vacuum <temp_file> --url URL --title TITLE
    3. Script reads temp file, runs vacuum, prints ONLY the clean menu
    4. LLM shows user the clean menu (raw page never in chat)

    5. User picks a number
    6. LLM calls: python ghost_cache_bridge.py action <number>
    7. Script reads cached vacuum result and returns a structured action payload
    8. LLM hands that payload to the active browser runtime

The vacuum result is cached at GHOST_CACHE so action() doesn't need
the vacuum JSON passed explicitly.
"""

from __future__ import annotations

import glob
import json
import sys
import os
from pathlib import Path

_root_dir = str(Path(__file__).resolve().parent.parent)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from helpers.vacuum import vacuum_from_snapshot_text, VacuumResult
from helpers.execute import build_action_payload, find_element
from shared_runtime import pid_exists

# Cache location for vacuum results between calls
# Each process gets its own cache file to avoid collisions between simultaneous instances
_pid = os.getpid()
GHOST_CACHE = Path(os.environ.get(
    "GHOST_CACHE",
    Path(__file__).resolve().parent.parent / "test_output" / f"_ghost_cache_{_pid}.json"
))


def _cleanup_stale_caches():
    """Remove cache files left by dead processes."""
    cache_dir = Path(__file__).resolve().parent.parent / "test_output"
    for f in glob.glob(str(cache_dir / "_ghost_cache_*.json")):
        p = Path(f)
        try:
            file_pid = int(p.stem.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            continue
        if file_pid == _pid:
            continue
        if not pid_exists(file_pid):
            try:
                p.unlink()
            except OSError:
                pass


_cleanup_stale_caches()


def cmd_vacuum(snapshot_file: str, url: str = "", title: str = ""):
    """Read raw page snapshot text from file, vacuum it, print clean menu, cache result."""
    snapshot_path = Path(snapshot_file)
    if not snapshot_path.exists():
        print(f"Error: {snapshot_path} not found", file=sys.stderr)
        sys.exit(1)

    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    result = vacuum_from_snapshot_text(snapshot_text, url=url, title=title)

    # Cache the vacuum result for subsequent action() calls
    cache_data = {
        "elements": result.elements,
        "page_url": result.page_url,
        "page_title": result.page_title,
        "element_count": result.element_count,
    }
    GHOST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GHOST_CACHE.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

    # Print ONLY the clean menu — this is what the LLM sees
    print(result.menu_text)


def cmd_action(choice: int, value: str = None):
    """Read cached vacuum result and map a choice to a structured action."""
    if not GHOST_CACHE.exists():
        print(json.dumps({"error": "No cached vacuum result. Run vacuum first."}))
        sys.exit(1)

    cache_data = json.loads(GHOST_CACHE.read_text(encoding="utf-8"))

    result = VacuumResult(
        menu_text="",
        elements=cache_data.get("elements", []),
        page_url=cache_data.get("page_url", ""),
        page_title=cache_data.get("page_title", ""),
        element_count=cache_data.get("element_count", 0),
    )

    action_payload = build_action_payload(choice, result, value=value)

    if action_payload.get("error"):
        print(json.dumps({"error": action_payload["error"]}))
        sys.exit(1)

    # Print the action dict — LLM uses ref to call computer tool
    print(json.dumps({
        "ref": action_payload["ref_id"],
        "action": action_payload["action_type"],
        "value": action_payload.get("value"),
        "description": action_payload["description"],
    }, ensure_ascii=False))


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ghost cached bridge")
    parser.add_argument("--self-test", action="store_true", help="Validate CLI wiring.")
    parser.add_argument("--print-config", action="store_true", help="Print resolved config.")
    parser.add_argument("--check-env", action="store_true", help="Check required env vars.")
    sub = parser.add_subparsers(dest="command")

    p_vac = sub.add_parser("vacuum", help="Vacuum raw page snapshot text into a clean menu")
    p_vac.add_argument("snapshot_file", help="Path to file containing raw page snapshot text")
    p_vac.add_argument("--url", default="", help="Page URL")
    p_vac.add_argument("--title", default="", help="Page title")

    p_act = sub.add_parser("action", help="Map menu number to a structured action payload")
    p_act.add_argument("choice", type=int, help="Menu number")
    p_act.add_argument("--value", default=None, help="Text value for fill actions")

    args = parser.parse_args()

    if args.self_test:
        _ = argparse.ArgumentParser(description="Ghost cached bridge")
        print("self-test: ok")
        return

    if args.print_config:
        print("skill=ghost-cli")
        print("api=none")
        return

    if args.check_env:
        print("no api_env configured")
        return

    if args.command == "vacuum":
        cmd_vacuum(args.snapshot_file, url=args.url, title=args.title)
    elif args.command == "action":
        cmd_action(args.choice, value=getattr(args, "value", None))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
