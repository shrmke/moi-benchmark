from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from astra.runners.hermes_terminal_bench.gateway_driver import (
    validate_run_event_stream,
    validate_session_export,
)
from astra.runners.astra_terminal_bench.trajectory_export import (
    validate_trajectory_bundle,
)
from astra.runners.pi_terminal_bench.events import (
    validate_event_stream as validate_pi_event_stream,
    validate_session as validate_pi_session,
)
from astra.runners.dsh_terminal_bench.events import (
    validate_event_stream as validate_dsh_event_stream,
)

from .core import LifecycleControllerError, parse_process_cleanup_report


_SHA256_LENGTH = 64
_HERMES_MANAGED_POLICY_SHA256 = (
    "c4d0745a7749bb2ad7b05a5e726121c7a0d330db79fe8cfc12954d546c250bf3"
)
_HERMES_MANAGED_ENV_SHA256 = (
    "1b0068a683773edf8397d4fa636d15c586950f219257826abe253f1b68a02639"
)
_HERMES_POLICY_GUARD_SHA256 = (
    "8c6c1ae40188ad7a7b4143585ac1ab9695f525ec908dba1659a6a77e3a66bcdf"
)
_FORBIDDEN_EVENT_FRAGMENTS = (
    "fault",
    "fault_injected",
    "kill",
    "post_fault",
    "relaunch",
    "signal",
)
_LEDGER_ENVELOPE_FIELDS = {
    "event",
    "monotonic_ns",
    "run_id",
    "schema_version",
    "sequence",
    "timestamp",
}


