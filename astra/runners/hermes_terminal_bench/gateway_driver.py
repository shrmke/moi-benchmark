#!/usr/bin/env python3
"""Drive one Hermes task through the gateway Runs API.

This process is intentionally the root of the tested Hermes process tree: it
starts the foreground gateway as a child, submits one run, and shuts the
gateway down after the run reaches a terminal state.  A host-side lifecycle
controller can therefore observe (and, in a later F1 implementation, target)
the complete product process tree without relying on Hermes internals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
NATIVE_TERMINAL_EVENTS = frozenset(
    f"run.{status}" for status in TERMINAL_STATUSES
)
DRIVER_TIMEOUT_EVENT = "run.timed_out"
TERMINAL_EVENTS = NATIVE_TERMINAL_EVENTS | {DRIVER_TIMEOUT_EVENT}
PROVIDER_KEY_NAMES = frozenset(
    {"DEEPSEEK_API_KEY", "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"}
)
MAX_TIMEOUT_SEC = 24000
DEADLINE_TERMINAL_GRACE_SEC = 8.0


class DriverTermination(RuntimeError):
    pass


class ProductDeadlineExpired(TimeoutError):
    pass


def run_terminal_exit_code(status: str) -> int:
    return {
        "completed": 0,
        "timed_out": 124,
        "cancelled": 125,
    }.get(status, 2)


def _raise_on_termination(signum: int, _frame: Any) -> None:
    raise DriverTermination(f"received signal {signum}")


def _raise_on_deadline(_signum: int, _frame: Any) -> None:
    raise ProductDeadlineExpired("Hermes run exceeded its driver deadline")


def find_hermes_command() -> list[str]:
    resolved = shutil.which("hermes")
    if resolved:
        return unwrap_hermes_launcher(resolved)
    candidates = (
        Path.home() / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return unwrap_hermes_launcher(str(candidate))
    raise FileNotFoundError("could not locate the installed hermes executable")


def unwrap_hermes_launcher(executable: str) -> list[str]:
    """Resolve the install.sh launcher that deliberately clears PYTHONPATH."""

    path = Path(executable)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [executable]
    for raw_line in text.splitlines():
        try:
            parts = shlex.split(raw_line)
        except ValueError:
            continue
        if parts[:1] != ["exec"] or parts[-1:] != ["$@"]:
            continue
        command = parts[1:-1]
        if len(command) not in (1, 2):
            continue
        target = Path(command[0])
        if not target.is_file() or not os.access(target, os.X_OK):
            continue
        if len(command) == 2 and not Path(command[1]).is_file():
            continue
        return command
    return [executable]


def gateway_command(
    hermes_executable: str | Iterable[str] = "hermes",
) -> list[str]:
    """Return the foreground gateway command, with no approval bypass flags."""

    executable = (
        [hermes_executable]
        if isinstance(hermes_executable, str)
        else list(hermes_executable)
    )
    return [
        *executable,
        "gateway",
        "run",
        "--no-supervise",
        "--external-supervisor",
    ]


def gateway_environment(
    base: dict[str, str],
    *,
    api_key: str,
    port: int,
    policy_guard_dir: str,
    policy_guard_sha256: str,
    policy_guard_evidence: str,
) -> dict[str, str]:
    """Build the product-only environment for the foreground gateway."""

    env = dict(base)
    # Pin process-scoped policy before Hermes imports approval or hook code.
    # Explicit false values also prevent a later user .env load from filling
    # otherwise-absent variables.
    env.update(
        {
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "127.0.0.1",
            "API_SERVER_PORT": str(port),
            "API_SERVER_KEY": api_key,
            "HERMES_EXEC_ASK": "1",
            "HERMES_GATEWAY_NO_SUPERVISE": "1",
            "HERMES_HOME": "/tmp/hermes",
            "HERMES_MANAGED_DIR": "/etc/hermes",
            "HERMES_YOLO_MODE": "0",
            "HERMES_ACCEPT_HOOKS": "0",
            "PYTHONPATH": policy_guard_dir,
            "HERMES_C0_POLICY_GUARD_SHA256": policy_guard_sha256,
            "HERMES_C0_POLICY_GUARD_EVIDENCE": policy_guard_evidence,
            "TERMINAL_CWD": "/app",
            "TERMINAL_ENV": "local",
        }
    )
    return env


def policy_guard_source_sha256(directory: str) -> str:
    source = Path(directory) / "sitecustomize.py"
    return hashlib.sha256(source.read_bytes()).hexdigest()


def wait_for_policy_guard(
    path: Path,
    *,
    gateway_pid: int,
    expected_sha256: str,
    process: subprocess.Popen[Any],
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("event") == "policy_guard.loaded"
                    and row.get("pid") == gateway_pid
                    and row.get("source_sha256") == expected_sha256
                ):
                    return row
        if process.poll() is not None:
            raise RuntimeError(
                "Hermes gateway exited before policy guard evidence"
            )
        time.sleep(0.05)
    raise RuntimeError("Hermes policy guard did not load before startup")


def parse_sse_data(lines: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from the API server's ``data:`` SSE frames."""

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def unresolved_approval_decision(event: dict[str, Any]) -> dict[str, Any]:
    """Return the one permitted external decision for an unresolved approval."""

    return {
        "event": "approval.decision",
        "run_id": event.get("run_id"),
        "request_id": event.get("request_id"),
        "choice": "deny",
        "policy": "deterministic_deny",
        "timestamp": time.time(),
    }


