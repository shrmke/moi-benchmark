from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.utils.env import parse_bool_env_value

from astra.runners.astra_smoke.core import (
    astra_args,
    normalize_linux_arch,
    parse_astra_json,
    validate_linux_elf,
    write_minimal_credentials,
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
from astra.runners.llm_observability import (
    apply_token_usage,
    observability_metadata,
    write_canonical_session,
)
from astra.runners.astra_terminal_bench.trajectory_export import (
    validate_trajectory_bundle,
)


REMOTE_BINARY = "/installed-agent/astra"
REMOTE_ROOT = "/tmp/astra-terminal-bench"
REMOTE_PROMPT = f"{REMOTE_ROOT}/instruction.md"
REMOTE_PROCESS_PROBE = "/installed-agent/lifecycle-process-probe.py"
REMOTE_PREDICATE_PROBE = "/installed-agent/lifecycle-predicate-probe.py"
REMOTE_TRAJECTORY_EXPORTER = "/installed-agent/astra-trajectory-export.py"
REMOTE_STREAM_TRANSPORT_RETRY = (
    "/installed-agent/astra-stream-transport-retry.py"
)
C0_TASK_TIMEOUT_SEC = {
    "modernize-scientific-stack": 1200,
    "overfull-hbox": 1500,
    "build-pmars": 1800,
    "db-wal-recovery": 1800,
}
C0_CLEANUP_GRACE_SEC = 2.0
C0_HOST_CLEANUP_MARGIN_SEC = 10
C0_PRODUCT_TIMEOUT_MULTIPLIER = 2.25
LLM_FALLBACK_TIMEOUT_SEC = 600
LLM_TOTAL_BUDGET_SEC = 27000
STREAM_OPTIONAL_RETRY_MIN_REMAINING_SEC = 930
IDENTITY_REGISTRATION_MAX_ATTEMPTS = 3
IDENTITY_WHOAMI_MAX_ATTEMPTS = 3
IDENTITY_RETRY_DELAY_SEC = 1.0
GENERIC_C0_PREDICATE_ID = "terminal-bench.generic.product-live"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_diagnostic(value: Optional[str]) -> dict[str, object]:
    encoded = (value or "").encode("utf-8", errors="replace")
    return {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _finalize_astra_observability(
    logs_dir: Path,
    session_id: str,
) -> dict[str, object]:
    native_sources: list[tuple[str, Path]] = []
    try:
        trajectory_root = logs_dir / "astra-trajectory"
        manifest = json.loads(
            (trajectory_root / "manifest.json").read_text(encoding="utf-8")
        )
        relative = manifest.get("local_journal_path")
        if isinstance(relative, str) and relative:
            native_sources.append(("astra_session_journal", trajectory_root / relative))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return write_canonical_session(
        output_path=logs_dir / "session.jsonl",
        agent="astra",
        session_id=session_id,
        native_sources=native_sources,
    )


def _load_trial_task(
    logs_dir: Path,
    instruction_sha256: str,
) -> tuple[str, float]:
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
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise LifecycleConfigurationError(
            f"could not resolve Terminal-Bench trial metadata from {trial_config_path}"
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


def _load_stream_transport_retry_report(
    path: Path,
    *,
    session_id: str,
    max_retries: int,
    overall_deadline_sec: float,
    optional_retry_min_remaining_sec: float,
) -> dict[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("invalid stream transport retry report")
    retry_count = report.get("retry_count")
    attempt_count = report.get("attempt_count")
    attempts = report.get("attempts")
    complete = report.get("complete")
    final_return_code = report.get("final_return_code")
    if (
        report.get("schema_version") != 1
        or report.get("session_id") != session_id
        or report.get("max_retries") != max_retries
        or report.get("overall_deadline_seconds") != overall_deadline_sec
        or report.get("optional_retry_min_remaining_seconds")
        != optional_retry_min_remaining_sec
        or type(complete) is not bool
        or type(retry_count) is not int
        or retry_count < 0
        or retry_count > max_retries
        or type(attempt_count) is not int
        or attempt_count < 1
        or attempt_count != retry_count + 1
        or not isinstance(attempts, list)
        or len(attempts) != attempt_count
        or type(report.get("recovered")) is not bool
        or type(report.get("exhausted")) is not bool
        or (
            complete
            and type(final_return_code) is not int
        )
        or (
            not complete
            and final_return_code is not None
            and type(final_return_code) is not int
        )
    ):
        raise ValueError("invalid stream transport retry report")
    return report


class AstraTerminalBenchAgent(BaseInstalledAgent):
    """Run Astra as an S0 agent on an unmodified Harbor task."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: Optional[str] = None,
        linux_binary_path: Optional[str] = None,
        max_turns: Optional[int] = None,
        turn_timeout_sec: int = 900,
        read_memory: Optional[bool] = None,
        *args,
        **kwargs,
    ):
        super().__init__(logs_dir=logs_dir, model_name=model_name, *args, **kwargs)
        self._access_token = self._get_env("ASTRA_ACCESS_TOKEN")
        # Never let Harbor's scoped environment inject the host token into
        # arbitrary task commands. The token is written only to the isolated
        # Astra credential file.
        self._extra_env.pop("ASTRA_ACCESS_TOKEN", None)
        self.linux_binary_path = linux_binary_path
        self.max_turns = int(max_turns) if max_turns is not None else None
        self.turn_timeout_sec = int(turn_timeout_sec)
        self.astra_model_name = model_name or self._get_env("ASTRA_TBENCH_MODEL")
        self._freeze_manifest_sha256 = self._get_env(
            "ASTRA_TBENCH_FREEZE_MANIFEST_SHA256"
        )
        if (
            self._freeze_manifest_sha256 is not None
            and (
                len(self._freeze_manifest_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self._freeze_manifest_sha256
                )
            )
        ):
            raise ValueError(
                "ASTRA_TBENCH_FREEZE_MANIFEST_SHA256 must be lowercase SHA-256"
            )
        self._formal_score_eligible = self._freeze_manifest_sha256 is not None
        self._evaluation_status = (
            "formal_frozen_inputs"
            if self._formal_score_eligible
            else "exploratory_unfrozen"
        )
        read_memory_value = self._get_env("ASTRA_TBENCH_READ_MEMORY")
        self.read_memory = parse_bool_env_value(
            read_memory_value if read_memory_value is not None else read_memory,
            name="ASTRA_TBENCH_READ_MEMORY",
            default=False,
        )
        self._memory_user_id: Optional[str] = None
        self._artifact_arch: Optional[str] = None
        self._artifact_sha256: Optional[str] = None
        self._task_workdir: Optional[str] = None
        if self.max_turns is not None and self.max_turns <= 0:
            raise ValueError("max_turns must be positive when configured")
        if self.turn_timeout_sec <= 0:
            raise ValueError("turn_timeout_sec must be positive")

    @staticmethod
    def name() -> str:
        return "astra-terminal-bench"

    def get_version_command(self) -> str:
        return f"{REMOTE_BINARY} --version"

    def _runtime_env(self) -> dict[str, str]:
        runtime_env = {
            "HOME": f"{REMOTE_ROOT}/home",
            "ASTRA_CLI_CREDENTIALS_DIR": f"{REMOTE_ROOT}/credentials",
            "ASTRA_LLM_FALLBACK_TIMEOUT_S": str(LLM_FALLBACK_TIMEOUT_SEC),
            "ASTRA_LLM_TOTAL_BUDGET_S": str(LLM_TOTAL_BUDGET_SEC),
        }
        if self.max_turns is not None:
            runtime_env["ASTRA_MAX_TURNS"] = str(self.max_turns)
        return runtime_env

    async def _discover_task_workdir(
        self,
        environment: BaseEnvironment,
    ) -> str:
        discovered = await environment.exec(
            command="pwd -P",
            timeout_sec=10,
        )
        task_workdir = (discovered.stdout or "").strip()
        if (
            discovered.return_code != 0
            or not task_workdir.startswith("/")
            or "\n" in task_workdir
            or "\x00" in task_workdir
        ):
            raise RuntimeError(
                "could not determine a safe task container working directory"
            )
        verified = await environment.exec(
            command=f"test -d {shlex.quote(task_workdir)}",
            timeout_sec=10,
        )
        if verified.return_code != 0:
            raise RuntimeError("the task container working directory does not exist")
        self._task_workdir = task_workdir
        _write_json(
            self.logs_dir / "task-workdir.json",
            {
                "schema_version": 1,
                "status": "verified",
                "task_workdir": task_workdir,
            },
        )
        return task_workdir

    async def _register_isolated_memory_identity(
        self,
        environment: BaseEnvironment,
    ) -> None:
        report_path = self.logs_dir / "identity-registration.json"
        report: dict[str, object] = {
            "schema_version": 1,
            "status": "in_progress",
            "started_at": _utc_timestamp(),
            "register_max_attempts": IDENTITY_REGISTRATION_MAX_ATTEMPTS,
            "whoami_max_attempts": IDENTITY_WHOAMI_MAX_ATTEMPTS,
            "attempts": [],
        }
        attempts = report["attempts"]
        assert isinstance(attempts, list)
        _write_json(report_path, report)

        username: Optional[str] = None
        for attempt in range(1, IDENTITY_REGISTRATION_MAX_ATTEMPTS + 1):
            username = f"tbench-{uuid.uuid4().hex}"
            password = f"Astra-{uuid.uuid4().hex}"
            started = time.monotonic()
            started_at = _utc_timestamp()
            try:
                registered = await environment.exec(
                    command=shlex.join(
                        [
                            REMOTE_BINARY,
                            "register",
                            "--username",
                            username,
                            "--email",
                            f"{username}@example.com",
                            "--password",
                            password,
                        ]
                    ),
                    env=self._runtime_env(),
                    timeout_sec=60,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "stage": "register",
                        "attempt": attempt,
                        "started_at": started_at,
                        "duration_sec": round(time.monotonic() - started, 6),
                        "return_code": None,
                        "error_type": type(exc).__name__,
                    }
                )
                register_succeeded = False
            else:
                attempts.append(
                    {
                        "stage": "register",
                        "attempt": attempt,
                        "started_at": started_at,
                        "duration_sec": round(time.monotonic() - started, 6),
                        "return_code": registered.return_code,
                        "stdout": _text_diagnostic(registered.stdout),
                        "stderr": _text_diagnostic(registered.stderr),
                    }
                )
                register_succeeded = registered.return_code == 0
            _write_json(report_path, report)
            if register_succeeded:
                break
            if attempt < IDENTITY_REGISTRATION_MAX_ATTEMPTS:
                await asyncio.sleep(IDENTITY_RETRY_DELAY_SEC * attempt)
        else:
            report.update(
                {
                    "status": "failed",
                    "failure_stage": "register",
                    "completed_at": _utc_timestamp(),
                }
            )
            _write_json(report_path, report)
            raise RuntimeError(
                "could not register the isolated Astra experiment identity"
            )

        assert username is not None
        for attempt in range(1, IDENTITY_WHOAMI_MAX_ATTEMPTS + 1):
            started = time.monotonic()
            started_at = _utc_timestamp()
            identity = None
            try:
                whoami = await environment.exec(
                    command=f"{REMOTE_BINARY} whoami",
                    env=self._runtime_env(),
                    timeout_sec=30,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "stage": "whoami",
                        "attempt": attempt,
                        "started_at": started_at,
                        "duration_sec": round(time.monotonic() - started, 6),
                        "return_code": None,
                        "error_type": type(exc).__name__,
                    }
                )
                valid_identity = False
                username_mismatch = False
            else:
                try:
                    identity = json.loads(whoami.stdout or "")
                except (json.JSONDecodeError, TypeError):
                    identity = None
                user_id = identity.get("user_id") if isinstance(identity, dict) else None
                actual_username = (
                    identity.get("username") if isinstance(identity, dict) else None
                )
                username_mismatch = (
                    whoami.return_code == 0
                    and isinstance(actual_username, str)
                    and actual_username != username
                )
                valid_identity = bool(
                    whoami.return_code == 0
                    and isinstance(user_id, str)
                    and user_id
                    and actual_username == username
                )
                attempts.append(
                    {
                        "stage": "whoami",
                        "attempt": attempt,
                        "started_at": started_at,
                        "duration_sec": round(time.monotonic() - started, 6),
                        "return_code": whoami.return_code,
                        "stdout": _text_diagnostic(whoami.stdout),
                        "stderr": _text_diagnostic(whoami.stderr),
                        "parsed": isinstance(identity, dict),
                        "username_match": (
                            actual_username == username
                            if isinstance(actual_username, str)
                            else None
                        ),
                        "user_id_present": bool(
                            isinstance(user_id, str) and user_id
                        ),
                    }
                )
            _write_json(report_path, report)
            if valid_identity:
                assert isinstance(identity, dict)
                user_id = identity["user_id"]
                assert isinstance(user_id, str)
                self._memory_user_id = user_id
                report.update(
                    {
                        "status": "complete",
                        "username": username,
                        "whoami_username_match": True,
                        "memory_user_id": user_id,
                        "completed_at": _utc_timestamp(),
                    }
                )
                _write_json(report_path, report)
                return
            if username_mismatch:
                break
            if attempt < IDENTITY_WHOAMI_MAX_ATTEMPTS:
                await asyncio.sleep(IDENTITY_RETRY_DELAY_SEC * attempt)

        report.update(
            {
                "status": "failed",
                "failure_stage": (
                    "whoami_username_mismatch"
                    if username_mismatch
                    else "whoami"
                ),
                "completed_at": _utc_timestamp(),
            }
        )
        _write_json(report_path, report)
        raise RuntimeError(
            "could not verify the isolated Astra experiment identity"
        )

    async def install(self, environment: BaseEnvironment) -> None:
        source_value = self.linux_binary_path or self._get_env(
            "ASTRA_TBENCH_LINUX_BINARY"
        )
        if not source_value:
            raise ValueError(
                "set linux_binary_path or ASTRA_TBENCH_LINUX_BINARY to a Linux Astra ELF"
            )
        source = Path(source_value).expanduser().resolve()
        artifact_arch = validate_linux_elf(source)

        api_url = self._get_env("ASTRA_API_URL")
        parsed_api_url = urlparse(api_url or "")
        if (
            parsed_api_url.scheme not in {"http", "https"}
            or parsed_api_url.hostname != "host.docker.internal"
        ):
            raise ValueError(
                "local Docker runs require ASTRA_API_URL with host "
                "host.docker.internal (for example http://host.docker.internal:17001)"
            )
        if self.read_memory and not self._access_token:
            raise ValueError("ASTRA_ACCESS_TOKEN must be supplied through Harbor agent env")
        if not self.astra_model_name:
            raise ValueError("set model_name or ASTRA_TBENCH_MODEL to an Astra model ID")

        await self._discover_task_workdir(environment)

        arch_result = await environment.exec(command="uname -m", timeout_sec=10)
        if arch_result.return_code != 0:
            raise RuntimeError("could not determine the task container architecture")
        container_arch = normalize_linux_arch(arch_result.stdout or "")
        if artifact_arch != container_arch:
            raise ValueError(
                f"Astra artifact architecture {artifact_arch} does not match "
                f"task container architecture {container_arch}"
            )

        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        self._artifact_arch = artifact_arch
        self._artifact_sha256 = digest.hexdigest()

        await environment.upload_file(source, REMOTE_BINARY)
        await self.exec_as_root(environment, f"chmod 0555 {shlex.quote(REMOTE_BINARY)}")

        prepare = (
            f"mkdir -p {shlex.quote(REMOTE_ROOT + '/home')} "
            f"{shlex.quote(REMOTE_ROOT + '/credentials')} && "
            f"chmod 0700 {shlex.quote(REMOTE_ROOT)} "
            f"{shlex.quote(REMOTE_ROOT + '/home')} "
            f"{shlex.quote(REMOTE_ROOT + '/credentials')}"
        )
        prepared = await environment.exec(command=prepare, timeout_sec=10)
        if prepared.return_code != 0:
            raise RuntimeError("could not prepare isolated Astra runtime directories")

        remote_credentials = f"{REMOTE_ROOT}/credentials/credentials.json"
        if self.read_memory:
            with tempfile.TemporaryDirectory(
                prefix="astra-tbench-credentials-"
            ) as directory:
                local_credentials = Path(directory) / "credentials.json"
                write_minimal_credentials(
                    local_credentials,
                    self._access_token or "",
                )
                await environment.upload_file(
                    local_credentials,
                    remote_credentials,
                )
        else:
            await self._register_isolated_memory_identity(environment)
        protected = await environment.exec(
            command=f"chmod 0600 {shlex.quote(remote_credentials)}",
            timeout_sec=10,
        )
        if protected.return_code != 0:
            raise RuntimeError("could not protect the isolated Astra credential profile")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task_workdir = self._task_workdir or await self._discover_task_workdir(
            environment
        )
        prompt_path = self.logs_dir / "instruction.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(instruction, encoding="utf-8")
        await environment.upload_file(prompt_path, REMOTE_PROMPT)

        health = await environment.exec(
            command=f"{REMOTE_BINARY} health",
            env=self._runtime_env(),
            timeout_sec=30,
        )
        if health.return_code != 0:
            raise RuntimeError("Astra API is not reachable from the task container")

        whoami = await environment.exec(
            command=f"{REMOTE_BINARY} whoami",
            env=self._runtime_env(),
            timeout_sec=30,
        )
        if whoami.return_code != 0:
            raise RuntimeError("Astra credentials are not accepted in the task container")

        command = (
            shlex.join(
                astra_args(
                    remote_binary=REMOTE_BINARY,
                    model_name=self.astra_model_name,
                    max_turns=None,
                    session_id=None,
                    permission_mode="bypass",
                    read_memory=self.read_memory,
                )
            )
            + f" < {shlex.quote(REMOTE_PROMPT)}"
        )
        result = await environment.exec(
            command=command,
            cwd=task_workdir,
            env=self._runtime_env(),
            timeout_sec=self.turn_timeout_sec,
        )

        stdout_path = self.logs_dir / "astra.stdout.json"
        stderr_path = self.logs_dir / "astra.stderr.txt"
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
        if result.return_code != 0:
            raise RuntimeError(
                f"Astra task process exited {result.return_code}; "
                "inspect astra.stderr.txt"
            )

        value = parse_astra_json(result.stdout or "")
        if value.get("success") is not True:
            raise RuntimeError("Astra task turn did not finish successfully")

        context.n_input_tokens = value.get("prompt_tokens")
        context.n_cache_tokens = value.get("cache", {}).get("read_tokens")
        context.n_output_tokens = value.get("completion_tokens")
        context.metadata = {
            "condition": "S0",
            "astra_session_id": value["session_id"],
            "astra_run_id": value.get("run_id"),
            "tool_calls_count": value.get("tool_calls_count"),
            "tools_used": value.get("tools_used", []),
            "artifact_arch": self._artifact_arch,
            "artifact_sha256": self._artifact_sha256,
            "task_workdir": task_workdir,
            "llm_fallback_timeout_sec": LLM_FALLBACK_TIMEOUT_SEC,
            "llm_total_budget_sec": LLM_TOTAL_BUDGET_SEC,
            "frozen_inputs_manifest_sha256": self._freeze_manifest_sha256,
            "fault_injected": False,
            "memory_read_enabled": self.read_memory,
            "memory_context_mode": (
                "existing_user" if self.read_memory else "fresh_user_isolation"
            ),
            "memory_user_id": self._memory_user_id,
        }


class AstraTerminalBenchC0Agent(AstraTerminalBenchAgent):
    """Run Astra behind the external lifecycle-clean C0 controller."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: Optional[str] = None,
        trigger_timeout_sec: float = 1800,
        poll_interval_sec: float = 0.5,
        stream_transport_retries: int = 2,
        stream_optional_retry_min_remaining_sec: float = (
            STREAM_OPTIONAL_RETRY_MIN_REMAINING_SEC
        ),
        product_timeout_multiplier: float = C0_PRODUCT_TIMEOUT_MULTIPLIER,
        *args,
        **kwargs,
    ):
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            *args,
            **kwargs,
        )
        self.trigger_timeout_sec = float(trigger_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.stream_transport_retries = int(stream_transport_retries)
        self.stream_optional_retry_min_remaining_sec = float(
            stream_optional_retry_min_remaining_sec
        )
        self.product_timeout_multiplier = float(product_timeout_multiplier)
        temperature_value = self._get_env("ASTRA_TBENCH_TEMPERATURE")
        self.requested_temperature = (
            float(temperature_value) if temperature_value is not None else None
        )
        if self.trigger_timeout_sec <= 0 or self.poll_interval_sec <= 0:
            raise ValueError("C0 controller timeouts must be positive")
        if self.stream_transport_retries < 0:
            raise ValueError("stream_transport_retries must be non-negative")
        if self.stream_optional_retry_min_remaining_sec < 0:
            raise ValueError(
                "stream_optional_retry_min_remaining_sec must be non-negative"
            )
        if self.product_timeout_multiplier <= 0:
            raise ValueError("product_timeout_multiplier must be positive")

    @staticmethod
    def name() -> str:
        return "astra-terminal-bench-c0"

    def _arm_harbor_secret_scrub(self) -> None:
        """Register the token only after Harbor snapshots the run environment."""
        if self._access_token:
            self._extra_env["ASTRA_ACCESS_TOKEN"] = self._access_token

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        python_result = await environment.exec(
            command="command -v python3",
            timeout_sec=10,
        )
        if python_result.return_code != 0:
            await self.exec_as_root(
                environment,
                (
                    "if command -v apt-get >/dev/null 2>&1; then "
                    "DEBIAN_FRONTEND=noninteractive apt-get update && "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y python3; "
                    "elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3; "
                    "elif command -v dnf >/dev/null 2>&1; then dnf install -y python3; "
                    "elif command -v yum >/dev/null 2>&1; then yum install -y python3; "
                    "else echo 'no supported package manager for python3' >&2; exit 127; fi"
                ),
                timeout_sec=300,
            )
        python_validation = await environment.exec(
            command=(
                "python3 --version && "
                "python3 -c 'import ctypes, json, os, pathlib, signal, subprocess'"
            ),
            timeout_sec=30,
        )
        if python_validation.return_code != 0:
            raise RuntimeError(
                "python3 is unavailable or incomplete in the task container"
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
            Path(__file__).with_name("trajectory_export.py"),
            REMOTE_TRAJECTORY_EXPORTER,
        )
        await environment.upload_file(
            Path(__file__).with_name("stream_transport_retry.py"),
            REMOTE_STREAM_TRANSPORT_RETRY,
        )
        await self.exec_as_root(
            environment,
            (
                f"chmod 0555 {shlex.quote(REMOTE_PROCESS_PROBE)} "
                f"{shlex.quote(REMOTE_PREDICATE_PROBE)} "
                f"{shlex.quote(REMOTE_TRAJECTORY_EXPORTER)} "
                f"{shlex.quote(REMOTE_STREAM_TRANSPORT_RETRY)}"
            ),
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Harbor 0.20 snapshots scoped_exec_env before entering run(), then
        # reads extra_env again for its final jobs-dir scrub. Delayed
        # registration therefore enables redaction without exposing the token
        # to this single-step C0 run's container commands.
        self._arm_harbor_secret_scrub()
        task_workdir = self._task_workdir or await self._discover_task_workdir(
            environment
        )
        run_id = str(uuid.uuid4())
        astra_session_id: Optional[str] = None
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
            if task_id in C0_TASK_TIMEOUT_SEC:
                raise LifecycleConfigurationError(
                    f"registered C0 instruction hash changed for {task_id!r}"
                )
            trigger = ExternalTriggerManifest(
                task_id=task_id,
                predicate_id=GENERIC_C0_PREDICATE_ID,
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
                        "the registered C0 trigger does not match the Harbor task"
                    )
                configured_product_timeout_sec = (
                    base_timeout_sec * self.product_timeout_multiplier
                )
            else:
                configured_product_timeout_sec = C0_TASK_TIMEOUT_SEC[task_id]
        product_timeout_sec = min(
            self.turn_timeout_sec,
            configured_product_timeout_sec,
        )
        thinking_effort = next(
            (
                effort
                for effort in ("low", "medium", "high", "max")
                if self.astra_model_name.endswith(f"(thinking:{effort})")
            ),
            None,
        )
        remote_run_root = f"{REMOTE_ROOT}/c0-{run_id}"
        paths = {
            "identity": f"{remote_run_root}/product.identity.json",
            "cleanup": f"{remote_run_root}/product.cleanup.json",
            "stdout": f"{remote_run_root}/product.stdout",
            "stderr": f"{remote_run_root}/product.stderr",
            "stdin": f"{remote_run_root}/instruction.md",
            "retry": f"{remote_run_root}/stream-transport-retry.json",
            "trajectory": f"{remote_run_root}/astra-trajectory",
        }
        initial_metadata = {
            "condition": "C0",
            "evaluation_status": self._evaluation_status,
            "formal_score_eligible": self._formal_score_eligible,
            "fault_injected": False,
            "fault_action": "noop",
            "memory_read_enabled": self.read_memory,
            "memory_context_mode": (
                "existing_user" if self.read_memory else "fresh_user_isolation"
            ),
            "memory_user_id": self._memory_user_id,
            "approval_policy": "astra_auto",
            "task_id": task_id,
            "instruction_sha256": instruction_sha256,
            "trigger_registration_status": trigger_registration_status,
            "trigger_scope": trigger_scope,
            "trigger_id": trigger.predicate_id,
            "trigger_manifest_sha256": trigger.sha256,
            "predicate_probe_sha256": lifecycle_predicate_probe_source_sha256(),
            "controller_ledger": str(ledger_path),
            "astra_session_id": astra_session_id,
            "astra_trajectory_status": "pending_registration",
            "trajectory_capture_required": True,
            "trajectory_capture_mode": "astra_server_and_local_session",
            "trajectory_capture_blocking": False,
            "configured_product_timeout_sec": configured_product_timeout_sec,
            "product_timeout_multiplier": self.product_timeout_multiplier,
            "product_timeout_sec": product_timeout_sec,
            "model_selector": self.astra_model_name,
            "thinking_effort": thinking_effort,
            "temperature_requested": self.requested_temperature,
            "temperature_effective": (
                None if thinking_effort else self.requested_temperature
            ),
            "temperature_policy": (
                "suppressed_by_thinking_protocol"
                if thinking_effort and self.requested_temperature is not None
                else "requested"
            ),
            "outer_cleanup_timeout_sec": (
                product_timeout_sec + C0_HOST_CLEANUP_MARGIN_SEC
            ),
            "task_workdir": task_workdir,
            "llm_fallback_timeout_sec": LLM_FALLBACK_TIMEOUT_SEC,
            "llm_total_budget_sec": LLM_TOTAL_BUDGET_SEC,
            "stream_transport_retry_limit": self.stream_transport_retries,
            "stream_optional_retry_min_remaining_sec": (
                self.stream_optional_retry_min_remaining_sec
            ),
            "frozen_inputs_manifest_sha256": self._freeze_manifest_sha256,
        }
        context.metadata = dict(initial_metadata)
        self._write_session_record(
            controller_run_id=run_id,
            session_id=astra_session_id,
            product_terminal_status="not_started",
            capture_status="pending_registration",
        )
        ledger.append(
            "controller_started",
            product="astra",
            product_version=self.version(),
            model_name=self.astra_model_name,
            max_turns=self.max_turns,
            turn_timeout_sec=product_timeout_sec,
            artifact_arch=self._artifact_arch,
            artifact_sha256=self._artifact_sha256,
            **initial_metadata,
        )
        prompt_path = self.logs_dir / "instruction.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(instruction, encoding="utf-8")
        prepared = await environment.exec(
            command=f"mkdir -p {shlex.quote(remote_run_root)}",
            timeout_sec=10,
        )
        if prepared.return_code != 0:
            context.metadata["astra_trajectory_status"] = "failed"
            self._write_session_record(
                controller_run_id=run_id,
                session_id=astra_session_id,
                product_terminal_status="not_started",
                capture_status="failed",
                error_type="ControlDirectoryPreparationFailed",
                failed=True,
            )
            raise RuntimeError("could not prepare the Astra C0 control directory")
        await environment.upload_file(prompt_path, paths["stdin"])

        health = await environment.exec(
            command=f"{REMOTE_BINARY} health",
            env=self._runtime_env(),
            timeout_sec=30,
        )
        ledger.append(
            "product_preflight",
            check="health",
            passed=health.return_code == 0,
            return_code=health.return_code,
        )
        if health.return_code != 0:
            context.metadata["astra_trajectory_status"] = "failed"
            self._write_session_record(
                controller_run_id=run_id,
                session_id=astra_session_id,
                product_terminal_status="not_started",
                capture_status="failed",
                error_type="HealthPreflightFailed",
                failed=True,
            )
            raise RuntimeError("Astra API is not reachable from the task container")

        whoami = await environment.exec(
            command=f"{REMOTE_BINARY} whoami",
            env=self._runtime_env(),
            timeout_sec=30,
        )
        ledger.append(
            "product_preflight",
            check="authentication",
            passed=whoami.return_code == 0,
            return_code=whoami.return_code,
        )
        if whoami.return_code != 0:
            context.metadata["astra_trajectory_status"] = "failed"
            self._write_session_record(
                controller_run_id=run_id,
                session_id=astra_session_id,
                product_terminal_status="not_started",
                capture_status="failed",
                error_type="AuthenticationPreflightFailed",
                failed=True,
            )
            raise RuntimeError("Astra credentials are not accepted in the task container")

        # New Astra CLIs treat --session-id strictly as a resume request. A
        # metadata-only POST /sessions row has no canonical state and cannot be
        # resumed, so let the first one-shot turn create its own Server session.
        # The emitted JSON supplies the authoritative session id for trajectory
        # export and any transport retry.
        initial_metadata["astra_trajectory_status"] = "pending_cli_session"
        context.metadata["astra_trajectory_status"] = "pending_cli_session"
        ledger.append("astra_session_creation_delegated_to_cli")

        astra_argv = astra_args(
            remote_binary=REMOTE_BINARY,
            model_name=self.astra_model_name,
            max_turns=None,
            session_id=astra_session_id,
            permission_mode="auto",
            read_memory=self.read_memory,
        )
        child_argv = [
            "python3",
            REMOTE_STREAM_TRANSPORT_RETRY,
            "--max-retries",
            str(self.stream_transport_retries),
            "--overall-deadline-seconds",
            str(product_timeout_sec),
            "--optional-retry-min-remaining-seconds",
            str(self.stream_optional_retry_min_remaining_sec),
            "--report",
            paths["retry"],
            "--",
            *astra_argv,
        ]
        product_command = process_probe_run_command(
            probe_path=REMOTE_PROCESS_PROBE,
            identity_path=paths["identity"],
            stdout_path=paths["stdout"],
            stderr_path=paths["stderr"],
            stdin_path=paths["stdin"],
            cwd=task_workdir,
            child_argv=child_argv,
            deadline_sec=product_timeout_sec,
            cleanup_report_path=paths["cleanup"],
            cleanup_grace_sec=C0_CLEANUP_GRACE_SEC,
            strict_cleanup=True,
        )
        product_done = asyncio.Event()
        product_error: Optional[Exception] = None
        product_result = None
        outcome = None
        cleanup_report = None
        cleanup_report_sha256 = None
        cleanup_error: Optional[Exception] = None
        trace_capture = None
        controller = C0Controller(
            C0ControllerConfig(
                identity_path=paths["identity"],
                predicate_probe_path=REMOTE_PREDICATE_PROBE,
                trigger=trigger,
                trigger_timeout_sec=min(
                    self.trigger_timeout_sec,
                    product_timeout_sec,
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
            trigger_registration_status=trigger_registration_status,
            trigger_scope=trigger_scope,
        )
        ledger.append("product_turn_started")

        async def execute_product():
            nonlocal product_error, cleanup_report, cleanup_report_sha256
            nonlocal cleanup_error, trace_capture, astra_session_id
            request_cleanup = False
            product_cancelled = False
            try:
                result = await environment.exec(
                    command=product_command,
                    cwd=task_workdir,
                    env=self._runtime_env(),
                    timeout_sec=(
                        product_timeout_sec + C0_HOST_CLEANUP_MARGIN_SEC
                    ),
                )
                (self.logs_dir / "adapter-exec.stdout.txt").write_text(
                    result.stdout or "",
                    encoding="utf-8",
                )
                (self.logs_dir / "adapter-exec.stderr.txt").write_text(
                    result.stderr or "",
                    encoding="utf-8",
                )
                try:
                    await environment.download_file(
                        paths["stdout"],
                        self.logs_dir / "astra.stdout.json",
                    )
                    emitted = parse_astra_json(
                        (self.logs_dir / "astra.stdout.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    emitted_session_id = emitted.get("session_id")
                    if not isinstance(emitted_session_id, str):
                        raise ValueError("Astra output contains no session_id")
                    uuid.UUID(emitted_session_id)
                    astra_session_id = emitted_session_id
                    initial_metadata["astra_session_id"] = astra_session_id
                    context.metadata["astra_session_id"] = astra_session_id
                    ledger.append(
                        "astra_session_created_by_cli",
                        astra_session_id=astra_session_id,
                    )
                except (OSError, ValueError, RuntimeError):
                    pass
                return result
            except asyncio.CancelledError:
                request_cleanup = True
                product_cancelled = True
                raise
            except Exception as exc:
                product_error = exc
                request_cleanup = True
                _write_json(
                    self.logs_dir / "adapter-exec-error.json",
                    {
                        "schema_version": 1,
                        "error_type": type(exc).__name__,
                        "task_workdir": task_workdir,
                    },
                )
                return None
            finally:
                try:
                    try:
                        cleanup_report, cleanup_report_sha256 = (
                            await collect_process_cleanup_report(
                                environment,
                                probe_path=REMOTE_PROCESS_PROBE,
                                identity_path=paths["identity"],
                                cleanup_report_path=paths["cleanup"],
                                request_cleanup=request_cleanup,
                            )
                        )
                    except Exception as exc:
                        cleanup_error = exc
                        product_error = product_error or exc
                        ledger.append(
                            "product_process_cleanup_unavailable",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                    else:
                        setattr(
                            product_done,
                            "_c0_product_terminal_status",
                            cleanup_report["product_terminal_status"],
                        )
                        ledger.append(
                            "product_process_cleanup",
                            reason=cleanup_report["reason"],
                            product_terminal_status=cleanup_report[
                                "product_terminal_status"
                            ],
                            zero_live_proven=True,
                            remaining_pids_count=0,
                            cleanup_report_sha256=cleanup_report_sha256,
                            fault_action=False,
                        )
                finally:
                    product_done.set()
                    product_terminal_status = (
                        cleanup_report["product_terminal_status"]
                        if cleanup_report
                        else ("cancelled" if product_cancelled else "adapter_infra_error")
                    )
                    if astra_session_id is None:
                        discovery = await environment.exec(
                            command=shlex.join(
                                [
                                    "python3",
                                    REMOTE_TRAJECTORY_EXPORTER,
                                    "discover",
                                    "--sessions-root",
                                    f"{REMOTE_ROOT}/home/.astra/sessions",
                                ]
                            ),
                            env=self._runtime_env(),
                            timeout_sec=10,
                        )
                        try:
                            discovered = json.loads(discovery.stdout or "")
                            discovered_session_id = discovered.get("session_id")
                            if not isinstance(discovered_session_id, str):
                                raise ValueError("session discovery returned no id")
                            uuid.UUID(discovered_session_id)
                            if discovery.return_code != 0:
                                raise ValueError("session discovery failed")
                            astra_session_id = discovered_session_id
                            initial_metadata["astra_session_id"] = astra_session_id
                            context.metadata["astra_session_id"] = astra_session_id
                            ledger.append(
                                "astra_session_discovered_from_isolated_home",
                                astra_session_id=astra_session_id,
                            )
                        except (json.JSONDecodeError, ValueError):
                            pass
                    ledger.append(
                        "astra_session_terminal",
                        astra_session_id=astra_session_id,
                        product_terminal_status=product_terminal_status,
                        failed=product_terminal_status != "completed",
                    )
                    try:
                        trace_capture = await self._collect_c0_logs(
                            environment,
                            paths,
                            controller_run_id=run_id,
                            session_id=astra_session_id,
                            product_terminal_status=product_terminal_status,
                        )
                    except Exception as exc:
                        trace_capture = {
                            "schema_version": 1,
                            "controller_run_id": run_id,
                            "astra_session_id": astra_session_id,
                            "product_terminal_status": product_terminal_status,
                            "failed": True,
                            "capture_failed": True,
                            "adapter_cancelled": False,
                            "capture_status": "missing",
                            "export_return_code": None,
                            "manifest_sha256": None,
                            "trajectory_file_count": 0,
                            "local_file_count": 0,
                            "local_trace_file_count": 0,
                            "tool_result_file_count": 0,
                            "server_event_count": 0,
                            "local_journal_event_count": 0,
                            "local_journal_terminal_event": None,
                            "errors": [
                                {
                                    "source": "trajectory_collection",
                                    "error": type(exc).__name__,
                                }
                            ],
                        }
                        try:
                            (
                                self.logs_dir / "trajectory-status.json"
                            ).write_text(
                                json.dumps(
                                    trace_capture,
                                    ensure_ascii=False,
                                    indent=2,
                                    sort_keys=True,
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                        except OSError:
                            pass
                    context.metadata.update(
                        {
                            "astra_trajectory_status": trace_capture[
                                "capture_status"
                            ],
                            "astra_trajectory_capture_failed": trace_capture[
                                "capture_failed"
                            ],
                            "astra_trajectory_manifest_sha256": trace_capture[
                                "manifest_sha256"
                            ],
                            "astra_trajectory_file_count": trace_capture[
                                "trajectory_file_count"
                            ],
                            "astra_trajectory_local_file_count": trace_capture[
                                "local_file_count"
                            ],
                            "astra_trajectory_local_trace_file_count": (
                                trace_capture["local_trace_file_count"]
                            ),
                            "astra_trajectory_tool_result_file_count": (
                                trace_capture["tool_result_file_count"]
                            ),
                            "astra_trajectory_server_event_count": trace_capture[
                                "server_event_count"
                            ],
                            "astra_trajectory_local_journal_event_count": (
                                trace_capture["local_journal_event_count"]
                            ),
                            "astra_trajectory_local_journal_terminal_event": (
                                trace_capture[
                                    "local_journal_terminal_event"
                                ]
                            ),
                        }
                    )
                    ledger.append(
                        "astra_trajectory_persisted",
                        astra_session_id=astra_session_id,
                        capture_status=trace_capture["capture_status"],
                        capture_failed=trace_capture["capture_failed"],
                        export_return_code=trace_capture["export_return_code"],
                        manifest_path="agent/astra-trajectory/manifest.json",
                        manifest_sha256=trace_capture["manifest_sha256"],
                        trajectory_file_count=trace_capture[
                            "trajectory_file_count"
                        ],
                        local_file_count=trace_capture["local_file_count"],
                        local_trace_file_count=trace_capture[
                            "local_trace_file_count"
                        ],
                        tool_result_file_count=trace_capture[
                            "tool_result_file_count"
                        ],
                        server_event_count=trace_capture["server_event_count"],
                        local_journal_event_count=trace_capture[
                            "local_journal_event_count"
                        ],
                        local_journal_terminal_event=trace_capture[
                            "local_journal_terminal_event"
                        ],
                        adapter_cancelled=(
                            product_terminal_status == "cancelled"
                            or trace_capture["adapter_cancelled"]
                        ),
                        failed=(
                            product_terminal_status != "completed"
                            or trace_capture["capture_failed"]
                            or trace_capture["adapter_cancelled"]
                        ),
                    )
                    if trace_capture["adapter_cancelled"]:
                        raise asyncio.CancelledError

        pending_failure: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as group:
                product_task = group.create_task(execute_product())
                controller_task = group.create_task(
                    controller.run(environment, product_done)
                )
        except asyncio.CancelledError as exc:
            pending_failure = exc
        except Exception as exc:
            pending_failure = exc

        if not product_task.cancelled():
            try:
                product_result = product_task.result()
            except Exception as exc:
                product_error = product_error or exc
        if not controller_task.cancelled():
            try:
                outcome = controller_task.result()
            except Exception:
                outcome = None

        return_code = (
            product_result.return_code if product_result is not None else None
        )
        product_terminal_status = (
            cleanup_report["product_terminal_status"]
            if cleanup_report
            else "adapter_infra_error"
        )
        retry_report = None
        retry_report_error = None
        try:
            retry_report = _load_stream_transport_retry_report(
                self.logs_dir / "stream-transport-retry.json",
                session_id=astra_session_id,
                max_retries=self.stream_transport_retries,
                overall_deadline_sec=product_timeout_sec,
                optional_retry_min_remaining_sec=(
                    self.stream_optional_retry_min_remaining_sec
                ),
            )
            if (
                retry_report["complete"]
                and retry_report["final_return_code"] != return_code
            ):
                raise ValueError(
                    "stream transport retry report return code mismatch"
                )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            retry_report_error = exc
        if retry_report is not None:
            ledger.append(
                "stream_transport_retry_summary",
                astra_session_id=astra_session_id,
                retry_count=retry_report["retry_count"],
                recovered=retry_report["recovered"],
                exhausted=retry_report["exhausted"],
                final_return_code=retry_report["final_return_code"],
                complete=retry_report["complete"],
                retry_skip_reason=retry_report.get("retry_skip_reason"),
                failure_classification=retry_report.get(
                    "failure_classification"
                ),
            )
        value = None
        parse_error = None
        stdout_path = self.logs_dir / "astra.stdout.json"
        if pending_failure is None:
            stdout_result = await environment.exec(
                command=f"cat {shlex.quote(paths['stdout'])}",
                timeout_sec=5,
            )
            if stdout_result.return_code == 0:
                stdout_path.write_text(
                    stdout_result.stdout or "",
                    encoding="utf-8",
                )
        if return_code == 0 and stdout_path.exists():
            try:
                value = parse_astra_json(
                    stdout_path.read_text(encoding="utf-8"),
                    expected_session_id=astra_session_id,
                )
            except Exception as exc:
                parse_error = exc
        ledger.append(
            "product_turn_exited",
            return_code=return_code,
            error_type=type(product_error).__name__ if product_error else None,
            product_terminal_status=product_terminal_status,
        )
        product_success = bool(value and value.get("success") is True)
        lifecycle_gate_passed = bool(
            outcome
            and outcome.trigger_hit
            and not outcome.fault_injected
        )
        try:
            observability = _finalize_astra_observability(
                self.logs_dir,
                astra_session_id,
            )
        except Exception as exc:
            observability = {
                "session_log_path": "agent/session.jsonl",
                "capture_status": "missing",
                "request_count": 0,
                "response_count": 0,
                "unpaired_request_count": 0,
                "incomplete_response_count": 0,
                "errors": [
                    {"source": "canonical_session", "error": type(exc).__name__}
                ],
                "token_usage": {
                    "input_tokens": None,
                    "fresh_input_tokens": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "coverage": "missing",
                    "reliable": False,
                },
            }
        metadata = {
            **initial_metadata,
            **observability_metadata(observability),
            "trigger_hit": bool(outcome and outcome.trigger_hit),
            "trigger_reason": outcome.reason if outcome else "controller_incomplete",
            "trigger_evidence_sha256": (
                outcome.evidence_sha256 if outcome else None
            ),
            "lifecycle_gate_passed": lifecycle_gate_passed,
            "product_return_code": return_code,
            "product_error_type": (
                type(product_error).__name__ if product_error else None
            ),
            "product_output_parse_error": (
                type(parse_error).__name__ if parse_error else None
            ),
            "product_completion_claim": product_success,
            "product_terminal_status": product_terminal_status,
            "stream_transport_retry_report_status": (
                (
                    "complete"
                    if retry_report["complete"]
                    else "incomplete"
                )
                if retry_report is not None
                else "missing_or_invalid"
            ),
            "stream_transport_retry_report_error_type": (
                type(retry_report_error).__name__
                if retry_report_error
                else None
            ),
            "stream_transport_retry_count": (
                retry_report["retry_count"] if retry_report else None
            ),
            "stream_transport_recovered": (
                retry_report["recovered"] if retry_report else None
            ),
            "stream_transport_retry_exhausted": (
                retry_report["exhausted"] if retry_report else None
            ),
            "stream_transport_retry_skip_reason": (
                retry_report.get("retry_skip_reason")
                if retry_report
                else None
            ),
            "stream_transport_failure_classification": (
                retry_report.get("failure_classification")
                if retry_report
                else None
            ),
            "product_cleanup_zero_live_proven": bool(
                cleanup_report and cleanup_report["zero_live_proven"]
            ),
            "product_cleanup_report_sha256": cleanup_report_sha256,
            "product_cleanup_error_type": (
                type(cleanup_error).__name__ if cleanup_error else None
            ),
            "product_cleanup_error": (
                str(cleanup_error) if cleanup_error else None
            ),
            "adapter_cancelled": bool(
                product_terminal_status == "cancelled"
                or (trace_capture and trace_capture["adapter_cancelled"])
            ),
            "astra_session_id": astra_session_id,
            "astra_run_id": value.get("run_id") if value else None,
            "tool_calls_count": value.get("tool_calls_count") if value else None,
            "tools_used": value.get("tools_used", []) if value else [],
            "artifact_arch": self._artifact_arch,
            "artifact_sha256": self._artifact_sha256,
            "astra_trajectory_status": (
                trace_capture["capture_status"] if trace_capture else "missing"
            ),
            "astra_trajectory_capture_failed": (
                trace_capture["capture_failed"] if trace_capture else True
            ),
            "astra_trajectory_dir": "agent/astra-trajectory",
            "astra_trajectory_manifest": (
                "agent/astra-trajectory/manifest.json"
            ),
            "astra_trajectory_export_return_code": (
                trace_capture["export_return_code"] if trace_capture else None
            ),
            "astra_trajectory_manifest_sha256": (
                trace_capture["manifest_sha256"] if trace_capture else None
            ),
            "astra_trajectory_file_count": (
                trace_capture["trajectory_file_count"] if trace_capture else 0
            ),
            "astra_trajectory_local_file_count": (
                trace_capture["local_file_count"] if trace_capture else 0
            ),
            "astra_trajectory_local_trace_file_count": (
                trace_capture["local_trace_file_count"] if trace_capture else 0
            ),
            "astra_trajectory_tool_result_file_count": (
                trace_capture["tool_result_file_count"] if trace_capture else 0
            ),
            "astra_trajectory_server_event_count": (
                trace_capture["server_event_count"] if trace_capture else 0
            ),
            "astra_trajectory_local_journal_event_count": (
                trace_capture["local_journal_event_count"]
                if trace_capture
                else 0
            ),
            "astra_trajectory_local_journal_terminal_event": (
                trace_capture["local_journal_terminal_event"]
                if trace_capture
                else None
            ),
        }
        context.metadata = metadata
        apply_token_usage(context, observability)
        session_failed = (
            product_terminal_status != "completed"
            or not product_success
        )
        self._write_session_record(
            controller_run_id=run_id,
            session_id=astra_session_id,
            product_terminal_status=product_terminal_status,
            capture_status=metadata["astra_trajectory_status"],
            error_type=(
                type(parse_error).__name__
                if parse_error
                else (type(product_error).__name__ if product_error else None)
            ),
            failed=session_failed,
            adapter_cancelled=metadata["adapter_cancelled"],
        )
        ledger.append(
            "astra_session_outcome",
            astra_session_id=astra_session_id,
            evaluation_status=self._evaluation_status,
            formal_score_eligible=self._formal_score_eligible,
            frozen_inputs_manifest_sha256=self._freeze_manifest_sha256,
            product_terminal_status=product_terminal_status,
            product_completion_claim=product_success,
            adapter_cancelled=metadata["adapter_cancelled"],
            failed=session_failed,
        )
        ledger.append(
            "controller_completed",
            evaluation_status=self._evaluation_status,
            formal_score_eligible=self._formal_score_eligible,
            frozen_inputs_manifest_sha256=self._freeze_manifest_sha256,
            trigger_registration_status=trigger_registration_status,
            trigger_scope=trigger_scope,
            trigger_hit=metadata["trigger_hit"],
            fault_injected=False,
            lifecycle_gate_passed=lifecycle_gate_passed,
            product_completion_claim=product_success,
            product_return_code=return_code,
            product_terminal_status=product_terminal_status,
            stream_transport_retry_count=metadata[
                "stream_transport_retry_count"
            ],
            stream_transport_recovered=metadata[
                "stream_transport_recovered"
            ],
            stream_transport_retry_exhausted=metadata[
                "stream_transport_retry_exhausted"
            ],
            product_cleanup_zero_live_proven=metadata[
                "product_cleanup_zero_live_proven"
            ],
            product_cleanup_error_type=metadata[
                "product_cleanup_error_type"
            ],
            adapter_cancelled=metadata["adapter_cancelled"],
            pending_error_type=(
                type(pending_failure).__name__ if pending_failure else None
            ),
            astra_session_id=astra_session_id,
            astra_trajectory_status=metadata["astra_trajectory_status"],
            astra_trajectory_capture_failed=metadata[
                "astra_trajectory_capture_failed"
            ],
            trajectory_capture_blocking=metadata[
                "trajectory_capture_blocking"
            ],
            astra_trajectory_manifest_sha256=metadata[
                "astra_trajectory_manifest_sha256"
            ],
            astra_trajectory_file_count=metadata["astra_trajectory_file_count"],
            astra_trajectory_server_event_count=metadata[
                "astra_trajectory_server_event_count"
            ],
            astra_trajectory_local_journal_event_count=metadata[
                "astra_trajectory_local_journal_event_count"
            ],
            astra_trajectory_local_journal_terminal_event=metadata[
                "astra_trajectory_local_journal_terminal_event"
            ],
        )
        if pending_failure is not None:
            raise pending_failure

    async def _collect_c0_logs(
        self,
        environment: BaseEnvironment,
        paths: dict[str, str],
        *,
        controller_run_id: str,
        session_id: str,
        product_terminal_status: str,
    ) -> dict[str, object]:
        collection_task = asyncio.create_task(
            self._collect_c0_logs_impl(
                environment,
                paths,
                controller_run_id=controller_run_id,
                session_id=session_id,
                product_terminal_status=product_terminal_status,
            )
        )
        try:
            result = await asyncio.shield(collection_task)
        except asyncio.CancelledError:
            # The task container is destroyed immediately after the adapter
            # returns. Finish the bounded copy, emit terminal evidence in the
            # caller, and only then preserve Harbor cancellation.
            while not collection_task.done():
                try:
                    await asyncio.shield(collection_task)
                except asyncio.CancelledError:
                    continue
            result = collection_task.result()
            result["adapter_cancelled"] = True
            result["failed"] = True
            errors = result.get("errors")
            if isinstance(errors, list):
                errors.append(
                    {
                        "source": "adapter",
                        "error": "CancelledDuringTrajectoryCollection",
                    }
                )
            (self.logs_dir / "trajectory-status.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            self._write_session_record(
                controller_run_id=controller_run_id,
                session_id=session_id,
                product_terminal_status=product_terminal_status,
                capture_status=str(result["capture_status"]),
                error_type="CancelledDuringTrajectoryCollection",
                failed=True,
                adapter_cancelled=True,
            )
            return result
        result["adapter_cancelled"] = False
        return result

    async def _collect_c0_logs_impl(
        self,
        environment: BaseEnvironment,
        paths: dict[str, str],
        *,
        controller_run_id: str,
        session_id: str,
        product_terminal_status: str,
    ) -> dict[str, object]:
        errors: list[dict[str, str]] = []
        downloads = (
            (paths["stdout"], self.logs_dir / "astra.stdout.json"),
            (paths["stderr"], self.logs_dir / "astra.stderr.txt"),
            (paths["identity"], self.logs_dir / "product.identity.json"),
            (paths["cleanup"], self.logs_dir / "product.cleanup.json"),
            (
                paths["retry"],
                self.logs_dir / "stream-transport-retry.json",
            ),
        )
        for remote_path, local_path in downloads:
            try:
                await asyncio.wait_for(
                    environment.download_file(remote_path, local_path),
                    timeout=10,
                )
            except Exception as exc:
                errors.append(
                    {
                        "source": local_path.name,
                        "error": type(exc).__name__,
                    }
                )

        export_command = shlex.join(
            [
                "python3",
                REMOTE_TRAJECTORY_EXPORTER,
                "export",
                "--session-id",
                session_id,
                "--terminal-status",
                product_terminal_status,
                "--sessions-root",
                f"{REMOTE_ROOT}/home/.astra/sessions",
                "--output-dir",
                paths["trajectory"],
            ]
        )
        try:
            export_result = await environment.exec(
                command=export_command,
                env={
                    **self._runtime_env(),
                    "ASTRA_API_URL": self._get_env("ASTRA_API_URL"),
                },
                timeout_sec=60,
            )
            export_return_code = export_result.return_code
            if export_return_code != 0:
                errors.append(
                    {
                        "source": "trajectory_export",
                        "error": f"return_code_{export_return_code}",
                    }
                )
        except Exception as exc:
            export_return_code = None
            errors.append(
                {
                    "source": "trajectory_export",
                    "error": type(exc).__name__,
                }
            )

        trajectory_dir = self.logs_dir / "astra-trajectory"
        try:
            await asyncio.wait_for(
                environment.download_dir(
                    paths["trajectory"],
                    trajectory_dir,
                ),
                timeout=30,
            )
        except Exception as exc:
            errors.append(
                {
                    "source": "trajectory_download",
                    "error": type(exc).__name__,
                }
            )

        manifest_path = trajectory_dir / "manifest.json"
        capture_status = "missing"
        manifest_sha256 = None
        local_file_count = 0
        local_trace_file_count = 0
        tool_result_file_count = 0
        server_event_count = 0
        local_journal_event_count = 0
        local_journal_terminal_event = None
        if manifest_path.is_file():
            try:
                manifest_bytes = manifest_path.read_bytes()
                manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                manifest = json.loads(manifest_bytes)
                status = manifest.get("capture_status")
                if status in {"complete", "partial", "missing"}:
                    capture_status = status
                raw_local_file_count = manifest.get("local_file_count")
                if isinstance(raw_local_file_count, int) and raw_local_file_count >= 0:
                    local_file_count = raw_local_file_count
                elif isinstance(manifest.get("local_files"), list):
                    local_file_count = len(manifest["local_files"])
                raw_local_trace_file_count = manifest.get(
                    "local_trace_file_count"
                )
                if (
                    isinstance(raw_local_trace_file_count, int)
                    and raw_local_trace_file_count >= 0
                ):
                    local_trace_file_count = raw_local_trace_file_count
                raw_tool_result_file_count = manifest.get(
                    "tool_result_file_count"
                )
                if (
                    isinstance(raw_tool_result_file_count, int)
                    and raw_tool_result_file_count >= 0
                ):
                    tool_result_file_count = raw_tool_result_file_count
                raw_server_event_count = manifest.get("server_event_count")
                if isinstance(raw_server_event_count, int) and raw_server_event_count >= 0:
                    server_event_count = raw_server_event_count
                raw_local_journal_event_count = manifest.get(
                    "local_journal_event_count"
                )
                if (
                    isinstance(raw_local_journal_event_count, int)
                    and raw_local_journal_event_count >= 0
                ):
                    local_journal_event_count = (
                        raw_local_journal_event_count
                    )
                if isinstance(
                    manifest.get("local_journal_terminal_event"),
                    str,
                ):
                    local_journal_terminal_event = manifest[
                        "local_journal_terminal_event"
                    ]
                manifest_errors = manifest.get("errors")
                if isinstance(manifest_errors, list):
                    errors.extend(
                        error
                        for error in manifest_errors
                        if isinstance(error, dict)
                    )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(
                    {
                        "source": "trajectory_manifest",
                        "error": type(exc).__name__,
                    }
                )

        if capture_status == "complete":
            if export_return_code != 0:
                capture_status = "partial"
                errors.append(
                    {
                        "source": "trajectory_manifest",
                        "error": "complete_manifest_with_failed_export",
                    }
                )
            else:
                try:
                    validated = validate_trajectory_bundle(
                        trajectory_dir,
                        session_id=session_id,
                        terminal_status=product_terminal_status,
                    )
                    manifest_sha256 = validated["manifest_sha256"]
                    local_file_count = validated["local_file_count"]
                    local_trace_file_count = validated[
                        "local_trace_file_count"
                    ]
                    tool_result_file_count = validated[
                        "tool_result_file_count"
                    ]
                    server_event_count = validated["server_event_count"]
                    local_journal_event_count = validated[
                        "local_journal_event_count"
                    ]
                    local_journal_terminal_event = validated[
                        "local_journal_terminal_event"
                    ]
                except (OSError, RuntimeError, ValueError) as exc:
                    capture_status = "partial"
                    errors.append(
                        {
                            "source": "trajectory_bundle_validation",
                            "error": type(exc).__name__,
                        }
                    )
        trajectory_file_count = (
            sum(1 for path in trajectory_dir.rglob("*") if path.is_file())
            if trajectory_dir.is_dir()
            else 0
        )
        status = {
            "schema_version": 1,
            "controller_run_id": controller_run_id,
            "astra_session_id": session_id,
            "product_terminal_status": product_terminal_status,
            "failed": (
                product_terminal_status != "completed"
                or capture_status != "complete"
            ),
            "capture_failed": capture_status != "complete",
            "adapter_cancelled": False,
            "capture_status": capture_status,
            "export_return_code": export_return_code,
            "manifest_sha256": manifest_sha256,
            "trajectory_file_count": trajectory_file_count,
            "local_file_count": local_file_count,
            "local_trace_file_count": local_trace_file_count,
            "tool_result_file_count": tool_result_file_count,
            "server_event_count": server_event_count,
            "local_journal_event_count": local_journal_event_count,
            "local_journal_terminal_event": local_journal_terminal_event,
            "errors": errors,
        }
        (self.logs_dir / "trajectory-status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._write_session_record(
            controller_run_id=controller_run_id,
            session_id=session_id,
            product_terminal_status=product_terminal_status,
            capture_status=capture_status,
            error_type=(
                errors[0].get("error")
                if errors and isinstance(errors[0].get("error"), str)
                else None
            ),
            failed=status["failed"],
        )
        return status

    def _write_session_record(
        self,
        *,
        controller_run_id: str,
        session_id: Optional[str],
        product_terminal_status: str,
        capture_status: str,
        error_type: Optional[str] = None,
        failed: Optional[bool] = None,
        adapter_cancelled: bool = False,
    ) -> None:
        if failed is None:
            failed = product_terminal_status not in {"not_started", "completed"}
        value = {
            "schema_version": 1,
            "controller_run_id": controller_run_id,
            "astra_session_id": session_id,
            "evaluation_status": self._evaluation_status,
            "formal_score_eligible": self._formal_score_eligible,
            "frozen_inputs_manifest_sha256": self._freeze_manifest_sha256,
            "product_terminal_status": product_terminal_status,
            "failed": failed,
            "adapter_cancelled": adapter_cancelled,
            "capture_status": capture_status,
            "error_type": error_type,
        }
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "astra-session.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
