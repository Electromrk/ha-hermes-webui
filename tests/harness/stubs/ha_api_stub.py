#!/usr/bin/env python3
"""Home Assistant API stub for the test harness.

Answers like the real pair of addresses an app can reach Core on:

  http://supervisor/core/api/...     needs the app's SUPERVISOR_TOKEN
  http://homeassistant:<port>/api/... needs a long-lived token, on --direct-port only

The token rules match what was measured: the Supervisor's proxy accepts only the app's own token,
so a long-lived token there gets a 401. Every call is kept in memory and readable at /_stub/calls,
with the data of each service call, so a test can read the notifications the app created.
Tokens are never logged or returned.

The WebSocket API (/api/websocket) does what the gateway's Home Assistant platform and the HA tools'
area lookup need: auth_required, auth, auth_ok or auth_invalid, subscribe_events, empty registry lists,
ping and pong. Each auth and subscription is kept in the calls as method "WS".
"""
import argparse
import base64
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

STATES = {
    "sun.sun": {"entity_id": "sun.sun", "state": "below_horizon", "attributes": {"friendly_name": "Sun"}},
    "light.kitchen": {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}},
}
SERVICES = [
    {"domain": "light", "services": {"turn_on": {"description": "Turn on"}, "turn_off": {"description": "Turn off"}}},
]
CALLS = []
LOCK = threading.Lock()


def record(entry):
    with LOCK:
        entry["seq"] = len(CALLS)
        CALLS.append(entry)


def make_handler(args, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            if path == "/_stub/calls":
                with LOCK:
                    return self._json(200, list(CALLS))
            if path == "/_stub/health":
                return self._json(200, {"ok": True})

            if path.startswith("/core/api/"):
                accepted, api_path, route = args.supervisor_token, path[len("/core"):], "supervisor"
            elif path.startswith("/api/") and port == args.direct_port:
                accepted, api_path, route = args.long_lived_token, path, "direct"
            else:
                return self._json(404, {"message": "not found"})

            if api_path == "/api/websocket" and self.headers.get("Upgrade", "").lower() == "websocket":
                return self._websocket(accepted, route)

            supplied = self.headers.get("Authorization", "")
            authorized = bool(accepted) and supplied == f"Bearer {accepted}"
            entry = {"method": method, "path": api_path, "route": route, "port": port, "authorized": authorized}
            if method == "POST" and api_path.startswith("/api/services/"):
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = None
                entry["data"] = {key: str(value)[:2000] for key, value in data.items()} if isinstance(data, dict) else None
            record(entry)
            if not authorized:
                return self._json(401, {"message": "Invalid access token or password"})
            return self._api(method, api_path, raw)

        # ---- WebSocket, RFC 6455, text frames only ----

        def _websocket(self, accepted, route):
            accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + WS_GUID).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            try:
                self._ws_send({"type": "auth_required", "ha_version": "2026.11.0"})
                message = self._ws_receive()
                authorized = bool(accepted) and isinstance(message, dict) and message.get("type") == "auth" \
                    and message.get("access_token") == accepted
                record({"method": "WS", "path": "/api/websocket", "route": route, "port": port, "authorized": authorized})
                if not authorized:
                    self._ws_send({"type": "auth_invalid", "message": "Invalid access token or password"})
                    return
                self._ws_send({"type": "auth_ok", "ha_version": "2026.11.0"})
                while True:
                    message = self._ws_receive()
                    if message is None:
                        return
                    kind = message.get("type")
                    if kind == "ping":
                        self._ws_send({"id": message.get("id"), "type": "pong"})
                        continue
                    if kind == "subscribe_events":
                        record({"method": "WS", "path": f"subscribe_events:{message.get('event_type')}", "route": route,
                                "port": port, "authorized": True})
                    result = [] if kind and kind.endswith("/list") else None
                    self._ws_send({"id": message.get("id"), "type": "result", "success": True, "result": result})
            except (OSError, ValueError, struct.error):
                return

        def _ws_send(self, payload, opcode=0x1):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            header = bytes([0x80 | opcode])
            if len(data) < 126:
                header += bytes([len(data)])
            elif len(data) < 65536:
                header += bytes([126]) + struct.pack(">H", len(data))
            else:
                header += bytes([127]) + struct.pack(">Q", len(data))
            self.wfile.write(header + data)
            self.wfile.flush()

        def _ws_receive(self):
            """The next text message as JSON, answering pings on the way. None when the client closes."""
            while True:
                first, second = self.rfile.read(2)
                opcode, length = first & 0x0F, second & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.rfile.read(8))[0]
                mask = self.rfile.read(4) if second & 0x80 else b"\0\0\0\0"
                data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self.rfile.read(length)))
                if opcode == 0x8:
                    return None
                if opcode == 0x9:
                    self._ws_send(data, opcode=0xA)
                    continue
                if opcode == 0x1:
                    return json.loads(data)

        def _api(self, method, path, raw):
            if method == "GET":
                if path == "/api/":
                    return self._json(200, {"message": "API running."})
                if path == "/api/config":
                    return self._json(200, {"version": "2026.11.0", "state": "RUNNING", "internal_url": None})
                if path == "/api/states":
                    return self._json(200, list(STATES.values()))
                if path.startswith("/api/states/"):
                    state = STATES.get(path[len("/api/states/"):])
                    return self._json(200, state) if state else self._json(404, {"message": "Entity not found."})
                if path == "/api/services":
                    return self._json(200, SERVICES)
            if method == "POST" and path.startswith("/api/services/"):
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"message": "Invalid JSON"})
                state = STATES.get(data.get("entity_id"))
                if state:
                    state["state"] = "on" if path.endswith("/turn_on") else "off"
                return self._json(200, [state] if state else [])
            return self._json(404, {"message": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--ports", default="80,8123", help="comma-separated ports to listen on")
    parser.add_argument("--direct-port", type=int, default=8123, help="the port playing Core itself")
    parser.add_argument("--supervisor-token", required=True)
    parser.add_argument("--long-lived-token", default="")
    args = parser.parse_args()
    servers = [ThreadingHTTPServer((args.host, int(port)), make_handler(args, int(port)))
               for port in args.ports.split(",")]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
