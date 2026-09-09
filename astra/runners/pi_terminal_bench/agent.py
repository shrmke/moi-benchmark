from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tomllib
import uuid
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.pi import Pi
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
from astra.runners.pi_terminal_bench.events import (
    validate_event_stream,
    validate_session,
)


FROZEN_PI_VERSION = "0.73.1"
FROZEN_MODEL_NAME = "zai/glm-5.2"
DEEPSEEK_MODEL_NAME = "deepseek/deepseek-v4-flash"
FROZEN_PROVIDER = "zai"
FROZEN_MODEL = "glm-5.2"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-v4-flash"
FROZEN_TOOLS = "read,bash,edit,write"
REMOTE_ROOT = "/tmp/pi-c0"
REMOTE_PI_HOME = "/tmp/pi-c0-config"
REMOTE_MODELS = f"{REMOTE_PI_HOME}/models.json"
REMOTE_SESSIONS = "/logs/agent/pi/sessions"
REMOTE_PREBUILT_MARKER = "/opt/moi/pi-preinstalled.json"
REMOTE_PROCESS_PROBE = "/installed-agent/lifecycle-process-probe.py"
REMOTE_PREDICATE_PROBE = "/installed-agent/lifecycle-predicate-probe.py"
GENERIC_C0_PREDICATE_ID = "terminal-bench.generic.product-live"
C0_PRODUCT_TIMEOUT_MULTIPLIER = 2.0
C0_MAX_PRODUCT_TIMEOUT_SEC = 24000
C0_HOST_CLEANUP_MARGIN_SEC = 40
C0_CLEANUP_GRACE_SEC = 10.0
_ZAI_KEY_NAMES = ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY")
_DEEPSEEK_KEY_NAMES = ("DEEPSEEK_API_KEY",)
_ENSURE_PYTHON3_COMMAND = (
    "if command -v apt-get >/dev/null 2>&1; then "
    "DEBIAN_FRONTEND=noninteractive apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y python3; "
    "elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3; "
    "elif command -v dnf >/dev/null 2>&1; then dnf install -y python3; "
    "elif command -v yum >/dev/null 2>&1; then yum install -y python3; "
    "else echo 'no supported package manager for python3' >&2; exit 127; fi"
)


def _managed_models_path() -> Path:
    return Path(__file__).with_name("managed") / "models.json"


def _managed_models_sha256() -> str:
    return hashlib.sha256(_managed_models_path().read_bytes()).hexdigest()


def _load_trial_task(
    logs_dir: Path,
    instruction_sha256: str,
) -> tuple[str, float]:
    trial_config_path = logs_dir.parent / "config.json"
    try:
        trial_config = json.loads(
            trial_config_path.read_text(encoding="utf-8")
        )
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
        max_timeout_sec = trial_config.get("agent", {}).get(
            "max_timeout_sec"
        )
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
    effective_timeout_sec = float(base_timeout_sec)
    if max_timeout_sec is not None:
        effective_timeout_sec = min(
            effective_timeout_sec,
            float(max_timeout_sec),
        )
    if effective_timeout_sec <= 0:
        raise LifecycleConfigurationError(
            "the Terminal-Bench agent timeout must be positive"
        )
    return task_id, effective_timeout_sec


