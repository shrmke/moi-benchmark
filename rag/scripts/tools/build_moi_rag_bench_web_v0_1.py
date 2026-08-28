#!/usr/bin/env python3
"""Build the MOI RAG Bench v0.2 evidence browser data and audit report.

This script is deliberately read-only with respect to the benchmark inputs. It
reads the separated raw corpus and writes a small, static web data package. The
web app fetches ready-for-eval Markdown lazily, so source binaries are not
duplicated into the web bundle.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "datasets/moi-rag-bench-v0.2-raw-corpus"
READY_ROOT = RAW_ROOT / "ready_for_eval"
# Keep the existing URL/path stable while the page now presents v0.2 data.
WEB_ROOT = ROOT / "web/moi-rag-bench-v0.1"
DATA_ROOT = WEB_ROOT / "data"
QUALITY_AUDIT_ROOT = ROOT / "runs/readiness/qa-quality-audit-20260821-v0.2-final-mllm-rerun"
EVALUATION_ROOT = ROOT / "runs/readiness/moi-rag-bench-v0.2-local-eval-20260821"
MLLM_AUDIT_ROOT = ROOT / "runs/readiness/mllm-semantic-audit-20260821-v0.2"

EXPECTED_COUNTS = {"documents": 500, "questions": 1000, "gold": 1000}

METRIC_TAXONOMY = {
    "retrieval_performance": {
        "label": "检索性能",
        "description": "命中正确文档、页面或证据位置。",
    },
    "qa_performance": {
        "label": "问答性能",
        "description": "在给定语料中生成正确、完整的回答。",
    },
    "refusal_performance": {
        "label": "拒答性能",
        "description": "识别不可回答、信息缺失或 null query，并正确拒答。",
    },
    "multi_hop_reasoning": {
        "label": "多跳推理",
        "description": "跨多个文档/证据完成比较、时间或推断。",
    },
    "structured_extraction": {
        "label": "结构化/元数据抽取",
        "description": "从元数据、字段或结构化内容中抽取目标值。",
    },
    "conflict_resolution": {
        "label": "冲突信息处理",
        "description": "面对不一致或冲突信息时给出可信结论。",
    },
    "constraint_following": {
        "label": "约束遵循",
        "description": "满足问题中格式、范围或条件约束。",
    },
    "completeness_coverage": {
        "label": "完整性覆盖",
        "description": "覆盖问题要求的全部事实、实体或要点。",
    },
    "multimodal_grounding": {
        "label": "多模态理解",
        "description": "依赖图、表、图表或版面视觉证据。",
    },
}


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_optional_json(path: Path, default: Any = None) -> Any:
    """Read an optional derived artifact without making the web build brittle."""

    return read_json(path) if path.is_file() else default


def read_optional_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path) if path.is_file() else []


def first_existing_file(root: Path, names: Iterable[str]) -> Optional[Path]:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Expected an object at {path}:{line_number}")
            rows.append(row)
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return False
    return True


def clean_relative_path(value: Any) -> str:
    """Return a normalized, non-absolute POSIX path for display and URLs."""

    text = str(value or "").replace("\\", "/")
    return text.lstrip("/")


def repo_relative(value: Any) -> str:
    """Hide machine-specific absolute prefixes in UI-facing provenance."""

    text = str(value or "")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute() and is_under(candidate, ROOT):
        return candidate.resolve().relative_to(ROOT.resolve()).as_posix()
    return clean_relative_path(text)


def repo_url(path: Path) -> str:
    relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
    return "/" + relative


def raw_url(relative_path: str) -> str:
    return repo_url(RAW_ROOT / clean_relative_path(relative_path))


def parse_evidence_type(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [text]


def project_metadata(question: dict[str, Any]) -> dict[str, Any]:
    metadata = question.get("metadata") or {}
    # Keep the fields useful for audit and rendering while omitting absolute
    # source-QA paths and large duplicated answer/evidence payloads.
    allowed = (
        "dataset",
        "original_type",
        "selection_bucket",
        "selection_domain",
        "selection_evidence_class",
        "condition",
        "path_mode",
        "protocol_tag",
        "qa_protocol_tag",
        "domain",
        "doc_name",
        "file_id",
        "page_ids",
        "official_question_type",
        "official_index",
        "source_types",
        "source_question_id",
        "source_record_locator",
        "scope_policy",
        "scope_id",
    )
    return {key: metadata[key] for key in allowed if key in metadata}


def mllm_review_status() -> str:
    """Return the status of the latest semantic MLLM audit artifact."""

    summary_path = first_existing_file(MLLM_AUDIT_ROOT, ("after-summary.json", "summary.json"))
    if not summary_path:
        return "pending_external_agent"
    summary = read_optional_json(summary_path, {}) or {}
    if summary.get("result") == "PASS":
        return "PASS"
    return "REVIEW_REQUIRED"


def compact_quality_case(
    audit_row: Optional[dict[str, Any]],
    evaluation_row: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Project the local QA audit and retrieval ledger into UI-sized fields."""

    audit = audit_row or {}
    evaluation = evaluation_row or {}
    raw_source = evaluation.get("raw_source") or {}
    quality = {
        "available": bool(audit_row),
        "audit_record_available": bool(audit_row),
        "risk_level": audit.get("risk_level", "UNKNOWN"),
        "disposition": audit.get("disposition", "REVIEW"),
        "global_rag_ready": audit.get("global_rag_ready"),
        "qa_tags": audit.get("qa_tags") or [],
        "risk_types": audit.get("risk_types") or [],
        "findings": audit.get("findings") or [],
        "scope": audit.get("scope") or {},
        "query_signals": audit.get("query_signals") or {},
        "gold_titles": audit.get("gold_titles") or [],
        "gold_doc_ids": audit.get("gold_doc_ids") or [],
        "metric_guess": audit.get("metric_guess"),
        "mllm_audit": audit.get("mllm_audit") or {},
        "source_question_id": audit.get("source_question_id"),
    }
    if not evaluation_row:
        retrieval = {
            "available": False,
            "status": "NOT_RUN",
            "retrieval_route": None,
            "runner_routes": [],
            "embedding_model": None,
            "metrics": {},
            "stage_latency_ms": {},
            "retrieval_latency_ms": None,
            "retrieved_chunk_count": 0,
            "retrieved_document_count": 0,
            "retrieved_documents_top_order": [],
            "top_hits": [],
            "raw_source": {},
        }
    else:
        retrieval = {
            "available": True,
            "status": evaluation.get("status", "UNKNOWN"),
            "retrieval_route": evaluation.get("retrieval_route"),
            "runner_routes": evaluation.get("runner_routes") or [],
            "embedding_model": evaluation.get("embedding_model"),
            "metrics": evaluation.get("metrics") or {},
            "stage_latency_ms": evaluation.get("stage_latency_ms") or {},
            "retrieval_latency_ms": evaluation.get("retrieval_latency_ms"),
            "retrieved_chunk_count": evaluation.get("retrieved_chunk_count", 0),
            "retrieved_document_count": evaluation.get("retrieved_document_count", 0),
            "retrieved_documents_top_order": evaluation.get("retrieved_documents_top_order") or [],
            "top_hits": evaluation.get("top_hits") or [],
            "raw_source": {
                "path": repo_relative(raw_source.get("path")),
                "line_number": raw_source.get("line_number"),
            },
        }
    return {"quality": quality, "retrieval_evaluation": retrieval}


