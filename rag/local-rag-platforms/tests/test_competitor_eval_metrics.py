from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parents[1] / "scripts/evaluation"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import competitor_eval_metrics as metrics
import competitor_eval_metric_registry as registry
import competitor_eval_judge as judge


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _make_package(tmp_path: Path, dataset_id: str = "multihop-rag", questions: list[dict] | None = None) -> Path:
    package = tmp_path / f"package-{dataset_id}"
    questions = questions or [
        {
            "question_id": "q1",
            "question": "What is alpha?",
            "reference_answer": "Alpha answer",
            "answerable": True,
            "gold_doc_ids": ["d1", "d2"],
            "gold_evidence": [
                {"evidence_id": "e1", "doc_id": "d1", "modality": "text"},
                {"evidence_id": "e2", "doc_id": "d2", "modality": "text"},
            ],
            "question_type": "inference",
            "metadata": {"category": "news"},
        },
        {
            "question_id": "q2",
            "question": "What is beta?",
            "reference_answer": "Beta answer",
            "answerable": True,
            "gold_doc_ids": ["d2", "d3"],
            "gold_evidence": [
                {"evidence_id": "e3", "doc_id": "d2", "modality": "text"},
                {"evidence_id": "e4", "doc_id": "d3", "modality": "text"},
            ],
            "question_type": "comparison",
            "metadata": {"category": "news"},
        },
        {
            "question_id": "q3",
            "question": "What is gamma?",
            "reference_answer": "Gamma answer",
            "answerable": True,
            "gold_doc_ids": ["d4"],
            "gold_evidence": [{"evidence_id": "e5", "doc_id": "d4", "modality": "text"}],
            "question_type": "temporal",
            "metadata": {"category": "news"},
        },
        {
            "question_id": "q4",
            "question": "What is delta?",
            "reference_answer": "Delta answer",
            "answerable": True,
            "gold_doc_ids": ["d5"],
            "gold_evidence": [{"evidence_id": "e6", "doc_id": "d5", "modality": "text"}],
            "question_type": "null",
            "metadata": {"category": "news"},
        },
    ]
    _write_jsonl(package / "questions.jsonl", questions)
    _write_jsonl(
        package / "gold.jsonl",
        [
            {
                "question_id": row["question_id"],
                "reference_answer": row["reference_answer"],
                "gold_doc_ids": row["gold_doc_ids"],
                "gold_evidence": row["gold_evidence"],
            }
            for row in questions
        ],
    )
    _write_json(
        package / "manifest.json",
        {
            "schema_version": "competitor-eval-ready-v1",
            "dataset_id": dataset_id,
            "protocol_tag": "ADAPTED_PROTOCOL",
            "status": "READY_ADAPTED",
            "counts": {"questions": len(questions), "documents": 5},
            "questions": "questions.jsonl",
            "gold": "gold.jsonl",
            "conditions": {"denominator_policy": "current_local_frozen"},
        },
    )
    return package


def _make_run(tmp_path: Path, dataset_id: str = "multihop-rag") -> tuple[Path, Path]:
    package = _make_package(tmp_path, dataset_id)
    run = tmp_path / "run"
    _write_json(
        run / "start-record.json",
        {
            "schema": "competitor-eval-run-start-v1",
            "run_id": "synthetic-run",
            "dataset": dataset_id,
            "dataset_revision": "local-freeze-1",
            "condition": "native",
            "planned": {"questions": 4, "repeats": 1, "initial_attempts": 4},
            "denominator_contract": {"unit": "one question x one initial repeat"},
        },
    )
    _write_jsonl(
        run / "initial-ledger.jsonl",
        [
            {"question_id": f"q{i}", "repeat_id": 1, "planned_denominator": True, "status": "not_started"}
            for i in range(1, 5)
        ],
    )
    _write_jsonl(
        run / "terminal-ledger.jsonl",
        [
            {
                "stage": "retrieval",
                "question_id": "q1",
                "repeat_id": 1,
                "status": "SUCCESS",
                "hits": [{"document_id": "d1"}, {"document_id": "d2"}],
                "latency_ms": 10,
            },
            {
                "stage": "retrieval",
                "question_id": "q2",
                "repeat_id": 1,
                "status": "SUCCESS",
                "hits": [{"document_id": "d2"}, {"document_id": "d3"}],
                "latency_ms": 20,
            },
            {
                "stage": "retrieval",
                "question_id": "q3",
                "repeat_id": 1,
                "status": "EMPTY",
                "hits": [],
                "latency_ms": 30,
            },
            {
                "stage": "retrieval",
                "question_id": "q4",
                "repeat_id": 1,
                "status": "FAILED",
                "error": "timeout",
            },
            {
                "stage": "qa",
                "question_id": "q1",
                "repeat_id": 1,
                "status": "SUCCESS",
                "answer": "Alpha answer",
                "latency_ms": 11,
            },
            {
                "stage": "qa",
                "question_id": "q2",
                "repeat_id": 1,
                "status": "SUCCESS",
                "answer": "Beta answer extra",
                "latency_ms": 21,
            },
            {
                "stage": "qa",
                "question_id": "q3",
                "repeat_id": 1,
                "status": "EMPTY",
                "answer": "",
                "latency_ms": 31,
            },
            {
                "stage": "qa",
                "question_id": "q4",
                "repeat_id": 1,
                "status": "FAILED",
                "error": "provider unavailable",
            },
        ],
    )
    return run, package


