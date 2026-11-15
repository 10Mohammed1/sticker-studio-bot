from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bot.packs.manager import (
    MAX_STICKERS,
    PackInfo,
    PackManagerError,
    StickerInfo,
    _friendly_error,
    copy_pack,
    delete_sticker,
    find_duplicates,
    is_bot_owned,
    merge_plan,
    move_sticker,
    normalize_title,
    pack_manifest,
    rename_pack,
    validate_index,
)
from bot.packs.organizer import (
    duplicate_groups,
    duplicates_in,
    ordered_stickers,
    position_label,
    sync_pack,
)

BOT_USERNAME = "studio_bot"


def make_pack(
    name: str = "demo_by_studio_bot",
    count: int = 3,
    *,
    fmt: str = "static",
    title: str | None = None,
    bot_username: str = BOT_USERNAME,
    unique_prefix: str | None = None,
) -> PackInfo:
    prefix = unique_prefix or name
    stickers = tuple(
        StickerInfo(
            file_id=f"{prefix}-file-{index}",
            file_unique_id=f"{prefix}-unique-{index}",
            emoji="🙂",
            sticker_format=fmt,
        )
        for index in range(count)
    )
    return PackInfo(
        name=name,
        title=title or name.replace("_", " "),
        stickers=stickers,
        bot_owned=is_bot_owned(name, bot_username),
    )


