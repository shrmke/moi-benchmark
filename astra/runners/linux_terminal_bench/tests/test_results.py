from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.linux_terminal_bench.products import PRODUCTS
from astra.runners.linux_terminal_bench.results import completed_tasks


class LinuxResultTests(unittest.TestCase):
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
