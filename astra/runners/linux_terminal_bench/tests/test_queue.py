from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astra.runners.linux_terminal_bench.queue import build_queue, load_queue, write_queue


class LinuxQueueTests(unittest.TestCase):
    @staticmethod
    def _write_task(root: Path, name: str, *, memory: int = 2048) -> None:
        task = root / name
        task.mkdir()
        (task / "task.toml").write_text(
            "[verifier]\n"
            "timeout_sec = 60.0\n"
            "[agent]\n"
            "timeout_sec = 900.0\n"
            "[environment]\n"
            f"memory_mb = {memory}\n"
            "cpus = 1\n",
            encoding="utf-8",
        )

    def test_full_queue_contains_all_89_tasks_and_tune_mjcf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(88):
                self._write_task(root, f"task-{index:03d}")
            self._write_task(root, "tune-mjcf", memory=8192)

            tasks = build_queue(root)

        self.assertEqual(len(tasks), 89)
        self.assertEqual(tasks[0].name, "tune-mjcf")

    def test_queue_round_trip_preserves_resource_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(88):
                self._write_task(root, f"task-{index:03d}")
            self._write_task(root, "tune-mjcf", memory=8192)
            queue = root / "queue.tsv"
            expected = build_queue(root)

            write_queue(queue, expected)

            self.assertEqual(load_queue(queue), expected)


if __name__ == "__main__":
    unittest.main()
