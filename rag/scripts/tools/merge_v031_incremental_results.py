#!/usr/bin/env python3
"""Merge inherited v0.3 rows with the 33 executed v0.3.1 rows."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LINEAGE = ROOT / "datasets/moi-rag-bench-v0.3.1-qa-revision/question-lineage.jsonl"
SPECS = {
    "moi_local": (
        "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun",
        "runs/bench-v0.3.1-incremental/20260827-moi-v031-new33-incremental",
        "runs/bench-v0.3.1-merged/20260827-moi-v031-merged-275",
    ),
    "dify_local": (
        "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool",
        "runs/bench-v0.3.1-incremental/20260827-dify-v031-new33-incremental-readyretry",
        "runs/bench-v0.3.1-merged/20260827-dify-v031-merged-275",
    ),
    "fastgpt_local": (
        "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k",
        "runs/bench-v0.3.1-incremental/20260827-fastgpt-v031-new33-incremental-indexreuse",
        "runs/bench-v0.3.1-merged/20260827-fastgpt-v031-merged-275",
    ),
    "maxkb_local": (
        "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f",
        "runs/bench-v0.3.1-incremental/20260827-maxkb-v031-new33-incremental-indexreuse",
        "runs/bench-v0.3.1-merged/20260827-maxkb-v031-merged-275",
    ),
}
JUDGE_FIELDS = (
    "provider",
    "model",
    "temperature",
    "max_tokens",
    "prompt_hash",
    "response_schema_hash",
    "thinking",
)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_checksum(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    atomic_write(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    write_checksum(path)


def write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_write(path.with_name(path.name + ".sha256"), f"{digest}  {path.name}\n")


def latest(rows: list[dict], fields: tuple[str, ...]) -> dict[tuple, dict]:
    selected = {}
    for row in rows:
        selected[tuple(row.get(field) for field in fields)] = row
    return selected


def provenance(lineage: dict, source_run: Path, inherited: bool) -> dict:
    return {
        "changes_from_parent": lineage.get("changes", []),
        "execution": "inherited_from_v0.3_final" if inherited else "executed_v0.3.1_increment",
        "inheritance_policy": "user_requested_parent_lineage_reuse",
        "parent_qa_changed": lineage.get("change_kind") == "rewritten",
        "parent_question_id": lineage.get("parent_question_id"),
        "source_run_dir": str(source_run),
        "source_run_id": source_run.name,
    }


def annotate_initial(path: Path, lineage_by_id: dict[str, dict], old_run: Path, new_run: Path) -> None:
    rows = load_jsonl(path)
    assert len(rows) == 275
    for row in rows:
        item = lineage_by_id[row["question_id"]]
        inherited = bool(item.get("parent_question_id"))
        row["incremental_provenance"] = provenance(item, old_run if inherited else new_run, inherited)
    write_jsonl(path, rows)


def merge_runner(lineage: list[dict], old_run: Path, new_run: Path, destination: Path) -> None:
    old_rows = latest(load_jsonl(old_run / "terminal-ledger.jsonl"), ("stage", "question_id", "repeat_id"))
    new_rows = latest(load_jsonl(new_run / "terminal-ledger.jsonl"), ("stage", "question_id", "repeat_id"))
    merged = []
    for item in lineage:
        inherited = bool(item.get("parent_question_id"))
        source_run = old_run if inherited else new_run
        source_id = item["parent_question_id"] if inherited else item["question_id"]
        source = old_rows if inherited else new_rows
        for stage in ("retrieval", "qa"):
            row = copy.deepcopy(source[(stage, source_id, 1)])
            row["question_id"] = item["question_id"]
            row["attempt_id"] = f"{item['question_id']}#repeat-1"
            row["run_id"] = destination.name
            row["incremental_provenance"] = provenance(item, source_run, inherited)
            merged.append(row)

    assert len(merged) == 550
    assert len({(row["stage"], row["question_id"], row["repeat_id"]) for row in merged}) == 550
    assert all(row["status"] == "SUCCESS" for row in merged)
    write_jsonl(destination / "terminal-ledger.jsonl", merged)
    annotate_initial(destination / "initial-ledger.jsonl", {item["question_id"]: item for item in lineage}, old_run, new_run)

    old_resource = json.loads((old_run / "resource-map.json").read_text(encoding="utf-8"))
    resource = json.loads((new_run / "resource-map.json").read_text(encoding="utf-8"))
    old_global = old_resource.get("resources", {}).get("__global__", {})
    new_global = resource.get("resources", {}).get("__global__", {})
    assert old_global.get("candidate_set_sha256") == new_global.get("candidate_set_sha256")
    assert old_global.get("document_count") == new_global.get("document_count") == 297
    resource["run_id"] = destination.name
    resource["incremental_merge"] = {
        "corpus_reembedded": False,
        "incremental_source_run": str(new_run),
        "inherited_source_run": str(old_run),
    }
    write_json(destination / "resource-map.json", resource)

    start_path = destination / "start-record.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["incremental_merge"] = {
        "executed_questions": 33,
        "inherited_questions": 242,
        "policy": "parent-lineage reuse plus executed new questions",
    }
    write_json(start_path, start)


def judge_signature(row: dict) -> tuple:
    return tuple(json.dumps(row.get(field), ensure_ascii=False, sort_keys=True) for field in JUDGE_FIELDS)


def merge_judge(lineage: list[dict], old_run: Path, new_run: Path, destination: Path) -> set[tuple]:
    judge_dir = destination / "judge"
    assert (judge_dir / "judge-initial-ledger.jsonl").is_file(), "run Judge preflight first"
    old_rows = latest(load_jsonl(old_run / "judge/judge-terminal-ledger.jsonl"), ("question_id", "repeat_id"))
    new_rows = latest(load_jsonl(new_run / "judge/judge-terminal-ledger.jsonl"), ("question_id", "repeat_id"))
    merged = []
    for item in lineage:
        inherited = bool(item.get("parent_question_id"))
        source_run = old_run if inherited else new_run
        source_id = item["parent_question_id"] if inherited else item["question_id"]
        source = old_rows if inherited else new_rows
        row = copy.deepcopy(source[(source_id, 1)])
        row["question_id"] = item["question_id"]
        row["source_run_id"] = destination.name
        if "judge_unit_id" in row:
            row["judge_unit_id"] = f"{item['question_id']}#repeat-1"
        if isinstance(row.get("judgement"), dict):
            row["judgement"]["question_id"] = item["question_id"]
        row["incremental_provenance"] = provenance(item, source_run, inherited)
        merged.append(row)

    assert len(merged) == 275
    assert len({(row["question_id"], row["repeat_id"]) for row in merged}) == 275
    assert all(row["status"] == "SUCCESS" for row in merged)
    signatures = {judge_signature(row) for row in merged}
    assert len(signatures) == 1
    write_jsonl(judge_dir / "judge-terminal-ledger.jsonl", merged)
    annotate_initial(judge_dir / "judge-initial-ledger.jsonl", {item["question_id"]: item for item in lineage}, old_run, new_run)

    start_path = judge_dir / "judge-start-record.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["incremental_merge"] = {
        "executed_questions": 33,
        "inherited_questions": 242,
        "policy": "parent-lineage reuse plus executed new questions",
    }
    write_json(start_path, start)
    return signatures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-only", action="store_true", help="merge runner rows before Judge preflight")
    args = parser.parse_args()
    lineage = load_jsonl(LINEAGE)
    assert len(lineage) == 275
    assert len({item["question_id"] for item in lineage}) == 275
    assert sum(bool(item.get("parent_question_id")) for item in lineage) == 242
    assert sum(not item.get("parent_question_id") for item in lineage) == 33

    summaries = []
    all_judge_signatures = set()
    for system_id, relative_paths in SPECS.items():
        old_run, new_run, destination = (ROOT / path for path in relative_paths)
        assert destination.is_dir(), f"missing merge skeleton: {destination}"
        merge_runner(lineage, old_run, new_run, destination)
        if not args.runner_only:
            signatures = merge_judge(lineage, old_run, new_run, destination)
            all_judge_signatures.update(signatures)
        summary = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "dataset": "moi-rag-bench-v0.3.1-qa-revision",
            "executed_questions": 33,
            "inherited_questions": 242,
            "inherited_questions_changed_from_parent": sum(
                bool(item.get("parent_question_id")) and item.get("change_kind") == "rewritten" for item in lineage
            ),
            "judge_success": None if args.runner_only else 275,
            "qa_success": 275,
            "retrieval_success": 275,
            "source_incremental_run": str(new_run),
            "source_v0_3_run": str(old_run),
            "system_id": system_id,
        }
        metrics_path = destination / "metrics-v1-aggregate.json"
        if metrics_path.is_file():
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            unified_judge = metrics.get("moi_unified", {}).get("judge", {})
            summary["metrics_status"] = metrics.get("status")
            summary["headline_metrics"] = {
                "answer_relevance": unified_judge.get("answer_relevance", {}).get("value"),
                "contradiction_free": unified_judge.get("contradiction_free", {}).get("value"),
                "exact_match": metrics.get("metrics", {}).get("qa", {}).get("exact_match"),
                "mrr": metrics.get("metrics", {}).get("retrieval", {}).get("mrr"),
                "response_claim_correctness": unified_judge.get("response_claim_correctness", {}).get("value"),
                "source_recall_at_10": metrics.get("metrics", {}).get("retrieval", {}).get("source_recall_at_10"),
                "token_f1": metrics.get("metrics", {}).get("qa", {}).get("token_f1"),
            }
        write_json(destination / "merge-summary.json", summary)
        summaries.append(summary)

    if not args.runner_only:
        assert len(all_judge_signatures) == 1, "Judge parameters differ across platforms"
    campaign = {"status": "PASS", "platforms": summaries}
    if all_judge_signatures:
        signature = next(iter(all_judge_signatures))
        campaign["judge_parameters"] = dict(zip(JUDGE_FIELDS, (json.loads(value) for value in signature)))
    write_json(ROOT / "runs/bench-v0.3.1-merged/campaign-summary.json", campaign)
    print(json.dumps(campaign, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
