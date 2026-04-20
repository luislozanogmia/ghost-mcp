---
name: ghost
description: "Ghost Browser v3 — AI CLI for deterministic web automation. Multi-instance MCP server with named Chrome sessions for interactive browsing."
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, mcp__ghost__ghost_instance_create, mcp__ghost__ghost_instance_list, mcp__ghost__ghost_instance_close, mcp__ghost__ghost_vacuum, mcp__ghost__ghost_click, mcp__ghost__ghost_more, mcp__ghost__ghost_screenshot, mcp__ghost__ghost_status, mcp__ghost__ghost_save_auth, mcp__ghost__ghost_eval
---

# Ghost Browser v3 — Model Usage Guide

Ghost is a browser automation MCP server. As the model, you control a live Chrome browser by calling Ghost MCP tools. This document tells you everything you need to operate it.

---

## Architecture (what you are talking to)

```
You (Claude)
  ↓ stdio MCP
ghost_stdio_proxy.py       <- the wrapper you connect to
  ↓ HTTP
mcp_server.py              <- daemon, manages named Chrome sessions
  ↓
chrome_mcp_runtime.py      <- wraps the Chrome MCP server process
  ↓
Chrome (live browser)
```

`ghost_stdio_proxy.py` is the only entry point. It auto-starts the daemon. You never call `mcp_server.py` directly.

---

## One-Time Wiring (user does this once)

**Repo:** `https://github.com/luislozanogmia/ghost-mcp`

```bash
pip install -r requirements.txt
playwright install chromium
```

Add to `.mcp.json`:
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

Once wired, Ghost MCP tools are available in your tool list. Verify with `ghost_status`.

---

## Tools You Have

| Tool | What it does |
|---|---|
| `ghost_status` | Check if browser is connected -- call this first if unsure |
| `ghost_vacuum` | Read current page -- returns numbered list of all interactive elements |
| `ghost_click` | Click element by number from vacuum output |
| `ghost_more` | Scroll / load more elements (`offset=N` to skip ahead) |
| `ghost_screenshot` | Take a screenshot for visual verification |
| `ghost_save_auth` | Save current browser cookies to disk -- call immediately after manual login |
| `ghost_instance_create` | Create or reuse a named Chrome session (optionally navigate to URL) |
| `ghost_instance_list` | List all active named sessions |
| `ghost_instance_close` | Close a named session (does NOT delete profile) |

All tools accept optional `instance_id`. Omit it to use the `default` session.

---

## How to Use Ghost -- Standard Flow

Every time you need to navigate or interact with a page, follow this sequence:

**1. Check connection**
```
ghost_status
```

**2. Find the right tab** (if user has multiple tabs open)
```bash
curl -s http://127.0.0.1:8766/call \
  -H "Content-Type: application/json" \
  -d '{"name":"list_pages","arguments":{}}'
```
Returns: `1: https://... [selected]`, `2: https://...` -- note the page ID.

**3. Navigate to a specific tab** (without touching others)
```bash
curl -s http://127.0.0.1:8766/call \
  -H "Content-Type: application/json" \
  -d '{"name":"navigate_page","arguments":{"type":"url","url":"https://TARGET","timeout":30000},"page_id":2}'
```

**4. Read the page**
```
ghost_vacuum
```
Returns a numbered list of every interactive element. Elements are indexed starting at 1.

**5. Interact**
```
ghost_click {"choice": 7}      # click element 7
ghost_more                     # load more if list was truncated
ghost_screenshot               # verify visually
```

**6. Re-vacuum after any navigation**
Every click that triggers a page load -- call `ghost_vacuum` again. Element numbers reset on every new page.

---

## Multi-Session Pattern

Use when you need two independent browser sessions simultaneously:

```
ghost_instance_create {"instance_id": "session-a", "url": "https://example.com"}
ghost_instance_create {"instance_id": "session-b", "url": "https://other.com"}
ghost_vacuum {"instance_id": "session-a"}
ghost_click {"instance_id": "session-b", "choice": 5}
ghost_instance_close {"instance_id": "session-b"}
```

Always pass the same `instance_id` on every call for that session.

---

## Auth Persistence

LinkedIn and other sites expire sessions. Correct flow:
1. User logs in manually in the browser
2. You call `ghost_save_auth` immediately on the same `instance_id`
3. Ghost saves cookies -- loaded automatically on next startup

Never attempt to type passwords -- security restriction.

---

## CRITICAL Rules

1. **Never use `ghost_vacuum url=X` when other tabs must stay untouched.** It navigates the internally tracked active tab. Use `navigate_page + page_id` via curl instead.
2. **Always `list_pages` before navigating** to confirm which tab ID to target.
3. **Always `ghost_save_auth` immediately after login** -- auth does not auto-persist.
4. **Re-vacuum after every navigation** -- element numbers are only valid for the current page state.

---

## References

- Tab management & proxy internals: [chrome-proxy.md](chrome-proxy.md)
- Auth persistence & browser profiles: [browser-context.md](browser-context.md)
