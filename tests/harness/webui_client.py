#!/usr/bin/env python3
"""A small webui client: JSON requests, SSE streams, one chat turn, one terminal session.

Works through the Ingress simulator and through real Ingress. Prints no cookies. Standard library only.
"""
import http.client
import json
import re
import threading
import time
import urllib.parse

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?")


def smuggled_request(path, body, forged_user="forged-smuggled"):
    """A complete POST as bytes, to send as the body of another request. It must never run on its own."""
    payload = json.dumps(body).encode()
    head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Remote-User-Id: {forged_user}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n")
    return head.encode() + payload


class Client:
    def __init__(self, base, cookie=None, headers=None, timeout=180):
        self.base = urllib.parse.urlsplit(base.rstrip("/") + "/")
        self.cookie = cookie
        self.session_cookie = None
        self.extra = dict(headers or {})
        self.timeout = timeout

    def _connection(self, timeout=None):
        return http.client.HTTPConnection(self.base.hostname, self.base.port or 80, timeout=timeout or self.timeout)

    def _headers(self, extra=None):
        headers = {"Accept": "*/*", **self.extra, **(extra or {})}
        cookies = [part for part in (self.cookie, self.session_cookie) if part]
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    def raw(self, method, path, body=None, headers=None):
        """Returns (status, response headers as a lowercase dict, body bytes)."""
        connection = self._connection()
        connection.request(method, self.base.path + path.lstrip("/"), body=body, headers=self._headers(headers))
        response = connection.getresponse()
        data = response.read()
        collected = {}
        for key, value in response.getheaders():
            collected.setdefault(key.lower(), value)
            if key.lower() == "set-cookie" and value.startswith("hermes_session="):
                self.session_cookie = value.split(";", 1)[0]
        connection.close()
        return response.status, collected, data

    def request(self, method, path, body=None):
        data, headers = None, {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, _headers, raw = self.raw(method, path, data, headers)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def sse(self, path, idle_timeout=120):
        """Yields (seconds since open, event, data) for each SSE event."""
        connection = self._connection(idle_timeout)
        connection.request("GET", self.base.path + path.lstrip("/"),
                           headers=self._headers({"Accept": "text/event-stream"}))
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"SSE HTTP {response.status}: {response.read()[:300]!r}")
        start = time.monotonic()
        event, data = None, []
        while True:
            line = response.readline()
            if not line:
                return
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data or event:
                    yield time.monotonic() - start, event, "\n".join(data)
                event, data = None, []
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())


def run_chat(client, message, session_id=None):
    """One chat turn. Returns (session id, [(seconds, event, data), ...])."""
    if not session_id:
        status, payload = client.request("POST", "api/session/new", {})
        if status != 200:
            raise RuntimeError(f"session/new HTTP {status}: {payload}")
        session_id = payload["session"]["session_id"]
    status, payload = client.request("POST", "api/chat/start", {"session_id": session_id, "message": message})
    if status != 200:
        raise RuntimeError(f"chat/start HTTP {status}: {payload}")
    timeline = []
    for moment, event, data in client.sse(f"api/chat/stream?stream_id={payload.get('stream_id')}"):
        timeline.append((moment, event, data))
        if event in ("stream_end", "apperror", "error", "cancel"):
            break
    return session_id, timeline


def token_gaps(timeline):
    """Returns (number of token events, seconds between the token events)."""
    tokens = [moment for moment, event, _data in timeline if event == "token"]
    return len(tokens), [later - earlier for earlier, later in zip(tokens, tokens[1:])]


def answer_text(timeline):
    return "".join(json.loads(data).get("text", "") for _moment, event, data in timeline if event == "token")


class Terminal:
    """The webui's built-in terminal over POST plus SSE. Query strings in output are masked."""

    def __init__(self, client, session_id=None):
        self.client = client
        self.session_id = session_id
        self.buffer = []
        self.stop = threading.Event()
        self.reader = None

    def start(self):
        if not self.session_id:
            status, payload = self.client.request("POST", "api/session/new", {})
            if status != 200:
                return status, payload
            self.session_id = payload["session"]["session_id"]
        status, payload = self.client.request(
            "POST", "api/terminal/start", {"session_id": self.session_id, "rows": 40, "cols": 200})
        if status == 200:
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            time.sleep(1.5)
        return status, payload

    def _read(self):
        try:
            for _moment, _event, data in self.client.sse(f"api/terminal/output?session_id={self.session_id}"):
                if self.stop.is_set():
                    return
                try:
                    decoded = json.loads(data)
                except ValueError:
                    decoded = data
                if isinstance(decoded, dict):
                    decoded = decoded.get("data") or decoded.get("text") or decoded.get("output") or ""
                self.buffer.append(str(decoded))
        except (OSError, RuntimeError, http.client.HTTPException):
            return

    def run(self, command, wait=2.0, until=None):
        """Waits `wait` seconds, then up to 90 s for the text `until`, which the echoed command must not contain."""
        status, _payload = self.client.request(
            "POST", "api/terminal/input", {"session_id": self.session_id, "data": command + "\n"})
        time.sleep(wait)
        deadline = time.monotonic() + 90
        while until and until not in "".join(self.buffer) and time.monotonic() < deadline:
            time.sleep(0.5)
        text = ANSI.sub("", "".join(self.buffer)).replace("\r", "")
        self.buffer.clear()
        return status, re.sub(r"(https?://[^/\s]+/[^?\s]*)\?\S+", r"\1?<masked>", text)

    def close(self):
        self.stop.set()
        status = self.client.request("POST", "api/terminal/close", {"session_id": self.session_id})[0]
        # hermes-webui v0.52.544 reads the pty without the lock that guards its close. If a program is still
        # running at the close, its reader can, for up to 0.25 s, take the bytes of a new connection that got
        # the same fd number, and that request hangs for 30 s, then fails (traced). A test is that fast.
        time.sleep(0.5)
        return status
