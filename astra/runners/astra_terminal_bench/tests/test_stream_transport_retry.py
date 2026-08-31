import io
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astra.runners.astra_terminal_bench.stream_transport_retry import (
    _parser,
    run_with_retries,
)


class StreamTransportRetryTests(unittest.TestCase):
    def _run(
        self,
        results,
        *,
        max_retries=2,
        overall_deadline_seconds=None,
        optional_retry_min_remaining_seconds=600.0,
    ):
        command = [
            "/installed-agent/astra",
            "--session-id",
            "session-1",
            "--stdin",
        ]
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "retry.json"
            with (
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.subprocess.run",
                    side_effect=results,
                ) as run,
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.sys.stdout",
                    SimpleNamespace(buffer=stdout),
                ),
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.sys.stderr",
                    SimpleNamespace(buffer=stderr),
                ),
            ):
                return_code = run_with_retries(
                    command,
                    b"original instruction\n",
                    max_retries=max_retries,
                    report_path=report_path,
                    overall_deadline_seconds=overall_deadline_seconds,
                    optional_retry_min_remaining_seconds=(
                        optional_retry_min_remaining_seconds
                    ),
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        return return_code, run, stdout.getvalue(), stderr.getvalue(), report

    def test_stream_transport_failure_resumes_same_session(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] connection lost",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b'{"success":true}\n',
                stderr=b"",
            ),
        ]

        return_code, run, stdout, stderr, report = self._run(results)

        self.assertEqual(return_code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            run.call_args_list[1].args[0],
        )
        self.assertEqual(
            run.call_args_list[0].args[0].count("--session-id"),
            1,
        )
        self.assertEqual(
            run.call_args_list[0].kwargs["input"],
            b"original instruction\n",
        )
        self.assertIn(
            b"Resume the same interrupted task",
            run.call_args_list[1].kwargs["input"],
        )
        self.assertEqual(stdout, b'{"success":true}\n')
        self.assertEqual(stderr, b"")
        self.assertEqual(report["session_id"], "session-1")
        self.assertEqual(report["retry_count"], 1)
        self.assertTrue(report["recovered"])
        self.assertFalse(report["exhausted"])

    def test_fresh_cli_session_is_discovered_and_used_for_retry(self):
        session_id = str(uuid.uuid4())
        command = [
            "/installed-agent/astra",
            "chat",
            "--no-resume",
            "--stdin",
        ]
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=json.dumps({"session_id": session_id}).encode(),
                stderr=b"[stream_transport] connection lost",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {"session_id": session_id, "success": True}
                ).encode(),
                stderr=b"",
            ),
        ]
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "astra.runners.astra_terminal_bench."
            "stream_transport_retry.subprocess.run",
            side_effect=results,
        ) as run, mock.patch(
            "astra.runners.astra_terminal_bench."
            "stream_transport_retry.sys.stdout",
            SimpleNamespace(buffer=stdout),
        ), mock.patch(
            "astra.runners.astra_terminal_bench."
            "stream_transport_retry.sys.stderr",
            SimpleNamespace(buffer=stderr),
        ):
            report_path = Path(directory) / "retry.json"
            return_code = run_with_retries(
                command,
                b"instruction\n",
                max_retries=2,
                report_path=report_path,
            )
            report = json.loads(report_path.read_text())

        self.assertEqual(return_code, 0)
        self.assertIn("--no-resume", run.call_args_list[0].args[0])
        retry_command = run.call_args_list[1].args[0]
        self.assertNotIn("--no-resume", retry_command)
        self.assertEqual(
            retry_command[retry_command.index("--session-id") + 1],
            session_id,
        )
        self.assertEqual(report["session_id"], session_id)

    def test_non_stream_failure_is_not_retried(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"partial [stream_transport]",
                stderr=b"ordinary command failure",
            )
        ]

        return_code, run, stdout, stderr, report = self._run(results)

        self.assertEqual(return_code, 3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(stdout, b"partial [stream_transport]")
        self.assertEqual(stderr, b"ordinary command failure")
        self.assertEqual(report["retry_count"], 0)
        self.assertFalse(report["recovered"])
        self.assertFalse(report["exhausted"])

    def test_stream_transport_failure_stops_after_retry_limit(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] stream transport failed",
            )
            for _ in range(3)
        ]

        return_code, run, _stdout, stderr, report = self._run(results)

        self.assertEqual(return_code, 3)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            stderr,
            b"[stream_transport] stream transport failed",
        )
        self.assertEqual(report["attempt_count"], 3)
        self.assertEqual(report["retry_count"], 2)
        self.assertFalse(report["recovered"])
        self.assertTrue(report["exhausted"])
        self.assertEqual(
            report["failure_classification"],
            "stream_transport_retry_exhausted",
        )

    def test_signal_return_code_matches_wrapper_exit_code(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=-9,
                stdout=b"",
                stderr=b"killed",
            )
        ]

        return_code, run, _stdout, stderr, report = self._run(results)

        self.assertEqual(return_code, 137)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(stderr, b"killed")
        self.assertEqual(report["final_return_code"], 137)
        self.assertEqual(report["attempts"][0]["return_code"], 137)

    def test_first_retry_is_guaranteed_but_optional_retry_can_be_skipped(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] connection lost",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] connection lost again",
            ),
        ]

        return_code, run, _stdout, stderr, report = self._run(
            results,
            overall_deadline_seconds=1800,
            optional_retry_min_remaining_seconds=1801,
        )

        self.assertEqual(return_code, 3)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            run.call_args_list[1].args[0],
        )
        self.assertEqual(
            stderr,
            b"[stream_transport] connection lost again",
        )
        self.assertEqual(report["retry_count"], 1)
        self.assertFalse(report["exhausted"])
        self.assertEqual(
            report["retry_skip_reason"],
            "insufficient_remaining_deadline_for_optional_retry",
        )
        self.assertEqual(
            report["failure_classification"],
            "stream_transport_optional_retry_skipped",
        )

    def test_optional_retry_runs_when_remaining_deadline_is_sufficient(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] connection lost",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=b"",
                stderr=b"[stream_transport] connection lost again",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"recovered",
                stderr=b"",
            ),
        ]

        return_code, run, stdout, _stderr, report = self._run(
            results,
            overall_deadline_seconds=1800,
            optional_retry_min_remaining_seconds=600,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(stdout, b"recovered")
        self.assertEqual(report["retry_count"], 2)
        self.assertTrue(report["recovered"])
        self.assertIsNone(report["retry_skip_reason"])

    def test_report_is_written_before_child_starts(self):
        observed = {}

        def inspect_running_report(*_args, **_kwargs):
            observed["report"] = json.loads(
                observed["report_path"].read_text(encoding="utf-8")
            )
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"ok",
                stderr=b"",
            )

        command = [
            "/installed-agent/astra",
            "--session-id",
            "session-1",
            "--stdin",
        ]
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "retry.json"
            observed["report_path"] = report_path
            with (
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.subprocess.run",
                    side_effect=inspect_running_report,
                ),
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.sys.stdout",
                    SimpleNamespace(buffer=stdout),
                ),
                mock.patch(
                    "astra.runners.astra_terminal_bench."
                    "stream_transport_retry.sys.stderr",
                    SimpleNamespace(buffer=stderr),
                ),
            ):
                return_code = run_with_retries(
                    command,
                    b"original instruction\n",
                    max_retries=2,
                    report_path=report_path,
                    overall_deadline_seconds=1800,
                )

        self.assertEqual(return_code, 0)
        running = observed["report"]
        self.assertFalse(running["complete"])
        self.assertEqual(running["status"], "attempt_running")
        self.assertEqual(
            running["failure_classification"],
            "attempt_in_progress",
        )
        self.assertEqual(running["attempts"][0]["status"], "running")
        self.assertIsNone(running["attempts"][0]["finished_at_utc"])

    def test_attempt_report_has_timing_and_remaining_deadline(self):
        results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"ok",
                stderr=b"",
            )
        ]

        _return_code, _run, _stdout, _stderr, report = self._run(
            results,
            overall_deadline_seconds=1800,
        )

        attempt = report["attempts"][0]
        self.assertEqual(attempt["status"], "completed")
        self.assertTrue(attempt["started_at_utc"].endswith("Z"))
        self.assertTrue(attempt["finished_at_utc"].endswith("Z"))
        self.assertGreaterEqual(attempt["duration_seconds"], 0)
        self.assertGreater(
            attempt["remaining_deadline_seconds_at_start"],
            0,
        )
        self.assertGreater(
            attempt["remaining_deadline_seconds_at_finish"],
            0,
        )
        self.assertEqual(report["overall_deadline_seconds"], 1800)
        self.assertEqual(
            report["optional_retry_min_remaining_seconds"],
            600.0,
        )

    def test_deadline_policy_inputs_must_be_valid(self):
        command = [
            "/installed-agent/astra",
            "--session-id",
            "session-1",
        ]
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "retry.json"
            with self.assertRaisesRegex(
                ValueError,
                "overall_deadline_seconds must be positive",
            ):
                run_with_retries(
                    command,
                    b"",
                    max_retries=2,
                    report_path=report_path,
                    overall_deadline_seconds=0,
                )
            with self.assertRaisesRegex(
                ValueError,
                "optional_retry_min_remaining_seconds must be non-negative",
            ):
                run_with_retries(
                    command,
                    b"",
                    max_retries=2,
                    report_path=report_path,
                    optional_retry_min_remaining_seconds=-1,
                )

    def test_cli_exposes_deadline_policy(self):
        args = _parser().parse_args(
            [
                "--max-retries",
                "2",
                "--report",
                "/tmp/retry.json",
                "--overall-deadline-seconds",
                "1800",
                "--optional-retry-min-remaining-seconds",
                "600",
                "--",
                "astra",
                "--session-id",
                "session-1",
            ]
        )

        self.assertEqual(args.overall_deadline_seconds, 1800)
        self.assertEqual(args.optional_retry_min_remaining_seconds, 600)
        self.assertEqual(
            args.command,
            ["--", "astra", "--session-id", "session-1"],
        )


if __name__ == "__main__":
    unittest.main()
