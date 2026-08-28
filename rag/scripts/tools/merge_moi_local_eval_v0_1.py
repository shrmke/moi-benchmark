#!/usr/bin/env python3
"""Stream-merge the MOI local retrieval runs into a compact audit-friendly ledger.

The native runner stores the full expanded chunk payload for every attempt.  That
is useful for forensic inspection but too large to load or rewrite as one
in-memory result set.  This tool keeps the raw ledgers untouched and emits one
small row per QA with metrics, audit tags, gold fields, and top-hit previews.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


METRIC_KEYS = (
    "source_recall",
    "evidence_recall",
    "reciprocal_rank",
    "answerability_accuracy",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-results", type=Path, required=True)
    parser.add_argument("--native-success-count", type=int, required=True)
    parser.add_argument("--fallback-results", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--top-hits", type=int, default=10)
    parser.add_argument("--preview-chars", type=int, default=300)
    return parser.parse_args()


def read_audit(path: Path) -> dict[str, dict[str, Any]]:
    audit: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            audit[row["question_id"]] = row
    return audit


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                yield line_number, json.loads(line)


def audit_view(case: dict[str, Any], audit: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metadata = case.get("metadata") or {}
    question_id = case.get("id", "")
    audited = audit.get(question_id, {})
    tags = metadata.get("qa_tags") or audited.get("qa_tags") or []
    return {
        "risk_level": metadata.get("audit_risk_level", audited.get("risk_level", "UNKNOWN")),
        "global_rag_ready": metadata.get("global_rag_ready", audited.get("global_rag_ready")),
        "qa_tags": tags,
        "risk_types": metadata.get("risk_types", audited.get("risk_types", [])),
        "source_dataset": metadata.get("source_dataset", audited.get("source_dataset")),
        "question_type": metadata.get("question_type", audited.get("question_type")),
        "original_question_id": metadata.get("original_question_id"),
        "gold_doc_ids": metadata.get("gold_doc_ids", []),
    }


def compact_hit(hit: dict[str, Any], preview_chars: int) -> dict[str, Any]:
    content = str(hit.get("content") or "")
    preview = " ".join(content.split())[:preview_chars]
    return {
        "rank": hit.get("rank"),
        "chunk_id": hit.get("chunk_id"),
        "file_id": hit.get("file_id"),
        "file_name": hit.get("file_name"),
        "score": hit.get("score"),
        "routes": hit.get("routes", []),
        "level": hit.get("level"),
        "chunk_index": hit.get("chunk_index"),
        "chunk_start": hit.get("chunk_start"),
        "chunk_end": hit.get("chunk_end"),
        "source_uri": hit.get("source_uri"),
        "content_preview": preview,
    }


def compact_row(
    row: dict[str, Any],
    *,
    route: str,
    raw_path: Path,
    raw_line_number: int,
    audit: dict[str, dict[str, Any]],
    top_hits: int,
    preview_chars: int,
) -> dict[str, Any]:
    case = row.get("case") or {}
    chunks = row.get("chunks") or []
    top = chunks[:top_hits]
    retrieved_documents: list[str] = []
    for hit in chunks:
        file_id = str(hit.get("file_id") or hit.get("file_name") or "")
        if file_id and file_id not in retrieved_documents:
            retrieved_documents.append(file_id)
    return {
        "id": case.get("id"),
        "question": case.get("question"),
        "status": row.get("status"),
        "repeat": row.get("repeat"),
        "retrieval_route": route,
        "runner_routes": row.get("routes", []),
        "embedding_model": row.get("embedding_model"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "retrieval_latency_ms": row.get("retrieval_latency_ms"),
        "stage_latency_ms": row.get("stage_latency_ms", {}),
        "metrics": row.get("metrics", {}),
        "audit": audit_view(case, audit),
        "gold": {
            "expected_answerable": case.get("expected_answerable"),
            "expected_answer_keywords": case.get("expected_answer_keywords", []),
            "relevant_documents": case.get("relevant_documents", []),
            "relevant_evidence": case.get("relevant_evidence", []),
            "retrieval_keywords": case.get("retrieval_keywords", []),
        },
        "retrieved_chunk_count": len(chunks),
        "retrieved_document_count": len(retrieved_documents),
        "retrieved_documents_top_order": retrieved_documents[:50],
        "top_hits": [compact_hit(hit, preview_chars) for hit in top],
        "raw_source": {
            "path": str(raw_path),
            "line_number": raw_line_number,
        },
    }


def safe_group_value(value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Aggregate:
    def __init__(self) -> None:
        self.count = 0
        self.metric_sums = Counter()
        self.latency_sum = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.count += 1
        metrics = row.get("metrics") or {}
        for key in METRIC_KEYS:
            value = metrics.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                self.metric_sums[key] += float(value)
        latency = row.get("retrieval_latency_ms")
        if isinstance(latency, (int, float)) and math.isfinite(float(latency)):
            self.latency_sum += float(latency)

    def as_dict(self) -> dict[str, Any]:
        means = {
            key: (self.metric_sums[key] / self.count if self.count else None)
            for key in METRIC_KEYS
        }
        means["retrieval_latency_mean_ms"] = self.latency_sum / self.count if self.count else None
        return {"count": self.count, "means": means}


def add_group(groups: dict[str, Aggregate], key: str, row: dict[str, Any]) -> None:
    groups.setdefault(key, Aggregate()).add(row)


def aggregate_rows(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    groups: dict[str, Aggregate] = {}
    route_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    dataset_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    materialized: list[dict[str, Any]] = []
    for row in rows:
        materialized.append(row)
        status = safe_group_value(row.get("status"))
        status_counts[status] += 1
        route = safe_group_value(row.get("retrieval_route"))
        route_counts[route] += 1
        audit = row.get("audit") or {}
        risk = safe_group_value(audit.get("risk_level"))
        dataset = safe_group_value(audit.get("source_dataset"))
        risk_counts[risk] += 1
        dataset_counts[dataset] += 1
        add_group(groups, "overall", row)
        add_group(groups, f"route:{route}", row)
        add_group(groups, f"risk_level:{risk}", row)
        add_group(groups, f"source_dataset:{dataset}", row)
        add_group(groups, f"global_rag_ready:{safe_group_value(audit.get('global_rag_ready'))}", row)
        add_group(groups, f"question_type:{safe_group_value(audit.get('question_type'))}", row)
    group_json = {key: value.as_dict() for key, value in sorted(groups.items())}
    summary = {
        "status_counts": dict(sorted(status_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "risk_level_counts": dict(sorted(risk_counts.items())),
        "source_dataset_counts": dict(sorted(dataset_counts.items())),
        "groups": group_json,
    }
    return summary, materialized


def write_report(path: Path, summary: dict[str, Any]) -> None:
    overall = summary["groups"]["overall"]
    lines = [
        "# MOI RAG Bench v0.1 local evaluation",
        "",
        "This is a completed 1,000-QA retrieval-only plumbing run over the persisted local hash vector table.",
        "The first 713 successful rows use native MatrixOne full-text + vector retrieval; the remaining 287 rows use the explicitly marked LIKE full-text fallback + vector retrieval after the native full-text route hung on one query.",
        "",
        "## Coverage",
        "",
        f"- Merged rows: **{overall['count']}**",
        f"- Status counts: `{json.dumps(summary['status_counts'], ensure_ascii=False)}`",
        f"- Retrieval routes: `{json.dumps(summary['route_counts'], ensure_ascii=False)}`",
        f"- Risk levels: `{json.dumps(summary['risk_level_counts'], ensure_ascii=False)}`",
        f"- Source datasets: `{json.dumps(summary['source_dataset_counts'], ensure_ascii=False)}`",
        "",
        "## Overall metrics",
        "",
        "| Group | n | source recall | evidence recall | MRR | answerability accuracy | mean latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def fmt(value: Any) -> str:
        return "-" if value is None else f"{float(value):.4f}"

    def metric_line(label: str, item: dict[str, Any]) -> str:
        means = item["means"]
        return "| %s | %d | %s | %s | %s | %s | %.1f |" % (
            label,
            item["count"],
            fmt(means.get("source_recall")),
            fmt(means.get("evidence_recall")),
            fmt(means.get("reciprocal_rank")),
            fmt(means.get("answerability_accuracy")),
            float(means.get("retrieval_latency_mean_ms") or 0),
        )

    lines.append(metric_line("overall", overall))
    lines.extend(["", "## Stratified metrics", "", "### By retrieval route", ""])
    lines.extend(
        metric_line(key.removeprefix("route:"), value)
        for key, value in summary["groups"].items()
        if key.startswith("route:")
    )
    lines.extend(["", "### By audit risk level", ""])
    lines.extend(
        metric_line(key.removeprefix("risk_level:"), value)
        for key, value in summary["groups"].items()
        if key.startswith("risk_level:")
    )
    lines.extend(["", "### By source dataset", ""])
    lines.extend(
        metric_line(key.removeprefix("source_dataset:"), value)
        for key, value in summary["groups"].items()
        if key.startswith("source_dataset:")
    )
    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "- The deterministic hash embedding is a local plumbing baseline, not a semantic embedding quality result.",
            "- The route mix is intentionally visible: native and LIKE-fallback metrics must not be reported as one canonical engine score.",
            "- QA audit tags are copied into every compact row; rows tagged `存在问题` / `暂不进入 global RAG` remain excluded from a clean global-RAG leaderboard unless a human overrides them.",
            "- The raw full-evidence ledgers remain at the paths recorded in each compact row; this report is the lightweight index for manual inspection.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    audit = read_audit(args.audit)
    compact_rows: list[dict[str, Any]] = []
    native_successes = 0
    native_failure: Optional[dict[str, Any]] = None
    seen_ids: set[str] = set()

    with args.native_results.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok":
                if native_failure is None:
                    native_failure = {
                        "id": (row.get("case") or {}).get("id"),
                        "status": row.get("status"),
                        "error": row.get("error"),
                        "retrieval_latency_ms": row.get("retrieval_latency_ms"),
                        "stage_latency_ms": row.get("stage_latency_ms", {}),
                        "raw_line_number": line_number,
                    }
                continue
            compact = compact_row(
                row,
                route="native_fulltext_plus_vector",
                raw_path=args.native_results,
                raw_line_number=line_number,
                audit=audit,
                top_hits=args.top_hits,
                preview_chars=args.preview_chars,
            )
            question_id = compact["id"]
            if question_id in seen_ids:
                raise SystemExit(f"duplicate native question id: {question_id}")
            seen_ids.add(question_id)
            compact_rows.append(compact)
            native_successes += 1
            if native_successes >= args.native_success_count:
                break

        # The native run intentionally has one failed attempt immediately after
        # the retained 713 successes. Read that next record for the audit trail
        # without scanning or materializing the rest of the raw ledger.
        if native_failure is None:
            next_line_number = line_number + 1
            for line in handle:
                if not line.strip():
                    next_line_number += 1
                    continue
                row = json.loads(line)
                if row.get("status") != "ok":
                    native_failure = {
                        "id": (row.get("case") or {}).get("id"),
                        "status": row.get("status"),
                        "error": row.get("error"),
                        "retrieval_latency_ms": row.get("retrieval_latency_ms"),
                        "stage_latency_ms": row.get("stage_latency_ms", {}),
                        "raw_line_number": next_line_number,
                    }
                break

    fallback_successes = 0
    with args.fallback_results.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            compact = compact_row(
                row,
                route="like_fulltext_fallback_plus_vector",
                raw_path=args.fallback_results,
                raw_line_number=line_number,
                audit=audit,
                top_hits=args.top_hits,
                preview_chars=args.preview_chars,
            )
            question_id = compact["id"]
            if question_id in seen_ids:
                raise SystemExit(f"duplicate fallback question id: {question_id}")
            seen_ids.add(question_id)
            compact_rows.append(compact)
            fallback_successes += 1

    with args.output_jsonl.open("w", encoding="utf-8") as output:
        for row in compact_rows:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    aggregate_summary, _ = aggregate_rows(compact_rows)
    summary = {
        "schema_version": "moi-rag-bench-v0.1-local-eval-mixed-v1",
        "planned_rows": len(audit),
        "merged_rows": len(compact_rows),
        "successful_rows": sum(1 for row in compact_rows if row.get("status") == "ok"),
        "native_success_rows": native_successes,
        "fallback_success_rows": fallback_successes,
        "complete": len(compact_rows) == len(audit) and len(seen_ids) == len(audit),
        "native_initial_run": {
            "attempts": args.native_success_count + (1 if native_failure else 0),
            "successful_attempts": args.native_success_count,
            "failed_attempts": 1 if native_failure else 0,
            "failure": native_failure,
            "raw_results": str(args.native_results),
        },
        "fallback_run": {
            "successful_attempts": fallback_successes,
            "raw_results": str(args.fallback_results),
        },
        "compact_ledger": str(args.output_jsonl),
        **aggregate_summary,
    }
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(args.report_md, summary)
    print(json.dumps({
        "complete": summary["complete"],
        "planned_rows": summary["planned_rows"],
        "merged_rows": summary["merged_rows"],
        "native_success_rows": native_successes,
        "fallback_success_rows": fallback_successes,
        "output": str(args.output_jsonl),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
