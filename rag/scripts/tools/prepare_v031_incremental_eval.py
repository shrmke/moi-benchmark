#!/usr/bin/env python3
"""Build the 33-question v0.3.1 incremental evaluation package."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REVISION = ROOT / "datasets/moi-rag-bench-v0.3.1-qa-revision"
SOURCE = REVISION / "ready_for_eval"
OUTPUT = REVISION / "incremental_new_qa_ready_for_eval"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite: {OUTPUT}")

    lineage = read_jsonl(REVISION / "question-lineage.jsonl")
    new_ids = {
        str(row["question_id"])
        for row in lineage
        if row.get("origin") == "constructed_from_frozen_corpus" and not row.get("parent_question_id")
    }
    questions = [row for row in read_jsonl(SOURCE / "questions.jsonl") if str(row["question_id"]) in new_ids]
    gold = [row for row in read_jsonl(SOURCE / "gold.jsonl") if str(row["question_id"]) in new_ids]
    if len(new_ids) != 33 or len(questions) != 33 or len(gold) != 33:
        raise SystemExit(f"expected 33 aligned new rows, got ids={len(new_ids)} questions={len(questions)} gold={len(gold)}")
    if {row["question_id"] for row in questions} != {row["question_id"] for row in gold}:
        raise SystemExit("new question/gold IDs do not align")

    staging = OUTPUT.with_name(OUTPUT.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(SOURCE / "documents", staging / "documents")
    shutil.copy2(SOURCE / "corpus.jsonl", staging / "corpus.jsonl")
    write_jsonl(staging / "questions.jsonl", questions)
    write_jsonl(staging / "gold.jsonl", gold)
    write_jsonl(staging / "question-lineage.jsonl", [row for row in lineage if str(row["question_id"]) in new_ids])

    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        dataset_id="moi-rag-bench-v0.3.1-new-qa-increment",
        dataset_name="MOI RAG Benchmark v0.3.1 newly constructed QA increment",
        revision="qa-revision-v0.3.1-new-33",
        dataset_revision="qa-revision-v0.3.1-new-33",
        parent_package="../ready_for_eval",
        incremental_eval={
            "policy": "execute_new_questions_only_and_inherit_parent_results",
            "new_question_count": 33,
            "inherited_question_count": 242,
            "lineage": "question-lineage.jsonl",
        },
        counts={
            **manifest["counts"],
            "question_rows": 33,
            "questions": 33,
            "gold_rows": 33,
        },
    )
    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if sum(1 for _ in (staging / "documents").iterdir()) != 297:
        raise SystemExit("incremental package does not contain 297 frozen documents")
    staging.replace(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
