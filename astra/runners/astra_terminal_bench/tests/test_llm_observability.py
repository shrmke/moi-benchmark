from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from astra.runners.llm_observability import (
    CaptureProxyHandler,
    _CaptureWriter,
    summarize_token_usage,
    write_canonical_session,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


class _UpstreamHandler(BaseHTTPRequestHandler):
    received_path = None
    received_body = None

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).received_path = self.path
        type(self).received_body = self.rfile.read(length)
        response = json.dumps(
            {
                "choices": [{"message": {"content": "done"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class LlmObservabilityTests(unittest.TestCase):
    def test_provider_boundary_preserves_exact_bodies_and_token_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
            upstream_thread = threading.Thread(
                target=upstream.serve_forever, daemon=True
            )
            upstream_thread.start()
            capture_path = root / "capture.jsonl"
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), CaptureProxyHandler)
            proxy.daemon_threads = True
            proxy.upstream = urlsplit(
                f"http://127.0.0.1:{upstream.server_port}/root"
            )
            proxy.writer = _CaptureWriter(capture_path)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            request = json.dumps(
                {
                    "model": "glm-5.2",
                    "messages": [
                        {"role": "system", "content": "system prompt"},
                        {"role": "user", "content": "hello"},
                    ],
                    "tools": [{"type": "function", "function": {"name": "bash"}}],
                },
                separators=(",", ":"),
            ).encode()
            try:
                client = http.client.HTTPConnection(
                    "127.0.0.1", proxy.server_port, timeout=5
                )
                client.request(
                    "POST",
                    "/chat/completions",
                    body=request,
                    headers={
                        "Authorization": "Bearer must-not-be-saved",
                        "Content-Type": "application/json",
                    },
                )
                response = client.getresponse()
                response_body = response.read()
                client.close()
            finally:
                proxy.shutdown()
                proxy.server_close()
                upstream.shutdown()
                upstream.server_close()

            self.assertEqual(response.status, 200)
            self.assertEqual(_UpstreamHandler.received_path, "/root/chat/completions")
            self.assertEqual(_UpstreamHandler.received_body, request)
            session_path = root / "session.jsonl"
            summary = write_canonical_session(
                output_path=session_path,
                agent="test-agent",
                session_id="session-1",
                native_sources=[],
                proxy_capture_path=capture_path,
            )

            rows = [json.loads(line) for line in session_path.read_text().splitlines()]
            request_row = next(row for row in rows if row["type"] == "llm_request_full")
            response_row = next(row for row in rows if row["type"] == "llm_response_full")
            self.assertEqual(
                base64.b64decode(request_row["request"]["body_base64"]), request
            )
            self.assertNotIn("Authorization", request_row["request"]["headers"])
            self.assertEqual(
                base64.b64decode(response_row["response"]["body_base64"]),
                response_body,
            )
            self.assertEqual(summary["capture_status"], "complete")
            usage = summary["token_usage"]
            self.assertEqual(usage["input_tokens"], 100)
            self.assertEqual(usage["fresh_input_tokens"], 60)
            self.assertEqual(usage["cache_read_tokens"], 40)
            self.assertEqual(usage["output_tokens"], 20)
            self.assertEqual(usage["reasoning_tokens"], 7)
            self.assertEqual(usage["total_tokens"], 120)
            self.assertTrue(usage["reliable"])

    def test_partial_exchange_is_saved_without_inventing_token_zeroes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.jsonl"
            _write_jsonl(
                capture,
                [
                    {
                        "type": "provider_request",
                        "exchange_id": "exchange-1",
                        "request": {
                            "body_base64": base64.b64encode(b"{}").decode(),
                            "body_text": "{}",
                            "body_json": {},
                        },
                    },
                    {
                        "type": "provider_response_start",
                        "exchange_id": "exchange-1",
                        "response": {"status": 200},
                    },
                    {
                        "type": "provider_response_chunk",
                        "exchange_id": "exchange-1",
                        "sequence": 0,
                        "chunk_base64": base64.b64encode(b"data: {\"choices\":[]}").decode(),
                    },
                ],
            )
            summary = write_canonical_session(
                output_path=root / "session.jsonl",
                agent="test-agent",
                session_id="session-1",
                native_sources=[],
                proxy_capture_path=capture,
            )

            self.assertEqual(summary["capture_status"], "partial")
            self.assertEqual(summary["incomplete_response_count"], 1)
            self.assertIsNone(summary["token_usage"]["input_tokens"])
            self.assertIsNone(summary["token_usage"]["output_tokens"])
            self.assertIsNone(summary["token_usage"]["total_tokens"])

    def test_astra_native_full_events_are_normalized_into_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "astra.jsonl"
            common = {
                "session_id": "session-1",
                "turn": 2,
                "round": 3,
            }
            _write_jsonl(
                native,
                [
                    {
                        **common,
                        "type": "llm_request_full",
                        "metadata": {
                            "attempt": 0,
                            "source": "inprocess",
                            "model": "glm-5.2",
                            "provider": "zai",
                            "request": {
                                "messages": [{"role": "user", "content": "hello"}],
                                "tools": [],
                            },
                        },
                    },
                    {
                        **common,
                        "type": "llm_response_full",
                        "metadata": {
                            "attempt": 0,
                            "source": "inprocess",
                            "model": "glm-5.2",
                            "provider": "zai",
                            "response": {
                                "outcome": "success",
                                "response": {
                                    "full_text": "done",
                                    "usage": {
                                        "prompt_tokens": 11,
                                        "completion_tokens": 5,
                                    },
                                },
                            },
                        },
                    },
                ],
            )
            summary = write_canonical_session(
                output_path=root / "session.jsonl",
                agent="astra",
                session_id="session-1",
                native_sources=[("astra_session_journal", native)],
            )

            self.assertEqual(summary["capture_status"], "complete")
            self.assertEqual(summary["request_count"], 1)
            self.assertEqual(summary["response_count"], 1)
            self.assertEqual(summary["token_usage"]["total_tokens"], 16)

    def test_anthropic_cache_components_normalize_to_input_total(self):
        usage = summarize_token_usage(
            [
                {"type": "llm_request_full", "exchange_id": "exchange-1"},
                {
                    "type": "llm_response_full",
                    "exchange_id": "exchange-1",
                    "response": {
                        "body_json": {
                            "usage": {
                                "input_tokens": 10,
                                "cache_read_input_tokens": 3,
                                "cache_creation_input_tokens": 2,
                                "output_tokens": 4,
                            }
                        }
                    },
                },
            ]
        )

        self.assertEqual(usage["fresh_input_tokens"], 10)
        self.assertEqual(usage["cache_read_tokens"], 3)
        self.assertEqual(usage["cache_write_tokens"], 2)
        self.assertEqual(usage["input_tokens"], 15)
        self.assertEqual(usage["total_tokens"], 19)


if __name__ == "__main__":
    unittest.main()
