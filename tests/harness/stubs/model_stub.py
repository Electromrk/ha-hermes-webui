#!/usr/bin/env python3
"""OpenAI-compatible model stub for the test harness.

Streams delayed SSE chunks, so a buffering proxy shows up as one late burst.
No key is checked and nothing leaves the process.

Prompt conventions (last user message):
  CALL_TOOL <tool_name> <json-args>   answer with one tool call
  FAIL_HTTP <status>                  answer with that HTTP error
  anything else                       stream CHUNKS text chunks
After a tool result, the stub streams the first 400 characters of that result.

Every request is appended to --log as one JSON line. Bearer values are never logged.
"""
import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
LOCK = threading.Lock()


def log_event(event):
    event["ts"] = time.time()
    with LOCK:
        with open(ARGS.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")


def content_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            data = [{"id": m, "object": "model", "owned_by": "stub", "created": 0}
                    for m in ("stub-model", "stub-model-b")]
            return self._json(200, {"object": "list", "data": data})
        if self.path.rstrip("/").endswith("/health"):
            return self._json(200, {"ok": True})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": {"message": "bad json"}})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            log_event({"path": self.path, "unsupported": True})
            return self._json(404, {"error": {"message": "unsupported"}})
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        log_event({
            "path": self.path,
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "n_messages": len(messages),
            "last_role": last.get("role"),
            "last_text": content_text(last)[:300],
            "tools": tools,
            "has_auth": bool(self.headers.get("Authorization")),
        })
        failure = re.search(r"FAIL_HTTP\s+(\d{3})", content_text(last))
        if failure:
            return self._json(int(failure.group(1)), {"error": {"message": "stub failure", "type": "stub"}})
        text, tool_call = self._plan(messages)
        if body.get("stream"):
            return self._stream(body, text, tool_call)
        return self._complete(body, text, tool_call)

    def _plan(self, messages):
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            result = content_text(last)
            return "TOOL_RESULT_SEEN " + result[:400], None
        text = content_text(last)
        match = re.search(r"CALL_TOOL\s+(\S+)\s*(\{.*\})?", text, re.S)
        if match:
            return "", {"name": match.group(1), "arguments": match.group(2) or "{}"}
        return " ".join(f"chunk-{i}" for i in range(1, ARGS.chunks + 1)), None

    def _complete(self, body, text, tool_call):
        message = {"role": "assistant", "content": text or None}
        finish = "stop"
        if tool_call:
            finish = "tool_calls"
            message["tool_calls"] = [{
                "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                "function": tool_call,
            }]
        return self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def _send_chunk(self, payload):
        data = b"data: " + payload + b"\n\n"
        self.wfile.write(hex(len(data))[2:].encode() + b"\r\n" + data + b"\r\n")
        self.wfile.flush()

    def _stream(self, body, text, tool_call):
        chat_id = "chatcmpl-" + uuid.uuid4().hex[:12]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(delta, finish=None, **extra):
            return json.dumps({
                "id": chat_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                **extra,
            }).encode()

        try:
            self._send_chunk(frame({"role": "assistant", "content": ""}))
            if tool_call:
                time.sleep(ARGS.delay)
                call_id = "call_" + uuid.uuid4().hex[:12]
                self._send_chunk(frame({"tool_calls": [{
                    "index": 0, "id": call_id, "type": "function",
                    "function": {"name": tool_call["name"], "arguments": ""},
                }]}))
                time.sleep(ARGS.delay)
                self._send_chunk(frame({"tool_calls": [{
                    "index": 0, "function": {"arguments": tool_call["arguments"]},
                }]}))
                self._send_chunk(frame({}, "tool_calls"))
            else:
                for word in text.split(" "):
                    time.sleep(ARGS.delay)
                    self._send_chunk(frame({"content": word + " "}))
                self._send_chunk(frame({}, "stop"))
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            self._send_chunk(json.dumps({
                "id": chat_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model"),
                "choices": [], "usage": usage,
            }).encode())
            self._send_chunk(b"[DONE]")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log_event({"client_disconnected": True})
        self.close_connection = True


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--chunks", type=int, default=8)
    parser.add_argument("--log", default="/tmp/model_stub.jsonl")
    ARGS = parser.parse_args()
    ThreadingHTTPServer((ARGS.host, ARGS.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
