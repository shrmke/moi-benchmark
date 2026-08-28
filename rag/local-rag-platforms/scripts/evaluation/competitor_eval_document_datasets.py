#!/usr/bin/env python3
"""Build eval-ready document and multimodal dataset fragments.

The builder is intentionally self contained.  It creates only small JSONL
indexes and JSON metadata; source PDFs, images, prepared JSONL files, and
parsed-text files are referenced in place and are never copied into the
package.

The public API is :func:`build_document_dataset` and
:func:`validate_manifest`.  The module can also be used as a small standalone
CLI::

    python competitor_eval_document_datasets.py build mmdocir \
        --repo-root /path/to/rag --package-root /tmp/mmdocir-fragment
    python competitor_eval_document_datasets.py validate /tmp/mmdocir-fragment
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "competitor-eval-ready-v1"
MODULE_DIR = Path(__file__).resolve().parent
EVALUATION_ORDER = [
    "WikiEval",
    "MMDocIR",
    "MMDocRAG",
    "DocBench",
    "MultiHop-RAG",
    "EnterpriseRAG-Bench",
    "FAB-Bench",
]
EXCLUDED_BENCHMARKS = ["OmniDocBench", "Lenovo"]

FIXED_PROVIDER: dict[str, Any] = {
    "model_egress": "external",
    "maas": {
        "provider": "maas",
        "judge_model": "glm-5.2",
        "generator_model": "glm-5.2",
        "multimodal_generator_model": "Qwen2.5-VL-72B-32K",
        "embedding_model": "bge-m3",
        "dimension": 1024,
    },
    "judge_model": "glm-5.2",
    "generator_model": "glm-5.2",
    "multimodal_generator_model": "Qwen2.5-VL-72B-32K",
    "embedding_model": "bge-m3",
    "embedding_dimension": 1024,
}

REQUIRED_CORPUS_FIELDS = (
    "doc_id",
    "title",
    "text_path",
    "binary_path",
    "media_type",
    "scope_id",
    "sha256",
    "metadata",
)
REQUIRED_QUESTION_FIELDS = (
    "question_id",
    "question",
    "reference_answer",
    "answerable",
    "scope_doc_ids",
    "gold_doc_ids",
    "gold_evidence",
    "question_type",
    "metadata",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class DatasetBuildError(RuntimeError):
    """Raised for a missing, ambiguous, or internally inconsistent source."""


def _canonical_dataset_id(dataset_id: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "", str(dataset_id).lower())
    aliases = {
        "mmdocir": "mmdocir",
        "mmdocrag": "mmdocrag",
        "docbench": "docbench",
    }
    if value not in aliases:
        raise DatasetBuildError(f"unsupported document dataset: {dataset_id!r}")
    return aliases[value]


def _as_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetBuildError(f"required source file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetBuildError(f"invalid JSON in {path}: {exc}") from exc


def _jsonl_load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetBuildError(f"required source JSONL is missing: {path}")
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DatasetBuildError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
                if not isinstance(item, dict):
                    raise DatasetBuildError(f"JSONL row at {path}:{line_number} is not an object")
                item["_source_line"] = line_number
                rows.append(item)
    except UnicodeDecodeError as exc:
        raise DatasetBuildError(f"source JSONL is not UTF-8: {path}") from exc
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            # The generic runner uses str.splitlines(), which also splits on
            # Unicode NEL/U+2028/U+2029. ASCII escaping keeps each JSON object
            # physically one line without changing its decoded text.
            handle.write(json.dumps(dict(row), ensure_ascii=True, sort_keys=True) + "\n")


def _clean_source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in row.items() if key != "_source_line"}


def _path_string(path: Path | None) -> str | None:
    return str(path.resolve()) if path is not None else None


def _resolve_reference(repo_root: Path, value: str | Path, *bases: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    candidates = [repo_root / candidate]
    candidates.extend(base / candidate for base in bases)
    for item in candidates:
        if item.exists():
            return item.resolve()
    # Returning the first canonical candidate gives the validator a useful
    # path in an explicit missing-entry record.
    return candidates[0].resolve()


def _relative_or_absolute(path: Path, base: Path) -> str:
    # Source references intentionally stay absolute.  Artifact references are
    # relative and are handled separately by _artifact_path.
    del base
    return str(path.resolve())


def _artifact_path(package_root: Path, condition: str, name: str) -> str:
    return str((Path(condition) / name).as_posix())


def _artifact_absolute(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _hash_file(path: Path, cache: dict[str, str]) -> str:
    path = path.resolve()
    if not path.is_file():
        raise DatasetBuildError(f"cannot hash missing source path: {path}")
    key = str(path)
    if key not in cache:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        cache[key] = digest.hexdigest()
    return cache[key]


def _sha_for_path(path: Path | None, cache: dict[str, str]) -> str | None:
    return _hash_file(path, cache) if path is not None and path.is_file() else None


def _record_sha256(*, source_sha256: str, doc_id: str, content: str | None) -> str:
    payload = json.dumps(
        {"content": content, "doc_id": doc_id, "source_sha256": source_sha256},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freeze_provider(provider_selection: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the runner's provider selection against the fixed contract.

    The runner has used both flat and nested provider selections over time.
    We accept either spelling but never allow a model override for this
    benchmark package.
    """

    if provider_selection is None:
        return json.loads(json.dumps(FIXED_PROVIDER))
    if not isinstance(provider_selection, Mapping):
        raise DatasetBuildError("provider selection is frozen: expected a mapping")

    def check(name: str, actual: Any, expected: Any) -> None:
        if actual is not None and actual != expected:
            raise DatasetBuildError(
                f"provider selection is frozen: {name} must be {expected!r}, got {actual!r}"
            )

    legacy_qianfan = provider_selection.get("qianfan")
    if legacy_qianfan is not None:
        raise DatasetBuildError("provider selection is frozen: qianfan is not admitted; use maas")
    maas = provider_selection.get("maas")
    if isinstance(maas, Mapping):
        check("maas.provider", maas.get("provider"), "maas")
        check("maas.judge_model", maas.get("judge_model"), FIXED_PROVIDER["judge_model"])
        check("maas.generator_model", maas.get("generator_model"), FIXED_PROVIDER["generator_model"])
        check(
            "maas.multimodal_generator_model",
            maas.get("multimodal_generator_model"),
            FIXED_PROVIDER["multimodal_generator_model"],
        )
        check("maas.embedding_model", maas.get("embedding_model"), FIXED_PROVIDER["embedding_model"])
        check("maas.dimension", maas.get("dimension"), FIXED_PROVIDER["embedding_dimension"])
    # Compatibility with competitor_eval_ready.py's public provider shape.
    text = provider_selection.get("text")
    if isinstance(text, Mapping):
        check("text.provider", text.get("provider"), "maas")
        check("text.model", text.get("model"), FIXED_PROVIDER["judge_model"])
    multimodal = provider_selection.get("multimodal")
    if isinstance(multimodal, Mapping):
        check("multimodal.provider", multimodal.get("provider"), "maas")
        check("multimodal.model", multimodal.get("model"), FIXED_PROVIDER["multimodal_generator_model"])
    embedding = provider_selection.get("embedding")
    if isinstance(embedding, Mapping):
        check("embedding.provider", embedding.get("provider"), "maas")
        check("embedding.model", embedding.get("model"), FIXED_PROVIDER["embedding_model"])
        check("embedding.dimension", embedding.get("dimension"), FIXED_PROVIDER["embedding_dimension"])

    aliases = {
        "judge_model": FIXED_PROVIDER["judge_model"],
        "judge-model": FIXED_PROVIDER["judge_model"],
        "generator_model": FIXED_PROVIDER["generator_model"],
        "generator-model": FIXED_PROVIDER["generator_model"],
        "llm_model": FIXED_PROVIDER["generator_model"],
        "embedding_model": FIXED_PROVIDER["embedding_model"],
        "embedding-model": FIXED_PROVIDER["embedding_model"],
        "embedding_dimension": FIXED_PROVIDER["embedding_dimension"],
        "embedding-dimension": FIXED_PROVIDER["embedding_dimension"],
        "dimension": FIXED_PROVIDER["embedding_dimension"],
    }
    for key, expected in aliases.items():
        check(key, provider_selection.get(key), expected)
    check("provider", provider_selection.get("provider"), "maas")
    check("model_egress", provider_selection.get("model_egress"), "external")
    return json.loads(json.dumps(FIXED_PROVIDER))


