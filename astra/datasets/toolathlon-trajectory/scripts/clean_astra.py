#!/usr/bin/env python3
"""Clean the selected Astra Toolathlon attempts into a public trajectory dataset."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = "toolathlon-trajectory/v1"
VALID_RESULTS = {"pass", "no_pass"}


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
        (re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s\"']+"), "Authorization: Bearer <REDACTED>"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<REDACTED_JWT>"),
        (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "<REDACTED_HF_TOKEN>"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED_AWS_ACCESS_KEY>"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "<REDACTED_API_KEY>"),
        (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<REDACTED_SLACK_TOKEN>"),
        (
            re.compile(
                r"(?i)\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))"
                r"\s*([:=])\s*([^\s,;\"']+)"
            ),
            r"\1\2<REDACTED>",
        ),
        (re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+@"), r"\1<REDACTED>@"),
        (re.compile(r"data:image/[^;,]+;base64,[A-Za-z0-9+/=]+"), "<REDACTED_IMAGE_DATA_URL>"),
        (re.compile(r"/home/[^/\s]+"), "<HOME>"),
        (re.compile(r"/Users/[^/\s]+"), "<HOME>"),
    )

    def __init__(self, workspace: Path | None = None) -> None:
        self.count = 0
        self.workspace = str(workspace) if workspace is not None else None

    def text(self, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if self.workspace:
            value, count = value.replace(self.workspace, "<WORKSPACE>"), value.count(self.workspace)
            self.count += count
        for pattern, replacement in self.PATTERNS:
            value, count = pattern.subn(replacement, value)
            self.count += count
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def arguments() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[4]
    dataset_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=repo_root
        / "astra/reports/Toolathlon-analysis/astra-969550b-toolathlon-108-task-results.csv",
        help="Selected one-row-per-task result projection.",
    )
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--output", type=Path, default=dataset_root)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    errors = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
                    continue
                if not isinstance(value, dict):
                    errors += 1
                    continue
                rows.append(value)
    except (OSError, UnicodeDecodeError):
        return [], 1
    return rows, errors


def unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def number(value: Any) -> int | float | None:
    value = unwrap(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def public_path(path: Path, repo_root: Path, redactor: Redactor) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return redactor.text(str(path.resolve()))


def render(value: Any, redactor: Redactor) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return redactor.text(value)
        return json.dumps(redactor.value(parsed), ensure_ascii=False, sort_keys=True)
    return json.dumps(redactor.value(value), ensure_ascii=False, sort_keys=True)


def evaluator_candidates(root: Path, run: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    evaluator = run.get("evaluator") if isinstance(run.get("evaluator"), dict) else {}
    result_file = evaluator.get("result_file")
    if isinstance(result_file, str):
        paths.append(root / result_file)
    paths.append(root / "evaluator/eval_res.json")
    paths.extend(sorted(root.glob("evaluator-rerun-*/eval_res.json"), reverse=True))
    paths.append(root / "task-state/eval_res.json")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


def select_evaluator(
    root: Path,
    run: dict[str, Any],
    selection: dict[str, str],
) -> tuple[dict[str, Any] | None, Path | None, list[str]]:
    expected = selection.get("status")
    evaluator = run.get("evaluator") if isinstance(run.get("evaluator"), dict) else {}
    run_status = evaluator.get("verify_status") or run.get("verify_status")
    run_error = unwrap(evaluator.get("error", run.get("evaluator_error")))
    reasons: list[str] = []
    valid: list[tuple[int, Path, dict[str, Any]]] = []
    for path in evaluator_candidates(root, run):
        result = load_json(path)
        if result is None or not isinstance(result.get("pass"), bool):
            continue
        result_status = result.get("status") or run_status or expected
        if result_status not in VALID_RESULTS:
            continue
        passed = result["pass"]
        if (result_status == "pass") != passed:
            continue
        if expected in VALID_RESULTS and (expected == "pass") != passed:
            continue
        if run_error:
            continue
        category = selection.get("failure_category", "").strip().lower()
        if category.startswith("evaluator_") and category not in {
            "evaluator_no_pass",
            "evaluator_output_mismatch",
        }:
            continue
        score = 0
        if isinstance(evaluator.get("result_file"), str) and path == root / evaluator["result_file"]:
            score += 100
        if result.get("evaluation_kind"):
            score += 30
        if result.get("status") in VALID_RESULTS:
            score += 20
        if result.get("finished_at") or result.get("created_at"):
            score += 10
        if path.parent.name.startswith("evaluator-rerun-"):
            score += 5
        if path == root / "evaluator/eval_res.json":
            score += 3
        if path == root / "task-state/eval_res.json":
            score -= 10
        valid.append((score, path, result))
    if valid:
        _, path, result = max(valid, key=lambda item: (item[0], str(item[1])))
        return result, path, []
    if run_error:
        reasons.append("evaluator_error")
    if expected not in VALID_RESULTS:
        reasons.append("invalid_selected_status")
    if not reasons:
        reasons.append("missing_or_invalid_evaluator")
    return None, None, reasons


def make_message(
    role: str,
    content: str,
    *,
    timestamp: Any = None,
    reasoning_content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: Any = None,
    tool_name: Any = None,
    is_error: Any = None,
    segment_index: int | None = None,
) -> dict[str, Any]:
    return {
        "seq": 0,
        "role": role,
        "content": content,
        "reasoning_content": reasoning_content or None,
        "timestamp": str(timestamp) if timestamp is not None else None,
        "tool_calls": tool_calls or [],
        "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
        "tool_name": str(tool_name) if tool_name is not None else None,
        "is_error": is_error if isinstance(is_error, bool) else None,
        "segment_index": segment_index,
        "internal_content_omitted": False,
    }


def continuation_is_complete(rows: list[dict[str, Any]]) -> bool:
    sessions = [index for index, row in enumerate(rows) if row.get("event") == "session_info"]
    interruptions = [index for index, row in enumerate(rows) if row.get("event") == "run_interrupted"]
    continuations = [index for index, row in enumerate(rows) if row.get("event") == "transport_continuation"]
    if not sessions:
        return False
    if len(sessions) != len(interruptions) + 1 or len(continuations) != len(interruptions):
        return False
    for interruption, continuation, next_session in zip(interruptions, continuations, sessions[1:]):
        if not interruption < continuation < next_session:
            return False
    return True


def parse_trajectory(
    path: Path,
    instruction: str,
    started_at: Any,
    run: dict[str, Any],
    redactor: Redactor,
) -> dict[str, Any]:
    rows, parse_errors = load_jsonl(path)
    event_counts = Counter(row.get("event", "<missing>") for row in rows)
    sequences = [row.get("sequence") for row in rows]
    sequence_contiguous = bool(sequences) and all(
        isinstance(value, int) for value in sequences
    ) and all(right == left + 1 for left, right in zip(sequences, sequences[1:]))

    messages = [make_message("user", redactor.text(instruction), timestamp=started_at, segment_index=0)]
    pending_reasoning: list[str] = []
    pending_text: list[str] = []
    call_ids: list[str | None] = []
    result_ids: list[str | None] = []
    emitted_results: set[str] = set()
    tool_names: dict[str, str | None] = {}
    segments: list[dict[str, Any]] = []
    segment_index = 0

    def flush_assistant(timestamp: Any, calls: list[dict[str, Any]] | None = None) -> None:
        nonlocal pending_reasoning, pending_text
        content = redactor.text("".join(pending_text))
        reasoning = redactor.text("".join(pending_reasoning))
        if content or reasoning or calls:
            messages.append(
                make_message(
                    "assistant",
                    content,
                    timestamp=timestamp,
                    reasoning_content=reasoning,
                    tool_calls=calls,
                    segment_index=segment_index,
                )
            )
        pending_reasoning = []
        pending_text = []

    for row in rows:
        event = row.get("event")
        native = row.get("native") if isinstance(row.get("native"), dict) else {}
        timestamp = row.get("timestamp")
        if event == "session_info":
            if segments:
                flush_assistant(timestamp)
            segment_index = len(segments) + 1
            segments.append(
                {
                    "segment_index": segment_index,
                    "session_id": native.get("session_id"),
                    "run_id": native.get("run_id"),
                    "start_sequence": row.get("sequence"),
                    "finished_status": None,
                    "finish_sequence": None,
                }
            )
        elif event == "reasoning_delta":
            pending_reasoning.append(str(native.get("content") or ""))
        elif event == "text_delta":
            pending_text.append(str(native.get("content") or ""))
        elif event == "tool_call":
            raw_call = native.get("tool_call") if isinstance(native.get("tool_call"), dict) else {}
            function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            call_id = raw_call.get("id")
            name = function.get("name")
            call_key = str(call_id) if call_id is not None else None
            call_ids.append(call_key)
            if call_key is not None:
                tool_names[call_key] = str(name) if name is not None else None
            call = {
                "id": call_key,
                "name": str(name) if name is not None else None,
                "arguments_json": render(function.get("arguments"), redactor),
            }
            flush_assistant(timestamp, [call])
        elif event == "tool_call_end":
            call_id = native.get("call_id")
            call_key = str(call_id) if call_id is not None else None
            result_ids.append(call_key)
            if call_key is not None and call_key in emitted_results:
                continue
            if call_key is not None:
                emitted_results.add(call_key)
            success = native.get("success")
            messages.append(
                make_message(
                    "tool",
                    render(native.get("result"), redactor),
                    timestamp=timestamp,
                    tool_call_id=call_key,
                    tool_name=native.get("tool") or tool_names.get(call_key or ""),
                    is_error=not success if isinstance(success, bool) else None,
                    segment_index=segment_index,
                )
            )
        elif event == "text_done":
            full_text = native.get("full_text")
            if isinstance(full_text, str):
                pending_text = [full_text]
            flush_assistant(timestamp)
        elif event == "run_finished":
            native_run_id = native.get("run_id")
            for segment in reversed(segments):
                if native_run_id is None or segment["run_id"] == native_run_id:
                    segment["finished_status"] = native.get("status")
                    segment["finish_sequence"] = row.get("sequence")
                    break
    flush_assistant(rows[-1].get("timestamp") if rows else started_at)

    for index, message in enumerate(messages):
        message["seq"] = index
    call_counter = Counter(item for item in call_ids if item)
    result_counter = Counter(item for item in result_ids if item)
    tools_paired = call_counter == result_counter and None not in call_ids and None not in result_ids
    continuation_complete = continuation_is_complete(rows)
    last_segment = segments[-1] if segments else None
    terminal = bool(last_segment and last_segment.get("finish_sequence") is not None)
    meaningful = any(
        message["role"] == "assistant"
        and (message["content"] or message["reasoning_content"] or message["tool_calls"])
        for message in messages
    )
    reasons: list[str] = []
    if parse_errors:
        reasons.append("trajectory_parse_error")
    if not sequence_contiguous:
        reasons.append("sequence_gap")
    if not terminal:
        reasons.append("missing_terminal")
    if not continuation_complete:
        reasons.append("continuation_gap")
    if not tools_paired:
        reasons.append("unpaired_tool_call")

    declared = run.get("trajectory") if isinstance(run.get("trajectory"), dict) else {}
    observed_started = event_counts.get("tool_transport_started", 0)
    observed_terminal = event_counts.get("tool_transport_completed", 0) + event_counts.get(
        "tool_transport_failed", 0
    )
    declared_started = number(declared.get("tool_started_events"))
    declared_terminal = number(declared.get("tool_terminal_events"))
    tool_count_consistent = (
        (declared_started is None or declared_started == observed_started)
        and (declared_terminal is None or declared_terminal == observed_terminal)
    )
    if not tool_count_consistent:
        reasons.append("tool_count_mismatch")

    return {
        "rows": rows,
        "messages": messages,
        "segments": segments,
        "event_counts": dict(sorted(event_counts.items())),
        "parseable": parse_errors == 0,
        "parse_errors": parse_errors,
        "sequence_contiguous": sequence_contiguous,
        "terminal": terminal,
        "continuation_complete": continuation_complete,
        "tool_calls_paired": tools_paired,
        "tool_count_consistent": tool_count_consistent,
        "tool_call_count": len(call_ids),
        "tool_result_count": len(result_ids),
        "meaningful": meaningful,
        "reasons": reasons,
    }


def summarize_usage(path: Path) -> dict[str, Any]:
    rows, parse_errors = load_jsonl(path)
    started: set[str] = set()
    completed: dict[str, dict[str, Any]] = {}
    models: list[str] = []
    for row in rows:
        event = row.get("event")
        request_id = row.get("model_request_id")
        if event == "model_request.started" and isinstance(request_id, str):
            started.add(request_id)
            model = row.get("effective_model") or row.get("requested_model")
            if isinstance(model, str) and model not in models:
                models.append(model)
        elif event == "model_request.completed" and isinstance(request_id, str):
            completed[request_id] = row

    metric_names = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "cache_read_tokens": "cache_read_tokens",
        "cache_write_tokens": "cache_write_tokens",
    }
    totals: dict[str, int | None] = {}
    available_counts: dict[str, int] = {}
    for output_name, source_name in metric_names.items():
        values: list[int] = []
        for row in completed.values():
            token_usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
            value = number(token_usage.get(source_name))
            if value is not None:
                values.append(int(value))
        totals[output_name] = sum(values) if values else None
        available_counts[output_name] = len(values)

    provider_reported = 0
    for row in completed.values():
        token_usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
        if all(number(token_usage.get(name)) is not None for name in ("input_tokens", "output_tokens", "total_tokens")):
            provider_reported += 1
    if not completed:
        status = "unavailable"
    elif provider_reported == len(completed):
        status = "complete"
    else:
        status = "partial"
    return {
        **totals,
        "model_request_started": len(started),
        "model_request_completed": len(completed),
        "model_request_succeeded": sum(row.get("success") is True for row in completed.values()),
        "models": models,
        "coverage": {
            "status": status,
            "provider_reported_requests": provider_reported,
            "completed_requests": len(completed),
            "available_metric_counts": available_counts,
            "parse_errors": parse_errors,
        },
    }


def selected_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {"task", "status", "kind", "source", "run_id"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Selection CSV must contain: {', '.join(sorted(required))}")
    tasks = [row["task"] for row in rows]
    if len(tasks) != len(set(tasks)):
        raise ValueError("Selection CSV contains duplicate task names")
    return rows


def build_record(
    selection: dict[str, str],
    root: Path,
    run: dict[str, Any],
    bundle: dict[str, Any],
    evaluator_result: dict[str, Any],
    evaluator_path: Path,
    trace: dict[str, Any],
    usage: dict[str, Any],
    repo_root: Path,
    redactor: Redactor,
) -> dict[str, Any]:
    task = selection["task"]
    attempt_run_id = selection.get("run_id") or str(run.get("run_id") or root.name)
    record_id = f"astra/{task}/{attempt_run_id}"
    prompt = bundle.get("prompt") if isinstance(bundle.get("prompt"), dict) else {}
    tools = bundle.get("tools") if isinstance(bundle.get("tools"), dict) else {}
    evaluator = run.get("evaluator") if isinstance(run.get("evaluator"), dict) else {}
    evaluator_status = "pass" if evaluator_result["pass"] else "no_pass"
    terminal_status = run.get("terminal_status") or selection.get("terminal")
    reasons = list(trace["reasons"])
    tier = "complete" if not reasons else "partial"
    result_summary = {
        key: evaluator_result[key]
        for key in (
            "details",
            "failure",
            "checks",
            "evaluation_kind",
            "limitations",
        )
        if key in evaluator_result
    }
    return {
        "record_id": record_id,
        "schema_version": SCHEMA_VERSION,
        "agent": {
            "name": "astra",
            "commit": run.get("astra_commit"),
            "system_id": run.get("system_id"),
            "model": usage["models"][-1] if usage["models"] else None,
            "tools": sorted(
                set(tools.get("needed_local_tools") or [])
                | set(tools.get("needed_mcp_servers") or [])
            ),
        },
        "benchmark": {
            "name": "Toolathlon",
            "task_id": task,
            "selection_kind": selection.get("kind"),
        },
        "trial": {
            "attempt_run_id": attempt_run_id,
            "attempt_directory": root.name,
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "terminal_status": terminal_status,
            "termination_reason": run.get("termination_reason"),
            "segments": trace["segments"],
        },
        "instruction": redactor.text(prompt.get("task") or ""),
        "messages": trace["messages"],
        "outcome": {
            "status": evaluator_status,
            "evaluator_valid": True,
            "evaluator_pass": evaluator_result["pass"],
            "evaluation_kind": evaluator_result.get("evaluation_kind")
            or evaluator.get("evaluation_kind")
            or ("offline" if selection.get("kind") == "offline" else "original"),
            "result_summary": redactor.value(result_summary),
        },
        "usage": {
            **usage,
            "tool_call_count": trace["tool_call_count"],
            "tool_result_count": trace["tool_result_count"],
        },
        "timing": {
            "e2e_seconds": float_or_none(selection.get("e2e_seconds")),
            "agent_seconds": float_or_none(selection.get("agent_seconds")),
            "evaluator_seconds": float_or_none(selection.get("evaluator_seconds")),
            "orchestration_seconds": float_or_none(selection.get("orchestration_seconds")),
        },
        "quality": {
            "tier": tier,
            "reasons": reasons,
            "trajectory_parseable": trace["parseable"],
            "sequence_contiguous": trace["sequence_contiguous"],
            "terminal": trace["terminal"],
            "continuation_complete": trace["continuation_complete"],
            "tool_calls_paired": trace["tool_calls_paired"],
            "tool_count_consistent": trace["tool_count_consistent"],
            "reasoning_included": any(
                message.get("reasoning_content") for message in trace["messages"]
            ),
            "reasoning_event_count": trace["event_counts"].get("reasoning_delta", 0),
            "token_usage_affects_tier": False,
            "redaction_count": redactor.count,
        },
        "source": {
            "attempt_path": public_path(root, repo_root, redactor),
            "trajectory_path": public_path(root / "trajectory.jsonl", repo_root, redactor),
            "evaluator_path": public_path(evaluator_path, repo_root, redactor),
            "selection_kind": selection.get("kind"),
            "event_counts": trace["event_counts"],
        },
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def clean(
    results_csv: Path,
    output: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    selections = selected_rows(results_csv)
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    for selection in selections:
        root = Path(selection["source"])
        if not root.is_absolute():
            root = repo_root / root
        run = load_json(root / "run.json")
        bundle = load_json(root / "task-bundle.public.json")
        redactor = Redactor(repo_root)
        task = selection["task"]
        attempt_run_id = selection.get("run_id") or (run or {}).get("run_id") or root.name
        record_id = f"astra/{task}/{attempt_run_id}"
        exclusion_reasons: list[str] = []
        if run is None:
            exclusion_reasons.append("missing_or_invalid_run")
        if bundle is None:
            exclusion_reasons.append("missing_or_invalid_task_bundle")
        prompt = (bundle or {}).get("prompt") if isinstance((bundle or {}).get("prompt"), dict) else {}
        instruction = prompt.get("task") if isinstance(prompt, dict) else None
        if not isinstance(instruction, str) or not instruction.strip():
            exclusion_reasons.append("missing_instruction")
        evaluator_result, evaluator_path, evaluator_reasons = select_evaluator(
            root, run or {}, selection
        )
        exclusion_reasons.extend(evaluator_reasons)
        trace = parse_trajectory(
            root / "trajectory.jsonl",
            instruction or "",
            (run or {}).get("started_at"),
            run or {},
            redactor,
        )
        if not trace["parseable"]:
            exclusion_reasons.append("trajectory_not_parseable")
        if not trace["meaningful"]:
            exclusion_reasons.append("missing_meaningful_agent_activity")
        if record_id in record_ids:
            exclusion_reasons.append("duplicate_record_id")
        evaluator_public_path = (
            public_path(evaluator_path, repo_root, redactor) if evaluator_path is not None else None
        )
        manifest = {
            "record_id": record_id,
            "task": task,
            "attempt_run_id": attempt_run_id,
            "selection_kind": selection.get("kind"),
            "selected_status": selection.get("status"),
            "source": public_path(root, repo_root, redactor),
            "evaluator_source": evaluator_public_path,
            "included": not exclusion_reasons,
            "exclusion_reasons": sorted(set(exclusion_reasons)),
        }
        manifest_rows.append(manifest)
        if exclusion_reasons:
            excluded.append(manifest)
            continue
        usage = summarize_usage(root / "model-usage.jsonl")
        record = build_record(
            selection,
            root,
            run or {},
            bundle or {},
            evaluator_result or {},
            evaluator_path or root / "evaluator/eval_res.json",
            trace,
            usage,
            repo_root,
            redactor,
        )
        records.append(record)
        record_ids.add(record_id)

    records.sort(key=lambda item: (item["benchmark"]["task_id"], item["record_id"]))
    complete = [record for record in records if record["quality"]["tier"] == "complete"]
    partial = [record for record in records if record["quality"]["tier"] == "partial"]
    reason_counts = Counter(
        reason for record in partial for reason in record["quality"]["reasons"]
    )
    usage_counts = Counter(record["usage"]["coverage"]["status"] for record in records)
    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at": generated_at,
        "schema_version": SCHEMA_VERSION,
        "selection_rows": len(selections),
        "retained": len(records),
        "complete": len(complete),
        "partial": len(partial),
        "excluded": len(excluded),
        "evaluator_valid": len(records),
        "outcomes": dict(Counter(record["outcome"]["status"] for record in records)),
        "selection_kinds": dict(
            Counter(record["benchmark"]["selection_kind"] for record in records)
        ),
        "partial_reason_counts_nonexclusive": dict(sorted(reason_counts.items())),
        "token_usage_coverage": dict(sorted(usage_counts.items())),
        "messages": sum(len(record["messages"]) for record in records),
        "tool_calls": sum(record["usage"]["tool_call_count"] for record in records),
        "reasoning_events": sum(
            record["quality"]["reasoning_event_count"] for record in records
        ),
        "excluded_records": excluded,
    }
    manifest = {
        "generated_at": generated_at,
        "schema_version": SCHEMA_VERSION,
        "source": public_path(results_csv, repo_root, Redactor(repo_root)),
        "records": manifest_rows,
    }
    if not dry_run:
        atomic_jsonl(output / "data/complete/astra.jsonl", complete)
        atomic_jsonl(output / "data/partial/astra.jsonl", partial)
        atomic_json(output / "quality_report.json", report)
        atomic_json(output / "selection_manifest.json", manifest)
    return report


def main() -> None:
    args = arguments()
    report = clean(
        args.results_csv.resolve(),
        args.output.resolve(),
        args.repo_root.resolve(),
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
