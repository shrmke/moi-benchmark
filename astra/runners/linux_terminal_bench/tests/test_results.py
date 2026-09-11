from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astra.runners.linux_terminal_bench.products import PRODUCTS
from astra.runners.linux_terminal_bench.results import completed_tasks, summarize, verifier_status


class LinuxResultTests(unittest.TestCase):
    def test_verifier_setup_failure_is_pending_for_every_product(self) -> None:
        for product_id, product in PRODUCTS.items():
            with self.subTest(product=product_id), tempfile.TemporaryDirectory() as directory:
                jobs = Path(directory) / "jobs"
                self._write_result(
                    jobs, multiplier=1.0, with_ctrf=True, trial="trial",
                    finished_at="2026-01-01T00:00:00Z", product_id=product_id,
                )
                result_path = jobs / "job/trial/result.json"
                verifier = result_path.parent / "verifier"
                ctrf_path = verifier / "ctrf.json"
                ctrf = json.loads(ctrf_path.read_text())
                ctrf["results"]["tests"][0]["trace"] = (
                    "/app/ocaml/tests.txt\nE       assert '40 tests passed' in ''"
                )
                ctrf_path.write_text(json.dumps(ctrf), encoding="utf-8")
                (verifier / "test-stdout.txt").write_text(
                    "fatal: unable to access 'https://github.com/sadiqj/ocaml/': GnuTLS recv error\n"
                    "cp: cannot stat 'ocaml-original/testsuite': No such file or directory",
                    encoding="utf-8",
                )
                original = result_path.read_bytes()
                self.assertEqual(completed_tasks(jobs, {"task"}, product), set())
                with patch("astra.runners.linux_terminal_bench.results._audit_status",
                           return_value=("not_checked", "")):
                    summary = summarize(
                        product=product, jobs_dir=jobs, tasks=["task"],
                        output_dir=Path(directory) / "analysis", dataset_commit="test",
                    )
                self.assertEqual(summary["pending"], ["task"])
                self.assertEqual(summary["failed_tasks"], 0)
                self.assertEqual(summary["valid_verifier_tasks"], 0)
                self.assertEqual(summary["verifier_infra_failure_tasks"], 1)
                self.assertEqual(summary["results"][0]["raw_reward"], 0)
                self.assertIsNone(summary["results"][0]["reward"])
                self.assertEqual(result_path.read_bytes(), original)
                self.assertEqual(
                    (Path(directory) / "analysis/pending.queue.txt").read_text(), "task\n"
                )

                # The latest valid rerun must replace the old invalid attempt.
                self._write_result(
                    jobs, multiplier=1.0, with_ctrf=True, trial="rerun",
                    finished_at="2026-01-02T00:00:00Z", product_id=product_id,
                )
                self.assertEqual(completed_tasks(jobs, {"task"}, product), {"task"})

    def test_cancellation_without_reward_is_not_verifier_infrastructure(self) -> None:
        result = {"exception_info": {"exception_type": "CancelledError"}}
        status = verifier_status(result, Path("unused/result.json"))
        self.assertEqual(status[0], "execution_incomplete")
        self.assertIsNone(status[1])

    @staticmethod
    def _write_result(
        jobs: Path,
        *,
        multiplier: float,
        with_ctrf: bool,
        trial: str,
        finished_at: str,
        product_id: str = "pi",
        dsh_finish_reason: str | None = None,
    ) -> None:
        product = PRODUCTS[product_id]
        trial_dir = jobs / "job" / trial
        trial_dir.mkdir(parents=True)
        payload = {
            "task_name": "terminal-bench/task",
            "finished_at": finished_at,
            "config": {
                "agent": {
                    "name": product.agent,
                    "model_name": product.model,
                    "kwargs": {
                        **product.required_kwargs,
                        "product_timeout_multiplier": multiplier,
                    },
                }
            },
            "verifier_result": {"rewards": {"reward": 0}},
        }
        if dsh_finish_reason is not None:
            payload["agent_result"] = {
                "metadata": {
                    "product": "dsh",
                    "dsh_finish_reason": dsh_finish_reason,
                }
            }
        (trial_dir / "result.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        if with_ctrf:
            verifier = trial_dir / "verifier"
            verifier.mkdir()
            (verifier / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [{"name": "test", "status": "failed"}],
                        }
                    }
                ),
                encoding="utf-8",
            )

    def test_resume_only_accepts_latest_valid_one_x_cohort_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            self._write_result(
                jobs,
                multiplier=2.0,
                with_ctrf=True,
                trial="old",
                finished_at="2026-01-01T00:00:00Z",
            )
            self.assertEqual(completed_tasks(jobs, {"task"}, PRODUCTS["pi"]), set())

            self._write_result(
                jobs,
                multiplier=1.0,
                with_ctrf=True,
                trial="new",
                finished_at="2026-01-02T00:00:00Z",
            )
            self.assertEqual(
                completed_tasks(jobs, {"task"}, PRODUCTS["pi"]), {"task"}
            )

    def test_missing_ctrf_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            self._write_result(
                jobs,
                multiplier=1.0,
                with_ctrf=False,
                trial="trial",
                finished_at="2026-01-01T00:00:00Z",
            )
            self.assertEqual(completed_tasks(jobs, {"task"}, PRODUCTS["pi"]), set())

    def test_dsh_error_finish_reason_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            self._write_result(
                jobs,
                multiplier=1.0,
                with_ctrf=True,
                trial="trial",
                finished_at="2026-01-01T00:00:00Z",
                product_id="dsh",
                dsh_finish_reason="error",
            )
            self.assertEqual(
                completed_tasks(jobs, {"task"}, PRODUCTS["dsh"]), set()
            )


if __name__ == "__main__":
    unittest.main()
