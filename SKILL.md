---
name: ghost
description: "Ghost Browser v3. Deterministic browser automation via MCP with multi-instance sessions and optional standalone automations."
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, mcp__ghost__ghost_instance_create, mcp__ghost__ghost_instance_list, mcp__ghost__ghost_instance_close, mcp__ghost__ghost_vacuum, mcp__ghost__ghost_click, mcp__ghost__ghost_more, mcp__ghost__ghost_screenshot, mcp__ghost__ghost_status, mcp__ghost__ghost_save_auth
---

# Ghost Browser v3

Ghost is an MCP-oriented browser automation skill for deterministic web interaction. It converts interactive pages into numbered menus, executes actions by element number, and supports multiple independent browser sessions.

## When To Use

- Interactive browsing through an AI agent
- Deterministic form filling and navigation
- Reproducible browser workflows that need session state
- Standalone browser automations stored under `tools/ghost/automations/`

## Core Capabilities

| Tool | Purpose |
|------|---------|
| `ghost_instance_create` | Create or reuse a named browser session |
| `ghost_instance_list` | List known browser sessions |
| `ghost_instance_close` | Close one named browser session |
| `ghost_vacuum` | Convert the current page into a numbered interactive menu |
| `ghost_click` | Execute an action by menu number |
| `ghost_more` | Continue through cached menu output |
| `ghost_screenshot` | Capture page state for in b visual verification |
| `ghost_status` | Report connection and page state |
| `ghost_save_auth` | Persist authenticated browser state for later reuse |
