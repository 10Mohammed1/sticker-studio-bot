from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456789:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sticker-studio-imports-"))
os.environ.setdefault("ADMIN_IDS", "1")

from bot import handlers, main


class ImportSmokeTests(unittest.TestCase):
    def test_router_has_message_and_callback_handlers(self) -> None:
        self.assertGreater(len(handlers.router.message.handlers), 5)
        self.assertGreater(len(handlers.router.callback_query.handlers), 0)

    def test_pipeline_limits_are_loaded(self) -> None:
        self.assertGreaterEqual(handlers.settings.max_concurrent_jobs, 1)
        self.assertGreaterEqual(handlers.settings.max_download_mb, 1)
        self.assertEqual(main.VERSION, "4.1.0")

    def test_admin_check_uses_settings(self) -> None:
        # Patch the frozen settings locally instead of relying on import order.
        original = handlers.settings
        handlers.settings = dataclasses.replace(original, admin_ids=frozenset({1}))
        try:
            self.assertTrue(handlers.is_admin(1))  # ADMIN_IDS contains 1
            self.assertFalse(handlers.is_admin(None))
            self.assertFalse(handlers.is_admin(999999))
        finally:
            handlers.settings = original

    def test_split_message_chunks_long_broadcasts(self) -> None:
        text = "\n".join(f"سطر رقم {index}" for index in range(2000))
        chunks = handlers.split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= handlers.BROADCAST_CHUNK for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_split_message_keeps_short_text_whole(self) -> None:
        self.assertEqual(handlers.split_message("مرحبا"), ["مرحبا"])
        self.assertEqual(handlers.split_message(""), [])


if __name__ == "__main__":
    unittest.main()
