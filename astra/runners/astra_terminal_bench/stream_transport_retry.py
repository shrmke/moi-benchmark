#!/usr/bin/env python3
"""Resume one Astra session after transient stream transport failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


_STREAM_TRANSPORT_MARKER = b"[stream_transport]"
_DEFAULT_OPTIONAL_RETRY_MIN_REMAINING_SECONDS = 600.0
_RESUME_PROMPT = (
    b"Resume the same interrupted task from the saved session state. "
    b"Do not restart or repeat completed work. Continue from the latest "
    b"available transcript or checkpoint and finish the original task.\n"
)


def _is_stream_transport_failure(
    return_code: int,
    stderr: bytes,
) -> bool:
    if return_code != 3:
        return False
    return _STREAM_TRANSPORT_MARKER in stderr.lower()


def _exit_code(return_code: int) -> int:
    return 128 + (-return_code) if return_code < 0 else return_code


def _session_id(command: Sequence[str]) -> str | None:
    positions = [
        index for index, value in enumerate(command) if value == "--session-id"
    ]
    if not positions and "--no-resume" in command:
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValueError("Astra retry command must contain exactly one --session-id")
    session_id = command[positions[0] + 1]
    if not session_id or session_id.startswith("-"):
        raise ValueError("Astra retry command has an invalid --session-id")
    return session_id


def _session_id_from_stdout(stdout: bytes) -> str | None:
    try:
        value = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    session_id = value.get("session_id") if isinstance(value, dict) else None
    if not isinstance(session_id, str):
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    return session_id


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seconds(value: float) -> float:
    return round(max(0.0, value), 3)


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return _seconds(deadline - time.monotonic())


def run_with_retries(
    command: Sequence[str],
    initial_input: bytes,
    *,
    max_retries: int,
    report_path: Path,
    overall_deadline_seconds: float | None = None,
    optional_retry_min_remaining_seconds: float = (
        _DEFAULT_OPTIONAL_RETRY_MIN_REMAINING_SECONDS
    ),
) -> int:
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if (
        overall_deadline_seconds is not None
        and overall_deadline_seconds <= 0
    ):
        raise ValueError("overall_deadline_seconds must be positive")
    if optional_retry_min_remaining_seconds < 0:
        raise ValueError(
            "optional_retry_min_remaining_seconds must be non-negative"
        )

    session_id = _session_id(command)
    current_command = list(command)
    wrapper_started_at = _utc_now()
    wrapper_started_monotonic = time.monotonic()
    deadline = (
        wrapper_started_monotonic + overall_deadline_seconds
        if overall_deadline_seconds is not None
        else None
    )
    deadline_at = (
        wrapper_started_at + timedelta(seconds=overall_deadline_seconds)
        if overall_deadline_seconds is not None
        else None
    )
    attempts: list[dict[str, Any]] = []
    request_input = initial_input

    def write_report(
        *,
        status: str,
        complete: bool,
        recovered: bool,
        exhausted: bool,
        final_return_code: int | None,
        retry_skip_reason: str | None,
        failure_classification: str | None,
    ) -> None:
        _atomic_json(
            report_path,
            {
                "schema_version": 1,
                "session_id": session_id,
                "max_retries": max_retries,
                "overall_deadline_seconds": overall_deadline_seconds,
                "optional_retry_min_remaining_seconds": (
                    optional_retry_min_remaining_seconds
                ),
                "wrapper_started_at_utc": _timestamp(wrapper_started_at),
                "deadline_at_utc": (
                    _timestamp(deadline_at) if deadline_at is not None else None
                ),
                "status": status,
                "attempt_count": len(attempts),
                "retry_count": max(0, len(attempts) - 1),
                "attempts": attempts,
                "complete": complete,
                "recovered": recovered,
                "exhausted": exhausted,
                "retry_skip_reason": retry_skip_reason,
                "failure_classification": failure_classification,
                "final_return_code": final_return_code,
                "remaining_deadline_seconds": _remaining_seconds(deadline),
            },
        )

    for attempt_index in range(max_retries + 1):
        attempt_started_at = _utc_now()
        attempt_started_monotonic = time.monotonic()
        attempt = {
            "attempt": attempt_index + 1,
            "retry_number": attempt_index,
            "input_mode": "initial" if attempt_index == 0 else "resume",
            "status": "running",
            "started_at_utc": _timestamp(attempt_started_at),
            "finished_at_utc": None,
            "duration_seconds": None,
            "remaining_deadline_seconds_at_start": _remaining_seconds(deadline),
            "remaining_deadline_seconds_at_finish": None,
            "return_code": None,
            "stdout_bytes": None,
            "stdout_sha256": None,
            "stderr_bytes": None,
            "stderr_sha256": None,
            "stream_transport_failure": None,
        }
        attempts.append(attempt)
        # Persist before entering the blocking child process. If an outer
        # deadline kills this wrapper, the running attempt remains visible.
        write_report(
            status="attempt_running",
            complete=False,
            recovered=False,
            exhausted=False,
            final_return_code=None,
            retry_skip_reason=None,
            failure_classification="attempt_in_progress",
        )

        result = subprocess.run(
            list(current_command),
            input=request_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        attempt_finished_at = _utc_now()
        attempt_finished_monotonic = time.monotonic()
        stream_transport_failure = _is_stream_transport_failure(
            result.returncode,
            result.stderr,
        )
        exit_code = _exit_code(result.returncode)
        if session_id is None:
            session_id = _session_id_from_stdout(result.stdout)
        attempt.update(
            {
                "status": "completed",
                "finished_at_utc": _timestamp(attempt_finished_at),
                "duration_seconds": _seconds(
                    attempt_finished_monotonic - attempt_started_monotonic
                ),
                "remaining_deadline_seconds_at_finish": (
                    _remaining_seconds(deadline)
                ),
                "return_code": exit_code,
                "stdout_bytes": len(result.stdout),
                "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                "stderr_bytes": len(result.stderr),
                "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                "stream_transport_failure": stream_transport_failure,
            }
        )

        retry_skip_reason = None
        should_retry = False
        if stream_transport_failure and attempt_index < max_retries:
            next_retry_number = attempt_index + 1
            if next_retry_number == 1:
                # The first retry is guaranteed by policy. Its execution is
                # still bounded by the outer process deadline.
                should_retry = True
            elif deadline is None:
                # Preserve the historical max_retries behavior when no
                # overall deadline is supplied.
                should_retry = True
            else:
                remaining = _remaining_seconds(deadline)
                should_retry = (
                    remaining is not None
                    and remaining >= optional_retry_min_remaining_seconds
                )
                if not should_retry:
                    retry_skip_reason = (
                        "insufficient_remaining_deadline_for_optional_retry"
                    )
            if should_retry and session_id is None:
                should_retry = False
                retry_skip_reason = "session_id_unavailable_for_retry"

        recovered = attempt_index > 0 and exit_code == 0
        exhausted = (
            stream_transport_failure and attempt_index == max_retries
        )
        if should_retry:
            failure_classification = None
            status = "retry_pending"
        elif recovered or exit_code == 0:
            failure_classification = None
            status = "complete"
        elif retry_skip_reason is not None:
            failure_classification = (
                "stream_transport_optional_retry_skipped"
            )
            status = "complete"
        elif exhausted:
            failure_classification = "stream_transport_retry_exhausted"
            status = "complete"
        else:
            failure_classification = "non_stream_failure"
            status = "complete"

        write_report(
            status=status,
            complete=not should_retry,
            recovered=recovered,
            exhausted=exhausted,
            final_return_code=exit_code,
            retry_skip_reason=retry_skip_reason,
            failure_classification=failure_classification,
        )
        if should_retry:
            if "--no-resume" in current_command:
                index = current_command.index("--no-resume")
                current_command[index : index + 1] = [
                    "--session-id",
                    session_id,
                ]
            request_input = _RESUME_PROMPT
            continue

        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()
        return exit_code

    raise AssertionError("retry loop did not return")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--overall-deadline-seconds",
        type=float,
        default=None,
        help=(
            "overall wrapper deadline used to gate optional retries; "
            "omit to preserve max-retries behavior"
        ),
    )
    parser.add_argument(
        "--optional-retry-min-remaining-seconds",
        type=float,
        default=_DEFAULT_OPTIONAL_RETRY_MIN_REMAINING_SECONDS,
        help=(
            "minimum remaining overall deadline required for retry number "
            "two and later (the first retry is always attempted)"
        ),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = _parser().parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("missing Astra command")
    return run_with_retries(
        command,
        sys.stdin.buffer.read(),
        max_retries=args.max_retries,
        report_path=args.report,
        overall_deadline_seconds=args.overall_deadline_seconds,
        optional_retry_min_remaining_seconds=(
            args.optional_retry_min_remaining_seconds
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
