from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


CLEAN = "C0"
PROCESS_KILL = "F1"
_CONDITIONS = {CLEAN, PROCESS_KILL}
_ELF_MACHINES = {62: "x86_64", 183: "aarch64"}
_LINUX_ARCH_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "x86_64": "x86_64",
    "aarch64": "aarch64",
}


class SmokeConfigurationError(ValueError):
    """The smoke runner configuration cannot preserve its safety contract."""


class ControllerError(RuntimeError):
    """The external controller could not establish or execute ground truth."""


def validate_condition(value: str) -> str:
    condition = value.strip().upper()
    if condition not in _CONDITIONS:
        raise SmokeConfigurationError(
            f"condition must be one of {sorted(_CONDITIONS)}, got {value!r}"
        )
    return condition


def validate_linux_elf(path: Path) -> str:
    """Reject host-native and unsupported artifacts before any upload."""
    try:
        header = path.read_bytes()[:20]
    except OSError as exc:
        raise SmokeConfigurationError(f"cannot read Astra artifact {path}: {exc}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise SmokeConfigurationError(
            f"{path} is not a Linux ELF artifact (a macOS Mach-O binary is invalid)"
        )
    if header[4] != 2 or header[5] != 1:
        raise SmokeConfigurationError(f"{path} must be a 64-bit little-endian ELF")
    machine = int.from_bytes(header[18:20], "little")
    if machine not in _ELF_MACHINES:
        raise SmokeConfigurationError(
            f"{path} has unsupported ELF machine {machine}; expected x86_64 or aarch64"
        )
    return _ELF_MACHINES[machine]


def normalize_linux_arch(value: str) -> str:
    arch = value.strip().lower()
    try:
        return _LINUX_ARCH_ALIASES[arch]
    except KeyError as exc:
        raise SmokeConfigurationError(
            f"unsupported Linux container architecture {value!r}"
        ) from exc


def write_minimal_credentials(path: Path, access_token: str) -> None:
    """Write one access-token-only profile to a new mode-0600 file."""
    if not access_token:
        raise SmokeConfigurationError("ASTRA_ACCESS_TOKEN is empty")
    payload = {
        "current_profile": "default",
        "profiles": {"default": {"access_token": access_token}},
    }
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise SmokeConfigurationError(
            f"cannot create temporary Astra credentials: {exc}"
        ) from exc


def lifecycle_gate_passes(
    condition: str,
    *,
    trigger_hit: bool,
    fault_injected: bool,
    turn_return_code: int,
) -> bool:
    condition = validate_condition(condition)
    if not trigger_hit:
        return False
    if condition == CLEAN:
        return not fault_injected and turn_return_code == 0
    return fault_injected and turn_return_code != 0