def add_artifact_gap(
    case: dict[str, Any],
    projected: dict[str, Any],
    *,
    audit_available: bool,
    evaluation_available: bool,
    evaluation_expected: bool = True,
) -> None:
    """Make stale/misaligned derived artifacts visible instead of hiding them."""

    quality = projected["quality"]
    quality["audit_record_available"] = audit_available
    quality["source_question_id"] = case.get("source_question_id")
    quality["metric_guess"] = quality.get("metric_guess") or case.get("metric_key")
    tags = list(quality.get("qa_tags") or [])
    risk_types = list(quality.get("risk_types") or [])
    findings = list(quality.get("findings") or [])

    def add_tag(code: str, label: str) -> None:
        if not any(isinstance(tag, dict) and tag.get("code") == code for tag in tags):
            tags.append({"code": code, "label": label})

    def add_risk_type(code: str) -> None:
        if code not in risk_types:
            risk_types.append(code)

    if not audit_available:
        quality["available"] = False
        quality["risk_level"] = "HIGH"
        quality["disposition"] = "REVIEW_BEFORE_EVAL"
        quality["global_rag_ready"] = False
        add_tag("problematic", "存在问题")
        add_tag("quality_audit_missing", "缺少 QA 质量审核")
        add_risk_type("quality_audit_missing")
        findings.append(
            {
                "severity": "HIGH",
                "risk_type": "quality_audit_missing",
                "message": "当前数据集中的这条 QA 没有匹配到质量审核记录；不能把它当作已完成首轮质量筛选。",
                "recommended_action": "重新对当前 package 运行 QA quality audit，并在人工审核完成前保留在 review 队列。",
                "evidence": {
                    "question_id": case.get("question_id"),
                    "source_question_id": case.get("source_question_id"),
                },
            }
        )

    if evaluation_expected and not evaluation_available:
        add_tag("evaluation_artifact_missing", "缺少检索评测")
        add_risk_type("evaluation_artifact_missing")
        findings.append(
            {
                "severity": "MEDIUM",
                "risk_type": "evaluation_artifact_missing",
                "message": "当前数据集中的这条 QA 没有匹配到本地检索评测 ledger；页面只展示质量审核，不应误读为已完成检索评测。",
                "recommended_action": "使用当前 questions.jsonl 重新运行本地 retrieval evaluation，或明确将此条留在待评测队列。",
                "evidence": {
                    "question_id": case.get("question_id"),
                    "source_question_id": case.get("source_question_id"),
                },
            }
        )

    if not quality.get("mllm_audit") or not quality["mllm_audit"].get("status"):
        quality["mllm_audit"] = {
            "status": "EXTERNAL_AUDIT_RECORD_MISSING",
            "mllm_required": None,
            "text_only_ready": None,
        }
    quality["qa_tags"] = tags
    quality["risk_types"] = risk_types
    quality["findings"] = findings


