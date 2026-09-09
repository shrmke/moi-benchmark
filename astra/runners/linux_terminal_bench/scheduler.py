from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .queue import Task


TaskExecutor = Callable[[Task], Awaitable[int]]


async def run_tasks(
    tasks: list[Task],
    execute: TaskExecutor,
    *,
    max_workers: int = 3,
) -> int:
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    available_memory = 3
    available_cpus = 6
    pending = list(tasks)
    running: dict[asyncio.Task[int], Task] = {}
    status = 0

    while pending or running:
        launched = False
        for task in list(pending):
            if (
                task.memory_tokens <= available_memory
                and task.cpus <= available_cpus
                and len(running) < max_workers
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
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
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
