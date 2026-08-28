#!/usr/bin/env python3
"""Contract tests for the derived MOI pure-text condition."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "scripts" / "tools" / "build_moi_text_only_condition_v0_1.py"
PARENT_ROOT = REPO_ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "ready_for_eval"
OUTPUT_ROOT = REPO_ROOT / "datasets" / "moi-rag-bench-v0.1-raw-corpus" / "text_only_ready_for_eval"

SPEC = importlib.util.spec_from_file_location("build_moi_text_only_condition_v0_1", BUILDER)
assert SPEC is not None and SPEC.loader is not None
BUILDER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER_MODULE)

EXCLUDED_QUESTION_IDS = frozenset(
    {
        "moi500d_qa_docbench_03833189506a",
        "moi500d_qa_docbench_13b28646165c",
        "moi500d_qa_docbench_1798dc75f934",
        "moi500d_qa_docbench_2cb6c35273f6",
        "moi500d_qa_docbench_307cd8975679",
        "moi500d_qa_docbench_33b222900d60",
        "moi500d_qa_docbench_6ee69e551d74",
        "moi500d_qa_docbench_8042559cabfc",
        "moi500d_qa_docbench_c1ed84fac277",
        "moi500d_qa_docbench_c94ef8f381a6",
        "moi500d_qa_docbench_e831a88fb280",
        "moi500d_qa_docbench_ffd377e70c21",
        "moi500d_qa_docbench_f584df39ce05",
        "moi500d_qa_mmdocir_028344f1ff69",
        "moi500d_qa_mmdocir_0b0655d8105f",
        "moi500d_qa_mmdocir_351d93c335c6",
        "moi500d_qa_mmdocir_4680b0da4694",
        "moi500d_qa_mmdocir_4708cefb7c62",
        "moi500d_qa_mmdocir_49cc77ee585f",
        "moi500d_qa_mmdocir_6821935ef19c",
        "moi500d_qa_mmdocir_6d9b91e94456",
        "moi500d_qa_mmdocir_ba77165231ad",
        "moi500d_qa_mmdocir_d1f295424406",
        "moi500d_qa_mmdocir_d6bcfff4b36b",
        "moi500d_qa_mmdocir_fc1ed1865075",
        "moi500d_qa_docbench_ac894f2653d5",
        "moi500d_qa_mmdocir_05dc922c6b20",
        "moi500d_qa_mmdocir_50203698a731",
        "moi500d_qa_mmdocir_843445a9ac75",
        "moi500d_qa_mmdocir_8b40cde96436",
        "moi500d_qa_mmdocir_8b5ab98961b2",
        "moi500d_qa_mmdocir_98d44bce8409",
        "moi500d_qa_mmdocir_c951a69042d8",
        "moi500d_qa_mmdocir_cc2f6a357b19",
        "moi500d_qa_mmdocir_f43a1188def0",
        "moi500d_qa_docbench_0ac6b215da1c",
        "moi500d_qa_docbench_815d28d96b1d",
        "moi500d_qa_docbench_a6a5e07f8217",
        "moi500d_qa_docbench_e579b4406d5c",
    }
)

SOURCE_QUOTAS = {
    "docbench": 450,
    "enterprise": 150,
    "multihop": 250,
    "mmdocir": 150,
}

SOURCE_ROOTS = {
    "docbench": REPO_ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "docbench" / "controlled-parsed-text",
    "enterprise": REPO_ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "enterprise-rag-bench",
    "multihop": REPO_ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "multihop-rag",
    "mmdocir": REPO_ROOT / ".local-services" / "competitor-eval-ready" / "v1" / "mmdocir" / "page",
}

VISUAL_MARKER_RE = re.compile(
    r"(?:\b(?:color|colour|yellow|red|green|blue|shape|chart|figure|plot|diagram|map|image|photo|picture|visual|graph|bar|pie|doughnut|axis|axes|slide|screen|icon|logo)\b"
    r"|\b(?:left|right|top|bottom|above|below|next\s+to)\b"
    r"|\b(?:highlighted|spatial|layout)\b"
    r"|\bhow\s+many\s+(?:people|persons|men|women|soldiers|cars|figures)\b"
    r"|\b(?:number|count)\s+of\s+(?:people|persons|men|women|soldiers|cars|figures)\b)",
    re.IGNORECASE,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_fingerprint(root: Path) -> str:
    entries = []
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root).as_posix()
        entries.append(f"{relative}\0{sha256_file(path)}\n")
    return hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()


def assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:  # pragma: no cover - assertion context
        raise AssertionError(f"{path} escapes {root}") from exc


def test_check_only_cli_accepts_the_derived_package() -> None:
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(OUTPUT_ROOT), "--check-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_parent_and_output_roots_must_be_fully_separated(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    sibling = tmp_path / "derived"

    BUILDER_MODULE.validate_separated_roots(parent, sibling)
    for unsafe_output in (parent, parent / "derived", tmp_path):
        try:
            BUILDER_MODULE.validate_separated_roots(parent, unsafe_output)
        except RuntimeError as exc:
            assert "fully separated" in str(exc)
        else:  # pragma: no cover - failure explanation
            raise AssertionError(f"unsafe nested output was accepted: {unsafe_output}")


def test_counts_ids_gold_links_and_source_quotas() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    documents = read_jsonl(OUTPUT_ROOT / "corpus.jsonl")
    questions = read_jsonl(OUTPUT_ROOT / "questions.jsonl")
    gold = read_jsonl(OUTPUT_ROOT / "gold.jsonl")

    assert manifest["schema"] == "competitor-eval-ready-v1"
    assert manifest["package_schema"] == "competitor-eval-ready-v1"
    assert manifest["documents"] == "corpus.jsonl"
    assert manifest["questions"] == "questions.jsonl"
    assert manifest["gold"] == "gold.jsonl"
    assert manifest["condition"] == "text-only-no-mllm"
    assert manifest["mllm_required"] is False
    assert manifest["image_llm"] == "NOT_APPLICABLE"
    expected_replacement_count = len(EXCLUDED_QUESTION_IDS)
    assert manifest["excluded_visual_qa_count"] == expected_replacement_count
    assert manifest["replacement_count"] == expected_replacement_count
    assert manifest["counts"] == {"documents": 500, "gold": 1000, "questions": 1000}
    assert len(documents) == 500
    assert len(questions) == 1000
    assert len(gold) == 1000

    document_ids = {row["doc_id"] for row in documents}
    question_ids = {row["question_id"] for row in questions}
    gold_ids = {row["question_id"] for row in gold}
    assert len(document_ids) == 500
    assert len(question_ids) == 1000
    assert gold_ids == question_ids
    assert not EXCLUDED_QUESTION_IDS & question_ids

    for row in questions:
        assert set(row.get("gold_doc_ids", [])) <= document_ids
        assert "image_paths" not in row
        assert "image_path" not in row
        assert "images" not in row
        assert "media" not in row or "image" not in str(row["media"]).casefold()
    for row in gold:
        assert set(row.get("gold_doc_ids", [])) <= document_ids
        assert set(row.get("gold", {}).get("document_ids", [])) <= document_ids

    source_counts = {}
    for row in questions:
        source_counts[row["source_dataset"]] = source_counts.get(row["source_dataset"], 0) + 1
    assert source_counts == SOURCE_QUOTAS
    assert manifest["allocation"]["source_qa"] == SOURCE_QUOTAS


def test_answerable_questions_have_a_reference_answer_after_deterministic_repair() -> None:
    questions = read_jsonl(OUTPUT_ROOT / "questions.jsonl")

    answerless = [
        row["question_id"]
        for row in questions
        if bool(row.get("answerable", True))
        and not str(row.get("reference_answer") or row.get("answer") or "").strip()
    ]
    assert answerless == []

    repaired = [
        row
        for row in questions
        if (row.get("metadata") or {}).get("reference_answer_fallback") == "gold_evidence"
    ]
    assert [row["question_id"] for row in repaired] == ["moi500d_qa_docbench_001d07e9163e"]
    assert repaired[0]["reference_answer"].startswith("The motivation behind the proposed method")


def test_derived_documents_are_physical_markdown_copies_under_documents() -> None:
    documents = read_jsonl(OUTPUT_ROOT / "corpus.jsonl")
    derived_documents = OUTPUT_ROOT / "documents"
    parent_documents = PARENT_ROOT / "documents"

    files = [path for path in derived_documents.rglob("*") if path.is_file()]
    assert len(files) == 500
    assert {path.suffix.casefold() for path in files} == {".md"}
    assert all(not path.is_symlink() for path in files)

    forbidden = [
        path
        for path in OUTPUT_ROOT.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".pdf", ".jpg", ".jpeg", ".png"}
    ]
    assert forbidden == []

    for row in documents:
        assert row["media_type"] == "text/markdown"
        assert "image_path" not in row
        assert "binary_path" not in row or not row["binary_path"]
        ingest_path = (OUTPUT_ROOT / row["text_path"]).resolve()
        assert_inside(ingest_path, derived_documents)
        assert ingest_path.is_file()
        assert not ingest_path.is_symlink()
        assert ingest_path.suffix.casefold() == ".md"
        parent_path = PARENT_ROOT / row["text_path"]
        assert parent_path.is_file()
        assert ingest_path.read_bytes() == parent_path.read_bytes()
        assert not os.path.samefile(ingest_path, parent_path)
        assert sha256_file(ingest_path) == row["sha256"]


def test_question_document_reference_fields_use_output_corpus_ids() -> None:
    document_ids = {row["doc_id"] for row in read_jsonl(OUTPUT_ROOT / "corpus.jsonl")}
    questions = read_jsonl(OUTPUT_ROOT / "questions.jsonl")

    for row in questions:
        for field in ("document_ids", "gold_doc_ids", "scope_doc_ids"):
            values = row.get(field, [])
            if isinstance(values, str):
                values = [values]
            assert set(values) <= document_ids, (row["question_id"], field, values)
        nested = row.get("gold") if isinstance(row.get("gold"), dict) else {}
        assert set(nested.get("document_ids", [])) <= document_ids, row["question_id"]


def test_visual_exclusions_and_replacements_have_independent_audits() -> None:
    exclusions = read_jsonl(OUTPUT_ROOT / "visual-exclusions.jsonl")
    replacements = read_jsonl(OUTPUT_ROOT / "replacement-audit.jsonl")
    questions = {row["question_id"]: row for row in read_jsonl(OUTPUT_ROOT / "questions.jsonl")}
    documents = {row["doc_id"]: row for row in read_jsonl(OUTPUT_ROOT / "corpus.jsonl")}

    expected_replacement_count = len(EXCLUDED_QUESTION_IDS)
    assert len(exclusions) == expected_replacement_count
    assert {row["question_id"] for row in exclusions} == EXCLUDED_QUESTION_IDS
    assert len(replacements) == expected_replacement_count
    assert {row["excluded_question_id"] for row in replacements} == EXCLUDED_QUESTION_IDS
    assert {row["replacement_question_id"] for row in replacements} <= set(questions)
    assert len({row["replacement_question_id"] for row in replacements}) == expected_replacement_count

    for row in exclusions:
        assert row["classification"] in {"semantic_visual_gap", "text_projection_gap"}
        assert row["replacement_required"] is True
        assert row["document_id"] in documents
        assert len(row["document_sha256"]) == 64

    for audit in replacements:
        assert audit["evidence_validation"] == "passed"
        assert audit["answer_bearing_text"] is True
        assert audit["visual_semantic_gap"] is False
        assert audit["matched_excerpt"].strip()
        assert len(audit["document_hash"]) == 64
        linked = set(audit["linked_document_ids"])
        assert linked
        assert linked <= set(documents)
        for doc_id in linked:
            assert audit["document_hashes"][doc_id] == documents[doc_id]["sha256"]
        replacement = questions[audit["replacement_question_id"]]
        assert str(replacement["reference_answer"]).strip()
        assert replacement["source_dataset"] == audit["source_dataset"]
        assert set(replacement["gold_doc_ids"]) == linked
        searchable = " ".join(
            (
                str(replacement.get("question") or ""),
                str((replacement.get("metadata") or {}).get("evidence_type") or ""),
                str(replacement.get("question_type") or ""),
            )
        )
        assert not VISUAL_MARKER_RE.search(searchable)


def test_replacements_are_unused_local_candidates_linked_only_to_selected_docs() -> None:
    parent_questions = read_jsonl(PARENT_ROOT / "questions.jsonl")
    parent_documents = read_jsonl(PARENT_ROOT / "corpus.jsonl")
    used_source_ids = {
        row["source_dataset"]: {
            question["source_question_id"]
            for question in parent_questions
            if question["source_dataset"] == row["source_dataset"]
        }
        for row in parent_questions
    }
    selected_source_docs = {
        row["metadata"]["source_dataset"]: set()
        for row in parent_documents
    }
    for row in parent_documents:
        selected_source_docs[row["metadata"]["source_dataset"]].add(row["metadata"]["source_document_id"])
    source_questions = {
        dataset: {row["question_id"]: row for row in read_jsonl(root / "questions.jsonl")}
        for dataset, root in SOURCE_ROOTS.items()
    }
    source_gold = {
        dataset: {row["question_id"]: row for row in read_jsonl(root / "gold.jsonl")}
        for dataset, root in SOURCE_ROOTS.items()
    }
    audits = read_jsonl(OUTPUT_ROOT / "replacement-audit.jsonl")
    for audit in audits:
        dataset = audit["source_dataset"]
        candidate_id = audit["replacement_source_question_id"]
        assert candidate_id not in used_source_ids[dataset]
        candidate = source_questions[dataset][candidate_id]
        gold = source_gold[dataset][candidate_id]
        if dataset == "mmdocir":
            refs = candidate.get("scope_doc_ids") or gold.get("scope_doc_ids") or []
        else:
            refs = candidate.get("gold_doc_ids") or gold.get("gold_doc_ids") or []
        if isinstance(refs, str):
            refs = [refs]
        assert set(refs) <= selected_source_docs[dataset]


def test_parent_and_output_hash_ledgers_are_valid() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    assert manifest["parent"]["sha256"] == package_fingerprint(PARENT_ROOT)
    assert manifest["parent"]["manifest_sha256"] == sha256_file(PARENT_ROOT / "manifest.json")

    output_hashes = manifest["output_hashes"]
    assert output_hashes
    actual_files = {
        path.relative_to(OUTPUT_ROOT).as_posix(): path
        for path in OUTPUT_ROOT.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert set(output_hashes) == set(actual_files)
    assert all(output_hashes[relative] == sha256_file(path) for relative, path in actual_files.items())


def test_manifest_is_self_contained_and_has_no_runtime_source_paths() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert ".local-services" not in serialized
    assert "root" not in manifest["parent"]
    assert manifest["replacement_policy"]["candidate_sources"] == sorted(SOURCE_QUOTAS)
    for value in manifest["artifacts"].values():
        path = Path(value)
        assert not path.is_absolute()
        assert ".." not in path.parts


def test_distribution_delta_reports_the_replacement_effect() -> None:
    manifest = read_json(OUTPUT_ROOT / "manifest.json")
    delta = manifest["distribution_delta"]
    assert set(delta) == {"source_dataset", "question_type", "domain"}
    assert delta["source_dataset"]["before"] == SOURCE_QUOTAS
    assert delta["source_dataset"]["after"] == SOURCE_QUOTAS
    expected_replacement_count = len(EXCLUDED_QUESTION_IDS)
    assert delta["question_type"]["total_replaced"] == expected_replacement_count
    assert delta["domain"]["total_replaced"] == expected_replacement_count
