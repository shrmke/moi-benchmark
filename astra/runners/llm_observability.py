"""Shared, agent-neutral LLM observability for benchmark runners.

The durable artifact is always ``agent/session.jsonl``. Hermes and Pi use a
temporary provider-boundary spool inside the task container; it is merged into
the session and never copied into the trial log directory as a separate LLM
log. Astra supplies the same canonical events from its native session journal.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import signal
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
REMOTE_PATH = "/installed-agent/moi-llm-observability.py"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 48765
PROXY_BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} contains a non-object JSONL row")
        rows.append(value)
    return rows


def _body_fields(body: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "body_base64": base64.b64encode(body).decode("ascii"),
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return result
    result["body_text"] = decoded
    try:
        result["body_json"] = json.loads(decoded)
    except json.JSONDecodeError:
        pass
    return result


def _safe_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers
        if name.lower() not in _SENSITIVE_HEADERS
    }


class _CaptureWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, exchange_id: str, **payload: Any) -> None:
        row = {
            "schema_version": SCHEMA_VERSION,
            "type": event_type,
            "exchange_id": exchange_id,
            "captured_at": time.time(),
            **payload,
        }
        encoded = json.dumps(
            row, ensure_ascii=False, separators=(",", ":")
        ) + "\n"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())


class CaptureProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "MOILLMCapture/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _handle(self) -> None:
        exchange_id = str(uuid.uuid4())
        length_value = self.headers.get("Content-Length")
        try:
            length = int(length_value or "0")
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(501, "chunked request bodies are unsupported")
            return
        body = self.rfile.read(length) if length else b""
        upstream = self.server.upstream  # type: ignore[attr-defined]
        writer = self.server.writer  # type: ignore[attr-defined]
        upstream_path = upstream.path.rstrip("/") + self.path
        writer.append(
            "provider_request",
            exchange_id,
            request={
                "method": self.command,
                "url": upstream.geturl().rstrip("/") + self.path,
                "headers": _safe_headers(self.headers.items()),
                **_body_fields(body),
            },
        )
        connection_class = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        connection_kwargs: dict[str, Any] = {"timeout": 300}
        if upstream.scheme == "https":
            connection_kwargs["context"] = ssl.create_default_context()
        connection = connection_class(
            upstream.hostname,
            upstream.port,
            **connection_kwargs,
        )
        forwarded_headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS | {"host", "content-length"}
        }
        forwarded_headers["Host"] = upstream.netloc
        if body:
            forwarded_headers["Content-Length"] = str(len(body))
        try:
            connection.request(
                self.command,
                upstream_path,
                body=body if body else None,
                headers=forwarded_headers,
            )
            response = connection.getresponse()
            response_headers = [
                (name, value)
                for name, value in response.getheaders()
                if name.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}
            ]
            writer.append(
                "provider_response_start",
                exchange_id,
                response={
                    "status": response.status,
                    "reason": response.reason,
                    "headers": _safe_headers(response.getheaders()),
                },
            )
            self.send_response(response.status, response.reason)
            for name, value in response_headers:
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sequence = 0
            while True:
                chunk = response.read1(16 * 1024)
                if not chunk:
                    break
                writer.append(
                    "provider_response_chunk",
                    exchange_id,
                    sequence=sequence,
                    chunk_base64=base64.b64encode(chunk).decode("ascii"),
                )
                sequence += 1
                self.wfile.write(chunk)
                self.wfile.flush()
            writer.append(
                "provider_response_end",
                exchange_id,
                chunk_count=sequence,
            )
        except Exception as exc:
            writer.append(
                "provider_error",
                exchange_id,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            if not self.wfile.closed:
                try:
                    self.send_error(502, "upstream provider request failed")
                except OSError:
                    pass
        finally:
            connection.close()

    do_DELETE = _handle
    do_GET = _handle
    do_PATCH = _handle
    do_POST = _handle
    do_PUT = _handle


def _serve(args: argparse.Namespace) -> int:
    upstream = urlsplit(args.upstream)
    if upstream.scheme not in {"http", "https"} or not upstream.hostname:
        raise ValueError("upstream must be an absolute HTTP(S) URL")
    capture = Path(args.capture).resolve()
    ready_file = Path(args.ready_file).resolve()
    pid_file = Path(args.pid_file).resolve()
    server = ThreadingHTTPServer((args.listen, args.port), CaptureProxyHandler)
    server.daemon_threads = True
    server.upstream = upstream  # type: ignore[attr-defined]
    server.writer = _CaptureWriter(capture)  # type: ignore[attr-defined]
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()) + "\n", encoding="ascii")
    ready_file.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "pid": os.getpid(),
                "listen": f"{args.listen}:{args.port}",
                "upstream": upstream.geturl(),
                "capture": str(capture),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        ready_file.unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
    return 0


def _read_live_pid(pid_file: Path) -> int:
    pid = int(pid_file.read_text(encoding="ascii").strip())
    if pid <= 1:
        raise ValueError("invalid proxy PID")
    os.kill(pid, 0)
    return pid


def _wait(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            _read_live_pid(Path(args.pid_file))
            value = json.loads(Path(args.ready_file).read_text(encoding="utf-8"))
            if value.get("schema_version") == SCHEMA_VERSION:
                return 0
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.05)
    return 1


def _stop(args: argparse.Namespace) -> int:
    pid_file = Path(args.pid_file)
    try:
        pid = _read_live_pid(pid_file)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        expected = str(Path(args.expected_capture).resolve()).encode()
        if Path(__file__).name.encode() not in cmdline or expected not in cmdline:
            raise RuntimeError("PID does not belong to the expected capture proxy")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return 0
            try:
                if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                    return 0
            except (OSError, IndexError):
                pass
            time.sleep(0.05)
        return 1
    except FileNotFoundError:
        return 0


def proxy_start_command(
    *, upstream: str, capture: str, ready_file: str, pid_file: str, log_file: str
) -> str:
    import shlex

    serve = shlex.join(
        [
            "python3",
            REMOTE_PATH,
            "serve",
            "--listen",
            PROXY_HOST,
            "--port",
            str(PROXY_PORT),
            "--upstream",
            upstream,
            "--capture",
            capture,
            "--ready-file",
            ready_file,
            "--pid-file",
            pid_file,
        ]
    )
    wait = shlex.join(
        [
            "python3",
            REMOTE_PATH,
            "wait",
            "--ready-file",
            ready_file,
            "--pid-file",
            pid_file,
            "--timeout",
            "10",
        ]
    )
    return f"{serve} > {shlex.quote(log_file)} 2>&1 & {wait}"


def proxy_stop_command(*, capture: str, pid_file: str) -> str:
    import shlex

    return shlex.join(
        [
            "python3",
            REMOTE_PATH,
            "stop",
            "--pid-file",
            pid_file,
            "--expected-capture",
            capture,
            "--timeout",
            "5",
        ]
    )


def _decode_proxy_exchanges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    exchanges: dict[str, dict[str, Any]] = {}
    for row in rows:
        exchange_id = row.get("exchange_id")
        if not isinstance(exchange_id, str):
            continue
        if exchange_id not in exchanges:
            exchanges[exchange_id] = {"chunks": [], "errors": []}
            order.append(exchange_id)
        exchange = exchanges[exchange_id]
        event_type = row.get("type")
        if event_type == "provider_request":
            exchange["request"] = row.get("request")
            exchange["request_at"] = row.get("captured_at")
        elif event_type == "provider_response_start":
            exchange["response_start"] = row.get("response")
        elif event_type == "provider_response_chunk":
            sequence = row.get("sequence")
            chunk = row.get("chunk_base64")
            if isinstance(sequence, int) and isinstance(chunk, str):
                exchange["chunks"].append((sequence, chunk))
        elif event_type == "provider_response_end":
            exchange["response_complete"] = True
        elif event_type == "provider_error":
            exchange["errors"].append(row.get("error"))

    canonical: list[dict[str, Any]] = []
    for exchange_id in order:
        exchange = exchanges[exchange_id]
        request = exchange.get("request")
        if isinstance(request, dict):
            canonical.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "llm_request_full",
                    "source": "provider_boundary",
                    "exchange_id": exchange_id,
                    "captured_at": exchange.get("request_at"),
                    "request": request,
                }
            )
        chunks = sorted(exchange["chunks"])
        if exchange.get("response_start") is not None or chunks or exchange["errors"]:
            try:
                body = b"".join(base64.b64decode(value) for _, value in chunks)
                body_value = _body_fields(body)
            except (ValueError, TypeError):
                body_value = {"body_decode_error": True}
            canonical.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "llm_response_full",
                    "source": "provider_boundary",
                    "exchange_id": exchange_id,
                    "complete": bool(exchange.get("response_complete")),
                    "response": {
                        **(exchange.get("response_start") or {}),
                        **body_value,
                    },
                    "errors": exchange["errors"],
                }
            )
    return canonical


def _astra_full_event(row: dict[str, Any]) -> dict[str, Any] | None:
    event_type = row.get("type")
    if event_type not in {"llm_request_full", "llm_response_full"}:
        return None
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    attempt = metadata.get("attempt", 0)
    exchange_id = (
        f"astra:{row.get('turn')}:{row.get('round')}:{attempt}:"
        f"{metadata.get('source', 'unknown')}"
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
        "source": "astra_native",
        "session_id": row.get("session_id"),
        "exchange_id": exchange_id,
        "turn": row.get("turn"),
        "round": row.get("round"),
        "attempt": attempt,
        "model": metadata.get("model"),
        "provider": metadata.get("provider"),
        "trace": metadata.get("trace"),
    }
    if event_type == "llm_request_full":
        value["request"] = metadata.get("request")
    else:
        value["response"] = metadata.get("response")
        value["complete"] = True
    return value


def _json_payloads(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    value = body.get("body_json")
    if isinstance(value, dict):
        return [value]
    text = body.get("body_text")
    if not isinstance(text, str):
        return []
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        candidate = line.removeprefix("data:").strip()
        if not candidate or candidate == "[DONE]":
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _find_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    usage = value.get("usage")
    if isinstance(usage, dict):
        return usage
    for key in ("response", "body_json"):
        nested = _find_usage(value.get(key))
        if nested is not None:
            return nested
    return None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first_integer(*values: Any) -> int | None:
    for value in values:
        parsed = _integer(value)
        if parsed is not None:
            return parsed
    return None


def summarize_token_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [row for row in records if row.get("type") == "llm_request_full"]
    responses = [row for row in records if row.get("type") == "llm_response_full"]
    totals = {
        "input_tokens": 0,
        "fresh_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }
    observed = {key: False for key in totals}
    usage_response_count = 0
    for response in responses:
        payloads = _json_payloads(response.get("response"))
        usages = [usage for value in payloads if (usage := _find_usage(value))]
        direct = _find_usage(response.get("response"))
        if direct is not None and direct not in usages:
            usages.append(direct)
        if not usages:
            continue
        # Streaming APIs may repeat cumulative usage. The final usage object is
        # authoritative for this exchange and must only be counted once.
        usage = usages[-1]
        usage_response_count += 1
        raw_input_tokens = _first_integer(
            usage.get("input_tokens"),
            usage.get("prompt_tokens"),
            usage.get("promptTokenCount"),
        )
        output_tokens = _first_integer(
            usage.get("output_tokens"),
            usage.get("completion_tokens"),
            usage.get("candidatesTokenCount"),
        )
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        input_details = usage.get("input_tokens_details")
        input_details = input_details if isinstance(input_details, dict) else {}
        completion_details = usage.get("completion_tokens_details")
        completion_details = (
            completion_details if isinstance(completion_details, dict) else {}
        )
        cache_read = _first_integer(
            usage.get("cache_read_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("cached_tokens"),
            usage.get("cacheRead"),
            usage.get("cachedContentTokenCount"),
            prompt_details.get("cached_tokens"),
            input_details.get("cached_tokens"),
        )
        cache_write = _first_integer(
            usage.get("cache_write_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cacheWrite"),
        )
        reasoning = _first_integer(
            usage.get("reasoning_tokens"),
            usage.get("thoughtsTokenCount"),
            completion_details.get("reasoning_tokens"),
        )
        anthropic_cache_semantics = (
            "cache_read_input_tokens" in usage
            or "cache_creation_input_tokens" in usage
        )
        input_tokens = raw_input_tokens
        fresh_input_tokens = raw_input_tokens
        if raw_input_tokens is not None and anthropic_cache_semantics:
            input_tokens = (
                raw_input_tokens + (cache_read or 0) + (cache_write or 0)
            )
        elif raw_input_tokens is not None and cache_read is not None:
            fresh_input_tokens = max(0, raw_input_tokens - cache_read)
        for key, value in (
            ("input_tokens", input_tokens),
            ("fresh_input_tokens", fresh_input_tokens),
            ("cache_read_tokens", cache_read),
            ("cache_write_tokens", cache_write),
            ("output_tokens", output_tokens),
            ("reasoning_tokens", reasoning),
        ):
            if value is not None:
                totals[key] += value
                observed[key] = True
    normalized = {
        key: totals[key] if observed[key] else None for key in totals
    }
    input_total = normalized["input_tokens"]
    output_total = normalized["output_tokens"]
    normalized["total_tokens"] = (
        input_total + output_total
        if input_total is not None and output_total is not None
        else None
    )
    normalized.update(
        {
            "schema_version": SCHEMA_VERSION,
            "source": "provider_response_usage",
            "scope": "all_observed_provider_exchanges",
            "input_semantics": "provider_input_total_including_cache",
            "fresh_input_semantics": "input_tokens_excluding_cache_reads_and_writes",
            "output_semantics": "provider_output_total_including_reasoning",
            "total_semantics": "input_tokens_plus_output_tokens",
            "request_count": len(requests),
            "response_count": len(responses),
            "usage_response_count": usage_response_count,
            "coverage": (
                "complete"
                if responses and usage_response_count == len(responses)
                else "partial"
                if usage_response_count
                else "missing"
            ),
            "reliable": bool(
                responses and usage_response_count == len(responses)
            ),
        }
    )
    return normalized


def write_canonical_session(
    *,
    output_path: Path,
    agent: str,
    session_id: str | None,
    native_sources: Iterable[tuple[str, Path]],
    proxy_capture_path: Path | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source, path in native_sources:
        try:
            for row in _jsonl_rows(path):
                full = _astra_full_event(row)
                if full is not None:
                    records.append(full)
                else:
                    records.append(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "type": "native_event",
                            "source": source,
                            "session_id": session_id,
                            "payload": row,
                        }
                    )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"source": source, "error": type(exc).__name__})
    if proxy_capture_path is not None:
        try:
            records.extend(_decode_proxy_exchanges(_jsonl_rows(proxy_capture_path)))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(
                {"source": "provider_boundary", "error": type(exc).__name__}
            )

    requests = [row for row in records if row.get("type") == "llm_request_full"]
    responses = [row for row in records if row.get("type") == "llm_response_full"]
    request_ids = {
        row.get("exchange_id") for row in requests if row.get("exchange_id")
    }
    response_ids = {
        row.get("exchange_id") for row in responses if row.get("exchange_id")
    }
    incomplete_response_ids = {
        row.get("exchange_id")
        for row in responses
        if row.get("complete") is False
    }
    unpaired = request_ids - response_ids
    if not requests:
        capture_status = "missing"
    elif unpaired or incomplete_response_ids or errors:
        capture_status = "partial"
    else:
        capture_status = "complete"
    token_usage = summarize_token_usage(records)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "session_log_path": "agent/session.jsonl",
        "session_id": session_id,
        "agent": agent,
        "capture_status": capture_status,
        "request_count": len(requests),
        "response_count": len(responses),
        "unpaired_request_count": len(unpaired),
        "incomplete_response_count": len(incomplete_response_ids),
        "errors": errors,
        "secret_header_policy": "authorization_cookie_and_api_key_headers_excluded",
        "token_usage": token_usage,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        header = {
            "schema_version": SCHEMA_VERSION,
            "type": "session_start",
            **summary,
        }
        stream.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.write(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "type": "session_end", **summary},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output_path)
    return summary


def observability_metadata(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_log_path": summary["session_log_path"],
        "llm_capture_status": summary["capture_status"],
        "llm_request_count": summary["request_count"],
        "llm_response_count": summary["response_count"],
        "llm_unpaired_request_count": summary["unpaired_request_count"],
        "llm_incomplete_response_count": summary["incomplete_response_count"],
        "llm_capture_errors": summary["errors"],
        "token_usage": summary["token_usage"],
    }


def apply_token_usage(context: Any, summary: dict[str, Any]) -> None:
    usage = summary["token_usage"]
    context.n_input_tokens = usage["input_tokens"]
    context.n_cache_tokens = usage["cache_read_tokens"]
    context.n_output_tokens = usage["output_tokens"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--listen", default=PROXY_HOST)
    serve.add_argument("--port", type=int, default=PROXY_PORT)
    serve.add_argument("--upstream", required=True)
    serve.add_argument("--capture", required=True)
    serve.add_argument("--ready-file", required=True)
    serve.add_argument("--pid-file", required=True)
    wait = commands.add_parser("wait")
    wait.add_argument("--ready-file", required=True)
    wait.add_argument("--pid-file", required=True)
    wait.add_argument("--timeout", type=float, default=10)
    stop = commands.add_parser("stop")
    stop.add_argument("--pid-file", required=True)
    stop.add_argument("--expected-capture", required=True)
    stop.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "wait":
        return _wait(args)
    return _stop(args)


if __name__ == "__main__":
    raise SystemExit(main())
