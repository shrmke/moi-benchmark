#!/usr/bin/env python3
"""Run Astra /chat/stream slices with a request-scoped MCP binding.

This is a benchmark transport shim, not an Agent implementation. Astra's
frozen server owns the agent loop, model calls, MCP discovery, and MCP tool
execution. The shim only submits the native request and records its SSE
events as JSONL for the common artifact normalizer. Explicit budget pauses
continue in the same session under the original outer process budget.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _event_type(row: dict[str, Any]) -> str:
    value = row.get("type") or row.get("event_type") or row.get("event") or ""
    return str(value).strip().lower()


def _string_field(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    data = row.get("data")
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _read_public_prompt() -> tuple[str, str]:
    try:
        value = json.load(sys.stdin)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("stdin must contain the public Astra request JSON") from exc
    if not isinstance(value, dict) or sorted(value) != ["system", "task"]:
        raise ValueError("stdin request must contain exactly system and task")
    return (
        _nonempty_string(value.get("system"), "system"),
        _nonempty_string(value.get("task"), "task"),
    )


def _validate_loopback_api_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Astra API URL must be loopback HTTP")
    return value.rstrip("/")


def _request_body(args: argparse.Namespace, system: str, task: str) -> dict[str, Any]:
    return {
        "message": task,
        "runtime_system_prompt": system,
        "model_selection": {"offering_id": os.environ["TOOLATHLON_ASTRA_OFFERING_ID"]},
        "runtime_profile": "request_scoped_runtime_mcp",
        "runtime_mcp_bindings": [
            {
                "id": "toolathlon",
                "transport": "sse",
                "url": args.gateway_url,
            }
        ],
        "interaction_mode": "auto",
        "interactive_client": False,
    }


def _emit_error(message: str, *, code: str) -> int:
    row = {"type": "error", "message": message, "code": code, "retryable": False}
    print(json.dumps(row, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
    print(
        json.dumps(
            {"success": False, "error": message, "response": ""},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 1


def run(args: argparse.Namespace) -> int:
    access_token = os.environ.get("ASTRA_ACCESS_TOKEN", "")
    if not access_token:
        return _emit_error("missing ephemeral Astra access token", code="missing_auth")
    try:
        system, task = _read_public_prompt()
        api_url = _validate_loopback_api_url(args.api_url)
        body = _request_body(args, system, task)
    except ValueError as exc:
        return _emit_error(str(exc), code="invalid_request")

    # Keep one monitored process and one proxy across all slices: the caller's
    # task deadline and model-request counter must never reset on continuation.
    observed_session_id: str | None = None
    continuation_count = 0
    while True:
        request = Request(
            f"{api_url}/chat/stream",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        text_parts: list[str] = []
        final_text: str | None = None
        terminal_seen = False
        terminal_error: str | None = None
        continuation_requested = False
        continuation_reason = None
        paused_finished = False
        fatal_event = False
        source_run_id: str | None = None
        try:
            with urlopen(request, timeout=args.socket_timeout_seconds) as response:
                if response.status != 200:
                    return _emit_error(
                        f"Astra chat stream returned HTTP {response.status}",
                        code="astra_http_error",
                    )
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        row = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    print(
                        json.dumps(row, ensure_ascii=False, sort_keys=True),
                        file=sys.stderr,
                        flush=True,
                    )
                    event = _event_type(row)
                    event_run_id = _string_field(row, "run_id")
                    if event_run_id:
                        source_run_id = event_run_id
                    event_session_id = _string_field(row, "session_id")
                    if event_session_id:
                        if (
                            observed_session_id is not None
                            and observed_session_id != event_session_id
                        ):
                            return _emit_error(
                                "Astra chat stream changed session identity",
                                code="astra_session_identity_changed",
                            )
                        observed_session_id = event_session_id
                    if event == "text_delta":
                        content = _string_field(row, "content")
                        if content:
                            text_parts.append(content)
                    elif event == "text_done":
                        final_text = _string_field(row, "full_text", "content") or final_text
                    elif event == "turn_complete":
                        terminal_seen = True
                        final_text = _string_field(row, "assistant_text") or final_text
                    elif event == "done":
                        terminal_seen = True
                    elif event == "run_finished":
                        terminal_seen = True
                        status = (_string_field(row, "status") or "completed").lower()
                        paused_finished = status == "paused" and row.get("resumable") is True
                        if status not in {"completed", "complete", "success", "succeeded", "ok"}:
                            terminal_error = _string_field(row, "error", "message") or status
                    elif event == "run_interrupted":
                        continuation_reason = row.get("kind")
                        continuation_requested = (
                            row.get("kind") in {"budget_exhausted", "execution_incomplete"}
                            and row.get("resumable") is True
                            and row.get("resume_action") == "continue_immediately"
                            and row.get("resume_mode") == "continue"
                        )
                        fatal_event = fatal_event or not continuation_requested
                        terminal_error = _string_field(row, "message", "error", "code") or event
                    elif event in {"error", "run_error", "run_cancelled"}:
                        fatal_event = True
                        terminal_error = _string_field(row, "message", "error", "code") or event
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            return _emit_error(
                f"Astra chat stream returned HTTP {exc.code}: {detail}",
                code="astra_http_error",
            )
        except (URLError, TimeoutError, OSError) as exc:
            return _emit_error(
                f"Astra chat stream transport failed: {type(exc).__name__}",
                code="astra_transport_error",
            )


        if continuation_requested and paused_finished and not fatal_event:
            if observed_session_id is None:
                return _emit_error(
                    "Cannot continue Astra without the original session identity",
                    code="astra_session_identity_missing",
                )
            continuation_count += 1
            print(json.dumps({
                "type": "transport_continuation",
                "session_id": observed_session_id,
                "source_run_id": source_run_id,
                "continuation_index": continuation_count,
                "reason": continuation_reason,
            }, sort_keys=True), file=sys.stderr, flush=True)
            # A finished execution slice is continued through session history,
            # not by replaying the original task or resuming a dead executor.
            body["session_id"] = observed_session_id
            body["message"] = "Continue the unfinished task from the current session state."
            continue
        break

    output = final_text if final_text is not None else "".join(text_parts)
    if terminal_error:
        return _emit_error(terminal_error, code="astra_run_error")
    if not terminal_seen:
        return _emit_error(
            "Astra chat stream closed without a terminal event",
            code="astra_stream_incomplete",
        )
    if observed_session_id is None:
        return _emit_error(
            "Astra chat stream did not report its auto-created session identity",
            code="astra_session_identity_missing",
        )
    print(
        json.dumps(
            {
                "success": True,
                "error": None,
                "response": output,
                "session_id": observed_session_id,
                "continuation_count": continuation_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--socket-timeout-seconds", type=float, default=310.0)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