def append_jsonl(
    path: Path,
    value: dict[str, Any],
    *,
    durable: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
        stream.write("\n")
        if durable:
            stream.flush()
            os.fsync(stream.fileno())


def sync_file(path: Path) -> None:
    """Ensure already-appended terminal evidence reaches durable storage."""

    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def validate_run_event_stream(
    path: Path,
    *,
    run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Validate the persisted driver/SSE stream for exactly one run."""

    if not run_id or not session_id:
        raise RuntimeError("run and session IDs are required for trajectory validation")
    try:
        payload = path.read_bytes()
        rows = [
            json.loads(line)
            for line in payload.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not parse the Hermes run event stream") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("Hermes run event stream is empty or invalid")

    for row in rows:
        row_run_id = row.get("run_id")
        if row_run_id is not None and row_run_id != run_id:
            raise RuntimeError("Hermes run event stream contains a mismatched run_id")
        row_session_id = row.get("session_id")
        if row_session_id is not None and row_session_id != session_id:
            raise RuntimeError(
                "Hermes run event stream contains a mismatched session_id"
            )

    submitted = [
        (index, row)
        for index, row in enumerate(rows)
        if row.get("event") == "run.submitted"
    ]
    if len(submitted) != 1:
        raise RuntimeError(
            "Hermes run event stream must contain exactly one run.submitted"
        )
    if (
        submitted[0][1].get("run_id") != run_id
        or submitted[0][1].get("session_id") != session_id
    ):
        raise RuntimeError("Hermes run.submitted IDs do not match the current run")

    terminal = [
        (index, row)
        for index, row in enumerate(rows)
        if row.get("event") in TERMINAL_EVENTS
    ]
    if len(terminal) != 1:
        raise RuntimeError(
            "Hermes run event stream must contain exactly one terminal event"
        )
    if terminal[0][1].get("run_id") != run_id:
        raise RuntimeError("Hermes terminal event does not match the current run")
    if terminal[0][0] <= submitted[0][0]:
        raise RuntimeError("Hermes terminal event precedes run.submitted")

    terminal_event = str(terminal[0][1]["event"])
    terminal_row = terminal[0][1]
    if terminal_event == DRIVER_TIMEOUT_EVENT:
        if (
            terminal_row.get("source") != "driver"
            or terminal_row.get("reason") != "ProductDeadlineExpired"
            or terminal_row.get("session_id") != session_id
        ):
            raise RuntimeError(
                "Hermes driver timeout terminal event is invalid"
            )
        terminal_source = "driver"
        terminal_reason = "ProductDeadlineExpired"
    else:
        if terminal_row.get("source") not in {None, "hermes"}:
            raise RuntimeError("Hermes native terminal event source is invalid")
        terminal_source = "hermes"
        terminal_reason = terminal_row.get("reason") or terminal_row.get(
            "error"
        )
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "event_count": len(rows),
        "submitted_count": 1,
        "terminal_event_count": 1,
        "terminal_event": terminal_event,
        "terminal_status": terminal_event.removeprefix("run."),
        "terminal_event_source": terminal_source,
        "terminal_reason": terminal_reason,
    }


def validate_session_export(
    path: Path,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Validate one JSONL session export for the explicitly requested ID."""

    if not session_id:
        raise RuntimeError("session ID is required for session export validation")
    try:
        payload = path.read_bytes()
        lines = [
            line for line in payload.decode("utf-8").splitlines() if line.strip()
        ]
        rows = [json.loads(line) for line in lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not parse the Hermes session export") from exc
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Hermes session export must contain exactly one session")
    exported = rows[0]
    exported_session_id = exported.get("id") or exported.get("session_id")
    if exported_session_id != session_id:
        raise RuntimeError("Hermes session export does not match the current session")
    messages = exported.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(message, dict) for message in messages)
    ):
        raise RuntimeError("Hermes session export has no valid messages")
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "session_id": exported_session_id,
        "message_count": len(messages),
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def consume_provider_environment(path: Path) -> dict[str, str]:
    """Read and immediately remove the one-run provider credential."""

    try:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "could not read the isolated provider credential"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError("isolated provider credential is not an object")
        key_name = value.get("key_name")
        key_value = value.get("key_value")
        if (
            key_name not in PROVIDER_KEY_NAMES
            or not isinstance(key_value, str)
            or not key_value
        ):
            raise RuntimeError("isolated provider credential is invalid")
        return {key_name: key_value}
    finally:
        path.unlink(missing_ok=True)


def request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            value = json.loads(payload) if payload else {}
            return response.status, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            value = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            value = {"error": payload}
        return exc.code, value if isinstance(value, dict) else {}


def wait_for_gateway(
    base_url: str,
    *,
    api_key: str,
    process: subprocess.Popen[Any],
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Hermes gateway exited during startup ({process.returncode})")
        try:
            status, value = request_json(
                "GET",
                f"{base_url}/health",
                api_key=api_key,
                timeout=2,
            )
            if status == 200 and value.get("status") == "ok":
                return
        except (OSError, TimeoutError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise TimeoutError("Hermes gateway did not become healthy before the deadline")


def stream_run(
    base_url: str,
    run_id: str,
    *,
    session_id: str,
    api_key: str,
    events_path: Path,
    deadline: float,
) -> dict[str, Any]:
    """Consume run events and deny every approval that smart mode escalates."""

    url = f"{base_url}/v1/runs/{run_id}/events"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    denied = 0
    with urllib.request.urlopen(request, timeout=45) as response:
        for event in parse_sse_data(response):
            if (
                event.get("run_id") is not None
                and event.get("run_id") != run_id
            ):
                raise RuntimeError("Hermes SSE event has a mismatched run_id")
            if (
                event.get("session_id") is not None
                and event.get("session_id") != session_id
            ):
                raise RuntimeError("Hermes SSE event has a mismatched session_id")
            append_jsonl(events_path, event)
            if time.monotonic() >= deadline:
                raise ProductDeadlineExpired(
                    "Hermes run exceeded its driver deadline"
                )
            if event.get("event") != "approval.request":
                continue

            decision = unresolved_approval_decision(event)
            status, value = request_json(
                "POST",
                f"{base_url}/v1/runs/{run_id}/approval",
                api_key=api_key,
                body={"choice": "deny"},
            )
            decision["http_status"] = status
            decision["resolved"] = value.get("resolved")
            append_jsonl(events_path, decision)
            if status != 200:
                raise RuntimeError(
                    f"could not deny unresolved Hermes approval (HTTP {status})"
                )
            denied += int(value.get("resolved") or 1)
    summary = validate_run_event_stream(
        events_path,
        run_id=run_id,
        session_id=session_id,
    )
    summary["approvals_denied"] = denied
    return summary


def persisted_native_terminal_event(
    path: Path,
    *,
    run_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Return one already-persisted native terminal without inventing one."""

    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not inspect Hermes run events") from exc
    native = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("event") in NATIVE_TERMINAL_EVENTS
    ]
    if len(native) > 1:
        raise RuntimeError("Hermes run event stream has duplicate native terminals")
    if not native:
        return None
    event = native[0]
    if event.get("run_id") != run_id:
        raise RuntimeError("Hermes native terminal has a mismatched run_id")
    event_session_id = event.get("session_id")
    if event_session_id is not None and event_session_id != session_id:
        raise RuntimeError("Hermes native terminal has a mismatched session_id")
    return event


def wait_for_native_terminal(
    base_url: str,
    run_id: str,
    *,
    session_id: str,
    api_key: str,
    events_path: Path,
    deadline: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Reconnect to the Runs SSE stream during bounded terminalization."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "deadline terminal grace expired before SSE reconnect"
    request = urllib.request.Request(
        f"{base_url}/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(0.1, min(remaining, 5.0)),
        ) as response:
            for event in parse_sse_data(response):
                if time.monotonic() >= deadline:
                    return None, "deadline terminal grace expired while reading SSE"
                event_run_id = event.get("run_id")
                if event_run_id is not None and event_run_id != run_id:
                    raise RuntimeError(
                        "Hermes deadline SSE event has a mismatched run_id"
                    )
                event_session_id = event.get("session_id")
                if (
                    event_session_id is not None
                    and event_session_id != session_id
                ):
                    raise RuntimeError(
                        "Hermes deadline SSE event has a mismatched session_id"
                    )
                append_jsonl(events_path, event)
                if event.get("event") in NATIVE_TERMINAL_EVENTS:
                    sync_file(events_path)
                    return event, None
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return None, "Hermes deadline SSE stream closed without a native terminal"


def finalize_deadline_terminal(
    base_url: str,
    run_id: str,
    *,
    session_id: str,
    api_key: str,
    events_path: Path,
    deadline_sec: float,
    grace_sec: float = DEADLINE_TERMINAL_GRACE_SEC,
) -> dict[str, Any]:
    """Capture a native deadline terminal or durably record driver timeout."""

    grace_started = time.monotonic()
    grace_deadline = grace_started + max(0.0, grace_sec)
    errors: list[str] = []
    status_observation: str | None = None
    status_last_event: str | None = None
    stop_http_status: int | None = None

    native = persisted_native_terminal_event(
        events_path,
        run_id=run_id,
        session_id=session_id,
    )
    if native is None and time.monotonic() < grace_deadline:
        request_timeout = max(
            0.1,
            min(2.0, grace_deadline - time.monotonic()),
        )
        try:
            status_code, status_value = request_json(
                "GET",
                f"{base_url}/v1/runs/{run_id}",
                api_key=api_key,
                timeout=request_timeout,
            )
            if status_code == 200:
                raw_status = status_value.get("status")
                status_observation = (
                    str(raw_status) if raw_status is not None else None
                )
                raw_last_event = status_value.get("last_event")
                status_last_event = (
                    str(raw_last_event) if raw_last_event is not None else None
                )
            else:
                errors.append(f"run status returned HTTP {status_code}")
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            errors.append(f"run status {type(exc).__name__}: {exc}")

        if (
            status_observation not in TERMINAL_STATUSES
            and time.monotonic() < grace_deadline
        ):
            request_timeout = max(
                0.1,
                min(2.0, grace_deadline - time.monotonic()),
            )
            try:
                stop_http_status, _ = request_json(
                    "POST",
                    f"{base_url}/v1/runs/{run_id}/stop",
                    api_key=api_key,
                    body={},
                    timeout=request_timeout,
                )
                if stop_http_status not in {200, 404}:
                    errors.append(
                        f"native run stop returned HTTP {stop_http_status}"
                    )
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                errors.append(f"native run stop {type(exc).__name__}: {exc}")

        native, stream_error = wait_for_native_terminal(
            base_url,
            run_id,
            session_id=session_id,
            api_key=api_key,
            events_path=events_path,
            deadline=grace_deadline,
        )
        if stream_error:
            errors.append(stream_error)

    native = native or persisted_native_terminal_event(
        events_path,
        run_id=run_id,
        session_id=session_id,
    )
    if native is None:
        append_jsonl(
            events_path,
            {
                "event": DRIVER_TIMEOUT_EVENT,
                "run_id": run_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "source": "driver",
                "reason": "ProductDeadlineExpired",
                "deadline_sec": deadline_sec,
                "grace_sec": grace_sec,
                "observed_hermes_status": status_observation,
                "observed_hermes_last_event": status_last_event,
            },
            durable=True,
        )
    else:
        sync_file(events_path)

    summary = validate_run_event_stream(
        events_path,
        run_id=run_id,
        session_id=session_id,
    )
    summary.update(
        {
            "approvals_denied": 0,
            "deadline_grace_sec": grace_sec,
            "deadline_grace_elapsed_sec": time.monotonic() - grace_started,
            "deadline_stop_http_status": stop_http_status,
            "deadline_observed_hermes_status": status_observation,
            "deadline_observed_hermes_last_event": status_last_event,
            "deadline_terminalization_errors": errors,
        }
    )
    return summary


def poll_terminal_status(
    base_url: str,
    run_id: str,
    *,
    api_key: str,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status_code, value = request_json(
            "GET",
            f"{base_url}/v1/runs/{run_id}",
            api_key=api_key,
        )
        if status_code != 200:
            raise RuntimeError(f"could not read Hermes run status (HTTP {status_code})")
        if value.get("status") in TERMINAL_STATUSES:
            return value
        time.sleep(0.2)
    raise ProductDeadlineExpired("Hermes run exceeded its driver deadline")


def stop_gateway(
    process: subprocess.Popen[Any],
    events_path: Path,
) -> dict[str, Any]:
    """Stop the foreground gateway as cleanup, never as a fault action."""

    cleanup = {
        "event": "gateway.cleanup",
        "fault_action": False,
        "pid": process.pid,
        "requested_signal": "SIGTERM",
        "escalated_to_sigkill": False,
        "timestamp": time.time(),
    }
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cleanup["escalated_to_sigkill"] = True
            process.kill()
            process.wait(timeout=10)
    cleanup["return_code"] = process.returncode
    append_jsonl(events_path, cleanup)
    return cleanup


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--events-file", type=Path, required=True)
    parser.add_argument("--gateway-log", type=Path, required=True)
    parser.add_argument("--provider-env-file", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--port", type=int, default=18642)
    parser.add_argument("--timeout-sec", type=float, default=1800)
    parser.add_argument("--cwd", default="/app")
    parser.add_argument("--policy-guard-dir", required=True)
    parser.add_argument("--policy-guard-sha256", required=True)
    parser.add_argument("--policy-guard-evidence", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout_sec <= 0 or args.timeout_sec > MAX_TIMEOUT_SEC:
        raise ValueError(
            f"--timeout-sec must be in (0, {MAX_TIMEOUT_SEC}]"
        )
    if not 1024 <= args.port <= 65535:
        raise ValueError("--port must be between 1024 and 65535")

    started_at = time.time()
    signal.signal(signal.SIGTERM, _raise_on_termination)
    signal.signal(signal.SIGINT, _raise_on_termination)
    signal.signal(signal.SIGALRM, _raise_on_deadline)
    signal.setitimer(signal.ITIMER_REAL, args.timeout_sec)
    deadline = time.monotonic() + args.timeout_sec
    api_key = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{args.port}"
    args.events_file.unlink(missing_ok=True)
    args.policy_guard_evidence.unlink(missing_ok=True)
    result: dict[str, Any] = {
        "status": "driver_failed",
        "session_id": args.session_id,
        "approval_policy": "hermes_native_smart_then_deterministic_deny",
        "approvals_denied": 0,
        "started_at": started_at,
    }

    args.gateway_log.parent.mkdir(parents=True, exist_ok=True)
    gateway_log = args.gateway_log.open("w", encoding="utf-8")
    gateway: subprocess.Popen[Any] | None = None
    run_id: str | None = None
    exit_code = 1
    try:
        instruction = args.instruction_file.read_text(encoding="utf-8")
        gateway_base_env = dict(os.environ)
        gateway_base_env.update(
            consume_provider_environment(args.provider_env_file)
        )
        if (
            policy_guard_source_sha256(args.policy_guard_dir)
            != args.policy_guard_sha256
        ):
            raise RuntimeError("Hermes policy guard source digest mismatch")
        gateway = subprocess.Popen(
            gateway_command(find_hermes_command()),
            cwd=args.cwd,
            env=gateway_environment(
                gateway_base_env,
                api_key=api_key,
                port=args.port,
                policy_guard_dir=args.policy_guard_dir,
                policy_guard_sha256=args.policy_guard_sha256,
                policy_guard_evidence=str(args.policy_guard_evidence),
            ),
            stdin=subprocess.DEVNULL,
            stdout=gateway_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        result["gateway_pid"] = gateway.pid
        append_jsonl(
            args.events_file,
            {
                "event": "gateway.started",
                "pid": gateway.pid,
                "session_id": args.session_id,
                "timestamp": time.time(),
            },
        )
        policy_guard = wait_for_policy_guard(
            args.policy_guard_evidence,
            gateway_pid=gateway.pid,
            expected_sha256=args.policy_guard_sha256,
            process=gateway,
            deadline=min(deadline, time.monotonic() + 30),
        )
        result["policy_guard_active"] = True
        result["policy_guard"] = policy_guard
        wait_for_gateway(
            base_url,
            api_key=api_key,
            process=gateway,
            deadline=min(deadline, time.monotonic() + 120),
        )

        status, start = request_json(
            "POST",
            f"{base_url}/v1/runs",
            api_key=api_key,
            body={"input": instruction, "session_id": args.session_id},
        )
        if status != 202 or not start.get("run_id"):
            raise RuntimeError(f"could not start Hermes run (HTTP {status})")
        run_id = str(start["run_id"])
        result["run_id"] = run_id
        append_jsonl(
            args.events_file,
            {
                "event": "run.submitted",
                "run_id": run_id,
                "session_id": args.session_id,
                "timestamp": time.time(),
            },
        )

        stream_summary = stream_run(
            base_url,
            run_id,
            session_id=args.session_id,
            api_key=api_key,
            events_path=args.events_file,
            deadline=deadline,
        )
        result["approvals_denied"] = stream_summary["approvals_denied"]
        result["stream_event_count"] = stream_summary["event_count"]
        result["stream_terminal_event"] = stream_summary["terminal_event"]
        result["stream_terminal_event_source"] = stream_summary[
            "terminal_event_source"
        ]
        result["stream_terminal_reason"] = stream_summary[
            "terminal_reason"
        ]
        result["stream_terminal_event_count"] = stream_summary[
            "terminal_event_count"
        ]
        result["stream_submitted_count"] = stream_summary["submitted_count"]
        final = poll_terminal_status(
            base_url,
            run_id,
            api_key=api_key,
            deadline=deadline,
        )
        if final.get("status") != stream_summary["terminal_status"]:
            raise RuntimeError(
                "Hermes terminal SSE event and run status are inconsistent"
            )
        result.update(
            {
                "status": final.get("status"),
                "output": final.get("output", ""),
                "usage": final.get("usage") or {},
                "error": final.get("error"),
            }
        )
        exit_code = run_terminal_exit_code(result["status"])
    except ProductDeadlineExpired as exc:
        signal.setitimer(signal.ITIMER_REAL, 0)
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "timed_out"
        if run_id is not None and gateway is not None and gateway.poll() is None:
            try:
                terminal_summary = finalize_deadline_terminal(
                    base_url,
                    run_id,
                    session_id=args.session_id,
                    api_key=api_key,
                    events_path=args.events_file,
                    deadline_sec=args.timeout_sec,
                )
                result["approvals_denied"] = terminal_summary[
                    "approvals_denied"
                ]
                result["stream_event_count"] = terminal_summary["event_count"]
                result["stream_terminal_event"] = terminal_summary[
                    "terminal_event"
                ]
                result["stream_terminal_event_source"] = terminal_summary[
                    "terminal_event_source"
                ]
                result["stream_terminal_reason"] = terminal_summary[
                    "terminal_reason"
                ]
                result["stream_terminal_event_count"] = terminal_summary[
                    "terminal_event_count"
                ]
                result["stream_submitted_count"] = terminal_summary[
                    "submitted_count"
                ]
                result["deadline_terminalization"] = {
                    key: value
                    for key, value in terminal_summary.items()
                    if key.startswith("deadline_")
                }
            except BaseException as terminal_exc:
                result["deadline_terminalization_error"] = (
                    f"{type(terminal_exc).__name__}: {terminal_exc}"
                )
        exit_code = run_terminal_exit_code(result["status"])
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "driver_failed"
        exit_code = 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        args.provider_env_file.unlink(missing_ok=True)
        if gateway is not None:
            try:
                result["cleanup"] = stop_gateway(gateway, args.events_file)
            except BaseException as exc:
                result["cleanup"] = {
                    "event": "gateway.cleanup_failed",
                    "fault_action": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                exit_code = 1
        gateway_log.close()
        result["finished_at"] = time.time()
        result["duration_sec"] = result["finished_at"] - started_at
        write_json(args.result_file, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
