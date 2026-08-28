#!/usr/bin/env python3
"""Produce a read-only health snapshot for a serial competitor evaluation.

The monitor is deliberately separate from the runner and campaign
orchestrator.  It consumes their append-only artifacts, checks the fixed
denominator and sequence, and never starts services or calls a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PLATFORM_ORDER = ("moi_local", "dify_local", "fastgpt_local", "maxkb_local")
EXPECTED_PROVIDER = "hybrid"
EXPECTED_TEXT_PROVIDER = "deepseek-official"
EXPECTED_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
EXPECTED_MAAS_BASE_URL = "https://api.modelarts-maas.com/v1"
EXPECTED_LLM = "deepseek-v4-flash"
EXPECTED_EMBEDDING = "bge-m3"
EXPECTED_REQUEST_MODELS = frozenset({EXPECTED_LLM, EXPECTED_EMBEDDING})
EXPECTED_EMBEDDING_DIMENSION = 1024
EXPECTED_VL_MODEL = "NOT_APPLICABLE"
MONITOR_SCHEMA = "moi-rag-bench-monitor-v1"
CAMPAIGN_SCHEMA = "competitor-eval-campaign-v1"
SEQUENCE_SCHEMA = "moi-rag-bench-serial-sequence-v1"
TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA = "moi-rag-bench-text-only-semantic-audit-v1"
PROVENANCE_SCHEMA = "competitor-eval-provenance-v1"
POSTPROCESS_CONTRACT_SCHEMA = "competitor-eval-postprocess-contract-v1"
EXPECTED_POSTPROCESS_ORDER = [
    "runner",
    "provider-audit-runner",
    "judge",
    "provider-audit-final",
    "metrics",
]
EXPECTED_COMPLETION_GATES = [
    "runner_status=SUCCESS",
    "provider_audit_runner_status=PASS",
    "judge_status=COMPLETE",
    "provider_audit_final_status=PASS",
    "metrics_status=COMPLETE",
]


class MonitorError(RuntimeError):
    """A safe, operator-facing monitor error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"FILE_MISSING:{path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"JSON_INVALID:{path}") from exc


def _sha256_sidecar(path: Path) -> tuple[dict[str, Any], list[str]]:
    sidecar = path.with_name(path.name + ".sha256")
    summary: dict[str, Any] = {
        "path": str(path),
        "sidecar": str(sidecar),
        "status": "MISSING",
    }
    if not path.is_file():
        return summary, [f"ARTIFACT_MISSING:{path.name}"]
    if not sidecar.is_file():
        return summary, [f"SHA256_SIDECAR_MISSING:{path.name}"]
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    observed = sidecar.read_text(encoding="utf-8").strip()
    expected_line = f"{expected}  {path.name}"
    summary["expected"] = expected
    summary["status"] = "PASS" if observed == expected_line else "MISMATCH"
    if summary["status"] != "PASS":
        return summary, [f"SHA256_SIDECAR_MISMATCH:{path.name}"]
    return summary, []


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_provenance_path(repo_root: Path, raw_path: Any) -> Path | None:
    if raw_path in (None, ""):
        return None
    candidate = Path(str(raw_path)).expanduser()
    return (candidate if candidate.is_absolute() else repo_root / candidate).resolve()


