from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.database import Database
from bot.keyboards import admin_keyboard


class UsernameAdminTests(unittest.TestCase):
    """``ADMIN_USERNAMES`` binds a username to the first ID that uses it."""

    def setUp(self) -> None:
        from bot import handlers

        self.handlers = handlers
        self._original = handlers.settings
        handlers.settings = dataclasses.replace(
            self._original, admin_ids=frozenset(), admin_usernames=frozenset({"m_o_eg"})
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "admins.sqlite3")
        asyncio.run(self.db.init(maintenance_default=False))

    def tearDown(self) -> None:
        self.handlers.settings = self._original
        self._tmp.cleanup()

    @staticmethod
    def _user(identifier: int, username: str | None):
        class User:
            id = identifier
            is_bot = False

        User.username = username
        return User()

    def test_configured_username_becomes_admin_then_is_bound(self) -> None:
        user = self._user(555, "M_O_EG")
        self.assertTrue(asyncio.run(self.handlers.is_admin_user(self.db, user)))
        bound = asyncio.run(self.db.get_setting("admin_bind:m_o_eg"))
        self.assertEqual(bound, "555")
        self.assertTrue(asyncio.run(self.handlers.is_admin_id(self.db, 555)))

    def test_other_ids_cannot_claim_a_bound_username(self) -> None:
        asyncio.run(self.handlers.is_admin_user(self.db, self._user(555, "M_O_EG")))
        impostor = self._user(999, "m_o_eg")
        self.assertFalse(asyncio.run(self.handlers.is_admin_user(self.db, impostor)))
        self.assertFalse(asyncio.run(self.handlers.is_admin_id(self.db, 999)))

    def test_unknown_usernames_are_ignored(self) -> None:
        outsider = self._user(777, "someone_else")
        self.assertFalse(asyncio.run(self.handlers.is_admin_user(self.db, outsider)))
        self.assertIsNone(asyncio.run(self.db.get_setting("admin_bind:someone_else")))

    def test_missing_username_or_user_is_rejected(self) -> None:
        self.assertFalse(asyncio.run(self.handlers.is_admin_user(self.db, self._user(1, None))))
        self.assertFalse(asyncio.run(self.handlers.is_admin_user(self.db, None)))
        self.assertFalse(asyncio.run(self.handlers.is_admin_id(self.db, None)))

    def test_numeric_ids_still_win_without_username(self) -> None:
        self.handlers.settings = dataclasses.replace(
            self._original, admin_ids=frozenset({42}), admin_usernames=frozenset()
        )
        self.assertTrue(asyncio.run(self.handlers.is_admin_user(self.db, self._user(42, None))))
        self.assertTrue(asyncio.run(self.handlers.is_admin_id(self.db, 42)))


class AdminUsernameConfigTests(unittest.TestCase):
    def test_parse_strips_at_signs_and_case(self) -> None:
        from bot.config import parse_admin_usernames

        self.assertEqual(
            parse_admin_usernames(" @M_O_EG ; Second_Dev ,, "),
            frozenset({"m_o_eg", "second_dev"}),
        )
        self.assertEqual(parse_admin_usernames(""), frozenset())


class AdminKeyboardTests(unittest.TestCase):
    def test_dashboard_offers_ai_errors_and_backup(self) -> None:
        callbacks = [
            button.callback_data
            for row in admin_keyboard(False, True).inline_keyboard
            for button in row
            if button.callback_data
        ]
        for expected in ("admin:monitor", "admin:ai", "admin:errors", "admin:backup"):
            with self.subTest(callback=expected):
                self.assertIn(expected, callbacks)

    def test_ai_button_reflects_state(self) -> None:
        def labels(enabled: bool) -> list[str]:
            return [
                button.text
                for row in admin_keyboard(False, enabled).inline_keyboard
                for button in row
                if button.callback_data == "admin:ai"
            ]

        self.assertIn("تفعيل", labels(False)[0])
        self.assertIn("إيقاف", labels(True)[0])


class AdminDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "admin.sqlite3")
        asyncio.run(self.db.init(maintenance_default=False))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_runtime_settings_round_trip(self) -> None:
        async def scenario():
            initial = await self.db.get_setting("ai_background")
            await self.db.set_setting("ai_background", "1")
            enabled = await self.db.get_setting("ai_background")
            flag = await self.db.get_flag("ai_background")
            await self.db.set_setting("ai_background", "0")
            disabled = await self.db.get_flag("ai_background")
            missing = await self.db.get_flag("missing", default=True)
            return initial, enabled, flag, disabled, missing

        initial, enabled, flag, disabled, missing = asyncio.run(scenario())
        self.assertIsNone(initial)
        self.assertEqual(enabled, "1")
        self.assertTrue(flag)
        self.assertFalse(disabled)
        self.assertTrue(missing)

    def test_recent_failed_jobs_only_returns_failures(self) -> None:
        async def scenario():
            await self.db.create_task("aaaa", 1, "convert")
            await self.db.update_task("aaaa", "failed", 0, "bad input")
            await self.db.create_task("bbbb", 2, "convert")
            await self.db.update_task("bbbb", "completed", 100, "ok")
            await self.db.create_task("cccc", 3, "animate")
            return await self.db.recent_failed_jobs(5)

        rows = asyncio.run(scenario())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "aaaa")
        self.assertEqual(rows[0]["detail"], "bad input")


class BackupTests(unittest.TestCase):
    def test_backup_is_a_consistent_sqlite_copy(self) -> None:
        from bot import handlers

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "live.sqlite3")
            asyncio.run(database.init(maintenance_default=False))
            asyncio.run(database.touch_user(42, "user", "User"))

            original = handlers.settings
            handlers.settings = dataclasses.replace(original, work_dir=root / "work")
            try:
                backup = asyncio.run(handlers._backup_database(database))
            finally:
                handlers.settings = original

            self.assertTrue(backup.exists())
            connection = sqlite3.connect(backup)
            try:
                count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


class AdminTextTests(unittest.TestCase):
    def test_admin_text_mentions_ai_state(self) -> None:
        from bot import handlers

        with tempfile.TemporaryDirectory() as temp_dir:
            universe = Path(temp_dir)
            database = Database(universe / "panel.sqlite3")
            asyncio.run(database.init(maintenance_default=False))
            original = handlers.settings
            handlers.settings = dataclasses.replace(original, data_dir=universe)
            try:
                text = asyncio.run(handlers._admin_text(database))
            finally:
                handlers.settings = original
        self.assertIn("لوحة المطور", text)
        self.assertIn("إزالة الخلفية", text)

    def test_admin_markup_reads_database_flags(self) -> None:
        from bot import handlers

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "panel.sqlite3")
            asyncio.run(database.init(maintenance_default=False))
            markup = asyncio.run(handlers._admin_markup(database))
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("admin:ai", callbacks)


if __name__ == "__main__":
    unittest.main()
