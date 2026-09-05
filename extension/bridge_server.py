"""
Ghost Bridge Server — WebSocket server that the Chrome extension connects to.

Replaces CDP transport entirely. Agents call ghost-cli → daemon → this server
→ Chrome extension → Chrome APIs. No CDP. No debugging dialogs.

Usage:
    python bridge_server.py [--port 9377]

The server exposes:
    ws://127.0.0.1:9377/ghost-bridge  — Chrome extension connects here
    http://127.0.0.1:9377/call         — Agents POST commands here (JSON-RPC style)
    http://127.0.0.1:9377/status       — GET connection status
"""

import asyncio
import json
import uuid
import argparse
import signal
import sys
from http import HTTPStatus

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    print("Install websockets: pip install websockets>=14.0")
    sys.exit(1)

try:
    from aiohttp import web
except ImportError:
    print("Install aiohttp: pip install aiohttp")
    sys.exit(1)


class BridgeServer:
    def __init__(self, port=9377):
        self.port = port
        self.extension_ws = None
        self.pending = {}  # id -> Future
        self.connected = False

    # ------------------------------------------------------------------
    # WebSocket handler — Chrome extension connects here
    # ------------------------------------------------------------------

    async def ws_handler(self, websocket):
        print(f"[bridge] Extension connected from {websocket.remote_address}")
        self.extension_ws = websocket
        self.connected = True

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Hello handshake
                if msg.get("type") == "hello":
                    print(f"[bridge] Extension v{msg.get('version', '?')} ready")
                    continue

                # Heartbeat
                if msg.get("type") == "heartbeat":
                    continue

                # Response to a pending command
                msg_id = msg.get("id")
                if msg_id and msg_id in self.pending:
                    self.pending[msg_id].set_result(msg)
                    continue

        except websockets.ConnectionClosed:
            pass
        finally:
            print("[bridge] Extension disconnected")
            self.extension_ws = None
            self.connected = False
            # Fail all pending requests
            for future in self.pending.values():
                if not future.done():
                    future.set_result({"error": "Extension disconnected"})
            self.pending.clear()

    # ------------------------------------------------------------------
    # Send a command to the extension and wait for response
    # ------------------------------------------------------------------

    async def send_command(self, command, args=None, timeout=60):
        if not self.connected or not self.extension_ws:
            raise Exception("NO_EXTENSION: Chrome extension is not connected. "
                            "Install Ghost Bridge and click Connect.")

        msg_id = str(uuid.uuid4())[:8]
        future = asyncio.get_event_loop().create_future()
        self.pending[msg_id] = future

        try:
            await self.extension_ws.send(json.dumps({
                "id": msg_id,
                "command": command,
                "args": args or {},
            }))

            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            raise Exception(f"TIMEOUT: Command '{command}' timed out after {timeout}s")
        finally:
            self.pending.pop(msg_id, None)

    # ------------------------------------------------------------------
    # HTTP API — agents POST commands here
    # ------------------------------------------------------------------

    async def handle_call(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        command = body.get("command")
        args = body.get("args", {})
        timeout = body.get("timeout", 60)

        if not command:
            return web.json_response({"error": "Missing 'command'"}, status=400)

        try:
            result = await self.send_command(command, args, timeout)
            if "error" in result:
                return web.json_response({"error": result["error"]}, status=502)
            return web.json_response({"result": result.get("result", result)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    async def handle_status(self, request):
        return web.json_response({
            "connected": self.connected,
            "port": self.port,
            "pending_commands": len(self.pending),
        })

    async def handle_health(self, request):
        return web.json_response({"ok": True, "bridge": "ghost"})

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self):
        # WebSocket server for the extension
        ws_server = await ws_serve(
            self.ws_handler,
            "127.0.0.1",
            self.port,
            # Serve the WS on /ghost-bridge path
        )

        # HTTP server for agent commands
        app = web.Application()
        app.router.add_post("/call", self.handle_call)
        app.router.add_get("/status", self.handle_status)
        app.router.add_get("/health", self.handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        http_site = web.TCPSite(runner, "127.0.0.1", self.port + 1)
        await http_site.start()

        print(f"[bridge] WebSocket server on ws://127.0.0.1:{self.port}/ghost-bridge")
        print(f"[bridge] HTTP API on http://127.0.0.1:{self.port + 1}/call")
        print(f"[bridge] Waiting for Chrome extension...")

        # Wait forever
        stop = asyncio.get_event_loop().create_future()

        def shutdown():
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, shutdown)

        try:
            await stop
        finally:
            ws_server.close()
            await ws_server.wait_closed()
            await runner.cleanup()
            print("\n[bridge] Shut down.")


def main():
    parser = argparse.ArgumentParser(description="Ghost Bridge Server")
    parser.add_argument("--port", type=int, default=9377, help="WebSocket port (HTTP = port+1)")
    args = parser.parse_args()

    server = BridgeServer(port=args.port)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
