#!/usr/bin/env python3
"""Drive the DSH JSON-RPC runtime and preserve one task's native event stream."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, TextIO


class ProtocolError(RuntimeError):
    """The DSH runtime violated the expected JSON-RPC activity contract."""


def _event_from(
    message: dict[str, Any], session_id: str
) -> Optional[dict[str, Any]]:
    if message.get("method") != "session.event":
        return None
    params = message.get("params")
    if not isinstance(params, dict) or params.get("sessionId") != session_id:
        return None
    event = params.get("event")
    return event if isinstance(event, dict) else None


def _has_receipt(event: dict[str, Any], message_id: str) -> bool:
    if event.get("type") != "agent/inbox/spliced":
        return False
    data = event.get("data")
    inserted = data.get("inserted") if isinstance(data, dict) else None
    return isinstance(inserted, list) and any(
        isinstance(message, dict) and message.get("id") == message_id
        for message in inserted
    )


def summarize_events(
    events: list[dict[str, Any]],
    session_id: str,
    *,
    max_turns: Optional[int] = None,
) -> dict[str, Any]:
    final_response = ""
    finish_reason: Optional[str] = None
    tool_calls = 0
    turns = 0
    steps = 0
    usage_seen = False
    usage = {
        "fresh_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }

    for event in events:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "turn/start":
            turns += 1
        elif event_type == "step/start":
            steps += 1
        elif event_type == "tool/call":
            tool_calls += 1
        elif event_type == "turn/end":
            reason = data.get("reason")
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if not isinstance(kind, str):
                raise ProtocolError("turn/end requires a string data.reason.kind")
            finish_reason = kind
        elif event_type == "assistant/message":
            message = data.get("message")
            content_owner = message if isinstance(message, dict) else data
            content = content_owner.get("content")
            if isinstance(content, list):
                final_response = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            reading = data.get("usage")
            if isinstance(reading, dict):
                usage_seen = True
                for source, target in (
                    ("inputTokens", "fresh_input_tokens"),
                    ("cacheReadTokens", "cache_read_tokens"),
                    ("cacheWriteTokens", "cache_write_tokens"),
                    ("outputTokens", "output_tokens"),
                    ("reasoningTokens", "reasoning_tokens"),
                ):
                    value = reading.get(source, 0)
                    if type(value) is not int or value < 0:
                        raise ProtocolError(f"invalid DSH usage field {source}")
                    usage[target] += value

    if finish_reason == "blocked" and max_turns is not None and steps == max_turns:
        finish_reason = "max_turns"

    return {
        "schema_version": 1,
        "status": "completed",
        "session_id": session_id,
        "final_response": final_response,
        "finish_reason": finish_reason,
        "event_count": len(events),
        "turn_count": turns,
        "step_count": steps,
        "tool_call_count": tool_calls,
        "usage": usage if usage_seen else None,
    }


class RuntimeClient:
    def __init__(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        stderr: TextIO,
        event_stream: TextIO,
        timeout_sec: float,
    ) -> None:
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=False,
            bufsize=0,
            env=env,
            start_new_session=False,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise ProtocolError("failed to open DSH runtime stdio")
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._events = event_stream
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._stdout, selectors.EVENT_READ)
        self._stdout_buffer = b""
        self._deadline = time.monotonic() + timeout_sec

    def send(self, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        self._stdin.write(payload)
        self._stdin.flush()

    def receive(self) -> dict[str, Any]:
        while b"\n" not in self._stdout_buffer:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise TimeoutError("timed out waiting for the DSH JSON-RPC runtime")
            chunk = os.read(self._stdout.fileno(), 65536)
            if not chunk:
                code = self.process.poll()
                raise ProtocolError(
                    f"DSH runtime closed stdout unexpectedly (exit={code})"
                )
            self._stdout_buffer += chunk
        raw_line, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
        try:
            message = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("DSH runtime emitted non-JSON stdout") from exc
        if not isinstance(message, dict):
            raise ProtocolError("DSH runtime message must be a JSON object")
        self._events.write(
            json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self._events.flush()
        return message

    def wait_response(
        self, request_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        pending: list[dict[str, Any]] = []
        while True:
            message = self.receive()
            if message.get("id") == request_id:
                if "error" in message:
                    raise ProtocolError(
                        f"DSH JSON-RPC request {request_id} failed: {message['error']}"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ProtocolError(
                        f"DSH JSON-RPC response {request_id} has no object result"
                    )
                return result, pending
            pending.append(message)

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    self.send({"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
                    self.wait_response(3)
                except (BrokenPipeError, OSError, ProtocolError, TimeoutError):
                    # Shutdown is best-effort after the run result (or its
                    # primary failure) has already been determined.
                    pass
        finally:
            try:
                self._stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)


def run(args: argparse.Namespace) -> dict[str, Any]:
    env = os.environ.copy()
    env["DSH_CWD"] = str(args.cwd)
    env["DSH_SESSION_ROOT"] = str(args.session_root)
    args.session_root.mkdir(parents=True, exist_ok=True)
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.events_file.parent.mkdir(parents=True, exist_ok=True)
    args.runtime_stderr_file.parent.mkdir(parents=True, exist_ok=True)

    with args.runtime_stderr_file.open(
        "w", encoding="utf-8"
    ) as runtime_stderr, args.events_file.open(
        "w", encoding="utf-8"
    ) as event_stream:
        client = RuntimeClient(
            [str(args.runtime), str(args.config)],
            env=env,
            stderr=runtime_stderr,
            event_stream=event_stream,
            timeout_sec=args.timeout_sec,
        )
        try:
            initialize_params: dict[str, Any] = {
                "cwd": str(args.cwd),
                "provider": args.provider,
                "model": args.model,
            }
            if args.max_tokens is not None:
                initialize_params["maxTokens"] = args.max_tokens
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": initialize_params,
                }
            )
            client.wait_response(1)
            if args.initialize_only:
                return {
                    "schema_version": 1,
                    "status": "initialized",
                    "session_id": args.session_id,
                }

            instruction = args.instruction_file.read_text(encoding="utf-8")
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": args.session_id,
                        "contentBlocks": [{"type": "text", "text": instruction}],
                    },
                }
            )
            prompt_result, pending = client.wait_response(2)
            message_id = prompt_result.get("messageId")
            if not isinstance(message_id, str) or not message_id:
                raise ProtocolError("session/prompt returned no messageId")

            events: list[dict[str, Any]] = []
            received = False
            for message in pending:
                event = _event_from(message, args.session_id)
                if event is not None:
                    events.append(event)
                    received = received or _has_receipt(event, message_id)

            while True:
                message = client.receive()
                event = _event_from(message, args.session_id)
                if event is not None:
                    events.append(event)
                    received = received or _has_receipt(event, message_id)
                params = message.get("params")
                if (
                    received
                    and message.get("method") == "session.status"
                    and isinstance(params, dict)
                    and params.get("sessionId") == args.session_id
                    and params.get("status") == "idle"
                ):
                    break
            return summarize_events(
                events,
                args.session_id,
                max_turns=args.max_turns,
            )
        finally:
            client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--instruction-file", type=Path)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--events-file", type=Path, required=True)
    parser.add_argument("--runtime-stderr-file", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout-sec", type=float, default=86400)
    parser.add_argument("--initialize-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.initialize_only and args.instruction_file is None:
        raise SystemExit(
            "--instruction-file is required unless --initialize-only is set"
        )
    result = run(args)
    args.result_file.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
