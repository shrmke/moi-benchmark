from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any

import yaml

from harbor.agents.installed.hermes import Hermes
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from astra.runners.hermes_terminal_bench.gateway_driver import (
    validate_run_event_stream,
    validate_session_export,
)
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


FROZEN_HERMES_VERSION = "v2026.7.20"
FROZEN_HERMES_RELEASE = "0.19.0"
FROZEN_HERMES_COMMIT = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
FROZEN_HERMES_INSTALLER_SHA256 = (
    "c5ba7e89627577fab914514736ecfb335"
    "9b66956ca00199bfef616ca35953cb9"
)
FROZEN_PLAYWRIGHT_RELEASE = "1.62.0"
FROZEN_MODEL_NAME = "zai/glm-5.2"
DEEPSEEK_MODEL_NAME = "deepseek/deepseek-v4-flash"
FROZEN_MAX_TURNS = 90
REMOTE_PREBUILT_MARKER = "/opt/moi/hermes-preinstalled.json"
REMOTE_HERMES_PYTHON = "/usr/local/lib/hermes-agent/venv/bin/python"
REMOTE_ROOT = "/tmp/hermes-c0"
REMOTE_DRIVER = "/installed-agent/hermes-c0-gateway-driver.py"
REMOTE_RESULT = "/logs/agent/hermes-run.json"
REMOTE_EVENTS = "/logs/agent/hermes-run-events.jsonl"
REMOTE_GATEWAY_LOG = "/logs/agent/hermes-gateway.txt"
REMOTE_SESSION_EXPORT = "/logs/agent/hermes-session.jsonl"
REMOTE_HERMES_HOME = "/tmp/hermes"
REMOTE_MANAGED_DIR = "/etc/hermes"
REMOTE_MANAGED_CONFIG = "/etc/hermes/config.yaml"
REMOTE_MANAGED_ENV = "/etc/hermes/.env"
REMOTE_POLICY_GUARD_DIR = "/installed-agent/hermes-c0-policy"
REMOTE_POLICY_GUARD = (
    "/installed-agent/hermes-c0-policy/sitecustomize.py"
)
REMOTE_POLICY_GUARD_EVIDENCE = "/logs/agent/hermes-policy-guard.jsonl"
REMOTE_PROCESS_PROBE = "/installed-agent/lifecycle-process-probe.py"
REMOTE_PREDICATE_PROBE = "/installed-agent/lifecycle-predicate-probe.py"
_ZAI_KEY_NAMES = ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY")
_DEEPSEEK_KEY_NAMES = ("DEEPSEEK_API_KEY",)
_C0_PRODUCT_TIMEOUT_SEC = {
    "modernize-scientific-stack": 1200,
    "overfull-hbox": 1500,
    "build-pmars": 1800,
    "db-wal-recovery": 1800,
}
_C0_CLEANUP_GRACE_SEC = 2.0
_C0_DRIVER_CLEANUP_ALLOWANCE_SEC = 30
_C0_HOST_CLEANUP_MARGIN_SEC = 40
_C0_PRODUCT_TIMEOUT_MULTIPLIER = 2.0
_C0_MAX_PRODUCT_TIMEOUT_SEC = 24000
_GENERIC_C0_PREDICATE_ID = "terminal-bench.generic.product-live"
_TRAJECTORY_FINALIZE_TIMEOUT_SEC = 45
_TRAJECTORY_DOWNLOAD_TIMEOUT_SEC = 10
_OPTIONAL_LOG_COLLECTION_TIMEOUT_SEC = 5
_ENSURE_PYTHON3_COMMAND = (
    "if command -v apt-get >/dev/null 2>&1; then "
    "DEBIAN_FRONTEND=noninteractive apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y python3; "
    "elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3; "
    "elif command -v dnf >/dev/null 2>&1; then dnf install -y python3; "
    "elif command -v yum >/dev/null 2>&1; then yum install -y python3; "
    "else echo 'no supported package manager for python3' >&2; exit 127; fi"
)


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
    if (
        task_id != task_path.name
        or local_instruction_sha256 != instruction_sha256
    ):
        raise LifecycleConfigurationError(
            "the Harbor trial task does not match the supplied instruction"
        )
    try:
        effective_timeout_sec = float(base_timeout_sec)
        if max_timeout_sec is not None:
            effective_timeout_sec = min(
                effective_timeout_sec,
                float(max_timeout_sec),
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


class HermesTerminalBenchS0Agent(Hermes):
    """Attach the S0 condition label to Harbor's built-in Hermes adapter."""

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        metadata = dict(context.metadata or {})
        metadata.update(
            {
                "condition": "S0",
                "fault_injected": False,
            }
        )
        context.metadata = metadata


class HermesTerminalBenchC0Agent(Hermes):
    """Run Hermes behind the C0 lifecycle seam through its gateway Runs API."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        max_turns: int = 90,
        turn_timeout_sec: int = 1800,
        trigger_timeout_sec: float = 1800,
        poll_interval_sec: float = 0.5,
        gateway_port: int = 18642,
        preinstalled: bool = False,
        product_timeout_multiplier: float = _C0_PRODUCT_TIMEOUT_MULTIPLIER,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            *args,
            **kwargs,
        )
        if self.version() != FROZEN_HERMES_VERSION:
            raise ValueError(
                f"Hermes C0 requires version {FROZEN_HERMES_VERSION}, "
                f"got {self.version()!r}"
            )
        if self.model_name == FROZEN_MODEL_NAME:
            self._managed_dir_name = "managed"
            credential_key_names = _ZAI_KEY_NAMES
            self._reasoning_effort = "high"
            self._temperature = 0.0
        elif self.model_name == DEEPSEEK_MODEL_NAME:
            self._managed_dir_name = "managed-deepseek"
            credential_key_names = _DEEPSEEK_KEY_NAMES
            self._reasoning_effort = "max"
            self._temperature = None
        else:
            raise ValueError(
                "Hermes C0 requires model zai/glm-5.2 or "
                "deepseek/deepseek-v4-flash, "
                f"got {self.model_name!r}"
            )
        self.max_turns = int(max_turns)
        if self.max_turns != FROZEN_MAX_TURNS:
            raise ValueError(
                f"Hermes C0 requires max_turns={FROZEN_MAX_TURNS}, "
                f"got {self.max_turns}"
            )
        self.turn_timeout_sec = int(turn_timeout_sec)
        self.trigger_timeout_sec = float(trigger_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.gateway_port = int(gateway_port)
        self.product_timeout_multiplier = float(product_timeout_multiplier)
        if not isinstance(preinstalled, bool):
            raise ValueError("preinstalled must be a boolean")
        self.preinstalled = preinstalled
        self._prebuilt_marker_verified = False
        self._prebuilt_marker_sha256: str | None = None
        if self.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if not 0 < self.turn_timeout_sec <= _C0_MAX_PRODUCT_TIMEOUT_SEC:
            raise ValueError(
                "turn_timeout_sec must be in "
                f"(0, {_C0_MAX_PRODUCT_TIMEOUT_SEC}]"
            )
        if self.trigger_timeout_sec <= 0 or self.poll_interval_sec <= 0:
            raise ValueError("C0 controller timeouts must be positive")
        if self.product_timeout_multiplier <= 0:
            raise ValueError("product_timeout_multiplier must be positive")
        if not 1024 <= self.gateway_port <= 65535:
            raise ValueError("gateway_port must be between 1024 and 65535")

        self._provider_key_name: str | None = None
        self._provider_key_value: str | None = None
        for key_name in credential_key_names:
            key_value = self._get_env(key_name)
            if key_value:
                self._provider_key_name = key_name
                self._provider_key_value = key_value
                break
        # The model credential is supplied only to the tested product process
        # tree, never to setup, probes, or controller commands.
        for key_name in (*_ZAI_KEY_NAMES, *_DEEPSEEK_KEY_NAMES):
            self._extra_env.pop(key_name, None)
        if not self._provider_key_value:
            raise ValueError(
                f"Hermes C0 requires {credential_key_names[0]}"
            )

        self._c0_metadata: dict[str, Any] = {
            "condition": "C0",
            "fault_injected": False,
            "fault_action": "noop",
            "approval_policy": "hermes_native_smart_then_deterministic_deny",
            "unresolved_approval_action": "deny",
            "yolo_enabled": False,
            "accept_hooks_enabled": False,
            "formal_score_eligible": False,
        }

    @staticmethod
    def name() -> str:
        return "hermes-terminal-bench-c0"

    def _arm_harbor_secret_scrub(self) -> None:
        """Register the provider key only after Harbor snapshots run env."""
        if self._provider_key_name and self._provider_key_value:
            self._extra_env[self._provider_key_name] = self._provider_key_value

    @staticmethod
    def _build_c0_config_yaml(max_turns: int) -> str:
        config: dict[str, Any] = {"agent": {"max_turns": max_turns}}
        return yaml.dump(config, default_flow_style=False, sort_keys=False)

    def _managed_config_path(self) -> Path:
        return Path(__file__).with_name(self._managed_dir_name) / "config.yaml"

    def _managed_config_sha256(self) -> str:
        return hashlib.sha256(self._managed_config_path().read_bytes()).hexdigest()

    def _managed_env_path(self) -> Path:
        return Path(__file__).with_name(self._managed_dir_name) / ".env"

    def _managed_env_sha256(self) -> str:
        return hashlib.sha256(self._managed_env_path().read_bytes()).hexdigest()

    @staticmethod
    def _policy_guard_path() -> Path:
        return (
            Path(__file__).with_name("policy_guard") / "sitecustomize.py"
        )

    @classmethod
    def _policy_guard_sha256(cls) -> str:
        return hashlib.sha256(cls._policy_guard_path().read_bytes()).hexdigest()

    @staticmethod
    def _mount_is_read_only(mountinfo: str, target: str) -> bool:
        for line in mountinfo.splitlines():
            fields = line.split()
            if len(fields) >= 6 and fields[4] == target:
                return "ro" in fields[5].split(",")
        return False

    @staticmethod
    def _gateway_driver_argv(
        *,
        instruction_path: str,
        provider_env_path: str,
        session_id: str,
        gateway_port: int,
        timeout_sec: int,
        product_cwd: str = "/app",
    ) -> list[str]:
        return [
            REMOTE_HERMES_PYTHON,
            REMOTE_DRIVER,
            "--instruction-file",
            instruction_path,
            "--result-file",
            REMOTE_RESULT,
            "--events-file",
            REMOTE_EVENTS,
            "--gateway-log",
            REMOTE_GATEWAY_LOG,
            "--provider-env-file",
            provider_env_path,
            "--session-id",
            session_id,
            "--port",
            str(gateway_port),
            "--timeout-sec",
            str(timeout_sec),
            "--cwd",
            product_cwd,
            "--policy-guard-dir",
            REMOTE_POLICY_GUARD_DIR,
            "--policy-guard-sha256",
            HermesTerminalBenchC0Agent._policy_guard_sha256(),
            "--policy-guard-evidence",
            REMOTE_POLICY_GUARD_EVIDENCE,
        ]

    def _product_env(self) -> dict[str, str]:
        return {
            "HERMES_HOME": REMOTE_HERMES_HOME,
            "HERMES_MANAGED_DIR": "/etc/hermes",
            "HERMES_YOLO_MODE": "0",
            "HERMES_ACCEPT_HOOKS": "0",
            "TERMINAL_ENV": "local",
        }

    def _guarded_product_env(self) -> dict[str, str]:
        return {
            **self._product_env(),
            "PYTHONPATH": REMOTE_POLICY_GUARD_DIR,
            "HERMES_C0_POLICY_GUARD_SHA256": self._policy_guard_sha256(),
            "HERMES_C0_POLICY_GUARD_EVIDENCE": (
                REMOTE_POLICY_GUARD_EVIDENCE
            ),
        }

    @staticmethod
    async def _resolve_product_cwd(environment: BaseEnvironment) -> str:
        """Use the conventional task workspace when an image lacks /app."""
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
            raise RuntimeError("could not resolve a Hermes product workspace")
        return cwd

    @staticmethod
    def _expected_prebuilt_marker() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "product": "hermes-agent",
            "hermes_ref": FROZEN_HERMES_VERSION,
            "hermes_release": FROZEN_HERMES_RELEASE,
            "source_commit": FROZEN_HERMES_COMMIT,
            "installer_sha256": FROZEN_HERMES_INSTALLER_SHA256,
            "playwright_release": FROZEN_PLAYWRIGHT_RELEASE,
        }

    async def _verify_preinstalled_hermes(
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
            raise RuntimeError(
                "Hermes prebuilt marker is missing or invalid"
            ) from exc
        if (
            marker_result.return_code != 0
            or not isinstance(marker, dict)
            or any(
                marker.get(key) != value
                for key, value in self._expected_prebuilt_marker().items()
            )
        ):
            raise RuntimeError(
                "Hermes prebuilt marker does not match the frozen build"
            )
        executable_result = await environment.exec(
            command=(
                'export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"; '
                "command -v hermes"
            ),
            timeout_sec=10,
        )
        if (
            executable_result.return_code != 0
            or (executable_result.stdout or "").strip()
            != "/usr/local/bin/hermes"
        ):
            raise RuntimeError(
                "Preinstalled Hermes executable is missing or shadowed"
            )
        version_result = await environment.exec(
            command="/usr/local/bin/hermes version",
            env=self._product_env(),
            timeout_sec=30,
        )
        expected_version = (
            f"Hermes Agent v{FROZEN_HERMES_RELEASE} "
            f"({FROZEN_HERMES_VERSION.removeprefix('v')})"
        )
        if (
            version_result.return_code != 0
            or expected_version
            not in (version_result.stdout or "").splitlines()
        ):
            raise RuntimeError(
                "Preinstalled Hermes executable does not match the frozen "
                "release"
            )
        commit_result = await environment.exec(
            command="cat /usr/local/lib/hermes-agent/.git/HEAD",
            timeout_sec=10,
        )
        if (
            commit_result.return_code != 0
            or (commit_result.stdout or "").strip()
            != FROZEN_HERMES_COMMIT
        ):
            raise RuntimeError(
                "Preinstalled Hermes source commit does not match the "
                "frozen revision"
            )
        self._prebuilt_marker_verified = True
        self._prebuilt_marker_sha256 = hashlib.sha256(
            (marker_result.stdout or "").encode()
        ).hexdigest()

    async def install(self, environment: BaseEnvironment) -> None:
        managed_config = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_MANAGED_CONFIG)}",
            timeout_sec=10,
        )
        if (
            managed_config.return_code != 0
            or hashlib.sha256(
                (managed_config.stdout or "").encode()
            ).hexdigest()
            != self._managed_config_sha256()
        ):
            raise RuntimeError(
                "Hermes C0 managed config is missing or does not match "
                "the frozen policy"
            )
        managed_env = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_MANAGED_ENV)}",
            timeout_sec=10,
        )
        managed_env_sha256 = self._managed_env_sha256()
        if (
            managed_env.return_code != 0
            or hashlib.sha256(
                (managed_env.stdout or "").encode()
            ).hexdigest()
            != managed_env_sha256
        ):
            raise RuntimeError(
                "Hermes C0 managed environment is missing or does not match "
                "the frozen policy"
            )
        mountinfo = await environment.exec(
            command="cat /proc/self/mountinfo",
            timeout_sec=10,
        )
        if (
            mountinfo.return_code != 0
            or not self._mount_is_read_only(
                mountinfo.stdout or "", REMOTE_MANAGED_DIR
            )
        ):
            raise RuntimeError(
                "Hermes C0 managed directory must be a read-only bind mount"
            )

        if self.preinstalled:
            await self._verify_preinstalled_hermes(environment)
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
        python_result = await environment.exec(
            command="command -v python3",
            timeout_sec=10,
        )
        if python_result.return_code != 0:
            raise RuntimeError("Hermes C0 requires python3 for lifecycle probes")
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(REMOTE_ROOT)} "
                f"{shlex.quote(REMOTE_POLICY_GUARD_DIR)} && "
                f"chmod 0700 {shlex.quote(REMOTE_ROOT)} && "
                f"chmod 0755 {shlex.quote(REMOTE_POLICY_GUARD_DIR)}"
            ),
            env={"HERMES_HOME": REMOTE_HERMES_HOME},
            timeout_sec=10,
        )
        await environment.upload_file(
            Path(__file__).with_name("gateway_driver.py"),
            REMOTE_DRIVER,
        )
        await environment.upload_file(
            process_probe_source_path(),
            REMOTE_PROCESS_PROBE,
        )
        await environment.upload_file(
            lifecycle_predicate_probe_source_path(),
            REMOTE_PREDICATE_PROBE,
        )
        await environment.upload_file(
            self._policy_guard_path(),
            REMOTE_POLICY_GUARD,
        )
        await self.exec_as_root(
            environment,
            (
                f"chmod 0555 {shlex.quote(REMOTE_DRIVER)} "
                f"{shlex.quote(REMOTE_PROCESS_PROBE)} "
                f"{shlex.quote(REMOTE_PREDICATE_PROBE)} "
                f"{shlex.quote(REMOTE_POLICY_GUARD)}"
            ),
        )
        remote_guard = await environment.exec(
            command=f"cat {shlex.quote(REMOTE_POLICY_GUARD)}",
            timeout_sec=10,
        )
        if (
            remote_guard.return_code != 0
            or hashlib.sha256(
                (remote_guard.stdout or "").encode()
            ).hexdigest()
            != self._policy_guard_sha256()
        ):
            raise RuntimeError(
                "Hermes C0 policy guard is missing or does not match "
                "the frozen source"
            )
        policy_env = self._guarded_product_env()
        approval_mode = await environment.exec(
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "hermes config get approvals.mode"
            ),
            env=policy_env,
            timeout_sec=30,
        )
        if (
            approval_mode.return_code != 0
            or (approval_mode.stdout or "").strip() != "smart"
        ):
            raise RuntimeError(
                "Hermes C0 managed approvals.mode did not resolve to smart"
            )
        mutation = await environment.exec(
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "hermes config set approvals.mode off"
            ),
            env=policy_env,
            timeout_sec=30,
        )
        if mutation.return_code == 0:
            raise RuntimeError(
                "Hermes C0 managed approval policy was unexpectedly writable"
            )

    async def _run_with_c0_controller(
        self,
        environment: BaseEnvironment,
        *,
        product_command: str,
        product_cwd: str = "/app",
        env: dict[str, str],
        timeout_sec: int,
        controller: C0Controller,
        product_done: asyncio.Event,
        cleanup_paths: dict[str, str] | None = None,
        emit: Any = None,
    ) -> tuple[Any, Exception | None, Any]:
        """Run the real product tree and external controller concurrently."""

        product_error: Exception | None = None
        self._last_process_cleanup = None
        self._last_process_cleanup_sha256 = None
        self._last_product_exec_result = None

        def save_product_launch_output(result: Any) -> None:
            """Keep shell-level diagnostics when the probe never starts."""
            for attribute, filename in (
                ("stdout", "product-launch.stdout.txt"),
                ("stderr", "product-launch.stderr.txt"),
            ):
                value = getattr(result, attribute, None)
                if isinstance(value, str) and value:
                    (self.logs_dir / filename).write_text(
                        value,
                        encoding="utf-8",
                    )

        async def execute_product() -> Any:
            nonlocal product_error
            request_cleanup = False
            try:
                result = await environment.exec(
                    command=product_command,
                    cwd=product_cwd,
                    env=env,
                    timeout_sec=timeout_sec,
                )
                self._last_product_exec_result = result
                save_product_launch_output(result)
                return result
            except asyncio.CancelledError:
                request_cleanup = True
                raise
            except Exception as exc:
                product_error = exc
                request_cleanup = True
                return None
            finally:
                try:
                    if cleanup_paths is not None:
                        try:
                            driver_result = await environment.exec(
                                command=f"cat {shlex.quote(REMOTE_RESULT)}",
                                timeout_sec=5,
                            )
                            if driver_result.return_code == 0:
                                try:
                                    driver_value = json.loads(
                                        driver_result.stdout or ""
                                    )
                                except json.JSONDecodeError:
                                    driver_value = None
                                if isinstance(driver_value, dict):
                                    (
                                        self.logs_dir / "hermes-run.json"
                                    ).write_text(
                                        driver_result.stdout or "",
                                        encoding="utf-8",
                                    )
                        except Exception:
                            pass
                        report, report_sha256 = (
                            await collect_process_cleanup_report(
                                environment,
                                probe_path=REMOTE_PROCESS_PROBE,
                                identity_path=cleanup_paths["identity"],
                                cleanup_report_path=cleanup_paths["cleanup"],
                                request_cleanup=request_cleanup,
                            )
                        )
                        terminal_status = report["product_terminal_status"]
                        self._last_process_cleanup = dict(report)
                        self._last_process_cleanup_sha256 = report_sha256
                        setattr(
                            product_done,
                            "_c0_product_terminal_status",
                            terminal_status,
                        )
                        if emit is not None:
                            emit(
                                "product_process_cleanup",
                                reason=report["reason"],
                                product_terminal_status=terminal_status,
                                zero_live_proven=True,
                                remaining_pids_count=0,
                                cleanup_report_sha256=report_sha256,
                                fault_action=False,
                            )
                except Exception as exc:
                    # A non-zero product exit is already the primary failure.
                    # Do not replace it with a secondary missing-cleanup error;
                    # metadata remains fail-closed because no clean report was
                    # recorded. A zero exit still requires strict proof.
                    suppress_cleanup_error = (
                        self._last_product_exec_result is not None
                        and self._last_product_exec_result.return_code != 0
                    )
                    if not suppress_cleanup_error:
                        if product_error is None:
                            product_error = exc
                        raise
                finally:
                    product_done.set()

        async with asyncio.TaskGroup() as group:
            product_task = group.create_task(execute_product())
            controller_task = group.create_task(
                controller.run(environment, product_done)
            )
        return product_task.result(), product_error, controller_task.result()

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Harbor 0.20 snapshots scoped_exec_env before entering run(), then
        # reads extra_env again for its final jobs-dir scrub. Delayed
        # registration therefore enables redaction without exposing the key
        # to this single-step C0 run's container commands.
        self._arm_harbor_secret_scrub()
        run_id = str(uuid.uuid4())
        ledger_path = self.logs_dir / "controller.jsonl"
        ledger = JsonlLedger(ledger_path, run_id)
        instruction_sha256 = hashlib.sha256(
            instruction.strip().encode()
        ).hexdigest()
        try:
            trigger = get_terminal_bench_trigger_for_instruction(instruction)
        except LifecycleConfigurationError:
            task_id, base_timeout_sec = _load_trial_task(
                self.logs_dir,
                instruction_sha256,
            )
            if task_id in _C0_PRODUCT_TIMEOUT_SEC:
                raise LifecycleConfigurationError(
                    f"registered C0 instruction hash changed for {task_id!r}"
                )
            trigger = ExternalTriggerManifest(
                task_id=task_id,
                predicate_id=_GENERIC_C0_PREDICATE_ID,
            )
            trigger_registration_status = "generic"
            trigger_scope = "generic_product_live"
            configured_product_timeout_sec = (
                base_timeout_sec * self.product_timeout_multiplier
            )
        else:
            task_id = trigger.task_id
            trigger_registration_status = "task_specific"
            trigger_scope = "task_specific_progress"
            trial_config_path = self.logs_dir.parent / "config.json"
            if trial_config_path.is_file():
                resolved_task_id, base_timeout_sec = _load_trial_task(
                    self.logs_dir,
                    instruction_sha256,
                )
                if resolved_task_id != task_id:
                    raise LifecycleConfigurationError(
                        "the registered C0 trigger does not match the "
                        "Harbor task"
                    )
                configured_product_timeout_sec = (
                    base_timeout_sec * self.product_timeout_multiplier
                )
            else:
                configured_product_timeout_sec = _C0_PRODUCT_TIMEOUT_SEC[
                    task_id
                ]
        product_timeout_sec = min(
            self.turn_timeout_sec,
            configured_product_timeout_sec,
        )
        controller_timeout_sec = min(
            self.trigger_timeout_sec,
            product_timeout_sec,
        )
        outer_timeout_sec = (
            product_timeout_sec + _C0_HOST_CLEANUP_MARGIN_SEC
        )
        supervisor_deadline_sec = (
            product_timeout_sec + _C0_DRIVER_CLEANUP_ALLOWANCE_SEC
        )
        driver_timeout_sec = product_timeout_sec
        managed_policy_sha256 = self._managed_config_sha256()
        managed_policy_artifact = self.logs_dir / "hermes-managed-config.yaml"
        managed_policy_artifact.parent.mkdir(parents=True, exist_ok=True)
        managed_policy_artifact.write_bytes(
            self._managed_config_path().read_bytes()
        )
        managed_env_sha256 = self._managed_env_sha256()
        managed_env_artifact = self.logs_dir / "hermes-managed.env"
        managed_env_artifact.write_bytes(
            self._managed_env_path().read_bytes()
        )
        policy_guard_sha256 = self._policy_guard_sha256()
        policy_guard_artifact = self.logs_dir / "hermes-policy-guard.py"
        policy_guard_artifact.write_bytes(
            self._policy_guard_path().read_bytes()
        )
        session_id = str(uuid.uuid4())
        remote_run_root = f"{REMOTE_ROOT}/run-{run_id}"
        paths = {
            "identity": f"{remote_run_root}/product.identity.json",
            "cleanup": f"{remote_run_root}/product.cleanup.json",
            "stdout": f"{remote_run_root}/product.stdout",
            "stderr": f"{remote_run_root}/product.stderr",
            "stdin": f"{remote_run_root}/instruction.md",
        }
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
                configured_product_timeout_sec
            ),
            "product_timeout_multiplier": (
                self.product_timeout_multiplier
            ),
            "product_timeout_sec": product_timeout_sec,
            "outer_cleanup_timeout_sec": outer_timeout_sec,
            "driver_timeout_sec": driver_timeout_sec,
            "supervisor_deadline_sec": supervisor_deadline_sec,
            "approval_policy": "hermes_native_smart_then_deterministic_deny",
            "managed_policy_path": REMOTE_MANAGED_CONFIG,
            "managed_policy_sha256": managed_policy_sha256,
            "managed_policy_read_only": True,
            "managed_env_path": REMOTE_MANAGED_ENV,
            "managed_env_sha256": managed_env_sha256,
            "managed_env_read_only": True,
            "policy_guard_path": REMOTE_POLICY_GUARD,
            "policy_guard_sha256": policy_guard_sha256,
            "hermes_install_mode": (
                "prebuilt" if self.preinstalled else "runtime"
            ),
            "hermes_prebuilt_marker_path": (
                REMOTE_PREBUILT_MARKER if self.preinstalled else None
            ),
            "hermes_prebuilt_marker_verified": (
                self._prebuilt_marker_verified
            ),
            "hermes_prebuilt_marker_sha256": (
                self._prebuilt_marker_sha256
            ),
            "hermes_prebuilt_source_commit": (
                FROZEN_HERMES_COMMIT if self.preinstalled else None
            ),
            "hermes_prebuilt_installer_sha256": (
                FROZEN_HERMES_INSTALLER_SHA256
                if self.preinstalled
                else None
            ),
            "hermes_prebuilt_playwright_release": (
                FROZEN_PLAYWRIGHT_RELEASE if self.preinstalled else None
            ),
            "unresolved_approval_action": "deny",
            "yolo_enabled": False,
            "accept_hooks_enabled": False,
            "hermes_session_id": session_id,
            "hermes_model": self.model_name,
            "hermes_reasoning_effort": self._reasoning_effort,
            "hermes_temperature": self._temperature,
            "trajectory_capture_required": True,
            "trajectory_capture_mode": "streaming_runs_api_jsonl",
            "trajectory_session_export_required": True,
            "trajectory_capture_blocking": False,
        }
        self._c0_metadata = dict(initial_metadata)
        context.metadata = dict(initial_metadata)
        ledger.append(
            "controller_started",
            product="hermes",
            product_version=self.version(),
            model_name=self.model_name,
            max_turns=self.max_turns,
            configured_turn_timeout_cap_sec=self.turn_timeout_sec,
            **initial_metadata,
        )

        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_run_root)} /logs/agent && "
                f"chmod 0700 {shlex.quote(remote_run_root)}"
            ),
            env={"HERMES_HOME": REMOTE_HERMES_HOME},
            timeout_sec=10,
        )
        prompt_path = self.logs_dir / "instruction.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(instruction, encoding="utf-8")
        await environment.upload_file(prompt_path, paths["stdin"])
        if not self._provider_key_name or not self._provider_key_value:
            raise RuntimeError("Hermes provider credential is unavailable")
        remote_provider_env = f"{remote_run_root}/provider.json"
        with tempfile.TemporaryDirectory(
            prefix="hermes-c0-provider-"
        ) as directory:
            local_provider_env = Path(directory) / "provider.json"
            local_provider_env.write_text(
                json.dumps(
                    {
                        "key_name": self._provider_key_name,
                        "key_value": self._provider_key_value,
                    }
                ),
                encoding="utf-8",
            )
            local_provider_env.chmod(0o600)
            await environment.upload_file(
                local_provider_env,
                remote_provider_env,
            )
        protected = await environment.exec(
            command=f"chmod 0600 {shlex.quote(remote_provider_env)}",
            timeout_sec=10,
        )
        if protected.return_code != 0:
            raise RuntimeError("could not protect the Hermes provider credential")

        config_yaml = self._build_c0_config_yaml(self.max_turns)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(REMOTE_HERMES_HOME)} && "
                f"cat > {shlex.quote(REMOTE_HERMES_HOME + '/config.yaml')} "
                "<< 'EOF'\n"
                f"{config_yaml}EOF"
            ),
            env={"HERMES_HOME": REMOTE_HERMES_HOME},
            timeout_sec=10,
        )

        mcp_command = self._build_register_mcp_servers_command()
        if mcp_command:
            await self.exec_as_agent(
                environment,
                command=mcp_command,
                env=self._guarded_product_env(),
                timeout_sec=10,
            )
        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(
                environment,
                command=skills_command,
                env={"HERMES_HOME": REMOTE_HERMES_HOME},
                timeout_sec=10,
            )

        version_check = await environment.exec(
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "hermes version"
            ),
            env=self._guarded_product_env(),
            timeout_sec=30,
        )
        ledger.append(
            "product_preflight",
            check="version",
            passed=version_check.return_code == 0,
            return_code=version_check.return_code,
        )
        if version_check.return_code != 0:
            raise RuntimeError("Hermes executable failed its version preflight")

        product_cwd = await self._resolve_product_cwd(environment)
        child_argv = self._gateway_driver_argv(
            instruction_path=paths["stdin"],
            provider_env_path=remote_provider_env,
            session_id=session_id,
            gateway_port=self.gateway_port,
            timeout_sec=driver_timeout_sec,
            product_cwd=product_cwd,
        )
        probe_command = process_probe_run_command(
            probe_path=REMOTE_PROCESS_PROBE,
            identity_path=paths["identity"],
            stdout_path=paths["stdout"],
            stderr_path=paths["stderr"],
            stdin_path=paths["stdin"],
            cwd=product_cwd,
            child_argv=child_argv,
            deadline_sec=supervisor_deadline_sec,
            cleanup_report_path=paths["cleanup"],
            cleanup_grace_sec=_C0_CLEANUP_GRACE_SEC,
            strict_cleanup=True,
        )
        product_command = (
            'export PATH="$HOME/.local/bin:$PATH" && '
            f"{probe_command}"
        )
        product_done = asyncio.Event()
        controller = C0Controller(
            C0ControllerConfig(
                identity_path=paths["identity"],
                predicate_probe_path=REMOTE_PREDICATE_PROBE,
                trigger=trigger,
                trigger_timeout_sec=controller_timeout_sec,
                process_probe_path=REMOTE_PROCESS_PROBE,
                poll_interval_sec=self.poll_interval_sec,
            ),
            ledger.append,
        )
        ledger.append(
            "trigger_armed",
            task_id=trigger.task_id,
            predicate_id=trigger.predicate_id,
            trigger_manifest_sha256=trigger.sha256,
        )
        ledger.append("product_turn_started")

        product_result = None
        product_error: Exception | None = None
        outcome = None
        pending_cancel: asyncio.CancelledError | None = None
        pending_error: Exception | None = None
        try:
            product_result, product_error, outcome = (
                await self._run_with_c0_controller(
                    environment,
                    product_command=product_command,
                    product_cwd=product_cwd,
                    env=self._product_env(),
                    timeout_sec=outer_timeout_sec,
                    controller=controller,
                    product_done=product_done,
                    cleanup_paths=paths,
                    emit=ledger.append,
                )
            )
        except asyncio.CancelledError as exc:
            pending_cancel = exc
        except Exception as exc:
            pending_error = exc
            if product_error is None:
                product_error = exc

        if product_result is None:
            product_result = self._last_product_exec_result

        return_code = (
            product_result.return_code if product_result is not None else None
        )
        cleanup_report = self._last_process_cleanup
        value = self._load_driver_result()
        driver_status = value.get("status") if value else None
        driver_error = value.get("error") if value else None
        driver_error_type = None
        if isinstance(driver_error, str) and driver_error:
            candidate = driver_error.partition(":")[0].strip()
            if candidate.isidentifier():
                driver_error_type = candidate
        product_error_type = driver_error_type or (
            type(product_error).__name__ if product_error else None
        )
        product_terminal_status = (
            cleanup_report["product_terminal_status"]
            if cleanup_report
            else {
                "timed_out": "timeout",
                "cancelled": "cancelled",
                "failed": "failed",
            }.get(driver_status, "adapter_infra_error")
        )
        ledger.append(
            "product_turn_exited",
            return_code=return_code,
            error_type=product_error_type,
            product_terminal_status=product_terminal_status,
        )

        driver_run_id = value.get("run_id") if value else None
        finalize_task = asyncio.create_task(
            asyncio.wait_for(
                self._finalize_run_artifacts(
                    environment,
                    paths=paths,
                    run_id=driver_run_id,
                    session_id=session_id,
                ),
                timeout=_TRAJECTORY_FINALIZE_TIMEOUT_SEC,
            )
        )
        trajectory_capture, finalizer_cancel = (
            await self._await_trajectory_finalizer(finalize_task)
        )
        if pending_cancel is None:
            pending_cancel = finalizer_cancel

        value = self._load_driver_result()
        usage = value.get("usage") if value else {}
        usage = usage if isinstance(usage, dict) else {}
        context.n_input_tokens = usage.get("input_tokens")
        context.n_output_tokens = usage.get("output_tokens")
        product_completion = bool(value and value.get("status") == "completed")
        lifecycle_gate_passed = bool(
            outcome and outcome.trigger_hit and not outcome.fault_injected
        )
        metadata = {
            **initial_metadata,
            **trajectory_capture,
            "trigger_hit": bool(outcome and outcome.trigger_hit),
            "trigger_reason": outcome.reason if outcome else "controller_incomplete",
            "trigger_evidence_sha256": (
                outcome.evidence_sha256 if outcome else None
            ),
            "lifecycle_gate_passed": lifecycle_gate_passed,
            "product_return_code": return_code,
            "product_error_type": (
                product_error_type
            ),
            "product_completion_claim": product_completion,
            "product_terminal_status": product_terminal_status,
            "product_cleanup_zero_live_proven": bool(
                cleanup_report and cleanup_report["zero_live_proven"]
            ),
            "product_cleanup_report_sha256": (
                self._last_process_cleanup_sha256
            ),
            "product_final_status": value.get("status") if value else None,
            "product_result_parseable": value is not None,
            "hermes_run_id": value.get("run_id") if value else None,
            "hermes_session_id": session_id,
            "driver_session_id_consistent": bool(
                value and value.get("session_id") == session_id
            ),
            "gateway_pid": value.get("gateway_pid") if value else None,
            "approvals_denied": (
                value.get("approvals_denied", 0) if value else 0
            ),
            "gateway_cleanup": value.get("cleanup") if value else None,
            "policy_guard_active": bool(
                value and value.get("policy_guard_active")
            ),
        }
        if value is not None and not metadata["driver_session_id_consistent"]:
            metadata["trajectory_capture_status"] = "failed"
            metadata["trajectory_capture_error"] = (
                "driver_result_session_id_mismatch"
            )
        self._c0_metadata = dict(metadata)
        context.metadata = metadata
        ledger.append(
            "controller_completed",
            trigger_hit=metadata["trigger_hit"],
            fault_injected=False,
            lifecycle_gate_passed=lifecycle_gate_passed,
            managed_policy_sha256=managed_policy_sha256,
            managed_env_sha256=managed_env_sha256,
            policy_guard_sha256=policy_guard_sha256,
            policy_guard_active=metadata["policy_guard_active"],
            hermes_install_mode=metadata["hermes_install_mode"],
            hermes_prebuilt_marker_verified=metadata[
                "hermes_prebuilt_marker_verified"
            ],
            hermes_prebuilt_marker_sha256=metadata[
                "hermes_prebuilt_marker_sha256"
            ],
            hermes_prebuilt_source_commit=metadata[
                "hermes_prebuilt_source_commit"
            ],
            hermes_prebuilt_installer_sha256=metadata[
                "hermes_prebuilt_installer_sha256"
            ],
            trajectory_capture_status=metadata[
                "trajectory_capture_status"
            ],
            trajectory_capture_blocking=metadata[
                "trajectory_capture_blocking"
            ],
            trajectory_capture_sha256=metadata[
                "trajectory_capture_sha256"
            ],
            trajectory_capture_error=metadata[
                "trajectory_capture_error"
            ],
            trajectory_event_count=metadata["trajectory_event_count"],
            trajectory_submitted_count=metadata[
                "trajectory_submitted_count"
            ],
            trajectory_terminal_event_count=metadata[
                "trajectory_terminal_event_count"
            ],
            trajectory_terminal_event=metadata[
                "trajectory_terminal_event"
            ],
            trajectory_terminal_event_source=metadata[
                "trajectory_terminal_event_source"
            ],
            trajectory_terminal_reason=metadata[
                "trajectory_terminal_reason"
            ],
            trajectory_session_export_status=metadata[
                "trajectory_session_export_status"
            ],
            trajectory_session_sha256=metadata[
                "trajectory_session_sha256"
            ],
            trajectory_session_id=metadata["trajectory_session_id"],
            trajectory_session_message_count=metadata[
                "trajectory_session_message_count"
            ],
            product_completion_claim=product_completion,
            product_return_code=return_code,
            product_terminal_status=product_terminal_status,
            product_cleanup_zero_live_proven=metadata[
                "product_cleanup_zero_live_proven"
            ],
        )
        if pending_cancel is not None:
            raise pending_cancel
        if pending_error is not None:
            raise pending_error

    @staticmethod
    def _failed_trajectory_capture(exc: BaseException) -> dict[str, Any]:
        return {
            "trajectory_capture_path": "agent/hermes-run-events.jsonl",
            "trajectory_capture_format": "hermes_runs_api_jsonl",
            "trajectory_capture_status": "failed",
            "trajectory_capture_sha256": None,
            "trajectory_event_stream_status": "failed",
            "trajectory_event_count": 0,
            "trajectory_submitted_count": 0,
            "trajectory_terminal_event_count": 0,
            "trajectory_terminal_event": None,
            "trajectory_terminal_event_source": None,
            "trajectory_terminal_reason": None,
            "trajectory_session_export_path": "agent/hermes-session.jsonl",
            "trajectory_session_export_format": "hermes_session_jsonl",
            "trajectory_session_export_status": "failed",
            "trajectory_session_sha256": None,
            "trajectory_session_id": None,
            "trajectory_session_message_count": 0,
            "trajectory_capture_error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    async def _await_trajectory_finalizer(
        self,
        finalize_task: asyncio.Task[dict[str, Any]],
    ) -> tuple[dict[str, Any], asyncio.CancelledError | None]:
        pending_cancel: asyncio.CancelledError | None = None
        while True:
            try:
                return await asyncio.shield(finalize_task), pending_cancel
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if (
                    current is not None
                    and current.cancelling()
                    and pending_cancel is None
                ):
                    pending_cancel = exc
                if finalize_task.done() and finalize_task.cancelled():
                    return self._failed_trajectory_capture(exc), pending_cancel
            except Exception as exc:
                return self._failed_trajectory_capture(exc), pending_cancel

    def _streaming_trajectory_capture(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        path = self.logs_dir / "hermes-run-events.jsonl"
        base = {
            "trajectory_capture_path": "agent/hermes-run-events.jsonl",
            "trajectory_capture_format": "hermes_runs_api_jsonl",
        }
        try:
            summary = validate_run_event_stream(
                path,
                run_id=run_id,
                session_id=session_id,
            )
        except (OSError, RuntimeError) as exc:
            return {
                **base,
                "trajectory_event_stream_status": "failed",
                "trajectory_capture_sha256": None,
                "trajectory_event_count": 0,
                "trajectory_submitted_count": 0,
                "trajectory_terminal_event_count": 0,
                "trajectory_terminal_event": None,
                "trajectory_terminal_event_source": None,
                "trajectory_terminal_reason": None,
                "trajectory_event_stream_error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        return {
            **base,
            "trajectory_event_stream_status": "saved",
            "trajectory_capture_sha256": summary["sha256"],
            "trajectory_event_count": summary["event_count"],
            "trajectory_submitted_count": summary["submitted_count"],
            "trajectory_terminal_event_count": summary[
                "terminal_event_count"
            ],
            "trajectory_terminal_event": summary["terminal_event"],
            "trajectory_terminal_event_source": summary[
                "terminal_event_source"
            ],
            "trajectory_terminal_reason": summary["terminal_reason"],
            "trajectory_event_stream_error": None,
        }

    def _session_export_capture(self, *, session_id: str) -> dict[str, Any]:
        path = self.logs_dir / "hermes-session.jsonl"
        base = {
            "trajectory_session_export_path": "agent/hermes-session.jsonl",
            "trajectory_session_export_format": "hermes_session_jsonl",
        }
        try:
            summary = validate_session_export(path, session_id=session_id)
        except (OSError, RuntimeError) as exc:
            return {
                **base,
                "trajectory_session_export_status": "failed",
                "trajectory_session_sha256": None,
                "trajectory_session_id": None,
                "trajectory_session_message_count": 0,
                "trajectory_session_export_error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        return {
            **base,
            "trajectory_session_export_status": "saved",
            "trajectory_session_sha256": summary["sha256"],
            "trajectory_session_id": summary["session_id"],
            "trajectory_session_message_count": summary["message_count"],
            "trajectory_session_export_error": None,
        }

    async def _download_required_if_missing(
        self,
        environment: BaseEnvironment,
        remote_path: str,
        local_path: Path,
    ) -> None:
        if local_path.is_file():
            return
        await asyncio.wait_for(
            environment.download_file(remote_path, local_path),
            timeout=_TRAJECTORY_DOWNLOAD_TIMEOUT_SEC,
        )
        if not local_path.is_file():
            raise RuntimeError(
                f"required Hermes trajectory artifact is missing: {remote_path}"
            )

    async def _capture_required_trajectory(
        self,
        environment: BaseEnvironment,
        *,
        run_id: Any,
        session_id: str,
    ) -> dict[str, Any]:
        errors: list[str] = []
        events_path = self.logs_dir / "hermes-run-events.jsonl"
        try:
            await self._download_required_if_missing(
                environment,
                REMOTE_EVENTS,
                events_path,
            )
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("Hermes driver result has no run_id")
            event_capture = self._streaming_trajectory_capture(
                run_id=run_id,
                session_id=session_id,
            )
            if event_capture["trajectory_event_stream_status"] != "saved":
                errors.append(event_capture["trajectory_event_stream_error"])
        except Exception as exc:
            event_capture = {
                "trajectory_capture_path": "agent/hermes-run-events.jsonl",
                "trajectory_capture_format": "hermes_runs_api_jsonl",
                "trajectory_event_stream_status": "failed",
                "trajectory_capture_sha256": None,
                "trajectory_event_count": 0,
                "trajectory_submitted_count": 0,
                "trajectory_terminal_event_count": 0,
                "trajectory_terminal_event": None,
                "trajectory_terminal_event_source": None,
                "trajectory_terminal_reason": None,
                "trajectory_event_stream_error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }
            errors.append(event_capture["trajectory_event_stream_error"])

        session_path = self.logs_dir / "hermes-session.jsonl"
        session_path.unlink(missing_ok=True)
        try:
            await self._export_session(environment, session_id=session_id)
            await self._download_required_if_missing(
                environment,
                REMOTE_SESSION_EXPORT,
                session_path,
            )
            session_capture = self._session_export_capture(
                session_id=session_id
            )
            if (
                session_capture["trajectory_session_export_status"]
                != "saved"
            ):
                errors.append(
                    session_capture["trajectory_session_export_error"]
                )
        except Exception as exc:
            session_capture = {
                "trajectory_session_export_path": (
                    "agent/hermes-session.jsonl"
                ),
                "trajectory_session_export_format": "hermes_session_jsonl",
                "trajectory_session_export_status": "failed",
                "trajectory_session_sha256": None,
                "trajectory_session_id": None,
                "trajectory_session_message_count": 0,
                "trajectory_session_export_error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }
            errors.append(session_capture["trajectory_session_export_error"])

        return {
            **event_capture,
            **session_capture,
            "trajectory_capture_status": "failed" if errors else "saved",
            "trajectory_capture_error": "; ".join(errors) if errors else None,
        }

    async def _finalize_run_artifacts(
        self,
        environment: BaseEnvironment,
        *,
        paths: dict[str, str],
        run_id: Any,
        session_id: str,
    ) -> dict[str, Any]:
        capture = await self._capture_required_trajectory(
            environment,
            run_id=run_id,
            session_id=session_id,
        )
        try:
            await asyncio.wait_for(
                self._collect_c0_logs(environment, paths),
                timeout=_OPTIONAL_LOG_COLLECTION_TIMEOUT_SEC,
            )
        except Exception:
            pass
        return capture

    def _load_driver_result(self) -> dict[str, Any] | None:
        result_path = self.logs_dir / "hermes-run.json"
        if not result_path.is_file():
            return None
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def _collect_c0_logs(
        self,
        environment: BaseEnvironment,
        paths: dict[str, str],
    ) -> None:
        downloads = (
            (paths["stdout"], self.logs_dir / "hermes-driver.stdout.txt"),
            (paths["stderr"], self.logs_dir / "hermes-driver.stderr.txt"),
            (paths["identity"], self.logs_dir / "product.identity.json"),
            (paths["cleanup"], self.logs_dir / "product.cleanup.json"),
            (REMOTE_RESULT, self.logs_dir / "hermes-run.json"),
            (REMOTE_GATEWAY_LOG, self.logs_dir / "hermes-gateway.txt"),
        )
        for remote_path, local_path in downloads:
            if local_path.is_file():
                continue
            try:
                await asyncio.wait_for(
                    environment.download_file(remote_path, local_path),
                    timeout=10,
                )
            except Exception:
                pass

    async def _export_session(
        self,
        environment: BaseEnvironment,
        *,
        session_id: str,
    ) -> None:
        result = await self.exec_as_agent(
            environment,
            command=(
                f"rm -f {shlex.quote(REMOTE_SESSION_EXPORT)} && "
                'export PATH="$HOME/.local/bin:$PATH" && '
                "hermes sessions export "
                f"{shlex.quote(REMOTE_SESSION_EXPORT)} "
                f"--session-id {shlex.quote(session_id)}"
            ),
            env=self._guarded_product_env(),
            timeout_sec=15,
        )
        if result.return_code != 0:
            raise RuntimeError(
                "Hermes current-session export command failed "
                f"(return code {result.return_code})"
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        trajectory_path = self.logs_dir / "trajectory.json"
        if trajectory_path.is_file() and self._c0_metadata.get(
            "hermes_session_id"
        ):
            try:
                trajectory = json.loads(
                    trajectory_path.read_text(encoding="utf-8")
                )
                if isinstance(trajectory, dict):
                    trajectory["session_id"] = self._c0_metadata[
                        "hermes_session_id"
                    ]
                    trajectory_path.write_text(
                        json.dumps(trajectory, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                pass
        metadata = dict(context.metadata or {})
        metadata.update(self._c0_metadata)
        metadata["trajectory_session_exported"] = (
            metadata.get("trajectory_session_export_status") == "saved"
        )
        metadata["trajectory_atif_converted"] = trajectory_path.is_file()
        context.metadata = metadata
