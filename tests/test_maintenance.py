from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from bot.database import Database
from bot.tasks.maintenance import (
    directory_size,
    purge_directory,
    purge_feature_sessions,
    purge_work_dir,
)


def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


class PurgeDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_old_files_are_removed_and_fresh_files_kept(self) -> None:
        stale = self.root / "stale.tmp"
        fresh = self.root / "fresh.tmp"
        stale.write_bytes(b"old")
        fresh.write_bytes(b"new")
        _age(stale, 10)

        removed = purge_directory(self.root, max_age_hours=6)

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(self.root.exists())

    def test_old_directories_are_removed_recursively(self) -> None:
        folder = self.root / "old-dir"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested" / "file.bin").write_bytes(b"x")
        _age(folder, 48)

        self.assertEqual(purge_directory(self.root, max_age_hours=6), 1)
        self.assertFalse(folder.exists())

    def test_missing_directory_is_harmless(self) -> None:
        self.assertEqual(purge_directory(self.root / "missing"), 0)

    def test_work_dir_purge_is_an_alias_with_cutoff(self) -> None:
        work = self.root / "work"
        work.mkdir()
        stale = work / "job"
        stale.mkdir()
        _age(stale, 9)

        self.assertEqual(purge_work_dir(work, max_age_hours=6), 1)
        self.assertFalse(stale.exists())


class PurgeSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_abandoned_sessions_are_removed_and_empty_user_dirs_pruned(self) -> None:
        old = self.data / "feature_sessions" / "1" / "old"
        new = self.data / "feature_sessions" / "1" / "new"
        for session in (old, new):
            session.mkdir(parents=True)
            (session / "image.png").write_bytes(b"pixels")
        _age(old, 30)

        removed = purge_feature_sessions(self.data, max_age_hours=24)

        self.assertEqual(removed, 1)
        self.assertTrue(new.exists())
        self.assertFalse(old.exists())

    def test_missing_sessions_root_returns_zero(self) -> None:
        self.assertEqual(purge_feature_sessions(self.data), 0)


class DirectorySizeTests(unittest.TestCase):
    def test_directory_size_sums_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.bin").write_bytes(b"1" * 100)
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.bin").write_bytes(b"2" * 50)

            self.assertEqual(directory_size(root), 150)
            self.assertEqual(directory_size(root / "missing"), 0)


class PackItemsDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "pack-items.sqlite3")
        asyncio.run(self.db.init(maintenance_default=False))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pack_items_round_trip_keeps_order(self) -> None:
        async def scenario():
            await self.db.touch_user(3, "user", "User")
            await self.db.save_active_pack(3, "demo_by_bot", "Demo", "static")
            packs = await self.db.get_packs(3)
            pack_id = packs[0].id
            await self.db.replace_pack_items(pack_id, [
                {"file_unique_id": "u1", "file_id": "f1", "emoji": "🙂",
                 "sticker_format": "static"},
                {"file_unique_id": "u2", "file_id": "f2", "emoji": "😀",
                 "sticker_format": "static"},
            ])
            first = await self.db.get_pack_items(pack_id)
            await self.db.replace_pack_items(pack_id, [
                {"file_unique_id": "u2", "file_id": "f2", "emoji": "😀",
                 "sticker_format": "static"},
                {"file_unique_id": "u1", "file_id": "f1", "emoji": "🙂",
                 "sticker_format": "static"},
            ])
            second = await self.db.get_pack_items(pack_id)
            await self.db.replace_pack_items(pack_id, [])
            third = await self.db.get_pack_items(pack_id)
            task_id = "task123"
            await self.db.create_task(task_id, 3, "convert")
            task = await self.db.get_task(task_id, 3)
            missing = await self.db.get_task(task_id, 99)
            return first, second, third, task, missing

        first, second, third, task, missing = asyncio.run(scenario())
        self.assertEqual([item["file_unique_id"] for item in first], ["u1", "u2"])
        self.assertEqual([item["file_unique_id"] for item in second], ["u2", "u1"])
        self.assertEqual(second[0]["position"], 0)
        self.assertEqual(third, [])
        self.assertIsNotNone(task)
        self.assertEqual(task["task_type"], "convert")
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