class FakeBot:
    """Only the Bot API surface the pack manager touches."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.moved: list[tuple[str, int]] = []
        self.renamed: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.downloaded: list[str] = []

    async def delete_sticker_from_set(self, sticker: str) -> None:
        self.deleted.append(sticker)

    async def set_sticker_position_in_set(self, sticker: str, position: int) -> None:
        self.moved.append((sticker, position))

    async def set_sticker_set_title(self, name: str, title: str) -> None:
        self.renamed.append((name, title))

    async def get_file(self, file_id: str):
        class TelegramFile:
            file_path = "stickers/file.webp"

        return TelegramFile()

    async def download_file(self, file_path: str):
        import io

        self.downloaded.append(file_path)
        return io.BytesIO(b"RIFF-fake-webp")

    async def create_new_sticker_set(self, **kwargs) -> bool:
        self.created.append(kwargs)
        return True


class TitleTests(unittest.TestCase):
    def test_normalize_title_collapses_whitespace(self) -> None:
        self.assertEqual(normalize_title("  My   Pack \n"), "My Pack")

    def test_normalize_title_rejects_empty_and_long(self) -> None:
        for value in ("", "   ", "x" * 65):
            with self.subTest(value=value[:10]):
                with self.assertRaises(PackManagerError):
                    normalize_title(value)

    def test_bot_ownership_depends_on_set_suffix(self) -> None:
        self.assertTrue(is_bot_owned("demo_by_studio_bot", BOT_USERNAME))
        self.assertFalse(is_bot_owned("demo_by_other_bot", BOT_USERNAME))
        self.assertFalse(is_bot_owned("demo_by_studio_bot", ""))
        self.assertFalse(is_bot_owned(None, BOT_USERNAME))


class StickerMutationTests(unittest.TestCase):
    def test_index_validation_matches_pack_bounds(self) -> None:
        pack = make_pack(count=2)
        self.assertEqual(validate_index(1, pack), 1)
        for index in (-1, 2):
            with self.subTest(index=index):
                with self.assertRaises(PackManagerError):
                    validate_index(index, pack)

    def test_delete_sticker_calls_telegram_once(self) -> None:
        bot = FakeBot()
        pack = make_pack(count=3)

        removed = asyncio.run(delete_sticker(bot, pack, 1))

        self.assertEqual(removed.file_id, "demo_by_studio_bot-file-1")
        self.assertEqual(bot.deleted, ["demo_by_studio_bot-file-1"])

    def test_move_sticker_reports_target_position(self) -> None:
        bot = FakeBot()
        pack = make_pack(count=3)

        moved = asyncio.run(move_sticker(bot, pack, 2, 0))

        self.assertEqual(moved.file_unique_id, "demo_by_studio_bot-unique-2")
        self.assertEqual(bot.moved, [("demo_by_studio_bot-file-2", 0)])

    def test_move_sticker_rejects_out_of_range_target(self) -> None:
        with self.assertRaises(PackManagerError):
            asyncio.run(move_sticker(FakeBot(), make_pack(count=3), 0, 3))

    def test_foreign_pack_cannot_be_modified(self) -> None:
        foreign = make_pack(name="someone_else_by_other_bot", count=2)
        bot = FakeBot()
        for call in (
            delete_sticker(bot, foreign, 0),
            move_sticker(bot, foreign, 0, 1),
            rename_pack(bot, foreign, "New"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(PackManagerError):
                    asyncio.run(call)
        self.assertEqual(bot.deleted, [])
        self.assertEqual(bot.moved, [])
        self.assertEqual(bot.renamed, [])

    def test_rename_pack_normalizes_title(self) -> None:
        bot = FakeBot()
        pack = make_pack(count=1)

        title = asyncio.run(rename_pack(bot, pack, "  New   Name  "))

        self.assertEqual(title, "New Name")
        self.assertEqual(bot.renamed, [("demo_by_studio_bot", "New Name")])


class MergeTests(unittest.TestCase):
    def test_merge_plan_returns_source_stickers(self) -> None:
        target = make_pack(count=2)
        source = make_pack(name="other_by_studio_bot", count=3)

        arriving = merge_plan(target, source)

        self.assertEqual([item.file_unique_id for item in arriving],
                         [item.file_unique_id for item in source.stickers])

    def test_merge_plan_rejects_same_pack(self) -> None:
        pack = make_pack(count=1)
        with self.assertRaises(PackManagerError):
            merge_plan(pack, pack)

    def test_merge_plan_rejects_format_mix(self) -> None:
        target = make_pack(count=1)
        animated = make_pack(name="anim_by_studio_bot", count=1, fmt="animated")
        with self.assertRaises(PackManagerError) as caught:
            merge_plan(target, animated)
        self.assertIn("الصيغ", str(caught.exception))

    def test_merge_plan_rejects_overflow(self) -> None:
        target = make_pack(count=MAX_STICKERS - 1)
        source = make_pack(name="bulk_by_studio_bot", count=2)
        with self.assertRaises(PackManagerError) as caught:
            merge_plan(target, source)
        self.assertIn(str(MAX_STICKERS), str(caught.exception))

    def test_merge_plan_rejects_foreign_source(self) -> None:
        target = make_pack(count=1)
        foreign = make_pack(name="foreign_by_other_bot", count=1)
        with self.assertRaises(PackManagerError):
            merge_plan(target, foreign)


class CopyTests(unittest.TestCase):
    def test_copy_pack_recreates_set_with_same_stickers(self) -> None:
        bot = FakeBot()
        pack = make_pack(count=2)

        name = asyncio.run(
            copy_pack(bot, 42, pack, "نسخة", BOT_USERNAME, new_name="copy_by_studio_bot")
        )

        self.assertEqual(name, "copy_by_studio_bot")
        self.assertEqual(len(bot.created), 1)
        created = bot.created[0]
        self.assertEqual(created["name"], "copy_by_studio_bot")
        self.assertEqual(created["title"], "نسخة")
        self.assertEqual(created["sticker_format"], "static")
        self.assertEqual(len(created["stickers"]), 2)
        self.assertEqual(len(bot.downloaded), 2)

    def test_copy_pack_rejects_empty_pack(self) -> None:
        with self.assertRaises(PackManagerError):
            asyncio.run(copy_pack(FakeBot(), 1, make_pack(count=0), "نسخة", BOT_USERNAME))


class DuplicateTests(unittest.TestCase):
    def test_within_pack_duplicates(self) -> None:
        sticker = StickerInfo("f", "unique", "🙂", "static")
        pack = PackInfo("p_by_studio_bot", "p", (sticker, sticker), True)

        self.assertEqual(len(duplicates_in(pack)), 2)

    def test_cross_pack_duplicates_use_unique_ids(self) -> None:
        first = make_pack(count=2, unique_prefix="shared")
        second = make_pack(name="second_by_studio_bot", count=2, unique_prefix="shared")

        found = find_duplicates([first, second])
        grouped = duplicate_groups([first, second])

        self.assertEqual(set(found), {"shared-unique-0", "shared-unique-1"})
        self.assertEqual(found, grouped)
        self.assertEqual(sorted(found["shared-unique-0"]),
                         ["demo_by_studio_bot", "second_by_studio_bot"])

    def test_no_duplicates_when_ids_differ(self) -> None:
        self.assertEqual(find_duplicates([make_pack(count=2)]), {})


class ManifestAndOrderTests(unittest.TestCase):
    def test_manifest_is_json_with_every_sticker(self) -> None:
        pack = make_pack(count=2)
        manifest = json.loads(pack_manifest(pack))
        self.assertEqual(manifest["name"], pack.name)
        self.assertEqual(manifest["sticker_count"], 2)
        self.assertEqual(len(manifest["stickers"]), 2)

    def test_order_helpers_are_one_based(self) -> None:
        pack = make_pack(count=2)
        self.assertEqual(ordered_stickers(pack), list(pack.stickers))
        self.assertEqual(position_label(0), "1")


class SyncPackTests(unittest.TestCase):
    def test_sync_pack_writes_items_in_telegram_order(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.calls: list[tuple[int, list[dict]]] = []

            async def replace_pack_items(self, pack_id, items):
                self.calls.append((pack_id, list(items)))

        db = FakeDB()
        pack = make_pack(count=2)

        stored = asyncio.run(sync_pack(db, 7, pack))

        self.assertEqual(stored, 2)
        self.assertEqual(db.calls[0][0], 7)
        self.assertEqual([item["file_unique_id"] for item in db.calls[0][1]],
                         ["demo_by_studio_bot-unique-0", "demo_by_studio_bot-unique-1"])
        self.assertEqual(db.calls[0][1][0]["sticker_format"], "static")


class FriendlyErrorTests(unittest.TestCase):
    def test_telegram_errors_become_actionable_arabic(self) -> None:
        self.assertIn("غير موجودة", _friendly_error(
            Exception("Bad Request: STICKERSET_INVALID"), "قراءة الحزمة"))
        self.assertIn("حد تيليجرام", _friendly_error(
            Exception("Bad Request: too much stickers"), "الإضافة"))
        self.assertIn("صلاحية", _friendly_error(
            Exception("Bad Request: not enough rights"), "الحذف"))
        self.assertIn("فشلت", _friendly_error(Exception("boom"), "النسخ"))


class KeyboardWiringTests(unittest.TestCase):
    def test_pack_editor_links_to_the_advanced_manager(self) -> None:
        from types import SimpleNamespace

        from bot.keyboards import pack_editor_keyboard

        keyboard = pack_editor_keyboard(SimpleNamespace(id=5, name="demo_by_studio_bot"))
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("pkm:open:5", callbacks)

    def test_admin_keyboard_exposes_monitoring(self) -> None:
        from bot.keyboards import admin_keyboard

        keyboard = admin_keyboard(False)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("admin:monitor", callbacks)

    def test_main_registers_the_pack_manager_router(self) -> None:
        import bot.main as entry

        self.assertEqual(
            entry.pack_manager_router.name, "sticker-studio-pack-manager"
        )


if __name__ == "__main__":
    unittest.main()
