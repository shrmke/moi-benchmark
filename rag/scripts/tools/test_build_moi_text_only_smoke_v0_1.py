#!/usr/bin/env python3
"""Focused contract tests for the deterministic readiness smoke package."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "scripts" / "tools" / "build_moi_text_only_smoke_v0_1.py"
PARENT_ROOT = REPO_ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"
OUTPUT_ROOT = REPO_ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_smoke_ready_for_eval"

SPEC = importlib.util.spec_from_file_location("build_moi_text_only_smoke_v0_1", BUILDER)
assert SPEC is not None and SPEC.loader is not None
BUILDER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER_MODULE)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_category_coverage() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    questions = read_jsonl(OUTPUT_ROOT / "questions.jsonl")
    coverage = manifest["category_coverage"]
    expected_categories = {
        BUILDER_MODULE.CATEGORY_ORDINARY,
        BUILDER_MODULE.CATEGORY_MULTIHOP,
        BUILDER_MODULE.CATEGORY_UNANSWERABLE,
        BUILDER_MODULE.CATEGORY_ENTERPRISE,
    }
    assert set(coverage) == expected_categories
    assert len(questions) == 4
    assert {row["question_id"] for row in questions} == {item["question_id"] for item in coverage.values()}

    selected = {row["question_id"]: row for row in questions}
    ordinary = selected[coverage[BUILDER_MODULE.CATEGORY_ORDINARY]["question_id"]]
    multihop = selected[coverage[BUILDER_MODULE.CATEGORY_MULTIHOP]["question_id"]]
    unanswerable = selected[coverage[BUILDER_MODULE.CATEGORY_UNANSWERABLE]["question_id"]]
    enterprise = selected[coverage[BUILDER_MODULE.CATEGORY_ENTERPRISE]["question_id"]]
    assert ordinary["source_dataset"] == "docbench"
    assert ordinary["question_type"] == "text-only"
    assert ordinary["answerable"] is True
    assert len(ordinary["gold_doc_ids"]) == 1
    assert multihop["source_dataset"] == "multihop"
    assert multihop["answerable"] is True
    assert len(multihop["gold_doc_ids"]) >= 2
    assert unanswerable["answerable"] is False
    assert unanswerable["question_type"] in {"unanswerable", "null_query"}
    assert enterprise["source_dataset"] == "enterprise"
    assert enterprise["question_type"] == "info_not_found"


def test_question_gold_ids_and_required_documents_are_exact() -> None:
    parent_questions = {row["question_id"]: row for row in read_jsonl(PARENT_ROOT / "questions.jsonl")}
    parent_gold = {row["question_id"]: row for row in read_jsonl(PARENT_ROOT / "gold.jsonl")}
    questions = read_jsonl(OUTPUT_ROOT / "questions.jsonl")
    gold = read_jsonl(OUTPUT_ROOT / "gold.jsonl")
    documents = read_jsonl(OUTPUT_ROOT / "corpus.jsonl")
    gold_by_id = {row["question_id"]: row for row in gold}

    assert len(questions) == len(gold) == 4
    assert set(gold_by_id) == {row["question_id"] for row in questions}
    for row in questions:
        question_id = row["question_id"]
        assert row == parent_questions[question_id]
        assert gold_by_id[question_id] == parent_gold[question_id]

    required = BUILDER_MODULE.required_document_ids(questions, gold_by_id)
    output_document_ids = {row["doc_id"] for row in documents}
    assert output_document_ids == required
    for row in questions:
        assert set(BUILDER_MODULE.document_ids_for_row(row)) <= output_document_ids
    for row in gold:
        assert set(BUILDER_MODULE.document_ids_for_row(row)) <= output_document_ids


def test_documents_are_physical_separated_markdown_copies_with_hash_ledgers() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    parent = {row["doc_id"]: row for row in read_jsonl(PARENT_ROOT / "corpus.jsonl")}
    output = {row["doc_id"]: row for row in read_jsonl(OUTPUT_ROOT / "corpus.jsonl")}
    files = [path for path in (OUTPUT_ROOT / "documents").rglob("*") if path.is_file()]
    assert {path.relative_to(OUTPUT_ROOT).as_posix() for path in files} == {
        row["text_path"] for row in output.values()
    }
    assert all(path.suffix.casefold() == ".md" and not path.is_symlink() for path in files)
    assert not [
        path
        for path in OUTPUT_ROOT.rglob("*")
        if path.is_file() and path.suffix.casefold() in BUILDER_MODULE.IMAGE_SUFFIXES
    ]

    for doc_id, row in output.items():
        source_path = PARENT_ROOT / parent[doc_id]["text_path"]
        output_path = OUTPUT_ROOT / row["text_path"]
        assert output_path.read_bytes() == source_path.read_bytes()
        assert not output_path.samefile(source_path)
        assert sha256_file(output_path) == row["sha256"]

    assert manifest["parent"]["sha256"] == BUILDER_MODULE.package_fingerprint(PARENT_ROOT)
    assert manifest["parent"]["manifest_sha256"] == sha256_file(PARENT_ROOT / "manifest.json")
    actual_hashes = BUILDER_MODULE.output_hashes(OUTPUT_ROOT)
    assert manifest["output_hashes"] == actual_hashes
    assert manifest["full_file_hashes"] == actual_hashes
    assert manifest["parent"]["file_hashes"] == BUILDER_MODULE.file_hashes(PARENT_ROOT)


def test_check_only_is_read_only_and_reproducible(tmp_path: Path) -> None:
    check_before = BUILDER_MODULE.package_fingerprint(OUTPUT_ROOT)
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--parent", str(PARENT_ROOT), "--output", str(OUTPUT_ROOT), "--check-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert BUILDER_MODULE.package_fingerprint(OUTPUT_ROOT) == check_before

    first = tmp_path / "first"
    second = tmp_path / "second"
    BUILDER_MODULE.build(PARENT_ROOT, first)
    BUILDER_MODULE.build(PARENT_ROOT, second)
    first_files = sorted(path.relative_to(first).as_posix() for path in first.rglob("*") if path.is_file())
    second_files = sorted(path.relative_to(second).as_posix() for path in second.rglob("*") if path.is_file())
    assert first_files == second_files
    assert all((first / relative).read_bytes() == (second / relative).read_bytes() for relative in first_files)


def test_overwrite_requires_explicit_safe_marker_and_roots_are_disjoint(tmp_path: Path) -> None:
    output = tmp_path / "smoke"
    BUILDER_MODULE.build(PARENT_ROOT, output)
    with pytest.raises(FileExistsError):
        BUILDER_MODULE.build(PARENT_ROOT, output)
    BUILDER_MODULE.build(PARENT_ROOT, output, overwrite=True)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "keep.txt").write_text("do not remove", encoding="utf-8")
    with pytest.raises(RuntimeError, match="existing readiness-smoke package"):
        BUILDER_MODULE.build(PARENT_ROOT, unsafe, overwrite=True)
    assert (unsafe / "keep.txt").read_text(encoding="utf-8") == "do not remove"

    with pytest.raises(RuntimeError, match="fully separated"):
        BUILDER_MODULE.validate_separated_roots(PARENT_ROOT, PARENT_ROOT / "nested")
    with pytest.raises(RuntimeError, match="fully separated"):
        BUILDER_MODULE.validate_separated_roots(PARENT_ROOT / "nested", PARENT_ROOT)


def test_manifest_explicitly_marks_non_score_status() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    assert manifest["schema"] == "competitor-eval-ready-v1"
    assert manifest["dataset_id"].endswith("readiness-smoke")
    assert manifest["condition"] == "text-only-no-mllm-readiness-smoke"
    assert manifest["mllm_required"] is False
    assert manifest["image_llm"] == "NOT_APPLICABLE"
    assert manifest["purpose"] == "readiness_only_not_for_scoring"
    assert manifest["readiness_only"] is True
    assert manifest["scoreable"] is False
    assert manifest["scoring_status"] == "NOT_FOR_SCORING"
    assert "not" in manifest["status"].casefold() and "scor" in manifest["status"].casefold()