def _provenance_summary(
    *,
    campaign: Mapping[str, Any],
    campaign_checkpoint: Path,
    package: Path,
    package_info: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate the immutable inputs recorded by the campaign planner.

    The git dirty snapshot is descriptive: P0/docs updates after planning are
    expected and must not invalidate an otherwise identical evaluation input.
    File fingerprints and the frozen evaluation policy are authoritative and
    fail closed when they drift.
    """

    raw = campaign.get("provenance")
    if not isinstance(raw, Mapping):
        return {"present": False, "status": "MISSING"}, ["CAMPAIGN_PROVENANCE_MISSING"]
    alerts: list[str] = []
    if raw.get("schema") != PROVENANCE_SCHEMA:
        alerts.append("CAMPAIGN_PROVENANCE_SCHEMA_INVALID")

    raw_repo_root = campaign.get("repo_root")
    repo_root = Path(str(raw_repo_root)).expanduser().resolve() if raw_repo_root else campaign_checkpoint.parent.resolve()
    summary: dict[str, Any] = {
        "present": True,
        "schema": raw.get("schema"),
        "status": "PASS",
        "captured_at": raw.get("captured_at"),
        "repo_root": str(repo_root),
        "git": raw.get("git") if isinstance(raw.get("git"), Mapping) else {},
        "package_records": [],
        "file_fingerprints": [],
        "evaluation_policy": raw.get("evaluation_policy") if isinstance(raw.get("evaluation_policy"), Mapping) else {},
    }

    package_records = raw.get("package_records")
    if not isinstance(package_records, list) or not package_records:
        alerts.append("CAMPAIGN_PROVENANCE_PACKAGE_RECORD_MISSING")
    else:
        dataset_id = package_info.get("dataset_id")
        matching = [
            item for item in package_records
            if isinstance(item, Mapping) and item.get("dataset_id") == dataset_id
        ]
        if not matching:
            alerts.append("CAMPAIGN_PROVENANCE_PACKAGE_MISMATCH")
        for item in package_records:
            if not isinstance(item, Mapping):
                alerts.append("CAMPAIGN_PROVENANCE_PACKAGE_RECORD_INVALID")
                continue
            counts = item.get("counts") if isinstance(item.get("counts"), Mapping) else {}
            observed = package_info.get("counts", {}).get("observed", {}) if isinstance(package_info.get("counts"), Mapping) else {}
            if any(counts.get(key) != observed.get(key) for key in ("documents", "questions", "gold")):
                alerts.append("CAMPAIGN_PROVENANCE_PACKAGE_COUNT_MISMATCH")
            manifest_record = item.get("manifest") if isinstance(item.get("manifest"), Mapping) else {}
            manifest_path = _resolve_provenance_path(repo_root, manifest_record.get("path"))
            expected_manifest = str(manifest_record.get("sha256") or "").strip().casefold()
            if manifest_path is not None and manifest_path != (package / "manifest.json").resolve():
                alerts.append("CAMPAIGN_PROVENANCE_PACKAGE_PATH_MISMATCH")
            if manifest_path is None or not manifest_path.is_file() or not expected_manifest:
                alerts.append("CAMPAIGN_PROVENANCE_MANIFEST_MISSING")
                manifest_status = "MISSING"
                observed_manifest = None
            else:
                observed_manifest = _sha256_file(manifest_path)
                manifest_status = "PASS" if observed_manifest == expected_manifest else "MISMATCH"
                if manifest_status != "PASS":
                    alerts.append("CAMPAIGN_PROVENANCE_MANIFEST_HASH_MISMATCH")
            summary["package_records"].append({
                "dataset_id": item.get("dataset_id"),
                "counts": dict(counts),
                "manifest": {
                    "path": str(manifest_path) if manifest_path else None,
                    "expected_sha256": expected_manifest or None,
                    "observed_sha256": observed_manifest,
                    "status": manifest_status,
                },
            })

    for category in ("source_artifacts", "version_artifacts", "service_materials"):
        entries = raw.get(category)
        if not isinstance(entries, list):
            alerts.append(f"CAMPAIGN_PROVENANCE_{category.upper()}_INVALID")
            continue
        for item in entries:
            if not isinstance(item, Mapping):
                alerts.append(f"CAMPAIGN_PROVENANCE_{category.upper()}_ENTRY_INVALID")
                continue
            path = _resolve_provenance_path(repo_root, item.get("path"))
            expected = str(item.get("sha256") or "").strip().casefold()
            observed = _sha256_file(path) if path is not None and path.is_file() else None
            status = "PASS" if observed is not None and expected and observed == expected else (
                "MISMATCH" if observed is not None and expected else "MISSING"
            )
            if status != "PASS":
                alerts.append(f"CAMPAIGN_PROVENANCE_{category.upper()}_HASH_MISMATCH")
            summary["file_fingerprints"].append({
                "category": category,
                "path": str(path) if path else None,
                "expected_sha256": expected or None,
                "observed_sha256": observed,
                "status": status,
            })

    policy = summary["evaluation_policy"]
    policy_checks = {
        "provider": (policy.get("provider"), EXPECTED_PROVIDER),
        "text_llm_provider": (policy.get("text_llm_provider"), EXPECTED_TEXT_PROVIDER),
        "deepseek_base_url": (policy.get("deepseek_base_url"), EXPECTED_DEEPSEEK_BASE_URL),
        "deepseek_llm_model": (policy.get("deepseek_llm_model"), EXPECTED_LLM),
        "maas_base_url": (policy.get("maas_base_url"), EXPECTED_MAAS_BASE_URL),
        "judge_model": (policy.get("judge_model"), EXPECTED_LLM),
        "maas_embedding_model": (policy.get("maas_embedding_model"), EXPECTED_EMBEDDING),
        "maas_embedding_dimension": (policy.get("maas_embedding_dimension"), EXPECTED_EMBEDDING_DIMENSION),
        "maas_vl_model": (policy.get("maas_vl_model"), EXPECTED_VL_MODEL),
        "mllm": (policy.get("mllm"), "NOT_APPLICABLE"),
    }
    for name, (observed, expected) in policy_checks.items():
        if observed != expected:
            alerts.append(f"CAMPAIGN_PROVENANCE_POLICY_{name.upper()}_MISMATCH")
    if policy.get("thinking") != {"type": "disabled"}:
        alerts.append("CAMPAIGN_PROVENANCE_POLICY_THINKING_MISMATCH")
    if list(policy.get("serial_order", []) or []) != list(PLATFORM_ORDER):
        alerts.append("CAMPAIGN_PROVENANCE_POLICY_SERIAL_ORDER_MISMATCH")

    summary["status"] = "PASS" if not alerts else "ALERT"
    return summary, alerts


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise MonitorError(f"FILE_MISSING:{path}") from exc
    rows: list[dict[str, Any]] = []
    for ordinal, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MonitorError(f"JSONL_INVALID:{path}:{ordinal}") from exc
        if not isinstance(value, dict):
            raise MonitorError(f"JSONL_ROW_NOT_OBJECT:{path}:{ordinal}")
        rows.append(value)
    return rows


def _manifest_artifact_path(package: Path, manifest: Mapping[str, Any], key: str, fallback: str) -> Path | None:
    value = manifest.get(key, fallback)
    if isinstance(value, (list, tuple)):
        return None
    if isinstance(value, Mapping):
        value = value.get("path")
    if value in (None, ""):
        return None
    candidate = Path(str(value)).expanduser()
    return (candidate if candidate.is_absolute() else package / candidate).resolve()


def _question_has_image(question: Mapping[str, Any]) -> bool:
    for key in ("image", "images", "image_path", "image_paths", "image_ids", "vision_input"):
        value = question.get(key)
        if value not in (None, "", [], {}, False):
            return True
    media = question.get("media")
    if isinstance(media, (list, tuple, set)):
        if any(str(item).casefold().startswith("image") for item in media):
            return True
    elif str(media or "").casefold().startswith("image"):
        return True
    media_type = str(question.get("media_type") or "").casefold()
    return media_type.startswith("image/") or media_type == "image"


def _row_key(row: Mapping[str, Any]) -> tuple[str, int] | None:
    question_id = str(row.get("question_id") or row.get("qa_id") or "").strip()
    if not question_id:
        return None
    try:
        repeat_id = int(row.get("repeat_id", 1) or 1)
    except (TypeError, ValueError):
        return None
    return question_id, repeat_id


def _ledger(path: Path, expected_n: int) -> dict[str, Any]:
    if not path.is_file():
        return {
            "path": str(path),
            "present": False,
            "rows": 0,
            "unique_n": 0,
            "duplicate_n": 0,
            "invalid_key_n": 0,
            "status_counts": {},
            "complete": False,
        }
    rows = _load_jsonl(path)
    keys = [_row_key(row) for row in rows]
    valid_keys = [key for key in keys if key is not None]
    unique_keys = set(valid_keys)
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "path": str(path),
        "present": True,
        "rows": len(rows),
        "unique_n": len(unique_keys),
        "duplicate_n": len(rows) - len(unique_keys),
        "invalid_key_n": len(rows) - len(valid_keys),
        "status_counts": dict(sorted(status_counts.items())),
        "complete": len(rows) == expected_n and len(unique_keys) == expected_n and len(valid_keys) == len(rows),
    }


def _last_progress(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    rows = _load_jsonl(path)
    if not rows:
        return None
    row = rows[-1]
    return {
        "at": row.get("at"),
        "event": row.get("event"),
        "phase": row.get("phase"),
        "status": row.get("status"),
    }


def _question_count(package: Path) -> tuple[int, dict[str, Any]]:
    if not package.is_dir():
        raise MonitorError(f"PACKAGE_NOT_DIRECTORY:{package}")
    manifest_path = package / "manifest.json"
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise MonitorError(f"MANIFEST_NOT_OBJECT:{manifest_path}")
    questions_path = package / str(manifest.get("questions", "questions.jsonl"))
    if not questions_path.is_file():
        questions_path = package / "questions.jsonl"
    rows = _load_jsonl(questions_path)
    observed_counts: dict[str, int | None] = {"questions": len(rows)}
    for key, fallback in (("documents", "corpus.jsonl"), ("gold", "gold.jsonl")):
        artifact = _manifest_artifact_path(package, manifest, key, fallback)
        observed_counts[key] = len(_load_jsonl(artifact)) if artifact is not None and artifact.is_file() else None
    declared_counts = manifest.get("counts") if isinstance(manifest.get("counts"), Mapping) else {}
    count_mismatches = [
        f"{key}:declared={declared_counts[key]}:observed={observed_counts[key]}"
        for key in ("documents", "questions", "gold")
        if key in declared_counts
        and observed_counts.get(key) is not None
        and declared_counts[key] != observed_counts[key]
    ]
    return len(rows), {
        "manifest": str(manifest_path),
        "dataset_id": manifest.get("dataset_id"),
        "mllm_required": manifest.get("mllm_required"),
        "questions": str(questions_path),
        "counts": {
            "declared": dict(declared_counts),
            "observed": observed_counts,
        },
        "count_mismatches": count_mismatches,
        "image_question_count": sum(1 for row in rows if _question_has_image(row)),
    }


def _parse_run_roots(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise MonitorError(f"RUN_ROOT_FORMAT:{value}")
        platform, path = value.split("=", 1)
        platform = platform.strip()
        if platform not in PLATFORM_ORDER or not path.strip():
            raise MonitorError(f"RUN_ROOT_PLATFORM:{value}")
        result[platform] = Path(path).expanduser().resolve()
    return result


def _resolve_run_root(unit: Mapping[str, Any], explicit: Mapping[str, Path]) -> Path | None:
    platform = str(unit.get("platform") or "")
    if platform in explicit:
        return explicit[platform]
    for field in ("runner_artifact_root",):
        value = unit.get(field)
        if value:
            return Path(str(value)).expanduser().resolve()
    output_root = unit.get("runner_output_root")
    run_id = unit.get("run_id")
    if output_root and run_id:
        return (Path(str(output_root)).expanduser() / str(run_id)).resolve()
    return None


def _config_alerts(config: Mapping[str, Any]) -> list[str]:
    checks = {
        "provider": (config.get("provider"), EXPECTED_PROVIDER),
        "text_llm_provider": (config.get("text_llm_provider"), EXPECTED_TEXT_PROVIDER),
        "deepseek_base_url": (config.get("deepseek_base_url"), EXPECTED_DEEPSEEK_BASE_URL),
        "deepseek_llm_model": (config.get("deepseek_llm_model"), EXPECTED_LLM),
        "maas_base_url": (config.get("maas_base_url"), EXPECTED_MAAS_BASE_URL),
        "maas_vl_model": (config.get("maas_vl_model"), EXPECTED_VL_MODEL),
        "maas_embedding_model": (config.get("maas_embedding_model"), EXPECTED_EMBEDDING),
        "maas_embedding_dimension": (config.get("maas_embedding_dimension"), EXPECTED_EMBEDDING_DIMENSION),
    }
    return [
        f"CONFIG_{name.upper()}_MISMATCH"
        for name, (observed, expected) in checks.items()
        if observed != expected
    ]


def _postprocess_contract_alerts(campaign: Mapping[str, Any]) -> list[str]:
    contract = campaign.get("postprocess_contract")
    if not isinstance(contract, Mapping):
        return ["CAMPAIGN_POSTPROCESS_CONTRACT_MISSING"]
    alerts: list[str] = []
    if contract.get("schema") != POSTPROCESS_CONTRACT_SCHEMA:
        alerts.append("CAMPAIGN_POSTPROCESS_CONTRACT_SCHEMA_INVALID")
    if list(contract.get("order_per_unit", []) or []) != EXPECTED_POSTPROCESS_ORDER:
        alerts.append("CAMPAIGN_POSTPROCESS_ORDER_MISMATCH")
    if list(contract.get("formal_completion_requires", []) or []) != EXPECTED_COMPLETION_GATES:
        alerts.append("CAMPAIGN_POSTPROCESS_COMPLETION_GATES_MISMATCH")
    return alerts


def _execution_gate(
    *, package: Path, package_info: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Describe the human signoff gate without starting any external work."""

    if package_info.get("mllm_required") is not False:
        return {
            "status": "BLOCKED",
            "reason": "PACKAGE_MLLM_REQUIRED",
            "text_only": False,
        }, ["PACKAGE_MLLM_REQUIRED"]

    raw_path = str(config.get("text_only_semantic_audit") or "").strip()
    if not raw_path:
        return {
            "status": "PENDING_TEXT_ONLY_SEMANTIC_AUDIT",
            "text_only": True,
            "required_schema": TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA,
            "reason": "campaign --execute requires a PASS signoff bound to the current manifest",
        }, []

    signoff_path = Path(raw_path).expanduser().resolve()
    base = {
        "text_only": True,
        "signoff": str(signoff_path),
        "required_schema": TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA,
    }
    if not signoff_path.is_file():
        return {**base, "status": "BLOCKED", "reason": "TEXT_ONLY_SEMANTIC_AUDIT_MISSING"}, [
            "TEXT_ONLY_SEMANTIC_AUDIT_MISSING"
        ]
    try:
        payload = _load_json(signoff_path)
    except MonitorError:
        return {**base, "status": "BLOCKED", "reason": "TEXT_ONLY_SEMANTIC_AUDIT_INVALID"}, [
            "TEXT_ONLY_SEMANTIC_AUDIT_INVALID"
        ]
    manifest_path = package / "manifest.json"
    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    observed_hash = str(payload.get("manifest_sha256") or "").strip().casefold() if isinstance(payload, Mapping) else ""
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA
        or payload.get("status") != "PASS"
        or payload.get("mllm_required") is not False
        or observed_hash != expected_hash
    ):
        return {
            **base,
            "status": "BLOCKED",
            "reason": "TEXT_ONLY_SEMANTIC_AUDIT_INVALID_OR_MANIFEST_MISMATCH",
            "manifest_sha256": expected_hash,
        }, ["TEXT_ONLY_SEMANTIC_AUDIT_INVALID"]
    return {
        **base,
        "status": "PASS",
        "manifest_sha256": expected_hash,
    }, []


