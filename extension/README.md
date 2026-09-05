# Ghost Bridge — Chrome Extension

Native Chrome bridge for ghost-cli. **No CDP. No debugging dialogs. No handholding.**

## Architecture

```
Agent (Claude, Hermes, Codex)
  │
  ▼
ghost-cli call ghost_vacuum ...
  │
  ▼
Bridge Server (bridge_server.py)     ← HTTP POST on :9378
  │
  ▼ WebSocket on :9377
Chrome Extension (background.js)
  │
  ▼ chrome.tabs / chrome.scripting APIs
Your actual Chrome browser
```

## Install

### 1. Install the Chrome extension

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select this `extension/` folder
5. Pin the Ghost Bridge icon in your toolbar

### 2. Install server dependencies

```bash
pip install websockets aiohttp
```

### 3. Start the bridge server

```bash
python extension/bridge_server.py
```

You'll see:
```
[bridge] WebSocket server on ws://127.0.0.1:9377/ghost-bridge
[bridge] HTTP API on http://127.0.0.1:9378/call
[bridge] Waiting for Chrome extension...
```

### 4. Connect the extension

Click the Ghost Bridge icon in Chrome → click **Connect**.
The badge turns green and the server logs:
```
[bridge] Extension connected
[bridge] Extension v1.0.0 ready
```

### 5. Test

```bash
# Check status
curl http://127.0.0.1:9378/status

# List tabs
curl -X POST http://127.0.0.1:9378/call \
  -H 'Content-Type: application/json' \
  -d '{"command": "ghost_tab_list"}'

# Navigate
curl -X POST http://127.0.0.1:9378/call \
  -H 'Content-Type: application/json' \
  -d '{"command": "ghost_navigate", "args": {"url": "https://example.com"}}'

# Read page
curl -X POST http://127.0.0.1:9378/call \
  -H 'Content-Type: application/json' \
  -d '{"command": "ghost_read", "args": {"max_chars": 2000}}'

# Screenshot
curl -X POST http://127.0.0.1:9378/call \
  -H 'Content-Type: application/json' \
  -d '{"command": "ghost_screenshot"}'
```

## ghost-cli integration

Once the bridge is running, ghost-cli uses it automatically:

```bash
# ghost-cli detects the bridge and uses it instead of CDP
./ghost-cli call ghost_tab_list --arguments '{}'
./ghost-cli call ghost_vacuum --arguments '{"url": "https://example.com"}'
./ghost-cli call ghost_click --arguments '{"choice": 5}'
```

## Commands

| Command | Description | Key args |
|---------|-------------|----------|
| `ghost_tab_list` | List all open tabs | — |
| `ghost_tab_open` | Open a new tab | `url`, `active` |
| `ghost_tab_switch` | Switch to a tab | `tab_id` or `tab_index` |
| `ghost_tab_close` | Close a tab | `tab_id` or `tab_index` |
| `ghost_navigate` | Navigate to URL | `url`, `tab_id` |
| `ghost_vacuum` | Navigate + read page | `url`, `limit`, `selector` |
| `ghost_read` | Read current page content | `max_chars`, `selector` |
| `ghost_click` | Click an element | `choice` or `selector` |
| `ghost_fill` | Fill an input | `choice`/`selector`, `value` |
| `ghost_key` | Press a key or type text | `key` or `text` |
| `ghost_eval` | Run JS in page context | `script` |
| `ghost_extract` | Structured extraction | `recipe` |
| `ghost_screenshot` | Capture visible tab | `format`, `quality` |
| `ghost_scroll` | Scroll the page | `direction`, `amount` |
| `ghost_wait` | Wait for element/time | `selector`, `ms`, `timeout` |

## What changed from CDP

| Before (CDP) | After (Extension) |
|---|---|
| Chrome needs `--remote-debugging-port` | Just install the extension |
| "Allow remote debugging?" dialog every time | Grant permissions once at install |
| Daemon holds a fragile CDP connection | Extension uses native Chrome APIs |
| Reconnect logic, handholding | Auto-reconnects, badge shows status |
| Can break Chrome's security model | Sandboxed, permission-scoped |
