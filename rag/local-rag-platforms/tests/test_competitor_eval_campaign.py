from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "local-rag-platforms/scripts/evaluation/competitor_eval_campaign.py"


def _module():
    name = "competitor_eval_campaign_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_huawei_maas_plugin_namespace_survives_campaign_env_filter():
    module = _module()

    env = module.load_campaign_env(
        ROOT,
        "dify_local",
        base_env={
            "DIFY_MAAS_LLM_PROVIDER": "matrixorigin/matrixorigin_huawei_maas/huawei_maas",
            "DIFY_MAAS_EMBEDDING_PROVIDER": "matrixorigin/matrixorigin_huawei_maas/huawei_maas",
        },
    )

    assert env["DIFY_MAAS_LLM_PROVIDER"] == "matrixorigin/matrixorigin_huawei_maas/huawei_maas"
    assert env["DIFY_MAAS_EMBEDDING_PROVIDER"] == "matrixorigin/matrixorigin_huawei_maas/huawei_maas"


def test_legacy_taas_namespace_is_still_rejected():
    module = _module()

    try:
        module.load_campaign_env(
            ROOT,
            "dify_local",
            base_env={
                "DIFY_ALLOW_GENERIC_COMPAT_ADAPTER": "0",
                "DIFY_MAAS_EMBEDDING_PROVIDER": "matrixorigin/matrixorigin_taas/matrixorigin_taas",
            },
        )
    except module.CampaignError as exc:
        assert str(exc) == "TAAS_PROVIDER_REFUSED:DIFY_MAAS_EMBEDDING_PROVIDER"
    else:  # pragma: no cover - assertion makes the failure explicit
        raise AssertionError("legacy TaaS namespace was accepted")


def test_legacy_dify_llm_selector_is_rejected_at_campaign_boundary():
    module = _module()

    try:
        module.load_campaign_env(
            ROOT,
            "dify_local",
            base_env={
                "DIFY_ALLOW_GENERIC_COMPAT_ADAPTER": "1",
                "DIFY_MAAS_LLM_PROVIDER": module.DIFY_GENERIC_OAI_ADAPTER,
            },
        )
    except module.CampaignError as exc:
        assert str(exc) == "TAAS_PROVIDER_REFUSED:DIFY_MAAS_LLM_PROVIDER"
    else:  # pragma: no cover - assertion makes the failure explicit
        raise AssertionError("legacy Dify LLM selector was accepted")


def test_non_maas_endpoint_markers_are_rejected_at_campaign_boundary():
    module = _module()

    for endpoint, expected in (
        ("https://qianfan.baidubce.com/v2", "MAAS_ONLY_BASE_URL_REQUIRED:maas_config"),
        ("https://qwen.example/v1", "MAAS_ONLY_BASE_URL_REQUIRED:maas_config"),
        ("https://api.openai.com/v1", "MAAS_ONLY_BASE_URL_REQUIRED:maas_config"),
    ):
        try:
            module.CampaignConfig(repo_root=ROOT, maas_base_url=endpoint)
        except module.CampaignError as exc:
            assert str(exc) == expected
        else:  # pragma: no cover - assertion makes the failure explicit
            raise AssertionError(f"non-MaaS endpoint was accepted: {endpoint}")


def test_campaign_child_env_overrides_stale_maas_runtime_values(tmp_path, monkeypatch):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        maas_base_url="https://api.modelarts-maas.com/v1",
        execute=True,
    )
    orchestrator = module.CampaignOrchestrator(config)
    captured = {}

    monkeypatch.setattr(module, "load_campaign_env", lambda *args, **kwargs: {
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_LLM_MODEL": "legacy-deepseek-model",
        "MAAS_BASE_URL": "https://api.modelarts-maas.com/v1",
        "MAAS_LLM_MODEL": "legacy-model",
        "MAAS_EMBEDDING_MODEL": "legacy-embedding",
        "MAAS_EMBEDDING_DIMENSION": "4096",
    })

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)
    result = orchestrator._run_external(
        ["python", "--text-llm-provider", "deepseek-official"],
        platform="moi_local",
        action="probe",
    )
    assert result["status"] == "SUCCESS"
    env = captured["env"]
    assert env["DEEPSEEK_BASE_URL"] == config.deepseek_base_url
    assert env["DEEPSEEK_LLM_MODEL"] == config.deepseek_llm_model
    assert env["MAAS_BASE_URL"] == config.maas_base_url
    assert env["MAAS_EMBEDDING_MODEL"] == config.maas_embedding_model
    assert env["MAAS_EMBEDDING_DIMENSION"] == str(config.maas_embedding_dimension)
    assert env["COMPETITOR_EMBEDDING_PROVIDER"] == "maas"


