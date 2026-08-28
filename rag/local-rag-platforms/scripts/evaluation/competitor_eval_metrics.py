#!/usr/bin/env python3
"""Deterministic aggregation for local competitor-evaluation runs.

This module is deliberately small and standard-library only.  It reads a
frozen package manifest plus the run's initial/terminal ledgers; it never
downloads data, calls a model, or edits ``TODO.md``.  Missing trace, Gold, or
judge input is represented explicitly instead of being guessed from answer
text.

The public seams are :func:`aggregate_run`, :func:`todo_markdown`, and the
``aggregate``/``todo-markdown`` CLI subcommands.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from competitor_eval_metric_registry import (
    MISSING_STRUCTURED_CLAIM_GOLD,
    METRIC_REGISTRY,
    REGISTRY_VERSION,
    answerability_label,
    gold_doc_count,
    metric_record,
    na_record,
    normalize_structured_gold,
    normalize_legacy_record,
    normalize_text,
    percentile as registry_percentile,
    source_for_row,
    structured_claim_gold_available,
    token_f1 as registry_token_f1,
)


TOP_K = (1, 3, 5, 10)
UNSUPPORTED_NEEDS_JUDGE = "UNSUPPORTED_NEEDS_JUDGE"
UNSUPPORTED_TRACE_UNAVAILABLE = "UNSUPPORTED_TRACE_UNAVAILABLE"
UNSUPPORTED_GOLD_UNAVAILABLE = "UNSUPPORTED_GOLD_UNAVAILABLE"
INCOMPLETE_PLANNED_DENOMINATOR = "INCOMPLETE_PLANNED_DENOMINATOR"
INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS = "INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS"

VALID_STATUSES = frozenset({"SUCCESS", "EMPTY"})
FAILED_STATUSES = frozenset(
    {
        "FAILED",
        "FAILURE",
        "ERROR",
        "BLOCKED",
        "TIMEOUT",
        "TIMED_OUT",
        "INTERRUPTED",
        "INVALID",
        "CANCELLED",
        "CANCELED",
        "ABORTED",
        "SKIPPED",
        "REJECTED",
    }
)
UNSUPPORTED_STATUSES = frozenset({"UNSUPPORTED", "NOT_SUPPORTED"})
PENDING_STATUSES = frozenset({"PLANNED", "NOT_STARTED", "PENDING", "RUNNING", "IN_PROGRESS"})
EXCLUDED_DATASETS = frozenset({"omnidocbench", "lenovo", "lenovo-bench"})

_DATASET_ALIASES = {
    "wiki": "wikieval",
    "wiki-eval": "wikieval",
    "wikieval": "wikieval",
    "multi-hop": "multihop-rag",
    "multihop": "multihop-rag",
    "multihoprag": "multihop-rag",
    "multihop-rag": "multihop-rag",
    "enterprise": "enterprise-rag-bench",
    "enterprise-rag": "enterprise-rag-bench",
    "enterpriserag": "enterprise-rag-bench",
    "enterpriserag-bench": "enterprise-rag-bench",
    "enterprise-rag-bench": "enterprise-rag-bench",
    "fab": "fab-bench",
    "fabbench": "fab-bench",
    "fab-bench": "fab-bench",
    "mm-doc-ir": "mmdocir",
    "mm_doc_ir": "mmdocir",
    "mmdocir": "mmdocir",
    "mm-doc-rag": "mmdocrag",
    "mm_doc_rag": "mmdocrag",
    "mmdocrag": "mmdocrag",
    "doc-bench": "docbench",
    "doc_bench": "docbench",
    "docbench": "docbench",
    "omnidocbench": "omnidocbench",
    "omni-doc-bench": "omnidocbench",
    "lenovo": "lenovo-bench",
    "lenovo-bench": "lenovo-bench",
    "moi-rag-bench-v0.1-text-only-no-mllm": "moi-rag-bench-v0.1-text-only-no-mllm",
    "moi-rag-bench-v0.1-ready-for-eval": "moi-rag-bench-v0.1-text-only-no-mllm",
    "moi-rag-bench-v0.1-mixed": "moi-rag-bench-v0.1-text-only-no-mllm",
}

_ID_KEYS = frozenset(
    {
        "id",
        "document_id",
        "doc_id",
        "file_id",
        "source_id",
        "chunk_id",
        "page_id",
        "layout_id",
        "quote_id",
        "evidence_id",
        "collection_id",
        "source",
        "source_name",
        "sourcename",
        "file_name",
        "filename",
        "name",
        "title",
    }
)
_ANSWER_KEYS = (
    "answer",
    "generated_answer",
    "prediction",
    "response",
    "output",
    "content",
    "text",
)
_HIT_KEYS = (
    "hits",
    "retrieval_hits",
    "retrieved",
    "retrieved_evidence",
    "evidence_hits",
    "search_results",
    "documents",
    "results",
    "records",
    "items",
    "list",
    "retriever_resources",
    "contexts",
    "chunks",
)
_STAGE_ALIASES = {
    "retrieve": "retrieval",
    "retrieval": "retrieval",
    "search": "retrieval",
    "direct_retrieval": "retrieval",
    "retriever": "retrieval",
    "qa": "qa",
    "answer": "qa",
    "generation": "qa",
    "generate": "qa",
    "native_qa": "qa",
    "controlled_qa": "qa",
}


class MetricsError(RuntimeError):
    """Raised for malformed local input or an unusable manifest."""


@dataclass
class PackageData:
    root: Path
    manifest_path: Path | None
    manifest: dict[str, Any]
    condition_manifest: dict[str, Any]
    dataset_id: str
    dataset_name: str
    revision: str
    split: str
    protocol_tag: str
    condition: str
    questions: list[dict[str, Any]]
    corpus: list[dict[str, Any]]
    gold: dict[str, dict[str, Any]]


@dataclass
class StageState:
    name: str
    units: list[tuple[str, int]]
    rows: dict[tuple[str, int], dict[str, Any]]
    questions: dict[str, dict[str, Any]]
    planned_n: int
    terminal_n: int
    valid_n: int
    failed_n: int
    unsupported_n: int
    pending_n: int
    status_counts: dict[str, int]

    @property
    def denominator(self) -> dict[str, int]:
        # Keep this compact and stable: callers use this object as the
        # denominator contract for every metric below it.
        return {
            "planned_n": self.planned_n,
            "terminal_n": self.terminal_n,
            "valid_n": self.valid_n,
            "failed_n": self.failed_n,
            "unsupported_n": self.unsupported_n,
            "pending_n": self.pending_n,
        }


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _boolish(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "ready", "complete", "completed"}:
            return True
        if normalized in {"false", "no", "0", "unknown", "missing"}:
            return False
    if value is None:
        return default
    return bool(value)


def _first(mapping: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if mapping is None:
        return default
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError as exc:
        raise MetricsError(f"FILE_MISSING: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MetricsError(f"JSON_INVALID: {path}: {exc}") from exc


def _jsonl_load(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError as exc:
        raise MetricsError(f"FILE_MISSING: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MetricsError(f"JSONL_INVALID: {path}:{line_number}: {exc}") from exc
        if isinstance(value, Mapping):
            rows.append(dict(value))
    return rows


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _resolve_ref(base: Path, value: Any, *, alternatives: Sequence[Path] = ()) -> Path | None:
    if not isinstance(value, (str, Path)):
        return None
    raw = Path(str(value)).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    candidates = [base / raw, *(root / raw for root in alternatives)]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else None


def _records_from_spec(base: Path, value: Any, label: str, *, alternatives: Sequence[Path] = ()) -> tuple[list[dict[str, Any]], Path | None]:
    source: Path | None = None
    if isinstance(value, Mapping):
        if isinstance(value.get("items"), list):
            value = value["items"]
        elif isinstance(value.get("records"), list):
            value = value["records"]
        elif value.get("path") is not None:
            value = value["path"]
    if isinstance(value, (str, Path)):
        source = _resolve_ref(base, value, alternatives=alternatives)
        if source is None or not source.exists():
            raise MetricsError(f"PACKAGE_{label.upper()}_MISSING: {value}")
        if source.is_dir():
            candidates = sorted(source.glob("*.jsonl")) + sorted(source.glob("*.json"))
            if len(candidates) != 1:
                raise MetricsError(f"PACKAGE_{label.upper()}_PATH_NOT_FILE: {source}")
            source = candidates[0]
        value = _jsonl_load(source) if source.suffix.lower() == ".jsonl" else _json_load(source)
    if isinstance(value, Mapping):
        for key in ("items", "records", label, "data"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise MetricsError(f"PACKAGE_{label.upper()}_MUST_BE_LIST")
    return [dict(row) for row in value if isinstance(row, Mapping)], source


def _manifest_path(path: str | Path, *, names: Sequence[str]) -> tuple[Path, Path | None]:
    supplied = Path(path).expanduser().resolve()
    if supplied.is_file():
        return supplied.parent, supplied
    if not supplied.is_dir():
        raise MetricsError(f"PATH_NOT_FOUND: {supplied}")
    for name in names:
        candidate = supplied / name
        if candidate.is_file():
            return supplied, candidate
    return supplied, None


def canonical_dataset_id(value: Any) -> str:
    text = str(value or "unknown").strip().casefold().replace("_", "-").replace(" ", "-")
    if text in _DATASET_ALIASES:
        return _DATASET_ALIASES[text]
    compact = re.sub(r"[^a-z0-9-]+", "", text)
    return _DATASET_ALIASES.get(compact, compact or "unknown")


def _condition_from_run(run_manifest: Mapping[str, Any]) -> str:
    return str(_first(run_manifest, "condition", "track", "evaluation_condition", default="") or "").strip()


def _condition_selection(manifest: Mapping[str, Any], run_condition: str) -> tuple[str, dict[str, Any]]:
    conditions = manifest.get("conditions")
    if not isinstance(conditions, Mapping):
        return run_condition or str(manifest.get("condition", "") or ""), dict(manifest)
    # Some older manifests use `conditions` as free-form package metadata
    # (for example denominator_policy), not as named condition specs.  Keep
    # those packages on the run/manifest condition instead of treating the
    # metadata key as an evaluation condition.
    condition_spec_keys = {
        "questions",
        "questions_path",
        "corpus",
        "documents_path",
        "gold",
        "gold_path",
        "artifacts",
        "paths",
    }
    has_named_condition = any(
        isinstance(value, Mapping) and bool(condition_spec_keys.intersection(value))
        for value in conditions.values()
    )
    if not has_named_condition:
        return run_condition or str(manifest.get("condition", "") or ""), dict(manifest)
    candidates = {str(key).casefold(): (str(key), value) for key, value in conditions.items()}
    if run_condition and run_condition.casefold() in candidates and isinstance(candidates[run_condition.casefold()][1], Mapping):
        key, value = candidates[run_condition.casefold()]
        return key, dict(value)
    if len(candidates) == 1:
        key, value = next(iter(candidates.values()))
        return key, dict(value) if isinstance(value, Mapping) else dict(manifest)
    for alias in ("native", "native-pdf", "page", "c15", "default"):
        if alias in candidates and isinstance(candidates[alias][1], Mapping):
            key, value = candidates[alias]
            return key, dict(value)
    return run_condition or "", dict(manifest)


def _question_id(row: Mapping[str, Any]) -> str:
    return str(_first(row, "question_id", "id", "qid", "query_id", default=""))


def _merge_question_gold(question: dict[str, Any], gold: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(question)
    if gold:
        for key, value in gold.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
    return normalize_structured_gold(merged)


def _validate_declared_counts(
    manifest: Mapping[str, Any],
    actual: Mapping[str, int],
) -> None:
    """Reject a declared-count mismatch for a package being aggregated."""

    declared = manifest.get("counts")
    if not isinstance(declared, Mapping):
        return
    for key, observed in actual.items():
        if key not in declared:
            continue
        value = declared[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MetricsError(f"PACKAGE_DECLARED_COUNT_INVALID:{key}")
        if value != observed:
            raise MetricsError(f"PACKAGE_COUNT_MISMATCH:{key}:declared={value}:actual={observed}")


def _validate_text_only_package(
    manifest: Mapping[str, Any],
    selected: Mapping[str, Any],
    *,
    questions: Sequence[Mapping[str, Any]],
    corpus: Sequence[Mapping[str, Any]],
    gold_count: int,
) -> None:
    """Keep unified metrics on the same pure-text admission contract."""

    def value(name: str, default: Any = None) -> Any:
        if selected.get(name) is not None:
            return selected[name]
        return manifest.get(name, default)

    count_manifest = selected if isinstance(selected.get("counts"), Mapping) else manifest
    _validate_declared_counts(
        count_manifest,
        {"documents": len(corpus), "questions": len(questions), "gold": gold_count},
    )
    if value("mllm_required") is not False:
        raise MetricsError("TEXT_ONLY_MLLM_REQUIRED_MUST_BE_FALSE")
    if str(value("image_llm", "")).strip().upper() != "NOT_APPLICABLE":
        raise MetricsError("TEXT_ONLY_IMAGE_LLM_MUST_BE_NOT_APPLICABLE")
    for row in corpus:
        media_text = " ".join(
            str(row.get(key)).casefold()
            for key in ("media", "modality", "media_type", "mime_type")
            if row.get(key) not in (None, "")
        )
        if any(marker in media_text for marker in ("image", "audio", "video")):
            raise MetricsError("TEXT_ONLY_CORPUS_MEDIA_UNSUPPORTED")
    for row in questions:
        image_values = row.get("images", row.get("image_paths", row.get("image_path")))
        if image_values not in (None, "", [], ()):
            raise MetricsError("TEXT_ONLY_QUESTION_IMAGE_INPUT_UNSUPPORTED")
        media_text = " ".join(
            str(row.get(key)).casefold()
            for key in ("media", "modality")
            if row.get(key) not in (None, "")
        )
        if any(marker in media_text for marker in ("image", "audio", "video")):
            raise MetricsError("TEXT_ONLY_QUESTION_MEDIA_UNSUPPORTED")


def _load_package(package: str | Path, run_manifest: Mapping[str, Any]) -> PackageData:
    root, manifest_path = _manifest_path(
        package,
        names=("package.json", "manifest.json", "dataset-fragment.json", "competitor-eval-ready.json", "package-manifest.json"),
    )
    raw_manifest: Any = {} if manifest_path is None else _json_load(manifest_path)
    if not isinstance(raw_manifest, Mapping):
        raise MetricsError("PACKAGE_MANIFEST_MUST_BE_OBJECT")
    nested_manifest = raw_manifest.get("package")
    if not isinstance(nested_manifest, Mapping):
        nested_manifest = raw_manifest.get("manifest")
    manifest = dict(nested_manifest) if isinstance(nested_manifest, Mapping) else dict(raw_manifest)
    run_condition = _condition_from_run(run_manifest)
    condition, selected = _condition_selection(manifest, run_condition)
    base = manifest_path.parent if manifest_path is not None else root
    # A selected document-fragment condition stores paths relative to the
    # fragment root.  Also try the condition directory for compact manifests.
    condition_root = base / condition if condition else base
    alternatives = (condition_root, base)
    selected_paths = _mapping(selected.get("paths")) or {}

    question_spec = _first(selected, "questions", "questions_path", default=None)
    if question_spec is None:
        question_spec = _first(selected_paths, "questions", "questions_path", default=None)
    if question_spec is None:
        question_spec = _first(manifest, "questions", "questions_path", default=None)
    if question_spec is None:
        for name in ("questions.jsonl", "questions.json"):
            candidate = condition_root / name
            if candidate.exists():
                question_spec = candidate
                break
    questions, _ = _records_from_spec(base, question_spec, "questions", alternatives=alternatives) if question_spec is not None else ([], None)

    gold_spec = _first(selected, "gold", "gold_path", default=None)
    if gold_spec is None:
        gold_spec = _first(selected_paths, "gold", "gold_path", default=None)
    if gold_spec is None:
        gold_spec = _first(manifest, "gold", "gold_path", default=None)
    if gold_spec is None:
        for name in ("gold.jsonl", "gold.json"):
            candidate = condition_root / name
            if candidate.exists():
                gold_spec = candidate
                break
    gold_rows, _ = _records_from_spec(base, gold_spec, "gold", alternatives=alternatives) if gold_spec is not None else ([], None)
    gold = {_question_id(row): row for row in gold_rows if _question_id(row)}
    questions = [_merge_question_gold(row, gold.get(_question_id(row))) for row in questions]

    corpus_spec = _first(selected, "corpus", "corpus_path", default=None)
    if corpus_spec is None:
        corpus_spec = _first(selected_paths, "corpus", "corpus_path", default=None)
    if corpus_spec is None:
        corpus_spec = _first(manifest, "corpus", "corpus_path", default=None)
    if corpus_spec is None:
        for name in ("corpus.jsonl", "corpus.json"):
            candidate = condition_root / name
            if candidate.exists():
                corpus_spec = candidate
                break
    corpus, _ = _records_from_spec(base, corpus_spec, "corpus", alternatives=alternatives) if corpus_spec is not None else ([], None)

    dataset_value = _first(
        manifest,
        "dataset_id",
        "dataset",
        "dataset_name",
        default=_first(run_manifest, "dataset_id", "dataset", "dataset_name", default="unknown"),
    )
    if isinstance(dataset_value, Mapping):
        dataset_name = str(_first(dataset_value, "name", "id", "dataset_id", default="unknown"))
    else:
        dataset_name = str(dataset_value)
    dataset_id = canonical_dataset_id(dataset_name)
    if condition == "text-only-no-mllm":
        _validate_text_only_package(
            manifest,
            selected,
            questions=questions,
            corpus=corpus,
            gold_count=len(gold_rows),
        )
    revision = str(
        _first(
            manifest,
            "revision",
            "dataset_revision",
            "gold_version",
            default=_first(run_manifest, "revision", "dataset_revision", "gold_version", default="UNKNOWN"),
        )
    )
    split = str(_first(manifest, "split", default=_first(run_manifest, "split", default="UNKNOWN")))
    protocol = str(
        _first(
            selected,
            "protocol_tag",
            "protocol",
            default=_first(
                manifest,
                "protocol_tag",
                "protocol",
                default=_first(run_manifest, "protocol_tag", "protocol", default="ADAPTED_PROTOCOL"),
            ),
        )
    )
    if not questions:
        # A package may expose only a count and put all Gold on the run ledger.
        # Keep the absence visible; placeholders are added from ledger rows in
        # aggregate_run and never manufacture Gold values.
        questions = []
    return PackageData(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        condition_manifest=selected,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        revision=revision,
        split=split,
        protocol_tag=protocol,
        condition=condition,
        questions=questions,
        corpus=corpus,
        gold=gold,
    )


def _load_run_manifest(run: str | Path) -> tuple[Path, dict[str, Any], Path | None]:
    root, manifest_path = _manifest_path(
        run,
        names=("run-manifest.json", "start-record.json", "run.json", "manifest.json", "package.json"),
    )
    if manifest_path is None:
        return root, {}, None
    raw = _json_load(manifest_path)
    return root, dict(raw) if isinstance(raw, Mapping) else {}, manifest_path


def _read_optional_jsonl(path: Path) -> list[dict[str, Any]]:
    return _jsonl_load(path) if path.is_file() else []


def _ledger_paths(root: Path) -> list[Path]:
    preferred = (
        "terminal-ledger.jsonl",
        "attempts.jsonl",
        "results.jsonl",
        "run-results.jsonl",
        "ledger.jsonl",
        "retrieval-ledger.jsonl",
        "qa-ledger.jsonl",
        "retrieval-results.jsonl",
        "qa-results.jsonl",
    )
    paths: list[Path] = []
    for name in preferred:
        candidate = root / name
        if candidate.is_file():
            paths.append(candidate)
    for pattern in (
        "**/terminal-ledger.jsonl",
        "**/attempts.jsonl",
        "**/results.jsonl",
        "**/run-results.jsonl",
        "**/retrieval-ledger.jsonl",
        "**/qa-ledger.jsonl",
        "**/retrieval-results.jsonl",
        "**/qa-results.jsonl",
    ):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file() and candidate not in paths and "http" not in candidate.parts:
                paths.append(candidate)
    return paths


def _load_ledgers(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    initial: list[dict[str, Any]] = []
    initial_candidates = sorted(root.glob("**/initial-ledger.jsonl"))
    for path in initial_candidates:
        if path.is_file():
            initial.extend(_jsonl_load(path))
    terminal: list[dict[str, Any]] = []
    for path in _ledger_paths(root):
        terminal.extend(_jsonl_load(path))
    return initial, terminal


def _normal_stage(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _STAGE_ALIASES.get(text, text)


def _status(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    # Historical competitor runners used a compact ``results.jsonl`` schema
    # with ``status: ok``.  Normalize only unambiguous terminal spellings so
    # that the denominator contract stays shared by all runner generations.
    return {
        "OK": "SUCCESS",
        "SUCCESSFUL": "SUCCESS",
        "SUCCEEDED": "SUCCESS",
        "COMPLETED": "SUCCESS",
        "COMPLETE": "SUCCESS",
        "EMPTY_RESULT": "EMPTY",
        "NO_RESULTS": "EMPTY",
        "NOT_FOUND": "EMPTY",
        "FAIL": "FAILED",
    }.get(normalized, normalized)


def _attempt_type_is_primary(row: Mapping[str, Any]) -> bool:
    attempt_type = str(_first(row, "attempt_type", "type", default="initial") or "initial").casefold()
    if attempt_type in {"retry", "diagnostic", "replacement", "recovery"}:
        return False
    if row.get("retry_of") not in (None, "") or row.get("replacement_of") not in (None, ""):
        return False
    if row.get("is_retry") is True:
        return False
    return row.get("planned_denominator", True) is not False


def _row_question_id(row: Mapping[str, Any]) -> str:
    direct = _first(row, "question_id", "qid", "query_id", default=None)
    if direct not in (None, ""):
        return str(direct)
    for key in ("case", "question", "query", "request"):
        nested = _mapping(row.get(key))
        nested_id = _first(nested, "question_id", "id", "qid", "query_id", default=None)
        if nested_id not in (None, ""):
            return str(nested_id)
    direct_id = row.get("id")
    return str(direct_id) if direct_id not in (None, "") else ""


def _repeat_id(row: Mapping[str, Any]) -> int:
    value = _first(row, "repeat_id", "repeat", "run_repeat", default=1)
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _row_has_stage_data(row: Mapping[str, Any], stage: str) -> bool:
    normal = _normal_stage(_first(row, "stage", "phase", "track", default=""))
    if normal == stage:
        return True
    if normal:
        return False
    if _status(row.get("status")) in FAILED_STATUSES | UNSUPPORTED_STATUSES:
        # A legacy whole-run result may record only a terminal error and no
        # stage payload.  Count that unit as failed/unsupported for both
        # applicable ledgers instead of silently turning it into pending.
        return True
    if stage == "retrieval":
        return any(
            key in row
            for key in (
                "hits",
                "retrieval",
                "retrieval_hits",
                "retrieved",
                "retrieval_contract",
                "retrieval_result",
                "retrieval_payload",
                "retrieved_evidence",
                "evidence_hits",
                "retrieval_trace",
                "search_results",
                "chunks",
                "contexts",
                "retriever_resources",
                "documents",
                "results",
            )
        )
    return any(key in row for key in ("answer", "generated_answer", "prediction", "qa", "answer_metrics", "qa_contract"))


def _dedupe_stage_rows(rows: Iterable[Mapping[str, Any]], stage: str) -> dict[tuple[str, int], dict[str, Any]]:
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        if not _attempt_type_is_primary(row) or not _row_has_stage_data(row, stage):
            continue
        question_id = _row_question_id(row)
        if not question_id:
            continue
        key = (question_id, _repeat_id(row))
        # Terminal-ledger writers are append-only.  If a generic source has
        # duplicate keys, keep the first terminal observation deterministically.
        if key not in selected or _status(selected[key].get("status")) in PENDING_STATUSES:
            selected[key] = row
    return selected


def _run_repeats(run_manifest: Mapping[str, Any], initial: Sequence[Mapping[str, Any]], questions_n: int) -> int:
    planned_block = _mapping(run_manifest.get("planned")) or {}
    planned = _first(planned_block, "repeats", default=None)
    if planned is None:
        planned = _first(_mapping(run_manifest.get("denominator_contract")), "repeats", default=None)
    values = [_repeat_id(row) for row in initial if _row_question_id(row)]
    if planned is None and values:
        planned = max(values)
    if planned is None and questions_n:
        initial_attempts = _first(planned_block, "initial_attempts", "attempts", default=None)
        try:
            if initial_attempts is not None and int(initial_attempts) >= questions_n and int(initial_attempts) % questions_n == 0:
                planned = int(initial_attempts) // questions_n
        except (TypeError, ValueError):
            pass
    try:
        repeats = int(planned or 1)
    except (TypeError, ValueError):
        repeats = 1
    return max(1, repeats)


def _manifest_question_count(package: PackageData, run_manifest: Mapping[str, Any]) -> int:
    if package.questions:
        return len(package.questions)
    counts = _mapping(package.manifest.get("counts")) or _mapping(package.condition_manifest.get("counts"))
    count = _first(counts, "questions", "question_rows", "question_count", "questions_count", "num_questions", default=None)
    if count is None:
        count = _first(package.manifest, "question_count", "questions_count", "num_questions", default=None)
    if count is None:
        count = _first(_mapping(run_manifest.get("planned")), "questions", "question_count", default=0)
    try:
        return max(0, int(count or 0))
    except (TypeError, ValueError):
        return 0


def _question_type(row: Mapping[str, Any]) -> str:
    metadata = _mapping(row.get("metadata")) or {}
    case = _mapping(row.get("case")) or {}
    case_metadata = _mapping(case.get("metadata")) or {}
    return str(
        _first(
            row,
            "question_type",
            default=_first(
                metadata,
                "question_type",
                "test_type",
                "original_type",
                default=_first(
                    case,
                    "question_type",
                    default=_first(case_metadata, "question_type", "test_type", "original_type", default=_first(row, "type", default="unknown")),
                ),
            ),
        )
        or "unknown"
    )


def _question_map(package: PackageData, terminal_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {_question_id(row): dict(row) for row in package.questions if _question_id(row)}
    for row in terminal_rows:
        qid = _row_question_id(row)
        if not qid or qid in result:
            continue
        # This fallback only copies explicitly observed fields.  It is useful
        # for generic/legacy ledgers while keeping missing Gold visibly
        # missing.  Some older competitor runners nested the case under
        # ``case`` and called retrieved chunks ``chunks``.
        case = _mapping(row.get("case")) or {}
        case_metadata = _mapping(case.get("metadata")) or {}
        metadata = dict(_mapping(row.get("metadata")) or case_metadata)
        for keyword_key in ("retrieval_keywords", "expected_answer_keywords", "reference_keywords", "gold_keywords"):
            if keyword_key not in metadata:
                keyword_value = _first(row, keyword_key, default=_first(case, keyword_key, default=None))
                if keyword_value is not None:
                    metadata[keyword_key] = keyword_value
        result[qid] = normalize_structured_gold({
            "question_id": qid,
            "source_dataset": _first(row, "source_dataset", "dataset", default=_first(case, "source_dataset", "dataset", default="")),
            "question_type": _question_type(row),
            "answerability": _first(row, "answerability", default=_first(case, "answerability", default="")),
            "answerable": _first(row, "answerable", default=_first(case, "answerable", default=True)),
            "question": _first(row, "question", default=_first(case, "question", default="")),
            "reference_answer": _first(
                row,
                "reference_answer",
                "gold_answer",
                "expected_answer",
                "ground_truth_answer",
                default=_first(case, "reference_answer", "gold_answer", "expected_answer", "ground_truth_answer", default=""),
            ),
            "gold_doc_ids": _first(
                row,
                "gold_doc_ids",
                "relevant_documents",
                "expected_doc_ids",
                default=_first(case, "gold_doc_ids", "relevant_documents", "expected_doc_ids", default=[]),
            ),
            "gold_evidence": _first(
                row,
                "gold_evidence",
                "evidence",
                "relevant_evidence",
                default=_first(case, "gold_evidence", "evidence", "relevant_evidence", default=[]),
            ),
            "metadata": metadata,
            "scored_reference_claims": _first(row, "scored_reference_claims", "reference_claims", "claims", default=_first(case, "scored_reference_claims", "reference_claims", "claims", default=None)),
            "critical_claims": _first(row, "critical_claims", "critical_required_claims", "required_claims", default=_first(case, "critical_claims", "critical_required_claims", "required_claims", default=None)),
            "evidence_sets": _first(row, "evidence_sets", "gold_evidence_sets", "claim_evidence_sets", default=_first(case, "evidence_sets", "gold_evidence_sets", "claim_evidence_sets", default=None)),
        })
    return result


def _build_stage(
    stage: str,
    package: PackageData,
    run_manifest: Mapping[str, Any],
    initial: Sequence[Mapping[str, Any]],
    terminal_rows: Sequence[Mapping[str, Any]],
    questions: Mapping[str, dict[str, Any]],
) -> StageState:
    questions_n = _manifest_question_count(package, run_manifest)
    repeats = _run_repeats(run_manifest, initial, questions_n)
    if package.questions:
        question_ids = [_question_id(row) for row in package.questions if _question_id(row)]
    else:
        question_ids = [qid for qid in questions if qid]
    if not question_ids and questions_n:
        question_ids = [f"__planned_{index + 1:05d}" for index in range(questions_n)]
    if questions_n and len(question_ids) < questions_n:
        existing = set(question_ids)
        for index in range(questions_n - len(question_ids)):
            candidate = f"__planned_{index + 1:05d}"
            while candidate in existing:
                candidate = f"_{candidate}"
            question_ids.append(candidate)
            existing.add(candidate)
    units = [(qid, repeat) for qid in question_ids for repeat in range(1, repeats + 1)]
    planned_n = questions_n * repeats if questions_n else len(units)
    selected = _dedupe_stage_rows(terminal_rows, stage)
    # The package (or its frozen count) defines the denominator.  Ignore
    # stray diagnostic/retry question IDs in a run directory rather than
    # allowing them to inflate terminal_n, latency samples, or slices.
    unit_keys = set(units)
    if unit_keys:
        selected = {key: row for key, row in selected.items() if key in unit_keys}
    counts: Counter[str] = Counter(_status(row.get("status")) for row in selected.values())
    terminal_n = sum(counts.get(value, 0) for value in counts if value not in PENDING_STATUSES)
    valid_n = sum(counts.get(value, 0) for value in VALID_STATUSES)
    failed_n = sum(counts.get(value, 0) for value in FAILED_STATUSES)
    unsupported_n = sum(counts.get(value, 0) for value in UNSUPPORTED_STATUSES)
    pending_n = max(0, planned_n - terminal_n)
    return StageState(
        name=stage,
        units=units,
        rows=selected,
        questions=dict(questions),
        planned_n=planned_n,
        terminal_n=terminal_n,
        valid_n=valid_n,
        failed_n=failed_n,
        unsupported_n=unsupported_n,
        pending_n=pending_n,
        status_counts=dict(sorted(counts.items())),
    )


def _metric_record(
    state: StageState,
    value: float | None,
    numerator: float | int | None,
    denominator: int | float | None,
    *,
    eligible_n: int = 0,
    missing_n: int = 0,
    reason: str | None = None,
    unit: str = "rate",
    aggregation: str = "planned-initial-denominator",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "value": None if value is None else _number(value),
        "numerator": None if numerator is None else _number(numerator),
        "denominator": None if denominator is None else _number(denominator),
        "planned_n": state.planned_n,
        "terminal_n": state.terminal_n,
        "valid_n": state.valid_n,
        "failed_n": state.failed_n,
        "unsupported_n": state.unsupported_n,
        "pending_n": state.pending_n,
        "eligible_n": int(eligible_n),
        "missing_n": int(missing_n),
        "unit": unit,
        "aggregation": aggregation,
    }
    if reason:
        record["reason"] = reason
    return record


def _number(value: float | int) -> int | float:
    if isinstance(value, int):
        return value
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _rate_from_values(
    state: StageState,
    values: Sequence[float],
    *,
    reason_if_empty: str = UNSUPPORTED_TRACE_UNAVAILABLE,
    eligible_n: int | None = None,
    missing_n: int | None = None,
    force_unsupported: str | None = None,
) -> dict[str, Any]:
    numerator = sum(float(value) for value in values)
    eligible = len(values) if eligible_n is None else eligible_n
    missing = max(0, state.planned_n - eligible) if missing_n is None else missing_n
    if state.planned_n <= 0:
        return _metric_record(state, None, numerator, None, eligible_n=eligible, missing_n=missing, reason="NO_PLANNED_ATTEMPTS")
    if force_unsupported:
        return _metric_record(state, None, numerator, state.planned_n, eligible_n=eligible, missing_n=missing, reason=force_unsupported)
    if state.terminal_n == 0:
        return _metric_record(state, None, numerator, state.planned_n, eligible_n=eligible, missing_n=missing, reason="NO_TERMINAL_OBSERVATIONS")
    if not values:
        return _metric_record(state, None, numerator, state.planned_n, eligible_n=eligible, missing_n=missing, reason=reason_if_empty)
    if state.pending_n:
        return _metric_record(state, None, numerator, state.planned_n, eligible_n=eligible, missing_n=missing, reason=INCOMPLETE_PLANNED_DENOMINATOR)
    return _metric_record(state, numerator / state.planned_n, numerator, state.planned_n, eligible_n=eligible, missing_n=missing)


def _unsupported_record(state: StageState, reason: str = UNSUPPORTED_NEEDS_JUDGE) -> dict[str, Any]:
    return _metric_record(state, None, 0, state.planned_n or None, reason=reason)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Match the existing local scorer's punctuation-insensitive normalization.
    return re.sub(r"\W+", "", text, flags=re.UNICODE)


def _tokens(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[\w\u4e00-\u9fff]+", text, flags=re.UNICODE)


def _token_f1(prediction: Any, reference: Any) -> float | None:
    expected = _tokens(reference)
    predicted = _tokens(prediction)
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


def _answer_from_row(row: Mapping[str, Any]) -> str | None:
    for key in _ANSWER_KEYS:
        value = row.get(key)
        if isinstance(value, str):
            return value
    for key in (
        "answer",
        "generated_answer",
        "prediction",
        "qa",
        "native",
        "generation",
        "response_payload",
        "raw_response",
        "result",
        "response",
        "output",
    ):
        nested = _mapping(row.get(key))
        if nested:
            found = _answer_from_row(nested)
            if found is not None:
                return found
    return None


def _gold_answer(question: Mapping[str, Any], row: Mapping[str, Any] | None = None) -> str | None:
    metadata = _mapping(question.get("metadata")) or {}
    for source in (question, metadata):
        value = _first(source, "reference_answer", "gold_answer", "expected_answer", "answer", "ground_truth_answer", default=None)
        if value is not None and str(value) != "":
            return str(value)
    # A run row may carry an explicit copied Gold field, but its ordinary
    # ``answer`` field is the prediction and must never become self-Gold.
    if row:
        value = _first(row, "reference_answer", "gold_answer", "expected_answer", "ground_truth_answer", default=None)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _gold_keywords(question: Mapping[str, Any]) -> list[str]:
    metadata = _mapping(question.get("metadata")) or {}
    values: list[str] = []
    for source in (question, metadata):
        for key in ("retrieval_keywords", "expected_answer_keywords", "reference_keywords", "gold_keywords"):
            if source.get(key) is not None:
                values.extend(str(item) for item in _as_list(source[key]) if str(item).strip())
    return list(dict.fromkeys(values))


def _as_ids(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key in ("id", "document_id", "doc_id", "file_id", "source_id", "quote_id", "evidence_id", "path", "name"):
            if value.get(key) not in (None, ""):
                result.append(str(value[key]))
                break
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_as_ids(item))
    elif value not in (None, ""):
        result.append(str(value))
    return list(dict.fromkeys(result))


def _evidence_ids(value: Mapping[str, Any]) -> list[str]:
    """Collect all explicit evidence identifiers, not just the first one."""

    result: list[str] = []
    for key in (
        "id",
        "quote_id",
        "evidence_id",
        "document_id",
        "doc_id",
        "file_id",
        "source_id",
        "chunk_id",
        "path",
        "name",
    ):
        if value.get(key) not in (None, ""):
            result.extend(_as_ids(value[key]))
    return list(dict.fromkeys(result))


def _id_variants(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    text = str(value).strip().casefold()
    if not text:
        return set()
    variants = {text}
    variants.add(Path(text).name)
    if text.endswith((".md", ".txt", ".pdf", ".json", ".jsonl")):
        variants.add(Path(text).stem)
    if ":" in text:
        variants.add(text.rsplit(":", 1)[-1])
    return {item for item in variants if item}


def _metadata_identifier_aliases(package: PackageData) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for row in package.corpus:
        ids = _as_ids(row)
        metadata = _mapping(row.get("metadata")) or {}
        for key in _ID_KEYS:
            if row.get(key) not in (None, ""):
                ids.extend(_as_ids(row[key]))
            if metadata.get(key) not in (None, ""):
                ids.extend(_as_ids(metadata[key]))
        if not ids:
            continue
        canonical = str(_first(row, "doc_id", "document_id", "id", default=ids[0]))
        variants = set().union(*(_id_variants(item) for item in ids))
        aliases.setdefault(canonical.casefold(), set()).update(variants)
        # Match either the frozen canonical ID or a runner's filename/source
        # alias against the same corpus row.
        for variant in variants:
            aliases.setdefault(variant, set()).update(variants)
    return aliases


def _gold_doc_ids(question: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("gold_doc_ids", "gold_document_ids", "relevant_document_ids", "relevant_documents", "document_ids", "expected_doc_ids", "valid_doc_ids"):
        if question.get(key) is not None:
            values.extend(_as_ids(question[key]))
    metadata = _mapping(question.get("metadata")) or {}
    for key in ("gold_doc_ids", "expected_doc_ids", "valid_doc_ids"):
        if metadata.get(key) is not None:
            values.extend(_as_ids(metadata[key]))
    if not values:
        for evidence in _gold_evidence(question):
            if isinstance(evidence, Mapping) and str(evidence.get("status", "resolved")).casefold() not in {"missing", "missing_candidate"}:
                values.extend(_evidence_ids(evidence))
    return list(dict.fromkeys(values))


def _gold_evidence(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = question.get("gold_evidence")
    if values is None:
        values = question.get("evidence")
    if values is None:
        values = []
        for modality in ("text", "image"):
            for item in _as_list(question.get(f"{modality}_evidence")):
                if isinstance(item, Mapping):
                    enriched = dict(item)
                    enriched.setdefault("modality", modality)
                    values.append(enriched)
    result: list[dict[str, Any]] = []
    for item in _as_list(values):
        if isinstance(item, Mapping):
            value = dict(item)
            if str(value.get("status", "resolved")).casefold() in {"missing", "missing_candidate"} and not _as_ids(value):
                continue
            result.append(value)
    return result


def _iter_nested_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_nested_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_mappings(child)


def _extract_hits(row: Mapping[str, Any]) -> tuple[list[Any] | None, bool]:
    """Return (hits, trace_available), without treating answer text as hits."""

    candidates: list[Any] = []
    for key in (
        "hits",
        "retrieval_hits",
        "diagnostic_hits",
        "retrieved",
        "retrieved_evidence",
        "evidence_hits",
        "retrieval",
        "retrieval_trace",
        "search_results",
        "documents",
        "contexts",
        "chunks",
        "results",
        "retriever_resources",
    ):
        if key in row:
            value = row[key]
            if isinstance(value, Mapping):
                for nested_key in _HIT_KEYS:
                    if isinstance(value.get(nested_key), list):
                        return list(value[nested_key]), True
            if isinstance(value, list):
                return list(value), True
    for key in ("retrieval", "retrieval_result", "retrieval_payload", "payload"):
        nested = _mapping(row.get(key))
        if not nested:
            continue
        for hit_key in (*_HIT_KEYS, "chunks"):
            value = nested.get(hit_key)
            if isinstance(value, list):
                return list(value), True
        if any(key in nested for key in ("records", "retriever_resources", "list")):
            return [], True
    if "hit_count" in row or "retrieval_trace_available" in row:
        return None, bool(row.get("retrieval_trace_available"))
    return None, False


def _hit_ids(hit: Any) -> set[str]:
    values: list[str] = []
    for mapping in _iter_nested_mappings(hit):
        for key, value in mapping.items():
            if str(key).casefold() in _ID_KEYS:
                values.extend(_as_ids(value))
    if isinstance(hit, str):
        values.append(hit)
    return set().union(*(_id_variants(value) for value in values)) if values else set()


def _hit_modalities(hit: Any) -> set[str]:
    values: list[str] = []
    for mapping in _iter_nested_mappings(hit):
        for key in ("modality", "quote_modality", "evidence_modality", "evidence_type", "content_type", "type", "media", "media_type"):
            if mapping.get(key) not in (None, ""):
                values.extend(str(item).casefold() for item in _as_list(mapping[key]))
    normalized: set[str] = set()
    for value in values:
        if "image" in value or value in {"figure", "chart", "table-image", "visual"}:
            normalized.add("image")
        elif "text" in value or value in {"ocr", "markdown", "plain"}:
            normalized.add("text")
    return normalized


def _ordered_hits(hits: Sequence[Any]) -> list[Any]:
    """Honor explicit runner ranks while preserving input order otherwise."""

    decorated: list[tuple[float | None, int, Any]] = []
    has_rank = False
    for index, hit in enumerate(hits):
        rank_value: float | None = None
        for mapping in _iter_nested_mappings(hit):
            raw = _first(mapping, "rank", "position", "rank_index", "order", default=None)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
                rank_value = float(raw)
                has_rank = True
                break
        decorated.append((rank_value, index, hit))
    if not has_rank:
        return list(hits)
    return [item for _, _, item in sorted(decorated, key=lambda value: (value[0] is None, value[0] if value[0] is not None else value[1], value[1]))]


def _locator_values(value: Any) -> list[tuple[str | None, str | None, tuple[float, ...] | None]]:
    result: list[tuple[str | None, str | None, tuple[float, ...] | None]] = []
    for mapping in _iter_nested_mappings(value):
        file_value = _first(mapping, "file_id", "file", "doc_name", "document_id", "doc_id", "source_id", default=None)
        page_value = _first(mapping, "page", "page_number", "page_id", default=None)
        bbox_value = _first(mapping, "bbox", "bounding_box", "box", default=None)
        bbox: tuple[float, ...] | None = None
        if isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
            try:
                bbox = tuple(float(item) for item in bbox_value)
            except (TypeError, ValueError):
                bbox = None
        if file_value is not None or page_value is not None or bbox is not None:
            result.append(
                (
                    next(iter(_id_variants(file_value)), None) if file_value is not None else None,
                    str(page_value) if page_value is not None else None,
                    bbox,
                )
            )
    # De-duplicate while preserving traversal order.
    output: list[tuple[str | None, str | None, tuple[float, ...] | None]] = []
    for item in result:
        if item not in output:
            output.append(item)
    return output


def _gold_locator_items(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for evidence in _gold_evidence(question):
        locators = _locator_values(evidence.get("locator", evidence))
        if locators:
            result.append(
                {
                    "evidence": evidence,
                    "locators": locators,
                    "ids": set().union(*(_id_variants(item) for item in _evidence_ids(evidence))),
                }
            )
    return result


def _match_id_sets(gold_ids: Iterable[str], hit_ids: set[str], aliases: Mapping[str, set[str]] | None = None) -> bool:
    hit = set(hit_ids)
    for gold in gold_ids:
        variants = _id_variants(gold)
        if aliases:
            variants.update(aliases.get(str(gold).casefold(), set()))
        if variants & hit:
            return True
    return False


def _retrieval_items(question: Mapping[str, Any], dataset_id: str) -> tuple[list[str], str]:
    # Platform ledgers usually expose document/page/quote IDs, not evidence
    # sentences.  MultiHop therefore uses Gold document IDs as its explicit
    # evidence unit unless a future ledger provides typed evidence IDs.
    docs = _gold_doc_ids(question)
    if docs:
        return docs, "document"
    return [], "unknown"


def _row_matches_gold_ids(
    hit: Any,
    gold_ids: Sequence[str],
    aliases: Mapping[str, set[str]] | None = None,
) -> bool:
    return _match_id_sets(gold_ids, _hit_ids(hit), aliases)


def _rank_values(
    question: Mapping[str, Any],
    hits: Sequence[Any],
    aliases: Mapping[str, set[str]],
    k: int,
) -> tuple[float, float, bool]:
    gold_ids, _ = _retrieval_items(question, "")
    if not gold_ids:
        return 0.0, 0.0, False
    matched: set[int] = set()
    first_rank: int | None = None
    for rank, hit in enumerate(_ordered_hits(hits)[:k], start=1):
        for index, gold_id in enumerate(gold_ids):
            if index in matched:
                continue
            if _row_matches_gold_ids(hit, [gold_id], aliases):
                matched.add(index)
                if first_rank is None:
                    first_rank = rank
    recall = len(matched) / len(gold_ids)
    mrr = 1.0 / first_rank if first_rank is not None else 0.0
    return recall, mrr, True


def _page_rank_values(question: Mapping[str, Any], hits: Sequence[Any], aliases: Mapping[str, set[str]], k: int) -> tuple[float, float, bool]:
    gold_items = _gold_locator_items(question)
    if not gold_items:
        return 0.0, 0.0, False
    ordered_hits = _ordered_hits(hits)
    hit_locators = [_locator_values(hit) for hit in ordered_hits[:k]]
    matched: set[int] = set()
    first_rank: int | None = None
    for rank, locators in enumerate(hit_locators, start=1):
        hit_ids = _hit_ids(ordered_hits[rank - 1])
        for index, item in enumerate(gold_items):
            if index in matched:
                continue
            direct = bool(item["ids"] & hit_ids)
            locator_match = any(
                (gold_file is None or hit_file is None or _id_variants(gold_file) & _id_variants(hit_file))
                and (gold_page is None or hit_page == gold_page)
                for gold_file, gold_page, _ in item["locators"]
                for hit_file, hit_page, _ in locators
            )
            if direct or locator_match:
                matched.add(index)
                if first_rank is None:
                    first_rank = rank
    return len(matched) / len(gold_items), (1.0 / first_rank if first_rank else 0.0), True


def _layout_available(question: Mapping[str, Any], hits: Sequence[Any]) -> bool:
    gold = [item for item in _gold_locator_items(question) if any(locator[2] is not None for locator in item["locators"])]
    hit_has_bbox = any(any(locator[2] is not None for locator in _locator_values(hit)) for hit in hits)
    return bool(gold) and (hit_has_bbox or not hits)


def _layout_rank_values(question: Mapping[str, Any], hits: Sequence[Any], k: int) -> tuple[float, bool]:
    gold_items = [item for item in _gold_locator_items(question) if any(locator[2] is not None for locator in item["locators"])]
    if not gold_items:
        return 0.0, False
    overlap_total = 0.0
    for item in gold_items:
        best_overlap = 0.0
        for hit in _ordered_hits(hits)[:k]:
            for gold_file, gold_page, gold_bbox in item["locators"]:
                if gold_bbox is None:
                    continue
                for hit_file, hit_page, hit_bbox in _locator_values(hit):
                    if hit_bbox is None:
                        continue
                    if gold_file and hit_file and not (_id_variants(gold_file) & _id_variants(hit_file)):
                        continue
                    if gold_page and hit_page and gold_page != hit_page:
                        continue
                    best_overlap = max(best_overlap, _bbox_gold_area_recall(gold_bbox, hit_bbox))
        overlap_total += best_overlap
    return overlap_total / len(gold_items), True


def _bbox_gold_area_recall(gold_bbox: tuple[float, ...], hit_bbox: tuple[float, ...]) -> float:
    """Return intersection area normalized by the Gold bbox area."""

    if len(gold_bbox) != 4 or len(hit_bbox) != 4:
        return 0.0
    gold_left, gold_top, gold_right, gold_bottom = gold_bbox
    hit_left, hit_top, hit_right, hit_bottom = hit_bbox
    gold_width = max(0.0, gold_right - gold_left)
    gold_height = max(0.0, gold_bottom - gold_top)
    gold_area = gold_width * gold_height
    if gold_area <= 0:
        return 0.0
    intersection_width = max(0.0, min(gold_right, hit_right) - max(gold_left, hit_left))
    intersection_height = max(0.0, min(gold_bottom, hit_bottom) - max(gold_top, hit_top))
    return min(1.0, (intersection_width * intersection_height) / gold_area)


def _typed_gold(question: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]] | None:
    evidence = _gold_evidence(question)
    if not evidence:
        return None
    output: dict[str, list[dict[str, Any]]] = {"text": [], "image": []}
    for item in evidence:
        modalities = _hit_modalities(item)
        if len(modalities) != 1:
            return None
        modality = next(iter(modalities))
        ids = _evidence_ids(item)
        if not ids:
            return None
        item_copy = dict(item)
        item_copy["_ids"] = ids
        output[modality].append(item_copy)
    if not output["text"] and not output["image"]:
        return None
    return output


def _typed_recall(question: Mapping[str, Any], hits: Sequence[Any], modality: str, k: int, aliases: Mapping[str, set[str]]) -> tuple[float, bool]:
    typed = _typed_gold(question)
    if typed is None or not typed.get(modality):
        return 0.0, False
    if hits and not any(_hit_modalities(hit) for hit in hits):
        return 0.0, False
    matched = 0
    ordered_hits = _ordered_hits(hits)
    for gold in typed[modality]:
        if any(
            modality in _hit_modalities(hit) and _row_matches_gold_ids(hit, gold["_ids"], aliases)
            for hit in ordered_hits[:k]
        ):
            matched += 1
    return matched / len(typed[modality]), True


def _latency_value(row: Mapping[str, Any], stage: str) -> float | None:
    if stage == "retrieval":
        keys = ("retrieval_latency_ms", "search_latency_ms", "retrieval_ms", "latency_ms", "duration_ms", "latency", "duration")
    elif stage == "e2e":
        keys = ("e2e_latency_ms", "end_to_end_latency_ms", "total_latency_ms", "latency_ms", "duration_ms", "latency", "duration")
    else:
        keys = ("generation_latency_ms", "qa_latency_ms", "generation_ms", "latency_ms", "duration_ms", "latency", "duration")
    for key in keys:
        value = row.get(key)
        if value is None:
            for nested_key in ("timings", "latencies", "metrics"):
                nested = _mapping(row.get(nested_key))
                if nested:
                    value = nested.get(key)
                    if value is not None:
                        break
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0:
            return float(value)
    latency_maps = [
        nested
        for key in ("latency", "timing", "timings", "latencies")
        if (nested := _mapping(row.get(key))) is not None
    ]
    mapping_keys = {
        "retrieval": ("retrieval", "retrieval_ms", "retrieval_latency_ms", "search"),
        "qa": ("qa", "generation", "generation_ms", "generation_latency_ms", "answer"),
        "e2e": ("e2e", "total", "total_ms", "e2e_ms", "end_to_end"),
    }.get(stage, ())
    for latency_map in latency_maps:
        for key in (*mapping_keys, "latency_ms", "duration_ms", "ms"):
            value = latency_map.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0:
                return float(value)
    if stage == "e2e":
        retrieval = row.get("retrieval_latency_ms")
        generation = row.get("generation_latency_ms")
        if (
            isinstance(retrieval, (int, float))
            and not isinstance(retrieval, bool)
            and isinstance(generation, (int, float))
            and not isinstance(generation, bool)
            and math.isfinite(float(retrieval))
            and math.isfinite(float(generation))
            and float(retrieval) >= 0
            and float(generation) >= 0
        ):
            return float(retrieval) + float(generation)
    return None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return ordered[index]


def _latency_record(state: StageState, values: Sequence[float], fraction: float) -> dict[str, Any]:
    value = _percentile(values, fraction)
    if value is None:
        reason = "NO_LATENCY_OBSERVATIONS" if state.terminal_n else "NO_TERMINAL_OBSERVATIONS"
    else:
        reason = None
    return _metric_record(
        state,
        value,
        value,
        len(values) if values else None,
        eligible_n=len(values),
        missing_n=max(0, state.planned_n - len(values)),
        reason=reason,
        unit="milliseconds",
        aggregation="observed-terminal-latencies; nearest-rank-percentile",
    )


def _qa_metrics(state: StageState, dataset_id: str, aliases: Mapping[str, set[str]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if dataset_id == "fab-bench":
        # FAB's released native score is a six-dimensional judge.  Lexical
        # answer matching is not promoted to correctness for this subjective
        # benchmark; deterministic coverage is reported separately.
        for name in ("completeness", "technical_depth", "factuality", "relevance", "context_utilization", "support_quality", "overall", "correctness", "semantic_correctness"):
            metrics[name] = _unsupported_record(state)
    else:
        values_by_metric: dict[str, list[float]] = {"answer_non_empty": [], "contains_gold": [], "normalized_em": [], "token_f1": []}
        missing_gold = 0
        for key in state.units:
            row = state.rows.get(key)
            question = state.questions.get(key[0], {})
            if row is None:
                continue
            status = _status(row.get("status"))
            if status not in VALID_STATUSES:
                continue
            answer = _answer_from_row(row)
            if answer is None and status == "EMPTY":
                # EMPTY is a terminal, valid response.  A few ledgers omit
                # the redundant empty answer field, so preserve its zero in
                # lexical QA denominators without treating a missing SUCCESS
                # answer as an empty answer.
                answer = ""
            if answer is None:
                # A successful row without an answer field is not an empty
                # answer; it is an unavailable QA observation.
                continue
            values_by_metric["answer_non_empty"].append(float(bool(answer.strip())))
            gold = _gold_answer(question, row)
            if gold is None:
                missing_gold += 1
                continue
            normalized_answer = _normalize(answer)
            normalized_gold = _normalize(gold)
            values_by_metric["contains_gold"].append(float(bool(normalized_gold and normalized_gold in normalized_answer)))
            values_by_metric["normalized_em"].append(float(bool(normalized_gold and normalized_gold == normalized_answer)))
            token_value = _token_f1(answer, gold)
            if token_value is not None:
                values_by_metric["token_f1"].append(token_value)
        for name, values in values_by_metric.items():
            if name != "answer_non_empty" and missing_gold:
                metrics[name] = _metric_record(
                    state,
                    None,
                    sum(values),
                    state.planned_n or None,
                    eligible_n=len(values),
                    missing_n=missing_gold,
                    reason=UNSUPPORTED_GOLD_UNAVAILABLE,
                )
            else:
                metrics[name] = _rate_from_values(state, values, eligible_n=len(values), missing_n=max(0, state.planned_n - len(values)))
        keyword_values: list[float] = []
        keyword_missing = 0
        for key in state.units:
            row = state.rows.get(key)
            question = state.questions.get(key[0], {})
            if row is None or _status(row.get("status")) not in VALID_STATUSES:
                continue
            answer = _answer_from_row(row)
            keywords = _gold_keywords(question)
            if answer is None or not keywords:
                keyword_missing += 1
                continue
            normalized = _normalize(answer)
            keyword_values.append(sum(bool(_normalize(keyword) and _normalize(keyword) in normalized) for keyword in keywords) / len(keywords))
        metrics["reference_keyword_recall"] = _rate_from_values(
            state,
            keyword_values,
            eligible_n=len(keyword_values),
            missing_n=keyword_missing + max(0, state.planned_n - state.valid_n),
            reason_if_empty="UNSUPPORTED_KEYWORDS_UNAVAILABLE",
        )
        metrics["em"] = metrics["normalized_em"]
        metrics["exact_match"] = metrics["normalized_em"]
        metrics["answer_non_empty_rate"] = metrics["answer_non_empty"]
        metrics["answer_contains_gold_rate"] = metrics["contains_gold"]
        if dataset_id == "docbench":
            # This is deliberately labelled as a proxy: DocBench correctness
            # itself is judge-defined, while strict normalized EM is local and
            # deterministic.
            metrics["deterministic_correctness_proxy"] = metrics["normalized_em"]
            metrics["correctness_proxy"] = metrics["deterministic_correctness_proxy"]

    # These metrics require a frozen RAGAS/LLM judge or semantic scorer.  A
    # lexical overlap score is intentionally not substituted for them.
    for name in (
        "faithfulness",
        "answer_relevance",
        "answer_relevancy",
        "context_precision",
        "context_recall",
        "context_relevance",
        "ragas_faithfulness",
        "ragas_answer_relevance",
        "ragas_answer_relevancy",
        "ragas_context_precision",
        "ragas_context_recall",
        "ragas_context_relevance",
        "semantic_correctness",
        "semantic_similarity",
        "answer_similarity",
        "answer_correctness",
        "judge_answer_correctness",
        "judge_answer_relevance",
        "judge_faithfulness",
        "judge_completeness",
        "judge_overall_quality",
        "correctness",
        "completeness",
        "dataset_aggregate",
        "strict_unanswerable_success",
        "tdas",
    ):
        metrics.setdefault(name, _unsupported_record(state))
    qa_latency_values = [
        value
        for row in state.rows.values()
        if _status(row.get("status")) not in PENDING_STATUSES and (value := _latency_value(row, "qa")) is not None
    ]
    e2e_latency_values = [
        value
        for row in state.rows.values()
        if _status(row.get("status")) not in PENDING_STATUSES and (value := _latency_value(row, "e2e")) is not None
    ]
    metrics["latency_ms_p50"] = _latency_record(state, qa_latency_values, 0.50)
    metrics["latency_ms_p95"] = _latency_record(state, qa_latency_values, 0.95)
    metrics["generation_latency_ms_p50"] = metrics["latency_ms_p50"]
    metrics["generation_latency_ms_p95"] = metrics["latency_ms_p95"]
    metrics["e2e_latency_ms_p50"] = _latency_record(state, e2e_latency_values, 0.50)
    metrics["e2e_latency_ms_p95"] = _latency_record(state, e2e_latency_values, 0.95)
    metrics["generation_p50"] = metrics["latency_ms_p50"]
    metrics["generation_p95"] = metrics["latency_ms_p95"]
    metrics["e2e_p50"] = metrics["e2e_latency_ms_p50"]
    metrics["e2e_p95"] = metrics["e2e_latency_ms_p95"]
    return metrics


def _retrieval_metrics(state: StageState, package: PackageData, aliases: Mapping[str, set[str]]) -> dict[str, Any]:
    dataset_id = package.dataset_id
    metrics: dict[str, Any] = {}
    for k in TOP_K:
        values: list[float] = []
        hit_values: list[float] = []
        eligible = 0
        for key in state.units:
            row = state.rows.get(key)
            question = state.questions.get(key[0], {})
            if row is None:
                continue
            status = _status(row.get("status"))
            hits, trace = _extract_hits(row)
            if status in VALID_STATUSES and trace and hits is not None:
                recall, _, available = _rank_values(question, hits, aliases, k)
                if available:
                    values.append(recall)
                    hit_values.append(float(recall > 0.0))
                    eligible += 1
        record = _rate_from_values(state, values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible))
        hit_record = _rate_from_values(state, hit_values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible))
        metrics[f"recall_at_{k}"] = record
        metrics[f"r@{k}"] = record
        metrics[f"R@{k}"] = record
        metrics[f"source_recall_at_{k}"] = record
        metrics[f"source_recall@{k}"] = record
        metrics[f"source_r@{k}"] = record
        metrics[f"hit_at_{k}"] = hit_record
        metrics[f"hit@{k}"] = hit_record

    mrr_values: list[float] = []
    mrr_eligible = 0
    for key in state.units:
        row = state.rows.get(key)
        question = state.questions.get(key[0], {})
        if row is None:
            continue
        if _status(row.get("status")) not in VALID_STATUSES:
            continue
        hits, trace = _extract_hits(row)
        if not trace or hits is None:
            continue
        _, mrr, available = _rank_values(question, hits, aliases, 10)
        if available:
            mrr_values.append(mrr)
            mrr_eligible += 1
    metrics["mrr"] = _rate_from_values(state, mrr_values, eligible_n=mrr_eligible, missing_n=max(0, state.planned_n - mrr_eligible))

    if dataset_id in {"multihop-rag", "enterprise-rag-bench"}:
        for k in TOP_K:
            evidence_values: list[float] = []
            complete_values: list[float] = []
            eligible = 0
            for key in state.units:
                row = state.rows.get(key)
                question = state.questions.get(key[0], {})
                if row is None or _status(row.get("status")) not in VALID_STATUSES:
                    continue
                hits, trace = _extract_hits(row)
                if not trace or hits is None:
                    continue
                value, _, available = _rank_values(question, hits, aliases, k)
                if available:
                    evidence_values.append(value)
                    complete_values.append(float(value == 1.0))
                    eligible += 1
            metrics[f"evidence_recall_at_{k}"] = _rate_from_values(state, evidence_values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible))
            metrics[f"all_evidence_success_at_{k}"] = _rate_from_values(state, complete_values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible))
            metrics[f"complete_evidence_set_recall_at_{k}"] = metrics[f"all_evidence_success_at_{k}"]
            metrics[f"evidence_recall@{k}"] = metrics[f"evidence_recall_at_{k}"]
            metrics[f"all_evidence_success@{k}"] = metrics[f"all_evidence_success_at_{k}"]
            metrics[f"complete_evidence_set_recall@{k}"] = metrics[f"complete_evidence_set_recall_at_{k}"]
            if dataset_id == "enterprise-rag-bench":
                metrics[f"document_recall_at_{k}"] = metrics[f"recall_at_{k}"]
                metrics[f"document_recall@{k}"] = metrics[f"recall_at_{k}"]
                metrics[f"doc_recall_at_{k}"] = metrics[f"recall_at_{k}"]
                metrics[f"all_required_doc_success_at_{k}"] = metrics[f"all_evidence_success_at_{k}"]
                metrics[f"completeness_proxy_at_{k}"] = metrics[f"complete_evidence_set_recall_at_{k}"]
                metrics[f"completeness_proxy@{k}"] = metrics[f"complete_evidence_set_recall_at_{k}"]

    if dataset_id == "mmdocir":
        for k in TOP_K:
            page_values: list[float] = []
            page_mrr_values: list[float] = []
            page_eligible = 0
            layout_values: list[float] = []
            layout_eligible = 0
            for key in state.units:
                row = state.rows.get(key)
                question = state.questions.get(key[0], {})
                if row is None or _status(row.get("status")) not in VALID_STATUSES:
                    continue
                hits, trace = _extract_hits(row)
                if not trace or hits is None:
                    continue
                page_recall, page_mrr, page_available = _page_rank_values(question, hits, aliases, k)
                if page_available:
                    page_values.append(page_recall)
                    page_mrr_values.append(page_mrr)
                    page_eligible += 1
                if _layout_available(question, hits):
                    layout_recall, layout_available = _layout_rank_values(question, hits, k)
                    if layout_available:
                        layout_values.append(layout_recall)
                        layout_eligible += 1
            metrics[f"page_recall_at_{k}"] = _rate_from_values(state, page_values, eligible_n=page_eligible, missing_n=max(0, state.planned_n - page_eligible))
            metrics[f"page_r@{k}"] = metrics[f"page_recall_at_{k}"]
            metrics[f"page_recall@{k}"] = metrics[f"page_recall_at_{k}"]
            if k in {1, 5, 10}:
                metrics[f"layout_recall_at_{k}"] = _rate_from_values(state, layout_values, eligible_n=layout_eligible, missing_n=max(0, state.planned_n - layout_eligible), reason_if_empty="UNSUPPORTED_LAYOUT_LOCATORS_UNAVAILABLE")
                metrics[f"layout_recall@{k}"] = metrics[f"layout_recall_at_{k}"]
        # The first-relevant page rank is a separate metric, not the average of
        # page recall values.
        page_mrr_values: list[float] = []
        page_eligible = 0
        for key in state.units:
            row = state.rows.get(key)
            question = state.questions.get(key[0], {})
            if row is None or _status(row.get("status")) not in VALID_STATUSES:
                continue
            hits, trace = _extract_hits(row)
            if not trace or hits is None:
                continue
            _, value, available = _page_rank_values(question, hits, aliases, 10)
            if available:
                page_mrr_values.append(value)
                page_eligible += 1
        metrics["page_mrr"] = _rate_from_values(state, page_mrr_values, eligible_n=page_eligible, missing_n=max(0, state.planned_n - page_eligible), reason_if_empty="UNSUPPORTED_PAGE_LOCATORS_UNAVAILABLE")

    if dataset_id == "mmdocrag":
        for modality in ("text", "image"):
            for k in TOP_K:
                values: list[float] = []
                eligible = 0
                for key in state.units:
                    row = state.rows.get(key)
                    question = state.questions.get(key[0], {})
                    if row is None or _status(row.get("status")) not in VALID_STATUSES:
                        continue
                    hits, trace = _extract_hits(row)
                    if not trace or hits is None:
                        continue
                    value, available = _typed_recall(question, hits, modality, k, aliases)
                    if available:
                        values.append(value)
                        eligible += 1
                reason = "UNSUPPORTED_TYPED_EVIDENCE_UNAVAILABLE"
                metrics[f"{modality}_evidence_recall_at_{k}"] = _rate_from_values(state, values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible), reason_if_empty=reason)
                metrics[f"{modality}_quote_recall_at_{k}"] = metrics[f"{modality}_evidence_recall_at_{k}"]
                metrics[f"{modality}_evidence_recall@{k}"] = metrics[f"{modality}_evidence_recall_at_{k}"]
                metrics[f"{modality}_quote_recall@{k}"] = metrics[f"{modality}_quote_recall_at_{k}"]
        for k in TOP_K:
            values: list[float] = []
            eligible = 0
            for key in state.units:
                row = state.rows.get(key)
                question = state.questions.get(key[0], {})
                if row is None or _status(row.get("status")) not in VALID_STATUSES:
                    continue
                hits, trace = _extract_hits(row)
                typed = _typed_gold(question)
                if not trace or hits is None or typed is None:
                    continue
                total = sum(len(items) for items in typed.values())
                if not total:
                    continue
                per_modality = []
                available = True
                for modality in ("text", "image"):
                    value, ok = _typed_recall(question, hits, modality, k, aliases)
                    if typed[modality]:
                        if not ok:
                            available = False
                            break
                        per_modality.append(value * len(typed[modality]))
                if available:
                    values.append(sum(per_modality) / total)
                    eligible += 1
            metrics[f"overall_evidence_recall_at_{k}"] = _rate_from_values(state, values, eligible_n=eligible, missing_n=max(0, state.planned_n - eligible), reason_if_empty="UNSUPPORTED_TYPED_EVIDENCE_UNAVAILABLE")
            metrics[f"overall_evidence_recall@{k}"] = metrics[f"overall_evidence_recall_at_{k}"]

    latency_values = [
        value
        for row in state.rows.values()
        if _status(row.get("status")) not in PENDING_STATUSES and (value := _latency_value(row, "retrieval")) is not None
    ]
    metrics["latency_ms_p50"] = _latency_record(state, latency_values, 0.50)
    metrics["latency_ms_p95"] = _latency_record(state, latency_values, 0.95)
    metrics["retrieval_latency_ms_p50"] = metrics["latency_ms_p50"]
    metrics["retrieval_latency_ms_p95"] = metrics["latency_ms_p95"]
    metrics["retrieval_p50"] = metrics["latency_ms_p50"]
    metrics["retrieval_p95"] = metrics["latency_ms_p95"]
    return metrics


def _slice_state(state: StageState, label: str) -> StageState:
    units = [unit for unit in state.units if _question_type(state.questions.get(unit[0], {})) == label]
    unit_keys = set(units)
    rows = {key: row for key, row in state.rows.items() if key in unit_keys}
    counts: Counter[str] = Counter(_status(row.get("status")) for row in rows.values())
    terminal_n = sum(counts.get(value, 0) for value in counts if value not in PENDING_STATUSES)
    valid_n = sum(counts.get(value, 0) for value in VALID_STATUSES)
    failed_n = sum(counts.get(value, 0) for value in FAILED_STATUSES)
    unsupported_n = sum(counts.get(value, 0) for value in UNSUPPORTED_STATUSES)
    return StageState(
        name=state.name,
        units=units,
        rows=rows,
        questions=state.questions,
        planned_n=len(units),
        terminal_n=terminal_n,
        valid_n=valid_n,
        failed_n=failed_n,
        unsupported_n=unsupported_n,
        pending_n=max(0, len(units) - terminal_n),
        status_counts=dict(sorted(counts.items())),
    )


def _stage_result(stage: str, state: StageState, package: PackageData, aliases: Mapping[str, set[str]]) -> dict[str, Any]:
    metrics = _retrieval_metrics(state, package, aliases) if stage == "retrieval" else _qa_metrics(state, package.dataset_id, aliases)
    result: dict[str, Any] = {
        "stage": stage,
        "denominator": state.denominator,
        "status_counts": state.status_counts,
        "metrics": metrics,
        "slices": {},
    }
    labels = sorted(
        {
            _question_type(state.questions[question_id])
            for question_id, _ in state.units
            if question_id in state.questions
        }
    )
    for label in labels:
        subset = _slice_state(state, label)
        subset_metrics = _retrieval_metrics(subset, package, aliases) if stage == "retrieval" else _qa_metrics(subset, package.dataset_id, aliases)
        result["slices"][label] = {
            "denominator": subset.denominator,
            "status_counts": subset.status_counts,
            "metrics": subset_metrics,
        }
    if package.dataset_id == "docbench" and stage == "qa":
        # DocBench's native correctness requires a judge.  Keep that metric
        # null above, and expose strict lexical Gold matches only as an
        # explicitly named diagnostic proxy.
        result["metrics"]["deterministic_correctness_proxy"] = result["metrics"].get("normalized_em", _unsupported_record(state, UNSUPPORTED_GOLD_UNAVAILABLE))
        result["metrics"]["correctness_proxy"] = result["metrics"]["deterministic_correctness_proxy"]
        result["correctness_proxy_by_question_type"] = {
            label: {
                metric_name: result["slices"][label]["metrics"].get(metric_name)
                for metric_name in (
                    "normalized_em",
                    "contains_gold",
                    "token_f1",
                    "answer_non_empty",
                    "deterministic_correctness_proxy",
                )
            }
            for label in labels
        }
        result["type_correctness_proxy"] = result["correctness_proxy_by_question_type"]
    # A short values view is convenient for report/table adapters and does not
    # replace the auditable metric records above.
    result["values"] = {name: record.get("value") for name, record in metrics.items() if isinstance(record, Mapping) and "value" in record}
    return result


def _state_for_units(state: StageState, units: Sequence[tuple[str, int]]) -> StageState:
    """Create a denominator-preserving view for one mixed-benchmark slice."""

    selected_units = list(units)
    selected_keys = set(selected_units)
    rows = {key: row for key, row in state.rows.items() if key in selected_keys}
    counts: Counter[str] = Counter(_status(row.get("status")) for row in rows.values())
    terminal_n = sum(counts.get(value, 0) for value in counts if value not in PENDING_STATUSES)
    valid_n = sum(counts.get(value, 0) for value in VALID_STATUSES)
    failed_n = sum(counts.get(value, 0) for value in FAILED_STATUSES)
    unsupported_n = sum(counts.get(value, 0) for value in UNSUPPORTED_STATUSES)
    return StageState(
        name=state.name,
        units=selected_units,
        rows=rows,
        questions=state.questions,
        planned_n=len(selected_units),
        terminal_n=terminal_n,
        valid_n=valid_n,
        failed_n=failed_n,
        unsupported_n=unsupported_n,
        pending_n=max(0, len(selected_units) - terminal_n),
        status_counts=dict(sorted(counts.items())),
    )


def _mixed_source_for_unit(
    retrieval_state: StageState,
    qa_state: StageState,
    key: tuple[str, int],
) -> str:
    question = retrieval_state.questions.get(key[0], qa_state.questions.get(key[0], {}))
    return source_for_row(retrieval_state.rows.get(key) or qa_state.rows.get(key), question)


def _mixed_rows_for_state(state: StageState) -> list[tuple[tuple[str, int], dict[str, Any], dict[str, Any]]]:
    result: list[tuple[tuple[str, int], dict[str, Any], dict[str, Any]]] = []
    for key in state.units:
        result.append((key, state.questions.get(key[0], {}), state.rows.get(key) or {}))
    return result


def _mixed_metric(
    metric_id: str,
    values: Sequence[float],
    denominator: int,
    *,
    eligible_n: int,
    missing_n: int,
    failed_n: int,
    na_reason: str | None = None,
    unit: str | None = None,
    aggregation: str = "planned_initial_denominator",
    protocol_label: str | None = None,
) -> dict[str, Any]:
    numerator = sum(float(value) for value in values)
    if denominator <= 0:
        return na_record(metric_id, "NO_APPLICABLE_ATTEMPTS", denominator=denominator, eligible_n=eligible_n, missing_n=missing_n, failed_n=failed_n, numerator=numerator, unit=unit, aggregation=aggregation, protocol_label=protocol_label)
    if na_reason:
        return na_record(metric_id, na_reason, denominator=denominator, eligible_n=eligible_n, missing_n=missing_n, failed_n=failed_n, numerator=numerator, unit=unit, aggregation=aggregation, protocol_label=protocol_label)
    if missing_n:
        return na_record(metric_id, "INCOMPLETE_PLANNED_DENOMINATOR", denominator=denominator, eligible_n=eligible_n, missing_n=missing_n, failed_n=failed_n, numerator=numerator, unit=unit, aggregation=aggregation, protocol_label=protocol_label)
    return metric_record(
        metric_id,
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        eligible_n=eligible_n,
        missing_n=missing_n,
        failed_n=failed_n,
        unit=unit,
        aggregation=aggregation,
        protocol_label=protocol_label,
    )


def _mixed_percentile_metric(
    metric_id: str,
    values: Sequence[float],
    fraction: float,
    *,
    planned_n: int,
    failed_n: int,
    na_reason: str | None = None,
) -> dict[str, Any]:
    if na_reason:
        return na_record(metric_id, na_reason, denominator=planned_n, eligible_n=0, missing_n=max(0, planned_n - failed_n), failed_n=failed_n, unit="milliseconds", aggregation="not_applicable")
    value = registry_percentile(values, fraction)
    if value is None:
        return na_record(metric_id, "NO_LATENCY_OBSERVATIONS", denominator=planned_n, eligible_n=0, missing_n=max(0, planned_n - failed_n), failed_n=failed_n, unit="milliseconds", aggregation="observed_terminal_latencies; nearest_rank_percentile")
    return metric_record(
        metric_id,
        value=value,
        numerator=value,
        denominator=planned_n,
        eligible_n=len(values),
        missing_n=max(0, planned_n - len(values) - failed_n),
        failed_n=failed_n,
        unit="milliseconds",
        aggregation="observed_terminal_latencies; nearest_rank_percentile",
    )


def _mixed_answer_from_row(row: Mapping[str, Any]) -> str | None:
    answer = _answer_from_row(row)
    return answer if answer is not None else None


def _mixed_refusal(answer: str | None) -> bool:
    if not answer:
        return False
    return bool(
        re.search(
            r"(?:\b(?:cannot|can't|unable|insufficient|not enough|no (?:relevant )?information|not provided|cannot determine|unknown|not found)\b|无法(?:确定|判断|回答|确认)?|不能(?:确定|判断|回答|确认)|信息不足|没有足够|未提供|无相关信息|未找到)",
            answer,
            re.IGNORECASE,
        )
    )


def _mixed_gold_ids(question: Mapping[str, Any]) -> list[str]:
    return _gold_doc_ids(question)


def _mixed_ranked_values(
    question: Mapping[str, Any],
    hits: Sequence[Any],
    aliases: Mapping[str, set[str]],
    k: int,
) -> dict[str, Any] | None:
    gold_ids = _mixed_gold_ids(question)
    if not gold_ids:
        return None
    ordered = _ordered_hits(hits)[:k]
    matched: set[int] = set()
    ranks: list[int] = []
    for rank, hit in enumerate(ordered, 1):
        for index, gold_id in enumerate(gold_ids):
            if index in matched:
                continue
            if _row_matches_gold_ids(hit, [gold_id], aliases):
                matched.add(index)
                ranks.append(rank)
    recall = len(matched) / len(gold_ids)
    hit = float(bool(matched))
    mrr = 1.0 / min(ranks) if ranks else 0.0
    average_precision = sum((index + 1) / rank for index, rank in enumerate(sorted(ranks))) / len(gold_ids)
    dcg = sum(1.0 / math.log2(rank + 1) for rank in ranks)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(gold_ids), k) + 1))
    return {
        "recall": recall,
        "hit": hit,
        "precision": len(matched) / len(ordered) if ordered else 0.0,
        "mrr": mrr,
        "map": average_precision,
        "ndcg": dcg / ideal if ideal else 0.0,
        "complete": float(len(matched) == len(gold_ids)),
        "matched": len(matched),
        "gold_count": len(gold_ids),
        "ranks": ranks,
    }


def _mixed_has_graded_qrels(question: Mapping[str, Any], hits: Sequence[Any]) -> bool:
    for key in ("graded_qrels", "qrels", "relevance_grades", "gold_relevance"):
        if question.get(key) not in (None, [], {}):
            return True
    for hit in hits:
        for mapping in _iter_nested_mappings(hit):
            if any(key in mapping for key in ("relevance", "relevance_grade", "grade", "qrel")):
                return True
    return False


def _mixed_evidence_sets(question: Mapping[str, Any]) -> list[list[str]]:
    raw = normalize_structured_gold(question).get("evidence_sets")
    if raw in (None, [], {}):
        return []
    result: list[list[str]] = []
    for candidate in _as_list(raw):
        if isinstance(candidate, Mapping):
            candidate = _first(candidate, "evidence", "items", "doc_ids", "document_ids", "ids", default=[])
        ids = _as_ids(candidate)
        if ids:
            result.append(ids)
    return result


def _mixed_structured_complete_value(
    question: Mapping[str, Any],
    hits: Sequence[Any],
    aliases: Mapping[str, set[str]],
    k: int,
) -> float | None:
    evidence_sets = _mixed_evidence_sets(question)
    if not evidence_sets:
        return None
    top_hits = _ordered_hits(hits)[:k]
    return float(
        any(
            all(any(_row_matches_gold_ids(hit, [evidence_id], aliases) for hit in top_hits) for evidence_id in evidence_set)
            for evidence_set in evidence_sets
        )
    )


def _mixed_ndcg_value(question: Mapping[str, Any], hits: Sequence[Any], k: int) -> float | None:
    raw_qrels: Any = None
    for key in ("graded_qrels", "qrels", "relevance_grades", "gold_relevance"):
        if question.get(key) not in (None, [], {}):
            raw_qrels = question[key]
            break
    qrels: dict[str, float] = {}
    if isinstance(raw_qrels, Mapping):
        for key, value in raw_qrels.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                qrels.update({variant: float(value) for variant in _id_variants(key)})
    elif isinstance(raw_qrels, list):
        for item in raw_qrels:
            if not isinstance(item, Mapping):
                continue
            identifier = _first(item, "document_id", "doc_id", "id", "source_id", default=None)
            grade = _first(item, "grade", "relevance", "score", default=None)
            if identifier is not None and isinstance(grade, (int, float)) and not isinstance(grade, bool):
                qrels.update({variant: float(grade) for variant in _id_variants(identifier)})
    if not qrels:
        return None
    gains: list[float] = []
    for rank, hit in enumerate(_ordered_hits(hits)[:k], 1):
        grade = max((qrels.get(identifier, 0.0) for identifier in _hit_ids(hit)), default=0.0)
        gains.append((2.0**grade - 1.0) / math.log2(rank + 1))
    ideal_grades = sorted(qrels.values(), reverse=True)[:k]
    ideal = sum((2.0**grade - 1.0) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, 1))
    return sum(gains) / ideal if ideal else 0.0


def _mixed_public_retrieval_supported(run_manifest: Mapping[str, Any]) -> bool:
    declared = _first(run_manifest, "public_retrieval_supported", "direct_retrieval_supported", default=None)
    if declared is False or str(declared).casefold() in {"false", "unsupported", "n/a", "na"}:
        return False
    system = str(_first(run_manifest, "system_id", "system", "platform", "runner", default="") or "").casefold()
    return "maxkb" not in system


def _mixed_retrieval_metrics(
    state: StageState,
    package: PackageData,
    aliases: Mapping[str, set[str]],
    *,
    public_retrieval_supported: bool = True,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    applicable_keys = [key for key, question, _ in _mixed_rows_for_state(state) if _mixed_gold_ids(question)]
    failed_applicable = sum(_status(state.rows.get(key, {}).get("status")) in FAILED_STATUSES for key in applicable_keys)
    if not public_retrieval_supported:
        for k in TOP_K:
            for name in (f"recall_at_{k}", f"hit_at_{k}", f"precision_at_{k}"):
                metrics[name] = na_record(name, "UNSUPPORTED_API", denominator=len(applicable_keys) or state.planned_n, missing_n=max(0, len(applicable_keys) - failed_applicable), failed_n=failed_applicable)
        for name in ("mrr", "map_at_10", *(f"ndcg_at_{k}" for k in TOP_K)):
            metrics[name] = na_record(name, "UNSUPPORTED_API", denominator=len(applicable_keys) or state.planned_n, missing_n=max(0, len(applicable_keys) - failed_applicable), failed_n=failed_applicable)
        for k in TOP_K:
            metrics[f"complete_evidence_set_recall_at_{k}"] = na_record(
                f"complete_evidence_set_recall_at_{k}",
                "UNSUPPORTED_API",
                denominator=len(applicable_keys) or state.planned_n,
                missing_n=max(0, len(applicable_keys) - failed_applicable),
                failed_n=failed_applicable,
            )
            metrics[f"adapted_complete_evidence_set_recall_at_{k}"] = metrics[f"complete_evidence_set_recall_at_{k}"]
        metrics["invalid_extra_rate_at_10"] = na_record("invalid_extra_rate_at_10", "UNSUPPORTED_API", denominator=state.planned_n, missing_n=max(0, state.planned_n - failed_applicable), failed_n=failed_applicable)
        metrics["invalid_extra_docs_at_10"] = metrics["invalid_extra_rate_at_10"]
        return metrics

    for k in TOP_K:
        recall_values: list[float] = []
        hit_values: list[float] = []
        precision_values: list[float] = []
        complete_values: list[float] = []
        structured_complete_values: list[float] = []
        eligible = 0
        missing = 0
        structured_keys = [key for key in applicable_keys if _mixed_evidence_sets(state.questions.get(key[0], {}))]
        structured_failed = sum(_status(state.rows.get(key, {}).get("status")) in FAILED_STATUSES for key in structured_keys)
        structured_missing = 0
        for key in applicable_keys:
            question = state.questions.get(key[0], {})
            row = state.rows.get(key)
            if row is None:
                missing += 1
                continue
            status = _status(row.get("status"))
            if status in FAILED_STATUSES | UNSUPPORTED_STATUSES:
                continue
            if status in PENDING_STATUSES:
                missing += 1
                continue
            hits, trace = _extract_hits(row)
            if not trace or hits is None:
                missing += 1
                continue
            score = _mixed_ranked_values(question, hits, aliases, k)
            if score is None:
                missing += 1
                continue
            eligible += 1
            recall_values.append(score["recall"])
            hit_values.append(score["hit"])
            precision_values.append(score["precision"])
            complete_values.append(score["complete"])
            if key in structured_keys:
                structured_value = _mixed_structured_complete_value(question, hits, aliases, k)
                if structured_value is None:
                    structured_missing += 1
                else:
                    structured_complete_values.append(structured_value)
        denominator = len(applicable_keys)
        metrics[f"recall_at_{k}"] = _mixed_metric(f"recall_at_{k}", recall_values, denominator, eligible_n=eligible, missing_n=missing, failed_n=failed_applicable)
        metrics[f"hit_at_{k}"] = _mixed_metric(f"hit_at_{k}", hit_values, denominator, eligible_n=eligible, missing_n=missing, failed_n=failed_applicable)
        metrics[f"precision_at_{k}"] = _mixed_metric(f"precision_at_{k}", precision_values, denominator, eligible_n=eligible, missing_n=missing, failed_n=failed_applicable)
        if not structured_keys:
            # The common Gold currently has one document/evidence list, not
            # the structured alternative-set contract.  Keep that distinction
            # explicit instead of silently upgrading the adapted calculation.
            metrics[f"complete_evidence_set_recall_at_{k}"] = na_record(
                f"complete_evidence_set_recall_at_{k}",
                MISSING_STRUCTURED_CLAIM_GOLD,
                denominator=denominator,
                eligible_n=0,
                missing_n=denominator,
                failed_n=failed_applicable,
            )
        else:
            metrics[f"complete_evidence_set_recall_at_{k}"] = _mixed_metric(
                f"complete_evidence_set_recall_at_{k}",
                structured_complete_values,
                len(structured_keys),
                eligible_n=len(structured_complete_values),
                missing_n=structured_missing,
                failed_n=structured_failed,
                protocol_label="MOI_UNIFIED_STRUCTURED_EVIDENCE_SETS",
            )
        metrics[f"adapted_complete_evidence_set_recall_at_{k}"] = _mixed_metric(
            f"adapted_complete_evidence_set_recall_at_{k}",
            complete_values,
            denominator,
            eligible_n=eligible,
            missing_n=missing,
            failed_n=failed_applicable,
            protocol_label="ADAPTED_GOLD_DOC_OR_EVIDENCE_SET",
        )

    mrr_values: list[float] = []
    map_values: list[float] = []
    ndcg_values_by_k: dict[int, list[float]] = {k: [] for k in TOP_K}
    mrr_eligible = 0
    map_eligible = 0
    ndcg_eligible_by_k: dict[int, int] = {k: 0 for k in TOP_K}
    for key in applicable_keys:
        question = state.questions.get(key[0], {})
        row = state.rows.get(key)
        if row is None or _status(row.get("status")) in FAILED_STATUSES | UNSUPPORTED_STATUSES:
            continue
        hits, trace = _extract_hits(row)
        if not trace or hits is None:
            continue
        score = _mixed_ranked_values(question, hits, aliases, 10)
        if score is None:
            continue
        mrr_values.append(score["mrr"])
        map_values.append(score["map"])
        mrr_eligible += 1
        map_eligible += 1
        if _mixed_has_graded_qrels(question, hits):
            for k in TOP_K:
                ndcg_value = _mixed_ndcg_value(question, hits, k)
                if ndcg_value is not None:
                    ndcg_values_by_k[k].append(ndcg_value)
                    ndcg_eligible_by_k[k] += 1
    metrics["mrr"] = _mixed_metric("mrr", mrr_values, len(applicable_keys), eligible_n=mrr_eligible, missing_n=max(0, len(applicable_keys) - mrr_eligible - failed_applicable), failed_n=failed_applicable)
    metrics["map_at_10"] = _mixed_metric("map_at_10", map_values, len(applicable_keys), eligible_n=map_eligible, missing_n=max(0, len(applicable_keys) - map_eligible - failed_applicable), failed_n=failed_applicable)
    for k in TOP_K:
        ndcg_eligible = ndcg_eligible_by_k[k]
        ndcg_values = ndcg_values_by_k[k]
        metrics[f"ndcg_at_{k}"] = na_record(
            f"ndcg_at_{k}",
            "MISSING_GRADED_QRELS" if ndcg_eligible == 0 else "INCOMPLETE_GRADED_QRELS",
            denominator=len(applicable_keys),
            eligible_n=ndcg_eligible,
            missing_n=max(0, len(applicable_keys) - ndcg_eligible - failed_applicable),
            failed_n=failed_applicable,
            numerator=sum(ndcg_values),
        )

    invalid_values: list[float] = []
    invalid_eligible = 0
    invalid_missing = 0
    invalid_failed = 0
    invalid_contract = False
    for key, question, row in _mixed_rows_for_state(state):
        valid_ids = _mixed_gold_ids(question)
        if not valid_ids:
            continue
        if question.get("valid_doc_ids") not in (None, [], {}):
            valid_ids = _as_ids(question["valid_doc_ids"])
        if not valid_ids:
            continue
        invalid_contract = True
        status = _status(row.get("status")) if row else "MISSING"
        if status in FAILED_STATUSES:
            invalid_failed += 1
            continue
        hits, trace = _extract_hits(row) if row else (None, False)
        if not trace or hits is None:
            invalid_missing += 1
            continue
        returned = _ordered_hits(hits)[:10]
        if not returned:
            invalid_values.append(0.0)
        else:
            invalid_values.append(sum(not _row_matches_gold_ids(hit, valid_ids, aliases) for hit in returned) / len(returned))
        invalid_eligible += 1
    if not invalid_contract:
        metrics["invalid_extra_rate_at_10"] = na_record("invalid_extra_rate_at_10", "NO_INVALID_EXTRA_GOLD_CONTRACT", denominator=0, missing_n=0, failed_n=0)
    else:
        metrics["invalid_extra_rate_at_10"] = _mixed_metric(
            "invalid_extra_rate_at_10",
            invalid_values,
            invalid_eligible + invalid_failed + invalid_missing,
            eligible_n=invalid_eligible,
            missing_n=invalid_missing,
            failed_n=invalid_failed,
            aggregation="micro_invalid_returned_items_at_10",
        )
    metrics["invalid_extra_docs_at_10"] = metrics["invalid_extra_rate_at_10"]
    return metrics


def _mixed_qa_metrics(state: StageState) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    answer_values: list[float] = []
    answer_eligible = 0
    answer_missing = 0
    answer_failed = 0
    lexical_values: dict[str, list[float]] = {"normalized_em": [], "token_f1": [], "contains_gold": []}
    lexical_eligible = {name: 0 for name in lexical_values}
    lexical_missing = {name: 0 for name in lexical_values}
    lexical_failed = {name: 0 for name in lexical_values}
    for key, question, row in _mixed_rows_for_state(state):
        status = _status(row.get("status")) if row else "MISSING"
        answer = _mixed_answer_from_row(row) if row else None
        if answer is None and status == "EMPTY":
            answer = ""
        if status in FAILED_STATUSES:
            answer_failed += 1
        elif status in VALID_STATUSES and answer is not None:
            answer_values.append(float(bool(answer.strip())))
            answer_eligible += 1
        else:
            answer_missing += 1

        gold = _gold_answer(question, row)
        normalized_gold = normalize_text(gold) if gold else ""
        if not normalized_gold:
            continue
        for name in lexical_values:
            lexical_eligible[name] += 1
            if status in FAILED_STATUSES:
                lexical_failed[name] += 1
                continue
            if status not in VALID_STATUSES or answer is None:
                lexical_missing[name] += 1
                continue
            normalized_answer = normalize_text(answer)
            if name == "normalized_em":
                value = float(normalized_answer == normalized_gold)
            elif name == "contains_gold":
                value = float(normalized_gold in normalized_answer)
            else:
                f1 = registry_token_f1(answer, gold)
                value = 0.0 if f1 is None else f1
            lexical_values[name].append(value)

    metrics["answer_non_empty"] = _mixed_metric(
        "answer_non_empty",
        answer_values,
        state.planned_n,
        eligible_n=answer_eligible,
        missing_n=answer_missing,
        failed_n=answer_failed,
    )
    # Lexical metrics are deterministic diagnostics.  A row with empty Gold
    # (even when it has evidence) is missing Gold for the lexical denominator;
    # a failed row with non-empty Gold remains a zero in that denominator.
    for name, values in lexical_values.items():
        metrics[name] = _mixed_metric(
            name,
            values,
            lexical_eligible[name],
            eligible_n=lexical_eligible[name],
            missing_n=lexical_missing[name],
            failed_n=lexical_failed[name],
        )
    metrics["em"] = metrics["normalized_em"]
    metrics["f1"] = metrics["token_f1"]
    metrics["contains_gold_rate"] = metrics["contains_gold"]
    metrics["adapted_em"] = metrics["normalized_em"]
    metrics["adapted_f1"] = metrics["token_f1"]
    metrics["adapted_contains_gold"] = metrics["contains_gold"]

    answerable_keys = [key for key, question, _ in _mixed_rows_for_state(state) if answerability_label(question) == "answerable"]
    false_refusal_values: list[float] = []
    false_refusal_failed = 0
    false_refusal_missing = 0
    for key in answerable_keys:
        row = state.rows.get(key) or {}
        status = _status(row.get("status"))
        if status in FAILED_STATUSES:
            false_refusal_failed += 1
            false_refusal_values.append(0.0)
            continue
        answer = _mixed_answer_from_row(row)
        if status not in VALID_STATUSES or answer is None:
            false_refusal_missing += 1
            continue
        false_refusal_values.append(float(_mixed_refusal(answer)))
    metrics["false_refusal_rate"] = _mixed_metric(
        "false_refusal_rate",
        false_refusal_values,
        len(answerable_keys),
        eligible_n=len(false_refusal_values),
        missing_n=false_refusal_missing,
        failed_n=false_refusal_failed,
        aggregation="answerable_question_macro; refusal_pattern_diagnostic",
    )
    return metrics


def _judge_rows(run_root: Path) -> list[dict[str, Any]]:
    candidates = (
        run_root / "judge-terminal-ledger.jsonl",
        run_root / "judge" / "judge-terminal-ledger.jsonl",
        run_root / "judge" / "terminal-ledger.jsonl",
    )
    rows: list[dict[str, Any]] = []
    for path in candidates:
        if path.is_file():
            rows.extend(_jsonl_load(path))
    return rows


_CANONICAL_JUDGE_DIMENSIONS = {
    "claim_correctness": ("claim_correctness",),
    "reference_claim_recall": ("reference_claim_recall",),
    "critical_claim_coverage": ("critical_claim_coverage",),
    "gold_evidence_support": ("gold_evidence_support",),
    "grounding": ("grounding",),
    "tdas": ("tdas",),
}

_ADAPTED_JUDGE_DIMENSIONS = {
    "response_claim_correctness": ("response_claim_correctness",),
    "reference_claim_recall_adapted": ("reference_claim_recall_adapted", "reference_claim_recall"),
    "critical_claim_coverage_adapted": ("critical_claim_coverage_adapted", "critical_claim_coverage"),
    "gold_evidence_support_adapted": ("gold_evidence_support_adapted", "gold_evidence_support"),
    "runtime_context_faithfulness": ("runtime_context_faithfulness", "actual_context_faithfulness"),
    "strict_unanswerable_success": ("strict_unanswerable_success", "strict_unanswerable", "strict_refusal", "strict_refusal_success"),
    "info_not_found_success_adapted": ("info_not_found_success_adapted", "info_not_found_success", "enterprise_info_not_found_success"),
    "strict_unanswerable": ("strict_unanswerable", "strict_refusal"),
    "false_refusal": ("false_refusal",),
    "answer_relevance": ("answer_relevance",),
    "instruction_compliance": ("instruction_compliance",),
    "contradiction_free": ("contradiction_free",),
    "unsupported_claim_rate": ("unsupported_claim_rate",),
}

_ANSWERABLE_ONLY_ADAPTED_JUDGE_DIMENSIONS = frozenset(
    {
        "response_claim_correctness",
        "reference_claim_recall_adapted",
        "critical_claim_coverage_adapted",
        "gold_evidence_support_adapted",
        "runtime_context_faithfulness",
    }
)


def _judge_dimension_value(row: Mapping[str, Any], name: str) -> float | None:
    judgement = row.get("judgement", row.get("judgment", row.get("judge", row)))
    if not isinstance(judgement, Mapping):
        return None
    dimensions = judgement.get("dimensions", judgement.get("metrics", judgement))
    if not isinstance(dimensions, Mapping):
        return None
    candidates = _CANONICAL_JUDGE_DIMENSIONS.get(name) or _ADAPTED_JUDGE_DIMENSIONS.get(name) or (name,)
    for candidate in candidates:
        item = dimensions.get(candidate)
        if isinstance(item, Mapping):
            if item.get("supported") is False:
                correctness = dimensions.get("response_claim_correctness")
                if (
                    name == "runtime_context_faithfulness"
                    and row.get("context_available") is True
                    and isinstance(correctness, Mapping)
                    and correctness.get("supported") is True
                    and isinstance(correctness.get("score"), (int, float))
                    and not isinstance(correctness.get("score"), bool)
                    and math.isfinite(float(correctness["score"]))
                    and float(correctness["score"]) > 0
                ):
                    return 0.0
                return None
            value = item.get("score", item.get("value"))
        else:
            value = item
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value)
    return None


def _judge_metric(
    state: StageState,
    rows_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    metric_id: str,
    *,
    keys: Sequence[tuple[str, int]] | None = None,
    protocol_label: str | None = None,
) -> dict[str, Any]:
    selected = list(keys if keys is not None else state.units)
    values: list[float] = []
    failed = 0
    missing = 0
    for key in selected:
        row = rows_by_key.get(key)
        if row is None:
            missing += 1
            continue
        status = _status(row.get("status"))
        if status in FAILED_STATUSES:
            failed += 1
            continue
        value = _judge_dimension_value(row, metric_id)
        if value is None:
            missing += 1
            continue
        values.append(value)
    if missing:
        return na_record(
            metric_id,
            INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS,
            denominator=len(selected),
            eligible_n=len(values),
            missing_n=missing,
            failed_n=failed,
            protocol_label=protocol_label,
            planned_n=len(selected),
            observed_n=len(values),
        )
    if not values:
        return na_record(
            metric_id,
            "UNSUPPORTED_NEEDS_JUDGE",
            denominator=len(selected),
            eligible_n=0,
            missing_n=missing,
            failed_n=failed,
            protocol_label=protocol_label,
            planned_n=len(selected),
            observed_n=0,
        )
    return metric_record(
        metric_id,
        value=sum(values) / len(selected) if selected else None,
        numerator=sum(values),
        denominator=len(selected),
        eligible_n=len(values),
        missing_n=missing,
        failed_n=failed,
        aggregation="judge_score_sum_over_planned_initial_denominator",
        protocol_label=protocol_label or "JUDGE_ROWS_MERGED",
        planned_n=len(selected),
        observed_n=len(values),
    )


def _mixed_judge_metrics(
    state: StageState,
    judge_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    latest: dict[tuple[str, int], Mapping[str, Any]] = {}
    for row in judge_rows:
        qid = _row_question_id(row)
        if not qid:
            continue
        latest[(qid, _repeat_id(row))] = row
    answerable = [key for key in state.units if answerability_label(state.questions.get(key[0], {})) == "answerable"]
    metrics = {}
    for name, source_name in (
            ("response_claim_correctness", "response_claim_correctness"),
            ("reference_claim_recall_adapted", "reference_claim_recall_adapted"),
            ("critical_claim_coverage_adapted", "critical_claim_coverage_adapted"),
            ("gold_evidence_support_adapted", "gold_evidence_support_adapted"),
            ("runtime_context_faithfulness", "runtime_context_faithfulness"),
            ("answer_relevance", "answer_relevance"),
            ("instruction_compliance", "instruction_compliance"),
            ("contradiction_free", "contradiction_free"),
            ("unsupported_claim_rate", "unsupported_claim_rate"),
    ):
        metrics[name] = _judge_metric(
            state,
            latest,
            source_name,
            keys=answerable if name in _ANSWERABLE_ONLY_ADAPTED_JUDGE_DIMENSIONS else None,
            protocol_label="ADAPTED_REFERENCE_RUBRIC",
        )
    unanswerable = [key for key in state.units if answerability_label(state.questions.get(key[0], {})) == "unanswerable"]
    info_not_found = [key for key in state.units if answerability_label(state.questions.get(key[0], {})) == "info_not_found"]
    metrics["strict_unanswerable_success"] = _judge_metric(
        state,
        latest,
        "strict_unanswerable_success",
        keys=unanswerable,
        protocol_label="ADAPTED_REFERENCE_RUBRIC",
    )
    metrics["info_not_found_success_adapted"] = _judge_metric(
        state,
        latest,
        "info_not_found_success_adapted",
        keys=info_not_found,
        protocol_label="ADAPTED_REFERENCE_RUBRIC",
    )
    structured_complete = structured_claim_gold_available(
        state.questions.get(key[0], {}) for key in state.units
    )
    for name in _CANONICAL_JUDGE_DIMENSIONS:
        if not structured_complete:
            metrics[name] = na_record(
                name,
                MISSING_STRUCTURED_CLAIM_GOLD,
                denominator=state.planned_n,
                eligible_n=0,
                missing_n=state.planned_n,
                protocol_label="CANONICAL_STRUCTURED_GOLD",
                planned_n=state.planned_n,
                observed_n=0,
            )
        else:
            metrics[name] = _judge_metric(
                state,
                latest,
                name,
                protocol_label="CANONICAL_STRUCTURED_GOLD",
            )
    critical_keys = [key for key in state.units if answerability_label(state.questions.get(key[0], {})) == "answerable"]
    if not structured_complete:
        metrics["critical_contradiction_rate"] = na_record(
            "critical_contradiction_rate",
            MISSING_STRUCTURED_CLAIM_GOLD,
            denominator=len(critical_keys),
            missing_n=len(critical_keys),
            protocol_label="CANONICAL_STRUCTURED_GOLD",
            planned_n=len(critical_keys),
            observed_n=0,
        )
    else:
        contradiction_values: list[float] = []
        contradiction_missing = 0
        contradiction_failed = 0
        for key in critical_keys:
            row = latest.get(key)
            status = _status(row.get("status")) if row else "MISSING"
            if status in FAILED_STATUSES:
                contradiction_failed += 1
                continue
            value = _judge_dimension_value(row, "contradiction_free") if row else None
            if value is None:
                contradiction_missing += 1
                continue
            contradiction_values.append(max(0.0, min(1.0, 1.0 - value)))
        metrics["critical_contradiction_rate"] = _mixed_metric(
            "critical_contradiction_rate",
            contradiction_values,
            len(critical_keys),
            eligible_n=len(contradiction_values),
            missing_n=contradiction_missing,
            failed_n=contradiction_failed,
            aggregation="answerable_question_macro_complement_of_contradiction_free_judge",
            protocol_label="CRITICAL_CONTRADICTION_DERIVED_FROM_JUDGE",
        )
    return metrics


def _mixed_refusal_metric(state: StageState, metric_id: str, label: str) -> dict[str, Any]:
    keys = [key for key in state.units if answerability_label(state.questions.get(key[0], {})) == label]
    values: list[float] = []
    failed = 0
    missing = 0
    for key in keys:
        row = state.rows.get(key) or {}
        status = _status(row.get("status"))
        if status in FAILED_STATUSES:
            failed += 1
            continue
        answer = _mixed_answer_from_row(row)
        if status not in VALID_STATUSES or answer is None:
            missing += 1
            continue
        values.append(float(_mixed_refusal(answer)))
    return _mixed_metric(
        metric_id,
        values,
        len(keys),
        eligible_n=len(values),
        missing_n=missing,
        failed_n=failed,
        aggregation="adapted_refusal_pattern_over_initial_denominator",
        protocol_label="ADAPTED_REFUSAL_PATTERN",
    )


def _normalize_legacy_stage_records(stage_result: dict[str, Any]) -> None:
    for name, record in list(stage_result.get("metrics", {}).items()):
        if isinstance(record, Mapping) and "value" in record:
            stage_result["metrics"][name] = normalize_legacy_record(name, record)
    for slice_result in stage_result.get("slices", {}).values():
        if not isinstance(slice_result, Mapping):
            continue
        slice_metrics = slice_result.get("metrics")
        if not isinstance(slice_metrics, dict):
            continue
        for name, record in list(slice_metrics.items()):
            if isinstance(record, Mapping) and "value" in record:
                slice_metrics[name] = normalize_legacy_record(name, record)


def _mixed_latency_metrics(
    retrieval_state: StageState,
    qa_state: StageState,
    *,
    public_retrieval_supported: bool = True,
) -> dict[str, Any]:
    def values_for(state: StageState, stage: str) -> list[float]:
        return [
            value
            for row in state.rows.values()
            if _status(row.get("status")) not in PENDING_STATUSES and (value := _latency_value(row, stage)) is not None
        ]

    retrieval_values = values_for(retrieval_state, "retrieval") if public_retrieval_supported else []
    qa_values = values_for(qa_state, "qa")
    e2e_values: list[float] = []
    for key in qa_state.units:
        retrieval_row = retrieval_state.rows.get(key)
        qa_row = qa_state.rows.get(key)
        if retrieval_row is None or qa_row is None:
            continue
        retrieval_value = _latency_value(retrieval_row, "retrieval")
        qa_value = _latency_value(qa_row, "qa")
        if retrieval_value is not None and qa_value is not None:
            e2e_values.append(retrieval_value + qa_value)
    result: dict[str, Any] = {"retrieval": {}, "qa": {}, "e2e": {}}
    for stage, values, state in (
        ("retrieval", retrieval_values, retrieval_state),
        ("qa", qa_values, qa_state),
        ("e2e", e2e_values, qa_state),
    ):
        for label, fraction in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
            metric_id = f"latency_ms.{stage}.{label}"
            result[stage][label] = _mixed_percentile_metric(
                metric_id,
                values,
                fraction,
                planned_n=state.planned_n,
                failed_n=state.failed_n,
                na_reason="UNSUPPORTED_API" if stage == "retrieval" and not public_retrieval_supported else None,
            )
    return result


def _mixed_request_success_metric(state: StageState) -> dict[str, Any]:
    """Report request success separately from terminal availability.

    ``EMPTY`` is terminal for latency accounting, but it is not a successful
    answer request.  Keeping the two metrics separate prevents empty answers
    from being silently promoted to success while preserving them in the
    initial denominator.
    """

    success_n = sum(_status((state.rows.get(key) or {}).get("status")) == "SUCCESS" for key in state.units)
    empty_n = sum(_status((state.rows.get(key) or {}).get("status")) == "EMPTY" for key in state.units)
    unsupported_n = sum(_status((state.rows.get(key) or {}).get("status")) in UNSUPPORTED_STATUSES for key in state.units)
    failed_n = state.failed_n + empty_n + unsupported_n
    if not state.planned_n:
        return na_record("request_success", "NO_PLANNED_ATTEMPTS", denominator=0)
    if state.pending_n:
        return na_record(
            "request_success",
            "INCOMPLETE_PLANNED_DENOMINATOR",
            denominator=state.planned_n,
            numerator=success_n,
            eligible_n=state.terminal_n,
            missing_n=state.pending_n,
            failed_n=failed_n,
            planned_n=state.planned_n,
            observed_n=state.terminal_n,
        )
    record = metric_record(
        "request_success",
        value=success_n / state.planned_n,
        numerator=success_n,
        denominator=state.planned_n,
        eligible_n=state.terminal_n,
        missing_n=0,
        failed_n=failed_n,
        aggregation="successful_terminal_requests_over_planned_initial_denominator",
        planned_n=state.planned_n,
        observed_n=state.terminal_n,
    )
    record["diagnostics"]["empty_n"] = empty_n
    record["diagnostics"]["unsupported_n"] = unsupported_n
    return record


def _mixed_repeat_consistency_metric(state: StageState) -> dict[str, Any]:
    groups: dict[str, list[tuple[int, Mapping[str, Any] | None]]] = {}
    for question_id, repeat_id in state.units:
        groups.setdefault(question_id, []).append((repeat_id, state.rows.get((question_id, repeat_id))))
    repeated = {question_id: sorted(rows) for question_id, rows in groups.items() if len(rows) > 1}
    if not repeated:
        return na_record(
            "repeat_consistency",
            "REPEAT_NOT_PLANNED",
            denominator=0,
            aggregation="not_applicable_without_repeated_question_units",
        )
    values: list[float] = []
    missing = 0
    failed = 0
    for rows in repeated.values():
        signatures: list[tuple[str, str]] = []
        complete = True
        for _, row in rows:
            if row is None:
                complete = False
                break
            status = _status(row.get("status"))
            if status in FAILED_STATUSES | UNSUPPORTED_STATUSES | PENDING_STATUSES:
                if status in FAILED_STATUSES | UNSUPPORTED_STATUSES:
                    failed += 1
                complete = False
                break
            signatures.append((status, normalize_text(_mixed_answer_from_row(row))))
        if not complete:
            missing += 1
            continue
        values.append(float(len(set(signatures)) == 1))
    return _mixed_metric(
        "repeat_consistency",
        values,
        len(repeated),
        eligible_n=len(values),
        missing_n=missing,
        failed_n=failed,
        aggregation="question_macro_exact_status_and_normalized_answer_stability",
        protocol_label="REPEAT_CONSISTENCY_DIAGNOSTIC",
    )


def _mixed_citation_values(row: Mapping[str, Any] | None) -> list[Any]:
    """Read only explicit citation fields; never parse citations from answer text."""

    if not isinstance(row, Mapping):
        return []
    for key in ("citations", "citation", "references", "source_citations", "answer_citations"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            return _as_list(value)
    return []


def _mixed_citation_id_set(value: Any) -> set[str]:
    return _hit_ids(value)


def _mixed_citation_metrics(
    state: StageState,
    package: PackageData,
    aliases: Mapping[str, set[str]],
) -> dict[str, Any]:
    known_ids: set[str] = set()
    for row in package.corpus:
        for identifier in _evidence_ids(row):
            known_ids.update(_id_variants(identifier))
    for values in aliases.values():
        known_ids.update(values)

    total = 0
    valid = 0
    fabricated = 0
    out_of_scope = 0
    entailment_values: list[float] = []
    entailment_missing = 0
    required_attempts = 0
    required_with_valid_citation = 0
    required_claim_trace = False
    for key, question, row in _mixed_rows_for_state(state):
        citations = _mixed_citation_values(row)
        answerable = answerability_label(question) == "answerable"
        required = question.get("citation_required") is True or str(question.get("citation_required", "")).casefold() in {"true", "yes", "1"}
        if required and answerable:
            required_attempts += 1
        if not citations:
            if required and answerable and _status(row.get("status")) in VALID_STATUSES:
                # The benchmark protocol explicitly treats a missing citation
                # on a citation-required answer as zero coverage.
                continue
            continue
        allowed_ids = _as_ids(
            _first(question, "allowed_document_ids", "allowed_doc_ids", "scope_doc_ids", default=None)
        ) or _gold_doc_ids(question)
        scope_declared = any(
            key in question
            for key in ("allowed_document_ids", "allowed_doc_ids", "scope_doc_ids", "gold_doc_ids", "gold_document_ids", "document_ids")
        ) or answerability_label(question) == "unanswerable"
        for citation in citations:
            total += 1
            citation_ids = _mixed_citation_id_set(citation)
            is_valid = bool(citation_ids & known_ids)
            if is_valid:
                valid += 1
            else:
                fabricated += 1
            if scope_declared and is_valid and not _match_id_sets(allowed_ids, citation_ids, aliases):
                out_of_scope += 1
            if isinstance(citation, Mapping):
                explicit_entailment = _first(citation, "entails", "entailed", "supported", "citation_entails", default=None)
                if isinstance(explicit_entailment, bool):
                    entailment_values.append(float(explicit_entailment))
                elif explicit_entailment is not None:
                    entailment_missing += 1
        if required and answerable:
            required_with_valid_citation += int(any(_match_id_sets(allowed_ids, _mixed_citation_id_set(item), aliases) for item in citations)) if allowed_ids else 0
        if _first(row, "response_claims", "claims", default=None) not in (None, [], {}):
            required_claim_trace = True

    if total:
        locator = metric_record(
            "citation_locator_validity",
            value=valid / total,
            numerator=valid,
            denominator=total,
            eligible_n=total,
            aggregation="submitted_citation_micro",
            protocol_label="EXPLICIT_CITATION_LOCATOR_ONLY",
        )
        fabricated_record = metric_record(
            "fabricated_citation_count",
            value=fabricated,
            numerator=fabricated,
            denominator=total,
            eligible_n=total,
            unit="count",
            aggregation="submitted_citation_micro",
            protocol_label="EXPLICIT_CITATION_LOCATOR_ONLY",
        )
        out_of_scope_record = metric_record(
            "out_of_scope_citation_count",
            value=out_of_scope,
            numerator=out_of_scope,
            denominator=total,
            eligible_n=total,
            unit="count",
            aggregation="submitted_citation_micro",
            protocol_label="EXPLICIT_CITATION_SCOPE_ONLY",
        )
    else:
        locator = na_record("citation_locator_validity", "NO_SUBMITTED_CITATION", denominator=0)
        fabricated_record = na_record("fabricated_citation_count", "NO_SUBMITTED_CITATION", denominator=0, unit="count")
        out_of_scope_record = na_record("out_of_scope_citation_count", "NO_SUBMITTED_CITATION", denominator=0, unit="count")

    if entailment_values and not entailment_missing and len(entailment_values) == total:
        entailment = metric_record(
            "citation_entailment_precision",
            value=sum(entailment_values) / total,
            numerator=sum(entailment_values),
            denominator=total,
            eligible_n=total,
            aggregation="submitted_citation_micro_explicit_entailment_trace",
            protocol_label="EXPLICIT_CITATION_ENTAILMENT_TRACE",
        )
    elif not total:
        entailment = na_record("citation_entailment_precision", "NO_SUBMITTED_CITATION", denominator=0)
    else:
        entailment = na_record(
            "citation_entailment_precision",
            "CITATION_ENTAILMENT_TRACE_UNAVAILABLE",
            denominator=total,
            eligible_n=len(entailment_values),
            missing_n=max(0, total - len(entailment_values)),
            numerator=sum(entailment_values),
        )

    if required_attempts:
        coverage = metric_record(
            "answer_claim_citation_coverage",
            value=required_with_valid_citation / required_attempts if not required_claim_trace else None,
            numerator=required_with_valid_citation,
            denominator=required_attempts,
            eligible_n=required_attempts if not required_claim_trace else 0,
            missing_n=0 if not required_claim_trace else required_attempts,
            na_reason="CITATION_CLAIM_TRACE_UNAVAILABLE" if required_claim_trace else None,
            aggregation="citation_required_attempt_macro_explicit_locator",
            protocol_label="EXPLICIT_CITATION_REQUIRED_GATE",
        )
        if required_claim_trace:
            coverage = na_record(
                "answer_claim_citation_coverage",
                "CITATION_CLAIM_TRACE_UNAVAILABLE",
                denominator=required_attempts,
                eligible_n=0,
                missing_n=required_attempts,
                numerator=required_with_valid_citation,
                protocol_label="EXPLICIT_CITATION_REQUIRED_GATE",
            )
    else:
        coverage = na_record("answer_claim_citation_coverage", "NO_CITATION_REQUIRED_CLAIMS", denominator=0)
    return {
        "citation_locator_validity": locator,
        "citation_entailment_precision": entailment,
        "answer_claim_citation_coverage": coverage,
        "fabricated_citation_count": fabricated_record,
        "out_of_scope_citation_count": out_of_scope_record,
    }


def _failure_class(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "pending"
    status = _status(row.get("status"))
    if status == "SUCCESS":
        return "success"
    if status == "EMPTY":
        return "empty"
    if status in UNSUPPORTED_STATUSES:
        return "unsupported"
    if status in PENDING_STATUSES:
        return "pending"
    text = " ".join(str(row.get(key, "")) for key in ("error", "error_code", "reason", "provider_status", "status")).casefold()
    if any(marker in text for marker in ("timeout", "timed_out", "deadline", "read timed")):
        return "timeout"
    if any(marker in text for marker in ("provider", "maas", "http", "429", "500", "502", "503", "504", "rate limit")):
        return "provider_error"
    if any(marker in text for marker in ("schema", "json", "parse", "contract")):
        return "schema_error"
    if status in FAILED_STATUSES:
        return "product_error"
    return "unknown"


def _corpus_text_length(package: PackageData, row: Mapping[str, Any]) -> int | None:
    for key in ("content", "text", "markdown", "body"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return len(value)
    metadata = _mapping(row.get("metadata")) or {}
    for key in ("text_path", "path", "file_path", "content_path", "markdown_path"):
        value = row.get(key, metadata.get(key))
        if value in (None, ""):
            continue
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = package.root / path
        if path.is_file():
            try:
                return len(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return None
    return None


def _document_length_slice_info(package: PackageData) -> tuple[dict[str, str], dict[str, Any]]:
    entries: list[tuple[str, int, set[str]]] = []
    for index, row in enumerate(package.corpus):
        length = _corpus_text_length(package, row)
        if length is None:
            continue
        identifiers = set().union(*(_id_variants(item) for item in _evidence_ids(row)))
        if not identifiers:
            identifiers = {f"__corpus_row_{index}"}
        canonical = str(_first(row, "doc_id", "document_id", "id", "title", default=f"row-{index}"))
        entries.append((canonical, length, identifiers))
    entries.sort(key=lambda item: (item[1], item[0].casefold()))
    labels: dict[str, str] = {}
    for index, (_, _, identifiers) in enumerate(entries):
        label = f"q{min(5, (index * 5) // max(1, len(entries)) + 1)}"
        for identifier in identifiers:
            labels[identifier] = label
    return labels, {
        "unit": "unicode_codepoints",
        "bucket_method": "empirical_frozen_corpus_quintile_by_document_length",
        "labels": ["q1", "q2", "q3", "q4", "q5"],
        "unknown_label": "unknown",
        "observed_document_n": len(entries),
    }


def _document_length_label(question: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    matched = [labels[variant] for identifier in _gold_doc_ids(question) for variant in _id_variants(identifier) if variant in labels]
    if not matched:
        return "unknown"
    return max(matched, key=lambda value: int(value[1:]) if value.startswith("q") else -1)


def _mixed_apply_structured_gold_gate(state: StageState, metrics: dict[str, Any]) -> dict[str, Any]:
    questions = [state.questions.get(key[0], {}) for key in state.units]
    if structured_claim_gold_available(questions):
        return metrics
    for name in (
        "claim_correctness",
        "reference_claim_recall",
        "critical_claim_coverage",
        "gold_evidence_support",
        "grounding",
        "tdas",
    ):
        metrics[name] = na_record(
            name,
            MISSING_STRUCTURED_CLAIM_GOLD,
            denominator=state.planned_n,
            missing_n=state.planned_n,
            protocol_label="CANONICAL_STRUCTURED_GOLD",
            planned_n=state.planned_n,
            observed_n=0,
        )
    return metrics


def _mixed_native_source_result(
    source: str,
    package: PackageData,
    retrieval_state: StageState,
    qa_state: StageState,
    aliases: Mapping[str, set[str]],
    judge_rows: Sequence[Mapping[str, Any]],
    *,
    public_retrieval_supported: bool,
) -> dict[str, Any]:
    source_package = replace(package, dataset_id=source)
    legacy_retrieval = _stage_result("retrieval", retrieval_state, source_package, aliases)
    legacy_qa = _stage_result("qa", qa_state, source_package, aliases)
    _normalize_legacy_stage_records(legacy_retrieval)
    _normalize_legacy_stage_records(legacy_qa)

    custom_retrieval = _mixed_retrieval_metrics(
        retrieval_state,
        source_package,
        aliases,
        public_retrieval_supported=public_retrieval_supported,
    )
    if source != "enterprise-rag-bench":
        custom_retrieval["invalid_extra_rate_at_10"] = na_record(
            "invalid_extra_rate_at_10",
            "NO_INVALID_EXTRA_GOLD_CONTRACT",
            denominator=0,
        )
        custom_retrieval["invalid_extra_docs_at_10"] = custom_retrieval["invalid_extra_rate_at_10"]
    legacy_retrieval["metrics"].update(custom_retrieval)
    if not public_retrieval_supported:
        for slice_result in legacy_retrieval.get("slices", {}).values():
            if not isinstance(slice_result, Mapping):
                continue
            slice_metrics = slice_result.get("metrics")
            denominator = (slice_result.get("denominator") or {}).get("planned_n", 0) if isinstance(slice_result.get("denominator"), Mapping) else 0
            if not isinstance(slice_metrics, dict):
                continue
            for name in list(slice_metrics):
                if any(name.casefold().startswith(prefix) for prefix in ("recall", "r@", "hit", "precision", "mrr", "map", "ndcg", "evidence", "page", "layout", "source")):
                    slice_metrics[name] = na_record(name, "UNSUPPORTED_API", denominator=denominator)

    custom_qa = _mixed_qa_metrics(qa_state)
    judge = _mixed_judge_metrics(qa_state, judge_rows)
    if not judge_rows:
        judge["info_not_found_success_adapted"] = _mixed_refusal_metric(qa_state, "info_not_found_success_adapted", "info_not_found")
    custom_qa.update(
        {
            "strict_unanswerable_success": judge["strict_unanswerable_success"],
            "info_not_found_success_adapted": judge["info_not_found_success_adapted"],
        }
    )
    legacy_qa["metrics"].update(custom_qa)
    latency = _mixed_latency_metrics(
        retrieval_state,
        qa_state,
        public_retrieval_supported=public_retrieval_supported,
    )
    legacy_retrieval["latency_ms"] = latency["retrieval"]
    legacy_qa["latency_ms"] = latency["qa"]
    legacy_qa["e2e_latency_ms"] = latency["e2e"]
    for label, record in latency["retrieval"].items():
        legacy_retrieval["metrics"][f"retrieval_latency_ms_{label}"] = record
        legacy_retrieval["metrics"][f"latency_ms_{label}"] = record
    for label, record in latency["qa"].items():
        legacy_qa["metrics"][f"generation_latency_ms_{label}"] = record
        legacy_qa["metrics"][f"latency_ms_{label}"] = record
    for label, record in latency["e2e"].items():
        legacy_qa["metrics"][f"e2e_latency_ms_{label}"] = record
    legacy_retrieval["values"] = {name: record.get("value") for name, record in legacy_retrieval["metrics"].items() if isinstance(record, Mapping)}
    legacy_qa["values"] = {name: record.get("value") for name, record in legacy_qa["metrics"].items() if isinstance(record, Mapping)}
    return {
        "source_dataset": source,
        "protocol": package.protocol_tag,
        "retrieval": legacy_retrieval,
        "qa": legacy_qa,
        "judge": judge,
    }


def _mixed_slice_payload(
    units: Sequence[tuple[str, int]],
    retrieval_state: StageState,
    qa_state: StageState,
    package: PackageData,
    aliases: Mapping[str, set[str]],
    judge_rows: Sequence[Mapping[str, Any]],
    *,
    public_retrieval_supported: bool,
) -> dict[str, Any]:
    subset_retrieval = _state_for_units(retrieval_state, units)
    subset_qa = _state_for_units(qa_state, units)
    retrieval = _mixed_retrieval_metrics(
        subset_retrieval,
        package,
        aliases,
        public_retrieval_supported=public_retrieval_supported,
    )
    qa = _mixed_qa_metrics(subset_qa)
    _mixed_apply_structured_gold_gate(subset_qa, qa)
    judge = _mixed_judge_metrics(subset_qa, judge_rows)
    if not judge_rows:
        judge["info_not_found_success_adapted"] = _mixed_refusal_metric(subset_qa, "info_not_found_success_adapted", "info_not_found")
    qa["strict_unanswerable_success"] = judge["strict_unanswerable_success"]
    qa["info_not_found_success_adapted"] = judge["info_not_found_success_adapted"]
    metrics = {**retrieval, **qa}
    metrics.update(judge)
    metrics["request_success"] = _mixed_request_success_metric(subset_qa)
    metrics["repeat_consistency"] = _mixed_repeat_consistency_metric(subset_qa)
    metrics.update(_mixed_citation_metrics(subset_qa, package, aliases))
    return {
        "denominator": subset_qa.denominator,
        "status_counts": subset_qa.status_counts,
        "metrics": metrics,
        "judge": judge,
        "latency_ms": _mixed_latency_metrics(
            subset_retrieval,
            subset_qa,
            public_retrieval_supported=public_retrieval_supported,
        ),
    }


def _mixed_slice_value(
    retrieval_state: StageState,
    qa_state: StageState,
    key: tuple[str, int],
    dimension: str,
    *,
    document_length_labels: Mapping[str, str] | None = None,
) -> str:
    question = qa_state.questions.get(key[0], {})
    row = qa_state.rows.get(key) or retrieval_state.rows.get(key)
    if dimension == "source_dataset":
        return source_for_row(row, question)
    if dimension == "question_type":
        return _question_type(question)
    if dimension == "answerability":
        return answerability_label(question)
    if dimension == "gold_doc_count":
        count = gold_doc_count(question)
        return "4+" if count >= 4 else str(count)
    if dimension == "document_length":
        return _document_length_label(question, document_length_labels or {})
    if dimension == "failure_class":
        return _failure_class(row)
    return "unknown"


def _mixed_slice_units(
    retrieval_state: StageState,
    qa_state: StageState,
    dimension: str,
    label: str,
    *,
    package: PackageData | None = None,
    document_length_labels: Mapping[str, str] | None = None,
) -> list[tuple[str, int]]:
    units: list[tuple[str, int]] = []
    for key in qa_state.units:
        value = _mixed_slice_value(
            retrieval_state,
            qa_state,
            key,
            dimension,
            document_length_labels=document_length_labels,
        )
        if value == label:
            units.append(key)
    return units


def _mixed_unified_report(
    run_root: Path,
    run_manifest: Mapping[str, Any],
    package: PackageData,
    retrieval_state: StageState,
    qa_state: StageState,
    aliases: Mapping[str, set[str]],
    judge_rows: Sequence[Mapping[str, Any]],
    readiness: Mapping[str, Any],
) -> dict[str, Any]:
    public_retrieval_supported = _mixed_public_retrieval_supported(run_manifest)
    document_length_labels, document_length_definition = _document_length_slice_info(package)
    retrieval = _mixed_retrieval_metrics(
        retrieval_state,
        package,
        aliases,
        public_retrieval_supported=public_retrieval_supported,
    )
    qa = _mixed_qa_metrics(qa_state)
    _mixed_apply_structured_gold_gate(qa_state, qa)
    judge = _mixed_judge_metrics(qa_state, judge_rows)
    if not judge_rows:
        judge["info_not_found_success_adapted"] = _mixed_refusal_metric(qa_state, "info_not_found_success_adapted", "info_not_found")
    qa["strict_unanswerable_success"] = judge["strict_unanswerable_success"]
    qa["info_not_found_success_adapted"] = judge["info_not_found_success_adapted"]

    planned_n = qa_state.planned_n
    failed_n = qa_state.failed_n
    pending_n = qa_state.pending_n
    source_valid = sum(
        bool(source_for_row(retrieval_state.rows.get(key) or qa_state.rows.get(key), qa_state.questions.get(key[0], {})) != "unknown")
        for key in qa_state.units
    )
    validity = metric_record(
        "validity",
        value=source_valid / planned_n if planned_n and source_valid == planned_n else None,
        numerator=source_valid,
        denominator=planned_n,
        eligible_n=source_valid,
        missing_n=max(0, planned_n - source_valid),
        failed_n=0,
    )
    readiness_rate = readiness.get("ready_rate")
    if isinstance(readiness_rate, Mapping) and readiness_rate.get("value") is not None:
        readiness_metric = metric_record(
            "readiness",
            value=readiness_rate.get("value"),
            numerator=readiness_rate.get("numerator"),
            denominator=readiness_rate.get("denominator"),
            eligible_n=int(readiness_rate.get("denominator", 0) or 0),
            missing_n=max(0, int(readiness_rate.get("denominator", 0) or 0) - int(readiness_rate.get("numerator", 0) or 0)),
            failed_n=sum(value for key, value in (readiness.get("status_counts") or {}).items() if str(key).casefold() in {"failed", "error", "timeout"}),
        )
    else:
        readiness_metric = na_record(
            "readiness",
            str(readiness.get("reason", "UNSUPPORTED_READINESS_LEDGER_UNAVAILABLE")),
            denominator=int(readiness.get("planned_n", len(package.corpus)) or 0),
        )
    availability = metric_record(
        "initial_availability",
        value=(qa_state.valid_n / planned_n) if planned_n and not pending_n else None,
        numerator=qa_state.valid_n,
        denominator=planned_n,
        eligible_n=qa_state.valid_n,
        missing_n=pending_n,
        failed_n=failed_n,
    )
    error_rate = metric_record(
        "error_rate",
        value=((failed_n + qa_state.unsupported_n) / planned_n) if planned_n and not pending_n else None,
        numerator=failed_n + qa_state.unsupported_n,
        denominator=planned_n,
        eligible_n=planned_n - pending_n,
        missing_n=pending_n,
        failed_n=failed_n,
    )
    initial_error_rate = dict(error_rate)
    initial_error_rate["metric_id"] = "initial_error_rate"
    initial_error_rate["metric"] = "initial_error_rate"

    metrics: dict[str, Any] = {
        "validity": validity,
        "readiness": readiness_metric,
        "initial_availability": availability,
        "availability": availability,
        "error_rate": error_rate,
        "initial_error_rate": initial_error_rate,
        "initial_error": initial_error_rate,
        **retrieval,
        **qa,
        **judge,
        "request_success": _mixed_request_success_metric(qa_state),
        "repeat_consistency": _mixed_repeat_consistency_metric(qa_state),
        **_mixed_citation_metrics(qa_state, package, aliases),
    }
    latency = _mixed_latency_metrics(
        retrieval_state,
        qa_state,
        public_retrieval_supported=public_retrieval_supported,
    )
    for stage, stage_values in latency.items():
        for percentile_name, record in stage_values.items():
            metrics[f"{stage}_latency_ms_{percentile_name}"] = record
    metrics["MAP@10"] = metrics["map_at_10"]
    metrics["nDCG@10"] = metrics["ndcg_at_10"]
    metrics["MRR@10"] = metrics["mrr"]
    for k in TOP_K:
        metrics[f"Recall@{k}"] = metrics[f"recall_at_{k}"]
        metrics[f"Hit@{k}"] = metrics[f"hit_at_{k}"]
        metrics[f"Precision@{k}"] = metrics[f"precision_at_{k}"]

    source_names = sorted({_mixed_source_for_unit(retrieval_state, qa_state, key) for key in qa_state.units})
    slices: dict[str, dict[str, Any]] = {}
    slice_dimensions = ("source_dataset", "question_type", "answerability", "gold_doc_count", "document_length", "failure_class")
    for dimension in slice_dimensions:
        by_label: dict[str, Any] = {}
        labels = sorted(
            {
                _mixed_slice_value(
                    retrieval_state,
                    qa_state,
                    key,
                    dimension,
                    document_length_labels=document_length_labels,
                )
                for key in qa_state.units
            }
        )
        for label in labels:
            by_label[label] = _mixed_slice_payload(
                _mixed_slice_units(
                    retrieval_state,
                    qa_state,
                    dimension,
                    label,
                    package=package,
                    document_length_labels=document_length_labels,
                ),
                retrieval_state,
                qa_state,
                package,
                aliases,
                judge_rows,
                public_retrieval_supported=public_retrieval_supported,
            )
        slices[dimension] = by_label

    return {
        "registry_version": REGISTRY_VERSION,
        "protocol": "MOI_UNIFIED_ADAPTED_V1",
        "public_retrieval_supported": public_retrieval_supported,
        "source_datasets": source_names,
        "metrics": metrics,
        "judge": judge,
        "latency_ms": latency,
        "slices": slices,
        "slice_definitions": {
            "document_length": document_length_definition,
            "failure_class": {
                "labels": ["success", "empty", "timeout", "provider_error", "schema_error", "product_error", "unsupported", "pending", "unknown"],
                "source": "QA terminal status/error_code/error; retrieval status when QA row is absent",
            },
        },
        "denominator": qa_state.denominator,
        "status_counts": qa_state.status_counts,
        "limitations": [
            "Canonical claim/grounding/TDAS metrics are N/A until scored_reference_claims, critical_claims, and evidence_sets are present in Gold.",
            "Adapted lexical answer and Gold document/evidence metrics are diagnostic and are not a compensating overall score.",
        ],
    }


def _mixed_metric_partitions(
    run_root: Path,
    run_manifest: Mapping[str, Any],
    package: PackageData,
    retrieval_state: StageState,
    qa_state: StageState,
    aliases: Mapping[str, set[str]],
    readiness: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    judge_rows = _judge_rows(run_root)
    sources = sorted({_mixed_source_for_unit(retrieval_state, qa_state, key) for key in qa_state.units})
    public_retrieval_supported = _mixed_public_retrieval_supported(run_manifest)
    native_by_source: dict[str, Any] = {}
    for source in sources:
        units = [key for key in qa_state.units if _mixed_source_for_unit(retrieval_state, qa_state, key) == source]
        native_by_source[source] = _mixed_native_source_result(
            source,
            package,
            _state_for_units(retrieval_state, units),
            _state_for_units(qa_state, units),
            aliases,
            judge_rows,
            public_retrieval_supported=public_retrieval_supported,
        )
    native = {
        "registry_version": REGISTRY_VERSION,
        "partition": "DATASET_NATIVE_OFFICIAL_OR_ADAPTED",
        "by_source_dataset": native_by_source,
    }
    unified = _mixed_unified_report(
        run_root,
        run_manifest,
        package,
        retrieval_state,
        qa_state,
        aliases,
        judge_rows,
        readiness,
    )
    return native, unified


def _nested_find(mapping: Any, keys: Sequence[str]) -> Any:
    wanted = {key.casefold() for key in keys}
    for value in _iter_nested_mappings(mapping):
        for key, child in value.items():
            if str(key).casefold() in wanted:
                return child
    return None


def _coverage_metric(numerator: Any, denominator: Any, reason: str = "UNSUPPORTED_COVERAGE_UNAVAILABLE") -> dict[str, Any]:
    try:
        numerator_value = float(numerator)
        denominator_value = float(denominator)
    except (TypeError, ValueError):
        return {"value": None, "numerator": None, "denominator": None, "unit": "rate", "reason": reason}
    if denominator_value <= 0:
        return {"value": None, "numerator": _number(numerator_value), "denominator": _number(denominator_value), "unit": "rate", "reason": reason}
    return {"value": _number(numerator_value / denominator_value), "numerator": _number(numerator_value), "denominator": _number(denominator_value), "unit": "rate"}


def _coverage(package: PackageData) -> dict[str, Any]:
    if package.dataset_id != "fab-bench":
        return {}
    sources = _nested_find(package.manifest, ("source_status_counts",))
    if not isinstance(sources, Mapping):
        sources = _nested_find(package.condition_manifest, ("source_status_counts",))
    source_acquired = 0
    source_total = 0
    if isinstance(sources, Mapping):
        source_acquired = int(sources.get("source_acquired", sources.get("source", 0)) or 0)
        declared_total = _first(sources, "source_total", "sources_total", "total", default=None)
        if isinstance(declared_total, (int, float)):
            source_total = int(declared_total)
        else:
            source_total = sum(int(value or 0) for value in sources.values() if isinstance(value, (int, float)))
    image = _nested_find(package.manifest, ("gold_image_coverage",))
    if not isinstance(image, Mapping):
        image = _nested_find(package.condition_manifest, ("gold_image_coverage",))
    if not isinstance(image, Mapping):
        image = {}
    result = {
        "source_document_coverage": _coverage_metric(source_acquired, source_total),
        "gold_image_evidence": _coverage_metric(image.get("available", image.get("gold_image_evidence_available")), image.get("required", image.get("gold_image_evidence"))),
        "source_complete": _boolish(
            _first(
                package.manifest,
                "source_complete",
                default=_first(package.condition_manifest, "source_complete", default=False),
            )
        ),
        "denominator_policy": "current_local_frozen",
    }
    return result


def _readiness(run_root: Path, package: PackageData) -> dict[str, Any]:
    resource_paths = sorted(run_root.glob("**/resource-map.json"))
    if not resource_paths:
        return {"status": None, "reason": "UNSUPPORTED_READINESS_LEDGER_UNAVAILABLE", "planned_n": len(package.corpus)}
    payload = _json_load(resource_paths[0])
    resources = payload.get("resources", {}) if isinstance(payload, Mapping) else {}
    statuses: Counter[str] = Counter()
    document_count = 0
    for resource in resources.values() if isinstance(resources, Mapping) else []:
        if not isinstance(resource, Mapping):
            continue
        documents = resource.get("documents", {})
        if isinstance(documents, Mapping) and documents:
            for document in documents.values():
                if isinstance(document, Mapping):
                    document_count += 1
                    statuses[str(document.get("status", "unknown")).casefold()] += 1
            continue
        if isinstance(documents, Sequence) and not isinstance(documents, (str, bytes)) and documents:
            for document in documents:
                if isinstance(document, Mapping):
                    document_count += 1
                    statuses[str(document.get("status", "unknown")).casefold()] += 1
            continue

        # Global-scope runners intentionally keep ``documents`` compact: the
        # resource-level fields are the durable readiness summary, while the
        # candidate IDs and materialized artifact hashes remain in the same
        # checkpoint.  Do not mistake an empty per-document map for zero
        # observed documents after a successful ingest.
        def nonnegative_int(name: str) -> int | None:
            value = resource.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        declared_count = nonnegative_int("document_count")
        ready_count = nonnegative_int("ready_document_count")
        ready_count_missing = ready_count is None
        unsupported_count = nonnegative_int("unsupported_document_count") or 0
        failed_count = nonnegative_int("failed_document_count") or 0
        resource_status = str(resource.get("status", "unknown")).casefold()
        resource_ready = bool(resource.get("ready")) or resource_status in {"ready", "indexed", "completed", "available"}
        if declared_count is None and ready_count is None:
            continue
        if declared_count is None:
            declared_count = ready_count or 0

        if ready_count_missing:
            ready_count = declared_count if resource_ready else 0
        ready_count = min(ready_count, declared_count)
        unsupported_count = min(unsupported_count, max(0, declared_count - ready_count))
        failed_count = min(failed_count, max(0, declared_count - ready_count - unsupported_count))
        observed_count = min(declared_count, ready_count + unsupported_count + failed_count)
        if observed_count:
            document_count += observed_count
            if ready_count:
                statuses["ready"] += ready_count
            if unsupported_count:
                statuses["unsupported"] += unsupported_count
            if failed_count:
                statuses["failed"] += failed_count
        elif ready_count_missing and resource_ready and declared_count:
            # A legacy compact checkpoint may only expose ``ready`` and the
            # planned document count.  Treat that combination as all ready.
            document_count += declared_count
            statuses["ready"] += declared_count
        else:
            statuses[str(resource.get("status", "unknown")).casefold()] += 1
    ready = sum(statuses.get(value, 0) for value in ("ready", "indexed", "completed", "available"))
    return {
        "status": "READY" if document_count and ready == document_count else "PARTIAL",
        "planned_n": len(package.corpus),
        "observed_n": document_count,
        "ready_n": ready,
        "ready_rate": _coverage_metric(ready, document_count or len(package.corpus)),
        "status_counts": dict(sorted(statuses.items())),
    }


def aggregate_run(run: str | Path, package: str | Path) -> dict[str, Any]:
    """Aggregate one local run against one local frozen package manifest."""

    run_root, run_manifest, run_manifest_path = _load_run_manifest(run)
    package_data = _load_package(package, run_manifest)
    base_result: dict[str, Any] = {
        "schema": "competitor-eval-metrics-v1",
        "status": "COMPLETE",
        "run": {
            "root": str(run_root),
            "manifest_path": str(run_manifest_path) if run_manifest_path else None,
            "run_id": _first(run_manifest, "run_id", "id", default=run_root.name),
        },
        "package": {"root": str(package_data.root), "manifest_path": str(package_data.manifest_path) if package_data.manifest_path else None},
        "dataset": {
            "dataset_id": package_data.dataset_id,
            "dataset_name": package_data.dataset_name,
            "revision": package_data.revision,
            "split": package_data.split,
            "protocol_tag": package_data.protocol_tag,
            "condition": package_data.condition,
        },
        "denominator_policy": "current_local_frozen",
        "excluded_datasets": sorted(EXCLUDED_DATASETS),
    }
    if package_data.dataset_id in EXCLUDED_DATASETS:
        base_result.update({"status": "EXCLUDED", "reason": "EXCLUDED_DATASET", "metrics": {}, "coverage": {}})
        return base_result

    initial, terminal_rows = _load_ledgers(run_root)
    # Initial rows carry the complete planned question set even when a run
    # stops before producing terminal observations.  They are used only to
    # preserve the frozen denominator/question slices; metrics still consume
    # terminal rows below.
    questions = _question_map(package_data, [*terminal_rows, *initial])
    # Legacy single-dataset packages predate row-level routing.  Materialize
    # their package dataset as the fallback only when a question has no
    # explicit source_dataset; mixed packages keep their per-row source route.
    for question in questions.values():
        if not str(question.get("source_dataset") or "").strip():
            question["source_dataset"] = package_data.dataset_id
    aliases = _metadata_identifier_aliases(package_data)
    retrieval_state = _build_stage("retrieval", package_data, run_manifest, initial, terminal_rows, questions)
    qa_state = _build_stage("qa", package_data, run_manifest, initial, terminal_rows, questions)
    base_result["retrieval"] = _stage_result("retrieval", retrieval_state, package_data, aliases)
    base_result["qa"] = _stage_result("qa", qa_state, package_data, aliases)
    base_result["readiness"] = _readiness(run_root, package_data)
    base_result["coverage"] = _coverage(package_data)
    dataset_native, moi_unified = _mixed_metric_partitions(
        run_root,
        run_manifest,
        package_data,
        retrieval_state,
        qa_state,
        aliases,
        base_result["readiness"],
    )
    base_result["dataset_native"] = dataset_native
    base_result["moi_unified"] = moi_unified
    base_result["metric_registry"] = {"version": REGISTRY_VERSION, "metrics": METRIC_REGISTRY}
    base_result["ledger"] = {
        "initial_n": len(initial),
        "terminal_n": len(terminal_rows),
        "terminal_paths": [str(path) for path in _ledger_paths(run_root)],
    }
    if retrieval_state.pending_n or qa_state.pending_n:
        base_result["status"] = "PARTIAL"
    base_result["metrics"] = {
        "retrieval": base_result["retrieval"]["values"],
        "qa": base_result["qa"]["values"],
    }
    return base_result


def aggregate(run: str | Path, package: str | Path) -> dict[str, Any]:
    """Compatibility alias for callers that mirror the CLI subcommand name."""

    return aggregate_run(run, package)


def _markdown_value(result: Mapping[str, Any], stage: str, name: str) -> str:
    record = _mapping(_mapping(result.get(stage)).get("metrics", {}).get(name)) if isinstance(result.get(stage), Mapping) else None
    value = record.get("value") if record else None
    return "—" if value is None else str(value)


def _markdown_cell(value: Any, limit: int = 160) -> str:
    text = str(value if value is not None else "")
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def todo_markdown(result_or_run: Mapping[str, Any] | str | Path, package: str | Path | None = None) -> str:
    """Return a bounded replacement-safe Markdown result block.

    The function only returns text.  It never opens or edits ``TODO.md``.
    """

    result = aggregate_run(result_or_run, package) if package is not None else dict(result_or_run)  # type: ignore[arg-type]
    dataset = _mapping(result.get("dataset")) or {}
    retrieval = _mapping(result.get("retrieval")) or {}
    qa = _mapping(result.get("qa")) or {}
    retrieval_denominator = _mapping(retrieval.get("denominator")) or {}
    qa_denominator = _mapping(qa.get("denominator")) or {}
    lines = [
        "<!-- COMPETITOR_EVAL_METRICS_START -->",
        "| Dataset | Condition | Planned R/QA | R@1 | R@3 | R@5 | R@10 | MRR | EM | Token F1 | Contains gold | Nonempty | R p50/p95 ms | QA p50/p95 ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| "
        + " | ".join(
            (
                _markdown_cell(dataset.get("dataset_name", dataset.get("dataset_id", "unknown"))),
                _markdown_cell(dataset.get("condition", "")),
                f"{retrieval_denominator.get('planned_n', '—')}/{qa_denominator.get('planned_n', '—')}",
                _markdown_value(result, "retrieval", "recall_at_1"),
                _markdown_value(result, "retrieval", "recall_at_3"),
                _markdown_value(result, "retrieval", "recall_at_5"),
                _markdown_value(result, "retrieval", "recall_at_10"),
                _markdown_value(result, "retrieval", "mrr"),
                _markdown_value(result, "qa", "normalized_em"),
                _markdown_value(result, "qa", "token_f1"),
                _markdown_value(result, "qa", "contains_gold"),
                _markdown_value(result, "qa", "answer_non_empty"),
                f"{_markdown_value(result, 'retrieval', 'latency_ms_p50')}/{_markdown_value(result, 'retrieval', 'latency_ms_p95')}",
                f"{_markdown_value(result, 'qa', 'latency_ms_p50')}/{_markdown_value(result, 'qa', 'latency_ms_p95')}",
            )
        )
        + " |",
        "",
        f"Denominator policy: `current_local_frozen`; retrieval valid/failed/pending = `{retrieval_denominator.get('valid_n', '—')}/{retrieval_denominator.get('failed_n', '—')}/{retrieval_denominator.get('pending_n', '—')}`, QA = `{qa_denominator.get('valid_n', '—')}/{qa_denominator.get('failed_n', '—')}/{qa_denominator.get('pending_n', '—')}`.",
        "Judge/semantic metrics remain `null` with `UNSUPPORTED_NEEDS_JUDGE`; this block contains deterministic ledger metrics only.",
        "<!-- COMPETITOR_EVAL_METRICS_END -->",
    ]
    return "\n".join(lines) + "\n"


def _write_text(path: str | Path, text: str) -> None:
    target = Path(path).expanduser().resolve()
    if target.name.casefold() == "todo.md":
        raise MetricsError("TODO_WRITE_FORBIDDEN")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    aggregate_parser = subparsers.add_parser("aggregate", help="aggregate a local run directory")
    aggregate_parser.add_argument("--run", required=True, type=Path)
    aggregate_parser.add_argument("--package", required=True, type=Path)
    aggregate_parser.add_argument("--output", required=True, type=Path)
    markdown_parser = subparsers.add_parser("todo-markdown", help="emit a bounded TODO result block")
    markdown_parser.add_argument("--run", required=True, type=Path)
    markdown_parser.add_argument("--package", required=True, type=Path)
    markdown_parser.add_argument("--output", type=Path, help="write the block here; stdout when omitted")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "aggregate":
            result = aggregate_run(args.run, args.package)
            _write_text(args.output, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            return 0
        block = todo_markdown(args.run, args.package)
        if args.output:
            _write_text(args.output, block)
        else:
            sys.stdout.write(block)
        return 0
    except MetricsError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