def _provider_summary(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    if path is None:
        return {"present": False}, []
    if not path.is_file():
        return {"present": False, "path": str(path)}, ["PROVIDER_AUDIT_MISSING"]
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        return {"present": True, "path": str(path)}, ["PROVIDER_AUDIT_INVALID"]
    violations = payload.get("violations")
    if not isinstance(violations, list):
        violations = []
    alerts: list[str] = []
    if payload.get("status") != "PASS":
        alerts.append("PROVIDER_AUDIT_NOT_PASS")
    if violations:
        alerts.append("PROVIDER_AUDIT_VIOLATION")
    if int(payload.get("image_request_count", 0) or 0) != 0:
        alerts.append("IMAGE_REQUEST_DETECTED")
    observed_models = set(str(item) for item in payload.get("request_models_observed", []) or [])
    try:
        request_records = int(payload.get("request_records", 0) or 0)
    except (TypeError, ValueError):
        request_records = 0
    missing_required_models = sorted(EXPECTED_REQUEST_MODELS - observed_models)
    unexpected_request_models = sorted(observed_models - EXPECTED_REQUEST_MODELS)
    if request_records <= 0:
        alerts.append("PROVIDER_REQUEST_EVIDENCE_MISSING")
    if missing_required_models:
        alerts.append("PROVIDER_MODEL_EVIDENCE_MISSING")
    if unexpected_request_models:
        alerts.append("PROVIDER_MODEL_OUT_OF_POLICY")
    return {
        "present": True,
        "path": str(path),
        "status": payload.get("status"),
        "request_records": request_records,
        "request_models": sorted(observed_models),
        "required_request_models": sorted(EXPECTED_REQUEST_MODELS),
        "missing_required_models": missing_required_models,
        "unexpected_request_models": unexpected_request_models,
        "image_request_count": int(payload.get("image_request_count", 0) or 0),
        "violations_n": len(violations),
        "external_hosts": list(payload.get("external_hosts_observed", []) or []),
    }, alerts


def _docker_summary() -> tuple[dict[str, Any], list[str]]:
    try:
        completed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}, ["DOCKER_CHECK_UNAVAILABLE"]
    if completed.returncode != 0:
        return {"status": "ERROR", "returncode": completed.returncode}, ["DOCKER_CHECK_FAILED"]
    names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    patterns = {
        "dify_local": "moi_dify_local-",
        "fastgpt_local": "moi_fastgpt_local-",
        "maxkb_local": "moi-maxkb-local",
    }
    competitors = {
        platform: [name for name in names if name == prefix or name.startswith(prefix)]
        for platform, prefix in patterns.items()
    }
    active = {platform: values for platform, values in competitors.items() if values}
    alerts = ["DOCKER_MULTIPLE_COMPETITORS"] if len(active) > 1 else []
    return {"status": "OK", "running": names, "competitors": active}, alerts