def test_active_campaign_child_env_removes_qianfan_and_forces_generic_maas_selectors():
    module = _module()

    env = module.load_campaign_env(
        ROOT,
        "fastgpt_local",
        base_env={
            "QIANFAN_API_KEY": "<redacted>",
            "QIANFAN_BASE_URL": "https://qianfan.baidubce.com/v2",
            "DIFY_QIANFAN_LLM_PROVIDER": "legacy/provider",
            "FASTGPT_MODEL_PROVIDER": "qianfan",
            "FASTGPT_LLM_PROVIDER": "taas",
        },
    )

    assert not any("QIANFAN" in key.upper() for key in env)
    assert env["COMPETITOR_TEXT_LLM_PROVIDER"] == "deepseek-official"
    assert "MODEL_PROVIDER" not in env
    assert "FASTGPT_PROVIDER" not in env
    assert "FASTGPT_MODEL_PROVIDER" not in env
    assert "FASTGPT_LLM_PROVIDER" not in env
    assert env["FASTGPT_EMBEDDING_PROVIDER"] == "maas"


def test_mixed_package_order_is_persisted_in_campaign_checkpoint(tmp_path):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        campaign_id="test-mixed-package",
    )

    module.CampaignOrchestrator(config).plan()
    checkpoint = module._load_json(config.checkpoint)

    assert checkpoint["evaluation_order"] == ["moi-rag-bench-v0-1-text-only-no-mllm"]
    assert checkpoint["platform_order"] == ["moi_local", "dify_local", "fastgpt_local", "maxkb_local"]
    assert [unit["platform"] for unit in checkpoint["units"]] == checkpoint["platform_order"]
    assert checkpoint["postprocess_contract"]["schema"] == "competitor-eval-postprocess-contract-v1"
    assert checkpoint["postprocess_contract"]["order_per_unit"] == [
        "runner",
        "provider-audit-runner",
        "judge",
        "provider-audit-final",
        "metrics",
    ]
    provenance = checkpoint["provenance"]
    assert provenance["schema"] == "competitor-eval-provenance-v1"
    assert provenance["package_records"][0]["counts"] == {"documents": 500, "gold": 1000, "questions": 1000}
    assert provenance["evaluation_policy"]["deepseek_llm_model"] == "deepseek-v4-flash"
    assert provenance["evaluation_policy"]["maas_vl_model"] == "NOT_APPLICABLE"
    assert provenance["evaluation_policy"]["maas_embedding_model"] == "bge-m3"
    assert provenance["evaluation_policy"]["maas_embedding_dimension"] == 1024
    assert provenance["evaluation_policy"]["thinking"] == {"type": "disabled"}
    assert provenance["evaluation_policy"]["serial_order"] == ["moi_local", "dify_local", "fastgpt_local", "maxkb_local"]
    assert any(str(item.get("path", "")).endswith("competitor_eval_campaign.py") for item in provenance["source_artifacts"])
    assert any(str(item.get("path", "")).endswith("matrixorigin-huawei-maas/models/shared.py") for item in provenance["source_artifacts"])
    assert any(str(item.get("path", "")).endswith("competitor_eval_metric_registry.py") for item in provenance["source_artifacts"])
    for unit in checkpoint["units"]:
        assert unit["runner_command"][unit["runner_command"].index("--run-id") + 1] == unit["run_id"]
        assert unit["judge_command"][unit["judge_command"].index("--run-dir") + 1] == unit["runner_artifact_root"]
        assert unit["provider_audit_command"][unit["provider_audit_command"].index("--runs-root") + 1] == unit["runner_artifact_root"]
        assert unit["provider_audit_runner_command"][unit["provider_audit_runner_command"].index("--runs-root") + 1] == unit["runner_artifact_root"]

    sidecar = config.checkpoint.with_name(config.checkpoint.name + ".sha256")
    expected = hashlib.sha256(config.checkpoint.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="utf-8") == f"{expected}  {config.checkpoint.name}\n"

    store = module.CampaignStore(config)
    store.set_active_service("moi_local")
    updated = hashlib.sha256(config.checkpoint.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="utf-8") == f"{updated}  {config.checkpoint.name}\n"

    store.event("unit-started", platform="moi_local")
    progress_sidecar = config.progress_log.with_name(config.progress_log.name + ".sha256")
    progress_digest = hashlib.sha256(config.progress_log.read_bytes()).hexdigest()
    assert progress_sidecar.read_text(encoding="utf-8") == f"{progress_digest}  {config.progress_log.name}\n"


