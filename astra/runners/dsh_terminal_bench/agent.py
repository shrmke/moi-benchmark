from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from astra.runners.lifecycle_c0 import (
    C0Controller,
    C0ControllerConfig,
    ExternalTriggerManifest,
    JsonlLedger,
    LifecycleConfigurationError,
    collect_process_cleanup_report,
    get_terminal_bench_trigger_for_instruction,
    lifecycle_predicate_probe_source_path,
    lifecycle_predicate_probe_source_sha256,
    process_probe_run_command,
    process_probe_source_path,
)

from .install_runtime import DSH_RUNTIME_VERSION, RUNTIME_ARTIFACTS


class DshSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DshEvaluationProfile:
    id: str
    harbor_model_name: str
    provider: str
    model: str
    credential_env: str
    base_url_env: Optional[str]
    config_filename: str
    support_filenames: tuple[str, ...]
    context_window: int
    max_tokens: Optional[int]
    max_turns: Optional[int]
    temperature: Optional[float]
    reasoning_effort: Optional[str]
    composition: str
    evaluation_status: str
    comparison_scope: str


DEEPSEEK_NATIVE_PROFILE_ID = "deepseek-native"
TERMINAL_BENCH_GLM52_PROFILE_ID = "terminalbench-glm52"
TERMINAL_BENCH_DEEPSEEK_V4_FLASH_MAX_PROFILE_ID = (
    "terminalbench-deepseek-v4-flash-max"
)
DEFAULT_PROFILE_ID = DEEPSEEK_NATIVE_PROFILE_ID

DSH_PROFILES = {
    DEEPSEEK_NATIVE_PROFILE_ID: DshEvaluationProfile(
        id=DEEPSEEK_NATIVE_PROFILE_ID,
        harbor_model_name="deepseek-official/deepseek-v4-flash",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        credential_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        config_filename="minimal.cordis.yml",
        support_filenames=(),
        context_window=1_000_000,
        max_tokens=49_152,
        max_turns=None,
        temperature=None,
        reasoning_effort=None,
        composition="official_jsonrpc_minimal",
        evaluation_status="exploratory_native_model",
        comparison_scope="end_to_end_native_model_not_harness_only",
    ),
    TERMINAL_BENCH_GLM52_PROFILE_ID: DshEvaluationProfile(
        id=TERMINAL_BENCH_GLM52_PROFILE_ID,
        harbor_model_name="zai/glm-5.2",
        provider="zai",
        model="glm-5.2",
        credential_env="ZAI_API_KEY",
        base_url_env=None,
        config_filename="minimal-pi-ai-glm52.cordis.yml",
        support_filenames=("terminalbench-profile.mjs",),
        context_window=1_000_000,
        max_tokens=None,
        max_turns=50,
        temperature=0.0,
        reasoning_effort="high",
        composition="jsonrpc_minimal_pi_ai_terminalbench",
        evaluation_status="exploratory_same_model_profile",
        comparison_scope="end_to_end_same_model_candidate",
    ),
    TERMINAL_BENCH_DEEPSEEK_V4_FLASH_MAX_PROFILE_ID: DshEvaluationProfile(
        id=TERMINAL_BENCH_DEEPSEEK_V4_FLASH_MAX_PROFILE_ID,
        harbor_model_name="deepseek-official/deepseek-v4-flash",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        credential_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        config_filename="minimal-deepseek-v4-flash-max.cordis.yml",
        support_filenames=("terminalbench-profile.mjs",),
        context_window=1_000_000,
        max_tokens=49_152,
        max_turns=50,
        temperature=0.0,
        reasoning_effort="max",
        composition="official_jsonrpc_minimal_terminalbench_max",
        evaluation_status="exploratory_native_model_max",
        comparison_scope="end_to_end_native_model_max_not_harness_only",
    ),
}

LOCAL_SOURCE_REFERENCE_VERSION = "0.1.0-rc.5"
LOCAL_SOURCE_REFERENCE_COMMIT = "47f943859bef60e4160492346772ded9b24f765a"

