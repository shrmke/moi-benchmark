from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest


HERE = Path(__file__).resolve().parents[1] / "scripts/evaluation"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import competitor_eval_judge as judge


@pytest.mark.parametrize(
    "dataset",
    ("moi-rag-bench-v0.3.1-qa-revision", "moi-rag-bench-v0.3.1-new-qa-increment"),
)
def test_v031_dataset_aliases_use_mixed_benchmark_rubric(dataset: str) -> None:
    assert judge._norm_dataset(dataset) == judge.MIXED_DATASET_ID


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{hashlib.sha256(encoded).hexdigest()}  {path.name}\n", encoding="utf-8"
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode()
    path.write_bytes(encoded)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{hashlib.sha256(encoded).hexdigest()}  {path.name}\n", encoding="utf-8"
    )


def make_package(
    root: Path,
    *,
    dataset: str = "WikiEval",
    questions: list[dict[str, Any]] | None = None,
    provider_selection: dict[str, Any] | None = None,
) -> Path:
    package = root / "condition-package"
    package.mkdir(parents=True, exist_ok=True)
    questions = questions or [
        {
            "question_id": "q-1",
            "question": "What is the answer?",
            "reference_answer": "The answer is blue.",
            "answerable": True,
            "question_type": "single_doc_single_evidence",
            "scope_doc_ids": ["doc-1"],
            "gold_doc_ids": ["doc-1"],
            "gold_evidence": [{"evidence_id": "e-1", "span": "The answer is blue."}],
        }
    ]
    corpus = [{"doc_id": "doc-1", "title": "Doc 1", "content": "The answer is blue.", "media_type": "text"}]
    gold = [{"question_id": row["question_id"], "reference_answer": row.get("reference_answer", "")} for row in questions]
    _write_jsonl(package / "questions.jsonl", questions)
    _write_jsonl(package / "corpus.jsonl", corpus)
    _write_jsonl(package / "gold.jsonl", gold)
    _write_json(
        package / "package.json",
        {
            "schema": "competitor-eval-ready-v1",
            "dataset": dataset,
            "revision": "frozen-rev-1",
            "split": "test",
            "protocol_tag": "ADAPTED_PROTOCOL",
            "condition": "native",
            "scope": "global",
            "artifacts": {
                "corpus.jsonl": "corpus.jsonl",
                "questions.jsonl": "questions.jsonl",
                "gold.jsonl": "gold.jsonl",
            },
            "provider_selection": provider_selection
            or {
                "text": {"provider": "deepseek-official", "model": "deepseek-v4-flash"},
                "embedding": {"provider": "huawei-maas", "model": "bge-m3", "dimension": 1024},
            },
        },
    )
    return package


