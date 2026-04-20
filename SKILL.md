---
name: ghost_mcp
description: Ghost Browser v3 browser automation via the Ghost MCP bridge and stdio proxy.
---

# Ghost MCP

Use this skill when browser automation should run through Ghost Browser v3 instead of the older chrome_mcp attachment flow.

## What this skill provides
- A deterministic Ghost MCP server with named browser instances
- A stdio proxy entrypoint for MCP clients
- A bridge for vacuum/action workflows over live browser pages
- Chrome session attachment through the Ghost runtime, including external browser support

## Architecture
```text
You (Coding Agent)
  ↓ stdio MCP
ghost_stdio_proxy.py       <- the wrapper you connect to
  ↓ HTTP
mcp_server.py              <- daemon, manages named Chrome sessions
  ↓
chrome_mcp_runtime.py      <- wraps the Chrome MCP server process
  ↓
Chrome (live browser)
```

`ghost_stdio_proxy.py` is the MCP entrypoint. It auto-starts the daemon. Do not call `mcp_server.py` directly.

## One-Time Wiring

From the skill folder:

```bash
pip install -r requirements.txt
playwright install chromium
```

To expose the server to an MCP client, point it at `ghost_stdio_proxy.py`:

```json
{
  "mcpServers": {
    "ghost": {
      "command": "python",
      "args": ["/path/to/ghost-mcp/ghost_stdio_proxy.py"]
    }
  }
}
```

If you are wiring Codex itself, add the `ghost` entry under your `mcp_servers` config and point it at the same proxy command:

```toml
[mcp_servers.ghost]
command = "/Users/luis.lozano/.codex/skills/ghost_mcp/.venv/bin/python"
args = ["/Users/luis.lozano/.codex/skills/ghost_mcp/ghost_stdio_proxy.py"]
enabled = true
```

Then reload or restart Codex so the new MCP server is registered.

## Runner
- `python3 ghost_mcp.py --help`
- `python3 ghost_mcp.py --self-test`
- `python3 ghost_mcp.py vacuum <temp_file> --url <url> --title <title>`
- `python3 ghost_mcp.py action <choice> --value <text>`
- `python3 ghost_stdio_proxy.py`

## Tools You Have

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

All tools accept optional `instance_id`. Omit it to use the `default` session.

## Critical Rules
1. Always call `ghost_status` before assuming the browser is connected.
2. Always re-vacuum after navigation; element numbers are only valid for the current page state.
3. Always call `ghost_save_auth` immediately after login so auth persists.
4. Use different `instance_id` values for independent browser sessions.

## Standard Flow
1. Check connection
```text
ghost_status
```

2. Read the page
```text
ghost_vacuum
```
Returns a numbered list of every interactive element. Elements are indexed starting at 1.

3. Interact
```text
ghost_click {"choice": 7}      # click element 7
ghost_more                     # load more if list was truncated
ghost_screenshot               # verify visually
```

4. Re-vacuum after any navigation. Element numbers reset on every new page.

## Multi-Session Pattern
Use when you need two independent browser sessions simultaneously:

```text
ghost_instance_create {"instance_id": "session-a", "url": "https://example.com"}
ghost_instance_create {"instance_id": "session-b", "url": "https://other.com"}
ghost_vacuum {"instance_id": "session-a"}
ghost_click {"instance_id": "session-b", "choice": 5}
ghost_instance_close {"instance_id": "session-b"}
```

Always pass the same `instance_id` on every call for that session.

## Auth Persistence
LinkedIn and other sites expire sessions. Correct flow:
1. User logs in manually in the browser
2. You call `ghost_save_auth` immediately on the same `instance_id`
3. Ghost saves cookies and loads them automatically on next startup

Never attempt to type passwords.
