from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bot.admin import monitoring
from bot.database import Stats
from bot.tasks.queue import QueueStats


class FakeQueue:
    def stats(self) -> QueueStats:
        return QueueStats(workers=2, queued=1, running=1, completed=7, failed=2, cancelled=1)

    def snapshot(self) -> list[dict]:
        return [
            {
                "task_id": "abcdef1234567890",
                "user_id": 5,
                "task_type": "convert",
                "status": "running",
                "progress": 42,
                "detail": "half",
                "elapsed": 3.2,
            }
        ]


class FakeDB:
    def __init__(self, path: str) -> None:
        self.path = path

    async def get_stats(self) -> Stats:
        return Stats(users=12, packs=4, jobs=30, failed=2, blocked=1)


class SnapshotTests(unittest.TestCase):
    def test_collect_combines_db_queue_and_host_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "blob.bin").write_bytes(b"x" * 2048)
            database = root / "bot.sqlite3"
            database.write_bytes(b"y" * 4096)

            snapshot = asyncio.run(
                monitoring.collect(FakeDB(str(database)), FakeQueue(), root)
            )

        self.assertEqual(snapshot["users"], 12)
        self.assertEqual(snapshot["queue"]["workers"], 2)
        self.assertEqual(snapshot["queue"]["completed"], 7)
        self.assertEqual(len(snapshot["active_jobs"]), 1)
        self.assertGreaterEqual(snapshot["data_dir_mb"], 0.0)
        self.assertAlmostEqual(snapshot["database_mb"], 4096 / (1024 * 1024), places=2)
        self.assertGreater(snapshot["disk"]["total_gb"], 0)
        self.assertGreater(snapshot["uptime_seconds"], -1)

    def test_format_report_is_arabic_html_with_queue_numbers(self) -> None:
        snapshot = {
            "users": 12,
            "blocked": 1,
            "packs": 4,
            "jobs": 30,
            "failed_jobs": 2,
            "queue": {
                "workers": 2, "queued": 1, "running": 1,
                "completed": 7, "failed": 2, "cancelled": 1,
            },
            "active_jobs": [
                {"task_id": "abcdef1234567890", "task_type": "convert",
                 "status": "running", "progress": 42, "elapsed": 3.2},
            ],
            "memory_mb": 128.5,
            "uptime_seconds": 3725,
            "disk": {"total_gb": 100.0, "free_gb": 40.0, "used_gb": 60.0,
                     "used_percent": 60.0},
            "data_dir_mb": 5.5,
            "database_mb": 1.25,
            "python": "3.12.10",
            "platform": "Linux",
        }

        report = monitoring.format_report(snapshot)

        for needle in ("مراقبة", "128.5 MB", "60.0/100.0 GB", "قيد التنفيذ",
                       "1س 2د 5ث", "abcdef12", "Python 3.12.10"):
            with self.subTest(needle=needle):
                self.assertIn(needle, report)

    def test_format_report_handles_missing_memory(self) -> None:
        report = monitoring.format_report({
            "memory_mb": None,
            "queue": {},
            "disk": {},
            "uptime_seconds": 0,
            "python": "3.12",
            "platform": "Windows",
        })
        self.assertIn("غير متاح", report)

    def test_host_helpers_are_non_fatal(self) -> None:
        self.assertIsInstance(monitoring.uptime_seconds(), int)
        memory = monitoring.resident_mb()
        self.assertTrue(memory is None or memory > 0)
        disk = monitoring.disk_snapshot(Path(tempfile.gettempdir()))
        self.assertGreater(disk["total_gb"], 0)
        self.assertGreaterEqual(disk["used_percent"], 0)


if __name__ == "__main__":
    unittest.main()
