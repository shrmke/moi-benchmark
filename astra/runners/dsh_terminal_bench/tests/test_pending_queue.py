from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.pi_terminal_bench.prebuilt.resource_queue import build_queue
from astra.runners.pi_terminal_bench.prebuilt.schedule import completed_tasks


DSH_AGENT = (
    "astra.runners.dsh_terminal_bench.agent:DshTerminalBenchC0Agent"
)


class DshPendingQueueTests(unittest.TestCase):
    @staticmethod
    def _write_task(root: Path, task_name: str) -> None:
        task_dir = root / task_name
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(
            "[agent]\n"
            "timeout_sec = 900.0\n"
            "[environment]\n"
            "memory_mb = 2048\n"
            "cpus = 1\n",
            encoding="utf-8",
        )

    def test_dsh_queue_reuses_pi_tune_mjcf_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_names = [f"task-{index:03d}" for index in range(88)]
            task_names.append("tune-mjcf")
            for task_name in task_names:
                self._write_task(root, task_name)

            rows = build_queue(root)

        self.assertEqual(len(rows), 88)
        self.assertNotIn("tune-mjcf", {row[0] for row in rows})

    def test_completed_tasks_uses_dsh_profile_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            trial_dir = jobs_dir / "job" / "task__trial"
            verifier_dir = trial_dir / "verifier"
            verifier_dir.mkdir(parents=True)
            (trial_dir / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": "terminal-bench/task",
                        "config": {
                            "install_only": False,
                            "agent": {
                                "name": DSH_AGENT,
                                "model_name": "zai/glm-5.2",
                                "kwargs": {
                                    "version": "0.1.0rc6",
                                    "profile": "terminalbench-glm52",
                                },
                            },
                        },
                        "verifier_result": {"rewards": {"reward": 0.0}},
                        "exception_info": None,
                        "finished_at": "2026-08-17T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (verifier_dir / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [
                                {"name": "test_answer", "status": "failed"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = completed_tasks(
                jobs_dir,
                expected_agent=DSH_AGENT,
                expected_model="zai/glm-5.2",
                expected_version="0.1.0rc6",
                required_kwargs={"profile": "terminalbench-glm52"},
            )

        self.assertEqual(completed, {"task"})

    def test_flash_max_cohort_does_not_reuse_glm52_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            trial_dir = jobs_dir / "job" / "task__trial"
            verifier_dir = trial_dir / "verifier"
            verifier_dir.mkdir(parents=True)
            (trial_dir / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": "terminal-bench/task",
                        "config": {
                            "install_only": False,
                            "agent": {
                                "name": DSH_AGENT,
                                "model_name": "zai/glm-5.2",
                                "kwargs": {
                                    "version": "0.1.0rc6",
                                    "profile": "terminalbench-glm52",
                                },
                            },
                        },
                        "verifier_result": {"rewards": {"reward": 1.0}},
                        "exception_info": None,
                        "finished_at": "2026-08-17T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (verifier_dir / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [
                                {"name": "test_answer", "status": "passed"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = completed_tasks(
                jobs_dir,
                expected_agent=DSH_AGENT,
                expected_model="deepseek-official/deepseek-v4-flash",
                expected_version="0.1.0rc6",
                required_kwargs={
                    "profile": "terminalbench-deepseek-v4-flash-max"
                },
            )

        self.assertEqual(completed, set())


if __name__ == "__main__":
    unittest.main()
