#!/usr/bin/env python3
"""Normalize product-specific Terminal-Bench 2.1 trajectories."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


PRODUCTS = ("dsh", "hermes", "pi", "astra")
VERIFIER_ERRORS = {"VerifierInfrastructureError", "VerifierTimeoutError"}


class Redactor:
    PATTERNS = (
        (
            re.compile(
                r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?"
                r"-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
                re.DOTALL,
            ),
            "<REDACTED_PRIVATE_KEY>",
        ),
        (re.compile(r"hf_[A-Za-z0-9]{20,}"), "<REDACTED_HF_TOKEN>"),
        (re.compile(r"AKIA[0-9A-Z]{16}"), "<REDACTED_AWS_ACCESS_KEY>"),
        (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "<REDACTED_API_KEY>"),
        (
            re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
            "<REDACTED_SLACK_TOKEN>",
        ),
    )

    def __init__(self) -> None:
        self.count = 0

    def text(self, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        value = value.replace("/home/vagrant/moi-benchmark", "<WORKSPACE>")
        for pattern, replacement in self.PATTERNS:
            value, replacements = pattern.subn(replacement, value)
            self.count += replacements
        return value


def arguments() -> argparse.Namespace:
    output_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=output_root.parents[2] / "work" / "linux-terminal-bench",
    )
    parser.add_argument("--output", type=Path, default=output_root)
    parser.add_argument(
        "--products",
        nargs="+",
        choices=PRODUCTS,
        default=["dsh", "hermes", "pi"],
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_jsonl(path: Path) -> list[dict[str, Any]] | None:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    return None
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return rows


def find_named(root: Path, filename: str) -> list[Path]:
    found: list[Path] = []
    for directory, _, filenames in os.walk(root, onerror=lambda _: None):
        if filename in filenames:
            found.append(Path(directory) / filename)
    return sorted(found)


def relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def metadata(result: dict[str, Any] | None) -> dict[str, Any]:
    return (((result or {}).get("agent_result") or {}).get("metadata") or {})


def as_timestamp(value: Any) -> str | None:
    return str(value) if value is not None else None


def elapsed(start: Any, finish: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((finish_dt - start_dt).total_seconds(), 6)


def render_content(value: Any, redactor: Redactor) -> tuple[str, bool]:
    """Render public content and report whether private reasoning was omitted."""
    if value is None:
        return "", False
    if isinstance(value, str):
        return redactor.text(value), False
    if isinstance(value, (int, float, bool)):
        return redactor.text(str(value)), False
    if isinstance(value, list):
        output: list[str] = []
        internal_omitted = False
        for item in value:
            if isinstance(item, str):
                output.append(redactor.text(item))
                continue
            if not isinstance(item, dict):
                output.append(redactor.text(item))
                continue
            part_type = item.get("type")
            if part_type in {"reasoning", "thinking", "reasoning_content"}:
                internal_omitted = True
                continue
            if part_type in {"tool-call", "toolCall"}:
                continue
            if part_type == "text":
                output.append(redactor.text(item.get("text")))
                continue
            if part_type == "image":
                mime = item.get("mimeType") or item.get("mime_type") or "unknown"
                data = item.get("data")
                size = len(data) if isinstance(data, str) else 0
                output.append(f"<IMAGE mime_type={mime} encoded_chars={size}>")
                continue
            nested = item.get("content", item.get("text"))
            if nested is not None:
                text, omitted = render_content(nested, redactor)
                if text:
                    output.append(text)
                internal_omitted = internal_omitted or omitted
            else:
                output.append(redactor.text(item))
        return "\n".join(item for item in output if item), internal_omitted
    if isinstance(value, dict):
        if "content" in value:
            return render_content(value["content"], redactor)
        if "text" in value:
            return redactor.text(value["text"]), False
    return redactor.text(value), False


def render_arguments(value: Any, redactor: Redactor) -> str:
    if isinstance(value, str):
        return redactor.text(value)
    try:
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        value = str(value)
    return redactor.text(value)


def make_tool_call(
    call_id: Any, name: Any, call_arguments: Any, redactor: Redactor
) -> dict[str, Any]:
    return {
        "id": str(call_id) if call_id is not None else None,
        "name": str(name) if name is not None else None,
        "arguments_json": render_arguments(call_arguments, redactor),
    }


def make_message(
    role: str,
    content: str,
    *,
    timestamp: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: Any = None,
    tool_name: Any = None,
    is_error: Any = None,
    internal_omitted: bool = False,
) -> dict[str, Any]:
    return {
        "seq": 0,
        "role": role,
        "content": content,
        "timestamp": as_timestamp(timestamp),
        "tool_calls": tool_calls or [],
        "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
        "tool_name": str(tool_name) if tool_name is not None else None,
        "is_error": is_error if isinstance(is_error, bool) else None,
        "internal_content_omitted": internal_omitted,
    }


def sequence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, item in enumerate(messages):
        item["seq"] = index
    return messages


def paired(call_ids: list[str | None], result_ids: list[str | None]) -> bool:
    return Counter(item for item in call_ids if item) == Counter(
        item for item in result_ids if item
    )


def parsed_trace(
    messages: list[dict[str, Any]],
    trace_path: Path | None,
    trace_format: str,
    source_root: Path,
    *,
    found: bool,
    parseable: bool,
    terminal: bool,
    capture_complete: bool,
    tool_calls_paired: bool,
    reported_tool_calls: int | None,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "messages": sequence(messages),
        "trace_path": relative(trace_path, source_root),
        "trace_format": trace_format,
        "found": found,
        "parseable": parseable,
        "terminal": terminal,
        "capture_complete": capture_complete,
        "tool_calls_paired": tool_calls_paired,
        "reported_tool_calls": reported_tool_calls,
        "reasons": reasons,
    }


def missing_trace(
    trace_path: Path | None, trace_format: str, source_root: Path
) -> dict[str, Any]:
    return parsed_trace(
        [],
        trace_path,
        trace_format,
        source_root,
        found=trace_path is not None,
        parseable=False,
        terminal=False,
        capture_complete=False,
        tool_calls_paired=False,
        reported_tool_calls=None,
        reasons=["missing_or_unreadable_session"],
    )


def parse_dsh(
    trial: Path,
    result: dict[str, Any] | None,
    source_root: Path,
    redactor: Redactor,
) -> dict[str, Any]:
    paths = find_named(trial / "agent", "session.jsonl")
    path = paths[0] if paths else None
    rows = load_jsonl(path) if path else None
    if rows is None:
        return missing_trace(path, "dsh_session_jsonl", source_root)

    messages: list[dict[str, Any]] = []
    call_ids: list[str | None] = []
    result_ids: list[str | None] = []
    call_names: dict[str, str | None] = {}
    for row in rows:
        event_type = row.get("type")
        data = row.get("data") or {}
        at = row.get("time")
        if event_type == "user/message":
            text, omitted = render_content(data.get("content"), redactor)
            messages.append(
                make_message("user", text, timestamp=at, internal_omitted=omitted)
            )
        elif event_type == "assistant/message":
            raw_message = data.get("message") or {}
            raw_content = raw_message.get("content") or []
            text, omitted = render_content(raw_content, redactor)
            calls = [
                make_tool_call(
                    part.get("id"),
                    part.get("name"),
                    part.get("arguments"),
                    redactor,
                )
                for part in raw_content
                if isinstance(part, dict) and part.get("type") == "tool-call"
            ]
            messages.append(
                make_message(
                    "assistant",
                    text,
                    timestamp=at,
                    tool_calls=calls,
                    internal_omitted=omitted,
                )
            )
        elif event_type == "tool/call":
            call_id = data.get("callId")
            call_ids.append(str(call_id) if call_id is not None else None)
            if call_id is not None:
                call_names[str(call_id)] = data.get("name")
        elif event_type == "tool/result":
            raw_message = data.get("message") or {}
            for part in raw_message.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "tool-result":
                    continue
                text, omitted = render_content(part.get("content"), redactor)
                call_id = part.get("toolCallId")
                result_ids.append(str(call_id) if call_id is not None else None)
                messages.append(
                    make_message(
                        "tool",
                        text,
                        timestamp=at,
                        tool_call_id=call_id,
                        tool_name=call_names.get(str(call_id)),
                        is_error=part.get("isError"),
                        internal_omitted=omitted,
                    )
                )

    md = metadata(result)
    terminal = any(row.get("type") == "turn/end" for row in rows)
    capture_complete = md.get("dsh_trajectory_status") == "saved"
    calls_paired = paired(call_ids, result_ids)
    reasons = []
    if not capture_complete:
        reasons.append("capture_not_saved")
    if not terminal:
        reasons.append("missing_terminal")
    if not calls_paired:
        reasons.append("unpaired_tool_call")
    return parsed_trace(
        messages,
        path,
        "dsh_session_jsonl",
        source_root,
        found=True,
        parseable=True,
        terminal=terminal,
        capture_complete=capture_complete,
        tool_calls_paired=calls_paired,
        reported_tool_calls=len(call_ids),
        reasons=reasons,
    )


def parse_hermes(
    trial: Path,
    result: dict[str, Any] | None,
    source_root: Path,
    redactor: Redactor,
) -> dict[str, Any]:
    path = trial / "agent" / "hermes-session.jsonl"
    session = load_json(path) if path.is_file() else None
    if session is None:
        return missing_trace(path if path.is_file() else None, "hermes_session_jsonl", source_root)

    messages: list[dict[str, Any]] = []
    call_ids: list[str | None] = []
    result_ids: list[str | None] = []
    for raw in session.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        text, content_omitted = render_content(raw.get("content"), redactor)
        internal_omitted = content_omitted or bool(
            raw.get("reasoning")
            or raw.get("reasoning_content")
            or raw.get("reasoning_details")
        )
        calls = []
        for raw_call in raw.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") or {}
            call_id = raw_call.get("id")
            call_ids.append(str(call_id) if call_id is not None else None)
            calls.append(
                make_tool_call(
                    call_id,
                    function.get("name") or raw_call.get("name"),
                    function.get("arguments", raw_call.get("arguments")),
                    redactor,
                )
            )
        result_id = raw.get("tool_call_id") if role == "tool" else None
        if role == "tool":
            result_ids.append(str(result_id) if result_id is not None else None)
        messages.append(
            make_message(
                role,
                text,
                timestamp=raw.get("timestamp"),
                tool_calls=calls,
                tool_call_id=result_id,
                tool_name=raw.get("tool_name"),
                internal_omitted=internal_omitted,
            )
        )

    md = metadata(result)
    terminal = md.get("trajectory_terminal_event_count") == 1
    capture_complete = (
        md.get("trajectory_capture_status") == "saved"
        and md.get("trajectory_session_export_status") == "saved"
    )
    calls_paired = paired(call_ids, result_ids)
    reasons = []
    if not capture_complete:
        reasons.append("capture_not_saved")
    if not terminal:
        reasons.append("missing_terminal")
    if not calls_paired:
        reasons.append("unpaired_tool_call")
    return parsed_trace(
        messages,
        path,
        "hermes_session_jsonl",
        source_root,
        found=True,
        parseable=True,
        terminal=terminal,
        capture_complete=capture_complete,
        tool_calls_paired=calls_paired,
        reported_tool_calls=len(call_ids),
        reasons=reasons,
    )


def parse_pi(
    trial: Path,
    result: dict[str, Any] | None,
    source_root: Path,
    redactor: Redactor,
) -> dict[str, Any]:
    session_dir = trial / "agent" / "pi-sessions"
    paths = sorted(session_dir.glob("*.jsonl")) if session_dir.is_dir() else []
    path = paths[0] if paths else None
    rows = load_jsonl(path) if path else None
    if rows is None:
        return missing_trace(path, "pi_session_jsonl", source_root)

    messages: list[dict[str, Any]] = []
    call_ids: list[str | None] = []
    result_ids: list[str | None] = []
    stop_reasons: list[str | None] = []
    for row in rows:
        if row.get("type") != "message":
            continue
        raw = row.get("message") or {}
        role = raw.get("role")
        if role not in {"user", "assistant", "toolResult"}:
            continue
        text, omitted = render_content(raw.get("content"), redactor)
        at = raw.get("timestamp", row.get("timestamp"))
        if role == "assistant":
            calls = []
            for part in raw.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "toolCall":
                    continue
                call_id = part.get("id")
                call_ids.append(str(call_id) if call_id is not None else None)
                calls.append(
                    make_tool_call(
                        call_id,
                        part.get("name"),
                        part.get("arguments"),
                        redactor,
                    )
                )
            stop_reasons.append(raw.get("stopReason"))
            messages.append(
                make_message(
                    "assistant",
                    text,
                    timestamp=at,
                    tool_calls=calls,
                    internal_omitted=omitted,
                )
            )
        elif role == "toolResult":
            call_id = raw.get("toolCallId")
            result_ids.append(str(call_id) if call_id is not None else None)
            messages.append(
                make_message(
                    "tool",
                    text,
                    timestamp=at,
                    tool_call_id=call_id,
                    tool_name=raw.get("toolName"),
                    is_error=raw.get("isError"),
                    internal_omitted=omitted,
                )
            )
        else:
            messages.append(
                make_message("user", text, timestamp=at, internal_omitted=omitted)
            )

    md = metadata(result)
    terminal = bool(stop_reasons) and stop_reasons[-1] in {"stop", "length", "error"}
    capture_complete = md.get("pi_trajectory_status") == "saved"
    calls_paired = paired(call_ids, result_ids)
    reasons = []
    if not capture_complete:
        reasons.append("capture_not_saved")
    if not terminal:
        reasons.append("missing_terminal")
    if not calls_paired:
        reasons.append("unpaired_tool_call")
    return parsed_trace(
        messages,
        path,
        "pi_session_jsonl",
        source_root,
        found=True,
        parseable=True,
        terminal=terminal,
        capture_complete=capture_complete,
        tool_calls_paired=calls_paired,
        reported_tool_calls=len(call_ids),
        reasons=reasons,
    )


def astra_messages(
    trial: Path, redactor: Redactor
) -> tuple[list[dict[str, Any]], Path | None]:
    candidates: list[tuple[list[dict[str, Any]], Path]] = []
    for path in find_named(trial / "agent", "conversation_log.jsonl"):
        rows = load_jsonl(path)
        if rows is None:
            continue
        for row in reversed(rows):
            raw_messages = row.get("messages")
            if not isinstance(raw_messages, list):
                continue
            output = []
            for raw in raw_messages:
                if not isinstance(raw, dict):
                    continue
                role = raw.get("role")
                if role not in {"user", "assistant", "tool"}:
                    continue
                text, omitted = render_content(raw.get("content"), redactor)
                calls = []
                for raw_call in raw.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function") or {}
                    calls.append(
                        make_tool_call(
                            raw_call.get("id"),
                            function.get("name") or raw_call.get("name"),
                            function.get("arguments", raw_call.get("arguments")),
                            redactor,
                        )
                    )
                output.append(
                    make_message(
                        role,
                        text,
                        timestamp=raw.get("timestamp"),
                        tool_calls=calls,
                        tool_call_id=raw.get("tool_call_id"),
                        tool_name=raw.get("tool_name"),
                        internal_omitted=omitted,
                    )
                )
            if output:
                candidates.append((output, path))
            break
    if candidates:
        output, path = max(candidates, key=lambda item: len(item[0]))
        return sequence(output), path

    wrapper = trial / "agent" / "session.jsonl"
    rows = load_jsonl(wrapper) if wrapper.is_file() else None
    if rows:
        output = []
        for row in rows:
            payload = row.get("payload") if row.get("type") == "native_event" else row
            if not isinstance(payload, dict) or payload.get("type") != "turn":
                continue
            user_text, user_omitted = render_content(payload.get("user_input"), redactor)
            answer_text, answer_omitted = render_content(
                payload.get("assistant_output"), redactor
            )
            if user_text:
                output.append(
                    make_message(
                        "user",
                        user_text,
                        timestamp=payload.get("ts"),
                        internal_omitted=user_omitted,
                    )
                )
            if answer_text:
                output.append(
                    make_message(
                        "assistant",
                        answer_text,
                        timestamp=payload.get("ts"),
                        internal_omitted=answer_omitted,
                    )
                )
        if output:
            return sequence(output), wrapper
    return [], None


def parse_astra(
    trial: Path,
    result: dict[str, Any] | None,
    source_root: Path,
    redactor: Redactor,
) -> dict[str, Any]:
    messages, path = astra_messages(trial, redactor)
    md = metadata(result)
    capture_complete = md.get("astra_trajectory_status") == "complete"
    wrapper = trial / "agent" / "session.jsonl"
    wrapper_rows = load_jsonl(wrapper) if wrapper.is_file() else None
    terminal = bool(
        wrapper_rows
        and any(row.get("type") == "session_end" for row in wrapper_rows)
    )
    reported = md.get("tool_calls_count")
    reported = reported if isinstance(reported, int) else None
    call_ids = [
        call.get("id")
        for item in messages
        for call in item.get("tool_calls") or []
    ]
    result_ids = [
        item.get("tool_call_id") for item in messages if item.get("role") == "tool"
    ]
    calls_paired = paired(call_ids, result_ids)
    if reported and not call_ids:
        calls_paired = False
    reasons = []
    if not capture_complete:
        reasons.append("capture_not_complete")
    if not terminal:
        reasons.append("missing_terminal")
    if not calls_paired:
        reasons.append("unpaired_or_missing_tool_details")
    return parsed_trace(
        messages,
        path,
        "astra_conversation_log",
        source_root,
        found=path is not None,
        parseable=path is not None,
        terminal=terminal,
        capture_complete=capture_complete,
        tool_calls_paired=calls_paired,
        reported_tool_calls=reported,
        reasons=reasons,
    )


PARSERS = {
    "dsh": parse_dsh,
    "hermes": parse_hermes,
    "pi": parse_pi,
    "astra": parse_astra,
}


def verifier(result: dict[str, Any] | None) -> tuple[bool, int | None, str | None]:
    reward = ((((result or {}).get("verifier_result") or {}).get("rewards") or {}).get("reward"))
    finished = (((result or {}).get("verifier") or {}).get("finished_at"))
    exception_type = (((result or {}).get("exception_info") or {}).get("exception_type"))
    valid = (
        isinstance(reward, (int, float))
        and not isinstance(reward, bool)
        and reward in (0, 1)
        and bool(finished)
        and exception_type not in VERIFIER_ERRORS
    )
    return valid, int(reward) if valid else None, exception_type


def task_id(trial: Path, result: dict[str, Any] | None) -> str:
    task_name = (result or {}).get("task_name")
    if isinstance(task_name, str) and task_name:
        return task_name.rsplit("/", 1)[-1]
    return trial.name.rsplit("__", 1)[0]


def verifier_counts(trial: Path) -> dict[str, int | None]:
    report = load_json(trial / "verifier" / "ctrf.json") or {}
    results = report.get("results")
    summary = results.get("summary") if isinstance(results, dict) else None
    return {
        f"verifier_{status}": (
            summary[status]
            if isinstance(summary, dict)
            and type(summary.get(status)) is int
            and summary[status] >= 0
            else None
        )
        for status in ("passed", "failed")
    }


def trials(product_root: Path) -> list[Path]:
    jobs = product_root / "jobs"
    if not jobs.is_dir():
        return []
    return sorted(
        trial
        for job in jobs.iterdir()
        if job.is_dir()
        for trial in job.iterdir()
        if trial.is_dir()
    )


def dataset_revision(source_root: Path) -> str | None:
    root = source_root.parent / "terminal-bench-2-1"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def build_record(
    product: str,
    trial: Path,
    result: dict[str, Any],
    config: dict[str, Any] | None,
    trace: dict[str, Any],
    redactor: Redactor,
    attempt_index: int,
    revision: str | None,
    source_root: Path,
) -> dict[str, Any]:
    md = metadata(result)
    verifier_valid, reward, exception_type = verifier(result)
    reasons = list(trace["reasons"])
    if not verifier_valid:
        reasons.append("invalid_verifier")
    reasons = sorted(set(reasons))
    tier = (
        "complete"
        if trace["capture_complete"]
        and trace["terminal"]
        and trace["tool_calls_paired"]
        and verifier_valid
        else "partial"
    )

    instruction_path = trial / "agent" / "instruction.md"
    try:
        instruction = redactor.text(instruction_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        instruction = ""

    agent_info = result.get("agent_info") or {}
    model_info = agent_info.get("model_info") or {}
    agent_config = (config or {}).get("agent") or {}
    kwargs = agent_config.get("kwargs") or {}
    tool_names = sorted(
        {
            call["name"]
            for item in trace["messages"]
            for call in item["tool_calls"]
            if call.get("name")
        }
    )
    if product == "astra" and not tool_names:
        tool_names = sorted(md.get("tools_used") or [])
    max_turns = kwargs.get("max_turns")
    if max_turns is None and product == "dsh":
        max_turns = md.get("dsh_max_turns")

    started = result.get("started_at")
    finished = result.get("finished_at")
    agent_execution = result.get("agent_execution") or {}
    verifier_execution = result.get("verifier") or {}
    raw_reward = (((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"))
    product_reasoning = md.get(f"{product}_reasoning_effort")
    product_temperature = md.get(f"{product}_temperature")

    return {
        "record_id": f"{product}/{trial.name}",
        "benchmark": {
            "name": "terminal-bench",
            "version": "2.1",
            "dataset_revision": revision,
            "task_id": task_id(trial, result),
            "task_checksum": result.get("task_checksum"),
            "instruction_sha256": md.get("instruction_sha256"),
        },
        "trial": {
            "id": trial.name,
            "attempt_index": attempt_index,
            "started_at": started,
            "finished_at": finished,
        },
        "agent": {
            "product": product,
            "name": agent_info.get("name"),
            "version": agent_info.get("version"),
            "model_provider": model_info.get("provider"),
            "model_name": model_info.get("name") or agent_config.get("model_name"),
            "condition": md.get("condition"),
            "reasoning_effort": product_reasoning,
            "temperature": product_temperature,
            "max_turns": max_turns,
            "tools": tool_names,
            "evaluation_status": md.get("evaluation_status"),
            "formal_score_eligible": md.get("formal_score_eligible"),
        },
        "instruction": instruction,
        "messages": trace["messages"],
        "outcome": {
            "reward": reward,
            "raw_structured_reward": raw_reward,
            "verifier_valid": verifier_valid,
            **verifier_counts(trial),
            "product_status": md.get("product_terminal_status"),
            "exception_type": exception_type,
        },
        "usage": {
            "input_tokens": ((result.get("agent_result") or {}).get("n_input_tokens")),
            "cache_tokens": ((result.get("agent_result") or {}).get("n_cache_tokens")),
            "output_tokens": ((result.get("agent_result") or {}).get("n_output_tokens")),
            "cost_usd": ((result.get("agent_result") or {}).get("cost_usd")),
            "tool_call_count": sum(len(item["tool_calls"]) for item in trace["messages"]),
            "agent_reported_tool_call_count": trace["reported_tool_calls"],
        },
        "timing": {
            "total_seconds": elapsed(started, finished),
            "agent_execution_seconds": elapsed(
                agent_execution.get("started_at"), agent_execution.get("finished_at")
            ),
            "verifier_seconds": elapsed(
                verifier_execution.get("started_at"), verifier_execution.get("finished_at")
            ),
        },
        "quality": {
            "tier": tier,
            "reasons": reasons,
            "trace_parseable": trace["parseable"],
            "has_user": any(item["role"] == "user" for item in trace["messages"]),
            "has_assistant": any(
                item["role"] == "assistant" for item in trace["messages"]
            ),
            "terminal_evidence": trace["terminal"],
            "capture_complete": trace["capture_complete"],
            "tool_calls_paired": trace["tool_calls_paired"],
            "verifier_valid": verifier_valid,
            "message_count": len(trace["messages"]),
            "redaction_count": redactor.count,
        },
        "source": {
            "trial_path": relative(trial, source_root),
            "trace_path": trace["trace_path"],
            "trace_format": trace["trace_format"],
        },
    }


def clean_product(
    product: str, source_root: Path, revision: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    product_trials = trials(source_root / product)
    staged = []
    attempts: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for trial in product_trials:
        result = load_json(trial / "result.json")
        redactor = Redactor()
        trace = PARSERS[product](trial, result, source_root, redactor)
        staged.append((trial, result, trace, redactor))
        attempts[task_id(trial, result)].append(
            (str((result or {}).get("started_at") or ""), trial.name)
        )

    indexes = {}
    for task_name, task_attempts in attempts.items():
        for index, (_, trial_name) in enumerate(sorted(task_attempts), start=1):
            indexes[(task_name, trial_name)] = index

    records = []
    counts: Counter[str] = Counter()
    partial_reasons: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    for trial, result, trace, redactor in staged:
        roles = Counter(item["role"] for item in trace["messages"])
        meaningful = trace["parseable"] and roles["user"] > 0 and roles["assistant"] > 0
        result_finished = bool(result and result.get("finished_at"))
        if not meaningful or not result_finished:
            counts["excluded_metadata_only"] += 1
            if result is None:
                excluded_reasons["missing_result"] += 1
            elif not result.get("finished_at"):
                excluded_reasons["unfinished_result"] += 1
            if not trace["found"]:
                excluded_reasons["missing_or_unreadable_session"] += 1
            elif not trace["parseable"]:
                excluded_reasons["parse_error"] += 1
            if roles["user"] == 0:
                excluded_reasons["missing_user"] += 1
            if roles["assistant"] == 0:
                excluded_reasons["missing_assistant"] += 1
            continue

        config = load_json(trial / "config.json")
        task_name = task_id(trial, result)
        record = build_record(
            product,
            trial,
            result,
            config,
            trace,
            redactor,
            indexes[(task_name, trial.name)],
            revision,
            source_root,
        )
        tier = record["quality"]["tier"]
        counts[tier] += 1
        if tier == "partial":
            partial_reasons.update(record["quality"]["reasons"])
        records.append(record)

    records.sort(
        key=lambda item: (
            item["benchmark"]["task_id"],
            item["trial"]["attempt_index"],
            item["trial"]["id"],
        )
    )
    report = {
        "discovered_trials": len(product_trials),
        "complete": counts["complete"],
        "partial": counts["partial"],
        "retained": counts["complete"] + counts["partial"],
        "excluded_metadata_only": counts["excluded_metadata_only"],
        "partial_reason_counts_nonexclusive": dict(sorted(partial_reasons.items())),
        "excluded_reason_counts_nonexclusive": dict(sorted(excluded_reasons.items())),
        "messages": sum(len(record["messages"]) for record in records),
        "tool_calls": sum(record["usage"]["tool_call_count"] for record in records),
        "redactions": sum(record["quality"]["redaction_count"] for record in records),
    }
    return records, report


def validate(records: list[dict[str, Any]]) -> None:
    seen = set()
    for record in records:
        record_id = record["record_id"]
        if record_id in seen:
            raise ValueError(f"duplicate record_id: {record_id}")
        seen.add(record_id)
        quality = record["quality"]
        if quality["tier"] not in {"complete", "partial"}:
            raise ValueError(f"invalid tier: {record_id}")
        roles = {item["role"] for item in record["messages"]}
        if not roles <= {"user", "assistant", "tool"}:
            raise ValueError(f"invalid role: {record_id}")
        if not {"user", "assistant"} <= roles:
            raise ValueError(f"missing required messages: {record_id}")
        if quality["tier"] == "complete" and not all(
            (
                quality["trace_parseable"],
                quality["terminal_evidence"],
                quality["capture_complete"],
                quality["tool_calls_paired"],
                quality["verifier_valid"] or quality.get("verifier_independent_tier", False),
            )
        ):
            raise ValueError(f"complete quality mismatch: {record_id}")
        if any(item["seq"] != index for index, item in enumerate(record["messages"])):
            raise ValueError(f"message sequence mismatch: {record_id}")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def main() -> int:
    args = arguments()
    source_root = args.source.resolve()
    output_root = args.output.resolve()
    revision = dataset_revision(source_root)
    all_records = []
    product_reports = {}
    for product in args.products:
        records, report = clean_product(product, source_root, revision)
        validate(records)
        all_records.extend(records)
        product_reports[product] = report
    validate(all_records)

    totals = {
        "discovered_trials": sum(x["discovered_trials"] for x in product_reports.values()),
        "complete": sum(x["complete"] for x in product_reports.values()),
        "partial": sum(x["partial"] for x in product_reports.values()),
        "retained": sum(x["retained"] for x in product_reports.values()),
        "excluded_metadata_only": sum(
            x["excluded_metadata_only"] for x in product_reports.values()
        ),
        "messages": sum(x["messages"] for x in product_reports.values()),
        "tool_calls": sum(x["tool_calls"] for x in product_reports.values()),
        "redactions": sum(x["redactions"] for x in product_reports.values()),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": f"<WORKSPACE>/work/{source_root.name}",
        "dataset_revision": revision,
        "products": product_reports,
        "totals": totals,
        "rules": {
            "retained_tiers": ["complete", "partial"],
            "complete": "meaningful trace + successful capture + terminal evidence + paired tools + valid verifier",
            "partial": "finished trial with user and assistant messages that does not satisfy every complete requirement",
            "valid_verifier": "structured reward 0/1 + verifier finished + no verifier timeout/infrastructure exception",
            "metadata_only": "reported but not written to the dataset",
            "internal_reasoning": "omitted",
        },
    }
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    for product in args.products:
        product_records = [x for x in all_records if x["agent"]["product"] == product]
        for tier in ("complete", "partial"):
            write_jsonl(
                output_root / "data" / tier / f"{product}.jsonl",
                [x for x in product_records if x["quality"]["tier"] == tier],
            )
    (output_root / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
