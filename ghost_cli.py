from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

_ghost_dir = str(Path(__file__).resolve().parent)
if _ghost_dir not in sys.path:
    sys.path.insert(0, _ghost_dir)

import runtime_host as runtime
from shared_runtime import (
    CLI_DAEMON_HOST,
    CLI_DAEMON_PID_FILE,
    CLI_DAEMON_PORT,
    CLI_DAEMON_STDERR_LOG_FILE,
    CLI_DAEMON_STDOUT_LOG_FILE,
    ensure_runtime_dirs,
    pid_exists,
    read_json,
)


def _tool_payload(tool) -> dict[str, object]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema or {},
    }


async def _invoke_tool(name: str, arguments: dict[str, object] | None) -> str:
    return await runtime.call_tool(name, arguments or {})


async def _shutdown_runtime(reason: str) -> None:
    await runtime._close_all_instance_browsers(reason)
    await runtime._stop_playwright()


async def _daemon_request(payload: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(CLI_DAEMON_HOST, CLI_DAEMON_PORT)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await reader.readline()
        if not raw:
            raise RuntimeError("ghost daemon closed without a response")
        response = json.loads(raw.decode("utf-8"))
        if not isinstance(response, dict):
            raise RuntimeError("ghost daemon returned an invalid response")
        return response
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def _daemon_is_ready() -> bool:
    try:
        response = await _daemon_request({"type": "health"})
    except Exception:
        return False
    return bool(response.get("ok"))


def _spawn_daemon() -> int:
    ensure_runtime_dirs()
    command = [sys.executable, str(Path(__file__).with_name("ghost_daemon.py"))]
    with open(CLI_DAEMON_STDOUT_LOG_FILE, "a", encoding="utf-8") as stdout_file, open(
        CLI_DAEMON_STDERR_LOG_FILE,
        "a",
        encoding="utf-8",
    ) as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
    return process.pid


async def _ensure_daemon() -> None:
    if await _daemon_is_ready():
        return

    pid_payload = read_json(CLI_DAEMON_PID_FILE)
    pid = int(pid_payload.get("pid", 0)) if isinstance(pid_payload, dict) else 0
    if not pid_exists(pid):
        _spawn_daemon()

    deadline = asyncio.get_running_loop().time() + 20.0
    while asyncio.get_running_loop().time() < deadline:
        if await _daemon_is_ready():
            return
        await asyncio.sleep(0.25)

    raise RuntimeError(f"ghost daemon did not start on {CLI_DAEMON_HOST}:{CLI_DAEMON_PORT}")


async def _list_tools_payload() -> list[dict[str, object]]:
    tools = await runtime.list_tools()
    return [_tool_payload(tool) for tool in tools]


def _load_arguments(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Arguments payload must be a JSON object.")
    return parsed


def _response_payload(tool: str, text: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": not text.startswith("Error:") and not text.startswith("Ghost error:"),
        "tool": tool,
        "output": text,
    }
    try:
        payload["parsed"] = json.loads(text)
    except json.JSONDecodeError:
        pass
    return payload


def cmd_list_tools(_args) -> None:
    async def _run() -> None:
        payload = await _list_tools_payload()
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    asyncio.run(_run())


def cmd_call(args) -> None:
    async def _run() -> None:
        arguments = _load_arguments(args.arguments)
        # Inject CLI --headless into ghost_instance_create arguments
        if getattr(args, "headless", False) and args.tool_name == "ghost_instance_create":
            arguments["headless"] = True
        # Also respect GHOST_HEADLESS environment variable
        if os.environ.get("GHOST_HEADLESS", "").lower() in ("1", "true", "yes") and args.tool_name == "ghost_instance_create":
            arguments.setdefault("headless", True)
        if args.ephemeral:
            text = await _invoke_tool(args.tool_name, arguments)
        else:
            await _ensure_daemon()
            response = await _daemon_request(
                {
                    "type": "call_tool",
                    "tool": args.tool_name,
                    "arguments": arguments,
                }
            )
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "ghost daemon call failed")))
            text = str(response.get("text", ""))
        if args.json_output:
            print(json.dumps(_response_payload(args.tool_name, text), ensure_ascii=False))
        else:
            print(text)
        if args.ephemeral:
            await _shutdown_runtime("cli one-shot command completed")

    asyncio.run(_run())


