from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.pi_terminal_bench.verifier_evidence import (
    VerifierEvidenceError,
    validate_binary_reward,
    validate_ctrf_report,
)


class VerifierEvidenceTests(unittest.TestCase):
    @staticmethod
    def _report(path: Path, trace: str, output: str, status: str = "failed") -> None:
        path.write_text(json.dumps({"results": {
            "summary": {"tests": 1},
            "tests": [{"name": "test", "status": status, "trace": trace}],
        }}), encoding="utf-8")
        (path.parent / "test-stdout.txt").write_text(output, encoding="utf-8")

    def test_rejects_ocaml_assertion_after_testsuite_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            self._report(path,
                "/app/ocaml/tests.txt\nE       assert '40 tests passed' in ''",
                "fatal: unable to access 'https://github.com/sadiqj/ocaml/': GnuTLS recv error (-110)\n"
                "cp: cannot stat 'ocaml-original/testsuite': No such file or directory",
            )
            with self.assertRaisesRegex(VerifierEvidenceError, "OCaml testsuite download failed"):
                validate_ctrf_report(path)

    def test_rejects_c4_fixture_network_failure_with_missing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            self._report(path,
                "def generate_test_data():\nload_dataset(\n"
                "ValueError: Couldn't find cache for allenai/c4",
                "Captured stderr setup\nNetwork is unreachable "
                "https://huggingface.co/datasets/allenai/c4/resolve/revision/c4.py",
            )
            with self.assertRaisesRegex(VerifierEvidenceError, "C4 test-data fixture"):
                validate_ctrf_report(path)

    def test_does_not_reclassify_missing_deliverables_or_runtime_errors(self) -> None:
        for trace in (
            "FileNotFoundError: /app/ars.R",
            "AssertionError: VNC service not listening on port 5901",
            "Error in library(rstan): there is no package called rstan",
            "TypeError: cannot unpack non-iterable NoneType object",
            "AssertionError: Failed on some testsFailed vectors",
        ):
            with self.subTest(trace=trace), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ctrf.json"
                self._report(path, trace,
                    "Connection refused\nNetwork is unreachable\nALERT DETECTED!\n"
                    "fatal: unable to access 'https://github.com/sadiqj/ocaml/': GnuTLS recv error\n"
                    "cp: cannot stat 'ocaml-original/testsuite': No such file or directory",
                )
                self.assertEqual(validate_ctrf_report(path), {"test_count": 1})

    def test_does_not_reject_success_after_transient_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            self._report(path,
                "def generate_test_data():\nload_dataset(\nCouldn't find cache for allenai/c4",
                "Captured stderr setup\nNetwork is unreachable "
                "https://huggingface.co/datasets/allenai/c4/resolve/revision/c4.py",
                status="passed",
            )
            self.assertEqual(validate_ctrf_report(path), {"test_count": 1})

    def test_requires_causal_logs_not_just_empty_output_or_missing_cache(self) -> None:
        for trace in (
            "/app/ocaml/tests.txt\nE       assert '40 tests passed' in ''",
            "def generate_test_data():\nload_dataset(\nCouldn't find cache for allenai/c4",
        ):
            with self.subTest(trace=trace), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ctrf.json"
                self._report(path, trace, "")
                self.assertEqual(validate_ctrf_report(path), {"test_count": 1})

    def test_accepts_binary_rewards(self) -> None:
        self.assertEqual(validate_binary_reward(0), 0.0)
        self.assertEqual(validate_binary_reward(1.0), 1.0)

    def test_rejects_non_binary_or_non_finite_rewards(self) -> None:
        for value in (True, -1, 0.5, 2, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(
                VerifierEvidenceError, "finite number 0 or 1"
            ):
                validate_binary_reward(value)

    def test_accepts_nonempty_ctrf_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            path.write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [{"name": "test_answer", "status": "failed"}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = validate_ctrf_report(path)

        self.assertEqual(report, {"test_count": 1})

    def test_rejects_missing_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                VerifierEvidenceError, "did not produce ctrf.json"
            ):
                validate_ctrf_report(Path(directory) / "ctrf.json")

    def test_rejects_zero_test_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            path.write_text(
                json.dumps(
                    {"results": {"summary": {"tests": 0}, "tests": []}}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                VerifierEvidenceError, "any tests were executed"
            ):
                validate_ctrf_report(path)

    def test_rejects_malformed_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(
                VerifierEvidenceError, "unreadable ctrf.json"
            ):
                validate_ctrf_report(path)

    def test_rejects_inconsistent_test_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            path.write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 2},
                            "tests": [
                                {"name": "test_answer", "status": "passed"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                VerifierEvidenceError, "any tests were executed"
            ):
                validate_ctrf_report(path)

    def test_rejects_skipped_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctrf.json"
            path.write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [
                                {"name": "test_answer", "status": "skipped"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                VerifierEvidenceError, "any test completed"
            ):
                validate_ctrf_report(path)