REMOTE_ROOT = "/installed-agent/dsh"
REMOTE_RUNTIME_DIR = f"{REMOTE_ROOT}/runtime"
REMOTE_RUNTIME = f"{REMOTE_RUNTIME_DIR}/dsh-jsonrpc-agent"
REMOTE_INSTALL_MARKER = f"{REMOTE_RUNTIME_DIR}/install.json"
REMOTE_RUNTIME_WHEEL = f"{REMOTE_ROOT}/runtime.whl"
REMOTE_INSTALLER = f"{REMOTE_ROOT}/install-runtime.py"
REMOTE_DRIVER = f"{REMOTE_ROOT}/driver.py"
REMOTE_CONFIG = f"{REMOTE_ROOT}/profile.cordis.yml"
REMOTE_PROCESS_PROBE = f"{REMOTE_ROOT}/lifecycle-process-probe.py"
REMOTE_PREDICATE_PROBE = f"{REMOTE_ROOT}/lifecycle-predicate-probe.py"
REMOTE_RESULT = "/logs/agent/dsh-run.json"
REMOTE_EVENTS = "/logs/agent/dsh-events.jsonl"
REMOTE_RUNTIME_STDERR = "/logs/agent/dsh-runtime.stderr.txt"
REMOTE_SESSION_ROOT = "/logs/agent/dsh-sessions"

GENERIC_C0_PREDICATE_ID = "terminal-bench.generic.product-live"
C0_PRODUCT_TIMEOUT_MULTIPLIER = 2.0
C0_MAX_PRODUCT_TIMEOUT_SEC = 24000
C0_HOST_CLEANUP_MARGIN_SEC = 40
C0_CLEANUP_GRACE_SEC = 2.0

