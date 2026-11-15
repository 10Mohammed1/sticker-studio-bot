"""In-process priority job queue for the single-process bot.

Telegram long polling, SQLite and (optionally) a single worker container make a
real broker such as Redis dead weight. This queue gives the pipeline what the
4.0 plan asks for without new infrastructure:

* bounded parallelism (``MAX_CONCURRENT_JOBS``);
* priority levels so admin/repair work jumps ahead of bulk conversions;
* cooperative progress reporting from blocking worker threads;
* cancellation that never corrupts the queue;
* duplicate suppression for the same user + task type;
* a short in-memory history so a failed job can be retried;
* an optional per-user cap that defers work instead of rejecting it.

The queue binds lazily to the running event loop so tests can use
``asyncio.run()`` repeatedly without leaking workers between loops.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import itertools
import time
import uuid
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

PRIORITY_LOW = 0
PRIORITY_NORMAL = 10
PRIORITY_ADMIN = 20

StateHook = Callable[[str, int, str, str, int, str], Awaitable[None]]
"""``(task_id, user_id, task_type, status, progress, detail)`` persisted by runtime."""


class QueueClosed(RuntimeError):
    """The queue is shutting down and cannot accept new jobs."""


class DuplicateJob(RuntimeError):
    """An identical job for this user is already queued or running."""


class UserQueueFull(RuntimeError):
    """The user already has the maximum allowed number of queued/running jobs."""


@dataclass(frozen=True)
class QueueStats:
    workers: int
    queued: int
    running: int
    completed: int
    failed: int
    cancelled: int

    @property
    def active(self) -> int:
        return self.queued + self.running


@dataclass
class Job:
    task_id: str
    user_id: int
    task_type: str
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    priority: int
    dedupe: str | None
    on_state: StateHook | None = None
    created: float = field(default_factory=time.monotonic)
    status: str = "queued"
    progress: int = 0
    detail: str = ""
    started: float | None = None
    finished: float | None = None
    future: asyncio.Future | None = None
    runner: asyncio.Task | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished or time.monotonic()
        return max(0.0, end - (self.started or self.created))

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "task_type": self.task_type,
            "status": self.status,
            "progress": self.progress,
            "detail": self.detail,
            "elapsed": round(self.elapsed, 1),
        }


class JobQueue:
    def __init__(
        self,
        workers: int = 1,
        *,
        per_user_limit: int | None = None,
        history_size: int = 200,
    ) -> None:
        self.worker_count = max(1, int(workers))
        self.per_user_limit = per_user_limit if (per_user_limit or 0) > 0 else None
        self.history_size = max(1, int(history_size))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.PriorityQueue | None = None
        self._workers: list[asyncio.Task] = []
        self._slots: asyncio.Semaphore | None = None
        self._jobs: dict[str, Job] = {}
        self._user_active: dict[int, int] = defaultdict(int)
        self._dedupe: dict[str, str] = {}
        self._seq = itertools.count()
        self._history: OrderedDict[str, tuple[Callable[..., Any], tuple, dict, str, int]] = (
            OrderedDict()
        )
        self._closed = False
        self._completed = 0
        self._failed = 0
        self._cancelled = 0

    # ------------------------------------------------------------------ setup
    def _bind_loop(self) -> None:
        """Create loop-bound primitives. A new loop (tests) resets stale state."""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._closed = False
        self._queue = asyncio.PriorityQueue()
        self._slots = None
        self._workers = [
            loop.create_task(self._worker(), name=f"job-worker-{index}")
            for index in range(self.worker_count)
        ]
        # Futures from a dead loop can never be resolved; drop them quietly.
        self._jobs = {key: job for key, job in self._jobs.items() if job.status == "running"}
        self._user_active.clear()
        self._dedupe.clear()
        self._seq = itertools.count()

    async def start(self) -> None:
        self._bind_loop()

    @asynccontextmanager
    async def slot(self, user_id: int | None = None):
        """Compatibility shim for the 3.x pipeline: bound outer work, never deadlock.

        The 3.x handler wrapped a whole message batch in ``async with
        task_manager.slot(user_id)`` and then called ``run_tracked`` inside. A real
        queue slot would deadlock with a single worker, so this only limits how
        many batches are prepared at once while the queue serialises the CPU work.
        """
        self._bind_loop()
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(2, self.worker_count * 4))
        async with self._slots:
            yield

    async def close(self) -> None:
        self._closed = True
        for job in list(self._jobs.values()):
            if job.status == "queued" and job.future and not job.future.done():
                job.status = "cancelled"
                job.future.cancel()
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        self._jobs.clear()
        self._dedupe.clear()

    # --------------------------------------------------------------- submission
    async def submit(
        self,
        user_id: int,
        task_type: str,
        function: Callable[..., Any],
        *args: Any,
        priority: int = PRIORITY_NORMAL,
        dedupe_key: str | None = None,
        on_state: StateHook | None = None,
        **kwargs: Any,
    ) -> Any:
        """Queue one blocking callable and await its result."""
        if self._closed:
            raise QueueClosed("الخدمة تُغلق الآن؛ أعد المحاولة بعد قليل.")
        self._bind_loop()
        assert self._queue is not None

        if dedupe_key and dedupe_key in self._dedupe:
            raise DuplicateJob("نفس العملية قيد التنفيذ بالفعل. انتظر نتيجتها.")
        if self.per_user_limit is not None and self.active_for_user(user_id) >= self.per_user_limit:
            raise UserQueueFull(
                "لديك مهام كثيرة قيد التنفيذ. انتظر انتهاء إحداها ثم أعد المحاولة."
            )

        task_id = uuid.uuid4().hex[:16]
        job = Job(
            task_id=task_id,
            user_id=user_id,
            task_type=task_type,
            function=function,
            args=args,
            kwargs=dict(kwargs),
            priority=priority,
            dedupe=dedupe_key,
            on_state=on_state,
            future=asyncio.get_running_loop().create_future(),
        )
        self._jobs[task_id] = job
        if dedupe_key:
            self._dedupe[dedupe_key] = task_id
        self._remember(task_id, function, args, kwargs, task_type, user_id)

        await self._notify(job, "queued", 0, "في الانتظار")
        await self._queue.put((-priority, next(self._seq), task_id))
        return await asyncio.shield(job.future)

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        try:
            while True:
                assert self._queue is not None
                _, _, task_id = await self._queue.get()
                job = self._jobs.get(task_id)
                if job is None or job.status != "queued":
                    self._queue.task_done()
                    continue
                if (
                    self.per_user_limit is not None
                    and self._user_active[job.user_id] >= self.per_user_limit
                ):
                    # Requeue behind the others; never spin on the same head item.
                    await self._queue.put((-job.priority, next(self._seq), task_id))
                    self._queue.task_done()
                    await asyncio.sleep(0.2)
                    continue
                await self._run(job)
                self._queue.task_done()
        except asyncio.CancelledError:
            return

    async def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = time.monotonic()
        job.progress = 5
        self._user_active[job.user_id] += 1
        await self._notify(job, "running", 5, "بدأت المعالجة")
        runner = asyncio.create_task(self._execute(job), name=f"job-{job.task_id}")
        job.runner = runner
        try:
            result = await runner
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.detail = "ألغى المستخدم المهمة"
            self._cancelled += 1
            await self._notify(job, "cancelled", job.progress, job.detail)
            self._finish(job, cancelled=True)
            if self._closed:
                raise
            return
        except Exception as exc:  # noqa: BLE001 - the caller decides how to present it
            job.status = "failed"
            job.detail = f"{type(exc).__name__}: {exc}"[:240]
            self._failed += 1
            await self._notify(job, "failed", job.progress, "فشلت المعالجة")
            self._finish(job, exception=exc)
            return
        job.status = "completed"
        job.progress = 100
        job.detail = "اكتملت المعالجة"
        self._completed += 1
        await self._notify(job, "completed", 100, job.detail)
        self._finish(job, result=result)

    async def _execute(self, job: Job) -> Any:
        kwargs = dict(job.kwargs)
        if self._accepts_report(job.function):
            kwargs["report"] = self._thread_reporter(job)
        if inspect.iscoroutinefunction(job.function):
            return await job.function(*job.args, **kwargs)
        return await asyncio.to_thread(functools.partial(job.function, *job.args, **kwargs))

    @staticmethod
    def _accepts_report(function: Callable[..., Any]) -> bool:
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return "report" in parameters

    def _thread_reporter(self, job: Job) -> Callable[[int, str], None]:
        loop = self._loop

        def report(percent: int, detail: str = "") -> None:
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(self._set_progress, job.task_id, percent, detail)

        return report

    def _set_progress(self, task_id: str, percent: int, detail: str) -> None:
        job = self._jobs.get(task_id)
        if job is None or job.status != "running":
            return
        job.progress = max(0, min(99, int(percent)))
        if detail:
            job.detail = detail[:240]
        if job.on_state is not None:
            asyncio.ensure_future(
                job.on_state(
                    job.task_id, job.user_id, job.task_type, job.status, job.progress, job.detail
                )
            )

    def _finish(
        self,
        job: Job,
        *,
        result: Any = None,
        exception: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        job.finished = time.monotonic()
        self._user_active[job.user_id] = max(0, self._user_active[job.user_id] - 1)
        if self._user_active[job.user_id] == 0:
            self._user_active.pop(job.user_id, None)
        if job.dedupe:
            self._dedupe.pop(job.dedupe, None)
        future = job.future
        if future is not None and not future.done():
            if cancelled:
                future.cancel()
            elif exception is not None:
                future.set_exception(exception)
            else:
                future.set_result(result)
        self._jobs.pop(job.task_id, None)

    async def _notify(self, job: Job, status: str, progress: int, detail: str) -> None:
        if job.on_state is None:
            return
        try:
            await job.on_state(job.task_id, job.user_id, job.task_type, status, progress, detail)
        except Exception:  # noqa: BLE001 - persistence must never kill the worker
            pass

    # --------------------------------------------------------------- inspection
    def cancel(self, task_id: str) -> bool:
        job = self._jobs.get(task_id)
        if job is None:
            return False
        if job.status == "queued":
            job.status = "cancelled"
            self._cancelled += 1
            if job.future and not job.future.done():
                job.future.cancel()
            if job.dedupe:
                self._dedupe.pop(job.dedupe, None)
            self._jobs.pop(task_id, None)
            return True
        if job.runner is not None and not job.runner.done():
            # The thread cannot be stopped, but its result is discarded and the
            # slot is released so the next job can start immediately.
            job.runner.cancel()
            return True
        return False

    def cancel_user(self, user_id: int) -> int:
        cancelled = 0
        for job in list(self._jobs.values()):
            if job.user_id == user_id and self.cancel(job.task_id):
                cancelled += 1
        return cancelled

    def active_for_user(self, user_id: int) -> int:
        return sum(
            1
            for job in self._jobs.values()
            if job.user_id == user_id and job.status in {"queued", "running"}
        )

    @property
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status in {"queued", "running"})

    def stats(self) -> QueueStats:
        queued = sum(1 for job in self._jobs.values() if job.status == "queued")
        running = sum(1 for job in self._jobs.values() if job.status == "running")
        return QueueStats(
            workers=self.worker_count,
            queued=queued,
            running=running,
            completed=self._completed,
            failed=self._failed,
            cancelled=self._cancelled,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [job.snapshot() for job in list(self._jobs.values())]

    # ------------------------------------------------------------------- retry
    def _remember(
        self,
        task_id: str,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        task_type: str,
        user_id: int,
    ) -> None:
        self._history[task_id] = (function, args, dict(kwargs), task_type, user_id)
        self._history.move_to_end(task_id)
        while len(self._history) > self.history_size:
            self._history.popitem(last=False)

    def can_retry(self, task_id: str, user_id: int) -> bool:
        entry = self._history.get(task_id)
        return bool(entry and entry[4] == user_id)

    async def retry(
        self, task_id: str, user_id: int, on_state: StateHook | None = None
    ) -> Any:
        entry = self._history.get(task_id)
        if entry is None or entry[4] != user_id:
            raise KeyError("لا يمكن إعادة هذه المهمة؛ أعد إرسال الملف من جديد.")
        function, args, kwargs, task_type, _ = entry
        return await self.submit(
            user_id, task_type, function, *args, on_state=on_state, **kwargs
        )