class PiTerminalBenchC0Agent(Pi):
    """Run Harbor 0.20's frozen Pi behind the product-neutral C0 seam."""

    SUPPORTS_RESUME = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        turn_timeout_sec: int = C0_MAX_PRODUCT_TIMEOUT_SEC,
        trigger_timeout_sec: float = C0_MAX_PRODUCT_TIMEOUT_SEC,
        poll_interval_sec: float = 0.5,
        preinstalled: bool = False,
        product_timeout_multiplier: float = C0_PRODUCT_TIMEOUT_MULTIPLIER,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            *args,
            **kwargs,
        )
        if self.version() != FROZEN_PI_VERSION:
            raise ValueError(
                f"Pi C0 requires version {FROZEN_PI_VERSION}, "
                f"got {self.version()!r}"
            )
        if self.model_name == FROZEN_MODEL_NAME:
            self._provider = FROZEN_PROVIDER
            self._model = FROZEN_MODEL
            self._credential_env = "ZAI_API_KEY"
            credential_key_names = _ZAI_KEY_NAMES
            self._reasoning_effort = "high"
        elif self.model_name == DEEPSEEK_MODEL_NAME:
            self._provider = DEEPSEEK_PROVIDER
            self._model = DEEPSEEK_MODEL
            self._credential_env = "DEEPSEEK_API_KEY"
            credential_key_names = _DEEPSEEK_KEY_NAMES
            self._reasoning_effort = "max"
        else:
            raise ValueError(
                "Pi C0 requires model zai/glm-5.2 or "
                "deepseek/deepseek-v4-flash, "
                f"got {self.model_name!r}"
            )
        self.turn_timeout_sec = int(turn_timeout_sec)
        self.trigger_timeout_sec = float(trigger_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.product_timeout_multiplier = float(product_timeout_multiplier)
        if not isinstance(preinstalled, bool):
            raise ValueError("preinstalled must be a boolean")
        self.preinstalled = preinstalled
        if not self.preinstalled:
            raise ValueError(
                "Pi C0 requires the prebuilt 0.73.1 runtime"
            )
        if not 0 < self.turn_timeout_sec <= C0_MAX_PRODUCT_TIMEOUT_SEC:
            raise ValueError(
                "turn_timeout_sec must be in "
                f"(0, {C0_MAX_PRODUCT_TIMEOUT_SEC}]"
            )
        if self.trigger_timeout_sec <= 0 or self.poll_interval_sec <= 0:
            raise ValueError("C0 controller timeouts must be positive")
        if self.product_timeout_multiplier <= 0:
            raise ValueError("product_timeout_multiplier must be positive")

        self._provider_key_value: str | None = None
        for key_name in credential_key_names:
            value = self._get_env(key_name)
            if value:
                self._provider_key_value = value
                break
        for key_name in (*_ZAI_KEY_NAMES, *_DEEPSEEK_KEY_NAMES):
            self._extra_env.pop(key_name, None)
        if not self._provider_key_value:
            raise ValueError(f"Pi C0 requires {self._credential_env}")
        self._c0_metadata: dict[str, Any] = {
            "condition": "C0",
            "fault_injected": False,
            "fault_action": "noop",
            "formal_score_eligible": False,
        }
        self._prebuilt_marker_verified = False
        self._prebuilt_marker_sha256: str | None = None

    @staticmethod
    def name() -> str:
        return "pi-terminal-bench-c0"

    @property
    def extra_env(self) -> dict[str, str]:
        """Expose the key only to Harbor's built-in log scrubber."""
        value = super().extra_env
        if self._provider_key_value:
            value[self._credential_env] = self._provider_key_value
        return value

    def _product_env(self) -> dict[str, str]:
        assert self._provider_key_value is not None
        return {
            self._credential_env: self._provider_key_value,
            "PI_CODING_AGENT_DIR": REMOTE_PI_HOME,
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }

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
            raise RuntimeError("could not resolve a Pi product workspace")
        return cwd

    async def _verify_preinstalled_pi(
        self,
        environment: BaseEnvironment,
    ) -> None:
        marker_result = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_PREBUILT_MARKER)}",
            timeout_sec=10,
        )
        try:
            marker = json.loads(marker_result.stdout or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Pi prebuilt marker is missing or invalid") from exc
        if (
            marker_result.return_code != 0
            or marker.get("schema_version") != 1
            or marker.get("product") != "pi-coding-agent"
            or marker.get("package")
            != "@mariozechner/pi-coding-agent"
            or marker.get("version") != FROZEN_PI_VERSION
        ):
            raise RuntimeError("Pi prebuilt marker does not match the cohort")
        version_result = await environment.exec(
            command="/usr/local/bin/pi --version",
            env=self._product_env(),
            timeout_sec=30,
        )
        version_lines = "\n".join(
            filter(None, (version_result.stdout, version_result.stderr))
        ).strip().splitlines()
        if (
            version_result.return_code != 0
            or not version_lines
            or version_lines[-1] != FROZEN_PI_VERSION
        ):
            raise RuntimeError("Preinstalled Pi version does not match 0.73.1")
        self._prebuilt_marker_verified = True
        self._prebuilt_marker_sha256 = hashlib.sha256(
            (marker_result.stdout or "").encode()
        ).hexdigest()

    async def install(self, environment: BaseEnvironment) -> None:
        if self.preinstalled:
            await self._verify_preinstalled_pi(environment)
        else:
            await super().install(environment)
        python_result = await environment.exec(
            command="command -v python3",
            timeout_sec=10,
        )
        if python_result.return_code != 0:
            await self.exec_as_root(
                environment,
                command=_ENSURE_PYTHON3_COMMAND,
                timeout_sec=300,
            )
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(REMOTE_ROOT)} "
                f"{shlex.quote(REMOTE_PI_HOME)} "
                f"{shlex.quote(REMOTE_SESSIONS)} && "
                f"chmod 0700 {shlex.quote(REMOTE_ROOT)} "
                f"{shlex.quote(REMOTE_PI_HOME)} "
                f"{shlex.quote(REMOTE_SESSIONS)}"
            ),
            timeout_sec=10,
        )
        await environment.upload_file(_managed_models_path(), REMOTE_MODELS)
        await environment.upload_file(
            process_probe_source_path(), REMOTE_PROCESS_PROBE
        )
        await environment.upload_file(
            lifecycle_predicate_probe_source_path(), REMOTE_PREDICATE_PROBE
        )
        await self.exec_as_root(
            environment,
            command=(
                f"chmod 0444 {shlex.quote(REMOTE_MODELS)} && "
                f"chmod 0555 {shlex.quote(REMOTE_PROCESS_PROBE)} "
                f"{shlex.quote(REMOTE_PREDICATE_PROBE)}"
            ),
            timeout_sec=10,
        )
        remote_models = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_MODELS)}",
            timeout_sec=10,
        )
        if (
            remote_models.return_code != 0
            or hashlib.sha256(
                (remote_models.stdout or "").encode()
            ).hexdigest()
            != _managed_models_sha256()
        ):
            raise RuntimeError("Pi managed models.json does not match the cohort")
        model_result = await environment.exec(
            command=f"/usr/local/bin/pi --list-models {self._model}",
            env=self._product_env(),
            timeout_sec=30,
        )
        if (
            model_result.return_code != 0
            or self._model not in (model_result.stdout or "").lower()
        ):
            raise RuntimeError(
                f"Pi cannot resolve the configured {self.model_name} model"
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
        emit: Any,
    ) -> tuple[Any, Exception | None, Any, dict[str, Any] | None, str | None]:
        product_error: Exception | None = None
        product_result = None
        cleanup_report: dict[str, Any] | None = None
        cleanup_sha256: str | None = None

        async def execute_product() -> Any:
            nonlocal product_error, product_result
            nonlocal cleanup_report, cleanup_sha256
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
                    setattr(
                        product_done,
                        "_c0_product_terminal_status",
                        cleanup_report["product_terminal_status"],
                    )
                    emit(
                        "product_process_cleanup",
                        reason=cleanup_report["reason"],
                        product_terminal_status=cleanup_report[
                            "product_terminal_status"
                        ],
                        zero_live_proven=True,
                        remaining_pids_count=0,
                        cleanup_report_sha256=cleanup_sha256,
                        fault_action=False,
                    )
                except Exception as exc:
                    product_error = product_error or exc
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

    async def _collect_artifacts(
        self,
        environment: BaseEnvironment,
        paths: dict[str, str],
    ) -> dict[str, Any]:
        downloads = (
            (paths["stdout"], self.logs_dir / "pi.txt"),
            (paths["stderr"], self.logs_dir / "pi.stderr.txt"),
            (paths["identity"], self.logs_dir / "product.identity.json"),
            (paths["cleanup"], self.logs_dir / "product.cleanup.json"),
        )
        for remote_path, local_path in downloads:
            try:
                await environment.download_file(remote_path, local_path)
            except Exception:
                pass
        session_root = self.logs_dir / "pi-sessions"
        try:
            await environment.download_dir(REMOTE_SESSIONS, session_root)
        except Exception:
            pass
        event_path = self.logs_dir / "pi.txt"
        raw_event_sha256: str | None = None
        try:
            digest = hashlib.sha256()
            with event_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            raw_event_sha256 = digest.hexdigest()
        except OSError:
            pass
        try:
            events = validate_event_stream(
                event_path,
                expected_provider=self._provider,
                expected_model=self._model,
            )
        except (OSError, RuntimeError) as exc:
            return {
                "pi_trajectory_status": "failed",
                "pi_trajectory_error": f"{type(exc).__name__}: {exc}",
                "pi_trajectory_sha256": raw_event_sha256,
                "pi_event_count": 0,
                "pi_session_id": None,
                "pi_session_sha256": None,
                "pi_session_entry_count": 0,
                "pi_final_stop_reason": None,
                "pi_provider_model_verified": False,
            }
        matching: list[dict[str, Any]] = []
        for path in sorted(session_root.rglob("*.jsonl")):
            try:
                matching.append(
                    validate_session(path, session_id=events["session_id"])
                )
            except (OSError, RuntimeError):
                continue
        if len(matching) != 1:
            return {
                "pi_trajectory_status": "failed",
                "pi_trajectory_error": (
                    "expected exactly one saved session matching stdout"
                ),
                "pi_trajectory_sha256": events["sha256"],
                "pi_event_count": events["event_count"],
                "pi_session_id": events["session_id"],
                "pi_session_sha256": None,
                "pi_session_entry_count": 0,
                "pi_final_stop_reason": events["stop_reason"],
                "pi_provider_model_verified": True,
            }
        session = matching[0]
        return {
            "pi_trajectory_status": (
                "saved" if events["complete"] else "failed"
            ),
            "pi_trajectory_error": (
                None if events["complete"] else "terminal stopReason is error/aborted"
            ),
            "pi_trajectory_sha256": events["sha256"],
            "pi_event_count": events["event_count"],
            "pi_assistant_message_count": events[
                "assistant_message_count"
            ],
            "pi_tool_call_count": events["tool_call_count"],
            "pi_session_id": events["session_id"],
            "pi_session_sha256": session["sha256"],
            "pi_session_entry_count": session["entry_count"],
            "pi_final_stop_reason": events["stop_reason"],
            "pi_provider_model_verified": True,
        }

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        run_id = str(uuid.uuid4())
        instruction_sha256 = hashlib.sha256(
            instruction.strip().encode()
        ).hexdigest()
        try:
            trigger = get_terminal_bench_trigger_for_instruction(instruction)
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
        except LifecycleConfigurationError as trigger_error:
            task_id, base_timeout_sec = _load_trial_task(
                self.logs_dir, instruction_sha256
            )
            if "pre-registered" not in str(trigger_error):
                raise
            trigger = ExternalTriggerManifest(
                task_id=task_id,
                predicate_id=GENERIC_C0_PREDICATE_ID,
            )
            trigger_registration_status = "generic"
            trigger_scope = "generic_product_live"
        product_timeout_sec = min(
            self.turn_timeout_sec,
            base_timeout_sec * self.product_timeout_multiplier,
        )
        outer_timeout_sec = product_timeout_sec + C0_HOST_CLEANUP_MARGIN_SEC
        product_cwd = await self._resolve_product_cwd(environment)
        remote_run_root = f"{REMOTE_ROOT}/run-{run_id}"
        paths = {
            "identity": f"{remote_run_root}/product.identity.json",
            "cleanup": f"{remote_run_root}/product.cleanup.json",
            "stdout": f"{remote_run_root}/product.stdout",
            "stderr": f"{remote_run_root}/product.stderr",
            "stdin": f"{remote_run_root}/instruction.md",
        }
        ledger_path = self.logs_dir / "controller.jsonl"
        ledger = JsonlLedger(ledger_path, run_id)
        initial_metadata = {
            "condition": "C0",
            "evaluation_status": "exploratory_unfrozen",
            "formal_score_eligible": False,
            "fault_injected": False,
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
            "task_workdir": product_cwd,
            "pi_version": FROZEN_PI_VERSION,
            "pi_model_provider": self._provider,
            "pi_model": self._model,
            "pi_reasoning_effort": self._reasoning_effort,
            "pi_temperature": None,
            "pi_tools": FROZEN_TOOLS,
            "pi_models_path": REMOTE_MODELS,
            "pi_models_sha256": _managed_models_sha256(),
            "pi_resources_disabled": True,
            "pi_install_mode": "prebuilt" if self.preinstalled else "runtime",
            "pi_prebuilt_marker_verified": self._prebuilt_marker_verified,
            "pi_prebuilt_marker_sha256": self._prebuilt_marker_sha256,
            "trajectory_capture_required": True,
            "trajectory_capture_mode": "pi_jsonl_and_saved_session",
            "trajectory_capture_blocking": False,
        }
        context.metadata = dict(initial_metadata)
        ledger.append(
            "controller_started",
            product="pi",
            product_version=FROZEN_PI_VERSION,
            model_name=self.model_name,
            **initial_metadata,
        )
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_run_root)} "
                f"{shlex.quote(REMOTE_SESSIONS)} && "
                f"chmod 0700 {shlex.quote(remote_run_root)} "
                f"{shlex.quote(REMOTE_SESSIONS)}"
            ),
            timeout_sec=10,
        )
        prompt_path = self.logs_dir / "instruction.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(instruction, encoding="utf-8")
        await environment.upload_file(prompt_path, paths["stdin"])
        preflight = await environment.exec(
            command=f"/usr/local/bin/pi --list-models {self._model}",
            env=self._product_env(),
            timeout_sec=30,
        )
        ledger.append(
            "product_preflight",
            check="version_model_runtime",
            passed=(
                preflight.return_code == 0
                and self._model in (preflight.stdout or "").lower()
            ),
            return_code=preflight.return_code,
        )
        if (
            preflight.return_code != 0
            or self._model not in (preflight.stdout or "").lower()
        ):
            raise RuntimeError("Pi model preflight failed")

        child_argv = [
            "/usr/local/bin/pi",
            "--print",
            "--mode",
            "json",
            "--session-dir",
            REMOTE_SESSIONS,
            "--provider",
            self._provider,
            "--model",
            self._model,
            "--tools",
            FROZEN_TOOLS,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        cli_flags = self.build_cli_flags()
        if cli_flags:
            child_argv.extend(shlex.split(cli_flags))
        product_command = process_probe_run_command(
            probe_path=REMOTE_PROCESS_PROBE,
            identity_path=paths["identity"],
            stdout_path=paths["stdout"],
            stderr_path=paths["stderr"],
            stdin_path=paths["stdin"],
            cwd=product_cwd,
            child_argv=child_argv,
            deadline_sec=product_timeout_sec,
            cleanup_report_path=paths["cleanup"],
            cleanup_grace_sec=C0_CLEANUP_GRACE_SEC,
            strict_cleanup=True,
            exclude_stdout_json_events=[
                "message_update",
                "tool_execution_update",
            ],
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
            product_cwd=product_cwd,
            timeout_sec=outer_timeout_sec,
            controller=controller,
            product_done=product_done,
            paths=paths,
            emit=ledger.append,
        )
        capture = await self._collect_artifacts(environment, paths)
        return_code = (
            product_result.return_code if product_result is not None else None
        )
        product_terminal_status = (
            cleanup_report["product_terminal_status"]
            if cleanup_report
            else "adapter_infra_error"
        )
        ledger.append(
            "product_turn_exited",
            return_code=return_code,
            error_type=(
                type(product_error).__name__ if product_error else None
            ),
            product_terminal_status=product_terminal_status,
        )
        lifecycle_gate_passed = bool(
            outcome.trigger_hit and not outcome.fault_injected
        )
        product_completion_claim = bool(
            return_code == 0
            and capture["pi_trajectory_status"] == "saved"
        )
        metadata = {
            **initial_metadata,
            **capture,
            "trigger_hit": outcome.trigger_hit,
            "trigger_reason": outcome.reason,
            "trigger_evidence_sha256": outcome.evidence_sha256,
            "lifecycle_gate_passed": lifecycle_gate_passed,
            "product_return_code": return_code,
            "product_error_type": (
                type(product_error).__name__ if product_error else None
            ),
            "product_completion_claim": product_completion_claim,
            "product_terminal_status": product_terminal_status,
            "product_cleanup_zero_live_proven": bool(
                cleanup_report and cleanup_report["zero_live_proven"]
            ),
            "product_cleanup_report_sha256": cleanup_sha256,
        }
        self._c0_metadata = dict(metadata)
        context.metadata = dict(metadata)
        super().populate_context_post_run(context)
        context.metadata = dict(metadata)
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
            pi_trajectory_status=capture["pi_trajectory_status"],
            pi_trajectory_error=capture["pi_trajectory_error"],
            pi_trajectory_sha256=capture["pi_trajectory_sha256"],
            pi_event_count=capture["pi_event_count"],
            pi_session_id=capture["pi_session_id"],
            pi_session_sha256=capture["pi_session_sha256"],
            pi_session_entry_count=capture["pi_session_entry_count"],
            pi_final_stop_reason=capture["pi_final_stop_reason"],
            pi_provider_model_verified=capture[
                "pi_provider_model_verified"
            ],
            trajectory_capture_blocking=False,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        metadata = dict(context.metadata or {})
        metadata.update(self._c0_metadata)
        context.metadata = metadata
