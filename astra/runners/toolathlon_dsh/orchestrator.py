from __future__ import annotations

import hashlib
import os
import re
import time
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from astra.runners.toolathlon_verified import model_proxy
from astra.runners.toolathlon_verified.adapter_common import AdapterOutcome
from astra.runners.toolathlon_verified.artifact_contract import ARTIFACT_SCHEMA_VERSION
from astra.runners.toolathlon_verified.bundle import write_public_bundle
from astra.runners.toolathlon_verified.contract import (
    ContractError,
    JsonlEventWriter,
    ModelFreeze,
    assert_no_secret_values,
    canonical_json_sha256,
    read_json_object,
    sha256_file,
    utc_now,
    write_json_atomic,
)
from astra.runners.toolathlon_verified.mcp_client import capture_tool_manifest
from astra.runners.toolathlon_verified.model_proxy import (
    ModelProxyConfig,
    provider_credential_fingerprint,
    wait_for_model_requests_to_settle,
)
from astra.runners.toolathlon_verified.orchestrator import (
    _failure_category,
    _failure_evidence,
    _redact_if_needed,
    _run_evaluator,
    _write_official_trajectory,
)
from astra.runners.toolathlon_verified.resources import ResourceSampler
from astra.runners.toolathlon_verified.trajectory import normalize_product_events

from .dsh_adapter import DSH_KEY_ENV, DSH_NODE_VERSION, DSH_VERSION, DshRuntime, run_dsh
from .node_model_proxy import NODE_MODEL_PROXY, NodeModelProxyServer


@dataclass(frozen=True)
class DshSpec:
    system_id: str
    experiment_id: str
    run_id: str
    task_id: str
    bundle_file: Path
    gateway_url: str
    workspace: Path
    output_dir: Path
    deadline_s: int
    max_model_requests: int
    model_freeze_path: Path
    task_requirements_manifest_path: Path
    permission_policy_path: Path


def _load_spec(raw: dict[str, Any]) -> DshSpec:
    run = raw.get("run")
    if not isinstance(run, dict) or run.get("system_id") != "dsh":
        raise ContractError("DSH orchestrator requires run.system_id=dsh")
    try:
        spec = DshSpec(
            system_id="dsh",
            experiment_id=str(run["experiment_id"]),
            run_id=str(run["run_id"]),
            task_id=str(run["task_id"]),
            bundle_file=Path(str(run["bundle_file"])).resolve(),
            gateway_url=str(run["gateway_url"]),
            workspace=Path(str(run["workspace"])).resolve(),
            output_dir=Path(str(run["output_dir"])).resolve(),
            deadline_s=int(run["deadline_s"]),
            max_model_requests=int(run["max_model_requests"]),
            model_freeze_path=Path(str(run["model_freeze"])).resolve(),
            task_requirements_manifest_path=Path(
                str(run["task_requirements_manifest"])
            ).resolve(),
            permission_policy_path=Path(str(run["permission_policy"])).resolve(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("invalid DSH run specification") from exc
    if spec.deadline_s not in {1800, 2700, 3600, 5400}:
        raise ContractError("DSH deadline is not a frozen Toolathlon runtime tier")
    if spec.max_model_requests != 100:
        raise ContractError("DSH max_model_requests must equal 100")
    if not spec.bundle_file.is_file() or not spec.workspace.is_dir():
        raise ContractError("DSH bundle or workspace is unavailable")
    return spec


def _dsh_name(value: str) -> str:
    joined = f"mcp__toolathlon__{value}"
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", joined)
    if normalized == joined and len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(f"toolathlon\0{value}".encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:64 - 12 - 1]}_{digest}"


def _add_dsh_tool_names(manifest: dict[str, Any], destination: Path) -> list[str]:
    names: list[str] = []
    rows = manifest.get("tools")
    if not isinstance(rows, list):
        raise ContractError("tools/list manifest has no tools array")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("gateway_tool_name"), str):
            raise ContractError("invalid tools/list row")
        name = _dsh_name(row["gateway_tool_name"])
        row["dsh_model_visible_tool_name"] = name
        names.append(name)
    names.sort()
    if len(names) != len(set(names)):
        manifest["dsh_run_qualification"] = "no_go_name_collision"
        raise ContractError("DSH-visible MCP tool names collide")
    # capture_tool_manifest also records the legacy Astra/Hermes projections.
    # Their lossy names may collide even though DSH's hashed projection does
    # not, so keep the DSH qualification independent and preserve both facts.
    manifest["dsh_run_qualification"] = "go"
    manifest["dsh_model_visible_tool_names_sha256"] = canonical_json_sha256(names)
    write_json_atomic(destination, manifest, mode=0o644)
    return names


