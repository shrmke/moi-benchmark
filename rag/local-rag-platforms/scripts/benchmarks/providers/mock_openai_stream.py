#!/usr/bin/env python3
"""Deterministic OpenAI-compatible chat server for API capacity benchmarks."""

from __future__ import annotations

import argparse
import json
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import urlsplit


MODEL = "mock-stream"


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], chunks: int, delay_ms: float):
        super().__init__(address, Handler)
        self.chunks = chunks
        self.delay_s = delay_ms / 1000


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/health":
            self._json({"status": "ok"})
        elif path in {"/models", "/v1/models"}:
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model", "type": "chat"}]})
        else:
            self._json({"error": "not_found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path not in {"/chat/completions", "/v1/chat/completions"}:
            self._json({"error": "not_found", "path": path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid_json"}, 400)
            return
        if request.get("stream"):
            self._stream()
            return
        content = "x" * self.server.chunks  # type: ignore[attr-defined]
        self._json(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": len(content),
                    "total_tokens": len(content) + 1,
                },
            }
        )

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for _ in range(self.server.chunks):  # type: ignore[attr-defined]
            time.sleep(self.server.delay_s)  # type: ignore[attr-defined]
            self._event({"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]})
        self._event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _event(self, payload: dict[str, Any]) -> None:
        value = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL,
            **payload,
        }
        self.wfile.write(b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def self_check() -> None:
    server = MockServer(("127.0.0.1", 0), chunks=2, delay_ms=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=b'{"stream":true}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert body.count(b"data: ") == 4
        assert body.endswith(b"data: [DONE]\n\n")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/models?source=self-check")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["data"][0]["type"] == "chat"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--chunks", type=int, default=64)
    parser.add_argument("--delay-ms", type=float, default=10)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.chunks <= 0 or args.delay_ms < 0:
        parser.error("--chunks must be positive and --delay-ms cannot be negative")
    if args.self_check:
        self_check()
        print("mock_openai_stream: ok")
        return
    server = MockServer((args.host, args.port), args.chunks, args.delay_ms)
    print(
        json.dumps(
            {
                "base_url": f"http://{args.host}:{server.server_port}/v1",
                "model": MODEL,
                "chunks": args.chunks,
                "delay_ms": args.delay_ms,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