_ENSURE_PYTHON3_COMMAND = (
    "if command -v apt-get >/dev/null 2>&1; then "
    "DEBIAN_FRONTEND=noninteractive apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ca-certificates; "
    "elif command -v apk >/dev/null 2>&1; then "
    "apk add --no-cache python3 ca-certificates; "
    "elif command -v dnf >/dev/null 2>&1; then "
    "dnf install -y python3 ca-certificates; "
    "elif command -v yum >/dev/null 2>&1; then "
    "yum install -y python3 ca-certificates; "
    "else echo 'no supported package manager for python3' >&2; exit 127; fi"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_trial_task(logs_dir: Path, instruction_sha256: str) -> tuple[str, float]:
    trial_config_path = logs_dir.parent / "config.json"
    try:
        trial_config = json.loads(trial_config_path.read_text(encoding="utf-8"))
        task_path = Path(trial_config["task"]["path"]).resolve()
        instruction_path = task_path / "instruction.md"
        task_config_path = task_path / "task.toml"
        local_instruction_sha256 = hashlib.sha256(
            instruction_path.read_text(encoding="utf-8").strip().encode()
        ).hexdigest()
        with task_config_path.open("rb") as stream:
            task_config = tomllib.load(stream)
        task_id = task_config["task"]["name"].rsplit("/", 1)[-1]
        base_timeout_sec = (
            trial_config.get("agent", {}).get("override_timeout_sec")
            or task_config["agent"]["timeout_sec"]
        )
        max_timeout_sec = trial_config.get("agent", {}).get("max_timeout_sec")
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        raise LifecycleConfigurationError(
            "could not resolve Terminal-Bench trial metadata from "
            f"{trial_config_path}"
        ) from exc
    if task_id != task_path.name or local_instruction_sha256 != instruction_sha256:
        raise LifecycleConfigurationError(
            "the Harbor trial task does not match the supplied instruction"
        )
    try:
        effective_timeout_sec = float(base_timeout_sec)
        if max_timeout_sec is not None:
            effective_timeout_sec = min(
                effective_timeout_sec, float(max_timeout_sec)
            )
    except (TypeError, ValueError) as exc:
        raise LifecycleConfigurationError(
            "the Terminal-Bench agent timeout is not numeric"
        ) from exc
    if effective_timeout_sec <= 0:
        raise LifecycleConfigurationError(
            "the Terminal-Bench agent timeout must be positive"
        )
    return task_id, effective_timeout_sec


class DshTerminalBenchS0Agent(BaseInstalledAgent):
    """Run a frozen DeepSeek Harness JSON-RPC evaluation profile."""

    SUPPORTS_RESUME = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: Optional[str] = None,
        profile: str = DEFAULT_PROFILE_ID,
        turn_timeout_sec: int = C0_MAX_PRODUCT_TIMEOUT_SEC,
        runtime_wheel_path: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            self.profile = DSH_PROFILES[profile]
        except KeyError as exc:
            raise ValueError(f"unknown DSH evaluation profile {profile!r}") from exc
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            *args,
            **kwargs,
        )
        if self.version() != DSH_RUNTIME_VERSION:
            raise ValueError(
                f"DSH requires version {DSH_RUNTIME_VERSION}, got {self.version()!r}"
            )
        if self.model_name != self.profile.harbor_model_name:
            raise ValueError(
                f"DSH profile {self.profile.id!r} requires model "
                f"{self.profile.harbor_model_name}, got {self.model_name!r}"
            )
        self.turn_timeout_sec = int(turn_timeout_sec)
        self.max_tokens = self.profile.max_tokens
        self.context_window = self.profile.context_window
        self.max_turns = self.profile.max_turns
        self.temperature = self.profile.temperature
        self.reasoning_effort = self.profile.reasoning_effort
        if not 0 < self.turn_timeout_sec <= C0_MAX_PRODUCT_TIMEOUT_SEC:
            raise ValueError(
                f"turn_timeout_sec must be in (0, {C0_MAX_PRODUCT_TIMEOUT_SEC}]"
            )

        self._provider_key = self._get_env(self.profile.credential_env)
        self._provider_base_url = (
            self._get_env(self.profile.base_url_env)
            if self.profile.base_url_env is not None
            else None
        )
        self._extra_env.pop(self.profile.credential_env, None)
        if self.profile.base_url_env is not None:
            self._extra_env.pop(self.profile.base_url_env, None)
        if not self._provider_key:
            raise ValueError(
                f"DSH profile {self.profile.id!r} requires "
                f"{self.profile.credential_env}"
            )
        configured_runtime_wheel = runtime_wheel_path or self._get_env(
            "DSH_RUNTIME_WHEEL"
        )
        self._extra_env.pop("DSH_RUNTIME_WHEEL", None)
        self.runtime_wheel_path = (
            Path(configured_runtime_wheel).resolve()
            if configured_runtime_wheel
            else None
        )
        if self.runtime_wheel_path is not None and not self.runtime_wheel_path.is_file():
            raise ValueError(
                f"DSH runtime wheel was not found: {self.runtime_wheel_path}"
            )
        self._install_marker: Optional[dict[str, Any]] = None
        self._run_metadata: dict[str, Any] = {}

    @staticmethod
    def name() -> str:
        return "dsh-terminal-bench-s0"

    def _config_path(self) -> Path:
        return Path(__file__).with_name(self.profile.config_filename)

    def _support_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(__file__).with_name(filename)
            for filename in self.profile.support_filenames
        )

    def _arm_harbor_secret_scrub(self) -> None:
        if self._provider_key:
            self._extra_env[self.profile.credential_env] = self._provider_key
        if self._provider_base_url and self.profile.base_url_env is not None:
            self._extra_env[self.profile.base_url_env] = self._provider_base_url

    def _product_env(self, *, setup_smoke: bool = False) -> dict[str, str]:
        env = {
            self.profile.credential_env: (
                "setup-smoke-no-model-call" if setup_smoke else str(self._provider_key)
            ),
            "DSH_MODEL": self.profile.model,
            "DSH_CONTEXT_WINDOW": str(self.context_window),
        }
        if self.max_turns is not None:
            env["DSH_MAX_TURNS"] = str(self.max_turns)
        if self.temperature is not None:
            env["DSH_TEMPERATURE"] = str(self.temperature)
        if self.max_tokens is not None:
            env["DSH_MAX_TOKENS"] = str(self.max_tokens)
        if self.reasoning_effort is not None:
            env["DSH_REASONING_EFFORT"] = self.reasoning_effort
        if (
            self._provider_base_url
            and self.profile.base_url_env is not None
            and not setup_smoke
        ):
            env[self.profile.base_url_env] = self._provider_base_url
        return env

    @staticmethod
    async def _resolve_product_cwd(environment: BaseEnvironment) -> str:
        result = await environment.exec(
            command=(
                "if test -d /app; then printf /app; "
                "elif test -d /workspace; then printf /workspace; "
                "else pwd; fi"
            ),
            timeout_sec=10,
        )
        cwd = (result.stdout or "").strip() or "/app"
        if result.return_code != 0 or not cwd.startswith("/"):
            raise RuntimeError("could not resolve the DSH product workspace")
        return cwd

    async def install(self, environment: BaseEnvironment) -> None:
        python_result = await environment.exec(
            command=(
                "python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'"
            ),
            timeout_sec=10,
        )
        if python_result.return_code != 0:
            await self.exec_as_root(
                environment,
                command=_ENSURE_PYTHON3_COMMAND,
                timeout_sec=300,
            )
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {shlex.quote(REMOTE_ROOT)} && "
                f"chmod 0755 {shlex.quote(REMOTE_ROOT)}"
            ),
            timeout_sec=10,
        )
        await environment.upload_file(
            Path(__file__).with_name("install_runtime.py"), REMOTE_INSTALLER
        )
        await environment.upload_file(
            Path(__file__).with_name("driver.py"), REMOTE_DRIVER
        )
        if self.runtime_wheel_path is not None:
            await environment.upload_file(
                self.runtime_wheel_path, REMOTE_RUNTIME_WHEEL
            )
        await environment.upload_file(self._config_path(), REMOTE_CONFIG)
        support_remote_paths: list[str] = []
        for support_path in self._support_paths():
            remote_path = f"{REMOTE_ROOT}/{support_path.name}"
            await environment.upload_file(support_path, remote_path)
            support_remote_paths.append(remote_path)
        readonly_paths = [REMOTE_CONFIG, *support_remote_paths]
        installer_wheel_arg = ""
        if self.runtime_wheel_path is not None:
            readonly_paths.append(REMOTE_RUNTIME_WHEEL)
            installer_wheel_arg = f" --wheel {shlex.quote(REMOTE_RUNTIME_WHEEL)}"
        readonly_args = " ".join(shlex.quote(path) for path in readonly_paths)
        await self.exec_as_root(
            environment,
            command=(
                f"chmod 0555 {shlex.quote(REMOTE_INSTALLER)} "
                f"{shlex.quote(REMOTE_DRIVER)} && "
                f"chmod 0444 {readonly_args} && "
                f"python3 {shlex.quote(REMOTE_INSTALLER)} "
                f"--destination {shlex.quote(REMOTE_RUNTIME_DIR)}"
                f"{installer_wheel_arg}"
            ),
            timeout_sec=300,
        )

        marker_result = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_INSTALL_MARKER)}",
            timeout_sec=10,
        )
        try:
            marker = json.loads(marker_result.stdout or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError("DSH install marker is missing or invalid") from exc
        artifact = RUNTIME_ARTIFACTS.get(marker.get("architecture"))
        if (
            marker_result.return_code != 0
            or marker.get("schema_version") != 1
            or marker.get("product") != "deepseek-harness"
            or marker.get("version") != DSH_RUNTIME_VERSION
            or marker.get("runtime_path") != REMOTE_RUNTIME
            or artifact is None
            or marker.get("wheel_sha256") != artifact.sha256
        ):
            raise RuntimeError("DSH install marker does not match the frozen runtime")
        self._install_marker = marker

        smoke_argv = self._driver_argv(
            instruction_file=None,
            result_file="/tmp/dsh-install-smoke.json",
            events_file="/tmp/dsh-install-smoke-events.jsonl",
            runtime_stderr_file="/tmp/dsh-install-smoke.stderr.txt",
            session_root="/tmp/dsh-install-smoke-sessions",
            session_id="install-smoke",
            cwd="/tmp",
            timeout_sec=60,
            initialize_only=True,
        )
        smoke = await environment.exec(
            command=shlex.join(smoke_argv),
            env=self._product_env(setup_smoke=True),
            timeout_sec=70,
        )
        if smoke.return_code != 0:
            raise self._classify_exec_error(shlex.join(smoke_argv), smoke)

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / f"dsh-{self.profile.id}.cordis.yml").write_bytes(
            self._config_path().read_bytes()
        )
        for support_path in self._support_paths():
            (self.logs_dir / support_path.name).write_bytes(
                support_path.read_bytes()
            )
        (self.logs_dir / "dsh-install.json").write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _driver_argv(
        self,
        *,
        instruction_file: Optional[str],
        result_file: str,
        events_file: str,
        runtime_stderr_file: str,
        session_root: str,
        session_id: str,
        cwd: str,
        timeout_sec: float,
        initialize_only: bool = False,
    ) -> list[str]:
        argv = [
            "python3",
            REMOTE_DRIVER,
            "--runtime",
            REMOTE_RUNTIME,
            "--config",
            REMOTE_CONFIG,
            "--result-file",
            result_file,
            "--events-file",
            events_file,
            "--runtime-stderr-file",
            runtime_stderr_file,
            "--session-root",
            session_root,
            "--session-id",
            session_id,
            "--cwd",
            cwd,
            "--provider",
            self.profile.provider,
            "--model",
            self.profile.model,
            "--timeout-sec",
            str(timeout_sec),
        ]
        if self.max_tokens is not None:
            argv.extend(["--max-tokens", str(self.max_tokens)])
        if self.max_turns is not None:
            argv.extend(["--max-turns", str(self.max_turns)])
        if instruction_file is not None:
            argv.extend(["--instruction-file", instruction_file])
        if initialize_only:
            argv.append("--initialize-only")
        return argv

    async def _collect_dsh_artifacts(
        self, environment: BaseEnvironment
    ) -> Optional[dict[str, Any]]:
        artifacts = (
            (REMOTE_RESULT, self.logs_dir / "dsh-run.json"),
            (REMOTE_EVENTS, self.logs_dir / "dsh-events.jsonl"),
            (REMOTE_RUNTIME_STDERR, self.logs_dir / "dsh-runtime.stderr.txt"),
        )
        for remote_path, local_path in artifacts:
            try:
                await environment.download_file(remote_path, local_path)
            except Exception:
                pass
        result_path = self.logs_dir / "dsh-run.json"
        if not result_path.is_file():
            return None
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("DSH driver result is invalid JSON") from exc
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise RuntimeError("DSH driver result has an invalid schema")
        return result

    def _base_metadata(self, condition: str, task_workdir: str) -> dict[str, Any]:
        marker = self._install_marker or {}
        return {
            "condition": condition,
            "fault_injected": False,
            "formal_score_eligible": False,
            "evaluation_status": self.profile.evaluation_status,
            "product": "dsh",
            "product_name": "deepseek-harness",
            "dsh_profile": self.profile.id,
            "dsh_runtime_version": DSH_RUNTIME_VERSION,
            "dsh_runtime_architecture": marker.get("architecture"),
            "dsh_runtime_wheel_sha256": marker.get("wheel_sha256"),
            "dsh_model_provider": self.profile.provider,
            "dsh_model": self.profile.model,
            "dsh_max_tokens": self.max_tokens,
            "dsh_context_window": self.context_window,
            "dsh_max_turns": self.max_turns,
            "dsh_max_turns_unit": (
                "model_request_steps" if self.max_turns is not None else None
            ),
            "dsh_temperature": self.temperature,
            "dsh_reasoning_effort": self.reasoning_effort,
            "dsh_composition": self.profile.composition,
            "dsh_config_sha256": _sha256(self._config_path()),
            "dsh_source_reference_version": LOCAL_SOURCE_REFERENCE_VERSION,
            "dsh_source_reference_commit": LOCAL_SOURCE_REFERENCE_COMMIT,
            "dsh_source_distribution_match": False,
            "comparison_scope": self.profile.comparison_scope,
            "task_workdir": task_workdir,
            "trajectory_capture_required": True,
            "trajectory_capture_mode": "dsh_jsonrpc_event_stream",
            "trajectory_capture_blocking": False,
            "trajectory_capture_path": "agent/dsh-events.jsonl",
        }

    def _apply_result(
        self,
        context: AgentContext,
        result: Optional[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        events_path = self.logs_dir / "dsh-events.jsonl"
        if result is None:
            metadata.update(
                {
                    "dsh_trajectory_status": "missing",
                    "dsh_trajectory_sha256": None,
                    "dsh_session_id": None,
                    "dsh_finish_reason": None,
                    "dsh_event_count": 0,
                    "dsh_turn_count": 0,
                    "dsh_step_count": 0,
                    "dsh_tool_call_count": 0,
                    "dsh_usage": None,
                }
            )
        else:
            usage = result.get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise RuntimeError("DSH result usage must be an object or null")
            metadata.update(
                {
                    "dsh_trajectory_status": (
                        "saved" if events_path.is_file() else "missing"
                    ),
                    "dsh_trajectory_sha256": (
                        _sha256(events_path) if events_path.is_file() else None
                    ),
                    "dsh_session_id": result.get("session_id"),
                    "dsh_finish_reason": result.get("finish_reason"),
                    "dsh_event_count": result.get("event_count"),
                    "dsh_turn_count": result.get("turn_count"),
                    "dsh_step_count": result.get("step_count"),
                    "dsh_tool_call_count": result.get("tool_call_count"),
                    "dsh_usage": usage,
                }
            )
            if usage is not None:
                fresh_input = usage.get("fresh_input_tokens")
                cache_read = usage.get("cache_read_tokens")
                output = usage.get("output_tokens")
                if all(type(value) is int and value >= 0 for value in (
                    fresh_input,
                    cache_read,
                    output,
                )):
                    context.n_input_tokens = fresh_input + cache_read
                    context.n_cache_tokens = cache_read
                    context.n_output_tokens = output
        context.metadata = dict(metadata)
        self._run_metadata = dict(metadata)
        return metadata

    @staticmethod
    def _require_successful_session(
        result: Optional[dict[str, Any]],
    ) -> None:
        if result is not None and result.get("status") != "completed":
            raise DshSessionError(
                "DSH session finished with "
                f"dsh_finish_reason={result.get('finish_reason')!r}"
            )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._arm_harbor_secret_scrub()
        task_workdir = await self._resolve_product_cwd(environment)
        metadata = self._base_metadata("S0", task_workdir)
        context.metadata = dict(metadata)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        instruction_path = self.logs_dir / "instruction.md"
        instruction_path.write_text(instruction, encoding="utf-8")
        remote_instruction = f"{REMOTE_ROOT}/instruction.md"
        await environment.upload_file(instruction_path, remote_instruction)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(REMOTE_SESSION_ROOT)} && "
                f"chmod 0700 {shlex.quote(REMOTE_SESSION_ROOT)}"
            ),
            timeout_sec=10,
        )
        argv = self._driver_argv(
            instruction_file=remote_instruction,
            result_file=REMOTE_RESULT,
            events_file=REMOTE_EVENTS,
            runtime_stderr_file=REMOTE_RUNTIME_STDERR,
            session_root=REMOTE_SESSION_ROOT,
            session_id=str(uuid.uuid4()),
            cwd=task_workdir,
            timeout_sec=self.turn_timeout_sec,
        )
        command = shlex.join(argv)
        result = await environment.exec(
            command=command,
            cwd=task_workdir,
            env=self._product_env(),
            timeout_sec=self.turn_timeout_sec + C0_HOST_CLEANUP_MARGIN_SEC,
        )
        driver_result = await self._collect_dsh_artifacts(environment)
        self._apply_result(context, driver_result, metadata)
        if result.return_code != 0:
            raise self._classify_exec_error(command, result)
        self._require_successful_session(driver_result)

    def populate_context_post_run(self, context: AgentContext) -> None:
        metadata = dict(context.metadata or {})
        metadata.update(self._run_metadata)
        context.metadata = metadata


