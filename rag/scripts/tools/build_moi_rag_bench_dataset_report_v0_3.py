#!/usr/bin/env python3
"""Adapt the v0.1 paper-style report to MOI RAG Bench v0.3 Final."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval"
BENCHMARK = ROOT / "datasets/moi-rag-bench-v0.3-final"
RAW_ROOT = ROOT / "datasets/moi-rag-bench-v0.3-final-raw-corpus"
QUALITY_ROOT = ROOT / "runs/readiness/qa-quality-audit-20260825-v0.3-final-balanced"
MLLM_ROOT = ROOT / "runs/readiness/mllm-semantic-audit-20260825-v0.3-final-balanced"
PREFLIGHT = ROOT / "runs/bench-v0.3-final/20260825-dify-v03-final-uuid-fix/preflight.json"
BASE_ARTIFACT = ROOT / "results/reports/moi-rag-bench-v0.1-text-only-dataset-report.artifact.json"
OUTPUT_STEM = "moi-rag-bench-v0.3-final-dataset-report"
DEFAULT_OUTPUT = ROOT / f"results/reports/{OUTPUT_STEM}.artifact.json"
DEFAULT_CHART_MAP = ROOT / f"results/reports/{OUTPUT_STEM}.chart-map.json"
QUERY_PATH = f"results/reports/{OUTPUT_STEM}.queries.sql"

EXPECTED = {"documents": 297, "questions": 275, "gold": 275}
SOURCES = ("docbench", "enterprise", "multihop")
SOURCE_LABELS = {
    "docbench": "DocBench",
    "enterprise": "EnterpriseRAG-Bench",
    "multihop": "MultiHop-RAG",
}
SOURCE_COVERAGE = {
    "docbench": "长文档文本问答、少量元数据与不可回答",
    "enterprise": "企业多源、语义、冲突、约束与完整性",
    "multihop": "比较、推断、时序与 null query",
}
METRIC_SPECS = {
    "qa_performance": (
        "问答正确性",
        "生成与 reference answer 语义一致且受语料支持的答案。",
        "Normalized EM / token F1；开放答案语义正确性与 faithfulness",
    ),
    "multi_hop_reasoning": (
        "多跳推理",
        "跨多个 gold 文档或证据完成比较、推断和时序组合。",
        "答案正确性 + Complete@k / All-evidence recall",
    ),
    "refusal_performance": (
        "拒答/弃权",
        "识别 unanswerable 或 null_query，并避免无依据作答。",
        "Abstention Precision / Recall / F1；false-refusal rate",
    ),
    "structured_extraction": (
        "结构化/元数据抽取",
        "从标题、章节、字段或结构化内容中抽取目标值。",
        "规范化 Exact Match、数值容差准确率、字段级 F1",
    ),
    "constraint_following": (
        "约束遵循",
        "满足问题规定的格式、范围或条件。",
        "约束逐项通过率 + 答案正确性",
    ),
    "conflict_resolution": (
        "冲突信息处理",
        "识别多源不一致并给出有证据的结论。",
        "结论正确性 + 冲突识别率 + 引用正确率",
    ),
    "completeness_coverage": (
        "完整性覆盖",
        "覆盖问题要求的全部实体、事实或子任务。",
        "Reference-claim recall / completeness + 无效额外事实率",
    ),
}
EXCLUSION_LABELS = {
    "weak_question_evidence_alignment": "问题与证据对齐弱",
    "arbitrary_null_answer_format": "任意化 null 答案格式",
    "near_duplicate_or_counterfactual_variant": "近重复或反事实变体",
    "maas_embedding_403": "MAAS Embedding 403",
    "contrived_counterfactual_premise": "人为反事实前提",
    "fulltext_timeout": "全文检索超时",
    "missing_judge_overall": "缺失 Judge overall",
    "multiple_questions_in_one_row": "单行包含多个问题",
    "unanswerable_label_contradicted_by_runtime_context": "不可回答标签与运行上下文冲突",
    "embedding_tls_timeout": "Embedding TLS 超时",
    "external_web_question": "外部 Web 问题",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Expected object: {path}:{number}")
        rows.append(row)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)] if ordered else 0


def metric_for(question: Mapping[str, Any]) -> str:
    dataset = str(question.get("source_dataset") or "").casefold()
    question_type = str(question.get("question_type") or "").casefold()
    metadata = question.get("metadata") if isinstance(question.get("metadata"), Mapping) else {}
    original_type = str(metadata.get("original_type") or question_type).casefold()
    if dataset == "docbench":
        if original_type in {"unanswerable", "una-web"} or question.get("answerable") is False:
            return "refusal_performance"
        return "structured_extraction" if original_type == "meta-data" else "qa_performance"
    if dataset == "multihop":
        return "refusal_performance" if question_type == "null_query" or question.get("answerable") is False else "multi_hop_reasoning"
    if dataset == "enterprise":
        return {
            "info_not_found": "refusal_performance",
            "conflicting_info": "conflict_resolution",
            "constrained": "constraint_following",
            "completeness": "completeness_coverage",
        }.get(question_type, "qa_performance")
    raise ValueError(f"Unknown source_dataset: {dataset}")


def unique_ids(rows: list[dict[str, Any]], key: str, label: str) -> list[str]:
    values = [str(row.get(key) or "") for row in rows]
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError(f"Invalid or duplicate {key} in {label}")
    return values


def validate(
    package_manifest: dict[str, Any],
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
    selection_rows: list[dict[str, Any]],
    selection_summary: dict[str, Any],
    package_validation: dict[str, Any],
    quality_rows: list[dict[str, Any]],
    quality_summary: dict[str, Any],
    mllm_summary: dict[str, Any],
    mllm_signoff: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, int]:
    observed = {"documents": len(corpus), "questions": len(questions), "gold": len(gold)}
    if observed != EXPECTED:
        raise ValueError(f"Count mismatch: {observed}")
    if package_manifest.get("dataset_id") != "moi-rag-bench-v0.3-final":
        raise ValueError("Wrong dataset_id")
    if package_manifest.get("mllm_required") is not False or package_manifest.get("image_llm") != "NOT_APPLICABLE":
        raise ValueError("Pure-text contract is not frozen")

    document_ids = unique_ids(corpus, "doc_id", "corpus")
    question_ids = unique_ids(questions, "question_id", "questions")
    gold_ids = unique_ids(gold, "question_id", "gold")
    if question_ids != gold_ids:
        raise ValueError("Question/Gold order or IDs differ")
    document_set = set(document_ids)
    refs = [str(doc_id) for row in gold for doc_id in row.get("gold_doc_ids") or []]
    if not set(refs) <= document_set:
        raise ValueError("Gold closure failed")

    ready_hashes = 0
    for row in corpus:
        relative = Path(str(row.get("text_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe ready path: {relative}")
        path = PACKAGE / relative
        if not path.is_file() or path.suffix.casefold() != ".md" or sha256_file(path) != row.get("sha256"):
            raise ValueError(f"Ready document failed integrity: {relative}")
        ready_hashes += 1

    provenance_ids = unique_ids(provenance, "doc_id", "provenance")
    if set(provenance_ids) != document_set:
        raise ValueError("Provenance/corpus document sets differ")
    source_hashes = 0
    for row in provenance:
        if row.get("missing_sources") or not row.get("source_asset_available"):
            raise ValueError(f"Missing source asset: {row['doc_id']}")
        for asset in row.get("copied_assets") or []:
            relative = Path(str(asset.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe source path: {relative}")
            path = RAW_ROOT / relative
            if not path.is_file() or sha256_file(path) != asset.get("sha256"):
                raise ValueError(f"Source asset failed integrity: {relative}")
            source_hashes += 1
    if source_hashes != 297:
        raise ValueError(f"Expected 297 source assets, found {source_hashes}")

    selected = [row for row in selection_rows if row.get("selected") is True]
    if len(selection_rows) != 1000 or len(selected) != 275:
        raise ValueError("Selection audit does not reconcile 1,000 -> 275")
    if {str(row["question_id"]) for row in selected} != set(question_ids):
        raise ValueError("Selected question IDs differ from package")
    if selection_summary.get("counts", {}).get("excluded_questions") != 725:
        raise ValueError("Selection summary exclusion count drifted")
    if package_validation.get("status") != "PASS" or package_validation.get("judge_score_mean") != 0.609927:
        raise ValueError("Package validation did not pass")

    quality_ids = unique_ids(quality_rows, "question_id", "quality audit")
    if set(quality_ids) != set(question_ids):
        raise ValueError("Quality audit IDs differ from package")
    if quality_summary.get("high_risk_question_ids") or quality_summary.get("review_question_ids"):
        raise ValueError("Quality audit contains high-risk/review IDs")
    if quality_summary.get("risk_level_counts") != {"LOW": 161, "PASS": 114}:
        raise ValueError("Quality audit summary drifted")

    audit_counts = mllm_summary.get("counts") or {}
    if (
        mllm_summary.get("result") != "PASS"
        or audit_counts.get("questions") != 275
        or audit_counts.get("explicit_media_input_rows") != 0
        or audit_counts.get("mllm_required_rows") != 0
        or audit_counts.get("multimodal_rows") != 0
        or mllm_summary.get("unsafe_question_ids")
    ):
        raise ValueError("MLLM audit failed")
    if (
        mllm_signoff.get("status") != "PASS"
        or mllm_signoff.get("decision") != "pure_text_llm_only"
        or mllm_signoff.get("manifest_sha256") != sha256_file(PACKAGE / "manifest.json")
        or mllm_signoff.get("audit_summary_sha256") != sha256_file(MLLM_ROOT / "summary.json")
        or mllm_signoff.get("audit_rows_sha256") != sha256_file(MLLM_ROOT / "audit.jsonl")
    ):
        raise ValueError("MLLM signoff hashes differ")
    if not (
        preflight.get("package_valid") is True
        and preflight.get("ready") is True
        and preflight.get("scope_verified") is True
        and preflight.get("scope") == "global"
    ):
        raise ValueError("Live package preflight failed")

    return {
        "ready_hashes": ready_hashes,
        "source_hashes": source_hashes,
        "gold_references": len(refs),
        "selected": len(selected),
    }


def build_profiles(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
    selection_rows: list[dict[str, Any]],
    selection_summary: dict[str, Any],
    quality_summary: dict[str, Any],
    validation: dict[str, int],
) -> dict[str, list[dict[str, Any]]]:
    gold_by_id = {row["question_id"]: row for row in gold}
    source_q = Counter(row["source_dataset"] for row in questions)
    source_d = Counter(row["metadata"]["source_dataset"] for row in corpus)
    answerability = Counter((row["source_dataset"], bool(row.get("answerable"))) for row in questions)
    selected_rows = [row for row in selection_rows if row.get("selected") is True]
    difficulty = Counter(
        (row["source_dataset"], str((row.get("moi_baseline") or {}).get("difficulty_bucket") or "unknown"))
        for row in selected_rows
    )
    source_composition = [
        {
            "source": SOURCE_LABELS[source],
            "source_key": source,
            "documents": source_d[source],
            "questions": source_q[source],
            "doc_share": source_d[source] / len(corpus),
            "qa_share": source_q[source] / len(questions),
            "answerable": answerability[(source, True)],
            "unanswerable": answerability[(source, False)],
            "easy": difficulty[(source, "easy")],
            "hard": difficulty[(source, "hard")],
            "coverage": SOURCE_COVERAGE[source],
        }
        for source in SOURCES
    ]

    corpus_chars: dict[str, list[int]] = defaultdict(list)
    corpus_bytes: dict[str, list[int]] = defaultdict(list)
    for row in corpus:
        source = row["metadata"]["source_dataset"]
        path = PACKAGE / row["text_path"]
        corpus_chars[source].append(len(path.read_text(encoding="utf-8")))
        corpus_bytes[source].append(path.stat().st_size)
    corpus_profile = [
        {
            "source": SOURCE_LABELS[source],
            "documents": len(corpus_chars[source]),
            "size_mib": round(sum(corpus_bytes[source]) / 1024 / 1024, 2),
            "characters": sum(corpus_chars[source]),
            "mean_chars": round(statistics.mean(corpus_chars[source])),
            "median_chars": round(statistics.median(corpus_chars[source])),
            "p95_chars": percentile(corpus_chars[source], 0.95),
            "max_chars": max(corpus_chars[source]),
        }
        for source in SOURCES
    ]

    type_counts = Counter(row["question_type"] for row in questions)
    type_source = Counter((row["question_type"], row["source_dataset"]) for row in questions)
    question_types = [
        {
            "question_type": question_type,
            "count": count,
            "share": count / len(questions),
            "docbench": type_source[(question_type, "docbench")],
            "enterprise": type_source[(question_type, "enterprise")],
            "multihop": type_source[(question_type, "multihop")],
        }
        for question_type, count in type_counts.most_common()
    ]

    metric_counts = Counter(metric_for(row) for row in questions)
    metric_source = Counter((metric_for(row), row["source_dataset"]) for row in questions)
    metric_distribution = []
    for metric_key, count in metric_counts.most_common():
        label, definition, recommended = METRIC_SPECS[metric_key]
        metric_distribution.append(
            {
                "metric_key": metric_key,
                "metric": label,
                "count": count,
                "share": count / len(questions),
                "source_breakdown": "；".join(
                    f"{SOURCE_LABELS[source]} {metric_source[(metric_key, source)]}"
                    for source in SOURCES
                    if metric_source[(metric_key, source)]
                ),
                "definition": definition,
                "recommended": recommended,
            }
        )

    domain_counts = Counter(str(row["metadata"].get("selection_domain") or "(missing)") for row in questions)
    domain_distribution = [
        {
            "domain": domain,
            "source": "DocBench" if domain == "docbench" else "MultiHop-RAG" if domain == "multihop" else "EnterpriseRAG-Bench",
            "questions": count,
            "share": count / len(questions),
        }
        for domain, count in domain_counts.most_common()
    ]
    evidence_counts = Counter(str(row["metadata"].get("selection_evidence_class") or "(missing)") for row in questions)
    evidence_classes = [
        {"evidence_class": key, "count": count, "share": count / len(questions), "interpretation": "来源筛选标签；最终运行均为纯文本"}
        for key, count in evidence_counts.most_common()
    ]

    gold_stats: dict[str, Counter[str]] = defaultdict(Counter)
    question_lengths: list[int] = []
    answer_lengths: list[int] = []
    for question in questions:
        source = question["source_dataset"]
        gold_row = gold_by_id[question["question_id"]]
        docs = gold_row.get("gold_doc_ids") or []
        evidence = gold_row.get("gold_evidence") or []
        stats = gold_stats[source]
        stats["questions"] += 1
        stats["with_gold_documents"] += bool(docs)
        stats["with_evidence"] += bool(evidence)
        stats["multi_document"] += len(docs) >= 2
        stats["gold_documents"] += len(docs)
        stats["evidence_items"] += len(evidence)
        question_lengths.append(len(question["question"]))
        answer_lengths.append(len(str(gold_row.get("reference_answer") or "")))
    gold_profile = [
        {
            "source": SOURCE_LABELS[source],
            "questions": stats["questions"],
            "with_gold_documents": stats["with_gold_documents"],
            "with_evidence": stats["with_evidence"],
            "multi_document": stats["multi_document"],
            "avg_gold_docs": round(stats["gold_documents"] / stats["questions"], 2),
            "avg_evidence_items": round(stats["evidence_items"] / stats["questions"], 2),
        }
        for source in SOURCES
        for stats in [gold_stats[source]]
    ]
    length_profile = [
        {
            "field": label,
            "unit": "Unicode 字符",
            "mean": round(statistics.mean(values), 1),
            "median": statistics.median(values),
            "p95": percentile(values, 0.95),
            "max": max(values),
            "empty": sum(value == 0 for value in values),
        }
        for label, values in (("Question", question_lengths), ("Reference answer", answer_lengths))
    ]

    selected_difficulty = selection_summary["selected_by_difficulty"]
    derivation_delta = [
        {
            "bucket": bucket,
            "questions": count,
            "share": count / len(questions),
            "definition": "基线 Judge < 0.9" if bucket == "hard" else "基线 Judge ≥ 0.9",
        }
        for bucket, count in (("easy", selected_difficulty["easy"]), ("hard", selected_difficulty["hard"]))
    ]
    replacement_transitions = [
        {
            "reason": EXCLUSION_LABELS.get(reason, reason),
            "reason_key": reason,
            "count": count,
            "source_share": count / 1000,
        }
        for reason, count in sorted(selection_summary["hard_exclusion_reason_counts"].items(), key=lambda item: (-item[1], item[0]))
    ]

    audit_detail = []
    for source in SOURCES:
        summary = quality_summary["by_dataset"][source]
        levels = summary["risk_level_counts"]
        risks = summary["risk_type_counts"]
        audit_detail.append(
            {
                "source": SOURCE_LABELS[source],
                "rows": summary["rows"],
                "pass": levels.get("PASS", 0),
                "low": levels.get("LOW", 0),
                "high": levels.get("HIGH", 0),
                "scope_warning": risks.get("scope_contract_warning", 0),
                "format_sensitive": risks.get("answer_format_sensitive", 0),
                "external_temporal": risks.get("external_or_temporal_scope", 0),
            }
        )

    source_mode_counts = Counter(row.get("source_mode") for row in provenance)
    source_asset_modes = [
        {
            "source_mode": "原始 PDF" if mode == "original_pdf" else "原始 Markdown",
            "documents": count,
            "raw_assets": count,
            "ready_representation": "每文档一份 Markdown",
        }
        for mode, count in (("original_pdf", source_mode_counts["original_pdf"]), ("original_md", source_mode_counts["original_md"]))
    ]

    audit_checks = [
        {"check": "文档 / Questions / Gold", "result": "PASS", "value": "297 / 275 / 275", "scope": "最终 eval 包"},
        {"check": "Question 与 Gold 顺序及 ID", "result": "PASS", "value": "275 / 275", "scope": "最终 eval 包"},
        {"check": "Gold 文档引用闭包", "result": "PASS", "value": f"{validation['gold_references']} 个引用", "scope": "最终 eval 包"},
        {"check": "Ready Markdown SHA-256", "result": "PASS", "value": f"{validation['ready_hashes']} / 297", "scope": "ready_for_eval"},
        {"check": "原始源资产 SHA-256", "result": "PASS", "value": f"{validation['source_hashes']} / 297", "scope": "source_files"},
        {"check": "技术失败进入选集", "result": "PASS", "value": "0", "scope": "selection audit"},
        {"check": "QA 高风险 / 重复组", "result": "PASS", "value": "0 / 0", "scope": "quality audit"},
        {"check": "显式媒体 / MLLM required", "result": "PASS", "value": "0 / 0", "scope": "MLLM audit"},
        {"check": "Dify live preflight", "result": "PASS", "value": "package_valid=true；ready=true", "scope": "global package"},
    ]
    file_contract = [
        {"path": "manifest.json", "rows_or_files": "1", "role": "版本、条件、路径与平台支持契约", "evaluator_use": "先读取并校验"},
        {"path": "corpus.jsonl", "rows_or_files": "297", "role": "doc_id、Markdown 路径、SHA-256 与来源元数据", "evaluator_use": "建立全局文档索引"},
        {"path": "documents/<doc_id>.md", "rows_or_files": "297", "role": "唯一允许摄入的纯文本语料", "evaluator_use": "上传、切分、Embedding"},
        {"path": "questions.jsonl", "rows_or_files": "275", "role": "问题、answerability、来源类型与 Gold 文档 ID", "evaluator_use": "仅发送 question 文本"},
        {"path": "gold.jsonl", "rows_or_files": "275", "role": "reference answer、Gold 文档与 evidence", "evaluator_use": "运行结束后离线评分"},
        {"path": "../provenance/documents.jsonl", "rows_or_files": "297", "role": "原始资产与 Ready Markdown 的唯一桥接层", "evaluator_use": "数据审计，不摄入"},
    ]
    scoring_protocol = [
        {"layer": "检索：文档级", "applicable_rows": 265, "primary_metrics": "Hit@1/5/10；Recall@1/5/10；MRR；nDCG@10", "aggregation": "Gold 文档非空；按来源和主维度 macro"},
        {"layer": "检索：证据级", "applicable_rows": 251, "primary_metrics": "Evidence Recall@k；Complete@k；quote 命中", "aggregation": "仅 Gold evidence 非空"},
        {"layer": "答案：短/结构化", "applicable_rows": "按答案形态", "primary_metrics": "Normalized EM；token F1；数值容差准确率", "aggregation": "显式冻结单位、舍入、日期和列表顺序"},
        {"layer": "答案：开放答案", "applicable_rows": "按答案形态", "primary_metrics": "correctness；completeness；faithfulness", "aggregation": "冻结并盲化 Judge；保存原始判分"},
        {"layer": "拒答/弃权", "applicable_rows": 27, "primary_metrics": "Abstention P/R/F1；false refusal；hallucination", "aggregation": "17 DocBench unanswerable + 10 null_query"},
        {"layer": "多跳", "applicable_rows": 95, "primary_metrics": "答案正确性；All-evidence recall；Complete@k", "aggregation": "要求全部必要文档覆盖"},
        {"layer": "系统", "applicable_rows": 275, "primary_metrics": "成功率；P50/P95 latency；token/cost；超时率", "aggregation": "失败计入可靠性并单独公布 Judge 分母"},
    ]

    return {
        "summary_scale": [{"questions": 275, "documents": 297, "gold": 275, "sources": 3}],
        "summary_audit": [{"selected": 275, "excluded": 725, "high_risk": 0, "mllm_required": 0}],
        "summary_gold": [{"with_gold_documents": 265, "with_evidence": 251, "reference_answers": 275, "multi_document": 100}],
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


def source_entries() -> list[dict[str, Any]]:
    entries = [
        ("final_manifest", "Final eval package manifest", "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/manifest.json"),
        ("final_questions", "Final questions", "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/questions.jsonl"),
        ("final_gold", "Final Gold labels", "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/gold.jsonl"),
        ("final_corpus", "Final corpus index and Markdown", "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/corpus.jsonl"),
        ("selection_summary", "v0.3 curation summary", "datasets/moi-rag-bench-v0.3-final/selection-summary.json"),
        ("selection_audit", "Per-question curation audit", "datasets/moi-rag-bench-v0.3-final/selection-audit.jsonl"),
        ("raw_manifest", "Separated source/ready corpus manifest", "datasets/moi-rag-bench-v0.3-final-raw-corpus/manifest.json"),
        ("provenance", "Source-to-ready provenance", "datasets/moi-rag-bench-v0.3-final-raw-corpus/provenance/documents.jsonl"),
        ("qa_quality_audit", "QA quality audit", "runs/readiness/qa-quality-audit-20260825-v0.3-final-balanced/summary.json"),
        ("mllm_audit", "Pure-text semantic audit", "runs/readiness/mllm-semantic-audit-20260825-v0.3-final-balanced/summary.json"),
        ("mllm_signoff", "Pure-text audit signoff", "runs/readiness/mllm-semantic-audit-20260825-v0.3-final-balanced/text-only-semantic-audit-signoff.json"),
        ("package_validation", "v0.3 package validation", "datasets/moi-rag-bench-v0.3-final-raw-corpus/validation.json"),
        ("preflight", "Dify live package preflight", "runs/bench-v0.3-final/20260825-dify-v03-final-uuid-fix/preflight.json"),
        ("report_profile", "Deterministic report profile", "tools/build_moi_rag_bench_dataset_report_v0_3.py"),
        ("readiness_bundle", "v0.3 curation and readiness bundle", "datasets/moi-rag-bench-v0.3-final/selection-summary.json"),
    ]
    return [{"id": source_id, "label": label, "path": path, "href": QUERY_PATH} for source_id, label, path in entries]


def root_sources(generated_at: str) -> list[dict[str, Any]]:
    return [
        {
            "id": entry["id"],
            "path": QUERY_PATH,
            "query": {
                "engine": "duckdb/local-filesystem",
                "language": "sql",
                "description": f"Profiles {entry['label']} for the v0.3 Final report.",
                "executed_at": generated_at,
                "tables_used": [entry["path"]],
            },
        }
        for entry in source_entries()
    ]


def table(table_id: str, title: str, dataset: str, source_id: str, columns: list[dict[str, Any]], subtitle: str = "", sort: tuple[str, str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"id": table_id, "title": title, "dataset": dataset, "sourceId": source_id, "columns": columns}
    if subtitle:
        value["subtitle"] = subtitle
    if sort:
        value["defaultSort"] = {"field": sort[0], "direction": sort[1]}
    return value


def build_artifact(profiles: dict[str, list[dict[str, Any]]], signoff: dict[str, Any]) -> dict[str, Any]:
    artifact = read_json(BASE_ARTIFACT)
    manifest = artifact["manifest"]
    generated_at = signoff["generated_at"]
    title = "MOI RAG Bench v0.3 Final：审核版数据集技术报告"
    manifest.update({"title": title, "description": "Paper-style technical report for the curated MOI RAG Bench v0.3 Final pure-text package.", "generatedAt": generated_at})
    artifact["snapshot"] = {"version": 1, "generatedAt": generated_at, "status": "ready", "datasets": profiles, "accessIssues": []}

    manifest["cards"] = [
        {"id": "scale_card", "description": "审核后评测集规模。", "dataset": "summary_scale", "sourceId": "final_manifest", "metrics": [
            {"label": "QA", "field": "questions", "format": "number"},
            {"label": "文档", "field": "documents", "format": "number"},
            {"label": "Gold", "field": "gold", "format": "number"},
            {"label": "来源", "field": "sources", "format": "number"},
        ]},
        {"id": "audit_card", "description": "从 1,000 QA 质量优先筛选。", "dataset": "summary_audit", "sourceId": "selection_summary", "metrics": [
            {"label": "保留 QA", "field": "selected", "format": "number"},
            {"label": "排除", "field": "excluded", "format": "number"},
            {"label": "高风险", "field": "high_risk", "format": "number"},
            {"label": "需 MLLM", "field": "mllm_required", "format": "number"},
        ]},
        {"id": "gold_card", "description": "Gold 标注及评分适用分母。", "dataset": "summary_gold", "sourceId": "final_gold", "metrics": [
            {"label": "有 Gold 文档", "field": "with_gold_documents", "format": "number"},
            {"label": "有 Evidence", "field": "with_evidence", "format": "number"},
            {"label": "Reference answer", "field": "reference_answers", "format": "number"},
            {"label": "多文档 QA", "field": "multi_document", "format": "number"},
        ]},
    ]
    manifest["charts"] = [
        {"id": "source_qa_chart", "title": "QA 数量按来源", "subtitle": "v0.3 Final；n=275；DocBench 130、MultiHop-RAG 105、EnterpriseRAG-Bench 40。", "type": "horizontalBar", "dataset": "source_composition", "sourceId": "selection_summary", "encodings": {
            "x": {"field": "source", "type": "nominal", "label": "来源"},
            "y": {"field": "questions", "type": "quantitative", "label": "QA 数"},
            "tooltip": [{"field": "documents", "type": "quantitative", "label": "文档"}, {"field": "qa_share", "type": "quantitative", "label": "QA 占比", "format": "percent"}],
        }, "xAxisTitle": "QA 数", "valueFormat": "number", "layout": "full"},
        {"id": "metric_distribution_chart", "title": "QA 数量按主评估维度", "subtitle": "v0.3 Final；n=275；每题分配一个可人工覆写的主维度。", "type": "horizontalBar", "dataset": "metric_distribution", "sourceId": "report_profile", "encodings": {
            "x": {"field": "metric", "type": "nominal", "label": "主评估维度"},
            "y": {"field": "count", "type": "quantitative", "label": "QA 数"},
            "tooltip": [{"field": "share", "type": "quantitative", "label": "占比", "format": "percent"}, {"field": "source_breakdown", "type": "nominal", "label": "来源构成"}],
        }, "xAxisTitle": "QA 数", "valueFormat": "number", "layout": "full"},
    ]
    manifest["tables"] = [
        table("source_composition_table", "来源构成与难度", "source_composition", "report_profile", [
            {"field": "source", "label": "来源", "type": "text"}, {"field": "documents", "label": "文档", "format": "number"}, {"field": "questions", "label": "QA", "format": "number"}, {"field": "qa_share", "label": "QA 占比", "format": "percent"}, {"field": "answerable", "label": "可回答", "format": "number"}, {"field": "unanswerable", "label": "不可回答", "format": "number"}, {"field": "easy", "label": "Easy", "format": "number"}, {"field": "hard", "label": "Hard", "format": "number"}, {"field": "coverage", "label": "覆盖重点", "type": "text"},
        ]),
        table("corpus_profile_table", "Ready Markdown 语料规模", "corpus_profile", "final_corpus", [
            {"field": "source", "label": "来源", "type": "text"}, {"field": "documents", "label": "文档", "format": "number"}, {"field": "size_mib", "label": "MiB", "format": "number"}, {"field": "characters", "label": "总字符", "format": "number"}, {"field": "median_chars", "label": "中位字符", "format": "number"}, {"field": "p95_chars", "label": "P95 字符", "format": "number"}, {"field": "max_chars", "label": "最大字符", "format": "number"},
        ], "字符数按 Unicode 字符统计，不等同于模型 token 数。"),
        table("question_types_table", "15 类来源问题类型", "question_types", "final_questions", [
            {"field": "question_type", "label": "Question type", "type": "text"}, {"field": "count", "label": "QA", "format": "number"}, {"field": "share", "label": "占比", "format": "percent"}, {"field": "docbench", "label": "DocBench", "format": "number"}, {"field": "enterprise", "label": "Enterprise", "format": "number"}, {"field": "multihop", "label": "MultiHop", "format": "number"},
        ], sort=("count", "desc")),
        table("metric_distribution_table", "QA 级主评估维度与建议计分", "metric_distribution", "report_profile", [
            {"field": "metric", "label": "主维度", "type": "text"}, {"field": "count", "label": "QA", "format": "number"}, {"field": "share", "label": "占比", "format": "percent"}, {"field": "source_breakdown", "label": "来源构成", "type": "text"}, {"field": "definition", "label": "定义", "type": "text"}, {"field": "recommended", "label": "建议指标", "type": "text"},
        ], "主维度为透明派生标签，可在审核 UI 中人工覆写。", ("count", "desc")),
        table("domain_distribution_table", "14 个来源领域标签", "domain_distribution", "final_questions", [
            {"field": "domain", "label": "领域标签", "type": "text"}, {"field": "source", "label": "来源", "type": "text"}, {"field": "questions", "label": "QA", "format": "number"}, {"field": "share", "label": "占比", "format": "percent"},
        ], "来源选择标签，不是跨来源统一 ontology。", ("questions", "desc")),
        table("evidence_classes_table", "来源 Evidence class", "evidence_classes", "final_questions", [
            {"field": "evidence_class", "label": "Evidence class", "type": "text"}, {"field": "count", "label": "QA", "format": "number"}, {"field": "share", "label": "占比", "format": "percent"}, {"field": "interpretation", "label": "解释", "type": "text"},
        ], sort=("count", "desc")),
        table("gold_profile_table", "Gold 文档与 Evidence 适用性", "gold_profile", "report_profile", [
            {"field": "source", "label": "来源", "type": "text"}, {"field": "questions", "label": "QA", "format": "number"}, {"field": "with_gold_documents", "label": "有 Gold 文档", "format": "number"}, {"field": "with_evidence", "label": "有 Evidence", "format": "number"}, {"field": "multi_document", "label": "多文档", "format": "number"}, {"field": "avg_gold_docs", "label": "平均 Gold 文档", "format": "number"}, {"field": "avg_evidence_items", "label": "平均 Evidence", "format": "number"},
        ]),
        table("length_profile_table", "问题与参考答案长度", "length_profile", "report_profile", [
            {"field": "field", "label": "字段", "type": "text"}, {"field": "unit", "label": "单位", "type": "text"}, {"field": "mean", "label": "均值", "format": "number"}, {"field": "median", "label": "中位数", "format": "number"}, {"field": "p95", "label": "P95", "format": "number"}, {"field": "max", "label": "最大", "format": "number"}, {"field": "empty", "label": "空值", "format": "number"},
        ]),
        table("source_asset_modes_table", "原始资产与 Ready 表示", "source_asset_modes", "provenance", [
            {"field": "source_mode", "label": "源资产模式", "type": "text"}, {"field": "documents", "label": "文档", "format": "number"}, {"field": "raw_assets", "label": "原始资产", "format": "number"}, {"field": "ready_representation", "label": "Ready 表示", "type": "text"},
        ]),
        table("derivation_delta_table", "质量门后的难度构成", "derivation_delta", "selection_summary", [
            {"field": "bucket", "label": "难度", "type": "text"}, {"field": "questions", "label": "QA", "format": "number"}, {"field": "share", "label": "占比", "format": "percent"}, {"field": "definition", "label": "校准定义", "type": "text"},
        ]),
        table("replacement_transitions_table", "硬排除原因记录", "replacement_transitions", "selection_summary", [
            {"field": "reason", "label": "原因", "type": "text"}, {"field": "reason_key", "label": "Reason key", "type": "text"}, {"field": "count", "label": "记录数", "format": "number"}, {"field": "source_share", "label": "占源集", "format": "percent"},
        ], "原因计数不等于全部 725 条排除：剩余差额来自质量门后的配额、代表性与难度选择。", ("count", "desc")),
        table("audit_detail_table", "QA 质量审计按来源", "audit_detail", "qa_quality_audit", [
            {"field": "source", "label": "来源", "type": "text"}, {"field": "rows", "label": "QA", "format": "number"}, {"field": "pass", "label": "PASS", "format": "number"}, {"field": "low", "label": "LOW", "format": "number"}, {"field": "high", "label": "HIGH", "format": "number"}, {"field": "scope_warning", "label": "Scope warning", "format": "number"}, {"field": "format_sensitive", "label": "格式敏感", "format": "number"}, {"field": "external_temporal", "label": "外部/时效", "format": "number"},
        ]),
        table("audit_checks_table", "发布前质量闸门", "audit_checks", "readiness_bundle", [
            {"field": "check", "label": "检查项", "type": "text"}, {"field": "result", "label": "结果", "type": "text"}, {"field": "value", "label": "观测值", "type": "text"}, {"field": "scope", "label": "范围", "type": "text"},
        ]),
        table("scoring_protocol_table", "建议评测层与有效分母", "scoring_protocol", "report_profile", [
            {"field": "layer", "label": "评测层", "type": "text"}, {"field": "applicable_rows", "label": "适用 QA", "type": "text"}, {"field": "primary_metrics", "label": "主指标", "type": "text"}, {"field": "aggregation", "label": "聚合规则", "type": "text"},
        ]),
        table("file_contract_table", "Eval 包文件契约", "file_contract", "final_manifest", [
            {"field": "path", "label": "相对路径", "type": "text"}, {"field": "rows_or_files", "label": "行/文件", "type": "text"}, {"field": "role", "label": "作用", "type": "text"}, {"field": "evaluator_use", "label": "评测器用法", "type": "text"},
        ]),
    ]

    bodies = {
        "title": f"# {title}\n\n**版本：** 0.3.0 · `curated-final-v0.3`  \n**条件：** `ready-for-eval-text-projection`  \n**状态：** READY · `competitor-eval-ready-v1`  \n**审核签署：** 2026-08-25",
        "abstract": "## 技术摘要\n\nMOI RAG Bench v0.3 Final 从 v0.2 的 1,000 条候选 QA 中按质量优先原则保留 **275 条**，配套 **297 份 Ready Markdown** 与 275 条 Gold；725 条未进入最终集。最终题集覆盖 DocBench、EnterpriseRAG-Bench 与 MultiHop-RAG，包含 248 条可回答题、27 条拒答题和 100 条多文档题。质量审计未发现 HIGH 风险或跨 Gold 文档重复组，MLLM 审计确认 275 条均无显式媒体输入且不需要 MLLM。",
        "technical_summary": "## 审核结论：质量门与纯文本条件均通过\n\n最终选集的基线 Judge 均值为 **0.609927**，落在预设 0.55–0.65 区间；按单题 Judge ≥ 0.9 的冻结阈值，164 条归为 easy、111 条归为 hard。技术失败、MAAS 403 与 MLLM unsafe 进入选集的数量均为 0。与最终 ID 集合对齐的自动 QA 审计给出 114 条 PASS、161 条 LOW、0 条 HIGH；LOW 主要来自 130 条 DocBench scope 契约提醒与 53 条答案格式敏感提醒。",
        "key_findings": "## 三来源构成以 DocBench 和 MultiHop 为主\n\nDocBench 提供 130 条 QA（47.3%），MultiHop-RAG 提供 105 条（38.2%），EnterpriseRAG-Bench 提供 40 条（14.5%）。图中同时保留每个来源的文档数和 QA 占比；本版未纳入 MMDocIR，因此不能把结果解释为页面级或布局检索能力。",
        "scope_definitions": "## 1. 范围、术语与评测条件\n\n**文档粒度**为一个 source document；**评测 scope** 为 `global`，系统一次摄入 297 份 Markdown，再处理 275 个查询。**Gold** 包含 reference answer、Gold 文档集合与可选 evidence。**难度**来自冻结的 MOI baseline Judge：quality gate 通过后再按基线分数校准；它是选择统计，不是待测系统结果。",
        "design_goals": "## 2. v0.3 Final 的方法学贡献\n\n1. **质量优先，再校准难度。** 先排除证据对齐弱、近重复、技术失败和不稳定问题，再把 Judge 均值控制在 0.55–0.65。\n2. **近重复代表题策略。** 每个 lexical/doc-scope cluster 只保留一个证据对齐代表。\n3. **Null-query 语义保护。** 保留全部 120 份 MultiHop 源文档，避免语料裁剪改变“信息不存在”的含义。\n4. **Source / Ready / Provenance 三层分离。** 原始 PDF/Markdown 与评测 Markdown 物理隔离。\n5. **QA 级多能力计分。** 七个主维度区分问答、多跳、拒答、元数据、冲突、约束和完整性。\n6. **双重审核。** QA 风险审核与纯文本/MLLM 审核分别冻结。",
        "construction_method": "## 3. 从 1,000 条候选筛到 275 条\n\n构建器以 `moi-rag-bench-v0.3:curated-balanced:20260825` 为稳定种子，先执行硬质量门，再做来源、类型、答案可用性和难度平衡。最终排除 725 条，保留 275 条；明确记录的硬排除原因覆盖证据对齐弱、任意化 null 格式、近重复/反事实变体、Embedding 403/超时、全文超时、缺失 Judge 和多问题混行等。硬原因计数不是 725 的完整分解，其余差额来自质量门后配额和代表性选择。",
        "source_composition_heading": "## 4. 297 文档支撑 275 条审核后 QA",
        "corpus_summary": "### 4.1 Ready Markdown 语料\n\n297 份 Markdown 共约 **32.78 MiB / 34,207,266 个 Unicode 字符**。DocBench 仍构成长文档体积主体；MultiHop 保留全部 120 文档，其中 7 份不被 Gold 直接引用，用于维持 null-query 的全局语料语义。",
        "question_types_heading": "### 4.2 15 类来源问题类型\n\n最大类型是 DocBench `text-only`（110）；MultiHop 的 inference、comparison 与 temporal 合计 95；拒答由 17 条 `unanswerable` 与 10 条 `null_query` 构成。",
        "domains_heading": "### 4.3 14 个领域标签与 3 个 Evidence class\n\nDocBench 和 MultiHop 保留来源级领域标签；Enterprise 覆盖 Slack、Confluence、GitHub、Jira、Linear、HubSpot、Gmail、Google Drive 与组合来源。Evidence class 仅有 `other`、`text`、`metadata`，没有图像模态输入。",
        "metrics_heading": "## 5. 七个主评估维度避免单一准确率掩盖能力差异",
        "metric_mapping_note": "主维度由透明规则派生：DocBench 拆为常规问答、元数据与拒答；MultiHop 拆为多跳与 null-query 拒答；Enterprise 按冲突、约束、完整性和常规问答拆分。文档/证据检索仍是所有可回答任务的横向评测层，但本版没有独立 MMDocIR retrieval 类型。",
        "gold_heading": "## 6. Gold 分母必须按评测层区分\n\n275 条均有 reference answer；265 条有 Gold 文档，251 条有 evidence，100 条需要两个及以上 Gold 文档。10 条无 Gold 文档题全部是 MultiHop `null_query`；24 条空 evidence 来自 14 条 DocBench `unanswerable` 与 10 条 `null_query`。",
        "separation_heading": "## 7. 原始资产与 Ready-for-Eval 完全分离\n\n原始层含 **130 份 PDF 与 167 份 Markdown**，共 297 个资产、约 397.33 MiB；Ready 层只摄入 297 份 Markdown。297 个 Ready SHA-256 和 297 个源资产 SHA-256 已重算通过，`provenance/documents.jsonl` 是唯一桥接层。",
        "text_only_derivation": "## 8. 质量筛选后再平衡 Easy / Hard\n\n冻结口径以单题基线 Judge ≥ 0.9 记为 easy、低于 0.9 记为 hard；164 条 easy 与 111 条 hard 形成 59.6% / 40.4% 的难度组合，基线 Judge 均值 0.609927。0.55–0.65 仅是最终选集均值的校准区间。质量门明确禁止技术失败进入选集，并对证据对齐、近重复、反事实前提和答案格式稳定性执行逐题审计。",
        "mllm_audit_method": "### 8.1 QA 风险与纯文本审核\n\nMLLM 审计中 275 条全部为 `NOT_APPLICABLE`：显式媒体输入、multimodal 类型、MLLM required、unsafe 和 review 均为 0。QA 质量审计没有 HIGH 或强制 review ID，但 161 条保留 LOW 提醒；其中 130 条 DocBench 的 source row 声明 document-local，而最终包采用 global scope 且 `scope_doc_ids` 为空。",
        "quality_heading": "## 9. 发布前质量闸门全部通过",
        "preflight_note": "Dify live preflight 已确认 `package_valid=true`、`ready=true`、`scope_verified=true`，并实际完成服务探针；它证明该包可被统一 runner 接受，不等价于四个平台都已完成 275-QA 正式评测。",
        "evaluation_protocol": "## 10. 建议评测协议\n\n1. 冻结系统版本、Embedding、LLM、切分参数、top-k、提示词与随机种子。\n2. 只摄入 `documents/<doc_id>.md`，一次建立 297 文档 global 索引。\n3. 逐题记录检索排序、最终答案、引用、弃权、状态、时延和 token/cost。\n4. 对 130 条 scope warning 使用 Gold 文档身份做离线检索评分，不把 scope 信息注入 query。\n5. 对 53 条格式敏感题冻结单位、舍入、日期和列表顺序规范。\n6. 按来源、类型、主维度和难度分层报告，并公布有效分母。\n7. 使用固定 seed bootstrap 报告 95% 置信区间。",
        "metric_formulas": "### 10.1 核心指标定义\n\n- `Hit@k_i = 1`：top-k 与 Gold 文档集合有交集。\n- `Recall@k_i = |Retrieved@k ∩ Gold| / |Gold|`：只对 Gold 非空题取 macro。\n- `Complete@k_i = 1`：全部必要文档或 evidence 均命中。\n- `MRR_i = 1 / rank(first relevant)`；未命中为 0。\n- `Abstention Precision = 正确拒答 / 所有拒答`；`Recall = 正确拒答 / 27 条应拒答题`。\n\n若发布综合分，应先在七个主维度内 macro，再等权聚合并同时公开七个分项。",
        "usage_heading": "## 11. 使用指南",
        "usage_python": "### 11.1 最小 Python 读取\n\n```python\nimport json\nfrom pathlib import Path\n\nroot = Path(\"datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval\")\n\ndef read_jsonl(name):\n    with (root / name).open(encoding=\"utf-8\") as f:\n        return [json.loads(line) for line in f if line.strip()]\n\ncorpus = read_jsonl(\"corpus.jsonl\")\nquestions = read_jsonl(\"questions.jsonl\")\ngold = {row[\"question_id\"]: row for row in read_jsonl(\"gold.jsonl\")}\ningest_paths = [root / row[\"text_path\"] for row in corpus]\n\nfor q in questions:\n    result = rag.query(q[\"question\"])  # 不把 Gold 字段注入 prompt\n    save_result(q[\"question_id\"], result, gold[q[\"question_id\"]])\n```",
        "usage_commands": "### 11.2 离线/在线预检\n\n```bash\npython3 local-rag-platforms/scripts/evaluation/competitor_eval_runner.py preflight \\\n  --system dify_local \\\n  --package datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval\n```\n\n如只验证文件契约而不访问服务，追加 `--dry-run`。正式运行应为每个平台创建独立资源并保留完整 ledger。",
        "result_schema": "### 11.3 建议逐题结果\n\n至少保存 `question_id`、`run_id`、`system_id`、有序 `retrieved_doc_ids`、检索分数、答案、引用、`abstained`、时延、状态、错误与配置指纹。评分产物追加 metric、Judge 版本、分数、理由和有效分母。",
        "limitations": "## 12. 限制与不确定性\n\n- 本版是从 v0.2 定向筛选的 27.5% 子集，不是原 1,000 QA 的无偏样本。\n- 难度由 MOI baseline Judge 校准，可能引入对该基线与 Judge 的选择偏差。\n- 未纳入 MMDocIR、多模态或独立 retrieval 类型，不能代表页面/布局/视觉检索。\n- 130 条 DocBench 存在 LOW scope 契约提醒；评测器必须保持 global query，不应泄露 Gold 文档身份。\n- 53 条答案格式敏感题需要显式 normalization。\n- QA 质量审计声明自身不是最终人工裁决，所有 disposition 仍为 `RETAIN_PENDING_HUMAN_CHECK`。\n- Runner manifest 的 `source_complete` 仍为 false；虽然 297 个本地源资产均存在且哈希通过，公开再分发仍需独立许可证与完整性确认。",
        "responsible_use": "## 13. 负责任使用与泄漏控制\n\n被测系统只能看到 Ready Markdown 与 `question` 文本。Reference answer、Gold 文档、evidence、source question ID、selection audit、baseline 分数和 QA 风险标签必须留在评分侧。公开结果应披露模型数据泄漏风险、联网/缓存、人工后编辑、失败题分母及是否复用知识库。",
        "citation": "## 14. 引用与版本标识\n\n```text\nDataset: MOI RAG Bench v0.3 Final\nDataset ID: moi-rag-bench-v0.3-final\nRevision: curated-final-v0.3\nSchema: competitor-eval-ready-v1\nCondition: ready-for-eval-text-projection\nSelection seed: moi-rag-bench-v0.3:curated-balanced:20260825\nAudit signoff: 2026-08-25\n```",
        "next_steps": "## 15. 推荐下一步\n\n1. 冻结 53 条格式敏感题的答案 normalization。\n2. 对 130 条 scope warning 做一次人工确认并导出最终标注 JSON。\n3. 用同一配置完成四平台 275-QA ingest → retrieval → QA。\n4. 冻结 Evidence matcher、Judge prompt 和失败分母协议。\n5. 发布 baseline 时附逐题结果、配置指纹与 bootstrap 区间。",
        "further_questions": "## 16. 后续研究问题\n\n- Quality-first 子集与完整 1,000 QA 的系统排名是否稳定？\n- Easy/Hard 分层对不同平台的区分度是否一致？\n- Scope warning 是否显著影响 global retrieval，而不影响答案生成？\n- 省略 MMDocIR 后，文本检索结论能否外推到页面和布局任务？",
    }
    block_sources = {
        "abstract": "readiness_bundle", "technical_summary": "readiness_bundle", "key_findings": "selection_summary", "scope_definitions": "final_manifest", "design_goals": "selection_summary", "construction_method": "selection_summary", "corpus_summary": "final_corpus", "question_types_heading": "final_questions", "domains_heading": "final_questions", "metric_mapping_note": "report_profile", "gold_heading": "final_gold", "separation_heading": "provenance", "text_only_derivation": "selection_summary", "mllm_audit_method": "readiness_bundle", "preflight_note": "preflight", "usage_commands": "final_manifest", "limitations": "readiness_bundle",
    }
    blocks = manifest["blocks"]
    for block in blocks:
        block.pop("sourceId", None)
        if block["id"] in bodies:
            block["body"] = bodies[block["id"]]
        if block["id"] in block_sources:
            block["sourceId"] = block_sources[block["id"]]
    source_chart_index = next(index for index, block in enumerate(blocks) if block["id"] == "source_chart")
    if not any(block["id"] == "metric_chart_explainer" for block in blocks):
        blocks.insert(source_chart_index + 1, {"id": "metric_chart_explainer", "type": "markdown", "sourceId": "report_profile", "body": "**主能力分布。** 145 条常规问答与 95 条多跳推理占 87.3%；27 条拒答提供独立弃权测试，结构化、冲突、约束和完整性为小样本能力切片，应单独公布置信区间。"})

    manifest["sources"] = source_entries()
    artifact["sources"] = root_sources(generated_at)
    artifact["package_info"] = {"originUrl": "artifact://moi-rag-bench-v0.3-final-dataset-report", "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False}}
    return artifact


def chart_map() -> dict[str, Any]:
    return {"schema": "moi-rag-bench-report-chart-map-v1", "charts": [
        {"chart_id": "source_qa_chart", "segment": "关键发现", "question": "275 条 QA 如何分布在三个来源？", "family": "comparison", "type": "horizontalBar", "fields": ["source", "questions", "documents", "qa_share"], "source_id": "selection_summary", "takeaway": "DocBench 130、MultiHop-RAG 105、EnterpriseRAG-Bench 40。", "palette_policy": "single-root preferred"},
        {"chart_id": "metric_distribution_chart", "segment": "关键发现", "question": "审核后 QA 的主评估能力如何分布？", "family": "comparison", "type": "horizontalBar", "fields": ["metric", "count", "share", "source_breakdown"], "source_id": "report_profile", "takeaway": "问答 145、多跳 95、拒答 27，其余四项为小样本能力切片。", "palette_policy": "single-root preferred"},
    ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--chart-map", type=Path, default=DEFAULT_CHART_MAP)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    package_manifest = read_json(PACKAGE / "manifest.json")
    corpus = read_jsonl(PACKAGE / "corpus.jsonl")
    questions = read_jsonl(PACKAGE / "questions.jsonl")
    gold = read_jsonl(PACKAGE / "gold.jsonl")
    provenance = read_jsonl(RAW_ROOT / "provenance/documents.jsonl")
    selection_rows = read_jsonl(BENCHMARK / "selection-audit.jsonl")
    selection_summary = read_json(BENCHMARK / "selection-summary.json")
    package_validation = read_json(RAW_ROOT / "validation.json")
    quality_rows = read_jsonl(QUALITY_ROOT / "qa-quality-audit.jsonl")
    quality_summary = read_json(QUALITY_ROOT / "summary.json")
    mllm_summary = read_json(MLLM_ROOT / "summary.json")
    mllm_signoff = read_json(MLLM_ROOT / "text-only-semantic-audit-signoff.json")
    preflight = read_json(PREFLIGHT)

    checks = validate(package_manifest, corpus, questions, gold, provenance, selection_rows, selection_summary, package_validation, quality_rows, quality_summary, mllm_summary, mllm_signoff, preflight)
    profiles = build_profiles(corpus, questions, gold, provenance, selection_rows, selection_summary, quality_summary, checks)
    assert sum(row["count"] for row in profiles["question_types"]) == 275
    assert sum(row["count"] for row in profiles["metric_distribution"]) == 275
    assert len(profiles["question_types"]) == 15
    assert len(profiles["metric_distribution"]) == 7
    assert len(profiles["domain_distribution"]) == 14

    artifact = build_artifact(profiles, mllm_signoff)
    title = artifact["manifest"]["title"]
    assert artifact["manifest"]["blocks"][0]["body"].startswith(f"# {title}")
    result = {"status": "OK", **EXPECTED, "question_types": 15, "primary_metrics": 7, "domains": 14, **checks}
    if args.check_only:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.chart_map.parent.mkdir(parents=True, exist_ok=True)
    args.chart_map.write_text(json.dumps(chart_map(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result.update({"output": str(args.output.relative_to(ROOT)), "chart_map": str(args.chart_map.relative_to(ROOT))})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
