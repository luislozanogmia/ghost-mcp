"""
Ghost Browser CLI — Entry point for `python -m ghost`.

Commands:
    ghost scout <url>           Scout a URL, save manifest + generated script.
    ghost run <script> <method> Import a generated script and call a method.
    ghost refresh <script>      Re-scout the original URL and regenerate.

Ghost Browser automation toolkit.
"""

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path


def cmd_scout(args):
    """Scout a URL: capture manifest and compile a browser script."""
    import scout as scout_module  # noqa: E402
    import compile as compile_module  # noqa: E402

    url = args.url
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ghost] Scouting: {url}")
    print(f"[ghost] Wait: {args.wait}s | Output: {output_dir}")
    if args.context_dir:
        print(f"[ghost] Browser context: {args.context_dir}")

    # Scout the page — returns a manifest dict
    manifest = scout_module.scout(
        url,
        wait_seconds=args.wait,
        browser_context_dir=args.context_dir,
    )

    # Derive filenames from the URL
    from urllib.parse import urlparse

    parsed = urlparse(url)
    slug = parsed.netloc.replace(".", "_").replace("-", "_")
    if parsed.path and parsed.path != "/":
        path_part = parsed.path.strip("/").replace("/", "_").replace("-", "_")
        slug = f"{slug}_{path_part}"

    manifest_path = output_dir / f"{slug}_manifest.json"
    script_path = output_dir / f"{slug}.py"

    # Save manifest
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # Compile manifest into a runnable script
    compact = not getattr(args, 'full', False)
    script_source = compile_module.compile_script(manifest, compact=compact)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_source)

    # Summary
    element_count = len(manifest.get("elements", []))
    print()
    print(f"[ghost] Done.")
    print(f"  URL:        {url}")
    print(f"  Elements:   {element_count}")
    print(f"  Manifest:   {manifest_path}")
    print(f"  Script:     {script_path}")


