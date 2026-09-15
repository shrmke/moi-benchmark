from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .adapter_common import (
    AdapterOutcome,
    EphemeralState,
    read_product_json_output,
    strip_provider_credentials,
    write_astra_credentials,
)
from .contract import (
    ContractError,
    canonical_json_sha256,
    read_json_object,
    sha256_file,
    write_json_atomic,
)
from .process_control import run_monitored_process
from .trajectory import read_json_rows


from .astra_adapter import AstraRuntime, astra_task_mcp_tool_names, ASTRA_RUNTIME_MCP_BINDING_FILENAME

def write_astra_runtime_mcp_binding(
    output_dir: Path,
    observed_tool_manifest: dict[str, Any],
    *,
    gateway_url: str,
) -> dict[str, Any]:
    """Freeze Astra's native request-scoped MCP binding for this attempt."""

    names = astra_task_mcp_tool_names(observed_tool_manifest)
    path = output_dir / ASTRA_RUNTIME_MCP_BINDING_FILENAME
    binding = {
        "schema_version": "toolathlon.astra-runtime-mcp-binding.v1",
        "endpoint": "/chat/stream",
        "runtime_profile": "request_scoped_runtime_mcp",
        "binding": {
            "id": "toolathlon",
            "transport": "sse",
            "url": gateway_url,
            "headers_present": False,
            "auth_token_present": False,
        },
        "interaction_mode": "auto",
        "expected_mcp_tool_names": names,
        "expected_mcp_tool_names_sha256": canonical_json_sha256(names),
    }
    write_json_atomic(path, binding, mode=0o644)
    return {
        "path": path,
        "tool_names": names,
        "tool_names_sha256": canonical_json_sha256(names),
        "binding_sha256": sha256_file(path),
    }


