from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astra.runners.toolathlon_verified.model_proxy import (
    ModelProxyConfig,
    ModelProxyServer,
    RequestBudget,
    _provider_error_diagnostics,
    _UsageProbe,
    load_distinct_provider_credentials,
    provider_credential_fingerprint,
    provider_user_id,
)
from astra.runners.toolathlon_verified.contract import ContractError


class _Upstream:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.authorizations: list[str] = []
        self.connection_count = 0
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                with owner.lock:
                    owner.connection_count += 1

            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                owner.requests.append(json.loads(self.rfile.read(length)))
                owner.authorizations.append(self.headers.get("Authorization", ""))
                payload = json.dumps(
                    {
                        "id": "response",
                        "choices": [
                            {"message": {"content": "ok"}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", "provider-header-id")
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _BrokenStreamUpstream(_Upstream):
    def __init__(self) -> None:
        owner = self
        self.requests = []
        self.authorizations = []
        self.connection_count = 0
        self.request_count = 0
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                with owner.lock:
                    owner.connection_count += 1

            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                owner.requests.append(json.loads(self.rfile.read(length)))
                with owner.lock:
                    owner.request_count += 1
                    request_count = owner.request_count
                if request_count > 1:
                    payload = json.dumps(
                        {
                            "id": "recovered-response",
                            "choices": [
                                {
                                    "message": {"content": "ok"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                event = b'data: {"choices":[]}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(f"{len(event):X}\r\n".encode("ascii"))
                self.wfile.write(event)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)


class ModelProxyTests(unittest.TestCase):
    def test_proxy_enforces_frozen_wire_parameters_and_hides_provider_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Upstream() as upstream:
            root = Path(directory)
            key = "provider-key-never-forwarded-from-product"
            config = ModelProxyConfig(
                upstream_base_url=upstream.url,
                upstream_api_key=key,
                effective_model="deepseek-v4-flash",
                temperature=0,
                thinking="enabled",
                reasoning_effort="max",
                max_requests=100,
                run_id="run-1",
                system_id="astra",
                events_path=root / "events.jsonl",
                state_path=root / "state.json",
            )
            with ModelProxyServer(config) as proxy:
                payload = json.dumps(
                    {
                        "model": "different-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "mcp__toolathlon__local-claim_done",
                                    "parameters": {"type": "object"},
                                },
                            }
                        ],
                        "temperature": 1,
                        "top_p": 0.2,
                        "reasoning_effort": "low",
                        "thinking": {"type": "disabled"},
                        "user_id": "product-controlled-id",
                        "extra_body": {
                            "user_id": "product-controlled-nested-id",
                            "preserved": "yes",
                        },
                    }
                ).encode()
                request = urllib.request.Request(
                    proxy.url + "/chat/completions",
                    data=payload,
                    headers={
                        "Authorization": "Bearer untrusted-product-key",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
            forwarded = upstream.requests[0]
            self.assertEqual(forwarded["model"], "deepseek-v4-flash")
            self.assertEqual(forwarded["temperature"], 0)
            self.assertNotIn("top_p", forwarded)
            self.assertEqual(forwarded["reasoning_effort"], "max")
            self.assertEqual(forwarded["thinking"], {"type": "enabled"})
            self.assertEqual(forwarded["user_id"], provider_user_id("astra", "run-1"))
            self.assertEqual(forwarded["extra_body"], {"preserved": "yes"})
            self.assertEqual(upstream.authorizations, [f"Bearer {key}"])
            self.assertNotIn(key, (root / "events.jsonl").read_text())
            events = [
                json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
            ]
            completed = next(
                event for event in events if event["event"] == "model_request.completed"
            )
            started = next(
                event for event in events if event["event"] == "model_request.started"
            )
            self.assertEqual(started["request_tool_count"], 1)
            self.assertEqual(started["thinking"], "enabled")
            self.assertEqual(started["thinking_wire_behavior"], "sent")
            self.assertEqual(started["reasoning_effort"], "max")
            self.assertEqual(started["reasoning_effort_wire_behavior"], "sent")
            self.assertEqual(
                started["generation_parameter_source"], "benchmark_override"
            )
            self.assertEqual(
                started["request_tool_names"],
                ["mcp__toolathlon__local-claim_done"],
            )
            self.assertEqual(completed["model_request_id"], "run-1:model:1")
            self.assertEqual(completed["provider_response_id"]["value"], "response")
            self.assertEqual(
                completed["provider_header_request_id"]["value"],
                "provider-header-id",
            )
            self.assertEqual(completed["finish_reasons"], ["stop"])
            self.assertEqual(
                completed["token_usage"]["input_tokens"]["value"], 2
            )
            self.assertEqual(
                completed["token_usage"]["output_tokens"]["value"], 1
            )
            self.assertEqual(
                completed["token_usage"]["cache_write_tokens"],
                {
                    "value": None,
                    "source": "provider_response",
                    "reliability": "missing",
                    "missing_reason": "provider_not_reported",
                },
            )
            self.assertEqual(
                completed["retry_of"]["missing_reason"],
                "product_retry_relation_not_exposed",
            )
            diagnostics = completed["transport_diagnostics"]
            self.assertEqual(diagnostics["phase"], "complete")
            self.assertFalse(diagnostics["connection_reused"])
            self.assertEqual(diagnostics["upstream_http_status"], 200)
            self.assertGreater(diagnostics["upstream_body_bytes_read"], 0)
            self.assertEqual(
                diagnostics["provider_request_id"], "provider-header-id"
            )
            self.assertEqual(diagnostics["provider_response_id"], "response")
            self.assertIsNone(diagnostics["provider_error"])
            self.assertIsNone(diagnostics["exception"])
            ready = next(event for event in events if event["event"] == "proxy.ready")
            self.assertEqual(
                ready["provider_credential_fingerprint"],
                provider_credential_fingerprint(key),
            )
            self.assertEqual(ready["provider_user_id"], provider_user_id("astra", "run-1"))

    def test_proxy_reuses_upstream_keep_alive_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Upstream() as upstream:
            root = Path(directory)
            config = ModelProxyConfig(
                upstream_base_url=upstream.url,
                upstream_api_key="provider-key-for-keep-alive",
                effective_model="deepseek-v4-flash",
                temperature=0,
                thinking="enabled",
                reasoning_effort="max",
                max_requests=100,
                run_id="keep-alive",
                system_id="astra",
                events_path=root / "events.jsonl",
                state_path=root / "state.json",
            )
            with ModelProxyServer(config) as proxy:
                for _ in range(5):
                    payload = json.dumps(
                        {"model": "deepseek-v4-flash", "messages": []}
                    ).encode()
                    request = urllib.request.Request(
                        proxy.url + "/chat/completions",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        response.read()

            self.assertEqual(len(upstream.requests), 5)
            self.assertEqual(upstream.connection_count, 1)
            completed = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
                if '"event":"model_request.completed"' in line
            ]
            self.assertEqual(
                [row["transport_diagnostics"]["connection_reused"] for row in completed],
                [False, True, True, True, True],
            )

    def test_proxy_aborts_started_stream_when_upstream_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _BrokenStreamUpstream() as upstream:
            root = Path(directory)
            config = ModelProxyConfig(
                upstream_base_url=upstream.url,
                upstream_api_key="provider-key-for-broken-stream",
                effective_model="deepseek-v4-flash",
                temperature=0,
                thinking="enabled",
                reasoning_effort="max",
                max_requests=100,
                run_id="broken-stream",
                system_id="astra",
                events_path=root / "events.jsonl",
                state_path=root / "state.json",
            )
            with ModelProxyServer(config) as proxy:
                payload = json.dumps(
                    {"model": "deepseek-v4-flash", "messages": []}
                ).encode()
                request = (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: 127.0.0.1\r\nContent-Length: {len(payload)}\r\n"
                    "Content-Type: application/json\r\nConnection: keep-alive\r\n\r\n"
                ).encode() + payload
                with socket.create_connection(
                    ("127.0.0.1", proxy.server.server_port), timeout=2
                ) as client:
                    client.settimeout(2)
                    client.sendall(request)
                    response = bytearray()
                    connection_reset = False
                    while True:
                        try:
                            chunk = client.recv(64 * 1024)
                        except ConnectionResetError:
                            connection_reset = True
                            break
                        if not chunk:
                            break
                        response.extend(chunk)

                recovered_request = urllib.request.Request(
                    proxy.url + "/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(recovered_request, timeout=5) as recovered:
                    self.assertEqual(recovered.status, 200)
                    self.assertEqual(
                        json.loads(recovered.read())["id"], "recovered-response"
                    )

            self.assertTrue(connection_reset)
            self.assertEqual(upstream.connection_count, 2)
            self.assertIn(b"HTTP/1.1 200", response)
            self.assertNotIn(b"\r\nConnection: close\r\n", response)
            self.assertIn(b'data: {"choices":[]}', response)
            self.assertNotIn(b"HTTP/1.1 502", response)
            self.assertNotIn(b"provider_transport_error", response)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
            ]
            completed = [
                event for event in events if event["event"] == "model_request.completed"
            ]
            self.assertFalse(completed[0]["success"])
            self.assertEqual(completed[0]["error_type"]["value"], "IncompleteRead")
            diagnostics = completed[0]["transport_diagnostics"]
            self.assertEqual(diagnostics["phase"], "read_upstream_body")
            self.assertEqual(diagnostics["upstream_http_status"], 200)
            self.assertEqual(diagnostics["exception"]["source"], "upstream")
            self.assertEqual(diagnostics["exception"]["type"], "IncompleteRead")
            self.assertIn("partial_bytes", diagnostics["exception"])
            self.assertTrue(completed[1]["success"])

    def test_provider_error_diagnostics_are_bounded_and_redacted(self) -> None:
        probe = _UsageProbe()
        probe.feed(
            json.dumps(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "type": "rate_limit_error",
                        "message": "retry without secret-provider-key",
                    }
                }
            ).encode()
        )
        diagnostics = _provider_error_diagnostics(
            probe.metadata()["provider_error"], secret="secret-provider-key"
        )
        self.assertEqual(
            diagnostics,
            {
                "code": "rate_limit_exceeded",
                "type": "rate_limit_error",
                "message": "retry without [REDACTED]",
            },
        )

    def test_request_budget_allows_exactly_the_frozen_number(self) -> None:
        credentials = load_distinct_provider_credentials(
            {
                "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY": "astra-key-0000000",
                "TOOLATHLON_DEEPSEEK_HERMES_API_KEY": "hermes-key-000000",
            }
        )
        self.assertEqual(
            credentials,
            {"astra": "astra-key-0000000", "hermes": "hermes-key-000000"},
        )
        with self.assertRaises(ContractError):
            load_distinct_provider_credentials(
                {
                    "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY": "shared-key-000000",
                    "TOOLATHLON_DEEPSEEK_HERMES_API_KEY": "shared-key-000000",
                }
            )
        with self.assertRaises(ContractError):
            load_distinct_provider_credentials(
                {
                    "DEEPSEEK_API_KEY": "legacy-shared-key",
                    "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY": "astra-key-0000000",
                    "TOOLATHLON_DEEPSEEK_HERMES_API_KEY": "hermes-key-000000",
                }
            )
        budget = RequestBudget(2)
        first = budget.reserve()
        second = budget.reserve()
        self.assertTrue(first[0])
        self.assertTrue(second[0])
        self.assertFalse(budget.exceeded.is_set())
        budget.finish(provider_request=first[2], success=True)
        self.assertFalse(budget.exceeded.is_set())
        budget.finish(provider_request=second[2], success=True)
        self.assertTrue(budget.exceeded.is_set())
        self.assertFalse(budget.reserve()[0])
        self.assertTrue(budget.exceeded.is_set())
        self.assertEqual(budget.snapshot()["provider_requests_forwarded"], 2)


if __name__ == "__main__":
    unittest.main()
