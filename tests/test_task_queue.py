from __future__ import annotations

import asyncio
import unittest

from bot.tasks.queue import (
    PRIORITY_ADMIN,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    DuplicateJob,
    JobQueue,
    UserQueueFull,
)


async def _blocker(gate: asyncio.Event, started: asyncio.Event, value: str = "done") -> str:
    started.set()
    await gate.wait()
    return value


class QueueOrderingTests(unittest.TestCase):
    def test_priority_jumps_ahead_of_queued_normal_work(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()
            gate = asyncio.Event()
            started = asyncio.Event()
            order: list[str] = []

            async def record(name: str) -> str:
                order.append(name)
                return name

            blocking = asyncio.create_task(
                queue.submit(1, "blocker", _blocker, gate, started)
            )
            await started.wait()
            low = asyncio.create_task(
                queue.submit(2, "low", record, "low", priority=PRIORITY_LOW)
            )
            admin = asyncio.create_task(
                queue.submit(3, "admin", record, "admin", priority=PRIORITY_ADMIN)
            )
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(blocking, low, admin)
            await queue.close()
            return order

        self.assertEqual(asyncio.run(scenario()), ["admin", "low"])

    def test_stats_track_completed_and_failed_jobs(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()

            def boom() -> None:
                raise ValueError("nope")

            await queue.submit(1, "ok", lambda: 1)
            with self.assertRaises(ValueError):
                await queue.submit(1, "bad", boom)
            stats = queue.stats()
            await queue.close()
            return stats

        stats = asyncio.run(scenario())
        self.assertEqual(stats.completed, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.queued, 0)
        self.assertEqual(stats.running, 0)


class ProgressTests(unittest.TestCase):
    def test_report_reaches_the_state_hook_while_running(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()
            seen: list[tuple[str, int]] = []

            async def hook(task_id, user_id, task_type, status, progress, detail):
                seen.append((status, progress))

            def work(report) -> int:
                report(50, "half")
                report(90, "almost")
                return 5

            result = await queue.submit(1, "convert", work, on_state=hook)
            await asyncio.sleep(0.05)
            await queue.close()
            return result, seen

        result, seen = asyncio.run(scenario())
        self.assertEqual(result, 5)
        self.assertIn(("queued", 0), seen)
        self.assertIn(("running", 5), seen)
        self.assertIn(("completed", 100), seen)
        self.assertTrue(any(progress in {50, 90} for status, progress in seen
                            if status == "running"))


class LimitsTests(unittest.TestCase):
    def test_duplicate_job_is_rejected_while_active(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()
            gate = asyncio.Event()
            started = asyncio.Event()
            first = asyncio.create_task(
                queue.submit(1, "same", _blocker, gate, started, dedupe_key="same-key")
            )
            await started.wait()
            with self.assertRaises(DuplicateJob):
                await queue.submit(1, "same", lambda: 2, dedupe_key="same-key")
            gate.set()
            await first
            await queue.close()

        asyncio.run(scenario())

    def test_per_user_limit_defers_extra_work(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1, per_user_limit=1)
            await queue.start()
            gate = asyncio.Event()
            started = asyncio.Event()
            first = asyncio.create_task(queue.submit(9, "job", _blocker, gate, started))
            await started.wait()
            with self.assertRaises(UserQueueFull):
                await queue.submit(9, "job", lambda: 1)
            other_task = asyncio.create_task(queue.submit(10, "job", lambda: 2))
            await asyncio.sleep(0.05)
            gate.set()
            await first
            other = await other_task
            await queue.close()
            return other

        self.assertEqual(asyncio.run(scenario()), 2)


class CancelAndRetryTests(unittest.TestCase):
    def test_cancel_queued_job_frees_the_worker(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()
            gate = asyncio.Event()
            started = asyncio.Event()
            first = asyncio.create_task(queue.submit(1, "blocker", _blocker, gate, started))
            await started.wait()
            second = asyncio.create_task(queue.submit(2, "victim", lambda: "never"))
            await asyncio.sleep(0.05)
            queued = [job for job in queue.snapshot() if job["status"] == "queued"]
            self.assertEqual(len(queued), 1)
            self.assertTrue(queue.cancel(queued[0]["task_id"]))
            gate.set()
            await first
            outcome = await asyncio.gather(second, return_exceptions=True)
            stats = queue.stats()
            await queue.close()
            return outcome[0], stats

        outcome, stats = asyncio.run(scenario())
        self.assertIsInstance(outcome, asyncio.CancelledError)
        self.assertEqual(stats.cancelled, 1)

    def test_retry_replays_original_arguments_for_owner_only(self) -> None:
        async def scenario():
            queue = JobQueue(workers=1)
            await queue.start()
            gate = asyncio.Event()
            started = asyncio.Event()
            first = asyncio.create_task(
                queue.submit(4, "job", _blocker, gate, started, value="first")
            )
            await started.wait()
            task_id = queue.snapshot()[0]["task_id"]
            gate.set()
            self.assertEqual(await first, "first")
            owner = queue.can_retry(task_id, 4)
            stranger = queue.can_retry(task_id, 5)
            again = await queue.retry(task_id, 4)
            await queue.close()
            return owner, stranger, again

        owner, stranger, again = asyncio.run(scenario())
        self.assertTrue(owner)
        self.assertFalse(stranger)
        self.assertEqual(again, "first")

    def test_slot_shim_does_not_deadlock_with_run_tracked(self) -> None:
        async def scenario():
            from bot.tasks.runtime import job_queue, run_tracked

            class FakeDB:
                async def create_task(self, *args):
                    return None

                async def update_task(self, *args, **kwargs):
                    return None

            await job_queue.start()
            async with job_queue.slot(1):
                result = await run_tracked(FakeDB(), 1, "nested", lambda: 3)
            return result

        self.assertEqual(asyncio.run(scenario()), 3)


if __name__ == "__main__":
    unittest.main()