def test_text_only_execute_fails_closed_without_semantic_signoff(tmp_path):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        campaign_id="test-text-only-signoff",
        execute=True,
    )

    try:
        module.CampaignOrchestrator(config).run()
    except module.CampaignError as exc:
        assert str(exc) == "TEXT_ONLY_SEMANTIC_AUDIT_REQUIRED"
    else:  # pragma: no cover - assertion makes the failure explicit
        raise AssertionError("text-only execute bypassed the semantic signoff gate")

    checkpoint = json.loads(config.checkpoint.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "planned"
    assert not (config.output_root / "001-moi-rag-bench-v0-1-text-only-no-mllm-text-only-no-mllm-moi_local").exists()


def test_text_only_signoff_rejects_multiple_distinct_manifests(tmp_path):
    module = _module()
    manifests = []
    for name in ("first", "second"):
        manifest = tmp_path / name / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"dataset_id": name}), encoding="utf-8")
        manifests.append(manifest)
    signoff = tmp_path / "audit.json"
    signoff.write_text(
        json.dumps(
            {
                "schema": module.TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA,
                "status": "PASS",
                "mllm_required": False,
                "manifest_sha256": hashlib.sha256(manifests[0].read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=tmp_path,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        text_only_semantic_audit=signoff,
        campaign_id="test-distinct-manifest-signoff",
        execute=True,
    )
    plan = {
        "units": [
            {"mllm_required": False, "manifest": str(manifest)}
            for manifest in manifests
        ]
    }

    try:
        module.CampaignOrchestrator(config)._validate_text_only_semantic_audit(plan)
    except module.CampaignError as exc:
        assert str(exc) == "TEXT_ONLY_SEMANTIC_AUDIT_MANIFEST_HASH_MISMATCH"
    else:  # pragma: no cover - assertion makes the failure explicit
        raise AssertionError("one signoff authorized multiple distinct manifests")


def test_campaign_preview_includes_judge_and_metrics_after_each_runner(tmp_path):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        campaign_id="test-postprocess-preview",
    )

    result = module.CampaignOrchestrator(config).run()

    assert result["status"] == "DRY_RUN"
    actions = [item["action"] for item in result["commands"]]
    assert actions == [
        "external-service",
        "runner-all",
        "provider-audit-runner",
        "judge-run",
        "provider-audit-final",
        "metrics-aggregate",
        "start",
        "runner-all",
        "provider-audit-runner",
        "judge-run",
        "provider-audit-final",
        "metrics-aggregate",
        "stop",
        "start",
        "runner-all",
        "provider-audit-runner",
        "judge-run",
        "provider-audit-final",
        "metrics-aggregate",
        "stop",
        "start",
        "runner-all",
        "provider-audit-runner",
        "judge-run",
        "provider-audit-final",
        "metrics-aggregate",
        "stop",
    ]
    judge = next(item for item in result["commands"] if item["action"] == "judge-run")
    metrics = next(item for item in result["commands"] if item["action"] == "metrics-aggregate")
    assert str(config.judge_path) in judge["command"]
    assert str(config.metrics_path) in metrics["command"]


def test_execute_runs_runner_judge_metrics_in_order_and_persists_results(tmp_path, monkeypatch):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    manifest = package / "manifest.json"
    signoff = tmp_path / "text-only-semantic-audit.json"
    signoff.write_text(
        json.dumps(
            {
                "schema": module.TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA,
                "status": "PASS",
                "mllm_required": False,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    class FakeServiceController:
        def __init__(self, orchestrator):
            self.orchestrator = orchestrator

        def ensure(self, platform):
            return None

        def stop_active(self):
            return None

    calls = []

    def fake_external(self, command, *, platform, action, allow_failure=False):
        calls.append(action)
        if action == "runner-all":
            output_root = Path(command[command.index("--output-root") + 1])
            run_id = command[command.index("--run-id") + 1]
            (output_root / run_id).mkdir(parents=True, exist_ok=True)
            return {
                "status": "SUCCESS",
                "returncode": 0,
                "command": list(command),
                "platform": platform,
                "action": action,
                "stdout": json.dumps({"status": "SUCCESS", "run_id": run_id}),
            }
        if action == "judge-run":
            output = Path(command[command.index("--output") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "judge-summary.json").write_text(
                json.dumps({"status": "COMPLETE", "denominator": {"pending_n": 0}}),
                encoding="utf-8",
            )
            return {"status": "SUCCESS", "returncode": 0, "command": list(command), "platform": platform, "action": action, "stdout": ""}
        if action in {"provider-audit-runner", "provider-audit-final"}:
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "schema": "moi-rag-bench-text-only-provider-audit-v1",
                        "status": "PASS",
                        "request_records": 1,
                        "request_models_observed": ["bge-m3", "deepseek-v4-flash"],
                        "image_request_count": 0,
                        "violations": [],
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "SUCCESS", "returncode": 0, "command": list(command), "platform": platform, "action": action, "stdout": ""}
        if action == "metrics-aggregate":
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"schema": "competitor-eval-metrics-v1", "status": "COMPLETE"}), encoding="utf-8")
            return {"status": "SUCCESS", "returncode": 0, "command": list(command), "platform": platform, "action": action, "stdout": ""}
        raise AssertionError(f"unexpected campaign action: {action}")

    monkeypatch.setattr(module, "ServiceController", FakeServiceController)
    monkeypatch.setattr(module.CampaignOrchestrator, "_run_external", fake_external)
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        text_only_semantic_audit=signoff,
        campaign_id="test-runner-judge-metrics",
        execute=True,
    )

    result = module.CampaignOrchestrator(config).run()

    assert result["status"] == "SUCCESS"
    assert calls == [
        stage
        for _ in range(4)
        for stage in (
            "runner-all",
            "provider-audit-runner",
            "judge-run",
            "provider-audit-final",
            "metrics-aggregate",
        )
    ]
    checkpoint = json.loads(config.checkpoint.read_text(encoding="utf-8"))
    assert all(unit["status"] == "completed" for unit in checkpoint["units"])
    assert all(unit["provider_audit_initial"]["phase"] == "runner" for unit in checkpoint["units"])
    assert all(unit["provider_audit"]["status"] == "PASS" for unit in checkpoint["units"])
    assert all(unit["provider_audit"]["phase"] == "final" for unit in checkpoint["units"])
    assert all(unit["judge"]["status"] == "COMPLETE" for unit in checkpoint["units"])
    assert all(unit["metrics"]["status"] == "COMPLETE" for unit in checkpoint["units"])


def _write_text_only_signoff(module, package: Path, path: Path) -> None:
    manifest = package / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema": module.TEXT_ONLY_SEMANTIC_AUDIT_SCHEMA,
                "status": "PASS",
                "mllm_required": False,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_execute_blocks_before_metrics_when_judge_is_incomplete(tmp_path, monkeypatch):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    signoff = tmp_path / "audit.json"
    _write_text_only_signoff(module, package, signoff)

    class FakeServiceController:
        def __init__(self, orchestrator):
            self.orchestrator = orchestrator

        def ensure(self, platform):
            return None

        def stop_active(self):
            return None

    calls = []

    def fake_runner(self, unit, stage, *, dry_run):
        calls.append("runner")
        return {"status": "SUCCESS", "returncode": 0, "run_id": unit["run_id"], "command": [], "result_keys": []}

    def fake_judge(self, unit, *, command="run", dry_run=False):
        calls.append("judge")
        return {"status": "INCOMPLETE", "returncode": 0, "command": [], "result_keys": []}

    def fake_provider_audit(self, unit, *, phase="final"):
        calls.append(f"provider-audit-{phase}")
        return {"status": "PASS", "returncode": 0, "command": [], "result_keys": []}

    def unexpected_metrics(self, unit):
        raise AssertionError("metrics must not run after an incomplete Judge")

    monkeypatch.setattr(module, "ServiceController", FakeServiceController)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_runner", fake_runner)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_provider_audit", fake_provider_audit)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_judge", fake_judge)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_metrics", unexpected_metrics)
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        text_only_semantic_audit=signoff,
        campaign_id="test-incomplete-judge",
        execute=True,
    )

    result = module.CampaignOrchestrator(config).run()

    assert result["status"] == "BLOCKED"
    assert calls == ["runner", "provider-audit-runner", "judge"]
    assert result["units"][0]["status"] == "blocked"


