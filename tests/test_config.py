from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# config.py creates one settings instance at import; give it a disposable token.
os.environ.setdefault("BOT_TOKEN", "123456789:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sticker-studio-config-"))
os.environ.setdefault("ADMIN_IDS", "1")

from bot.config import Settings, parse_admin_ids


class ConfigTests(unittest.TestCase):
    def test_parse_admin_ids_accepts_commas_semicolons_and_spaces(self) -> None:
        self.assertEqual(parse_admin_ids("1, 2;3 , 4"), frozenset({1, 2, 3, 4}))
        self.assertEqual(parse_admin_ids(""), frozenset())

    def test_parse_admin_ids_rejects_text(self) -> None:
        with self.assertRaises(ValueError):
            parse_admin_ids("123, admin")

    def test_settings_reads_both_legacy_and_new_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "BOT_TOKEN": "123456789:TOKEN",
                "ADMIN_IDS": "42,43",
                "DATA_DIR": tmp,
                "TEMP_DIR": "",
                "DATABASE_PATH": "",
                "MAX_DOWNLOAD_MB": "99",
                "MAX_ARCHIVE_ITEMS": "500",
                "ENABLE_BG_REMOVAL": "0",
                "MAINTENANCE_MODE": "true",
                "USER_COOLDOWN_SECONDS": "0",
                "AUTO_PACK": "0",
                "TELEGRAM_PROXY": "socks5://127.0.0.1:1080",
            }
            with patch.dict(os.environ, env, clear=False):
                settings = Settings.from_env()

        data_dir = Path(tmp)
        self.assertEqual(settings.admin_ids, frozenset({42, 43}))
        self.assertEqual(settings.max_download_mb, 20)  # clamped to Telegram's limit
        self.assertEqual(settings.max_archive_items, 200)  # clamped
        self.assertEqual(settings.work_dir, data_dir / "tmp")
        self.assertEqual(settings.database_path, data_dir / "sticker_studio.sqlite3")
        self.assertFalse(settings.enable_bg_removal)
        self.assertTrue(settings.maintenance_default)
        self.assertEqual(settings.user_cooldown_seconds, 0)
        self.assertFalse(settings.auto_pack)
        self.assertEqual(settings.telegram_proxy, "socks5://127.0.0.1:1080")

    def test_auto_pack_is_on_by_default(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "123456789:TOKEN"}, clear=False):
            os.environ.pop("AUTO_PACK", None)
            settings = Settings.from_env()
        self.assertTrue(settings.auto_pack)

    def test_placeholder_token_is_rejected(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "PUT_YOUR_BOT_TOKEN_HERE"}, clear=False):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_work_dir_is_used_when_data_dir_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "BOT_TOKEN": "123456789:TOKEN",
                "DATA_DIR": "",
                "WORK_DIR": tmp,
                "TEMP_DIR": "",
                "DATABASE_PATH": "",
            }
            with patch.dict(os.environ, env, clear=False):
                settings = Settings.from_env()
            self.assertEqual(settings.data_dir, Path(tmp))
            self.assertEqual(settings.database_path, Path(tmp) / "sticker_studio.sqlite3")


if __name__ == "__main__":
    unittest.main()