def _run_admin(
    runtime: AstraRuntime,
    credentials_dir: Path,
    args: list[str],
    *,
    home: Path,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    strip_provider_credentials(env)
    env.pop("ASTRA_ACCESS_TOKEN", None)
    env.pop(runtime.admin_token_env, None)
    env.update(
        {
            "HOME": str(home),
            "ASTRA_CLI_CREDENTIALS_DIR": str(credentials_dir),
            "ASTRA_API_URL": runtime.api_url,
        }
    )
    return subprocess.run(
        [str(runtime.executable), "--api-url", runtime.api_url, "admin", *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _configure_run_model(
    runtime: AstraRuntime,
    *,
    credentials_dir: Path,
    home: Path,
    proxy_url: str,
) -> dict[str, Any]:
    model = "deepseek-v4-flash"
    shown = _run_admin(runtime, credentials_dir, ["model", "show", model], home=home)
    if shown.returncode != 0:
        raise ContractError(
            "Astra benchmark model is not pre-provisioned; runtime model add is forbidden"
        )
    quirks = json.dumps(
        {"wire_model_name": model, "fallback_chain": []},
        separators=(",", ":"),
    )
    updated = _run_admin(
        runtime,
        credentials_dir,
        [
            "model",
            "update",
            model,
            "--base-url",
            proxy_url,
            "--active",
            "true",
            "--quirks",
            quirks,
        ],
        home=home,
    )
    if updated.returncode != 0:
        raise RuntimeError(
            f"Astra model update failed with exit code {updated.returncode}"
        )
    return {
        "action": "update_without_probe",
        "model": model,
        "provider": "openai",
        "wire_model_name": model,
        "base_url": proxy_url,
        "fallback_chain": [],
        "active": True,
        "api_key_updated": False,
        "connectivity_probe_expected": False,
    }


def run_astra(
    *,
    runtime: AstraRuntime,
    public_bundle: dict[str, Any],
    gateway_url: str,
    workspace: Path,
    output_dir: Path,
    proxy_url: str,
    deadline_seconds: int,
    budget_exceeded: Callable[[], bool],
    model_request_snapshot: Callable[[], dict[str, Any]],
    experiment_id: str,
    task_id: str,
    run_id: str,
    attempt_ordinal: int,
    runtime_mcp_binding_path: Path,
    task_mcp_tool_names: list[str],
    on_product_pid: Callable[[int], None] | None = None,
    on_agent_start: Callable[[], None] | None = None,
    ephemeral_root: Path | None = None,
) -> AdapterOutcome:
    admin_token = os.environ.get(runtime.admin_token_env, "")
    if runtime.configure_model and not admin_token:
        raise ContractError(f"missing Astra admin credential env {runtime.admin_token_env}")

    prompt = public_bundle["prompt"]
    runtime_mcp_binding_path = runtime_mcp_binding_path.resolve()
    if runtime_mcp_binding_path != (
        output_dir / ASTRA_RUNTIME_MCP_BINDING_FILENAME
    ).resolve():
        raise ContractError("Astra runtime MCP binding must be an output artifact")
    binding = read_json_object(runtime_mcp_binding_path)
    expected_binding = {
        "schema_version": "toolathlon.astra-runtime-mcp-binding.v1",
        "endpoint": "/chat/stream",
        "runtime_profile": "request_scoped_runtime_mcp",
        "binding": {
            "id": "toolathlon",
            "transport": "sse",
            "url": gateway_url,
            "headers_present": False,
            "auth_token_present": False,
        },
        "interaction_mode": "auto",
        "expected_mcp_tool_names": task_mcp_tool_names,
        "expected_mcp_tool_names_sha256": canonical_json_sha256(
            task_mcp_tool_names
        ),
    }
    if binding != expected_binding:
        raise ContractError("Astra runtime MCP binding does not match current tools/list")
    if task_mcp_tool_names != sorted(set(task_mcp_tool_names)):
        raise ContractError("Astra task MCP tool names must be sorted and unique")
    if any(
        not name.startswith("mcp__toolathlon__") for name in task_mcp_tool_names
    ):
        raise ContractError("Astra task tool settings contain a non-Toolathlon MCP name")
    stdout_path = output_dir / "adapter.stdout.log"
    stderr_path = output_dir / "adapter.stderr.log"

    from .product_identity import private_identity_projection, provision_astra_identity

    identity = provision_astra_identity(
        api_url=runtime.api_url,
        output_dir=output_dir,
        experiment_id=experiment_id,
        task_id=task_id,
        run_id=run_id,
        attempt_ordinal=attempt_ordinal,
    )

    with EphemeralState(prefix="toolathlon-astra-", preferred_root=ephemeral_root) as state:
        home = state.path / "home"
        home.mkdir(mode=0o700)
        model_registration: dict[str, Any] | None = None
        if runtime.configure_model:
            admin_credentials = state.path / "admin-credentials"
            write_astra_credentials(admin_credentials, admin_token)
            model_registration = _configure_run_model(
                runtime,
                credentials_dir=admin_credentials,
                home=home,
                proxy_url=proxy_url,
            )
        setup_model_requests = int(
            model_request_snapshot().get("provider_requests_forwarded", -1)
        )
        if setup_model_requests != 0:
            raise ContractError(
                "Astra forwarded a model request before Agent execution"
            )

        transport_client = Path(__file__).with_name(
            "astra_969550b_runtime_mcp_client.py"
        ).resolve()
        if not transport_client.is_file():
            raise ContractError("Astra runtime MCP transport client is missing")
        argv = [
            sys.executable,
            str(transport_client),
            "--api-url",
            runtime.api_url,
            "--gateway-url",
            gateway_url,
            "--model",
            "deepseek-v4-flash",
        ]
        env = dict(os.environ)
        strip_provider_credentials(env)
        env.pop("ASTRA_ACCESS_TOKEN", None)
        env.pop(runtime.admin_token_env, None)
        env.update(
            {
                "HOME": str(home),
                "ASTRA_API_URL": runtime.api_url,
                "ASTRA_ACCESS_TOKEN": identity.access_token,
                "ASTRA_LOG_FORMAT": "json",
            }
        )
        setup_model_requests = int(
            model_request_snapshot().get("provider_requests_forwarded", -1)
        )
        if setup_model_requests != 0:
            raise ContractError(
                "Astra forwarded a model request before Agent execution"
            )
        # Provider secrets are held only by the model proxy.
        result = run_monitored_process(
            argv,
            cwd=workspace,
            env=env,
            stdin_payload=json.dumps(
                {"system": prompt["system"], "task": prompt["task"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            deadline_seconds=deadline_seconds,
            budget_exceeded=budget_exceeded,
            on_start=on_product_pid,
            on_agent_start=on_agent_start,
        )
        native = list(read_json_rows(stderr_path))
        product = read_product_json_output(stdout_path)
        output = ""
        error: str | None = None
        server_session_id: str | None = None
        if product is not None:
            output_value = product.get("response") or product.get("output") or product.get("content")
            if isinstance(output_value, str):
                output = output_value
            error_value = product.get("error")
            if error_value:
                error = str(error_value)
            session_value = product.get("session_id")
            if isinstance(session_value, str) and session_value:
                server_session_id = session_value
        if result.termination_reason == "agent_deadline":
            terminal_status = "timeout"
            termination_reason = result.termination_reason
        elif result.termination_reason == "max_model_requests":
            terminal_status = "max_steps"
            termination_reason = result.termination_reason
        elif result.return_code == 0:
            terminal_status = "completed"
            termination_reason = result.termination_reason
        else:
            terminal_status = "crashed"
            termination_reason = result.termination_reason
            error = error or f"Astra exited with code {result.return_code}"
        metadata = {
            "ephemeral_state_on_tmpfs": state.on_tmpfs,
            "product_identity": private_identity_projection(
                output_dir / "product-identity.private.json"
            ),
            "model_registration": model_registration,
            "setup_provider_requests_before_agent": setup_model_requests,
            "server_session": {
                "strategy": "native_chat_stream_auto_create",
                "requested_session_id": None,
                "observed_session_id": server_session_id,
            },
            "command": {
                "argv_without_prompt": [
                    "<python>" if item == sys.executable else "<astra-api-client>"
                    if item == str(transport_client)
                    else item
                    for item in argv
                ],
                "max_turns_source": "astra_internal_default",
                "adapter_retry_count": 0,
                "task_tool_exposure": {
                    "policy": "native_request_scoped_runtime_mcp",
                    "mcp_tool_count": len(task_mcp_tool_names),
                    "mcp_tool_names_sha256": canonical_json_sha256(
                        task_mcp_tool_names
                    ),
                    "binding_artifact": ASTRA_RUNTIME_MCP_BINDING_FILENAME,
                    "binding_sha256": sha256_file(runtime_mcp_binding_path),
                    "other_task_mcp_tools_allowed": False,
                    "astra_builtin_tools_retained": True,
                },
                "astra_agent_loop_owner": "astra_969550b_server",
                "transport_shim_agent_logic": False,
            },
        }
        return AdapterOutcome(
            terminal_status=terminal_status,
            product_exit_code=result.return_code,
            termination_reason=termination_reason,
            output=output,
            error=error,
            duration_seconds=result.duration_seconds,
            product_pid=result.terminated_pid,
            escalated_to_sigkill=result.escalated_to_sigkill,
            native_events=native,
            metadata=metadata,
            sensitive_values=(identity.access_token, identity.password),
        )