class DshTerminalBenchC0Agent(DshTerminalBenchS0Agent):
    """Run DSH behind the framework's product-neutral C0 no-op seam."""

    def __init__(
        self,
        *args: Any,
        trigger_timeout_sec: float = C0_MAX_PRODUCT_TIMEOUT_SEC,
        poll_interval_sec: float = 0.5,
        product_timeout_multiplier: float = C0_PRODUCT_TIMEOUT_MULTIPLIER,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.trigger_timeout_sec = float(trigger_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.product_timeout_multiplier = float(product_timeout_multiplier)
        if self.trigger_timeout_sec <= 0 or self.poll_interval_sec <= 0:
            raise ValueError("C0 controller timeouts must be positive")
        if self.product_timeout_multiplier <= 0:
            raise ValueError("product_timeout_multiplier must be positive")

    @staticmethod
    def name() -> str:
        return "dsh-terminal-bench-c0"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await environment.upload_file(
            process_probe_source_path(), REMOTE_PROCESS_PROBE
        )
        await environment.upload_file(
            lifecycle_predicate_probe_source_path(), REMOTE_PREDICATE_PROBE
        )
        await self.exec_as_root(
            environment,
            command=(
                f"chmod 0555 {shlex.quote(REMOTE_PROCESS_PROBE)} "
                f"{shlex.quote(REMOTE_PREDICATE_PROBE)}"
            ),
            timeout_sec=10,
        )

    async def _run_with_controller(
        self,
        environment: BaseEnvironment,
        *,
        product_command: str,
        product_cwd: str,
        timeout_sec: float,
        controller: C0Controller,
        product_done: asyncio.Event,
        paths: dict[str, str],
        ledger: JsonlLedger,
    ) -> tuple[Any, Optional[Exception], Any, Optional[dict[str, Any]], Optional[str]]:
        product_result = None
        product_error: Optional[Exception] = None
        cleanup_report: Optional[dict[str, Any]] = None
        cleanup_sha256: Optional[str] = None

        async def execute_product() -> Any:
            nonlocal product_result, product_error, cleanup_report, cleanup_sha256
            request_cleanup = False
            try:
                product_result = await environment.exec(
                    command=product_command,
                    cwd=product_cwd,
                    env=self._product_env(),
                    timeout_sec=timeout_sec,
                )
                return product_result
            except asyncio.CancelledError:
                request_cleanup = True
                raise
            except Exception as exc:
                product_error = exc
                request_cleanup = True
                return None
            finally:
                try:
                    cleanup_report, cleanup_sha256 = (
                        await collect_process_cleanup_report(
                            environment,
                            probe_path=REMOTE_PROCESS_PROBE,
                            identity_path=paths["identity"],
                            cleanup_report_path=paths["cleanup"],
                            request_cleanup=request_cleanup,
                        )
                    )
                    terminal_status = cleanup_report["product_terminal_status"]
                    setattr(
                        product_done,
                        "_c0_product_terminal_status",
                        terminal_status,
                    )
                    ledger.append(
                        "product_process_cleanup",
                        reason=cleanup_report["reason"],
                        product_terminal_status=terminal_status,
                        zero_live_proven=True,
                        remaining_pids_count=0,
                        cleanup_report_sha256=cleanup_sha256,
                        fault_action=False,
                    )
                finally:
                    product_done.set()

        async with asyncio.TaskGroup() as group:
            product_task = group.create_task(execute_product())
            controller_task = group.create_task(
                controller.run(environment, product_done)
            )
        return (
            product_task.result(),
            product_error,
            controller_task.result(),
            cleanup_report,
            cleanup_sha256,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._arm_harbor_secret_scrub()
        run_id = str(uuid.uuid4())
        instruction_sha256 = hashlib.sha256(
            instruction.strip().encode()
        ).hexdigest()
        try:
            trigger = get_terminal_bench_trigger_for_instruction(instruction)
        except LifecycleConfigurationError:
            task_id, base_timeout_sec = _load_trial_task(
                self.logs_dir, instruction_sha256
            )
            trigger = ExternalTriggerManifest(
                task_id=task_id,
                predicate_id=GENERIC_C0_PREDICATE_ID,
            )
            trigger_registration_status = "generic"
            trigger_scope = "generic_product_live"
        else:
            task_id = trigger.task_id
            trigger_registration_status = "task_specific"
            trigger_scope = "task_specific_progress"
            resolved_task_id, base_timeout_sec = _load_trial_task(
                self.logs_dir, instruction_sha256
            )
            if resolved_task_id != task_id:
                raise LifecycleConfigurationError(
                    "registered C0 trigger does not match the Harbor task"
                )

        product_timeout_sec = min(
            self.turn_timeout_sec,
            base_timeout_sec * self.product_timeout_multiplier,
        )
        outer_timeout_sec = product_timeout_sec + C0_HOST_CLEANUP_MARGIN_SEC
        task_workdir = await self._resolve_product_cwd(environment)
        remote_run_root = f"/tmp/dsh-c0/run-{run_id}"
        paths = {
            "identity": f"{remote_run_root}/product.identity.json",
            "cleanup": f"{remote_run_root}/product.cleanup.json",
            "stdout": f"{remote_run_root}/product.stdout",
            "stderr": f"{remote_run_root}/product.stderr",
            "stdin": f"{remote_run_root}/instruction.md",
        }
        ledger_path = self.logs_dir / "controller.jsonl"
        ledger = JsonlLedger(ledger_path, run_id)
        metadata = {
            **self._base_metadata("C0", task_workdir),
            "fault_action": "noop",
            "task_id": task_id,
            "instruction_sha256": instruction_sha256,
            "trigger_registration_status": trigger_registration_status,
            "trigger_scope": trigger_scope,
            "trigger_id": trigger.predicate_id,
            "trigger_manifest_sha256": trigger.sha256,
            "predicate_probe_sha256": lifecycle_predicate_probe_source_sha256(),
            "controller_ledger": str(ledger_path),
            "configured_product_timeout_sec": (
                base_timeout_sec * self.product_timeout_multiplier
            ),
            "product_timeout_multiplier": self.product_timeout_multiplier,
            "product_timeout_sec": product_timeout_sec,
            "outer_cleanup_timeout_sec": outer_timeout_sec,
        }
        context.metadata = dict(metadata)
        ledger.append(
            "controller_started",
            product_version=DSH_RUNTIME_VERSION,
            model_name=self.model_name,
            **metadata,
        )

        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_run_root)} "
                f"{shlex.quote(REMOTE_SESSION_ROOT)} && "
                f"chmod 0700 {shlex.quote(remote_run_root)} "
                f"{shlex.quote(REMOTE_SESSION_ROOT)}"
            ),
            timeout_sec=10,
        )
        instruction_path = self.logs_dir / "instruction.md"
        instruction_path.parent.mkdir(parents=True, exist_ok=True)
        instruction_path.write_text(instruction, encoding="utf-8")
        await environment.upload_file(instruction_path, paths["stdin"])

        preflight_argv = self._driver_argv(
            instruction_file=None,
            result_file=f"{remote_run_root}/preflight.json",
            events_file=f"{remote_run_root}/preflight-events.jsonl",
            runtime_stderr_file=f"{remote_run_root}/preflight.stderr.txt",
            session_root=f"{remote_run_root}/preflight-sessions",
            session_id="c0-preflight",
            cwd=task_workdir,
            timeout_sec=60,
            initialize_only=True,
        )
        preflight = await environment.exec(
            command=shlex.join(preflight_argv),
            cwd=task_workdir,
            env=self._product_env(setup_smoke=True),
            timeout_sec=70,
        )
        ledger.append(
            "product_preflight",
            check="runtime_initialize",
            passed=preflight.return_code == 0,
            return_code=preflight.return_code,
        )
        if preflight.return_code != 0:
            raise self._classify_exec_error(shlex.join(preflight_argv), preflight)

        session_id = str(uuid.uuid4())
        driver_argv = self._driver_argv(
            instruction_file=paths["stdin"],
            result_file=REMOTE_RESULT,
            events_file=REMOTE_EVENTS,
            runtime_stderr_file=REMOTE_RUNTIME_STDERR,
            session_root=REMOTE_SESSION_ROOT,
            session_id=session_id,
            cwd=task_workdir,
            timeout_sec=product_timeout_sec,
        )
        product_command = process_probe_run_command(
            probe_path=REMOTE_PROCESS_PROBE,
            identity_path=paths["identity"],
            stdout_path=paths["stdout"],
            stderr_path=paths["stderr"],
            stdin_path=paths["stdin"],
            cwd=task_workdir,
            child_argv=driver_argv,
            deadline_sec=product_timeout_sec,
            cleanup_report_path=paths["cleanup"],
            cleanup_grace_sec=C0_CLEANUP_GRACE_SEC,
            strict_cleanup=True,
        )
        product_done = asyncio.Event()
        controller = C0Controller(
            C0ControllerConfig(
                identity_path=paths["identity"],
                predicate_probe_path=REMOTE_PREDICATE_PROBE,
                trigger=trigger,
                trigger_timeout_sec=min(
                    self.trigger_timeout_sec, product_timeout_sec
                ),
                process_probe_path=REMOTE_PROCESS_PROBE,
                poll_interval_sec=self.poll_interval_sec,
            ),
            ledger.append,
        )
        ledger.append(
            "trigger_armed",
            task_id=task_id,
            predicate_id=trigger.predicate_id,
            trigger_manifest_sha256=trigger.sha256,
        )
        ledger.append("product_turn_started")
        (
            product_result,
            product_error,
            outcome,
            cleanup_report,
            cleanup_sha256,
        ) = await self._run_with_controller(
            environment,
            product_command=product_command,
            product_cwd=task_workdir,
            timeout_sec=outer_timeout_sec,
            controller=controller,
            product_done=product_done,
            paths=paths,
            ledger=ledger,
        )

        try:
            await environment.download_file(
                paths["cleanup"], self.logs_dir / "product.cleanup.json"
            )
        except Exception:
            pass
        driver_result = await self._collect_dsh_artifacts(environment)
        return_code = (
            product_result.return_code if product_result is not None else None
        )
        product_terminal_status = (
            cleanup_report["product_terminal_status"]
            if cleanup_report is not None
            else "adapter_infra_error"
        )
        ledger.append(
            "product_turn_exited",
            return_code=return_code,
            error_type=(type(product_error).__name__ if product_error else None),
            product_terminal_status=product_terminal_status,
        )
        lifecycle_gate_passed = bool(outcome.trigger_hit and not outcome.fault_injected)
        metadata.update(
            {
                "trigger_hit": outcome.trigger_hit,
                "trigger_reason": outcome.reason,
                "trigger_evidence_sha256": outcome.evidence_sha256,
                "lifecycle_gate_passed": lifecycle_gate_passed,
                "product_return_code": return_code,
                "product_error_type": (
                    type(product_error).__name__ if product_error else None
                ),
                "product_terminal_status": product_terminal_status,
                "product_cleanup_zero_live_proven": bool(
                    cleanup_report and cleanup_report["zero_live_proven"]
                ),
                "product_cleanup_report_sha256": cleanup_sha256,
            }
        )
        self._apply_result(context, driver_result, metadata)
        product_completion_claim = bool(
            return_code == 0
            and metadata.get("dsh_trajectory_status") == "saved"
            and driver_result is not None
            and driver_result.get("status") == "completed"
        )
        metadata["product_completion_claim"] = product_completion_claim
        context.metadata = dict(metadata)
        self._run_metadata = dict(metadata)
        ledger.append(
            "controller_completed",
            trigger_hit=outcome.trigger_hit,
            fault_injected=False,
            lifecycle_gate_passed=lifecycle_gate_passed,
            product_completion_claim=product_completion_claim,
            product_return_code=return_code,
            product_terminal_status=product_terminal_status,
            product_cleanup_zero_live_proven=metadata[
                "product_cleanup_zero_live_proven"
            ],
            dsh_trajectory_status=metadata["dsh_trajectory_status"],
            dsh_trajectory_sha256=metadata["dsh_trajectory_sha256"],
            dsh_session_id=metadata["dsh_session_id"],
            dsh_finish_reason=metadata["dsh_finish_reason"],
            dsh_event_count=metadata["dsh_event_count"],
            trajectory_capture_blocking=False,
        )
        if product_error is not None:
            raise product_error
        if product_result is None:
            raise RuntimeError("DSH product process did not return a result")
        if product_result.return_code != 0:
            raise self._classify_exec_error(product_command, product_result)
        self._require_successful_session(driver_result)
