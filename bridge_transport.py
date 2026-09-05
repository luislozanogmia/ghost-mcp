"""
Ghost Bridge Transport — HTTP client that talks to the bridge_server,
which forwards commands to the Chrome extension.

Drop-in replacement for chrome_transport.py. Same interface, no CDP.

Usage in ghost_daemon.py:
    from bridge_transport import BridgeTransport
    transport = BridgeTransport(port=9378)  # HTTP port = WS port + 1
    result = await transport.call("ghost_tab_list", {})
"""

import json
import urllib.request
import urllib.error


class BridgeTransport:
    """Synchronous HTTP client for the Ghost Bridge server."""

    def __init__(self, port=9378, timeout=60):
        self.base_url = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        self._connected = False

    def status(self):
        """Check bridge server and extension status."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/status",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                self._connected = data.get("connected", False)
                return data
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            self._connected = False
            return {"connected": False, "error": "Bridge server not running"}

    @property
    def connected(self):
        return self._connected

    def call(self, command, args=None, timeout=None):
        """Send a command to Chrome via the extension bridge."""
        payload = json.dumps({
            "command": command,
            "args": args or {},
            "timeout": timeout or self.timeout,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/call",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=(timeout or self.timeout) + 5) as resp:
                data = json.loads(resp.read())
                if "error" in data:
                    raise BridgeError(data["error"])
                return data.get("result", data)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body)
                raise BridgeError(err.get("error", body))
            except json.JSONDecodeError:
                raise BridgeError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise BridgeError(
                f"NO_BRIDGE: Cannot reach bridge server at {self.base_url}. "
                f"Start it with: python extension/bridge_server.py"
            )

    def ping(self):
        """Quick connectivity check."""
        try:
            result = self.call("ping", timeout=5)
            return result.get("pong", False)
        except BridgeError:
            return False


class BridgeError(Exception):
    """Error from the bridge server or Chrome extension."""
    pass
