#!/usr/bin/env python3
"""Merge the two MOI v0.2 result segments into one judge-ready run."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


DATASET_ID = "moi-rag-bench-v0.2-ready-for-eval"
JUDGE_DATASET_ALIAS = "moi-rag-bench-v0.2-ready-for-eval"
CONDITION = "text-only-no-mllm"
MAX_CONTEXT_BYTES = 100_000
JUDGE_MODEL = "deepseek-v4-flash"
JUDGE_PROVIDER = "deepseek-official"
JUDGE_BASE_URL = "https://api.deepseek.com"
JUDGE_API_KEY_ENV = "DEEPSEEK_API_KEY_NEW"
JUDGE_CONCURRENCY = 4
SCRIPT = Path(__file__).resolve().parents[2] / "local-rag-platforms/scripts/evaluation/competitor_eval_judge.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.with_suffix(path.suffix + ".sha256").write_text(f"{sha256(path)}  {path.name}\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    path.with_suffix(path.suffix + ".sha256").write_text(f"{sha256(path)}  {path.name}\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(value)
    return rows


def context_part(chunk: dict[str, Any]) -> str:
    page = chunk.get("page_number") or 0
    return (
        f"[source={chunk.get('file_name', '')} page={int(page)} "
        f"source_uri={chunk.get('source_uri', '')} chunk={chunk.get('chunk_id', '')}]\n"
        f"{chunk.get('content', '')}"
    )


def truncate_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")


def bounded_context(chunks: Any) -> tuple[list[dict[str, Any]], int, bool]:
    selected: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for raw in chunks if isinstance(chunks, list) else []:
        if not isinstance(raw, dict):
            continue
        chunk = dict(raw)
        part = context_part(chunk)
        separator = 2 if selected else 0
        part_bytes = len(part.encode("utf-8"))
        if used + separator + part_bytes <= MAX_CONTEXT_BYTES:
            selected.append(chunk)
            used += separator + part_bytes
            continue
        truncated = True
        if not selected:
            empty = dict(chunk)
            empty["content"] = ""
            prefix_bytes = len(context_part(empty).encode("utf-8"))
            chunk["content"] = truncate_utf8(str(chunk.get("content", "")), MAX_CONTEXT_BYTES - prefix_bytes)
            selected.append(chunk)
            used = len(context_part(chunk).encode("utf-8"))
        break
    return selected, used, truncated


def status(raw: dict[str, Any]) -> str:
    return "SUCCESS" if str(raw.get("status", "")).casefold() in {"ok", "success"} else "FAILED"


def qid(raw: dict[str, Any]) -> str:
    return str((raw.get("case") or {}).get("id") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="v0.2 ready_for_eval package")
    parser.add_argument("--old-results", type=Path, required=True)
    parser.add_argument("--new-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    package = args.package.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output is not empty: {output}")
    for path in (package / "questions.jsonl", package / "corpus.jsonl", package / "gold.jsonl", args.old_results, args.new_results):
        if not path.is_file():
            raise SystemExit(f"missing input: {path}")

    questions = read_jsonl(package / "questions.jsonl")
    question_ids = [str(row.get("question_id") or row.get("id") or "") for row in questions]
    if not question_ids or any(not value for value in question_ids) or len(set(question_ids)) != len(question_ids):
        raise SystemExit("package questions must have unique non-empty question_id values")
    expected = set(question_ids)

    source_rows: dict[str, tuple[Path, int, str, str]] = {}
    source_counts: Counter[str] = Counter()
    source_status: Counter[str] = Counter()
    alignment_mismatches: list[dict[str, Any]] = []
    runner_success_n = 0
    for source in (args.old_results.expanduser().resolve(), args.new_results.expanduser().resolve()):
        with source.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise SystemExit(f"{source}:{line_number}: result is not an object")
                question_id = qid(raw)
                if not question_id:
                    raise SystemExit(f"{source}:{line_number}: result case.id is missing")
                if question_id in source_rows:
                    previous = source_rows[question_id]
                    raise SystemExit(f"duplicate result id {question_id}: {previous[0]}:{previous[1]} and {source}:{line_number}")
                if question_id not in expected:
                    raise SystemExit(f"result id not in package: {question_id}")
                raw_status = str(raw.get("status", ""))
                source_rows[question_id] = (source, line_number, str(line).rstrip("\n"), raw_status)
                source_counts[source.name] += 1
                source_status[raw_status] += 1
                runner_success_n += int(status(raw) == "SUCCESS")

                if status(raw) == "SUCCESS":
                    selected, used, truncated = bounded_context(raw.get("chunks"))
                    recorded_bytes = raw.get("generation_context_bytes")
                    recorded_chunks = raw.get("generation_context_chunks")
                    if recorded_bytes != used or recorded_chunks != len(selected):
                        alignment_mismatches.append(
                            {
                                "question_id": question_id,
                                "recorded_bytes": recorded_bytes,
                                "computed_bytes": used,
                                "recorded_chunks": recorded_chunks,
                                "computed_chunks": len(selected),
                                "recorded_truncated": raw.get("generation_context_truncated"),
                                "computed_truncated": truncated,
                            }
                        )

    if set(source_rows) != expected:
        missing = sorted(expected - set(source_rows))
        extra = sorted(set(source_rows) - expected)
        raise SystemExit(f"coverage mismatch: missing={missing[:10]} extra={extra[:10]}")

    output.mkdir(parents=True, exist_ok=True)
    merged_path = output / "merged-results.jsonl"
    source_map: list[dict[str, Any]] = []
    with merged_path.open("w", encoding="utf-8") as merged:
        for question_id in question_ids:
            source, line_number, raw_line, _ = source_rows[question_id]
            merged.write(raw_line + "\n")
            source_map.append(
                {
                    "question_id": question_id,
                    "source_path": str(source),
                    "source_line": line_number,
                    "status": source_rows[question_id][3],
                }
            )
    merged_path.with_suffix(merged_path.suffix + ".sha256").write_text(f"{sha256(merged_path)}  {merged_path.name}\n", encoding="utf-8")
    write_jsonl(output / "source-map.jsonl", source_map)

    package_manifest = {
        "schema": "competitor-eval-ready-v1",
        "package_schema": "competitor-eval-ready-v1",
        "dataset_id": DATASET_ID,
        "dataset_name": "MOI RAG Benchmark v0.2 ready-for-eval text projection",
        "revision": "local-raw-v0.2",
        "dataset_revision": "local-raw-v0.2",
        "split": "evaluation",
        "condition": CONDITION,
        "mllm_required": False,
        "image_llm": "NOT_APPLICABLE",
        "corpus": str(package / "corpus.jsonl"),
        "documents": str(package / "corpus.jsonl"),
        "questions": str(package / "questions.jsonl"),
        "gold": str(package / "gold.jsonl"),
        "counts": {"documents": 500, "questions": len(question_ids), "gold": len(read_jsonl(package / "gold.jsonl"))},
        "provider_selection": {
            "embedding": {"provider": "maas", "model": "bge-m3", "dimension": 1024},
            "text": {"provider": JUDGE_PROVIDER, "model": JUDGE_MODEL},
        },
        "source_benchmark": "datasets/moi-rag-bench-v0.2",
        "source_ready_package": str(package),
    }
    package_root = output / "judge-package"
    package_root.mkdir()
    write_json(package_root / "manifest.json", package_manifest)

    run_id = "moi-v0.2-1000-dsv4f-judge-20260824"
    start_record = {
        "schema": "competitor-eval-run-start-v1",
        "run_id": run_id,
        "status_at_start": "not_started",
        "dataset": DATASET_ID,
        "dataset_revision": "local-raw-v0.2",
        "condition": CONDITION,
        "provider": {
            "policy": "deepseek_text_judge_huawei_maas_embedding",
            "text_judge": {"provider": JUDGE_PROVIDER, "model": JUDGE_MODEL, "base_url": JUDGE_BASE_URL},
            "embedding": {"provider": "maas", "model": "bge-m3", "dimension": 1024},
            "multimodal": {"used": False, "model": "NOT_APPLICABLE"},
        },
        "planned": {"questions": len(question_ids), "initial_attempts": len(question_ids), "repeats": 1},
        "source_results": [str(args.old_results.expanduser().resolve()), str(args.new_results.expanduser().resolve())],
        "merged_results": str(merged_path),
        "actual_context_contract": {
            "source": "MOI generation context projection",
            "max_bytes": MAX_CONTEXT_BYTES,
            "selection": "same bounded first-ranked chunks as local MatrixFlow generation",
        },
    }
    write_json(output / "start-record.json", start_record)
    write_jsonl(
        output / "initial-ledger.jsonl",
        [
            {
                "schema": "competitor-eval-initial-ledger-v1",
                "attempt_id": f"{question_id}#repeat-1",
                "question_id": question_id,
                "repeat_id": 1,
                "stage": "initial",
                "status": "not_started",
                "planned_denominator": True,
            }
            for question_id in question_ids
        ],
    )

    terminal_rows: list[dict[str, Any]] = []
    with merged_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            raw = json.loads(line)
            question_id = qid(raw)
            run_status = status(raw)
            selected, used, truncated = bounded_context(raw.get("chunks"))
            source = source_rows[question_id]
            terminal_rows.append(
                {
                    "schema": "competitor-eval-terminal-ledger-v1",
                    "stage": "retrieval",
                    "question_id": question_id,
                    "repeat_id": 1,
                    "status": run_status,
                    "hits": selected,
                    "context_bytes": used,
                    "context_chunks": len(selected),
                    "context_truncated": truncated,
                    "source_result_path": str(source[0]),
                    "source_result_line": source[1],
                    "error": raw.get("error") if run_status != "SUCCESS" else None,
                }
            )
            terminal_rows.append(
                {
                    "schema": "competitor-eval-terminal-ledger-v1",
                    "stage": "qa",
                    "question_id": question_id,
                    "repeat_id": 1,
                    "status": run_status,
                    "answer": str(raw.get("answer") or ""),
                    "runner_status": str(raw.get("status") or ""),
                    "generation_provider": raw.get("generation_provider"),
                    "generation_model": raw.get("generation_model"),
                    "embedding_model": raw.get("embedding_model"),
                    "source_result_path": str(source[0]),
                    "source_result_line": source[1],
                    "merged_result_line": line_number,
                    "error": raw.get("error") if run_status != "SUCCESS" else None,
                }
            )
    write_jsonl(output / "terminal-ledger.jsonl", terminal_rows)

    summary = {
        "schema": "moi-v0.2-judge-input-summary-v1",
        "status": "SUCCESS",
        "run_id": run_id,
        "dataset": DATASET_ID,
        "dataset_revision": "local-raw-v0.2",
        "condition": CONDITION,
        "planned_n": len(question_ids),
        "source_rows": len(source_rows),
        "runner_success_n": runner_success_n,
        "runner_failed_n": len(source_rows) - runner_success_n,
        "source_counts": dict(source_counts),
        "source_status_counts": dict(source_status),
        "context_alignment_mismatch_n": len(alignment_mismatches),
        "context_alignment_mismatches": alignment_mismatches[:20],
        "merged_results": str(merged_path),
        "judge_package": str(package_root),
        "source_package": str(package),
    }
    write_json(output / "summary.json", summary)
    write_json(
        output / "judge-parameters.json",
        {
            "script": str(SCRIPT),
            "script_sha256": sha256(SCRIPT),
            "provider": JUDGE_PROVIDER,
            "model": JUDGE_MODEL,
            "model_version": JUDGE_MODEL,
            "base_url": JUDGE_BASE_URL,
            "api_key_env": JUDGE_API_KEY_ENV,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
            "concurrency": JUDGE_CONCURRENCY,
            "retries": 2,
            "timeout_seconds": 180,
            "condition": CONDITION,
            "multimodal": False,
            "embedding": {"provider": "maas", "model": "bge-m3", "calls_in_judge": False},
        },
    )
    (output / "README.md").write_text(
        "# MOI RAG Bench v0.2 merged Judge input\n\n"
        f"Merged {len(question_ids)} unique QA rows from two completed MOI segments.\n\n"
        "The Judge consumes `start-record.json`, `initial-ledger.jsonl`, and `terminal-ledger.jsonl`; "
        "`merged-results.jsonl` is the unmodified merged native result stream.\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
