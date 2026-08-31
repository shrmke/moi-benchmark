from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astra.runners.pi_terminal_bench.prebuilt.resource_queue import build_queue


class PiResourceQueueTests(unittest.TestCase):
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

    def test_excludes_tune_mjcf_from_complete_source_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_names = [f"task-{index:03d}" for index in range(88)]
            task_names.append("tune-mjcf")
            for task_name in task_names:
                self._write_task(root, task_name)

            rows = build_queue(root)

        self.assertEqual(len(rows), 88)
        self.assertNotIn("tune-mjcf", {row[0] for row in rows})

    def test_requires_excluded_task_in_source_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(89):
                self._write_task(root, f"task-{index:03d}")

            with self.assertRaisesRegex(
                RuntimeError,
                "excluded Pi tasks are missing.*tune-mjcf",
            ):
                build_queue(root)

    def test_can_select_named_tasks_without_pi_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(89):
                self._write_task(root, f"task-{index:03d}")

            rows = build_queue(
                root,
                excluded_tasks=frozenset(),
                included_tasks=frozenset({"task-003", "task-017"}),
            )

        self.assertEqual({row[0] for row in rows}, {"task-003", "task-017"})
