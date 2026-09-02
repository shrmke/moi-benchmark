from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astra.runners.toolathlon_dsh.node_model_proxy import NodeModelProxyServer
from astra.runners.toolathlon_verified.artifact_contract import _validate_model_usage
from astra.runners.toolathlon_verified.model_proxy import ModelProxyConfig


class _Upstream:
    def __init__(
        self,
        *,
        break_first_stream: bool = False,
        close_first_before_headers: bool = False,
    ) -> None:
        self.break_first_stream = break_first_stream
        self.close_first_before_headers = close_first_before_headers
        self.connection_count = 0
        self.requests: list[dict] = []
        self.authorizations: list[str | None] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                with owner._lock:
                    owner.connection_count += 1

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                owner.requests.append(json.loads(self.rfile.read(length)))
                owner.authorizations.append(self.headers.get("Authorization"))
                if owner.close_first_before_headers and len(owner.requests) == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                    return
                if owner.break_first_stream and len(owner.requests) == 1:
                    event = b'data: {"id":"partial","choices":[]}\n\n'
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
                    return
                payload = json.dumps(
                    {
                        "id": "response",
                        "choices": [{"finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 2,
                            "total_tokens": 3,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", "provider-request")
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_Upstream":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _node() -> Path:
    value = os.environ.get("TOOLATHLON_DSH_NODE") or shutil.which("node")
    if not value:
        raise unittest.SkipTest("Node is unavailable")
    return Path(value).resolve()


def _config(root: Path, upstream_url: str, *, run_id: str) -> ModelProxyConfig:
    return ModelProxyConfig(
        upstream_base_url=upstream_url,
        upstream_api_key="provider-key-for-node-sidecar",
        effective_model="deepseek-v4-flash",
        temperature=0,
        thinking="enabled",
        reasoning_effort="max",
        max_requests=100,
        run_id=run_id,
        system_id="astra",
        events_path=root / "events.jsonl",
        state_path=root / "state.json",
    )


def _wait_for_completed(proxy: NodeModelProxyServer, expected: int) -> dict:
    deadline = time.monotonic() + 5
    while True:
        snapshot = proxy.budget.snapshot()
        if snapshot["provider_requests_completed"] == expected:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"model requests did not settle: {snapshot}")
        time.sleep(0.01)


def _node_fetch(node: Path, url: str) -> dict:
    client = """
const url = process.argv[1]
try {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({model: 'wrong', messages: [], stream: true}),
  })
  for await (const chunk of response.body) void chunk
  console.log(JSON.stringify({cleanEof: true, status: response.status}))
} catch (error) {
  console.log(JSON.stringify({
    cleanEof: false,
    name: error.name,
    message: error.message,
    code: error.cause?.code || null,
  }))
}
"""
    result = subprocess.run(
        [str(node), "--input-type=module", "--eval", client, url],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


class NodeModelProxyTests(unittest.TestCase):
    def test_undici_sidecar_freezes_request_and_reuses_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Upstream() as upstream:
            root = Path(directory)
            config = _config(root, upstream.url, run_id="node-keep-alive")
            with NodeModelProxyServer(config, node=_node()) as proxy:
                for _ in range(5):
                    request = urllib.request.Request(
                        proxy.url + "/chat/completions",
                        data=json.dumps(
                            {
                                "model": "wrong-model",
                                "messages": [],
                                "top_p": 0.5,
                            }
                        ).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        response.read()
                snapshot = _wait_for_completed(proxy, 5)
                self.assertTrue(proxy.undici_version)

            self.assertEqual(snapshot["provider_requests_failed"], 0)
            self.assertEqual(len(upstream.requests), 5)
            self.assertLessEqual(upstream.connection_count, 2)
            self.assertEqual(
                upstream.authorizations,
                ["Bearer provider-key-for-node-sidecar"] * 5,
            )
            normalized = upstream.requests[0]
            self.assertEqual(normalized["model"], "deepseek-v4-flash")
            self.assertEqual(normalized["temperature"], 0)
            self.assertEqual(normalized["thinking"], {"type": "enabled"})
            self.assertNotIn("top_p", normalized)
            event_text = config.events_path.read_text(encoding="utf-8")
            self.assertNotIn("provider-key-for-node-sidecar", event_text)
            events = [json.loads(line) for line in event_text.splitlines()]
            _validate_model_usage(
                events,
                {"run_id": "node-keep-alive", "system_id": "astra"},
            )
            completed = [
                event for event in events if event["event"] == "model_request.completed"
            ]
            self.assertEqual(
                completed[-1]["transport_diagnostics"]["transport"],
                "node_fetch_undici",
            )
            self.assertTrue(completed[-1]["transport_diagnostics"]["connection_reused"])

    def test_broken_upstream_stream_remains_a_transport_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Upstream(
            break_first_stream=True
        ) as upstream:
            root = Path(directory)
            config = _config(root, upstream.url, run_id="node-broken-stream")
            node = _node()
            with NodeModelProxyServer(config, node=node) as proxy:
                failure = _node_fetch(
                    node,
                    proxy.url + "/chat/completions",
                )
                self.assertFalse(failure["cleanEof"])
                self.assertEqual(failure["message"], "terminated")
                self.assertEqual(failure["code"], "UND_ERR_SOCKET")

                request = urllib.request.Request(
                    proxy.url + "/chat/completions",
                    data=json.dumps({"model": "wrong", "messages": []}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.loads(response.read())["id"], "response")
                snapshot = _wait_for_completed(proxy, 2)

            self.assertEqual(snapshot["provider_requests_failed"], 1)
            events = [
                json.loads(line)
                for line in config.events_path.read_text(encoding="utf-8").splitlines()
            ]
            completed = [
                event for event in events if event["event"] == "model_request.completed"
            ]
            diagnostics = completed[0]["transport_diagnostics"]
            self.assertEqual(diagnostics["phase"], "read_upstream_body")
            self.assertEqual(diagnostics["exception"]["source"], "upstream")
            self.assertEqual(diagnostics["exception"]["code"], "UND_ERR_SOCKET")
            self.assertTrue(completed[1]["success"])

    def test_failure_before_upstream_headers_is_not_converted_to_http_502(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Upstream(
            close_first_before_headers=True
        ) as upstream:
            root = Path(directory)
            config = _config(root, upstream.url, run_id="node-no-upstream-headers")
            node = _node()
            with NodeModelProxyServer(config, node=node) as proxy:
                failure = _node_fetch(node, proxy.url + "/chat/completions")
                self.assertFalse(failure["cleanEof"])
                self.assertEqual(failure["code"], "UND_ERR_SOCKET")
                snapshot = _wait_for_completed(proxy, 1)

            self.assertEqual(snapshot["provider_requests_failed"], 1)
            events = [
                json.loads(line)
                for line in config.events_path.read_text(encoding="utf-8").splitlines()
            ]
            completed = next(
                event for event in events if event["event"] == "model_request.completed"
            )
            self.assertIsNone(completed["http_status"])
            diagnostics = completed["transport_diagnostics"]
            self.assertIsNone(diagnostics["upstream_http_status"])
            self.assertFalse(diagnostics["downstream_response_started"])
            self.assertEqual(diagnostics["exception"]["source"], "upstream")


if __name__ == "__main__":
    unittest.main()
