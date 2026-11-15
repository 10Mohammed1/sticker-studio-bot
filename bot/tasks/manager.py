"""Small async task coordinator for the single-process long-polling bot."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections import defaultdict
from collections.abc import AsyncIterator


class TaskManager:
    """Bound the complete pipeline and allow users to cancel their active work."""

    def __init__(self, max_concurrent: int = 1):
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._active: dict[int, set[asyncio.Task]] = defaultdict(set)

    @asynccontextmanager
    async def slot(self, user_id: int) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("TaskManager requires an asyncio task")
        self._active[user_id].add(task)
        try:
            async with self._semaphore:
                yield
        finally:
            tasks = self._active.get(user_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self._active.pop(user_id, None)

    def cancel_user(self, user_id: int) -> int:
        tasks = list(self._active.get(user_id, ()))
        current = asyncio.current_task()
        cancelled = 0
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    @property
    def active_count(self) -> int:
        return sum(len(tasks) for tasks in self._active.values())
