---
name: ghost-cli
description: Ghost Browser v3 browser automation via the `ghost-cli` direct runtime. The old server path remains only as an unsupported legacy shim.
---

# Ghost CLI

Use this skill when browser automation should run through Ghost Browser v3 using the direct CLI runtime.

## What this skill provides
- A deterministic direct CLI runtime with named browser instances
- A long-lived JSON-line REPL for agentic browser sessions
- A bridge for vacuum/action workflows over live browser pages
- Chrome session attachment through the Ghost runtime, including external browser support
- A legacy server shim kept only for older integrations and not supported for production use

## Architecture
```text
You (Coding Agent)
  ↓ CLI / JSON lines
ghost_cli.py               <- the supported entrypoint
  ↓
runtime host               <- in-process Ghost runtime
  ↓
live Chrome transport      <- used when attaching to an existing Chrome session
  ↓
Chrome (live browser)
```

Primary path: `./ghost-cli`

Archived legacy server path: `deprecated/mcp/`

The legacy server path is not supported for real browsing runs. It was moved to
`deprecated/mcp/` so the repo root only carries the supported CLI runtime.

## One-Time Wiring

From the skill folder:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Runner
- `./ghost-cli list-tools`
- `./ghost-cli call ghost_status`
- `./ghost-cli call ghost_instance_create --arguments '{"instance_id":"live","cdp_url":"live-chrome"}'`
- `./ghost-cli repl`
- `python3 helpers/ghost_cache_bridge.py --help`
- `python3 helpers/ghost_cache_bridge.py --self-test`
- `python3 helpers/ghost_cache_bridge.py vacuum <temp_file> --url <url> --title <title>`
- `python3 helpers/ghost_cache_bridge.py action <choice> --value <text>`
- `python3 deprecated/mcp/ghost_stdio_proxy.py` (archived unsupported shim)

See [FUNCTIONALITY.md](/Users/luis.lozano/.codex/skills/ghost-cli/FUNCTIONALITY.md:1) for the old-tool to CLI mapping.

## Commands You Have

| Tool | What it does |
|---|---|
| `ghost_status` | Check if browser is connected; call this first if unsure |
| `ghost_vacuum` | Read current page and return a numbered list of interactive elements |
| `ghost_click` | Click element by number from vacuum output |
| `ghost_more` | Scroll / load more elements (`offset=N` to skip ahead) |
| `ghost_screenshot` | Take a screenshot for visual verification |
| `ghost_save_auth` | Save current browser cookies to disk; call immediately after manual login |
| `ghost_instance_create` | Create or reuse a named Chrome session, optionally navigating to a URL |
| `ghost_instance_list` | List all active named sessions |
| `ghost_instance_close` | Close a named session without deleting its profile |

All commands accept optional `instance_id`. Omit it to use the `default` session.

## Critical Rules
1. Always call `ghost_status` before assuming the browser is connected.
2. Always re-vacuum after navigation; element numbers are only valid for the current page state.
3. Always call `ghost_save_auth` immediately after login so auth persists.
4. Use different `instance_id` values for independent browser sessions.
5. Prefer `./ghost-cli repl` for long LinkedIn runs so state stays in one CLI process.
6. Do not treat anything under `deprecated/mcp/` as a supported production transport.

## Standard Flow
1. Check connection
```text
./ghost-cli call ghost_status
```

2. Read the page
```text
./ghost-cli call ghost_vacuum
```
Returns a numbered list of every interactive element. Elements are indexed starting at 1.

3. Interact
```text
./ghost-cli call ghost_click --arguments '{"choice":7}'
./ghost-cli call ghost_more
./ghost-cli call ghost_screenshot
```

4. Re-vacuum after any navigation. Element numbers reset on every new page.

## Multi-Session Pattern
Use when you need two independent browser sessions simultaneously:

```text
./ghost-cli call ghost_instance_create --arguments '{"instance_id":"session-a","url":"https://example.com"}'
./ghost-cli call ghost_instance_create --arguments '{"instance_id":"session-b","url":"https://other.com"}'
./ghost-cli call ghost_vacuum --arguments '{"instance_id":"session-a"}'
./ghost-cli call ghost_click --arguments '{"instance_id":"session-b","choice":5}'
./ghost-cli call ghost_instance_close --arguments '{"instance_id":"session-b"}'
```

Always pass the same `instance_id` on every call for that session.

## Auth Persistence
LinkedIn and other sites expire sessions. Correct flow:
1. User logs in manually in the browser
2. You call `ghost_save_auth` immediately on the same `instance_id`
3. Ghost saves cookies and loads them automatically on next startup

Never attempt to type passwords.
