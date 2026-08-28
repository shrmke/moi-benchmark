"""Versioned metric names and the common aggregate-record shape.

The mixed evaluator imports this module instead of inventing a second record
format for source-routed results.  It intentionally contains no platform or
judge code: registry metadata is the contract, while the evaluator supplies
the observations and applicability decisions.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence


REGISTRY_VERSION = "moi-rag-mixed-metric-registry-v1"
METRIC_VERSION = "1.0"
MISSING_STRUCTURED_CLAIM_GOLD = "MISSING_STRUCTURED_CLAIM_GOLD"
STRUCTURED_GOLD_FIELDS = (
    "scored_reference_claims",
    "critical_claims",
    "evidence_sets",
)
STRUCTURED_GOLD_ALIASES: dict[str, tuple[str, ...]] = {
    "scored_reference_claims": (
        "scored_reference_claims",
        "reference_claims",
        "scored_claims",
        "claims",
    ),
    "critical_claims": (
        "critical_claims",
        "critical_required_claims",
        "required_claims",
    ),
    "evidence_sets": (
        "evidence_sets",
        "gold_evidence_sets",
        "claim_evidence_sets",
    ),
}

_SOURCE_ALIASES = {
    "wiki": "wikieval",
    "wiki-eval": "wikieval",
    "wikieval": "wikieval",
    "multi-hop": "multihop-rag",
    "multihop": "multihop-rag",
    "multihop-rag": "multihop-rag",
    "multihoprag": "multihop-rag",
    "enterprise": "enterprise-rag-bench",
    "enterprise-rag": "enterprise-rag-bench",
    "enterprise-rag-bench": "enterprise-rag-bench",
    "enterpriserag": "enterprise-rag-bench",
    "enterpriserag-bench": "enterprise-rag-bench",
    "doc-bench": "docbench",
    "doc_bench": "docbench",
    "docbench": "docbench",
    "mm-doc-ir": "mmdocir",
    "mm_doc_ir": "mmdocir",
    "mmdocir": "mmdocir",
    "mm-doc-rag": "mmdocrag",
    "mm_doc_rag": "mmdocrag",
    "mmdocrag": "mmdocrag",
    "moi-rag-bench-v0.1-text-only-no-mllm": "moi-rag-bench-v0.1-text-only-no-mllm",
    "moi-rag-bench-v0.1-ready-for-eval": "moi-rag-bench-v0.1-text-only-no-mllm",
    "moi-rag-bench-v0.1-mixed": "moi-rag-bench-v0.1-text-only-no-mllm",
}


def canonical_source_dataset(value: Any) -> str:
    """Return the stable source key used by both output partitions."""

    text = str(value or "unknown").strip().casefold().replace("_", "-").replace(" ", "-")
    if text in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[text]
    compact = re.sub(r"[^a-z0-9-]+", "", text)
    return _SOURCE_ALIASES.get(compact, compact or "unknown")


def _spec(
    layer: str,
    formula: str,
    *,
    unit: str = "rate",
    direction: str = "higher_is_better",
    applicability: str = "all_initial_attempts",
    na_reason: str = "UNSUPPORTED_NOT_APPLICABLE",
) -> dict[str, Any]:
    return {
        "metric_version": METRIC_VERSION,
        "layer": layer,
        "unit": unit,
        "direction": direction,
        "formula": formula,
        "applicability": applicability,
        "na_reason": na_reason,
    }


METRIC_REGISTRY: dict[str, dict[str, Any]] = {
    "validity": _spec("validity", "valid initial units / planned initial units"),
    "readiness": _spec("readiness", "searchable-ready required units / planned corpus units"),
    "initial_availability": _spec("reliability", "compliant terminal initial units / planned initial units"),
    "error_rate": _spec("reliability", "failed initial units / planned initial units", direction="lower_is_better"),
    "initial_error_rate": _spec("reliability", "failed initial units / planned initial units", direction="lower_is_better"),
    "request_success": _spec("reliability", "successful terminal requests / planned initial requests"),
    "repeat_consistency": _spec(
        "reliability",
        "repeat-stable outcomes / repeated question units",
        applicability="repeated_question_units",
        na_reason="REPEAT_NOT_PLANNED",
    ),
    "em": _spec("answer", "normalized exact matches / attempts with non-empty Gold"),
    "normalized_em": _spec("answer", "normalized exact matches / attempts with non-empty Gold"),
    "f1": _spec("answer", "token F1 summed over attempts with non-empty Gold / eligible attempts"),
    "token_f1": _spec("answer", "token F1 summed over attempts with non-empty Gold / eligible attempts"),
    "contains_gold": _spec("answer", "answers containing normalized Gold / attempts with non-empty Gold"),
    "answer_non_empty": _spec("answer", "non-empty terminal answers / planned initial attempts"),
    "adapted_em": _spec("answer", "adapted normalized exact-match diagnostic"),
    "adapted_f1": _spec("answer", "adapted token-F1 diagnostic"),
    "adapted_contains_gold": _spec("answer", "adapted contains-Gold diagnostic"),
    "strict_unanswerable_success": _spec("answer", "strict refusal successes / unanswerable attempts"),
    "false_refusal_rate": _spec("answer", "false refusals / answerable attempts", direction="lower_is_better"),
    "info_not_found_success_adapted": _spec(
        "answer",
        "adapted Enterprise info-not-found successes / Enterprise info-not-found attempts",
        applicability="enterprise_info_not_found",
    ),
    "response_claim_correctness": _spec(
        "diagnostic",
        "adapted response-claim correctness labels / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "reference_claim_recall_adapted": _spec(
        "diagnostic",
        "adapted reference-answer claim recall / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "critical_claim_coverage_adapted": _spec(
        "diagnostic",
        "adapted critical-claim coverage / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "gold_evidence_support_adapted": _spec(
        "diagnostic",
        "adapted Gold-evidence support / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "runtime_context_faithfulness": _spec(
        "diagnostic",
        "adapted runtime-context faithfulness / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "strict_unanswerable": _spec(
        "diagnostic",
        "adapted strict-unanswerable judge score / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "false_refusal": _spec(
        "diagnostic",
        "adapted false-refusal judge score / observed adapted judge units",
        applicability="adapted_reference_rubric",
        direction="lower_is_better",
    ),
    "answer_relevance": _spec(
        "diagnostic",
        "adapted answer-relevance judge score / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "instruction_compliance": _spec(
        "diagnostic",
        "adapted instruction-compliance judge score / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "contradiction_free": _spec(
        "diagnostic",
        "adapted contradiction-free judge score / observed adapted judge units",
        applicability="adapted_reference_rubric",
    ),
    "unsupported_claim_rate": _spec(
        "diagnostic",
        "adapted unsupported-claim rate / observed adapted judge units",
        applicability="adapted_reference_rubric",
        direction="lower_is_better",
    ),
    "critical_contradiction_rate": _spec(
        "answer",
        "critical contradiction count / applicable answerable attempts",
        applicability="structured_critical_claim_gold",
        direction="lower_is_better",
    ),
    "citation_locator_validity": _spec(
        "citation",
        "resolvable submitted citations / submitted citations",
        applicability="submitted_citations",
        na_reason="NO_SUBMITTED_CITATION",
    ),
    "citation_entailment_precision": _spec(
        "citation",
        "fully entailing submitted citations / submitted citations",
        applicability="submitted_citations_with_entailment_trace",
        na_reason="CITATION_ENTAILMENT_TRACE_UNAVAILABLE",
    ),
    "answer_claim_citation_coverage": _spec(
        "citation",
        "answer claims with a valid entailing citation / citation-required answer claims",
        applicability="citation_required_answerable",
        na_reason="NO_CITATION_REQUIRED_CLAIMS",
    ),
    "fabricated_citation_count": _spec(
        "citation",
        "submitted citations that do not resolve to the frozen corpus",
        unit="count",
        direction="lower_is_better",
        applicability="submitted_citations",
        na_reason="NO_SUBMITTED_CITATION",
    ),
    "out_of_scope_citation_count": _spec(
        "citation",
        "submitted citations outside the frozen selected-document scope",
        unit="count",
        direction="lower_is_better",
        applicability="submitted_citations",
        na_reason="NO_SUBMITTED_CITATION",
    ),
    "claim_correctness": _spec("grounding", "judge claim correctness from frozen claim rows"),
    "grounding": _spec("grounding", "judge grounding score from frozen judge rows"),
    "tdas": _spec("answer", "frozen judge TDAS pass rate", applicability="structured_claim_gold"),
    "reference_claim_recall": _spec("answer", "covered Gold claims / scored Gold claims", applicability="structured_claim_gold"),
    "critical_claim_coverage": _spec("answer", "covered critical claims / critical claims", applicability="structured_claim_gold"),
    "gold_evidence_support": _spec("grounding", "Gold-supported response claims / response claims", applicability="structured_claim_gold"),
    "invalid_extra_rate_at_10": _spec("retrieval", "invalid returned items / returned items", direction="lower_is_better"),
    "invalid_extra_docs_at_10": _spec("retrieval", "invalid returned documents / returned documents", direction="lower_is_better"),
}

for _k in (1, 3, 5, 10):
    METRIC_REGISTRY[f"recall_at_{_k}"] = _spec("retrieval", f"Gold items found in top {_k} / applicable Gold items")
    METRIC_REGISTRY[f"hit_at_{_k}"] = _spec("retrieval", f"attempts with any Gold hit in top {_k} / applicable attempts")
    METRIC_REGISTRY[f"precision_at_{_k}"] = _spec("retrieval", f"relevant returned items in top {_k} / returned items in top {_k}")
    METRIC_REGISTRY[f"complete_evidence_set_recall_at_{_k}"] = _spec(
        "retrieval",
        f"attempts with a complete Gold evidence set in top {_k} / applicable attempts",
        applicability="structured_evidence_sets",
    )
    METRIC_REGISTRY[f"adapted_complete_evidence_set_recall_at_{_k}"] = _spec(
        "retrieval",
        f"adapted Gold document/evidence set hits in top {_k} / applicable attempts",
        applicability="gold_doc_ids_or_gold_evidence",
    )
    METRIC_REGISTRY[f"ndcg_at_{_k}"] = _spec(
        "retrieval",
        f"normalized discounted cumulative gain at {_k} over graded qrels",
        applicability="graded_qrels",
        na_reason="MISSING_GRADED_QRELS",
    )

for _name, _formula in (
    ("mrr", "reciprocal first relevant rank / applicable attempts"),
    ("map_at_10", "mean average precision at 10 over applicable attempts"),
    ("ndcg_at_10", "normalized discounted gain at 10 over graded qrels"),
):
    METRIC_REGISTRY[_name] = _spec("retrieval", _formula, applicability="matching_ranked_qrels")

for _stage in ("retrieval", "qa", "e2e"):
    for _percentile in ("p50", "p95", "p99"):
        METRIC_REGISTRY[f"latency_ms.{_stage}.{_percentile}"] = _spec(
            "performance",
            f"{_percentile} percentile of observed {_stage} milliseconds",
            unit="milliseconds",
            direction="lower_is_better",
            applicability="terminal_rows_with_latency",
            na_reason="NO_LATENCY_OBSERVATIONS",
        )

for _metric_id, _entry in METRIC_REGISTRY.items():
    _entry.setdefault("metric_id", _metric_id)


def metric_record(
    metric_id: str,
    *,
    value: int | float | None,
    numerator: int | float | None,
    denominator: int | float | None,
    eligible_n: int = 0,
    missing_n: int = 0,
    failed_n: int = 0,
    na_reason: str | None = None,
    unit: str | None = None,
    aggregation: str = "planned_initial_denominator",
    protocol_label: str | None = None,
    planned_n: int | None = None,
    observed_n: int | None = None,
) -> dict[str, Any]:
    """Build the auditable record required by the mixed benchmark schema."""

    spec = METRIC_REGISTRY.get(metric_id, {})
    record: dict[str, Any] = {
        "metric_id": metric_id,
        "metric": metric_id,
        "metric_version": spec.get("metric_version", METRIC_VERSION),
        "value": _number(value),
        "numerator": _number(numerator),
        "denominator": _number(denominator),
        "eligible_n": int(eligible_n),
        "missing_n": int(missing_n),
        "failed_n": int(failed_n),
        "na_reason": na_reason,
        "unit": unit or spec.get("unit", "rate"),
        "aggregation": aggregation,
        "planned_n": planned_n if planned_n is not None else _count(denominator),
        "observed_n": observed_n if observed_n is not None else int(eligible_n),
    }
    direction = spec.get("direction")
    record["higher_is_better"] = True if direction == "higher_is_better" else False if direction == "lower_is_better" else None
    record["applicable"] = na_reason is None
    record["reason_code"] = _reason_code(na_reason)
    record["diagnostics"] = {
        "planned_n": record["planned_n"],
        "observed_n": record["observed_n"],
        "eligible_n": record["eligible_n"],
        "missing_n": record["missing_n"],
        "failed_n": record["failed_n"],
    }
    for field in ("layer", "direction", "applicability", "formula"):
        if field in spec:
            record[field] = spec[field]
    if protocol_label is not None:
        record["protocol_label"] = protocol_label
    return record


def na_record(
    metric_id: str,
    reason: str,
    *,
    denominator: int | float | None,
    eligible_n: int = 0,
    missing_n: int = 0,
    failed_n: int = 0,
    numerator: int | float | None = 0,
    unit: str | None = None,
    aggregation: str = "not_applicable",
    protocol_label: str | None = None,
    planned_n: int | None = None,
    observed_n: int | None = None,
) -> dict[str, Any]:
    normalized_reason = reason if str(reason).startswith("N/A:") else f"N/A:{reason}"
    return metric_record(
        metric_id,
        value=None,
        numerator=numerator,
        denominator=denominator,
        eligible_n=eligible_n,
        missing_n=missing_n,
        failed_n=failed_n,
        na_reason=normalized_reason,
        unit=unit,
        aggregation=aggregation,
        protocol_label=protocol_label,
        planned_n=planned_n,
        observed_n=observed_n,
    )


def _number(value: int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _reason_code(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = str(reason)
    return text[4:] if text.startswith("N/A:") else text


def _count(value: int | float | None) -> int | None:
    if value is None:
        return None
    return int(value)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return ordered[index]


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\W+", "", text, flags=re.UNICODE)


def tokens(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[\w\u4e00-\u9fff]+", text, flags=re.UNICODE)


def token_f1(prediction: Any, reference: Any) -> float | None:
    expected = tokens(reference)
    predicted = tokens(prediction)
    if not expected:
        return None
    if not predicted:
        return 0.0
    expected_counts = Counter(expected)
    predicted_counts = Counter(predicted)
    overlap = sum((expected_counts & predicted_counts).values())
    if not overlap:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def answerability_label(question: Mapping[str, Any]) -> str:
    """Classify the frozen question without inferring claims from its answer."""

    raw_type = str(question.get("question_type", question.get("type", "")) or "").casefold().replace("-", "_").replace(" ", "_")
    answerable_value = question.get("answerable", question.get("expected_answerable", ""))
    raw_answerability = str(question.get("answerability", answerable_value) or "").casefold()
    if question.get("info_not_found") is True or "info_not_found" in raw_type or "info-not-found" in raw_answerability:
        return "info_not_found"
    if answerable_value is False or raw_answerability in {"false", "no", "unanswerable", "unanswerable_query", "null"} or raw_type in {"null", "null_query", "unanswerable", "unanswerable_query"}:
        return "unanswerable"
    return "answerable"


def gold_doc_count(question: Mapping[str, Any]) -> int:
    value = question.get("gold_doc_ids")
    if value is None:
        value = question.get("gold_document_ids", question.get("relevant_document_ids", question.get("relevant_documents", [])))
    if value in (None, [], {}) and isinstance(question.get("gold_evidence"), list):
        value = [
            item.get("document_id", item.get("doc_id", item.get("file_id", item.get("source_id"))))
            for item in question["gold_evidence"]
            if isinstance(item, Mapping)
        ]
    if isinstance(value, (list, tuple, set)):
        return len({str(item.get("id", item.get("document_id", item.get("doc_id", item)))) if isinstance(item, Mapping) else str(item) for item in value if item not in (None, "")})
    return 1 if value not in (None, "") else 0


def source_for_row(row: Mapping[str, Any] | None, question: Mapping[str, Any]) -> str:
    """Prefer the ledger's row-level source route, then frozen Gold metadata."""

    if row and str(row.get("source_dataset") or "").strip():
        return canonical_source_dataset(row["source_dataset"])
    question_source = question.get("source_dataset")
    if not str(question_source or "").strip():
        question_source = question.get("dataset", "unknown")
    return canonical_source_dataset(question_source)


