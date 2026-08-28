#!/usr/bin/env python3
"""Curate MOI RAG Bench v0.2 into a compact, difficulty-balanced v0.3 package."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
SOURCE_BENCH = ROOT / "datasets/moi-rag-bench-v0.2"
SOURCE_RAW = ROOT / "datasets/moi-rag-bench-v0.2-raw-corpus"
SOURCE_READY = SOURCE_RAW / "ready_for_eval"
QUALITY_AUDIT = ROOT / "runs/readiness/qa-quality-audit-20260821-v0.2-final-mllm-rerun/qa-quality-audit.jsonl"
MLLM_AUDIT = ROOT / "runs/readiness/mllm-semantic-audit-20260821-v0.2/audit.jsonl"
MOI_RUN = ROOT / "runs/moi-v0.2-deepseek-20260824/20260824-moi-v02-dsv4f-maas-full-reuse-index/qa-merged-1000-dsv4f-judge"
DEFAULT_BENCH_OUTPUT = ROOT / "datasets/moi-rag-bench-v0.3-final"
DEFAULT_RAW_OUTPUT = ROOT / "datasets/moi-rag-bench-v0.3-final-raw-corpus"
SELECTION_SEED = "moi-rag-bench-v0.3:curated-balanced:20260825"
DATASET_ID = "moi-rag-bench-v0.3-final"

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", re.IGNORECASE)
STOPWORDS = frozenset(
    """
    a an and are as at be been by can could did do does for from had has have how in into is it its
    may of on or that the their this to was were what when where which who why with would according
    based article articles report reports recently
    """.split()
)
POSITIVE_ANSWERS = {"yes", "true", "agree", "similar", "same", "consistent"}
CONTRIVED_PREMISE_RE = re.compile(
    r"\b(?:favorite colou?r|favorite ice cream|customer satisfaction scores?|perfect compliance|"
    r"no betting options|fail(?:ed)? to publish|withdrawal from (?:her|his|the) tour schedule)\b",
    re.IGNORECASE,
)
ARBITRARY_NULL_FORMAT_RE = re.compile(
    r"\b(?:single letter|single-digit|single character|initial of|initial ['\"]?[a-z]|letter grade|which letter)\b",
    re.IGNORECASE,
)

# The selected benchmark has 275 QA: enough coverage for stable slices without
# retaining the repeated templates that dominate v0.2.
STRATA = [
    ("docbench", "meta-data", None, None, 3),
    ("docbench", "unanswerable", None, "hard", 8),
    ("docbench", "unanswerable", None, "easy", 9),
    ("docbench", "text-only", None, "hard", 44),
    ("docbench", "text-only", None, "easy", 66),
    ("enterprise", "basic", None, "hard", 3),
    ("enterprise", "basic", None, "easy", 9),
    ("enterprise", "semantic", None, "hard", 13),
    ("enterprise", "semantic", None, "easy", 3),
    ("enterprise", "intra_document_reasoning", None, "easy", 3),
    ("enterprise", "project_related", None, "easy", 2),
    ("enterprise", "constrained", None, "easy", 2),
    ("enterprise", "conflicting_info", None, "easy", 2),
    ("enterprise", "miscellaneous", None, "easy", 2),
    ("enterprise", "completeness", None, "easy", 1),
    ("multihop", "inference_query", None, "hard", 10),
    ("multihop", "inference_query", None, "easy", 25),
    ("multihop", "comparison_query", "positive", "hard", 10),
    ("multihop", "comparison_query", "positive", "easy", 10),
    ("multihop", "comparison_query", "negative", "hard", 5),
    ("multihop", "comparison_query", "negative", "easy", 5),
    ("multihop", "temporal_query", "positive", "hard", 10),
    ("multihop", "temporal_query", "positive", "easy", 10),
    ("multihop", "temporal_query", "negative", "hard", 6),
    ("multihop", "temporal_query", "negative", "easy", 4),
    ("multihop", "null_query", None, "hard", 1),
    ("multihop", "null_query", None, "easy", 9),
]


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold().rstrip("."))


def tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_RE.findall(str(value or ""))
        if len(token) > 2 and token.casefold() not in STOPWORDS
    }


def evidence_text(row: Mapping[str, Any]) -> str:
    values: list[str] = []
    for item in row.get("gold_evidence") or []:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, Mapping):
            for key in ("evidence", "text", "quote", "content", "snippet", "passage"):
                if item.get(key):
                    values.append(str(item[key]))
                    break
    return "\n".join(values)


def stable_key(question_id: str) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}|{question_id}".encode()).hexdigest()


def answer_label(row: Mapping[str, Any]) -> str:
    answer = normalized(row.get("reference_answer"))
    if answer in POSITIVE_ANSWERS:
        return "positive"
    if answer == "no":
        return "negative"
    return "other"


def result_summaries(path: Path) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            question_id = str((row.get("case") or {}).get("id") or "")
            metrics = row.get("metrics") or {}
            summaries[question_id] = {
                "status": row.get("status"),
                "error": row.get("error"),
                "source_recall": metrics.get("source_recall"),
                "evidence_recall": metrics.get("evidence_recall"),
                "reciprocal_rank": metrics.get("reciprocal_rank"),
                "retrieval_latency_ms": row.get("retrieval_latency_ms"),
                "generation_latency_ms": row.get("generation_latency_ms"),
                "generation_context_truncated": row.get("generation_context_truncated"),
                "answer": row.get("answer"),
            }
    return summaries


def failure_code(record: Mapping[str, Any]) -> str:
    error = str(record.get("error") or "")
    if "ModelArts.81011" in error or "sensitive information" in error:
        return "maas_embedding_403"
    if "context deadline exceeded" in error:
        return "fulltext_timeout"
    if "TLS handshake timeout" in error:
        return "embedding_tls_timeout"
    return "runner_failed"


def build_states() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    questions = read_jsonl(SOURCE_READY / "questions.jsonl")
    gold_by_id = {row["question_id"]: row for row in read_jsonl(SOURCE_READY / "gold.jsonl")}
    audit_by_id = {row["question_id"]: row for row in read_jsonl(QUALITY_AUDIT)}
    mllm_by_id = {row["question_id"]: row for row in read_jsonl(MLLM_AUDIT)}
    judge_by_id = {row["question_id"]: row for row in read_jsonl(MOI_RUN / "judge/judge-terminal-ledger.jsonl")}
    results = result_summaries(MOI_RUN / "merged-results.jsonl")
    states: list[dict[str, Any]] = []

    for ordinal, question in enumerate(questions):
        question_id = str(question["question_id"])
        gold = gold_by_id[question_id]
        audit = audit_by_id[question_id]
        mllm = mllm_by_id[question_id]
        judge = judge_by_id[question_id]
        judgement = judge.get("judgement") if isinstance(judge.get("judgement"), Mapping) else {}
        dimensions = judgement.get("dimensions") if isinstance(judgement.get("dimensions"), Mapping) else {}
        judge_score = judgement.get("overall")
        question_tokens = tokens(question.get("question"))
        evidence_tokens = tokens(evidence_text(question))
        overlap = len(question_tokens & evidence_tokens) / max(1, len(question_tokens))
        word_count = len(TOKEN_RE.findall(str(question.get("question") or "")))
        hard_reasons: list[str] = []

        if judge.get("status") != "SUCCESS":
            hard_reasons.append(failure_code(judge))
        if judge.get("status") == "SUCCESS" and not isinstance(judge_score, (int, float)):
            hard_reasons.append("missing_judge_overall")
        if audit.get("risk_level") in {"HIGH", "MEDIUM"}:
            hard_reasons.append("quality_audit_not_low_risk")
        if question.get("question_type") == "una-web":
            hard_reasons.append("external_web_question")
        if mllm.get("status") in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE", "REVIEW_REQUIRED"}:
            hard_reasons.append("text_projection_not_safe")
        strict = (dimensions.get("strict_unanswerable") or {}).get("score")
        faithful = (dimensions.get("runtime_context_faithfulness") or {}).get("score")
        if not question.get("answerable") and strict == 0 and faithful == 1:
            hard_reasons.append("unanswerable_label_contradicted_by_runtime_context")

        external_risk = "external_or_temporal_scope" in audit.get("risk_types", [])
        question_text = str(question.get("question") or "")
        if question_text.count("?") > 1:
            hard_reasons.append("multiple_questions_in_one_row")
        if question.get("question_type") in {"comparison_query", "temporal_query"} and overlap < 0.15:
            hard_reasons.append("weak_question_evidence_alignment")
        if CONTRIVED_PREMISE_RE.search(question_text):
            hard_reasons.append("contrived_counterfactual_premise")
        if question.get("question_type") == "null_query" and ARBITRARY_NULL_FORMAT_RE.search(question_text):
            hard_reasons.append("arbitrary_null_answer_format")
        quality_score = (
            4.0 * overlap
            + (0.25 if audit.get("risk_level") == "PASS" else 0.0)
            - (0.35 if external_risk else 0.0)
            - max(0, word_count - 70) / 50
        )
        states.append(
            {
                "ordinal": ordinal,
                "question_id": question_id,
                "question": question,
                "gold": gold,
                "source_dataset": question.get("source_dataset"),
                "question_type": question.get("question_type"),
                "answerable": bool(question.get("answerable")),
                "answer": normalized(question.get("reference_answer")),
                "answer_label": answer_label(question),
                "gold_doc_ids": list(question.get("gold_doc_ids") or gold.get("gold_doc_ids") or []),
                "quality_risk_level": audit.get("risk_level"),
                "quality_risk_types": list(audit.get("risk_types") or []),
                "mllm_status": mllm.get("status"),
                "question_word_count": word_count,
                "question_evidence_token_overlap": round(overlap, 6),
                "external_or_temporal_scope_flag": external_risk,
                "quality_score": quality_score,
                "judge_status": judge.get("status"),
                "judge_score": float(judge_score) if isinstance(judge_score, (int, float)) else None,
                "difficulty_bucket": "easy" if isinstance(judge_score, (int, float)) and judge_score >= 0.9 else "hard",
                "judge_dimensions": {
                    key: value.get("score")
                    for key, value in dimensions.items()
                    if isinstance(value, Mapping)
                },
                "runner_error": judge.get("error"),
                "result": results[question_id],
                "hard_exclusion_reasons": hard_reasons,
            }
        )
    return states, {row["question_id"]: row for row in states}


def assign_duplicate_clusters(states: list[dict[str, Any]]) -> None:
    parent = list(range(len(states)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    token_sets: list[set[str]] = []
    for index, state in enumerate(states):
        grouped[(str(state["source_dataset"]), str(state["question_type"]))].append(index)
        token_sets.append(tokens(state["question"].get("question")))

    # ponytail: O(n^2) within source/type groups is intentional for 1,000 rows;
    # replace with MinHash only if the benchmark grows by an order of magnitude.
    for indexes in grouped.values():
        for offset, left in enumerate(indexes):
            left_tokens = token_sets[left]
            left_docs = set(states[left]["gold_doc_ids"])
            for right in indexes[offset + 1 :]:
                right_tokens = token_sets[right]
                union_size = len(left_tokens | right_tokens)
                similarity = len(left_tokens & right_tokens) / union_size if union_size else 0.0
                right_docs = set(states[right]["gold_doc_ids"])
                same_answer = states[left]["answer"] == states[right]["answer"]
                same_docs = bool(left_docs) and left_docs == right_docs
                short_answer = states[left]["answer"] in {"yes", "no", "insufficient information", "not mentioned"}
                if (same_answer and similarity >= 0.55) or (same_docs and similarity >= 0.45) or (same_answer and short_answer and similarity >= 0.72):
                    union(left, right)

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(states)):
        clusters[find(index)].append(index)
    for indexes in clusters.values():
        if len(indexes) == 1:
            states[indexes[0]]["duplicate_cluster_id"] = None
            states[indexes[0]]["duplicate_cluster_size"] = 1
            continue
        question_ids = sorted(states[index]["question_id"] for index in indexes)
        cluster_id = "near_duplicate_" + hashlib.sha1("\n".join(question_ids).encode()).hexdigest()[:12]
        eligible = [index for index in indexes if not states[index]["hard_exclusion_reasons"]]
        labels = {states[index]["answer_label"] for index in indexes}
        positive = [index for index in indexes if states[index]["answer_label"] == "positive"]
        eligible_positive = [index for index in positive if not states[index]["hard_exclusion_reasons"]]
        if labels >= {"positive", "negative"}:
            # Polarity-flipped duplicates are evidence-grounded most reliably by
            # their positive version; negative mutations often introduce an
            # arbitrary false premise and merely test its absence.
            representative_pool = eligible_positive or positive
        else:
            representative_pool = eligible or indexes
        representative = max(
            representative_pool,
            key=lambda index: (states[index]["quality_score"], -states[index]["question_word_count"], stable_key(states[index]["question_id"])),
        )
        for index in indexes:
            states[index]["duplicate_cluster_id"] = cluster_id
            states[index]["duplicate_cluster_size"] = len(indexes)
            states[index]["duplicate_representative"] = states[representative]["question_id"]
            if index != representative and not states[index]["hard_exclusion_reasons"]:
                states[index]["hard_exclusion_reasons"].append("near_duplicate_or_counterfactual_variant")


def select_rows(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    doc_use: Counter[str] = Counter()
    docset_use: Counter[tuple[str, ...]] = Counter()
    answer_use: Counter[tuple[str, str, str]] = Counter()

    def cap_allows(row: Mapping[str, Any], relaxed: bool = False) -> bool:
        dataset = str(row["source_dataset"])
        question_type = str(row["question_type"])
        docs = list(row["gold_doc_ids"])
        answer = str(row["answer"])
        if not relaxed and dataset == "docbench" and any(doc_use[doc] >= 1 for doc in docs):
            return False
        if not relaxed and dataset == "multihop" and docs and docset_use[tuple(sorted(docs))] >= 2:
            return False
        if not relaxed and dataset == "multihop" and any(doc_use[doc] >= 6 for doc in docs):
            return False
        if question_type == "inference_query" and answer_use[(dataset, question_type, answer)] >= 3:
            return False
        if dataset == "docbench" and question_type == "text-only":
            cap = 15 if answer == "yes" else 10 if answer == "no" else 3
            if answer_use[(dataset, question_type, answer)] >= cap:
                return False
        return True

    def choose(dataset: str, question_type: str, label: str | None, bucket: str | None, count: int) -> None:
        chosen = 0
        for relaxed in (False, True):
            while chosen < count:
                candidates = [
                    row
                    for row in states
                    if row["question_id"] not in selected_ids
                    and not row["hard_exclusion_reasons"]
                    and row["source_dataset"] == dataset
                    and row["question_type"] == question_type
                    and (label is None or row["answer_label"] == label)
                    and (bucket is None or row["difficulty_bucket"] == bucket)
                    and cap_allows(row, relaxed=relaxed)
                ]
                if not candidates:
                    break
                candidates.sort(
                    key=lambda row: (
                        -sum(doc_use[doc] == 0 for doc in row["gold_doc_ids"]),
                        answer_use[(dataset, question_type, row["answer"])],
                        docset_use[tuple(sorted(row["gold_doc_ids"]))],
                        -row["quality_score"],
                        row["question_word_count"],
                        stable_key(row["question_id"]),
                    )
                )
                row = candidates[0]
                row["selection_stratum"] = {
                    "source_dataset": dataset,
                    "question_type": question_type,
                    "answer_label": label,
                    "difficulty_bucket": bucket,
                    "document_cap_relaxed": relaxed,
                }
                selected.append(row)
                selected_ids.add(row["question_id"])
                doc_use.update(row["gold_doc_ids"])
                docset_use[tuple(sorted(row["gold_doc_ids"]))] += 1
                answer_use[(dataset, question_type, row["answer"])] += 1
                chosen += 1
            if chosen == count:
                return
        available = Counter(
            (row["answer_label"], row["difficulty_bucket"])
            for row in states
            if not row["hard_exclusion_reasons"] and row["source_dataset"] == dataset and row["question_type"] == question_type
        )
        raise RuntimeError(f"Cannot fill stratum {(dataset, question_type, label, bucket)}: {chosen}/{count}; available={dict(available)}")

    for stratum in STRATA:
        choose(*stratum)
    selected.sort(key=lambda row: row["ordinal"])
    return selected


def mean(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if isinstance(value, (int, float))]
    return sum(usable) / len(usable) if usable else None


def selection_summary(states: list[dict[str, Any]], selected: list[dict[str, Any]], documents: list[dict[str, Any]]) -> dict[str, Any]:
    selected_ids = {row["question_id"] for row in selected}
    excluded_reasons = Counter(reason for row in states if row["question_id"] not in selected_ids for reason in row["hard_exclusion_reasons"])
    scores = [row["judge_score"] for row in selected]
    summary = {
        "schema": "moi-rag-bench-v0.3-curation-summary-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_seed": SELECTION_SEED,
        "source_dataset_id": "moi-rag-bench-v0.2-ready-for-eval",
        "dataset_id": DATASET_ID,
        "counts": {
            "source_questions": len(states),
            "selected_questions": len(selected),
            "excluded_questions": len(states) - len(selected),
            "selected_documents": len(documents),
            "maas_403_selected": sum("maas_embedding_403" in row["hard_exclusion_reasons"] for row in selected),
            "technical_failures_selected": sum(row["judge_status"] != "SUCCESS" for row in selected),
            "mllm_unsafe_selected": sum(row["mllm_status"] in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE", "REVIEW_REQUIRED"} for row in selected),
        },
        "selected_by_source": dict(sorted(Counter(row["source_dataset"] for row in selected).items())),
        "selected_by_type": {
            f"{dataset}:{question_type}": count
            for (dataset, question_type), count in sorted(Counter((row["source_dataset"], row["question_type"]) for row in selected).items())
        },
        "selected_by_difficulty": dict(sorted(Counter(row["difficulty_bucket"] for row in selected).items())),
        "selected_judge_score": {
            "n": sum(isinstance(value, (int, float)) for value in scores),
            "mean": round(float(mean(scores) or 0.0), 6),
            "min": min(scores),
            "max": max(scores),
        },
        "selected_moi_retrieval": {
            metric: round(float(mean(row["result"].get(metric) for row in selected) or 0.0), 6)
            for metric in ("source_recall", "evidence_recall", "reciprocal_rank")
        },
        "selected_answerability": dict(sorted(Counter("answerable" if row["answerable"] else "unanswerable" for row in selected).items())),
        "selected_document_sources": dict(sorted(Counter(row["source_dataset"] for row in documents).items())),
        "hard_exclusion_reason_counts": dict(sorted(excluded_reasons.items())),
        "policy": {
            "quality_first": True,
            "difficulty_calibration_only_after_quality_gate": True,
            "judge_mean_required_range": [0.55, 0.65],
            "near_duplicate_policy": "one evidence-aligned representative per lexical/doc-scope cluster",
            "null_query_corpus_policy": "retain all 120 MultiHop source documents so absence semantics are not changed by corpus pruning",
            "technical_failure_policy": "exclude all runner failures, including every ModelArts.81011 HTTP 403 row",
        },
    }
    return summary


def build_selection() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    states, _ = build_states()
    assign_duplicate_clusters(states)
    selected = select_rows(states)
    selected_ids = {row["question_id"] for row in selected}
    bench_documents = read_jsonl(SOURCE_BENCH / "documents.jsonl")
    required_docs = {doc for row in selected for doc in row["gold_doc_ids"]}
    required_docs.update(row["doc_id"] for row in bench_documents if row.get("source_dataset") == "multihop")
    documents = [row for row in bench_documents if row["doc_id"] in required_docs]
    summary = selection_summary(states, selected, documents)
    score_mean = summary["selected_judge_score"]["mean"]
    if len(selected) != 275:
        raise RuntimeError(f"Expected 275 selected QA, got {len(selected)}")
    if not 0.55 <= score_mean <= 0.65:
        raise RuntimeError(f"Selected Judge mean outside [0.55, 0.65]: {score_mean}")
    if summary["counts"]["maas_403_selected"] or summary["counts"]["technical_failures_selected"]:
        raise RuntimeError("Technical failure leaked into the selected set")
    return states, selected, documents, summary


def audit_rows(states: list[dict[str, Any]], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_ids = {row["question_id"] for row in selected}
    rows: list[dict[str, Any]] = []
    for state in states:
        is_selected = state["question_id"] in selected_ids
        reasons = list(state["hard_exclusion_reasons"])
        if not is_selected and not reasons:
            reasons = ["coverage_or_difficulty_quota_reduction"]
        rows.append(
            {
                "question_id": state["question_id"],
                "source_dataset": state["source_dataset"],
                "question_type": state["question_type"],
                "question": state["question"].get("question"),
                "reference_answer": state["question"].get("reference_answer"),
                "answerable": state["answerable"],
                "gold_doc_ids": state["gold_doc_ids"],
                "selected": is_selected,
                "disposition": "SELECTED" if is_selected else "EXCLUDED",
                "reasons": reasons,
                "selection_stratum": state.get("selection_stratum"),
                "quality": {
                    "risk_level": state["quality_risk_level"],
                    "risk_types": state["quality_risk_types"],
                    "question_word_count": state["question_word_count"],
                    "question_evidence_token_overlap": state["question_evidence_token_overlap"],
                    "external_or_temporal_scope_flag": state["external_or_temporal_scope_flag"],
                    "near_duplicate_cluster_id": state.get("duplicate_cluster_id"),
                    "near_duplicate_cluster_size": state.get("duplicate_cluster_size"),
                    "near_duplicate_representative": state.get("duplicate_representative"),
                },
                "moi_baseline": {
                    "runner_status": state["result"].get("status"),
                    "runner_error": state["runner_error"],
                    "judge_status": state["judge_status"],
                    "judge_score": state["judge_score"],
                    "difficulty_bucket": state["difficulty_bucket"],
                    "source_recall": state["result"].get("source_recall"),
                    "evidence_recall": state["result"].get("evidence_recall"),
                    "reciprocal_rank": state["result"].get("reciprocal_rank"),
                    "answer": state["result"].get("answer"),
                },
            }
        )
    return rows


def materialize(bench_output: Path, raw_output: Path) -> dict[str, Any]:
    if bench_output.exists() or raw_output.exists():
        raise RuntimeError(f"Output already exists: {bench_output if bench_output.exists() else raw_output}")
    states, selected, documents, summary = build_selection()
    selected_ids = {row["question_id"] for row in selected}
    selected_doc_ids = {row["doc_id"] for row in documents}
    source_questions = read_jsonl(SOURCE_BENCH / "questions.jsonl")
    source_gold = read_jsonl(SOURCE_BENCH / "gold.jsonl")
    questions = [row for row in source_questions if row["question_id"] in selected_ids]
    gold_by_id = {row["question_id"]: row for row in source_gold}
    gold = [gold_by_id[row["question_id"]] for row in questions]
    audit = audit_rows(states, selected)

    bench_output.mkdir(parents=True)
    write_jsonl(bench_output / "documents.jsonl", documents)
    write_jsonl(bench_output / "questions.jsonl", questions)
    write_jsonl(bench_output / "gold.jsonl", gold)
    write_jsonl(bench_output / "selection-audit.jsonl", audit)
    write_json(bench_output / "selection-summary.json", summary)
    write_json(
        bench_output / "manifest.json",
        {
            "schema": "moi-rag-bench-unified-v0.3-curated",
            "version": "0.3.0",
            "dataset_id": DATASET_ID,
            "generated_at": "2026-08-25",
            "selection_seed": SELECTION_SEED,
            "source_benchmark": "datasets/moi-rag-bench-v0.2",
            "counts": {"documents": len(documents), "questions": len(questions), "gold": len(gold)},
            "artifacts": {
                "documents": "documents.jsonl",
                "questions": "questions.jsonl",
                "gold": "gold.jsonl",
                "selection_audit": "selection-audit.jsonl",
                "selection_summary": "selection-summary.json",
            },
            "curation": summary["policy"],
        },
    )

    ready_output = raw_output / "ready_for_eval"
    source_output = raw_output / "source_files"
    provenance_output = raw_output / "provenance"
    (ready_output / "documents").mkdir(parents=True)
    source_output.mkdir(parents=True)
    provenance_output.mkdir(parents=True)
    source_corpus = read_jsonl(SOURCE_READY / "corpus.jsonl")
    corpus = []
    for row in source_corpus:
        if row["doc_id"] not in selected_doc_ids:
            continue
        copied = dict(row)
        copied["scope_id"] = f"{DATASET_ID}-global"
        corpus.append(copied)
        shutil.copy2(SOURCE_READY / row["text_path"], ready_output / row["text_path"])

    ready_questions_by_id = {row["question_id"]: row for row in read_jsonl(SOURCE_READY / "questions.jsonl")}
    ready_gold_by_id = {row["question_id"]: row for row in read_jsonl(SOURCE_READY / "gold.jsonl")}
    ready_questions = [ready_questions_by_id[row["question_id"]] for row in questions]
    ready_gold = [ready_gold_by_id[row["question_id"]] for row in questions]
    write_jsonl(ready_output / "corpus.jsonl", corpus)
    write_jsonl(ready_output / "questions.jsonl", ready_questions)
    write_jsonl(ready_output / "gold.jsonl", ready_gold)

    qa_counts = Counter(doc for row in ready_questions for doc in row.get("gold_doc_ids") or [])
    provenance = []
    for row in read_jsonl(SOURCE_RAW / "provenance/documents.jsonl"):
        if row["doc_id"] not in selected_doc_ids:
            continue
        copied = dict(row)
        copied["linked_question_count"] = qa_counts[row["doc_id"]]
        provenance.append(copied)
        source_dir = SOURCE_RAW / row["source_directory"]
        target_dir = raw_output / row["source_directory"]
        shutil.copytree(source_dir, target_dir)
    write_jsonl(provenance_output / "documents.jsonl", provenance)

    ready_manifest = {
        "schema": "competitor-eval-ready-v1",
        "schema_version": "competitor-eval-ready-v1",
        "package_schema": "competitor-eval-ready-v1",
        "dataset_id": DATASET_ID,
        "dataset_name": "MOI RAG Benchmark v0.3 curated final text projection",
        "dataset_revision": "curated-final-v0.3",
        "revision": "curated-final-v0.3",
        "split": "evaluation",
        "scope": "global",
        "scope_policy": "global",
        "condition": "ready-for-eval-text-projection",
        "mllm_required": False,
        "image_llm": "NOT_APPLICABLE",
        "multimodal": False,
        "status": "READY_LOCAL_RAW_CORPUS",
        "readiness_status": "READY",
        "source_complete": False,
        "ingest_representation": "source_document",
        "evaluation": {"scope": "global", "ingest_representation": "source_document"},
        "documents": "corpus.jsonl",
        "corpus_path": "corpus.jsonl",
        "questions": "questions.jsonl",
        "questions_path": "questions.jsonl",
        "gold": "gold.jsonl",
        "gold_path": "gold.jsonl",
        "counts": {
            "documents": len(corpus),
            "corpus_rows": len(corpus),
            "runner_documents": len(corpus),
            "questions": len(ready_questions),
            "question_rows": len(ready_questions),
            "gold_rows": len(ready_gold),
        },
        "conditions": {
            "raw_asset_copy": True,
            "text_projection": True,
            "native_pdf_ingest": False,
            "source_ready_separated": True,
            "mllm_required": False,
            "note": "Only ready_for_eval/documents are package inputs; source assets remain isolated under ../source_files.",
        },
        "platform_support": {
            platform: {"text": True, "native_qa": True, "multimodal": False}
            for platform in ("dify_local", "fastgpt_local", "maxkb_local", "moi_local")
        },
        "source_manifest": "../provenance/documents.jsonl",
        "benchmark_manifest": "../../moi-rag-bench-v0.3-final/manifest.json",
    }
    write_json(ready_output / "manifest.json", ready_manifest)

    source_bytes = sum(int(row.get("source_asset_bytes") or 0) for row in provenance)
    raw_manifest = {
        "schema": "moi-rag-bench-raw-corpus-v0.3",
        "version": "0.3.0",
        "layout_revision": "source-ready-separated-v1",
        "generated_at": "2026-08-25",
        "source_benchmark": "datasets/moi-rag-bench-v0.3-final",
        "counts": {
            "documents": len(corpus),
            "questions": len(ready_questions),
            "gold": len(ready_gold),
            "source_asset_count": sum(int(row.get("source_asset_count") or 0) for row in provenance),
            "source_asset_bytes": source_bytes,
            "source_asset_mib": round(source_bytes / 1024 / 1024, 2),
        },
        "source_datasets": dict(sorted(Counter(row["source_dataset"] for row in provenance).items())),
        "separation_contract": {
            "source_files_root": "source_files/",
            "ready_for_eval_root": "ready_for_eval/",
            "provenance_root": "provenance/",
            "source_files_contain_generated_eval_text": False,
            "ready_for_eval_contains_original_source_assets": False,
            "ready_ingest_paths_must_remain_under": "ready_for_eval/documents/",
            "only_bridge": "provenance/documents.jsonl",
        },
        "selection_summary": "../moi-rag-bench-v0.3-final/selection-summary.json",
    }
    write_json(raw_output / "manifest.json", raw_manifest)
    write_json(
        raw_output / "qa-eval-plan.json",
        {
            "schema": "moi-rag-bench-qa-eval-plan-v0.3",
            "package_manifest": "ready_for_eval/manifest.json",
            "scope": "global",
            "question_count": len(ready_questions),
            "document_count": len(corpus),
            "default_ingest_projection": "ready_for_eval/documents",
        },
    )
    (raw_output / "README.md").write_text(
        f"# MOI RAG Benchmark v0.3 curated final\n\n"
        f"- Documents: {len(corpus)}\n- QA: {len(ready_questions)}\n"
        f"- Baseline MOI Judge mean: {summary['selected_judge_score']['mean']}\n"
        "- `source_files/` and `ready_for_eval/` are fully separated; only the latter is used for text-only evaluation.\n",
        encoding="utf-8",
    )

    validate_outputs(bench_output, raw_output, summary)
    return summary


def validate_outputs(bench_output: Path, raw_output: Path, summary: Mapping[str, Any]) -> None:
    documents = read_jsonl(bench_output / "documents.jsonl")
    questions = read_jsonl(raw_output / "ready_for_eval/questions.jsonl")
    gold = read_jsonl(raw_output / "ready_for_eval/gold.jsonl")
    corpus = read_jsonl(raw_output / "ready_for_eval/corpus.jsonl")
    provenance = read_jsonl(raw_output / "provenance/documents.jsonl")
    question_ids = [row["question_id"] for row in questions]
    gold_ids = [row["question_id"] for row in gold]
    corpus_ids = {row["doc_id"] for row in corpus}
    if not (len(question_ids) == len(set(question_ids)) == len(gold_ids) == 275):
        raise RuntimeError("QA/gold count or uniqueness validation failed")
    if question_ids != gold_ids:
        raise RuntimeError("Question/gold ordering differs")
    if len(documents) != len(corpus) or len(corpus) != len(provenance):
        raise RuntimeError("Document/corpus/provenance count differs")
    if any(doc not in corpus_ids for row in questions for doc in row.get("gold_doc_ids") or []):
        raise RuntimeError("Selected QA references a document outside the selected corpus")
    if any(str(row.get("question_type", "")).casefold().startswith("multimodal") for row in questions):
        raise RuntimeError("Multimodal row leaked into text-only final package")
    for row in corpus:
        path = raw_output / "ready_for_eval" / row["text_path"]
        if not path.is_file():
            raise RuntimeError(f"Missing ready document: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise RuntimeError(f"Ready document hash mismatch: {path}")
    validation = {
        "schema": "moi-rag-bench-v0.3-validation-v1",
        "status": "PASS",
        "questions": len(questions),
        "documents": len(corpus),
        "gold_closure": True,
        "question_gold_order_match": True,
        "ready_document_hashes_match": True,
        "maas_403_selected": summary["counts"]["maas_403_selected"],
        "technical_failures_selected": summary["counts"]["technical_failures_selected"],
        "mllm_unsafe_selected": summary["counts"]["mllm_unsafe_selected"],
        "judge_score_mean": summary["selected_judge_score"]["mean"],
        "judge_score_required_range": [0.55, 0.65],
    }
    write_json(bench_output / "validation.json", validation)
    write_json(raw_output / "validation.json", validation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-output", type=Path, default=DEFAULT_BENCH_OUTPUT)
    parser.add_argument("--raw-output", type=Path, default=DEFAULT_RAW_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        _, _, _, summary = build_selection()
    else:
        summary = materialize(args.benchmark_output.resolve(), args.raw_output.resolve())
    print(json.dumps({"status": "OK", "dry_run": args.dry_run, "summary": summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
