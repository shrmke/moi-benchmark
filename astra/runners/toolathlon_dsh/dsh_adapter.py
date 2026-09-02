from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from astra.runners.toolathlon_verified.adapter_common import (
    AdapterOutcome,
    EphemeralState,
    strip_provider_credentials,
)
from astra.runners.toolathlon_verified.contract import (
    ContractError,
    canonical_json_sha256,
    sha256_file,
)
from astra.runners.toolathlon_verified.lifecycle import TASK_IMAGE
from astra.runners.toolathlon_verified.process_control import run_monitored_process
from astra.runners.toolathlon_verified.trajectory import read_json_rows


DSH_VERSION = "0.1.0-rc.7"
DSH_NODE_VERSION = "22.19.0"
DSH_KEY_ENV = "TOOLATHLON_DEEPSEEK_DSH_API_KEY"
CONTAINER_WORKSPACE = "/workspace/dumps/workspace"
CONTAINER_DSH_ROOT = "/opt/dsh"
CONTAINER_NODE_ROOT = "/opt/node"
CONTAINER_STATE = "/run/dsh-state"
CONTAINER_BRIDGE = "/opt/toolathlon/classic_sse_stdio_bridge.mjs"
CONTAINER_NODE = f"{CONTAINER_NODE_ROOT}/node"
DOCKER = Path("/usr/bin/docker")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")

_SANDBOX_PREFLIGHT = """\
for path in \
  /home/vagrant/dataset/Toolathlon \
  /home/vagrant/moi-benchmark \
  /home/vagrant/deepseek-harness \
  /tmp/toolathlon_src \
  /var/run/docker.sock
do
  if [ -e "$path" ]; then
    echo "DSH sandbox exposed forbidden host path: $path" >&2
    exit 78
  fi
done
if [ ! -d /workspace/dumps/workspace ]; then
  echo "DSH sandbox workspace mount is unavailable" >&2
  exit 78
fi
exec "$@"
"""


