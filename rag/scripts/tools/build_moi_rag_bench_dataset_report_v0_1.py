#!/usr/bin/env python3
"""Build the paper-style MOI RAG Bench v0.1 text-only dataset report.

The report is generated only from the frozen local benchmark package and its
readiness artifacts.  The script validates the package before materializing a
canonical Data Analytics report artifact; the portable HTML is produced by the
plugin's delivery script in a separate step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
RAW_MANIFEST = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/manifest.json"
SELECTION_MANIFEST = ROOT / "datasets/moi-rag-bench-v0.1/manifest.json"
AUDIT_ROOT = ROOT / "runs/readiness/mllm-semantic-audit-20260820"
AUDIT_SUMMARY = AUDIT_ROOT / "after-summary.json"
AUDIT_ROWS = AUDIT_ROOT / "after-audit.jsonl"
AUDIT_SIGNOFF = AUDIT_ROOT / "text-only-semantic-audit-signoff.json"
PREFLIGHT = (
    ROOT
    / "runs/readiness/text-only-ready-preflight/20260820-text-only-ready/preflight.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "results/reports/moi-rag-bench-v0.1-text-only-dataset-report.artifact.json"
)
DEFAULT_CHART_MAP = (
    ROOT
    / "results/reports/moi-rag-bench-v0.1-text-only-dataset-report.chart-map.json"
)

EXPECTED = {"documents": 500, "questions": 1000, "gold": 1000}
SOURCE_LABELS = {
    "docbench": "DocBench",
    "enterprise": "EnterpriseRAG-Bench",
    "mmdocir": "MMDocIR",
    "multihop": "MultiHop-RAG",
}
SOURCE_COVERAGE = {
    "docbench": "长文档问答、元数据、文本投影、不可回答",
    "enterprise": "企业多源、语义问答、冲突、约束与信息缺失",
    "mmdocir": "跨领域长文档与页面/证据检索",
    "multihop": "比较、推断、时序与空查询",
}
MM_DOC_CLASSES = {
    "Academic paper",
    "Administration/Industry file",
    "Brochure",
    "Financial report",
    "Guidebook",
    "News",
    "Research report / Introduction",
    "Tutorial/Workshop",
}

METRIC_SPECS = {
    "qa_performance": {
        "label": "问答正确性",
        "definition": "在文本语料中生成与 reference answer 语义一致、且受证据支持的答案。",
        "recommended": "Normalized EM / token F1（短答案）；语义正确性与证据忠实度（开放答案）",
    },
    "multi_hop_reasoning": {
        "label": "多跳推理",
        "definition": "跨多个 gold 文档或证据完成比较、推断和时序组合。",
        "recommended": "答案正确性 + Complete@k / All-evidence recall + 推理证据覆盖",
    },
    "retrieval_performance": {
        "label": "检索性能",
        "definition": "从 500 文档全局语料中命中 gold 文档、页面或证据。",
        "recommended": "Hit@k、Recall@k、MRR、nDCG@k；报告 k∈{1,5,10}",
    },
    "structured_extraction": {
        "label": "结构化/元数据抽取",
        "definition": "从标题、章节、字段、统计信息或序列化表格中抽取目标值。",
        "recommended": "规范化 Exact Match、数值容差准确率、字段级 F1",
    },
    "text_projection_reasoning": {
        "label": "文本投影推理",
        "definition": "处理来源类型保留为 multimodal-*、但经审计已可由 Markdown 文本/表格独立回答的问题。",
        "recommended": "答案正确性 + 文本证据忠实度；不得使用图像输入",
    },
    "refusal_performance": {
        "label": "拒答/弃权",
        "definition": "识别 unanswerable、una-web、null_query 或 info_not_found，并避免无依据作答。",
        "recommended": "Abstention Precision / Recall / F1、False-refusal rate、回答正确率",
    },
    "constraint_following": {
        "label": "约束遵循",
        "definition": "满足问题规定的格式、范围、条件或输出约束。",
        "recommended": "约束逐项通过率 + 答案正确性",
    },
    "conflict_resolution": {
        "label": "冲突信息处理",
        "definition": "识别多源不一致，给出有证据的消歧、保留或冲突说明。",
        "recommended": "结论正确性 + 冲突识别率 + 证据引用正确率",
    },
    "completeness_coverage": {
        "label": "完整性覆盖",
        "definition": "覆盖问题要求的全部实体、事实或子任务。",
        "recommended": "Reference-claim recall / completeness + 无效额外事实率",
    },
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[int], fraction: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = round((len(ordered) - 1) * fraction)
    return ordered[max(0, min(len(ordered) - 1, index))]


def metric_for(question: Mapping[str, Any]) -> str:
    """Return the report's transparent, single primary evaluation dimension."""

    dataset = str(question.get("source_dataset") or "").casefold()
    question_type = str(question.get("question_type") or "").casefold()
    metadata = question.get("metadata")
    original_type = str(
        metadata.get("original_type") if isinstance(metadata, Mapping) else ""
    ).casefold()
    answerable = question.get("answerable")

    if dataset == "mmdocir":
        return "retrieval_performance"
    if dataset == "docbench":
        if original_type in {"unanswerable", "una-web"} or answerable is False:
            return "refusal_performance"
        if original_type in {"multimodal-t", "multimodal-f"}:
            return "text_projection_reasoning"
        if original_type == "meta-data":
            return "structured_extraction"
        return "qa_performance"
    if dataset == "multihop":
        if question_type == "null_query" or answerable is False:
            return "refusal_performance"
        return "multi_hop_reasoning"
    if dataset == "enterprise":
        return {
            "info_not_found": "refusal_performance",
            "conflicting_info": "conflict_resolution",
            "constrained": "constraint_following",
            "completeness": "completeness_coverage",
        }.get(question_type, "qa_performance")
    return "qa_performance"


def assert_ids_unique(rows: list[dict[str, Any]], key: str, label: str) -> set[str]:
    ids = [str(row.get(key) or "") for row in rows]
    if any(not item for item in ids):
        raise ValueError(f"{label} contains an empty {key}")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} contains duplicate {key} values")
    return set(ids)