class AuditError(RuntimeError):
    """Persisted C0 evidence is missing or internally inconsistent."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{path} must contain a JSON object")
    return value


def _load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read controller ledger {path}: {exc}") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise AuditError("controller ledger must contain JSON objects")
    return rows


def _validate_envelope(rows: list[dict[str, Any]]) -> str:
    sequences = [row.get("sequence") for row in rows]
    if not all(type(sequence) is int for sequence in sequences) or sequences != list(
        range(1, len(rows) + 1)
    ):
        raise AuditError("ledger sequence is not contiguous from 1")

    run_id_values = [row.get("run_id") for row in rows]
    if not all(isinstance(run_id, str) and run_id for run_id in run_id_values):
        raise AuditError("ledger must contain one non-empty run_id")
    run_ids = set(run_id_values)
    if len(run_ids) != 1:
        raise AuditError("ledger must contain one non-empty run_id")

    monotonic_values = [row.get("monotonic_ns") for row in rows]
    if not all(type(value) is int for value in monotonic_values):
        raise AuditError("ledger is missing integer monotonic_ns values")
    if monotonic_values != sorted(monotonic_values):
        raise AuditError("ledger monotonic timestamps moved backwards")

    for row in rows:
        if row.get("schema_version") != 1:
            raise AuditError("unsupported ledger schema version")
        if not isinstance(row.get("event"), str) or not row["event"]:
            raise AuditError("ledger event names must be non-empty strings")
        try:
            timestamp = datetime.fromisoformat(str(row["timestamp"]))
        except (KeyError, ValueError) as exc:
            raise AuditError("ledger has an invalid wall-clock timestamp") from exc
        if timestamp.tzinfo is None:
            raise AuditError("ledger wall-clock timestamps must be timezone-aware")

    return next(iter(run_ids))


def _events(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("event") == name]


def _one(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = _events(rows, name)
    if len(matches) != 1:
        raise AuditError(f"expected exactly one {name!r} event, found {len(matches)}")
    return matches[0]


def _at_most_one(
    rows: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    matches = _events(rows, name)
    if len(matches) > 1:
        raise AuditError(f"expected at most one {name!r} event, found {len(matches)}")
    return matches[0] if matches else None


def _assert_order(events: Sequence[dict[str, Any]]) -> None:
    sequences = [event["sequence"] for event in events]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        names = [event["event"] for event in events]
        raise AuditError(f"events are out of order: {names}")


def _require_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuditError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _same(field: str, values: Sequence[Any]) -> Any:
    if not values or any(value != values[0] for value in values[1:]):
        raise AuditError(f"{field} is inconsistent between metadata and ledger")
    return values[0]


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    agent_result = result.get("agent_result")
    if not isinstance(agent_result, dict):
        raise AuditError("trial result has no agent_result object")
    metadata = agent_result.get("metadata")
    if not isinstance(metadata, dict):
        raise AuditError("trial result has no agent_result.metadata object")
    return metadata


def _validate_no_fault_events(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        event = str(row.get("event", "")).lower()
        if event != "fault_action" and any(
            fragment in event for fragment in _FORBIDDEN_EVENT_FRAGMENTS
        ):
            raise AuditError(f"C0 ledger contains forbidden fault event {event!r}")
        if row.get("fault_injected") is True:
            raise AuditError("C0 ledger records fault_injected=true")
        if "condition" in row and row["condition"] != "C0":
            raise AuditError("C0 ledger contains a non-C0 condition")
        if "fault_action" in row:
            if event == "product_process_cleanup":
                if row["fault_action"] is not False:
                    raise AuditError("process cleanup is incorrectly marked as a fault")
            elif row["fault_action"] != "noop":
                raise AuditError("C0 ledger registers a non-noop fault action")
        if "action" in row and row["action"] != "noop":
            raise AuditError("C0 ledger contains a non-noop action")
        for field, value in row.items():
            if "signal" in field.lower() and value not in (None, False, [], {}):
                raise AuditError(f"C0 ledger contains signal evidence in {field!r}")


def _manifest_sha256(
    *, task_id: str, predicate_id: str, stable_observations: int
) -> str:
    payload = {
        "predicate_id": predicate_id,
        "stable_observations": stable_observations,
        "task_id": task_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _upstream(result: dict[str, Any]) -> dict[str, Any]:
    verifier_result = result.get("verifier_result")
    rewards = (
        verifier_result.get("rewards")
        if isinstance(verifier_result, dict)
        else None
    )
    return {
        "verifier_rewards": rewards,
        "exception_info": result.get("exception_info"),
    }


def _validate_astra_trajectory(
    result_path: Path,
    metadata: dict[str, Any],
    started: dict[str, Any],
    completed: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if started.get("product") != "astra":
        return None
    if (
        started.get("trajectory_capture_required") is not True
        or started.get("trajectory_capture_mode")
        != "astra_server_and_local_session"
    ):
        raise AuditError("Astra required trajectory capture is not armed")
    capture_blocking = _same(
        "trajectory_capture_blocking",
        [
            metadata.get("trajectory_capture_blocking"),
            started.get("trajectory_capture_blocking"),
            completed.get("trajectory_capture_blocking"),
        ],
    )
    if capture_blocking is not False:
        raise AuditError("Astra trajectory capture is incorrectly blocking")

    registered = _one(rows, "astra_session_registered")
    terminal = _one(rows, "astra_session_terminal")
    persisted = _one(rows, "astra_trajectory_persisted")
    outcome = _one(rows, "astra_session_outcome")
    product_started = _one(rows, "product_turn_started")
    process_cleanup = _one(rows, "product_process_cleanup")
    product_exited = _one(rows, "product_turn_exited")
    _assert_order(
        [
            registered,
            product_started,
            process_cleanup,
            terminal,
            persisted,
            product_exited,
            outcome,
            completed,
        ]
    )

    session_id = _same(
        "astra_session_id",
        [
            metadata.get("astra_session_id"),
            registered.get("astra_session_id"),
            terminal.get("astra_session_id"),
            persisted.get("astra_session_id"),
            outcome.get("astra_session_id"),
            completed.get("astra_session_id"),
        ],
    )
    if not isinstance(session_id, str) or not session_id:
        raise AuditError("Astra trajectory has no session identity")
    terminal_status = _same(
        "Astra trajectory product_terminal_status",
        [
            metadata.get("product_terminal_status"),
            terminal.get("product_terminal_status"),
            outcome.get("product_terminal_status"),
            completed.get("product_terminal_status"),
        ],
    )
    product_completion_claim = _same(
        "Astra product_completion_claim",
        [
            metadata.get("product_completion_claim"),
            outcome.get("product_completion_claim"),
            completed.get("product_completion_claim"),
        ],
    )
    capture_status = _same(
        "astra_trajectory_status",
        [
            metadata.get("astra_trajectory_status"),
            persisted.get("capture_status"),
            completed.get("astra_trajectory_status"),
        ],
    )
    capture_failed = _same(
        "astra_trajectory_capture_failed",
        [
            metadata.get("astra_trajectory_capture_failed"),
            persisted.get("capture_failed"),
            completed.get("astra_trajectory_capture_failed"),
        ],
    )
    if capture_status not in {"complete", "partial", "missing"}:
        raise AuditError("Astra trajectory capture status is invalid")
    if capture_failed is not (capture_status != "complete"):
        raise AuditError("Astra trajectory capture failure flag is inconsistent")
    if (
        metadata.get("astra_trajectory_manifest")
        != "agent/astra-trajectory/manifest.json"
        or persisted.get("manifest_path")
        != "agent/astra-trajectory/manifest.json"
    ):
        raise AuditError("Astra trajectory manifest path is inconsistent")

    manifest_sha256 = _same(
        "astra_trajectory_manifest_sha256",
        [
            metadata.get("astra_trajectory_manifest_sha256"),
            persisted.get("manifest_sha256"),
            completed.get("astra_trajectory_manifest_sha256"),
        ],
    )
    if manifest_sha256 is not None:
        manifest_sha256 = _require_sha256(
            manifest_sha256,
            "astra_trajectory_manifest_sha256",
        )
    trajectory_file_count = _same(
        "astra_trajectory_file_count",
        [
            metadata.get("astra_trajectory_file_count"),
            persisted.get("trajectory_file_count"),
            completed.get("astra_trajectory_file_count"),
        ],
    )
    server_event_count = _same(
        "astra_trajectory_server_event_count",
        [
            metadata.get("astra_trajectory_server_event_count"),
            persisted.get("server_event_count"),
            completed.get("astra_trajectory_server_event_count"),
        ],
    )
    local_file_count = _same(
        "astra_trajectory_local_file_count",
        [
            metadata.get("astra_trajectory_local_file_count"),
            persisted.get("local_file_count"),
        ],
    )
    local_trace_file_count = _same(
        "astra_trajectory_local_trace_file_count",
        [
            metadata.get("astra_trajectory_local_trace_file_count"),
            persisted.get("local_trace_file_count"),
        ],
    )
    tool_result_file_count = _same(
        "astra_trajectory_tool_result_file_count",
        [
            metadata.get("astra_trajectory_tool_result_file_count"),
            persisted.get("tool_result_file_count"),
        ],
    )
    local_journal_event_count = _same(
        "astra_trajectory_local_journal_event_count",
        [
            metadata.get("astra_trajectory_local_journal_event_count"),
            persisted.get("local_journal_event_count"),
            completed.get("astra_trajectory_local_journal_event_count"),
        ],
    )
    local_journal_terminal_event = _same(
        "astra_trajectory_local_journal_terminal_event",
        [
            metadata.get("astra_trajectory_local_journal_terminal_event"),
            persisted.get("local_journal_terminal_event"),
            completed.get("astra_trajectory_local_journal_terminal_event"),
        ],
    )
    for field, value in (
        ("astra_trajectory_file_count", trajectory_file_count),
        ("astra_trajectory_server_event_count", server_event_count),
        ("astra_trajectory_local_file_count", local_file_count),
        ("astra_trajectory_local_trace_file_count", local_trace_file_count),
        ("astra_trajectory_local_journal_event_count", local_journal_event_count),
    ):
        if type(value) is not int or value < 0:
            raise AuditError(f"{field} must be a non-negative integer")
    if type(tool_result_file_count) is not int or tool_result_file_count < 0:
        raise AuditError(
            "astra_trajectory_tool_result_file_count must be non-negative"
        )

    trajectory_root = result_path.parent / "agent" / "astra-trajectory"
    if capture_status == "complete":
        if manifest_sha256 is None:
            raise AuditError("complete Astra trajectory has no manifest SHA-256")
        for field, value in (
            ("astra_trajectory_file_count", trajectory_file_count),
            ("astra_trajectory_server_event_count", server_event_count),
            ("astra_trajectory_local_file_count", local_file_count),
            ("astra_trajectory_local_trace_file_count", local_trace_file_count),
            (
                "astra_trajectory_local_journal_event_count",
                local_journal_event_count,
            ),
        ):
            if value <= 0:
                raise AuditError(f"{field} must be a positive integer")
        try:
            validated = validate_trajectory_bundle(
                trajectory_root,
                session_id=session_id,
                terminal_status=terminal_status,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise AuditError(f"invalid Astra trajectory bundle: {exc}") from exc
        if (
            validated["manifest_sha256"] != manifest_sha256
            or validated["trajectory_file_count"] != trajectory_file_count
            or validated["server_event_count"] != server_event_count
            or validated["local_file_count"] != local_file_count
            or validated["local_trace_file_count"] != local_trace_file_count
            or validated["tool_result_file_count"] != tool_result_file_count
            or validated["local_journal_event_count"]
            != local_journal_event_count
            or validated["local_journal_terminal_event"]
            != local_journal_terminal_event
        ):
            raise AuditError("Astra trajectory evidence is internally inconsistent")

    session_record = _load_json(
        result_path.parent / "agent" / "astra-session.json"
    )
    created_record = _load_json(
        result_path.parent / "agent" / "astra-session-created.json"
    )
    status_record = _load_json(
        result_path.parent / "agent" / "trajectory-status.json"
    )
    expected_failed = (
        terminal_status != "completed"
        or product_completion_claim is not True
    )
    expected_persist_failed = (
        terminal_status != "completed"
        or capture_failed
        or persisted.get("adapter_cancelled") is True
    )
    expected_status_failed = terminal_status != "completed" or capture_failed
    if (
        terminal.get("failed") is not (terminal_status != "completed")
        or persisted.get("failed") is not expected_persist_failed
        or outcome.get("failed") is not expected_failed
        or session_record.get("astra_session_id") != session_id
        or session_record.get("product_terminal_status") != terminal_status
        or session_record.get("capture_status") != capture_status
        or session_record.get("failed") is not expected_failed
        or created_record.get("session_id") != session_id
        or status_record.get("astra_session_id") != session_id
        or status_record.get("product_terminal_status") != terminal_status
        or status_record.get("capture_status") != capture_status
        or status_record.get("capture_failed") is not capture_failed
        or status_record.get("failed") is not expected_status_failed
        or status_record.get("manifest_sha256") != manifest_sha256
        or status_record.get("trajectory_file_count") != trajectory_file_count
        or status_record.get("local_file_count") != local_file_count
        or status_record.get("local_trace_file_count")
        != local_trace_file_count
        or status_record.get("tool_result_file_count")
        != tool_result_file_count
        or status_record.get("server_event_count") != server_event_count
        or status_record.get("local_journal_event_count")
        != local_journal_event_count
        or status_record.get("local_journal_terminal_event")
        != local_journal_terminal_event
    ):
        raise AuditError("Astra session or trajectory status record is inconsistent")
    return {
        "path": str(trajectory_root),
        "status": capture_status,
        "complete": capture_status == "complete",
        "capture_failed": capture_failed,
        "blocking": capture_blocking,
        "session_id": session_id,
        "manifest_sha256": manifest_sha256,
        "file_count": trajectory_file_count,
        "local_file_count": local_file_count,
        "local_trace_file_count": local_trace_file_count,
        "tool_result_file_count": tool_result_file_count,
        "server_event_count": server_event_count,
        "local_journal_event_count": local_journal_event_count,
        "local_journal_terminal_event": local_journal_terminal_event,
    }


def _validate_hermes_managed_policy(
    result_path: Path,
    metadata: dict[str, Any],
    started: dict[str, Any],
    completed: dict[str, Any],
    product_terminal_status: str,
) -> dict[str, Any] | None:
    if started.get("product") != "hermes":
        return None
    policy_sha256 = _same(
        "managed_policy_sha256",
        [
            metadata.get("managed_policy_sha256"),
            started.get("managed_policy_sha256"),
            completed.get("managed_policy_sha256"),
        ],
    )
    policy_sha256 = _require_sha256(
        policy_sha256, "managed_policy_sha256"
    )
    if (
        metadata.get("managed_policy_path") != "/etc/hermes/config.yaml"
        or started.get("managed_policy_path") != "/etc/hermes/config.yaml"
        or metadata.get("managed_policy_read_only") is not True
        or started.get("managed_policy_read_only") is not True
    ):
        raise AuditError(
            "Hermes managed policy is not recorded as the read-only "
            "/etc/hermes/config.yaml"
        )

    artifact_path = (
        result_path.parent / "agent" / "hermes-managed-config.yaml"
    )
    try:
        policy_bytes = artifact_path.read_bytes()
    except OSError as exc:
        raise AuditError(
            f"cannot validate Hermes managed policy artifact "
            f"{artifact_path}: {exc}"
        ) from exc
    if hashlib.sha256(policy_bytes).hexdigest() != policy_sha256:
        raise AuditError(
            "Hermes managed policy artifact SHA-256 does not match the ledger"
        )
    if policy_sha256 != _HERMES_MANAGED_POLICY_SHA256:
        raise AuditError(
            "Hermes managed policy does not match the frozen C0 policy digest"
        )

    env_sha256 = _same(
        "managed_env_sha256",
        [
            metadata.get("managed_env_sha256"),
            started.get("managed_env_sha256"),
            completed.get("managed_env_sha256"),
        ],
    )
    env_sha256 = _require_sha256(env_sha256, "managed_env_sha256")
    if (
        metadata.get("managed_env_path") != "/etc/hermes/.env"
        or started.get("managed_env_path") != "/etc/hermes/.env"
        or metadata.get("managed_env_read_only") is not True
        or started.get("managed_env_read_only") is not True
    ):
        raise AuditError(
            "Hermes managed environment is not recorded as the read-only "
            "/etc/hermes/.env"
        )
    env_artifact_path = (
        result_path.parent / "agent" / "hermes-managed.env"
    )
    try:
        env_bytes = env_artifact_path.read_bytes()
    except OSError as exc:
        raise AuditError(
            f"cannot validate Hermes managed environment artifact "
            f"{env_artifact_path}: {exc}"
        ) from exc
    if hashlib.sha256(env_bytes).hexdigest() != env_sha256:
        raise AuditError(
            "Hermes managed environment artifact SHA-256 does not match "
            "the ledger"
        )
    if env_sha256 != _HERMES_MANAGED_ENV_SHA256:
        raise AuditError(
            "Hermes managed environment does not match the frozen C0 digest"
        )

    guard_sha256 = _same(
        "policy_guard_sha256",
        [
            metadata.get("policy_guard_sha256"),
            started.get("policy_guard_sha256"),
            completed.get("policy_guard_sha256"),
        ],
    )
    guard_sha256 = _require_sha256(
        guard_sha256, "policy_guard_sha256"
    )
    if (
        metadata.get("policy_guard_path")
        != "/installed-agent/hermes-c0-policy/sitecustomize.py"
        or started.get("policy_guard_path")
        != "/installed-agent/hermes-c0-policy/sitecustomize.py"
        or metadata.get("policy_guard_active") is not True
        or completed.get("policy_guard_active") is not True
    ):
        raise AuditError(
            "Hermes C0 policy guard is not recorded as active"
        )
    guard_artifact_path = (
        result_path.parent / "agent" / "hermes-policy-guard.py"
    )
    try:
        guard_bytes = guard_artifact_path.read_bytes()
    except OSError as exc:
        raise AuditError(
            f"cannot validate Hermes policy guard artifact "
            f"{guard_artifact_path}: {exc}"
        ) from exc
    if hashlib.sha256(guard_bytes).hexdigest() != guard_sha256:
        raise AuditError(
            "Hermes policy guard artifact SHA-256 does not match the ledger"
        )
    if guard_sha256 != _HERMES_POLICY_GUARD_SHA256:
        raise AuditError(
            "Hermes policy guard does not match the frozen C0 digest"
        )
    driver_result = _load_json(
        result_path.parent / "agent" / "hermes-run.json"
    )
    guard_evidence = driver_result.get("policy_guard")
    if (
        driver_result.get("policy_guard_active") is not True
        or not isinstance(guard_evidence, dict)
        or guard_evidence.get("event") != "policy_guard.loaded"
        or guard_evidence.get("pid") != driver_result.get("gateway_pid")
        or guard_evidence.get("source_sha256") != guard_sha256
    ):
        raise AuditError(
            "Hermes driver result has no matching policy guard evidence"
        )

    if (
        started.get("trajectory_capture_required") is not True
        or started.get("trajectory_capture_mode")
        != "streaming_runs_api_jsonl"
        or started.get("trajectory_session_export_required") is not True
    ):
        raise AuditError("Hermes required trajectory capture is not armed")
    capture_blocking = _same(
        "trajectory_capture_blocking",
        [
            metadata.get("trajectory_capture_blocking"),
            started.get("trajectory_capture_blocking"),
            completed.get("trajectory_capture_blocking"),
        ],
    )
    if capture_blocking is not False:
        raise AuditError("Hermes trajectory capture is incorrectly blocking")
    capture_status = _same(
        "trajectory_capture_status",
        [
            metadata.get("trajectory_capture_status"),
            completed.get("trajectory_capture_status"),
        ],
    )
    capture_error = _same(
        "trajectory_capture_error",
        [
            metadata.get("trajectory_capture_error"),
            completed.get("trajectory_capture_error"),
        ],
    )
    if capture_status not in {"saved", "failed"}:
        raise AuditError("Hermes trajectory capture status is invalid")
    if (
        metadata.get("trajectory_capture_path")
        != "agent/hermes-run-events.jsonl"
        or metadata.get("trajectory_capture_format")
        != "hermes_runs_api_jsonl"
        or metadata.get("trajectory_session_export_path")
        != "agent/hermes-session.jsonl"
        or metadata.get("trajectory_session_export_format")
        != "hermes_session_jsonl"
    ):
        raise AuditError("Hermes trajectory artifact paths are inconsistent")

    event_stream_status = metadata.get("trajectory_event_stream_status")
    if event_stream_status not in {"saved", "failed"}:
        raise AuditError("Hermes event stream capture status is invalid")
    trajectory_sha256 = _same(
        "trajectory_capture_sha256",
        [
            metadata.get("trajectory_capture_sha256"),
            completed.get("trajectory_capture_sha256"),
        ],
    )
    trajectory_event_count = _same(
        "trajectory_event_count",
        [
            metadata.get("trajectory_event_count"),
            completed.get("trajectory_event_count"),
        ],
    )
    submitted_count = _same(
        "trajectory_submitted_count",
        [
            metadata.get("trajectory_submitted_count"),
            completed.get("trajectory_submitted_count"),
        ],
    )
    terminal_event_count = _same(
        "trajectory_terminal_event_count",
        [
            metadata.get("trajectory_terminal_event_count"),
            completed.get("trajectory_terminal_event_count"),
        ],
    )
    terminal_event = _same(
        "trajectory_terminal_event",
        [
            metadata.get("trajectory_terminal_event"),
            completed.get("trajectory_terminal_event"),
        ],
    )
    terminal_event_source = _same(
        "trajectory_terminal_event_source",
        [
            metadata.get("trajectory_terminal_event_source"),
            completed.get("trajectory_terminal_event_source"),
        ],
    )
    terminal_reason = _same(
        "trajectory_terminal_reason",
        [
            metadata.get("trajectory_terminal_reason"),
            completed.get("trajectory_terminal_reason"),
        ],
    )
    if (
        type(trajectory_event_count) is not int
        or trajectory_event_count < 0
        or type(submitted_count) is not int
        or submitted_count < 0
        or type(terminal_event_count) is not int
        or terminal_event_count < 0
    ):
        raise AuditError("Hermes trajectory event counts are invalid")
    if (
        terminal_event in {"run.completed", "run.failed", "run.cancelled"}
        and terminal_event_source is None
    ):
        # Legacy native-terminal trials predate the explicit source field.
        terminal_event_source = "hermes"
    if event_stream_status == "saved":
        if terminal_event == "run.timed_out":
            if (
                terminal_event_source != "driver"
                or terminal_reason != "ProductDeadlineExpired"
                or product_terminal_status != "timeout"
                or completed.get("product_return_code") != 124
            ):
                raise AuditError(
                    "Hermes driver deadline terminal metadata is inconsistent"
                )
        elif terminal_event_source != "hermes":
            raise AuditError("Hermes native terminal source is inconsistent")

    session_export_status = _same(
        "trajectory_session_export_status",
        [
            metadata.get("trajectory_session_export_status"),
            completed.get("trajectory_session_export_status"),
        ],
    )
    if session_export_status not in {"saved", "failed"}:
        raise AuditError("Hermes session export capture status is invalid")
    session_sha256 = _same(
        "trajectory_session_sha256",
        [
            metadata.get("trajectory_session_sha256"),
            completed.get("trajectory_session_sha256"),
        ],
    )
    session_id = _same(
        "trajectory_session_id",
        [
            metadata.get("trajectory_session_id"),
            completed.get("trajectory_session_id"),
        ],
    )
    session_message_count = _same(
        "trajectory_session_message_count",
        [
            metadata.get("trajectory_session_message_count"),
            completed.get("trajectory_session_message_count"),
        ],
    )
    if type(session_message_count) is not int or session_message_count < 0:
        raise AuditError("Hermes session export message count is invalid")

    hermes_run_id = _same(
        "hermes_run_id",
        [
            metadata.get("hermes_run_id"),
            driver_result.get("run_id"),
        ],
    )
    hermes_session_id = _same(
        "hermes_session_id",
        [
            metadata.get("hermes_session_id"),
            driver_result.get("session_id"),
        ],
    )
    if (
        not isinstance(hermes_run_id, str)
        or not hermes_run_id
        or not isinstance(hermes_session_id, str)
        or not hermes_session_id
        or metadata.get("driver_session_id_consistent") is not True
    ):
        raise AuditError("Hermes trajectory has invalid run or session identity")

    trajectory_path = (
        result_path.parent / "agent" / "hermes-run-events.jsonl"
    )
    session_path = result_path.parent / "agent" / "hermes-session.jsonl"
    if capture_status == "failed":
        if not isinstance(capture_error, str) or not capture_error:
            raise AuditError("failed Hermes trajectory capture has no error")
        if event_stream_status == "saved":
            trajectory_sha256 = _require_sha256(
                trajectory_sha256, "trajectory_capture_sha256"
            )
            if (
                trajectory_event_count <= 0
                or submitted_count != 1
                or terminal_event_count != 1
                or terminal_event
                not in {
                    "run.completed",
                    "run.failed",
                    "run.cancelled",
                    "run.timed_out",
                }
                or terminal_event_source not in {"hermes", "driver"}
            ):
                raise AuditError(
                    "saved Hermes event stream metadata is inconsistent"
                )
        elif (
            trajectory_sha256 is not None
            or trajectory_event_count != 0
            or submitted_count != 0
            or terminal_event_count != 0
            or terminal_event is not None
            or terminal_event_source is not None
            or terminal_reason is not None
        ):
            raise AuditError("failed Hermes event stream metadata is inconsistent")

        if session_export_status == "saved":
            session_sha256 = _require_sha256(
                session_sha256, "trajectory_session_sha256"
            )
            if (
                session_id != hermes_session_id
                or session_message_count <= 0
            ):
                raise AuditError(
                    "saved Hermes session export metadata is inconsistent"
                )
        elif (
            session_sha256 is not None
            or session_id is not None
            or session_message_count != 0
        ):
            raise AuditError("failed Hermes session export metadata is inconsistent")
        if event_stream_status == "saved" and session_export_status == "saved":
            raise AuditError(
                "failed Hermes trajectory capture has no failed component"
            )
    else:
        if capture_error is not None:
            raise AuditError("saved Hermes trajectory capture records an error")
        if (
            event_stream_status != "saved"
            or session_export_status != "saved"
            or trajectory_event_count <= 0
            or submitted_count != 1
            or terminal_event_count != 1
            or session_id != hermes_session_id
            or session_message_count <= 0
        ):
            raise AuditError(
                "Hermes required trajectory is not recorded as saved"
            )
        trajectory_sha256 = _require_sha256(
            trajectory_sha256, "trajectory_capture_sha256"
        )
        session_sha256 = _require_sha256(
            session_sha256, "trajectory_session_sha256"
        )
        try:
            trajectory_bytes = trajectory_path.read_bytes()
        except OSError as exc:
            raise AuditError(
                f"cannot validate Hermes streaming trajectory "
                f"{trajectory_path}: {exc}"
            ) from exc
        if hashlib.sha256(trajectory_bytes).hexdigest() != trajectory_sha256:
            raise AuditError(
                "Hermes streaming trajectory SHA-256 does not match the ledger"
            )
        try:
            event_summary = validate_run_event_stream(
                trajectory_path,
                run_id=hermes_run_id,
                session_id=hermes_session_id,
            )
        except RuntimeError as exc:
            raise AuditError(
                f"invalid Hermes streaming trajectory: {exc}"
            ) from exc
        terminal_event = _same(
            "trajectory_terminal_event",
            [
                terminal_event,
                driver_result.get("stream_terminal_event"),
                event_summary["terminal_event"],
            ],
        )
        event_terminal_source = event_summary["terminal_event_source"]
        driver_terminal_source = driver_result.get(
            "stream_terminal_event_source"
        )
        if terminal_event_source != event_terminal_source or (
            driver_terminal_source is not None
            and driver_terminal_source != event_terminal_source
        ):
            raise AuditError(
                "trajectory_terminal_event_source is inconsistent"
            )
        terminal_event_source = event_terminal_source
        event_terminal_reason = event_summary["terminal_reason"]
        driver_terminal_reason = driver_result.get("stream_terminal_reason")
        for recorded_reason in (terminal_reason, driver_terminal_reason):
            if (
                recorded_reason is not None
                and recorded_reason != event_terminal_reason
            ):
                raise AuditError("trajectory_terminal_reason is inconsistent")
        terminal_reason = event_terminal_reason
        driver_stream_event_count = driver_result.get("stream_event_count")
        if (
            event_summary["sha256"] != trajectory_sha256
            or event_summary["event_count"] != trajectory_event_count
            or type(driver_stream_event_count) is not int
            or driver_stream_event_count <= 0
            or event_summary["event_count"]
            not in {
                driver_stream_event_count,
                driver_stream_event_count + 1,
            }
            or event_summary["submitted_count"]
            != driver_result.get("stream_submitted_count")
            or event_summary["terminal_event_count"]
            != driver_result.get("stream_terminal_event_count")
            or terminal_event
            not in {
                "run.completed",
                "run.failed",
                "run.cancelled",
                "run.timed_out",
            }
            or terminal_event_source not in {"hermes", "driver"}
            or (
                terminal_event == "run.timed_out"
                and driver_result.get("status") != "timed_out"
            )
        ):
            raise AuditError(
                "Hermes streaming trajectory evidence is internally inconsistent"
            )
        try:
            session_summary = validate_session_export(
                session_path,
                session_id=hermes_session_id,
            )
        except RuntimeError as exc:
            raise AuditError(f"invalid Hermes session export: {exc}") from exc
        if (
            session_summary["sha256"] != session_sha256
            or session_summary["session_id"] != session_id
            or session_summary["message_count"] != session_message_count
        ):
            raise AuditError(
                "Hermes session export evidence is internally inconsistent"
            )
    return {
        "path": str(artifact_path),
        "sha256": policy_sha256,
        "read_only_mount": True,
        "environment": {
            "path": str(env_artifact_path),
            "sha256": env_sha256,
            "read_only_managed_overlay": True,
        },
        "policy_guard": {
            "path": str(guard_artifact_path),
            "sha256": guard_sha256,
            "gateway_pid": driver_result.get("gateway_pid"),
        },
        "trajectory": {
            "path": str(trajectory_path),
            "status": capture_status,
            "complete": capture_status == "saved",
            "blocking": capture_blocking,
            "error": capture_error,
            "event_stream_status": event_stream_status,
            "sha256": trajectory_sha256,
            "event_count": trajectory_event_count,
            "run_id": hermes_run_id,
            "session_id": hermes_session_id,
            "terminal_event": terminal_event,
            "terminal_event_source": terminal_event_source,
            "terminal_reason": terminal_reason,
            "session_export": {
                "path": str(session_path),
                "status": session_export_status,
                "sha256": session_sha256,
                "message_count": session_message_count,
            },
        },
    }


def _validate_pi_trajectory(
    result_path: Path,
    metadata: dict[str, Any],
    started: dict[str, Any],
    completed: dict[str, Any],
) -> dict[str, Any] | None:
    if started.get("product") != "pi":
        return None
    if (
        started.get("product_version") != "0.73.1"
        or started.get("model_name") != "zai/glm-5.2"
        or metadata.get("pi_version") != "0.73.1"
    ):
        raise AuditError("Pi version/model is outside the frozen cohort")
    if (
        started.get("trajectory_capture_required") is not True
        or started.get("trajectory_capture_mode")
        != "pi_jsonl_and_saved_session"
        or started.get("trajectory_capture_blocking") is not False
    ):
        raise AuditError("Pi trajectory capture is not armed correctly")
    models_sha256 = _same(
        "pi_models_sha256",
        [metadata.get("pi_models_sha256"), started.get("pi_models_sha256")],
    )
    _require_sha256(models_sha256, "pi_models_sha256")
    trajectory_status = _same(
        "pi_trajectory_status",
        [
            metadata.get("pi_trajectory_status"),
            completed.get("pi_trajectory_status"),
        ],
    )
    trajectory_sha256 = _same(
        "pi_trajectory_sha256",
        [
            metadata.get("pi_trajectory_sha256"),
            completed.get("pi_trajectory_sha256"),
        ],
    )
    session_id = _same(
        "pi_session_id",
        [metadata.get("pi_session_id"), completed.get("pi_session_id")],
    )
    session_sha256 = _same(
        "pi_session_sha256",
        [
            metadata.get("pi_session_sha256"),
            completed.get("pi_session_sha256"),
        ],
    )
    provider_model_verified = _same(
        "pi_provider_model_verified",
        [
            metadata.get("pi_provider_model_verified"),
            completed.get("pi_provider_model_verified"),
        ],
    )
    event_path = result_path.parent / "agent" / "pi.txt"
    if trajectory_status == "failed":
        capture_error = _same(
            "pi_trajectory_error",
            [
                metadata.get("pi_trajectory_error"),
                completed.get("pi_trajectory_error"),
            ],
        )
        if not isinstance(capture_error, str) or not capture_error:
            raise AuditError("failed Pi trajectory capture has no error")
        trajectory_sha256 = _require_sha256(
            trajectory_sha256, "pi_trajectory_sha256"
        )
        try:
            event_bytes = event_path.read_bytes()
        except OSError as exc:
            raise AuditError(f"cannot validate Pi raw event stream: {exc}") from exc
        if hashlib.sha256(event_bytes).hexdigest() != trajectory_sha256:
            raise AuditError("Pi raw event stream SHA-256 does not match the ledger")
        if provider_model_verified is not False:
            raise AuditError("failed Pi trajectory incorrectly verifies provider/model")
        return {
            "path": str(event_path),
            "status": "failed",
            "sha256": trajectory_sha256,
            "error": capture_error,
            "blocking": False,
        }
    if trajectory_status != "saved" or provider_model_verified is not True:
        raise AuditError("Pi trajectory/provider/model verification failed")
    trajectory_sha256 = _require_sha256(
        trajectory_sha256, "pi_trajectory_sha256"
    )
    session_sha256 = _require_sha256(session_sha256, "pi_session_sha256")
    if not isinstance(session_id, str) or not session_id:
        raise AuditError("Pi trajectory has no session identity")

    try:
        event_summary = validate_pi_event_stream(
            event_path,
            expected_provider="zai",
            expected_model="glm-5.2",
        )
    except (OSError, RuntimeError) as exc:
        raise AuditError(f"invalid Pi event stream: {exc}") from exc
    if (
        event_summary["sha256"] != trajectory_sha256
        or event_summary["session_id"] != session_id
        or event_summary["event_count"] != metadata.get("pi_event_count")
        or event_summary["stop_reason"]
        != metadata.get("pi_final_stop_reason")
    ):
        raise AuditError("Pi event stream evidence is internally inconsistent")

    matching_sessions: list[dict[str, Any]] = []
    session_root = result_path.parent / "agent" / "pi-sessions"
    for path in sorted(session_root.rglob("*.jsonl")):
        try:
            matching_sessions.append(
                validate_pi_session(path, session_id=session_id)
            )
        except (OSError, RuntimeError):
            continue
    if len(matching_sessions) != 1:
        raise AuditError("Pi must preserve exactly one matching saved session")
    session_summary = matching_sessions[0]
    if (
        session_summary["sha256"] != session_sha256
        or session_summary["entry_count"]
        != metadata.get("pi_session_entry_count")
    ):
        raise AuditError("Pi saved session evidence is internally inconsistent")
    return {
        "path": str(event_path),
        "status": trajectory_status,
        "sha256": trajectory_sha256,
        "event_count": event_summary["event_count"],
        "session_id": session_id,
        "session_sha256": session_sha256,
        "stop_reason": event_summary["stop_reason"],
        "provider": event_summary["provider"],
        "model": event_summary["model"],
        "blocking": False,
    }


def _validate_dsh_trajectory(
    result_path: Path,
    metadata: dict[str, Any],
    started: dict[str, Any],
    completed: dict[str, Any],
) -> dict[str, Any] | None:
    if started.get("product") != "dsh":
        return None
    if (
        started.get("trajectory_capture_required") is not True
        or started.get("trajectory_capture_mode")
        != "dsh_jsonrpc_event_stream"
        or metadata.get("trajectory_capture_path")
        != "agent/dsh-events.jsonl"
    ):
        raise AuditError("DSH required trajectory capture is not armed")
    blocking = _same(
        "trajectory_capture_blocking",
        [
            metadata.get("trajectory_capture_blocking"),
            started.get("trajectory_capture_blocking"),
            completed.get("trajectory_capture_blocking"),
        ],
    )
    if blocking is not False:
        raise AuditError("DSH trajectory capture is incorrectly blocking")
    status = _same(
        "dsh_trajectory_status",
        [
            metadata.get("dsh_trajectory_status"),
            completed.get("dsh_trajectory_status"),
        ],
    )
    trajectory_sha256 = _same(
        "dsh_trajectory_sha256",
        [
            metadata.get("dsh_trajectory_sha256"),
            completed.get("dsh_trajectory_sha256"),
        ],
    )
    session_id = _same(
        "dsh_session_id",
        [metadata.get("dsh_session_id"), completed.get("dsh_session_id")],
    )
    event_count = _same(
        "dsh_event_count",
        [metadata.get("dsh_event_count"), completed.get("dsh_event_count")],
    )
    finish_reason = _same(
        "dsh_finish_reason",
        [
            metadata.get("dsh_finish_reason"),
            completed.get("dsh_finish_reason"),
        ],
    )
    if status == "missing":
        if (
            trajectory_sha256 is not None
            or session_id is not None
            or finish_reason is not None
            or event_count != 0
        ):
            raise AuditError("missing DSH trajectory has inconsistent metadata")
        return {
            "path": None,
            "status": "missing",
            "complete": False,
            "blocking": False,
        }
    if status != "saved":
        raise AuditError("DSH trajectory status is invalid")
    if not isinstance(session_id, str) or not session_id:
        raise AuditError("saved DSH trajectory has no session ID")
    if type(event_count) is not int or event_count <= 0:
        raise AuditError("saved DSH trajectory has an invalid event count")
    trajectory_sha256 = _require_sha256(
        trajectory_sha256, "dsh_trajectory_sha256"
    )
    trajectory_path = result_path.parent / "agent" / "dsh-events.jsonl"
    try:
        summary = validate_dsh_event_stream(
            trajectory_path,
            session_id=session_id,
            max_turns=metadata.get("dsh_max_turns"),
        )
    except RuntimeError as exc:
        raise AuditError(f"invalid DSH trajectory: {exc}") from exc
    driver_result = _load_json(
        result_path.parent / "agent" / "dsh-run.json"
    )
    if (
        driver_result.get("schema_version") != 1
        or driver_result.get("status") != "completed"
        or driver_result.get("session_id") != session_id
        or driver_result.get("event_count") != event_count
        or driver_result.get("finish_reason") != finish_reason
        or summary["sha256"] != trajectory_sha256
        or summary["event_count"] != event_count
        or summary["finish_reason"] != finish_reason
    ):
        raise AuditError("DSH trajectory evidence is internally inconsistent")
    return {
        "path": str(trajectory_path),
        "status": "saved",
        "complete": True,
        "blocking": False,
        "sha256": trajectory_sha256,
        "session_id": session_id,
        "event_count": event_count,
        "wire_message_count": summary["wire_message_count"],
        "finish_reason": finish_reason,
    }


def audit_trial(result_path: Path) -> dict[str, Any]:
    """Audit one persisted Harbor C0 trial without executing the product."""

    result_path = result_path.resolve()
    result = _load_json(result_path)
    metadata = _metadata(result)
    ledger_path = result_path.parent / "agent" / "controller.jsonl"
    rows = _load_rows(ledger_path)
    run_id = _validate_envelope(rows)
    _validate_no_fault_events(rows)

    started = _one(rows, "controller_started")
    armed = _one(rows, "trigger_armed")
    product_started = _one(rows, "product_turn_started")
    lifecycle_started = _one(rows, "lifecycle_controller_started")
    process_cleanup = _one(rows, "product_process_cleanup")
    product_exited = _one(rows, "product_turn_exited")
    completed = _one(rows, "controller_completed")

    if rows[0] is not started or rows[-1] is not completed:
        raise AuditError("controller_started/controller_completed must bound the ledger")

    observed = _at_most_one(rows, "trigger_observed")
    no_hit = _at_most_one(rows, "trigger_no_hit")
    if (observed is None) == (no_hit is None):
        raise AuditError(
            "ledger must contain exactly one trigger_observed or trigger_no_hit event"
        )
    terminal_trigger = observed if observed is not None else no_hit
    assert terminal_trigger is not None

    registered = _at_most_one(rows, "product_process_registered")
    if registered is not None:
        pid = registered.get("pid")
        supervisor_pid = registered.get("supervisor_pid")
        if (
            type(pid) is not int
            or pid <= 1
            or type(supervisor_pid) is not int
            or supervisor_pid <= 1
            or registered.get("ppid") != supervisor_pid
            or registered.get("pgid") != pid
            or registered.get("sid") != pid
            or type(registered.get("start_ticks")) is not int
            or registered["start_ticks"] <= 0
        ):
            raise AuditError("registered product process identity is unsafe")
        executable = registered.get("exe")
        if not isinstance(executable, str) or not executable.startswith("/"):
            raise AuditError("registered product executable is not absolute")
        _require_sha256(registered.get("identity_sha256"), "identity_sha256")
        _require_sha256(registered.get("cgroup_sha256"), "cgroup_sha256")

    ordered = [started, armed, product_started, lifecycle_started]
    if registered is not None:
        ordered.append(registered)
    ordered.extend([product_exited, completed])
    _assert_order(ordered)
    for event in (terminal_trigger, process_cleanup):
        if not lifecycle_started["sequence"] < event["sequence"] < product_exited["sequence"]:
            raise AuditError(
                f"{event['event']} is outside the active product phase"
            )

    if (
        process_cleanup.get("fault_action") is not False
        or process_cleanup.get("zero_live_proven") is not True
        or process_cleanup.get("remaining_pids_count") != 0
    ):
        raise AuditError("product cleanup does not prove a fault-free zero-live state")
    cleanup_report_sha256 = _require_sha256(
        process_cleanup.get("cleanup_report_sha256"),
        "cleanup_report_sha256",
    )
    cleanup_report_path = result_path.parent / "agent" / "product.cleanup.json"
    try:
        cleanup_report_raw = cleanup_report_path.read_text(encoding="utf-8")
        cleanup_report = parse_process_cleanup_report(
            cleanup_report_raw
        )
    except (OSError, LifecycleControllerError) as exc:
        raise AuditError(
            f"cannot validate process cleanup artifact {cleanup_report_path}: {exc}"
        ) from exc
    artifact_cleanup_sha256 = hashlib.sha256(
        cleanup_report_raw.encode()
    ).hexdigest()
    if artifact_cleanup_sha256 != cleanup_report_sha256:
        raise AuditError("process cleanup artifact SHA-256 does not match the ledger")
    product_terminal_status = _same(
        "product_terminal_status",
        [
            metadata.get("product_terminal_status"),
            cleanup_report.get("product_terminal_status"),
            process_cleanup.get("product_terminal_status"),
            product_exited.get("product_terminal_status"),
            completed.get("product_terminal_status"),
        ],
    )
    if product_terminal_status not in {
        "completed",
        "failed",
        "timeout",
        "cancelled",
    }:
        raise AuditError("product_terminal_status is invalid")
    if metadata.get("product_cleanup_zero_live_proven") is not True:
        raise AuditError("metadata does not preserve the zero-live cleanup proof")
    if completed.get("product_cleanup_zero_live_proven") is not True:
        raise AuditError("controller completion does not preserve zero-live cleanup")
    if metadata.get("product_cleanup_report_sha256") != cleanup_report_sha256:
        raise AuditError("metadata and cleanup report SHA-256 disagree")
    astra_trajectory = _validate_astra_trajectory(
        result_path,
        metadata,
        started,
        completed,
        rows,
    )
    managed_policy = _validate_hermes_managed_policy(
        result_path,
        metadata,
        started,
        completed,
        product_terminal_status,
    )
    pi_trajectory = _validate_pi_trajectory(
        result_path,
        metadata,
        started,
        completed,
    )
    dsh_trajectory = _validate_dsh_trajectory(
        result_path,
        metadata,
        started,
        completed,
    )

    preflights = _events(rows, "product_preflight")
    if not preflights:
        raise AuditError("ledger has no successful product preflight")
    for preflight in preflights:
        if not started["sequence"] < preflight["sequence"] < armed["sequence"]:
            raise AuditError("product_preflight is outside the preflight phase")
        if (
            preflight.get("passed") is not True
            or type(preflight.get("return_code")) is not int
            or preflight["return_code"] != 0
        ):
            raise AuditError("product_preflight did not pass")

    if metadata.get("condition") != "C0":
        raise AuditError("trial metadata condition is not C0")
    if started.get("condition") != "C0":
        raise AuditError("controller_started condition is not C0")
    if lifecycle_started.get("condition") != "C0":
        raise AuditError("lifecycle controller condition is not C0")
    if metadata.get("fault_action") != "noop":
        raise AuditError("trial metadata did not register the C0 no-op")
    if started.get("fault_action") != "noop":
        raise AuditError("controller_started did not register the C0 no-op")
    if lifecycle_started.get("fault_action") != "noop":
        raise AuditError("lifecycle controller did not register the C0 no-op")
    if metadata.get("fault_injected") is not False:
        raise AuditError("trial metadata does not record fault_injected=false")
    if completed.get("fault_injected") is not False:
        raise AuditError("controller completion does not record fault_injected=false")

    task_id = _same(
        "task_id",
        [
            metadata.get("task_id"),
            started.get("task_id"),
            armed.get("task_id"),
            lifecycle_started.get("task_id"),
        ],
    )
    predicate_id = _same(
        "predicate_id",
        [
            metadata.get("trigger_id"),
            started.get("trigger_id"),
            armed.get("predicate_id"),
            lifecycle_started.get("predicate_id"),
        ],
    )
    if not isinstance(task_id, str) or not task_id:
        raise AuditError("task_id must be a non-empty string")
    if not isinstance(predicate_id, str) or not predicate_id:
        raise AuditError("predicate_id must be a non-empty string")

    stable_observations = lifecycle_started.get("stable_observations")
    if type(stable_observations) is not int or stable_observations < 2:
        raise AuditError("stable_observations must be an integer of at least 2")

    manifest_sha256 = _same(
        "trigger_manifest_sha256",
        [
            metadata.get("trigger_manifest_sha256"),
            started.get("trigger_manifest_sha256"),
            armed.get("trigger_manifest_sha256"),
            lifecycle_started.get("trigger_manifest_sha256"),
        ],
    )
    manifest_sha256 = _require_sha256(
        manifest_sha256, "trigger_manifest_sha256"
    )
    expected_manifest_sha256 = _manifest_sha256(
        task_id=task_id,
        predicate_id=predicate_id,
        stable_observations=stable_observations,
    )
    if manifest_sha256 != expected_manifest_sha256:
        raise AuditError("trigger manifest SHA-256 does not match its ledger fields")

    predicate_probe_sha256 = _same(
        "predicate_probe_sha256",
        [
            metadata.get("predicate_probe_sha256"),
            started.get("predicate_probe_sha256"),
            lifecycle_started.get("predicate_probe_source_sha256"),
        ],
    )
    predicate_probe_sha256 = _require_sha256(
        predicate_probe_sha256, "predicate_probe_sha256"
    )

    metadata_trigger_hit = metadata.get("trigger_hit")
    completed_trigger_hit = completed.get("trigger_hit")
    if type(metadata_trigger_hit) is not bool:
        raise AuditError("trial metadata trigger_hit must be boolean")
    if completed_trigger_hit is not metadata_trigger_hit:
        raise AuditError("metadata and controller trigger_hit disagree")

    metadata_gate = metadata.get("lifecycle_gate_passed")
    completed_gate = completed.get("lifecycle_gate_passed")
    if type(metadata_gate) is not bool:
        raise AuditError("trial metadata lifecycle_gate_passed must be boolean")
    if completed_gate is not metadata_gate:
        raise AuditError("metadata and controller lifecycle gate disagree")

    actions = _events(rows, "fault_action")
    trigger_reason: str
    evidence_sha256: str | None
    if observed is not None:
        if registered is None:
            raise AuditError("trigger hit has no registered product process")
        if metadata_trigger_hit is not True or metadata_gate is not True:
            raise AuditError("trigger hit must set lifecycle_gate_passed=true")
        if len(actions) != 1:
            raise AuditError(
                f"trigger hit requires exactly one no-op action, found {len(actions)}"
            )
        action = actions[0]
        if action.get("action") != "noop" or action.get("executed") is not True:
            raise AuditError("trigger hit did not execute exactly the registered no-op")
        action_fields = set(action) - _LEDGER_ENVELOPE_FIELDS
        if action_fields != {"action", "executed"}:
            raise AuditError("C0 no-op event contains unexpected fault-action evidence")
        _assert_order([observed, action, product_exited])

        _same(
            "observed task_id",
            [task_id, observed.get("task_id")],
        )
        _same(
            "observed predicate_id",
            [predicate_id, observed.get("predicate_id")],
        )
        observed_stability = observed.get("stable_observations")
        if (
            type(observed_stability) is not int
            or observed_stability < stable_observations
        ):
            raise AuditError("trigger observation is not stable")
        evidence = observed.get("evidence")
        if not isinstance(evidence, dict):
            raise AuditError("trigger evidence must be a JSON object")
        canonical_evidence = json.dumps(
            evidence, sort_keys=True, separators=(",", ":")
        ).encode()
        expected_evidence_sha256 = hashlib.sha256(canonical_evidence).hexdigest()
        evidence_sha256 = _require_sha256(
            observed.get("evidence_sha256"), "trigger evidence_sha256"
        )
        if evidence_sha256 != expected_evidence_sha256:
            raise AuditError("trigger evidence SHA-256 does not match its payload")
        if metadata.get("trigger_evidence_sha256") != evidence_sha256:
            raise AuditError("metadata and ledger trigger evidence SHA-256 disagree")
        trigger_reason = "clean_noop"
        if metadata.get("trigger_reason") != trigger_reason:
            raise AuditError("metadata has an unexpected trigger-hit reason")
        audit_status = "pass"
        trigger_status = "hit"
    else:
        assert no_hit is not None
        if metadata_trigger_hit is not False or metadata_gate is not False:
            raise AuditError("trigger no-hit must set lifecycle_gate_passed=false")
        if actions:
            raise AuditError("trigger no-hit must not execute a fault action")
        trigger_reason_value = no_hit.get("reason")
        if not isinstance(trigger_reason_value, str) or not trigger_reason_value:
            raise AuditError("trigger_no_hit has no reason")
        trigger_reason = trigger_reason_value
        if metadata.get("trigger_reason") != trigger_reason:
            raise AuditError("metadata and ledger no-hit reason disagree")
        if metadata.get("trigger_evidence_sha256") is not None:
            raise AuditError("trigger no-hit unexpectedly records an evidence hash")
        evidence_sha256 = None
        audit_status = "no_hit"
        trigger_status = "no_hit"

    controller_ledger = _same(
        "controller_ledger",
        [
            metadata.get("controller_ledger"),
            started.get("controller_ledger"),
        ],
    )
    if not isinstance(controller_ledger, str) or not controller_ledger:
        raise AuditError("controller_ledger must be a non-empty string")

    return {
        "schema_version": 1,
        "audit_kind": "terminal_bench_c0",
        "audit_status": audit_status,
        "infrastructure_failure": False,
        "condition": "C0",
        "lifecycle_gate_passed": metadata_gate,
        "trial": {
            "id": result.get("id"),
            "name": result.get("trial_name"),
            "task_name": result.get("task_name"),
            "result_path": str(result_path),
            "controller_ledger": str(ledger_path),
        },
        "controller": {
            "run_id": run_id,
            "event_count": len(rows),
        },
        "product": {
            "terminal_status": product_terminal_status,
            "cleanup_zero_live_proven": True,
            "cleanup_report_sha256": cleanup_report_sha256,
            "cleanup_report": str(cleanup_report_path),
            "trajectory": astra_trajectory,
            "managed_policy": managed_policy,
            "pi_trajectory": pi_trajectory,
            "dsh_trajectory": dsh_trajectory,
        },
        "trigger": {
            "status": trigger_status,
            "reason": trigger_reason,
            "task_id": task_id,
            "predicate_id": predicate_id,
            "manifest_sha256": manifest_sha256,
            "predicate_probe_sha256": predicate_probe_sha256,
            "evidence_sha256": evidence_sha256,
        },
        "upstream": _upstream(result),
    }


def _looks_like_trial_result(
    path: Path, payload: dict[str, Any] | None
) -> bool:
    if (path.parent / "agent" / "controller.jsonl").is_file():
        return True
    if not isinstance(payload, dict) or not payload.get("trial_name"):
        return False
    try:
        if _metadata(payload).get("condition") == "C0":
            return True
    except AuditError:
        pass
    config = payload.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    agent_name = agent.get("name") if isinstance(agent, dict) else None
    return isinstance(agent_name, str) and "c0" in agent_name.lower()


def discover_trials(target: Path) -> list[Path]:
    """Find Harbor trial result files under a job run or jobs root."""

    target = target.expanduser().resolve()
    if not target.exists():
        raise AuditError(f"audit target does not exist: {target}")

    if target.is_file():
        candidates = [target] if target.name == "result.json" else []
    else:
        candidates = sorted(target.rglob("result.json"))

    trial_results: list[Path] = []
    for path in candidates:
        payload: dict[str, Any] | None
        try:
            payload = _load_json(path)
        except AuditError:
            payload = None
        if _looks_like_trial_result(path, payload):
            trial_results.append(path)

    if not trial_results:
        raise AuditError(f"no Harbor C0 trial results found below {target}")
    return trial_results


def _best_effort_upstream(result_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result = _load_json(result_path)
    except AuditError:
        return {}, {"verifier_rewards": None, "exception_info": None}
    return result, _upstream(result)


def _infra_report(result_path: Path, error: AuditError) -> dict[str, Any]:
    result, upstream = _best_effort_upstream(result_path)
    return {
        "schema_version": 1,
        "audit_kind": "terminal_bench_c0",
        "audit_status": "infra_error",
        "infrastructure_failure": True,
        "condition": "C0",
        "lifecycle_gate_passed": False,
        "trial": {
            "id": result.get("id"),
            "name": result.get("trial_name"),
            "task_name": result.get("task_name"),
            "result_path": str(result_path.resolve()),
            "controller_ledger": str(
                (result_path.parent / "agent" / "controller.jsonl").resolve()
            ),
        },
        "controller": None,
        "trigger": None,
        "upstream": upstream,
        "audit_error": str(error),
    }


def _reward_summary(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, list[float]] = {}
    for report in reports:
        upstream = report.get("upstream")
        rewards = (
            upstream.get("verifier_rewards")
            if isinstance(upstream, dict)
            else None
        )
        if not isinstance(rewards, dict):
            continue
        for name, value in rewards.items():
            if (
                isinstance(name, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ):
                values.setdefault(name, []).append(float(value))
    return {
        name: {
            "count": len(metric_values),
            "mean": sum(metric_values) / len(metric_values),
            "values": metric_values,
        }
        for name, metric_values in sorted(values.items())
    }


def _write_sidecar(report: dict[str, Any], result_path: Path) -> None:
    sidecar_path = result_path.parent / "c0-audit.json"
    temporary_path = result_path.parent / ".c0-audit.json.tmp"
    try:
        temporary_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(sidecar_path)
    except OSError as exc:
        raise AuditError(f"cannot write sidecar {sidecar_path}: {exc}") from exc


def audit_target(target: Path, *, write: bool = False) -> dict[str, Any]:
    result_paths = discover_trials(target)
    reports: list[dict[str, Any]] = []
    for result_path in result_paths:
        try:
            report = audit_trial(result_path)
        except AuditError as exc:
            report = _infra_report(result_path, exc)
        reports.append(report)
        if write:
            _write_sidecar(report, result_path)

    n_infra = sum(
        report["infrastructure_failure"] is True for report in reports
    )
    n_no_hit = sum(report["audit_status"] == "no_hit" for report in reports)
    n_pass = sum(report["audit_status"] == "pass" for report in reports)
    n_upstream_exceptions = sum(
        isinstance(report.get("upstream"), dict)
        and report["upstream"].get("exception_info") is not None
        for report in reports
    )
    if n_infra:
        overall_status = "infra_error"
    elif n_no_hit:
        overall_status = "gate_incomplete"
    else:
        overall_status = "pass"

    return {
        "schema_version": 1,
        "audit_kind": "terminal_bench_c0_job",
        "target": str(target.expanduser().resolve()),
        "write_sidecars": write,
        "status": overall_status,
        "summary": {
            "n_trials": len(reports),
            "n_pass": n_pass,
            "n_no_hit": n_no_hit,
            "n_lifecycle_gate_passed": n_pass,
            "n_infrastructure_failures": n_infra,
            "n_upstream_exceptions": n_upstream_exceptions,
            "verifier_rewards": _reward_summary(reports),
        },
        "trials": reports,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit persisted Harbor C0 lifecycle evidence offline"
    )
    parser.add_argument(
        "target",
        type=Path,
        help="Harbor job run directory, jobs root, or trial result.json",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write c0-audit.json beside each trial result (default: read-only)",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_target(args.target, write=args.write)
    except AuditError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["summary"]["n_infrastructure_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
