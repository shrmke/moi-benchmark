#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from astra.runners.pi_terminal_bench.verifier_evidence import (
    VERIFIER_INFRA_EXCEPTION_TYPES,
    VerifierEvidenceError,
    validate_binary_reward,
    validate_ctrf_report,
)


EXPECTED_AGENT = (
    "astra.runners.pi_terminal_bench.agent:PiTerminalBenchC0Agent"
)
EXPECTED_MODEL = "zai/glm-5.2"
EXPECTED_VERSION = "0.73.1"
DEFAULT_REQUIRED_KWARGS = {"preinstalled": True}
TASK_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


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


def load_queue(path: Path) -> list[Task]:
    tasks: list[Task] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4 or not TASK_PATTERN.fullmatch(fields[0]):
            raise ValueError(f"invalid queue line {line_number}: {line!r}")
        task = Task(
            fields[0], float(fields[1]), int(fields[2]), int(fields[3])
        )
        if task.name in seen:
            raise ValueError(f"duplicate queued task: {task.name}")
        task.memory_tokens
        seen.add(task.name)
        tasks.append(task)
    return tasks


def completed_tasks(
    jobs_dir: Path,
    *,
    expected_agent: str = EXPECTED_AGENT,
    expected_model: str | None = EXPECTED_MODEL,
    expected_version: str | None = EXPECTED_VERSION,
    required_kwargs: Mapping[str, object] | None = None,
) -> set[str]:
    if required_kwargs is None:
        required_kwargs = DEFAULT_REQUIRED_KWARGS
    if not jobs_dir.is_dir():
        return set()
    latest: dict[str, tuple[str, Path, dict]] = {}
    for path in jobs_dir.glob("*/*/result.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            trial_config = result.get("config") or {}
            agent = trial_config.get("agent") or {}
            kwargs = agent.get("kwargs") or {}
            task = result["task_name"].rsplit("/", 1)[-1]
            finished_at = result.get("finished_at")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if (
            finished_at
            and trial_config.get("install_only") is not True
            and agent.get("name") == expected_agent
            and (
                expected_model is None
                or agent.get("model_name") == expected_model
            )
            and (
                expected_version is None
                or kwargs.get("version") == expected_version
            )
            and all(
                kwargs.get(key) == value
                for key, value in required_kwargs.items()
            )
        ):
            candidate = (str(finished_at), path, result)
            previous = latest.get(task)
            if previous is None or (candidate[0], str(candidate[1])) > (
                previous[0],
                str(previous[1]),
            ):
                latest[task] = candidate
    return {
        task
        for task, (_, path, result) in latest.items()
        if has_valid_verifier_result(result, path)
    }


def has_valid_verifier_result(result: dict, result_path: Path) -> bool:
    exception = result.get("exception_info")
    exception_type = (
        exception.get("exception_type") if isinstance(exception, dict) else None
    )
    if exception_type in VERIFIER_INFRA_EXCEPTION_TYPES:
        return False
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get(
        "reward"
    )
    try:
        validate_binary_reward(reward)
    except VerifierEvidenceError:
        return False
    try:
        validate_ctrf_report(result_path.parent / "verifier" / "ctrf.json")
    except VerifierEvidenceError:
        return False
    return True


def parse_cohort_kwargs(values: Sequence[str] | None) -> dict[str, object]:
    if values is None:
        return dict(DEFAULT_REQUIRED_KWARGS)
    result: dict[str, object] = {}
    for value in values:
        key, separator, encoded = value.partition("=")
        if not separator or not key or key in result:
            raise ValueError(f"invalid cohort kwarg: {value!r}")
        try:
            result[key] = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON value in cohort kwarg: {value!r}"
            ) from exc
    return result