def _unit_snapshot(unit: Mapping[str, Any], expected_n: int, explicit_roots: Mapping[str, Path]) -> tuple[dict[str, Any], list[str]]:
    run_root = _resolve_run_root(unit, explicit_roots)
    status = str(unit.get("status") or "pending").casefold()
    result: dict[str, Any] = {
        "ordinal": unit.get("ordinal"),
        "platform": unit.get("platform"),
        "run_id": unit.get("run_id"),
        "campaign_status": status,
        "mllm_required": unit.get("mllm_required"),
        "run_root": str(run_root) if run_root else None,
        "phase": "NOT_STARTED" if run_root is None else "RUNNING",
        "initial_n": 0,
        "terminal_n": 0,
        "initial_unique_n": 0,
        "terminal_unique_n": 0,
        "pending_n": expected_n,
        "judge_initial_n": 0,
        "judge_terminal_n": 0,
        "metrics_status": "NOT_STARTED",
        "last_progress": None,
    }
    alerts: list[str] = []
    if run_root is None or not run_root.is_dir():
        result["phase"] = "NOT_STARTED"
        if status not in {"pending", "planned", "preview"}:
            alerts.append("RUN_ARTIFACT_MISSING")
        return result, alerts

    initial = _ledger(run_root / "initial-ledger.jsonl", expected_n)
    terminal = _ledger(run_root / "terminal-ledger.jsonl", expected_n)
    result.update({
        "phase": "COMPLETE" if terminal.get("complete") else "RUNNING",
        "initial_n": initial["rows"],
        "terminal_n": terminal["rows"],
        "initial_unique_n": initial["unique_n"],
        "terminal_unique_n": terminal["unique_n"],
        "pending_n": max(0, expected_n - terminal["unique_n"]),
        "initial_ledger": initial,
        "terminal_ledger": terminal,
        "last_progress": _last_progress(run_root / "progress.jsonl"),
    })
    if not initial["present"]:
        alerts.append("RUN_LEDGER_INITIAL_MISSING")
    if initial["duplicate_n"] or initial["invalid_key_n"]:
        alerts.append("RUN_LEDGER_INITIAL_INVALID")
    if initial["present"] and not initial["complete"]:
        alerts.append("RUN_LEDGER_INITIAL_MISMATCH")
    if status not in {"pending", "planned", "preview"}:
        if not terminal["present"]:
            alerts.append("RUN_LEDGER_TERMINAL_MISSING")
        elif terminal["unique_n"] != initial["unique_n"]:
            alerts.append("RUN_LEDGER_TERMINAL_INCOMPLETE")

    judge_root = run_root / "judge"
    judge_initial_path = judge_root / "judge-initial-ledger.jsonl"
    judge_terminal_path = judge_root / "judge-terminal-ledger.jsonl"
    if judge_initial_path.is_file() or judge_terminal_path.is_file():
        judge_initial = _ledger(judge_initial_path, expected_n)
        judge_terminal = _ledger(judge_terminal_path, expected_n)
        result.update({
            "judge_initial_n": judge_initial["rows"],
            "judge_terminal_n": judge_terminal["rows"],
            "judge_initial_ledger": judge_initial,
            "judge_terminal_ledger": judge_terminal,
        })
        if status == "completed" and not judge_terminal["complete"]:
            alerts.append("JUDGE_LEDGER_INCOMPLETE")
    elif status == "completed":
        alerts.append("JUDGE_ARTIFACT_MISSING")

    metrics_path = run_root / "unified-metrics.json"
    if metrics_path.is_file():
        try:
            metrics = _load_json(metrics_path)
            metrics_status = str(metrics.get("status") or "UNKNOWN") if isinstance(metrics, Mapping) else "INVALID"
        except MonitorError:
            metrics_status = "INVALID"
        result["metrics_status"] = metrics_status
        if status == "completed" and metrics_status != "COMPLETE":
            alerts.append("METRICS_NOT_COMPLETE")
    elif status == "completed":
        alerts.append("METRICS_ARTIFACT_MISSING")

    # ``provider-audit-runner.json`` is the early gate; ``provider-audit.json``
    # is the final gate and is produced only after Judge raw request artifacts
    # exist.  Keep the final result under the historical ``provider_audit``
    # key while exposing both phases for operators.
    provider_audit_initial_path = run_root / "provider-audit-runner.json"
    if provider_audit_initial_path.is_file():
        provider_audit_initial, initial_alerts = _provider_summary(provider_audit_initial_path)
        result["provider_audit_initial"] = provider_audit_initial
        alerts.extend(f"UNIT_{code}" for code in initial_alerts)

    provider_audit_path = run_root / "provider-audit.json"
    if provider_audit_path.is_file():
        provider_audit, provider_alerts = _provider_summary(provider_audit_path)
        result["provider_audit"] = provider_audit
        result["provider_audit_final"] = provider_audit
        alerts.extend(f"UNIT_{code}" for code in provider_alerts)
    elif status == "completed":
        alerts.append("PROVIDER_AUDIT_ARTIFACT_MISSING")

    if unit.get("mllm_required") is not False:
        alerts.append("MLLM_REQUIRED_FOR_UNIT")
    return result, alerts