def validate_inputs(
    manifest: dict[str, Any],
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    audit_summary: dict[str, Any],
    audit_signoff: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, int]:
    observed = {
        "documents": len(corpus),
        "questions": len(questions),
        "gold": len(gold),
    }
    if observed != EXPECTED or manifest.get("counts") != EXPECTED:
        raise ValueError(f"Count contract failed: expected={EXPECTED}, observed={observed}")
    if manifest.get("condition") != "text-only-no-mllm":
        raise ValueError("Unexpected benchmark condition")
    if manifest.get("mllm_required") is not False or manifest.get("image_llm") != "NOT_APPLICABLE":
        raise ValueError("Manifest does not freeze the pure-text condition")

    document_ids = assert_ids_unique(corpus, "doc_id", "corpus")
    question_ids = assert_ids_unique(questions, "question_id", "questions")
    gold_ids = assert_ids_unique(gold, "question_id", "gold")
    if question_ids != gold_ids:
        raise ValueError("Question and gold ID sets differ")
    missing_refs = sorted(
        {
            str(doc_id)
            for row in gold
            for doc_id in row.get("gold_doc_ids") or []
            if str(doc_id) not in document_ids
        }
    )
    if missing_refs:
        raise ValueError(f"Gold rows reference missing documents: {missing_refs[:5]}")

    for row in corpus:
        relative = Path(str(row.get("text_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe document path: {relative}")
        path = PACKAGE / relative
        if not path.is_file() or path.suffix.casefold() != ".md":
            raise ValueError(f"Missing Markdown ingest file: {relative}")
        if sha256_file(path) != row.get("sha256"):
            raise ValueError(f"Corpus hash mismatch: {relative}")

    excluded_ids = {str(row.get("question_id")) for row in exclusions}
    replacement_ids = {str(row.get("replacement_question_id")) for row in replacements}
    if len(exclusions) != 39 or len(replacements) != 39:
        raise ValueError("The 39-for-39 replacement contract failed")
    if excluded_ids & question_ids:
        raise ValueError("Excluded visual-gap questions remain in the eval set")
    if not replacement_ids <= question_ids:
        raise ValueError("One or more replacement questions are absent")
    if any(row.get("evidence_validation") != "passed" for row in replacements):
        raise ValueError("A replacement failed evidence validation")

    hash_entries = manifest.get("output_hashes")
    if not isinstance(hash_entries, Mapping) or len(hash_entries) != 506:
        raise ValueError("Expected 506 frozen output hashes")
    for relative, expected_hash in hash_entries.items():
        path = PACKAGE / str(relative)
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"Frozen output hash mismatch: {relative}")

    audit_counts = audit_summary.get("counts") or {}
    if (
        audit_summary.get("result") != "PASS"
        or audit_counts.get("questions") != 1000
        or audit_counts.get("mllm_required_rows") != 0
        or audit_counts.get("text_projection_unsafe_rows") != 0
        or audit_counts.get("review_required_rows") != 0
        or audit_counts.get("text_projection_safe_rows") != 109
        or audit_summary.get("unsafe_question_ids")
    ):
        raise ValueError("MLLM semantic audit did not pass the pure-text contract")
    if (
        audit_signoff.get("status") != "PASS"
        or audit_signoff.get("decision") != "pure_text_llm_only"
        or audit_signoff.get("manifest_sha256") != sha256_file(PACKAGE / "manifest.json")
        or audit_signoff.get("audit_summary_sha256") != sha256_file(AUDIT_SUMMARY)
        or audit_signoff.get("audit_rows_sha256") != sha256_file(AUDIT_ROWS)
    ):
        raise ValueError("MLLM signoff hashes or decision do not match current files")
    if not (
        preflight.get("ready") is True
        and preflight.get("package_valid") is True
        and preflight.get("scope_verified") is True
        and preflight.get("scope") == "global"
        and preflight.get("dry_run") is True
        and preflight.get("network_performed") is False
    ):
        raise ValueError("Package dry-run preflight is not ready")

    return {
        "output_hashes_verified": len(hash_entries),
        "excluded_ids_absent": len(excluded_ids),
        "replacement_ids_present": len(replacement_ids),
        "gold_references_resolved": sum(len(row.get("gold_doc_ids") or []) for row in gold),
    }


def build_profiles(
    manifest: dict[str, Any],
    raw_manifest: dict[str, Any],
    questions: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    audit_summary: dict[str, Any],
    audit_rows: list[dict[str, Any]],
    validation: dict[str, int],
) -> dict[str, list[dict[str, Any]]]:
    gold_by_id = {str(row["question_id"]): row for row in gold}
    source_questions = Counter(str(row["source_dataset"]) for row in questions)
    source_documents = Counter(
        str((row.get("metadata") or {}).get("source_dataset")) for row in corpus
    )
    source_composition: list[dict[str, Any]] = []
    for source in ("docbench", "enterprise", "multihop", "mmdocir"):
        documents = source_documents[source]
        question_count = source_questions[source]
        source_composition.append(
            {
                "source": SOURCE_LABELS[source],
                "source_key": source,
                "documents": documents,
                "questions": question_count,
                "doc_share": documents / len(corpus),
                "qa_share": question_count / len(questions),
                "qa_per_doc": round(question_count / documents, 2),
                "coverage": SOURCE_COVERAGE[source],
            }
        )

    corpus_chars: dict[str, list[int]] = defaultdict(list)
    corpus_bytes: dict[str, list[int]] = defaultdict(list)
    for row in corpus:
        source = str((row.get("metadata") or {}).get("source_dataset"))
        path = PACKAGE / str(row["text_path"])
        text = path.read_text(encoding="utf-8")
        corpus_chars[source].append(len(text))
        corpus_bytes[source].append(path.stat().st_size)
    corpus_profile: list[dict[str, Any]] = []
    for source in ("docbench", "enterprise", "multihop", "mmdocir"):
        chars = corpus_chars[source]
        byte_values = corpus_bytes[source]
        corpus_profile.append(
            {
                "source": SOURCE_LABELS[source],
                "documents": len(chars),
                "characters": sum(chars),
                "size_mib": round(sum(byte_values) / 1024 / 1024, 2),
                "mean_chars": round(statistics.mean(chars)),
                "median_chars": round(statistics.median(chars)),
                "p95_chars": percentile(chars, 0.95),
                "max_chars": max(chars),
            }
        )

    question_type_counts = Counter(str(row["question_type"]) for row in questions)
    type_by_source = Counter(
        (str(row["question_type"]), str(row["source_dataset"])) for row in questions
    )
    question_types: list[dict[str, Any]] = []
    for question_type, count in question_type_counts.most_common():
        question_types.append(
            {
                "question_type": question_type,
                "count": count,
                "share": count / len(questions),
                "docbench": type_by_source[(question_type, "docbench")],
                "enterprise": type_by_source[(question_type, "enterprise")],
                "multihop": type_by_source[(question_type, "multihop")],
                "mmdocir": type_by_source[(question_type, "mmdocir")],
                "modality_note": (
                    "来源类型；已审计为文本投影安全"
                    if question_type.startswith("multimodal")
                    else "纯文本评测类型"
                ),
            }
        )

    metric_counts = Counter(metric_for(row) for row in questions)
    metric_by_source = Counter(
        (metric_for(row), str(row["source_dataset"])) for row in questions
    )
    metric_distribution: list[dict[str, Any]] = []
    for metric_key, count in metric_counts.most_common():
        spec = METRIC_SPECS[metric_key]
        metric_distribution.append(
            {
                "metric_key": metric_key,
                "metric": spec["label"],
                "count": count,
                "share": count / len(questions),
                "definition": spec["definition"],
                "recommended": spec["recommended"],
                "source_breakdown": "；".join(
                    f"{SOURCE_LABELS[source]} {metric_by_source[(metric_key, source)]}"
                    for source in ("docbench", "enterprise", "multihop", "mmdocir")
                    if metric_by_source[(metric_key, source)]
                ),
            }
        )

    domain_counts = Counter(
        str((row.get("metadata") or {}).get("selection_domain") or "(missing)")
        for row in questions
    )
    domain_distribution: list[dict[str, Any]] = []
    for domain, count in domain_counts.most_common():
        if domain == "docbench":
            source = "DocBench"
        elif domain == "multihop":
            source = "MultiHop-RAG"
        elif domain in MM_DOC_CLASSES:
            source = "MMDocIR"
        else:
            source = "EnterpriseRAG-Bench"
        domain_distribution.append(
            {
                "domain": domain,
                "source": source,
                "questions": count,
                "share": count / len(questions),
            }
        )

    evidence_class_counts = Counter(
        str((row.get("metadata") or {}).get("selection_evidence_class") or "(missing)")
        for row in questions
    )
    evidence_classes = [
        {
            "evidence_class": evidence_class,
            "count": count,
            "share": count / len(questions),
            "interpretation": (
                "来源筛选标签；不表示运行时需要图像"
                if evidence_class in {"multimodal", "table", "chart", "figure", "layout"}
                else "来源筛选标签"
            ),
        }
        for evidence_class, count in evidence_class_counts.most_common()
    ]

    evidence_by_source: dict[str, Counter[str]] = defaultdict(Counter)
    gold_doc_count = Counter()
    evidence_count = Counter()
    question_lengths: list[int] = []
    answer_lengths: list[int] = []
    for question in questions:
        source = str(question["source_dataset"])
        gold_row = gold_by_id[str(question["question_id"])]
        gold_docs = gold_row.get("gold_doc_ids") or []
        evidence = gold_row.get("gold_evidence") or []
        stats = evidence_by_source[source]
        stats["questions"] += 1
        stats["gold_documents"] += len(gold_docs)
        stats["evidence_items"] += len(evidence)
        stats["with_gold_documents"] += bool(gold_docs)
        stats["with_evidence"] += bool(evidence)
        stats["multi_document"] += len(gold_docs) >= 2
        gold_doc_count[len(gold_docs)] += 1
        evidence_count[len(evidence)] += 1
        question_lengths.append(len(str(question.get("question") or "")))
        answer_lengths.append(len(str(gold_row.get("reference_answer") or "")))
    gold_profile: list[dict[str, Any]] = []
    for source in ("docbench", "enterprise", "multihop", "mmdocir"):
        stats = evidence_by_source[source]
        gold_profile.append(
            {
                "source": SOURCE_LABELS[source],
                "questions": stats["questions"],
                "with_gold_documents": stats["with_gold_documents"],
                "with_evidence": stats["with_evidence"],
                "multi_document": stats["multi_document"],
                "gold_documents_total": stats["gold_documents"],
                "evidence_items_total": stats["evidence_items"],
                "avg_gold_docs": round(stats["gold_documents"] / stats["questions"], 2),
                "avg_evidence_items": round(stats["evidence_items"] / stats["questions"], 2),
            }
        )

    length_profile = [
        {
            "field": "Question",
            "unit": "Unicode 字符",
            "mean": round(statistics.mean(question_lengths), 1),
            "median": statistics.median(question_lengths),
            "p95": percentile(question_lengths, 0.95),
            "max": max(question_lengths),
            "empty": sum(value == 0 for value in question_lengths),
        },
        {
            "field": "Reference answer",
            "unit": "Unicode 字符",
            "mean": round(statistics.mean(answer_lengths), 1),
            "median": statistics.median(answer_lengths),
            "p95": percentile(answer_lengths, 0.95),
            "max": max(answer_lengths),
            "empty": sum(value == 0 for value in answer_lengths),
        },
    ]

    before = manifest["distribution_delta"]["question_type"]["before"]
    after = manifest["distribution_delta"]["question_type"]["after"]
    changed_types = sorted(set(before) | set(after))
    derivation_delta = [
        {
            "question_type": question_type,
            "parent": before.get(question_type, 0),
            "text_only": after.get(question_type, 0),
            "delta": after.get(question_type, 0) - before.get(question_type, 0),
        }
        for question_type in changed_types
        if before.get(question_type, 0) != after.get(question_type, 0)
    ]
    transition_counts = Counter(
        (str(row["source_question_type"]), str(row["replacement_question_type"]))
        for row in replacements
    )
    replacement_transitions = [
        {
            "from_type": source_type,
            "to_type": replacement_type,
            "count": count,
        }
        for (source_type, replacement_type), count in transition_counts.most_common()
    ]

    multimodal_rows = [
        row
        for row in audit_rows
        if str(row.get("original_type") or "").casefold().startswith("multimodal")
    ]
    manual_ids = set(audit_summary.get("manual_decision_ids_present") or [])
    confidence_counts = Counter(
        str((row.get("decision") or {}).get("confidence") or "unknown")
        for row in multimodal_rows
    )
    audit_detail = [
        {
            "audit_slice": "来源类型 multimodal-*",
            "rows": len(multimodal_rows),
            "safe": sum(row.get("status") == "TEXT_PROJECTION_SAFE" for row in multimodal_rows),
            "unsafe_or_review": sum(
                row.get("status")
                in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE", "REVIEW_REQUIRED"}
                for row in multimodal_rows
            ),
            "manual_decisions": len(manual_ids),
            "high_confidence": confidence_counts["high"],
            "medium_confidence": confidence_counts["medium"],
        }
    ]

    source_modes = raw_manifest.get("source_modes") or {}
    source_asset_modes = [
        {
            "source_mode": "原始 PDF",
            "documents": source_modes.get("original_pdf", 0),
            "raw_assets": source_modes.get("original_pdf", 0),
            "ready_representation": "每文档一份 Markdown",
        },
        {
            "source_mode": "原始 Markdown",
            "documents": source_modes.get("original_md", 0),
            "raw_assets": source_modes.get("original_md", 0),
            "ready_representation": "每文档一份 Markdown",
        },
        {
            "source_mode": "MMDocIR 页面图像",
            "documents": source_modes.get("original_page_images", 0),
            "raw_assets": raw_manifest["counts"]["mmdocir_page_asset_count"],
            "ready_representation": "按 source document 重构 Markdown",
        },
    ]

    audit_checks = [
        {"check": "文档 / Questions / Gold 精确数量", "result": "PASS", "value": "500 / 1,000 / 1,000", "scope": "最终 eval 包"},
        {"check": "Question 与 Gold ID 一一对齐", "result": "PASS", "value": "1,000 / 1,000", "scope": "最终 eval 包"},
        {"check": "Gold 文档引用可解析", "result": "PASS", "value": f"{validation['gold_references_resolved']:,} 个引用", "scope": "最终 eval 包"},
        {"check": "冻结输出哈希", "result": "PASS", "value": f"{validation['output_hashes_verified']} / 506", "scope": "最终 eval 包"},
        {"check": "视觉语义缺口已排除", "result": "PASS", "value": f"{validation['excluded_ids_absent']} / 39", "scope": "排除清单"},
        {"check": "文本安全替换已进入评测集", "result": "PASS", "value": f"{validation['replacement_ids_present']} / 39", "scope": "替换审计"},
        {"check": "显式媒体输入", "result": "PASS", "value": "0", "scope": "MLLM 语义审计"},
        {"check": "MLLM required / unsafe / review", "result": "PASS", "value": "0 / 0 / 0", "scope": "MLLM 语义审计"},
        {"check": "离线 package preflight", "result": "PASS", "value": "ready=true；scope=global", "scope": "FastGPT dry-run（未测服务）"},
    ]

    file_contract = [
        {"path": "manifest.json", "rows_or_files": "1", "role": "版本、条件、配额、分布差异与 506 个输出哈希", "evaluator_use": "先读取并校验"},
        {"path": "corpus.jsonl", "rows_or_files": "500", "role": "doc_id、Markdown 路径、哈希与来源元数据", "evaluator_use": "建立文档索引"},
        {"path": "documents/<doc_id>.md", "rows_or_files": "500", "role": "唯一允许摄入的纯文本语料", "evaluator_use": "上传/切分/Embedding"},
        {"path": "questions.jsonl", "rows_or_files": "1,000", "role": "问题、answerability、来源类型与 gold doc IDs", "evaluator_use": "发送 query；不要把 gold 字段拼进 prompt"},
        {"path": "gold.jsonl", "rows_or_files": "1,000", "role": "reference answer、gold 文档与 evidence", "evaluator_use": "运行结束后离线评分"},
        {"path": "visual-exclusions.jsonl", "rows_or_files": "39", "role": "已排除的视觉语义缺口", "evaluator_use": "审计；不得加入本条件"},
        {"path": "replacement-audit.jsonl", "rows_or_files": "39", "role": "逐条替换、答案承载文本和文档哈希", "evaluator_use": "复核派生过程"},
        {"path": "README.md", "rows_or_files": "1", "role": "数据包契约与重建命令", "evaluator_use": "操作入口"},
    ]

    scoring_protocol = [
        {"layer": "检索：文档级", "applicable_rows": 954, "primary_metrics": "Hit@1/5/10；Recall@1/5/10；MRR；nDCG@10", "aggregation": "先逐题，再按来源/主指标 macro；空 gold_doc_ids 不进分母"},
        {"layer": "检索：证据级", "applicable_rows": 803, "primary_metrics": "Evidence Recall@k；Complete@k；locator/quote 命中", "aggregation": "仅 gold_evidence 非空；文本与 locator 分开报告"},
        {"layer": "答案：短答案", "applicable_rows": "按答案形态", "primary_metrics": "Normalized EM；token F1；数值容差准确率", "aggregation": "按题型 macro，并报告有效分母"},
        {"layer": "答案：开放答案", "applicable_rows": "按答案形态", "primary_metrics": "语义正确性；完整性；faithfulness；引用正确率", "aggregation": "冻结 Judge 模型/提示词；盲评；保留原始判分"},
        {"layer": "拒答/弃权", "applicable_rows": 91, "primary_metrics": "Abstention P/R/F1；false refusal；hallucinated-answer rate", "aggregation": "77 个显式 false + 14 个 info_not_found 语义拒答"},
        {"layer": "多跳", "applicable_rows": 221, "primary_metrics": "答案正确性；All-evidence recall；Complete@k", "aggregation": "要求全部必要文档/证据覆盖；同时报告部分召回"},
        {"layer": "系统", "applicable_rows": 1000, "primary_metrics": "成功率；P50/P95 latency；token/cost；超时率", "aggregation": "失败计入系统可靠性，不进入答案 Judge 分母时须另报"},
    ]

    return {
        "summary_scale": [
            {
                "documents": len(corpus),
                "questions": len(questions),
                "gold": len(gold),
                "sources": len(source_questions),
            }
        ],
        "summary_audit": [
            {
                "excluded": manifest["excluded_visual_qa_count"],
                "replacements": manifest["replacement_count"],
                "mllm_required": audit_summary["counts"]["mllm_required_rows"],
                "text_projection_safe": audit_summary["counts"]["text_projection_safe_rows"],
            }
        ],
        "summary_gold": [
            {
                "with_gold_documents": sum(bool(row.get("gold_doc_ids")) for row in gold),
                "with_evidence": sum(bool(row.get("gold_evidence")) for row in gold),
                "reference_answers": sum(bool(str(row.get("reference_answer") or "")) for row in gold),
                "multi_document": sum(len(row.get("gold_doc_ids") or []) >= 2 for row in gold),
            }
        ],
        "source_composition": source_composition,
        "corpus_profile": corpus_profile,
        "question_types": question_types,
        "metric_distribution": metric_distribution,
        "domain_distribution": domain_distribution,
        "evidence_classes": evidence_classes,
        "gold_profile": gold_profile,
        "length_profile": length_profile,
        "derivation_delta": derivation_delta,
        "replacement_transitions": replacement_transitions,
        "audit_detail": audit_detail,
        "source_asset_modes": source_asset_modes,
        "audit_checks": audit_checks,
        "file_contract": file_contract,
        "scoring_protocol": scoring_protocol,
    }


def source_manifest_entries() -> list[dict[str, Any]]:
    entries = [
        {"id": "final_manifest", "label": "Final text-only package manifest", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/manifest.json"},
        {"id": "final_questions", "label": "Final questions", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/questions.jsonl"},
        {"id": "final_gold", "label": "Final gold labels", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/gold.jsonl"},
        {"id": "final_corpus", "label": "Final corpus index and Markdown files", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/corpus.jsonl"},
        {"id": "questions_gold_join", "label": "Question–gold applicability profile", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/{questions,gold}.jsonl"},
        {"id": "primary_metric_mapping", "label": "Deterministic primary-metric mapping", "path": "tools/build_moi_rag_bench_dataset_report_v0_1.py"},
        {"id": "raw_manifest", "label": "Separated raw/ready corpus manifest", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/manifest.json"},
        {"id": "selection_manifest", "label": "Unified benchmark selection manifest", "path": "datasets/moi-rag-bench-v0.1/manifest.json"},
        {"id": "replacement_audit", "label": "Text-safe replacement audit", "path": "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval/replacement-audit.jsonl"},
        {"id": "mllm_audit", "label": "MLLM semantic audit", "path": "runs/readiness/mllm-semantic-audit-20260820/after-summary.json"},
        {"id": "mllm_signoff", "label": "Pure-text semantic audit signoff", "path": "runs/readiness/mllm-semantic-audit-20260820/text-only-semantic-audit-signoff.json"},
        {"id": "readiness_bundle", "label": "Integrity, MLLM, and preflight readiness bundle", "path": "runs/readiness/{mllm-semantic-audit-20260820,text-only-ready-preflight}/"},
        {"id": "preflight", "label": "Offline package preflight", "path": "runs/readiness/text-only-ready-preflight/20260820-text-only-ready/preflight.json"},
    ]
    for entry in entries:
        entry["href"] = (
            "results/reports/"
            "moi-rag-bench-v0.1-text-only-dataset-report.queries.sql"
        )
    return entries


def top_level_sources(generated_at: str) -> list[dict[str, Any]]:
    descriptions = {
        "final_manifest": "Loads the frozen package contract, allocations, derivation deltas, and output hashes.",
        "final_questions": "Profiles the 1,000 final question records by source, type, domain, and answerability.",
        "final_gold": "Profiles reference answers, gold documents, and evidence applicability.",
        "final_corpus": "Profiles the 500 indexed Markdown files and verifies declared SHA-256 values.",
        "questions_gold_join": "Joins questions and gold by question_id to compute scoring denominators by source.",
        "primary_metric_mapping": "Applies the documented deterministic QA-to-primary-metric mapping over final questions.",
        "raw_manifest": "Describes the mutually exclusive source_files, ready_for_eval, and provenance layers.",
        "selection_manifest": "Documents fixed quotas, source candidate pools, deduplication, and historical-run basis.",
        "replacement_audit": "Profiles the 39-for-39 text-safe replacement transitions and evidence checks.",
        "mllm_audit": "Checks explicit media inputs and semantic text-projection safety for all 1,000 questions.",
        "mllm_signoff": "Freezes the PASS decision and hashes for manifest, audit rows, and audit summary.",
        "readiness_bundle": "Combines integrity checks, semantic audit, signoff, and offline package preflight.",
        "preflight": "Validates package schema and global scope without network or service execution.",
    }
    return [
        {
            "id": entry["id"],
            "path": "results/reports/moi-rag-bench-v0.1-text-only-dataset-report.queries.sql",
            "query": {
                "engine": "local-filesystem",
                "description": descriptions[entry["id"]],
                "executed_at": generated_at,
                "tables_used": [entry["path"]],
            },
        }
        for entry in source_manifest_entries()
    ]


def build_artifact(
    profiles: dict[str, list[dict[str, Any]]],
    audit_signoff: dict[str, Any],
) -> dict[str, Any]:
    generated_at = str(
        audit_signoff.get("generated_at")
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    title = "MOI RAG Bench v0.1 Text-Only Ready-for-Eval：数据集技术报告"

    cards = [
        {
            "id": "scale_card",
            "description": "冻结数据包规模。",
            "dataset": "summary_scale",
            "sourceId": "final_manifest",
            "metrics": [
                {"label": "文档", "field": "documents", "format": "number"},
                {"label": "QA", "field": "questions", "format": "number"},
                {"label": "Gold", "field": "gold", "format": "number"},
                {"label": "来源包", "field": "sources", "format": "number"},
            ],
        },
        {
            "id": "audit_card",
            "description": "文本条件派生与 MLLM 语义审计。",
            "dataset": "summary_audit",
            "sourceId": "mllm_audit",
            "metrics": [
                {"label": "视觉缺口排除", "field": "excluded", "format": "number"},
                {"label": "文本安全替换", "field": "replacements", "format": "number"},
                {"label": "文本投影安全", "field": "text_projection_safe", "format": "number"},
                {"label": "需 MLLM", "field": "mllm_required", "format": "number"},
            ],
        },
        {
            "id": "gold_card",
            "description": "Gold 标注及适用分母。",
            "dataset": "summary_gold",
            "sourceId": "final_gold",
            "metrics": [
                {"label": "有 gold 文档", "field": "with_gold_documents", "format": "number"},
                {"label": "有 evidence", "field": "with_evidence", "format": "number"},
                {"label": "Reference answer", "field": "reference_answers", "format": "number"},
                {"label": "多文档 QA", "field": "multi_document", "format": "number"},
            ],
        },
    ]

    charts = [
        {
            "id": "source_qa_chart",
            "title": "四个来源形成互补任务组合",
            "subtitle": "DocBench 提供 45% QA，MultiHop-RAG 提供 25%；EnterpriseRAG-Bench 与 MMDocIR 各占 15%。",
            "type": "horizontalBar",
            "dataset": "source_composition",
            "sourceId": "final_manifest",
            "encodings": {
                "x": {"field": "source", "type": "nominal", "label": "来源"},
                "y": {"field": "questions", "type": "quantitative", "label": "QA 数"},
                "tooltip": [
                    {"field": "documents", "type": "quantitative", "label": "文档数"},
                    {"field": "qa_share", "type": "quantitative", "label": "QA 占比", "format": "percent"},
                ],
            },
            "xAxisTitle": "QA 数",
            "valueFormat": "number",
            "layout": "full",
        },
        {
            "id": "metric_distribution_chart",
            "title": "主评估维度覆盖检索、生成、拒答与推理",
            "subtitle": "每条 QA 分配一个可人工覆写的主维度；300 条常规问答与 221 条多跳推理构成最大两个分组。",
            "type": "horizontalBar",
            "dataset": "metric_distribution",
            "sourceId": "primary_metric_mapping",
            "encodings": {
                "x": {"field": "metric", "type": "nominal", "label": "主评估维度"},
                "y": {"field": "count", "type": "quantitative", "label": "QA 数"},
                "tooltip": [
                    {"field": "share", "type": "quantitative", "label": "占比", "format": "percent"},
                    {"field": "source_breakdown", "type": "nominal", "label": "来源构成"},
                ],
            },
            "xAxisTitle": "QA 数",
            "valueFormat": "number",
            "layout": "full",
        },
    ]

    def table(
        table_id: str,
        title_text: str,
        dataset: str,
        source_id: str,
        columns: list[dict[str, Any]],
        subtitle: str = "",
        sort: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": table_id,
            "title": title_text,
            "dataset": dataset,
            "sourceId": source_id,
            "columns": columns,
        }
        if subtitle:
            value["subtitle"] = subtitle
        if sort:
            value["defaultSort"] = {"field": sort[0], "direction": sort[1]}
        return value

    tables = [
        table(
            "source_composition_table",
            "来源配额与覆盖重点",
            "source_composition",
            "final_manifest",
            [
                {"field": "source", "label": "来源", "type": "text"},
                {"field": "documents", "label": "文档", "format": "number"},
                {"field": "doc_share", "label": "文档占比", "format": "percent"},
                {"field": "questions", "label": "QA", "format": "number"},
                {"field": "qa_share", "label": "QA 占比", "format": "percent"},
                {"field": "qa_per_doc", "label": "QA/文档", "format": "number"},
                {"field": "coverage", "label": "覆盖重点", "type": "text"},
            ],
        ),
        table(
            "corpus_profile_table",
            "Markdown 语料规模",
            "corpus_profile",
            "final_corpus",
            [
                {"field": "source", "label": "来源", "type": "text"},
                {"field": "documents", "label": "文档", "format": "number"},
                {"field": "size_mib", "label": "MiB", "format": "number"},
                {"field": "characters", "label": "总字符", "format": "number"},
                {"field": "median_chars", "label": "中位字符", "format": "number"},
                {"field": "p95_chars", "label": "P95 字符", "format": "number"},
                {"field": "max_chars", "label": "最大字符", "format": "number"},
            ],
            "字符数按 Unicode 字符统计，不等同于模型 token 数。",
        ),
        table(
            "question_types_table",
            "21 类来源问题类型",
            "question_types",
            "final_questions",
            [
                {"field": "question_type", "label": "Question type", "type": "text"},
                {"field": "count", "label": "QA", "format": "number"},
                {"field": "share", "label": "占比", "format": "percent"},
                {"field": "docbench", "label": "DocBench", "format": "number"},
                {"field": "enterprise", "label": "Enterprise", "format": "number"},
                {"field": "multihop", "label": "MultiHop", "format": "number"},
                {"field": "mmdocir", "label": "MMDocIR", "format": "number"},
                {"field": "modality_note", "label": "模态说明", "type": "text"},
            ],
            "multimodal-* 是来源谱系标签；最终条件不要求图像输入。",
            sort=("count", "desc"),
        ),
        table(
            "metric_distribution_table",
            "QA 级主评估维度与建议计分",
            "metric_distribution",
            "primary_metric_mapping",
            [
                {"field": "metric", "label": "主维度", "type": "text"},
                {"field": "count", "label": "QA", "format": "number"},
                {"field": "share", "label": "占比", "format": "percent"},
                {"field": "source_breakdown", "label": "来源构成", "type": "text"},
                {"field": "definition", "label": "定义", "type": "text"},
                {"field": "recommended", "label": "建议指标", "type": "text"},
            ],
            "主维度由可复现规则生成，评测 UI 中仍可逐题人工覆写并导出 JSON。",
            sort=("count", "desc"),
        ),
        table(
            "domain_distribution_table",
            "31 个来源领域标签",
            "domain_distribution",
            "final_questions",
            [
                {"field": "domain", "label": "领域标签", "type": "text"},
                {"field": "source", "label": "来源", "type": "text"},
                {"field": "questions", "label": "QA", "format": "number"},
                {"field": "share", "label": "占比", "format": "percent"},
            ],
            "这是来源选择时的标签体系，不是跨来源统一 ontology。",
            ("questions", "desc"),
        ),
        table(
            "evidence_classes_table",
            "来源 evidence class 分布",
            "evidence_classes",
            "final_questions",
            [
                {"field": "evidence_class", "label": "Evidence class", "type": "text"},
                {"field": "count", "label": "QA", "format": "number"},
                {"field": "share", "label": "占比", "format": "percent"},
                {"field": "interpretation", "label": "解释", "type": "text"},
            ],
            sort=("count", "desc"),
        ),
        table(
            "gold_profile_table",
            "Gold 文档与 evidence 适用性",
            "gold_profile",
            "questions_gold_join",
            [
                {"field": "source", "label": "来源", "type": "text"},
                {"field": "questions", "label": "QA", "format": "number"},
                {"field": "with_gold_documents", "label": "有 gold 文档", "format": "number"},
                {"field": "with_evidence", "label": "有 evidence", "format": "number"},
                {"field": "multi_document", "label": "多文档", "format": "number"},
                {"field": "avg_gold_docs", "label": "平均 gold 文档", "format": "number"},
                {"field": "avg_evidence_items", "label": "平均 evidence", "format": "number"},
            ],
            "缺少 gold evidence 不等于标签错误；应按任务语义使用适用分母。",
        ),
        table(
            "length_profile_table",
            "问题与参考答案长度",
            "length_profile",
            "questions_gold_join",
            [
                {"field": "field", "label": "字段", "type": "text"},
                {"field": "unit", "label": "单位", "type": "text"},
                {"field": "mean", "label": "均值", "format": "number"},
                {"field": "median", "label": "中位数", "format": "number"},
                {"field": "p95", "label": "P95", "format": "number"},
                {"field": "max", "label": "最大", "format": "number"},
                {"field": "empty", "label": "空值", "format": "number"},
            ],
        ),
        table(
            "source_asset_modes_table",
            "原始资产与 ready 表示完全分离",
            "source_asset_modes",
            "raw_manifest",
            [
                {"field": "source_mode", "label": "源资产模式", "type": "text"},
                {"field": "documents", "label": "覆盖文档", "format": "number"},
                {"field": "raw_assets", "label": "原始资产数", "format": "number"},
                {"field": "ready_representation", "label": "Ready 表示", "type": "text"},
            ],
        ),
        table(
            "derivation_delta_table",
            "文本条件派生导致的问题类型变化",
            "derivation_delta",
            "final_manifest",
            [
                {"field": "question_type", "label": "Question type", "type": "text"},
                {"field": "parent", "label": "Parent", "format": "number"},
                {"field": "text_only", "label": "Text-only", "format": "number"},
                {"field": "delta", "label": "Δ", "format": "number", "signed": True},
            ],
        ),
        table(
            "replacement_transitions_table",
            "39 条替换的类型转移",
            "replacement_transitions",
            "replacement_audit",
            [
                {"field": "from_type", "label": "排除类型", "type": "text"},
                {"field": "to_type", "label": "替换类型", "type": "text"},
                {"field": "count", "label": "条数", "format": "number"},
            ],
            sort=("count", "desc"),
        ),
        table(
            "audit_detail_table",
            "保留 multimodal-* 来源类型的语义审计",
            "audit_detail",
            "mllm_audit",
            [
                {"field": "audit_slice", "label": "审计切片", "type": "text"},
                {"field": "rows", "label": "总数", "format": "number"},
                {"field": "safe", "label": "文本投影安全", "format": "number"},
                {"field": "unsafe_or_review", "label": "Unsafe / review", "format": "number"},
                {"field": "manual_decisions", "label": "显式人工决策", "format": "number"},
                {"field": "high_confidence", "label": "高置信", "format": "number"},
                {"field": "medium_confidence", "label": "中置信", "format": "number"},
            ],
        ),
        table(
            "audit_checks_table",
            "发布前质量闸门",
            "audit_checks",
            "readiness_bundle",
            [
                {"field": "check", "label": "检查项", "type": "text"},
                {"field": "result", "label": "结果", "type": "text"},
                {"field": "value", "label": "观测值", "type": "text"},
                {"field": "scope", "label": "范围", "type": "text"},
            ],
        ),
        table(
            "scoring_protocol_table",
            "建议评测层与有效分母",
            "scoring_protocol",
            "primary_metric_mapping",
            [
                {"field": "layer", "label": "评测层", "type": "text"},
                {"field": "applicable_rows", "label": "适用 QA", "type": "text"},
                {"field": "primary_metrics", "label": "主指标", "type": "text"},
                {"field": "aggregation", "label": "聚合规则", "type": "text"},
            ],
        ),
        table(
            "file_contract_table",
            "Eval 包文件契约",
            "file_contract",
            "final_manifest",
            [
                {"field": "path", "label": "相对路径", "type": "text"},
                {"field": "rows_or_files", "label": "行/文件", "type": "text"},
                {"field": "role", "label": "作用", "type": "text"},
                {"field": "evaluator_use", "label": "评测器用法", "type": "text"},
            ],
        ),
    ]

    blocks: list[dict[str, Any]] = [
        {
            "id": "title",
            "type": "markdown",
            "body": f"# {title}\n\n**版本：** v0.1 · `text-only-no-mllm` 派生条件  \n**状态：** READY · `competitor-eval-ready-v1`  \n**审计签署：** 2026-08-20  \n\n面向统一 RAG 系统比较、人工 QA 审核与可复现实验的论文式 dataset report。",
        },
        {
            "id": "abstract",
            "type": "markdown",
            "sourceId": "final_manifest",
            "body": "## 摘要\n\nMOI RAG Bench v0.1 Text-Only Ready-for-Eval 是一个从四个本地已跑通 benchmark 中按固定配额筛选并统一化的 RAG 评测集。最终包包含 **500 份 Markdown 文档、1,000 条 QA 与 1,000 条 Gold**。为建立严格的纯文本条件，构建流程排除 39 条存在视觉语义缺口的 QA，并以 39 条经过答案承载文本校验的候选进行等额替换；来源数据集和领域配额保持不变。数据包冻结图像模型为 `NOT_APPLICABLE`，所有摄入路径只指向 `documents/<doc_id>.md`。",
        },
        {"id": "scale_metrics", "type": "metric-strip", "cardIds": ["scale_card"]},
        {"id": "audit_metrics", "type": "metric-strip", "cardIds": ["audit_card"]},
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "mllm_audit",
            "body": "## 技术总结\n\n全量语义审计覆盖 1,000 条 QA：其中 891 条与多模态来源类型无关，109 条保留 `multimodal-t` / `multimodal-f` 来源标签但均判定为 `TEXT_PROJECTION_SAFE`；显式媒体输入、`MLLM_REQUIRED`、`TEXT_PROJECTION_UNSAFE` 与 `REVIEW_REQUIRED` 均为 0。这里的核心区分是：**来源 question type 不是运行时模态要求**。",
        },
        {
            "id": "key_findings",
            "type": "markdown",
            "sourceId": "primary_metric_mapping",
            "body": "## 关键发现\n\n- 任务不被压缩成一个 QA 准确率：主维度覆盖问答正确性、多跳推理、检索、结构化抽取、文本投影推理、拒答、约束、冲突和完整性。\n- 最大的两个任务切片是问答正确性（300）与多跳推理（221）；检索切片为 150。\n- 拒答切片为 91：77 条显式不可回答，另有 14 条 `info_not_found` 语义拒答。\n- 建议按主维度与来源分别 macro，完整公布有效分母，不建议只发布一个混合总分。",
        },
        {"id": "source_chart", "type": "chart", "chartId": "source_qa_chart", "layout": "full"},
        {"id": "metric_chart", "type": "chart", "chartId": "metric_distribution_chart", "layout": "full"},
        {
            "id": "scope_definitions",
            "type": "markdown",
            "sourceId": "final_manifest",
            "body": "## 1. 范围、术语与评测条件\n\n**文档粒度**是一个 source document；MMDocIR 的页面记录被重组为完整文档。**评测 scope** 是 `global`，即系统一次摄入全部 500 文档，再处理 1,000 个查询。**Gold** 包含 reference answer、gold 文档列表与可选 evidence。**Text-only** 表示系统不得读取原始 PDF 页面图像或调用 MLLM；它不意味着来源数据中的 table、chart、figure 等谱系标签会被删除。",
        },
        {
            "id": "design_goals",
            "type": "markdown",
            "sourceId": "selection_manifest",
            "body": "## 2. 设计目标与提出的创新点\n\n1. **跨 benchmark 的唯一文档组合。** 以标准化文档 ID、标题和源文件名进行去重；选择阶段阻断 229 个 DocBench 名称候选，并从 MMDocIR 移除 727 个重叠问题。\n2. **固定配额而非便利抽样。** 四个来源在文档、QA、类型和领域上预先设定配额，避免单一来源支配整个测试。\n3. **source / ready / provenance 三层契约。** 原始资产与评测 Markdown 物理分离，`provenance/documents.jsonl` 是唯一桥接层。\n4. **语义级纯文本派生。** 不按 `multimodal-*` 标签粗暴删除，而是先移除无答案承载文本的视觉缺口，再审计保留题是否能由序列化文本独立回答。\n5. **QA 级任务感知计分。** 每题拥有一个主评估维度，可人工覆写并导出 JSON；这使拒答、多跳、结构化抽取等能力不会被单一平均分淹没。\n6. **可审计发布链。** 逐条保留排除、替换和语义审计记录，并冻结输出哈希与签署文件。\n\n这些是本数据集发布的方法学贡献；若要声称相对于全部公开 benchmark 的“首创”，仍需单独完成系统性的相关工作比较。",
        },
        {
            "id": "construction_method",
            "type": "markdown",
            "sourceId": "selection_manifest",
            "body": "## 3. 数据集构建方法\n\n候选池来自本地已有 ready package 和历史成功运行：DocBench 1,102 条候选、EnterpriseRAG-Bench 500 条、MMDocIR 931 条、MultiHop-RAG 2,556 条，共 5,089 条候选 QA。构建器先按稳定种子 `moi-rag-bench-v0.1:20260816` 选择 QA，再收齐其所需文档，并强制 500 文档 / 1,000 QA 配额、ID 唯一、Question–Gold 对齐、Gold 引用可解析和文档内容非空。历史结果只用于筛选可运行数据包，不被拼接成新的跨协议分数。",
        },
        {
            "id": "source_composition_heading",
            "type": "markdown",
            "body": "## 4. 数据集构成",
        },
        {"id": "source_table", "type": "table", "tableId": "source_composition_table", "layout": "full"},
        {
            "id": "corpus_summary",
            "type": "markdown",
            "sourceId": "final_corpus",
            "body": "### 4.1 文档语料\n\n500 份 Markdown 共约 **44.92 MiB / 46,878,793 个 Unicode 字符**。DocBench 长文档占主要体积，而 EnterpriseRAG-Bench 与 MultiHop-RAG 提供更短的企业记录和证据链文本。文档长度明显长尾，因此系统比较应同时报告切分策略、chunk 大小、重叠、Embedding 模型和索引参数。",
        },
        {"id": "corpus_table", "type": "table", "tableId": "corpus_profile_table", "layout": "full"},
        {
            "id": "question_types_heading",
            "type": "markdown",
            "sourceId": "final_questions",
            "body": "### 4.2 问题类型\n\n最终集保留 21 个来源 question type。数量最大的类型依次是 `text-only`（180）、`retrieval`（150）、`meta-data`（113）、`multimodal-t`（89）、`comparison_query`（84）和 `inference_query`（80）。这些标签用于保持来源协议谱系；跨来源比较应优先使用下一节定义的统一主评估维度。",
        },
        {"id": "question_types_table_block", "type": "table", "tableId": "question_types_table", "layout": "full"},
        {
            "id": "domains_heading",
            "type": "markdown",
            "sourceId": "final_questions",
            "body": "### 4.3 领域与 evidence class\n\n选择元数据包含 31 个领域标签与 8 个 evidence class。Enterprise 标签反映 Confluence、GitHub、Gmail、Google Drive、HubSpot、Jira、Linear、Slack 等来源组合；MMDocIR 覆盖学术论文、财报、新闻、指南、研究报告等文档类别。`table`、`chart`、`figure`、`layout` 与 `multimodal` 是来源筛选标签，不自动构成图像输入要求。",
        },
        {"id": "domains_table", "type": "table", "tableId": "domain_distribution_table", "layout": "full"},
        {"id": "evidence_classes_table_block", "type": "table", "tableId": "evidence_classes_table", "layout": "full"},
        {
            "id": "metrics_heading",
            "type": "markdown",
            "body": "## 5. 统一主评估维度",
        },
        {"id": "metric_table", "type": "table", "tableId": "metric_distribution_table", "layout": "full"},
        {
            "id": "metric_mapping_note",
            "type": "markdown",
            "sourceId": "primary_metric_mapping",
            "body": "主维度映射是本报告提出的**透明派生标注**：MMDocIR → 检索；DocBench 按不可回答、元数据、文本投影和常规文本问答拆分；MultiHop 按 null query 与推理拆分；Enterprise 按 info-not-found、冲突、约束、完整性与常规问答拆分。它不替换来源标签，也不妨碍人工标注。评测网页中的导入/导出 JSON 应被视为最终人工覆写层。",
        },
        {"id": "gold_metrics", "type": "metric-strip", "cardIds": ["gold_card"]},
        {
            "id": "gold_heading",
            "type": "markdown",
            "sourceId": "final_gold",
            "body": "## 6. Gold、Evidence 与适用分母\n\n所有 1,000 条记录都有非空 reference answer；954 条提供至少一个 gold 文档，803 条提供至少一个 evidence item，238 条需要两个及以上 gold 文档。46 条无 gold 文档的题目来自 29 条 MultiHop `null_query`、14 条 Enterprise `info_not_found` 与 3 条 Enterprise `high_level`。因此，文档检索、证据检索和答案评分必须使用不同分母。",
        },
        {"id": "gold_table", "type": "table", "tableId": "gold_profile_table", "layout": "full"},
        {"id": "length_table", "type": "table", "tableId": "length_profile_table", "layout": "full"},
        {
            "id": "separation_heading",
            "type": "markdown",
            "sourceId": "raw_manifest",
            "body": "## 7. 原始源文件与 Ready-for-Eval 的分离\n\n原始层包含 180 份 PDF、270 份源 Markdown，以及代表 50 个 MMDocIR 文档的 2,207 张页面图像，估算体积 769.0 MiB。最终 eval 包只含 500 份 Markdown 和 JSON/JSONL 元数据；原始 PDF/JPG 不在任何摄入路径中。`source_files/` 不保存生成的评测文本，`ready_for_eval/` 不保存原始资产，两者只通过 provenance 记录关联。",
        },
        {"id": "source_assets_table", "type": "table", "tableId": "source_asset_modes_table", "layout": "full"},
        {
            "id": "text_only_derivation",
            "type": "markdown",
            "sourceId": "final_manifest",
            "body": "## 8. Text-only 派生与 MLLM 审核\n\n父集中的 39 条视觉语义缺口被排除，其中 DocBench 18 条、MMDocIR 21 条；随后从未使用候选中加入同来源、同领域约束下的 39 条文本安全 QA。所有替换均验证 reference answer 可由链接 Markdown 中的答案承载文本支持。来源 QA 配额与 31 个领域标签的分布完全不变；只有四个 question type 的数量发生变化。另有 1 条空 reference answer 通过冻结 Gold evidence 进行确定性修复。",
        },
        {"id": "derivation_delta", "type": "table", "tableId": "derivation_delta_table", "layout": "full"},
        {"id": "replacement_transitions", "type": "table", "tableId": "replacement_transitions_table", "layout": "full"},
        {
            "id": "mllm_audit_method",
            "type": "markdown",
            "sourceId": "mllm_audit",
            "body": "### 8.1 语义审计方法\n\n审计器对每题检查显式 media 字段，读取链接 Markdown，并对来源 `multimodal-*` 题验证序列化表格、答案数值、可推导操作数和答案文本支持。109 条保留题全部为文本投影安全：89 条 `multimodal-t`、20 条 `multimodal-f`；其中 23 个 ID 使用显式人工决策，其余 86 条由确定性文本支持规则通过。审计置信度为 94 条高、15 条中，unsafe/review 为 0。签署文件冻结 manifest、audit rows 与 summary 的 SHA-256。",
        },
        {"id": "audit_detail", "type": "table", "tableId": "audit_detail_table", "layout": "full"},
        {
            "id": "quality_heading",
            "type": "markdown",
            "body": "## 9. 质量控制与可复现性",
        },
        {"id": "audit_checks", "type": "table", "tableId": "audit_checks_table", "layout": "full"},
        {
            "id": "preflight_note",
            "type": "markdown",
            "sourceId": "preflight",
            "body": "离线 preflight 已确认 package schema、文件引用和 global scope 可被统一 runner 接受，且 `ready=true`。因为该次运行使用 `--dry-run`、没有网络请求且服务探针为 `SKIPPED`，它证明的是**数据包就绪**，不是 FastGPT 或其他平台服务已在线。",
        },
        {
            "id": "evaluation_protocol",
            "type": "markdown",
            "body": "## 10. 建议评测协议\n\n1. 冻结一个系统版本、Embedding 模型、LLM、切分参数、top-k、提示词和随机种子。\n2. 仅摄入 `documents/<doc_id>.md`，一次建立包含 500 文档的 global 索引。禁止摄入 questions、gold、排除清单或原始图片。\n3. 逐题记录原始 query、检索排序及分数、最终答案、引用、状态、时延、token/cost 和错误。\n4. 先计算检索层，再计算答案层；不可回答题使用统一弃权规范。\n5. 对开放答案使用冻结且盲化的 Judge 协议，同时保存判分理由；短答案优先使用确定性指标。\n6. 按来源、question type、主评估维度与 answerability 分层报告，并公布每个指标的适用分母。\n7. 对逐题指标使用固定 seed 的 bootstrap（建议 10,000 次）报告 95% 置信区间；多次随机运行另报均值与方差。",
        },
        {"id": "scoring_protocol", "type": "table", "tableId": "scoring_protocol_table", "layout": "full"},
        {
            "id": "metric_formulas",
            "type": "markdown",
            "body": "### 10.1 核心指标定义\n\n- `Hit@k_i = 1`，当 top-k 检索结果与 gold 文档集合有交集，否则为 0。\n- `Recall@k_i = |Retrieved@k ∩ Gold| / |Gold|`；只对 Gold 非空题取 macro。\n- `Complete@k_i = 1`，当全部 gold 文档/证据均出现在 top-k。\n- `MRR_i = 1 / rank(first relevant)`；未命中为 0。\n- `Abstention Precision = 正确拒答 / 所有拒答`；`Abstention Recall = 正确拒答 / 应拒答题`。\n- 开放答案同时评估 correctness、completeness 与 faithfulness，避免“措辞相似但无证据”的答案获得高分。\n\n若发布综合分，建议先在九个主维度内做 macro，再对维度做等权平均；必须同时发布九个分项，不能让 300 条常规问答通过样本量压倒只有 1–9 条的稀有能力。",
        },
        {
            "id": "usage_heading",
            "type": "markdown",
            "body": "## 11. 使用指南",
        },
        {"id": "file_contract", "type": "table", "tableId": "file_contract_table", "layout": "full"},
        {
            "id": "usage_python",
            "type": "markdown",
            "body": "### 11.1 最小 Python 读取\n\n```python\nimport json\nfrom pathlib import Path\n\nroot = Path(\"datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval\")\n\ndef read_jsonl(name):\n    with (root / name).open(encoding=\"utf-8\") as f:\n        return [json.loads(line) for line in f if line.strip()]\n\nmanifest = json.loads((root / \"manifest.json\").read_text(encoding=\"utf-8\"))\ncorpus = read_jsonl(\"corpus.jsonl\")\nquestions = read_jsonl(\"questions.jsonl\")\ngold_by_id = {row[\"question_id\"]: row for row in read_jsonl(\"gold.jsonl\")}\n\n# 只把这些 Markdown 交给被测系统。\ningest_paths = [root / row[\"text_path\"] for row in corpus]\n\nfor q in questions:\n    system_answer = rag.query(q[\"question\"])  # 不要把 q[\"gold\"] 或 gold 注入 prompt\n    gold = gold_by_id[q[\"question_id\"]]\n    save_result(q[\"question_id\"], system_answer, gold)\n```",
        },
        {
            "id": "usage_commands",
            "type": "markdown",
            "sourceId": "final_manifest",
            "body": "### 11.2 重建与离线检查\n\n```bash\npython3 scripts/tools/build_moi_text_only_condition_v0_1.py \\\n  --output datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval \\\n  --check-only\n\npython3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight \\\n  --system fastgpt_local \\\n  --package datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval \\\n  --output-root runs/readiness/text-only-ready-preflight \\\n  --run-id 20260820-text-only-ready \\\n  --dry-run\n```\n\n`--check-only` 不改写数据包；preflight 的 `--dry-run` 不发网络请求。正式运行时应为每个平台创建独立资源并保存完整 run ledger。",
        },
        {
            "id": "result_schema",
            "type": "markdown",
            "body": "### 11.3 建议结果记录\n\n每条结果至少保存：`question_id`、`run_id`、`system_id`、`retrieved_doc_ids`（有序）、`retrieval_scores`、`answer`、`citations`、`abstained`、`latency_ms`、`status`、`error`、模型与索引配置指纹。评分产物再追加 metric、judge 版本、分数、理由与有效分母。这样可以重算指标而无需重新调用系统。",
        },
        {
            "id": "limitations",
            "type": "markdown",
            "sourceId": "selection_manifest",
            "body": "## 12. 限制、风险与不确定性\n\n- 这是四个来源的统一筛选切片，不是对原 benchmark 全量分布的无偏估计；Enterprise 使用本地 adapted 500-question slice。\n- 来源协议、答案形态和 domain/evidence taxonomy 不完全同构，不能把来源历史分数直接横向拼接。\n- 纯文本条件测量序列化 Markdown 上的能力，不代表原始 PDF/JPG 的解析或视觉理解能力。\n- 109 条来源 `multimodal-*` 虽通过语义审计，仍应在首轮实跑后抽样复核模型是否利用了正确文本证据。\n- 结构化抽取、冲突、约束和完整性中的稀有类型样本较少，置信区间会较宽。\n- 当前报告未发现统一 license manifest；再分发原始资产或公开数据集前，必须逐一核对四个来源的许可证、使用条款与企业内容边界。\n- 数据主要来自既有本地包与历史运行可用性，可能继承源 benchmark 的语言、领域和标注偏差。",
        },
        {
            "id": "responsible_use",
            "type": "markdown",
            "body": "## 13. 负责任使用与泄漏控制\n\n评测系统只能看到 Markdown 文档和问题文本。`reference_answer`、`gold_doc_ids`、`gold_evidence`、`source_question_id`、替换审计与人工指标标注都必须留在评分侧。公开排行榜应披露模型是否见过源 benchmark、是否使用缓存/联网搜索、是否复用已配置知识库、是否发生人工后编辑，以及失败题如何进入分母。企业来源内容在共享前需要单独做权限、隐私与许可证审查。",
        },
        {
            "id": "citation",
            "type": "markdown",
            "body": "## 14. 引用与版本标识\n\n建议在实验中记录：\n\n```text\nDataset: MOI RAG Bench v0.1 Text-Only Ready-for-Eval\nDataset ID: moi-rag-bench-v0.1-text-only-no-mllm\nRevision: derived-local-v0.1\nProtocol tag: MOI_RAG_BENCH_V0_1_TEXT_ONLY_NO_MLLM\nPackage schema: competitor-eval-ready-v1\nCondition: text-only-no-mllm\nAudit signoff date: 2026-08-20\n```\n\n如形成正式论文，应再补齐作者、机构、发布 URL、许可证与 BibTeX；不要用本地路径充当公开持久标识符。",
        },
        {
            "id": "next_steps",
            "type": "markdown",
            "body": "## 15. 推荐下一步\n\n1. 用评测网页完成 1,000 条主维度人工复核，冻结 `metric-annotations-v1.json`。\n2. 在至少两个平台上完成相同配置的 1,000-QA dry-run → ingest → retrieval → QA 全链路。\n3. 为 803 条 evidence-applicable QA 冻结证据匹配器与 locator 规范。\n4. 为开放答案冻结 Judge 模型、prompt、温度、重试和盲化策略。\n5. 发布首个 baseline 时同步给出逐题结果、配置指纹、失败清单和 bootstrap 置信区间。\n6. 在 v0.2 增加稀有能力样本，并把来源许可证与数据声明纳入 manifest。",
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": "## 16. 后续研究问题\n\n- 文档长度、chunk 策略与长文档检索误差之间是什么关系？\n- 纯文本投影题与原生 text-only 题是否呈现系统性难度差异？\n- 检索召回提升能在多大程度上传导为答案 correctness 与 completeness？\n- 拒答阈值在 91 条语义拒答题与 909 条可回答/非主拒答题之间如何校准？\n- 不同来源的评分协议如何归一化，同时避免小样本能力被掩盖？",
        },
    ]

    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "A paper-style technical report for the frozen MOI RAG Bench v0.1 pure-text evaluation package.",
            "generatedAt": generated_at,
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": source_manifest_entries(),
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": profiles,
            "accessIssues": [],
        },
        "sources": top_level_sources(generated_at),
        "package_info": {
            "originUrl": "artifact://moi-rag-bench-v0.1-text-only-dataset-report",
            "controls": {
                "edit": False,
                "refresh": False,
                "persistence": False,
                "copyAsImage": False,
            },
        },
    }


def build_chart_map() -> dict[str, Any]:
    return {
        "schema": "moi-rag-bench-report-chart-map-v1",
        "charts": [
            {
                "chart_id": "source_qa_chart",
                "question": "四个来源分别贡献多少 QA，配额是否均衡且互补？",
                "metric": "questions",
                "dimensions": ["source"],
                "filters": [],
                "source_id": "final_manifest",
                "takeaway": "DocBench 450、MultiHop-RAG 250，EnterpriseRAG-Bench 与 MMDocIR 各 150。",
            },
            {
                "chart_id": "metric_distribution_chart",
                "question": "1,000 条 QA 的主评估能力如何分布？",
                "metric": "count",
                "dimensions": ["primary_metric"],
                "filters": [],
                "source_id": "primary_metric_mapping",
                "takeaway": "问答正确性 300、多跳 221、检索 150；另外六项覆盖结构化、文本投影、拒答和稀有企业能力。",
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--chart-map", type=Path, default=DEFAULT_CHART_MAP)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    manifest = read_json(PACKAGE / "manifest.json")
    raw_manifest = read_json(RAW_MANIFEST)
    selection_manifest = read_json(SELECTION_MANIFEST)
    corpus = read_jsonl(PACKAGE / "corpus.jsonl")
    questions = read_jsonl(PACKAGE / "questions.jsonl")
    gold = read_jsonl(PACKAGE / "gold.jsonl")
    exclusions = read_jsonl(PACKAGE / "visual-exclusions.jsonl")
    replacements = read_jsonl(PACKAGE / "replacement-audit.jsonl")
    audit_summary = read_json(AUDIT_SUMMARY)
    audit_rows = read_jsonl(AUDIT_ROWS)
    audit_signoff = read_json(AUDIT_SIGNOFF)
    preflight = read_json(PREFLIGHT)

    validation = validate_inputs(
        manifest,
        corpus,
        questions,
        gold,
        exclusions,
        replacements,
        audit_summary,
        audit_signoff,
        preflight,
    )
    profiles = build_profiles(
        manifest,
        raw_manifest,
        questions,
        gold,
        corpus,
        replacements,
        audit_summary,
        audit_rows,
        validation,
    )

    metric_total = sum(row["count"] for row in profiles["metric_distribution"])
    type_total = sum(row["count"] for row in profiles["question_types"])
    domain_total = sum(row["questions"] for row in profiles["domain_distribution"])
    if (metric_total, type_total, domain_total) != (1000, 1000, 1000):
        raise ValueError("Report summary tables do not reconcile to 1,000 QA")
    if len(profiles["question_types"]) != 21 or len(profiles["domain_distribution"]) != 31:
        raise ValueError("Question-type or domain taxonomy drifted")

    artifact = build_artifact(profiles, audit_signoff)
    if artifact["manifest"]["title"] not in artifact["manifest"]["blocks"][0]["body"]:
        raise ValueError("Title block and manifest title do not match")

    result = {
        "status": "OK",
        "documents": len(corpus),
        "questions": len(questions),
        "gold": len(gold),
        "question_types": len(profiles["question_types"]),
        "primary_metrics": len(profiles["metric_distribution"]),
        "domains": len(profiles["domain_distribution"]),
        **validation,
    }
    if args.check_only:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    output = args.output.resolve()
    chart_map = args.chart_map.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    chart_map.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    chart_map.write_text(
        json.dumps(build_chart_map(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result["output"] = str(output.relative_to(ROOT))
    result["chart_map"] = str(chart_map.relative_to(ROOT))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
