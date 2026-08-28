#!/usr/bin/env python3
"""Build a low-risk replacement slice for MOI RAG Bench v0.2.

The script has three deliberate stages:

1. ``pool`` builds an audit package from the current 500-document corpus and
   all available text-only candidates whose gold scope is already in those
   documents.
2. ``select`` uses the completed ``moi-rag-qa-audit`` output to retain the
   existing LOW/PASS rows and fill the removed rows with newly selected LOW/PASS
   candidates.
3. ``materialize`` copies the selected benchmark into the separated raw/source
   layout by reusing the existing source resolver.

No MLLM classification is performed here. Multimodal question types and
MMDocIR replacement candidates are intentionally not admitted because their
new per-row semantic sign-off is outside this session.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
BASE_BENCH = ROOT / "datasets/moi-rag-bench-v0.1"
POOL_ROOT = ROOT / "runs/readiness/moi-rag-bench-v0.2-candidate-pool"
POOL_PACKAGE = POOL_ROOT / "ready_for_eval"
FINAL_BENCH = ROOT / "datasets/moi-rag-bench-v0.2"
FINAL_RAW = ROOT / "datasets/moi-rag-bench-v0.2-raw-corpus"
SELECTION_SEED = "moi-rag-bench-v0.2:low-risk-replacement:20260821"
ALLOWED_NEW_TYPES = {
    "text-only",
    "meta-data",
    "unanswerable",
    "una-web",
    "basic",
    "semantic",
    "intra_document_reasoning",
    "project_related",
    "constrained",
    "conflicting_info",
    "completeness",
    "miscellaneous",
    "high_level",
    "info_not_found",
    "comparison_query",
    "inference_query",
    "temporal_query",
    "null_query",
}
MM_TYPES = {"multimodal-t", "multimodal-f"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def load_unique_builder():
    path = ROOT / "scripts/tools/build_moi_unique_bench_v0_1.py"
    spec = importlib.util.spec_from_file_location("moi_unique_builder_v01", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load unique benchmark builder: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    module.READY_ROOT = ROOT / ".local-services/competitor-eval-ready/v1"
    module.PARSED_ROOT = ROOT / "outputs/parsed-documents/moi-ready-v1/datasets"
    package_paths = {
        "docbench": "docbench/controlled-parsed-text",
        "enterprise": "enterprise-rag-bench",
        "multihop": "multihop-rag",
        "mmdocir": "mmdocir/page",
    }
    for dataset, relative in package_paths.items():
        module.SOURCE_CONFIG[dataset]["package_root"] = module.READY_ROOT / relative
    return module


def load_raw_builder():
    path = ROOT / "tools/build_moi_raw_corpus_v0_1.py"
    spec = importlib.util.spec_from_file_location("moi_raw_builder_v01", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load raw corpus builder: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_key(question_id: str) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}|{question_id}".encode("utf-8")).hexdigest()


def replace_text(value: Any, old: str = "v0.1", new: str = "v0.2") -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [replace_text(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_text(item, old, new) for key, item in value.items()}
    return value


def prepare_sources() -> tuple[
    Any,
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, str],
    list[dict[str, Any]],
]:
    builder = load_unique_builder()
    loaded: dict[str, dict[str, Any]] = {}
    doc_records: dict[str, dict[str, Any]] = {}
    for dataset in builder.SOURCE_CONFIG:
        loaded[dataset] = builder.load_source(dataset)
        doc_records[dataset] = builder.build_doc_records(dataset, loaded[dataset])
    docbench_names: set[str] = set()
    for record in doc_records["docbench"].values():
        docbench_names.update(name for name in record.get("dedup_names", set()) if name)

    base_documents = read_jsonl(BASE_BENCH / "documents.jsonl")
    doc_map: dict[str, str] = {
        f"{row['source_dataset']}|{row['source_document_id']}": row["doc_id"]
        for row in base_documents
    }
    return builder, loaded, doc_records, doc_map, base_documents


def collect_candidate_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    builder, loaded, doc_records, doc_map, base_documents = prepare_sources()
    base_questions = {row["question_id"]: row for row in read_jsonl(BASE_BENCH / "questions.jsonl")}
    base_gold = {row["question_id"]: row for row in read_jsonl(BASE_BENCH / "gold.jsonl")}
    current_ids = set(base_questions)
    candidates: dict[str, dict[str, Any]] = {}
    docbench_names: set[str] = set()
    for record in doc_records["docbench"].values():
        docbench_names.update(name for name in record.get("dedup_names", set()) if name)

    for dataset in ("docbench", "enterprise", "multihop"):
        for candidate in builder.candidate_rows(
            dataset,
            loaded[dataset],
            doc_records[dataset],
            docbench_names,
        ):
            question_type = str(candidate["question_type"])
            if question_type in MM_TYPES or question_type not in ALLOWED_NEW_TYPES:
                continue
            refs = {(dataset, str(ref)) for ref in candidate.get("refs", set())}
            if refs and any(f"{ds}|{ref}" not in doc_map for ds, ref in refs):
                continue
            global_id = builder.global_question_id(dataset, str(candidate["question_id"]))
            question = builder.normalized_source_q(dataset, candidate, {
                (ds, ref): gid
                for key, gid in doc_map.items()
                for ds, ref in [key.split("|", 1)]
            })
            gold = builder.normalized_gold(dataset, candidate, {
                (ds, ref): gid
                for key, gid in doc_map.items()
                for ds, ref in [key.split("|", 1)]
            })
            # Preserve the existing row byte-for-byte when it is part of the
            # current package; replacements use the canonical source projection.
            if global_id in current_ids:
                question = base_questions[global_id]
                gold = base_gold[global_id]
            candidates[global_id] = {
                "question": question,
                "gold": gold,
                "source_dataset": dataset,
                "source_question_id": str(candidate["question_id"]),
                "question_type": question_type,
                "is_current": global_id in current_ids,
            }

    rows = [candidates[key] for key in sorted(candidates)]
    return rows, base_documents


def write_package(
    package_root: Path,
    question_rows: list[dict[str, Any]],
    document_rows: list[dict[str, Any]],
    *,
    dataset_id: str,
    package_name: str,
) -> None:
    if package_root.exists():
        raise RuntimeError(f"Output already exists; pass --overwrite for this exact target: {package_root}")
    documents_root = package_root / "documents"
    documents_root.mkdir(parents=True, exist_ok=True)
    corpus_rows: list[dict[str, Any]] = []
    for row in document_rows:
        doc_id = str(row["doc_id"])
        content = str(row.get("content") or "")
        (documents_root / f"{doc_id}.md").write_text(content, encoding="utf-8")
        corpus_rows.append(
            {
                "doc_id": doc_id,
                "scope_id": f"{dataset_id}-global",
                "ingest_role": "source_document",
                "text_path": f"documents/{doc_id}.md",
                "media_type": "text/markdown",
                "title": row.get("title") or doc_id,
                "sha256": sha256_text(content),
                "metadata": {
                    "source_dataset": row.get("source_dataset"),
                    "source_document_id": row.get("source_document_id"),
                    "source_mode": "benchmark_text_projection",
                },
            }
        )
    questions = [row["question"] for row in question_rows]
    gold = [row["gold"] for row in question_rows]
    write_jsonl(package_root / "corpus.jsonl", corpus_rows)
    write_jsonl(package_root / "questions.jsonl", questions)
    write_jsonl(package_root / "gold.jsonl", gold)
    write_json(
        package_root / "manifest.json",
        {
            "schema": "competitor-eval-ready-v1",
            "schema_version": "competitor-eval-ready-v1",
            "package_schema": "competitor-eval-ready-v1",
            "dataset_id": dataset_id,
            "dataset_name": package_name,
            "dataset_revision": "local-low-risk-v0.2",
            "revision": "local-low-risk-v0.2",
            "split": "evaluation",
            "scope": "global",
            "scope_policy": "global",
            "status": "READY",
            "readiness_status": "READY",
            "source_complete": False,
            "documents": "corpus.jsonl",
            "corpus_path": "corpus.jsonl",
            "questions": "questions.jsonl",
            "questions_path": "questions.jsonl",
            "gold": "gold.jsonl",
            "gold_path": "gold.jsonl",
            "counts": {
                "documents": len(document_rows),
                "corpus_rows": len(corpus_rows),
                "runner_documents": len(document_rows),
                "questions": len(questions),
                "question_rows": len(questions),
                "gold_rows": len(gold),
            },
            "conditions": {
                "text_only": True,
                "mllm_rerun": False,
                "note": "This candidate/final package contains only text-only source question types; MLLM classification was not rerun in this session.",
            },
        },
    )


def build_pool(overwrite: bool) -> dict[str, Any]:
    if POOL_ROOT.exists():
        if not overwrite:
            raise RuntimeError(f"Pool exists; pass --overwrite: {POOL_ROOT}")
        shutil.rmtree(POOL_ROOT)
    question_rows, document_rows = collect_candidate_rows()
    write_package(
        POOL_PACKAGE,
        question_rows,
        document_rows,
        dataset_id="moi-rag-bench-v0.2-candidate-pool",
        package_name="MOI RAG Bench v0.2 low-risk candidate pool",
    )
    write_json(
        POOL_ROOT / "candidate-index.json",
        {
            "schema": "moi-rag-bench-v0.2-candidate-index-v1",
            "candidate_rows": len(question_rows),
            "document_rows": len(document_rows),
            "current_rows": sum(1 for row in question_rows if row["is_current"]),
            "new_rows": sum(1 for row in question_rows if not row["is_current"]),
            "by_dataset": dict(Counter(row["source_dataset"] for row in question_rows)),
            "selection_policy": "text-only candidate types; candidate gold scope must be inside the current 500-document corpus",
            "records": [
                {
                    "question_id": row["question"]["question_id"],
                    "source_dataset": row["source_dataset"],
                    "source_question_id": row["source_question_id"],
                    "question_type": row["question_type"],
                    "is_current": row["is_current"],
                }
                for row in question_rows
            ],
        },
    )
    return {"candidate_rows": len(question_rows), "document_rows": len(document_rows)}


def select_final(audit_path: Path, overwrite: bool) -> dict[str, Any]:
    if FINAL_BENCH.exists():
        if not overwrite:
            raise RuntimeError(f"Final benchmark exists; pass --overwrite: {FINAL_BENCH}")
        shutil.rmtree(FINAL_BENCH)
    pool_rows = read_jsonl(POOL_PACKAGE / "questions.jsonl")
    pool_gold = {row["question_id"]: row for row in read_jsonl(POOL_PACKAGE / "gold.jsonl")}
    candidate_index = read_json(POOL_ROOT / "candidate-index.json")
    candidate_meta = {row["question_id"]: row for row in candidate_index.get("records", [])}
    pool_index = {
        row["question_id"]: {
            "question": row,
            **candidate_meta[row["question_id"]],
        }
        for row in pool_rows
        if row["question_id"] in candidate_meta
    }
    if len(pool_index) != len(pool_rows):
        raise RuntimeError("candidate-index.json is missing one or more pool question records")
    audit_rows = read_jsonl(audit_path)
    audit_by_id = {row["question_id"]: row for row in audit_rows}
    safe_ids = {
        question_id
        for question_id, row in audit_by_id.items()
        if row.get("risk_level") in {"LOW", "PASS"}
    }
    safe_current = [
        pool_index[question_id]
        for question_id in sorted(safe_ids)
        if pool_index.get(question_id, {}).get("is_current")
    ]
    if len(safe_current) > 1000:
        raise RuntimeError(f"Current safe rows already exceed target: {len(safe_current)}")

    selected = list(safe_current)
    selected_ids = {row["question"]["question_id"] for row in selected}
    remaining = 1000 - len(selected)
    desired_final_counts = {"docbench": 450, "enterprise": 150, "multihop": 400}
    current_counts = Counter(row["source_dataset"] for row in selected)
    new_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for dataset in desired_final_counts:
        new_by_dataset[dataset] = sorted(
            [
                pool_index[question_id]
                for question_id in safe_ids - selected_ids
                if pool_index[question_id]["source_dataset"] == dataset
            ],
            key=lambda row: stable_key(row["question_id"]),
        )

    for dataset, desired in desired_final_counts.items():
        need = max(0, desired - current_counts[dataset])
        take = min(need, len(new_by_dataset[dataset]), remaining)
        selected.extend(new_by_dataset[dataset][:take])
        selected_ids.update(row["question"]["question_id"] for row in new_by_dataset[dataset][:take])
        current_counts[dataset] += take
        remaining -= take

    if remaining:
        fallback = sorted(
            [
                pool_index[question_id]
                for question_id in safe_ids - selected_ids
                if not pool_index[question_id].get("is_current")
            ],
            key=lambda row: (0 if row["source_dataset"] == "multihop" else 1, stable_key(row["question_id"])),
        )
        fallback_take = fallback[:remaining]
        selected.extend(fallback_take)
        selected_ids.update(row["question"]["question_id"] for row in fallback_take)
        current_counts.update(row["source_dataset"] for row in fallback_take)
        remaining -= len(fallback_take)
    if remaining:
        raise RuntimeError(
            f"Not enough LOW/PASS candidates to reach 1000: selected={len(selected)}, safe_pool={len(safe_ids)}"
        )

    selected.sort(key=lambda row: row["question"]["question_id"])
    documents = read_jsonl(BASE_BENCH / "documents.jsonl")
    FINAL_BENCH.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BASE_BENCH / "documents.jsonl", FINAL_BENCH / "documents.jsonl")
    write_jsonl(FINAL_BENCH / "questions.jsonl", [row["question"] for row in selected])
    write_jsonl(FINAL_BENCH / "gold.jsonl", [pool_gold[row["question"]["question_id"]] for row in selected])
    write_jsonl(
        FINAL_BENCH / "selection.jsonl",
        [
            {
                "kind": "question",
                "question_id": row["question"]["question_id"],
                "source_dataset": row["source_dataset"],
                "source_question_id": row["source_question_id"],
                "selection_role": "retained_low_or_pass" if row.get("is_current") else "new_low_or_pass_replacement",
            }
            for row in selected
        ],
    )
    write_json(
        FINAL_BENCH / "manifest.json",
        {
            "schema": "moi-rag-bench-unified-v0.2-low-risk",
            "version": "0.2.0",
            "generated_at": "2026-08-21",
            "selection_seed": SELECTION_SEED,
            "counts": {"documents": len(documents), "questions": len(selected), "gold": len(selected)},
            "allocation": dict(sorted(current_counts.items())),
            "risk_policy": {
                "allowed_risk_levels": ["LOW", "PASS"],
                "removed_risk_levels": ["MEDIUM", "HIGH"],
                "audit_source": str(audit_path.relative_to(ROOT)),
                "mllm_rerun": False,
                "multimodal_replacement_policy": "exclude new multimodal and MMDocIR candidates until an external per-row semantic sign-off exists",
            },
            "source_benchmark": "datasets/moi-rag-bench-v0.1",
            "artifacts": {
                "documents": "documents.jsonl",
                "questions": "questions.jsonl",
                "gold": "gold.jsonl",
                "selection": "selection.jsonl",
            },
        },
    )
    return {
        "selected": len(selected),
        "retained": sum(1 for row in selected if row.get("is_current")),
        "replacements": sum(1 for row in selected if not row.get("is_current")),
        "by_dataset": dict(sorted(current_counts.items())),
        "risk_levels": dict(Counter(audit_by_id[row["question_id"]]["risk_level"] for row in selected)),
    }


def materialize_raw(overwrite: bool) -> dict[str, Any]:
    raw_builder = load_raw_builder()
    raw_builder.BENCH_ROOT = FINAL_BENCH
    raw_builder.DEFAULT_OUTPUT = FINAL_RAW
    if FINAL_RAW.exists() and not overwrite:
        raise RuntimeError(f"Raw corpus exists; pass --overwrite: {FINAL_RAW}")
    summary = raw_builder.build(FINAL_RAW, overwrite=overwrite)
    for path in [
        FINAL_RAW / "manifest.json",
        FINAL_RAW / "qa-eval-plan.json",
        FINAL_RAW / "ready_for_eval/manifest.json",
    ]:
        if path.exists():
            write_json(path, replace_text(read_json(path)))
    readme = FINAL_RAW / "README.md"
    if readme.exists():
        readme.write_text(readme.read_text(encoding="utf-8").replace("v0.1", "v0.2"), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["pool", "select", "materialize"])
    parser.add_argument("--audit", type=Path, help="Completed audit JSONL for select stage")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.stage == "pool":
        result = build_pool(args.overwrite)
    elif args.stage == "select":
        if not args.audit:
            parser.error("select requires --audit")
        result = select_final(args.audit.resolve(), args.overwrite)
    else:
        result = materialize_raw(args.overwrite)
    print(json.dumps({"status": "OK", "stage": args.stage, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
