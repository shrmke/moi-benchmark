#!/usr/bin/env python3
"""Create a reproducible human batch-admission annotation for the MOI RAG bench."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "web/moi-rag-bench-v0.1/data/cases.json"
DEFAULT_OUTPUT = ROOT / "runs/readiness/human-quality-review-20260821-v0.2/moi-rag-bench-v0.2-quality-reviews.json"
DEFAULT_WEB_OUTPUT = ROOT / "web/moi-rag-bench-v0.1/data/quality-reviews.json"
AUDIT_SOURCE = "runs/readiness/qa-quality-audit-20260821-v0.2-final-mllm-rerun/qa-quality-audit.jsonl"
BENCHMARK = "moi-rag-bench-v0.2"
NOTE = "人工批量确认：全部 QA 已确认可入库。"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--web-output", type=Path, default=DEFAULT_WEB_OUTPUT)
    parser.add_argument(
        "--reviewed-at",
        default=None,
        help="UTC ISO-8601 timestamp; defaults to the current UTC time.",
    )
    return parser.parse_args()


def reviewed_at(value: str | None) -> str:
    if value:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_payload(cases_path: Path, timestamp: str) -> dict[str, object]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"cases.json must contain a non-empty cases list: {cases_path}")

    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError("cases.json contains a non-object case")
        question_id = str(row.get("question_id") or "")
        if not question_id:
            raise ValueError("case is missing question_id")
        if question_id in seen:
            raise ValueError(f"duplicate question_id: {question_id}")
        seen.add(question_id)
        quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        records.append(
            {
                "question_id": question_id,
                "source_dataset": row.get("source_dataset"),
                "question_type": row.get("question_type"),
                "risk_level": quality.get("risk_level"),
                "disposition": quality.get("disposition"),
                "global_rag_ready": quality.get("global_rag_ready"),
                "qa_tags": quality.get("qa_tags") or [],
                "risk_types": quality.get("risk_types") or [],
                "human_review_status": "confirmed",
                "human_reviewed": True,
                "admission_decision": "ok",
                "ingest_ready": True,
                "note": NOTE,
                "source": "human_batch",
                "updated_at": timestamp,
            }
        )

    return {
        "schema": "moi-rag-bench-quality-reviews-v1",
        "benchmark": BENCHMARK,
        "exported_at": timestamp,
        "review_scope": "all_cases",
        "review_method": "human_batch_confirmation",
        "decision": "ok",
        "count": len(records),
        "audit_source": AUDIT_SOURCE,
        "records": records,
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    payload = build_payload(args.cases, reviewed_at(args.reviewed_at))
    write_json(args.output, payload)
    write_json(args.web_output, payload)
    print(json.dumps({"status": "OK", "count": payload["count"], "output": str(args.output), "web_output": str(args.web_output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
