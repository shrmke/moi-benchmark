#!/usr/bin/env python3
"""Build the MOI-inclusive MOI RAG Benchmark 2.0 report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
EVAL_SCRIPTS = ROOT / "local-rag-platforms/scripts/evaluation"
sys.path.insert(0, str(EVAL_SCRIPTS))

from competitor_eval_metric_registry import REGISTRY_VERSION, percentile  # noqa: E402
from competitor_eval_metrics import (  # noqa: E402
    _mixed_ranked_values,
    aggregate_run,
)


BASE_ARTIFACT = ROOT / "results/reports/moi-rag-bench-v0.3-final-data-to-eval-report.artifact.json"
BASE_CHART_MAP = ROOT / "results/reports/moi-rag-bench-v0.3-final-data-to-eval-report.chart-map.json"
SERIAL_REPORT = ROOT / "runs/bench-v0.3-final/moi-rag-bench-v0.3-final-serial-report.json"
V3_PACKAGE = ROOT / "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval"
MOI_RUN = ROOT / "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun"
MOI_NATIVE = MOI_RUN / "moi/cli-output-global/20260826-115956.073"
MOI_RESULTS = MOI_NATIVE / "results.jsonl"
MOI_INGEST_STATE = MOI_NATIVE / "ingest-state.json"
MOI_JUDGE = MOI_RUN / "judge/judge-terminal-ledger.jsonl"
MOI_JUDGE_START = MOI_RUN / "judge/judge-start-record.json"
RUNS = {
    "dify": ROOT / "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool",
    "fastgpt": ROOT / "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k",
    "maxkb": ROOT / "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f",
}
SERIAL_KEYS = {"dify": "dify_local", "fastgpt": "fastgpt_local", "maxkb": "maxkb_local"}
PLATFORM_ORDER = ("moi", "dify", "fastgpt", "maxkb")
OUTPUT_STEM = "moi-rag-benchmark-report-v2.0"
DEFAULT_SUMMARY = ROOT / f"runs/bench-v0.3-final/{OUTPUT_STEM}-summary.json"
DEFAULT_ARTIFACT = ROOT / f"results/reports/{OUTPUT_STEM}.artifact.json"
DEFAULT_CHART_MAP = ROOT / f"results/reports/{OUTPUT_STEM}.chart-map.json"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object: {path}:{number}")
            rows.append(value)
    return rows


def find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    return next(item for item in items if item.get("id") == item_id)


def compact_metric(value: Mapping[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    observed_n = int(value.get("observed_n") or value.get("eligible_n") or 0)
    numerator = value.get("numerator")
    return {
        "value": value.get("value"),
        "planned_n": int(value.get("planned_n") or value.get("denominator") or 0),
        "observed_n": observed_n,
        "missing_n": int(value.get("missing_n") or 0),
        "failed_n": int(value.get("failed_n") or 0),
        "na_reason": value.get("na_reason") or value.get("reason"),
        "observed_mean": (
            float(numerator) / observed_n
            if value.get("value") is not None and isinstance(numerator, (int, float)) and observed_n
            else value.get("value")
        ),
    }


def latest_rows(path: Path, key_fields: tuple[str, ...]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {
        tuple(row.get(field) for field in key_fields): row
        for row in read_jsonl(path)
    }


def summarize_moi_native_results(path: Path) -> dict[str, Any]:
    question_ids: set[str] = set()
    status_counts: dict[str, int] = {}
    latency_values: dict[str, list[float]] = {"retrieval": [], "qa": [], "e2e": []}
    embedding_models: set[str] = set()
    generation_models: set[str] = set()
    rows = 0
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Expected JSON object: {path}:{number}")
            rows += 1
            question_id = row.get("case", {}).get("id")
            if isinstance(question_id, str):
                question_ids.add(question_id)
            status = str(row.get("status") or "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
            retrieval_ms = row.get("retrieval_latency_ms")
            qa_ms = row.get("generation_latency_ms")
            if isinstance(retrieval_ms, (int, float)):
                latency_values["retrieval"].append(float(retrieval_ms))
            if isinstance(qa_ms, (int, float)):
                latency_values["qa"].append(float(qa_ms))
            if isinstance(retrieval_ms, (int, float)) and isinstance(qa_ms, (int, float)):
                latency_values["e2e"].append(float(retrieval_ms + qa_ms))
            if row.get("embedding_model"):
                embedding_models.add(str(row["embedding_model"]))
            if row.get("generation_model"):
                generation_models.add(str(row["generation_model"]))
    return {
        "rows": rows,
        "question_ids": question_ids,
        "status_counts": status_counts,
        "embedding_models": sorted(embedding_models),
        "generation_models": sorted(generation_models),
        "latency_ms": {
            stage: {
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
                "observed_n": len(values),
            }
            for stage, values in latency_values.items()
        },
    }


def maxkb_retrieval_diagnostics(
    run: Path, questions: list[dict[str, Any]]
) -> dict[str, Any]:
    questions_by_id = {row["question_id"]: row for row in questions}
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for row in read_jsonl(run / "terminal-ledger.jsonl"):
        if row.get("stage") == "retrieval" and row.get("status") == "SUCCESS":
            rows[(str(row["question_id"]), int(row.get("repeat_id") or 1))] = row
    if len(rows) != 275:
        raise ValueError(f"MaxKB diagnostic retrieval coverage drifted: {len(rows)}/275")

    metric_names = tuple(
        [f"recall_at_{k}" for k in (1, 3, 5, 10)]
        + [f"hit_at_{k}" for k in (1, 3, 5, 10)]
        + ["mrr"]
    )
    values = {
        ordering: {name: [] for name in metric_names}
        for ordering in ("api_return_order", "comprehensive_score_desc")
    }
    latencies: list[float] = []
    eligible_n = 0
    no_gold_n = 0
    score_descending_rows = 0
    duplicate_source_document_rows = 0

    def hit_score(hit: Any) -> float:
        if not isinstance(hit, Mapping):
            return float("-inf")
        value = hit.get("comprehensive_score", hit.get("similarity"))
        return float(value) if isinstance(value, (int, float)) else float("-inf")

    def source_id(hit: Any) -> str:
        if not isinstance(hit, Mapping):
            return ""
        name = str(hit.get("document_name") or hit.get("title") or "").split("#", 1)[0]
        return Path(name).stem.casefold()

    for (question_id, _repeat_id), row in sorted(rows.items()):
        question = questions_by_id[question_id]
        hits = list(row.get("hits") or [])
        latency = row.get("latency_ms")
        if isinstance(latency, (int, float)):
            latencies.append(float(latency))
        scores = [hit_score(hit) for hit in hits]
        if all(left >= right for left, right in zip(scores, scores[1:])):
            score_descending_rows += 1
        document_ids = [source_id(hit) for hit in hits if source_id(hit)]
        if len(set(document_ids)) < len(document_ids):
            duplicate_source_document_rows += 1

        ranked_hits = {
            "api_return_order": hits,
            "comprehensive_score_desc": sorted(hits, key=hit_score, reverse=True),
        }
        raw_probe = [
            {"rank": rank, "doc_id": source_id(hit)}
            for rank, hit in enumerate(hits, 1)
        ]
        if _mixed_ranked_values(question, raw_probe, {}, 10) is None:
            no_gold_n += 1
            continue
        eligible_n += 1
        for ordering, ordered in ranked_hits.items():
            normalized = [
                {"rank": rank, "doc_id": source_id(hit)}
                for rank, hit in enumerate(ordered, 1)
            ]
            for k in (1, 3, 5, 10):
                scored = _mixed_ranked_values(question, normalized, {}, k)
                assert scored is not None
                values[ordering][f"recall_at_{k}"].append(float(scored["recall"]))
                values[ordering][f"hit_at_{k}"].append(float(scored["hit"]))
            scored = _mixed_ranked_values(question, normalized, {}, 10)
            assert scored is not None
            values[ordering]["mrr"].append(float(scored["mrr"]))

    if eligible_n != 265 or no_gold_n != 10 or len(latencies) != 275:
        raise ValueError("MaxKB diagnostic denominator drifted")
    ordered_latencies = sorted(latencies)

    def run_percentile(fraction: float) -> float:
        return round(ordered_latencies[round((len(ordered_latencies) - 1) * fraction)], 3)

    return {
        "contract": "diagnostic_admin_contract",
        "strict_comparable": False,
        "planned_n": len(rows),
        "eligible_n": eligible_n,
        "no_gold_n": no_gold_n,
        "score_descending_rows": score_descending_rows,
        "duplicate_source_document_rows": duplicate_source_document_rows,
        "latency_ms_p50": run_percentile(0.50),
        "latency_ms_p95": run_percentile(0.95),
        "latency_ms_p99": run_percentile(0.99),
        "orderings": {
            ordering: {
                name: sum(metric_values) / eligible_n
                for name, metric_values in ordering_values.items()
            }
            for ordering, ordering_values in values.items()
        },
    }


def validate_moi_run(
    questions: list[dict[str, Any]],
    native: dict[str, Any],
    aggregate: dict[str, Any],
    serial: dict[str, Any],
) -> dict[str, Any]:
    expected_question_ids = {row["question_id"] for row in questions}
    expected_document_ids = {path.stem for path in (V3_PACKAGE / "documents").glob("*.md")}
    if len(expected_question_ids) != 275 or len(expected_document_ids) != 297:
        raise ValueError("v0.3 frozen package denominator drifted")
    if native["rows"] != 275 or native["question_ids"] != expected_question_ids:
        raise ValueError("MOI native result IDs do not exactly cover v0.3")
    if native["status_counts"] != {"ok": 275}:
        raise ValueError(f"MOI native results are not all successful: {native['status_counts']}")
    if native["embedding_models"] != ["bge-m3"] or native["generation_models"] != ["deepseek-v4-flash"]:
        raise ValueError("MOI native result model contract drifted")
    if any(stage["observed_n"] != 275 for stage in native["latency_ms"].values()):
        raise ValueError("MOI native latency coverage drifted")

    ingest = read_json(MOI_INGEST_STATE)
    ingested_document_ids = {row.get("file_id") for row in ingest.get("documents") or []}
    ingested_chunks = ingest.get("chunks") or []
    if not (
        ingested_document_ids == expected_document_ids
        and ingest.get("embedding_model") == "bge-m3"
        and ingest.get("embedding_dimension") == 1024
        and isinstance(ingested_chunks, list)
        and ingested_chunks
    ):
        raise ValueError("MOI strict ingest does not exactly match the v0.3 document package")

    start = read_json(MOI_RUN / "start-record.json")
    provider = start.get("provider") or {}
    if not (
        start.get("dataset") == "moi-rag-bench-v0.3-final"
        and start.get("dataset_revision") == "curated-final-v0.3"
        and start.get("planned", {}).get("files") == 297
        and start.get("planned", {}).get("questions") == 275
        and provider.get("embedding") == "maas/bge-m3/1024"
        and provider.get("llm") == "deepseek-official/deepseek-v4-flash"
        and provider.get("image_question_count") == 0
        and provider.get("thinking", {}).get("type") == "disabled"
    ):
        raise ValueError("MOI strict start-record contract drifted")

    terminal = latest_rows(MOI_RUN / "terminal-ledger.jsonl", ("question_id", "repeat_id", "stage"))
    for stage in ("retrieval", "qa"):
        rows = [row for key, row in terminal.items() if key[2] == stage]
        if len(rows) != 275 or {row.get("question_id") for row in rows} != expected_question_ids:
            raise ValueError(f"MOI {stage} terminal coverage drifted")
        if any(row.get("status") != "SUCCESS" for row in rows):
            raise ValueError(f"MOI {stage} contains a non-success terminal row")

    protocol = serial["judge_protocol"]
    judge_start = read_json(MOI_JUDGE_START).get("judge") or {}
    judges = latest_rows(MOI_JUDGE, ("question_id", "repeat_id"))
    if len(judges) != 275 or {row.get("question_id") for row in judges.values()} != expected_question_ids:
        raise ValueError("MOI Judge terminal coverage drifted")
    for row in [judge_start, *judges.values()]:
        thinking = row.get("thinking")
        thinking_type = thinking.get("type") if isinstance(thinking, Mapping) else thinking
        if not (
            row.get("provider") == protocol["provider"]
            and row.get("model") == protocol["model"]
            and row.get("temperature") == protocol["temperature"]
            and row.get("prompt_hash") == protocol["prompt_hash"]
            and row.get("prompt_version") == protocol["prompt_version"]
            and thinking_type == protocol["thinking"]
        ):
            raise ValueError("MOI Judge parameters differ from the serial Judge protocol")
    if any(row.get("status") != "SUCCESS" for row in judges.values()):
        raise ValueError("MOI Judge contains a non-success latest terminal row")

    unified = aggregate.get("moi_unified") or {}
    if not (
        aggregate.get("status") == "COMPLETE"
        and unified.get("denominator") == {
            "planned_n": 275,
            "terminal_n": 275,
            "valid_n": 275,
            "failed_n": 0,
            "unsupported_n": 0,
            "pending_n": 0,
        }
        and unified.get("status_counts") == {"SUCCESS": 275}
    ):
        raise ValueError("MOI unified aggregate is not a complete 275/275 result")

    return {
        "question_ids_equal": 275,
        "indexed_documents": 297,
        "indexed_chunks": len(ingested_chunks),
        "native_answer_success": 275,
        "retrieval_success": 275,
        "qa_success": 275,
        "judge_success": 275,
        "judge_physical_rows": len(read_jsonl(MOI_JUDGE)),
        "judge_latest_rows": len(judges),
        "judge_parameters_equal": True,
        "embedding_model": "bge-m3",
        "generation_model": "deepseek-v4-flash",
    }


def build_moi_platform(aggregate: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    unified = aggregate["moi_unified"]
    metrics = unified["metrics"]
    judge_dimensions = unified["judge"].get("dimensions") or unified["judge"]
    return {
        "label": "MOI",
        "comparison_tier": "strict_same_corpus_native",
        "corpus_documents": 297,
        "question_records": 275,
        "retrieval": {
            name: compact_metric(metrics.get(name))
            for name in ("recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10", "mrr")
        },
        "qa": {
            name: compact_metric(metrics.get(name))
            for name in ("normalized_em", "token_f1", "contains_gold", "answer_non_empty")
        },
        "latency_ms": native["latency_ms"],
        "judge": {
            "status": "COMPLETE",
            "dimensions": {
                name: compact_metric(judge_dimensions.get(name))
                for name in (
                    "answer_relevance",
                    "contradiction_free",
                    "instruction_compliance",
                    "response_claim_correctness",
                    "runtime_context_faithfulness",
                    "unsupported_claim_rate",
                )
            },
        },
        "operations": {
            stage: {"planned": 275, "success": 275, "failed": 0}
            for stage in ("retrieval", "qa", "judge")
        },
        "contracts": {
            "retrieval": "matrixflow_cli_native_hybrid",
            "qa": "matrixflow_cli_native_generation",
        },
        "provider": {
            "embedding": {"name": "maas", "model": "bge-m3", "dimension": 1024},
            "text_llm": {"name": "deepseek-official", "model": "deepseek-v4-flash"},
            "image_llm": "NOT_APPLICABLE",
        },
        "source_run": str(MOI_RUN.relative_to(ROOT)),
    }


def build_competitor_platform(
    key: str, run: Path, serial: dict[str, Any], questions: list[dict[str, Any]]
) -> dict[str, Any]:
    result = aggregate_run(run, V3_PACKAGE)
    if result.get("status") not in {"COMPLETE", "PARTIAL"}:
        raise ValueError(f"Unified metrics failed for {key}: {result.get('status')}")
    unified = result["moi_unified"]
    metrics = unified["metrics"]
    judge_dimensions = unified["judge"].get("dimensions") or unified["judge"]
    serial_result = serial["platforms"][SERIAL_KEYS[key]]
    comparison_tier = (
        "strict_same_corpus_native"
        if key in {"dify", "fastgpt"}
        else "offline_restored_diagnostic_retrieval_external_generation"
    )
    retrieval_names = ("recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10", "mrr")
    retrieval = {name: compact_metric(metrics.get(name)) for name in retrieval_names}
    strict_retrieval = retrieval if key == "maxkb" else None
    contracts = dict(serial_result["contracts"])
    retrieval_method = "native_rank_order"
    diagnostic = maxkb_retrieval_diagnostics(run, questions) if key == "maxkb" else None
    if diagnostic is not None:
        serial_retrieval = serial_result["retrieval"]
        raw = diagnostic["orderings"]["api_return_order"]
        if not (
            abs(raw["hit_at_10"] - serial_retrieval["evidence_recall_at_10"]) < 1e-12
            and abs(raw["mrr"] - serial_retrieval["mrr"]) < 1e-12
        ):
            raise ValueError("MaxKB diagnostic replay differs from the frozen serial result")
        restored = diagnostic["orderings"]["comprehensive_score_desc"]
        retrieval = {
            name: {
                "value": round(restored[name], 6),
                "planned_n": diagnostic["eligible_n"],
                "observed_n": diagnostic["eligible_n"],
                "missing_n": 0,
                "failed_n": 0,
                "na_reason": None,
                "observed_mean": restored[name],
            }
            for name in retrieval_names
        }
        retrieval_method = "offline_comprehensive_score_desc_restoration"
        contracts["retrieval"] = "diagnostic_admin_contract + offline_comprehensive_score_desc_restoration"
    latency = {
        stage: {name: compact_metric(record)["value"] for name, record in values.items()}
        for stage, values in unified["latency_ms"].items()
    }
    if diagnostic is not None:
        latency["retrieval"] = {
            "p50": diagnostic["latency_ms_p50"],
            "p95": diagnostic["latency_ms_p95"],
            "p99": diagnostic["latency_ms_p99"],
        }
    return {
        "label": serial_result["label"] if key != "maxkb" else "MaxKB (offline-restored)",
        "comparison_tier": comparison_tier,
        "corpus_documents": 297,
        "question_records": 275,
        "retrieval": retrieval,
        "retrieval_strict": strict_retrieval,
        "retrieval_metric_method": retrieval_method,
        "retrieval_diagnostic": serial_result["retrieval"] if key == "maxkb" else None,
        "retrieval_diagnostic_recomputed": diagnostic,
        "qa": {name: compact_metric(metrics.get(name)) for name in ("normalized_em", "token_f1", "contains_gold", "answer_non_empty")},
        "latency_ms": latency,
        "judge": {
            "status": unified["judge"].get("status", "COMPLETE"),
            "dimensions": {
                name: compact_metric(judge_dimensions.get(name))
                for name in (
                    "answer_relevance",
                    "contradiction_free",
                    "instruction_compliance",
                    "response_claim_correctness",
                    "runtime_context_faithfulness",
                    "unsupported_claim_rate",
                )
            },
        },
        "operations": serial["execution"]["all_stage_counts"],
        "contracts": contracts,
        "provider": serial_result["provider"],
        "source_run": str(run.relative_to(ROOT)),
    }


def build_summary(serial: dict[str, Any]) -> dict[str, Any]:
    questions = read_jsonl(V3_PACKAGE / "questions.jsonl")
    moi_aggregate = aggregate_run(MOI_RUN, V3_PACKAGE)
    moi_native = summarize_moi_native_results(MOI_RESULTS)
    proof = validate_moi_run(questions, moi_native, moi_aggregate, serial)
    platforms = {"moi": build_moi_platform(moi_aggregate, moi_native)}
    platforms.update({key: build_competitor_platform(key, run, serial, questions) for key, run in RUNS.items()})
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema": "moi-rag-benchmark-report-v2-summary-v3",
        "status": "COMPLETE_WITH_COMPARABILITY_LIMITS",
        "generated_at": generated_at,
        "dataset": {
            "id": "moi-rag-bench-v0.3-final",
            "revision": "curated-final-v0.3",
            "condition": "text-only-no-mllm",
            "document_files": 297,
            "question_records": 275,
            "retrieval_gold_denominator": 265,
            "mllm_calls": 0,
        },
        "platform_order": list(PLATFORM_ORDER),
        "platforms": platforms,
        "metric_contract": {
            "registry_version": REGISTRY_VERSION,
            "retrieval_denominator": "265 questions with at least one frozen Gold document; misses remain zero",
            "qa_denominator": "275 frozen questions; deterministic normalization and token overlap",
            "judge_denominator": "275 planned Judge units; any dimension-level N/A keeps the strict aggregate N/A",
            "latency": "nearest-rank p50/p95/p99 over observed native-stage milliseconds; MOI uses MatrixFlow native result timings",
            "total_score": "NOT_COMPUTED",
        },
        "judge_protocol": serial["judge_protocol"],
        "comparability": {
            "strict_same_corpus_native_platforms": ["moi", "dify", "fastgpt"],
            "caveated_platforms": ["maxkb"],
            "moi_strict_run_proof": proof,
            "maxkb_limit": "MaxKB uses diagnostic_admin_contract retrieval and external DeepSeek generation; retrieval ranks are restored offline from vendor comprehensive_score because paragraph hydration loses return order.",
            "maxkb_diagnostic_policy": "Use comprehensive_score-desc offline-restored Recall@k/MRR in the main comparison with a caveated label; preserve strict public retrieval as N/A and API-return-order values in the diagnostic sensitivity table.",
            "ranking_policy": "Dimension-level comparisons only; no opaque total score or unconditional winner.",
        },
    }


def value(platform: dict[str, Any], group: str, name: str) -> Any:
    return platform[group][name]["value"]


def report_datasets(summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    platforms = summary["platforms"]
    retrieval_quality: list[dict[str, Any]] = []
    latency: list[dict[str, Any]] = []
    qa_quality: list[dict[str, Any]] = []
    judge_quality: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    judge_results: list[dict[str, Any]] = []
    maxkb_diagnostic: list[dict[str, Any]] = []
    retrieval_labels = (("recall_at_1", "Recall@1"), ("recall_at_10", "Recall@10"), ("mrr", "MRR"))
    judge_labels = (("answer_relevance", "答案相关性"), ("contradiction_free", "无矛盾"), ("instruction_compliance", "指令遵循"))

    for order, key in enumerate(PLATFORM_ORDER, 1):
        platform = platforms[key]
        common = {
            "platform": platform["label"],
            "platform_order": order,
            "comparison_tier": platform["comparison_tier"],
            "corpus_documents": platform["corpus_documents"],
            "retrieval_metric_method": platform.get("retrieval_metric_method", "native_rank_order"),
        }
        for name, label in retrieval_labels:
            metric_value = value(platform, "retrieval", name)
            if metric_value is not None:
                retrieval_quality.append({**common, "metric": label, "value": metric_value})
        for stage, label in (("retrieval", "Retrieval"), ("qa", "QA"), ("e2e", "E2E")):
            latency_value = platform["latency_ms"][stage].get("p50")
            if latency_value is not None:
                latency.append(
                    {
                        **common,
                        "stage": label,
                        "p50_seconds": latency_value / 1000,
                        "p95_seconds": platform["latency_ms"][stage].get("p95") / 1000,
                        "p99_seconds": platform["latency_ms"][stage].get("p99") / 1000,
                    }
                )
        for name, label in (("token_f1", "Token F1"), ("contains_gold", "Contains gold")):
            qa_quality.append({**common, "metric": label, "value": value(platform, "qa", name)})
        dimensions = platform["judge"]["dimensions"]
        for name, label in judge_labels:
            record = dimensions[name]
            if record["value"] is not None:
                judge_quality.append({**common, "metric": label, "value": record["value"], "observed_n": record["observed_n"]})
        unsupported_record = dimensions["unsupported_claim_rate"]
        if unsupported_record["value"] is not None:
            unsupported.append({**common, "value": unsupported_record["value"], "observed_n": unsupported_record["observed_n"]})

        results.append(
            {
                **common,
                "retrieval_denominator": platform["retrieval"]["recall_at_10"]["planned_n"],
                "recall_at_1": value(platform, "retrieval", "recall_at_1"),
                "recall_at_3": value(platform, "retrieval", "recall_at_3"),
                "recall_at_5": value(platform, "retrieval", "recall_at_5"),
                "recall_at_10": value(platform, "retrieval", "recall_at_10"),
                "mrr": value(platform, "retrieval", "mrr"),
                "retrieval_p50_s": platform["latency_ms"]["retrieval"].get("p50") / 1000 if platform["latency_ms"]["retrieval"].get("p50") is not None else None,
                "retrieval_p95_s": platform["latency_ms"]["retrieval"].get("p95") / 1000 if platform["latency_ms"]["retrieval"].get("p95") is not None else None,
                "normalized_em": value(platform, "qa", "normalized_em"),
                "token_f1": value(platform, "qa", "token_f1"),
                "contains_gold": value(platform, "qa", "contains_gold"),
                "qa_p50_s": platform["latency_ms"]["qa"].get("p50") / 1000,
                "qa_p95_s": platform["latency_ms"]["qa"].get("p95") / 1000,
                "e2e_p50_s": platform["latency_ms"]["e2e"].get("p50") / 1000,
                "e2e_p95_s": platform["latency_ms"]["e2e"].get("p95") / 1000,
                "retrieval_contract": platform["contracts"]["retrieval"],
                "qa_contract": platform["contracts"]["qa"],
            }
        )
        response = dimensions["response_claim_correctness"]
        context = dimensions["runtime_context_faithfulness"]
        judge_results.append(
            {
                **common,
                "answer_relevance": dimensions["answer_relevance"]["value"],
                "contradiction_free": dimensions["contradiction_free"]["value"],
                "instruction_compliance": dimensions["instruction_compliance"]["value"],
                "response_claim_correctness": response["value"],
                "runtime_context_faithfulness": context["value"],
                "unsupported_claim_rate": unsupported_record["value"],
                "unsupported_observed_mean": unsupported_record["observed_mean"],
                "response_missing_n": response["missing_n"],
                "context_missing_n": context["missing_n"],
                "unsupported_missing_n": unsupported_record["missing_n"],
            }
        )

    diagnostic = platforms["maxkb"]["retrieval_diagnostic_recomputed"]
    for order, (ordering, label) in enumerate(
        (
            ("api_return_order", "API 原始返回顺序"),
            ("comprehensive_score_desc", "comprehensive_score 降序（诊断）"),
        ),
        1,
    ):
        row = diagnostic["orderings"][ordering]
        maxkb_diagnostic.append(
            {
                "ordering_rank": order,
                "ordering": label,
                "planned_questions": diagnostic["planned_n"],
                "gold_eligible_questions": diagnostic["eligible_n"],
                "recall_at_1": row["recall_at_1"],
                "recall_at_3": row["recall_at_3"],
                "recall_at_5": row["recall_at_5"],
                "recall_at_10": row["recall_at_10"],
                "hit_at_1": row["hit_at_1"],
                "hit_at_3": row["hit_at_3"],
                "hit_at_5": row["hit_at_5"],
                "hit_at_10": row["hit_at_10"],
                "mrr_at_10": row["mrr"],
                "retrieval_p50_s": diagnostic["latency_ms_p50"] / 1000,
                "retrieval_p95_s": diagnostic["latency_ms_p95"] / 1000,
                "contract": diagnostic["contract"],
                "strict_comparable": "否",
            }
        )

    return {
        "eval_completion": [
            {
                "platforms": len(platforms),
                "per_platform_questions": 275,
                "retrieval_success": sum(platform["operations"]["retrieval"]["success"] for platform in platforms.values()),
                "qa_success": sum(platform["operations"]["qa"]["success"] for platform in platforms.values()),
                "judge_success": sum(platform["operations"]["judge"]["success"] for platform in platforms.values()),
                "terminal_failures": sum(
                    platform["operations"][stage]["failed"]
                    for platform in platforms.values()
                    for stage in ("retrieval", "qa", "judge")
                ),
                "strictly_comparable_platforms": len(summary["comparability"]["strict_same_corpus_native_platforms"]),
                "caveated_platforms": len(summary["comparability"]["caveated_platforms"]),
            }
        ],
        "eval_retrieval_quality": retrieval_quality,
        "eval_latency": latency,
        "eval_qa_quality": qa_quality,
        "eval_judge_quality": judge_quality,
        "eval_unsupported_claims": unsupported,
        "eval_results": results,
        "eval_judge_results": judge_results,
        "eval_maxkb_retrieval_diagnostic": maxkb_diagnostic,
    }


def build_artifact(base: dict[str, Any], summary: dict[str, Any], summary_path: Path) -> dict[str, Any]:
    manifest = base["manifest"]
    generated_at = summary["generated_at"]
    title = "MOI RAG Benchmark 2.0：四平台数据与评测总报告"
    manifest.update(
        {
            "title": title,
            "description": "MOI-inclusive four-platform report with unified metrics and explicit comparability tiers.",
            "generatedAt": generated_at,
        }
    )
    base["snapshot"]["generatedAt"] = generated_at
    base["snapshot"]["datasets"].update(report_datasets(summary))
    maxkb_diagnostic = summary["platforms"]["maxkb"]["retrieval_diagnostic_recomputed"]
    maxkb_raw = maxkb_diagnostic["orderings"]["api_return_order"]
    maxkb_sorted = maxkb_diagnostic["orderings"]["comprehensive_score_desc"]
    moi = summary["platforms"]["moi"]
    dify = summary["platforms"]["dify"]
    fastgpt = summary["platforms"]["fastgpt"]
    maxkb = summary["platforms"]["maxkb"]
    report_date = datetime.fromisoformat(generated_at).astimezone().date().isoformat()

    find(manifest["blocks"], "title")["body"] = (
        f"# {title}\n\n"
        "**数据版本：** 0.3.0 · `curated-final-v0.3`  \n"
        "**评测条件：** `text-only-no-mllm`  \n"
        "**平台：** MOI → Dify → FastGPT → MaxKB  \n"
        f"**报告日期：** {report_date}"
    )
    abstract = find(manifest["blocks"], "abstract")
    abstract.pop("sourceId", None)
    abstract["body"] = (
        "## Executive Summary\n\n"
        "- **Benchmark 2.0 已整合 MOI 的 v0.3 严格复跑。** 四个平台都使用同一 297 文档语料和 275 QA，最终 retrieval、QA、Judge 均为 275/275 成功，Judge 参数一致；MaxKB 仍保留 admin 诊断检索＋外部生成的契约边界。\n"
        f"- **检索呈现不同优势。** 在 265 条带 Gold 文档的问题上，MaxKB 离线恢复排序后的 Recall@10/MRR 为 {value(maxkb, 'retrieval', 'recall_at_10'):.3f}/{value(maxkb, 'retrieval', 'mrr'):.3f}，数值最高但属于带条件结果；严格原生链路中 FastGPT/Dify/MOI 的 Recall@10 为 {value(fastgpt, 'retrieval', 'recall_at_10'):.3f}/{value(dify, 'retrieval', 'recall_at_10'):.3f}/{value(moi, 'retrieval', 'recall_at_10'):.3f}。MOI Recall@1 为 {value(moi, 'retrieval', 'recall_at_1'):.3f}，是四平台最高。\n"
        f"- **MOI 文本答案指标高于 Dify/FastGPT，但原生检索仍是主要时延来源。** MOI Token F1 为 {value(moi, 'qa', 'token_f1'):.3f}；原生 retrieval p50/p95 为 {moi['latency_ms']['retrieval']['p50'] / 1000:.3f}/{moi['latency_ms']['retrieval']['p95'] / 1000:.3f} 秒，QA p50 为 {moi['latency_ms']['qa']['p50'] / 1000:.3f} 秒。\n"
        "- **不发布单一总冠军。** MaxKB 链路非严格原生，Judge 是适配型参考答案 rubric，核心指标尚无置信区间；本报告只做分维度比较，不计算会掩盖契约与统计不确定性的总分。"
    )

    preflight = find(manifest["blocks"], "preflight_note")
    preflight.pop("sourceId", None)
    preflight["body"] = (
        f"MOI v0.3 严格复跑已确认独立索引 297 份文档、{summary['comparability']['moi_strict_run_proof']['indexed_chunks']:,} 个 chunk，原生结果 275/275 成功；"
        "Dify、FastGPT、MaxKB 的 v0.3 Final 串行终态也均为 275/275 retrieval、QA 与 Judge 成功。"
        "四平台 Judge provider/model/prompt hash/temperature/thinking 参数完全一致；MOI 原生时延来自 MatrixFlow 结果文件，不使用统一 ledger 中仅代表适配器抽取的耗时。"
    )

    card = find(manifest["cards"], "eval_completion_card")
    card.update(
        {
            "description": "四平台最终结果包完成度；三平台同语料原生对照，MaxKB 单列带条件结果。",
            "sourceId": "benchmark_v2",
            "metrics": [
                {"label": "纳入平台", "field": "platforms", "format": "number"},
                {"label": "每平台 QA", "field": "per_platform_questions", "format": "number"},
                {"label": "Retrieval 成功", "field": "retrieval_success", "format": "number"},
                {"label": "QA 成功", "field": "qa_success", "format": "number"},
                {"label": "Judge 成功", "field": "judge_success", "format": "number"},
                {"label": "严格可比平台", "field": "strictly_comparable_platforms", "format": "number"},
                {"label": "带条件平台", "field": "caveated_platforms", "format": "number"},
            ],
        }
    )

    chart_updates = {
        "eval_retrieval_quality_chart": (
            "统一检索质量对比（MaxKB 为离线恢复）",
            "Gold 文档有效分母 265；MOI/Dify/FastGPT 使用原生排序，MaxKB 按 vendor comprehensive_score 离线恢复排序。",
            "265 条带 Gold 文档的问题；MaxKB 为带条件离线恢复值",
        ),
        "eval_latency_chart": (
            "P50 时延对比",
            "Retrieval、QA 与 E2E；柱为 p50，tooltip 保留 p95/p99；平台原生计时边界并不完全相同。",
            "每平台 275 QA；MOI 为 MatrixFlow 原生阶段计时，MaxKB retrieval 为诊断链路",
        ),
        "eval_qa_quality_chart": (
            "答案文本指标对比",
            "每平台 275 QA；Token F1 与 contains-gold 均按 0–1 口径。",
            "每平台 275 QA",
        ),
        "eval_judge_quality_chart": (
            "统一 Judge 质量维度",
            "相同 DeepSeek Judge、prompt hash、temperature=0；图中三项均完整覆盖 275 QA。",
            "每平台 275 Judge 单元",
        ),
        "eval_unsupported_claims_chart": (
            "Unsupported-claim rate",
            "越低越好；四平台该维度均完整覆盖 275 Judge 单元。",
            "每平台 275 Judge 单元",
        ),
    }
    for chart_id, (title_value, subtitle, denominator) in chart_updates.items():
        chart = find(manifest["charts"], chart_id)
        chart.update({"title": title_value, "subtitle": subtitle, "sourceId": "benchmark_v2"})
        chart["comparisonContext"]["denominator"] = denominator
        tooltips = chart["encodings"].setdefault("tooltip", [])
        for tooltip in (
            {"field": "comparison_tier", "type": "nominal", "label": "可比性"},
            {"field": "corpus_documents", "type": "quantitative", "label": "索引文档数"},
        ):
            if tooltip not in tooltips:
                tooltips.append(tooltip)
        if chart_id == "eval_retrieval_quality_chart":
            method_tooltip = {"field": "retrieval_metric_method", "type": "nominal", "label": "排序口径"}
            if method_tooltip not in tooltips:
                tooltips.append(method_tooltip)
    latency_chart = find(manifest["charts"], "eval_latency_chart")
    p99_tooltip = {"field": "p99_seconds", "type": "quantitative", "label": "P99 时延", "unit": "s"}
    if p99_tooltip not in latency_chart["encodings"]["tooltip"]:
        latency_chart["encodings"]["tooltip"].append(p99_tooltip)

    maxkb_table = {
        "id": "eval_maxkb_retrieval_diagnostic_table",
        "title": "MaxKB 离线恢复排序审计",
        "subtitle": "主表采用 comprehensive_score 降序恢复值；API 原始顺序保留为敏感性对照，public retrieval 仍为 N/A。",
        "dataset": "eval_maxkb_retrieval_diagnostic",
        "sourceId": "benchmark_v2",
        "columns": [
            {"field": "ordering", "label": "排序口径", "type": "text"},
            {"field": "gold_eligible_questions", "label": "Gold 分母", "format": "number"},
            {"field": "recall_at_1", "label": "R@1", "format": "percent"},
            {"field": "recall_at_3", "label": "R@3", "format": "percent"},
            {"field": "recall_at_5", "label": "R@5", "format": "percent"},
            {"field": "recall_at_10", "label": "R@10", "format": "percent"},
            {"field": "hit_at_1", "label": "Hit@1", "format": "percent"},
            {"field": "hit_at_3", "label": "Hit@3", "format": "percent"},
            {"field": "hit_at_5", "label": "Hit@5", "format": "percent"},
            {"field": "hit_at_10", "label": "Hit@10", "format": "percent"},
            {"field": "mrr_at_10", "label": "MRR@10", "format": "number"},
            {"field": "retrieval_p50_s", "label": "p50(s)", "format": "number"},
            {"field": "retrieval_p95_s", "label": "p95(s)", "format": "number"},
            {"field": "contract", "label": "契约", "type": "text"},
            {"field": "strict_comparable", "label": "严格可比", "type": "text"},
        ],
        "defaultSort": {"field": "ordering", "direction": "asc"},
    }
    manifest["tables"] = [
        table for table in manifest["tables"]
        if table.get("id") != maxkb_table["id"]
    ] + [maxkb_table]
    manifest["blocks"] = [
        block for block in manifest["blocks"]
        if block.get("id") not in {"eval_maxkb_diagnostic_finding", "eval_maxkb_diagnostic_table_block"}
    ]
    insert_at = next(
        index for index, block in enumerate(manifest["blocks"])
        if block.get("id") == "eval_retrieval_chart_block"
    ) + 1
    manifest["blocks"][insert_at:insert_at] = [
        {
            "id": "eval_maxkb_diagnostic_finding",
            "type": "markdown",
            "body": (
                "### MaxKB 主指标采用离线恢复排序，原始顺序保留作敏感性对照\n\n"
                "此前 MMDocIR 评测已确认：MaxKB `hit_test` 的有序命中在 paragraph hydration 后丢失返回顺序，但每条仍保留 `comprehensive_score/similarity`。"
                "既有评估器因此按 vendor 分数字段降序恢复排名；本次对 v0.3 ledger 离线重放同一规则，并将恢复值纳入主 Retrieval 图表。"
                f"本地保留了 {maxkb_diagnostic['planned_n']} 条 `diagnostic_admin_contract` hit-test 结果，其中 "
                f"{maxkb_diagnostic['eligible_n']} 条有冻结 Gold 文档、{maxkb_diagnostic['no_gold_n']} 条不适用检索 Recall。"
                f"API 原始顺序的 Recall@1/3/5/10 为 {maxkb_raw['recall_at_1']:.3f}/{maxkb_raw['recall_at_3']:.3f}/"
                f"{maxkb_raw['recall_at_5']:.3f}/{maxkb_raw['recall_at_10']:.3f}，MRR@10 为 {maxkb_raw['mrr']:.3f}；"
                f"按 `comprehensive_score` 降序后为 {maxkb_sorted['recall_at_1']:.3f}/{maxkb_sorted['recall_at_3']:.3f}/"
                f"{maxkb_sorted['recall_at_5']:.3f}/{maxkb_sorted['recall_at_10']:.3f}，MRR@10 为 {maxkb_sorted['mrr']:.3f}。"
                f"仅 {maxkb_diagnostic['score_descending_rows']}/275 条 API 结果本身已按分数降序，"
                f"{maxkb_diagnostic['duplicate_source_document_rows']}/275 条含同一源文档的重复段落，因此两种排序差异很大。"
                f"Hit@10 为 {maxkb_raw['hit_at_10']:.3f}，检索 p50/p95 为 "
                f"{maxkb_diagnostic['latency_ms_p50'] / 1000:.3f}/{maxkb_diagnostic['latency_ms_p95'] / 1000:.3f} 秒。"
                "**主表中的 MaxKB 值代表离线恢复后的 admin 检索排序，不代表 public API 原始返回序列；API 原始顺序只作敏感性对照。**"
            ),
            "sourceId": "benchmark_v2",
        },
        {
            "id": "eval_maxkb_diagnostic_table_block",
            "type": "table",
            "tableId": "eval_maxkb_retrieval_diagnostic_table",
        },
    ]

    results_table = find(manifest["tables"], "eval_results_table")
    results_table.update(
        {
            "title": "统一 Retrieval、QA 与时延精确结果",
            "subtitle": "Recall/MRR 使用统一 Gold 与 scorer；MaxKB 为离线恢复排序并显式标注，时延单位为秒。",
            "sourceId": "benchmark_v2",
            "columns": [
                {"field": "platform", "label": "平台", "type": "text"},
                {"field": "comparison_tier", "label": "可比性", "type": "text"},
                {"field": "corpus_documents", "label": "索引文档", "format": "number"},
                {"field": "retrieval_denominator", "label": "R 分母", "format": "number"},
                {"field": "recall_at_1", "label": "R@1", "format": "percent"},
                {"field": "recall_at_3", "label": "R@3", "format": "percent"},
                {"field": "recall_at_5", "label": "R@5", "format": "percent"},
                {"field": "recall_at_10", "label": "R@10", "format": "percent"},
                {"field": "mrr", "label": "MRR", "format": "number"},
                {"field": "retrieval_p50_s", "label": "R p50(s)", "format": "number"},
                {"field": "retrieval_p95_s", "label": "R p95(s)", "format": "number"},
                {"field": "normalized_em", "label": "EM", "format": "percent"},
                {"field": "token_f1", "label": "Token F1", "format": "percent"},
                {"field": "contains_gold", "label": "Contains gold", "format": "percent"},
                {"field": "qa_p50_s", "label": "QA p50(s)", "format": "number"},
                {"field": "qa_p95_s", "label": "QA p95(s)", "format": "number"},
                {"field": "e2e_p50_s", "label": "E2E p50(s)", "format": "number"},
                {"field": "e2e_p95_s", "label": "E2E p95(s)", "format": "number"},
                {"field": "retrieval_contract", "label": "Retrieval contract", "type": "text"},
                {"field": "retrieval_metric_method", "label": "排序口径", "type": "text"},
                {"field": "qa_contract", "label": "QA contract", "type": "text"},
            ],
        }
    )
    judge_table = find(manifest["tables"], "eval_judge_results_table")
    judge_table.update(
        {
            "title": "统一 Judge 精确结果",
            "subtitle": "严格值要求 275 完整分母；observed mean 仅作诊断，不替代 N/A。",
            "sourceId": "benchmark_v2",
            "columns": [
                {"field": "platform", "label": "平台", "type": "text"},
                {"field": "comparison_tier", "label": "可比性", "type": "text"},
                {"field": "answer_relevance", "label": "相关性", "format": "number"},
                {"field": "contradiction_free", "label": "无矛盾", "format": "number"},
                {"field": "instruction_compliance", "label": "指令遵循", "format": "number"},
                {"field": "response_claim_correctness", "label": "声明正确性", "format": "number"},
                {"field": "runtime_context_faithfulness", "label": "上下文忠实度", "format": "number"},
                {"field": "unsupported_claim_rate", "label": "Unsupported strict", "format": "percent"},
                {"field": "unsupported_observed_mean", "label": "Unsupported observed", "format": "percent"},
                {"field": "response_missing_n", "label": "声明 N/A", "format": "number"},
                {"field": "context_missing_n", "label": "上下文 N/A", "format": "number"},
                {"field": "unsupported_missing_n", "label": "Unsupported N/A", "format": "number"},
            ],
        }
    )

    block_bodies = {
        "eval_overview": "## MOI 严格复跑已补齐，MaxKB 离线恢复值纳入主检索对比\n\nMOI、Dify、FastGPT 都在 v0.3 的 297 文档上运行原生检索与生成；MaxKB 使用 admin 诊断检索、离线恢复排序和外部生成。四平台 Retrieval 数值均进入主图主表，但 MaxKB 始终标为带条件结果，不进入严格原生平台排名。**本报告展示分维度结果，不发布会掩盖契约差异的总分。**",
        "eval_retrieval_finding": (
            "### MOI Recall@1 最高；MaxKB 离线恢复后高 K 与 MRR 最高（带条件）\n\n"
            f"在 265 条带 Gold 文档的问题上，MaxKB/FastGPT/Dify/MOI Recall@10 分别为 **{value(maxkb, 'retrieval', 'recall_at_10'):.3f}**/"
            f"**{value(fastgpt, 'retrieval', 'recall_at_10'):.3f}**/**{value(dify, 'retrieval', 'recall_at_10'):.3f}**/**{value(moi, 'retrieval', 'recall_at_10'):.3f}**；"
            f"Recall@1 则为 MOI **{value(moi, 'retrieval', 'recall_at_1'):.3f}**、MaxKB {value(maxkb, 'retrieval', 'recall_at_1'):.3f}、FastGPT {value(fastgpt, 'retrieval', 'recall_at_1'):.3f}、Dify {value(dify, 'retrieval', 'recall_at_1'):.3f}。"
            f"MRR 为 MaxKB **{value(maxkb, 'retrieval', 'mrr'):.3f}**、FastGPT {value(fastgpt, 'retrieval', 'mrr'):.3f}、MOI {value(moi, 'retrieval', 'mrr'):.3f}、Dify {value(dify, 'retrieval', 'mrr'):.3f}。"
            "MOI 的首条命中最强，但 Top-10 对多 Gold 问题的新增覆盖有限；MaxKB 的高 K/MRR 数值优势来自 vendor score 离线恢复排序，必须与原生 API 排名区分。"
            "**旧记录中的 0.891 是 Any-Gold Hit@10，不是分数型 Recall@k。**"
        ),
        "eval_latency_finding": (
            "### MOI 检索仍主导端到端时延，但严格复跑较历史结果改善\n\n"
            f"Dify/FastGPT/MOI retrieval p50 为 **{dify['latency_ms']['retrieval']['p50'] / 1000:.3f}**/"
            f"**{fastgpt['latency_ms']['retrieval']['p50'] / 1000:.3f}**/**{moi['latency_ms']['retrieval']['p50'] / 1000:.3f}** 秒；"
            f"MOI retrieval p95 为 {moi['latency_ms']['retrieval']['p95'] / 1000:.3f} 秒，QA p50 为 {moi['latency_ms']['qa']['p50'] / 1000:.3f} 秒，"
            f"E2E p50 为 {moi['latency_ms']['e2e']['p50'] / 1000:.3f} 秒。"
            "**优化优先级仍应放在 MatrixFlow 混合检索、重复 chunk 排序和 full-text 长尾；跨平台时延只能方向性比较，因为计时边界与调用路径不同。**"
        ),
        "eval_qa_finding": (
            "### MOI 文本答案指标高于 Dify 与 FastGPT\n\n"
            f"Token F1：MaxKB **{value(maxkb, 'qa', 'token_f1'):.3f}**、MOI **{value(moi, 'qa', 'token_f1'):.3f}**、"
            f"Dify {value(dify, 'qa', 'token_f1'):.3f}、FastGPT {value(fastgpt, 'qa', 'token_f1'):.3f}；"
            f"contains-gold：MaxKB {value(maxkb, 'qa', 'contains_gold'):.1%}、MOI {value(moi, 'qa', 'contains_gold'):.1%}、"
            f"Dify {value(dify, 'qa', 'contains_gold'):.1%}、FastGPT {value(fastgpt, 'qa', 'contains_gold'):.1%}。"
            "MaxKB 使用外部 DeepSeek 生成，不能解释为原生 app 表现。所有平台 normalized EM 都很低，开放答案应以 Token F1、Judge 和切片指标共同解释。"
        ),
        "eval_judge_finding": (
            "### MOI 的指令遵循与声明正确性突出，但上下文忠实度缺 1 条\n\n"
            f"MOI instruction compliance 为 **{moi['judge']['dimensions']['instruction_compliance']['value']:.3f}**，高于 MaxKB {maxkb['judge']['dimensions']['instruction_compliance']['value']:.3f}、"
            f"Dify {dify['judge']['dimensions']['instruction_compliance']['value']:.3f}、FastGPT {fastgpt['judge']['dimensions']['instruction_compliance']['value']:.3f}；"
            f"MOI response-claim correctness 为 **{moi['judge']['dimensions']['response_claim_correctness']['value']:.3f}** 且 275 条完整。"
            f"MaxKB 的 relevance/contradiction-free 最高（{maxkb['judge']['dimensions']['answer_relevance']['value']:.3f}/{maxkb['judge']['dimensions']['contradiction_free']['value']:.3f}）。"
            "MOI runtime-context faithfulness 缺 1 条，因此严格总体值保持 N/A。"
        ),
        "eval_unsupported_finding": (
            "### 完整分母下 FastGPT 的无支持声明风险最低\n\n"
            f"Unsupported-claim rate 越低越好：FastGPT **{fastgpt['judge']['dimensions']['unsupported_claim_rate']['value']:.1%}**、"
            f"Dify {dify['judge']['dimensions']['unsupported_claim_rate']['value']:.1%}、MOI {moi['judge']['dimensions']['unsupported_claim_rate']['value']:.1%}、"
            f"MaxKB {maxkb['judge']['dimensions']['unsupported_claim_rate']['value']:.1%}。四平台该维度均完整覆盖 275 条。"
            "**平台选择必须同时权衡检索覆盖、声明支持性、拒答和链路契约。**"
        ),
        "eval_exact_results_heading": "### 精确结果、统一分母与契约\n\n下表保留四平台 Recall@1/3/5/10、MRR、QA、时延、Judge 和契约。MaxKB 主行采用 `comprehensive_score` 降序离线恢复值；其严格 public retrieval 仍为 N/A，原始返回顺序保留在后续审计表。",
        "eval_comparability_note": "### 可比性边界\n\nMOI/Dify/FastGPT 都使用 v0.3 的 297 文档、`bge-m3`、`deepseek-v4-flash` 与相同 Judge 协议，可做严格同语料原生质量对照。MOI 时延来自 MatrixFlow 原生阶段结果，Dify/FastGPT 来自各自 runner/API 边界，时延仅作方向性比较。MaxKB 标为 `offline_restored_diagnostic_retrieval_external_generation`：主图主表使用 vendor score 离线恢复排序，API 原始顺序保留为敏感性对照，public retrieval 仍为 N/A。这些边界阻止无条件总排名。",
    }
    for block_id, body in block_bodies.items():
        block = find(manifest["blocks"], block_id)
        block.update({"body": body, "sourceId": "benchmark_v2"})
    for block in manifest["blocks"]:
        if block.get("id", "").startswith("eval_") and block.get("sourceId") == "serial_eval":
            block["sourceId"] = "benchmark_v2"

    limitations = find(manifest["blocks"], "limitations")
    limitations["body"] = (
        "## 12. 限制与不确定性\n\n"
        "- 本版是从 v0.2 定向筛选的 27.5% 子集，不是原 1,000 QA 的无偏样本。\n"
        "- MaxKB 使用 admin 诊断检索与外部 DeepSeek 生成，不等同于 public retrieval 或原生 MaxKB app 路径；主表 score 排名是复用历史评估规则的离线恢复值，不是 API 原始返回序列。\n"
        "- Judge 使用适配型参考答案 rubric；缺少 structured claims/evidence sets，canonical claim correctness、grounding、TDAS 等指标仍为 N/A。\n"
        "- MOI 的 runtime-context faithfulness 有 1 条 N/A；MaxKB 的 response correctness/context faithfulness 有 8/3 条 N/A。\n"
        "- Dify 的统一指标回放保留 1 条首次 QA 失败并按冻结分母计零，最终串行状态已恢复为 275/275。\n"
        "- 平台时延边界不完全一致，且 cold/warm 状态未冻结；MOI 的部署 image digest/version 仍记录为 UNKNOWN。\n"
        "- 核心指标尚未提供 bootstrap 置信区间或显著性检验，小差异不能解释为稳定排名。\n"
        "- 未纳入 MMDocIR、多模态或页面/布局任务；本报告不代表视觉 RAG 能力。\n"
        "- 53 条答案格式敏感题需要显式 normalization；Normalized EM 不应单独作为主结论。\n"
        "- 数据来源的公开再分发仍需独立许可证与完整性确认。"
    )
    find(manifest["blocks"], "next_steps")["body"] = (
        "## 15. 推荐下一步\n\n"
        "1. 修正 Dify 首次失败/恢复记录的最新终态选择，再冻结统一 ledger。\n"
        "2. 优先治理 MOI Top-10 同源重复 chunk 与 full-text 长尾时延。\n"
        "3. 补齐 structured claims/evidence sets，并复核 MOI 唯一一条 context-faithfulness N/A。\n"
        "4. 为核心指标补 fixed-seed bootstrap 95% 置信区间和平台差值区间。\n"
        "5. 固定平台版本、镜像 digest、cold/warm 条件与统一外层 wall-clock 时延边界。\n"
        "6. 修复或验证 MaxKB paragraph hydration 的顺序保持，在同一 Gold 上对照原生返回序列与离线恢复序列。"
    )
    find(manifest["blocks"], "further_questions")["body"] = (
        "## 16. 后续研究问题\n\n"
        f"- 为什么 MOI Recall@1 达 {value(moi, 'retrieval', 'recall_at_1'):.3f}，但 Recall@10 仅 {value(moi, 'retrieval', 'recall_at_10'):.3f}？\n"
        "- 去除重复 chunk 与优化 full-text 后，MOI retrieval p95 能下降多少？\n"
        "- MaxKB 修复返回顺序后，原生序列与当前离线恢复序列是否逐条一致？\n"
        "- Quality-first 275 子集与完整 1,000 QA 的平台排序是否稳定？\n"
        "- 纯文本结论在独立多模态 benchmark 上是否仍成立？"
    )

    summary_relative = str(summary_path.relative_to(ROOT))
    manifest_source = {
        "id": "benchmark_v2",
        "label": "MOI/Dify/FastGPT/MaxKB Benchmark 2.0 unified summary",
        "path": summary_relative,
    }
    manifest["sources"] = [source for source in manifest["sources"] if source.get("id") != "benchmark_v2"] + [manifest_source]
    source = {
        "id": "benchmark_v2",
        "path": summary_relative,
        "query": {
            "engine": "duckdb/local-filesystem",
            "language": "sql",
            "sql": f"SELECT * FROM read_json_auto('{summary_relative}');",
            "generator": "python3 tools/build_moi_rag_benchmark_report_v2.py",
            "description": "The Python generator replays all four v0.3 runs, validates the strict 297-document MOI index, and writes this unified summary; the SQL query reads the frozen summary used by report widgets. No new model calls are made.",
            "executed_at": generated_at,
            "tables_used": [
                "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/questions.jsonl",
                "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval/documents/*.md",
                "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun/terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun/judge/judge-terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun/moi/cli-output-global/20260826-115956.073/results.jsonl",
                "runs/bench-v0.3-final/20260826-moi-v03-final-strict-rerun/moi/cli-output-global/20260826-115956.073/ingest-state.json",
                "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool/terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260825-dify-v03-final-native-bool/judge/judge-terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k/terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260825-fastgpt-v03-final-dsv4f-chunk8k/judge/judge-terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f/terminal-ledger.jsonl",
                "runs/bench-v0.3-final/20260825-maxkb-v03-final-dsv4f/judge/judge-terminal-ledger.jsonl",
                "local-rag-platforms/scripts/evaluation/mmdocir_document_local_eval.py",
                "results/reports/MOI_rag_benchmark_v1.0.md",
            ],
            "filters": {"question_ids": "v0.3-final frozen 275", "retrieval_gold_denominator": 265},
            "metric_definitions": {
                "Recall@k": "Macro fraction of frozen Gold documents matched in the first k ordered hits over 265 eligible questions.",
                "MRR": "Mean reciprocal rank of the first matched Gold document over the same 265-question denominator.",
                "MaxKB offline-restored Recall@k": "Macro Gold-document recall over 265 eligible questions after restoring rank by comprehensive_score descending, matching the historical evaluator workaround for MaxKB paragraph hydration losing order; public retrieval remains N/A.",
                "MaxKB diagnostic Hit@k": "Fraction of the same 265 questions with at least one Gold document in the first k diagnostic hits.",
                "Token F1": "Deterministic token-overlap F1 between generated and reference answers over 275 questions.",
                "Unsupported-claim rate": "Strict Judge aggregate over all 275 planned units; any missing dimension observation yields N/A.",
                "Latency": "Nearest-rank p50/p95/p99 from each platform's observed native-stage timings; cross-platform boundary differences are retained as a limitation.",
            },
        },
    }
    base["sources"] = [item for item in base["sources"] if item.get("id") != "benchmark_v2"] + [source]
    used_source_ids = {
        item["sourceId"]
        for collection in (manifest["blocks"], manifest["cards"], manifest["charts"], manifest["tables"])
        for item in collection
        if item.get("sourceId")
    }
    manifest["sources"] = [item for item in manifest["sources"] if item.get("id") in used_source_ids]
    base["sources"] = [item for item in base["sources"] if item.get("id") in used_source_ids]
    base["package_info"] = {
        "originUrl": "artifact://moi-rag-benchmark-report-v2.0",
        "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False},
    }

    assert manifest["blocks"][0]["body"].startswith(f"# {title}")
    assert manifest["blocks"][1]["body"].startswith("## Executive Summary")
    assert len(base["snapshot"]["datasets"]["eval_results"]) == 4
    assert len(base["snapshot"]["datasets"]["eval_judge_results"]) == 4
    assert len(base["snapshot"]["datasets"]["eval_retrieval_quality"]) == 12
    assert len(base["snapshot"]["datasets"]["eval_maxkb_retrieval_diagnostic"]) == 2
    assert summary["platforms"]["moi"]["corpus_documents"] == 297
    assert len(summary["comparability"]["strict_same_corpus_native_platforms"]) == 3
    assert value(maxkb, "retrieval", "mrr") == round(maxkb_sorted["mrr"], 6)
    assert maxkb["retrieval_strict"]["mrr"]["value"] is None
    serialized = json.dumps(base, ensure_ascii=False)
    assert "historical_500_document_index_subset" not in serialized and "MOI (500-doc)" not in serialized
    assert "/Users/" not in serialized and "DEEPSEEK_API_KEY" not in serialized
    return base


def build_chart_map(base: dict[str, Any]) -> dict[str, Any]:
    updates = {
        "eval_retrieval_quality_chart": ("四平台 2.0", "统一检索 Recall@1/10 与 MRR 如何？", "MOI Recall@1 最高；MaxKB 离线恢复后的 Recall@10/MRR 数值最高，但属于带条件结果。"),
        "eval_latency_chart": ("四平台 2.0", "Retrieval、QA 与 E2E 的 p50/p95/p99 相差多少？", "MOI 的检索长尾仍主导 E2E；跨平台计时边界仅支持方向性比较。"),
        "eval_qa_quality_chart": ("四平台 2.0", "答案文本匹配如何？", "MaxKB 最高但链路带条件；MOI 高于 Dify 与 FastGPT。"),
        "eval_judge_quality_chart": ("四平台 2.0", "完整 Judge 维度表现如何？", "MOI 的指令遵循最高，MaxKB 的相关性与无矛盾最高。"),
        "eval_unsupported_claims_chart": ("四平台 2.0", "严格完整分母下谁更少产生无支持声明？", "FastGPT 最低，Dify 与 MOI 接近，MaxKB 最高。"),
    }
    for chart in base.get("charts") or []:
        if chart.get("chart_id") not in updates:
            continue
        segment, question, takeaway = updates[chart["chart_id"]]
        chart.update({"segment": segment, "question": question, "takeaway": takeaway, "source_id": "benchmark_v2"})
        if chart["chart_id"] == "eval_retrieval_quality_chart":
            chart["fields"] = ["metric", "platform", "value", "comparison_tier", "corpus_documents", "retrieval_metric_method"]
        if chart["chart_id"] == "eval_latency_chart":
            chart["fields"] = ["stage", "platform", "p50_seconds", "p95_seconds", "p99_seconds", "comparison_tier"]
    base["repeated_family_note"] = "All five evaluation visuals remain bar-family snapshot comparisons; no temporal trend is implied. Four categories stay within the relaxed multi-category palette cap."
    base["table_only_notes"] = [
        "MaxKB main retrieval uses the offline-restored score order; the two-row audit table preserves API-order sensitivity and contract caveats."
    ]
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--chart-map", type=Path, default=DEFAULT_CHART_MAP)
    args = parser.parse_args()

    serial = read_json(SERIAL_REPORT)
    if serial.get("status") != "COMPLETE" or serial.get("judge_protocol", {}).get("same_parameters_verified") is not True:
        raise ValueError("The three-platform serial report is not a complete common-Judge source")
    summary = build_summary(serial)
    artifact = build_artifact(read_json(BASE_ARTIFACT), summary, args.summary)
    chart_map = build_chart_map(read_json(BASE_CHART_MAP))

    for path, payload in ((args.summary, summary), (args.artifact, artifact), (args.chart_map, chart_map)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.summary)
    print(args.artifact)
    print(args.chart_map)


if __name__ == "__main__":
    main()