def _adapter_failure(exc: BaseException, duration: float) -> AdapterOutcome:
    return AdapterOutcome(
        terminal_status="crashed",
        product_exit_code=None,
        termination_reason="adapter_error",
        output="",
        error=f"{type(exc).__name__}: {exc}",
        duration_seconds=duration,
        product_pid=None,
        escalated_to_sigkill=False,
        native_events=[],
        metadata={"adapter_exception_type": type(exc).__name__},
    )


def run(
    config_path: Path,
    *,
    before_evaluator: Callable[[AdapterOutcome], None] | None = None,
    lifecycle_writer: JsonlEventWriter | None = None,
    resource_sampler: ResourceSampler | None = None,
    on_product_pid: Callable[[int], None] | None = None,
) -> int:
    started_at = utc_now()
    raw = read_json_object(config_path)
    spec = _load_spec(raw)
    model_freeze = ModelFreeze.load(spec.model_freeze_path)
    tiers_path = Path(str(raw["task_runtime_tiers"])).resolve()
    tier = read_json_object(tiers_path).get("tasks", {}).get(spec.task_id)
    if not isinstance(tier, dict) or tier.get("deadline_seconds") != spec.deadline_s:
        raise ContractError("DSH run deadline does not match task runtime tier")
    key = os.environ.get(DSH_KEY_ENV, "")
    if len(key) < 16 or any(ord(character) < 33 for character in key):
        raise ContractError(f"missing or invalid {DSH_KEY_ENV}")

    # Reuse the shared model freeze and artifact contract while the DSH slot
    # runs its transport on the same Node/Undici stack as the product.
    model_proxy.PROVIDER_KEY_ENV_BY_SYSTEM["dsh"] = DSH_KEY_ENV
    runtime = DshRuntime.load_from_environment()
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    events = JsonlEventWriter(
        spec.output_dir / "adapter-events.jsonl",
        run_id=spec.run_id,
        system_id="dsh",
    )
    events.append("run.preparing", task_id=spec.task_id)
    public_path = spec.output_dir / "task-bundle.public.json"
    public_bundle = write_public_bundle(
        spec.bundle_file,
        public_path,
        expected_task_id=spec.task_id,
        workspace=spec.workspace,
    )
    if lifecycle_writer is not None:
        lifecycle_writer.append("tools_list.start")
    observed_path = spec.output_dir / "tool-schema-observed.json"
    observed = capture_tool_manifest(
        task_id=spec.task_id,
        gateway_url=spec.gateway_url,
        destination=observed_path,
        timeout_s=600,
    )
    tool_names = _add_dsh_tool_names(observed, observed_path)
    if lifecycle_writer is not None:
        lifecycle_writer.append(
            "tools_list.end", status="passed", tool_set_sha256=observed["tool_set_sha256"]
        )

    replacement = raw.get("replacement_for_run_id")
    resolved = {
        "schema_version": 1,
        "benchmark_status": "exploratory_dsh_only",
        "started_at": started_at,
        "experiment_id": spec.experiment_id,
        "run_id": spec.run_id,
        "task_id": spec.task_id,
        "system_id": "dsh",
        "pair_id": f"{spec.experiment_id}:{spec.task_id}",
        "replacement_for_run_id": replacement,
        "runtime": {
            "product": "dsh-headless",
            "version": DSH_VERSION,
            "node_version": DSH_NODE_VERSION,
            "node": str(runtime.node),
            "root": str(runtime.root),
            "bridge": str(runtime.bridge),
            "node_sha256": sha256_file(runtime.node),
            "cli_sha256": sha256_file(runtime.root / "apps/cli/lib/bin.js"),
            "headless_sha256": sha256_file(
                runtime.root / "packages/bundle/headless/lib/index.js"
            ),
            "bridge_sha256": sha256_file(runtime.bridge),
            "model_proxy": str(NODE_MODEL_PROXY),
            "model_proxy_transport": "node_fetch_undici",
        },
        "freeze": {
            "model_sha256": sha256_file(spec.model_freeze_path),
            "runtime_tiers_sha256": sha256_file(tiers_path),
            "task_requirements_sha256": sha256_file(
                spec.task_requirements_manifest_path
            ),
        },
        "prompt": public_bundle["prompt"],
        "budget": {
            "tier": tier.get("tier"),
            "agent_deadline_seconds": spec.deadline_s,
            "max_product_model_requests": spec.max_model_requests,
        },
        "model": {
            "provider": model_freeze.provider,
            "request_id": model_freeze.request_model_id,
            "temperature": model_freeze.temperature,
            "thinking": model_freeze.thinking,
            "reasoning_effort": model_freeze.reasoning_effort,
            "credential": {
                "selected_environment": DSH_KEY_ENV,
                "selected_fingerprint": provider_credential_fingerprint(key),
                "values_logged": False,
            },
        },
        "adapter": {
            "fresh_state": True,
            "resume": False,
            "tool_set_sha256": observed["tool_set_sha256"],
            "tool_exposure": {
                "scope": "current_task_attempt_only",
                "mechanism": "dsh_headless_stdio_classic_sse_bridge",
                "gateway_url": spec.gateway_url,
                "mcp_tool_count": len(tool_names),
                "mcp_tool_names_sha256": canonical_json_sha256(tool_names),
                "all_observed_task_mcp_tools_required": True,
                "other_task_mcp_tools_allowed": False,
                "product_builtin_tools_retained": True,
                "provider_request_tool_names_recorded": True,
                "dsh_run_qualification": observed["dsh_run_qualification"],
                "legacy_projection_collisions": observed.get("collisions", []),
            },
            "product_identity": {
                "attempt_ordinal": 2 if replacement else 1,
                "attempt_label": "a2" if replacement else "a1",
                "strategy": "dsh_fresh_ephemeral_headless_home",
                "provider_user_id_is_product_identity": False,
            },
        },
    }
    write_json_atomic(spec.output_dir / "resolved-config.json", resolved, mode=0o644)

    product_pid: list[int | None] = [None]
    sampler = resource_sampler or ResourceSampler(
        spec.output_dir / "resource-usage.jsonl",
        run_id=spec.run_id,
        system_id="dsh",
        product_pid=lambda: product_pid[0],
    )
    proxy_config = ModelProxyConfig(
        upstream_base_url=model_freeze.provider_base_url,
        upstream_api_key=key,
        effective_model=model_freeze.request_model_id,
        temperature=model_freeze.temperature,
        thinking=model_freeze.thinking,
        reasoning_effort=model_freeze.reasoning_effort,
        max_requests=spec.max_model_requests,
        run_id=spec.run_id,
        system_id="dsh",
        events_path=spec.output_dir / "model-usage.jsonl",
        state_path=spec.output_dir / "model-proxy-state.json",
    )
    agent_started = [False]

    def mark_started() -> None:
        if not agent_started[0]:
            agent_started[0] = True
            events.append("agent.execution_start", deadline_seconds=spec.deadline_s)

    if lifecycle_writer is not None:
        lifecycle_writer.append("adapter.start")
    adapter_started = time.monotonic()
    sampler_context = nullcontext(sampler) if resource_sampler is not None else sampler
    with sampler_context, NodeModelProxyServer(proxy_config, node=runtime.node) as proxy:
        try:
            outcome = run_dsh(
                runtime=runtime,
                public_bundle=public_bundle,
                gateway_url=spec.gateway_url,
                workspace=spec.workspace,
                output_dir=spec.output_dir,
                proxy_url=proxy.url,
                deadline_seconds=spec.deadline_s,
                budget_exceeded=proxy.budget.exceeded.is_set,
                model_request_snapshot=proxy.budget.snapshot,
                task_mcp_tool_names=tool_names,
                on_product_pid=lambda pid: (
                    product_pid.__setitem__(0, pid),
                    on_product_pid(pid) if on_product_pid is not None else None,
                ),
                on_agent_start=mark_started,
            )
            outcome.metadata["post_terminal_model_drain"] = wait_for_model_requests_to_settle(
                proxy.budget.snapshot, context="DSH post-terminal"
            )
        except BaseException as exc:
            outcome = _adapter_failure(exc, time.monotonic() - adapter_started)
            (spec.output_dir / "adapter.stderr.log").write_text(
                "".join(traceback.format_exception(exc)), encoding="utf-8"
            )

    if agent_started[0]:
        events.append(
            "agent.execution_end",
            terminal_status=outcome.terminal_status,
            termination_reason=outcome.termination_reason,
        )
    else:
        events.append("agent.execution_not_started", terminal_status=outcome.terminal_status)
    trajectory = normalize_product_events(
        outcome.native_events,
        run_id=spec.run_id,
        system_id="dsh",
        trajectory_path=spec.output_dir / "trajectory.jsonl",
        tool_calls_path=spec.output_dir / "tool-calls.jsonl",
        observed_tool_manifest=observed,
    )
    for path, value in (
        (spec.output_dir / "adapter.stdout.log", outcome.output),
        (spec.output_dir / "adapter.stderr.log", outcome.error or ""),
    ):
        if not path.exists():
            path.write_text(value, encoding="utf-8")
    _write_official_trajectory(spec, outcome=outcome, trajectory_summary=trajectory)
    if lifecycle_writer is not None:
        lifecycle_writer.append(
            "adapter.end",
            status=outcome.terminal_status,
            termination_reason=outcome.termination_reason,
        )

    inherited_key = os.environ.pop(DSH_KEY_ENV, None)
    try:
        if before_evaluator is not None:
            before_evaluator(outcome)
        events.append("evaluator.start")
        if lifecycle_writer is not None:
            lifecycle_writer.append("evaluator.start")
        evaluator = _run_evaluator(
            raw.get("evaluator"),
            spec=spec,
            public_bundle_path=public_path,
            evaluator_timeout_seconds=3600,
            agent_exit_code=0 if outcome.terminal_status == "completed" else 1,
        )
    finally:
        if inherited_key is not None:
            os.environ[DSH_KEY_ENV] = inherited_key
    events.append("evaluator.end", verify_status=evaluator["verify_status"])
    if lifecycle_writer is not None:
        lifecycle_writer.append("evaluator.end", verify_status=evaluator["verify_status"])

    failure = _failure_category(outcome, evaluator)
    validity = (
        "infra_invalid"
        if failure in {"adapter_error", "environment_error", "evaluator_error"}
        else "valid"
    )
    run_record = {
        "schema_version": 1,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "benchmark_status": "exploratory_dsh_only",
        "experiment_id": spec.experiment_id,
        "run_id": spec.run_id,
        "system_id": "dsh",
        "task_id": spec.task_id,
        "pair_id": f"{spec.experiment_id}:{spec.task_id}",
        "started_at": started_at,
        "finished_at": utc_now(),
        "terminal_status": outcome.terminal_status,
        "termination_reason": outcome.termination_reason,
        "product_exit_code": outcome.product_exit_code,
        "timeout": outcome.terminal_status == "timeout",
        "deadline_s": spec.deadline_s,
        "agent_duration_seconds": outcome.duration_seconds,
        "verify_status": evaluator["verify_status"],
        "reward": evaluator["reward"],
        "evaluator_exit_code": evaluator["exit_code"],
        "evaluator_error": evaluator["error"],
        "evaluator_duration_seconds": evaluator["duration_seconds"],
        "run_validity": validity,
        "primary_failure_category": failure,
        "adapter": outcome.metadata,
        "trajectory": trajectory,
        "model_budget": read_json_object(spec.output_dir / "model-proxy-state.json")[
            "budget"
        ],
        "artifact_gate": {"status": "pending_cleanup_and_validation"},
    }
    write_json_atomic(
        spec.output_dir / "failure-evidence.json",
        _failure_evidence(
            spec,
            primary_failure=failure,
            outcome=outcome,
            evaluator=evaluator,
        ),
        mode=0o644,
    )
    write_json_atomic(spec.output_dir / "run.json", run_record, mode=0o644)
    candidates = [path for path in spec.output_dir.rglob("*") if path.is_file()]
    _redact_if_needed(candidates, [key])
    assert_no_secret_values(candidates, [key])
    return 0 if validity == "valid" else 2
