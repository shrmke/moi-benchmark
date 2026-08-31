from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astra.runners.pi_terminal_bench.prebuilt.schedule import (
    Task,
    completed_tasks,
    load_queue,
    run_tasks,
)


class _Process:
    def __init__(
        self,
        task: str,
        active: set[str],
        snapshots: list[set[str]],
        return_code: int = 0,
    ):
        self.task = task
        self.active = active
        self.snapshots = snapshots
        self.return_code = return_code

    async def wait(self) -> int:
        self.active.add(self.task)
        self.snapshots.append(set(self.active))
        await asyncio.sleep(0)
        self.active.remove(self.task)
        return self.return_code


class PiScheduleTests(unittest.IsolatedAsyncioTestCase):
    def _write_result(
        self,
        jobs_dir: Path,
        *,
        reward: float = 0.0,
        with_ctrf: bool,
        exception: dict | None = None,
        trial_name: str = "task__trial",
        finished_at: str = "2026-08-12T00:00:00Z",
    ) -> None:
        trial_dir = jobs_dir / "job" / trial_name
        trial_dir.mkdir(parents=True)
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/task",
                    "config": {
                        "install_only": False,
                        "agent": {
                            "name": (
                                "astra.runners.pi_terminal_bench.agent:"
                                "PiTerminalBenchC0Agent"
                            ),
                            "model_name": "zai/glm-5.2",
                            "kwargs": {
                                "version": "0.73.1",
                                "preinstalled": True,
                            },
                        },
                    },
                    "verifier_result": {"rewards": {"reward": reward}},
                    "exception_info": exception,
                    "finished_at": finished_at,
                }
            ),
            encoding="utf-8",
        )
        if with_ctrf:
            verifier_dir = trial_dir / "verifier"
            verifier_dir.mkdir()
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

    def test_completed_tasks_requires_verifier_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(jobs_dir, with_ctrf=False)

            self.assertEqual(completed_tasks(jobs_dir), set())

    def test_completed_tasks_accepts_real_failed_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(jobs_dir, with_ctrf=True)

            self.assertEqual(completed_tasks(jobs_dir), {"task"})

    def test_completed_tasks_keeps_valid_verifier_after_agent_exception(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(
                jobs_dir,
                with_ctrf=True,
                exception={"exception_type": "AgentTimeoutError"},
            )

            self.assertEqual(completed_tasks(jobs_dir), {"task"})

    def test_completed_tasks_retries_verifier_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(
                jobs_dir,
                with_ctrf=True,
                exception={"exception_type": "VerifierInfrastructureError"},
            )

            self.assertEqual(completed_tasks(jobs_dir), set())

    def test_completed_tasks_uses_latest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(
                jobs_dir,
                with_ctrf=True,
                trial_name="task__old",
                finished_at="2026-08-12T00:00:00Z",
            )
            self._write_result(
                jobs_dir,
                with_ctrf=False,
                trial_name="task__new",
                finished_at="2026-08-12T01:00:00Z",
            )

            self.assertEqual(completed_tasks(jobs_dir), set())

    def test_completed_tasks_supports_astra_environment_model_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            self._write_result(jobs_dir, with_ctrf=True)
            result_path = jobs_dir / "job" / "task__trial" / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["config"]["agent"] = {
                "name": (
                    "astra.runners.astra_terminal_bench.agent:"
                    "AstraTerminalBenchC0Agent"
                ),
                "model_name": None,
                "kwargs": {
                    "max_turns": 50,
                    "product_timeout_multiplier": 1.0,
                },
            }
            result_path.write_text(json.dumps(result), encoding="utf-8")

            completed = completed_tasks(
                jobs_dir,
                expected_agent=(
                    "astra.runners.astra_terminal_bench.agent:"
                    "AstraTerminalBenchC0Agent"
                ),
                expected_model=None,
                expected_version=None,
                required_kwargs={
                    "max_turns": 50,
                    "product_timeout_multiplier": 1.0,
                },
            )

            self.assertEqual(completed, {"task"})

    def test_direct_schedule_entrypoint_resolves_workspace_imports(self) -> None:
        script = Path(__file__).resolve().parents[1] / "prebuilt" / "schedule.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        process = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(process.returncode, 0, process.stderr)

    def test_rerun_completed_prints_tasks_with_existing_results(self) -> None:
        script = Path(__file__).resolve().parents[1] / "prebuilt" / "schedule.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "jobs"
            self._write_result(jobs_dir, with_ctrf=True)
            queue = root / "queue.tsv"
            queue.write_text("task\t10\t2048\t1\n", encoding="utf-8")
            process = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--queue",
                    str(queue),
                    "--jobs-dir",
                    str(jobs_dir),
                    "--generated-root",
                    str(root / "generated"),
                    "--config",
                    str(root / "config.yaml"),
                    "--workspace-root",
                    str(root),
                    "--harbor-bin",
                    "harbor",
                    "--print-pending",
                    "--rerun-completed",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, "task\t10\t2048\t1\n")

    def test_load_queue_rejects_unsupported_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.tsv"
            path.write_text("task\t10\t6144\t1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported declared memory"):
                load_queue(path)

    async def test_eight_gb_task_never_overlaps(self) -> None:
        tasks = [
            Task("heavy", 10, 8192, 4),
            Task("small-a", 10, 2048, 1),
            Task("small-b", 10, 2048, 1),
        ]
        active: set[str] = set()
        snapshots: list[set[str]] = []

        async def create_process(*args, **kwargs):
            task = Path(args[7]).name
            return _Process(task, active, snapshots)

        with tempfile.TemporaryDirectory() as directory, patch(
            "asyncio.create_subprocess_exec", side_effect=create_process
        ):
            root = Path(directory)
            status = await run_tasks(
                tasks,
                harbor_bin="harbor",
                config=root / "config.yaml",
                jobs_dir=root / "jobs",
                generated_root=root,
                workspace_root=root,
            )

        self.assertEqual(status, 0)
        self.assertIn({"heavy"}, snapshots)
        self.assertTrue(
            all("heavy" not in state or state == {"heavy"} for state in snapshots)
        )

    async def test_nonzero_case_does_not_interrupt_remaining_queue(self) -> None:
        tasks = [
            Task("fails", 10, 8192, 4),
            Task("continues", 10, 8192, 4),
        ]
        active: set[str] = set()
        snapshots: list[set[str]] = []
        started: list[str] = []

        async def create_process(*args, **kwargs):
            task = Path(args[7]).name
            started.append(task)
            return _Process(
                task,
                active,
                snapshots,
                return_code=17 if task == "fails" else 0,
            )

        with tempfile.TemporaryDirectory() as directory, patch(
            "asyncio.create_subprocess_exec", side_effect=create_process
        ):
            root = Path(directory)
            status = await run_tasks(
                tasks,
                harbor_bin="harbor",
                config=root / "config.yaml",
                jobs_dir=root / "jobs",
                generated_root=root,
                workspace_root=root,
            )

        self.assertEqual(status, 1)
        self.assertEqual(started, ["fails", "continues"])

    async def test_max_workers_caps_process_concurrency(self) -> None:
        tasks = [
            Task("small-a", 10, 2048, 1),
            Task("small-b", 10, 2048, 1),
            Task("small-c", 10, 2048, 1),
        ]
        active: set[str] = set()
        snapshots: list[set[str]] = []

        async def create_process(*args, **kwargs):
            task = Path(args[7]).name
            return _Process(task, active, snapshots)

        with tempfile.TemporaryDirectory() as directory, patch(
            "asyncio.create_subprocess_exec", side_effect=create_process
        ):
            root = Path(directory)
            status = await run_tasks(
                tasks,
                harbor_bin="harbor",
                config=root / "config.yaml",
                jobs_dir=root / "jobs",
                generated_root=root,
                workspace_root=root,
                max_workers=2,
            )

        self.assertEqual(status, 0)
        self.assertTrue(all(len(state) <= 2 for state in snapshots))