def _make_mixed_fixture(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "mixed-package"
    questions = [
        {
            "question_id": "mh-1",
            "source_dataset": "multihop",
            "question": "Which two facts answer alpha?",
            "question_type": "inference",
            "answerability": "answerable",
            "answerable": True,
            "reference_answer": "Alpha beta",
            "gold_doc_ids": ["mh-doc-1", "mh-doc-2"],
            "gold_evidence": [{"doc_id": "mh-doc-1"}, {"doc_id": "mh-doc-2"}],
        },
        {
            "question_id": "mh-null",
            "source_dataset": "multihop",
            "question": "What is not in the corpus?",
            "question_type": "null",
            "answerability": "unanswerable",
            "answerable": False,
            "reference_answer": "",
            "gold_doc_ids": [],
            "gold_evidence": [],
        },
        {
            "question_id": "ent-info",
            "source_dataset": "enterprise",
            "question": "Which policy is absent?",
            "question_type": "info_not_found",
            "answerability": "info_not_found",
            "info_not_found": True,
            "answerable": True,
            "reference_answer": "",
            "gold_doc_ids": [],
            "gold_evidence": [],
        },
        {
            "question_id": "doc-1",
            "source_dataset": "docbench",
            "question": "What does the document say?",
            "question_type": "text-only",
            "answerability": "answerable",
            "answerable": True,
            "reference_answer": "Doc answer",
            "gold_doc_ids": ["docbench-1"],
            "gold_evidence": [{"doc_id": "docbench-1"}],
        },
        {
            "question_id": "doc-empty-reference",
            "source_dataset": "docbench",
            "question": "Which evidence is present?",
            "question_type": "text-only",
            "answerability": "answerable",
            "answerable": True,
            "reference_answer": "",
            "gold_doc_ids": ["docbench-2"],
            "gold_evidence": [{"doc_id": "docbench-2"}],
        },
        {
            "question_id": "mm-1",
            "source_dataset": "mmdocir",
            "question": "Which page contains the answer?",
            "question_type": "retrieval",
            "answerability": "answerable",
            "answerable": True,
            "reference_answer": "MM answer",
            "gold_doc_ids": ["mm-doc-1"],
            "gold_evidence": [{"doc_id": "mm-doc-1", "locator": {"file_id": "mm-file", "page": 2}}],
        },
    ]
    _write_jsonl(package / "questions.jsonl", questions)
    _write_jsonl(package / "gold.jsonl", questions)
    _write_jsonl(
        package / "corpus.jsonl",
        [
            {"document_id": "mh-doc-1"},
            {"document_id": "mh-doc-2"},
            {"document_id": "docbench-1"},
            {"document_id": "docbench-2"},
            {"document_id": "mm-doc-1"},
        ],
    )
    _write_json(
        package / "manifest.json",
        {
            "schema_version": "competitor-eval-ready-v1",
            "dataset_id": "moi-rag-bench-v0.1-mixed",
            "protocol_tag": "MIXED_BENCHMARK_ADAPTED_V1",
            "status": "READY_ADAPTED",
            "counts": {"questions": len(questions), "documents": 5},
            "questions": "questions.jsonl",
            "gold": "gold.jsonl",
            "corpus": "corpus.jsonl",
            "source_datasets": ["multihop", "enterprise", "docbench", "mmdocir"],
        },
    )

    run = tmp_path / "mixed-run"
    _write_json(
        run / "start-record.json",
        {
            "schema": "competitor-eval-run-start-v1",
            "run_id": "mixed-run",
            "system_id": "dify_local",
            "dataset": "moi-rag-bench-v0.1-mixed",
            "planned": {"questions": len(questions), "repeats": 1, "initial_attempts": len(questions)},
        },
    )
    _write_jsonl(
        run / "initial-ledger.jsonl",
        [
            {
                "question_id": row["question_id"],
                "source_dataset": row["source_dataset"],
                "question_type": row["question_type"],
                "answerable": row["answerable"],
                "repeat_id": 1,
                "planned_denominator": True,
            }
            for row in questions
        ],
    )
    _write_jsonl(
        run / "terminal-ledger.jsonl",
        [
            {
                "stage": "retrieval",
                "question_id": "mh-1",
                "source_dataset": "multihop",
                "status": "SUCCESS",
                "hits": [
                    {"document_id": "mh-doc-1", "rank": 1},
                    {"document_id": "mh-extra", "rank": 2},
                    {"document_id": "mh-doc-2", "rank": 3},
                ],
                "latency_ms": 10,
            },
            {
                "stage": "retrieval",
                "question_id": "mh-null",
                "source_dataset": "multihop",
                "status": "SUCCESS",
                "hits": [],
                "latency_ms": 20,
            },
            {
                "stage": "retrieval",
                "question_id": "ent-info",
                "source_dataset": "enterprise",
                "status": "SUCCESS",
                "hits": [{"document_id": "ent-extra"}],
                "latency_ms": 30,
            },
            {
                "stage": "retrieval",
                "question_id": "doc-1",
                "source_dataset": "docbench",
                "status": "FAILED",
                "error": "provider timeout",
            },
            {
                "stage": "retrieval",
                "question_id": "doc-empty-reference",
                "source_dataset": "docbench",
                "status": "SUCCESS",
                "hits": [{"document_id": "docbench-2"}],
                "latency_ms": 40,
            },
            {
                "stage": "retrieval",
                "question_id": "mm-1",
                "source_dataset": "mmdocir",
                "status": "SUCCESS",
                "hits": [{"document_id": "mm-doc-1", "file_id": "mm-file", "page": 2}],
                "latency_ms": 50,
            },
            {
                "stage": "qa",
                "question_id": "mh-1",
                "source_dataset": "multihop",
                "status": "SUCCESS",
                "answer": "Alpha beta",
                "latency_ms": 11,
            },
            {
                "stage": "qa",
                "question_id": "mh-null",
                "source_dataset": "multihop",
                "status": "SUCCESS",
                "answer": "I cannot answer because the evidence is insufficient.",
                "latency_ms": 21,
            },
            {
                "stage": "qa",
                "question_id": "ent-info",
                "source_dataset": "enterprise",
                "status": "SUCCESS",
                "answer": "I cannot find this information in the indexed sources.",
                "latency_ms": 31,
            },
            {
                "stage": "qa",
                "question_id": "doc-1",
                "source_dataset": "docbench",
                "status": "FAILED",
                "error": "provider timeout",
            },
            {
                "stage": "qa",
                "question_id": "doc-empty-reference",
                "source_dataset": "docbench",
                "status": "SUCCESS",
                "answer": "Some answer",
                "latency_ms": 41,
            },
            {
                "stage": "qa",
                "question_id": "mm-1",
                "source_dataset": "mmdocir",
                "status": "SUCCESS",
                "answer": "MM answer extra",
                "latency_ms": 51,
            },
        ],
    )
    _write_json(
        run / "resource-map.json",
        {
            "resources": {
                "__global__": {
                    "documents": {
                        "mh-doc-1": {"status": "ready"},
                        "mh-doc-2": {"status": "ready"},
                        "docbench-1": {"status": "ready"},
                        "docbench-2": {"status": "ready"},
                        "mm-doc-1": {"status": "failed"},
                    }
                }
            }
        },
    )
    judge_rows = []
    for question_id, source_dataset, claim, grounding, tdas in (
        ("mh-1", "multihop", 1.0, 1.0, 1.0),
        ("mh-null", "multihop", 0.0, 0.0, 0.0),
        ("ent-info", "enterprise", 1.0, 1.0, 1.0),
        ("doc-empty-reference", "docbench", 0.5, 0.5, 0.0),
        ("mm-1", "mmdocir", 1.0, 1.0, 1.0),
    ):
        dimensions = {
            "claim_correctness": {"score": claim, "supported": True},
            "grounding": {"score": grounding, "supported": True},
            "tdas": {"score": tdas, "supported": True},
        }
        if question_id == "mh-null":
            dimensions["strict_unanswerable"] = {"score": 1.0, "supported": True}
        if question_id == "ent-info":
            dimensions["info_not_found_success"] = {"score": 1.0, "supported": True}
        judge_rows.append(
            {
                "question_id": question_id,
                "source_dataset": source_dataset,
                "status": "SUCCESS",
                "judgement": {"dimensions": dimensions},
            }
        )
    judge_rows.append(
        {
            "question_id": "doc-1",
            "source_dataset": "docbench",
            "status": "FAILED",
            "error": "judge timeout",
        }
    )
    _write_jsonl(run / "judge-terminal-ledger.jsonl", judge_rows)
    return run, package


def _assert_metric_record(record: dict) -> None:
    assert {
        "metric_id",
        "value",
        "numerator",
        "denominator",
        "aggregation",
        "higher_is_better",
        "applicable",
        "reason_code",
        "unit",
        "diagnostics",
        "eligible_n",
        "missing_n",
        "failed_n",
        "na_reason",
    } <= set(record)
    assert isinstance(record["diagnostics"], dict)


def test_mixed_aggregate_cli_routes_four_sources_and_keeps_native_unified_separate(tmp_path):
    run, package = _make_mixed_fixture(tmp_path)
    output = tmp_path / "mixed-metrics.json"

    assert metrics.main(["aggregate", "--run", str(run), "--package", str(package), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))

    assert set(result["dataset_native"]["by_source_dataset"]) == {
        "multihop-rag",
        "enterprise-rag-bench",
        "docbench",
        "mmdocir",
    }
    assert "moi_unified" in result
    assert "overall_winner" not in result
    assert "winner" not in result

    unified = result["moi_unified"]
    for name in ("initial_availability", "request_success", "repeat_consistency", "error_rate", "recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10", "precision_at_1", "precision_at_3", "precision_at_5", "precision_at_10", "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10", "mrr", "map_at_10", "ndcg_at_1", "ndcg_at_3", "ndcg_at_5", "ndcg_at_10", "complete_evidence_set_recall_at_10", "invalid_extra_rate_at_10", "citation_locator_validity", "citation_entailment_precision", "answer_claim_citation_coverage", "fabricated_citation_count", "out_of_scope_citation_count", "critical_contradiction_rate", "em", "f1", "contains_gold", "strict_unanswerable_success", "false_refusal_rate", "info_not_found_success_adapted"):
        _assert_metric_record(unified["metrics"][name])
    assert unified["metrics"]["initial_availability"]["numerator"] == 5
    assert unified["metrics"]["initial_availability"]["denominator"] == 6
    assert unified["metrics"]["initial_availability"]["failed_n"] == 1
    assert unified["metrics"]["strict_unanswerable_success"]["denominator"] == 1
    assert unified["metrics"]["info_not_found_success_adapted"]["denominator"] == 1
    assert unified["metrics"]["false_refusal_rate"]["denominator"] == 4

    for stage in ("retrieval", "qa", "e2e"):
        for percentile in ("p50", "p95", "p99"):
            _assert_metric_record(unified["latency_ms"][stage][percentile])
    assert unified["latency_ms"]["retrieval"]["p99"]["eligible_n"] == 5
    assert unified["latency_ms"]["qa"]["p99"]["failed_n"] == 1

    assert unified["metrics"]["claim_correctness"]["value"] is None
    assert unified["metrics"]["claim_correctness"]["na_reason"] == "N/A:MISSING_STRUCTURED_CLAIM_GOLD"
    assert unified["metrics"]["tdas"]["value"] is None
    assert unified["metrics"]["tdas"]["na_reason"] == "N/A:MISSING_STRUCTURED_CLAIM_GOLD"
    assert unified["judge"]["claim_correctness"]["value"] is None
    assert unified["judge"]["claim_correctness"]["na_reason"] == "N/A:MISSING_STRUCTURED_CLAIM_GOLD"
    assert unified["judge"]["grounding"]["value"] is None
    assert unified["judge"]["tdas"]["value"] is None

    assert unified["slices"]["source_dataset"]["multihop-rag"]["denominator"]["planned_n"] == 2
    assert unified["slices"]["question_type"]["inference"]["denominator"]["planned_n"] == 1
    assert unified["slices"]["answerability"]["unanswerable"]["denominator"]["planned_n"] == 1
    assert unified["slices"]["gold_doc_count"]["0"]["denominator"]["planned_n"] == 2
    assert "document_length" in unified["slices"]
    assert "failure_class" in unified["slices"]

    docbench = result["dataset_native"]["by_source_dataset"]["docbench"]
    assert docbench["qa"]["metrics"]["normalized_em"]["eligible_n"] == 1
    assert docbench["qa"]["metrics"]["token_f1"]["eligible_n"] == 1
    assert docbench["qa"]["metrics"]["contains_gold"]["eligible_n"] == 1
    assert docbench["retrieval"]["metrics"]["invalid_extra_rate_at_10"]["value"] is None
    assert docbench["retrieval"]["metrics"]["invalid_extra_rate_at_10"]["na_reason"] == "N/A:NO_INVALID_EXTRA_GOLD_CONTRACT"


def test_mixed_aggregate_preserves_maxkb_public_retrieval_as_na(tmp_path):
    run, package = _make_run(tmp_path, "wikieval")
    start = json.loads((run / "start-record.json").read_text(encoding="utf-8"))
    start["system_id"] = "maxkb_local"
    _write_json(run / "start-record.json", start)
    output = tmp_path / "maxkb-metrics.json"

    assert metrics.main(["aggregate", "--run", str(run), "--package", str(package), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))

    retrieval = result["moi_unified"]["metrics"]
    assert retrieval["recall_at_10"]["value"] is None
    assert retrieval["recall_at_10"]["na_reason"] == "N/A:UNSUPPORTED_API"


def test_aggregate_uses_frozen_planned_denominator_and_deterministic_metrics(tmp_path):
    run, package = _make_run(tmp_path)

    result = metrics.aggregate_run(run, package)

    retrieval = result["retrieval"]
    assert retrieval["denominator"]["planned_n"] == 4
    assert retrieval["denominator"]["valid_n"] == 3
    assert retrieval["denominator"]["failed_n"] == 1
    assert retrieval["denominator"]["pending_n"] == 0
    assert retrieval["metrics"]["recall_at_1"]["value"] == 0.25
    assert retrieval["metrics"]["recall_at_3"]["value"] == 0.5
    assert retrieval["metrics"]["mrr"]["value"] == 0.5
    assert retrieval["metrics"]["all_evidence_success_at_3"]["value"] == 0.5
    assert retrieval["metrics"]["latency_ms_p50"]["value"] == 20.0
    assert retrieval["metrics"]["latency_ms_p95"]["value"] == 30.0

    qa = result["qa"]
    assert qa["denominator"] == {"planned_n": 4, "terminal_n": 4, "valid_n": 3, "failed_n": 1, "unsupported_n": 0, "pending_n": 0}
    assert qa["metrics"]["answer_non_empty"]["value"] == 0.5
    assert qa["metrics"]["contains_gold"]["value"] == 0.5
    assert qa["metrics"]["normalized_em"]["value"] == 0.25
    assert qa["metrics"]["token_f1"]["value"] == 0.45
    assert set(qa["slices"]) == {"inference", "comparison", "temporal", "null"}
    assert qa["slices"]["comparison"]["metrics"]["contains_gold"]["value"] == 1


def test_readiness_uses_compact_resource_summary_when_documents_are_omitted(tmp_path):
    run, package = _make_run(tmp_path)
    _write_jsonl(
        package / "corpus.jsonl",
        [{"document_id": f"d{i}"} for i in range(1, 6)],
    )
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest["corpus"] = "corpus.jsonl"
    _write_json(package / "manifest.json", manifest)
    _write_json(
        run / "resource-map.json",
        {
            "resources": {
                "__global__": {
                    "status": "ready",
                    "ready": True,
                    "document_count": 5,
                    "ready_document_count": 5,
                    "unsupported_document_count": 0,
                    "documents": {},
                }
            }
        },
    )

    result = metrics.aggregate_run(run, package)

    assert result["status"] == "COMPLETE"
    assert result["readiness"] == {
        "status": "READY",
        "planned_n": 5,
        "observed_n": 5,
        "ready_n": 5,
        "ready_rate": {"value": 1, "numerator": 5, "denominator": 5, "unit": "rate"},
        "status_counts": {"ready": 5},
    }


def test_readiness_does_not_count_planned_compact_documents_as_observed(tmp_path):
    run, package = _make_run(tmp_path)
    _write_jsonl(
        package / "corpus.jsonl",
        [{"document_id": f"d{i}"} for i in range(1, 6)],
    )
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest["corpus"] = "corpus.jsonl"
    _write_json(package / "manifest.json", manifest)
    _write_json(
        run / "resource-map.json",
        {
            "resources": {
                "__global__": {
                    "status": "not_started",
                    "ready": False,
                    "document_count": 5,
                    "documents": {},
                }
            }
        },
    )

    readiness = metrics.aggregate_run(run, package)["readiness"]

    assert readiness["status"] == "PARTIAL"
    assert readiness["planned_n"] == 5
    assert readiness["observed_n"] == 0
    assert readiness["ready_n"] == 0
    assert readiness["ready_rate"]["value"] == 0
    assert readiness["status_counts"] == {"not_started": 1}


def test_legacy_results_rows_are_normalized_without_filling_pending_quality(tmp_path):
    run, package = _make_run(tmp_path, "wikieval")
    (run / "terminal-ledger.jsonl").unlink()
    _write_jsonl(
        run / "results.jsonl",
        [
            {
                "case": {"id": "q1", "question_type": "inference"},
                "repeat": 1,
                "status": "ok",
                "chunks": [{"document_id": "d1"}],
                "answer": "Alpha answer",
                "retrieval_latency_ms": 10,
                "generation_latency_ms": 15,
            },
            {
                "case": {"id": "q2", "question_type": "comparison"},
                "repeat": 1,
                "status": "ok",
                "chunks": [{"document_id": "d3"}],
                "answer": "wrong",
                "retrieval_latency_ms": 20,
                "generation_latency_ms": 25,
            },
            {
                "case": {"id": "q3", "question_type": "temporal"},
                "repeat": 1,
                "status": "error",
                "chunks": [],
                "answer": "",
                "retrieval_latency_ms": 30,
            },
        ],
    )

    result = metrics.aggregate(run, package)

    assert result["retrieval"]["denominator"] == {
        "planned_n": 4,
        "terminal_n": 3,
        "valid_n": 2,
        "failed_n": 1,
        "unsupported_n": 0,
        "pending_n": 1,
    }
    assert result["retrieval"]["metrics"]["recall_at_1"]["value"] is None
    assert result["retrieval"]["metrics"]["retrieval_p50"]["value"] == 20
    assert result["qa"]["metrics"]["answer_non_empty"]["value"] is None


def test_excluded_benchmarks_are_explicitly_skipped(tmp_path):
    run, package = _make_run(tmp_path, "OmniDocBench")

    result = metrics.aggregate_run(run, package)

    assert result["status"] == "EXCLUDED"
    assert result["reason"] == "EXCLUDED_DATASET"
    assert result["metrics"] == {}


def test_manifest_count_remains_authoritative_when_question_file_is_absent(tmp_path):
    package = tmp_path / "count-only-package"
    _write_json(
        package / "manifest.json",
        {
            "schema_version": "competitor-eval-ready-v1",
            "dataset_id": "wikieval",
            "counts": {"questions": 4},
        },
    )
    run = tmp_path / "count-only-run"
    _write_json(run / "start-record.json", {"planned": {"questions": 4, "initial_attempts": 4}})
    _write_jsonl(
        run / "initial-ledger.jsonl",
        [{"question_id": f"q{i}", "repeat_id": 1} for i in range(1, 5)],
    )
    _write_jsonl(
        run / "terminal-ledger.jsonl",
        [
            {"stage": "retrieval", "question_id": "q1", "status": "SUCCESS", "hits": []},
            {"stage": "qa", "question_id": "q1", "status": "SUCCESS", "answer": "untrusted prediction"},
        ],
    )

    result = metrics.aggregate_run(run, package)

    assert result["retrieval"]["denominator"]["planned_n"] == 4
    assert result["retrieval"]["denominator"]["pending_n"] == 3
    assert result["qa"]["metrics"]["normalized_em"]["value"] is None
    assert result["qa"]["metrics"]["normalized_em"]["reason"] == metrics.UNSUPPORTED_GOLD_UNAVAILABLE


def test_judge_and_semantic_metrics_are_explicitly_unsupported(tmp_path):
    run, package = _make_run(tmp_path, "wikieval")

    result = metrics.aggregate_run(run, package)

    for name in ("faithfulness", "answer_relevance", "context_precision", "context_recall", "semantic_correctness"):
        record = result["qa"]["metrics"][name]
        assert record["value"] is None
        assert record["reason"] == metrics.UNSUPPORTED_NEEDS_JUDGE


def test_mmdocir_page_metrics_require_and_use_explicit_locators(tmp_path):
    questions = [
        {
            "question_id": "mm-q1",
            "question": "Which page?",
            "reference_answer": "page one",
            "answerable": True,
            "gold_doc_ids": ["page-1", "page-2"],
            "gold_evidence": [
                {"doc_id": "page-1", "locator": {"file_id": "file-a", "page": 1}},
                {"doc_id": "page-2", "locator": {"file_id": "file-a", "page": 2}},
            ],
            "question_type": "retrieval",
            "metadata": {"domain": "finance"},
        }
    ]
    package = _make_package(tmp_path, "mmdocir", questions)
    run = tmp_path / "mmdocir-run"
    _write_json(run / "start-record.json", {"planned": {"questions": 1, "initial_attempts": 1}})
    _write_jsonl(run / "initial-ledger.jsonl", [{"question_id": "mm-q1", "planned_denominator": True}])
    _write_jsonl(
        run / "terminal-ledger.jsonl",
        [
            {
                "stage": "retrieval",
                "question_id": "mm-q1",
                "status": "SUCCESS",
                "hits": [
                    {"file_id": "file-a", "page_number": 1},
                    {"file_id": "file-a", "page_number": 9},
                ],
                "latency_ms": 5,
            }
        ],
    )

    result = metrics.aggregate_run(run, package)

    assert result["retrieval"]["metrics"]["page_recall_at_1"]["value"] == 0.5
    assert result["retrieval"]["metrics"]["page_recall_at_3"]["value"] == 0.5
    assert result["retrieval"]["metrics"]["page_mrr"]["value"] == 1.0


def test_mmdocrag_typed_evidence_and_fab_coverage_are_deterministic(tmp_path):
    questions = [
        {
            "question_id": "rag-q1",
            "question": "What does the figure say?",
            "reference_answer": "answer",
            "answerable": True,
            "gold_doc_ids": ["doc-1"],
            "gold_evidence": [
                {"quote_id": "text-1", "doc_id": "text-1", "modality": "text"},
                {"quote_id": "image-1", "doc_id": "image-1", "modality": "image"},
            ],
            "question_type": "Descriptive",
            "metadata": {"evidence_modality_type": ["text", "image"]},
        }
    ]
    package = _make_package(tmp_path, "mmdocrag", questions)
    run = tmp_path / "mmdocrag-run"
    _write_json(run / "start-record.json", {"planned": {"questions": 1, "initial_attempts": 1}})
    _write_jsonl(run / "initial-ledger.jsonl", [{"question_id": "rag-q1", "planned_denominator": True}])
    _write_jsonl(
        run / "terminal-ledger.jsonl",
        [
            {
                "stage": "retrieval",
                "question_id": "rag-q1",
                "status": "SUCCESS",
                "hits": [
                    {"quote_id": "text-1", "modality": "text"},
                    {"quote_id": "image-1", "modality": "image"},
                ],
                "latency_ms": 5,
            }
        ],
    )

    result = metrics.aggregate_run(run, package)

    assert result["retrieval"]["metrics"]["text_evidence_recall_at_5"]["value"] == 1.0
    assert result["retrieval"]["metrics"]["image_evidence_recall_at_5"]["value"] == 1.0

    fab_package = _make_package(tmp_path, "fab-bench", questions)
    fab_manifest = json.loads((fab_package / "manifest.json").read_text(encoding="utf-8"))
    fab_manifest["source_complete"] = False
    fab_manifest["conditions"] = {
        "count_basis": "current_local_frozen",
        "gold_image_coverage": {"required": 4, "available": 1, "missing": 3},
        "source_status_counts": {"source_acquired": 1, "evidence_only": 1},
    }
    _write_json(fab_package / "manifest.json", fab_manifest)
    fab_result = metrics.aggregate_run(run, fab_package)
    assert fab_result["dataset"]["dataset_id"] == "fab-bench"
    assert fab_result["coverage"]["gold_image_evidence"]["value"] == 0.25
    assert fab_result["qa"]["metrics"]["factuality"]["value"] is None
    assert fab_result["qa"]["metrics"]["factuality"]["reason"] == metrics.UNSUPPORTED_NEEDS_JUDGE


def test_cli_writes_json_and_bounded_markdown_without_editing_todo(tmp_path, capsys):
    run, package = _make_run(tmp_path)
    output = tmp_path / "aggregate.json"
    todo = tmp_path / "TODO.md"
    todo.write_text("sentinel\n", encoding="utf-8")

    assert metrics.main(["aggregate", "--run", str(run), "--package", str(package), "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "competitor-eval-metrics-v1"

    assert metrics.main(["todo-markdown", "--run", str(run), "--package", str(package)]) == 0
    block = capsys.readouterr().out
    assert block.startswith("<!-- COMPETITOR_EVAL_METRICS_START -->")
    assert block.rstrip().endswith("<!-- COMPETITOR_EVAL_METRICS_END -->")
    assert len(block) < 12000
    assert todo.read_text(encoding="utf-8") == "sentinel\n"


def test_shared_structured_gold_normalizer_is_the_same_contract_for_judge_and_metrics():
    question = {
        "claims": [{"claim_id": "c1", "text": "Fact one"}],
        "critical_required_claims": ["c1"],
        "claim_evidence_sets": [["e1"]],
    }

    assert metrics.normalize_structured_gold is registry.normalize_structured_gold
    assert judge.normalize_structured_gold is registry.normalize_structured_gold
    assert metrics.normalize_structured_gold(question)["critical_claims"] == ["c1"]
    assert registry.structured_claim_gold_available([question]) is True
    assert registry.structured_claim_gold_available([{"gold_evidence": [{"evidence_id": "e1"}]}]) is False


def test_generated_text_only_id_keeps_canonical_claim_metrics_na_without_structured_gold(tmp_path):
    run, package = _make_run(tmp_path, "moi-rag-bench-v0.1-text-only-no-mllm")

    result = metrics.aggregate_run(run, package)

    assert result["dataset"]["dataset_id"] == "moi-rag-bench-v0.1-text-only-no-mllm"
    for name in ("claim_correctness", "reference_claim_recall", "critical_claim_coverage", "gold_evidence_support", "grounding", "tdas"):
        assert result["moi_unified"]["metrics"][name]["value"] is None
        assert result["moi_unified"]["metrics"][name]["na_reason"] == "N/A:MISSING_STRUCTURED_CLAIM_GOLD"


def test_text_only_metrics_reject_declared_count_mismatch(tmp_path):
    run, package = _make_run(tmp_path, "moi-rag-bench-v0.1-text-only-no-mllm")
    run_manifest_path = run / "start-record.json"
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    run_manifest["condition"] = "text-only-no-mllm"
    _write_json(run_manifest_path, run_manifest)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "condition": "text-only-no-mllm",
            "mllm_required": False,
            "image_llm": "NOT_APPLICABLE",
        }
    )
    _write_json(manifest_path, manifest)

    with pytest.raises(metrics.MetricsError, match="PACKAGE_COUNT_MISMATCH:documents"):
        metrics.aggregate_run(run, package)


def test_empty_source_dataset_falls_back_to_package_dataset_for_legacy_rows(tmp_path):
    run, package = _make_run(tmp_path, "wikieval")
    questions = [json.loads(line) for line in (package / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    questions[0]["source_dataset"] = ""
    _write_jsonl(package / "questions.jsonl", questions)

    result = metrics.aggregate_run(run, package)

    assert result["moi_unified"]["source_datasets"] == ["wikieval"]
    assert set(result["dataset_native"]["by_source_dataset"]) == {"wikieval"}


def test_missing_judge_observation_is_na_and_adapted_diagnostic_is_not_canonical(tmp_path):
    run, package = _make_mixed_fixture(tmp_path)
    judge_rows = [
        json.loads(line)
        for line in (run / "judge-terminal-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("question_id") != "doc-empty-reference"
    ]
    for row in judge_rows:
        dimensions = row.get("judgement", {}).get("dimensions", {})
        if row.get("status") == "SUCCESS":
            dimensions["response_claim_correctness"] = dimensions.pop("claim_correctness")
            dimensions["runtime_context_faithfulness"] = dimensions.pop("grounding")
    _write_jsonl(run / "judge-terminal-ledger.jsonl", judge_rows)

    result = metrics.aggregate_run(run, package)["moi_unified"]
    adapted = result["judge"]["response_claim_correctness"]

    assert adapted["value"] is None
    assert adapted["na_reason"] == "N/A:INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS"
    assert adapted["planned_n"] == 4
    assert adapted["observed_n"] == 2
    assert adapted["missing_n"] == 1
    assert adapted["failed_n"] == 1
    assert adapted["protocol_label"] == "ADAPTED_REFERENCE_RUBRIC"
    assert result["metrics"]["claim_correctness"]["value"] is None
    assert result["metrics"]["claim_correctness"]["na_reason"] == "N/A:MISSING_STRUCTURED_CLAIM_GOLD"
    assert "judge_claim_correctness" not in result["metrics"]


def test_metrics_replay_scores_explicit_context_mismatch_as_zero() -> None:
    row = {
        "context_available": True,
        "judgement": {
            "dimensions": {
                "response_claim_correctness": {"score": 1.0, "supported": True},
                "runtime_context_faithfulness": {"score": None, "supported": False},
            }
        },
    }

    assert metrics._judge_dimension_value(row, "runtime_context_faithfulness") == 0.0


def _add_structured_gold_aliases(package: Path) -> None:
    questions = [json.loads(line) for line in (package / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    for question in questions:
        question["claims"] = [{"claim_id": "c1", "text": "A frozen claim"}]
        question["critical_required_claims"] = ["c1"]
        question["claim_evidence_sets"] = [["d1"]]
    _write_jsonl(package / "questions.jsonl", questions)


def _rewrite_canonical_judge_rows(run: Path, *, omit_question_id: str | None = None) -> None:
    rows = [json.loads(line) for line in (run / "judge-terminal-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for row in rows:
        if row.get("question_id") == omit_question_id:
            continue
        row["status"] = "SUCCESS"
        row["judgement"] = {
            "dimensions": {
                "claim_correctness": {"score": 1.0, "supported": True},
                "reference_claim_recall": {"score": 1.0, "supported": True},
                "critical_claim_coverage": {"score": 1.0, "supported": True},
                "gold_evidence_support": {"score": 1.0, "supported": True},
                "grounding": {"score": 1.0, "supported": True},
                "tdas": {"score": 1.0, "supported": True},
            }
        }
        rewritten.append(row)
    _write_jsonl(run / "judge-terminal-ledger.jsonl", rewritten)


def test_canonical_judge_metrics_require_normalized_gold_and_complete_observations(tmp_path):
    run, package = _make_mixed_fixture(tmp_path)
    _add_structured_gold_aliases(package)
    _rewrite_canonical_judge_rows(run)

    result = metrics.aggregate_run(run, package)["moi_unified"]

    for name in ("claim_correctness", "reference_claim_recall", "critical_claim_coverage", "gold_evidence_support", "grounding", "tdas"):
        assert result["metrics"][name]["value"] == 1
        assert result["metrics"][name]["na_reason"] is None


def test_canonical_judge_metrics_are_na_when_structured_gold_is_present_but_judge_is_partial(tmp_path):
    run, package = _make_mixed_fixture(tmp_path)
    _add_structured_gold_aliases(package)
    _rewrite_canonical_judge_rows(run, omit_question_id="mm-1")

    result = metrics.aggregate_run(run, package)["moi_unified"]
    record = result["metrics"]["claim_correctness"]

    assert record["value"] is None
    assert record["na_reason"] == "N/A:INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS"
    assert record["planned_n"] == 6
    assert record["observed_n"] == 5
    assert record["missing_n"] == 1