def build_snapshot(
    *,
    package: Path,
    sequence_path: Path,
    campaign_checkpoint: Path,
    provider_audit: Path | None = None,
    progress_log: Path | None = None,
    explicit_run_roots: Mapping[str, Path] | None = None,
    check_docker: bool = False,
) -> dict[str, Any]:
    expected_n, package_info = _question_count(package)
    sequence = _load_json(sequence_path)
    campaign = _load_json(campaign_checkpoint)
    if not isinstance(sequence, Mapping) or sequence.get("schema") != SEQUENCE_SCHEMA:
        raise MonitorError(f"SEQUENCE_SCHEMA_INVALID:{sequence_path}")
    if not isinstance(campaign, Mapping) or campaign.get("schema") != CAMPAIGN_SCHEMA:
        raise MonitorError(f"CAMPAIGN_SCHEMA_INVALID:{campaign_checkpoint}")

    alert_codes: list[str] = []
    integrity: dict[str, Any] = {}
    sequence_integrity, sequence_integrity_alerts = _sha256_sidecar(sequence_path)
    checkpoint_integrity, checkpoint_integrity_alerts = _sha256_sidecar(campaign_checkpoint)
    integrity["sequence"] = sequence_integrity
    integrity["campaign_checkpoint"] = checkpoint_integrity
    alert_codes.extend(sequence_integrity_alerts)
    alert_codes.extend(checkpoint_integrity_alerts)
    if progress_log is not None:
        progress_integrity, progress_integrity_alerts = _sha256_sidecar(progress_log)
        integrity["progress_log"] = progress_integrity
        alert_codes.extend(progress_integrity_alerts)
    expected_order = list(PLATFORM_ORDER)
    sequence_order = list(sequence.get("order", []) or [])
    checkpoint_order = list(campaign.get("platform_order", []) or [])
    if sequence_order != expected_order:
        alert_codes.append("SEQUENCE_ORDER_MISMATCH")
    if checkpoint_order != expected_order:
        alert_codes.append("CAMPAIGN_PLATFORM_ORDER_MISMATCH")
    alert_codes.extend(_postprocess_contract_alerts(campaign))
    if package_info.get("mllm_required") is not False:
        alert_codes.append("PACKAGE_MLLM_REQUIRED")
    if int(package_info.get("image_question_count", 0) or 0) != 0:
        alert_codes.append("PACKAGE_IMAGE_QUESTION_COUNT_MUST_BE_0")
    for mismatch in package_info.get("count_mismatches", []) or []:
        alert_codes.append(f"PACKAGE_COUNT_MISMATCH:{mismatch}")
    provenance, provenance_alerts = _provenance_summary(
        campaign=campaign,
        campaign_checkpoint=campaign_checkpoint,
        package=package,
        package_info=package_info,
    )
    alert_codes.extend(provenance_alerts)
    config = campaign.get("config") if isinstance(campaign.get("config"), Mapping) else {}
    alert_codes.extend(_config_alerts(config))
    execution_gate, execution_gate_alerts = _execution_gate(
        package=package,
        package_info=package_info,
        config=config,
    )
    alert_codes.extend(execution_gate_alerts)
    sequence_state = {
        "status": sequence.get("status"),
        "current_system": sequence.get("current_system"),
        "current_stage": sequence.get("current_stage"),
        "last_completed_item": sequence.get("last_completed_item"),
        "next_action": sequence.get("next_action"),
    }
    # A sequence record that says it can start while the campaign is still
    # fail-closed is an operator-facing integrity error, not a harmless label.
    if execution_gate.get("status") == "PENDING_TEXT_ONLY_SEMANTIC_AUDIT":
        sequence_status = str(sequence.get("status") or "").upper()
        if sequence_status in {"READY", "READY_TO_START", "READY_FOR_FORMAL_TEXT_ONLY_CAMPAIGN"}:
            alert_codes.append("SEQUENCE_EXECUTION_GATE_MISMATCH")

    provider, provider_alerts = _provider_summary(provider_audit)
    alert_codes.extend(provider_alerts)
    docker: dict[str, Any] = {"status": "SKIPPED"}
    if check_docker:
        docker, docker_alerts = _docker_summary()
        alert_codes.extend(docker_alerts)

    units_payload: list[dict[str, Any]] = []
    for unit in campaign.get("units", []) or []:
        if not isinstance(unit, Mapping):
            alert_codes.append("CAMPAIGN_UNIT_INVALID")
            continue
        snapshot, unit_alerts = _unit_snapshot(unit, expected_n, explicit_run_roots or {})
        units_payload.append(snapshot)
        alert_codes.extend(unit_alerts)

    # Preserve first-observed order while keeping the public artifact compact.
    alert_codes = list(dict.fromkeys(alert_codes))
    all_not_started = bool(units_payload) and all(item["phase"] == "NOT_STARTED" for item in units_payload)
    all_complete = bool(units_payload) and all(item["phase"] == "COMPLETE" for item in units_payload)
    if alert_codes:
        status = "ALERT"
    elif all_complete:
        status = "COMPLETE"
    elif all_not_started:
        status = "READY_TO_START"
    else:
        status = "MONITORING"

    return {
        "schema": MONITOR_SCHEMA,
        "observed_at": _utc_now(),
        "status": status,
        "alert_codes": alert_codes,
        "campaign": {
            "id": campaign.get("campaign_id"),
            "status": campaign.get("status"),
            "checkpoint": str(campaign_checkpoint),
            "sequence": str(sequence_path),
        },
        "package": {
            **package_info,
            "expected_initial_n": expected_n,
        },
        "policy": {
            "provider": EXPECTED_PROVIDER,
            "text_llm": EXPECTED_LLM,
            "embedding": EXPECTED_EMBEDDING,
            "embedding_dimension": EXPECTED_EMBEDDING_DIMENSION,
            "mllm": "NOT_APPLICABLE",
        },
        "provider_audit": provider,
        "provenance": provenance,
        "docker": docker,
        "integrity": integrity,
        "execution_gate": execution_gate,
        "sequence": sequence_state,
        "sequence_order": expected_order,
        "units": units_payload,
        "next_action": "inspect alert_codes" if alert_codes else (
            "obtain text-only semantic-audit PASS signoff before campaign --execute"
            if execution_gate["status"] == "PENDING_TEXT_ONLY_SEMANTIC_AUDIT"
            else "start first serial unit" if status == "READY_TO_START" else "continue monitoring"
        ),
    }


