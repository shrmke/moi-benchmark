from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


HERE = Path(__file__).resolve().parents[1] / "scripts/evaluation"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import competitor_eval_runner as runner


def test_provider_chat_probe_allows_reasoning_before_final_answer() -> None:
    assert runner.PROVIDER_CHAT_PROBE_MAX_TOKENS >= 256


def test_non_maas_provider_markers_are_rejected_before_profile_creation() -> None:
    with pytest.raises(runner.RunnerError, match="FROZEN_PROVIDER_REQUIRED"):
        runner._reject_taas(["https://qianfan.baidubce.com/v2"])
    with pytest.raises(runner.RunnerError, match="FROZEN_PROVIDER_REQUIRED"):
        runner._reject_taas(["qwen3-embedding-8b"])
    with pytest.raises(runner.RunnerError, match="MAAS_ONLY_BASE_URL_REQUIRED"):
        runner._validate_maas_base_url("https://api.openai.com/v1")


def test_maxkb_external_context_prefers_ranked_chunks_and_bounds_full_paragraphs() -> None:
    context = runner._maxkb_external_context(
        [
            {
                "similarity": 0.9,
                "content": "A very long source paragraph that must not be sent in full. " * 500,
                "chunks": [
                    "unrelated context " * 30,
                    "The Angola model allows natural resources as collateral for loans.",
                ],
            }
        ],
        "What model allows natural resources as collateral for loans?",
        max_chars=500,
        max_chunks=4,
    )
    assert "Angola model" in context
    assert len(context) <= 500
    assert "A very long source paragraph" not in context


def test_maxkb_external_context_uses_short_prefix_when_no_chunk_matches() -> None:
    context = runner._maxkb_external_context(
        [{"content": "unrelated source text " * 500, "chunks": ["unrelated chunk"]}],
        "A question with no lexical overlap",
        max_chars=700,
    )
    assert len(context) <= 700
    assert "prefix" in context


def test_moi_text_only_dry_run_freezes_canonical_1000_attempt_ledger(tmp_path, monkeypatch) -> None:
    package = make_qa_package(tmp_path, question_count=1000)
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)

    status = runner.main(
        [
            "preflight",
            "--system",
            "moi_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "moi-text-only",
            "--text-llm-provider",
            "deepseek-official",
            "--deepseek-llm-model",
            "deepseek-v4-flash",
            "--maas-vl-model",
            "qwen2.5-vl-72b",
            "--maas-embedding-model",
            "bge-m3",
            "--maas-embedding-dimension",
            "1024",
            "--dry-run",
        ]
    )

    assert status == 0
    root = tmp_path / "runs" / "moi-text-only"
    start = json.loads((root / "start-record.json").read_text(encoding="utf-8"))
    assert start["system_id"] == "moi_local"
    assert start["planned"]["questions"] == 1000
    assert start["planned"]["initial_attempts"] == 1000
    assert start["planned"]["image_questions"] == 0
    assert start["provider"]["llm"] == "deepseek-official/deepseek-v4-flash"
    assert start["provider"]["embedding"] == "maas/bge-m3/1024"
    assert start["provider"]["image_llm"] == "NOT_APPLICABLE"
    assert start["provider"]["thinking"] == {"type": "disabled"}
    assert start["pipeline"]["image_generator"] == "NOT_APPLICABLE"
    assert start["pipeline"]["thinking"] == {"type": "disabled"}
    assert len(runner._read_jsonl(root / "initial-ledger.jsonl")) == 1000


def test_moi_rejects_image_questions_and_canonical_adapter_preserves_gold_fields(tmp_path, monkeypatch) -> None:
    image_package = make_package(tmp_path / "image", image_question=True)
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)
    assert runner.main([
        "preflight",
        "--system", "moi_local",
        "--package", str(image_package),
        "--output-root", str(tmp_path / "image-runs"),
        "--run-id", "moi-image-rejected",
        "--dry-run",
    ]) == 2

    package = runner.EvalPackage.load(make_package(tmp_path / "text"))
    args = runner.build_parser().parse_args([
        "all",
        "--system", "moi_local",
        "--package", str(package.root),
        "--output-root", str(tmp_path / "text-runs"),
        "--run-id", "moi-fixture",
        "--dry-run",
    ])
    context = runner.RunnerContext(args, package)
    adapter = context.adapter()
    canonical = adapter.canonical_package("__global__", package.documents)
    question = canonical["questions"][0]
    assert canonical["documents"][0]["metadata"]["file_id"] == "doc-1"
    assert question["id"] == "q-1"
    assert question["relevant_documents"] == ["doc-1"]
    assert question["relevant_evidence"] == ["gold evidence"]
    assert question["reference_answer"] == "gold answer"
    assert "expected_keywords" not in question


def test_declared_package_counts_and_text_only_flags_are_admission_contract(tmp_path) -> None:
    package = make_package(tmp_path)
    manifest_path = package / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "condition": "text-only-no-mllm",
            "mllm_required": False,
            "image_llm": "NOT_APPLICABLE",
            "counts": {"documents": 2, "questions": 1, "gold": 0},
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(runner.PackageError, match="PACKAGE_COUNT_MISMATCH:documents"):
        runner.EvalPackage.load(package)

    manifest["counts"]["documents"] = 1
    manifest["mllm_required"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(runner.PackageError, match="TEXT_ONLY_MLLM_REQUIRED_MUST_BE_FALSE"):
        runner.EvalPackage.load(package)


def test_moi_materializes_isolated_maas_config_for_cli(tmp_path, monkeypatch) -> None:
    package = make_package(tmp_path)
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)

    assert runner.main([
        "preflight",
        "--system", "moi_local",
        "--package", str(package),
        "--output-root", str(tmp_path / "runs"),
        "--run-id", "moi-config-isolation",
        "--llm-provider", "deepseek-official",
        "--llm-model", "deepseek-v4-flash",
        "--maas-embedding-model", "bge-m3",
        "--maas-embedding-dimension", "1024",
        "--dry-run",
    ]) == 0

    root = tmp_path / "runs" / "moi-config-isolation"
    config = json.loads((root / "moi" / "config.json").read_text(encoding="utf-8"))
    assert config["matrixone"]["database"].startswith("moi_eval_moi_config_isolation")
    assert config["matrixone"]["vector_table"].startswith("embedding_results_moi_config_isolation")
    assert config["embedding"] == {
        "api_key_env": "MAAS_API_KEY",
        "base_url": "https://api.modelarts-maas.com/v1",
        "dimension": 1024,
        "mode": "maas",
        "model": "bge-m3",
    }
    assert config["generation"]["provider"] == "deepseek-official"
    assert config["generation"]["api_key_env"] == "DEEPSEEK_API_KEY_NEW"
    assert config["generation"]["model"] == "deepseek-v4-flash"
    assert config["generation"]["thinking"] == "disabled"
    assert config["generation"]["include_page_images"] is False
    assert config["generation"]["system_prompt"] == runner.SHARED_PROMPT_TEXT


def test_moi_cli_result_uses_one_offset_index_instead_of_reloading_jsonl(tmp_path) -> None:
    rows = [
        {"case": {"id": "q-1"}, "status": "ok", "answer": "one"},
        {"case": {"id": "q-2"}, "status": "ok", "answer": "two"},
    ]
    path = tmp_path / "results.jsonl"
    encoded = [json.dumps(row).encode() + b"\n" for row in rows]
    path.write_bytes(b"".join(encoded))
    context = SimpleNamespace(
        root=tmp_path.resolve(),
        args=SimpleNamespace(dry_run=False),
        package=SimpleNamespace(),
        env={},
        contract={},
        _state_lock=threading.RLock(),
        _moi_result_offsets={},
    )
    adapter = runner.MoiAdapter(context)
    resource = {"cli": {"result_file": path.name}}

    assert adapter._cli_result(resource, SimpleNamespace(question_id="q-2"))["answer"] == "two"
    assert adapter._cli_result(resource, SimpleNamespace(question_id="q-1#repeat-1"))["answer"] == "one"
    assert context._moi_result_offsets[path.resolve()] == {"q-1": 0, "q-2": len(encoded[0])}


def test_start_record_freezes_shared_prompt_hash_and_nonzero_max_output(tmp_path, monkeypatch) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path))
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)
    args = runner.build_parser().parse_args([
        "preflight", "--system", "moi_local", "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", "prompt-contract", "--dry-run",
    ])

    runner.RunnerContext(args, package)

    start = json.loads((tmp_path / "runs" / "prompt-contract" / "start-record.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", start["pipeline"]["prompt_hash"])
    assert start["pipeline"]["prompt_version"] == "competitor-eval-shared-rag-v1"
    assert start["pipeline"]["max_output_tokens"] == 1024


def test_moi_fixture_is_forbidden_for_non_dry_run(tmp_path, monkeypatch) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path))
    monkeypatch.setenv("MOI_EVAL_FIXTURE", "1")
    args = runner.build_parser().parse_args([
        "all", "--system", "moi_local", "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", "moi-fixture-live",
    ])

    with pytest.raises(runner.RunnerError, match="TEST_FIXTURE_FORBIDDEN"):
        runner.RunnerContext(args, package)


def test_dify_native_app_uses_selected_llm_profile_for_model_and_provider(tmp_path, monkeypatch) -> None:
    selected = runner.ProviderProfile(
        name="deepseek-official",
        base_url="https://api.deepseek.com",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
    )
    context = SimpleNamespace(
        env={"DIFY_DEEPSEEK_LLM_PROVIDER": "deepseek-provider"},
        progress=None,
        root=tmp_path,
        profiles={
            "qianfan": runner.ProviderProfile(
                name="qianfan",
                base_url="https://qianfan.example/v1",
                api_key="",
                model="deepseek-v4-flash",
                image_model="qwen3.5-35b-a3b",
            ),
            "maas": runner.ProviderProfile(
                name="maas",
                base_url="https://maas.example/v1",
                api_key="maas-key",
                model="",
                embedding_model="bge-m3",
                embedding_dimension=1024,
            ),
            "llm": selected,
        },
        write_secret=lambda key, value: f"secrets/{key}.key",
    )
    adapter = object.__new__(runner.DifyAdapter)
    adapter.context = context
    adapter.args = SimpleNamespace(run_id="selected-llm")
    adapter.env = context.env
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)

    result = adapter._create_native_app("__global__", "dataset-1")

    configured = next(
        call for call in FakeDifyAppBinding.calls if call.get("path", "").endswith("/model-config")
    )
    assert configured["body"]["model"]["name"] == "deepseek-v4-flash"
    assert configured["body"]["model"]["provider"] == "deepseek-provider"
    assert configured["body"]["model"]["completion_params"]["thinking"] is False
    assert configured["body"]["pre_prompt"] == runner.SHARED_PROMPT_TEXT
    assert result["model"] == "deepseek-v4-flash"
    assert result["provider"] == "deepseek-official"
    assert result["thinking"] == {"type": "disabled"}


def test_maas_cli_aliases_select_glm_model_without_qianfan_fallback(tmp_path) -> None:
    args = runner.build_parser().parse_args([
        "preflight",
        "--system", "dify_local",
        "--package", str(make_package(tmp_path)),
        "--llm-provider", "deepseek-official",
        "--llm-model", "deepseek-v4-flash",
        "--dry-run",
    ])
    assert args.text_llm_provider == "deepseek-official"
    assert args.deepseek_llm_model == "deepseek-v4-flash"


@pytest.mark.parametrize("system", ["moi_local", "dify_local", "fastgpt_local", "maxkb_local"])
def test_all_platforms_reject_non_maas_text_provider_at_preflight(tmp_path, system) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path / system))
    args = runner.build_parser().parse_args([
        "preflight", "--system", system, "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", f"{system}-qianfan",
        "--llm-provider", "qianfan", "--dry-run",
    ])

    with pytest.raises(runner.RunnerError, match="DEEPSEEK_TEXT_PROVIDER_REQUIRED"):
        runner.RunnerContext(args, package)


@pytest.mark.parametrize("system", ["moi_local", "dify_local", "fastgpt_local", "maxkb_local"])
def test_all_platform_defaults_freeze_maas_generation_and_embedding(tmp_path, system) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path / system))
    args = runner.build_parser().parse_args([
        "preflight", "--system", system, "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", f"{system}-defaults", "--dry-run",
    ])

    context = runner.RunnerContext(args, package)

    assert context.profiles["llm"].name == "deepseek-official"
    assert context.profiles["llm"].model == "deepseek-v4-flash"
    assert context.profiles["maas"].name == "maas"
    assert context.profiles["maas"].embedding_model == "bge-m3"
    assert context.profiles["maas"].embedding_dimension == 1024
    assert context.profiles["vision"].model == "NOT_APPLICABLE"