@dataclass(frozen=True)
class DshRuntime:
    node: Path
    root: Path
    bridge: Path

    @classmethod
    def load_from_environment(cls) -> "DshRuntime":
        node_value = os.environ.get("TOOLATHLON_DSH_NODE", "")
        root_value = os.environ.get("TOOLATHLON_DSH_ROOT", "")
        if not node_value:
            raise ContractError("missing TOOLATHLON_DSH_NODE")
        if not root_value:
            raise ContractError("missing TOOLATHLON_DSH_ROOT")
        node = Path(node_value).resolve()
        root = Path(root_value).resolve()
        bridge = Path(__file__).with_name("classic_sse_stdio_bridge.mjs").resolve()
        if not node.is_file() or not os.access(node, os.X_OK):
            raise ContractError(f"DSH Node executable is unavailable: {node}")
        if not root.is_dir() or root.is_symlink():
            raise ContractError(f"DSH runtime root is unavailable or symlinked: {root}")
        for relative in (
            "apps/cli/lib/bin.js",
            "apps/cli/package.json",
            "packages/bundle/headless/lib/index.js",
        ):
            if not (root / relative).is_file():
                raise ContractError(f"DSH runtime artifact is unavailable: {root / relative}")
        if not bridge.is_file() or bridge.is_symlink():
            raise ContractError(f"DSH MCP bridge is unavailable: {bridge}")
        completed = subprocess.run(
            [str(node), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        expected = f"v{DSH_NODE_VERSION}"
        if completed.returncode != 0 or completed.stdout.strip() != expected:
            raise ContractError(
                f"DSH runtime requires Node {expected}; got {completed.stdout.strip()!r}"
            )
        package = json.loads((root / "apps/cli/package.json").read_text(encoding="utf-8"))
        if package.get("version") != DSH_VERSION:
            raise ContractError(
                f"DSH runtime must be exactly {DSH_VERSION}; got {package.get('version')!r}"
            )
        if not DOCKER.is_file() or not os.access(DOCKER, os.X_OK):
            raise ContractError(f"DSH container launcher is unavailable: {DOCKER}")
        return cls(node=node, root=root, bridge=bridge)


def _bind_mount(source: Path, destination: str, *, readonly: bool = False) -> str:
    option = f"type=bind,src={source},dst={destination}"
    return f"{option},readonly" if readonly else option


def _read_container_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractError(f"could not read DSH sidecar cidfile: {exc}") from exc
    if not value:
        return None
    if _CONTAINER_ID.fullmatch(value) is None:
        raise ContractError("DSH sidecar cidfile contains an invalid container ID")
    return value


def _inspect_container_pid(
    container_name: str, environment: dict[str, str]
) -> tuple[str, int] | None:
    try:
        completed = subprocess.run(
            [
                str(DOCKER),
                "inspect",
                "--format",
                "{{.Id}} {{.State.Pid}}",
                container_name,
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = completed.stdout.strip().split()
    if completed.returncode != 0 or len(fields) != 2:
        return None
    container_id, raw_pid = fields
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if _CONTAINER_ID.fullmatch(container_id) is None or pid <= 0:
        return None
    return container_id, pid


def _observe_container_pid(
    *,
    container_name: str,
    environment: dict[str, str],
    stop: threading.Event,
    observed: list[tuple[str, int] | None],
    callback: Callable[[int], None],
) -> None:
    while not stop.wait(0.05):
        identity = _inspect_container_pid(container_name, environment)
        if identity is None:
            continue
        observed[0] = identity
        callback(identity[1])
        return


def _remove_dsh_container(container_name: str, environment: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [str(DOCKER), "rm", "-f", container_name],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"could not clean up DSH sidecar: {exc}") from exc
    if completed.returncode == 0:
        return "removed"
    details = completed.stdout.strip()
    if "No such container" in details or "No such object" in details:
        return "already_absent"
    raise ContractError(
        f"could not clean up DSH sidecar {container_name}: {details or completed.returncode}"
    )


def _profile_patch() -> str:
    return """\
- id: system-prompt
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    persona: !!js process.env.TOOLATHLON_DSH_SYSTEM_PROMPT

- id: agent-instructions
  disabled: true

- id: session-title-llm
  disabled: true

- id: llm-deepseek
  config:
    retryPolicy:
      mode: normal
      maxRetries: 5
      backoff:
        initialDelayMs: 1000
        maxDelayMs: 16000

- id: session-telemetry-otel
  disabled: true

- id: web
  disabled: true

- id: web-search-deepseek
  disabled: true

- id: tool-web
  disabled: true

- id: session-persistence-jsonl
  config:
    root: !!js process.env.TOOLATHLON_DSH_SESSION_ROOT
    compression: none
    packChunks: false

- insert:
    - id: toolathlon-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: toolathlon
        command: !!js process.env.TOOLATHLON_DSH_NODE
        args:
          - !!js process.env.TOOLATHLON_DSH_BRIDGE
          - !!js process.env.TOOLATHLON_DSH_GATEWAY_URL
        env: {}
        cwd: !!js process.env.TOOLATHLON_DSH_WORKSPACE
        toolCallTimeoutMs: 120000
        failOnStartupError: true
        reconnect:
          enabled: false
          initialDelayMs: 1000
          maxDelayMs: 1000
          maxAttempts: 1
"""


def _assistant_content(data: dict[str, Any]) -> Any:
    message = data.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return data.get("content")


def _assistant_text(data: dict[str, Any]) -> str:
    content = _assistant_content(data)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


def _assistant_error(data: dict[str, Any]) -> str | None:
    message = data.get("message")
    if not isinstance(message, dict):
        message = data
    if message.get("stopReason") != "error":
        return None
    value = message.get("errorMessage")
    return value if isinstance(value, str) and value else "DSH assistant stopped with an error"


def _last_assistant_error(events: Iterable[dict[str, Any]]) -> str | None:
    for raw in reversed(list(events)):
        data = raw.get("data")
        if isinstance(data, dict):
            error = _assistant_error(data)
            if error is not None:
                return error
    return None


def _normalize_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    names_by_call: dict[str, str] = {}
    for raw in events:
        row = dict(raw)
        event_type = str(raw.get("type", "unknown"))
        data = raw.get("data")
        if not isinstance(data, dict):
            data = {}
        row["dsh_event"] = raw
        if raw.get("time") is not None:
            row["timestamp"] = raw["time"]
        if event_type == "tool/call":
            call_id = data.get("callId")
            name = data.get("name")
            if isinstance(call_id, str) and call_id:
                if isinstance(name, str) and name:
                    names_by_call[call_id] = name
                row["tool_call_id"] = call_id
                row["call_id"] = call_id
            if isinstance(name, str) and name:
                row["tool_name"] = name
                row["name"] = name
            if "arguments" in data:
                row["arguments"] = data["arguments"]
            row["event"] = "tool.execution_start"
        elif event_type == "tool/result":
            message = data.get("message")
            source = message.get("source") if isinstance(message, dict) else None
            call_id = data.get("callId")
            if not isinstance(call_id, str) and isinstance(source, dict):
                call_id = source.get("callId")
            if isinstance(call_id, str) and call_id:
                row["tool_call_id"] = call_id
                row["call_id"] = call_id
                if call_id in names_by_call:
                    row["tool_name"] = names_by_call[call_id]
                    row["name"] = names_by_call[call_id]
            is_error = data.get("isError") is True or data.get("error") is not None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    is_error = is_error or any(
                        isinstance(item, dict) and item.get("isError") is True
                        for item in content
                    )
            row["success"] = not is_error
            row["event"] = "tool.execution_error" if is_error else "tool.execution_end"
        elif event_type == "turn/end":
            row["event"] = "turn.end"
        else:
            row["event"] = event_type
            if event_type == "assistant/message":
                text = _assistant_text(data)
                if text:
                    row["assistant_text"] = text
        normalized.append(row)
    return normalized


def _session_files(home: Path) -> list[Path]:
    return sorted(
        path
        for path in home.rglob("session.jsonl")
        if path.is_file() and not path.is_symlink()
    )


def _last_assistant_text(events: Iterable[dict[str, Any]]) -> str:
    for raw in reversed(list(events)):
        if raw.get("type") != "assistant/message":
            continue
        data = raw.get("data")
        if isinstance(data, dict):
            text = _assistant_text(data)
            if text.strip():
                return text
    return ""


def _last_turn_reason(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for raw in reversed(list(events)):
        if raw.get("type") != "turn/end":
            continue
        data = raw.get("data")
        if isinstance(data, dict) and isinstance(data.get("reason"), dict):
            return data["reason"]
    return None


def run_dsh(
    *,
    runtime: DshRuntime,
    public_bundle: dict[str, Any],
    gateway_url: str,
    workspace: Path,
    output_dir: Path,
    proxy_url: str,
    deadline_seconds: int,
    budget_exceeded: Callable[[], bool],
    model_request_snapshot: Callable[[], dict[str, Any]],
    task_mcp_tool_names: list[str],
    on_product_pid: Callable[[int], None] | None = None,
    on_agent_start: Callable[[], None] | None = None,
) -> AdapterOutcome:
    stdout_path = output_dir / "adapter.stdout.log"
    stderr_path = output_dir / "adapter.stderr.log"
    prompt = public_bundle.get("prompt")
    if not isinstance(prompt, dict):
        raise ContractError("DSH public bundle has no prompt object")
    system_prompt = prompt.get("system")
    task_prompt = prompt.get("task")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ContractError("DSH Toolathlon system prompt is empty")
    if not isinstance(task_prompt, str) or not task_prompt.strip():
        raise ContractError("DSH Toolathlon task prompt is empty")
    if "\x00" in system_prompt or "\x00" in task_prompt:
        raise ContractError("DSH prompt contains a NUL byte")
    if workspace.is_symlink():
        raise ContractError("DSH workspace must not be a symlink")
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"DSH workspace is unavailable: {exc}") from exc
    if not workspace.is_dir():
        raise ContractError("DSH workspace must be a directory")

    with EphemeralState(prefix="toolathlon-dsh-") as state:
        home = state.path / "home"
        profile = home / "profiles" / "headless"
        profile.mkdir(mode=0o700, parents=True)
        (profile / "cordis.patch.yml").write_text(_profile_patch(), encoding="utf-8")
        os.chmod(profile / "cordis.patch.yml", 0o600)
        bridge_copy = state.path / "classic_sse_stdio_bridge.mjs"
        bridge_copy.write_bytes(runtime.bridge.read_bytes())
        bridge_copy.chmod(0o444)

        env = dict(os.environ)
        strip_provider_credentials(env)
        env.pop(DSH_KEY_ENV, None)
        env.pop("ASTRA_ADMIN_ACCESS_TOKEN", None)
        setup_requests = int(model_request_snapshot().get("provider_requests_forwarded", -1))
        if setup_requests != 0:
            raise ContractError("DSH forwarded a model request before Agent execution")

        container_name = f"toolathlon-dsh-agent-{state.path.name}"
        cidfile = state.path / "container.cid"
        sidecar_uid = os.getuid()
        sidecar_gid = os.getgid()
        dsh_argv = [
            CONTAINER_NODE,
            f"{CONTAINER_DSH_ROOT}/apps/cli/lib/bin.js",
            "--profile",
            "headless",
            task_prompt,
        ]
        argv = [
            str(DOCKER),
            "run",
            "--rm",
            "--name",
            container_name,
            "--cidfile",
            str(cidfile),
            "--network",
            "host",
            "--user",
            f"{sidecar_uid}:{sidecar_gid}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--tmpfs",
            "/root:rw,nosuid,nodev,size=64m",
            "--mount",
            _bind_mount(workspace, CONTAINER_WORKSPACE),
            "--mount",
            _bind_mount(runtime.root, CONTAINER_DSH_ROOT, readonly=True),
            "--mount",
            _bind_mount(runtime.node.parent, CONTAINER_NODE_ROOT, readonly=True),
            "--mount",
            _bind_mount(state.path, CONTAINER_STATE),
            "--mount",
            _bind_mount(bridge_copy, CONTAINER_BRIDGE, readonly=True),
            "--env",
            f"HOME={CONTAINER_STATE}/home",
            "--env",
            f"DSH_HOME={CONTAINER_STATE}/home",
            "--env",
            "DSH_PERMISSION_MODE=danger-full-access",
            "--env",
            "DSH_TOOLS_MODE=native",
            "--env",
            "DSH_TELEMETRY_DISABLED=1",
            "--env",
            "NARB_DISABLE_NATIVE_CACHE=1",
            "--env",
            f"DEEPSEEK_BASE_URL={proxy_url.rstrip('/')}",
            "--env",
            "DEEPSEEK_API_KEY=toolathlon-run-proxy",
            "--env",
            f"TOOLATHLON_DSH_NODE={CONTAINER_NODE}",
            "--env",
            f"TOOLATHLON_DSH_BRIDGE={CONTAINER_BRIDGE}",
            "--env",
            f"TOOLATHLON_DSH_GATEWAY_URL={gateway_url}",
            "--env",
            f"TOOLATHLON_DSH_SESSION_ROOT={CONTAINER_STATE}/home/sessions",
            "--env",
            f"TOOLATHLON_DSH_WORKSPACE={CONTAINER_WORKSPACE}",
            "--env",
            f"TOOLATHLON_DSH_SYSTEM_PROMPT={system_prompt}",
            "--workdir",
            CONTAINER_WORKSPACE,
            TASK_IMAGE,
            "/bin/sh",
            "-eu",
            "-c",
            _SANDBOX_PREFLIGHT,
            "dsh-sandbox",
            *dsh_argv,
        ]
        observer_stop = threading.Event()
        observed_container: list[tuple[str, int] | None] = [None]
        observer: threading.Thread | None = None
        if on_product_pid is not None:
            observer = threading.Thread(
                target=_observe_container_pid,
                kwargs={
                    "container_name": container_name,
                    "environment": env,
                    "stop": observer_stop,
                    "observed": observed_container,
                    "callback": on_product_pid,
                },
                name="dsh-sidecar-pid-observer",
                daemon=True,
            )
            observer.start()
        cleanup_status = "not_attempted"
        try:
            result = run_monitored_process(
                argv,
                cwd=workspace,
                env=env,
                stdin_payload=b"",
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                deadline_seconds=deadline_seconds,
                budget_exceeded=budget_exceeded,
                on_agent_start=on_agent_start,
            )
        finally:
            observer_stop.set()
            if observer is not None:
                observer.join(timeout=5)
            cleanup_status = _remove_dsh_container(container_name, env)

        container_id = (
            observed_container[0][0]
            if observed_container[0] is not None
            else _read_container_id(cidfile)
        )
        product_pid = observed_container[0][1] if observed_container[0] is not None else None
        session_files = _session_files(home)
        raw_events: list[dict[str, Any]] = []
        session_path: Path | None = None
        if len(session_files) == 1:
            session_path = session_files[0]
            raw_events = list(read_json_rows(session_path))
            shutil.copyfile(session_path, output_dir / "dsh-session.jsonl")
            os.chmod(output_dir / "dsh-session.jsonl", 0o644)
        elif len(session_files) > 1:
            raise ContractError(f"DSH expected one session log, found {len(session_files)}")

        native_events = _normalize_events(raw_events)
        turn_reason = _last_turn_reason(raw_events)
        completed_turn = turn_reason is not None and turn_reason.get("kind") == "completed"
        agent_error = _last_assistant_error(raw_events)
        stdout_output = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        session_output = _last_assistant_text(raw_events)
        error: str | None = None
        if result.termination_reason == "agent_deadline":
            terminal_status = "timeout"
        elif result.termination_reason == "max_model_requests":
            terminal_status = "max_steps"
        elif agent_error is not None:
            terminal_status = "crashed"
            error = agent_error
        elif result.return_code == 0 and completed_turn and session_path is not None:
            terminal_status = "completed"
        else:
            terminal_status = "crashed"
            error = (
                stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                or (
                    f"DSH exited with code {result.return_code}"
                    if result.return_code != 0
                    else "DSH exited without a completed turn"
                )
            )
        return AdapterOutcome(
            terminal_status=terminal_status,
            product_exit_code=result.return_code,
            termination_reason=result.termination_reason,
            output=session_output or stdout_output,
            error=error,
            duration_seconds=result.duration_seconds,
            product_pid=product_pid,
            escalated_to_sigkill=result.escalated_to_sigkill,
            native_events=native_events,
            metadata={
                "ephemeral_state_on_tmpfs": state.on_tmpfs,
                "setup_provider_requests_before_agent": setup_requests,
                "session": {
                    "fresh_home": True,
                    "session_count": len(session_files),
                    "completed_turn": completed_turn,
                    "turn_reason": turn_reason,
                    "raw_event_count": len(raw_events),
                },
                "output": {
                    "session_assistant_text_present": bool(session_output),
                    "stdout_present": bool(stdout_output),
                    "stdout_session_match": not stdout_output
                    or not session_output
                    or stdout_output == session_output,
                },
                "runtime": {
                    "product": "dsh",
                    "version": DSH_VERSION,
                    "node_version": DSH_NODE_VERSION,
                    "node_sha256": sha256_file(runtime.node),
                    "cli_sha256": sha256_file(runtime.root / "apps/cli/lib/bin.js"),
                    "headless_sha256": sha256_file(
                        runtime.root / "packages/bundle/headless/lib/index.js"
                    ),
                    "bridge_sha256": sha256_file(runtime.bridge),
                },
                "command": {
                    "mode": "headless",
                    "resume": False,
                    "permission_mode": "danger-full-access_inside_read_only_sidecar",
                    "toolathlon_system_prompt_mode": "replace_dsh_deployment_persona",
                    "toolathlon_system_prompt_sha256": canonical_json_sha256(system_prompt),
                    "workspace_namespace": {
                        "mode": "docker_sidecar_allowlist",
                        "image": TASK_IMAGE,
                        "container_id": container_id,
                        "root_filesystem_read_only": True,
                        "process_user": {
                            "uid": sidecar_uid,
                            "gid": sidecar_gid,
                        },
                        "linux_capabilities": [],
                        "no_new_privileges": True,
                        "docker_socket_exposed": False,
                        "host_home_exposed": False,
                        "host_tmp_exposed": False,
                        "workspace_host_source": str(workspace),
                        "workspace_mount_mode": "read_write",
                        "runtime_mount_mode": "read_only",
                        "product_cwd": CONTAINER_WORKSPACE,
                        "network_mode": "host_for_loopback_gateway_and_proxy",
                        "cleanup_status": cleanup_status,
                        "product_pid_source": (
                            "docker_inspect_state_pid" if product_pid is not None else "not_observed"
                        ),
                    },
                    "task_tool_exposure": {
                        "mcp_tool_count": len(task_mcp_tool_names),
                        "mcp_tool_names_sha256": canonical_json_sha256(task_mcp_tool_names),
                        "dsh_builtin_tools_retained": True,
                        "mcp_transport": "classic_sse_to_stdio_bridge",
                    },
                },
            },
        )
