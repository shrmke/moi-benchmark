#!/usr/bin/env python3
"""Clean selected Hermes, PI, and DSH Toolathlon runs into the public trajectory schema."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import csv
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from clean_astra import Redactor

SCHEMA_VERSION = "toolathlon-trajectory/v1"
VALID_RESULTS = {"pass", "no_pass"}
PRODUCTS = ("hermes", "pi", "dsh")


def arguments() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument(
        "--comparison-csv",
        type=Path,
        default=repo_root / "astra/reports/Toolathlon-analysis/astra-hermes-pi-toolathlon-108-task-results.csv",
    )
    parser.add_argument(
        "--dsh-attempts-csv",
        type=Path,
        default=repo_root / "astra/reports/Toolathlon-analysis/dsh-toolathlon-all-attempt-results.csv",
    )
    parser.add_argument("--output", type=Path, default=repo_root / "astra/datasets/toolathlon-trajectory")
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


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
                skipped_event = None
                for candidate in ("message_update", "assistant/chunk"):
                    if f'"event": "{candidate}"' in line or f'"event":"{candidate}"' in line:
                        skipped_event = candidate
                        break
                if skipped_event is not None:
                    sequence = re.search(r'"sequence"\s*:\s*(\d+)', line)
                    rows.append({
                        "event": skipped_event,
                        "sequence": int(sequence.group(1)) if sequence else None,
                    })
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                else:
                    errors += 1
    except (OSError, UnicodeDecodeError):
        return [], 1
    return rows, errors


def number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return None


def public_path(path: Path, repo_root: Path, redactor: Redactor) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return redactor.text(str(path.resolve()))


def content_text(items: Any, kinds: set[str], redactor: Redactor) -> str:
    if not isinstance(items, list):
        return ""
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in kinds:
            continue
        value = item.get("text") if "text" in item else item.get("thinking")
        if isinstance(value, str):
            values.append(value)
    return redactor.text("\n".join(values))


def render(value: Any, redactor: Redactor) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return redactor.text(value)
    return json.dumps(redactor.value(value), ensure_ascii=False, sort_keys=True)


def message(
    role: str,
    content: str,
    timestamp: Any,
    *,
    reasoning: str = "",
    calls: list[dict[str, Any]] | None = None,
    call_id: Any = None,
    tool_name: Any = None,
    is_error: Any = None,
    omitted: bool = False,
) -> dict[str, Any]:
    return {
        "seq": 0,
        "role": role,
        "content": content,
        "reasoning_content": reasoning or None,
        "timestamp": str(timestamp) if timestamp is not None else None,
        "tool_calls": calls or [],
        "tool_call_id": str(call_id) if call_id is not None else None,
        "tool_name": str(tool_name) if tool_name is not None else None,
        "is_error": is_error if isinstance(is_error, bool) else None,
        "segment_index": 1 if role != "user" else 0,
        "internal_content_omitted": omitted,
    }


def assistant_from_content(items: Any, timestamp: Any, redactor: Redactor) -> dict[str, Any] | None:
    text = content_text(items, {"text"}, redactor)
    reasoning = content_text(items, {"thinking", "reasoning"}, redactor)
    calls: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type") not in {"toolCall", "tool-call"}:
                continue
            calls.append(
                {
                    "id": str(item.get("id")) if item.get("id") is not None else None,
                    "name": str(item.get("name")) if item.get("name") is not None else None,
                    "arguments_json": render(item.get("arguments"), redactor),
                }
            )
    if not text and not reasoning and not calls:
        return None
    return message("assistant", text, timestamp, reasoning=reasoning, calls=calls)


def parse_hermes(rows: list[dict[str, Any]], instruction: str, started_at: Any, redactor: Redactor) -> tuple[list[dict[str, Any]], list[str], int, int]:
    messages = [message("user", redactor.text(instruction), started_at)]
    pending_text: list[str] = []
    pending_reasoning: list[str] = []
    pending_by_name: dict[str, deque[str]] = defaultdict(deque)
    calls = results = 0

    def flush(timestamp: Any) -> None:
        nonlocal pending_text, pending_reasoning
        text = redactor.text("".join(pending_text))
        reasoning = redactor.text("\n".join(pending_reasoning))
        if text or reasoning:
            messages.append(message("assistant", text, timestamp, reasoning=reasoning))
        pending_text = []
        pending_reasoning = []

    for row in rows:
        event = row.get("event")
        native = row.get("native") if isinstance(row.get("native"), dict) else {}
        timestamp = row.get("timestamp")
        if event == "message.delta" and isinstance(native.get("delta"), str):
            pending_text.append(native["delta"])
        elif event == "reasoning.available" and isinstance(native.get("text"), str):
            pending_reasoning.append(native["text"])
        elif event == "tool.started":
            flush(timestamp)
            calls += 1
            name = str(native.get("tool") or "unknown")
            call_id = f"hermes-tool-{calls:04d}"
            pending_by_name[name].append(call_id)
            messages.append(message("assistant", "", timestamp, calls=[{"id": call_id, "name": name, "arguments_json": ""}], omitted=True))
        elif event == "tool.completed":
            flush(timestamp)
            results += 1
            name = str(native.get("tool") or "unknown")
            call_id = pending_by_name[name].popleft() if pending_by_name[name] else f"hermes-unmatched-{results:04d}"
            messages.append(message("tool", "", timestamp, call_id=call_id, tool_name=name, is_error=boolean(native.get("error")), omitted=True))
        elif event == "run.completed":
            output = native.get("output")
            if not pending_text and isinstance(output, str) and output:
                pending_text.append(output)
            flush(timestamp)
    flush(rows[-1].get("timestamp") if rows else started_at)
    reasons = [] if calls == results and all(not queue for queue in pending_by_name.values()) else ["unpaired_tool_call"]
    return messages, reasons, calls, results


def parse_pi(rows: list[dict[str, Any]], instruction: str, started_at: Any, redactor: Redactor) -> tuple[list[dict[str, Any]], list[str], int, int]:
    messages = [message("user", redactor.text(instruction), started_at)]
    starts: list[str | None] = []
    ends: list[str | None] = []
    for row in rows:
        event = row.get("event")
        native = row.get("native") if isinstance(row.get("native"), dict) else {}
        timestamp = row.get("timestamp")
        if event == "message_end":
            native_message = native.get("message") if isinstance(native.get("message"), dict) else {}
            if native_message.get("role") == "assistant":
                item = assistant_from_content(native_message.get("content"), timestamp, redactor)
                if item is not None:
                    messages.append(item)
        elif event == "tool.execution_start":
            starts.append(str(native.get("toolCallId") or native.get("tool_call_id")) if native.get("toolCallId") or native.get("tool_call_id") else None)
        elif event == "tool.execution_end":
            call_id = native.get("toolCallId") or native.get("tool_call_id")
            ends.append(str(call_id) if call_id is not None else None)
            result = native.get("result")
            messages.append(message("tool", render(result, redactor), timestamp, call_id=call_id, tool_name=native.get("toolName") or native.get("tool_name"), is_error=boolean(native.get("isError"))))
    reasons = [] if Counter(starts) == Counter(ends) and None not in starts and None not in ends else ["unpaired_tool_call"]
    return messages, reasons, len(starts), len(ends)


def parse_dsh(rows: list[dict[str, Any]], instruction: str, started_at: Any, redactor: Redactor) -> tuple[list[dict[str, Any]], list[str], int, int]:
    messages = [message("user", redactor.text(instruction), started_at)]
    starts: list[str | None] = []
    ends: list[str | None] = []
    for row in rows:
        event = row.get("event")
        native = row.get("native") if isinstance(row.get("native"), dict) else {}
        timestamp = row.get("timestamp")
        if event == "assistant/message":
            data = native.get("data") if isinstance(native.get("data"), dict) else {}
            native_message = data.get("message") if isinstance(data.get("message"), dict) else {}
            item = assistant_from_content(native_message.get("content"), timestamp, redactor)
            if item is not None:
                messages.append(item)
        elif event == "tool.execution_start":
            call_id = native.get("call_id") or native.get("tool_call_id")
            starts.append(str(call_id) if call_id is not None else None)
        elif event in {"tool.execution_end", "tool.execution_error"}:
            call_id = native.get("call_id") or native.get("tool_call_id")
            ends.append(str(call_id) if call_id is not None else None)
            data = native.get("data") if isinstance(native.get("data"), dict) else {}
            native_message = data.get("message") if isinstance(data.get("message"), dict) else {}
            parts = native_message.get("content") if isinstance(native_message.get("content"), list) else []
            result_content: Any = parts
            if len(parts) == 1 and isinstance(parts[0], dict) and parts[0].get("type") == "tool-result":
                result_content = parts[0].get("content")
            success = boolean(native.get("success"))
            messages.append(message("tool", render(result_content, redactor), timestamp, call_id=call_id, tool_name=native.get("name") or native.get("tool_name"), is_error=True if event == "tool.execution_error" else (not success if success is not None else None)))
    reasons = [] if Counter(starts) == Counter(ends) and None not in starts and None not in ends else ["unpaired_tool_call"]
    return messages, reasons, len(starts), len(ends)


def selected_rows(args: argparse.Namespace) -> dict[str, list[dict[str, str]]]:
    comparison = read_csv(args.comparison_csv)
    selected = {
        product: [row for row in comparison if row.get("system") == product and row.get("run_directory")]
        for product in ("hermes", "pi")
    }
    latest: dict[str, dict[str, str]] = {}
    for row in read_csv(args.dsh_attempts_csv):
        task = row.get("task_id", "")
        if not task or not row.get("run_directory") or not row.get("started_at"):
            continue
        key = (row.get("started_at", ""), row.get("batch", ""), row.get("run_id", ""))
        old = latest.get(task)
        old_key = (old.get("started_at", ""), old.get("batch", ""), old.get("run_id", "")) if old else None
        if old_key is None or key > old_key:
            latest[task] = row
    selected["dsh"] = [latest[task] for task in sorted(latest)]
    return selected


def evaluator(root: Path) -> tuple[dict[str, Any] | None, Path | None]:
    for relative in ("evaluator/eval_res.json", "evaluation/eval_res.json", "eval_res.json"):
        path = root / relative
        value = load_json(path)
        if value is not None and isinstance(value.get("pass"), bool):
            return value, path
    return None, None


def clean(row: dict[str, str], product: str, repo_root: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    root = Path(row["run_directory"])
    if not root.is_absolute():
        root = repo_root / root
    redactor = Redactor(repo_root)
    run = load_json(root / "run.json")
    bundle = load_json(root / "task-bundle.public.json")
    eval_result, eval_path = evaluator(root)
    status = row.get("verify_status")
    exclusion: list[str] = []
    if run is None:
        exclusion.append("missing_or_invalid_run")
    if bundle is None:
        exclusion.append("missing_or_invalid_task_bundle")
    if status not in VALID_RESULTS:
        exclusion.append("invalid_selected_status")
    if eval_result is None or eval_path is None:
        exclusion.append("missing_or_invalid_evaluator")
    elif status in VALID_RESULTS and eval_result["pass"] != (status == "pass"):
        exclusion.append("evaluator_projection_mismatch")
    manifest = {
        "record_id": f"{product}/{row.get('task_id')}/{row.get('run_id')}",
        "task": row.get("task_id"),
        "attempt_run_id": row.get("run_id"),
        "selection_kind": row.get("source") or row.get("batch"),
        "selected_status": status,
        "source": public_path(root, repo_root, redactor),
        "evaluator_source": public_path(eval_path, repo_root, redactor) if eval_path else None,
        "included": not exclusion,
        "exclusion_reasons": exclusion,
    }
    if exclusion or run is None or bundle is None or eval_result is None or eval_path is None:
        return None, manifest

    trajectory_path = root / "trajectory.jsonl"
    events, parse_errors = load_jsonl(trajectory_path)
    sequences = [event.get("sequence") for event in events]
    sequence_contiguous = bool(sequences) and all(isinstance(value, int) for value in sequences) and all(right == left + 1 for left, right in zip(sequences, sequences[1:]))
    instruction = str(bundle.get("prompt") or "")
    parser = {"hermes": parse_hermes, "pi": parse_pi, "dsh": parse_dsh}[product]
    messages, reasons, call_count, result_count = parser(events, instruction, run.get("started_at"), redactor)
    if parse_errors:
        reasons.append("trajectory_parse_error")
    if not sequence_contiguous:
        reasons.append("sequence_gap")
    terminal = run.get("terminal_status") == "completed"
    if not terminal:
        reasons.append("missing_terminal")
    declared = run.get("trajectory") if isinstance(run.get("trajectory"), dict) else {}
    tool_count_consistent = declared.get("tool_started_events") == call_count and declared.get("tool_terminal_events") == result_count
    if not tool_count_consistent:
        reasons.append("tool_count_mismatch")
    meaningful = any(item["role"] == "assistant" and (item["content"] or item["reasoning_content"] or item["tool_calls"]) for item in messages)
    if not meaningful:
        reasons.append("no_meaningful_assistant_content")
    reasons = list(dict.fromkeys(reasons))
    for index, item in enumerate(messages):
        item["seq"] = index

    tools = bundle.get("tools") if isinstance(bundle.get("tools"), list) else []
    tool_names = []
    for tool in tools:
        if isinstance(tool, str):
            tool_names.append(tool)
        elif isinstance(tool, dict):
            name = tool.get("name") or (tool.get("function") or {}).get("name") if isinstance(tool.get("function"), dict) else tool.get("name")
            if name:
                tool_names.append(str(name))
    usage_started = number(row.get("model_requests_started"))
    usage_completed = number(row.get("model_requests_completed"))
    usage_reported = number(row.get("token_usage_reported_completed"))
    usage_missing = number(row.get("token_usage_missing_completed"))
    record = {
        "agent": {
            "commit": None,
            "model": row.get("model_id") or None,
            "name": product,
            "system_id": product,
            "tools": sorted(set(tool_names)),
        },
        "benchmark": {
            "name": "toolathlon",
            "selection_kind": row.get("source") or row.get("batch") or "effective_projection",
            "task_id": row.get("task_id"),
        },
        "instruction": redactor.text(instruction),
        "messages": messages,
        "outcome": {
            "evaluation_kind": eval_result.get("evaluation_kind") or "original",
            "evaluator_pass": bool(eval_result["pass"]),
            "evaluator_valid": True,
            "result_summary": redactor.value({key: value for key, value in eval_result.items() if key != "pass"}),
            "status": status,
        },
        "quality": {
            "continuation_complete": True,
            "reasoning_event_count": sum(1 for event in events if "reason" in str(event.get("event", "")).lower()),
            "reasoning_included": any(item.get("reasoning_content") for item in messages),
            "reasons": reasons,
            "redaction_count": redactor.count,
            "sequence_contiguous": sequence_contiguous,
            "terminal": terminal,
            "tier": "complete" if not reasons else "partial",
            "token_usage_affects_tier": False,
            "tool_calls_paired": "unpaired_tool_call" not in reasons,
            "tool_count_consistent": tool_count_consistent,
            "trajectory_parseable": parse_errors == 0,
        },
        "record_id": manifest["record_id"],
        "schema_version": SCHEMA_VERSION,
        "source": {
            "attempt_path": public_path(root, repo_root, redactor),
            "evaluator_path": public_path(eval_path, repo_root, redactor),
            "event_counts": dict(sorted(Counter(event.get("event", "<missing>") for event in events).items())),
            "selection_kind": row.get("source") or row.get("batch") or "effective_projection",
            "trajectory_path": public_path(trajectory_path, repo_root, redactor),
        },
        "timing": {
            "agent_seconds": number(row.get("agent_seconds")) or number(run.get("agent_duration_seconds")),
            "e2e_seconds": number(row.get("e2e_seconds")),
            "evaluator_seconds": number(row.get("evaluator_seconds")) or number(run.get("evaluator_duration_seconds")),
            "orchestration_seconds": number(row.get("orchestration_seconds")),
        },
        "trial": {
            "attempt_directory": root.name,
            "attempt_run_id": row.get("run_id"),
            "finished_at": run.get("finished_at"),
            "segments": [{
                "finish_sequence": sequences[-1] if sequences else None,
                "finished_status": run.get("terminal_status"),
                "run_id": row.get("run_id"),
                "segment_index": 1,
                "session_id": None,
                "start_sequence": sequences[0] if sequences else None,
            }],
            "started_at": run.get("started_at"),
            "terminal_status": run.get("terminal_status"),
            "termination_reason": run.get("termination_reason"),
        },
        "usage": {
            "cache_read_tokens": number(row.get("cache_read_tokens")),
            "cache_write_tokens": None,
            "coverage": {
                "reported_completed": usage_reported,
                "missing_completed": usage_missing,
                "reliable": boolean(row.get("token_reliable")),
                "note": row.get("token_note") or None,
            },
            "input_tokens": number(row.get("token_input")),
            "model_request_completed": usage_completed,
            "model_request_started": usage_started,
            "model_request_succeeded": number(row.get("model_requests_successful_events")) or usage_completed,
            "models": [row["model_id"]] if row.get("model_id") else [],
            "output_tokens": number(row.get("token_output")),
            "tool_call_count": call_count,
            "tool_result_count": result_count,
            "total_tokens": number(row.get("token_total")),
        },
    }
    manifest["included"] = True
    return record, manifest


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = arguments()
    selected = selected_rows(args)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "products": {},
    }
    for product in args.products:
        records: list[dict[str, Any]] = []
        manifests: list[dict[str, Any]] = []
        for row in selected[product]:
            record, manifest = clean(row, product, args.repo_root)
            manifests.append(manifest)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: (item["benchmark"]["task_id"], item["trial"]["started_at"] or "", item["record_id"]))
        complete = [record for record in records if record["quality"]["tier"] == "complete"]
        partial = [record for record in records if record["quality"]["tier"] == "partial"]
        reasons = Counter(reason for record in partial for reason in record["quality"]["reasons"])
        report["products"][product] = {
            "selected": len(selected[product]),
            "retained": len(records),
            "complete": len(complete),
            "partial": len(partial),
            "excluded": len(selected[product]) - len(records),
            "outcomes": dict(sorted(Counter(record["outcome"]["status"] for record in records).items())),
            "partial_reason_counts_nonexclusive": dict(sorted(reasons.items())),
            "messages": sum(len(record["messages"]) for record in records),
            "tool_calls": sum(record["usage"]["tool_call_count"] for record in records),
            "excluded_records": [item for item in manifests if not item["included"]],
        }
        if not args.dry_run:
            write_jsonl(args.output / "data/complete" / f"{product}.jsonl", complete)
            write_jsonl(args.output / "data/partial" / f"{product}.jsonl", partial)
    if not args.dry_run:
        report_path = args.output / "other_agents_quality_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
