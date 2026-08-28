#!/usr/bin/env python3
"""Audit semantic MLLM requirements for the MOI text-only condition.

The audit is deliberately conservative.  A missing image field is not treated
as proof that a question is text-safe: linked Markdown, answer-bearing table
cells, captions, and derivable numeric operands are checked as well.  The
small manual decision table records the cases that require semantic review;
the remaining multimodal-t rows are accepted only when their tabular/text
projection is recoverable from the copied Markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import datetime, timezone
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGE = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"
DEFAULT_OUTPUT = ROOT / "runs" / "readiness" / "mllm-semantic-audit-20260820" / "audit.jsonl"
DEFAULT_SUMMARY = ROOT / "runs" / "readiness" / "mllm-semantic-audit-20260820" / "summary.json"
DEFAULT_SIGNOFF = ROOT / "runs" / "readiness" / "mllm-semantic-audit-20260820" / "text-only-semantic-audit-signoff.json"

MEDIA_KEYS = {
    "binary_path",
    "image",
    "image_path",
    "image_paths",
    "images",
    "layout_mapping",
    "media",
    "modality",
    "requires_mllm",
    "requires_vision",
    "source_binary_path",
}
VISUAL_WORD_RE = re.compile(
    r"\b(?:color|colour|chart|figure|fig\.?|plot|diagram|map|image|photo|picture|visual|graph|bar|pie|legend|axis|axes|spatial|layout|cluster|clusters|trend|highlighted)\b",
    re.IGNORECASE,
)
CALCULATION_RE = re.compile(
    r"\b(?:calculate|difference|change|increase|decrease|percentage|average|ratio|combined|total|how much more|how much larger|sum|subtract|subtracting|improve|improvement)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z])")
TOKEN_RE = re.compile(r"[a-z0-9%]+")


# These rows were found by reviewing the linked Markdown and the original
# evidence.  They are intentionally explicit so a future rerun cannot turn a
# visual-only question into a text-only pass merely because a number happens
# to occur elsewhere in a long annual report.
MANUAL_DECISIONS: dict[str, dict[str, Any]] = {
    "moi500d_qa_docbench_0ac6b215da1c": {
        "status": "MLLM_REQUIRED",
        "support_kind": "visual_pie_chart_only",
        "confidence": "high",
        "reason": "The answer is the largest pie-chart segment and its 61% value; the linked Markdown has no answer-bearing textual statement for that segment.",
    },
    "moi500d_qa_docbench_815d28d96b1d": {
        "status": "MLLM_REQUIRED",
        "support_kind": "visual_cluster_comparison_only",
        "confidence": "high",
        "reason": "The Markdown says that styles are separated and that VAE is smoother, but does not state that DAE has clearer separation; the DAE-versus-VAE comparison is only in the t-SNE plot.",
    },
    "moi500d_qa_docbench_e579b4406d5c": {
        "status": "TEXT_PROJECTION_UNSAFE",
        "support_kind": "parsed_numeric_sign_gap",
        "confidence": "high",
        "reason": "The parsed Markdown preserves 0.12 and 1.06 but loses the minus sign on the informal score, so the exact gold comparison cannot be reproduced from the eval ingest text.",
    },
    "moi500d_qa_docbench_a6a5e07f8217": {
        "status": "TEXT_PROJECTION_UNSAFE",
        "support_kind": "table_value_mismatch",
        "confidence": "high",
        "reason": "The gold answer is 25,523, which is absent from the linked Markdown; the visible text table contains 22,902 and 6,295 instead, so the answer-bearing table projection is not trustworthy.",
    },
    "moi500d_qa_docbench_1862dced7e7c": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_and_opening_text",
        "confidence": "high",
        "reason": "The opening method description and Figure 1 caption explicitly describe contextual replacement with a bidirectional language model.",
    },
    "moi500d_qa_docbench_306529cfeae0": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_text",
        "confidence": "high",
        "reason": "The Markdown Figure 2 caption explicitly states clustering by supersense part of speech.",
    },
    "moi500d_qa_docbench_38d516182d4d": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "section_text",
        "confidence": "high",
        "reason": "Section 3.2.3 explicitly defines LSTUR-ini and LSTUR-con and how each combines long- and short-term representations.",
    },
    "moi500d_qa_docbench_3b363cf476cd": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_and_equation_text",
        "confidence": "high",
        "reason": "The Figure 2 caption and following equation text explicitly state that concatenated logits pass through softmax.",
    },
    "moi500d_qa_docbench_4b5ea6211ac8": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "architecture_section_text",
        "confidence": "high",
        "reason": "The token-representation section states that a bidirectional LSTM over input tokens produces the token representations.",
    },
    "moi500d_qa_docbench_5e21ff9a51e5": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "serialized_table",
        "confidence": "high",
        "reason": "The executive-officer table and surrounding prose explicitly identify Steven M. Rales as Chairman of the Board.",
    },
    "moi500d_qa_docbench_7fe81d31ccc2": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "architecture_context_text",
        "confidence": "medium",
        "reason": "The paper text describes the character sequence RNN/NER path and the bidirectional LSTM forward/backward representation; the answer does not depend on pixel layout.",
    },
    "moi500d_qa_docbench_83726e542dc6": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "serialized_table",
        "confidence": "high",
        "reason": "The Markdown contains the customer-accounts-by-country table, including the Asia rows and Hong Kong value 531,489.",
    },
    "moi500d_qa_docbench_92ee46d168b0": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_and_section_text",
        "confidence": "high",
        "reason": "The Figure 1 caption and Section 3 explicitly describe SenseBERT's S mapping in parallel with BERT's W mapping.",
    },
    "moi500d_qa_docbench_975aa9d4d864": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_and_section_text",
        "confidence": "high",
        "reason": "The Figure 2 caption and Section 2.4 explicitly describe independent encoding, pooling, and triplet-margin loss.",
    },
    "moi500d_qa_docbench_9806d5f15cbd": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "section_equation_text",
        "confidence": "high",
        "reason": "Section 6.2 explicitly defines specificity using normalized inverse document frequency/NIDF.",
    },
    "moi500d_qa_docbench_9ed3a66ccdba": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "architecture_explanation_text",
        "confidence": "medium",
        "reason": "The surrounding text explains word-level semantic representations and character-level morphology/OOV handling; no image content is needed.",
    },
    "moi500d_qa_docbench_be3f415abf7a": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "result_narrative_text",
        "confidence": "high",
        "reason": "The results narrative explicitly states that coreference propagation is best at the second iteration (N = 2).",
    },
    "moi500d_qa_docbench_be40003f34c3": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "serialized_table_and_narrative",
        "confidence": "high",
        "reason": "The Markdown table contains Arabic Where = +2.6 and the surrounding narrative explains the Figure 3 interpretation.",
    },
    "moi500d_qa_docbench_c5050d5dd039": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "section_text",
        "confidence": "high",
        "reason": "Section 3.1 explicitly names the title encoder and topic encoder as the two news-encoder submodules.",
    },
    "moi500d_qa_docbench_dc3caeb42204": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "result_narrative_text",
        "confidence": "high",
        "reason": "Section 3.5 explicitly states that decoding saturates at batch size 100 while training speed keeps growing.",
    },
    "moi500d_qa_docbench_dffd8e0addeb": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_text",
        "confidence": "high",
        "reason": "The Figure 2 caption explicitly states that the paragraph with the lowest y_empty score is selected.",
    },
    "moi500d_qa_docbench_e3fa923f4193": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "figure_caption_and_section_text",
        "confidence": "high",
        "reason": "The Figure 2 caption and Section 3.2 describe combining representations for downstream classification.",
    },
    "moi500d_qa_docbench_f075885d734f": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "architecture_equation_text",
        "confidence": "high",
        "reason": "The DS-DST text explicitly defines cosine similarity between candidate-value and context representations.",
    },
    "moi500d_qa_docbench_fae687a299f7": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "section_text_and_jit_definition",
        "confidence": "medium",
        "reason": "The Toyota Markdown states that the fleet system operates e-Palettes in a just-in-time fashion and defines the JIT objective in text.",
    },
    "moi500d_qa_docbench_2cce6c2ecbf4": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "derivable_from_serialized_table",
        "confidence": "high",
        "reason": "The linked table retains the SE counts 95 and 272; the gold 34.93% is a deterministic percentage calculation.",
    },
    "moi500d_qa_docbench_5b13c4d1c3da": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "derivable_from_serialized_table",
        "confidence": "high",
        "reason": "The linked table retains the EN-DA and EN-RO counts; the gold difference is a deterministic subtraction.",
    },
    "moi500d_qa_docbench_5cb0a7cad53d": {
        "status": "TEXT_PROJECTION_SAFE",
        "support_kind": "derivable_from_serialized_table",
        "confidence": "high",
        "reason": "The linked cross-language score table retains the German-row operands; the gold 56.43 is their arithmetic mean.",
    },
}


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


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(value: Any) -> str:
    text = html.unescape(str(value or "")).lower()
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    return text


def tokens(value: Any) -> set[str]:
    return set(TOKEN_RE.findall(normalized_text(value)))


def number_value(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "").replace("%", ""))
    except InvalidOperation:
        return None


def numbers(value: Any) -> list[Decimal]:
    result: list[Decimal] = []
    for match in NUMBER_RE.findall(normalized_text(value)):
        parsed = number_value(match)
        if parsed is not None:
            result.append(parsed)
    return result


def non_empty_media_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            if name.casefold() in MEDIA_KEYS and item not in (None, "", [], {}):
                found.append(path)
            found.extend(non_empty_media_keys(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(non_empty_media_keys(item, f"{prefix}[{index}]"))
    return found


def answer_text(row: Mapping[str, Any]) -> str:
    return str(row.get("reference_answer") or row.get("answer") or "").strip()


def evidence_text(row: Mapping[str, Any]) -> str:
    values: list[str] = []
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        values.append(str(metadata.get("original_evidence") or ""))
    evidence = row.get("gold_evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, Mapping):
                values.append(str(item.get("evidence") or ""))
            else:
                values.append(str(item))
    elif evidence:
        values.append(str(evidence))
    return "\n".join(value for value in values if value)


def linked_document_text(row: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]], package: Path) -> tuple[str, list[str]]:
    values = row.get("gold_doc_ids") or row.get("document_ids") or []
    if isinstance(values, str):
        values = [values]
    texts: list[str] = []
    paths: list[str] = []
    for value in values:
        doc_id = str(value)
        document = documents.get(doc_id)
        if not document:
            continue
        relative = str(document.get("text_path") or "")
        path = package / relative
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8"))
            paths.append(relative)
    return "\n\n".join(texts), paths


def textual_support(row: Mapping[str, Any], document_text: str, status: str) -> dict[str, Any]:
    answer = answer_text(row)
    question = str(row.get("question") or "")
    evidence = evidence_text(row)
    linked = normalized_text(document_text)
    answer_tokens = tokens(answer)
    linked_tokens = tokens(document_text)
    matched_tokens = sorted(answer_tokens & linked_tokens)
    missing_tokens = sorted(answer_tokens - linked_tokens)
    answer_numbers = numbers(answer)
    linked_numbers = set(numbers(document_text))
    missing_numbers = [str(value) for value in answer_numbers if value not in linked_numbers]
    cues = bool(CALCULATION_RE.search(question) or CALCULATION_RE.search(evidence))
    has_table = "<table" in linked or "|---" in linked
    visual_markers = sorted(set(VISUAL_WORD_RE.findall(question + "\n" + evidence)))
    if status == "TEXT_PROJECTION_SAFE":
        support_class = "manual_text_projection"
    elif status == "MLLM_REQUIRED":
        support_class = "visual_semantics_not_serialized"
    elif status == "TEXT_PROJECTION_UNSAFE":
        support_class = "text_projection_integrity_gap"
    elif has_table and not missing_numbers:
        support_class = "serialized_table_or_text"
    elif has_table and cues and len(answer_numbers) > len(missing_numbers):
        support_class = "derivable_from_serialized_table"
    else:
        support_class = "unproven"
    return {
        "support_class": support_class,
        "answer_token_ratio": round(len(matched_tokens) / max(1, len(answer_tokens)), 4),
        "answer_token_count": len(answer_tokens),
        "matched_answer_tokens": matched_tokens,
        "missing_answer_tokens": missing_tokens,
        "answer_numeric_values": [str(value) for value in answer_numbers],
        "missing_answer_numeric_values": missing_numbers,
        "calculation_or_derivation_cue": cues,
        "serialized_table_present": has_table,
        "visual_language_markers": visual_markers,
    }


def classify(row: Mapping[str, Any], document_text: str) -> tuple[str, dict[str, Any]]:
    question_id = str(row.get("question_id") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    original_type = str(metadata.get("original_type") or row.get("question_type") or "").casefold()
    manual = MANUAL_DECISIONS.get(question_id)
    if manual:
        return str(manual["status"]), dict(manual)
    if not original_type.startswith("multimodal"):
        return "NOT_APPLICABLE", {"confidence": "high", "reason": "The row is not marked multimodal in the source condition."}
    if original_type == "multimodal-f":
        return "REVIEW_REQUIRED", {
            "confidence": "low",
            "reason": "A figure/diagram question has no explicit reviewed text projection decision.",
        }
    # Multimodal-t is accepted only when the tabular projection itself is
    # present.  A derived numeric answer is safe when at least one source
    # operand remains in the text; a direct table value must be present.
    support = textual_support(row, document_text, "")
    if support["serialized_table_present"] and not support["missing_answer_numeric_values"]:
        return "TEXT_PROJECTION_SAFE", {
            "confidence": "high",
            "reason": "The answer-bearing table/text values are present in the linked Markdown.",
        }
    if (
        support["serialized_table_present"]
        and support["calculation_or_derivation_cue"]
        and support["answer_numeric_values"]
        and len(support["answer_numeric_values"]) > len(support["missing_answer_numeric_values"])
    ):
        return "TEXT_PROJECTION_SAFE", {
            "confidence": "medium",
            "reason": "The result is derivable from numeric operands retained in the linked Markdown table/text.",
        }
    return "REVIEW_REQUIRED", {
        "confidence": "low",
        "reason": "The multimodal table projection does not provide enough answer-bearing text for an automatic pure-text pass.",
    }


def audit(package: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = read_json(package / "manifest.json")
    questions = read_jsonl(package / "questions.jsonl")
    documents = {str(row.get("doc_id")): row for row in read_jsonl(package / "corpus.jsonl")}
    gold_ids = {str(row.get("question_id")) for row in read_jsonl(package / "gold.jsonl")}
    rows: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("question_id") or "")
        document_text, document_paths = linked_document_text(question, documents, package)
        status, decision = classify(question, document_text)
        metadata = question.get("metadata") if isinstance(question.get("metadata"), Mapping) else {}
        media_fields = sorted(set(non_empty_media_keys(question) + non_empty_media_keys(metadata)))
        support = textual_support(question, document_text, status)
        row = {
            "question_id": question_id,
            "source_dataset": question.get("source_dataset"),
            "source_question_id": question.get("source_question_id"),
            "question_type": question.get("question_type"),
            "original_type": metadata.get("original_type") or question.get("question_type"),
            "question": question.get("question"),
            "reference_answer": answer_text(question),
            "gold_doc_ids": question.get("gold_doc_ids") or [],
            "document_paths": document_paths,
            "explicit_media_fields": media_fields,
            "text_only_projection_claim": metadata.get("text_only_projection"),
            "status": status,
            "mllm_required": status == "MLLM_REQUIRED",
            "text_only_ready": status == "TEXT_PROJECTION_SAFE",
            "decision": decision,
            "support": support,
            "gold_row_present": question_id in gold_ids,
        }
        rows.append(row)

    multimodal = [row for row in rows if str(row.get("original_type") or "").casefold().startswith("multimodal")]
    status_counts = Counter(str(row["status"]) for row in rows)
    type_counts = Counter(str(row.get("original_type") or "unknown") for row in multimodal)
    summary = {
        "schema": "moi-rag-bench-text-only-mllm-semantic-audit-v0.1",
        "package": str(package),
        "manifest_dataset_id": manifest.get("dataset_id"),
        "manifest_condition": manifest.get("condition"),
        "counts": {
            "questions": len(rows),
            "gold_rows": len(gold_ids),
            "multimodal_rows": len(multimodal),
            "explicit_media_input_rows": sum(bool(row["explicit_media_fields"]) for row in rows),
            "mllm_required_rows": sum(row["status"] == "MLLM_REQUIRED" for row in multimodal),
            "text_projection_unsafe_rows": sum(row["status"] == "TEXT_PROJECTION_UNSAFE" for row in multimodal),
            "review_required_rows": sum(row["status"] == "REVIEW_REQUIRED" for row in multimodal),
            "text_projection_safe_rows": sum(row["status"] == "TEXT_PROJECTION_SAFE" for row in multimodal),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "multimodal_type_counts": dict(sorted(type_counts.items())),
        "unsafe_question_ids": [
            row["question_id"]
            for row in multimodal
            if row["status"] in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE", "REVIEW_REQUIRED"}
        ],
        "manual_decision_ids_present": sorted(set(MANUAL_DECISIONS) & {row["question_id"] for row in rows}),
        "result": "PASS"
        if not any(row["status"] in {"MLLM_REQUIRED", "TEXT_PROJECTION_UNSAFE", "REVIEW_REQUIRED"} for row in multimodal)
        else "FAIL",
    }
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--signoff",
        type=Path,
        default=DEFAULT_SIGNOFF,
        help="Write the campaign-consumable semantic-audit signoff sidecar.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, summary = audit(args.package.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = args.package.resolve() / "manifest.json"
    manifest = read_json(manifest_path)
    counts = summary["counts"]
    signoff = {
        "schema": "moi-rag-bench-text-only-semantic-audit-v1",
        "status": "PASS" if summary["result"] == "PASS" else "FAIL",
        "audit_schema": summary["schema"],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "package": str(args.package.resolve()),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "dataset_id": manifest.get("dataset_id"),
        "condition": manifest.get("condition"),
        "audit_rows_path": str(args.output.resolve()),
        "audit_rows_sha256": sha256_file(args.output.resolve()),
        "audit_summary_path": str(args.summary.resolve()),
        "audit_summary_sha256": sha256_file(args.summary.resolve()),
        "counts": counts,
        "mllm_required": counts["mllm_required_rows"] > 0,
        "image_llm": manifest.get("image_llm", "NOT_APPLICABLE"),
        "explicit_media_input_rows": counts["explicit_media_input_rows"],
        "unsafe_question_ids": summary["unsafe_question_ids"],
        "decision": "pure_text_llm_only" if summary["result"] == "PASS" else "hold_for_semantic_review",
    }
    args.signoff.parent.mkdir(parents=True, exist_ok=True)
    args.signoff.write_text(json.dumps(signoff, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