def make_runner(
    root: Path,
    questions: list[dict[str, Any]],
    *,
    qa_statuses: dict[str, str] | None = None,
    answers: dict[str, str] | None = None,
    retrieval_hits: dict[str, list[dict[str, Any]]] | None = None,
    qa_extra: dict[str, dict[str, Any]] | None = None,
    runner_status: str = "SUCCESS",
) -> Path:
    run_dir = root / "runner-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    qa_statuses = qa_statuses or {}
    answers = answers or {}
    retrieval_hits = retrieval_hits or {}
    qa_extra = qa_extra or {}
    _write_json(
        run_dir / "start-record.json",
        {
            "schema": "competitor-eval-run-start-v1",
            "run_id": "runner-run-1",
            "status_at_start": "not_started",
            "dataset": "WikiEval",
            "dataset_revision": "frozen-rev-1",
            "condition": "native",
            "provider": {
                "policy": "no_taas",
                "llm": "deepseek-official/deepseek-v4-flash",
                "embedding": "maas/bge-m3/1024",
            },
            "planned": {"questions": len(questions), "initial_attempts": len(questions), "repeats": 1},
        },
    )
    _write_jsonl(
        run_dir / "initial-ledger.jsonl",
        [
            {
                "schema": "competitor-eval-initial-ledger-v1",
                "attempt_id": f"{row['question_id']}#repeat-1",
                "question_id": row["question_id"],
                "repeat_id": 1,
                "stage": "initial",
                "status": "not_started",
                "planned_denominator": True,
            }
            for row in questions
        ],
    )
    terminal: list[dict[str, Any]] = []
    for row in questions:
        question_id = str(row["question_id"])
        retrieval_row: dict[str, Any] = {
            "schema": "competitor-eval-terminal-ledger-v1",
            "stage": "retrieval",
            "question_id": question_id,
            "repeat_id": 1,
            "status": "SUCCESS",
            "hits": retrieval_hits.get(question_id, [{"doc_id": "doc-1", "text": "The answer is blue."}]),
        }
        terminal.append(retrieval_row)
        status = qa_statuses.get(question_id, "SUCCESS")
        qa_row: dict[str, Any] = {
            "schema": "competitor-eval-terminal-ledger-v1",
            "stage": "qa",
            "question_id": question_id,
            "repeat_id": 1,
            "status": status,
            "answer": answers.get(question_id, "The answer is blue."),
            **qa_extra.get(question_id, {}),
        }
        terminal.append(qa_row)
    _write_jsonl(run_dir / "terminal-ledger.jsonl", terminal)
    _write_json(run_dir / "summary.json", {"status": runner_status, "run_id": "runner-run-1"})
    return run_dir


def make_generated_text_only_fixture(root: Path) -> Path:
    """Write the generated builder's manifest shape with one representative row."""

    package = make_package(
        root,
        dataset="moi-rag-bench-v0.1-text-only-no-mllm",
        questions=[
            {
                "question_id": "q-generated",
                "question": "What is the answer?",
                "reference_answer": "The answer is blue.",
                "answerable": True,
                "question_type": "text-only",
                "source_dataset": "docbench",
                "gold_doc_ids": ["doc-1"],
                "gold_evidence": [{"doc_id": "doc-1", "span": "The answer is blue."}],
            }
        ],
    )
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest.pop("dataset", None)
    manifest.update(
        {
            "schema": "competitor-eval-ready-v1",
            "schema_version": "competitor-eval-ready-v1",
            "package_schema": "competitor-eval-ready-v1",
            "dataset_id": "moi-rag-bench-v0.1-text-only-no-mllm",
            "dataset_name": "MOI RAG Benchmark v0.1 derived text-only-no-mllm condition",
            "dataset_revision": "derived-local-v0.1",
            "revision": "derived-local-v0.1",
            "condition": "text-only-no-mllm",
            "mllm_required": False,
            "image_llm": "NOT_APPLICABLE",
            "protocol_tag": "MOI_RAG_BENCH_V0_1_TEXT_ONLY_NO_MLLM",
            "split": "evaluation",
            "status": "READY_LOCAL_DERIVED_TEXT_ONLY",
            "readiness_status": "READY",
            "documents": "corpus.jsonl",
            "corpus_path": "corpus.jsonl",
            "questions": "questions.jsonl",
            "questions_path": "questions.jsonl",
            "gold": "gold.jsonl",
            "gold_path": "gold.jsonl",
            "counts": {"documents": 1, "questions": 1, "gold": 1},
        }
    )
    _write_json(package / "package.json", manifest)
    return package