async def run_tasks(
    tasks: list[Task],
    *,
    harbor_bin: str,
    config: Path,
    jobs_dir: Path,
    generated_root: Path,
    workspace_root: Path,
    max_workers: int | None = None,
    job_name_prefix: str | None = None,
) -> int:
    available_memory = 3
    available_cpus = 6
    pending = list(tasks)
    running: dict[asyncio.Task[int], Task] = {}
    status = 0

    async def execute(task: Task) -> int:
        env = dict(os.environ)
        current_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(workspace_root) + (
            f":{current_pythonpath}" if current_pythonpath else ""
        )
        command = [
            harbor_bin,
            "run",
            "--config",
            str(config),
            "--jobs-dir",
            str(jobs_dir),
            "--path",
            str(generated_root / task.name),
            "--no-force-build",
            "--yes",
        ]
        if job_name_prefix is not None:
            command.extend(["--job-name", f"{job_name_prefix}-{task.name}"])
        process = await asyncio.create_subprocess_exec(*command, env=env)
        return await process.wait()

    while pending or running:
        launched = False
        for task in list(pending):
            if (
                task.memory_tokens <= available_memory
                and task.cpus <= available_cpus
                and (max_workers is None or len(running) < max_workers)
            ):
                available_memory -= task.memory_tokens
                available_cpus -= task.cpus
                pending.remove(task)
                future = asyncio.create_task(execute(task))
                running[future] = task
                print(
                    f"started {task.name}: memory={task.memory_mb}MB "
                    f"cpus={task.cpus}",
                    flush=True,
                )
                launched = True
                if task.memory_tokens == 3:
                    break
        if launched and pending:
            continue
        if not running:
            raise RuntimeError("resource queue cannot schedule its next task")
        done, _ = await asyncio.wait(
            running, return_when=asyncio.FIRST_COMPLETED
        )
        for future in done:
            task = running.pop(future)
            return_code = future.result()
            available_memory += task.memory_tokens
            available_cpus += task.cpus
            print(
                f"finished {task.name}: return_code={return_code}",
                flush=True,
            )
            if return_code != 0:
                status = 1
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run prebuilt Pi tasks with 8GB-aware bin packing"
    )
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument(
        "--generated-root",
        "--tasks-root",
        dest="generated_root",
        type=Path,
        required=True,
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--harbor-bin", required=True)
    parser.add_argument("--print-pending", action="store_true")
    parser.add_argument("--rerun-completed", action="store_true")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--job-name-prefix")
    parser.add_argument("--expected-agent", default=EXPECTED_AGENT)
    parser.add_argument("--expected-model", default=EXPECTED_MODEL)
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--ignore-model-name", action="store_true")
    parser.add_argument("--ignore-agent-version", action="store_true")
    parser.add_argument(
        "--cohort-kwarg",
        action="append",
        help="required agent kwarg as KEY=JSON; replaces the Pi default",
    )
    args = parser.parse_args()
    tasks = load_queue(args.queue)
    try:
        required_kwargs = parse_cohort_kwargs(args.cohort_kwarg)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.rerun_completed:
        completed = completed_tasks(
            args.jobs_dir,
            expected_agent=args.expected_agent,
            expected_model=(None if args.ignore_model_name else args.expected_model),
            expected_version=(
                None if args.ignore_agent_version else args.expected_version
            ),
            required_kwargs=required_kwargs,
        )
        tasks = [task for task in tasks if task.name not in completed]
    if args.max_tasks is not None:
        if args.max_tasks <= 0:
            parser.error("--max-tasks must be positive")
        tasks = tasks[: args.max_tasks]
    if args.max_workers is not None and args.max_workers <= 0:
        parser.error("--max-workers must be positive")
    if args.print_pending:
        for task in tasks:
            print(
                f"{task.name}\t{task.timeout_sec:g}\t"
                f"{task.memory_mb}\t{task.cpus}"
            )
        return 0
    missing = [
        task.name
        for task in tasks
        if not (args.generated_root / task.name / "task.toml").is_file()
    ]
    if missing:
        parser.error(
            "generated tasks are missing before scheduling: "
            + ", ".join(missing[:5])
        )
    return asyncio.run(
        run_tasks(
            tasks,
            harbor_bin=args.harbor_bin,
            config=args.config,
            jobs_dir=args.jobs_dir,
            generated_root=args.generated_root,
            workspace_root=args.workspace_root,
            max_workers=args.max_workers,
            job_name_prefix=args.job_name_prefix,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
