#!/usr/bin/env python3
"""Build a deterministic, text-only readiness smoke package.

The source package is treated as immutable.  The output contains exactly four
source question/gold pairs and the physical Markdown copies needed by their
gold document references.  This package is a readiness input only and must
not be used as a benchmark score denominator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARENT = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"
DEFAULT_OUTPUT = ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_smoke_ready_for_eval"

SCHEMA = "competitor-eval-ready-v1"
DATASET_ID = "moi-rag-bench-v0.1-text-only-no-mllm-readiness-smoke"
CONDITION = "text-only-no-mllm-readiness-smoke"
PURPOSE = "readiness_only_not_for_scoring"
IMAGE_LLM = "NOT_APPLICABLE"
SELECTION_SEED = "moi-rag-bench-v0.1:text-only-no-mllm-readiness-smoke:v0.1"

CATEGORY_ORDINARY = "ordinary_single_document_nonvisual"
CATEGORY_MULTIHOP = "answerable_multidocument_multihop"
CATEGORY_UNANSWERABLE = "strict_unanswerable_null"
CATEGORY_ENTERPRISE = "enterprise_info_not_found"
CATEGORY_ORDER = (
    CATEGORY_ORDINARY,
    CATEGORY_MULTIHOP,
    CATEGORY_UNANSWERABLE,
    CATEGORY_ENTERPRISE,
)

IMAGE_SUFFIXES = frozenset({".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"})
IMAGE_FIELD_NAMES = frozenset(
    {
        "image",
        "image_path",
        "image_paths",
        "images",
        "media",
        "media_path",
        "media_paths",
        "source_image",
        "source_image_path",
    }
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected a JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hashes(root: Path, *, include_manifest: bool = True) -> dict[str, str]:
    """Return deterministic hashes for all regular files below ``root``.

    The output manifest cannot hash itself without a circular fixed point, so
    output ledgers intentionally omit only the root ``manifest.json``.  Parent
    ledgers include the parent's already-materialized manifest.
    """

    result: dict[str, str] = {}
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root).as_posix()
        if not include_manifest and relative == "manifest.json":
            continue
        result[relative] = sha256_file(path)
    return result


def package_fingerprint(root: Path) -> str:
    entries = [f"{relative}\0{digest}\n" for relative, digest in file_hashes(root).items()]
    return sha256_bytes("".join(entries).encode("utf-8"))


def validate_separated_roots(parent_root: Path, output: Path) -> None:
    """Reject equal or nested parent/output trees in either direction."""

    parent_resolved = parent_root.resolve()
    output_resolved = output.resolve()
    if parent_resolved == output_resolved:
        raise RuntimeError("Parent and output package roots must be different and fully separated")
    if parent_resolved in output_resolved.parents or output_resolved in parent_resolved.parents:
        raise RuntimeError("Parent and output package roots must be fully separated")


def relative_to_root(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _value_is_present(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def contains_image_media(value: Any, field_name: str = "") -> bool:
    """Detect structured image/media references without scanning prose."""

    field = field_name.casefold()
    if (field in IMAGE_FIELD_NAMES or "image" in field) and _value_is_present(value):
        return True
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_field = str(key)
            child_field_lower = child_field.casefold()
            if child_field_lower in {"media_type", "mime_type", "content_type"}:
                if str(child).casefold().startswith("image/"):
                    return True
            if child_field_lower in {"type", "kind"} and str(child).casefold() in {"image", "picture", "image_media"}:
                return True
            if contains_image_media(child, child_field):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_image_media(child, field_name) for child in value)
    if isinstance(value, str) and (field.endswith("_path") or field.endswith("_paths")):
        return Path(value).suffix.casefold() in IMAGE_SUFFIXES
    return False


def reject_image_media_files(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES:
            raise RuntimeError(f"Image/PDF media is not allowed in text-only smoke input: {path}")


def reject_image_media_records(rows: Iterable[Mapping[str, Any]], label: str) -> None:
    for row in rows:
        if contains_image_media(row):
            identifier = row.get("question_id") or row.get("doc_id") or "unknown"
            raise RuntimeError(f"Image media is not allowed in {label}: {identifier}")


def _path_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes its documents directory: {path}") from exc
    return resolved


def load_parent(parent_root: Path) -> dict[str, Any]:
    required = (
        parent_root / "manifest.json",
        parent_root / "corpus.jsonl",
        parent_root / "questions.jsonl",
        parent_root / "gold.jsonl",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Parent package is incomplete: " + ", ".join(missing))
    if not parent_root.is_dir() or parent_root.is_symlink():
        raise RuntimeError(f"Parent package root is not a real directory: {parent_root}")

    reject_image_media_files(parent_root)
    manifest = read_json(parent_root / "manifest.json")
    if manifest.get("schema") != SCHEMA and manifest.get("package_schema") != SCHEMA:
        raise RuntimeError(f"Parent package is not {SCHEMA}: {parent_root}")

    documents = read_jsonl(parent_root / "corpus.jsonl")
    questions = read_jsonl(parent_root / "questions.jsonl")
    gold = read_jsonl(parent_root / "gold.jsonl")
    document_ids = [str(row.get("doc_id") or "") for row in documents]
    question_ids = [str(row.get("question_id") or "") for row in questions]
    gold_ids = [str(row.get("question_id") or "") for row in gold]
    if not documents or any(not value for value in document_ids) or len(set(document_ids)) != len(document_ids):
        raise RuntimeError("Parent document IDs are missing or not unique")
    if not questions or any(not value for value in question_ids) or len(set(question_ids)) != len(question_ids):
        raise RuntimeError("Parent question IDs are missing or not unique")
    if len(set(gold_ids)) != len(gold_ids) or set(question_ids) != set(gold_ids):
        raise RuntimeError("Parent question/gold IDs are not unique and aligned")

    documents_root = (parent_root / "documents").resolve()
    if not documents_root.is_dir() or documents_root.is_symlink():
        raise RuntimeError(f"Parent documents directory is not a real directory: {documents_root}")
    docs_by_id = {str(row["doc_id"]): row for row in documents}
    for path in documents_root.rglob("*"):
        if path.is_file() and (path.is_symlink() or path.suffix.casefold() != ".md"):
            raise RuntimeError(f"Parent documents must be physical Markdown files only: {path}")

    for row in documents:
        text_path = str(row.get("text_path") or "")
        if not text_path or Path(text_path).suffix.casefold() != ".md":
            raise RuntimeError(f"Parent document is not Markdown: {row.get('doc_id')}")
        source_path = parent_root / text_path
        if source_path.is_symlink():
            raise RuntimeError(f"Parent document must not be a symlink: {source_path}")
        source_path = _path_inside(source_path, documents_root, "Parent document path")
        if not source_path.is_file():
            raise RuntimeError(f"Parent Markdown document is missing: {source_path}")
        if row.get("media_type") != "text/markdown":
            raise RuntimeError(f"Parent document is not marked text/markdown: {row.get('doc_id')}")
        if sha256_file(source_path) != str(row.get("sha256") or ""):
            raise RuntimeError(f"Parent document hash mismatch: {source_path}")

    reject_image_media_records(documents, "parent corpus")
    reject_image_media_records(questions, "parent questions")
    reject_image_media_records(gold, "parent gold")
    gold_by_id = {str(row["question_id"]): row for row in gold}
    return {
        "manifest": manifest,
        "documents": documents,
        "questions": questions,
        "gold": gold,
        "gold_by_id": gold_by_id,
        "docs_by_id": docs_by_id,
        "fingerprint": package_fingerprint(parent_root),
        "manifest_sha256": sha256_file(parent_root / "manifest.json"),
        "file_hashes": file_hashes(parent_root),
    }


def _as_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return [str(item) for item in values if str(item)]


def document_ids_for_row(row: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("gold_doc_ids", "document_ids"):
        values.extend(_as_ids(row.get(key)))
    nested_gold = row.get("gold")
    if isinstance(nested_gold, Mapping):
        values.extend(_as_ids(nested_gold.get("document_ids")))
        values.extend(_as_ids(nested_gold.get("gold_doc_ids")))
    return list(dict.fromkeys(values))


def metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, Mapping) else {}


def question_type(row: Mapping[str, Any]) -> str:
    return str(row.get("question_type") or metadata(row).get("original_type") or "").casefold()


def source_dataset(row: Mapping[str, Any]) -> str:
    return str(row.get("source_dataset") or "").casefold()


def is_nonvisual(row: Mapping[str, Any]) -> bool:
    if contains_image_media(row):
        return False
    qtype = question_type(row)
    original_type = str(metadata(row).get("original_type") or "").casefold()
    evidence_type = str(metadata(row).get("evidence_type") or "").casefold()
    if qtype.startswith("multimodal") or original_type.startswith("multimodal"):
        return False
    if evidence_type in {"image", "visual", "multimodal"}:
        return False
    return True


def has_reference_answer(row: Mapping[str, Any]) -> bool:
    value = row.get("reference_answer")
    if value is None:
        value = row.get("answer")
    return bool(str(value or "").strip())


def select_categories(parent_questions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    questions = list(parent_questions)

    ordinary_candidates = [
        row
        for row in questions
        if row.get("answerable") is True
        and question_type(row) == "text-only"
        and len(document_ids_for_row(row)) == 1
        and has_reference_answer(row)
        and is_nonvisual(row)
    ]
    ordinary_candidates.sort(key=lambda row: (0 if source_dataset(row) == "docbench" else 1, str(row["question_id"])))

    multihop_candidates = [
        row
        for row in questions
        if row.get("answerable") is True
        and source_dataset(row) == "multihop"
        and len(document_ids_for_row(row)) >= 2
        and has_reference_answer(row)
        and is_nonvisual(row)
    ]
    multihop_candidates.sort(key=lambda row: str(row["question_id"]))

    unanswerable_candidates = [
        row
        for row in questions
        if row.get("answerable") is False
        and question_type(row) in {"unanswerable", "null_query"}
        and is_nonvisual(row)
    ]
    unanswerable_candidates.sort(key=lambda row: str(row["question_id"]))

    enterprise_candidates = [
        row
        for row in questions
        if source_dataset(row) == "enterprise"
        and question_type(row) == "info_not_found"
        and is_nonvisual(row)
    ]
    enterprise_candidates.sort(key=lambda row: str(row["question_id"]))

    pools = {
        CATEGORY_ORDINARY: ordinary_candidates,
        CATEGORY_MULTIHOP: multihop_candidates,
        CATEGORY_UNANSWERABLE: unanswerable_candidates,
        CATEGORY_ENTERPRISE: enterprise_candidates,
    }
    missing = [category for category in CATEGORY_ORDER if not pools[category]]
    if missing:
        counts = {category: len(pools[category]) for category in CATEGORY_ORDER}
        raise RuntimeError(f"Could not select all readiness-smoke categories: missing={missing} candidates={counts}")

    selected = {category: pools[category][0] for category in CATEGORY_ORDER}
    selected_ids = [str(row["question_id"]) for row in selected.values()]
    if len(set(selected_ids)) != len(selected_ids):
        raise RuntimeError("Readiness-smoke category selections are not disjoint")
    return selected


def category_coverage(selected: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        category: {
            "question_id": str(row["question_id"]),
            "source_question_id": str(row.get("source_question_id") or ""),
            "source_dataset": str(row.get("source_dataset") or ""),
            "question_type": str(row.get("question_type") or ""),
            "answerable": row.get("answerable"),
            "gold_doc_ids": document_ids_for_row(row),
        }
        for category, row in selected.items()
    }


def required_document_ids(
    questions: Iterable[Mapping[str, Any]], gold_by_id: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    required: set[str] = set()
    for question in questions:
        question_id = str(question["question_id"])
        required.update(document_ids_for_row(question))
        required.update(document_ids_for_row(gold_by_id[question_id]))
    return required


def output_hashes(output: Path) -> dict[str, str]:
    return file_hashes(output, include_manifest=False)


def _sorted_counts(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(str(value) for value in values)
    return {key: counts[key] for key in sorted(counts)}


def write_readme(output: Path, coverage: Mapping[str, Mapping[str, Any]]) -> None:
    lines = [
        "# MOI RAG Benchmark v0.1 — text-only readiness smoke",
        "",
        "This is a deterministic four-QA readiness package derived from `text_only_ready_for_eval`.",
        "It is intended to verify ingestion and retrieval plumbing before a benchmark run.",
        "",
        "## Contract",
        "",
        f"- Schema: `{SCHEMA}`",
        f"- Dataset ID: `{DATASET_ID}`",
        f"- Condition: `{CONDITION}`",
        f"- Purpose: `{PURPOSE}`",
        "- MLLM required: `false`",
        f"- Image LLM: `{IMAGE_LLM}`",
        "- Four question/gold records are preserved from the parent package.",
        "- Only required physical Markdown documents are copied; PDFs and images are forbidden.",
        "- This package is readiness-only and is not a scoring or benchmark denominator.",
        "",
        "## Selected categories",
        "",
    ]
    for category in CATEGORY_ORDER:
        details = coverage[category]
        lines.append(f"- `{category}`: `{details['question_id']}`")
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "```bash",
            "python3 scripts/tools/build_moi_text_only_smoke_v0_1.py \\",
            "  --output datasets/moi-rag-bench-v0.1-raw-corpus/text_only_smoke_ready_for_eval",
            "python3 scripts/tools/build_moi_text_only_smoke_v0_1.py \\",
            "  --output datasets/moi-rag-bench-v0.1-raw-corpus/text_only_smoke_ready_for_eval --check-only",
            "```",
            "",
        ]
    )
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _safe_to_overwrite(output: Path) -> None:
    if output.is_symlink():
        raise RuntimeError(f"Refusing to overwrite a symlink output root: {output}")
    if not output.is_dir():
        raise RuntimeError(f"Refusing to overwrite a non-directory output: {output}")
    manifest_path = output / "manifest.json"
    if output.resolve() == DEFAULT_OUTPUT.resolve():
        return
    if not manifest_path.is_file():
        raise RuntimeError("--overwrite requires an existing readiness-smoke package manifest")
    existing = read_json(manifest_path)
    if existing.get("dataset_id") != DATASET_ID or existing.get("purpose") != PURPOSE:
        raise RuntimeError("--overwrite is restricted to an existing readiness-smoke package")


def _prepare_output(output: Path, overwrite: bool) -> None:
    if output.is_symlink():
        raise RuntimeError(f"Output root must not be a symlink: {output}")
    if not output.exists():
        return
    if not overwrite:
        raise FileExistsError(f"Output already exists; pass --overwrite to rebuild this smoke package: {output}")
    _safe_to_overwrite(output)
    shutil.rmtree(output)


def build(parent_root: Path, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
    validate_separated_roots(parent_root, output)
    parent = load_parent(parent_root)
    selected = select_categories(parent["questions"])
    coverage = category_coverage(selected)
    selected_questions = sorted(selected.values(), key=lambda row: str(row["question_id"]))
    selected_ids = {str(row["question_id"]) for row in selected_questions}
    selected_gold = sorted(
        (parent["gold_by_id"][question_id] for question_id in selected_ids),
        key=lambda row: str(row["question_id"]),
    )
    document_ids = required_document_ids(selected_questions, parent["gold_by_id"])
    unknown_docs = sorted(document_ids - set(parent["docs_by_id"]))
    if unknown_docs:
        raise RuntimeError(f"Selected QA references documents absent from parent corpus: {unknown_docs}")
    selected_documents = sorted(
        (parent["docs_by_id"][doc_id] for doc_id in document_ids),
        key=lambda row: str(row["doc_id"]),
    )

    _prepare_output(output, overwrite)
    output.mkdir(parents=True)
    documents_root = output / "documents"
    documents_root.mkdir()
    for row in selected_documents:
        source_path = _path_inside(parent_root / str(row["text_path"]), (parent_root / "documents").resolve(), "Parent document path")
        destination = _path_inside(output / str(row["text_path"]), documents_root.resolve(), "Output document path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        if destination.is_symlink() or destination.read_bytes() != source_path.read_bytes():
            raise RuntimeError(f"Physical Markdown copy failed: {destination}")

    write_jsonl(output / "corpus.jsonl", selected_documents)
    write_jsonl(output / "questions.jsonl", selected_questions)
    write_jsonl(output / "gold.jsonl", selected_gold)
    write_readme(output, coverage)

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "package_schema": SCHEMA,
        "dataset_id": DATASET_ID,
        "dataset_name": "MOI RAG Benchmark v0.1 text-only readiness smoke",
        "dataset_revision": "readiness-smoke-v0.1",
        "revision": "readiness-smoke-v0.1",
        "condition": CONDITION,
        "purpose": PURPOSE,
        "readiness_only": True,
        "scoreable": False,
        "scoring_status": "NOT_FOR_SCORING",
        "evaluation_status": "READINESS_ONLY_NOT_FOR_SCORING",
        "status": "READY_READINESS_ONLY_NOT_FOR_SCORING",
        "readiness_status": "READY",
        "mllm_required": False,
        "image_llm": IMAGE_LLM,
        "ingest_representation": "source_document",
        "protocol_tag": "MOI_RAG_BENCH_V0_1_TEXT_ONLY_NO_MLLM_READINESS_SMOKE",
        "scope": "global",
        "scope_policy": "global",
        "split": "evaluation",
        "documents": "corpus.jsonl",
        "corpus_path": "corpus.jsonl",
        "questions": "questions.jsonl",
        "questions_path": "questions.jsonl",
        "gold": "gold.jsonl",
        "gold_path": "gold.jsonl",
        "counts": {"documents": len(selected_documents), "questions": 4, "gold": 4},
        "allocation": {
            "category_qa": {category: 1 for category in CATEGORY_ORDER},
            "source_qa": _sorted_counts(row.get("source_dataset") for row in selected_questions),
            "source_documents": _sorted_counts(
                (row.get("metadata") or {}).get("source_dataset", "") for row in selected_documents
            ),
        },
        "selection": {
            "seed": SELECTION_SEED,
            "algorithm": "stable question_id; ordinary prefers docbench text-only; required document union",
            "categories": coverage,
        },
        "category_coverage": coverage,
        "parent": {
            "root": relative_to_root(parent_root),
            "sha256": parent["fingerprint"],
            "manifest_sha256": parent["manifest_sha256"],
            "file_hashes": parent["file_hashes"],
        },
        "artifacts": {
            "readme": "README.md",
            "corpus": "corpus.jsonl",
            "questions": "questions.jsonl",
            "gold": "gold.jsonl",
            "documents": "documents/",
            "manifest": "manifest.json",
        },
        "hash_scope": "all output files except manifest.json, which cannot hash itself",
    }
    hashes = output_hashes(output)
    manifest["output_hashes"] = hashes
    manifest["full_file_hashes"] = hashes
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_output(parent_root, output)
    return manifest


def _validate_manifest_hashes(parent_root: Path, output: Path, manifest: Mapping[str, Any], parent: Mapping[str, Any]) -> None:
    parent_info = manifest.get("parent")
    if not isinstance(parent_info, Mapping):
        raise RuntimeError("Smoke manifest is missing parent hashes")
    if parent_info.get("sha256") != parent["fingerprint"]:
        raise RuntimeError("Parent package SHA256 does not match")
    if parent_info.get("manifest_sha256") != parent["manifest_sha256"]:
        raise RuntimeError("Parent manifest SHA256 does not match")
    if parent_info.get("file_hashes") != parent["file_hashes"]:
        raise RuntimeError("Parent full file hash ledger does not match")
    actual_hashes = output_hashes(output)
    if manifest.get("output_hashes") != actual_hashes or manifest.get("full_file_hashes") != actual_hashes:
        raise RuntimeError("Output full file hash ledger does not match")


def validate_output(parent_root: Path, output: Path) -> dict[str, Any]:
    validate_separated_roots(parent_root, output)
    if output.is_symlink() or not output.is_dir():
        raise FileNotFoundError(f"Smoke output package is missing or not a real directory: {output}")
    parent = load_parent(parent_root)
    manifest = read_json(output / "manifest.json")
    expected_manifest_values = {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "package_schema": SCHEMA,
        "dataset_id": DATASET_ID,
        "condition": CONDITION,
        "purpose": PURPOSE,
        "readiness_only": True,
        "scoreable": False,
        "scoring_status": "NOT_FOR_SCORING",
        "mllm_required": False,
        "image_llm": IMAGE_LLM,
    }
    for key, expected in expected_manifest_values.items():
        if manifest.get(key) != expected:
            raise RuntimeError(f"Smoke manifest field {key!r} is not {expected!r}")
    _validate_manifest_hashes(parent_root, output, manifest, parent)

    questions = read_jsonl(output / "questions.jsonl")
    gold = read_jsonl(output / "gold.jsonl")
    documents = read_jsonl(output / "corpus.jsonl")
    if len(questions) != 4 or len(gold) != 4:
        raise RuntimeError("Readiness smoke must contain exactly four questions and four gold rows")
    if len({str(row.get("question_id")) for row in questions}) != 4:
        raise RuntimeError("Smoke question IDs are not unique")
    question_by_id = {str(row["question_id"]): row for row in questions}
    gold_by_id = {str(row["question_id"]): row for row in gold}
    if set(question_by_id) != set(gold_by_id):
        raise RuntimeError("Smoke question and gold IDs are not aligned")

    selected = select_categories(parent["questions"])
    expected_coverage = category_coverage(selected)
    if manifest.get("category_coverage") != expected_coverage:
        raise RuntimeError("Smoke category coverage is not reproducible from the parent")
    selection = manifest.get("selection")
    if not isinstance(selection, Mapping) or selection.get("seed") != SELECTION_SEED or selection.get("categories") != expected_coverage:
        raise RuntimeError("Smoke selection audit is missing or changed")
    expected_question_ids = {details["question_id"] for details in expected_coverage.values()}
    if set(question_by_id) != expected_question_ids:
        raise RuntimeError("Smoke question IDs do not cover exactly the four selected categories")

    parent_questions = {str(row["question_id"]): row for row in parent["questions"]}
    for question_id, row in question_by_id.items():
        if row != parent_questions[question_id]:
            raise RuntimeError(f"Smoke question record was not preserved: {question_id}")
        if gold_by_id[question_id] != parent["gold_by_id"][question_id]:
            raise RuntimeError(f"Smoke gold record was not preserved: {question_id}")
    reject_image_media_records(questions, "smoke questions")
    reject_image_media_records(gold, "smoke gold")

    document_by_id = {str(row["doc_id"]): row for row in documents}
    if len(document_by_id) != len(documents):
        raise RuntimeError("Smoke document IDs are not unique")
    required_ids = required_document_ids(questions, gold_by_id)
    if set(document_by_id) != required_ids:
        raise RuntimeError("Smoke corpus is not exactly the union of required gold documents")
    if manifest.get("counts") != {"documents": len(documents), "questions": 4, "gold": 4}:
        raise RuntimeError("Smoke manifest counts do not match")
    if manifest.get("allocation", {}).get("category_qa") != {category: 1 for category in CATEGORY_ORDER}:
        raise RuntimeError("Smoke category allocation is not exact")

    parent_documents_root = (parent_root / "documents").resolve()
    output_documents_root = (output / "documents").resolve()
    actual_document_files = [path for path in output_documents_root.rglob("*") if path.is_file()]
    expected_paths = {str(row["text_path"]) for row in documents}
    actual_paths = {path.relative_to(output).as_posix() for path in actual_document_files}
    if actual_paths != expected_paths:
        raise RuntimeError("Smoke documents directory contains extra or missing files")
    for row in documents:
        doc_id = str(row["doc_id"])
        if row != parent["docs_by_id"][doc_id]:
            raise RuntimeError(f"Smoke corpus record was not preserved: {doc_id}")
        output_path = output / str(row["text_path"])
        parent_path = parent_root / str(row["text_path"])
        if output_path.is_symlink() or output_path.suffix.casefold() != ".md":
            raise RuntimeError(f"Smoke document is not a physical Markdown file: {output_path}")
        _path_inside(output_path, output_documents_root, "Output document path")
        _path_inside(parent_path, parent_documents_root, "Parent document path")
        if not output_path.is_file() or not parent_path.is_file():
            raise RuntimeError(f"Smoke document is missing: {output_path}")
        if output_path.read_bytes() != parent_path.read_bytes():
            raise RuntimeError(f"Smoke document content differs from parent: {output_path}")
        try:
            if output_path.samefile(parent_path):
                raise RuntimeError(f"Smoke document is not physically separated from parent: {output_path}")
        except FileNotFoundError as exc:
            raise RuntimeError(f"Smoke document disappeared during validation: {output_path}") from exc
        if sha256_file(output_path) != str(row.get("sha256") or ""):
            raise RuntimeError(f"Smoke document hash mismatch: {output_path}")

    reject_image_media_files(output)
    forbidden = [path for path in output.rglob("*") if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES]
    if forbidden:
        raise RuntimeError(f"Forbidden media in smoke output: {forbidden[:3]}")
    return {"status": "OK", "documents": len(documents), "questions": 4, "gold": 4, "scoreable": False}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="Validate an existing smoke package without writing")
    mode.add_argument("--overwrite", action="store_true", help="Rebuild an existing smoke package after safety checks")
    return parser.parse_args()


def resolve_from_root(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    args = parse_args()
    parent = resolve_from_root(args.parent)
    output = resolve_from_root(args.output)
    if args.check_only:
        result = validate_output(parent, output)
    else:
        manifest = build(parent, output, overwrite=args.overwrite)
        result = {"status": "BUILT", "output": str(output), "counts": manifest["counts"], "purpose": PURPOSE}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