def test_maxkb_qa_uses_external_maas_generation_after_diagnostic_retrieval(tmp_path) -> None:
    calls: list[dict[str, object]] = []
    llm = runner.ProviderProfile(
        name="deepseek-official",
        base_url="https://api.deepseek.com",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
    )
    embedding = runner.ProviderProfile(
        name="maas",
        base_url="https://maas.example/v1",
        api_key="maas-key",
        model="",
        embedding_model="bge-m3",
        embedding_dimension=1024,
    )

    class MaaSHTTP:
        def request(self, method, path, **kwargs):
            calls.append({"method": method, "path": path, **kwargs})
            return {"choices": [{"message": {"content": "external answer"}}]}

    context = SimpleNamespace(
        contract=runner.CONTRACTS["systems"]["maxkb_local"],
        profiles={"llm": llm, "maas": embedding},
        args=SimpleNamespace(run_id="maxkb-external", qa_timeout=1, query_timeout=1, top_k=2),
        _maas_qa_rate_limiter=SimpleNamespace(wait_for_slot=lambda: None),
        _http=lambda *args, **kwargs: MaaSHTTP(),
    )
    adapter = object.__new__(runner.MaxKBAdapter)
    adapter.context = context
    adapter.args = context.args
    adapter.package = SimpleNamespace()
    adapter.env = {}
    adapter.contract = context.contract
    adapter.public_base = "http://maxkb.example/chat/api"
    adapter._qa_rate_limiter = context._maas_qa_rate_limiter
    adapter._admin = lambda *args, **kwargs: {
        "records": [{"content": "retrieved MaxKB context", "score": 0.9, "document_id": "doc-1"}]
    }
    question = SimpleNamespace(
        media=["text"],
        question_id="q-1",
        text="What is the answer?",
        image_paths=[],
    )

    result = adapter.qa(question, {"knowledge_id": "kb-1"})

    assert result["contract"] == "external_generation_from_maxkb_retrieval"
    assert result["comparability"] == "diagnostic_admin_retrieval_plus_external_deepseek_generation"
    assert result["retrieval_contract"] == "diagnostic_admin_contract"
    assert result["generation_provider"] == "deepseek-official"
    assert result["generation_model"] == "deepseek-v4-flash"
    assert result["thinking"] == {"type": "disabled"}
    chat = calls[-1]
    assert chat["path"] == "/chat/completions"
    assert chat["json_body"]["model"] == "deepseek-v4-flash"
    assert chat["json_body"]["thinking"] == {"type": "disabled"}
    assert "retrieved MaxKB context" in json.dumps(chat["json_body"], ensure_ascii=False)


def test_dify_maas_selection_rejects_missing_maas_native_provider(tmp_path, monkeypatch) -> None:
    selected = runner.ProviderProfile(
        name="deepseek-official",
        base_url="https://api.deepseek.com",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
    )
    context = SimpleNamespace(
        env={"DIFY_QIANFAN_LLM_PROVIDER": "old-qianfan-provider"},
        progress=None,
        root=tmp_path,
        profiles={"llm": selected},
        write_secret=lambda key, value: f"secrets/{key}.key",
    )
    adapter = object.__new__(runner.DifyAdapter)
    adapter.context = context
    adapter.args = SimpleNamespace(run_id="missing-maas-provider")
    adapter.env = context.env
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)

    with pytest.raises(runner.ProviderUnavailable, match="DIFY_DEEPSEEK_LLM_PROVIDER_MISSING"):
        adapter._create_native_app("__global__", "dataset-1")


def test_dify_maas_preflight_discovers_the_selected_provider_and_both_active_models(tmp_path, monkeypatch) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path))
    monkeypatch.setenv("DIFY_API_BASE_URL", "http://dify.example/v1")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-secret")
    monkeypatch.setenv("DIFY_DEEPSEEK_LLM_PROVIDER", "provider-agent-reported-llm")
    monkeypatch.setenv("DIFY_MAAS_EMBEDDING_PROVIDER", "provider-agent-reported-embedding")
    monkeypatch.setenv("DEEPSEEK_API_KEY_NEW", "deepseek-secret")
    monkeypatch.setenv("MAAS_API_KEY", "maas-secret")

    class DiscoveryHTTP:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            if path == "/models":
                return {"data": [{"id": "deepseek-v4-flash"}, {"id": "bge-m3"}]}
            if path == "/embeddings":
                return {"data": [{"embedding": [0.0] * 1024}]}
            if path == "/chat/completions":
                return {"choices": [{"message": {"content": "READY", "reasoning_tokens": 0}}]}
            if path == "/workspaces/current/models/model-types/llm":
                return {"data": [{"provider": "provider-agent-reported-llm", "models": [{"model": "deepseek-v4-flash"}]}]}
            if path == "/workspaces/current/models/model-types/text-embedding":
                return {"data": [{"provider": "provider-agent-reported-embedding", "models": [{"model": "bge-m3"}]}]}
            raise AssertionError(f"unexpected discovery request: {method} {path}")

    monkeypatch.setattr(runner, "ArtifactHTTP", DiscoveryHTTP)
    args = runner.build_parser().parse_args([
        "preflight", "--system", "dify_local", "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-provider-discovery",
        "--llm-provider", "deepseek-official", "--llm-model", "deepseek-v4-flash",
        "--maas-embedding-model", "bge-m3", "--maas-embedding-dimension", "1024",
    ])
    context = runner.RunnerContext(args, package)

    result = context.provider_probe()

    assert result["ready"] is True
    assert result["native_model_discovery"]["llm"]["provider"] == "provider-agent-reported-llm"
    assert result["native_model_discovery"]["llm"]["model"] == "deepseek-v4-flash"
    assert result["native_model_discovery"]["embedding"]["provider"] == "provider-agent-reported-embedding"
    assert result["native_model_discovery"]["embedding"]["model"] == "bge-m3"


def test_dify_maas_preflight_fails_closed_when_discovered_pair_is_not_active(tmp_path, monkeypatch) -> None:
    package = runner.EvalPackage.load(make_package(tmp_path))
    monkeypatch.setenv("DIFY_API_BASE_URL", "http://dify.example/v1")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-secret")
    monkeypatch.setenv("DIFY_DEEPSEEK_LLM_PROVIDER", "provider-agent-reported-llm")
    monkeypatch.setenv("DIFY_MAAS_EMBEDDING_PROVIDER", "provider-agent-reported-embedding")
    monkeypatch.setenv("DEEPSEEK_API_KEY_NEW", "deepseek-secret")
    monkeypatch.setenv("MAAS_API_KEY", "maas-secret")

    class MissingModelHTTP:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            if path == "/models":
                return {"data": [{"id": "deepseek-v4-flash"}, {"id": "bge-m3"}]}
            if path == "/embeddings":
                return {"data": [{"embedding": [0.0] * 1024}]}
            if path == "/chat/completions":
                return {"choices": [{"message": {"content": "READY", "reasoning_tokens": 0}}]}
            if path == "/workspaces/current/models/model-types/llm":
                return {"data": [{"provider": "provider-agent-reported-llm", "models": [{"model": "deepseek-chat"}]}]}
            if path == "/workspaces/current/models/model-types/text-embedding":
                return {"data": [{"provider": "provider-agent-reported-embedding", "models": [{"model": "bge-m3"}]}]}
            raise AssertionError(f"unexpected missing-model request: {method} {path}")

    monkeypatch.setattr(runner, "ArtifactHTTP", MissingModelHTTP)
    args = runner.build_parser().parse_args([
        "preflight", "--system", "dify_local", "--package", str(package.root),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-provider-missing",
        "--llm-provider", "deepseek-official", "--llm-model", "deepseek-v4-flash",
        "--maas-embedding-model", "bge-m3", "--maas-embedding-dimension", "1024",
    ])
    context = runner.RunnerContext(args, package)

    result = context.provider_probe()

    assert result["ready"] is False
    assert result["native_model_discovery"]["ready"] is False
    assert result["native_model_discovery"]["error"] == "DIFY_NATIVE_SELECTED_MODEL_NOT_ACTIVE"