def build_quality_assessment(
    audit_root: Path,
    evaluation_root: Path,
    mllm_audit_root: Path,
    expected_question_ids: Optional[Iterable[str]] = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load the completed audit/eval artifacts used by the human review UI."""

    audit_rows = read_optional_jsonl(audit_root / "qa-quality-audit.jsonl")
    audit_by_id = {str(row.get("question_id")): row for row in audit_rows if row.get("question_id")}
    evaluation_rows = read_optional_jsonl(evaluation_root / "evaluation-ledger.compact.jsonl")
    evaluation_by_id = {str(row.get("id")): row for row in evaluation_rows if row.get("id")}
    audit_summary = read_optional_json(audit_root / "summary.json", {}) or {}
    evaluation_summary = read_optional_json(evaluation_root / "evaluation-summary.mixed.json", {}) or {}
    mllm_summary_path = first_existing_file(mllm_audit_root, ("after-summary.json", "summary.json"))
    mllm_rows_path = first_existing_file(mllm_audit_root, ("after-audit.jsonl", "audit.jsonl"))
    mllm_summary = read_optional_json(mllm_summary_path, {}) if mllm_summary_path else {}
    mllm_summary = mllm_summary or {}
    mllm_rows = read_optional_jsonl(mllm_rows_path) if mllm_rows_path else []
    mllm_counts = mllm_summary.get("counts") or {}
    evaluation_expected = evaluation_root.exists()

    expected_ids = {str(value) for value in (expected_question_ids or []) if value}
    audit_ids = set(audit_by_id)
    evaluation_ids = set(evaluation_by_id)
    mllm_ids = {
        str(row.get("question_id"))
        for row in mllm_rows
        if row.get("question_id")
    }

    all_ids = sorted(set(audit_by_id) | set(evaluation_by_id))
    cases = {
        question_id: compact_quality_case(
            audit_by_id.get(question_id), evaluation_by_id.get(question_id)
        )
        for question_id in all_ids
    }

    risk_counts = Counter(
        str(row.get("risk_level") or "UNKNOWN") for row in audit_rows
    )
    disposition_counts = Counter(
        str(row.get("disposition") or "REVIEW") for row in audit_rows
    )
    tag_counts = Counter(
        str(tag.get("code"))
        for row in audit_rows
        for tag in (row.get("qa_tags") or [])
        if isinstance(tag, dict) and tag.get("code")
    )
    ready_counts = Counter(
        "true" if row.get("global_rag_ready") is True
        else "false" if row.get("global_rag_ready") is False
        else "conditional"
        for row in audit_rows
    )
    eval_status_counts = Counter(
        str(row.get("status") or "UNKNOWN") for row in evaluation_rows
    )
    route_counts = Counter(
        str(row.get("retrieval_route") or "UNKNOWN") for row in evaluation_rows
    )
    audit_available = bool(audit_rows)
    evaluation_available = bool(evaluation_rows)
    mllm_available = bool(mllm_summary)
    audit_aligned = bool(
        expected_ids
        and not (expected_ids - audit_ids)
        and not (audit_ids - expected_ids)
    )
    evaluation_aligned = not evaluation_expected or (
        expected_ids
        and not (expected_ids - evaluation_ids)
        and not (evaluation_ids - expected_ids)
    )
    artifact_alignment = {
        "status": "partial"
        if audit_aligned and not evaluation_expected
        else "aligned" if audit_aligned and evaluation_aligned
        else "mismatch" if expected_ids else "not_checked",
        "evaluation_expected": evaluation_expected,
        "expected_case_rows": len(expected_ids) if expected_ids else None,
        "audit_rows": len(audit_rows),
        "audit_matched_case_rows": len(expected_ids & audit_ids) if expected_ids else None,
        "audit_missing_case_rows": len(expected_ids - audit_ids) if expected_ids else None,
        "audit_orphan_rows": len(audit_ids - expected_ids) if expected_ids else None,
        "evaluation_rows": len(evaluation_rows),
        "evaluation_matched_case_rows": len(expected_ids & evaluation_ids) if expected_ids else None,
        "evaluation_missing_case_rows": len(expected_ids - evaluation_ids) if expected_ids else None,
        "evaluation_orphan_rows": len(evaluation_ids - expected_ids) if expected_ids else None,
        "mllm_detail_rows": len(mllm_rows),
        "mllm_matched_case_rows": len(expected_ids & mllm_ids) if expected_ids else None,
        "mllm_missing_case_rows": len(expected_ids - mllm_ids) if expected_ids else None,
        "mllm_orphan_rows": len(mllm_ids - expected_ids) if expected_ids else None,
    }

    summary = {
        "schema": "moi-rag-bench-quality-assessment-v1",
        "available": audit_available,
        "audit_source": repo_relative(audit_root / "qa-quality-audit.jsonl") if audit_available else None,
        "audit_summary_source": repo_relative(audit_root / "summary.json") if audit_available else None,
        "evaluation_available": evaluation_available,
        "evaluation_source": repo_relative(evaluation_root / "evaluation-ledger.compact.jsonl") if evaluation_available else None,
        "evaluation_summary_source": repo_relative(evaluation_root / "evaluation-summary.mixed.json") if evaluation_available else None,
        "rows": len(audit_rows),
        "case_rows": len(expected_ids) if expected_ids else len(cases),
        "evaluation_rows": len(evaluation_rows),
        "audit_matched_case_rows": artifact_alignment["audit_matched_case_rows"],
        "audit_missing_case_rows": artifact_alignment["audit_missing_case_rows"],
        "evaluation_matched_case_rows": artifact_alignment["evaluation_matched_case_rows"],
        "evaluation_missing_case_rows": artifact_alignment["evaluation_missing_case_rows"],
        "artifact_alignment": artifact_alignment,
        "risk_level_counts": dict(sorted(risk_counts.items())),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "tag_counts": dict(sorted(tag_counts.items())),
        "global_rag_ready_counts": dict(sorted(ready_counts.items())),
        "evaluation_status_counts": dict(sorted(eval_status_counts.items())),
        "evaluation_route_counts": dict(sorted(route_counts.items())),
        "problematic_count": tag_counts.get("problematic", 0),
        "tagged_count": sum(1 for row in audit_rows if row.get("qa_tags")),
        "evaluation_summary": evaluation_summary,
        "audit_summary": audit_summary,
        "mllm": {
            "available": mllm_available,
            "source": repo_relative(mllm_summary_path) if mllm_available and mllm_summary_path else None,
            "status": "PASS" if mllm_summary.get("result") == "PASS" else "complete_external_agent" if mllm_available else "not_loaded",
            "rows": mllm_counts.get("questions"),
            "multimodal_rows": mllm_counts.get("multimodal_rows"),
            "mllm_required_rows": mllm_counts.get("mllm_required_rows"),
            "text_projection_safe_rows": mllm_counts.get("text_projection_safe_rows"),
            "review_rows": mllm_counts.get("review_required_rows"),
            "unsafe_rows": mllm_counts.get("text_projection_unsafe_rows"),
            "detail_rows": len(mllm_rows),
            "matched_case_rows": artifact_alignment["mllm_matched_case_rows"],
            "missing_case_rows": artifact_alignment["mllm_missing_case_rows"],
            "orphan_rows": artifact_alignment["mllm_orphan_rows"],
        },
    }
    return cases, summary


def infer_metric(question: dict[str, Any]) -> tuple[str, str]:
    """Assign one primary evaluation metric; this remains human-editable in the UI."""

    dataset = str(question.get("source_dataset") or "").lower()
    question_type = str(question.get("question_type") or "").lower()
    metadata = question.get("metadata") or {}
    original_type = str(metadata.get("original_type") or "").lower()

    if dataset == "mmdocir":
        return "retrieval_performance", "MMDocIR 的 question_type 为 retrieval，主指标是页面/证据检索命中。"
    if dataset == "docbench":
        if original_type in {"unanswerable", "una-web"} or question.get("answerable") is False:
            return "refusal_performance", "DocBench 标注为不可回答或 web-unanswerable。"
        if original_type in {"multimodal-t", "multimodal-f"}:
            return "multimodal_grounding", "DocBench 标注为多模态 QA，答案依赖视觉证据。"
        if original_type == "meta-data":
            return "structured_extraction", "DocBench 标注为 meta-data，核心是字段/元数据抽取。"
        return "qa_performance", "DocBench text-only QA 的主指标是答案正确性与证据支撑。"
    if dataset == "multihop":
        if question_type == "null_query" or question.get("answerable") is False:
            return "refusal_performance", "MultiHop null_query/不可回答问题，核心是正确拒答。"
        return "multi_hop_reasoning", "MultiHop comparison/inference/temporal 问题需要跨证据推理。"
    if dataset == "enterprise":
        if question_type == "info_not_found":
            return "refusal_performance", "EnterpriseRAG info_not_found 需要识别信息缺失并拒答。"
        if question_type == "conflicting_info":
            return "conflict_resolution", "问题类型直接测试冲突信息处理。"
        if question_type == "constrained":
            return "constraint_following", "问题类型直接测试约束遵循。"
        if question_type == "completeness":
            return "completeness_coverage", "问题类型直接测试答案完整性覆盖。"
        return "qa_performance", "EnterpriseRAG 其余问题的主指标是答案正确性与证据支撑。"
    return "qa_performance", "未匹配专门类别，默认按问答性能评估。"


def expected_empty_evidence(question: dict[str, Any]) -> bool:
    dataset = str(question.get("source_dataset") or "").lower()
    metadata = question.get("metadata") or {}
    original_type = str(metadata.get("original_type") or "").lower()
    question_type = str(question.get("question_type") or "").lower()
    if question.get("answerable") is False:
        return True
    if dataset == "docbench" and original_type == "meta-data":
        return True
    if dataset == "multihop" and question_type == "null_query":
        return True
    return False


def normalize_evidence(items: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(items, list):
        return [], {"total": 0, "text": 0, "locator": 0, "resolved": 0}
    normalized: list[dict[str, Any]] = []
    counts = Counter()
    for index, item in enumerate(items, 1):
        if isinstance(item, str):
            normalized.append({"index": index, "kind": "text", "text": item})
            counts["text"] += 1
            continue
        if isinstance(item, dict):
            has_text = isinstance(item.get("evidence"), str) and bool(
                item.get("evidence", "").strip()
            )
            kind = "text" if has_text else "locator"
            projected: dict[str, Any] = {
                "index": index,
                "kind": kind,
                "doc_id": item.get("doc_id"),
                "status": item.get("status"),
            }
            if "evidence" in item:
                projected["text"] = item.get("evidence")
            if "locator" in item:
                projected["locator"] = item.get("locator")
            normalized.append(projected)
            counts[kind] += 1
            if item.get("status") == "resolved":
                counts["resolved"] += 1
            continue
        normalized.append({"index": index, "kind": "unknown", "value": item})
        counts["unknown"] += 1
    counts["total"] = len(items)
    return normalized, dict(counts)


def build_documents(
    corpus_rows: list[dict[str, Any]],
    provenance_rows: list[dict[str, Any]],
    issues: dict[str, dict[str, list[dict[str, Any]]]],
    verify_source_hashes: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    source_stats = Counter()
    ready_hashes_verified = 0
    source_hashes_verified = 0

    for row in corpus_rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            issues["__global__"]["errors"].append(
                {"code": "document_id_missing", "message": "A corpus row has no doc_id."}
            )
            continue
        if doc_id in documents:
            issues["__global__"]["errors"].append(
                {"code": "document_id_duplicate", "message": f"Duplicate doc_id: {doc_id}"}
            )
            continue

        text_path_raw = str(row.get("text_path") or "")
        text_path = Path(text_path_raw)
        text_abs = READY_ROOT / text_path
        if (
            not text_path_raw
            or text_path.is_absolute()
            or ".." in text_path.parts
            or not is_under(text_abs, READY_ROOT)
        ):
            issues["__global__"]["errors"].append(
                {
                    "code": "ready_path_invalid",
                    "message": f"{doc_id} has invalid ready text path: {text_path_raw}",
                }
            )
        elif not text_abs.is_file():
            issues["__global__"]["errors"].append(
                {
                    "code": "ready_text_missing",
                    "message": f"{doc_id} ready text is missing: {text_path_raw}",
                }
            )
        else:
            actual_hash = sha256_file(text_abs)
            if actual_hash != row.get("sha256"):
                issues["__global__"]["errors"].append(
                    {
                        "code": "ready_text_hash_mismatch",
                        "message": f"{doc_id} ready text hash does not match corpus.jsonl.",
                    }
                )
            else:
                ready_hashes_verified += 1

        provenance = next(
            (item for item in provenance_rows if item.get("doc_id") == doc_id), None
        )
        if provenance is None:
            issues["__global__"]["errors"].append(
                {
                    "code": "provenance_missing",
                    "message": f"{doc_id} has no provenance row.",
                }
            )
            provenance = {}

        source_assets: list[dict[str, Any]] = []
        for asset in provenance.get("copied_assets") or []:
            asset_path_raw = str(asset.get("path") or asset.get("relative_path") or "")
            asset_path = Path(asset_path_raw)
            asset_abs = RAW_ROOT / asset_path
            asset_out: dict[str, Any] = {
                "path": clean_relative_path(asset_path_raw),
                "url": raw_url(asset_path_raw),
                "bytes": asset.get("bytes"),
                "sha256": asset.get("sha256"),
                "original_path": repo_relative(asset.get("source_path")),
            }
            original_path = repo_relative(asset.get("source_path"))
            if original_path and not original_path.startswith("/"):
                original_abs = ROOT / original_path
                if original_abs.is_file() and is_under(original_abs, ROOT):
                    asset_out["original_url"] = repo_url(original_abs)

            valid_source_path = (
                bool(asset_path_raw)
                and not asset_path.is_absolute()
                and ".." not in asset_path.parts
                and is_under(asset_abs, RAW_ROOT / "source_files")
            )
            if not valid_source_path:
                issues[doc_id]["errors"].append(
                    {
                        "code": "source_path_invalid",
                        "message": f"Invalid copied source asset path: {asset_path_raw}",
                    }
                )
            elif not asset_abs.is_file():
                issues[doc_id]["errors"].append(
                    {
                        "code": "source_asset_missing",
                        "message": f"Copied source asset is missing: {asset_path_raw}",
                    }
                )
            else:
                source_stats["present"] += 1
                if verify_source_hashes:
                    actual_hash = sha256_file(asset_abs)
                    if actual_hash != asset.get("sha256"):
                        issues[doc_id]["errors"].append(
                            {
                                "code": "source_asset_hash_mismatch",
                                "message": f"Source asset hash does not match: {asset_path_raw}",
                            }
                        )
                    else:
                        source_hashes_verified += 1
            source_assets.append(asset_out)

        source_stats["assets"] += len(source_assets)
        if provenance.get("source_asset_available"):
            source_stats["docs_with_source_assets"] += 1
        if provenance.get("native_document_available"):
            source_stats["docs_with_native_source"] += 1

        documents[doc_id] = {
            "doc_id": doc_id,
            "title": row.get("title") or doc_id,
            "source_dataset": row.get("metadata", {}).get("source_dataset"),
            "source_document_id": row.get("metadata", {}).get("source_document_id"),
            "source_mode": row.get("metadata", {}).get("source_mode")
            or provenance.get("source_mode"),
            "ready_text_mode": provenance.get("ready_text_mode"),
            "ready_path": text_path_raw,
            "ready_url": repo_url(text_abs),
            "ready_sha256": row.get("sha256"),
            "ready_char_count": text_abs.stat().st_size if text_abs.is_file() else None,
            "source_directory": provenance.get("source_directory"),
            "source_asset_available": bool(provenance.get("source_asset_available")),
            "native_document_available": bool(provenance.get("native_document_available")),
            "source_asset_count": len(source_assets),
            "source_assets": source_assets,
            "missing_sources": provenance.get("missing_sources") or [],
            "linked_question_count": provenance.get("linked_question_count", 0),
        }

    source_stats["hashes_verified"] = source_hashes_verified
    source_stats["ready_hashes_verified"] = ready_hashes_verified
    source_stats["hash_verification_requested"] = int(verify_source_hashes)
    return documents, dict(source_stats)


def add_issue(
    issues: dict[str, dict[str, list[dict[str, Any]]]],
    question_id: str,
    severity: str,
    code: str,
    message: str,
) -> None:
    issues.setdefault(question_id, {"errors": [], "warnings": []})
    bucket = "errors" if severity == "error" else "warnings"
    issues[question_id][bucket].append({"code": code, "message": message})


def validate_questions(
    questions: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    issues: dict[str, dict[str, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    question_ids = [str(row.get("question_id") or "") for row in questions]
    gold_ids = [str(row.get("question_id") or "") for row in gold_rows]
    question_by_id = {row.get("question_id"): row for row in questions}
    gold_by_id = {row.get("question_id"): row for row in gold_rows}
    document_ids = set(documents)

    if len(question_ids) != len(set(question_ids)):
        add_issue(issues, "__global__", "error", "question_id_duplicate", "Question IDs are not unique.")
    if len(gold_ids) != len(set(gold_ids)):
        add_issue(issues, "__global__", "error", "gold_question_id_duplicate", "Gold question IDs are not unique.")

    all_question_ids = list(dict.fromkeys(question_ids + gold_ids))
    cases: list[dict[str, Any]] = []
    annotation_rows: list[dict[str, Any]] = []
    status_counts = Counter()
    evidence_counts = Counter()
    metric_counts = Counter()
    metric_by_dataset = Counter()
    warning_rows: list[dict[str, Any]] = []

    for question_id in all_question_ids:
        question = question_by_id.get(question_id)
        gold = gold_by_id.get(question_id)
        if question is None:
            add_issue(issues, question_id, "error", "question_missing", "No question row for this gold.")
            continue
        if gold is None:
            add_issue(issues, question_id, "error", "gold_missing", "No gold row for this question.")
            continue

        dataset = str(question.get("source_dataset") or "")
        question_type = str(question.get("question_type") or "")
        if not str(question.get("question") or "").strip():
            add_issue(issues, question_id, "error", "question_empty", "Question text is empty.")
        if not dataset:
            add_issue(issues, question_id, "error", "source_dataset_missing", "source_dataset is empty.")
        if not question_type:
            add_issue(issues, question_id, "error", "question_type_missing", "question_type is empty.")
        if gold.get("question_id") != question_id:
            add_issue(issues, question_id, "error", "gold_id_mismatch", "Gold question_id does not match its question.")
        if gold.get("source_dataset") != dataset:
            add_issue(issues, question_id, "error", "dataset_mismatch", "Question and gold source_dataset differ.")
        if gold.get("source_question_id") != question.get("source_question_id"):
            add_issue(
                issues,
                question_id,
                "error",
                "source_question_id_mismatch",
                "Question and gold source_question_id differ.",
            )

        gold_doc_ids = gold.get("gold_doc_ids")
        if not isinstance(gold_doc_ids, list):
            add_issue(issues, question_id, "error", "gold_doc_ids_invalid", "gold_doc_ids must be a list.")
            gold_doc_ids = []
        elif any(not isinstance(doc_id, str) or not doc_id.strip() for doc_id in gold_doc_ids):
            add_issue(
                issues,
                question_id,
                "error",
                "gold_doc_id_item_invalid",
                "Every gold_doc_ids item must be a non-empty string.",
            )
        if question.get("gold_doc_ids") != gold_doc_ids:
            add_issue(
                issues,
                question_id,
                "error",
                "question_gold_doc_mismatch",
                "Question and gold gold_doc_ids differ.",
            )
        missing_doc_ids = [doc_id for doc_id in gold_doc_ids if doc_id not in document_ids]
        if missing_doc_ids:
            add_issue(
                issues,
                question_id,
                "error",
                "gold_doc_missing",
                f"gold_doc_ids are not present in corpus.jsonl: {missing_doc_ids}",
            )

        gold_evidence = gold.get("gold_evidence")
        if not isinstance(gold_evidence, list):
            add_issue(issues, question_id, "error", "gold_evidence_invalid", "gold_evidence must be a list.")
            gold_evidence = []
        for item in gold_evidence:
            if not isinstance(item, (str, dict)):
                add_issue(
                    issues,
                    question_id,
                    "error",
                    "gold_evidence_item_invalid",
                    "Every gold_evidence item must be a string or object.",
                )
            elif isinstance(item, str) and not item.strip():
                add_issue(
                    issues,
                    question_id,
                    "warning",
                    "gold_evidence_text_empty",
                    "A gold_evidence text item is empty.",
                )

        reference_answer = gold.get("reference_answer")
        if reference_answer is None:
            add_issue(
                issues,
                question_id,
                "warning",
                "reference_answer_null",
                "reference_answer is null; preserve and review before scoring.",
            )
            reference_answer = ""
        elif not isinstance(reference_answer, str):
            add_issue(
                issues,
                question_id,
                "error",
                "reference_answer_invalid",
                "reference_answer must be a string.",
            )
            reference_answer = str(reference_answer)
        if question.get("answerable") is True and not reference_answer.strip():
            add_issue(
                issues,
                question_id,
                "warning",
                "reference_answer_empty",
                "answerable=true but reference_answer is empty; preserve and review before scoring.",
            )
        if not gold_evidence and not expected_empty_evidence(question):
            add_issue(
                issues,
                question_id,
                "warning",
                "gold_evidence_empty_unexpected",
                "gold_evidence is empty for an otherwise answerable/non-null case.",
            )

        normalized_evidence, evidence_summary = normalize_evidence(gold_evidence)
        evidence_type = (question.get("metadata") or {}).get("evidence_type")
        evidence_tokens = parse_evidence_type(evidence_type)
        for token in evidence_tokens or ["none"]:
            evidence_counts[token] += 1

        status = "error" if issues[question_id]["errors"] else "warning" if issues[question_id]["warnings"] else "valid"
        status_counts[status] += 1
        if status == "warning":
            warning_rows.append(
                {
                    "question_id": question_id,
                    "codes": [item["code"] for item in issues[question_id]["warnings"]],
                }
            )
        metric_key, metric_reason = infer_metric(question)
        metric_label = METRIC_TAXONOMY[metric_key]["label"]
        metric_counts[metric_key] += 1
        metric_by_dataset[(dataset, metric_key)] += 1

        case = {
            "question_id": question_id,
            "source_question_id": question.get("source_question_id"),
            "source_dataset": dataset,
            "question_type": question_type,
            "question": question.get("question") or "",
            "source_answer": question.get("answer") or "",
            "answerable": question.get("answerable"),
            "reference_answer": reference_answer,
            "gold_doc_ids": gold_doc_ids,
            "source_gold_doc_ids": gold.get("source_gold_doc_ids") or [],
            "gold_evidence": gold_evidence,
            "evidence_items": normalized_evidence,
            "evidence_summary": evidence_summary,
            "evidence_type": evidence_type,
            "evidence_types": evidence_tokens,
            "metadata": project_metadata(question),
            "metric_key": metric_key,
            "metric_label": metric_label,
            "metric_reason": metric_reason,
            "metric_annotation": {
                "metric_key": metric_key,
                "metric_label": metric_label,
                "source": "auto",
                "reviewed": False,
                "note": "",
            },
            "mllm_review_status": mllm_review_status(),
            "mllm_required": None,
            "mllm_reasons": [],
            "validation_status": status,
            "validation_issues": issues[question_id]["errors"] + issues[question_id]["warnings"],
        }
        cases.append(case)

        list_row = {
            "question_id": question_id,
            "source_question_id": question.get("source_question_id"),
            "source_dataset": dataset,
            "question_type": question_type,
            "question": question.get("question") or "",
            "answerable": question.get("answerable"),
            "reference_answer": reference_answer,
            "gold_doc_ids": gold_doc_ids,
            "gold_evidence": gold_evidence,
            "evidence_type": evidence_type,
            "metric_key": metric_key,
            "metric_label": metric_label,
            "metric_reason": metric_reason,
            "mllm_review_status": mllm_review_status(),
            "mllm_required": None,
            "mllm_reasons": [],
            "validation_status": status,
            "validation_issues": case["validation_issues"],
        }
        annotation_rows.append(list_row)

    cases.sort(key=lambda row: row["question_id"])
    annotation_rows.sort(key=lambda row: row["question_id"])

    dataset_counts = Counter(str(row.get("source_dataset") or "") for row in questions)
    type_counts = Counter(
        (str(row.get("source_dataset") or ""), str(row.get("question_type") or ""))
        for row in questions
    )
    validation_summary = {
        "question_rows": len(questions),
        "gold_rows": len(gold_rows),
        "question_ids_in_both": len(set(question_ids) & set(gold_ids)),
        "case_status": dict(status_counts),
        "empty_gold_evidence": sum(not (row.get("gold_evidence") or []) for row in gold_rows),
        "empty_gold_evidence_expected": sum(
            not (gold_by_id.get(row.get("question_id"), {}).get("gold_evidence") or [])
            and expected_empty_evidence(row)
            for row in questions
            if row.get("question_id") in gold_by_id
        ),
        "empty_gold_evidence_unexpected": sum(
            not (gold_by_id.get(row.get("question_id"), {}).get("gold_evidence") or [])
            and not expected_empty_evidence(row)
            for row in questions
            if row.get("question_id") in gold_by_id
        ),
        "warning_rows": warning_rows,
        "by_dataset": dict(dataset_counts),
        "by_question_type": {
            f"{dataset}:{question_type}": count
            for (dataset, question_type), count in sorted(type_counts.items())
        },
        "evidence_type_tokens": dict(evidence_counts),
        "by_metric": dict(metric_counts),
        "by_dataset_metric": {
            f"{dataset}:{metric_key}": count
            for (dataset, metric_key), count in sorted(metric_by_dataset.items())
        },
        "mllm_review_status": mllm_review_status(),
        "mllm_processed_in_this_session": False,
        "mllm_pending_count": len(annotation_rows),
    }
    return cases, annotation_rows, {"annotations": annotation_rows, "summary": validation_summary}


def build() -> dict[str, Any]:
    global RAW_ROOT, READY_ROOT, WEB_ROOT, DATA_ROOT, QUALITY_AUDIT_ROOT, EVALUATION_ROOT, MLLM_AUDIT_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-source-hashes",
        action="store_true",
        help="Hash every copied source asset as part of the separation audit.",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=RAW_ROOT,
        help="Separated raw corpus root (defaults to datasets/moi-rag-bench-v0.2-raw-corpus).",
    )
    parser.add_argument(
        "--web-root",
        type=Path,
        default=WEB_ROOT,
        help="Static web root (defaults to web/moi-rag-bench-v0.1 for URL compatibility).",
    )
    parser.add_argument(
        "--audit-root",
        type=Path,
        default=QUALITY_AUDIT_ROOT,
        help="Per-QA quality audit root; missing artifacts leave the quality panel in a not-loaded state.",
    )
    parser.add_argument(
        "--evaluation-root",
        type=Path,
        default=EVALUATION_ROOT,
        help="Local retrieval evaluation root; missing artifacts leave the evaluation trace unavailable.",
    )
    parser.add_argument(
        "--mllm-audit-root",
        type=Path,
        default=MLLM_AUDIT_ROOT,
        help="Completed external MLLM audit root used for display only.",
    )
    args = parser.parse_args()

    RAW_ROOT = args.raw_root.resolve()
    READY_ROOT = RAW_ROOT / "ready_for_eval"
    WEB_ROOT = args.web_root.resolve()
    DATA_ROOT = WEB_ROOT / "data"
    QUALITY_AUDIT_ROOT = args.audit_root.resolve()
    EVALUATION_ROOT = args.evaluation_root.resolve()
    MLLM_AUDIT_ROOT = args.mllm_audit_root.resolve()

    corpus_rows = read_jsonl(READY_ROOT / "corpus.jsonl")
    questions = read_jsonl(READY_ROOT / "questions.jsonl")
    gold_rows = read_jsonl(READY_ROOT / "gold.jsonl")
    provenance_rows = read_jsonl(RAW_ROOT / "provenance/documents.jsonl")

    issues: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"errors": [], "warnings": []}
    )
    for name, rows in (
        ("documents", corpus_rows),
        ("questions", questions),
        ("gold", gold_rows),
    ):
        expected = EXPECTED_COUNTS[name]
        if len(rows) != expected:
            add_issue(
                issues,
                "__global__",
                "error",
                f"{name}_count_mismatch",
                f"Expected {expected} {name}, found {len(rows)}.",
            )

    documents, source_stats = build_documents(
        corpus_rows,
        provenance_rows,
        issues,
        verify_source_hashes=args.verify_source_hashes,
    )
    cases, annotation_rows, question_summary = validate_questions(
        questions,
        gold_rows,
        documents,
        issues,
    )
    quality_by_id, quality_summary = build_quality_assessment(
        QUALITY_AUDIT_ROOT,
        EVALUATION_ROOT,
        MLLM_AUDIT_ROOT,
        expected_question_ids=[case["question_id"] for case in cases],
    )
    for case in cases:
        question_id = case["question_id"]
        quality = quality_by_id.get(question_id)
        audit_available = bool(quality and quality.get("quality", {}).get("audit_record_available"))
        evaluation_available = bool(quality and quality.get("retrieval_evaluation", {}).get("available"))
        if quality is None:
            quality = compact_quality_case(None, None)
        add_artifact_gap(
            case,
            quality,
            audit_available=audit_available,
            evaluation_available=evaluation_available,
            evaluation_expected=EVALUATION_ROOT.exists(),
        )
        case.update(quality)
        case_mllm = quality["quality"].get("mllm_audit") or {}
        case["mllm_review_status"] = case_mllm.get("status") or case.get("mllm_review_status") or "NOT_LOADED"
        if case_mllm.get("mllm_required") is not None:
            case["mllm_required"] = case_mllm["mllm_required"]
    case_by_id = {case["question_id"]: case for case in cases}
    for row in annotation_rows:
        quality = case_by_id.get(row["question_id"], {}).get("quality", {})
        case_mllm = quality.get("mllm_audit") or {}
        row["mllm_review_status"] = case_mllm.get("status") or row.get("mllm_review_status") or "NOT_LOADED"
        if case_mllm.get("mllm_required") is not None:
            row["mllm_required"] = case_mllm["mllm_required"]

    # Recompute dashboard counts over the actual package rows, including any
    # explicit artifact-coverage gaps added above. The source audit counts are
    # retained in the nested audit_summary and alignment fields.
    final_quality_rows = [case["quality"] for case in cases]
    final_risk_counts = Counter(str(row.get("risk_level") or "UNKNOWN") for row in final_quality_rows)
    final_ready_counts = Counter(
        "true" if row.get("global_rag_ready") is True
        else "false" if row.get("global_rag_ready") is False
        else "conditional"
        for row in final_quality_rows
    )
    final_tag_counts = Counter(
        str(tag.get("code"))
        for row in final_quality_rows
        for tag in (row.get("qa_tags") or [])
        if isinstance(tag, dict) and tag.get("code")
    )
    quality_summary["source_audit_risk_level_counts"] = quality_summary.get("risk_level_counts", {})
    quality_summary["source_audit_global_rag_ready_counts"] = quality_summary.get("global_rag_ready_counts", {})
    quality_summary["source_audit_tag_counts"] = quality_summary.get("tag_counts", {})
    quality_summary["risk_level_counts"] = dict(sorted(final_risk_counts.items()))
    quality_summary["global_rag_ready_counts"] = dict(sorted(final_ready_counts.items()))
    quality_summary["tag_counts"] = dict(sorted(final_tag_counts.items()))
    quality_summary["problematic_count"] = final_tag_counts.get("problematic", 0)
    quality_summary["tagged_count"] = sum(1 for row in final_quality_rows if row.get("qa_tags"))
    quality_summary["case_rows"] = len(cases)
    quality_summary["case_audit_rows"] = sum(
        1 for row in final_quality_rows if row.get("audit_record_available")
    )
    quality_summary["case_evaluation_rows"] = sum(
        1 for case in cases if case.get("retrieval_evaluation", {}).get("available")
    )
    quality_summary["artifact_gap_count"] = sum(
        1 for row in final_quality_rows if any(
            isinstance(tag, dict)
            and tag.get("code") in {"quality_audit_missing", "evaluation_artifact_missing"}
            for tag in (row.get("qa_tags") or [])
        )
    )
    if quality_summary["mllm"]["available"]:
        question_summary["summary"]["mllm_review_status"] = quality_summary["mllm"]["status"]
        question_summary["summary"]["mllm_processed_in_this_session"] = False
        question_summary["summary"]["mllm_pending_count"] = quality_summary["mllm"].get(
            "mllm_required_rows", 0
        )

    all_errors = [
        {"question_id": qid, **issue}
        for qid, buckets in issues.items()
        for issue in buckets["errors"]
    ]
    all_warnings = [
        {"question_id": qid, **issue}
        for qid, buckets in issues.items()
        for issue in buckets["warnings"]
    ]

    separation_checks = {
        "source_root": "source_files",
        "ready_root": "ready_for_eval",
        "source_assets_present": source_stats.get("present", 0),
        "source_assets_total": source_stats.get("assets", 0),
        "source_assets_all_present": source_stats.get("present", 0) == source_stats.get("assets", 0),
        "source_hashes_verified": args.verify_source_hashes,
        "source_hashes_verified_count": source_stats.get("hashes_verified", 0),
        "ready_document_hashes_verified_count": source_stats.get("ready_hashes_verified", 0),
        "ready_documents_under_ready_root": all(
            is_under(READY_ROOT / str(row.get("text_path") or ""), READY_ROOT)
            for row in corpus_rows
            if row.get("text_path")
        ),
        "source_assets_under_source_root": all(
            is_under(RAW_ROOT / str(asset.get("path") or asset.get("relative_path") or ""), RAW_ROOT / "source_files")
            for row in provenance_rows
            for asset in (row.get("copied_assets") or [])
        ),
        "ready_has_no_source_binary_extensions": not any(
            path.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png"}
            for path in READY_ROOT.rglob("*")
            if path.is_file()
        ),
        "legacy_mixed_roots_absent": not any(
            (RAW_ROOT / name).exists() for name in ("corpus", "eval_text", "qa-package")
        ),
    }

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    validation = {
        "schema": "moi-rag-bench-web-validation-v1",
        "generated_at": generated_at,
        "inputs": {
            "raw_root": str(RAW_ROOT.relative_to(ROOT)) if is_under(RAW_ROOT, ROOT) else str(RAW_ROOT),
            "ready_manifest": repo_relative(READY_ROOT / "manifest.json"),
            "corpus": repo_relative(READY_ROOT / "corpus.jsonl"),
            "questions": repo_relative(READY_ROOT / "questions.jsonl"),
            "gold": repo_relative(READY_ROOT / "gold.jsonl"),
            "provenance": repo_relative(RAW_ROOT / "provenance/documents.jsonl"),
        },
        "expected_counts": EXPECTED_COUNTS,
        "observed_counts": {
            "documents": len(corpus_rows),
            "questions": len(questions),
            "gold": len(gold_rows),
            "provenance": len(provenance_rows),
        },
        "source_ready_separation": separation_checks,
        "document_audit": {
            "documents_with_source_assets": source_stats.get("docs_with_source_assets", 0),
            "documents_with_native_source": source_stats.get("docs_with_native_source", 0),
            "source_asset_count": source_stats.get("assets", 0),
        },
        "golden_audit": question_summary["summary"],
        "quality_assessment": quality_summary,
        "metric_taxonomy": METRIC_TAXONOMY,
        "mllm_review": {
            "status": "deferred_to_external_agent",
            "processed_in_this_session": False,
            "pending_count": len(annotation_rows),
            "note": "No MLLM requirement classification or exclusion was performed in this session.",
        },
        "issues": {
            "error_count": len(all_errors),
            "warning_count": len(all_warnings),
            "errors": all_errors,
            "warnings": all_warnings,
        },
        "ready_for_eval": {
            "structurally_valid": not all_errors,
            "gold_alignment_valid": not any(
                issue["code"] in {"question_missing", "gold_missing", "gold_id_mismatch", "gold_doc_missing", "dataset_mismatch"}
                for issue in all_errors
            ),
            "review_required": bool(all_warnings),
        },
    }
    if quality_summary["mllm"]["available"]:
        validation["mllm_review"] = {
            "status": quality_summary["mllm"]["status"],
            "processed_in_this_session": False,
            "pending_count": quality_summary["mllm"].get("mllm_required_rows", 0),
            "source": quality_summary["mllm"].get("source"),
            "note": "页面展示最新 MLLM semantic audit artifact；网页构建本身不重新执行模型审核。",
        }

    documents_list = sorted(documents.values(), key=lambda row: row["doc_id"])
    write_json(DATA_ROOT / "documents.json", documents_list)
    write_json(DATA_ROOT / "cases.json", {
        "schema": "moi-rag-bench-web-cases-v1",
        "generated_at": generated_at,
        "count": len(cases),
        "cases": cases,
    })
    write_json(DATA_ROOT / "validation.json", validation)
    write_json(DATA_ROOT / "quality.json", quality_summary)
    write_json(DATA_ROOT / "metric-annotations-template.json", {
        "schema": "moi-rag-bench-metric-annotations-v1",
        "count": len(annotation_rows),
        "records": annotation_rows,
    })
    write_jsonl(DATA_ROOT / "metric-annotations-template.jsonl", annotation_rows)
    write_json(DATA_ROOT / "summary.json", {
        "schema": "moi-rag-bench-web-summary-v1",
        "generated_at": generated_at,
        "documents": len(documents),
        "questions": len(cases),
        "gold": len(gold_rows),
        "mllm_review_status": quality_summary["mllm"]["status"],
        "mllm_pending": quality_summary["mllm"].get("mllm_required_rows", len(annotation_rows)),
        "metric_annotation_rows": len(annotation_rows),
        "validation_errors": len(all_errors),
        "validation_warnings": len(all_warnings),
        "quality_assessment": quality_summary,
        "by_dataset": question_summary["summary"]["by_dataset"],
        "by_metric": question_summary["summary"]["by_metric"],
    })

    print(json.dumps({
        "status": "OK" if not all_errors else "ERROR",
        "web_root": str(WEB_ROOT),
        "documents": len(documents),
        "questions": len(cases),
        "gold": len(gold_rows),
        "mllm_review_status": quality_summary["mllm"]["status"],
        "mllm_pending": quality_summary["mllm"].get("mllm_required_rows", len(annotation_rows)),
        "metric_annotation_rows": len(annotation_rows),
        "validation_errors": len(all_errors),
        "validation_warnings": len(all_warnings),
        "quality_audit_rows": quality_summary["rows"],
        "quality_audit_matched_rows": quality_summary["case_audit_rows"],
        "quality_problematic_rows": quality_summary["problematic_count"],
        "quality_evaluation_rows": quality_summary["case_evaluation_rows"],
        "quality_evaluation_ledger_rows": quality_summary["evaluation_rows"],
        "source_assets": source_stats.get("assets", 0),
        "source_hashes_verified": source_stats.get("hashes_verified", 0),
    }, ensure_ascii=False, indent=2))

    if all_errors:
        raise SystemExit(1)
    return validation


if __name__ == "__main__":
    build()