def _material_structured_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def normalize_structured_gold(question: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy structured-Gold aliases to one canonical contract.

    The evaluator may read an older frozen row, but it must make the alias
    translation explicit before any canonical metric is considered applicable.
    ``gold`` is inspected as a legacy nested container; ordinary
    ``gold_evidence`` is intentionally not an alias for ``evidence_sets``.
    """

    normalized = dict(question)
    sources: list[Mapping[str, Any]] = [question]
    nested = question.get("gold")
    if isinstance(nested, Mapping):
        sources.append(nested)
    aliases_used: dict[str, str] = {}
    for canonical_name in STRUCTURED_GOLD_FIELDS:
        if _material_structured_value(normalized.get(canonical_name)):
            continue
        for source in sources:
            for alias in STRUCTURED_GOLD_ALIASES[canonical_name]:
                value = source.get(alias)
                if not _material_structured_value(value):
                    continue
                normalized[canonical_name] = value
                if alias != canonical_name:
                    aliases_used[canonical_name] = alias
                break
            if canonical_name in normalized and _material_structured_value(normalized[canonical_name]):
                break
    if aliases_used:
        existing = normalized.get("structured_gold_aliases")
        explicit = dict(existing) if isinstance(existing, Mapping) else {}
        normalized["structured_gold_aliases"] = {**explicit, **aliases_used}
    return normalized


def structured_gold_missing_fields(question: Mapping[str, Any]) -> tuple[str, ...]:
    normalized = normalize_structured_gold(question)
    return tuple(field for field in STRUCTURED_GOLD_FIELDS if not _material_structured_value(normalized.get(field)))


def structured_gold_complete(question: Mapping[str, Any]) -> bool:
    return not structured_gold_missing_fields(question)


def structured_claim_gold_available(questions: Iterable[Mapping[str, Any]]) -> bool:
    materialized = list(questions)
    return bool(materialized) and all(structured_gold_complete(question) for question in materialized)


def normalize_legacy_record(metric_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade the established single-dataset record without changing its value."""

    reason = record.get("na_reason", record.get("reason"))
    denominator = record.get("denominator")
    eligible = int(record.get("eligible_n", 0) or 0)
    missing = int(record.get("missing_n", max(0, int(denominator or 0) - eligible)) or 0)
    failed = int(record.get("failed_n", 0) or 0)
    planned = record.get("planned_n")
    observed = record.get("observed_n")
    return metric_record(
        metric_id,
        value=record.get("value"),
        numerator=record.get("numerator"),
        denominator=denominator,
        eligible_n=eligible,
        missing_n=missing,
        failed_n=failed,
        na_reason=reason,
        unit=record.get("unit"),
        aggregation=str(record.get("aggregation", "planned_initial_denominator")),
        protocol_label=record.get("protocol_label"),
        planned_n=int(planned) if planned is not None else None,
        observed_n=int(observed) if observed is not None else None,
    )
