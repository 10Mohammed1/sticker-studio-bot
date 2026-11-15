"""Shared job-queue facade: persistence, priorities and retry for CPU work."""
from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

from .queue import (
    PRIORITY_ADMIN,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    DuplicateJob,
    JobQueue,
    QueueStats,
    UserQueueFull,
)

T = TypeVar("T")


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


_max_concurrent = _env_int("MAX_CONCURRENT_JOBS", 1, 1, 16)
_per_user_limit = _env_int("MAX_JOBS_PER_USER", 2, 1, 32)

job_queue = JobQueue(workers=_max_concurrent, per_user_limit=_per_user_limit)

# Backwards-compatible name used by the 3.x handlers (/cancel, /admin).
task_manager = job_queue


async def _state_hook(
    db: Any, task_id: str, user_id: int, task_type: str, status: str,
    progress: int, detail: str,
) -> None:
    if status == "queued":
        await db.create_task(task_id, user_id, task_type)
    else:
        await db.update_task(task_id, status, progress, detail)


def _dedupe_key(user_id: int, task_type: str, args: tuple[Any, ...]) -> str:
    """Same user + same task + same inputs while one is active = duplicate."""
    return f"{user_id}:{task_type}:{hash(tuple(repr(arg) for arg in args))}"


async def run_tracked(
    db: Any,
    user_id: int,
    task_type: str,
    function: Callable[..., T],
    *args: Any,
    priority: int = PRIORITY_NORMAL,
    dedupe: bool = True,
    **kwargs: Any,
) -> T:
    """Run one blocking job under the shared queue and record its state."""
    return await job_queue.submit(
        user_id,
        task_type,
        function,
        *args,
        priority=priority,
        dedupe_key=_dedupe_key(user_id, task_type, args) if dedupe else None,
        on_state=lambda *state: _state_hook(db, *state),
        **kwargs,
    )


async def retry_task(db: Any, user_id: int, task_id: str) -> Any:
    """Re-run a recent job with its original arguments."""
    return await job_queue.retry(
        task_id, user_id, on_state=lambda *state: _state_hook(db, *state)
    )


def queue_stats() -> QueueStats:
    return job_queue.stats()


def queue_snapshot() -> list[dict[str, Any]]:
    return job_queue.snapshot()


async def start_queue() -> None:
    await job_queue.start()


async def stop_queue() -> None:
    await job_queue.close()


__all__ = [
    "PRIORITY_ADMIN",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "DuplicateJob",
    "UserQueueFull",
    "job_queue",
    "queue_snapshot",
    "queue_stats",
    "retry_task",
    "run_tracked",
    "start_queue",
    "stop_queue",
    "task_manager",
]
