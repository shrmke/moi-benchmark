#!/usr/bin/env python3
"""Build one CSV row for every DSH Toolathlon attempt in every result batch."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OUT_DIR = Path(__file__).resolve().parent
ROOT = OUT_DIR.parents[2]
DEFAULT_RESULTS = ROOT / "astra/results"
DEFAULT_OUTPUT = OUT_DIR / "dsh-toolathlon-all-attempt-results.csv"
PROTOCOL = ROOT / "astra/benchmark/toolathlon-verified/freeze/execution-protocol.freeze.json"
ATTEMPT_RE = re.compile(r"-a(\d+)(?:-|$)")
TRANSPORT_ERROR_TYPES = frozenset(
    {
        "ConnectionError",
        "ECONNRESET",
        "IncompleteRead",
        "SSLEOFError",
        "STREAM_CLOSED",
        "UND_ERR_SOCKET",
        "downstream_disconnected",
    }
)

sys.path.insert(0, str(OUT_DIR))
from generate_astra_hermes_comparison import build_row  # noqa: E402


PREFERRED_FIELDS = (
    "position",
    "task_id",
    "system",
    "batch",
    "state",
    "result_available",
    "agent_executed",
    "evaluator_executed",
    "verify_status",
    "run_validity",
    "terminal_status",
    "failure_category",
    "incomplete_stage",
    "incomplete_reason",
    "run_id",
    "attempt_ordinal",
    "source",
    "run_directory",
    "started_at",
    "start_time_source",
    "finished_at",
    "observed_seconds",
    "e2e_seconds",
    "agent_seconds",
    "evaluator_seconds",
    "orchestration_seconds",
    "timeout",
    "timeout_scope",
    "normal_e2e_success",
    "artifact_gate_status",
    "benchmark_status",
    "experiment_id",
    "product_version",
    "node_version",
    "model_id",
    "model_provider",
    "thinking",
    "reasoning_effort",
    "temperature",
    "agent_deadline_seconds",
    "budget_tier",
    "mcp_tool_count",
    "model_proxy_transport",
    "tool_calls",
    "tool_failures",
    "started_only_tool_calls",
    "claim_done_seen",
    "terminal_tool_counts",
    "model_request_limit",
    "model_request_limit_exceeded",
    "model_requests_started",
    "model_requests_completed",
    "model_requests_failed",
    "model_requests_successful_events",
    "stream_requests",
    "non_stream_requests",
    "model_transport_errors",
    "model_http_error_responses",
    "model_provider_error_responses",
    "model_error_type_counts",
    "model_http_status_counts",
    "connection_reused_true",
    "connection_reused_false",
    "connection_reused_unknown",
    "token_input",
    "token_output",
    "token_total",
    "cache_read_tokens",
    "token_reliable",
    "token_note",
    "token_usage_reported_completed",
    "token_usage_missing_completed",
    "preprocess_status",
    "running_status",
    "evaluation_status",
    "last_lifecycle_event",
    "last_lifecycle_at",
)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_jsonl_tolerant(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def task_positions() -> dict[str, int]:
    protocol = load_object(PROTOCOL)
    phases = protocol.get("formal_phases", {})
    tasks = list(phases.get("first_batch", {}).get("tasks", []))
    tasks.extend(phases.get("remaining_batch", {}).get("tasks", []))
    if len(tasks) != 108 or len(set(tasks)) != 108:
        raise RuntimeError("frozen protocol does not define exactly 108 tasks")
    return {str(task_id): position for position, task_id in enumerate(tasks, 1)}


def scalar(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def observed_seconds(started_at: Any, finished_at: Any) -> float | None:
    started = parse_timestamp(started_at)
    finished = parse_timestamp(finished_at)
    return (finished - started).total_seconds() if started and finished else None


def attempt_ordinal(run_id: str) -> int:
    match = ATTEMPT_RE.search(run_id)
    return int(match.group(1)) if match else 0


def status_fields(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    status = load_object(run_dir / "task-state/status.json")
    return status, {
        "preprocess_status": status.get("preprocess"),
        "running_status": status.get("running"),
        "evaluation_status": status.get("evaluation"),
    }


def all_events(run_dir: Path) -> list[dict[str, Any]]:
    rows = load_jsonl_tolerant(run_dir / "lifecycle-events.jsonl")
    rows.extend(load_jsonl_tolerant(run_dir / "adapter-events.jsonl"))
    return sorted(rows, key=lambda row: str(row.get("timestamp") or ""))


def event_present(events: list[dict[str, Any]], name: str) -> bool:
    return any(row.get("event") == name for row in events)


def attempt_times(
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[str, str, str, float | None]:
    if run.get("started_at"):
        started_at = str(run["started_at"])
        source = "run.json"
    elif events and events[0].get("timestamp"):
        started_at = str(events[0]["timestamp"])
        source = "lifecycle_event"
    else:
        started_at = datetime.fromtimestamp(
            run_dir.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        source = "directory_mtime"
    if run.get("finished_at"):
        finished_at = str(run["finished_at"])
    elif events and events[-1].get("timestamp"):
        finished_at = str(events[-1]["timestamp"])
    else:
        finished_at = ""
    return started_at, finished_at, source, observed_seconds(started_at, finished_at)


def cache_read_tokens(run_dir: Path) -> int | None:
    total = 0
    observed = False
    for row in load_jsonl_tolerant(run_dir / "model-usage.jsonl"):
        if row.get("event") != "model_request.completed":
            continue
        value = scalar((row.get("token_usage") or {}).get("cache_read_tokens"))
        if isinstance(value, (int, float)):
            total += int(value)
            observed = True
    return total if observed else None


def model_transport_fields(run_dir: Path) -> dict[str, Any]:
    completed = [
        row
        for row in load_jsonl_tolerant(run_dir / "model-usage.jsonl")
        if row.get("event") == "model_request.completed"
    ]
    errors: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    successful = 0
    transport_errors = 0
    http_errors = 0
    provider_errors = 0
    reused: Counter[str] = Counter()
    for row in completed:
        if row.get("success") is True:
            successful += 1
        error_type = scalar(row.get("error_type"))
        if error_type:
            errors[str(error_type)] += 1
        status = row.get("http_status")
        if isinstance(status, int):
            statuses[str(status)] += 1
            if status >= 400:
                http_errors += 1
        diagnostics = row.get("transport_diagnostics") or {}
        if (
            diagnostics.get("exception") is not None
            or str(error_type) in TRANSPORT_ERROR_TYPES
        ):
            transport_errors += 1
        if diagnostics.get("provider_error") is not None:
            provider_errors += 1
        connection_reused = diagnostics.get("connection_reused")
        if connection_reused is True:
            reused["true"] += 1
        elif connection_reused is False:
            reused["false"] += 1
        else:
            reused["unknown"] += 1
    return {
        "model_requests_successful_events": successful,
        "model_transport_errors": transport_errors,
        "model_http_error_responses": http_errors,
        "model_provider_error_responses": provider_errors,
        "model_error_type_counts": json.dumps(
            dict(sorted(errors.items())), separators=(",", ":")
        ),
        "model_http_status_counts": json.dumps(
            dict(sorted(statuses.items())), separators=(",", ":")
        ),
        "connection_reused_true": reused["true"],
        "connection_reused_false": reused["false"],
        "connection_reused_unknown": reused["unknown"],
    }


def terminal_tool_counts(run_dir: Path) -> str:
    counts: Counter[str] = Counter()
    for row in load_jsonl_tolerant(run_dir / "tool-calls.jsonl"):
        if row.get("state") not in {"succeeded", "failed"}:
            continue
        name = row.get("canonical_tool_name") or row.get("visible_tool_name")
        counts[str(name or "unknown")] += 1
    return json.dumps(dict(counts.most_common()), separators=(",", ":"))


def config_fields(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    config = load_object(run_dir / "resolved-config.json")
    runtime = config.get("runtime", {})
    model = config.get("model", {})
    budget = config.get("budget", {})
    adapter = config.get("adapter", {})
    exposure = adapter.get("tool_exposure", {})
    return {
        "artifact_gate_status": (run.get("artifact_gate") or {}).get("status"),
        "benchmark_status": run.get("benchmark_status") or config.get("benchmark_status"),
        "experiment_id": run.get("experiment_id") or config.get("experiment_id"),
        "product_version": runtime.get("version"),
        "node_version": runtime.get("node_version"),
        "model_id": model.get("request_id"),
        "model_provider": model.get("provider"),
        "thinking": model.get("thinking"),
        "reasoning_effort": model.get("reasoning_effort"),
        "temperature": model.get("temperature"),
        "agent_deadline_seconds": budget.get("agent_deadline_seconds")
        or run.get("deadline_s"),
        "budget_tier": budget.get("tier"),
        "mcp_tool_count": exposure.get("mcp_tool_count"),
        "model_proxy_transport": runtime.get("model_proxy_transport"),
    }


def classify_incomplete(
    run_dir: Path,
    events: list[dict[str, Any]],
    status: dict[str, Any],
) -> tuple[str, str]:
    names = {str(row.get("event")) for row in events}
    preprocess = status.get("preprocess")
    run_id = run_dir.name
    if "reset.start" not in names:
        if "manifest-missing" in run_id:
            return "preflight", "credential manifest unavailable during preflight"
        return "preflight", "preflight failed before task reset; detailed exception unavailable in attempt artifacts"
    if "reset.end" not in names:
        return "reset", "task reset did not complete"
    if "container.ready" not in names:
        return "container_start", "task container did not become ready"
    if "preprocess.start" not in names:
        return "prepare", "container copy or credential preparation failed before preprocess"
    if preprocess == "fail":
        return "preprocess", "task preprocess command exited non-zero"
    if preprocess == "running" or "preprocess.end" not in names:
        return "preprocess", "attempt was interrupted while task preprocess was running"
    if "gateway.start" in names and "gateway.ready" not in names:
        return "gateway", "tool gateway did not become ready before the lifecycle timeout"
    if "tools_list.start" in names and "tools_list.end" not in names:
        return "tool_discovery", "task tool discovery did not complete"
    if event_present(events, "agent.execution_start"):
        if event_present(events, "agent.execution_end"):
            return "post_agent", "agent ended but evaluator or run finalization did not complete"
        return "agent", "attempt was interrupted during agent execution"
    if "adapter.start" in names:
        return "agent_start", "adapter started but no agent execution result was recorded"
    return "post_preprocess", "preprocess completed but agent execution did not start"


def complete_row(
    position: int,
    task_id: str,
    batch: str,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    row = build_row(position, task_id, "dsh", run_dir, batch)
    agent_executed = event_present(events, "agent.execution_start") or isinstance(
        run.get("agent_duration_seconds"), (int, float)
    )
    evaluator_executed = event_present(events, "evaluator.end") or (
        "evaluator_exit_code" in run
    )
    status, status_values = status_fields(run_dir)
    started_at, finished_at, time_source, duration = attempt_times(
        run_dir, run, events
    )
    last_event = events[-1] if events else {}
    row.update(
        {
            "batch": batch,
            "state": "complete" if agent_executed and evaluator_executed else "incomplete",
            "result_available": bool(agent_executed and evaluator_executed),
            "agent_executed": agent_executed,
            "evaluator_executed": evaluator_executed,
            "attempt_ordinal": attempt_ordinal(str(run.get("run_id") or run_dir.name)),
            "started_at": started_at,
            "start_time_source": time_source,
            "finished_at": finished_at,
            "observed_seconds": duration,
            "incomplete_stage": "" if agent_executed and evaluator_executed else "finalization",
            "incomplete_reason": "" if agent_executed and evaluator_executed else "run.json exists without both agent and evaluator evidence",
            "cache_read_tokens": cache_read_tokens(run_dir),
            "terminal_tool_counts": terminal_tool_counts(run_dir),
            "last_lifecycle_event": last_event.get("event"),
            "last_lifecycle_at": last_event.get("timestamp"),
            **status_values,
            **config_fields(run_dir, run),
            **model_transport_fields(run_dir),
        }
    )
    return row


def incomplete_row(
    position: int,
    task_id: str,
    batch: str,
    run_dir: Path,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    status, status_values = status_fields(run_dir)
    stage, reason = classify_incomplete(run_dir, events, status)
    started_at, finished_at, time_source, duration = attempt_times(
        run_dir, {}, events
    )
    last_event = events[-1] if events else {}
    agent_executed = event_present(events, "agent.execution_start")
    evaluator_executed = event_present(events, "evaluator.end")
    return {
        "position": position,
        "task_id": task_id,
        "system": "dsh",
        "batch": batch,
        "state": "incomplete",
        "result_available": False,
        "agent_executed": agent_executed,
        "evaluator_executed": evaluator_executed,
        "verify_status": "missing",
        "run_validity": "missing",
        "terminal_status": "missing",
        "failure_category": "infra_incomplete",
        "incomplete_stage": stage,
        "incomplete_reason": reason,
        "run_id": run_dir.name,
        "attempt_ordinal": attempt_ordinal(run_dir.name),
        "source": batch,
        "run_directory": str(run_dir.resolve()),
        "started_at": started_at,
        "start_time_source": time_source,
        "finished_at": finished_at,
        "observed_seconds": duration,
        "normal_e2e_success": False,
        "terminal_tool_counts": terminal_tool_counts(run_dir),
        "cache_read_tokens": cache_read_tokens(run_dir),
        "last_lifecycle_event": last_event.get("event"),
        "last_lifecycle_at": last_event.get("timestamp"),
        **status_values,
        **config_fields(run_dir, {}),
        **model_transport_fields(run_dir),
    }


def collect_rows(results_root: Path) -> list[dict[str, Any]]:
    positions = task_positions()
    rows: list[dict[str, Any]] = []
    for runs_root in sorted(results_root.glob("*/runs/dsh")):
        batch = runs_root.parent.parent.name
        for task_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            task_id = task_dir.name
            if task_id not in positions:
                raise RuntimeError(f"unknown Toolathlon task in DSH results: {task_id}")
            for run_dir in sorted(path for path in task_dir.iterdir() if path.is_dir()):
                events = all_events(run_dir)
                run = load_object(run_dir / "run.json")
                if run:
                    row = complete_row(
                        positions[task_id], task_id, batch, run_dir, run, events
                    )
                else:
                    row = incomplete_row(
                        positions[task_id], task_id, batch, run_dir, events
                    )
                rows.append(row)
    rows.sort(
        key=lambda row: (
            int(row["position"]),
            parse_timestamp(row.get("started_at")) or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("batch") or ""),
            str(row.get("run_id") or ""),
        )
    )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(PREFERRED_FIELDS)
    fields.extend(
        sorted({key for row in rows for key in row}.difference(PREFERRED_FIELDS))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a complete all-attempt DSH Toolathlon CSV"
    )
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = collect_rows(args.results_root.resolve())
    write_csv(args.output.resolve(), rows)
    counts = Counter(str(row["state"]) for row in rows)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "rows": len(rows),
                "states": dict(sorted(counts.items())),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
