from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astra.runners.linux_terminal_bench.queue import Task
from astra.runners.linux_terminal_bench.run import _execute_tasks
from astra.runners.linux_terminal_bench.scheduler import run_tasks


class LinuxSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_eight_gb_task_is_isolated_and_small_tasks_overlap(self) -> None:
        tasks = [
            Task("heavy", 10, 8192, 4),
            Task("small-a", 10, 2048, 1),
            Task("small-b", 10, 2048, 1),
        ]
        active: set[str] = set()
        snapshots: list[set[str]] = []

        async def execute(task: Task) -> int:
            active.add(task.name)
            snapshots.append(set(active))
            await asyncio.sleep(0.01)
            active.remove(task.name)
            return 0

        status = await run_tasks(tasks, execute, max_workers=3)

        self.assertEqual(status, 0)
        self.assertNotIn("heavy", set().union(*(s for s in snapshots if len(s) > 1)))
        self.assertIn({"small-a", "small-b"}, snapshots)

    async def test_parallel_harbor_jobs_receive_unique_names(self) -> None:
        tasks = [
            Task("small-a", 10, 2048, 1),
            Task("small-b", 10, 2048, 1),
        ]
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        with tempfile.TemporaryDirectory() as directory, patch(
            "astra.runners.linux_terminal_bench.run.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create:
            root = Path(directory)
            status = await _execute_tasks(
                tasks,
                product=SimpleNamespace(
                    id="dsh",
                    prebuilt=None,
                    config_path=root / "dsh.yaml",
                ),
                root=root,
                tasks_root=root / "tasks",
                generated_root=root / "generated",
                jobs_dir=root / "jobs",
                harbor_bin="harbor",
                env={},
                max_workers=3,
            )

        self.assertEqual(status, 0)
        names = []
        for invocation in create.await_args_list:
            command = list(invocation.args)
            names.append(command[command.index("--job-name") + 1])
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(any(name.endswith("__small-a") for name in names))
        self.assertTrue(any(name.endswith("__small-b") for name in names))

    async def test_hermes_prepares_each_image_immediately_before_harbor(
        self,
    ) -> None:
        tasks = [
            Task("small-a", 10, 2048, 1),
            Task("small-b", 10, 2048, 1),
        ]
        events: list[str] = []

        def prepare(_product, prepared, **_kwargs) -> None:
            self.assertEqual(len(prepared), 1)
            events.append(f"prepare:{prepared[0].name}")

        async def create_process(*command, **_kwargs):
            command = list(command)
            task_name = Path(command[command.index("--path") + 1]).name
            events.append(f"harbor:{task_name}")
            return SimpleNamespace(wait=AsyncMock(return_value=0))

        with tempfile.TemporaryDirectory() as directory, patch(
            "astra.runners.linux_terminal_bench.run._prepare_images",
            side_effect=prepare,
        ), patch(
            "astra.runners.linux_terminal_bench.run.asyncio.create_subprocess_exec",
            side_effect=create_process,
        ):
            root = Path(directory)
            status = await _execute_tasks(
                tasks,
                product=SimpleNamespace(
                    id="hermes",
                    prebuilt="hermes",
                    config_path=root / "hermes.yaml",
                ),
                root=root,
                tasks_root=root / "tasks",
                generated_root=root / "generated",
                jobs_dir=root / "jobs",
                harbor_bin="harbor",
                env={},
                max_workers=3,
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            events,
            [
                "prepare:small-a",
                "harbor:small-a",
                "prepare:small-b",
                "harbor:small-b",
            ],
        )


if __name__ == "__main__":
    unittest.main()
