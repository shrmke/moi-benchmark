from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


EXPECTED_TASKS = 89
TASK_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SECTION_PATTERN = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)]\s*$")
NUMBER_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


@dataclass(frozen=True)
class Task:
    name: str
    timeout_sec: float
    memory_mb: int
    cpus: int

    @property
    def memory_tokens(self) -> int:
        if self.memory_mb <= 2048:
            return 1
        if self.memory_mb <= 4096:
            return 2
        if self.memory_mb == 8192:
            return 3
        raise ValueError(
            f"unsupported declared memory for {self.name}: {self.memory_mb}"
        )

    def as_tsv(self) -> str:
        return (
            f"{self.name}\t{self.timeout_sec:g}\t"
            f"{self.memory_mb}\t{self.cpus}\n"
        )


def _resource_values(text: str, path: Path) -> tuple[str, str, str]:
    section = ""
    values: dict[tuple[str, str], str] = {}
    wanted = {
        ("agent", "timeout_sec"),
        ("environment", "memory_mb"),
        ("environment", "cpus"),
    }
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = SECTION_PATTERN.fullmatch(line)
        if match:
            section = match.group(1)
            continue
        key, separator, value = line.partition("=")
        identity = (section, key.strip())
        if separator and identity in wanted:
            value = value.strip()
            if identity in values or not NUMBER_PATTERN.fullmatch(value):
                raise ValueError(f"invalid {section}.{key.strip()} in {path}")
            values[identity] = value
    missing = wanted - values.keys()
    if missing:
        formatted = ", ".join(f"{section}.{key}" for section, key in sorted(missing))
        raise ValueError(f"missing task resources in {path}: {formatted}")
    return (
        values[("agent", "timeout_sec")],
        values[("environment", "memory_mb")],
        values[("environment", "cpus")],
    )


def read_task(path: Path) -> Task:
    if not TASK_PATTERN.fullmatch(path.name):
        raise ValueError(f"invalid task directory name: {path.name!r}")
    config_path = path / "task.toml"
    text = config_path.read_text(encoding="utf-8")
    timeout_sec, memory_mb, cpus = _resource_values(text, config_path)
    task = Task(
        name=path.name,
        timeout_sec=float(timeout_sec),
        memory_mb=int(memory_mb),
        cpus=int(cpus),
    )
    if task.timeout_sec <= 0 or task.cpus <= 0:
        raise ValueError(f"task resources must be positive: {path.name}")
    task.memory_tokens
    return task


def build_queue(
    tasks_root: Path,
    *,
    included_tasks: frozenset[str] | None = None,
) -> list[Task]:
    tasks = [
        read_task(path)
        for path in tasks_root.iterdir()
        if (path / "task.toml").is_file()
    ]
    if len(tasks) != EXPECTED_TASKS:
        raise ValueError(
            f"expected {EXPECTED_TASKS} Terminal-Bench tasks, found {len(tasks)}"
        )
    names = {task.name for task in tasks}
    if "tune-mjcf" not in names:
        raise ValueError("the 89-task Linux cohort must contain tune-mjcf")
    if included_tasks is not None:
        missing = included_tasks - names
        if missing:
            raise ValueError("unknown requested tasks: " + ", ".join(sorted(missing)))
        tasks = [task for task in tasks if task.name in included_tasks]
    return sorted(
        tasks,
        key=lambda task: (-task.memory_mb, -task.timeout_sec, -task.cpus, task.name),
    )


def load_queue(path: Path) -> list[Task]:
    tasks: list[Task] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4 or not TASK_PATTERN.fullmatch(fields[0]):
            raise ValueError(f"invalid queue line {line_number}: {line!r}")
        task = Task(fields[0], float(fields[1]), int(fields[2]), int(fields[3]))
        if task.name in seen:
            raise ValueError(f"duplicate queued task: {task.name}")
        task.memory_tokens
        seen.add(task.name)
        tasks.append(task)
    return tasks


def write_queue(path: Path, tasks: list[Task]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(task.as_tsv() for task in tasks), encoding="utf-8")
