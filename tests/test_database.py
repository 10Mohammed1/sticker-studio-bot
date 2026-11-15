from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        asyncio.run(self.db.init(maintenance_default=False))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_user_tracking_stats_and_blocking(self) -> None:
        asyncio.run(self.db.touch_user(10, "alice", "Alice"))
        asyncio.run(self.db.touch_user(11, "bob", "Bob"))

        stats = asyncio.run(self.db.get_stats())
        self.assertEqual(stats.users, 2)
        self.assertEqual(stats.blocked, 0)

        self.assertTrue(asyncio.run(self.db.set_blocked(10, True)))
        self.assertTrue(asyncio.run(self.db.is_blocked(10)))
        self.assertEqual(asyncio.run(self.db.all_user_ids()), [11])

        self.assertFalse(asyncio.run(self.db.set_blocked(999, True)))

    def test_pack_lifecycle(self) -> None:
        asyncio.run(self.db.touch_user(7, None, "Seven"))

        active = asyncio.run(self.db.get_active_pack(7))
        self.assertFalse(active.exists)

        asyncio.run(self.db.start_pack(7, "My pack"))
        active = asyncio.run(self.db.get_active_pack(7))
        self.assertTrue(active.exists)
        self.assertIsNone(active.name)
        self.assertEqual(active.title, "My pack")

        asyncio.run(self.db.save_active_pack(7, "abc_by_testbot", "My pack", "static"))
        active = asyncio.run(self.db.get_active_pack(7))
        self.assertEqual(active.name, "abc_by_testbot")
        self.assertEqual(active.sticker_format, "static")
        self.assertEqual(len(asyncio.run(self.db.get_packs(7))), 1)

        asyncio.run(self.db.forget_pack_name(7))
        active = asyncio.run(self.db.get_active_pack(7))
        self.assertIsNone(active.name)
        self.assertEqual(active.title, "My pack")
        self.assertIsNone(active.sticker_format)
        # The deleted Telegram set must not linger in the /packs panel.
        self.assertEqual(asyncio.run(self.db.get_packs(7)), [])

        asyncio.run(self.db.clear_active_pack(7))
        self.assertFalse(asyncio.run(self.db.get_active_pack(7)).exists)

    def test_jobs_and_maintenance(self) -> None:
        asyncio.run(self.db.touch_user(1, "u", "U"))
        asyncio.run(self.db.add_job(1, "image", "WEBP", "success"))
        asyncio.run(self.db.add_job(1, "video", "WEBM", "failed"))

        stats = asyncio.run(self.db.get_stats())
        self.assertEqual(stats.jobs, 2)
        self.assertEqual(stats.failed, 1)

        self.assertFalse(asyncio.run(self.db.get_maintenance()))
        asyncio.run(self.db.set_maintenance(True))
        self.assertTrue(asyncio.run(self.db.get_maintenance()))

    def test_pack_registry_supports_rename_and_reactivation(self) -> None:
        asyncio.run(self.db.touch_user(8, "u", "U"))

        asyncio.run(self.db.start_pack(8, "ملصقاتي — ثابت", auto=True))
        asyncio.run(
            self.db.save_active_pack(8, "first_by_bot", "ملصقاتي — ثابت", "static", auto=True)
        )
        asyncio.run(self.db.start_pack(8, "ملصقاتي — فيديو", auto=True))
        asyncio.run(
            self.db.save_active_pack(8, "second_by_bot", "ملصقاتي — فيديو", "video", auto=True)
        )

        packs = asyncio.run(self.db.get_packs(8))
        self.assertEqual([pack.name for pack in packs], ["second_by_bot", "first_by_bot"])
        self.assertTrue(asyncio.run(self.db.get_active_pack(8)).auto)
        first = next(pack for pack in packs if pack.name == "first_by_bot")

        self.assertTrue(asyncio.run(self.db.update_pack_title(first.id, 8, "اسم جديد")))
        self.assertEqual(
            asyncio.run(self.db.get_active_pack(8)).title, "ملصقاتي — فيديو"
        )
        self.assertFalse(asyncio.run(self.db.update_pack_title(first.id, 999, "x")))

        asyncio.run(self.db.activate_pack(8, asyncio.run(self.db.get_pack(first.id, 8))))
        active = asyncio.run(self.db.get_active_pack(8))
        self.assertEqual(active.name, "first_by_bot")
        self.assertEqual(active.title, "اسم جديد")
        self.assertEqual(active.sticker_format, "static")
        self.assertFalse(active.auto)

    def test_old_database_is_migrated_and_active_pack_backfilled(self) -> None:
        path = Path(self._tmp.name) / "legacy.sqlite3"
        old = sqlite3.connect(path)
        try:
            old.executescript(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    active_pack_name TEXT,
                    active_pack_title TEXT,
                    active_pack_format TEXT
                );
                """
            )
            old.execute(
                """INSERT INTO users(user_id, joined_at, last_seen, active_pack_name,
                   active_pack_title, active_pack_format)
                   VALUES(5, 'x', 'x', 'old_by_bot', 'حزمة قديمة', 'video')"""
            )
            old.commit()
        finally:
            old.close()

        legacy = Database(path)
        asyncio.run(legacy.init(maintenance_default=False))

        active = asyncio.run(legacy.get_active_pack(5))
        self.assertTrue(active.exists)
        self.assertEqual(active.name, "old_by_bot")
        self.assertFalse(active.auto)

        packs = asyncio.run(legacy.get_packs(5))
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].name, "old_by_bot")
        self.assertEqual(packs[0].sticker_format, "video")
        self.assertEqual(packs[0].title, "حزمة قديمة")

    def test_start_pack_resets_previous_name_but_keeps_new_title(self) -> None:
        asyncio.run(self.db.touch_user(5, "u", "U"))
        asyncio.run(self.db.save_active_pack(5, "old_by_bot", "Old", "video"))
        asyncio.run(self.db.start_pack(5, "New"))
        active = asyncio.run(self.db.get_active_pack(5))
        self.assertIsNone(active.name)
        self.assertEqual(active.title, "New")
        self.assertIsNone(active.sticker_format)


if __name__ == "__main__":
    unittest.main()