def test_execute_blocks_when_unified_metrics_are_partial(tmp_path, monkeypatch):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    signoff = tmp_path / "audit.json"
    _write_text_only_signoff(module, package, signoff)

    class FakeServiceController:
        def __init__(self, orchestrator):
            self.orchestrator = orchestrator

        def ensure(self, platform):
            return None

        def stop_active(self):
            return None

    calls = []

    def fake_runner(self, unit, stage, *, dry_run):
        calls.append("runner")
        return {"status": "SUCCESS", "returncode": 0, "run_id": unit["run_id"], "command": [], "result_keys": []}

    def fake_judge(self, unit, *, command="run", dry_run=False):
        calls.append("judge")
        return {"status": "COMPLETE", "returncode": 0, "command": [], "result_keys": []}

    def fake_provider_audit(self, unit, *, phase="final"):
        calls.append(f"provider-audit-{phase}")
        return {"status": "PASS", "returncode": 0, "command": [], "result_keys": []}

    def fake_metrics(self, unit):
        calls.append("metrics")
        return {"status": "PARTIAL", "returncode": 0, "command": [], "result_keys": []}

    monkeypatch.setattr(module, "ServiceController", FakeServiceController)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_runner", fake_runner)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_provider_audit", fake_provider_audit)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_judge", fake_judge)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_metrics", fake_metrics)
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        text_only_semantic_audit=signoff,
        campaign_id="test-partial-metrics",
        execute=True,
    )

    result = module.CampaignOrchestrator(config).run()

    assert result["status"] == "BLOCKED"
    assert calls == ["runner", "provider-audit-runner", "judge", "provider-audit-final", "metrics"]
    assert result["units"][0]["status"] == "blocked"


