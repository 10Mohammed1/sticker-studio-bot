from __future__ import annotations

import re
import unittest
from pathlib import Path

from bot.stickers import (
    auto_pack_title,
    format_auto_title,
    format_label,
    normalize_pack_title,
    pack_link,
    pack_short_name,
    sticker_format_for_path,
)


class StickerPackTests(unittest.TestCase):
    def test_format_mapping(self) -> None:
        self.assertEqual(sticker_format_for_path(Path("a.webp")), "static")
        self.assertEqual(sticker_format_for_path(Path("a.WEBM")), "video")
        self.assertEqual(sticker_format_for_path(Path("a.tgs")), "animated")
        self.assertIsNone(sticker_format_for_path(Path("a.png")))
        self.assertIsNone(sticker_format_for_path(Path("a.zip")))

    def test_pack_name_is_telegram_safe_and_ends_with_bot_suffix(self) -> None:
        name = pack_short_name(123456789, "My_Sticker_Bot")
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.endswith("_by_my_sticker_bot"))
        self.assertRegex(name, re.compile(r"^[a-z0-9_]+$"))

    def test_pack_name_survives_a_long_bot_username(self) -> None:
        suffix = "_by_" + "a" * 50
        name = pack_short_name(1, "A" * 50)
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.endswith(suffix))
        self.assertTrue(name[: -len(suffix)])

    def test_links_and_labels(self) -> None:
        self.assertEqual(pack_link("abc"), "https://t.me/addstickers/abc")
        self.assertIn("WEBP", format_label("static"))
        self.assertIn("WEBM", format_label("video"))
        self.assertIn("متحرك", format_label("animated"))
        self.assertIn("غير محددة", format_label(None))

    def test_auto_pack_title_uses_first_name(self) -> None:
        self.assertEqual(auto_pack_title("Alice"), "ملصقات Alice")
        self.assertEqual(auto_pack_title("  "), "ملصقاتي")
        self.assertEqual(auto_pack_title(None), "ملصقاتي")
        self.assertEqual(len(auto_pack_title("x" * 100)), 64)

    def test_auto_title_gets_a_format_suffix(self) -> None:
        self.assertEqual(format_auto_title("ملصقاتي", "static"), "ملصقاتي — ثابت")
        self.assertEqual(format_auto_title("ملصقاتي", "video"), "ملصقاتي — فيديو")
        self.assertEqual(format_auto_title("ملصقاتي", "animated"), "ملصقاتي — متحرك")
        self.assertEqual(format_auto_title("ملصقاتي", None), "ملصقاتي")
        self.assertEqual(len(format_auto_title("x" * 64, "video")), 64)

    def test_title_normalization(self) -> None:
        self.assertEqual(normalize_pack_title("  My pack  "), "My pack")
        self.assertIsNone(normalize_pack_title(""))
        self.assertIsNone(normalize_pack_title("   "))
        self.assertIsNone(normalize_pack_title("x" * 65))
        self.assertEqual(normalize_pack_title("x" * 64), "x" * 64)


if __name__ == "__main__":
    unittest.main()
