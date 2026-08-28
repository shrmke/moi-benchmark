#!/usr/bin/env python3
"""Materialize the v0.1 package into native MatrixFlow local-RAG inputs.

The benchmark package remains immutable.  This creates a derived parsed-doc
JSONL and a native QuestionCase JSONL whose file-name contract matches the
product runner's source-recall metric.  QA audit tags are carried in question
metadata so retrieval results can be stratified without changing gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evidence_texts(row: dict[str, Any]) -> list[str]:
    values = row.get("gold_evidence") or []
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
            continue
        if not isinstance(value, dict):
            continue
        text = value.get("evidence")
        if isinstance(text, str) and text.strip():
            result.append(text.strip())
    return list(dict.fromkeys(result))


def gold_file_names(row: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in row.get("gold_doc_ids") or []:
        if isinstance(value, str) and value.strip():
            result.append(f"{value.strip()}.md")
    return list(dict.fromkeys(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    package = args.package.resolve()
    audit_path = args.audit.resolve()
    output = args.output.resolve()
    questions_path = package / "questions.jsonl"
    corpus_path = package / "corpus.jsonl"
    audit_rows = read_jsonl(audit_path)
    audit_by_id = {row.get("question_id"): row for row in audit_rows}
    questions = read_jsonl(questions_path)
    corpus = read_jsonl(corpus_path)

    if len(audit_by_id) != len(audit_rows):
        raise ValueError("audit contains duplicate or missing question_id values")
    if len(audit_rows) != len(questions):
        raise ValueError(f"audit/questions count mismatch: {len(audit_rows)} != {len(questions)}")
    if len({row.get("question_id") for row in questions}) != len(questions):
        raise ValueError("questions contains duplicate question_id values")

    parsed_documents: list[dict[str, Any]] = []
    for index, row in enumerate(corpus):
        doc_id = str(row["doc_id"])
        text_path = package / str(row["text_path"])
        content = text_path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"empty source document: {text_path}")
        metadata = dict(row.get("metadata") or {})
        file_name = f"{doc_id}.md"
        metadata.update(
            {
                "file_id": doc_id,
                "raw_file_id": doc_id,
                "source_file_id": doc_id,
                "file_name": file_name,
                "source_file_name": file_name,
                "source_uri": f"benchmark://moi-rag-bench-v0.1/{doc_id}",
                "document_index": index,
                "benchmark_doc_id": doc_id,
                "benchmark_scope": row.get("scope_id"),
            }
        )
        parsed_documents.append(
            {
                "id": doc_id,
                "content": content,
                "type": "text",
                "metadata": metadata,
            }
        )

    native_questions: list[dict[str, Any]] = []
    for row in questions:
        question_id = str(row["question_id"])
        audit = audit_by_id.get(question_id)
        if audit is None:
            raise ValueError(f"missing audit row for {question_id}")
        native_questions.append(
            {
                "id": question_id,
                "question": row["question"],
                "retrieval_keywords": [row["question"]],
                "relevant_documents": gold_file_names(row),
                "relevant_evidence": evidence_texts(row),
                "expected_answerable": bool(row.get("answerable")),
                "expected_answer_keywords": [],
                "metadata": {
                    "source_dataset": row.get("metadata", {}).get("dataset"),
                    "question_type": row.get("question_type"),
                    "gold_doc_ids": row.get("gold_doc_ids") or [],
                    "audit_risk_level": audit.get("risk_level"),
                    "risk_types": audit.get("risk_types") or [],
                    "qa_tags": audit.get("qa_tags") or [],
                    "global_rag_ready": audit.get("global_rag_ready"),
                    "original_question_id": question_id,
                },
            }
        )

    output.mkdir(parents=True, exist_ok=True)
    documents_path = output / "documents.native.jsonl"
    native_questions_path = output / "questions.native.jsonl"
    write_jsonl(documents_path, parsed_documents)
    write_jsonl(native_questions_path, native_questions)
    manifest = {
        "schema_version": "moi-rag-bench-v0.1.native-local-eval.v1",
        "source_package": str(package),
        "source_questions": str(questions_path),
        "source_corpus": str(corpus_path),
        "source_audit": str(audit_path),
        "source_hashes": {
            "questions_jsonl": sha256_file(questions_path),
            "corpus_jsonl": sha256_file(corpus_path),
            "audit_jsonl": sha256_file(audit_path),
        },
        "documents_path": str(documents_path),
        "questions_path": str(native_questions_path),
        "document_count": len(parsed_documents),
        "question_count": len(native_questions),
        "question_id_contract": "questions.jsonl.question_id -> native.id",
        "source_recall_contract": "native.relevant_documents contains <gold_doc_id>.md, matching parsed metadata.file_name",
        "scope_contract": "global corpus; no per-row FileIDs filter",
        "audit_tag_contract": "native.metadata.qa_tags mirrors qa-quality-audit.jsonl",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