def _write_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    digest = hashlib.sha256(encoded).hexdigest()
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="emit a read-only monitoring snapshot")
    snapshot.add_argument("--package", type=Path, required=True)
    snapshot.add_argument("--sequence", type=Path, required=True)
    snapshot.add_argument("--campaign-checkpoint", type=Path, required=True)
    snapshot.add_argument("--provider-audit", type=Path)
    snapshot.add_argument("--progress-log", type=Path)
    snapshot.add_argument("--run-root", action="append", default=[], metavar="PLATFORM=PATH")
    snapshot.add_argument("--check-docker", action="store_true")
    snapshot.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "snapshot":  # pragma: no cover - argparse enforces this
        raise MonitorError(f"COMMAND_UNSUPPORTED:{args.command}")
    try:
        snapshot = build_snapshot(
            package=args.package.expanduser().resolve(),
            sequence_path=args.sequence.expanduser().resolve(),
            campaign_checkpoint=args.campaign_checkpoint.expanduser().resolve(),
            provider_audit=args.provider_audit.expanduser().resolve() if args.provider_audit else None,
            progress_log=args.progress_log.expanduser().resolve() if args.progress_log else None,
            explicit_run_roots=_parse_run_roots(args.run_root),
            check_docker=args.check_docker,
        )
    except MonitorError as exc:
        snapshot = {"schema": MONITOR_SCHEMA, "status": "ALERT", "alert_codes": [str(exc)]}
        if args.output:
            _write_artifact(args.output.expanduser().resolve(), snapshot)
        print(json.dumps(snapshot, ensure_ascii=False))
        return 1
    if args.output:
        _write_artifact(args.output.expanduser().resolve(), snapshot)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if snapshot["status"] == "ALERT" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