def configure_generated_runner(run_dir: Path, *, question_id: str = "q-generated") -> None:
    start = json.loads((run_dir / "start-record.json").read_text(encoding="utf-8"))
    start.update({"dataset": "moi-rag-bench-v0.1-text-only-no-mllm", "condition": "text-only-no-mllm"})
    _write_json(run_dir / "start-record.json", start)
    initial = [json.loads(line) for line in (run_dir / "initial-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in initial:
        row["question_id"] = question_id
        row["attempt_id"] = f"{question_id}#repeat-1"
    _write_jsonl(run_dir / "initial-ledger.jsonl", initial)
    terminal = [json.loads(line) for line in (run_dir / "terminal-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in terminal:
        row["question_id"] = question_id
    _write_jsonl(run_dir / "terminal-ledger.jsonl", terminal)


def valid_response(question_id: str, dataset: str = "wikieval", score: float = 1.0) -> dict[str, Any]:
    contract = judge.metric_contract_for(dataset)
    return {
        "schema": "competitor-eval-judge-response-v1",
        "question_id": question_id,
        "dimensions": {
            name: {"score": score, "supported": True, "reason": "supported by the supplied record"}
            for name in contract["dimensions"]
        },
        "overall": score,
    }


class FakeHTTP:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return {"choices": [{"message": {"content": json.dumps(response)}}]}


def http_factory(fake: FakeHTTP):
    return lambda *args, **kwargs: fake


def test_preflight_freezes_judge_artifacts_and_contract_without_http(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest["conditions"] = {"documents": 1, "questions": 1, "selection": "metadata-not-named-conditions"}
    _write_json(package / "package.json", manifest)
    run_dir = make_runner(tmp_path, questions)
    output = tmp_path / "judge-output"

    result = judge.preflight(run_dir, package, output=output, env={}, dry_run=True)

    assert result["status"] == "READY"
    assert result["provider"]["model"] == "deepseek-v4-flash"
    assert result["provider"]["multimodal"] is False
    assert result["metric_contract"]["protocol_tag"] == "RAGAS_COMPATIBLE_JUDGE"
    assert (output / "judge-start-record.json").is_file()
    assert (output / "judge-start-record.json.sha256").is_file()
    assert (output / "judge-initial-ledger.jsonl").is_file()
    assert (output / "judge-initial-ledger.jsonl.sha256").is_file()
    assert "secret" not in (output / "judge-start-record.json").read_text(encoding="utf-8")


def test_dry_preflight_accepts_runner_package_without_completed_qa(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    _write_jsonl(run_dir / "terminal-ledger.jsonl", [])
    _write_json(run_dir / "summary.json", {"status": "DRY_RUN", "run_id": "fixture-run"})

    result = judge.preflight(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={},
        dry_run=True,
    )

    assert result["status"] == "DRY_RUN"
    assert result["planned_n"] == 1
    assert "RUN_QA_TERMINAL_LEDGER_NOT_PRESENT_DRY_RUN" in result["warnings"]


def test_run_requires_schema_and_records_redacted_raw_request_response(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": [{"span": "Blue"}]}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions, qa_extra={"q-1": {"citations": [{"doc_id": "doc-1"}]}})
    fake = FakeHTTP([valid_response("q-1")])
    output = tmp_path / "judge-output"

    result = judge.run(
        run_dir,
        package,
        output=output,
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
        http_factory=http_factory(fake),
    )

    assert result["status"] == "COMPLETE"
    assert len(fake.calls) == 1
    request = fake.calls[0]["json_body"]
    assert request["model"] == "deepseek-v4-flash"
    assert request["thinking"] == {"type": "disabled"}
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    raw = next((output / "judge-raw").glob("*.json"))
    raw_text = raw.read_text(encoding="utf-8")
    assert "secret" not in raw_text
    assert "<redacted>" in raw_text
    terminal = [json.loads(line) for line in (output / "judge-terminal-ledger.jsonl").read_text().splitlines()]
    assert terminal[0]["status"] == "SUCCESS"
    assert terminal[0]["judge_model"] == "deepseek-v4-flash"
    assert terminal[0]["prompt_hash"].startswith("sha256:")


def test_resume_reuses_terminal_units_after_interruption(tmp_path: Path) -> None:
    questions = [
        {"question_id": "q-1", "question": "What 1?", "reference_answer": "Blue", "gold_evidence": []},
        {"question_id": "q-2", "question": "What 2?", "reference_answer": "Blue", "gold_evidence": []},
    ]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    first = FakeHTTP([valid_response("q-1"), KeyboardInterrupt()])
    output = tmp_path / "judge-output"

    with pytest.raises(KeyboardInterrupt):
        judge.run(run_dir, package, output=output, env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"}, http_factory=http_factory(first))
    assert len(first.calls) == 2
    first_terminal = [json.loads(line) for line in (output / "judge-terminal-ledger.jsonl").read_text().splitlines()]
    assert [row["question_id"] for row in first_terminal] == ["q-1"]

    second = FakeHTTP([valid_response("q-2")])
    result = judge.run(run_dir, package, output=output, env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"}, http_factory=http_factory(second))

    assert result["status"] == "COMPLETE"
    assert len(second.calls) == 1
    assert "q-2" in second.calls[0]["json_body"]["messages"][1]["content"]


def test_failures_remain_in_denominator_and_absent_context_is_unsupported(tmp_path: Path) -> None:
    questions = [
        {"question_id": "q-1", "question": "What 1?", "reference_answer": "Blue", "gold_evidence": [{"span": "Blue"}]},
        {"question_id": "q-2", "question": "What 2?", "reference_answer": "Blue", "gold_evidence": [{"span": "Blue"}]},
    ]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions, qa_statuses={"q-2": "FAILED"}, retrieval_hits={"q-1": []})
    fake = FakeHTTP([valid_response("q-1")])
    output = tmp_path / "judge-output"

    judge.run(run_dir, package, output=output, env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"}, http_factory=http_factory(fake))
    aggregate = judge.aggregate(run_dir, package, output=output)

    assert aggregate["denominator"]["planned_n"] == 2
    assert aggregate["denominator"]["failed_n"] == 1
    assert aggregate["dimensions"]["answer_relevance"]["eligible_n"] == 1
    assert aggregate["dimensions"]["answer_relevance"]["value"] == pytest.approx(0.5)
    assert aggregate["dimensions"]["faithfulness"]["na_n"] == 1
    assert aggregate["dimensions"]["faithfulness"]["failed_n"] == 1
    assert aggregate["dimensions"]["faithfulness"]["value"] is None
    assert aggregate["dimensions"]["faithfulness"]["na_reason"] == "N/A:INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS"


def test_mixed_dimensions_use_answerability_specific_denominators(tmp_path: Path) -> None:
    questions = [
        {
            "question_id": "q-answerable",
            "question": "What is the answer?",
            "reference_answer": "Blue",
            "answerable": True,
            "gold_evidence": [{"span": "Blue"}],
        },
        {
            "question_id": "q-unanswerable",
            "question": "What is not stated?",
            "reference_answer": "Not stated.",
            "answerable": False,
            "gold_evidence": [],
        },
    ]
    dataset = "moi-rag-bench-v0.3.1-qa-revision"
    package = make_package(tmp_path, dataset=dataset, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    start_path = run_dir / "start-record.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["dataset"] = dataset
    _write_json(start_path, start)

    answerable = valid_response("q-answerable", dataset=dataset)
    unanswerable = valid_response("q-unanswerable", dataset=dataset)
    for name in judge.MIXED_ANSWERABLE_ONLY_DIMENSION_NAMES:
        unanswerable["dimensions"][name] = {
            "score": None,
            "supported": False,
            "reason": "N_A_UNANSWERABLE_ITEM",
        }
    fake = FakeHTTP([answerable, unanswerable])
    output = tmp_path / "judge-output"

    judge.run(
        run_dir,
        package,
        output=output,
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
        http_factory=http_factory(fake),
    )
    aggregate = judge.aggregate(run_dir, package, output=output)

    response = aggregate["dimensions"]["response_claim_correctness"]
    refusal = aggregate["dimensions"]["strict_unanswerable"]
    assert response["value"] == 1.0
    assert response["planned_n"] == response["observed_n"] == 1
    assert response["missing_n"] == 0
    assert refusal["value"] == 1.0
    assert refusal["planned_n"] == refusal["observed_n"] == 1
    assert refusal["missing_n"] == 0


def test_explicit_context_without_claim_support_scores_runtime_faithfulness_zero() -> None:
    dataset = "moi-rag-bench-v0.3.1-qa-revision"
    unit = judge.JudgeUnit(
        question_id="q-1",
        repeat_id=1,
        question={"question_id": "q-1", "answerable": True},
        qa=None,
        retrieval=None,
        answer="The answer is blue.",
        actual_context=[{"text": "The context is unrelated."}],
        gold_evidence=[{"span": "The answer is blue."}],
        citations=None,
        image_paths=[],
        runner_status="SUCCESS",
        runner_error=None,
    )
    response = valid_response("q-1", dataset=dataset)
    response["dimensions"]["runtime_context_faithfulness"] = {
        "score": None,
        "supported": False,
        "reason": "The answer is not based on the supplied actual_context.",
    }

    normalized = judge._normalize_judgement(response, unit, dataset)

    assert normalized["dimensions"]["runtime_context_faithfulness"] == {
        "score": 0.0,
        "supported": True,
        "reason": "PROTOCOL_ZERO: scored answer claims are not supported by the explicit runtime context",
    }


def test_real_local_image_evidence_still_uses_plain_text_maas_judge(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "Read the chart.", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    image = tmp_path / "evidence.png"
    image.write_bytes(b"fake-png-bytes")
    run_dir = make_runner(
        tmp_path,
        questions,
        retrieval_hits={"q-1": [{"doc_id": "doc-1", "image_path": str(image)}]},
    )
    fake = FakeHTTP([valid_response("q-1")])

    judge.run(run_dir, package, output=tmp_path / "judge-output", env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"}, http_factory=http_factory(fake))

    request = fake.calls[0]["json_body"]
    assert request["model"] == "deepseek-v4-flash"
    assert request["thinking"] == {"type": "disabled"}
    content = request["messages"][1]["content"]
    assert isinstance(content, str)
    assert "image_url" not in json.dumps(request, ensure_ascii=False)


def test_taas_is_rejected_before_any_http(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(
        tmp_path,
        questions=questions,
        provider_selection={"text": {"provider": "taas", "model": "some-model"}},
    )
    run_dir = make_runner(tmp_path, questions)
    fake = FakeHTTP([valid_response("q-1")])

    result = judge.run(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret", "TAAS_API_KEY": "must-not-be-used"},
        http_factory=http_factory(fake),
    )

    assert result["status"] == "BLOCKED"
    assert "TAAS" in " ".join(result["errors"])
    assert fake.calls == []


def test_non_maas_judge_endpoint_is_rejected_before_any_http(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    fake = FakeHTTP([valid_response("q-1")])

    result = judge.run(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
        maas_base_url="https://qianfan.baidubce.com/v2",
        http_factory=http_factory(fake),
    )

    assert result["status"] == "BLOCKED"
    assert "qianfan" in " ".join(result["errors"]).casefold()
    assert fake.calls == []


def test_arbitrary_judge_endpoint_is_rejected_before_any_http(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    fake = FakeHTTP([valid_response("q-1")])

    result = judge.run(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
        maas_base_url="https://api.openai.com/v1",
        http_factory=http_factory(fake),
    )

    assert result["status"] == "BLOCKED"
    assert "JUDGE_BASE_URL_MUST_USE_DEEPSEEK_OFFICIAL" in result["errors"]
    assert fake.calls == []


def test_huawei_maas_dify_plugin_namespace_is_not_taas(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    start_path = run_dir / "start-record.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["provider"]["native_llm_provider"] = "matrixorigin/matrixorigin_huawei_maas/huawei_maas"
    _write_json(start_path, start)

    result = judge.JudgeRunner(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
    ).preflight()

    assert result["status"] == "READY"
    assert not any("TAAS_PROVIDER_FORBIDDEN" in error for error in result["errors"])


def test_invalid_judge_json_is_retried_within_same_unit_and_then_terminal_failure(tmp_path: Path) -> None:
    questions = [{"question_id": "q-1", "question": "What?", "reference_answer": "Blue", "gold_evidence": []}]
    package = make_package(tmp_path, questions=questions)
    run_dir = make_runner(tmp_path, questions)
    fake = FakeHTTP([{"not": "the required schema"}, {"also": "invalid"}])

    result = judge.run(
        run_dir,
        package,
        output=tmp_path / "judge-output",
        env={"DEEPSEEK_API_KEY_NEW": "fixture-secret"},
        http_factory=http_factory(fake),
        retries=1,
    )

    assert result["status"] == "COMPLETE"
    assert len(fake.calls) == 2
    rows = [json.loads(line) for line in (tmp_path / "judge-output/judge-terminal-ledger.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["retry_count"] == 1
    assert rows[0]["error_code"] == "JUDGE_SCHEMA_INVALID"


def test_generated_text_only_package_alias_loads_with_frozen_text_only_contract(tmp_path: Path) -> None:
    actual_package = Path(__file__).resolve().parents[2] / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    package = actual_package if actual_package.is_dir() else make_generated_text_only_fixture(tmp_path)

    loaded = judge.load_condition_package(package)

    assert loaded.dataset_id == "moi-rag-bench-v0.1-text-only-no-mllm"
    assert loaded.condition == "text-only-no-mllm"
    assert judge.metric_contract_for(loaded.dataset_id)["rubric_mode"] == "ADAPTED_REFERENCE_RUBRIC"
    assert judge.metric_contract_for("moi-rag-bench-v0.1-ready-for-eval")["rubric_mode"] == "ADAPTED_REFERENCE_RUBRIC"


def test_text_only_readiness_smoke_alias_uses_the_same_frozen_rubric(tmp_path: Path) -> None:
    package = make_generated_text_only_fixture(tmp_path)
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest["dataset_id"] = "moi-rag-bench-v0.1-text-only-no-mllm-readiness-smoke"
    manifest["dataset_name"] = "MOI RAG Benchmark v0.1 text-only readiness smoke"
    manifest["condition"] = "text-only-no-mllm-readiness-smoke"
    _write_json(package / "package.json", manifest)

    loaded = judge.load_condition_package(package)

    assert loaded.dataset_id == "moi-rag-bench-v0.1-text-only-no-mllm"
    assert loaded.condition == "text-only-no-mllm-readiness-smoke"
    assert judge.metric_contract_for(loaded.dataset_id)["rubric_mode"] == "ADAPTED_REFERENCE_RUBRIC"


def test_text_only_judge_rejects_declared_count_mismatch(tmp_path: Path) -> None:
    package = make_generated_text_only_fixture(tmp_path)
    manifest_path = package / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["questions"] = 2
    _write_json(manifest_path, manifest)

    with pytest.raises(judge.PackageError, match="PACKAGE_COUNT_MISMATCH:questions"):
        judge.load_condition_package(package)


def test_shared_structured_gold_aliases_are_normalized_for_judge_routing() -> None:
    question = {
        "question_id": "q-1",
        "question": "Which facts?",
        "claims": [{"claim_id": "c1", "text": "Fact one"}],
        "critical_required_claims": ["c1"],
        "claim_evidence_sets": [{"set_id": "es1", "items": [{"evidence_id": "e1"}]}],
    }

    normalized = judge.normalize_structured_gold(question)
    rubric = judge.select_frozen_rubric(normalized)

    assert normalized["scored_reference_claims"] == question["claims"]
    assert normalized["critical_claims"] == question["critical_required_claims"]
    assert normalized["evidence_sets"] == question["claim_evidence_sets"]
    assert rubric["canonical_claims_available"] is True
