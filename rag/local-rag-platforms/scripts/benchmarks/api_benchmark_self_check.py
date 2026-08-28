from __future__ import annotations

import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


SRC = Path(__file__).resolve().parents[2] / "dify-rag-eval/src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dify_rag_eval.api_benchmark import (  # noqa: E402
    ScenarioConfig,
    TargetConfig,
    measure_request,
    run_benchmark,
    summarize_samples,
    write_markdown_report,
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/sse-workflow-failed":
            body = b'data: {"event":"workflow_finished","data":{"status":"failed","error":"boom"}}\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-error":
            body = b'data: {"error":"failed"}\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-a2a-complete":
            if self.headers.get("Accept") != "text/event-stream":
                self.send_error(406)
                return
            body = b'data: {"result":{"kind":"status-update","status":{"state":"completed"},"final":true}}\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-openai-complete":
            body = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-fastgpt-complete":
            body = b'data: {"event":"workflowDuration","data":{"durationSeconds":0.1}}\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-maxkb-complete":
            if self.headers.get("Accept") != "*/*":
                self.send_error(406)
                return
            body = b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}\n\n'
            content_type = "text/event-stream"
        elif self.path == "/sse-incomplete":
            body = b'data: {"event":"message","data":"x"}\n\n'
            content_type = "text/event-stream"
        else:
            body = b"" if self.path == "/sse-empty" else b'{"ok":true}'
            content_type = "text/event-stream" if self.path == "/sse-empty" else "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def test_protocol_metrics_do_not_fabricate_ttfe_or_sse_success() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = TargetConfig(
        name="test",
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key_env=None,
        auth_header=None,
    )
    try:
        json_sample = measure_request(
            target, ScenarioConfig("events", "/json", protocol="json", body={}), {}, 5
        )
        json_summary = summarize_samples([json_sample], 1, 1, 1)
        assert json_sample["first_byte_ms"] is not None
        assert json_sample["first_event_ms"] is None
        assert json_summary["ttfb_ms"]["count"] == 1
        assert json_summary["ttfb_ms"]["p90"] == json_sample["first_byte_ms"]
        assert json_summary["ttfe_ms"]["count"] == 0
        assert json_summary["ttfe_ms"]["p90"] is None
        assert json_summary["event_throughput_events_per_s"] is None

        scenario = ScenarioConfig("events", "/json", protocol="json", body={})
        report, _ = run_benchmark(
            {"test": TargetConfig("test", target.base_url, None, None, event=scenario)},
            scenarios=("events",),
            connection_levels=(1,),
            duration_s=0.05,
            warmup_s=0,
            max_requests=2,
            environ={},
        )
        assert 0 < report["results"][0]["average_in_flight"] <= 1
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.md"
            write_markdown_report(report, report_path)
            assert "TTFB p50/p95" in report_path.read_text()

        error_sample = measure_request(
            target,
            ScenarioConfig("events", "/sse-error", protocol="sse", body={}),
            {},
            5,
        )
        error_summary = summarize_samples([error_sample], 1, 1, 1)
        assert error_sample["transport_success"] is True
        assert error_sample["success"] is False
        assert error_summary["transport_successes"] == 1
        assert error_summary["successes"] == 0
        assert error_summary["application_error_requests"] == 1

        workflow_error_sample = measure_request(
            target,
            ScenarioConfig("events", "/sse-workflow-failed", protocol="sse", body={}),
            {},
            5,
        )
        assert workflow_error_sample["transport_success"] is True
        assert workflow_error_sample["success"] is False
        assert workflow_error_sample["application_error_events"] == 1

        empty_sample = measure_request(
            target,
            ScenarioConfig("events", "/sse-empty", protocol="sse", body={}),
            {},
            5,
        )
        empty_summary = summarize_samples([empty_sample], 1, 1, 1)
        assert empty_sample["transport_success"] is True
        assert empty_sample["success"] is False
        assert empty_sample["application_error"] == "empty_sse_stream"
        assert empty_summary["application_error_requests"] == 1

        incomplete = measure_request(
            target, ScenarioConfig("events", "/sse-incomplete", protocol="sse", body={}), {}, 5
        )
        assert incomplete["success"] is False
        assert incomplete["application_error"] == "incomplete_sse_stream"

        for path in ("/sse-a2a-complete", "/sse-openai-complete", "/sse-fastgpt-complete"):
            complete = measure_request(
                target, ScenarioConfig("events", path, protocol="sse", body={}), {}, 5
            )
            assert complete["success"] is True
            assert complete["terminal_success_events"] == 1

        maxkb_complete = measure_request(
            target,
            ScenarioConfig(
                "events", "/sse-maxkb-complete", protocol="sse", body={}, headers={"Accept": "*/*"}
            ),
            {},
            5,
        )
        assert maxkb_complete["success"] is True
        assert maxkb_complete["terminal_success_events"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    test_protocol_metrics_do_not_fabricate_ttfe_or_sse_success()