def cmd_run(args):
    """Import a generated script, instantiate its class, call a method."""
    script_path = Path(args.script).resolve()
    method_name = args.method
    method_args = args.args

    if not script_path.exists():
        print(f"[ghost] Error: script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    # Dynamically import the script as a module
    spec = importlib.util.spec_from_file_location("ghost_script", str(script_path))
    if spec is None or spec.loader is None:
        print(f"[ghost] Error: could not load module from {script_path}", file=sys.stderr)
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Find the generated class — look for one with __ghost_meta__
    target_cls = None
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if isinstance(obj, type) and hasattr(obj, "__ghost_meta__"):
            target_cls = obj
            break

    if target_cls is None:
        # Fallback: pick the first class defined in the module
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if isinstance(obj, type) and obj.__module__ == "ghost_script":
                target_cls = obj
                break

    if target_cls is None:
        print("[ghost] Error: no suitable class found in script.", file=sys.stderr)
        sys.exit(1)

    # Instantiate and call the method
    async def _run():
        from playwright.async_api import async_playwright

        browser = None
        context = None
        instance = None

        init_signature = inspect.signature(target_cls.__init__)
        init_params = [
            param
            for name, param in init_signature.parameters.items()
            if name != "self"
        ]
        requires_browser_context = any(
            param.default is inspect._empty
            and param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for param in init_params
        )

        playwright = await async_playwright().start()

        try:
            if requires_browser_context:
                browser = await playwright.chromium.launch()
                context = await browser.new_context()
                instance = target_cls(context)
            else:
                instance = target_cls()

            if not hasattr(instance, method_name):
                print(f"[ghost] Error: method '{method_name}' not found on {target_cls.__name__}.", file=sys.stderr)
                sys.exit(1)

            method = getattr(instance, method_name)

            if hasattr(instance, "launch"):
                await instance.launch()
            elif hasattr(instance, "open"):
                await instance.open()

            result = method(*method_args)
            # Await if coroutine
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
        finally:
            try:
                if instance is not None and hasattr(instance, "close"):
                    await instance.close()
            except (AttributeError, Exception):
                pass
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
            await playwright.stop()

        return result

    result = asyncio.run(_run())
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


def cmd_refresh(args):
    """Re-scout the original URL from a generated script and regenerate."""
    import scout as scout_module  # noqa: E402
    import compile as compile_module  # noqa: E402

    script_path = Path(args.script).resolve()

    if not script_path.exists():
        print(f"[ghost] Error: script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    # Import the script to read __ghost_meta__
    spec = importlib.util.spec_from_file_location("ghost_script", str(script_path))
    if spec is None or spec.loader is None:
        print(f"[ghost] Error: could not load module from {script_path}", file=sys.stderr)
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Find __ghost_meta__ on any class in the module
    meta = None
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if isinstance(obj, type) and hasattr(obj, "__ghost_meta__"):
            meta = obj.__ghost_meta__
            break

    # Also check module-level __ghost_meta__
    if meta is None:
        meta = getattr(mod, "__ghost_meta__", None)

    if meta is None:
        print("[ghost] Error: no __ghost_meta__ found in script. Cannot determine original URL.", file=sys.stderr)
        sys.exit(1)

    url = meta.get("url")
    old_tree_hash = meta.get("tree_hash")

    if not url:
        print("[ghost] Error: __ghost_meta__ has no 'url' field.", file=sys.stderr)
        sys.exit(1)

    print(f"[ghost] Refreshing: {url}")
    print(f"[ghost] Script: {script_path}")

    # Re-scout
    context_dir = meta.get("context_dir")
    wait = meta.get("wait", 3)
    manifest = scout_module.scout(url, wait_seconds=wait, browser_context_dir=context_dir)

    new_tree_hash = manifest.get("tree_hash")

    if old_tree_hash and new_tree_hash and old_tree_hash == new_tree_hash:
        print()
        print(f"[ghost] No changes detected (tree_hash: {old_tree_hash[:12]}...).")
        print(f"[ghost] Skipping regeneration.")
        return

    # Regenerate
    script_source = compile_module.compile_script(manifest)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_source)

    # Save updated manifest alongside
    manifest_path = script_path.with_name(script_path.stem + "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    element_count = len(manifest.get("elements", []))
    print()
    print(f"[ghost] Regenerated.")
    print(f"  Elements:   {element_count}")
    if old_tree_hash and new_tree_hash:
        print(f"  Old hash:   {old_tree_hash[:12]}...")
        print(f"  New hash:   {new_tree_hash[:12]}...")
    print(f"  Script:     {script_path}")
    print(f"  Manifest:   {manifest_path}")


def build_parser():
    """Build the argparse parser with browser and generation subcommands."""
    parser = argparse.ArgumentParser(
        prog="ghost",
        description="Ghost Browser — Headless page intelligence for agentic navigation.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- scout ---
    p_scout = subparsers.add_parser(
        "scout",
        help="Scout a URL: capture accessibility tree and generate a browser script.",
    )
    p_scout.add_argument("url", help="The URL to scout.")
    p_scout.add_argument(
        "--output-dir", "-o",
        default=".",
        help="Directory to save manifest and script (default: current dir).",
    )
    p_scout.add_argument(
        "--wait", "-w",
        type=int,
        default=3,
        help="Seconds to wait for page load (default: 3).",
    )
    p_scout.add_argument(
        "--context-dir",
        default=None,
        help="Path to a saved browser context directory (for authenticated sessions).",
    )
    p_scout.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="Generate methods for ALL elements, not just interactive ones (default: compact).",
    )

    # --- run ---
    p_run = subparsers.add_parser(
        "run",
        help="Run a method from a generated Ghost script.",
    )
    p_run.add_argument("script", help="Path to the generated .py script.")
    p_run.add_argument("method", help="Name of the method to call on the script class.")
    p_run.add_argument(
        "args",
        nargs="*",
        default=[],
        help="Additional arguments passed to the method.",
    )

    # --- refresh ---
    p_refresh = subparsers.add_parser(
        "refresh",
        help="Re-scout the original URL and regenerate a Ghost script.",
    )
    p_refresh.add_argument("script", help="Path to the existing generated .py script.")

    p_tool = subparsers.add_parser(
        "tool",
        help="Call one Ghost browser tool directly through the CLI runtime.",
    )
    p_tool.add_argument("tool_name", help="Ghost tool name, e.g. ghost_vacuum.")
    p_tool.add_argument(
        "--arguments",
        default="{}",
        help="JSON object of tool arguments.",
    )
    p_tool.add_argument(
        "--json-output",
        action="store_true",
        help="Wrap the tool response in a JSON envelope.",
    )

    p_repl = subparsers.add_parser(
        "repl",
        help="Run a long-lived JSON-line Ghost CLI session.",
    )

    subparsers.add_parser(
        "list-tools",
        help="List the Ghost browser tool surface exposed by the CLI runtime.",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "scout": cmd_scout,
        "run": cmd_run,
        "refresh": cmd_refresh,
    }

    if args.command in {"tool", "repl", "list-tools"}:
        import ghost_cli

        if args.command == "tool":
            ghost_cli.cmd_call(args)
        elif args.command == "repl":
            ghost_cli.cmd_repl(args)
        else:
            ghost_cli.cmd_list_tools(args)
        return

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print("\n[ghost] Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"[ghost] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
