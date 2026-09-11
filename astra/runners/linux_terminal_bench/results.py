from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from astra.runners.pi_terminal_bench.verifier_evidence import (
    VERIFIER_INFRA_EXCEPTION_TYPES,
    VerifierEvidenceError,
    validate_binary_reward,
    validate_ctrf_report,
)

from .products import Product


RESULT_FIELDS = (
    "task",
    "raw_reward",
    "reward",
    "verifier_status",
    "verifier_test_count",
    "verifier_evidence_error",
    "c0_audit_status",
    "c0_audit_error",
    "product_terminal_status",
    "finish_reason",
    "trigger_scope",
    "trigger_hit",
    "cleanup_zero_live_proven",
    "product_timeout_multiplier",
    "product_timeout_sec",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "trial_exception_type",
    "finished_at",
    "result_path",
)


def belongs_to_cohort(result: dict[str, Any], product: Product) -> bool:
    config = result.get("config") or {}
    agent = config.get("agent") or {}
    kwargs = agent.get("kwargs") or {}
    return (
        config.get("install_only") is not True
        and agent.get("name") == product.agent
        and (not product.match_model or agent.get("model_name") == product.model)
        and all(kwargs.get(key) == value for key, value in product.required_kwargs.items())
    )


def latest_results(
    jobs_dir: Path,
    expected_tasks: set[str],
    product: Product,
) -> dict[str, tuple[dict[str, Any], Path]]:
    latest: dict[str, tuple[dict[str, Any], Path]] = {}
    if not jobs_dir.is_dir():
        return latest
    for path in jobs_dir.glob("*/*/result.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            task = result["task_name"].rsplit("/", 1)[-1]
            finished_at = result.get("finished_at")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if task not in expected_tasks or not finished_at or not belongs_to_cohort(result, product):
            continue
        candidate = (result, path)
        previous = latest.get(task)
        if previous is None or (str(finished_at), str(path)) > (
            str(previous[0].get("finished_at") or ""),
            str(previous[1]),
        ):
            latest[task] = candidate
    return latest


def verifier_status(
    result: dict[str, Any],
    result_path: Path,
) -> tuple[str, float | None, int, str]:
    metadata = ((result.get("agent_result") or {}).get("metadata") or {})
    if (
        metadata.get("product") == "dsh"
        and metadata.get("dsh_finish_reason") == "error"
    ):
        return "agent_infra_failure", None, 0, "dsh_finish_reason=error"
    exception = result.get("exception_info")
    exception_type = exception.get("exception_type") if isinstance(exception, dict) else None
    if exception_type in VERIFIER_INFRA_EXCEPTION_TYPES:
        return "verifier_infra_failure", None, 0, str(exception_type)
    reward_value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if exception_type == "CancelledError" and reward_value is None:
        return "execution_incomplete", None, 0, "run cancelled before a verifier result was available"
    try:
        reward = validate_binary_reward(reward_value)
        evidence = validate_ctrf_report(result_path.parent / "verifier" / "ctrf.json")
    except VerifierEvidenceError as exc:
        return "verifier_infra_failure", None, 0, str(exc)
    return ("passed" if reward == 1.0 else "failed", reward, evidence["test_count"], "")


def completed_tasks(
    jobs_dir: Path,
    expected_tasks: set[str],
    product: Product,
) -> set[str]:
    return {
        task
        for task, (result, path) in latest_results(jobs_dir, expected_tasks, product).items()
        if verifier_status(result, path)[0] in {"passed", "failed"}
    }


def _audit_status(path: Path) -> tuple[str, str]:
    try:
        from astra.runners.lifecycle_c0.audit import AuditError, audit_trial

        result = audit_trial(path)
        return str(result["audit_status"]), ""
    except (ImportError, KeyError) as exc:
        return "infra_error", str(exc)
    except AuditError as exc:
        return "infra_error", str(exc)


def result_row(task: str, result: dict[str, Any], path: Path) -> dict[str, Any]:
    status, reward, test_count, evidence_error = verifier_status(result, path)
    agent_result = result.get("agent_result") or {}
    metadata = agent_result.get("metadata") or {}
    audit_status, audit_error = _audit_status(path)
    exception = result.get("exception_info")
    return {
        "task": task,
        "raw_reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
        "reward": reward,
        "verifier_status": status,
        "verifier_test_count": test_count,
        "verifier_evidence_error": evidence_error,
        "c0_audit_status": audit_status,
        "c0_audit_error": audit_error,
        "product_terminal_status": metadata.get("product_terminal_status"),
        "finish_reason": (
            metadata.get("pi_final_stop_reason")
            or metadata.get("dsh_finish_reason")
            or metadata.get("finish_reason")
        ),
        "trigger_scope": metadata.get("trigger_scope"),
        "trigger_hit": metadata.get("trigger_hit"),
        "cleanup_zero_live_proven": metadata.get("product_cleanup_zero_live_proven"),
        "product_timeout_multiplier": metadata.get("product_timeout_multiplier"),
        "product_timeout_sec": metadata.get("product_timeout_sec"),
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cache_tokens": agent_result.get("n_cache_tokens"),
        "trial_exception_type": (
            exception.get("exception_type") if isinstance(exception, dict) else None
        ),
        "finished_at": result.get("finished_at"),
        "result_path": str(path.resolve()),
    }


def summarize(
    *,
    product: Product,
    jobs_dir: Path,
    tasks: list[str],
    output_dir: Path,
    dataset_commit: str,
) -> dict[str, Any]:
    latest = latest_results(jobs_dir, set(tasks), product)
    rows = [result_row(task, *latest[task]) for task in tasks if task in latest]
    completed = {
        row["task"]
        for row in rows
        if row["verifier_status"] in {"passed", "failed"}
    }
    pending = [task for task in tasks if task not in completed]
    valid_rows = [row for row in rows if row["reward"] is not None]
    summary = {
        "schema_version": 1,
        "platform": "linux",
        "condition": "C0",
        "product": product.id,
        "model": product.model,
        "dataset_commit": dataset_commit,
        "product_timeout_multiplier": 1.0,
        "expected_tasks": len(tasks),
        "recorded_tasks": len(rows),
        "valid_verifier_tasks": len(valid_rows),
        "passed_tasks": sum(row["reward"] == 1.0 for row in valid_rows),
        "failed_tasks": sum(row["reward"] == 0.0 for row in valid_rows),
        "verifier_infra_failure_tasks": sum(
            row["verifier_status"] == "verifier_infra_failure" for row in rows
        ),
        "execution_incomplete_tasks": sum(
            row["verifier_status"] == "execution_incomplete" for row in rows
        ),
        "pending_tasks": len(pending),
        "mean_reward": (
            sum(float(row["reward"]) for row in valid_rows) / len(valid_rows)
            if valid_rows
            else None
        ),
        "pending": pending,
        "results": rows,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "summary.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_dir / "summary.json")
    temporary_csv = output_dir / "summary.csv.tmp"
    with temporary_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary_csv.replace(output_dir / "summary.csv")
    (output_dir / "pending.queue.txt").write_text(
        "".join(f"{task}\n" for task in pending), encoding="utf-8"
    )
    return summary
