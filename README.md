# ghost-cli

`ghost-cli` is the supported way to run Ghost Browser automation.

## Status

The old Ghost server path is no longer supported.

It was originally added to expose Ghost through an MCP-style server, but in
real long-running use it disconnected repeatedly and added recovery complexity
without adding useful capability over the direct CLI runtime. The supported
path is now the CLI only:

- `./ghost-cli call ...`
- `./ghost-cli repl`

The supported CLI path no longer depends on the Python `mcp` package.

The legacy server files still exist in the repository only as compatibility
shims for older integrations. They are not the recommended or supported way to
run Ghost.

Archived legacy server files now live under `deprecated/mcp/`.

Non-core utilities now live under `helpers/`.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Quick Start

List tools:

```bash
./ghost-cli list-tools
```

Run one command:

```bash
./ghost-cli call ghost_instance_create --arguments '{"instance_id":"live","cdp_url":"live-chrome"}'
./ghost-cli call ghost_status --arguments '{"instance_id":"live"}'
./ghost-cli call ghost_vacuum --arguments '{"instance_id":"live","limit":50}'
```

Attach to a managed Playwright LinkedIn session:

```bash
./ghost-cli call ghost_instance_create --arguments '{"instance_id":"li-b","playwright_session":"linkedin_auth_b"}'
./ghost-cli call ghost_vacuum --arguments '{"instance_id":"li-b","url":"https://www.linkedin.com/feed/","limit":20}'
```

Run a long-lived session:

```bash
./ghost-cli repl
```

Example REPL commands:

```json
{"tool":"ghost_instance_create","arguments":{"instance_id":"live","cdp_url":"live-chrome"}}
{"tool":"ghost_vacuum","arguments":{"instance_id":"live","limit":50}}
{"tool":"ghost_click","arguments":{"instance_id":"live","choice":12}}
```

```json
{"tool":"ghost_instance_create","arguments":{"instance_id":"li-b","playwright_session":"linkedin_auth_b"}}
{"tool":"ghost_vacuum","arguments":{"instance_id":"li-b","limit":20}}
{"tool":"ghost_eval","arguments":{"instance_id":"li-b","script":"() => ({title: document.title, href: location.href})"}}
```

## Supported Model

- Direct CLI runtime
- Long-lived JSON-line REPL sessions
- Vacuum and numbered-selector workflows
- Live Chrome attach via the Ghost runtime
- Managed Playwright session attach for `linkedin_auth_a` and `linkedin_auth_b`

## Unsupported Model

- Running Ghost as the primary browser automation path through an MCP server
- Treating the legacy server shim as a production transport

See [FUNCTIONALITY.md](./FUNCTIONALITY.md) for the CLI command map.
