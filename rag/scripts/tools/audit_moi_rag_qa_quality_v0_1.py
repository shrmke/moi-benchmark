#!/usr/bin/env python3
"""First-pass quality audit for the MOI RAG benchmark QA package.

The audit is intentionally package-aware.  It does not rewrite questions or
gold; it produces one review record per QA and filtered lists for rows that
should be excluded or reviewed before a global-corpus RAG evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGE = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"
DEFAULT_OUTPUT = ROOT / "runs" / "readiness" / "qa-quality-audit-20260820" / "qa-quality-audit.jsonl"
DEFAULT_MLLM_AUDIT = ROOT / "runs" / "readiness" / "mllm-semantic-audit-20260820" / "after-audit.jsonl"

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", re.IGNORECASE)
QUESTION_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
GENERIC_SCOPE_RE = re.compile(
    r"\b(?:section|sections|chapter|chapters|page|pages|figure|fig\.?|table|tables|"
    r"paragraph|paragraphs|document|documents|paper|papers|report|reports|article|"
    r"articles|newspaper|newspapers|file|files|study|studies|last page|first sentence|"
    r"cover page|front page)\b",
    re.IGNORECASE,
)
ANAPHOR_RE = re.compile(
    r"\b(?:the|this|that)\s+(?:document|paper|report|article|newspaper|file|study)\b",
    re.IGNORECASE,
)
REFUSAL_RE = re.compile(
    r"\b(?:not mentioned|not found|insufficient information|not fully answerable|"
    r"not answerable|cannot be answered|does not contain|does not have|not provided|"
    r"not available from (?:the )?documents?)\b",
    re.IGNORECASE,
)
EXTERNAL_SCOPE_RE = re.compile(
    r"\b(?:currently|latest|today|recently|on the web|google scholar|external source|"
    r"current production|as of)\b",
    re.IGNORECASE,
)
FORMAT_SENSITIVE_RE = re.compile(
    r"\b(?:exact|calculate|difference|percentage|percent|ratio|how many|how much|"
    r"what date|on what date|when|first|last|highest|lowest|total|list|rank)\b",
    re.IGNORECASE,
)

# Words that describe the QA template or a document-local operation.  They do
# not count as a document identity anchor.  The list is deliberately narrow:
# named entities and technical terms remain eligible as anchors.
GENERIC_QUERY_WORDS = frozenset(
    """
    a an and are as at be been brief by can could did do does doing for from had has have
    he her how in into is it its last many may mention mentioned mentions more most of on
    or other overall page pages paper paragraph report section sections should study that
    the their there these this those to total was were what when where which who why with
    would document documents figure figures table tables file files article articles
    newspaper newspapers chapter chapters summary summarize summarise title titles word words
    number numbers common abbreviation abbreviations revised revise released release cover
    covered mainly major message purpose content main first second third all any another
    according based context brief exactly exact stated statedly
    """.split()
)

SEVERITY_RANK = {"PASS": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
DISPOSITION_BY_SEVERITY = {
    "HIGH": "EXCLUDE_GLOBAL_RAG",
    "MEDIUM": "REVIEW_BEFORE_EVAL",
    "LOW": "RETAIN_PENDING_HUMAN_CHECK",
    "PASS": "RETAIN_PENDING_HUMAN_CHECK",
}

RISK_TAG_LABELS = {
    "missing_document_identity": "缺少文档定位",
    "duplicate_query_across_documents": "跨文档重复问题",
    "missing_gold_document_reference": "缺少 gold 文档",
    "gold_answer_repaired_from_evidence": "gold 答案由 evidence 修补",
    "mllm_or_text_projection_unsafe": "文本投影不安全",
    "missing_answerable_evidence": "缺少可回答证据",
    "locator_only_evidence": "仅有定位证据",
    "scope_contract_warning": "scope 契约需确认",
    "answerability_label_inconsistency": "answerable 标签矛盾",
    "external_or_temporal_scope": "外部或时间范围风险",
    "answer_format_sensitive": "答案格式敏感",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(unicodedata.normalize("NFKC", str(value))).strip()


def tokens(value: Any) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text_value(value))]


def normalize_question(value: Any) -> str:
    return QUESTION_NORMALIZE_RE.sub(" ", text_value(value).casefold()).strip()


def preview(value: Any, limit: int = 320) -> str:
    value_text = re.sub(r"\s+", " ", text_value(value))
    if len(value_text) <= limit:
        return value_text
    return value_text[: limit - 1].rstrip() + "…"


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.casefold() in {"true", "yes", "1", "y"}
    return bool(value)


def nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def package_scope(manifest: Mapping[str, Any]) -> str:
    evaluation = manifest.get("evaluation")
    if isinstance(evaluation, Mapping) and evaluation.get("scope"):
        return text_value(evaluation["scope"])
    for key in ("scope", "evaluation_scope"):
        if manifest.get(key):
            return text_value(manifest[key])
    return "unknown"


def evidence_profile(evidence: Any) -> dict[str, Any]:
    """Classify evidence without treating source-ID namespaces as errors."""

    if not isinstance(evidence, list):
        evidence = [] if evidence in (None, "") else [evidence]
    text_items: list[str] = []
    locator_items: list[dict[str, Any]] = []
    meaningful_items = 0
    for item in evidence:
        if isinstance(item, str):
            if item.strip():
                meaningful_items += 1
                text_items.append(preview(item))
            continue
        if not isinstance(item, Mapping):
            if nonempty(item):
                meaningful_items += 1
            continue
        item_text = ""
        for key in ("evidence", "text", "quote", "content", "snippet", "passage"):
            if nonempty(item.get(key)):
                item_text = text_value(item[key])
                break
        locator = item.get("locator")
        has_locator = nonempty(item.get("doc_id")) or nonempty(locator)
        if item_text:
            meaningful_items += 1
            text_items.append(preview(item_text))
        elif has_locator:
            meaningful_items += 1
            locator_items.append(
                {
                    "doc_id": item.get("doc_id"),
                    "locator": locator,
                }
            )
        elif any(nonempty(item.get(key)) for key in ("status", "source", "id")):
            meaningful_items += 1
    if not meaningful_items:
        state = "empty"
    elif text_items:
        state = "text"
    else:
        state = "locator_only"
    return {
        "state": state,
        "entry_count": len(evidence),
        "meaningful_entry_count": meaningful_items,
        "text_entry_count": len(text_items),
        "locator_entry_count": len(locator_items),
        "text_previews": text_items[:3],
        "locators": locator_items[:8],
    }


def metric_guess(row: Mapping[str, Any]) -> str:
    question_type = text_value(row.get("question_type")).casefold()
    question = text_value(row.get("question"))
    if not truthy(row.get("answerable")) or "unanswer" in question_type or "null" in question_type:
        return "refusal"
    if len(row.get("gold_doc_ids") or []) > 1 or text_value(row.get("source_dataset")) == "multihop":
        return "multi_hop_reasoning"
    if "retrieval" in question_type:
        return "retrieval"
    if "multimodal" in question_type:
        return "text_projection_qa"
    if "meta" in question_type:
        return "document_metadata_qa"
    if FORMAT_SENSITIVE_RE.search(question):
        return "answer_exactness"
    return "question_answering"


def make_group_id(normalized_question: str) -> str:
    digest = hashlib.sha1(normalized_question.encode("utf-8")).hexdigest()[:12]
    return f"duplicate_{digest}"


def build_document_index(package: Path, corpus_rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    documents: dict[str, dict[str, Any]] = {}
    document_frequency: Counter[str] = Counter()
    for row in corpus_rows:
        doc_id = text_value(row.get("doc_id"))
        if not doc_id:
            continue
        text_path = package / text_value(row.get("text_path"))
        document_text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else ""
        token_set = set(tokens(f"{row.get('title', '')} {document_text}"))
        document_frequency.update(token_set)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        documents[doc_id] = {
            "doc_id": doc_id,
            "title": text_value(row.get("title")),
            "text_path": text_value(row.get("text_path")),
            "metadata": dict(metadata),
            "source_document_id": text_value(metadata.get("source_document_id")),
            "token_count": len(token_set),
        }
    return documents, document_frequency


def anchor_terms(question: str, document_frequency: Counter[str], document_count: int) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in tokens(question):
        if token in seen or token in GENERIC_QUERY_WORDS or len(token) < 3 or token.isdigit():
            continue
        seen.add(token)
        frequency = int(document_frequency.get(token, 0))
        # A query term absent from the copied corpus is not a safe identity
        # anchor: it may be a typo or an answer-only token.
        if 0 < frequency <= 8:
            values.append({"term": token, "document_frequency": frequency})
    return values


def source_doc_ids_for_gold(doc_ids: Iterable[str], documents: Mapping[str, Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for doc_id in doc_ids:
        source_id = text_value(documents.get(doc_id, {}).get("source_document_id"))
        if source_id:
            values.append(source_id)
    return sorted(set(values))


def add_finding(
    findings: list[dict[str, Any]],
    code: str,
    severity: str,
    message: str,
    action: str,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    findings.append(
        {
            "risk_type": code,
            "severity": severity,
            "message": message,
            "recommended_action": action,
            "evidence": dict(evidence or {}),
        }
    )


def audit_row(
    row: Mapping[str, Any],
    gold_row: Mapping[str, Any],
    manifest: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    document_frequency: Counter[str],
    duplicate_groups: Mapping[str, list[Mapping[str, Any]]],
    mllm_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    question_id = text_value(row.get("question_id"))
    question = text_value(row.get("question"))
    source_dataset = text_value(row.get("source_dataset"))
    question_type = text_value(row.get("question_type"))
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    source_gold = gold_row.get("source_gold") if isinstance(gold_row.get("source_gold"), Mapping) else {}
    gold_doc_ids = [text_value(value) for value in (row.get("gold_doc_ids") or gold_row.get("gold_doc_ids") or []) if text_value(value)]
    gold_evidence = row.get("gold_evidence") if "gold_evidence" in row else gold_row.get("gold_evidence")
    answerable = truthy(row.get("answerable"))
    evidence = evidence_profile(gold_evidence)
    source_reference = text_value(source_gold.get("reference_answer"))
    reference_answer = text_value(row.get("reference_answer") or gold_row.get("reference_answer"))
    package_scope_value = package_scope(manifest)
    row_scope = text_value(metadata.get("scope_policy"))
    scope_doc_ids = metadata.get("scope_doc_ids")
    if not isinstance(scope_doc_ids, list):
        scope_doc_ids = []
    anchors = anchor_terms(question, document_frequency, len(documents))
    normalized = normalize_question(question)
    duplicate_rows = duplicate_groups.get(normalized, [])
    duplicate_doc_ids = sorted({doc_id for item in duplicate_rows for doc_id in (item.get("gold_doc_ids") or []) if doc_id})
    duplicate_group_id = make_group_id(normalized) if len(duplicate_rows) > 1 and len(duplicate_doc_ids) > 1 else None
    mllm = mllm_rows.get(question_id, {})
    mllm_status = text_value(mllm.get("status"))
    mllm_unsafe = mllm_status in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE"} or truthy(mllm.get("mllm_required")) and not truthy(mllm.get("text_only_ready"))

    findings: list[dict[str, Any]] = []
    if duplicate_group_id:
        add_finding(
            findings,
            "duplicate_query_across_documents",
            "HIGH",
            "The normalized question is reused for multiple gold documents in the global package, so the query alone does not select a unique target.",
            "Add an explicit document/entity identifier, split the evaluation by document scope, or remove the duplicate.",
            {
                "duplicate_group_id": duplicate_group_id,
                "group_size": len(duplicate_rows),
                "gold_doc_ids": duplicate_doc_ids,
                "question_ids": sorted(text_value(item.get("question_id")) for item in duplicate_rows),
            },
        )

    if answerable and not gold_doc_ids:
        add_finding(
            findings,
            "missing_gold_document_reference",
            "HIGH",
            "The row is marked answerable but has no gold document, so retrieval and evidence attribution cannot be evaluated against the copied corpus.",
            "Attach the supporting document(s), or relabel this row as an explicit refusal/not-found case with a separate refusal contract.",
            {
                "answerable": answerable,
                "question_type": question_type,
                "selection_bucket": metadata.get("selection_bucket"),
                "gold_evidence_state": evidence["state"],
            },
        )
        if REFUSAL_RE.search(reference_answer) or REFUSAL_RE.search(text_value(row.get("answer"))):
            add_finding(
                findings,
                "answerability_label_inconsistency",
                "HIGH",
                "The row is labeled answerable while its reference answer explicitly says the requested information is unavailable.",
                "Relabel it as refusal/unanswerable, or provide a positive evidence-bearing answer and gold document.",
                {
                    "answerable": answerable,
                    "answer_preview": preview(row.get("answer")),
                    "reference_preview": preview(reference_answer),
                },
            )

    if source_gold and not source_reference and reference_answer:
        add_finding(
            findings,
            "gold_answer_repaired_from_evidence",
            "HIGH",
            "The source gold reference answer is empty, while the derived package contains a reference answer; this answer is not independently authoritative.",
            "Obtain a source reference answer or exclude the row from high-confidence answer scoring.",
            {
                "source_reference_answer": "",
                "derived_reference_preview": preview(reference_answer),
                "fallback": metadata.get("reference_answer_fallback"),
                "source_gold_evidence_state": evidence_profile(source_gold.get("gold_evidence")).get("state"),
            },
        )

    if mllm_unsafe:
        add_finding(
            findings,
            "mllm_or_text_projection_unsafe",
            "HIGH",
            "The existing semantic MLLM audit marks this row as requiring vision or as unsafe for text-only projection.",
            "Keep the row out of the text-only condition; do not silently score it as text-only.",
            {
                "mllm_status": mllm_status,
                "mllm_required": mllm.get("mllm_required"),
                "text_only_ready": mllm.get("text_only_ready"),
                "audit_reason": (mllm.get("decision") or {}).get("reason") if isinstance(mllm.get("decision"), Mapping) else None,
            },
        )

    is_document_local_global = row_scope == "document-local" and package_scope_value.casefold() == "global"
    if is_document_local_global:
        add_finding(
            findings,
            "scope_contract_warning",
            "LOW",
            "Row metadata declares document-local scope while the eval package is global; the row has no explicit scope list.",
            "Prefer a document-local evaluation subset or add an explicit document identity/scope contract.",
            {
                "row_scope_policy": row_scope,
                "package_scope": package_scope_value,
                "scope_doc_ids": scope_doc_ids,
            },
        )
        if not anchors and GENERIC_SCOPE_RE.search(question):
            add_finding(
                findings,
                "missing_document_identity",
                "HIGH",
                "The question uses a document-local structural/anaphoric reference but supplies no discriminative document or entity anchor for a global corpus.",
                "Add the document title/source identifier to the question, or run the row in a package containing only its gold document.",
                {
                    "generic_scope_cues": sorted(set(match.casefold() for match in GENERIC_SCOPE_RE.findall(question))),
                    "anaphoric_document_reference": bool(ANAPHOR_RE.search(question)),
                    "anchor_terms": anchors,
                    "gold_doc_ids": gold_doc_ids,
                    "gold_titles": [documents.get(doc_id, {}).get("title") for doc_id in gold_doc_ids],
                },
            )

    if answerable and evidence["state"] == "empty":
        add_finding(
            findings,
            "missing_answerable_evidence",
            "MEDIUM",
            "The row is answerable but its gold evidence is empty or structurally blank, which prevents reliable answer-grounding review.",
            "Add a text span or a resolvable locator; if the dataset contract is document-only, keep the row in a review slice.",
            {
                "gold_doc_ids": gold_doc_ids,
                "evidence_profile": evidence,
            },
        )
    elif answerable and evidence["state"] == "locator_only":
        add_finding(
            findings,
            "locator_only_evidence",
            "MEDIUM",
            "Gold evidence contains only document/page/locator identifiers and no text-bearing span.",
            "Resolve the locator during human review or add a text evidence span; do not treat locator absence as answer falsity.",
            {"gold_doc_ids": gold_doc_ids, "evidence_profile": evidence},
        )

    if EXTERNAL_SCOPE_RE.search(question) and answerable:
        add_finding(
            findings,
            "external_or_temporal_scope",
            "LOW",
            "The question contains current/external-knowledge wording that may require a time or source snapshot beyond the package.",
            "Record the corpus snapshot/date in the metric contract or move the row to an explicitly time-bounded slice.",
            {"matched_question": preview(question)},
        )

    if FORMAT_SENSITIVE_RE.search(question):
        add_finding(
            findings,
            "answer_format_sensitive",
            "LOW",
            "The question asks for an exact count/date/calculation/order/list, so scoring normalization must be explicit.",
            "Specify accepted units, rounding, date format, and list-order policy in the evaluator.",
            {"question": preview(question)},
        )

    if not findings:
        risk_level = "PASS"
    else:
        risk_level = max((item["severity"] for item in findings), key=lambda value: SEVERITY_RANK[value])
    disposition = DISPOSITION_BY_SEVERITY[risk_level]
    global_rag_ready: bool | str
    if risk_level == "HIGH":
        global_rag_ready = False
    elif risk_level == "MEDIUM":
        global_rag_ready = "conditional"
    else:
        global_rag_ready = True

    if risk_level == "HIGH":
        qa_tags = [
            {"code": "problematic", "label": "存在问题"},
            {"code": "exclude_global_rag", "label": "暂不进入 global RAG"},
        ]
    elif risk_level == "MEDIUM":
        qa_tags = [
            {"code": "problematic", "label": "存在问题"},
            {"code": "review_before_eval", "label": "评估前人工复核"},
        ]
    elif risk_level == "LOW":
        qa_tags = [{"code": "caution", "label": "注意项"}]
    else:
        qa_tags = []
    qa_tags.extend(
        {"code": code, "label": RISK_TAG_LABELS.get(code, code)}
        for code in [item["risk_type"] for item in findings]
    )

    doc_rows = [documents.get(doc_id, {}) for doc_id in gold_doc_ids]
    result = {
        "question_id": question_id,
        "source_question_id": text_value(row.get("source_question_id")),
        "source_dataset": source_dataset,
        "question_type": question_type,
        "metric_guess": metric_guess(row),
        "question": question,
        "answerable": answerable,
        "answer_preview": preview(row.get("answer")),
        "reference_answer_preview": preview(reference_answer),
        "gold_doc_ids": gold_doc_ids,
        "gold_source_document_ids": source_doc_ids_for_gold(gold_doc_ids, documents),
        "gold_titles": [text_value(item.get("title")) for item in doc_rows if text_value(item.get("title"))],
        "gold_evidence_profile": evidence,
        "scope": {
            "package_scope": package_scope_value,
            "row_scope_policy": row_scope or None,
            "scope_doc_ids": scope_doc_ids,
        },
        "query_signals": {
            "normalized_question": normalized,
            "anchor_terms": anchors,
            "generic_scope_cue": bool(GENERIC_SCOPE_RE.search(question)),
            "anaphoric_document_reference": bool(ANAPHOR_RE.search(question)),
            "external_or_temporal_cue": bool(EXTERNAL_SCOPE_RE.search(question)),
            "format_sensitive": bool(FORMAT_SENSITIVE_RE.search(question)),
        },
        "mllm_audit": {
            "status": mllm_status or None,
            "mllm_required": mllm.get("mllm_required"),
            "text_only_ready": mllm.get("text_only_ready"),
        },
        "risk_level": risk_level,
        "disposition": disposition,
        "global_rag_ready": global_rag_ready,
        "qa_tags": qa_tags,
        "risk_types": [item["risk_type"] for item in findings],
        "findings": findings,
    }
    if duplicate_group_id:
        result["duplicate_group_id"] = duplicate_group_id
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--question-id", default=None, help="Audit only one question row.")
    parser.add_argument("--mllm-audit", type=Path, default=DEFAULT_MLLM_AUDIT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package = args.package.resolve()
    manifest = read_json(package / "manifest.json")
    question_rows = read_jsonl(package / "questions.jsonl")
    gold_rows = read_jsonl(package / "gold.jsonl")
    corpus_rows = read_jsonl(package / "corpus.jsonl")
    gold_by_id = {text_value(row.get("question_id")): row for row in gold_rows}
    documents, document_frequency = build_document_index(package, corpus_rows)
    mllm_rows = {}
    if args.mllm_audit and args.mllm_audit.exists():
        mllm_rows = {text_value(row.get("question_id")): row for row in read_jsonl(args.mllm_audit)}

    duplicate_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in question_rows:
        duplicate_groups[normalize_question(row.get("question"))].append(row)

    selected_rows = question_rows
    if args.question_id:
        selected_rows = [row for row in question_rows if text_value(row.get("question_id")) == args.question_id]
        if not selected_rows:
            raise SystemExit(f"question_id not found: {args.question_id}")

    results = [
        audit_row(
            row,
            gold_by_id.get(text_value(row.get("question_id")), {}),
            manifest,
            documents,
            document_frequency,
            duplicate_groups,
            mllm_rows,
        )
        for row in selected_rows
    ]
    results.sort(key=lambda row: text_value(row.get("question_id")))

    output = args.output
    if output.suffix.casefold() != ".jsonl":
        output = output / ("single-case.jsonl" if args.question_id else "qa-quality-audit.jsonl")
    write_jsonl(output, results)

    high_rows = [row for row in results if row["risk_level"] == "HIGH"]
    review_rows = [row for row in results if row["risk_level"] == "MEDIUM"]
    if not args.question_id:
        write_jsonl(output.with_name("high-risk-qa.jsonl"), high_rows)
        write_jsonl(output.with_name("review-qa.jsonl"), review_rows)

    risk_type_counts: Counter[str] = Counter()
    for result in results:
        risk_type_counts.update(result["risk_types"])
    decision_counts = Counter(result["disposition"] for result in results)
    severity_counts = Counter(result["risk_level"] for result in results)
    dataset_summary: dict[str, dict[str, Any]] = {}
    for dataset in sorted({text_value(row.get("source_dataset")) for row in results}):
        dataset_rows = [row for row in results if row["source_dataset"] == dataset]
        dataset_summary[dataset] = {
            "rows": len(dataset_rows),
            "risk_level_counts": dict(sorted(Counter(row["risk_level"] for row in dataset_rows).items())),
            "disposition_counts": dict(sorted(Counter(row["disposition"] for row in dataset_rows).items())),
            "risk_type_counts": dict(sorted(Counter(code for row in dataset_rows for code in row["risk_types"]).items())),
        }

    all_duplicate_groups = {
        key: value
        for key, value in duplicate_groups.items()
        if len(value) > 1
        and len({doc_id for item in value for doc_id in (item.get("gold_doc_ids") or []) if doc_id}) > 1
    }
    document_local_rows = [row for row in question_rows if text_value((row.get("metadata") or {}).get("scope_policy")) == "document-local"]
    summary = {
        "audit": "moi-rag-qa-quality-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": str(package),
        "package_scope": package_scope(manifest),
        "question_id_filter": args.question_id,
        "input_counts": {
            "questions": len(question_rows),
            "gold": len(gold_rows),
            "corpus_documents": len(corpus_rows),
            "mllm_audit_rows": len(mllm_rows),
            "audited_rows": len(results),
        },
        "risk_level_counts": dict(sorted(severity_counts.items())),
        "disposition_counts": dict(sorted(decision_counts.items())),
        "risk_type_counts": dict(sorted(risk_type_counts.items())),
        "by_dataset": dataset_summary,
        "structural_signals": {
            "document_local_rows": len(document_local_rows),
            "document_local_rows_with_empty_scope_doc_ids": sum(
                not isinstance((row.get("metadata") or {}).get("scope_doc_ids"), list)
                or not (row.get("metadata") or {}).get("scope_doc_ids")
                for row in document_local_rows
            ),
            "duplicate_groups_across_gold_documents": len(all_duplicate_groups),
            "duplicate_rows_across_gold_documents": sum(len(value) for value in all_duplicate_groups.values()),
            "answerable_rows_without_gold_document": sum(
                truthy(row.get("answerable")) and not row.get("gold_doc_ids") for row in question_rows
            ),
        },
        "method": {
            "scope_identity_rule": "document-local + global package + generic structural/anaphoric cue + no corpus-discriminative anchor => HIGH missing_document_identity",
            "duplicate_rule": "normalized exact question attached to multiple gold documents => HIGH duplicate_query_across_documents",
            "evidence_rule": "empty answerable evidence => MEDIUM; locator-only evidence => MEDIUM; refusal rows are not penalized for expected empty evidence",
            "mllm_rule": "consume existing audit only; do not perform new visual classification",
            "not_a_final_human_adjudication": True,
        },
        "high_risk_question_ids": [row["question_id"] for row in high_rows],
        "review_question_ids": [row["question_id"] for row in review_rows],
    }
    summary_path = args.summary or output.with_name("summary.json")
    write_json(summary_path, summary)

    print(
        json.dumps(
            {
                "status": "OK",
                "audited_rows": len(results),
                "risk_level_counts": dict(sorted(severity_counts.items())),
                "high_risk": len(high_rows),
                "review": len(review_rows),
                "output": str(output),
                "summary": str(summary_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