def _compat_provider_selection() -> dict[str, Any]:
    """Return the provider shape consumed by competitor_eval_ready.py."""

    return {
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


def _representation_contract(ingest_representation: str) -> dict[str, Any]:
    return {
        "version": "document-ingest-representation-v1",
        "frozen": True,
        "ingest_representation": ingest_representation,
        "source_document": {
            "transport": "referenced_path",
            "pdf_path_field": "binary_path",
            "sha256_basis": "source_file_bytes",
        },
        "candidate_text": {
            "transport": "inline_content",
            "media_type": "text/plain",
            "ingest_paths_must_be_null": True,
            "sha256_basis": "canonical_source_sha256_doc_id_content",
        },
        "candidate_image": {
            "transport": "inline_text_representation",
            "media_type": "text/plain",
            "source_asset_path": "metadata.source_binary_path",
            "ingest_paths_must_be_null": True,
            "sha256_basis": "canonical_source_sha256_doc_id_content",
        },
        "missing": {
            "transport": "inline_missing_placeholder",
            "sha256": None,
        },
    }


def _compat_counts(
    corpus: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    answerable = sum(bool(row.get("answerable")) for row in questions)
    return {
        "documents": len(corpus),
        "questions": len(questions),
        "corpus_rows": len(corpus),
        "question_rows": len(questions),
        "answerable_questions": answerable,
        "unanswerable_questions": len(questions) - answerable,
        "gold_document_links": sum(len(row.get("gold_doc_ids", [])) for row in questions),
        "gold_evidence": sum(len(row.get("gold_evidence", [])) for row in questions),
        "gold_rows": len(gold),
    }


def _add_compatibility_fields(
    manifest: dict[str, Any],
    *,
    readiness_status: str,
    corpus: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Add the common manifest aliases used by the main eval-ready builder."""

    manifest.update(
        {
            "schema_version": SCHEMA,
            "package_schema": SCHEMA,
            "status": readiness_status,
            # ``source_complete`` is scoped to the frozen local denominator;
            # paper/public gaps are represented independently below.
            "source_complete": True,
            "provider_selection": _compat_provider_selection(),
            "counts": _compat_counts(corpus, questions, gold),
            "metric_contract": {
                "denominator_policy": "current_local_frozen",
                "conditions": {
                    name: value.get("metric_contract", {})
                    for name, value in manifest.get("conditions", {}).items()
                    if isinstance(value, Mapping)
                },
            },
            "notes": [
                "No new data is downloaded by this builder.",
                "Paper-declared counts are reference metadata; local frozen files define READY denominators.",
                "OmniDocBench and Lenovo are excluded from this document-dataset package.",
            ],
        }
    )
    return manifest


def _metric_contract(dataset_id: str, condition: str) -> dict[str, Any]:
    if dataset_id == "mmdocir":
        if condition == "page":
            return {
                "protocol": "MMDocIR official retrieval protocol",
                "primary": ["page_recall@1", "page_recall@3", "page_recall@5"],
                "secondary": [
                    "page_recall@10",
                    "page_mrr",
                    "candidate_coverage",
                    "retrieval_failure_rate",
                ],
                "latency": ["retrieval_p50_ms", "retrieval_p95_ms"],
                "slices": ["domain", "question_type", "evidence_type", "scope_id"],
                "denominator": "all answerable official questions; unresolved gold candidates remain failures",
            }
        if condition == "layout":
            return {
                "protocol": "MMDocIR official retrieval protocol",
                "primary": ["layout_recall@1", "layout_recall@5", "layout_recall@10"],
                "secondary": [
                    "candidate_coverage",
                    "layout_bbox_overlap_recall",
                    "retrieval_failure_rate",
                ],
                "latency": ["retrieval_p50_ms", "retrieval_p95_ms"],
                "slices": ["domain", "question_type", "evidence_type", "scope_id"],
                "denominator": "all answerable official questions; missing exact layout candidates are explicit failures",
            }
        raise DatasetBuildError(f"unknown MMDocIR condition: {condition}")
    if dataset_id == "mmdocrag":
        return {
            "protocol": "MMDocRAG official quote-selection and answer-evaluation protocol",
            "primary": ["text_quote_recall@5", "image_quote_recall@5", "overall_quote_recall"],
            "secondary": [
                "text_quote_precision",
                "text_quote_recall",
                "text_quote_f1",
                "image_quote_precision",
                "image_quote_recall",
                "image_quote_f1",
                "overall_quote_precision",
                "overall_quote_recall",
                "overall_quote_f1",
                "bleu",
                "rouge_l",
                "judge_answer_correctness",
                "judge_answer_relevance",
                "judge_faithfulness",
                "judge_completeness",
                "judge_overall_quality",
                "candidate_coverage",
                "retrieval_failure_rate",
            ],
            "latency": ["retrieval_p50_ms", "retrieval_p95_ms", "generation_p50_ms", "generation_p95_ms"],
            "slices": [
                "candidate_pool",
                "pure_text",
                "multimodal",
                "ocr_text",
                "vlm_text",
                "evidence_modality_type",
                "question_type",
                "domain",
            ],
            "denominator": "all 2,000 official evaluation questions per condition; actual candidate shortfalls are retained",
            "judge_dimensions": [
                "answer_correctness",
                "answer_relevance",
                "faithfulness",
                "completeness",
                "overall_quality",
            ],
        }
    if dataset_id == "docbench":
        return {
            "protocol": "DocBench official question answering protocol",
            "primary": ["correctness_binary"],
            "secondary": [
                "correctness_by_question_type",
                "accepted_file_rate",
                "searchable_ready_rate",
                "unanswerable_detection_rate",
            ],
            "latency": ["end_to_end_p50_ms", "end_to_end_p95_ms"],
            "slices": ["question_type", "domain", "answerable", "scope_id"],
            "denominator": "all 1,102 official questions; unanswerable and una-web rows are not dropped",
            "condition": condition,
        }
    raise DatasetBuildError(f"unsupported dataset for metric contract: {dataset_id}")


def _scope(dataset_id: str, document_key: str) -> str:
    return f"{dataset_id}:doc:{document_key}"


def _source_metadata(**values: Any) -> dict[str, Any]:
    values.setdefault("path_mode", "reference")
    return values


def _corpus_row(
    *,
    doc_id: str,
    title: str,
    scope_id: str,
    media_type: str,
    text_path: Path | None,
    binary_path: Path | None,
    hash_path: Path | None,
    content: str | None,
    ingest_role: str,
    metadata: Mapping[str, Any],
    hash_cache: dict[str, str],
) -> dict[str, Any]:
    meta = dict(metadata)
    meta.setdefault("path_mode", "reference")
    meta["ingest_role"] = ingest_role
    if hash_path is not None:
        meta["sha256_path"] = _path_string(hash_path)
        source_sha = _sha_for_path(hash_path, hash_cache)
        if source_sha is None:
            sha = None
        else:
            meta["source_sha256"] = source_sha
            sha = (
                _record_sha256(source_sha256=source_sha, doc_id=doc_id, content=content)
                if content is not None
                else source_sha
            )
    else:
        sha = None
        meta.setdefault("source_status", "missing")
    if sha is None and meta.get("source_status") != "missing":
        raise DatasetBuildError(f"corpus row {doc_id} has no hashable source path")
    return {
        "doc_id": doc_id,
        "title": title,
        "text_path": _path_string(text_path),
        "binary_path": _path_string(binary_path),
        "content": content,
        # Runner transport role is normalized to candidate_text for both
        # textual quotes and image descriptions; metadata.ingest_role keeps
        # the original candidate modality frozen for analysis.
        "ingest_role": "candidate_text" if ingest_role == "candidate_image" else ingest_role,
        "media_type": media_type,
        "scope_id": scope_id,
        "sha256": sha,
        "metadata": meta,
    }


def _missing_corpus_row(
    *, doc_id: str, title: str, scope_id: str, media_type: str, reason: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    meta = dict(metadata)
    meta.update(
        {
            "source_status": "missing",
            "missing_reason": reason,
            "path_mode": "reference",
            "ingest_role": "missing",
        }
    )
    return {
        "doc_id": doc_id,
        "title": title,
        "text_path": None,
        "binary_path": None,
        # The generic runner requires either a resolvable path or inline
        # content.  Keep the source explicitly missing while making the
        # frozen-denominator placeholder loadable without inventing a file.
        "content": f"[MISSING SOURCE: {reason}]",
        "ingest_role": "missing",
        "media_type": media_type,
        "scope_id": scope_id,
        "sha256": None,
        "metadata": meta,
    }


def _question_row(
    *,
    question_id: str,
    question: str,
    answer: Any,
    answerable: bool,
    scope_doc_ids: Sequence[str],
    gold_doc_ids: Sequence[str],
    gold_evidence: Sequence[Mapping[str, Any]],
    question_type: Any,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question": str(question),
        "reference_answer": "" if answer is None else str(answer),
        "answerable": bool(answerable),
        "scope_doc_ids": list(dict.fromkeys(str(item) for item in scope_doc_ids)),
        "gold_doc_ids": list(dict.fromkeys(str(item) for item in gold_doc_ids)),
        "gold_evidence": [dict(item) for item in gold_evidence],
        "question_type": question_type if question_type is not None else "unknown",
        "metadata": dict(metadata),
    }


def _gold_row(question: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "question_id": question["question_id"],
        "scope_doc_ids": list(question["scope_doc_ids"]),
        "gold_doc_ids": list(question["gold_doc_ids"]),
        "gold_evidence": list(question["gold_evidence"]),
        "reference_answer": question["reference_answer"],
        "metadata": {"source_question_id": question["question_id"]},
    }


def _condition_fragment(
    *,
    dataset_id: str,
    dataset_name: str,
    package_root: Path,
    condition: str,
    protocol_tag: str,
    corpus: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    metric_contract: Mapping[str, Any],
    coverage: Mapping[str, Any] | None = None,
    candidate_pool: Mapping[str, Any] | None = None,
    scope_policy: str = "document-local",
    readiness_status: str = "READY",
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    condition_root = package_root / condition
    ingest_representation = (
        "source_document" if dataset_id == "docbench" and condition == "native-pdf" else "candidate_markdown"
    )
    representation_contract = _representation_contract(ingest_representation)
    corpus_by_id = {str(row["doc_id"]): row for row in corpus}
    corpus_ids_by_scope: dict[str, list[str]] = defaultdict(list)
    for row in corpus:
        corpus_ids_by_scope[str(row["scope_id"])].append(str(row["doc_id"]))
    runner_questions: list[dict[str, Any]] = []
    for source_question in questions:
        question = dict(source_question)
        document_ids: list[str] = []
        for scope_ref in question.get("scope_doc_ids", []):
            ref = str(scope_ref)
            if ref in corpus_ids_by_scope:
                document_ids.extend(corpus_ids_by_scope[ref])
            elif ref in corpus_by_id:
                document_ids.extend(corpus_ids_by_scope[str(corpus_by_id[ref]["scope_id"])])
        question["document_ids"] = list(dict.fromkeys(document_ids))
        runner_questions.append(question)
    _write_jsonl(condition_root / "corpus.jsonl", corpus)
    _write_jsonl(condition_root / "questions.jsonl", runner_questions)
    _write_jsonl(condition_root / "gold.jsonl", gold)
    counts: dict[str, Any] = {
        "corpus_rows": len(corpus),
        "questions": len(questions),
        "gold_rows": len(gold),
        "documents": len({row["scope_id"] for row in corpus}),
    }
    artifact_hash_cache: dict[str, str] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    for name in ("corpus.jsonl", "questions.jsonl", "gold.jsonl"):
        path = condition_root / name
        artifacts[name] = {
            "path": name,
            "sha256": _hash_file(path, artifact_hash_cache),
            "bytes": path.stat().st_size,
        }
    revision = "local-frozen-v1"
    split = "evaluation"
    start_record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "run_id": "<assigned-by-runner>",
        "status_at_start": "not_started",
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "dataset_revision": revision,
        "revision": revision,
        "split": split,
        "protocol_tag": protocol_tag,
        "condition": condition,
        "scope": "document_local",
        "ingest_representation": ingest_representation,
        "evaluation": {"ingest_representation": ingest_representation, "scope": "document_local"},
        "denominator_policy": "current_local_frozen",
        "planned_counts": counts,
        "provider_selection": _compat_provider_selection(),
        "provider_contract": json.loads(json.dumps(FIXED_PROVIDER)),
        "metric_contract": dict(metric_contract),
        "representation_contract": representation_contract,
        "data_hashes": {name: f"sha256:{entry['sha256']}" for name, entry in artifacts.items()},
        "preflight": {
            "package_validation_required": True,
            "external_model_egress": True,
            "no_downloads": True,
        },
    }
    _write_json(condition_root / "start-record.template.json", start_record)
    template_path = condition_root / "start-record.template.json"
    artifacts["start-record.template.json"] = {
        "path": "start-record.template.json",
        "sha256": _hash_file(template_path, artifact_hash_cache),
        "bytes": template_path.stat().st_size,
    }
    condition_manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "package_schema": SCHEMA,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "revision": revision,
        "dataset_revision": revision,
        "split": split,
        "protocol_tag": protocol_tag,
        "condition": condition,
        "scope": "document_local",
        "ingest_representation": ingest_representation,
        "evaluation": {"ingest_representation": ingest_representation, "scope": "document_local"},
        "scope_policy": scope_policy,
        "status": readiness_status,
        "readiness_status": readiness_status,
        "source_complete": True,
        "denominator_policy": "current_local_frozen",
        "corpus_path": "corpus.jsonl",
        "questions_path": "questions.jsonl",
        "gold_path": "gold.jsonl",
        "artifacts": artifacts,
        "artifact_hashes": {name: entry["sha256"] for name, entry in artifacts.items()},
        "counts": {**counts, "runner_documents": len(corpus)},
        "provider": json.loads(json.dumps(FIXED_PROVIDER)),
        "provider_selection": _compat_provider_selection(),
        "metric_contract": dict(metric_contract),
        "representation_contract": representation_contract,
        "limitations": list(limitations),
    }
    if coverage is not None:
        condition_manifest["coverage"] = dict(coverage)
    if candidate_pool is not None:
        condition_manifest["candidate_pool"] = dict(candidate_pool)
    _write_json(condition_root / "manifest.json", condition_manifest)
    result: dict[str, Any] = {
        "condition": condition,
        "protocol_tag": protocol_tag,
        "readiness_status": readiness_status,
        "denominator_policy": "current_local_frozen",
        "scope_policy": scope_policy,
        "paths": {
            "corpus": _artifact_path(package_root, condition, "corpus.jsonl"),
            "questions": _artifact_path(package_root, condition, "questions.jsonl"),
            "gold": _artifact_path(package_root, condition, "gold.jsonl"),
            "manifest": _artifact_path(package_root, condition, "manifest.json"),
            "start_record_template": _artifact_path(package_root, condition, "start-record.template.json"),
        },
        "corpus_path": _artifact_path(package_root, condition, "corpus.jsonl"),
        "questions_path": _artifact_path(package_root, condition, "questions.jsonl"),
        "gold_path": _artifact_path(package_root, condition, "gold.jsonl"),
        "manifest_path": _artifact_path(package_root, condition, "manifest.json"),
        "start_record_template_path": _artifact_path(package_root, condition, "start-record.template.json"),
        "counts": counts,
        "metric_contract": dict(metric_contract),
        "limitations": list(limitations),
    }
    if coverage is not None:
        result["coverage"] = dict(coverage)
    if candidate_pool is not None:
        result["candidate_pool"] = dict(candidate_pool)
    return result


def _candidate_paths(repo_root: Path, *relative: str) -> list[Path]:
    roots = [repo_root]
    rag = repo_root / "rag"
    if rag.is_dir() and rag not in roots:
        roots.append(rag)
    return [root.joinpath(*relative) for root in roots]


def _find_mmdocir_prepared(repo_root: Path) -> Path:
    candidates = [
        *_candidate_paths(repo_root, "runs", "stage1", "mmdocir"),
        *_candidate_paths(repo_root, "datasets", "downloads", "document-rag", "mmdocir", "prepared"),
        *_candidate_paths(repo_root, "mmdocir", "prepared"),
    ]
    prepared: list[Path] = []
    for candidate in candidates:
        if candidate.name == "prepared" and (candidate / "pages.jsonl").is_file():
            prepared.append(candidate)
        elif candidate.is_dir():
            prepared.extend(item for item in candidate.glob("*/artifacts/prepared") if (item / "pages.jsonl").is_file())
    if not prepared:
        raise DatasetBuildError(
            "MMDocIR prepared data not found; checked runs/stage1/mmdocir/*/artifacts/prepared and "
            "datasets/downloads/document-rag/mmdocir/prepared"
        )
    # Prefer the largest complete prepared run.  This avoids selecting an
    # older smoke run when the repository contains multiple stage artifacts.
    def score(path: Path) -> tuple[int, int, int, str]:
        manifest = path / "manifest.json"
        values = _json_load(manifest) if manifest.is_file() else {}
        if not isinstance(values, Mapping):
            values = {}
        return (
            int(values.get("questions", 0) or 0),
            int(values.get("pages", 0) or 0),
            int(values.get("layouts", 0) or 0),
            str(path),
        )

    return max(prepared, key=score).resolve()


def _find_data_root(repo_root: Path, dataset: str) -> Path:
    candidates = [
        *_candidate_paths(repo_root, "datasets", "downloads", "document-rag", dataset, "data"),
        *_candidate_paths(repo_root, dataset, "data"),
        *_candidate_paths(repo_root, "data"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    checked = ", ".join(str(item) for item in candidates)
    raise DatasetBuildError(f"{dataset} data root not found; checked: {checked}")


def _find_docbench_parsed(repo_root: Path) -> Path | None:
    candidates = [
        *_candidate_paths(repo_root, "outputs", "parsed-documents", "moi-ready-v1", "datasets", "docbench", "manifest.jsonl"),
        *_candidate_paths(repo_root, "datasets", "docbench", "parsed", "manifest.jsonl"),
    ]
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _resolve_image(
    raw_root: Path,
    image_ref: Any,
    extra_roots: Sequence[Path] = (),
    *,
    resolution_cache: dict[str, dict[str, Path]] | None = None,
) -> Path | None:
    if not image_ref:
        return None
    ref = Path(str(image_ref)).expanduser()
    if ref.is_absolute() and ref.is_file():
        return ref.resolve()
    candidates = [raw_root / ref, raw_root / "extracted" / ref, raw_root / "images" / ref]
    candidates.extend(root / ref for root in extra_roots)
    candidates.extend(root / ref.name for root in [raw_root, raw_root / "extracted", raw_root / "images", *extra_roots])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    # A final basename lookup is useful for the archive layouts used by the
    # original releases.  Index each root once: unresolved layout references
    # can occur tens of thousands of times, and a per-row rglob would turn a
    # valid local build into an accidental full-tree scan per row.
    cache = resolution_cache if resolution_cache is not None else {}
    for root in [raw_root, *extra_roots]:
        root = root.resolve()
        cache_key = str(root)
        if cache_key not in cache:
            index: dict[str, Path] = {}
            if root.is_dir():
                try:
                    for found in root.rglob("*"):
                        if found.is_file():
                            index.setdefault(found.name, found.resolve())
                except OSError:
                    index = {}
            cache[cache_key] = index
        found = cache[cache_key].get(ref.name)
        if found is not None:
            return found
    return None


def _lookup(mapping: Mapping[Any, Any], value: Any) -> Any:
    if value in mapping:
        return mapping[value]
    text = str(value)
    if text in mapping:
        return mapping[text]
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return mapping.get(integer)


def _float_list(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _boxes_equal(left: Any, right: Any, tolerance: float = 1e-4) -> bool:
    a = _float_list(left)
    b = _float_list(right)
    return a is not None and b is not None and all(abs(x - y) <= tolerance for x, y in zip(a, b))


def _box_iou(left: Any, right: Any) -> float:
    a = _float_list(left)
    b = _float_list(right)
    if a is None or b is None:
        return 0.0
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    intersection = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _mmdocir_layout_mapping(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = row.get("layout_mapping", [])
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _mmdocir_page_value(mapping: Mapping[str, Any]) -> Any:
    for key in ("page", "page_number", "page_id"):
        if key in mapping:
            return mapping[key]
    return None


def _mmdocir_layout_value(mapping: Mapping[str, Any]) -> Any:
    for key in ("bbox", "bounding_box", "box"):
        if key in mapping:
            return mapping[key]
    return None


def _mmdocir_page_key(file_id: Any, page_number: Any) -> tuple[str, str]:
    return str(file_id), str(page_number)


def _mmdocir_corpus(
    *,
    prepared: Path,
    raw_root: Path,
    pages: list[dict[str, Any]],
    layouts: list[dict[str, Any]],
    hash_cache: dict[str, str],
    resolution_cache: dict[str, dict[str, Path]],
    granularity: str,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str], dict[tuple[str, str], list[dict[str, Any]]]]:
    corpus: list[dict[str, Any]] = []
    page_lookup: dict[tuple[str, str], str] = {}
    layout_lookup: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    source_path = prepared / ("pages.jsonl" if granularity == "page" else "layouts.jsonl")
    source_rows = pages if granularity == "page" else layouts
    for index, row in enumerate(source_rows):
        raw_metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        file_id = row.get("file_id", raw_metadata.get("file_id", "unknown"))
        page_number = row.get("page_number", raw_metadata.get("page_id", "unknown"))
        source_id = str(row.get("id", f"{granularity}-{index}"))
        if granularity == "page":
            doc_id = f"mmdocir:page:{source_id}"
            page_lookup[_mmdocir_page_key(file_id, page_number)] = doc_id
            binary = _resolve_image(
                raw_root,
                raw_metadata.get("image_path"),
                resolution_cache=resolution_cache,
            )
            hash_path = binary if binary is not None else source_path
            metadata = _source_metadata(
                dataset="MMDocIR",
                granularity="page",
                source_path=_path_string(source_path),
                source_record_locator={"line": row.get("_source_line", index + 1), "id": source_id},
                source_id=source_id,
                file_id=str(file_id),
                page_number=page_number,
                doc_name=raw_metadata.get("doc_name", str(file_id)),
                domain=raw_metadata.get("domain"),
                image_path=raw_metadata.get("image_path"),
                source_binary_path=_path_string(binary),
                binary_status="ok" if binary is not None else "missing",
            )
            corpus.append(
                _corpus_row(
                    doc_id=doc_id,
                    title=f"{raw_metadata.get('doc_name', file_id)} page {page_number}",
                    scope_id=_scope("mmdocir", str(file_id)),
                    media_type="text/plain",
                    text_path=None,
                    binary_path=None,
                    hash_path=source_path,
                    content=_candidate_text(
                        row.get("content"),
                        fallback=f"[EMPTY MMDOCIR PAGE CANDIDATE: {source_id}]",
                    ),
                    ingest_role="candidate_text",
                    metadata=metadata,
                    hash_cache=hash_cache,
                )
            )
        else:
            doc_id = f"mmdocir:layout:{source_id}"
            layout_lookup[_mmdocir_page_key(file_id, page_number)].append(
                {"doc_id": doc_id, "row": row, "metadata": raw_metadata}
            )
            binary = _resolve_image(
                raw_root,
                raw_metadata.get("image_path"),
                resolution_cache=resolution_cache,
            )
            metadata = _source_metadata(
                dataset="MMDocIR",
                granularity="layout",
                source_path=_path_string(source_path),
                source_record_locator={"line": row.get("_source_line", index + 1), "id": source_id},
                source_id=source_id,
                file_id=str(file_id),
                page_number=page_number,
                doc_name=raw_metadata.get("doc_name", str(file_id)),
                domain=raw_metadata.get("domain"),
                layout_id=raw_metadata.get("layout_id"),
                layout_type=raw_metadata.get("layout_type"),
                bbox=raw_metadata.get("bbox"),
                page_size=raw_metadata.get("page_size"),
                image_path=raw_metadata.get("image_path"),
                source_binary_path=_path_string(binary),
                binary_status="ok" if binary is not None else "missing",
            )
            corpus.append(
                _corpus_row(
                    doc_id=doc_id,
                    title=f"{raw_metadata.get('doc_name', file_id)} page {page_number} layout {raw_metadata.get('layout_id', source_id)}",
                    scope_id=_scope("mmdocir", str(file_id)),
                    media_type="text/plain",
                    text_path=None,
                    binary_path=None,
                    hash_path=source_path,
                    content=_candidate_text(
                        row.get("content"),
                        fallback=f"[EMPTY MMDOCIR LAYOUT CANDIDATE: {source_id}]",
                    ),
                    ingest_role="candidate_text",
                    metadata=metadata,
                    hash_cache=hash_cache,
                )
            )
    return corpus, page_lookup, layout_lookup


def _mmdocir_questions(
    *,
    questions: list[dict[str, Any]],
    page_lookup: Mapping[tuple[str, str], str],
    layout_lookup: Mapping[tuple[str, str], list[dict[str, Any]]],
    condition: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    output: list[dict[str, Any]] = []
    missing = 0
    total_evidence = 0
    for index, row in enumerate(questions):
        file_id = str(row.get("file_id", row.get("doc_name", "unknown")))
        scope_id = _scope("mmdocir", file_id)
        question_id = f"mmdocir:{condition}:q:{row.get('id', index)}"
        # ``scope_doc_ids`` names the document-local scope, not every
        # candidate row in that scope.  Candidate page/layout IDs are kept in
        # Gold and evidence below; the validator expands this scope over the
        # corpus rows before checking those references.
        scope_doc_ids = [scope_id]
        evidence: list[dict[str, Any]] = []
        gold_ids: list[str] = []
        if condition == "page":
            page_values = row.get("page_ids", [])
            if not isinstance(page_values, list):
                page_values = [page_values]
            for page_value in page_values:
                total_evidence += 1
                candidate = page_lookup.get(_mmdocir_page_key(file_id, page_value))
                if candidate is None:
                    missing += 1
                    evidence.append(
                        {
                            "status": "missing_candidate",
                            "doc_id": None,
                            "locator": {"file_id": file_id, "page": page_value},
                            "missing_reason": "page candidate is absent from prepared pages.jsonl",
                        }
                    )
                else:
                    gold_ids.append(candidate)
                    evidence.append(
                        {
                            "status": "resolved",
                            "doc_id": candidate,
                            "locator": {"file_id": file_id, "page": page_value},
                        }
                    )
        else:
            mappings = _mmdocir_layout_mapping(row)
            for mapping in mappings:
                total_evidence += 1
                page_value = _mmdocir_page_value(mapping)
                candidates = layout_lookup.get(_mmdocir_page_key(file_id, page_value), [])
                selected: Mapping[str, Any] | None = None
                for candidate in candidates:
                    candidate_meta = candidate.get("metadata", {})
                    candidate_bbox = candidate_meta.get("bbox")
                    if _boxes_equal(candidate_bbox, _mmdocir_layout_value(mapping)):
                        selected = candidate
                        break
                match_type = "exact_bbox"
                if selected is None and candidates:
                    scored = [
                        (  # type: ignore[misc]
                            _box_iou(candidate.get("metadata", {}).get("bbox"), _mmdocir_layout_value(mapping)),
                            candidate,
                        )
                        for candidate in candidates
                    ]
                    best_iou, best_candidate = max(scored, key=lambda item: item[0])
                    if best_iou >= 0.98:
                        selected = best_candidate
                        match_type = "iou>=0.98"
                if selected is None:
                    missing += 1
                    evidence.append(
                        {
                            "status": "missing_candidate",
                            "doc_id": None,
                            "locator": {"file_id": file_id, "page": page_value, "bbox": _mmdocir_layout_value(mapping)},
                            "missing_reason": "layout bbox has no exact or high-IoU prepared candidate",
                        }
                    )
                else:
                    candidate_id = str(selected["doc_id"])
                    gold_ids.append(candidate_id)
                    evidence.append(
                        {
                            "status": "resolved",
                            "doc_id": candidate_id,
                            "match": match_type,
                            "locator": {"file_id": file_id, "page": page_value, "bbox": _mmdocir_layout_value(mapping)},
                        }
                    )
        question = _question_row(
            question_id=question_id,
            question=row.get("question", ""),
            answer=row.get("answer", ""),
            answerable=True,
            scope_doc_ids=scope_doc_ids,
            gold_doc_ids=gold_ids,
            gold_evidence=evidence,
            question_type="retrieval",
            metadata=_source_metadata(
                dataset="MMDocIR",
                condition=condition,
                protocol_tag="OFFICIAL",
                qa_protocol_tag="ADAPTED_PROTOCOL",
                source_question_id=row.get("id", index),
                source_record_locator={"line": row.get("_source_line", index + 1), "id": row.get("id", index)},
                file_id=file_id,
                doc_name=row.get("doc_name"),
                domain=row.get("domain"),
                evidence_type=row.get("evidence_type"),
                page_ids=row.get("page_ids", []),
                layout_mapping=row.get("layout_mapping", []),
                scope_policy="document-local",
                scope_id=scope_id,
                scope_candidate_count=len(scope_doc_ids),
            ),
        )
        output.append(question)
    coverage = {
        "gold_references_total": total_evidence,
        "gold_references_resolved": total_evidence - missing,
        "gold_references_missing": missing,
        "questions": len(output),
    }
    return output, [_gold_row(question) for question in output], coverage


def _build_mmdocir(repo_root: Path, package_root: Path, hash_cache: dict[str, str]) -> dict[str, Any]:
    prepared = _find_mmdocir_prepared(repo_root)
    raw_root = _find_data_root(repo_root, "mmdocir")
    pages = _jsonl_load(prepared / "pages.jsonl")
    layouts = _jsonl_load(prepared / "layouts.jsonl")
    questions = _jsonl_load(prepared / "questions.jsonl")
    resolution_cache: dict[str, dict[str, Path]] = {}
    prepared_manifest_path = prepared / "manifest.json"
    prepared_manifest = _json_load(prepared_manifest_path) if prepared_manifest_path.is_file() else {}
    if isinstance(prepared_manifest, Mapping):
        expected_pairs = {
            "selected_documents": len({str(row.get("file_id")) for row in pages}),
            "questions": len(questions),
            "pages": len(pages),
            "layouts": len(layouts),
        }
        for key, actual in expected_pairs.items():
            if key in prepared_manifest and int(prepared_manifest[key]) != actual:
                raise DatasetBuildError(
                    f"MMDocIR prepared count mismatch for {key}: manifest={prepared_manifest[key]}, actual={actual}"
                )
    page_corpus, page_lookup, _ = _mmdocir_corpus(
        prepared=prepared,
        raw_root=raw_root,
        pages=pages,
        layouts=[],
        hash_cache=hash_cache,
        resolution_cache=resolution_cache,
        granularity="page",
    )
    layout_corpus, _, layout_lookup = _mmdocir_corpus(
        prepared=prepared,
        raw_root=raw_root,
        pages=[],
        layouts=layouts,
        hash_cache=hash_cache,
        resolution_cache=resolution_cache,
        granularity="layout",
    )
    page_questions, page_gold, page_coverage = _mmdocir_questions(
        questions=questions, page_lookup=page_lookup, layout_lookup={}, condition="page"
    )
    layout_questions, layout_gold, layout_coverage = _mmdocir_questions(
        questions=questions, page_lookup={}, layout_lookup=layout_lookup, condition="layout"
    )
    conditions = {
        "page": _condition_fragment(
            dataset_id="mmdocir",
            dataset_name="MMDocIR",
            package_root=package_root,
            condition="page",
            protocol_tag="OFFICIAL",
            corpus=page_corpus,
            questions=page_questions,
            gold=page_gold,
            metric_contract=_metric_contract("mmdocir", "page"),
            coverage={
                **page_coverage,
                "candidate_count": len(page_corpus),
                "candidate_granularity": "page",
                "count_basis": "current_local_frozen",
                "source_prepared": _path_string(prepared),
                "source_status": "complete",
            },
            limitations=["Corpus rows reference the prepared MMDocIR JSONL and extracted page assets in place."],
        ),
        "layout": _condition_fragment(
            dataset_id="mmdocir",
            dataset_name="MMDocIR",
            package_root=package_root,
            condition="layout",
            protocol_tag="OFFICIAL",
            corpus=layout_corpus,
            questions=layout_questions,
            gold=layout_gold,
            metric_contract=_metric_contract("mmdocir", "layout"),
            coverage={
                **layout_coverage,
                "candidate_count": len(layout_corpus),
                "candidate_granularity": "layout",
                "count_basis": "current_local_frozen",
                "source_prepared": _path_string(prepared),
                "source_status": "complete",
            },
            limitations=["Layout binary assets are optional source references; unresolved Gold layout matches remain explicit failures."],
        ),
    }
    return _add_compatibility_fields(
        {
            "schema": SCHEMA,
            "dataset_id": "mmdocir",
            "dataset_name": "MMDocIR",
            "source": {
                "prepared_root": _path_string(prepared),
                "raw_root": _path_string(raw_root),
                "path_mode": "reference",
            },
            "protocol": "OFFICIAL",
            "protocol_tag": "OFFICIAL",
            "readiness_status": "READY_ADAPTED",
            "readiness": {
                "status": "READY_ADAPTED",
                "denominator": "current_local_frozen",
                "blocking": [],
                "limitations": [
                    "The retrieval conditions use the currently available prepared MMDocIR files.",
                    "QA fields and downstream QA metrics are an ADAPTED_PROTOCOL extension.",
                ],
            },
            "denominator_policy": {
                "name": "current_local_frozen",
                "paper_declared_counts_are_reference_only": True,
            },
            "evaluation_order": list(EVALUATION_ORDER),
            "evaluation_order_index": EVALUATION_ORDER.index("MMDocIR"),
            "excluded_benchmarks": list(EXCLUDED_BENCHMARKS),
            "scope_policy": "document-local",
            "official_target_counts": {"documents": 313, "page_candidates": 20395, "layout_candidates": 170338, "questions": 1658},
            "dataset_counts": {
                "documents": len({str(row.get("file_id")) for row in pages}),
                "page_candidates": len(pages),
                "layout_candidates": len(layouts),
                "questions": len(questions),
            },
            "qa": {
                "protocol_tag": "ADAPTED_PROTOCOL",
                "reason": "MMDocIR's released retrieval benchmark is extended with answer/evidence QA fields for this package",
                "metric_contract": {
                    "primary": ["contains_gold", "normalized_em", "token_f1"],
                    "secondary": ["answer_correctness", "faithfulness", "citation_support", "qa_failure_rate"],
                    "denominator": "all released MMDocIR questions in each source condition",
                },
                "source_conditions": ["page", "layout"],
            },
            "conditions": conditions,
        },
        readiness_status="READY_ADAPTED",
        corpus=page_corpus,
        questions=page_questions,
        gold=page_gold,
    )


def _normal_name(value: Any) -> str:
    text = Path(str(value)).name.lower()
    if text.endswith(".pdf"):
        text = text[:-4]
    return text


def _mmdocrag_doc_map(data_root: Path) -> dict[str, Path]:
    pdf_root = data_root / "doc_pdfs"
    if not pdf_root.is_dir():
        raise DatasetBuildError(f"MMDocRAG PDF root is missing: {pdf_root}")
    result: dict[str, Path] = {}
    for path in sorted(pdf_root.rglob("*.pdf")):
        key = _normal_name(path.name)
        if key in result and result[key] != path:
            raise DatasetBuildError(f"duplicate MMDocRAG PDF basename: {key}")
        result[key] = path.resolve()
    if not result:
        raise DatasetBuildError(f"MMDocRAG PDF root contains no PDFs: {pdf_root}")
    return result


def _declared_mmdocrag_documents(data_root: Path, local_count: int, question_count: int) -> int:
    manifest_path = data_root / "dataset_manifest.json"
    if manifest_path.is_file():
        value = _json_load(manifest_path)
        if isinstance(value, Mapping):
            for key in ("declared_pdf_count", "pdf_count", "documents", "num_documents"):
                if key in value:
                    try:
                        return int(value[key])
                    except (TypeError, ValueError) as exc:
                        raise DatasetBuildError(f"invalid MMDocRAG declared document count in {manifest_path}") from exc
    # The official evaluation release declares 222 source PDFs while the
    # downloadable doc_pdfs directory contains 220.
    if question_count == 2000:
        return 222
    return local_count


def _mmdocrag_image(
    data_root: Path,
    image_ref: Any,
    *,
    resolution_cache: dict[str, dict[str, Path]],
) -> Path | None:
    return _resolve_image(
        data_root,
        image_ref,
        [data_root / "images"],
        resolution_cache=resolution_cache,
    )


def _mmdocrag_quote_id(modality: str, quote_id: Any, existing: Mapping[str, Any]) -> str:
    base = f"mmdocrag:quote:{modality}:{quote_id}"
    if base not in existing:
        return base
    suffix = 2
    while f"{base}:{suffix}" in existing:
        suffix += 1
    return f"{base}:{suffix}"


def _candidate_text(*values: Any, fallback: str) -> str:
    for value in values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True).strip()
        if text:
            return text
    return fallback


def _mmdocrag_build_condition(
    *,
    data_root: Path,
    package_root: Path,
    condition: str,
    rows: list[dict[str, Any]],
    local_docs: Mapping[str, Path],
    declared_documents: int,
    hash_cache: dict[str, str],
    resolution_cache: dict[str, dict[str, Path]],
) -> tuple[dict[str, Any], set[str]]:
    candidate_pool = (
        {"name": "C15", "text": 10, "image": 5, "total": 15}
        if condition == "c15"
        else {"name": "C20", "text": 12, "image": 8, "total": 20}
    )
    eval_path = data_root / ("evaluation_15.jsonl" if condition == "c15" else "evaluation_20.jsonl")
    eval_path = eval_path.resolve()
    eval_doc_names = {_normal_name(row.get("doc_name", "unknown")) for row in rows}
    all_names = sorted(set(local_docs) | eval_doc_names)
    corpus: list[dict[str, Any]] = []
    scope_by_name: dict[str, str] = {}
    for name in all_names:
        display_name = name
        scope_id = _scope("mmdocrag", display_name)
        scope_by_name[name] = scope_id
        pdf = local_docs.get(name)
        if pdf is None:
            corpus.append(
                _missing_corpus_row(
                    doc_id=f"mmdocrag:doc:{display_name}",
                    title=display_name,
                    scope_id=scope_id,
                    media_type="application/pdf",
                    reason="evaluation document name has no matching local PDF",
                    metadata=_source_metadata(
                        dataset="MMDocRAG",
                        source_status="missing",
                        source_name=display_name,
                        official_evaluation_reference=True,
                    ),
                )
            )
        else:
            corpus.append(
                _corpus_row(
                    doc_id=f"mmdocrag:doc:{display_name}",
                    title=display_name,
                    scope_id=scope_id,
                    media_type="application/pdf",
                    text_path=None,
                    binary_path=pdf,
                    hash_path=pdf,
                    content=None,
                    ingest_role="source_document",
                    metadata=_source_metadata(
                        dataset="MMDocRAG",
                        source_path=_path_string(pdf),
                        source_name=display_name,
                        source_status="ok",
                    ),
                    hash_cache=hash_cache,
                )
            )

    named_missing = sum(1 for name in all_names if name not in local_docs)
    missing_slots = max(0, declared_documents - len(local_docs) - named_missing)
    for index in range(missing_slots):
        display_name = f"__missing_source_{index + 1:03d}"
        scope_id = _scope("mmdocrag", display_name)
        scope_by_name[display_name] = scope_id
        corpus.append(
            _missing_corpus_row(
                doc_id=f"mmdocrag:doc:{display_name}",
                title=display_name,
                scope_id=scope_id,
                media_type="application/pdf",
                reason="official MMDocRAG document declaration exceeds local PDF inventory; source name unavailable",
                metadata=_source_metadata(
                    dataset="MMDocRAG",
                    source_name=display_name,
                    official_declared_missing=True,
                ),
            )
        )

    corpus_by_quote: dict[str, str] = {}
    questions: list[dict[str, Any]] = []
    gold_missing = 0
    image_missing = 0
    actual_candidate_total = 0
    actual_text_total = 0
    actual_image_total = 0
    gold_total = 0
    gold_resolved = 0
    for row_index, row in enumerate(rows):
        raw_doc_name = str(row.get("doc_name", "unknown"))
        doc_name = _normal_name(raw_doc_name)
        scope_id = scope_by_name.get(doc_name, _scope("mmdocrag", doc_name))
        parent_doc_id = f"mmdocrag:doc:{doc_name}"
        quote_candidates: list[tuple[str, Mapping[str, Any]]] = []
        # Scope references are document-local scope IDs.  Quote candidate IDs
        # remain separate Gold references, even though they share this scope.
        scope_doc_ids: list[str] = [scope_id]
        text_quotes = row.get("text_quotes", [])
        image_quotes = row.get("img_quotes", [])
        if not isinstance(text_quotes, list):
            text_quotes = []
        if not isinstance(image_quotes, list):
            image_quotes = []
        actual_text_total += len(text_quotes)
        actual_image_total += len(image_quotes)
        for quote in text_quotes:
            if not isinstance(quote, Mapping):
                continue
            quote_candidates.append(("text", quote))
        for quote in image_quotes:
            if not isinstance(quote, Mapping):
                continue
            quote_candidates.append(("image", quote))
        actual_candidate_total += len(quote_candidates)
        quote_ids_for_question: dict[str, list[str]] = defaultdict(list)
        for modality, quote in quote_candidates:
            original_quote_id = str(quote.get("quote_id", f"{modality}-{len(quote_ids_for_question)}"))
            quote_doc_id = _mmdocrag_quote_id(modality, original_quote_id, corpus_by_quote)
            corpus_by_quote[quote_doc_id] = original_quote_id
            quote_ids_for_question[original_quote_id].append(quote_doc_id)
            image_path = (
                _mmdocrag_image(
                    data_root,
                    quote.get("img_path"),
                    resolution_cache=resolution_cache,
                )
                if modality == "image"
                else None
            )
            if modality == "image" and image_path is None:
                image_missing += 1
            metadata = _source_metadata(
                dataset="MMDocRAG",
                condition=condition,
                quote_id=original_quote_id,
                quote_modality=modality,
                source_eval_path=_path_string(eval_path),
                source_record_locator={"line": row.get("_source_line", row_index + 1), "q_id": row.get("q_id")},
                doc_name=raw_doc_name,
                page_id=quote.get("page_id"),
                layout_id=quote.get("layout_id"),
                img_path=quote.get("img_path"),
                source_binary_path=_path_string(image_path),
                img_description=quote.get("img_description"),
                quote_text=quote.get("text"),
                original_quote=dict(quote),
                binary_status="ok" if image_path is not None else ("not_applicable" if modality == "text" else "missing"),
            )
            if modality == "text":
                content = _candidate_text(
                    quote.get("text"),
                    quote.get("quote_text"),
                    fallback=f"[EMPTY TEXT QUOTE: {original_quote_id}]",
                )
                ingest_role = "candidate_text"
                title = f"{raw_doc_name} text quote {original_quote_id}"
            else:
                content = _candidate_text(
                    quote.get("img_description"),
                    quote.get("ocr_text"),
                    quote.get("ocr"),
                    quote.get("description"),
                    quote.get("text"),
                    fallback=f"[IMAGE CANDIDATE: {original_quote_id}]",
                )
                ingest_role = "candidate_image"
                title = f"{raw_doc_name} image quote {original_quote_id}"
            corpus.append(
                _corpus_row(
                    doc_id=quote_doc_id,
                    title=title,
                    scope_id=scope_id,
                    media_type="text/plain",
                    text_path=None,
                    binary_path=None,
                    hash_path=eval_path,
                    content=content,
                    ingest_role=ingest_role,
                    metadata=metadata,
                    hash_cache=hash_cache,
                )
            )
        raw_gold = row.get("gold_quotes", [])
        if not isinstance(raw_gold, list):
            raw_gold = [raw_gold]
        gold_evidence: list[dict[str, Any]] = []
        gold_ids: list[str] = [parent_doc_id]
        for quote_id in raw_gold:
            gold_total += 1
            quote_id_string = str(quote_id)
            choices = quote_ids_for_question.get(quote_id_string, [])
            if choices:
                gold_resolved += 1
                chosen = choices[0]
                gold_ids.append(chosen)
                corpus_row = next(item for item in corpus if item["doc_id"] == chosen)
                gold_evidence.append(
                    {
                        "quote_id": quote_id_string,
                        "doc_id": chosen,
                        "status": "resolved" if corpus_row["metadata"].get("source_status") != "missing" else "missing",
                        "modality": corpus_row["metadata"].get("quote_modality"),
                        "text": corpus_row["metadata"].get("quote_text"),
                        "img_path": corpus_row["metadata"].get("img_path"),
                        "original_quote": corpus_row["metadata"].get("original_quote"),
                    }
                )
                if corpus_row["metadata"].get("source_status") == "missing":
                    gold_missing += 1
            else:
                gold_missing += 1
                gold_evidence.append(
                    {
                        "quote_id": quote_id_string,
                        "doc_id": None,
                        "status": "missing",
                        "missing_reason": "gold quote id is absent from this condition's candidate quotes",
                    }
                )
        question = _question_row(
            question_id=f"mmdocrag:{condition}:q:{row.get('q_id', row_index)}",
            question=row.get("question", ""),
            answer=row.get("answer_short") or row.get("answer_interleaved", ""),
            answerable=True,
            scope_doc_ids=scope_doc_ids,
            gold_doc_ids=gold_ids,
            gold_evidence=gold_evidence,
            question_type=row.get("question_type", "unknown"),
            metadata=_source_metadata(
                dataset="MMDocRAG",
                condition=condition,
                candidate_pool=candidate_pool["name"],
                candidate_pool_target=candidate_pool,
                actual_candidate_count=len(quote_candidates),
                actual_text_candidate_count=len(text_quotes),
                actual_image_candidate_count=len(image_quotes),
                candidate_shortfall=max(0, candidate_pool["total"] - len(quote_candidates)),
                source_record_locator={"line": row.get("_source_line", row_index + 1), "q_id": row.get("q_id")},
                source_eval_path=_path_string(eval_path),
                q_id=row.get("q_id"),
                old_id=row.get("old_id"),
                domain=row.get("domain"),
                evidence_modality_type=row.get("evidence_modality_type"),
                answer_interleaved=row.get("answer_interleaved"),
                scope_policy="document-local",
                scope_id=scope_id,
                scope_candidate_count=len(scope_doc_ids),
            ),
        )
        questions.append(question)
    missing_source_entries = sum(1 for row in corpus if row["metadata"].get("source_status") == "missing" and row["metadata"].get("official_declared_missing"))
    # Named missing documents are also source entries, but quote assets are not
    # documents and must not inflate this coverage number.
    missing_source_entries += sum(
        1
        for row in corpus
        if row["metadata"].get("source_status") == "missing"
        and row["media_type"] == "application/pdf"
        and not row["metadata"].get("official_declared_missing")
    )
    coverage = {
        "official_questions": 2000,
        "questions": len(questions),
        "count_basis": "current_local_frozen",
        "declared_documents": declared_documents,
        "local_documents": len(local_docs),
        "missing_source_entries": missing_source_entries,
        "missing_source_entry_ids": [
            row["doc_id"]
            for row in corpus
            if row["metadata"].get("source_status") == "missing" and row["media_type"] == "application/pdf"
        ],
        "candidate_quotes": actual_candidate_total,
        "text_candidate_quotes": actual_text_total,
        "image_candidate_quotes": actual_image_total,
        "gold_quotes": gold_total,
        "gold_quotes_resolved": gold_resolved,
        "gold_references_missing": gold_missing,
        "image_assets_missing": image_missing,
        "source_status": "complete" if missing_source_entries == 0 and image_missing == 0 else "partial_with_explicit_missing_entries",
        "evaluation_source": _path_string(eval_path),
    }
    condition_fragment = _condition_fragment(
        dataset_id="mmdocrag",
        dataset_name="MMDocRAG",
        package_root=package_root,
        condition=condition,
        protocol_tag="OFFICIAL",
        corpus=corpus,
        questions=questions,
        gold=[_gold_row(question) for question in questions],
        metric_contract=_metric_contract("mmdocrag", condition),
        coverage=coverage,
        candidate_pool=candidate_pool,
        limitations=[
            "C15/C20 preserve the official target budgets while retaining each local row's actual candidate count.",
            *(["Officially declared source PDFs not present in the local inventory are explicit missing entries and do not remove questions."] if missing_source_entries else []),
            *(["Quoted image paths that are absent locally remain explicit missing quote assets."] if image_missing else []),
        ],
    )
    condition_fragment["counts"]["documents"] = len({row["scope_id"] for row in corpus})
    return condition_fragment, {scope for scope in scope_by_name.values()}


def _build_mmdocrag(repo_root: Path, package_root: Path, hash_cache: dict[str, str]) -> dict[str, Any]:
    data_root = _find_data_root(repo_root, "mmdocrag")
    local_docs = _mmdocrag_doc_map(data_root)
    rows_by_condition: dict[str, list[dict[str, Any]]] = {}
    for condition, filename in (("c15", "evaluation_15.jsonl"), ("c20", "evaluation_20.jsonl")):
        rows_by_condition[condition] = _jsonl_load(data_root / filename)
    question_count = len(rows_by_condition["c15"])
    if len(rows_by_condition["c20"]) != question_count:
        raise DatasetBuildError(
            f"MMDocRAG C15/C20 question count mismatch: {len(rows_by_condition['c15'])} vs {len(rows_by_condition['c20'])}"
        )
    declared_documents = _declared_mmdocrag_documents(data_root, len(local_docs), question_count)
    conditions: dict[str, Any] = {}
    scope_sets: dict[str, set[str]] = {}
    resolution_cache: dict[str, dict[str, Path]] = {}
    for condition in ("c15", "c20"):
        fragment, scopes = _mmdocrag_build_condition(
            data_root=data_root,
            package_root=package_root,
            condition=condition,
            rows=rows_by_condition[condition],
            local_docs=local_docs,
            declared_documents=declared_documents,
            hash_cache=hash_cache,
            resolution_cache=resolution_cache,
        )
        conditions[condition] = fragment
        scope_sets[condition] = scopes
    missing_documents = max(0, declared_documents - len(local_docs))
    primary_condition = conditions["c15"]
    primary_corpus = _jsonl_load(_artifact_absolute(package_root, primary_condition["paths"]["corpus"]))
    primary_questions = _jsonl_load(_artifact_absolute(package_root, primary_condition["paths"]["questions"]))
    primary_gold = _jsonl_load(_artifact_absolute(package_root, primary_condition["paths"]["gold"]))
    return _add_compatibility_fields(
        {
            "schema": SCHEMA,
            "dataset_id": "mmdocrag",
            "dataset_name": "MMDocRAG",
            "source": {"data_root": _path_string(data_root), "path_mode": "reference"},
            "protocol": "OFFICIAL",
            "protocol_tag": "OFFICIAL",
            "readiness_status": "READY",
            "readiness": {
                "status": "READY",
                "denominator": "current_local_frozen",
                "blocking": [],
                "limitations": [
                    "Local PDF and quote inventories are the evaluation denominator.",
                    "The public 222-PDF declaration is retained as coverage metadata; missing entries are explicit.",
                ],
            },
            "denominator_policy": {
                "name": "current_local_frozen",
                "paper_declared_counts_are_reference_only": True,
            },
            "evaluation_order": list(EVALUATION_ORDER),
            "evaluation_order_index": EVALUATION_ORDER.index("MMDocRAG"),
            "excluded_benchmarks": list(EXCLUDED_BENCHMARKS),
            "scope_policy": "document-local",
            "official_target_counts": {"declared_documents": 222, "local_pdf_documents": 220, "questions": 2000},
            "dataset_counts": {
                "declared_documents": declared_documents,
                "local_pdf_documents": len(local_docs),
                "missing_source_entries": missing_documents,
                "questions": question_count,
            },
            "conditions": conditions,
        },
        readiness_status="READY",
        corpus=primary_corpus,
        questions=primary_questions,
        gold=primary_gold,
    )


def _docbench_files(data_root: Path) -> list[tuple[str, Path, Path]]:
    if not data_root.is_dir():
        raise DatasetBuildError(f"DocBench data root is missing: {data_root}")
    result: list[tuple[str, Path, Path]] = []
    directories = sorted((path for path in data_root.iterdir() if path.is_dir() and path.name.isdigit()), key=lambda item: int(item.name))
    for directory in directories:
        pdfs = sorted(directory.glob("*.pdf"))
        qa_candidates = sorted(directory.glob("*_qa.jsonl"))
        if len(pdfs) != 1 or len(qa_candidates) != 1:
            raise DatasetBuildError(
                f"DocBench document directory must contain one PDF and one *_qa.jsonl: {directory} "
                f"(pdfs={len(pdfs)}, qa={len(qa_candidates)})"
            )
        pdf = pdfs[0].resolve()
        result.append((pdf.stem, pdf, qa_candidates[0].resolve()))
    if not result:
        raise DatasetBuildError(f"DocBench data root contains no numeric document directories: {data_root}")
    return result


def _docbench_questions(
    *,
    records: Sequence[tuple[str, Path, Path]],
    condition: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    questions: list[dict[str, Any]] = []
    for doc_name, _pdf, qa_path in records:
        rows = _jsonl_load(qa_path)
        for index, row in enumerate(rows):
            question_type = str(row.get("type", "unknown"))
            answerable = question_type.lower() not in {"unanswerable", "una-web"}
            scope_id = _scope("docbench", doc_name)
            doc_id = f"docbench:doc:{doc_name}"
            raw_evidence = row.get("evidence")
            evidence: list[dict[str, Any]] = []
            if raw_evidence not in (None, "", [], {}):
                evidence.append(
                    {
                        "status": "resolved",
                        "doc_id": doc_id,
                        "evidence": raw_evidence,
                    }
                )
            question = _question_row(
                question_id=f"docbench:{condition}:{doc_name}:q:{index:04d}",
                question=row.get("question", ""),
                answer=row.get("answer", ""),
                answerable=answerable,
                scope_doc_ids=[scope_id],
                gold_doc_ids=[doc_id],
                gold_evidence=evidence,
                question_type=question_type,
                metadata=_source_metadata(
                    dataset="DocBench",
                    condition=condition,
                    source_qa_path=_path_string(qa_path),
                    source_record_locator={"line": row.get("_source_line", index + 1), "document": doc_name, "index": index},
                    original_type=question_type,
                    original_evidence=raw_evidence,
                    answerable_protocol=answerable,
                    scope_policy="document-local",
                ),
            )
            questions.append(question)
    return questions, [_gold_row(question) for question in questions]


def _docbench_parsed_content(path: Path, doc_name: str) -> str:
    parts: list[str] = []
    for row in _jsonl_load(path):
        value = row.get("content", row.get("text", row.get("markdown")))
        if value not in (None, ""):
            parts.append(str(value).strip())
    return "\n\n".join(part for part in parts if part) or f"[EMPTY DOCBENCH PARSED DOCUMENT: {doc_name}]"


def _docbench_parsed_rows(repo_root: Path, parsed_manifest_path: Path, records: Sequence[tuple[str, Path, Path]]) -> list[dict[str, Any]]:
    parsed_rows = _jsonl_load(parsed_manifest_path)
    by_doc: dict[str, dict[str, Any]] = {}
    for row in parsed_rows:
        doc_id = str(row.get("document_id", row.get("doc_name", "")))
        if doc_id:
            by_doc[_normal_name(doc_id)] = row
    result: list[dict[str, Any]] = []
    for doc_name, pdf, _qa in records:
        row = by_doc.get(_normal_name(doc_name))
        if row is None:
            # Some parsers use a source basename rather than the manifest's
            # document_id; match the declared source path as a fallback.
            for candidate in parsed_rows:
                source = candidate.get("source_path", candidate.get("source_pdf"))
                if source and _normal_name(source) == _normal_name(pdf.name):
                    row = candidate
                    break
        if row is None:
            result.append(
                {
                    "doc_name": doc_name,
                    "pdf": pdf,
                    "status": "missing",
                    "reason": "no controlled parsed-text manifest row for native PDF",
                }
            )
            continue
        raw_documents_path = row.get("documents_path", row.get("text_path"))
        if not raw_documents_path:
            result.append({"doc_name": doc_name, "pdf": pdf, "status": "missing", "reason": "parsed manifest has no documents_path"})
            continue
        documents_path = _resolve_reference(repo_root, str(raw_documents_path), parsed_manifest_path.parent)
        if not documents_path.is_file():
            result.append(
                {
                    "doc_name": doc_name,
                    "pdf": pdf,
                    "status": "missing",
                    "reason": f"parsed documents_path does not exist: {documents_path}",
                }
            )
            continue
        result.append(
            {
                "doc_name": doc_name,
                "pdf": pdf,
                "status": "ok",
                "path": documents_path,
                "manifest_row": _clean_source_row(row),
            }
        )
    return result


def _build_docbench(repo_root: Path, package_root: Path, hash_cache: dict[str, str]) -> dict[str, Any]:
    data_root = _find_data_root(repo_root, "docbench")
    records = _docbench_files(data_root)
    native_corpus: list[dict[str, Any]] = []
    for doc_name, pdf, qa_path in records:
        native_corpus.append(
            _corpus_row(
                doc_id=f"docbench:doc:{doc_name}",
                title=doc_name,
                scope_id=_scope("docbench", doc_name),
                media_type="application/pdf",
                text_path=None,
                binary_path=pdf,
                hash_path=pdf,
                content=None,
                ingest_role="source_document",
                metadata=_source_metadata(
                    dataset="DocBench",
                    condition="native-pdf",
                    source_pdf_path=_path_string(pdf),
                    source_qa_path=_path_string(qa_path),
                    source_status="ok",
                ),
                hash_cache=hash_cache,
            )
        )
    native_questions, native_gold = _docbench_questions(records=records, condition="native-pdf")
    native_counts_by_type: dict[str, int] = defaultdict(int)
    for question in native_questions:
        native_counts_by_type[str(question["question_type"])] += 1
    conditions: dict[str, Any] = {
        "native-pdf": _condition_fragment(
            dataset_id="docbench",
            dataset_name="DocBench",
            package_root=package_root,
            condition="native-pdf",
            protocol_tag="NATIVE_PDF",
            corpus=native_corpus,
            questions=native_questions,
            gold=native_gold,
            metric_contract=_metric_contract("docbench", "native-pdf"),
            coverage={
                "documents": len(native_corpus),
                "questions": len(native_questions),
                "count_basis": "current_local_frozen",
                "question_type_counts": dict(sorted(native_counts_by_type.items())),
                "source_status": "complete",
                "native_pdf": True,
            },
            limitations=["Native PDF is the primary DocBench condition; original PDF paths remain referenced in place."],
        )
    }
    parsed_manifest = _find_docbench_parsed(repo_root)
    if parsed_manifest is not None:
        parsed_rows = _docbench_parsed_rows(repo_root, parsed_manifest, records)
        parsed_corpus: list[dict[str, Any]] = []
        for item in parsed_rows:
            doc_name = item["doc_name"]
            if item["status"] == "ok":
                parsed_path = item["path"]
                parsed_corpus.append(
                    _corpus_row(
                        doc_id=f"docbench:doc:{doc_name}",
                        title=doc_name,
                        scope_id=_scope("docbench", doc_name),
                        media_type="text/plain",
                        text_path=None,
                        binary_path=None,
                        hash_path=parsed_path,
                        content=_docbench_parsed_content(parsed_path, doc_name),
                        ingest_role="candidate_text",
                        metadata=_source_metadata(
                            dataset="DocBench",
                            condition="controlled-parsed-text",
                            parsed_manifest_path=_path_string(parsed_manifest),
                            parsed_source_pdf_path=_path_string(item["pdf"]),
                            parsed_manifest_row=item.get("manifest_row", {}),
                            source_status="ok",
                        ),
                        hash_cache=hash_cache,
                    )
                )
            else:
                parsed_corpus.append(
                    _missing_corpus_row(
                        doc_id=f"docbench:doc:{doc_name}",
                        title=doc_name,
                        scope_id=_scope("docbench", doc_name),
                        media_type="application/jsonl",
                        reason=str(item["reason"]),
                        metadata=_source_metadata(
                            dataset="DocBench",
                            condition="controlled-parsed-text",
                            parsed_manifest_path=_path_string(parsed_manifest),
                            parsed_source_pdf_path=_path_string(item["pdf"]),
                        ),
                    )
                )
        parsed_questions, parsed_gold = _docbench_questions(records=records, condition="controlled-parsed-text")
        parsed_missing = sum(1 for row in parsed_corpus if row["metadata"].get("source_status") == "missing")
        conditions["controlled-parsed-text"] = _condition_fragment(
            dataset_id="docbench",
            dataset_name="DocBench",
            package_root=package_root,
            condition="controlled-parsed-text",
            protocol_tag="CONTROLLED_PARSED_TEXT",
            corpus=parsed_corpus,
            questions=parsed_questions,
            gold=parsed_gold,
            metric_contract=_metric_contract("docbench", "controlled-parsed-text"),
            coverage={
                "documents": len(parsed_corpus),
                "questions": len(parsed_questions),
                "count_basis": "current_local_frozen",
                "parsed_manifest": _path_string(parsed_manifest),
                "missing_source_entries": parsed_missing,
                "native_pdf": False,
                "controlled_parsed_text": True,
                "source_status": "complete" if parsed_missing == 0 else "partial_with_explicit_missing_entries",
            },
            limitations=[
                "Parsed text is an optional controlled condition kept separate from Native PDF.",
                *(["Parsed-text rows with unavailable source files are explicit missing entries."] if parsed_missing else []),
            ],
        )
    primary_condition = conditions["native-pdf"]
    primary_corpus = _jsonl_load(_artifact_absolute(package_root, primary_condition["paths"]["corpus"]))
    return _add_compatibility_fields(
        {
            "schema": SCHEMA,
            "dataset_id": "docbench",
            "dataset_name": "DocBench",
            "source": {"data_root": _path_string(data_root), "path_mode": "reference"},
            "protocol": "OFFICIAL",
            "protocol_tag": "OFFICIAL",
            "readiness_status": "READY",
            "readiness": {
                "status": "READY",
                "denominator": "current_local_frozen",
                "blocking": [],
                "limitations": [
                    "Native PDF and QA rows currently present locally define the denominator.",
                    "Controlled parsed-text is optional and is never substituted for Native PDF.",
                ],
            },
            "denominator_policy": {
                "name": "current_local_frozen",
                "paper_declared_counts_are_reference_only": True,
            },
            "evaluation_order": list(EVALUATION_ORDER),
            "evaluation_order_index": EVALUATION_ORDER.index("DocBench"),
            "excluded_benchmarks": list(EXCLUDED_BENCHMARKS),
            "scope_policy": "document-local",
            "official_target_counts": {"documents": 229, "questions": 1102},
            "dataset_counts": {
                "documents": len(records),
                "questions": len(native_questions),
                "question_type_counts": dict(sorted(native_counts_by_type.items())),
            },
            "conditions": conditions,
        },
        readiness_status="READY",
        corpus=primary_corpus,
        questions=native_questions,
        gold=native_gold,
    )


def _condition_root_from_manifest(manifest: Mapping[str, Any]) -> Path:
    raw_root = manifest.get("package_root")
    if raw_root:
        return _as_path(str(raw_root))
    raw_path = manifest.get("manifest_path")
    if raw_path:
        return _as_path(str(raw_path)).parent
    raise DatasetBuildError("manifest has no package_root or manifest_path")


def _load_artifact_rows(path: Path, kind: str, errors: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        errors.append(f"{kind} artifact is missing: {path}")
        return []
    try:
        return _jsonl_load(path)
    except DatasetBuildError as exc:
        errors.append(str(exc))
        return []


def _validate_manifest_structure(manifest: Mapping[str, Any], errors: list[str]) -> None:
    if manifest.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}, got {manifest.get('schema')!r}")
    if not isinstance(manifest.get("dataset_id"), str):
        errors.append("dataset_id is missing")
    try:
        _freeze_provider(manifest.get("provider"))
    except DatasetBuildError as exc:
        errors.append(str(exc))
    if not isinstance(manifest.get("conditions"), Mapping) or not manifest.get("conditions"):
        errors.append("conditions must be a non-empty mapping")


def _validate_path_and_hash(
    row: Mapping[str, Any],
    *,
    condition_root: Path,
    kind: str,
    index: int,
    verify_hashes: bool,
    hash_cache: dict[str, str],
    errors: list[str],
) -> None:
    doc_id = row.get("doc_id", row.get("question_id", index))
    for path_key in ("text_path", "binary_path") if kind == "corpus" else ():
        raw_path = row.get(path_key)
        if raw_path is None:
            continue
        path = _artifact_absolute(condition_root, raw_path)
        if not path.is_file():
            errors.append(f"{kind} {doc_id} {path_key} does not exist: {path}")
    if kind != "corpus":
        return
    metadata = row.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    missing = metadata.get("source_status") == "missing"
    ingest_role = metadata.get("ingest_role")
    if ingest_role not in {"source_document", "candidate_text", "candidate_image", "missing"}:
        errors.append(f"corpus {doc_id} has invalid metadata.ingest_role: {ingest_role!r}")
    text_path = row.get("text_path")
    binary_path = row.get("binary_path")
    content = row.get("content")
    if not missing and text_path is None and binary_path is None and not isinstance(content, str):
        errors.append(f"corpus {doc_id} has no path or inline content and is not explicitly missing")
    if ingest_role in {"candidate_text", "candidate_image"}:
        if not isinstance(content, str) or not content.strip():
            errors.append(f"candidate corpus {doc_id} has empty inline content")
        if text_path is not None or binary_path is not None:
            errors.append(f"candidate corpus {doc_id} must use inline content rather than an ingest path")
        if row.get("media_type") != "text/plain":
            errors.append(f"candidate corpus {doc_id} inline representation must use media_type=text/plain")
    if ingest_role == "source_document" and row.get("media_type") == "application/pdf" and binary_path is None:
        errors.append(f"PDF source corpus {doc_id} must retain binary_path")
    sha = row.get("sha256")
    if missing:
        if sha not in (None, ""):
            errors.append(f"missing corpus {doc_id} must have sha256=null")
        if not metadata.get("missing_reason"):
            errors.append(f"missing corpus {doc_id} lacks metadata.missing_reason")
        return
    if not isinstance(sha, str) or not HEX64.fullmatch(sha):
        errors.append(f"corpus {doc_id} has invalid sha256")
        return
    hash_ref = metadata.get("sha256_path")
    if hash_ref is None:
        errors.append(f"corpus {doc_id} lacks metadata.sha256_path")
        return
    hash_path = _artifact_absolute(condition_root, hash_ref)
    if not hash_path.is_file():
        errors.append(f"corpus {doc_id} metadata.sha256_path does not exist: {hash_path}")
        return
    if verify_hashes:
        try:
            source_sha = _hash_file(hash_path, hash_cache)
        except DatasetBuildError as exc:
            errors.append(str(exc))
        else:
            declared_source_sha = metadata.get("source_sha256")
            if declared_source_sha != source_sha:
                errors.append(
                    f"corpus {doc_id} source_sha256 mismatch: expected {declared_source_sha}, actual {source_sha}"
                )
            actual = (
                _record_sha256(source_sha256=source_sha, doc_id=str(doc_id), content=content)
                if content is not None
                else source_sha
            )
            if actual != sha:
                errors.append(f"corpus {doc_id} sha256 mismatch: expected {sha}, actual {actual}")


def _validate_condition(
    *,
    condition_name: str,
    condition: Mapping[str, Any],
    package_root: Path,
    verify_hashes: bool,
    hash_cache: dict[str, str],
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    paths = condition.get("paths") if isinstance(condition.get("paths"), Mapping) else {}
    corpus_path_value = paths.get("corpus", condition.get("corpus_path"))
    questions_path_value = paths.get("questions", condition.get("questions_path"))
    gold_path_value = paths.get("gold", condition.get("gold_path"))
    if not corpus_path_value or not questions_path_value or not gold_path_value:
        errors.append(f"condition {condition_name} does not declare corpus/questions/gold paths")
        return {}
    condition_root = package_root
    corpus_path = _artifact_absolute(package_root, corpus_path_value)
    questions_path = _artifact_absolute(package_root, questions_path_value)
    gold_path = _artifact_absolute(package_root, gold_path_value)
    condition_manifest_value = paths.get("manifest", condition.get("manifest_path"))
    condition_manifest_path = (
        _artifact_absolute(package_root, condition_manifest_value)
        if condition_manifest_value
        else package_root / condition_name / "manifest.json"
    )
    if not condition_manifest_path.is_file():
        errors.append(f"condition {condition_name} is missing runner manifest: {condition_manifest_path}")
    else:
        loaded_condition_manifest = _json_load(condition_manifest_path)
        if not isinstance(loaded_condition_manifest, Mapping):
            errors.append(f"condition {condition_name} runner manifest is not an object")
        else:
            for key, expected in (
                ("schema", SCHEMA),
                ("schema_version", SCHEMA),
                ("condition", condition_name),
                ("scope", "document_local"),
            ):
                if loaded_condition_manifest.get(key) != expected:
                    errors.append(
                        f"condition {condition_name} manifest {key}={loaded_condition_manifest.get(key)!r}, "
                        f"expected {expected!r}"
                    )
            declared_artifacts = loaded_condition_manifest.get("artifacts")
            if not isinstance(declared_artifacts, Mapping):
                errors.append(f"condition {condition_name} manifest artifacts is not an object")
            else:
                for artifact_name in (
                    "corpus.jsonl",
                    "questions.jsonl",
                    "gold.jsonl",
                    "start-record.template.json",
                ):
                    artifact = declared_artifacts.get(artifact_name)
                    if not isinstance(artifact, Mapping) or not artifact.get("path"):
                        errors.append(f"condition {condition_name} manifest lacks artifact {artifact_name}")
                        continue
                    artifact_path = _artifact_absolute(condition_manifest_path.parent, str(artifact["path"]))
                    if not artifact_path.is_file():
                        errors.append(f"condition {condition_name} artifact is missing: {artifact_path}")
                        continue
                    if artifact.get("bytes") != artifact_path.stat().st_size:
                        errors.append(
                            f"condition {condition_name} artifact {artifact_name} byte count differs from manifest"
                        )
                    if verify_hashes:
                        declared_hash = artifact.get("sha256")
                        actual_hash = _hash_file(artifact_path, hash_cache)
                        if declared_hash != actual_hash:
                            errors.append(
                                f"condition {condition_name} artifact {artifact_name} sha256 differs from manifest"
                            )
    corpus = _load_artifact_rows(corpus_path, f"{condition_name}.corpus", errors)
    questions = _load_artifact_rows(questions_path, f"{condition_name}.questions", errors)
    gold = _load_artifact_rows(gold_path, f"{condition_name}.gold", errors)
    corpus_ids: set[str] = set()
    corpus_scopes: set[str] = set()
    corpus_scope_by_id: dict[str, str] = {}
    corpus_ids_by_scope: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(corpus):
        for key in REQUIRED_CORPUS_FIELDS:
            if key not in row:
                errors.append(f"{condition_name}.corpus row {index} lacks {key}")
        doc_id = row.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            errors.append(f"{condition_name}.corpus row {index} has invalid doc_id")
        elif doc_id in corpus_ids:
            errors.append(f"{condition_name}.corpus duplicate doc_id: {doc_id}")
        else:
            corpus_ids.add(doc_id)
        scope_id = row.get("scope_id")
        if not isinstance(scope_id, str) or not scope_id:
            errors.append(f"{condition_name}.corpus row {index} has invalid scope_id")
        else:
            corpus_scopes.add(scope_id)
            if isinstance(doc_id, str) and doc_id:
                corpus_scope_by_id[doc_id] = scope_id
                corpus_ids_by_scope[scope_id].add(doc_id)
        if not isinstance(row.get("metadata"), Mapping):
            errors.append(f"{condition_name}.corpus row {index} metadata is not an object")
        if not isinstance(row.get("media_type"), str) or not row.get("media_type"):
            errors.append(f"{condition_name}.corpus row {index} has invalid media_type")
        _validate_path_and_hash(
            row,
            condition_root=condition_root,
            kind="corpus",
            index=index,
            verify_hashes=verify_hashes,
            hash_cache=hash_cache,
            errors=errors,
        )
    question_ids: set[str] = set()
    question_by_id: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(questions):
        for key in REQUIRED_QUESTION_FIELDS:
            if key not in row:
                errors.append(f"{condition_name}.questions row {index} lacks {key}")
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            errors.append(f"{condition_name}.questions row {index} has invalid question_id")
        elif question_id in question_ids:
            errors.append(f"{condition_name}.questions duplicate question_id: {question_id}")
        else:
            question_ids.add(question_id)
            question_by_id[question_id] = row
        if not isinstance(row.get("scope_doc_ids"), list):
            errors.append(f"{condition_name}.questions row {index} scope_doc_ids is not a list")
        if not isinstance(row.get("gold_doc_ids"), list):
            errors.append(f"{condition_name}.questions row {index} gold_doc_ids is not a list")
        if not isinstance(row.get("gold_evidence"), list):
            errors.append(f"{condition_name}.questions row {index} gold_evidence is not a list")
        if not isinstance(row.get("metadata"), Mapping):
            errors.append(f"{condition_name}.questions row {index} metadata is not an object")
        scope_refs = row.get("scope_doc_ids", []) if isinstance(row.get("scope_doc_ids"), list) else []
        allowed_scope_ids: set[str] = set()
        for scope_ref in scope_refs:
            if scope_ref in corpus_ids:
                allowed_scope_ids.add(corpus_scope_by_id[scope_ref])
            elif scope_ref in corpus_scopes:
                allowed_scope_ids.add(scope_ref)
            else:
                errors.append(f"{condition_name}.questions {question_id} scope_doc_ids references unknown doc/scope: {scope_ref}")
        document_ids = row.get("document_ids")
        if not isinstance(document_ids, list) or not document_ids:
            errors.append(f"{condition_name}.questions {question_id} document_ids is not a non-empty list")
        else:
            for document_id in document_ids:
                if document_id not in corpus_ids:
                    errors.append(
                        f"{condition_name}.questions {question_id} document_ids references unknown doc_id: {document_id}"
                    )
                elif corpus_scope_by_id.get(document_id) not in allowed_scope_ids:
                    errors.append(
                        f"{condition_name}.questions {question_id} document_ids is outside scope_doc_ids: {document_id}"
                    )
        for gold_id in row.get("gold_doc_ids", []) if isinstance(row.get("gold_doc_ids"), list) else []:
            if gold_id not in corpus_ids:
                errors.append(f"{condition_name}.questions {question_id} gold_doc_ids references unknown doc_id: {gold_id}")
            elif corpus_scope_by_id.get(gold_id) not in allowed_scope_ids:
                errors.append(f"{condition_name}.questions {question_id} gold_doc_ids is outside scope_doc_ids: {gold_id}")
        for evidence_index, evidence in enumerate(row.get("gold_evidence", []) if isinstance(row.get("gold_evidence"), list) else []):
            if not isinstance(evidence, Mapping):
                errors.append(f"{condition_name}.questions {question_id} gold_evidence[{evidence_index}] is not an object")
                continue
            evidence_doc_id = evidence.get("doc_id")
            if evidence_doc_id is not None and evidence_doc_id not in corpus_ids:
                errors.append(
                    f"{condition_name}.questions {question_id} gold_evidence[{evidence_index}] references unknown doc_id: {evidence_doc_id}"
                )
            elif evidence_doc_id is not None and corpus_scope_by_id.get(evidence_doc_id) not in allowed_scope_ids:
                errors.append(
                    f"{condition_name}.questions {question_id} gold_evidence[{evidence_index}] is outside scope_doc_ids: {evidence_doc_id}"
                )
            if evidence_doc_id is None and evidence.get("status") not in {"missing", "missing_candidate"}:
                errors.append(
                    f"{condition_name}.questions {question_id} gold_evidence[{evidence_index}] null doc_id is not an explicit missing entry"
                )
            if evidence_doc_id is None and not evidence.get("missing_reason"):
                errors.append(
                    f"{condition_name}.questions {question_id} gold_evidence[{evidence_index}] missing entry lacks missing_reason"
                )
    gold_ids: set[str] = set()
    for index, row in enumerate(gold):
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            errors.append(f"{condition_name}.gold row {index} has invalid question_id")
        elif question_id in gold_ids:
            errors.append(f"{condition_name}.gold duplicate question_id: {question_id}")
        else:
            gold_ids.add(question_id)
        for key in ("scope_doc_ids", "gold_doc_ids", "gold_evidence"):
            if key not in row:
                errors.append(f"{condition_name}.gold row {index} lacks {key}")
        if isinstance(row.get("gold_doc_ids"), list):
            for gold_id in row["gold_doc_ids"]:
                if gold_id not in corpus_ids:
                    errors.append(f"{condition_name}.gold {question_id} gold_doc_ids references unknown doc_id: {gold_id}")
        if question_id in question_by_id:
            question = question_by_id[question_id]
            if list(row.get("gold_doc_ids", [])) != list(question.get("gold_doc_ids", [])):
                errors.append(f"{condition_name}.gold {question_id} gold_doc_ids differs from questions.jsonl")
            for evidence in row.get("gold_evidence", []) if isinstance(row.get("gold_evidence"), list) else []:
                if isinstance(evidence, Mapping) and evidence.get("doc_id") is not None and evidence["doc_id"] not in corpus_ids:
                    errors.append(f"{condition_name}.gold {question_id} gold_evidence references unknown doc_id: {evidence['doc_id']}")
    if question_ids != gold_ids:
        errors.append(
            f"{condition_name}.gold question_id set differs from questions.jsonl: "
            f"questions_only={sorted(question_ids - gold_ids)[:5]}, gold_only={sorted(gold_ids - question_ids)[:5]}"
        )
    counts = condition.get("counts") if isinstance(condition.get("counts"), Mapping) else {}
    expected_counts = {
        "corpus_rows": len(corpus),
        "questions": len(questions),
        "gold_rows": len(gold),
        "documents": len(corpus_scopes),
    }
    for key, actual in expected_counts.items():
        if key in counts and counts[key] != actual:
            errors.append(f"{condition_name}.counts.{key}={counts[key]} but actual is {actual}")
    coverage = condition.get("coverage") if isinstance(condition.get("coverage"), Mapping) else {}
    if "missing_source_entries" in coverage:
        actual_missing_sources = sum(1 for row in corpus if isinstance(row.get("metadata"), Mapping) and row["metadata"].get("source_status") == "missing" and row.get("media_type") == "application/pdf")
        if coverage["missing_source_entries"] != actual_missing_sources:
            errors.append(
                f"{condition_name}.coverage.missing_source_entries={coverage['missing_source_entries']} but actual is {actual_missing_sources}"
            )
    if "gold_references_missing" in coverage:
        actual_missing_gold = sum(
            1
            for question in questions
            for evidence in question.get("gold_evidence", [])
            if isinstance(evidence, Mapping) and evidence.get("doc_id") is None
        )
        if coverage["gold_references_missing"] != actual_missing_gold:
            errors.append(
                f"{condition_name}.coverage.gold_references_missing={coverage['gold_references_missing']} but actual is {actual_missing_gold}"
            )
    if any(isinstance(row.get("metadata"), Mapping) and row["metadata"].get("source_status") == "missing" for row in corpus):
        warnings.append(f"{condition_name} contains explicit missing source entries")
    if any(
        isinstance(evidence, Mapping) and evidence.get("doc_id") is None
        for question in questions
        for evidence in question.get("gold_evidence", [])
        if isinstance(question.get("gold_evidence"), list)
    ):
        warnings.append(f"{condition_name} contains explicit missing Gold evidence entries")
    return {
        "corpus_rows": len(corpus),
        "questions": len(questions),
        "gold_rows": len(gold),
        "documents": len(corpus_scopes),
    }


def validate_manifest(
    manifest_or_path: Mapping[str, Any] | str | Path,
    *,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Validate a built fragment and return structured errors/warnings.

    ``manifest_or_path`` may be the returned mapping, the path to
    ``dataset-fragment.json``, or the package directory containing it.
    """

    if isinstance(manifest_or_path, Mapping):
        manifest = dict(manifest_or_path)
    else:
        path = _as_path(manifest_or_path)
        if path.is_dir():
            path = path / "dataset-fragment.json"
        manifest = _json_load(path)
        if not isinstance(manifest, Mapping):
            return {"valid": False, "errors": [f"manifest is not an object: {path}"], "warnings": [], "checks": {}}
        manifest = dict(manifest)
        manifest.setdefault("manifest_path", str(path))
    errors: list[str] = []
    warnings: list[str] = []
    _validate_manifest_structure(manifest, errors)
    try:
        package_root = _condition_root_from_manifest(manifest)
    except DatasetBuildError as exc:
        package_root = Path.cwd()
        errors.append(str(exc))
    hash_cache: dict[str, str] = {}
    condition_counts: dict[str, Any] = {}
    conditions = manifest.get("conditions")
    if isinstance(conditions, Mapping):
        for name, condition in conditions.items():
            if not isinstance(condition, Mapping):
                errors.append(f"condition {name} is not an object")
                continue
            condition_counts[str(name)] = _validate_condition(
                condition_name=str(name),
                condition=condition,
                package_root=package_root,
                verify_hashes=verify_hashes,
                hash_cache=hash_cache,
                errors=errors,
                warnings=warnings,
            )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "schema": manifest.get("schema"),
            "provider_frozen": manifest.get("provider") == FIXED_PROVIDER,
            "hashes_verified": bool(verify_hashes),
            "conditions": condition_counts,
        },
    }


def build_document_dataset(
    dataset_id: str,
    repo_root: str | Path,
    package_root: str | Path,
    provider_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one document dataset fragment and return its manifest mapping."""

    dataset = _canonical_dataset_id(dataset_id)
    repo = _as_path(repo_root)
    package = _as_path(package_root)
    package.mkdir(parents=True, exist_ok=True)
    provider = _freeze_provider(provider_selection)
    hash_cache: dict[str, str] = {}
    if dataset == "mmdocir":
        manifest = _build_mmdocir(repo, package, hash_cache)
    elif dataset == "mmdocrag":
        manifest = _build_mmdocrag(repo, package, hash_cache)
    else:
        manifest = _build_docbench(repo, package, hash_cache)
    manifest["provider"] = provider
    manifest["package_root"] = str(package)
    manifest_path = package / "dataset-fragment.json"
    _write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)
    validation = validate_manifest(manifest, verify_hashes=True)
    if not validation["valid"]:
        raise DatasetBuildError(
            f"built {dataset} fragment failed validation: " + "; ".join(validation["errors"][:12])
        )
    manifest["validation"] = validation
    # Persist the validation result too, while keeping the returned mapping
    # useful to callers that need the path immediately.
    _write_json(manifest_path, manifest)
    return manifest


def _cli_provider(value: str | None) -> Mapping[str, Any] | None:
    if not value:
        return None
    path = Path(value)
    if path.is_file():
        loaded = _json_load(path)
    else:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DatasetBuildError("--provider-json must be a JSON object or a JSON file path") from exc
    if not isinstance(loaded, Mapping):
        raise DatasetBuildError("--provider-json must contain a JSON object")
    return loaded


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build an eval-ready dataset fragment")
    # Support both the standalone spelling and the sibling
    # competitor_eval_ready.py delegation spelling:
    #   build mmdocir --package-root ...
    #   build --dataset mmdocir --output ...
    build_parser.add_argument("dataset_id", nargs="?", choices=["mmdocir", "mmdocrag", "docbench"])
    build_parser.add_argument("--dataset", dest="dataset_option", choices=["mmdocir", "mmdocrag", "docbench"])
    build_parser.add_argument("--repo-root", required=True, type=Path)
    build_parser.add_argument("--package-root", dest="package_root", type=Path)
    build_parser.add_argument(
        "--output",
        dest="output_root",
        type=Path,
        help="compatibility output root; writes under <output>/<dataset_id>",
    )
    build_parser.add_argument("--provider-json", help="provider JSON object or path to one")
    validate_parser = subparsers.add_parser("validate", help="validate a dataset-fragment.json or package directory")
    validate_parser.add_argument("manifest_or_package", type=Path)
    validate_parser.add_argument("--no-hash", action="store_true", help="skip source SHA-256 recomputation")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            selected_dataset = args.dataset_option or args.dataset_id
            if not selected_dataset:
                raise DatasetBuildError("build requires a dataset ID (positional or --dataset)")
            if args.package_root is None and args.output_root is None:
                raise DatasetBuildError("build requires --package-root (or compatibility alias --output)")
            package_root = args.package_root or (args.output_root / selected_dataset)
            manifest = build_document_dataset(
                selected_dataset,
                args.repo_root,
                package_root,
                _cli_provider(args.provider_json),
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        result = validate_manifest(args.manifest_or_package, verify_hashes=not args.no_hash)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["valid"] else 2
    except DatasetBuildError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(_main())
