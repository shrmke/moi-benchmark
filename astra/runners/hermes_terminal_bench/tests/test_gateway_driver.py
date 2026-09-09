from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astra.runners.hermes_terminal_bench import gateway_driver
from astra.runners.hermes_terminal_bench.gateway_driver import (
    ProductDeadlineExpired,
    consume_provider_environment,
    finalize_deadline_terminal,
    gateway_command,
    gateway_environment,
    parse_sse_data,
    poll_terminal_status,
    run_terminal_exit_code,
    unwrap_hermes_launcher,
    unresolved_approval_decision,
    validate_run_event_stream,
    validate_session_export,
    wait_for_policy_guard,
)


class GatewayDriverTests(unittest.TestCase):
    def test_terminal_exit_codes_preserve_timeout_and_cancellation(self) -> None:
        self.assertEqual(run_terminal_exit_code("completed"), 0)
        self.assertEqual(run_terminal_exit_code("failed"), 2)
        self.assertEqual(run_terminal_exit_code("timed_out"), 124)
        self.assertEqual(run_terminal_exit_code("cancelled"), 125)

    def test_expired_run_deadline_has_explicit_terminal_type(self) -> None:
        with self.assertRaises(ProductDeadlineExpired):
            poll_terminal_status(
                "http://127.0.0.1:1",
                "run-1",
                api_key="local-api-secret",
                deadline=0,
            )

    def test_provider_credential_is_consumed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text(
                json.dumps(
                    {
                        "key_name": "GLM_API_KEY",
                        "key_value": "provider-secret",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                consume_provider_environment(path),
                {"GLM_API_KEY": "provider-secret"},
            )
            self.assertFalse(path.exists())

    def test_deepseek_provider_credential_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text(
                json.dumps(
                    {
                        "key_name": "DEEPSEEK_API_KEY",
                        "key_value": "provider-secret",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                consume_provider_environment(path),
                {"DEEPSEEK_API_KEY": "provider-secret"},
            )
            self.assertFalse(path.exists())

    def test_gateway_command_has_no_automatic_approval_flags(self) -> None:
        command = gateway_command()

        self.assertEqual(command[:3], ["hermes", "gateway", "run"])
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--accept-hooks", command)

    def test_install_launcher_is_unwrapped_for_policy_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "venv" / "bin" / "hermes"
            target.parent.mkdir(parents=True)
            target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            target.chmod(0o755)
            launcher = root / "hermes"
            launcher.write_text(
                (
                    "#!/usr/bin/env bash\n"
                    "unset PYTHONPATH\n"
                    f'exec "{target}" "$@"\n'
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)

            resolved = unwrap_hermes_launcher(str(launcher))

        self.assertEqual(resolved, [str(target)])

    def test_fhs_install_launcher_is_unwrapped_for_policy_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
            interpreter.chmod(0o755)
            script = root / "hermes"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            launcher = root / "bin" / "hermes"
            launcher.parent.mkdir()
            launcher.write_text(
                (
                    "#!/usr/bin/env bash\n"
                    "unset PYTHONPATH\n"
                    "unset PYTHONHOME\n"
                    f'exec "{interpreter}" "{script}" "$@"\n'
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)

            resolved = unwrap_hermes_launcher(str(launcher))

        self.assertEqual(resolved, [str(interpreter), str(script)])
        self.assertEqual(
            gateway_command(resolved)[:4],
            [str(interpreter), str(script), "gateway", "run"],
        )

    def test_unwrapped_fhs_launcher_loads_policy_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guard_dir = root / "policy"
            guard_dir.mkdir()
            guard_source = (
                Path(__file__).parents[1]
                / "policy_guard"
                / "sitecustomize.py"
            )
            guard = guard_dir / "sitecustomize.py"
            guard.write_bytes(guard_source.read_bytes())
            guard_sha256 = hashlib.sha256(guard.read_bytes()).hexdigest()
            evidence = root / "policy-guard.jsonl"
            script = root / "hermes"
            script.write_text(
                "import time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            launcher = root / "bin" / "hermes"
            launcher.parent.mkdir()
            launcher.write_text(
                (
                    "#!/usr/bin/env bash\n"
                    "unset PYTHONPATH\n"
                    "unset PYTHONHOME\n"
                    f'exec "{sys.executable}" "{script}" "$@"\n'
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            command = gateway_command(
                unwrap_hermes_launcher(str(launcher))
            )
            process = subprocess.Popen(
                command,
                env=gateway_environment(
                    {"PATH": os.environ.get("PATH", "")},
                    api_key="local-api-secret",
                    port=18642,
                    policy_guard_dir=str(guard_dir),
                    policy_guard_sha256=guard_sha256,
                    policy_guard_evidence=str(evidence),
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                row = wait_for_policy_guard(
                    evidence,
                    gateway_pid=process.pid,
                    expected_sha256=guard_sha256,
                    process=process,
                    deadline=time.monotonic() + 2,
                )
            finally:
                process.terminate()
                process.wait(timeout=5)

        self.assertEqual(row["event"], "policy_guard.loaded")
        self.assertEqual(row["pid"], process.pid)
        self.assertEqual(row["source_sha256"], guard_sha256)

    def test_gateway_environment_pins_managed_policy_and_disables_yolo(self) -> None:
        env = gateway_environment(
            {
                "HERMES_YOLO_MODE": "1",
                "HERMES_MANAGED_DIR": "/tmp/attacker-policy",
                "HERMES_ACCEPT_HOOKS": "1",
                "GLM_API_KEY": "provider-secret",
            },
            api_key="local-api-secret",
            port=18642,
            policy_guard_dir="/installed-agent/hermes-c0-policy",
            policy_guard_sha256="a" * 64,
            policy_guard_evidence="/logs/agent/hermes-policy-guard.jsonl",
        )

        self.assertEqual(env["HERMES_YOLO_MODE"], "0")
        self.assertEqual(env["HERMES_MANAGED_DIR"], "/etc/hermes")
        self.assertEqual(env["HERMES_ACCEPT_HOOKS"], "0")
        self.assertEqual(env["HERMES_HOME"], "/tmp/hermes")
        self.assertEqual(env["HERMES_EXEC_ASK"], "1")
        self.assertEqual(
            env["PYTHONPATH"], "/installed-agent/hermes-c0-policy"
        )
        self.assertEqual(env["HERMES_C0_POLICY_GUARD_SHA256"], "a" * 64)
        self.assertEqual(env["API_SERVER_ENABLED"], "true")
        self.assertEqual(env["API_SERVER_KEY"], "local-api-secret")
        self.assertEqual(env["GLM_API_KEY"], "provider-secret")

    def test_unresolved_approval_is_always_denied(self) -> None:
        decision = unresolved_approval_decision(
            {
                "event": "approval.request",
                "run_id": "run-1",
                "request_id": "approval-1",
                "choices": ["once", "session", "always", "deny"],
            }
        )

        self.assertEqual(decision["choice"], "deny")
        self.assertEqual(decision["policy"], "deterministic_deny")
        self.assertEqual(decision["run_id"], "run-1")
        self.assertEqual(decision["request_id"], "approval-1")

    def test_parse_sse_data_ignores_comments_and_invalid_json(self) -> None:
        events = list(
            parse_sse_data(
                [
                    b": keepalive\n",
                    b"event: message\n",
                    b"data: not-json\n",
                    b'data: {"event":"tool.started","tool":"terminal"}\n',
                    b"\n",
                ]
            )
        )

        self.assertEqual(
            events,
            [{"event": "tool.started", "tool": "terminal"}],
        )

    def _write_events(
        self,
        path: Path,
        *events: dict[str, object],
    ) -> None:
        path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

    def test_run_event_stream_requires_one_submitted_and_one_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {"event": "gateway.started", "session_id": "session-1"},
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
                {"event": "tool.started", "run_id": "run-1"},
                {"event": "run.completed", "run_id": "run-1"},
            )

            summary = validate_run_event_stream(
                path,
                run_id="run-1",
                session_id="session-1",
            )

        self.assertEqual(summary["submitted_count"], 1)
        self.assertEqual(summary["terminal_event_count"], 1)
        self.assertEqual(summary["terminal_event"], "run.completed")
        self.assertEqual(summary["terminal_event_source"], "hermes")
        self.assertIsNone(summary["terminal_reason"])
        self.assertEqual(summary["event_count"], 4)

    def test_driver_timeout_is_a_distinct_valid_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
                {
                    "event": "run.timed_out",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "source": "driver",
                    "reason": "ProductDeadlineExpired",
                    "timestamp": 1.0,
                },
            )

            summary = validate_run_event_stream(
                path,
                run_id="run-1",
                session_id="session-1",
            )

        self.assertEqual(summary["terminal_event"], "run.timed_out")
        self.assertEqual(summary["terminal_status"], "timed_out")
        self.assertEqual(summary["terminal_event_source"], "driver")
        self.assertEqual(summary["terminal_reason"], "ProductDeadlineExpired")

    def test_driver_timeout_requires_matching_ids_source_and_reason(self) -> None:
        base = {
            "event": "run.timed_out",
            "run_id": "run-1",
            "session_id": "session-1",
            "source": "driver",
            "reason": "ProductDeadlineExpired",
        }
        invalid = (
            {**base, "session_id": "session-other"},
            {**base, "source": "hermes"},
            {**base, "reason": "some other timeout"},
        )
        for terminal in invalid:
            with self.subTest(terminal=terminal):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "events.jsonl"
                    self._write_events(
                        path,
                        {
                            "event": "run.submitted",
                            "run_id": "run-1",
                            "session_id": "session-1",
                        },
                        terminal,
                    )
                    with self.assertRaises(RuntimeError):
                        validate_run_event_stream(
                            path,
                            run_id="run-1",
                            session_id="session-1",
                        )

    def test_native_and_driver_terminals_are_rejected_as_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
                {"event": "run.completed", "run_id": "run-1"},
                {
                    "event": "run.timed_out",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "source": "driver",
                    "reason": "ProductDeadlineExpired",
                },
            )

            with self.assertRaisesRegex(RuntimeError, "one terminal event"):
                validate_run_event_stream(
                    path,
                    run_id="run-1",
                    session_id="session-1",
                )

    def test_deadline_without_native_terminal_records_driver_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
            )
            with patch.object(
                gateway_driver,
                "request_json",
                side_effect=[
                    (200, {"status": "running", "last_event": "tool.started"}),
                    (200, {"status": "stopping"}),
                ],
            ), patch.object(
                gateway_driver,
                "wait_for_native_terminal",
                return_value=(None, "grace expired"),
            ):
                summary = finalize_deadline_terminal(
                    "http://127.0.0.1:18642",
                    "run-1",
                    session_id="session-1",
                    api_key="secret",
                    events_path=path,
                    deadline_sec=1200,
                    grace_sec=1,
                )

            rows = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(summary["terminal_event"], "run.timed_out")
        self.assertEqual(summary["terminal_event_source"], "driver")
        self.assertEqual(rows[-1]["event"], "run.timed_out")
        self.assertEqual(rows[-1]["deadline_sec"], 1200)
        self.assertEqual(rows[-1]["observed_hermes_status"], "running")

    def test_deadline_grace_keeps_native_terminal_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
            )

            def append_cancelled(*_args, **_kwargs):
                event = {"event": "run.cancelled", "run_id": "run-1"}
                gateway_driver.append_jsonl(path, event)
                return event, None

            with patch.object(
                gateway_driver,
                "request_json",
                side_effect=[
                    (200, {"status": "running"}),
                    (200, {"status": "stopping"}),
                ],
            ), patch.object(
                gateway_driver,
                "wait_for_native_terminal",
                side_effect=append_cancelled,
            ):
                summary = finalize_deadline_terminal(
                    "http://127.0.0.1:18642",
                    "run-1",
                    session_id="session-1",
                    api_key="secret",
                    events_path=path,
                    deadline_sec=1200,
                    grace_sec=1,
                )
            rows = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(summary["terminal_event"], "run.cancelled")
        self.assertEqual(summary["terminal_event_source"], "hermes")
        self.assertEqual(
            [row["event"] for row in rows if row["event"].startswith("run.")],
            ["run.submitted", "run.cancelled"],
        )

    def test_existing_native_terminals_never_get_driver_fallback(self) -> None:
        for terminal_event in (
            "run.completed",
            "run.failed",
            "run.cancelled",
        ):
            with self.subTest(terminal_event=terminal_event):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "events.jsonl"
                    self._write_events(
                        path,
                        {
                            "event": "run.submitted",
                            "run_id": "run-1",
                            "session_id": "session-1",
                        },
                        {"event": terminal_event, "run_id": "run-1"},
                    )
                    with patch.object(
                        gateway_driver,
                        "request_json",
                    ) as request_mock, patch.object(
                        gateway_driver,
                        "wait_for_native_terminal",
                    ) as wait_mock:
                        summary = finalize_deadline_terminal(
                            "http://127.0.0.1:18642",
                            "run-1",
                            session_id="session-1",
                            api_key="secret",
                            events_path=path,
                            deadline_sec=1200,
                            grace_sec=1,
                        )
                    request_mock.assert_not_called()
                    wait_mock.assert_not_called()
                    rows = [
                        json.loads(line)
                        for line in path.read_text().splitlines()
                    ]
                    self.assertEqual(summary["terminal_event"], terminal_event)
                    self.assertNotIn(
                        "run.timed_out",
                        [row["event"] for row in rows],
                    )

    def test_run_event_stream_rejects_early_eof_and_wrong_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
            )
            with self.assertRaisesRegex(RuntimeError, "terminal event"):
                validate_run_event_stream(
                    path,
                    run_id="run-1",
                    session_id="session-1",
                )

            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
                {"event": "run.completed", "run_id": "run-other"},
            )
            with self.assertRaisesRegex(RuntimeError, "mismatched run_id"):
                validate_run_event_stream(
                    path,
                    run_id="run-1",
                    session_id="session-1",
                )

    def test_run_event_stream_rejects_duplicate_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self._write_events(
                path,
                {
                    "event": "run.submitted",
                    "run_id": "run-1",
                    "session_id": "session-1",
                },
                {"event": "run.completed", "run_id": "run-1"},
                {"event": "run.failed", "run_id": "run-1"},
            )

            with self.assertRaisesRegex(RuntimeError, "one terminal event"):
                validate_run_event_stream(
                    path,
                    run_id="run-1",
                    session_id="session-1",
                )

    def test_run_event_stream_rejects_duplicate_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            submitted = {
                "event": "run.submitted",
                "run_id": "run-1",
                "session_id": "session-1",
            }
            self._write_events(
                path,
                submitted,
                submitted,
                {"event": "run.completed", "run_id": "run-1"},
            )

            with self.assertRaisesRegex(RuntimeError, "one run.submitted"):
                validate_run_event_stream(
                    path,
                    run_id="run-1",
                    session_id="session-1",
                )

    def test_session_export_requires_matching_nonempty_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "id": "session-1",
                        "messages": [{"role": "user", "content": "task"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = validate_session_export(
                path,
                session_id="session-1",
            )
            self.assertEqual(summary["session_id"], "session-1")
            self.assertEqual(summary["message_count"], 1)

            with self.assertRaisesRegex(RuntimeError, "current session"):
                validate_session_export(path, session_id="session-other")

            path.write_text(
                '{"id":"session-1","messages":[]}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "no valid messages"):
                validate_session_export(path, session_id="session-1")

            path.unlink()
            with self.assertRaisesRegex(RuntimeError, "parse"):
                validate_session_export(path, session_id="session-1")