def test_maxkb_native_app_is_rejected_when_thinking_cannot_be_proven(tmp_path) -> None:
    selected = runner.ProviderProfile(
        name="deepseek-official",
        base_url="https://api.deepseek.com",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
    )
    context = SimpleNamespace(
        profiles={"llm": selected},
        root=tmp_path,
        write_secret=lambda key, value: f"secrets/{key}.key",
    )
    adapter = object.__new__(runner.MaxKBAdapter)
    adapter.context = context
    adapter.args = SimpleNamespace(run_id="selected-llm", top_k=10, upload_timeout=1)
    adapter.chat_model_id = ""
    calls: list[dict] = []

    def admin(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {"data": [{"id": "chat-glm", "model_name": "deepseek-v4-flash", "provider": "model_openai_provider"}]}

    adapter._admin = admin

    assert adapter._discover_chat_model() == "chat-glm"
    with pytest.raises(runner.ContractUnsupported, match="MAXKB_NATIVE_THINKING_DISABLE_UNPROVEN"):
        adapter._create_native_app("__global__", "knowledge-1")
    assert not any(call["path"] == "/workspace/default/application" for call in calls)


def test_maxkb_explicit_model_ids_are_preferred_and_duplicate_discovery_fails_closed(tmp_path) -> None:
    selected = runner.ProviderProfile(
        name="deepseek-official",
        base_url="https://api.deepseek.com",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        embedding_model="bge-m3",
        embedding_dimension=1024,
    )
    context = SimpleNamespace(
        profiles={"llm": selected, "maas": selected},
        root=tmp_path,
        write_secret=lambda key, value: f"secrets/{key}.key",
    )
    adapter = object.__new__(runner.MaxKBAdapter)
    adapter.context = context
    adapter.args = SimpleNamespace(run_id="model-id-selection", query_timeout=1)
    adapter.env = {
        "MAXKB_EMBEDDING_MODEL_ID": "bge-explicit",
        "MAXKB_LLM_MODEL_ID": "glm-explicit",
    }
    adapter.embedding_model_id = "bge-explicit"
    adapter.chat_model_id = "glm-explicit"
    adapter._admin = lambda *args, **kwargs: {
        "data": [
            {"id": "bge-old", "model_name": "bge-m3", "provider": "model_openai_provider"},
            {"id": "bge-new", "model_name": "bge-m3", "provider": "model_openai_provider"},
        ]
    }

    assert adapter._discover_embedding_model() == "bge-explicit"

    adapter.embedding_model_id = ""
    with pytest.raises(runner.ContractUnsupported, match="AMBIGUOUS_SET_MAXKB_EMBEDDING_MODEL_ID"):
        adapter._discover_embedding_model()


def test_bounded_resource_names_preserve_unique_suffix_information() -> None:
    prefix = "CompetitorEval-local-frozen-v14-20260811-mmdocir_doc_"
    first = runner._bounded_unique_name(prefix + "first-scope", 40)
    second = runner._bounded_unique_name(prefix + "second-scope", 40)
    assert len(first) == 40
    assert len(second) == 40
    assert first != second


def test_adapter_id_extracts_id_from_nested_dify_document_response() -> None:
    adapter = object.__new__(runner.BaseAdapter)

    assert adapter._id(
        {"document": {"id": "document-uuid", "indexing_status": "waiting"}},
        ("document", "document_id", "id"),
    ) == "document-uuid"


def test_dify_readiness_paginates_and_aggregates_document_statuses() -> None:
    responses = [
        {"data": [{"indexing_status": "completed"}] * 100, "has_more": True},
        {"data": [{"indexing_status": "ready"}] * 2, "has_more": False},
    ]

    class ReadinessHTTP:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def request(self, method, path, **kwargs):
            self.calls.append({"method": method, "path": path, **kwargs})
            return responses[kwargs["params"]["page"] - 1]

    client = ReadinessHTTP()
    adapter = object.__new__(runner.DifyAdapter)
    adapter.args = type("Args", (), {"query_timeout": 2})()
    adapter.dataset_key = "test-key"
    adapter.client = client

    snapshot = adapter._wait_ready_once("dataset-1", 102)

    assert snapshot == {
        "items": 102,
        "expected": 102,
        "statuses": {"completed": 100, "ready": 2},
        "ready": True,
    }
    assert [call["params"] for call in client.calls] == [
        {"page": 1, "limit": 100},
        {"page": 2, "limit": 100},
    ]
    assert [call["operation"] for call in client.calls] == [
        "dify-readiness-dataset-1",
        "dify-readiness-dataset-1",
    ]


def test_dify_readiness_error_on_later_page_still_fails() -> None:
    responses = [
        {"data": [{"indexing_status": "completed"}], "has_more": True},
        {"data": [{"indexing_status": "error"}], "has_more": False},
    ]

    class ReadinessHTTP:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def request(self, method, path, **kwargs):
            self.calls.append({"method": method, "path": path, **kwargs})
            return responses[kwargs["params"]["page"] - 1]

    client = ReadinessHTTP()
    adapter = object.__new__(runner.DifyAdapter)
    adapter.args = type("Args", (), {"query_timeout": 2})()
    adapter.dataset_key = "test-key"
    adapter.client = client

    with pytest.raises(runner.EvalError, match=r"DIFY_INDEX_FAILED:.*'error': 1"):
        adapter._wait_ready_once("dataset-1", 2)

    assert [call["params"]["page"] for call in client.calls] == [1, 2]


def test_dify_retrieval_truncates_queries_to_public_api_limit() -> None:
    class RetrievalHTTP:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def request(self, method, path, **kwargs):
            self.calls.append({"method": method, "path": path, **kwargs})
            return {"records": [{"segment": {"document_id": "doc-1"}}]}

    client = RetrievalHTTP()
    adapter = object.__new__(runner.DifyAdapter)
    adapter.args = type("Args", (), {"top_k": 10, "query_timeout": 2})()
    adapter.dataset_key = "test-key"
    adapter.client = client
    adapter._supported = lambda operation, media: None

    question = type("Question", (), {"text": "q" * 300, "media": (), "question_id": "q-1"})()
    result = adapter.retrieve(question, {"dataset_id": "dataset-1"})

    assert client.calls[0]["json_body"]["query"] == "q" * 250
    assert result["retrieval_query_chars"] == 250
    assert result["retrieval_query_truncated"] is True


def test_dify_global_ingest_reuses_submitted_remote_documents() -> None:
    documents = [
        type("Document", (), {"document_id": "doc-1", "source_hash": "hash-1"})(),
        type("Document", (), {"document_id": "doc-2", "source_hash": "hash-2"})(),
    ]
    resource = {
        "resource_id": "dataset-1",
        "dataset_id": "dataset-1",
        "uploads": {
            "doc-1": {"status": "submitted", "remote_id": "remote-1"},
            "doc-2": {"status": "submitted", "remote_id": "repaired-remote-2"},
        },
    }
    adapter = object.__new__(runner.DifyAdapter)
    adapter.package = type("Package", (), {"scope": "global"})()
    adapter.reuse_configured_resource = False
    adapter.configured_dataset_id = ""
    adapter.context = type(
        "Context",
        (),
        {"load_resources": lambda self: {"resources": {runner.GLOBAL_RESOURCE: resource}}},
    )()
    wait_calls: list[tuple[str, int]] = []
    uploads: list[tuple] = []

    adapter._target_resources = lambda: [(runner.GLOBAL_RESOURCE, documents)]
    adapter._existing = lambda key: resource
    adapter._ingest_selection = lambda key, candidates, current: (documents, {})
    adapter._upload_document = lambda *args: uploads.append(args)
    adapter._wait_ready = lambda dataset_id, expected: wait_calls.append((dataset_id, expected)) or {
        "items": expected,
        "expected": expected,
        "statuses": {"completed": expected},
    }
    adapter._ensure_native_app = lambda key, current: None
    adapter._save_resource = lambda key, current: None

    result = runner.DifyAdapter.ingest(adapter)

    assert result["status"] == "SUCCESS"
    assert uploads == []
    assert wait_calls == [("dataset-1", 2)]
    assert resource["uploads"]["doc-2"]["remote_id"] == "repaired-remote-2"


def test_fastgpt_readiness_waits_for_active_training_before_reporting_failure(monkeypatch) -> None:
    responses = iter(
        [
            {
                "code": 200,
                "data": {
                    "list": [
                        {
                            "trainingAmount": 24,
                            "activeTrainingAmount": 23,
                            "finalErrorAmount": 1,
                            "hasError": True,
                        }
                    ]
                },
            },
            {
                "code": 200,
                "data": {
                    "list": [
                        {
                            "trainingAmount": 24,
                            "activeTrainingAmount": 0,
                            "finalErrorAmount": 1,
                            "hasError": True,
                        }
                    ]
                },
            },
        ]
    )

    class ReadinessHTTP:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return next(responses)

    adapter = object.__new__(runner.FastGPTAdapter)
    adapter.args = type("Args", (), {"index_timeout": 10, "query_timeout": 2, "poll_seconds": 0})()
    adapter.api_key = "test-key"
    adapter.client = ReadinessHTTP()
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    result = adapter._wait_ready("dataset-1", 1)
    assert result["active_training"] == 0
    assert result["errors"] == 1
    assert result["failed_collection_ids"] == [""]

    assert adapter.client.calls == 2


def test_fastgpt_readiness_extends_bounded_deadline_while_indexing_progresses(monkeypatch) -> None:
    responses = iter(
        [
            {
                "code": 200,
                "data": {"list": [{
                    "trainingAmount": value,
                    "activeTrainingAmount": value,
                    "finalErrorAmount": 0,
                    "hasError": False,
                    "dataAmount": 4 - value,
                }]},
            }
            for value in (3, 2, 1, 0)
        ]
    )

    class ReadinessHTTP:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return next(responses)

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    adapter = object.__new__(runner.FastGPTAdapter)
    adapter.args = type("Args", (), {"index_timeout": 2, "query_timeout": 2, "poll_seconds": 1})()
    adapter.api_key = "test-key"
    adapter.client = ReadinessHTTP()
    monkeypatch.setattr(runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runner.time, "sleep", clock.sleep)

    result = adapter._wait_ready("dataset-1", 1)

    assert result["training"] == 0
    assert result["active_training"] == 0
    assert adapter.client.calls == 4


def test_fastgpt_readiness_progress_grace_has_a_hard_limit(monkeypatch) -> None:
    class ReadinessHTTP:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            value = 10 - self.calls
            return {
                "code": 200,
                "data": {"list": [{
                    "trainingAmount": value,
                    "activeTrainingAmount": value,
                    "finalErrorAmount": 0,
                    "hasError": False,
                    "dataAmount": self.calls,
                }]},
            }

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    adapter = object.__new__(runner.FastGPTAdapter)
    adapter.args = type("Args", (), {"index_timeout": 2, "query_timeout": 2, "poll_seconds": 1})()
    adapter.api_key = "test-key"
    adapter.client = ReadinessHTTP()
    monkeypatch.setattr(runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runner.time, "sleep", clock.sleep)

    with pytest.raises(runner.EvalError, match=r"FASTGPT_INDEX_TIMEOUT:.*'progress_extensions': 2"):
        adapter._wait_ready("dataset-1", 1)

    assert adapter.client.calls == 4


def make_package(root: Path, *, image_question: bool = False) -> Path:
    package = root / "package"
    (package / "documents").mkdir(parents=True)
    (package / "documents" / "doc-1.md").write_text("gold answer\n", encoding="utf-8")
    image_value = []
    if image_question:
        image = package / "documents" / "question.png"
        image.write_bytes(b"not-a-real-png-fixture")
        image_value = ["documents/question.png"]
    (package / "questions.jsonl").write_text(
        json.dumps(
            {
                "id": "q-1",
                "question": "What is the answer?",
                "document_id": "doc-1",
                "answer": "gold answer",
                "gold_document_ids": ["doc-1"],
                "gold_evidence": [{"evidence": "gold evidence"}],
                "images": image_value,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps(
            {
                "schema": "competitor-eval-ready-v1",
                "dataset": "mock-dataset",
                "revision": "mock-rev-1",
                "split": "test",
                "scope": "global",
                "protocol_tag": "ADAPTED_PROTOCOL",
                "documents": [{"id": "doc-1", "path": "documents/doc-1.md", "media": "markdown"}],
                "questions": "questions.jsonl",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package


def make_qa_package(root: Path, question_count: int = 3) -> Path:
    package = make_package(root)
    rows = [
        {
            "id": f"q-{index}",
            "question": f"What is answer {index}?",
            "document_id": "doc-1",
            "answer": "gold answer",
            "gold_document_ids": ["doc-1"],
        }
        for index in range(1, question_count + 1)
    ]
    (package / "questions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return package


def make_document_local_package(
    root: Path,
    *,
    scopes: int = 2,
    candidates_per_scope: int = 2,
    missing_candidate: bool = False,
) -> Path:
    package = root / "document-local-package"
    package.mkdir(parents=True)
    corpus_rows = []
    question_rows = []
    for scope_index in range(scopes):
        scope_id = f"bench:scope:{scope_index}"
        candidate_ids = []
        for candidate_index in range(candidates_per_scope):
            candidate_id = f"bench:candidate:{scope_index}:{candidate_index}"
            candidate_ids.append(candidate_id)
            row = {
                "doc_id": candidate_id,
                "scope_id": scope_id,
                "text_path": "corpus.jsonl",
                "binary_path": "never-upload-this.png",
                "media_type": "text/plain",
                "sha256": f"{scope_index + 1:064x}",
                "metadata": {
                    "ingest_role": "candidate_text",
                    "page_number": scope_index + 1,
                    "layout_id": candidate_index,
                },
            }
            if not (missing_candidate and scope_index == 0 and candidate_index == 0):
                row["content"] = f"content for {candidate_id}"
            corpus_rows.append(row)
        question_rows.append(
            {
                "question_id": f"q-{scope_index}",
                "question": "find the candidate",
                "scope_doc_ids": [scope_id if scope_index == 0 else candidate_ids[0]],
                "gold_doc_ids": [candidate_ids[-1]],
            }
        )
    (package / "corpus.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in corpus_rows), encoding="utf-8"
    )
    (package / "questions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in question_rows), encoding="utf-8"
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "competitor-eval-ready-v1",
                "dataset_id": "scope-fixture",
                "scope": "document_local",
                "condition": "controlled-candidates",
                "ingest_representation": "candidate_markdown",
                "artifacts": {"corpus.jsonl": "corpus.jsonl", "questions.jsonl": "questions.jsonl"},
            }
        ),
        encoding="utf-8",
    )
    return package


class NoNetworkHTTP:
    def __init__(self, *args, **kwargs):
        raise AssertionError("dry-run attempted to construct an HTTP client")


class FakeDifyAppBinding:
    calls: list[dict] = []

    def __init__(self, env, app_id, progress):
        self.env = env
        self.app_id = app_id
        self.progress = progress

    def _login(self):
        self.calls.append({"method": "LOGIN"})

    @staticmethod
    def _unwrap(payload):
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _request(self, method, path, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        if method == "POST" and path == "/console/api/apps":
            return {"id": "dify-app-new", "model_config": {}}
        if method == "POST" and path.endswith("/api-keys"):
            return {"id": "dify-key-new", "token": "dify-new-app-secret"}
        if method == "GET" and path == "/console/api/apps/dify-app-new":
            return {
                "model_config": {
                    "model": {
                        "provider": "qianfan-provider",
                        "name": "old-model",
                        "mode": "chat",
                        "completion_params": {},
                    },
                    "configs": {},
                    "dataset_configs": {"datasets": {"strategy": "router", "datasets": []}},
                }
            }
        if method == "POST" and path.endswith("/model-config"):
            return {"result": "success"}
        raise AssertionError(f"unexpected Dify console request: {method} {path} {body}")

    def bind(self, dataset_id, output):
        self.calls.append({"method": "BIND", "app_id": self.app_id, "dataset_id": dataset_id})
        return {"app_id": self.app_id, "bound_dataset_id": dataset_id}


class FakeHTTP:
    calls: list[dict] = []
    counter = 0

    def __init__(self, base_url, output, progress, timeout=120):
        self.base_url = base_url.rstrip("/")
        self.output = Path(output)
        self.progress = progress
        self.timeout = timeout

    def root(self):
        return self

    def _record(self, operation, request, response):
        type(self).counter += 1
        type(self).calls.append({"base_url": self.base_url, "operation": operation, "request": request})
        path = self.output / "http" / f"{type(self).counter:06d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', operation)}.json"
        runner.json_dump(path, {"operation": operation, "request": request, "response": response})

    def request(self, method, path, *, api_key=None, params=None, json_body=None, file_path=None, form=None, operation="request", timeout=None):
        run_root = self.output
        assert (run_root / "start-record.json").exists()
        request = {"method": method, "path": path, "params": params, "json": json_body, "api_key": api_key}
        if file_path is not None:
            request["file"] = Path(file_path).name
        if "qianfan.baidubce.com" in self.base_url:
            if path == "/models":
                response = {"data": [{"id": "deepseek-v4-flash"}, {"id": "qwen3.5-35b-a3b"}]}
            elif path == "/chat/completions":
                response = {"choices": [{"message": {"content": "OK"}}]}
            else:
                response = {}
        elif "modelarts-maas.com" in self.base_url:
            if path == "/models":
                response = {"data": [{"id": "bge-m3"}, {"id": "deepseek-v4-pro"}]}
            elif path == "/embeddings":
                response = {"data": [{"embedding": [0.0] * 1024}]}
            elif path == "/chat/completions":
                response = {"choices": [{"message": {"content": "OK"}}]}
            else:
                response = {}
        elif "api.deepseek.com" in self.base_url:
            if path == "/models":
                response = {"data": [{"id": "deepseek-v4-flash"}]}
            elif path == "/chat/completions":
                response = {"choices": [{"message": {"content": "OK"}}]}
            else:
                response = {}
        elif ":8010" in self.base_url:
            response = self.dify_response(method, path, json_body, file_path)
        elif "fastgpt" in self.base_url or ":3000" in self.base_url:
            response = self.fastgpt_response(method, path, json_body, file_path)
        elif "/admin/api" in self.base_url:
            response = self.maxkb_admin_response(method, path, json_body)
        elif "/chat/api" in self.base_url:
            response = {"choices": [{"message": {"content": "gold answer"}}]}
        else:
            response = {}
        self._record(operation, request, response)
        return response

    @staticmethod
    def dify_response(method, path, body, file_path):
        if method == "GET" and path == "/console/api/setup":
            return {"status": "finished"}
        if method == "GET" and path == "/workspaces/current/models/model-types/llm":
            return {"data": [{"provider": "test-maas-llm-provider", "models": [{"model": "deepseek-v4-flash"}]}]}
        if method == "GET" and path == "/workspaces/current/models/model-types/text-embedding":
            return {"data": [{"provider": "test-maas-embedding-provider", "models": [{"model": "bge-m3"}]}]}
        if method == "POST" and path == "/datasets":
            return {"id": "dify-dataset-new"}
        if method == "POST" and path.endswith("/document/create-by-file"):
            return {"document": {"id": "dify-document-new"}}
        if method == "GET" and path.endswith("/documents"):
            return {"data": [{"id": "dify-document-new", "indexing_status": "completed"}]}
        if method == "POST" and path.endswith("/retrieve"):
            return {"records": [{"segment": {"document": {"id": "doc-1"}, "content": "gold answer"}}]}
        if method == "POST" and path == "/chat-messages":
            return {"answer": "gold answer"}
        raise AssertionError(f"unexpected Dify fake request: {method} {path} {body} {file_path}")

    @staticmethod
    def fastgpt_response(method, path, body, file_path):
        if method == "GET" and path == "/":
            return {"ok": True}
        if path == "/api/core/dataset/create":
            return {"code": 200, "data": "fastgpt-dataset-new"}
        if path == "/api/core/dataset/collection/create":
            return {"code": 200, "data": {"id": "collection-1"}}
        if path == "/api/core/dataset/data/pushData":
            return {"code": 200, "data": {"accepted": True}}
        if path == "/api/core/dataset/collection/listV2":
            return {"code": 200, "data": {"list": [{"trainingAmount": 0, "activeTrainingAmount": 0, "finalErrorAmount": 0, "hasError": False}]}}
        if path == "/api/core/app/create":
            return {"code": 200, "data": {"id": "fastgpt-app-new"}}
        if path == "/api/support/openapi/create":
            return {"code": 200, "data": "fastgpt-new-app-secret"}
        if path == "/api/core/dataset/searchTest":
            return {"code": 200, "data": {"list": [{"sourceName": "doc-1", "q": "gold answer"}]}}
        if path == "/api/v1/chat/completions":
            return {"code": 200, "data": {"choices": [{"message": {"content": "gold answer"}}]}}
        raise AssertionError(f"unexpected FastGPT fake request: {method} {path} {body} {file_path}")

    @staticmethod
    def maxkb_admin_response(method, path, body):
        if method == "GET" and path == "/admin/":
            return {"ok": True}
        if method == "GET" and path == "/workspace/default/model":
            return {
                "data": [
                    {"id": "embedding-1", "model_name": "bge-m3", "name": "MaaS bge-m3", "provider": "model_openai_provider"},
                    {"id": "chat-1", "model_name": "deepseek-v4-flash", "name": "MaaS deepseek-v4-flash", "provider": "model_openai_provider"},
                ]
            }
        if method == "POST" and path == "/workspace/default/knowledge/base":
            return {"data": {"id": "knowledge-1"}}
        if method == "PUT" and path.endswith("/document/batch_create"):
            return {"data": [{"id": "document-1"}]}
        if method == "GET" and "/document/document-1" in path:
            return {"status": "nnn2"}
        if method == "POST" and path.endswith("/hit_test"):
            return {"data": [{"document_id": "doc-1", "content": "gold answer"}]}
        if method == "POST" and path == "/workspace/default/application":
            return {"data": {"id": "maxkb-app-new"}}
        if method == "PUT" and path == "/workspace/default/application/maxkb-app-new/publish":
            return {"data": {"is_publish": True}}
        if method == "POST" and path == "/workspace/default/application/maxkb-app-new/application_key":
            return {"data": {"secret_key": "maxkb-new-app-secret"}}
        raise AssertionError(f"unexpected MaxKB fake request: {method} {path} {body}")


class UnsupportedFastGPTAppHTTP(FakeHTTP):
    def request(self, method, path, **kwargs):
        if path == "/api/core/app/create":
            raise runner.ContractUnsupported("FASTGPT_APP_CREATE_HTTP_404")
        return super().request(method, path, **kwargs)


class ScopeDifyHTTP(FakeHTTP):
    """Deterministic Dify fake for the document-local ingest state machine."""

    calls: list[dict] = []
    counter = 0
    dataset_counter = 0
    fail_dataset = ""
    indexing_ready = True

    @classmethod
    def reset_scope_state(cls) -> None:
        cls.calls = []
        cls.counter = 0
        cls.dataset_counter = 0
        cls.fail_dataset = ""
        cls.indexing_ready = True

    def dify_response(self, method, path, body, file_path):
        if method == "GET" and path == "/console/api/setup":
            return {"status": "finished"}
        if method == "GET" and path == "/workspaces/current/models/model-types/llm":
            return {"data": [{"provider": "test-maas-llm-provider", "models": [{"model": "deepseek-v4-flash"}]}]}
        if method == "GET" and path == "/workspaces/current/models/model-types/text-embedding":
            return {"data": [{"provider": "test-maas-embedding-provider", "models": [{"model": "bge-m3"}]}]}
        if method == "POST" and path == "/datasets":
            type(self).dataset_counter += 1
            return {"id": f"dify-scope-dataset-{type(self).dataset_counter}"}
        if method == "POST" and path.endswith("/document/create-by-file"):
            dataset_id = path.split("/")[2]
            if dataset_id == type(self).fail_dataset:
                raise runner.EvalError(f"DIFY_UPLOAD_FAILED:{dataset_id}")
            return {"document": {"id": f"{dataset_id}-document"}}
        if method == "GET" and path.endswith("/documents"):
            dataset_id = path.split("/")[2]
            status = "completed" if type(self).indexing_ready else "indexing"
            return {"data": [{"id": f"{dataset_id}-document", "indexing_status": status}]}
        if method == "POST" and path.endswith("/retrieve"):
            return {"records": [{"segment": {"document": {"id": "doc-1"}, "content": "gold answer"}}]}
        if method == "POST" and path == "/chat-messages":
            return {"answer": "gold answer"}
        raise AssertionError(f"unexpected scoped Dify fake request: {method} {path} {body} {file_path}")


class ConcurrentMaxKBHTTP(FakeHTTP):
    """MaxKB double for resource-level concurrency and checkpoint tests."""

    calls: list[dict] = []
    counter = 0
    knowledge_counter = 0
    active_batch = 0
    max_active_batch = 0
    lock = threading.Lock()
    fail_readiness_remaining = 0

    @classmethod
    def reset_maxkb_state(cls) -> None:
        cls.calls = []
        cls.counter = 0
        cls.knowledge_counter = 0
        cls.active_batch = 0
        cls.max_active_batch = 0
        cls.fail_readiness_remaining = 0

    def request(self, method, path, **kwargs):
        if method == "PUT" and path.endswith("/document/batch_create"):
            with type(self).lock:
                type(self).active_batch += 1
                type(self).max_active_batch = max(type(self).max_active_batch, type(self).active_batch)
            try:
                time.sleep(0.01)
                return super().request(method, path, **kwargs)
            finally:
                with type(self).lock:
                    type(self).active_batch -= 1
        return super().request(method, path, **kwargs)

    @classmethod
    def maxkb_admin_response(cls, method, path, body):
        if method == "POST" and path == "/workspace/default/knowledge/base":
            with cls.lock:
                cls.knowledge_counter += 1
                knowledge_id = f"knowledge-{cls.knowledge_counter}"
            return {"data": {"id": knowledge_id}}
        if method == "PUT" and path.endswith("/document/batch_create"):
            knowledge_id = path.split("/")[4]
            return {"data": [{"id": f"{knowledge_id}-document"}]}
        if method == "GET" and "/document/" in path:
            with cls.lock:
                should_fail = cls.fail_readiness_remaining > 0
                if should_fail:
                    cls.fail_readiness_remaining -= 1
            if should_fail:
                return {"status": "nnn3"}
            return {"status": "nnn2"}
        if method == "POST" and path == "/workspace/default/application":
            knowledge_id = body["knowledge_id_list"][0]
            return {"data": {"id": f"app-{knowledge_id}"}}
        if method == "PUT" and path.endswith("/publish"):
            return {"data": {"is_publish": True}}
        if method == "POST" and path.endswith("/application_key"):
            app_id = path.split("/")[-2]
            return {"data": {"secret_key": f"secret-{app_id}"}}
        return FakeHTTP.maxkb_admin_response(method, path, body)


class ConcurrentFastGPTHTTP(FakeHTTP):
    """FastGPT double for resource-level concurrency and resume checkpoints."""

    calls: list[dict] = []
    counter = 0
    dataset_counter = 0
    active_collection_create = 0
    max_active_collection_create = 0
    lock = threading.Lock()
    fail_dataset_scope = ""
    stall_dataset_scope = ""
    stall_dataset_ids: set[str] = set()
    stalled_dataset_ids_seen: set[str] = set()
    collection_barrier: threading.Barrier | None = None
    collection_barrier_waiters = 0

    @classmethod
    def reset_fastgpt_state(cls) -> None:
        cls.calls = []
        cls.counter = 0
        cls.dataset_counter = 0
        cls.active_collection_create = 0
        cls.max_active_collection_create = 0
        cls.fail_dataset_scope = ""
        cls.stall_dataset_scope = ""
        cls.stall_dataset_ids = set()
        cls.stalled_dataset_ids_seen = set()
        cls.collection_barrier = None
        cls.collection_barrier_waiters = 0

    def request(self, method, path, **kwargs):
        if method == "POST" and path == "/api/core/dataset/collection/create":
            with type(self).lock:
                type(self).active_collection_create += 1
                type(self).max_active_collection_create = max(
                    type(self).max_active_collection_create,
                    type(self).active_collection_create,
                )
            try:
                barrier = None
                if type(self).collection_barrier_waiters > 0:
                    barrier = type(self).collection_barrier
                    type(self).collection_barrier_waiters -= 1
                if barrier is not None:
                    barrier.wait(timeout=2)
                time.sleep(0.01)
                return super().request(method, path, **kwargs)
            finally:
                with type(self).lock:
                    type(self).active_collection_create -= 1
        return super().request(method, path, **kwargs)

    @classmethod
    def fastgpt_response(cls, method, path, body, file_path):
        if method == "POST" and path == "/api/core/dataset/create":
            name = str((body or {}).get("name", ""))
            if cls.fail_dataset_scope and cls.fail_dataset_scope in name:
                return {"code": 500, "message": "simulated dataset failure"}
            with cls.lock:
                cls.dataset_counter += 1
                dataset_id = f"fastgpt-dataset-{cls.dataset_counter}"
                if cls.stall_dataset_scope and cls.stall_dataset_scope in name:
                    cls.stall_dataset_ids.add(dataset_id)
            return {"code": 200, "data": dataset_id}
        if method == "POST" and path == "/api/core/dataset/collection/create":
            dataset_id = str((body or {}).get("datasetId", ""))
            return {"code": 200, "data": {"id": f"{dataset_id}-collection"}}
        if method == "POST" and path == "/api/core/dataset/data/pushData":
            return {"code": 200, "data": {"accepted": True}}
        if method == "POST" and path == "/api/core/dataset/collection/listV2":
            dataset_id = str((body or {}).get("datasetId", ""))
            if dataset_id in cls.stall_dataset_ids and dataset_id not in cls.stalled_dataset_ids_seen:
                cls.stalled_dataset_ids_seen.add(dataset_id)
                raise runner.EvalError("FASTGPT_INDEX_TIMEOUT: simulated readiness failure")
            return {
                "code": 200,
                "data": {
                    "list": [
                        {
                            "_id": f"{dataset_id}-collection",
                            "trainingAmount": 0,
                            "activeTrainingAmount": 0,
                            "finalErrorAmount": 0,
                            "hasError": False,
                            "dataAmount": 1,
                        }
                    ]
                },
            }
        if method == "POST" and path == "/api/core/app/create":
            return {"code": 200, "data": {"id": f"fastgpt-app-{cls.dataset_counter}"}}
        return FakeHTTP.fastgpt_response(method, path, body, file_path)


class ConcurrentArtifactHTTP:
    """HTTP double whose ArtifactHTTP save hook would race without protection."""

    def __init__(self, base_url, output, progress, timeout=120):
        self.base_url = base_url
        self.output = Path(output)
        self.counter = 0

    def _save_http(self, operation, request, response, error=None):
        current = self.counter
        time.sleep(0.002)
        self.counter = current + 1
        runner.json_dump(
            self.output / "http" / f"{self.counter:06d}-{operation}.json",
            {"operation": operation, "request": request, "response": response, "error": error},
        )

    def request(self, method, path, *, operation="request", **kwargs):
        self._save_http(operation, {"method": method, "path": path, **kwargs}, {"ok": True})
        return {"ok": True}
@pytest.fixture(autouse=True)
def reset_fake_http(monkeypatch):
    FakeHTTP.calls = []
    FakeHTTP.counter = 0
    FakeDifyAppBinding.calls = []
    ScopeDifyHTTP.reset_scope_state()
    ConcurrentMaxKBHTTP.reset_maxkb_state()
    ConcurrentFastGPTHTTP.reset_fastgpt_state()
    # Isolate unit tests from the shared root .env's stale Dify selectors;
    # live readiness tests set their discovered provider IDs explicitly.
    monkeypatch.setenv("DIFY_QIANFAN_LLM_PROVIDER", "")
    monkeypatch.setenv("DIFY_QIANFAN_EMBEDDING_PROVIDER", "")
    monkeypatch.setenv("DIFY_DEEPSEEK_LLM_PROVIDER", "test-maas-llm-provider")
    monkeypatch.setenv("DIFY_MAAS_EMBEDDING_PROVIDER", "test-maas-embedding-provider")
    monkeypatch.setenv("COMPETITOR_EMBEDDING_PROVIDER", "maas")
    yield


def set_provider_keys(monkeypatch):
    monkeypatch.setenv("QIANFAN_API_KEY", "qianfan-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY_NEW", "deepseek-secret")
    monkeypatch.setenv("MAAS_API_KEY", "maas-secret")


def test_all_reconciles_transient_ingest_failure_when_later_stage_makes_resource_ready(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)

    def fake_preflight(self):
        return {"status": "READY", "ready": True}

    def fake_ingest(self):
        payload = self.load_resources()
        for resource in payload["resources"].values():
            resource.update({"status": "failed", "error": "TASK_BUSY"})
        self.save_resources(payload)
        return {"status": "PARTIAL", "resource_counts": {"FAILED": 1}}

    def fake_retrieval(self):
        payload = self.load_resources()
        for resource in payload["resources"].values():
            resource.update({"status": "ready", "ready": True})
            resource.pop("error", None)
        self.save_resources(payload)
        return {"status": "SUCCESS", "terminal_counts": {"SUCCESS": 1}}

    monkeypatch.setattr(runner.RunnerContext, "preflight", fake_preflight)
    monkeypatch.setattr(runner.RunnerContext, "ingest", fake_ingest)
    monkeypatch.setattr(runner.RunnerContext, "retrieval", fake_retrieval)
    monkeypatch.setattr(runner.RunnerContext, "qa", lambda self: {"status": "SUCCESS"})

    status = runner.main(
        [
            "all",
            "--system",
            "maxkb_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "recovered-ingest",
        ]
    )

    assert status == 0
    summary = json.loads((tmp_path / "runs" / "recovered-ingest" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "SUCCESS"
    assert summary["ingest"]["status"] == "SUCCESS"
    assert summary["ingest"]["resource_counts"] == {"READY": 1}
    assert summary["ingest"]["reconciled_from_resource_map"] is True


def test_qa_command_resumes_incomplete_ingest_before_answering(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=2, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    ingest_calls = []

    class FakeAdapter:
        def __init__(self, context):
            self.context = context

        def ingest(self):
            ingest_calls.append("ingest")
            payload = self.context.load_resources()
            for resource in payload["resources"].values():
                resource.update({"status": "ready", "ready": True})
            self.context.save_resources(payload)
            return {"status": "SUCCESS", "resource_counts": {"READY": 2}}

        def qa(self, question, resource):
            return {"answer": "gold answer", "contract": "native_qa"}

    def fake_preflight(self):
        payload = self.load_resources()
        resources = list(payload["resources"].values())
        resources[0].update({"status": "ready", "ready": True})
        resources[1].update({"status": "failed", "error": "TRANSIENT_INDEX_FAILURE"})
        self.save_resources(payload)
        return {"status": "READY", "ready": True}

    monkeypatch.setattr(runner.RunnerContext, "preflight", fake_preflight)
    monkeypatch.setattr(runner.RunnerContext, "adapter", lambda self: FakeAdapter(self))

    status = runner.main(
        [
            "qa",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "qa-resumes-ingest",
        ]
    )

    assert status == 0
    assert ingest_calls == ["ingest"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "runs" / "qa-resumes-ingest" / "terminal-ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["status"] for row in rows if row["stage"] == "qa"] == ["SUCCESS", "SUCCESS"]


def test_dry_run_writes_immutable_artifacts_before_any_http(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)

    status = runner.main(
        [
            "preflight",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "dry-run",
            "--dry-run",
        ]
    )

    assert status == 0
    root = tmp_path / "runs" / "dry-run"
    start = json.loads((root / "start-record.json").read_text(encoding="utf-8"))
    assert start["status_at_start"] == "not_started"
    assert start["provider"]["llm"] == "deepseek-official/deepseek-v4-flash"
    assert start["provider"]["image_llm"] == "NOT_APPLICABLE"
    assert start["provider"]["embedding"] == "maas/bge-m3/1024"
    assert start["provider"]["taas_used"] is False
    assert start["benchmark_scope"]["evaluation_order"] == [
        "WikiEval",
        "MMDocIR",
        "MMDocRAG",
        "DocBench",
        "MultiHop-RAG",
        "EnterpriseRAG-Bench",
        "FAB-Bench",
    ]
    assert start["benchmark_scope"]["paper_full_corpus_required"] is False
    assert "OmniDocBench" in start["benchmark_scope"]["excluded_datasets"]
    assert "Lenovo-bench" in start["benchmark_scope"]["excluded_datasets"]
    assert (root / "start-record.json.sha256").exists()
    assert (root / "initial-ledger.jsonl").exists()
    assert (root / "initial-ledger.jsonl.sha256").exists()
    assert len(runner._read_jsonl(root / "initial-ledger.jsonl")) == 1
    assert not (root / "providers" / "probe.json").read_text(encoding="utf-8").find("SKIPPED") == -1


def test_qa_start_record_freezes_quality_only_metric_contract(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setattr(runner.RunnerContext, "adapter", lambda self: object())

    status = runner.main(
        [
            "qa",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "qa-quality-only",
            "--dry-run",
        ]
    )

    assert status == 0
    start = json.loads((tmp_path / "runs" / "qa-quality-only" / "start-record.json").read_text(encoding="utf-8"))
    contract = start["metric_contract"]
    assert contract["evaluation_scope"] == "native_qa_quality_only"
    assert contract["direct_retrieval"] == "NOT_RUN"
    assert contract["latency"] == []
    assert "token_f1" in contract["primary"]


def test_non_maas_embedding_selection_is_rejected(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)
    monkeypatch.setenv("COMPETITOR_EMBEDDING_PROVIDER", "qianfan")
    monkeypatch.setenv("QIANFAN_EMBEDDING_MODEL", "qwen3-embedding-8b")
    monkeypatch.setenv("QIANFAN_EMBEDDING_DIMENSION", "4096")

    assert runner.main([
        "preflight", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "qianfan-embedding", "--dry-run",
    ]) == 2


def test_fastgpt_rejects_non_glm_maas_model(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setenv("FASTGPT_LLM_PROVIDER", "maas")
    monkeypatch.setenv("DEEPSEEK_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)

    assert runner.main([
        "preflight", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maas-dsv4p", "--dry-run",
    ]) == 2


def test_maas_text_llm_is_rejected_for_multimodal_questions(tmp_path, monkeypatch):
    package = make_package(tmp_path, image_question=True)
    monkeypatch.setenv("FASTGPT_LLM_PROVIDER", "maas")
    monkeypatch.setenv("DEEPSEEK_LLM_MODEL", "deepseek-v4-pro")

    status = runner.main([
        "preflight", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maas-dsv4p-vl", "--dry-run",
    ])
    assert status == 2


def test_dify_credentials_aliases_are_loaded_without_leaking_secret(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setenv("DIFY_LOCAL_API_KEY", "dify-native-secret")
    monkeypatch.setenv("DIFY_LOCAL_APP_ID", "dify-app")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-secret")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_ID", "configured-dataset")
    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)

    context = runner.RunnerContext(
        type(
            "Args",
            (),
            {
                "system": "dify_local",
                "package": package,
                "output_root": tmp_path / "runs",
                "run_id": "dify-aliases",
                "dry_run": True,
                "repeats": 1,
                "top_k": 10,
                "poll_seconds": 0,
                "service_timeout": 1,
                "provider_timeout": 1,
                "upload_timeout": 1,
                "index_timeout": 1,
                "query_timeout": 1,
                "qa_timeout": 1,
                "qianfan_base_url": "",
                "maas_base_url": "",
                "qianfan_llm_model": "",
                "qianfan_image_llm_model": "",
                "maas_embedding_model": "",
                "maas_embedding_dimension": 0,
            },
        )()
        ,
        runner.EvalPackage.load(package),
    )
    assert context.env["DIFY_LOCAL_DATASET_ID"] == "configured-dataset"
    start_text = (context.root / "start-record.json").read_text(encoding="utf-8")
    assert "dify-native-secret" not in start_text
    assert "dify-dataset-secret" not in start_text
    assert context.profiles["llm"].name == "deepseek-official"
    assert context.profiles["llm"].model == "deepseek-v4-flash"


def test_dify_all_creates_fresh_dataset_and_qa_app_bound_to_it(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_ID", "dify-dataset-old")
    monkeypatch.setenv("DIFY_LOCAL_API_KEY", "dify-old-app-secret")
    monkeypatch.setenv("DIFY_LOCAL_APP_ID", "dify-app-old")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_PASSWORD", "console-secret")
    monkeypatch.setenv("DIFY_DEEPSEEK_LLM_PROVIDER", "test-maas-llm-provider")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "dify-isolated",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    assert status == 0
    run_root = tmp_path / "runs" / "dify-isolated"
    resource = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    assert resource["dataset_id"] == "dify-dataset-new"
    assert resource["dataset_id"] != "dify-dataset-old"
    assert resource["app_id"] == "dify-app-new"
    assert resource["native_app"]["bound_resource_id"] == "dify-dataset-new"
    secret_path = run_root / resource["app_key_secret_path"]
    assert secret_path.read_text(encoding="utf-8").strip() == "dify-new-app-secret"
    assert secret_path.stat().st_mode & 0o777 == 0o600
    qa_call = next(call for call in FakeHTTP.calls if call["operation"].startswith("dify-qa-"))
    assert qa_call["request"]["api_key"] == "dify-new-app-secret"
    assert "dify-old-app-secret" not in (run_root / "resource-map.json").read_text(encoding="utf-8")
    assert all("dify-new-app-secret" not in path.read_text(encoding="utf-8") for path in run_root.rglob("*.json"))
    assert {call.get("app_id") for call in FakeDifyAppBinding.calls if call["method"] == "BIND"} == {"dify-app-new"}
    configure_call = next(
        call
        for call in FakeDifyAppBinding.calls
        if call["method"] == "POST" and call["path"].endswith("/model-config")
    )
    assert configure_call["body"]["model"] == {
        "provider": "test-maas-llm-provider",
        "name": "deepseek-v4-flash",
        "mode": "chat",
        "completion_params": {"thinking": False, "max_tokens": 1024},
    }
    assert configure_call["body"]["pre_prompt"] == runner.SHARED_PROMPT_TEXT


def test_dify_explicit_configured_resource_reuse_does_not_upload(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setenv("DIFY_LOCAL_DATASET_ID", "configured-dataset")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_PASSWORD", "console-secret")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "qa",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "dify-reuse",
            "--reuse-configured-resource",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    assert status == 0
    assert not any(call["operation"].startswith("dify-upload-") for call in FakeHTTP.calls)
    resource = json.loads(
        (tmp_path / "runs/dify-reuse/resource-map.json").read_text(encoding="utf-8")
    )["resources"][runner.GLOBAL_RESOURCE]
    assert resource["dataset_id"] == "configured-dataset"
    assert resource["reused_without_upload"] is True
    assert resource["uploads"]["doc-1"]["origin"] == "configured_resource"


def test_fastgpt_all_keeps_terminal_rows_and_uses_mock_http(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-secret")
    monkeypatch.setenv("FASTGPT_APP_ID", "fastgpt-app")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "fastgpt_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "fastgpt-mock",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )
    assert status == 0
    root = tmp_path / "runs" / "fastgpt-mock"
    rows = runner._read_jsonl(root / "terminal-ledger.jsonl")
    assert {(row["stage"], row["status"]) for row in rows} == {("retrieval", "SUCCESS"), ("qa", "SUCCESS")}
    assert json.loads((root / "retrieval-metrics.json").read_text(encoding="utf-8"))["planned_n"] == 1
    assert json.loads((root / "qa-metrics.json").read_text(encoding="utf-8"))["answer_contains_gold_rate"] == 1.0
    assert all("fastgpt-secret" not in path.read_text(encoding="utf-8") for path in (root / "http").glob("*.json"))
    assert any(call["operation"].startswith("fastgpt-push-data") for call in FakeHTTP.calls)
    assert any(call["operation"].startswith("provider-maas") for call in FakeHTTP.calls)
    retrieval_call = next(call for call in FakeHTTP.calls if call["operation"].startswith("fastgpt-retrieval"))
    assert retrieval_call["request"]["json"]["limit"] == 20000


def test_fastgpt_all_ignores_old_resource_and_qa_uses_new_bound_app_key(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-admin-secret")
    monkeypatch.setenv("FASTGPT_APP_ID", "fastgpt-app-old")
    monkeypatch.setenv("FASTGPT_DATASET_ID", "fastgpt-dataset-old")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "fastgpt_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "fastgpt-isolated",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    assert status == 0
    run_root = tmp_path / "runs" / "fastgpt-isolated"
    resource = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    assert resource["dataset_id"] == "fastgpt-dataset-new"
    assert resource["dataset_id"] != "fastgpt-dataset-old"
    assert resource["app_id"] == "fastgpt-app-new"
    assert resource["native_app"]["bound_resource_id"] == "fastgpt-dataset-new"
    create_app = next(call for call in FakeHTTP.calls if call["operation"].startswith("fastgpt-create-app-"))
    assert "fastgpt-dataset-new" in json.dumps(create_app["request"]["json"])
    qa_call = next(call for call in FakeHTTP.calls if call["operation"].startswith("fastgpt-qa-"))
    assert qa_call["request"]["json"]["appId"] == "fastgpt-app-new"
    assert qa_call["request"]["api_key"] == "fastgpt-admin-secret"
    assert qa_call["request"]["json"]["appId"] != "fastgpt-app-old"
    secret_path = run_root / resource["app_key_secret_path"]
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert resource["native_app"]["key_origin"] == "configured_team_openapi_key"
    assert not any(call["operation"].startswith("fastgpt-create-app-key-") for call in FakeHTTP.calls)
    assert "fastgpt-admin-secret" not in (run_root / "resource-map.json").read_text(encoding="utf-8")
    assert all("fastgpt-admin-secret" not in path.read_text(encoding="utf-8") for path in run_root.rglob("*.json"))


def test_same_run_resume_reuses_resource_map_app_and_secret(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-admin-secret")
    monkeypatch.setenv("FASTGPT_DATASET_ID", "fastgpt-dataset-old")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    argv = [
        "all",
        "--system",
        "fastgpt_local",
        "--package",
        str(package),
        "--output-root",
        str(tmp_path / "runs"),
        "--run-id",
        "fastgpt-resume",
        "--index-timeout",
        "1",
        "--poll-seconds",
        "0",
    ]

    assert runner.main(argv) == 0
    run_root = tmp_path / "runs" / "fastgpt-resume"
    before = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    create_operations = {
        "fastgpt-create-dataset-",
        "fastgpt-create-app-",
        "fastgpt-create-app-key-",
    }
    before_counts = {
        prefix: sum(call["operation"].startswith(prefix) for call in FakeHTTP.calls)
        for prefix in create_operations
    }

    assert runner.main(argv) == 0
    after = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    after_counts = {
        prefix: sum(call["operation"].startswith(prefix) for call in FakeHTTP.calls)
        for prefix in create_operations
    }
    assert after["dataset_id"] == before["dataset_id"] == "fastgpt-dataset-new"
    assert after["app_id"] == before["app_id"] == "fastgpt-app-new"
    assert after["app_key_secret_path"] == before["app_key_secret_path"]
    assert after_counts == before_counts


def test_same_run_rejects_noncanonical_fastgpt_provider_adjustment(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-admin-secret")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    argv = [
        "all", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "fastgpt-provider-adjustment",
        "--index-timeout", "1", "--poll-seconds", "0",
    ]

    assert runner.main(argv) == 0
    first_app_creates = sum(call["operation"].startswith("fastgpt-create-app-") for call in FakeHTTP.calls)
    first_dataset_creates = sum(call["operation"].startswith("fastgpt-create-dataset-") for call in FakeHTTP.calls)

    monkeypatch.setenv("FASTGPT_LLM_PROVIDER", "maas")
    monkeypatch.setenv("DEEPSEEK_LLM_MODEL", "deepseek-v4-pro")
    assert runner.main(argv + ["--text-llm-provider", "deepseek-official", "--deepseek-llm-model", "deepseek-v4-pro"]) == 2
    assert first_app_creates >= 1
    assert first_dataset_creates >= 1


def test_native_app_creation_unsupported_is_terminal_not_historical_fallback(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-admin-secret")
    monkeypatch.setenv("FASTGPT_APP_ID", "fastgpt-app-old")
    monkeypatch.setattr(runner, "ArtifactHTTP", UnsupportedFastGPTAppHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "fastgpt_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "fastgpt-app-unsupported",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    assert status == 0
    run_root = tmp_path / "runs" / "fastgpt-app-unsupported"
    resource = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    assert resource["status"] == "ready"
    assert resource["native_app"]["status"] == "unsupported"
    assert "app_id" not in resource
    qa = next(row for row in runner._read_jsonl(run_root / "terminal-ledger.jsonl") if row["stage"] == "qa")
    assert qa["status"] == "UNSUPPORTED"
    assert "fastgpt-app-old" not in json.dumps(FakeHTTP.calls)


def test_maxkb_admin_hit_test_is_diagnostic_and_public_retrieval_unsupported(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_APP_KEY", "maxkb-app-secret")
    monkeypatch.setenv("MAXKB_APPLICATION_ID", "app-1")
    monkeypatch.setenv("MAXKB_OPENAI_BASE_URL", "http://127.0.0.1:8090/chat/api/app-1")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "maxkb_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "maxkb-mock",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )
    assert status == 0
    root = tmp_path / "runs" / "maxkb-mock"
    rows = runner._read_jsonl(root / "terminal-ledger.jsonl")
    retrieval = next(row for row in rows if row["stage"] == "retrieval")
    qa = next(row for row in rows if row["stage"] == "qa")
    assert retrieval["status"] == "SUCCESS"
    assert retrieval["retrieval_contract"] == "diagnostic_admin_contract"
    assert retrieval["hit_count"] == 1
    assert qa["status"] == "SUCCESS"
    assert "maxkb-admin-hit-test" in " ".join(call["operation"] for call in FakeHTTP.calls)


def test_maxkb_all_uses_external_generation_and_ignores_historical_app(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_APP_KEY", "maxkb-old-app-secret")
    monkeypatch.setenv("MAXKB_APPLICATION_ID", "maxkb-app-old")
    monkeypatch.setenv("MAXKB_OPENAI_BASE_URL", "http://127.0.0.1:8090/chat/api/maxkb-app-old")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    status = runner.main(
        [
            "all",
            "--system",
            "maxkb_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "maxkb-isolated",
            "--index-timeout",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    assert status == 0
    run_root = tmp_path / "runs" / "maxkb-isolated"
    resource = json.loads((run_root / "resource-map.json").read_text(encoding="utf-8"))["resources"][runner.GLOBAL_RESOURCE]
    assert resource["knowledge_id"] == "knowledge-1"
    assert resource["native_app"]["status"] == "unsupported"
    assert resource["native_app"]["contract"] == "external_generation_from_maxkb_retrieval"
    qa_call = next(call for call in FakeHTTP.calls if call["operation"].startswith("maxkb-external-maas-qa-"))
    assert qa_call["request"]["path"] == "/chat/completions"
    assert qa_call["request"]["json"]["model"] == "deepseek-v4-flash"
    assert qa_call["request"]["json"]["thinking"] == {"type": "disabled"}
    assert "maxkb-app-old" not in json.dumps(qa_call["request"])


def test_actual_ready_v1_wikieval_uses_corpus_questions_and_gold_without_documents_dir(tmp_path, monkeypatch):
    package = HERE.parents[2] / ".local-services" / "competitor-eval-ready" / "v1" / "wikieval"
    assert package.is_dir(), "the checked-in local ready-v1 package is required for this regression"
    assert (package / "corpus.jsonl").is_file()
    assert (package / "questions.jsonl").is_file()
    assert (package / "gold.jsonl").is_file()
    assert not (package / "documents").exists()

    loaded = runner.EvalPackage.load(package)
    assert loaded.dataset == "wikieval"
    assert loaded.scope == "global"
    assert len(loaded.documents) == 50
    assert len(loaded.questions) == 50
    assert loaded.documents[0].raw["doc_id"] == loaded.documents[0].document_id
    assert loaded.documents[0].package_path is not None
    assert loaded.documents[0].package_path.is_absolute()
    assert loaded.questions[0].answer
    assert runner._gold_document_ids(loaded.questions[0]) == {"pslv-c56-916ba90c32.md"}

    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)
    status = runner.main(
        [
            "preflight",
            "--system",
            "dify_local",
            "--package",
            str(package),
            "--output-root",
            str(tmp_path / "runs"),
            "--run-id",
            "actual-wikieval",
            "--dry-run",
        ]
    )
    assert status == 0
    run_root = tmp_path / "runs" / "actual-wikieval"
    start = json.loads((run_root / "start-record.json").read_text(encoding="utf-8"))
    assert start["planned"]["files"] == 50
    assert start["planned"]["questions"] == 50
    assert json.loads((run_root / "preflight-status.json").read_text(encoding="utf-8"))["status"] == "DRY_RUN"
    assert len(runner._read_jsonl(run_root / "initial-ledger.jsonl")) == 50
    assert not list((run_root / "source").iterdir()), "ready-v1 source paths are used in place"


def test_document_local_scope_ids_group_candidates_and_resource_for_uses_scope(tmp_path, monkeypatch):
    package_path = make_document_local_package(tmp_path, scopes=2, candidates_per_scope=3)
    loaded = runner.EvalPackage.load(package_path)

    assert len(loaded.target_documents()) == 2
    assert {key: len(documents) for key, documents in loaded.target_documents()} == {
        "bench:scope:0": 3,
        "bench:scope:1": 3,
    }
    assert loaded.questions[0].scope_ids == ["bench:scope:0"]
    assert loaded.questions[1].scope_ids == ["bench:scope:1"], "a candidate doc_id resolves to its shared scope"
    assert len(loaded.required_document_ids(loaded.questions[0])) == 3
    assert runner._gold_document_ids(loaded.questions[0]) == {"bench:candidate:0:2"}

    context = object.__new__(runner.RunnerContext)
    context.package = loaded
    resource = {"resource_id": "resource-for-scope-1"}
    key, selected = runner.RunnerContext.resource_for(context, loaded.questions[1], {"bench:scope:1": resource})
    assert key == "bench:scope:1"
    assert selected is resource

    monkeypatch.setattr(runner, "ArtifactHTTP", NoNetworkHTTP)
    assert runner.main([
        "preflight", "--system", "dify_local", "--package", str(package_path),
        "--output-root", str(tmp_path / "runs"), "--run-id", "scope-materialization", "--dry-run",
    ]) == 0
    run_root = tmp_path / "runs" / "scope-materialization"
    markdown_files = sorted((run_root / "source").glob("*-candidates.md"))
    assert len(markdown_files) == 2, "one deterministic upload artifact is materialized per scope"
    materialized = markdown_files[0].read_text(encoding="utf-8")
    assert "competitor-eval-candidate:" in materialized
    assert '"doc_id":"bench:candidate:0:2"' in materialized
    assert "corpus.jsonl" not in {item["path"] for item in json.loads((run_root / "resource-map.json").read_text())["resources"]["bench:scope:0"]["materialized_artifacts"]}
    metrics = runner._retrieval_metrics(loaded.questions[0], [{"content": materialized}], 10)
    assert metrics["evidence_recall"] == 1.0


def test_missing_inline_candidate_is_preserved_as_terminal_unsupported(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=1, candidates_per_scope=2, missing_candidate=True)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setattr(runner, "ArtifactHTTP", FakeHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)

    assert runner.main([
        "all", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "missing-candidate",
        "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0
    rows = runner._read_jsonl(tmp_path / "runs" / "missing-candidate" / "terminal-ledger.jsonl")
    assert {(row["stage"], row["status"]) for row in rows} == {
        ("retrieval", "UNSUPPORTED"),
        ("qa", "UNSUPPORTED"),
    }
    resource = json.loads((tmp_path / "runs" / "missing-candidate" / "resource-map.json").read_text())["resources"]["bench:scope:0"]
    assert resource["unsupported_document_count"] == 1
    upload_calls = [call for call in FakeHTTP.calls if call["operation"].startswith("dify-upload-")]
    assert len(upload_calls) == 1
    assert upload_calls[0]["request"]["file"].endswith("-candidates.md")


def test_dify_generic_compat_adapter_requires_explicit_allow_flag():
    with pytest.raises(runner.RunnerError, match="FROZEN_PROVIDER_REQUIRED"):
        runner._reject_dify_adapters([runner.DIFY_GENERIC_OAI_ADAPTER], allow_generic_compat=False)

    runner._reject_dify_adapters([runner.DIFY_GENERIC_OAI_ADAPTER], allow_generic_compat=True)

    with pytest.raises(runner.RunnerError, match="FROZEN_PROVIDER_REQUIRED"):
        runner._reject_dify_adapters(["https://token.moi.matrixorigin.cn/v1"], allow_generic_compat=True)

    runner._reject_dify_adapters(
        ["matrixorigin/matrixorigin_huawei_maas/huawei_maas"],
        allow_generic_compat=True,
    )


def test_source_document_representation_deduplicates_pdf_and_missing_pdf_is_not_a_package_blocker(tmp_path):
    package = tmp_path / "source-pdf-package"
    package.mkdir()
    pdf = package / "source.pdf"
    pdf.write_bytes(b"%PDF fixture")
    corpus = [
        {"doc_id": "source-a", "scope_id": "scope-a", "binary_path": "source.pdf", "media_type": "application/pdf", "metadata": {"ingest_role": "source_document"}},
        {"doc_id": "source-a-alias", "scope_id": "scope-a", "binary_path": "source.pdf", "media_type": "application/pdf", "metadata": {"ingest_role": "source_document"}},
        {"doc_id": "source-b", "scope_id": "scope-b", "binary_path": "missing.pdf", "media_type": "application/pdf", "metadata": {"ingest_role": "source_document"}},
    ]
    (package / "corpus.jsonl").write_text("".join(json.dumps(row) + "\n" for row in corpus))
    (package / "questions.jsonl").write_text(
        json.dumps({"question_id": "q-a", "question": "a", "scope_doc_ids": ["scope-a"], "gold_doc_ids": ["source-a"]}) + "\n" +
        json.dumps({"question_id": "q-b", "question": "b", "scope_doc_ids": ["scope-b"], "gold_doc_ids": ["source-b"]}) + "\n"
    )
    (package / "manifest.json").write_text(json.dumps({
        "schema": "competitor-eval-ready-v1", "dataset": "pdf-fixture", "scope": "document_local",
        "ingest_representation": "source_document", "artifacts": {"corpus.jsonl": "corpus.jsonl", "questions.jsonl": "questions.jsonl"},
    }))
    loaded = runner.EvalPackage.load(package)
    args = runner.build_parser().parse_args([
        "preflight", "--system", "dify_local", "--package", str(package), "--output-root", str(tmp_path / "runs"),
        "--run-id", "source-plan", "--dry-run",
    ])
    context = runner.RunnerContext(args, loaded)
    assert len(context.ingest_plan("scope-a").artifacts) == 1, "the same original PDF is uploaded once per scope"
    assert context.ingest_plan("scope-b").unsupported_candidates == {"source-b": "SOURCE_DOCUMENT_MISSING"}


def test_actual_document_local_packages_have_scope_sized_resources():
    root = HERE.parents[2] / ".local-services" / "competitor-eval-ready" / "v1"
    cases = {
        "mmdocir/layout": {313},
        "mmdocir/page": {313},
        "mmdocrag/c15": {220, 222},
        "mmdocrag/c20": {220, 222},
        "docbench/native-pdf": {229},
        "docbench/controlled-parsed-text": {229},
    }
    for relative, expected in cases.items():
        loaded = runner.EvalPackage.load(root / relative)
        assert len(loaded.target_documents()) in expected, relative
        assert len(loaded.target_documents()) == len(loaded.scope_groups)
        assert len(loaded.target_documents()) < len(loaded.documents) or len(loaded.documents) == len(loaded.scope_groups)
        assert all(len(question.scope_ids) == 1 for question in loaded.questions)


def test_dify_document_local_submits_bounded_scope_window_before_polling(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=4, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_PASSWORD", "console-secret")
    monkeypatch.setenv("DIFY_ALLOW_GENERIC_COMPAT_ADAPTER", "1")
    monkeypatch.setattr(runner, "ArtifactHTTP", ScopeDifyHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    assert runner.main([
        "ingest", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-scope-window",
        "--dify-max-indexing-scopes", "2", "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0

    dify_calls = [call for call in ScopeDifyHTTP.calls if call["operation"].startswith("dify-")]
    first_poll = next(index for index, call in enumerate(dify_calls) if call["operation"].startswith("dify-readiness-"))
    submitted_before_first_poll = dify_calls[:first_poll]
    assert sum(call["operation"].startswith("dify-create-dataset-") for call in submitted_before_first_poll) == 2
    assert sum(call["operation"].startswith("dify-upload-") for call in submitted_before_first_poll) == 2

    resources = json.loads(
        (tmp_path / "runs/dify-scope-window/resource-map.json").read_text(encoding="utf-8")
    )["resources"]
    assert len(resources) == 4
    assert all(resource["status"] == "ready" for resource in resources.values())
    assert all(resource["ingest_state"]["phase"] == "ready" for resource in resources.values())
    assert all(resource["ingest_state"]["active_indexing"] is False for resource in resources.values())


def test_dify_document_local_scope_failure_does_not_drop_other_scopes(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=3, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_PASSWORD", "console-secret")
    monkeypatch.setenv("DIFY_ALLOW_GENERIC_COMPAT_ADAPTER", "1")
    ScopeDifyHTTP.fail_dataset = "dify-scope-dataset-2"
    monkeypatch.setattr(runner, "ArtifactHTTP", ScopeDifyHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    assert runner.main([
        "ingest", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-scope-failure",
        "--dify-max-indexing-scopes", "3", "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0

    root = tmp_path / "runs/dify-scope-failure"
    resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert set(resources) == {"bench:scope:0", "bench:scope:1", "bench:scope:2"}
    assert resources["bench:scope:0"]["status"] == "ready"
    assert resources["bench:scope:1"]["status"] == "failed"
    assert resources["bench:scope:1"]["ingest_state"]["phase"] == "failed"
    assert "DIFY_UPLOAD_FAILED" in resources["bench:scope:1"]["error"]
    assert resources["bench:scope:2"]["status"] == "ready"
    ingest_status = json.loads((root / "ingest-status.json").read_text(encoding="utf-8"))
    assert ingest_status["status"] == "PARTIAL"
    assert ingest_status["result"]["resource_counts"]["FAILED"] == 1


def test_dify_document_local_failed_scope_resumes_without_reupload(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=1, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("DIFY_LOCAL_DATASET_API_KEY", "dify-dataset-admin-secret")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("DIFY_LOCAL_ADMIN_PASSWORD", "console-secret")
    monkeypatch.setenv("DIFY_ALLOW_GENERIC_COMPAT_ADAPTER", "1")
    ScopeDifyHTTP.indexing_ready = False
    monkeypatch.setattr(runner, "ArtifactHTTP", ScopeDifyHTTP)
    monkeypatch.setattr(runner, "DifyAppBinding", FakeDifyAppBinding)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    argv = [
        "ingest", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-scope-resume",
        "--dify-max-indexing-scopes", "1", "--index-timeout", "0.01", "--poll-seconds", "0",
    ]

    assert runner.main(argv) == 0
    root = tmp_path / "runs/dify-scope-resume"
    first_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert first_resources["bench:scope:0"]["status"] == "failed"
    first_create_count = sum(call["operation"].startswith("dify-create-dataset-") for call in ScopeDifyHTTP.calls)
    first_upload_count = sum(call["operation"].startswith("dify-upload-") for call in ScopeDifyHTTP.calls)
    assert (first_create_count, first_upload_count) == (1, 1)

    ScopeDifyHTTP.indexing_ready = True
    assert runner.main(argv) == 0
    second_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert second_resources["bench:scope:0"]["status"] == "ready"
    assert sum(call["operation"].startswith("dify-create-dataset-") for call in ScopeDifyHTTP.calls) == first_create_count
    assert sum(call["operation"].startswith("dify-upload-") for call in ScopeDifyHTTP.calls) == first_upload_count
    assert second_resources["bench:scope:0"]["ingest_state"]["phase"] == "ready"


def test_dify_scope_concurrency_cli_and_env_are_bounded(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=1, candidates_per_scope=1)
    monkeypatch.setenv("DIFY_MAX_INDEXING_SCOPES", "5")
    args = runner.build_parser().parse_args([
        "preflight", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "dify-scope-limit", "--dry-run",
    ])
    env = runner._merge_env_files("dify_local")
    assert runner._dify_max_indexing_scopes(args, env) == 5
    args.dify_max_indexing_scopes = 2
    assert runner._dify_max_indexing_scopes(args, env) == 2
    args.dify_max_indexing_scopes = 12
    assert runner._dify_max_indexing_scopes(args, env) == 12
    args.dify_max_indexing_scopes = 17
    with pytest.raises(runner.RunnerError, match="DIFY_MAX_INDEXING_SCOPES_INVALID"):
        runner._dify_max_indexing_scopes(args, env)


def test_qa_concurrency_cli_and_env_are_bounded(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setenv("COMPETITOR_EVAL_QA_CONCURRENCY", "5")
    args = runner.build_parser().parse_args([
        "qa", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "qa-limit", "--dry-run",
    ])
    env = runner._merge_env_files("dify_local")
    assert runner._qa_concurrency(args, env) == 5
    assert runner.QA_CONCURRENCY_DEFAULT == 4
    args.qa_concurrency = 2
    assert runner._qa_concurrency(args, env) == 2
    args.qa_concurrency = 17
    with pytest.raises(runner.RunnerError, match="QA_CONCURRENCY_INVALID"):
        runner._qa_concurrency(args, env)


def test_qa_is_bounded_ordered_session_isolated_and_artifact_safe(tmp_path, monkeypatch):
    package = make_qa_package(tmp_path, question_count=3)
    set_provider_keys(monkeypatch)
    monkeypatch.setattr(runner, "ArtifactHTTP", ConcurrentArtifactHTTP)
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    session_ids = []

    class ConcurrentQAAdapter:
        def __init__(self, context):
            self.context = context

        def ingest(self):
            payload = self.context.load_resources()
            for resource in payload["resources"].values():
                resource.update({"status": "ready", "ready": True, "resource_id": "qa-resource"})
            self.context.save_resources(payload)
            return {"status": "SUCCESS", "resource_counts": {"READY": 1}}

        def qa(self, question, resource):
            nonlocal active, max_active
            session_ids.append(question.question_id)
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                self.context._http("http://qa-artifact.test").request(
                    "POST", "/qa", operation=f"qa-{question.question_id}"
                )
                time.sleep(0.02)
                if question.question_id == "q-2#repeat-2":
                    raise runner.EvalError("QA_PROVIDER_FAILURE")
                return {"answer": "gold answer", "contract": "native_qa"}
            finally:
                with active_lock:
                    active -= 1

    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"ready": True})
    monkeypatch.setattr(runner.RunnerContext, "adapter", lambda self: ConcurrentQAAdapter(self))

    assert runner.main([
        "qa", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "qa-concurrent",
        "--repeats", "2", "--qa-concurrency", "2",
    ]) == 0

    root = tmp_path / "runs" / "qa-concurrent"
    rows = runner._read_jsonl(root / "terminal-ledger.jsonl")
    assert [(row["question_id"], row["repeat_id"]) for row in rows] == [
        (f"q-{question}", repeat)
        for question in range(1, 4)
        for repeat in range(1, 3)
    ]
    assert [row["status"] for row in rows] == ["SUCCESS", "SUCCESS", "SUCCESS", "FAILED", "SUCCESS", "SUCCESS"]
    assert rows[3]["error"] == "QA_PROVIDER_FAILURE"
    assert set(session_ids) == {
        f"q-{question}#repeat-{repeat}"
        for question in range(1, 4)
        for repeat in range(1, 3)
    }
    assert max_active == 2

    artifacts = sorted((root / "http").glob("*.json"))
    assert len(artifacts) == 6
    assert [int(path.name.split("-", 1)[0]) for path in artifacts] == list(range(1, 7))
    assert all(path.with_suffix(path.suffix + ".sha256").exists() for path in artifacts)
    assert json.loads((root / "start-record.json").read_text(encoding="utf-8"))["runtime"]["qa_concurrency"] == 2


def test_maxkb_ingest_concurrency_cli_and_env_are_bounded(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setenv("MAXKB_INGEST_CONCURRENCY", "5")
    args = runner.build_parser().parse_args([
        "ingest", "--system", "maxkb_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maxkb-limit", "--dry-run",
    ])
    env = runner._merge_env_files("maxkb_local")
    assert runner._maxkb_ingest_concurrency(args, env) == 5
    assert runner.MAXKB_INGEST_CONCURRENCY_DEFAULT == 2
    args.maxkb_ingest_concurrency = 3
    assert runner._maxkb_ingest_concurrency(args, env) == 3
    args.maxkb_ingest_concurrency = 17
    with pytest.raises(runner.RunnerError, match="MAXKB_INGEST_CONCURRENCY_INVALID"):
        runner._maxkb_ingest_concurrency(args, env)


def test_fastgpt_ingest_concurrency_cli_and_env_are_bounded(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    monkeypatch.setenv("FASTGPT_INGEST_CONCURRENCY", "5")
    args = runner.build_parser().parse_args([
        "ingest", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "fastgpt-limit", "--dry-run",
    ])
    env = runner._merge_env_files("fastgpt_local")
    assert args.fastgpt_ingest_concurrency is None
    assert runner._fastgpt_ingest_concurrency(args, env) == 5
    assert runner.FASTGPT_INGEST_CONCURRENCY_DEFAULT == 2

    cli_args = runner.build_parser().parse_args([
        "ingest", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "fastgpt-cli-limit",
        "--fastgpt-ingest-concurrency", "3", "--dry-run",
    ])
    assert runner._fastgpt_ingest_concurrency(cli_args, env) == 3

    cli_args.fastgpt_ingest_concurrency = 17
    with pytest.raises(runner.RunnerError, match="FASTGPT_INGEST_CONCURRENCY_INVALID"):
        runner._fastgpt_ingest_concurrency(cli_args, env)


def test_maxkb_ingest_runs_independent_resources_concurrently_and_keeps_resource_order(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=4, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_ADMIN_BASE_URL", "http://maxkb.test/admin/api")
    monkeypatch.setattr(runner, "ArtifactHTTP", ConcurrentMaxKBHTTP)
    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})

    assert runner.main([
        "ingest", "--system", "maxkb_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maxkb-concurrent",
        "--maxkb-ingest-concurrency", "2", "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0

    root = tmp_path / "runs" / "maxkb-concurrent"
    resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert list(resources) == [f"bench:scope:{index}" for index in range(4)]
    assert all(resource["status"] == "ready" for resource in resources.values())
    assert ConcurrentMaxKBHTTP.max_active_batch == 2
    assert json.loads((root / "start-record.json").read_text(encoding="utf-8"))["runtime"]["maxkb_ingest_concurrency"] == 2

    artifacts = sorted((root / "http").glob("*.json"))
    assert [int(path.name.split("-", 1)[0]) for path in artifacts] == list(range(1, len(artifacts) + 1))
    assert all(path.with_suffix(path.suffix + ".sha256").exists() for path in artifacts)


def test_fastgpt_ingest_aggregates_mixed_statuses_and_resumes_in_resource_order(tmp_path, monkeypatch):
    package = make_document_local_package(
        tmp_path,
        scopes=5,
        candidates_per_scope=1,
        missing_candidate=True,
    )
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("FASTGPT_API_KEY", "fastgpt-admin-secret")
    ConcurrentFastGPTHTTP.fail_dataset_scope = "bench_scope_2"
    ConcurrentFastGPTHTTP.stall_dataset_scope = "bench_scope_1"
    ConcurrentFastGPTHTTP.collection_barrier = threading.Barrier(2)
    ConcurrentFastGPTHTTP.collection_barrier_waiters = 2
    monkeypatch.setattr(runner, "ArtifactHTTP", ConcurrentFastGPTHTTP)
    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})

    argv = [
        "ingest", "--system", "fastgpt_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "fastgpt-concurrent",
        "--fastgpt-ingest-concurrency", "2", "--index-timeout", "1", "--poll-seconds", "0",
    ]
    assert runner.main(argv) == 0

    root = tmp_path / "runs" / "fastgpt-concurrent"
    first_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert list(first_resources) == [f"bench:scope:{index}" for index in range(5)]
    assert [resource["status"] for resource in first_resources.values()] == [
        "unsupported", "failed", "failed", "ready", "ready",
    ]
    first_ingest = json.loads((root / "ingest-status.json").read_text(encoding="utf-8"))["result"]
    assert first_ingest["resource_counts"] == {
        "UNSUPPORTED": 1,
        "TIMEOUT": 1,
        "FAILED": 1,
        "READY": 2,
    }
    assert ConcurrentFastGPTHTTP.max_active_collection_create == 2
    assert json.loads((root / "start-record.json").read_text(encoding="utf-8"))["runtime"]["fastgpt_ingest_concurrency"] == 2

    stalled = first_resources["bench:scope:1"]
    stalled_dataset_id = stalled["dataset_id"]
    stalled_upload = next(iter(stalled["uploads"].values()))
    assert stalled_upload["status"] == "submitted"
    first_collection_count = sum(
        call["operation"].startswith("fastgpt-create-collection-")
        and call["request"]["json"].get("datasetId") == stalled_dataset_id
        for call in ConcurrentFastGPTHTTP.calls
    )
    first_push_count = sum(
        call["operation"].startswith("fastgpt-push-data-")
        and call["request"]["json"].get("collectionId") == stalled_upload["collection_id"]
        for call in ConcurrentFastGPTHTTP.calls
    )
    assert (first_collection_count, first_push_count) == (1, 1)

    # The first pass deliberately synchronized collection creation above.  The
    # resume pass may only need one new collection, so do not retain that test
    # barrier across the second invocation.
    ConcurrentFastGPTHTTP.collection_barrier = None
    ConcurrentFastGPTHTTP.fail_dataset_scope = ""
    assert runner.main(argv) == 0

    second_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert list(second_resources) == [f"bench:scope:{index}" for index in range(5)]
    assert [resource["status"] for resource in second_resources.values()] == [
        "unsupported", "ready", "ready", "ready", "ready",
    ]
    second_ingest = json.loads((root / "ingest-status.json").read_text(encoding="utf-8"))["result"]
    assert second_ingest["resource_counts"] == {"UNSUPPORTED": 1, "READY": 4}
    assert second_resources["bench:scope:1"]["dataset_id"] == stalled_dataset_id
    assert sum(
        call["operation"].startswith("fastgpt-create-collection-")
        and call["request"]["json"].get("datasetId") == stalled_dataset_id
        for call in ConcurrentFastGPTHTTP.calls
    ) == first_collection_count
    assert sum(
        call["operation"].startswith("fastgpt-push-data-")
        and call["request"]["json"].get("collectionId") == stalled_upload["collection_id"]
        for call in ConcurrentFastGPTHTTP.calls
    ) == first_push_count


def test_maxkb_global_ingest_is_naturally_single_resource(tmp_path, monkeypatch):
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_ADMIN_BASE_URL", "http://maxkb.test/admin/api")
    monkeypatch.setattr(runner, "ArtifactHTTP", ConcurrentMaxKBHTTP)
    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})

    assert runner.main([
        "ingest", "--system", "maxkb_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maxkb-global",
        "--maxkb-ingest-concurrency", "2", "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0
    assert ConcurrentMaxKBHTTP.max_active_batch == 1


def test_maxkb_failure_isolated_and_resume_reuses_submitted_document(tmp_path, monkeypatch):
    package = make_document_local_package(tmp_path, scopes=4, candidates_per_scope=1)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_ADMIN_BASE_URL", "http://maxkb.test/admin/api")
    ConcurrentMaxKBHTTP.fail_readiness_remaining = 1
    monkeypatch.setattr(runner, "ArtifactHTTP", ConcurrentMaxKBHTTP)
    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})
    argv = [
        "ingest", "--system", "maxkb_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maxkb-resume",
        "--maxkb-ingest-concurrency", "2", "--index-timeout", "1", "--poll-seconds", "0",
    ]

    assert runner.main(argv) == 0
    root = tmp_path / "runs" / "maxkb-resume"
    first_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    failed = [resource for resource in first_resources.values() if resource["status"] == "failed"]
    assert len(failed) == 1
    assert sum(resource["status"] == "ready" for resource in first_resources.values()) == 3
    failed_resource = failed[0]
    submitted = next(iter(failed_resource["uploads"].values()))
    assert submitted["status"] == "submitted"
    create_count = sum(call["operation"].startswith("maxkb-create-knowledge-") for call in ConcurrentMaxKBHTTP.calls)
    batch_count = sum(call["operation"].startswith("maxkb-batch-create-document-") for call in ConcurrentMaxKBHTTP.calls)
    assert (create_count, batch_count) == (4, 4)

    assert runner.main(argv) == 0
    second_resources = json.loads((root / "resource-map.json").read_text(encoding="utf-8"))["resources"]
    assert all(resource["status"] == "ready" for resource in second_resources.values())
    assert sum(call["operation"].startswith("maxkb-create-knowledge-") for call in ConcurrentMaxKBHTTP.calls) == create_count
    assert sum(call["operation"].startswith("maxkb-batch-create-document-") for call in ConcurrentMaxKBHTTP.calls) == batch_count


def test_maxkb_admin_retries_task_busy_and_http_5xx_with_bounded_backoff(tmp_path, monkeypatch):
    class RetryingMaxKBHTTP(FakeHTTP):
        attempts: dict[str, int] = {}

        @classmethod
        def reset_retry_state(cls):
            cls.calls = []
            cls.counter = 0
            cls.attempts = {}

        def request(self, method, path, **kwargs):
            operation = kwargs.get("operation", "request")
            attempt = type(self).attempts.get(operation, 0) + 1
            type(self).attempts[operation] = attempt
            if operation.startswith("maxkb-create-knowledge-") and attempt == 1:
                raise runner.HTTPFailure(503, {"message": "temporary server"}, self.base_url, operation)
            if operation.startswith("maxkb-batch-create-document-") and attempt == 1:
                return_value = {"code": 409, "message": "任务正在执行"}
                self._record(operation, {"method": method, "path": path}, return_value)
                return return_value
            return super().request(method, path, **kwargs)

    RetryingMaxKBHTTP.reset_retry_state()
    package = make_package(tmp_path)
    set_provider_keys(monkeypatch)
    monkeypatch.setenv("MAXKB_ADMIN_TOKEN", "maxkb-admin-secret")
    monkeypatch.setenv("MAXKB_ADMIN_BASE_URL", "http://maxkb.test/admin/api")
    delays = []
    monkeypatch.setattr(runner.time, "sleep", delays.append)
    monkeypatch.setattr(runner, "ArtifactHTTP", RetryingMaxKBHTTP)
    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})

    assert runner.main([
        "ingest", "--system", "maxkb_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "maxkb-retry",
        "--index-timeout", "1", "--poll-seconds", "0",
    ]) == 0
    assert RetryingMaxKBHTTP.attempts["maxkb-create-knowledge-global"] == 2
    assert RetryingMaxKBHTTP.attempts["maxkb-batch-create-document-0001"] == 2
    assert delays == sorted(delays)
    assert all(0 < delay <= runner.MAXKB_INGEST_RETRY_BACKOFF_MAX_SECONDS for delay in delays)


def test_json_dump_atomically_replaces_json_and_preserves_mode_and_hash(tmp_path, monkeypatch):
    target = tmp_path / "resource-map.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    target.chmod(0o600)
    original_target = json.loads(target.read_text(encoding="utf-8"))
    real_replace = runner.os.replace
    replacements = []

    def observe_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        replacements.append((source, destination))
        if destination == target:
            assert source.parent == target.parent
            assert source != target
            assert json.loads(target.read_text(encoding="utf-8")) == original_target
            assert json.loads(source.read_text(encoding="utf-8")) == {"new": [1, 2, 3]}
        return real_replace(source, destination)

    monkeypatch.setattr(runner.os, "replace", observe_replace)
    runner.json_dump(target, {"new": [1, 2, 3]})

    assert json.loads(target.read_text(encoding="utf-8")) == {"new": [1, 2, 3]}
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.with_suffix(target.suffix + ".sha256").read_text(encoding="utf-8") == (
        f"{runner.sha256_bytes(target.read_bytes())}  {target.name}\n"
    )
    assert replacements
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_qa_timeout_and_interrupted_rows_make_stage_partial_without_changing_main_exit(tmp_path, monkeypatch):
    package = make_qa_package(tmp_path, question_count=3)
    set_provider_keys(monkeypatch)

    class StatusQAAdapter:
        def __init__(self, context):
            self.context = context

        def ingest(self):
            payload = self.context.load_resources()
            for resource in payload["resources"].values():
                resource.update({"status": "ready", "ready": True, "resource_id": "qa-resource"})
            self.context.save_resources(payload)
            return {"status": "SUCCESS", "resource_counts": {"READY": 1}}

        def qa(self, question, resource):
            if question.question_id == "q-1#repeat-1":
                raise runner.EvalError("QA timeout")
            if question.question_id == "q-2#repeat-1":
                raise runner.EvalError("QA interrupted")
            return {"answer": "gold answer", "contract": "native_qa"}

    monkeypatch.setattr(runner.RunnerContext, "preflight", lambda self: {"status": "READY", "ready": True})
    monkeypatch.setattr(runner.RunnerContext, "adapter", lambda self: StatusQAAdapter(self))

    assert runner.main([
        "qa", "--system", "dify_local", "--package", str(package),
        "--output-root", str(tmp_path / "runs"), "--run-id", "qa-statuses",
    ]) == 0
    summary = json.loads((tmp_path / "runs" / "qa-statuses" / "summary.json").read_text(encoding="utf-8"))
    assert summary["qa"]["status"] == "PARTIAL"
    rows = runner._read_jsonl(tmp_path / "runs" / "qa-statuses" / "terminal-ledger.jsonl")
    assert [row["status"] for row in rows if row["stage"] == "qa"] == ["TIMEOUT", "INTERRUPTED", "SUCCESS"]
