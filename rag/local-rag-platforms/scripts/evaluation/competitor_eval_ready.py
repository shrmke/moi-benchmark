#!/usr/bin/env python3
"""Build and validate the shared competitor eval-ready text packages.

The package is intentionally an index over already materialized local inputs.
Large corpora are never copied into the output directory; corpus rows point at
the source file and carry its current SHA-256 instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
PLATFORM_ROOT = HERE.parents[1]
ROOT = PLATFORM_ROOT.parent
DEFAULT_OUTPUT = ROOT / ".local-services/competitor-eval-ready/v1"
SCHEMA_VERSION = "competitor-eval-ready-v1"

CORE_DATASET_IDS = (
    "wikieval",
    "multihop-rag",
    "enterprise-rag-bench",
    "fab-bench",
)
DOCUMENT_DATASET_IDS = ("mmdocir", "mmdocrag", "docbench")
DATASET_ORDER = (
    "wikieval",
    "mmdocir",
    "mmdocrag",
    "docbench",
    "multihop-rag",
    "enterprise-rag-bench",
    "fab-bench",
)
ALLOWED_STATUSES = {"READY", "READY_ADAPTED", "BLOCKED"}

DATASET_ALIASES = {
    "wiki": "wikieval",
    "wiki-eval": "wikieval",
    "wikieval": "wikieval",
    "multi-hop": "multihop-rag",
    "multihop": "multihop-rag",
    "multihoprag": "multihop-rag",
    "multihop-rag": "multihop-rag",
    "enterprise": "enterprise-rag-bench",
    "enterprise-rag": "enterprise-rag-bench",
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
}

PROVIDER_SELECTION = {
    "text": {
        "provider": "maas",
        "display_name": "MaaS",
        "model": "glm-5.2",
    },
    "multimodal": {
        "provider": "maas",
        "display_name": "MaaS",
        "model": "Qwen2.5-VL-72B-32K",
    },
    "embedding": {
        "provider": "maas",
        "display_name": "MaaS",
        "model": "bge-m3",
        "dimension": 1024,
    },
}

PLATFORM_SUPPORT = {
    "moi_local": {"text": True, "multimodal": True, "native_qa": True, "direct_retrieval": True},
    "dify_local": {"text": True, "multimodal": False, "native_qa": True, "direct_retrieval": True},
    "fastgpt_local": {"text": True, "multimodal": False, "native_qa": True, "direct_retrieval": True},
    "maxkb_local": {"text": True, "multimodal": False, "native_qa": True, "direct_retrieval": True},
}


class EvalReadyError(RuntimeError):
    """A user-actionable build, validation, or delegation error."""


class SourceError(EvalReadyError):
    """A required local source is missing or inconsistent."""


@dataclass
class DatasetContent:
    dataset_id: str
    protocol_tag: str
    status: str
    source_complete: bool
    conditions: dict[str, Any]
    corpus: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    gold: list[dict[str, Any]]
    source_inputs: list[Path]
    count_extras: dict[str, Any]
    notes: list[str]


def canonical_dataset_id(value: str) -> str:
    normalized = value.strip().casefold().replace(" ", "-")
    if normalized in {"core", "core-text", "core-datasets"}:
        return "core"
    if normalized in {"all", "all-datasets"}:
        return "all"
    if normalized in {"document", "documents", "document-datasets", "document-benchmarks"}:
        return "documents"
    try:
        return DATASET_ALIASES[normalized]
    except KeyError as exc:
        choices = ", ".join((*CORE_DATASET_IDS, *DOCUMENT_DATASET_IDS, "core", "all"))
        raise EvalReadyError(f"unknown dataset '{value}'; choose one of: {choices}") from exc


def expand_dataset_selection(values: Sequence[str] | None) -> list[str]:
    selected = list(values or ["core"])
    expanded: list[str] = []
    for value in selected:
        for item in value.split(","):
            canonical = canonical_dataset_id(item)
            if canonical == "core":
                candidates = list(CORE_DATASET_IDS)
            elif canonical in {"all", "documents"}:
                candidates = list(DATASET_ORDER) if canonical == "all" else list(DOCUMENT_DATASET_IDS)
            else:
                candidates = [canonical]
            for candidate in candidates:
                if candidate not in expanded:
                    expanded.append(candidate)
    return expanded


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"required source does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"invalid JSON source {path}: {exc}") from exc


def _jsonl_load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SourceError(f"required JSONL source does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SourceError(f"invalid JSONL source {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise SourceError(f"JSONL source {path}:{line_number} must contain objects")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _resolve_path(value: str | Path, *, repo_root: Path, package_dir: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = []
    if package_dir is not None:
        candidates.append(package_dir / path)
    candidates.append(repo_root / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else path


def _path_ref(path: Path) -> str:
    return str(path.resolve())


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".jsonl": "application/jsonl",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")


def _title_from_text(path: Path, fallback: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or fallback
            match = re.match(r"^-\s*title:\s*`?(.+?)`?\s*$", stripped, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip() or fallback
    except OSError:
        pass
    return fallback


def _strip_prefix(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    if text.casefold().startswith(prefix.casefold()):
        return text[len(prefix) :].strip()
    return text


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")


def _doc_row(
    *,
    doc_id: str,
    title: str,
    path: Path,
    scope_id: str,
    metadata: Mapping[str, Any] | None = None,
    field: str = "text_path",
    media_type: str | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise SourceError(f"corpus reference does not exist: {path}")
    return {
        "doc_id": doc_id,
        "title": title or doc_id,
        field: _path_ref(path),
        "media_type": media_type or _media_type(path),
        "scope_id": scope_id,
        "sha256": sha256_file(path),
        "metadata": dict(metadata or {}),
    }


def _question_row(
    *,
    question_id: str,
    question: str,
    reference_answer: Any,
    answerable: bool,
    scope_doc_ids: Sequence[str],
    gold_doc_ids: Sequence[str],
    gold_evidence: Sequence[Any],
    question_type: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question": question,
        "reference_answer": "" if reference_answer is None else str(reference_answer),
        "answerable": bool(answerable),
        "scope_doc_ids": list(dict.fromkeys(str(value) for value in scope_doc_ids)),
        "gold_doc_ids": list(dict.fromkeys(str(value) for value in gold_doc_ids)),
        "gold_evidence": list(gold_evidence),
        "question_type": question_type or "unknown",
        "metadata": dict(metadata or {}),
    }


def _metric_contract(dataset_id: str) -> dict[str, Any]:
    contracts = {
        "wikieval": {
            "primary": ["source_recall@1", "source_recall@3", "source_recall@5", "source_recall@10", "mrr"],
            "secondary": ["reference_keyword_recall", "answer_non_empty_rate", "faithfulness", "answer_relevance", "context_precision", "context_recall"],
            "latency": ["retrieval_p50", "retrieval_p95", "generation_p50", "generation_p95", "e2e_p50", "e2e_p95"],
            "slices": ["source", "question_length", "evidence_length"],
        },
        "multihop-rag": {
            "primary": ["map@4", "mrr@4", "hit@4", "hit@10", "evidence_recall@10", "all_evidence_success"],
            "secondary": ["normalized_em", "token_f1", "answerable_accuracy", "null_refusal_success"],
            "latency": ["retrieval_p50", "retrieval_p95", "generation_p50", "generation_p95", "e2e_p50", "e2e_p95"],
            "slices": ["question_type", "gold_document_count", "category", "temporal_span"],
        },
        "enterprise-rag-bench": {
            "primary": ["answer_correctness", "completeness", "document_recall@10", "invalid_extra_docs", "leaderboard_aggregate"],
            "secondary": ["complete_evidence_set_recall", "all_required_doc_success", "strict_unanswerable_success", "claim_f1"],
            "latency": ["retrieval_p50", "retrieval_p95", "generation_p50", "generation_p95", "e2e_p50", "e2e_p95"],
            "slices": ["question_type", "source_type", "gold_document_count", "conflict_status"],
        },
        "fab-bench": {
            "primary": ["completeness", "technical_depth", "factuality", "relevance", "context_utilization", "support_quality", "overall"],
            "secondary": ["gold_evidence_coverage", "gold_image_coverage", "source_status_slice", "visual_coverage_failure_count"],
            "latency": ["retrieval_p50", "retrieval_p95", "generation_p50", "generation_p95", "e2e_p50", "e2e_p95"],
            "slices": ["test_type", "question_format", "source_status", "has_image_evidence"],
        },
    }
    contract = dict(contracts[dataset_id])
    contract["valid_denominator_policy"] = "record valid_n and failed_n for every metric; planned failures remain in the denominator"
    return contract


def _platform_support(dataset_id: str) -> dict[str, Any]:
    support = json.loads(json.dumps(PLATFORM_SUPPORT))
    if dataset_id == "fab-bench":
        for value in support.values():
            value["multimodal"] = False
            value["multimodal_note"] = "Gold visual coverage is incomplete; text/evidence-only condition only"
    return support


def _base_manifest(content: DatasetContent) -> dict[str, Any]:
    conditions = dict(content.conditions)
    conditions.setdefault("denominator_policy", "current_local_frozen_data")
    conditions.setdefault("download_new_data", False)
    conditions.setdefault("paper_full_corpus_required", False)
    return {
        "schema_version": SCHEMA_VERSION,
        "package_schema": SCHEMA_VERSION,
        "dataset_id": content.dataset_id,
        "protocol_tag": content.protocol_tag,
        "status": content.status if content.status in ALLOWED_STATUSES else "BLOCKED",
        "source_complete": bool(content.source_complete),
        "counts": {},
        "provider_selection": json.loads(json.dumps(PROVIDER_SELECTION)),
        "metric_contract": _metric_contract(content.dataset_id),
        "conditions": conditions,
        "platform_support": _platform_support(content.dataset_id),
        "notes": content.notes,
    }


def _counts(content: DatasetContent) -> dict[str, Any]:
    answerable = sum(bool(row.get("answerable")) for row in content.questions)
    return {
        "documents": len(content.corpus),
        "questions": len(content.questions),
        "corpus_rows": len(content.corpus),
        "question_rows": len(content.questions),
        "answerable_questions": answerable,
        "unanswerable_questions": len(content.questions) - answerable,
        "gold_document_links": sum(len(row.get("gold_doc_ids", [])) for row in content.questions),
        "gold_evidence": sum(len(row.get("gold_evidence", [])) for row in content.questions),
        **content.count_extras,
    }


def _source_rows_by_id(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        for key in keys:
            value = row.get(key)
            if value is not None:
                result[str(value)] = row
    return result


def _wiki_sources(repo_root: Path) -> tuple[Path, Path, Path | None]:
    public = repo_root / "datasets/downloads/public/wiki-eval/ragas-wiki-labelling.json"
    artifacts = repo_root / "runs/stage1/ragas-wikieval-moi/20260807-160000-wikieval/artifacts"
    docs = artifacts / "documents"
    derived_questions = artifacts / "questions.jsonl"
    if public.is_file() and docs.is_dir():
        return public, docs, derived_questions if derived_questions.is_file() else None
    if derived_questions.is_file() and docs.is_dir():
        return derived_questions, docs, None
    raise SourceError(
        "WikiEval sources not found; expected public "
        "datasets/downloads/public/wiki-eval/ragas-wiki-labelling.json and frozen documents/"
    )


def _wiki_doc_for_source(source: str, files: Sequence[Path], index: int, preferred: Sequence[str]) -> Path:
    by_name = {path.name: path for path in files}
    for name in preferred:
        if name in by_name:
            return by_name[name]
    source_key = _normal_key(source)
    candidates = [path for path in files if source_key and source_key in _normal_key(path.stem)]
    if len(candidates) == 1:
        return candidates[0]
    if index < len(files):
        return files[index]
    raise SourceError(f"WikiEval source '{source}' has no materialized document")


def _wiki_grounded_answer(row: Mapping[str, Any]) -> str:
    for item in row.get("faithfullness", []) or []:
        if isinstance(item, Mapping) and str(item.get("id", "")).casefold() == "grounded_answer":
            return _strip_prefix(item.get("body"), "Answer:")
    return _strip_prefix(row.get("reference"), "Answer:")


def build_wikieval(repo_root: Path, *, strict_counts: bool = True) -> DatasetContent:
    public_path, docs_dir, derived_path = _wiki_sources(repo_root)
    raw = _json_load(public_path)
    if isinstance(raw, list):
        public_rows = raw
    else:
        public_rows = raw.get("rows", []) if isinstance(raw, Mapping) else []
    if not isinstance(public_rows, list):
        raise SourceError(f"WikiEval source must contain a list: {public_path}")
    files = sorted(path for path in docs_dir.iterdir() if path.is_file() and not path.name.startswith("."))
    if strict_counts and (len(public_rows) != 50 or len(files) != 50):
        raise SourceError(f"WikiEval frozen count mismatch: rows={len(public_rows)}, documents={len(files)}, expected 50/50")
    if not public_rows or not files:
        raise SourceError("WikiEval has no rows or documents")

    derived_rows = _jsonl_load(derived_path) if derived_path else []
    derived_by_source = {str(row.get("metadata", {}).get("source", "")): row for row in derived_rows}
    derived_by_id = {str(row.get("id", "")): row for row in derived_rows}
    docs: list[dict[str, Any]] = []
    selected_paths: list[Path] = []
    scope_id = "wikieval-frozen-50-v1"
    for index, row in enumerate(public_rows):
        source = str(row.get("source", ""))
        legacy = derived_by_source.get(source) or derived_by_id.get(f"wikieval-{index + 1:03d}") or {}
        preferred = [str(value) for value in legacy.get("relevant_documents", [])]
        path = _wiki_doc_for_source(source, files, index, preferred)
        if path not in selected_paths:
            selected_paths.append(path)
            docs.append(
                _doc_row(
                    doc_id=path.name,
                    title=_title_from_text(path, source or path.stem),
                    path=path,
                    scope_id=scope_id,
                    metadata={"dataset": "WikiEval", "source": source, "frozen": True},
                )
            )
    # The input is a one-source-per-row freeze. Include any materialized source
    # not reached by source matching so validation can expose the count.
    if len(docs) < len(files):
        for path in files:
            if path not in selected_paths:
                docs.append(_doc_row(doc_id=path.name, title=_title_from_text(path, path.stem), path=path, scope_id=scope_id, metadata={"dataset": "WikiEval", "frozen": True}))
    docs_by_id = {row["doc_id"]: row for row in docs}
    all_doc_ids = list(docs_by_id)
    questions: list[dict[str, Any]] = []
    for index, row in enumerate(public_rows):
        source = str(row.get("source", ""))
        legacy = derived_by_source.get(source) or derived_by_id.get(f"wikieval-{index + 1:03d}") or {}
        preferred = [str(value) for value in legacy.get("relevant_documents", [])]
        path = _wiki_doc_for_source(source, files, index, preferred)
        gold_doc_ids = [path.name]
        evidence = legacy.get("relevant_evidence") or [str(row.get("context", ""))]
        reference = legacy.get("reference") or _wiki_grounded_answer(row)
        question = _strip_prefix(row.get("question"), "Question:") or str(legacy.get("question", ""))
        question_id = str(legacy.get("id") or f"wikieval-{index + 1:03d}")
        questions.append(
            _question_row(
                question_id=question_id,
                question=question,
                reference_answer=reference,
                answerable=True,
                scope_doc_ids=all_doc_ids,
                gold_doc_ids=gold_doc_ids,
                gold_evidence=evidence,
                question_type="open_ended",
                metadata={
                    "dataset": "WikiEval",
                    "source": source,
                    "public_row_id": row.get("id"),
                    "retrieval_keywords": legacy.get("retrieval_keywords", [question]),
                    "expected_answer_keywords": legacy.get("expected_answer_keywords", []),
                    "source_path": _path_ref(public_path),
                },
            )
        )
    if strict_counts and (len(docs) != 50 or len(questions) != 50):
        raise SourceError(f"WikiEval package count mismatch: documents={len(docs)}, questions={len(questions)}, expected 50/50")
    return DatasetContent(
        dataset_id="wikieval",
        protocol_tag="WIKIEVAL_FROZEN_50_50_V1",
        status="READY",
        source_complete=len(docs) == len(files) == len(public_rows),
        conditions={
            "split": "frozen",
            "selection": "50 public WikiEval rows paired with 50 frozen Markdown sources",
            "source_path": _path_ref(public_path),
            "document_path": _path_ref(docs_dir),
            "text_only": True,
        },
        corpus=docs,
        questions=questions,
        gold=[],
        source_inputs=[public_path, docs_dir, *( [derived_path] if derived_path else [])],
        count_extras={"frozen_rows": len(public_rows), "frozen_documents": len(files)},
        notes=["WikiEval is a 50/50 frozen text regression set; it is not a multimodal or full-corpus benchmark."],
    )


def _multihop_document_paths(prepared_dir: Path, corpus: Sequence[Mapping[str, Any]]) -> dict[int, Path]:
    result: dict[int, Path] = {}
    manifest_path = prepared_dir / "corpus-manifest.json"
    if manifest_path.is_file():
        value = _json_load(manifest_path)
        rows = value.get("documents", []) if isinstance(value, Mapping) else []
        for item in rows:
            if not isinstance(item, Mapping) or item.get("article_index") is None:
                continue
            path = prepared_dir / "upload-to-dify" / str(item.get("filename", ""))
            if path.is_file():
                result[int(item["article_index"])] = path
    if not result:
        docs_dir = prepared_dir / "upload-to-dify"
        for path in sorted(docs_dir.glob("article-*.md")) if docs_dir.is_dir() else []:
            match = re.match(r"article-(\d{4})-", path.name)
            if match:
                result[int(match.group(1)) - 1] = path
    return result


def build_multihop(repo_root: Path, *, strict_counts: bool = True) -> DatasetContent:
    public_dir = repo_root / "datasets/downloads/public/multihop-rag"
    corpus_path = public_dir / "corpus.json"
    questions_path = public_dir / "MultiHopRAG.json"
    corpus = _json_load(corpus_path)
    questions_raw = _json_load(questions_path)
    if not isinstance(corpus, list) or not isinstance(questions_raw, list):
        raise SourceError("MultiHop-RAG corpus.json and MultiHopRAG.json must both contain lists")
    if strict_counts and (len(corpus) != 609 or len(questions_raw) != 2556):
        raise SourceError(f"MultiHop-RAG official count mismatch: documents={len(corpus)}, questions={len(questions_raw)}, expected 609/2556")
    prepared_dir = repo_root / "datasets/downloads/prepared/multihop-rag-dify"
    per_doc_paths = _multihop_document_paths(prepared_dir, corpus)
    scope_id = "multihop-rag-official-609-v1"
    docs: list[dict[str, Any]] = []
    title_to_doc: dict[str, str] = {}
    for index, item in enumerate(corpus):
        title = str(item.get("title", ""))
        path = per_doc_paths.get(index)
        if path is not None:
            doc_id = path.name
            row = _doc_row(
                doc_id=doc_id,
                title=title or path.stem,
                path=path,
                scope_id=scope_id,
                metadata={
                    "dataset": "MultiHop-RAG",
                    "official_index": index,
                    "source": item.get("source"),
                    "author": item.get("author"),
                    "published_at": item.get("published_at"),
                    "category": item.get("category"),
                    "url": item.get("url"),
                    "official_source_path": _path_ref(corpus_path),
                },
            )
        else:
            # A fixture or a clean checkout may only have the official JSON.
            # Keep a JSON pointer instead of materializing 609 copies.
            row = _doc_row(
                doc_id=f"multihop-doc-{index + 1:04d}",
                title=title or f"document-{index + 1:04d}",
                path=corpus_path,
                scope_id=scope_id,
                media_type="application/json",
                metadata={
                    "dataset": "MultiHop-RAG",
                    "official_index": index,
                    "source_json_pointer": f"/{index}/body",
                    "source": item.get("source"),
                    "author": item.get("author"),
                    "published_at": item.get("published_at"),
                    "category": item.get("category"),
                    "url": item.get("url"),
                },
            )
        docs.append(row)
        title_to_doc[title] = row["doc_id"]
    all_doc_ids = [row["doc_id"] for row in docs]
    qtype_counts: Counter[str] = Counter()
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(questions_raw):
        question_type = str(item.get("question_type", "unknown"))
        qtype_counts[question_type] += 1
        evidence_sources = [source for source in item.get("evidence_list", []) if isinstance(source, Mapping)]
        gold_doc_ids = [title_to_doc[str(source.get("title", ""))] for source in evidence_sources if str(source.get("title", "")) in title_to_doc]
        facts = [str(source.get("fact", "")) for source in evidence_sources if str(source.get("fact", "")).strip()]
        answerable = question_type != "null_query"
        questions.append(
            _question_row(
                question_id=f"multihop-rag-{index + 1:04d}",
                question=str(item.get("query", "")),
                reference_answer=item.get("answer", ""),
                answerable=answerable,
                scope_doc_ids=all_doc_ids,
                gold_doc_ids=gold_doc_ids,
                gold_evidence=facts,
                question_type=question_type,
                metadata={
                    "dataset": "MultiHop-RAG",
                    "official_index": index,
                    "evidence_sources": evidence_sources,
                    "official_question_type": question_type,
                },
            )
        )
    if strict_counts and (len(docs) != 609 or len(questions) != 2556):
        raise SourceError(f"MultiHop-RAG package count mismatch: documents={len(docs)}, questions={len(questions)}, expected 609/2556")
    return DatasetContent(
        dataset_id="multihop-rag",
        protocol_tag="MULTIHOP_RAG_OFFICIAL_ALL_609_2556_V1",
        status="READY",
        source_complete=True,
        conditions={
            "selection": "all rows from official corpus.json and MultiHopRAG.json",
            "document_count": len(corpus),
            "question_count": len(questions_raw),
            "question_type_counts": dict(qtype_counts),
            "sampling": "none; the legacy 20-question smoke sample is excluded",
            "text_only": True,
        },
        corpus=docs,
        questions=questions,
        gold=[],
        source_inputs=[corpus_path, questions_path, *( [prepared_dir / "corpus-manifest.json"] if (prepared_dir / "corpus-manifest.json").is_file() else [])],
        count_extras={"official_documents": len(corpus), "official_questions": len(questions_raw), "question_type_counts": dict(qtype_counts)},
        notes=["Built from the official 609-row corpus.json and 2,556-row MultiHopRAG.json; no 20-question sample is used."],
    )


def _enterprise_root(repo_root: Path) -> Path:
    preferred = repo_root / "datasets/downloads/prepared/moi-ragbench-20260805-full-enterprise/enterprise-rag-bench"
    fallback = repo_root / "datasets/downloads/prepared/moi-ragbench-20260805/enterprise-rag-bench"
    for candidate in (preferred, fallback):
        if (candidate / "questions.jsonl").is_file() and (candidate / "corpus").is_dir():
            return candidate
    raise SourceError("EnterpriseRAG-Bench question-linked slice not found; expected the full-enterprise prepared directory")


def _enterprise_doc_id(value: Any) -> str:
    return Path(str(value)).stem


def build_enterprise(repo_root: Path, *, strict_counts: bool = True) -> DatasetContent:
    root = _enterprise_root(repo_root)
    questions_path = root / "questions.jsonl"
    gold_path = root / "gold-questions.jsonl"
    question_raw = _jsonl_load(questions_path)
    gold_raw = _jsonl_load(gold_path) if gold_path.is_file() else []
    gold_by_id = {str(row.get("question_id", "")): row for row in gold_raw}
    files = sorted(root.joinpath("corpus").glob("*.md"))
    if strict_counts and (len(files) != 722 or len(question_raw) != 500):
        raise SourceError(f"EnterpriseRAG-Bench adapted-slice count mismatch: documents={len(files)}, questions={len(question_raw)}, expected 722/500")
    scope_id = "enterprise-rag-bench-question-linked-full-500-adapted-v1"
    docs: list[dict[str, Any]] = []
    for path in files:
        doc_id = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        source_match = re.search(r"^-\s*source_type:\s*`?([^`\n]+)", text, flags=re.MULTILINE | re.IGNORECASE)
        source_type = source_match.group(1).strip() if source_match else None
        docs.append(
            _doc_row(
                doc_id=doc_id,
                title=_title_from_text(path, doc_id),
                path=path,
                scope_id=scope_id,
                metadata={"dataset": "EnterpriseRAG-Bench", "source_type": source_type, "adapted_slice": True},
            )
        )
    doc_ids = [row["doc_id"] for row in docs]
    questions: list[dict[str, Any]] = []
    type_counts: Counter[str] = Counter()
    for row in question_raw:
        question_id = str(row.get("id", ""))
        metadata = row.get("metadata", {}) if isinstance(row.get("metadata"), Mapping) else {}
        gold = gold_by_id.get(question_id, {})
        qtype = str(metadata.get("question_type") or gold.get("question_type") or "unknown")
        type_counts[qtype] += 1
        gold_doc_ids = [
            _enterprise_doc_id(value)
            for value in (metadata.get("expected_doc_ids") or row.get("relevant_documents") or gold.get("expected_doc_ids") or [])
        ]
        evidence = row.get("relevant_evidence") or gold.get("answer_facts") or []
        answer = metadata.get("gold_answer") or gold.get("gold_answer") or ""
        questions.append(
            _question_row(
                question_id=question_id,
                question=str(row.get("question", "")),
                reference_answer=answer,
                answerable=bool(row.get("expected_answerable", True)),
                scope_doc_ids=doc_ids,
                gold_doc_ids=gold_doc_ids,
                gold_evidence=evidence,
                question_type=qtype,
                metadata={
                    "dataset": "EnterpriseRAG-Bench",
                    "source_types": metadata.get("source_types", []),
                    "answer_facts": gold.get("answer_facts", []),
                    "full_corpus_rows": 511962,
                    "adapted_slice": "question-linked full-500",
                },
            )
        )
    if strict_counts and (len(docs) != 722 or len(questions) != 500):
        raise SourceError(f"EnterpriseRAG-Bench package count mismatch: documents={len(docs)}, questions={len(questions)}, expected 722/500")
    extra_sources = [questions_path]
    for path in (gold_path, root / "README.md", root.parent / "run-manifest.json"):
        if path.is_file():
            extra_sources.append(path)
    return DatasetContent(
        dataset_id="enterprise-rag-bench",
        protocol_tag="ENTERPRISERAG_BENCH_QUESTION_LINKED_FULL_500_ADAPTED_SLICE_V1",
        status="READY_ADAPTED",
        source_complete=False,
        conditions={
            "selection": "local question-linked full-500 adapted slice",
            "preferred_source": "moi-ragbench-20260805-full-enterprise",
            "documents": len(files),
            "questions": len(question_raw),
            "full_corpus_rows": 511962,
            "full_corpus_included": False,
            "not_full_corpus_note": "This package is not the 511,962-row full corpus.",
            "question_type_counts": dict(type_counts),
            "text_only": True,
        },
        corpus=docs,
        questions=questions,
        gold=[],
        source_inputs=extra_sources,
        count_extras={"slice_documents": len(files), "slice_questions": len(question_raw), "full_corpus_rows": 511962, "question_type_counts": dict(type_counts)},
        notes=["EnterpriseRAG-Bench is READY_ADAPTED for the local question-linked full-500 slice; it is not a full-corpus result."],
    )


def _fab_root(repo_root: Path) -> Path:
    root = repo_root / "datasets/downloads/prepared/fab-bench-complete-20260805"
    if not (root / "source-registry.jsonl").is_file():
        raise SourceError("FAB-Bench prepared package not found: datasets/downloads/prepared/fab-bench-complete-20260805")
    return root


def build_fab(repo_root: Path, *, strict_counts: bool = True) -> DatasetContent:
    root = _fab_root(repo_root)
    source_registry_path = root / "source-registry.jsonl"
    evidence_registry_path = root / "evidence-registry.jsonl"
    source_manifest_path = root / "evidence-prepared/fab-bench/source-manifest.json"
    questions_path = root / "questions.jsonl"
    gold_path = root / "gold-questions.jsonl"
    source_registry = _jsonl_load(source_registry_path)
    evidence_registry = _jsonl_load(evidence_registry_path) if evidence_registry_path.is_file() else []
    source_by_id = {str(row.get("doc_id", "")): row for row in source_registry}
    evidence_by_doc: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in evidence_registry:
        evidence_by_doc[str(row.get("doc_id", ""))].append(row)
    if source_manifest_path.is_file():
        manifest_value = _json_load(source_manifest_path)
        manifest_docs = manifest_value.get("documents", []) if isinstance(manifest_value, Mapping) else []
    else:
        manifest_docs = [{"doc_id": row.get("doc_id"), "file_name": f"{row.get('doc_id')}.md"} for row in source_registry]
    question_raw = _jsonl_load(questions_path)
    gold_raw = _jsonl_load(gold_path)
    question_by_id = {str(row.get("id", "")): row for row in question_raw}
    original_source_paths: dict[str, Path] = {}
    missing_original_sources: list[str] = []
    for source in source_registry:
        original_path = source.get("original_path")
        if not original_path:
            continue
        resolved = _resolve_path(str(original_path), repo_root=repo_root)
        if resolved.is_file():
            original_source_paths[str(source.get("doc_id", ""))] = resolved
        else:
            missing_original_sources.append(str(source.get("doc_id", "")))
    docs: list[dict[str, Any]] = []
    scope_id = "fab-bench-public-complete-evidence-only-v1"
    for item in manifest_docs:
        doc_id = str(item.get("doc_id", ""))
        path = root / "evidence-prepared/fab-bench/corpus" / str(item.get("file_name", f"{doc_id}.md"))
        source = source_by_id.get(doc_id, {})
        evidence_title = next((str(row.get("section_title", "")) for row in evidence_by_doc.get(doc_id, []) if str(row.get("section_title", "")).strip()), "")
        original_path = original_source_paths.get(doc_id)
        docs.append(
            _doc_row(
                doc_id=doc_id,
                title=_title_from_text(path, evidence_title or doc_id),
                path=path,
                scope_id=scope_id,
                metadata={
                    "dataset": "FAB-Bench",
                    "source_kind": source.get("source_kind"),
                    "source_status": source.get("source_status", "evidence_only"),
                    "original_source_status": source.get("original_source_status"),
                    "original_path": _path_ref(original_path) if original_path else None,
                    "source_sha256": source.get("source_sha256"),
                    "original_sha256": sha256_file(original_path) if original_path else None,
                    "original_source_missing_locally": bool(source.get("original_path")) and original_path is None,
                    "evidence_blocks": item.get("evidence_blocks", source.get("evidence_blocks", 0)),
                    "has_image_evidence": bool(source.get("has_image_evidence")),
                    "image_asset_status": source.get("image_asset_status"),
                },
            )
        )
    if strict_counts and len(docs) != 127:
        raise SourceError(f"FAB-Bench source registry count mismatch: documents={len(docs)}, expected 127")
    doc_ids = [row["doc_id"] for row in docs]
    questions: list[dict[str, Any]] = []
    image_evidence = 0
    for index, gold_row in enumerate(gold_raw):
        question_id = str(gold_row.get("test_id") or question_by_id.get(str(gold_row.get("id", "")), {}).get("id") or f"fab-bench-{index + 1:03d}")
        raw = question_by_id.get(question_id, {})
        sources = [source for source in gold_row.get("gold_context_sources", []) if isinstance(source, Mapping)]
        if not sources:
            source_docs = raw.get("relevant_documents", [])
            sources = [{"doc_id": _enterprise_doc_id(value), "evidence": evidence} for value, evidence in zip(source_docs, raw.get("relevant_evidence", []))]
        gold_doc_ids = [str(source.get("doc_id", "")) for source in sources if str(source.get("doc_id", ""))]
        evidence = [str(source.get("evidence", "")) for source in sources if str(source.get("evidence", "")).strip()]
        has_image = any(bool(source.get("has_image")) for source in sources)
        image_evidence += sum(bool(source.get("has_image")) for source in sources)
        raw_meta = raw.get("metadata", {}) if isinstance(raw.get("metadata"), Mapping) else {}
        questions.append(
            _question_row(
                question_id=question_id,
                question=str(gold_row.get("question") or raw.get("question", "")),
                reference_answer=gold_row.get("ground_truth_answer") or raw_meta.get("ground_truth_answer", ""),
                answerable=bool(raw.get("expected_answerable", True)),
                scope_doc_ids=doc_ids,
                gold_doc_ids=gold_doc_ids,
                gold_evidence=evidence,
                question_type=str(gold_row.get("test_type") or raw_meta.get("test_type") or "unknown"),
                metadata={
                    "dataset": "FAB-Bench",
                    "question_format": gold_row.get("question_format") or raw_meta.get("question_format"),
                    "primary_metric": gold_row.get("primary_metric") or raw_meta.get("primary_metric"),
                    "source_chapter": gold_row.get("source_chapter"),
                    "has_image_evidence": has_image,
                    "gold_context_sources": sources,
                    "source_status": "public_evidence_only",
                },
            )
        )
    if strict_counts and len(questions) != 200:
        raise SourceError(f"FAB-Bench question count mismatch: questions={len(questions)}, expected 200")
    completeness_path = root / "source-completeness.json"
    completeness = _json_load(completeness_path) if completeness_path.is_file() else {}
    required_images = int(completeness.get("image_evidence_items", image_evidence) or image_evidence)
    available_images = int(completeness.get("gold_image_evidence_with_source_assets", 0) or 0)
    missing_images = max(0, required_images - available_images)
    status_counts = Counter(str(row.get("source_status", "evidence_only")) for row in source_registry)
    source_inputs = [source_registry_path, questions_path, gold_path]
    source_inputs.extend(original_source_paths.values())
    for path in (evidence_registry_path, source_manifest_path, completeness_path, root / "visual-registry.jsonl"):
        if path.is_file():
            source_inputs.append(path)
    return DatasetContent(
        dataset_id="fab-bench",
        protocol_tag="FAB_BENCH_PUBLIC_COMPLETE_EVIDENCE_ONLY_V1",
        status="READY_ADAPTED",
        source_complete=False,
        conditions={
            "selection": "public-complete-evidence-only",
            "questions": len(questions),
            "doc_ids": len(docs),
            "source_status_counts": dict(status_counts),
            "source_documents": int(status_counts.get("source_acquired", 0)),
            "evidence_only_documents": int(status_counts.get("evidence_only", 0)),
            "gold_image_coverage": {
                "required": required_images,
                "available": available_images,
                "missing": missing_images,
                "coverage": (available_images / required_images) if required_images else 1.0,
                "status": "coverage_failure_retained" if missing_images else "complete",
            },
            "source_complete": False,
            "missing_original_sources_locally": len(missing_original_sources),
            "text_only": True,
        },
        corpus=docs,
        questions=questions,
        gold=[],
        source_inputs=source_inputs,
        count_extras={
            "source_documents": int(status_counts.get("source_acquired", 0)),
            "evidence_only_documents": int(status_counts.get("evidence_only", 0)),
            "gold_image_evidence": required_images,
            "gold_image_evidence_available": available_images,
            "gold_image_evidence_missing": missing_images,
            "missing_original_sources_locally": len(missing_original_sources),
        },
        notes=[
            "FAB-Bench remains public-complete-evidence-only: 45 source documents and 82 evidence-only documents in the full package.",
            "Missing paper/source assets remain metadata; they do not block the locally usable evidence-only text denominator.",
        ],
    )


BUILDERS = {
    "wikieval": build_wikieval,
    "multihop-rag": build_multihop,
    "enterprise-rag-bench": build_enterprise,
    "fab-bench": build_fab,
}


def _gold_rows(questions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "question_id": row["question_id"],
            "reference_answer": row["reference_answer"],
            "answerable": row["answerable"],
            "gold_doc_ids": row["gold_doc_ids"],
            "gold_evidence": row["gold_evidence"],
            "question_type": row["question_type"],
            "metadata": row.get("metadata", {}),
        }
        for row in questions
    ]


def _artifact_record(path: Path, package_dir: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(package_dir)),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _source_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = raw_path.resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise SourceError(f"source input does not exist: {path}")
        records.append({"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return records


def _start_template(manifest: Mapping[str, Any], artifacts: Mapping[str, Any]) -> dict[str, Any]:
    def data_hash(name: str) -> str:
        record = artifacts.get(name, {}) if isinstance(artifacts, Mapping) else {}
        return f"sha256:{record.get('sha256', 'UNKNOWN')}"

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": "<timestamp>-<system>-<dataset>-<condition>",
        "status_at_start": "not_started",
        "system_id": "moi_local|dify_local|fastgpt_local|maxkb_local",
        "dataset": manifest.get("dataset_id", "UNKNOWN"),
        "dataset_revision": manifest.get("protocol_tag", "UNKNOWN"),
        "split": manifest.get("conditions", {}).get("split", "frozen_or_adapted"),
        "protocol_tag": manifest.get("protocol_tag", "UNKNOWN"),
        "condition": "native|actual|oracle|retrieval|text_evidence_only",
        "planned": {
            "files": manifest.get("counts", {}).get("documents", 0),
            "pages": 0,
            "questions": manifest.get("counts", {}).get("questions", 0),
            "repeats": 1,
            "initial_attempts": manifest.get("counts", {}).get("questions", 0),
        },
        "data_hashes": {
            "manifest": "sha256:MANIFEST_HASH_AT_START",
            "questions": data_hash("questions.jsonl"),
            "documents": data_hash("corpus.jsonl"),
            "gold": data_hash("gold.jsonl"),
        },
        "system": {"version": "UNKNOWN", "deployment": "self_hosted", "image_digest": "UNKNOWN", "platform": "UNKNOWN"},
        "pipeline": {
            "parser": "platform_native_or_recorded",
            "chunking": "platform_native_or_recorded",
            "embedding": "MaaS/bge-m3/1024",
            "retriever": "platform_native_or_recorded",
            "reranker": "disabled|recorded",
            "generator": "MaaS/glm-5.2",
            "multimodal_generator": "MaaS/Qwen2.5-VL-72B-32K",
            "judge": "recorded_or_N/A",
            "prompt_hash": "sha256:UNKNOWN",
            "top_k": [1, 3, 5, 10],
            "context_budget": "UNKNOWN",
            "max_output_tokens": 0,
        },
        "metric_contract": manifest.get("metric_contract", {}),
        "provider": manifest.get("provider_selection", {}),
        "runtime": {"host": "UNKNOWN", "docker_platform": "UNKNOWN", "cpu": 0, "memory_gib": 0, "random_seed": "UNKNOWN"},
        "latency_boundary": {"retrieval": "recorded", "generation": "recorded", "e2e": "recorded", "cold_or_warm": "recorded"},
        "failure_policy": {"initial_denominator": "all planned initial attempts", "retry": "diagnostic only", "timeout_s": 0, "max_retries": 0},
        "artifact_policy": {"raw_http": True, "request_response_redacted": True, "ledger": "initial-ledger.jsonl", "config_snapshot": "start-record.json"},
        "preflight": {"service_ready": False, "provider_probe": False, "corpus_ready": False, "scope_verified": False},
    }


def _write_package(content: DatasetContent, output_root: Path, *, repo_root: Path) -> dict[str, Any]:
    package_dir = output_root / content.dataset_id
    package_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = package_dir / "corpus.jsonl"
    questions_path = package_dir / "questions.jsonl"
    gold_path = package_dir / "gold.jsonl"
    template_path = package_dir / "start-record.template.json"
    _write_jsonl(corpus_path, content.corpus)
    _write_jsonl(questions_path, content.questions)
    gold_rows = content.gold or _gold_rows(content.questions)
    _write_jsonl(gold_path, gold_rows)
    manifest = _base_manifest(content)
    manifest["counts"] = _counts(content)
    initial_artifacts = {
        name: _artifact_record(path, package_dir)
        for name, path in (("corpus.jsonl", corpus_path), ("questions.jsonl", questions_path), ("gold.jsonl", gold_path))
    }
    _write_json(template_path, _start_template(manifest, initial_artifacts))
    artifacts: dict[str, Any] = {
        name: _artifact_record(path, package_dir)
        for name, path in (("corpus.jsonl", corpus_path), ("questions.jsonl", questions_path), ("gold.jsonl", gold_path), ("start-record.template.json", template_path))
    }
    artifacts["source_inputs"] = _source_records(content.source_inputs)
    manifest["artifacts"] = artifacts
    _write_json(package_dir / "manifest.json", manifest)
    validation = validate_package(package_dir, repo_root=repo_root)
    if not validation["valid"]:
        raise EvalReadyError(f"generated {content.dataset_id} package failed validation: {'; '.join(validation['errors'])}")
    return validation


def build_dataset(dataset_id: str, output_root: str | Path = DEFAULT_OUTPUT, *, repo_root: str | Path = ROOT, strict_counts: bool = True) -> dict[str, Any]:
    canonical = canonical_dataset_id(dataset_id)
    if canonical not in BUILDERS:
        raise EvalReadyError(f"{canonical} is built by the optional document-dataset builder")
    repo = Path(repo_root).resolve()
    output = Path(output_root)
    if not output.is_absolute():
        output = repo / output
    content = BUILDERS[canonical](repo, strict_counts=strict_counts)
    return _write_package(content, output, repo_root=repo)


def delegate_document_dataset(dataset_id: str, output_root: Path, repo_root: Path) -> dict[str, Any]:
    script = HERE / "competitor_eval_document_datasets.py"
    if not script.is_file():
        raise EvalReadyError(
            f"selected document dataset '{dataset_id}' requires sibling {script.name}, but it was not found; "
            "select only the four core datasets or add the document-dataset builder"
        )
    command = [sys.executable, str(script), "build", "--dataset", dataset_id, "--output", str(output_root), "--repo-root", str(repo_root)]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise EvalReadyError(f"document dataset builder failed for {dataset_id} (exit {completed.returncode}): {detail}")
    return {"dataset_id": dataset_id, "delegated": True, "output": str(output_root), "stdout": completed.stdout.strip()}


def build_selected(dataset_ids: Sequence[str], output_root: str | Path = DEFAULT_OUTPUT, *, repo_root: str | Path = ROOT, strict_counts: bool = True) -> list[dict[str, Any]]:
    repo = Path(repo_root).resolve()
    output = Path(output_root)
    if not output.is_absolute():
        output = repo / output
    results: list[dict[str, Any]] = []
    for dataset_id in expand_dataset_selection(dataset_ids):
        if dataset_id in BUILDERS:
            results.append(build_dataset(dataset_id, output, repo_root=repo, strict_counts=strict_counts))
        else:
            results.append(delegate_document_dataset(dataset_id, output, repo))
    return results


def _validate_hash(path: Path, expected: Any, errors: list[str], label: str) -> None:
    if not path.is_file():
        errors.append(f"{label}: referenced file does not exist: {path}")
        return
    actual = sha256_file(path)
    if str(expected) != actual:
        errors.append(f"{label}: sha256 mismatch for {path}: expected {expected}, got {actual}")


def validate_package(package_dir: str | Path, *, repo_root: str | Path = ROOT) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    repo = Path(repo_root).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return {"valid": False, "package": str(package), "status": "BLOCKED", "errors": [f"manifest.json missing: {manifest_path}"], "warnings": [], "counts": {}}
    try:
        manifest = _json_load(manifest_path)
    except EvalReadyError as exc:
        return {"valid": False, "package": str(package), "status": "BLOCKED", "errors": [str(exc)], "warnings": [], "counts": {}}
    if not isinstance(manifest, Mapping):
        return {"valid": False, "package": str(package), "status": "BLOCKED", "errors": ["manifest.json must be an object"], "warnings": [], "counts": {}}
    if manifest.get("schema_version") != SCHEMA_VERSION and manifest.get("package_schema") != SCHEMA_VERSION:
        errors.append(f"unsupported package schema: {manifest.get('schema_version')!r}")
    dataset_id = str(manifest.get("dataset_id", ""))
    if dataset_id not in (*CORE_DATASET_IDS, *DOCUMENT_DATASET_IDS):
        errors.append(f"unknown dataset_id: {dataset_id!r}")
    status = str(manifest.get("status", ""))
    if status not in ALLOWED_STATUSES:
        errors.append(f"invalid status: {status!r}")
    if not isinstance(manifest.get("source_complete"), bool):
        errors.append("source_complete must be boolean")
    providers = manifest.get("provider_selection")
    if not isinstance(providers, Mapping):
        errors.append("provider_selection must be an object")
    else:
        for category, expected in PROVIDER_SELECTION.items():
            actual = providers.get(category)
            if not isinstance(actual, Mapping):
                errors.append(f"provider_selection.{category} missing")
                continue
            for key in ("provider", "model"):
                if actual.get(key) != expected[key]:
                    errors.append(f"provider_selection.{category}.{key} must be {expected[key]!r}")
            if category == "embedding" and actual.get("dimension") != 1024:
                errors.append("provider_selection.embedding.dimension must be 1024")
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        errors.append("artifacts must be an object")
        artifacts = {}
    required_artifacts = ("corpus.jsonl", "questions.jsonl", "gold.jsonl", "start-record.template.json")
    for name in required_artifacts:
        record = artifacts.get(name)
        if not isinstance(record, Mapping):
            errors.append(f"artifacts.{name} missing")
            continue
        path = _resolve_path(str(record.get("path", "")), repo_root=repo, package_dir=package)
        _validate_hash(path, record.get("sha256"), errors, f"artifacts.{name}")
    source_records = artifacts.get("source_inputs", [])
    if not isinstance(source_records, list):
        errors.append("artifacts.source_inputs must be a list")
        source_records = []
    for index, record in enumerate(source_records):
        if not isinstance(record, Mapping):
            errors.append(f"artifacts.source_inputs[{index}] must be an object")
            continue
        path = _resolve_path(str(record.get("path", "")), repo_root=repo, package_dir=package)
        _validate_hash(path, record.get("sha256"), errors, f"artifacts.source_inputs[{index}]")

    corpus_path = package / "corpus.jsonl"
    questions_path = package / "questions.jsonl"
    gold_path = package / "gold.jsonl"
    try:
        corpus = _jsonl_load(corpus_path)
        questions = _jsonl_load(questions_path)
        gold = _jsonl_load(gold_path)
    except EvalReadyError as exc:
        errors.append(str(exc))
        corpus, questions, gold = [], [], []
    corpus_ids: set[str] = set()
    for index, row in enumerate(corpus):
        prefix = f"corpus.jsonl[{index}]"
        doc_id = str(row.get("doc_id", ""))
        if not doc_id:
            errors.append(f"{prefix}: doc_id missing")
        if doc_id in corpus_ids:
            errors.append(f"{prefix}: duplicate doc_id {doc_id!r}")
        corpus_ids.add(doc_id)
        if not str(row.get("title", "")).strip():
            errors.append(f"{prefix}: title missing")
        path_fields = [field for field in ("text_path", "binary_path") if row.get(field)]
        if len(path_fields) != 1:
            errors.append(f"{prefix}: exactly one text_path or binary_path is required")
        else:
            ref = _resolve_path(str(row[path_fields[0]]), repo_root=repo, package_dir=package)
            _validate_hash(ref, row.get("sha256"), errors, prefix)
        for key in ("media_type", "scope_id"):
            if not str(row.get(key, "")).strip():
                errors.append(f"{prefix}: {key} missing")
        if not isinstance(row.get("metadata"), Mapping):
            errors.append(f"{prefix}: metadata must be an object")
    question_ids: set[str] = set()
    for index, row in enumerate(questions):
        prefix = f"questions.jsonl[{index}]"
        question_id = str(row.get("question_id", ""))
        if not question_id:
            errors.append(f"{prefix}: question_id missing")
        if question_id in question_ids:
            errors.append(f"{prefix}: duplicate question_id {question_id!r}")
        question_ids.add(question_id)
        for key in ("question", "reference_answer", "question_type"):
            if key not in row:
                errors.append(f"{prefix}: {key} missing")
        if not isinstance(row.get("answerable"), bool):
            errors.append(f"{prefix}: answerable must be boolean")
        for key in ("scope_doc_ids", "gold_doc_ids", "gold_evidence"):
            if not isinstance(row.get(key), list):
                errors.append(f"{prefix}: {key} must be a list")
        if not isinstance(row.get("metadata"), Mapping):
            errors.append(f"{prefix}: metadata must be an object")
        scope_ids = set(str(value) for value in row.get("scope_doc_ids", []) if value is not None)
        gold_ids = set(str(value) for value in row.get("gold_doc_ids", []) if value is not None)
        if not scope_ids <= corpus_ids:
            errors.append(f"{prefix}: scope_doc_ids reference missing corpus ids: {sorted(scope_ids - corpus_ids)[:5]}")
        if not gold_ids <= corpus_ids:
            errors.append(f"{prefix}: gold_doc_ids reference missing corpus ids: {sorted(gold_ids - corpus_ids)[:5]}")
        if not gold_ids <= scope_ids:
            errors.append(f"{prefix}: gold_doc_ids must be within scope_doc_ids")
    gold_ids = {str(row.get("question_id", "")) for row in gold}
    if gold_ids != question_ids:
        errors.append("gold.jsonl question_id set does not match questions.jsonl")
    counts = manifest.get("counts", {}) if isinstance(manifest.get("counts"), Mapping) else {}
    expected_counts = {"documents": len(corpus), "questions": len(questions), "corpus_rows": len(corpus), "question_rows": len(questions)}
    for key, actual in expected_counts.items():
        if key in counts and counts.get(key) != actual:
            errors.append(f"manifest counts.{key}={counts.get(key)!r} does not match {actual}")
    if dataset_id == "enterprise-rag-bench" and bool(manifest.get("source_complete")):
        errors.append("EnterpriseRAG-Bench adapted slice cannot be source_complete")
    if dataset_id == "fab-bench" and bool(manifest.get("source_complete")):
        errors.append("FAB-Bench public-complete-evidence-only package cannot be source_complete")
    return {
        "valid": not errors,
        "package": str(package),
        "dataset_id": dataset_id,
        "status": status if not errors else "BLOCKED",
        "declared_status": status,
        "source_complete": manifest.get("source_complete"),
        "counts": {"documents": len(corpus), "questions": len(questions), "gold": len(gold)},
        "errors": errors,
        "warnings": warnings,
    }


def status_package(package_dir: str | Path, *, repo_root: str | Path = ROOT) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    validation = validate_package(package, repo_root=repo_root)
    if not (package / "manifest.json").is_file():
        validation["status"] = "BLOCKED"
        validation["reason"] = "manifest.json missing"
    return validation


def _known_package_ids(output: Path) -> list[str]:
    ids: list[str] = []
    for dataset_id in DATASET_ORDER:
        if (output / dataset_id / "manifest.json").is_file():
            ids.append(dataset_id)
    return ids


def status_root(output_root: str | Path = DEFAULT_OUTPUT, *, repo_root: str | Path = ROOT, dataset_ids: Sequence[str] | None = None) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    output = Path(output_root)
    if not output.is_absolute():
        output = repo / output
    ids = expand_dataset_selection(dataset_ids) if dataset_ids else _known_package_ids(output)
    return {"schema_version": SCHEMA_VERSION, "output": str(output.resolve()), "datasets": [status_package(output / dataset_id, repo_root=repo) for dataset_id in ids]}


def validate_root(output_root: str | Path = DEFAULT_OUTPUT, *, repo_root: str | Path = ROOT, dataset_ids: Sequence[str] | None = None) -> dict[str, Any]:
    result = status_root(output_root, repo_root=repo_root, dataset_ids=dataset_ids)
    result["valid"] = bool(result["datasets"]) and all(row.get("valid") for row in result["datasets"])
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, validate, or inspect competitor eval-ready packages")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "validate", "status"):
        sub = subparsers.add_parser(command)
        sub.add_argument("dataset_positional", nargs="?", help="optional positional dataset alias")
        sub.add_argument("--dataset", "--datasets", action="append", help="dataset id, comma-separated; repeatable (default: core for build)")
        sub.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        sub.add_argument("--repo-root", type=Path, default=ROOT)
        if command == "build":
            sub.add_argument("--allow-fixture", action="store_true", help="do not enforce production frozen counts; intended for temporary tests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else repo / args.output
    requested_datasets = list(args.dataset or [])
    if args.dataset_positional:
        requested_datasets.append(args.dataset_positional)
    try:
        if args.command == "build":
            results = build_selected(requested_datasets or ["core"], output, repo_root=repo, strict_counts=not args.allow_fixture)
            print(json.dumps({"command": "build", "schema_version": SCHEMA_VERSION, "output": str(output), "datasets": results}, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "validate":
            result = validate_root(output, repo_root=repo, dataset_ids=requested_datasets or None)
            print(json.dumps({"command": "validate", **result}, ensure_ascii=False, indent=2, default=str))
            return 0 if result["valid"] else 1
        result = status_root(output, repo_root=repo, dataset_ids=requested_datasets or None)
        print(json.dumps({"command": "status", **result}, ensure_ascii=False, indent=2, default=str))
        return 0
    except EvalReadyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