def parse_astra_json(
    raw: str, expected_session_id: Optional[str] = None
) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControllerError(f"Astra did not emit one JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError("Astra JSON output is not an object")
    session_id = value.get("session_id")
    if not isinstance(session_id, str):
        raise ControllerError("Astra JSON output has no string session_id")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise ControllerError(f"Astra returned a non-UUID session_id: {session_id!r}") from exc
    if expected_session_id is not None and session_id != expected_session_id:
        raise ControllerError(
            f"Astra changed session_id: expected {expected_session_id}, got {session_id}"
        )
    return value


def parse_identity(raw: str) -> dict[str, Any]:
    try:
        identity = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControllerError(f"invalid process identity JSON: {exc}") from exc
    if not isinstance(identity, dict):
        raise ControllerError("process identity must be a JSON object")
    for key in (
        "pid",
        "ppid",
        "pgid",
        "sid",
        "start_ticks",
        "exe",
        "cgroup",
        "supervisor",
    ):
        if key not in identity:
            raise ControllerError(f"process identity is missing {key}")
    if (
        not isinstance(identity["pid"], int)
        or not isinstance(identity["pgid"], int)
        or identity["pid"] <= 1
        or identity["pid"] != identity["pgid"]
        or identity["pid"] != identity["sid"]
    ):
        raise ControllerError("process identity is not an isolated process-group leader")
    if not isinstance(identity["start_ticks"], int) or identity["start_ticks"] <= 0:
        raise ControllerError("process identity has invalid start_ticks")
    if not isinstance(identity["exe"], str) or not identity["exe"].startswith("/"):
        raise ControllerError("process identity has invalid executable path")
    if not isinstance(identity["cgroup"], str) or not identity["cgroup"]:
        raise ControllerError("process identity has invalid cgroup")
    supervisor = identity["supervisor"]
    if (
        not isinstance(supervisor, dict)
        or not isinstance(supervisor.get("pid"), int)
        or supervisor["pid"] <= 1
        or supervisor["pid"] == identity["pid"]
        or identity["ppid"] != supervisor["pid"]
    ):
        raise ControllerError("process identity has an invalid supervisor")
    for key in ("ppid", "pgid", "sid", "start_ticks", "exe", "cgroup"):
        if key not in supervisor:
            raise ControllerError(f"supervisor identity is missing {key}")
    return identity


class JsonlLedger:
    """Append-only host-side evidence; prompts and credentials never enter it."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._sequence = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> None:
        self._sequence += 1
        record = {
            "schema_version": 1,
            "run_id": self.run_id,
            "sequence": self._sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


@dataclass(frozen=True)
class ControllerConfig:
    condition: str
    trigger_path: str
    identity_path: str
    probe_path: str
    expected_exe: str
    trigger_timeout_sec: float = 60.0
    poll_interval_sec: float = 0.2

    def __post_init__(self) -> None:
        validate_condition(self.condition)
        for name, value in (
            ("trigger_path", self.trigger_path),
            ("identity_path", self.identity_path),
            ("probe_path", self.probe_path),
            ("expected_exe", self.expected_exe),
        ):
            if not value.startswith("/") or "\n" in value or "\x00" in value:
                raise SmokeConfigurationError(f"{name} must be a safe absolute path")
        if self.trigger_timeout_sec <= 0 or self.poll_interval_sec <= 0:
            raise SmokeConfigurationError("controller timeouts must be positive")


@dataclass(frozen=True)
class FaultOutcome:
    trigger_hit: bool
    fault_injected: bool
    reason: str
    identity: Optional[dict[str, Any]] = None


class FaultController:
    """Host-side controller using only BaseEnvironment-compatible exec calls."""

    def __init__(
        self,
        config: ControllerConfig,
        emit: Callable[..., None],
    ):
        self.config = config
        self.emit = emit

    async def run(self, environment: Any, turn_done: asyncio.Event) -> FaultOutcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.trigger_timeout_sec
        identity = await self._wait_for_identity(environment, turn_done, deadline)
        if identity is None:
            self.emit("trigger_no_hit", reason="turn_ended_before_identity")
            return FaultOutcome(False, False, "turn_ended_before_identity")

        self.emit(
            "product_process_registered",
            pid=identity["pid"],
            ppid=identity["ppid"],
            pgid=identity["pgid"],
            sid=identity["sid"],
            start_ticks=identity["start_ticks"],
            exe=identity["exe"],
            supervisor_pid=identity["supervisor"]["pid"],
        )
        trigger_command = f"test -e {shlex.quote(self.config.trigger_path)}"
        while loop.time() < deadline:
            if turn_done.is_set():
                self.emit("trigger_no_hit", reason="turn_completed")
                return FaultOutcome(False, False, "turn_completed", identity)
            result = await environment.exec(command=trigger_command, timeout_sec=5)
            if result.return_code == 0:
                self.emit("trigger_observed", predicate="path_exists")
                if self.config.condition == CLEAN:
                    self.emit("fault_action", action="noop", executed=True)
                    return FaultOutcome(True, False, "clean_noop", identity)
                return await self._kill(environment, identity)
            if result.return_code != 1:
                raise ControllerError(
                    f"trigger probe failed with return code {result.return_code}"
                )
            await asyncio.sleep(self.config.poll_interval_sec)

        self.emit("trigger_no_hit", reason="trigger_timeout")
        return FaultOutcome(False, False, "trigger_timeout", identity)

    async def _wait_for_identity(
        self, environment: Any, turn_done: asyncio.Event, deadline: float
    ) -> Optional[dict[str, Any]]:
        command = f"cat {shlex.quote(self.config.identity_path)}"
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            if turn_done.is_set():
                return None
            result = await environment.exec(command=command, timeout_sec=5)
            if result.return_code == 0 and result.stdout:
                return parse_identity(result.stdout)
            await asyncio.sleep(self.config.poll_interval_sec)
        return None

    async def _kill(
        self, environment: Any, identity: dict[str, Any]
    ) -> FaultOutcome:
        argv = [
            "python3",
            self.config.probe_path,
            "kill",
            "--identity",
            self.config.identity_path,
            "--expected-exe",
            self.config.expected_exe,
        ]
        result = await environment.exec(command=shlex.join(argv), timeout_sec=10)
        try:
            detail = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ControllerError("kill probe returned invalid JSON") from exc
        targeted_tree_pids = detail.get("targeted_tree_pids", [])
        targeted_descendant_pids = detail.get("targeted_descendant_pids", [])
        surviving_tree_pids = detail.get("surviving_tree_pids", [])
        signal_errors = detail.get("signal_errors", [])
        executed = (
            result.return_code == 0
            and detail.get("status") == "killed"
            and detail.get("root_pid") == identity["pid"]
            and detail.get("supervisor_pid") == identity["supervisor"]["pid"]
            and isinstance(detail.get("freeze_rounds"), int)
            and detail["freeze_rounds"] >= 2
            and isinstance(targeted_tree_pids, list)
            and all(isinstance(pid, int) for pid in targeted_tree_pids)
            and identity["pid"] in targeted_tree_pids
            and isinstance(targeted_descendant_pids, list)
            and bool(targeted_descendant_pids)
            and all(
                isinstance(pid, int)
                and pid in targeted_tree_pids
                and pid != identity["pid"]
                for pid in targeted_descendant_pids
            )
            and surviving_tree_pids == []
            and signal_errors == []
        )
        self.emit(
            "fault_action",
            action="freeze_kill_tree_sigkill",
            executed=executed,
            return_code=result.return_code,
            root_pid=identity["pid"],
            supervisor_pid=identity["supervisor"]["pid"],
            freeze_rounds=detail.get("freeze_rounds"),
            targeted_tree_pids=targeted_tree_pids,
            targeted_descendant_pids=targeted_descendant_pids,
            targeted_pgids=detail.get("targeted_pgids", []),
            targeted_sids=detail.get("targeted_sids", []),
            surviving_tree_pids=surviving_tree_pids,
            signal_errors=signal_errors,
        )
        if not executed:
            return FaultOutcome(True, False, "kill_raced_or_refused", identity)
        environment_probe = await environment.exec(command="true", timeout_sec=5)
        self.emit(
            "task_environment_post_fault_probe",
            alive=environment_probe.return_code == 0,
        )
        if environment_probe.return_code != 0:
            raise ControllerError("task environment did not survive product process kill")
        return FaultOutcome(True, True, "fault_injected", identity)


def astra_args(
    *,
    remote_binary: str,
    model_name: Optional[str],
    max_turns: Optional[int],
    session_id: Optional[str],
    permission_mode: str,
    read_memory: bool = True,
) -> list[str]:
    argv = [remote_binary]
    if model_name:
        argv.extend(["--model", model_name])
    if not read_memory:
        argv.extend(["--disallowed-tools", "memory"])
    argv.extend(["--bare", "--no-instructions"])
    if max_turns is not None:
        argv.extend(["--max-turns", str(max_turns)])
    argv.append("chat")
    if session_id is None:
        argv.append("--no-resume")
    else:
        argv.extend(["--session-id", session_id])
    argv.extend(
        ["--permission-mode", permission_mode, "--json", "--no-color", "--stdin"]
    )
    return argv


def probe_run_command(
    *,
    probe_path: str,
    identity_path: str,
    stdout_path: str,
    stderr_path: str,
    stdin_path: str,
    cwd: str,
    child_argv: list[str],
    deadline_sec: Optional[float] = None,
    cleanup_report_path: Optional[str] = None,
    cleanup_grace_sec: float = 2.0,
    strict_cleanup: bool = False,
    exclude_stdout_json_events: Optional[list[str]] = None,
) -> str:
    argv = [
        "python3",
        probe_path,
        "run",
        "--identity",
        identity_path,
        "--stdout",
        stdout_path,
        "--stderr",
        stderr_path,
        "--stdin",
        stdin_path,
        "--cwd",
        cwd,
    ]
    if deadline_sec is not None:
        argv.extend(["--deadline-sec", str(deadline_sec)])
    if cleanup_report_path is not None:
        argv.extend(["--cleanup-report", cleanup_report_path])
    if strict_cleanup:
        argv.extend(
            [
                "--cleanup-grace-sec",
                str(cleanup_grace_sec),
                "--strict-cleanup",
            ]
        )
    for event_type in exclude_stdout_json_events or []:
        argv.extend(["--exclude-stdout-json-event", event_type])
    argv.extend(["--", *child_argv])
    return shlex.join(argv)