def cmd_daemon_status(_args) -> None:
    async def _run() -> None:
        if await _daemon_is_ready():
            response = await _daemon_request({"type": "health"})
            print(json.dumps(response, ensure_ascii=False))
            return

        pid_payload = read_json(CLI_DAEMON_PID_FILE)
        print(
            json.dumps(
                {
                    "ok": False,
                    "ready": False,
                    "pid": (pid_payload or {}).get("pid") if isinstance(pid_payload, dict) else None,
                    "pid_file": str(CLI_DAEMON_PID_FILE),
                },
                ensure_ascii=False,
            )
        )

    asyncio.run(_run())


def cmd_daemon_stop(_args) -> None:
    async def _run() -> None:
        if not await _daemon_is_ready():
            print(json.dumps({"ok": True, "stopped": False, "reason": "daemon not running"}, ensure_ascii=False))
            return
        response = await _daemon_request({"type": "shutdown"})
        print(json.dumps(response, ensure_ascii=False))

    asyncio.run(_run())


def cmd_repl(_args) -> None:
    async def _run() -> None:
        tools = await _list_tools_payload()
        tool_names = [tool["name"] for tool in tools]
        print(json.dumps({"ok": True, "ready": True, "tools": tool_names}, ensure_ascii=False))
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":
                break

            raw = line.strip()
            if not raw:
                continue
            if raw.lower() in {"exit", "quit"}:
                break
            if raw.lower() == "help":
                print(json.dumps({"ok": True, "tools": tools}, ensure_ascii=False))
                continue

            try:
                if raw.startswith("{"):
                    command = json.loads(raw)
                    if not isinstance(command, dict):
                        raise ValueError("Command must be a JSON object.")
                    tool_name = command.get("tool")
                    arguments = command.get("arguments") or {}
                else:
                    parts = raw.split(maxsplit=1)
                    tool_name = parts[0]
                    arguments = _load_arguments(parts[1] if len(parts) > 1 else None)

                if not isinstance(tool_name, str) or not tool_name:
                    raise ValueError("Command must include a non-empty tool name.")
                if not isinstance(arguments, dict):
                    raise ValueError("Command arguments must be a JSON object.")

                text = await _invoke_tool(tool_name, arguments)
                print(json.dumps(_response_payload(tool_name, text), ensure_ascii=False))
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))

        await _shutdown_runtime("ghost cli repl exited")

    asyncio.run(_run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghost-cli",
        description="Ghost Browser direct CLI runtime.",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list-tools", help="List the Ghost tool surface exposed by the CLI runtime.")
    p_list.set_defaults(func=cmd_list_tools)

    p_call = sub.add_parser("call", help="Call one Ghost tool once.")
    p_call.add_argument("tool_name", help="Ghost tool name, e.g. ghost_vacuum.")
    p_call.add_argument(
        "--arguments",
        default="{}",
        help="JSON object of tool arguments.",
    )
    p_call.add_argument(
        "--json-output",
        action="store_true",
        help="Wrap the response in a JSON envelope.",
    )
    p_call.add_argument(
        "--ephemeral",
        action="store_true",
        help="Run the tool in a throwaway process instead of the persistent local daemon.",
    )
    p_call.add_argument(
        "--headless",
        action="store_true",
        help="Launch browser in headless mode (no visible window). Only affects ghost_instance_create.",
    )
    p_call.set_defaults(func=cmd_call)

    p_repl = sub.add_parser("repl", help="Run a long-lived JSON-line Ghost CLI session.")
    p_repl.set_defaults(func=cmd_repl)

    p_daemon_status = sub.add_parser("daemon-status", help="Show whether the persistent Ghost CLI daemon is running.")
    p_daemon_status.set_defaults(func=cmd_daemon_status)

    p_daemon_stop = sub.add_parser("daemon-stop", help="Ask the persistent Ghost CLI daemon to shut down cleanly.")
    p_daemon_stop.set_defaults(func=cmd_daemon_stop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