def test_execute_blocks_before_metrics_when_final_provider_audit_fails(tmp_path, monkeypatch):
    module = _module()
    package = ROOT / "datasets/moi-rag-bench-v0.1-raw-corpus/text_only_ready_for_eval"
    signoff = tmp_path / "audit.json"
    _write_text_only_signoff(module, package, signoff)

    class FakeServiceController:
        def __init__(self, orchestrator):
            self.orchestrator = orchestrator

        def ensure(self, platform):
            return None

        def stop_active(self):
            return None

    calls = []

    def fake_runner(self, unit, stage, *, dry_run):
        calls.append("runner")
        return {"status": "SUCCESS", "returncode": 0, "run_id": unit["run_id"], "command": [], "result_keys": []}

    def fake_judge(self, unit, *, command="run", dry_run=False):
        calls.append("judge")
        return {"status": "COMPLETE", "returncode": 0, "command": [], "result_keys": []}

    def fake_provider_audit(self, unit, *, phase="final"):
        calls.append(f"provider-audit-{phase}")
        status = "PASS" if phase == "runner" else "FAIL"
        return {"status": status, "returncode": 0, "command": [], "result_keys": []}

    def unexpected_metrics(self, unit):
        raise AssertionError("metrics must not run after a failed final provider audit")

    monkeypatch.setattr(module, "ServiceController", FakeServiceController)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_runner", fake_runner)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_provider_audit", fake_provider_audit)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_judge", fake_judge)
    monkeypatch.setattr(module.CampaignOrchestrator, "_invoke_metrics", unexpected_metrics)
    config = module.CampaignConfig(
        repo_root=ROOT,
        package_root=package,
        output_root=tmp_path / "artifacts",
        checkpoint=tmp_path / "campaign.json",
        progress_log=tmp_path / "progress.jsonl",
        text_only_semantic_audit=signoff,
        campaign_id="test-final-provider-audit",
        execute=True,
    )

    result = module.CampaignOrchestrator(config).run()

    assert result["status"] == "BLOCKED"
    assert calls == ["runner", "provider-audit-runner", "judge", "provider-audit-final"]
    assert result["units"][0]["status"] == "blocked"
