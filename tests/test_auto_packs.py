"""End-to-end tests for the 4.1 permanent two-pack system.

Every scenario from the specification is covered against a real SQLite file
and a fake Telegram that enforces the same rules as the Bot API (one format
per set, 50 stickers max, sets must exist, network errors are transient).
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456789:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sticker-studio-autopack-"))

from PIL import Image, ImageDraw  # noqa: E402

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError  # noqa: E402

from bot import backups  # noqa: E402
from bot.converter import OutputFile, process_file  # noqa: E402
from bot.database import Database  # noqa: E402
from bot.packs import auto_add, auto_manager, deduplication, user_packs  # noqa: E402

BOT_USERNAME = "studio_bot"
OWNER = 7001
OTHER = 7002
MAX = 50


@dataclass
class FakeSticker:
    file_id: str
    file_unique_id: str
    emoji: str | None = "✨"
    is_animated: bool = False
    is_video: bool = False


def ffmpeg_binaries() -> tuple[str, str] | None:
    """Locate FFmpeg/FFprobe from PATH or the local static-ffmpeg dev helper."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    try:
        from static_ffmpeg import run

        ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise()
        return str(ffmpeg), str(ffprobe)
    except Exception:  # noqa: BLE001 - optional local test dependency
        return None


class FakeTelegram:
    """Minimal Bot API surface used by the auto-add pipeline."""

    def __init__(self) -> None:
        self.sets: dict[str, dict] = {}
        self.create_calls: list[dict] = []
        self.add_calls: list[tuple[str, int]] = []
        self.downloaded: list[str] = []
        self.fail_next_adds = 0
        self.land_then_fail = False
        self.create_land_then_fail = False
        self.missing: set[str] = set()
        self.limit = MAX

    # -- helpers -----------------------------------------------------
    def sticker_count(self, name: str) -> int:
        return len(self.sets[name]["stickers"])

    def names(self) -> list[str]:
        return sorted(self.sets)

    def set_title(self, name: str) -> str:
        return self.sets[name]["title"]

    # -- Bot API -----------------------------------------------------
    async def create_new_sticker_set(
        self, user_id: int, name: str, title: str, stickers, sticker_format: str
    ):
        self.create_calls.append(
            {"user_id": user_id, "name": name, "title": title,
             "format": sticker_format, "count": len(stickers)}
        )
        if name in self.sets:
            raise TelegramBadRequest(
                method="createNewStickerSet", message="Bad Request: name already taken"
            )
        new = [
            FakeSticker(file_id=f"{name}-f0", file_unique_id=f"{name}-u0",
                        is_video=sticker_format == "video",
                        is_animated=sticker_format == "animated")
        ]
        self.sets[name] = {
            "title": title, "format": sticker_format, "owner": user_id,
            "stickers": new,
        }
        if self.create_land_then_fail:
            self.create_land_then_fail = False
            raise TelegramNetworkError(
                method="createNewStickerSet", message="response lost"
            )
        return True

    async def add_sticker_to_set(self, user_id: int, name: str, sticker):
        self.add_calls.append((name, user_id))
        if self.fail_next_adds > 0:
            self.fail_next_adds -= 1
            raise TelegramNetworkError(method="addStickerToSet", message="connection reset")
        if name in self.missing or name not in self.sets:
            raise TelegramBadRequest(
                method="addStickerToSet", message="Bad Request: STICKERSET_INVALID"
            )
        pack = self.sets[name]
        if len(pack["stickers"]) >= self.limit:
            raise TelegramBadRequest(
                method="addStickerToSet",
                message="Bad Request: STICKERS_TOO_MUCH 50 stickers max",
            )
        position = len(pack["stickers"])
        created = FakeSticker(
            file_id=f"{name}-f{position}", file_unique_id=f"{name}-u{position}",
            is_video=pack["format"] == "video",
            is_animated=pack["format"] == "animated",
        )
        pack["stickers"].append(created)
        if self.land_then_fail:
            self.land_then_fail = False
            raise TelegramNetworkError(method="addStickerToSet", message="response lost")
        return created

    async def get_sticker_set(self, name: str):
        if name in self.missing or name not in self.sets:
            raise TelegramBadRequest(
                method="getStickerSet", message="Bad Request: STICKERSET_INVALID"
            )
        pack = self.sets[name]
        return SimpleNamespace(
            name=name,
            title=pack["title"],
            stickers=list(pack["stickers"]),
        )

    async def get_file(self, file_id: str):
        return SimpleNamespace(file_path=f"stickers/{file_id}.webp")

    async def download_file(self, file_path: str):
        import io

        self.downloaded.append(file_path)
        return io.BytesIO(b"RIFF-fake")


class Harness(unittest.TestCase):
    """Shared temp database + fake Telegram, reset for each test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "stickers.sqlite3"
        self.db = Database(self.db_path)
        asyncio.run(self.db.init(maintenance_default=False))
        asyncio.run(self.db.touch_user(OWNER, "owner", "Owner"))
        asyncio.run(self.db.touch_user(OTHER, "other", "Other"))
        self.bot = FakeTelegram()
        self._settings = auto_manager.settings
        auto_manager.settings = dataclasses.replace(self._settings, auto_pack=True)
        self._delay = auto_add.RETRY_BASE_SECONDS
        auto_add.RETRY_BASE_SECONDS = 0
        auto_add._locks.clear()

    def tearDown(self) -> None:
        auto_manager.settings = self._settings
        auto_add.RETRY_BASE_SECONDS = self._delay
        auto_add._locks.clear()
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------
    def sticker(self, name: str, payload: bytes = b"pixels", **subdirs: str) -> Path:
        folder = self.root / "work" / (subdirs.get("folder") or "run")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(payload)
        return path

    def add(self, path: Path, user: int = OWNER, first_name: str = "Owner"):
        return asyncio.run(
            auto_add.add_to_user_pack(
                self.bot, self.db, user, path,
                bot_username=BOT_USERNAME, first_name=first_name,
            )
        )

    def packs(self, user: int = OWNER) -> list[dict]:
        return asyncio.run(self.db.get_user_packs(user))

    def conversions(self, user: int = OWNER) -> list[dict]:
        return asyncio.run(self.db.get_conversions(user, 20))

    def conversion_settings(self, *, enable_bg_removal: bool = False):
        pair = ffmpeg_binaries() or ("ffmpeg", "ffprobe")
        return SimpleNamespace(
            max_archive_items=10,
            max_expanded_mb=50,
            max_pdf_pages=2,
            enable_bg_removal=enable_bg_removal,
            ffmpeg_path=pair[0],
            ffprobe_path=pair[1],
            max_processing_seconds=120,
        )


class TwoPackTests(Harness):
    """Scenarios 1-5: one permanent pack per kind per user."""

    def test_01_first_image_creates_the_static_pack(self) -> None:
        outcome = self.add(self.sticker("first.webp", b"one"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(len(self.bot.sets), 1)
        name = outcome.pack_name
        self.assertTrue(name.endswith(f"_by_{BOT_USERNAME}"))
        self.assertEqual(self.bot.sticker_count(name), 1)
        packs = self.packs()
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]["slot"], user_packs.SLOT_STATIC)
        self.assertEqual(packs[0]["sticker_count"], 1)
        self.assertEqual(packs[0]["name"], name)

    def test_02_second_image_reuses_the_same_pack(self) -> None:
        first = self.add(self.sticker("a.webp", b"one"))
        second = self.add(self.sticker("b.webp", b"two"))
        self.assertEqual(first.pack_name, second.pack_name)
        self.assertEqual(second.status, auto_add.STATUS_ADDED)
        self.assertEqual(len(self.bot.create_calls), 1)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 2)
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 2)

    def test_03_first_gif_creates_the_animated_pack(self) -> None:
        outcome = self.add(self.sticker("loop.webm", b"gif-bytes"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(outcome.slot, user_packs.SLOT_ANIMATED)
        self.assertEqual(self.bot.sets[outcome.pack_name]["format"], "video")
        self.assertEqual(len(self.bot.sets), 1)

    def test_04_second_animated_file_reuses_the_animated_pack(self) -> None:
        first = self.add(self.sticker("one.webm", b"g1"))
        second = self.add(self.sticker("two.webm", b"g2"))
        self.assertEqual(first.pack_name, second.pack_name)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 2)
        self.assertEqual(len(self.bot.create_calls), 1)

    def test_05_two_users_never_share_a_pack(self) -> None:
        mine = self.add(self.sticker("mine.webp", b"mine"))
        theirs = self.add(self.sticker("theirs.webp", b"theirs"), user=OTHER, first_name="Other")
        self.assertNotEqual(mine.pack_name, theirs.pack_name)
        self.assertEqual(len(self.bot.sets), 2)
        self.assertEqual(self.bot.sticker_count(mine.pack_name), 1)
        self.assertEqual(self.bot.sticker_count(theirs.pack_name), 1)
        self.assertEqual(len(self.packs(OWNER)), 1)
        self.assertEqual(len(self.packs(OTHER)), 1)

    def test_tgs_slot_is_created_only_when_needed(self) -> None:
        static = self.add(self.sticker("a.webp", b"a"))
        tgs = self.add(self.sticker("anim.tgs", b"tgs"))
        self.assertEqual(tgs.slot, user_packs.SLOT_ANIMATED_TGS)
        self.assertNotEqual(static.pack_name, tgs.pack_name)
        self.assertEqual(self.bot.sets[tgs.pack_name]["format"], "animated")


class ExplicitPackCompatibilityTests(Harness):
    """4.0 /newpack and /packs -> use must keep working as an override."""

    def test_newpack_override_receives_outputs_and_leaves_permanent_pack_empty(self) -> None:
        asyncio.run(self.db.start_pack(OWNER, "حزمة مخصصة"))
        outputs = [OutputFile(self.sticker("named.webp", b"named"), "WEBP", "")]
        outcomes = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER, outputs,
                bot_username=BOT_USERNAME, first_name="Owner",
            )
        )
        self.assertEqual(outcomes[0].status, auto_add.STATUS_CREATED)
        names = self.bot.names()
        self.assertEqual(len(names), 1)
        self.assertEqual(self.bot.set_title(names[0]), "حزمة مخصصة")
        self.assertEqual(self.packs(OWNER), [])  # permanent pair untouched
        self.assertEqual(self.conversions()[0]["status"], "created")

        # /endpack returns to the permanent-pack default.
        asyncio.run(self.db.clear_active_pack(OWNER))
        after = self.add(self.sticker("after.webp", b"after"))
        self.assertEqual(after.slot, user_packs.SLOT_STATIC)
        self.assertEqual(len(self.packs(OWNER)), 1)

    def test_explicit_pack_skips_a_duplicate_sticker(self) -> None:
        asyncio.run(self.db.start_pack(OWNER, "حزمة مخصصة"))
        path = self.sticker("same.webp", b"same")
        first = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER, [OutputFile(path, "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        second = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER, [OutputFile(path, "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        self.assertEqual(first[0].status, auto_add.STATUS_CREATED)
        self.assertEqual(second[0].status, auto_add.STATUS_DUPLICATE)
        self.assertEqual(len(self.bot.sets), 1)
        self.assertEqual(self.bot.sticker_count(self.bot.names()[0]), 1)


class ManualPackOnlyTests(Harness):
    """Automatic packs off: only /newpack creates a pack."""

    def test_disabled_auto_add_creates_no_pack_for_a_plain_photo(self) -> None:
        asyncio.run(self.db.set_user_preference(OWNER, "auto_pack", False))
        outcomes = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER,
                [OutputFile(self.sticker("plain.webp", b"plain"), "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        self.assertEqual(outcomes, [])
        self.assertEqual(self.bot.sets, {})
        self.assertEqual(self.packs(OWNER), [])
        self.assertEqual(self.conversions()[0]["status"], "disabled")

    def test_disabled_auto_add_still_feeds_a_newpack_pack(self) -> None:
        asyncio.run(self.db.set_user_preference(OWNER, "auto_pack", False))
        asyncio.run(self.db.start_pack(OWNER, "حزمتي اليدوية"))
        outcomes = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER,
                [OutputFile(self.sticker("manual.webp", b"manual"), "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        self.assertEqual(outcomes[0].status, auto_add.STATUS_CREATED)
        names = self.bot.names()
        self.assertEqual(len(names), 1)
        self.assertEqual(self.bot.set_title(names[0]), "حزمتي اليدوية")
        self.assertEqual(self.bot.sticker_count(names[0]), 1)
        self.assertEqual(self.packs(OWNER), [])  # no automatic pair
        self.assertEqual(self.conversions()[0]["status"], "created")

    def test_host_kill_switch_still_allows_a_manual_pack(self) -> None:
        auto_manager.settings = dataclasses.replace(self._settings, auto_pack=False)
        asyncio.run(self.db.start_pack(OWNER, "يدوية مع القفل"))
        outcomes = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER,
                [OutputFile(self.sticker("locked.webp", b"locked"), "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        self.assertEqual(outcomes[0].status, auto_add.STATUS_CREATED)
        self.assertEqual(self.bot.sticker_count(self.bot.names()[0]), 1)
        # A second file without an explicit pack must not create anything.
        asyncio.run(self.db.clear_active_pack(OWNER))
        skipped = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER,
                [OutputFile(self.sticker("free.webp", b"free"), "WEBP", "")],
                bot_username=BOT_USERNAME,
            )
        )
        self.assertEqual(skipped, [])
        self.assertEqual(len(self.bot.sets), 1)
        self.assertEqual(self.conversions()[0]["status"], "disabled")


class RestartTests(Harness):
    """Scenario 6: a restart must keep using the same Telegram sets."""

    def test_06_packs_survive_a_process_restart(self) -> None:
        first = self.add(self.sticker("before.webp", b"before"))
        # Simulate a restart: new Database object, cold in-memory caches.
        self.db = Database(self.db_path)
        asyncio.run(self.db.init(maintenance_default=False))
        auto_add._locks.clear()
        user_packs_adopted = asyncio.run(user_packs.list_packs(self.db, OWNER))
        self.assertEqual([pack.name for pack in user_packs_adopted], [first.pack_name])

        second = self.add(self.sticker("after.webp", b"after"))
        self.assertEqual(second.pack_name, first.pack_name)
        self.assertEqual(len(self.bot.sets), 1)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 2)


class ToggleTests(Harness):
    """Scenarios 7-9: the switch never stops conversion."""

    def setUp(self) -> None:
        super().setUp()
        self.first = self.add(self.sticker("seed.webp", b"seed"))

    def test_07_disabling_auto_add_is_persisted(self) -> None:
        self.assertTrue(asyncio.run(auto_manager.enabled(self.db, OWNER)))
        asyncio.run(auto_manager.set_enabled(self.db, OWNER, False))
        self.assertFalse(asyncio.run(auto_manager.enabled(self.db, OWNER)))
        # A fresh Database object (restart) still reads the saved choice.
        reopened = Database(self.db_path)
        asyncio.run(reopened.init(maintenance_default=False))
        self.assertFalse(asyncio.run(auto_manager.enabled(reopened, OWNER)))

    def test_08_conversion_continues_while_disabled(self) -> None:
        asyncio.run(auto_manager.set_enabled(self.db, OWNER, False))
        outputs = [OutputFile(self.sticker("off.webp", b"off"), "WEBP", "")]
        outcomes = asyncio.run(
            auto_manager.handle_outputs(
                self.bot, self.db, OWNER, outputs,
                bot_username=BOT_USERNAME, first_name="Owner",
            )
        )
        self.assertEqual(outcomes, [])
        self.assertEqual(len(self.bot.create_calls), 1)  # only the seed pack
        self.assertEqual(self.bot.sticker_count(self.first.pack_name), 1)
        latest = self.conversions()[0]
        self.assertEqual(latest["status"], "disabled")
        text = auto_manager.summary_text([], converted=1, auto_enabled=False)
        self.assertIn("الإضافة التلقائية متوقفة", text)

    def test_09_reenabling_adds_to_the_same_pack(self) -> None:
        asyncio.run(auto_manager.set_enabled(self.db, OWNER, False))
        asyncio.run(auto_manager.set_enabled(self.db, OWNER, True))
        outcome = self.add(self.sticker("again.webp", b"again"))
        self.assertEqual(outcome.status, auto_add.STATUS_ADDED)
        self.assertEqual(outcome.pack_name, self.first.pack_name)
        self.assertEqual(self.bot.sticker_count(self.first.pack_name), 2)
        self.assertEqual(len(self.bot.sets), 1)


class FailureTests(Harness):
    """Scenarios 10-11: retries never duplicate, duplicates are skipped."""

    def test_10a_transient_network_error_is_retried(self) -> None:
        self.bot.fail_next_adds = 0  # first upload creates the set
        first = self.add(self.sticker("one.webp", b"one"))
        self.bot.fail_next_adds = 1
        second = self.add(self.sticker("two.webp", b"two"))
        self.assertEqual(second.status, auto_add.STATUS_ADDED)
        self.assertEqual(second.pack_name, first.pack_name)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 2)
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 2)

    def test_10b_lost_response_is_adopted_not_repeated(self) -> None:
        first = self.add(self.sticker("one.webp", b"one"))
        self.bot.land_then_fail = True
        second = self.add(self.sticker("two.webp", b"two"))
        self.assertEqual(second.status, auto_add.STATUS_ADDED)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 2)  # not 3
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 2)
        self.assertEqual(len(self.bot.add_calls), 1)  # adopted, never repeated

    def test_10c_persistent_outage_reports_failure_without_writing(self) -> None:
        first = self.add(self.sticker("one.webp", b"one"))
        self.bot.fail_next_adds = 99
        failed = self.add(self.sticker("two.webp", b"two"))
        self.assertEqual(failed.status, auto_add.STATUS_FAILED)
        self.assertIn("⚠️", failed.message)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 1)
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 1)
        self.assertIsNone(
            asyncio.run(self.db.get_dedupe(OWNER, deduplication.sha256_file(
                self.root / "work" / "run" / "two.webp"
            )))
        )
        self.assertEqual(self.conversions()[0]["status"], "failed")

    def test_10d_lost_create_response_reuses_the_same_set(self) -> None:
        self.bot.create_land_then_fail = True
        outcome = self.add(self.sticker("first.webp", b"first"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(len(self.bot.sets), 1)  # no orphaned duplicate set
        self.assertEqual(len(self.bot.create_calls), 1)
        self.assertEqual(self.bot.sticker_count(outcome.pack_name), 1)
        self.assertEqual(self.packs(OWNER)[0]["name"], outcome.pack_name)
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 1)

    def test_10e_foreign_name_collision_never_writes_into_it(self) -> None:
        foreign_name = "taken_by_studio_bot"
        self.bot.sets[foreign_name] = {
            "title": "حزمة شخص آخر", "format": "static", "owner": OTHER,
            "stickers": [FakeSticker(file_id="foreign-f", file_unique_id="foreign-u")],
        }
        with patch(
            "bot.packs.auto_add.pack_short_name",
            side_effect=[foreign_name, "mine_by_studio_bot"],
        ):
            outcome = self.add(self.sticker("mine.webp", b"mine"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(outcome.pack_name, "mine_by_studio_bot")
        self.assertEqual(self.bot.sticker_count(foreign_name), 1)  # untouched
        self.assertEqual(self.bot.sticker_count("mine_by_studio_bot"), 1)

    def test_last_added_at_is_persisted_after_confirmation(self) -> None:
        first = self.add(self.sticker("one.webp", b"one"))
        row = self.packs(OWNER)[0]
        self.assertTrue(row["last_added_at"])
        self.assertEqual(row["sticker_count"], 1)
        self.assertEqual(row["name"], first.pack_name)
        self.assertEqual(row["last_file_unique_id"], f"{first.pack_name}-u0")

    def test_11_duplicate_file_is_not_added_twice(self) -> None:
        path = self.sticker("same.webp", b"identical")
        first = self.add(path)
        second = self.add(path)
        self.assertEqual(second.status, auto_add.STATUS_DUPLICATE)
        self.assertIn("بالفعل", second.message)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 1)
        self.assertEqual(self.packs(OWNER)[0]["sticker_count"], 1)
        self.assertGreaterEqual(
            len([row for row in self.conversions() if row["status"] == "duplicate"]), 1
        )

    def test_same_bytes_with_new_name_is_still_a_duplicate(self) -> None:
        first = self.add(self.sticker("one.webp", b"same-bytes"))
        second = self.add(self.sticker("copy.webp", b"same-bytes"))
        self.assertEqual(first.pack_name, second.pack_name)
        self.assertEqual(second.status, auto_add.STATUS_DUPLICATE)
        self.assertEqual(self.bot.sticker_count(first.pack_name), 1)


class LimitTests(Harness):
    """Scenario 12: the 50-sticker limit rotates without deleting anything."""

    def test_12_full_pack_opens_part_two(self) -> None:
        first = self.add(self.sticker("first.webp", b"first"))
        pack = self.packs()[0]
        # Fill the live set to Telegram's limit, exactly like a busy user.
        self.bot.sets[first.pack_name]["stickers"] = [
            FakeSticker(file_id=f"{first.pack_name}-x{i}",
                        file_unique_id=f"{first.pack_name}-x{i}")
            for i in range(MAX)
        ]
        asyncio.run(self.db.set_user_pack_count(pack["id"], MAX))

        outcome = self.add(self.sticker("fifty-first.webp", b"next"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertNotEqual(outcome.pack_name, first.pack_name)
        self.assertEqual(self.bot.sticker_count(first.pack_name), MAX)  # untouched
        self.assertEqual(self.bot.sticker_count(outcome.pack_name), 1)
        packs = sorted(self.packs(), key=lambda row: row["part"])
        self.assertEqual([row["part"] for row in packs], [1, 2])
        self.assertEqual(packs[1]["sticker_format"], "static")

    def test_full_pack_from_telegram_error_rotates(self) -> None:
        first = self.add(self.sticker("first.webp", b"first"))
        self.bot.limit = 1  # Telegram reports "too much" on the next add
        outcome = self.add(self.sticker("second.webp", b"second"))
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertNotEqual(outcome.pack_name, first.pack_name)
        self.assertEqual(len(self.packs()), 2)


class ConversionIntegrationTests(Harness):
    """Real files through ``process_file`` and into the permanent packs."""

    def test_static_png_becomes_transparent_webp_and_joins_static_pack(self) -> None:
        uploads = self.root / "uploads"
        uploads.mkdir()
        source = uploads / "portrait.png"
        image = Image.new("RGBA", (320, 240), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((40, 20, 280, 220), fill=(220, 40, 90, 255))
        image.save(source)

        outputs = process_file(
            source, "portrait.png", self.root / "converted", self.conversion_settings()
        )
        self.assertEqual(len(outputs), 1)
        sticker = outputs[0].path
        self.assertEqual(sticker.suffix.lower(), ".webp")
        with Image.open(sticker) as check:
            self.assertEqual(check.size, (512, 512))
            self.assertEqual(check.format, "WEBP")
            self.assertEqual(check.convert("RGBA").getpixel((0, 0))[3], 0)

        outcome = self.add(sticker)
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(outcome.slot, user_packs.SLOT_STATIC)
        self.assertEqual(self.bot.sets[outcome.pack_name]["format"], "static")
        self.assertEqual(self.bot.sticker_count(outcome.pack_name), 1)

    def test_gif_becomes_vp9_webm_and_joins_animated_pack(self) -> None:
        pair = ffmpeg_binaries()
        if pair is None:
            self.skipTest("FFmpeg/FFprobe not installed")

        uploads = self.root / "uploads"
        uploads.mkdir()
        source = uploads / "loop.gif"
        frames = []
        for offset, color in ((0, (255, 0, 0, 255)), (12, (0, 120, 255, 255))):
            frame = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            ImageDraw.Draw(frame).ellipse(
                (20 + offset, 20, 140 + offset, 140), fill=color
            )
            frames.append(frame)
        frames[0].save(
            source, save_all=True, append_images=frames[1:],
            duration=120, loop=0, disposal=2,
        )

        outputs = process_file(
            source, "loop.gif", self.root / "converted", self.conversion_settings()
        )
        self.assertEqual(len(outputs), 1)
        sticker = outputs[0].path
        self.assertEqual(sticker.suffix.lower(), ".webm")
        self.assertEqual(sticker.read_bytes()[:4], b"\x1a\x45\xdf\xa3")
        probe = subprocess.run(
            [
                pair[1], "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "default=nw=1", str(sticker),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        self.assertIn("codec_name=vp9", probe)
        self.assertIn("width=512", probe)

        # Transparency survives the GIF -> VP9 alpha conversion.
        frame = self.root / "converted" / "gif_frame.png"
        subprocess.run(
            [
                pair[0], "-hide_banner", "-loglevel", "error", "-y",
                "-c:v", "libvpx-vp9", "-i", str(sticker),
                "-frames:v", "1", "-pix_fmt", "rgba", str(frame),
            ],
            check=True, timeout=60,
        )
        with Image.open(frame) as decoded:
            rgba = decoded.convert("RGBA")
            self.assertEqual(rgba.getpixel((0, 0))[3], 0)
            self.assertEqual(rgba.getpixel((256, 256))[3], 255)

        outcome = self.add(sticker)
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(outcome.slot, user_packs.SLOT_ANIMATED)
        self.assertEqual(self.bot.sets[outcome.pack_name]["format"], "video")

    def test_mp4_becomes_video_sticker(self) -> None:
        pair = ffmpeg_binaries()
        if pair is None:
            self.skipTest("FFmpeg/FFprobe not installed")

        uploads = self.root / "uploads"
        uploads.mkdir()
        source = uploads / "clip.mp4"
        subprocess.run(
            [
                pair[0], "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=256x256:d=1:r=10",
                "-c:v", "mpeg4", "-q:v", "5", str(source),
            ],
            check=True, timeout=60,
        )

        outputs = process_file(
            source, "clip.mp4", self.root / "converted", self.conversion_settings()
        )
        self.assertEqual(len(outputs), 1)
        sticker = outputs[0].path
        self.assertEqual(sticker.suffix.lower(), ".webm")
        outcome = self.add(sticker)
        self.assertEqual(outcome.status, auto_add.STATUS_CREATED)
        self.assertEqual(outcome.slot, user_packs.SLOT_ANIMATED)
        self.assertEqual(self.bot.sets[outcome.pack_name]["format"], "video")


class MigrationTests(unittest.TestCase):
    """Scenario 13: schema upgrades keep every old row."""

    def test_13_existing_4_0_data_survives_and_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            db = Database(path)
            asyncio.run(db.init(maintenance_default=False))
            asyncio.run(db.touch_user(99, "legacy", "Legacy"))
            asyncio.run(db.set_user_preference(99, "auto_pack", False))
            asyncio.run(
                db.save_active_pack(99, "old_by_studio_bot", "حزمة قديمة", "static", auto=True)
            )
            row = asyncio.run(db.get_packs(99))[0]
            asyncio.run(
                db.replace_pack_items(
                    row.id,
                    [{"file_unique_id": "u1", "file_id": "f1", "sticker_format": "static",
                      "position": 0, "emoji": "🙂"}],
                )
            )

            # Upgrade in place: init() is idempotent and additive.
            reopened = Database(path)
            asyncio.run(reopened.init(maintenance_default=False))
            users = asyncio.run(reopened.all_user_ids())
            packs = asyncio.run(reopened.get_packs(99))
            prefs = asyncio.run(reopened.get_user_preferences(99))
            self.assertIn(99, users)
            self.assertEqual([pack.name for pack in packs], ["old_by_studio_bot"])
            self.assertFalse(prefs["auto_pack"])
            self.assertEqual(asyncio.run(reopened.get_pack_items(row.id))[0]["file_id"], "f1")

            imported = asyncio.run(user_packs.adopt_legacy(reopened, 99))
            self.assertEqual(imported, 1)
            permanent = asyncio.run(user_packs.list_packs(reopened, 99))
            self.assertEqual(permanent[0].name, "old_by_studio_bot")
            self.assertEqual(permanent[0].sticker_count, 1)
            self.assertEqual(permanent[0].slot, user_packs.SLOT_STATIC)

    def test_new_tables_do_not_break_old_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "schema.sqlite3")
            asyncio.run(db.init(maintenance_default=False))
            asyncio.run(db.init(maintenance_default=False))  # idempotent
            stats = asyncio.run(db.get_stats())
            self.assertEqual(stats.users, 0)
            self.assertEqual(asyncio.run(db.get_user_packs(1)), [])
            self.assertEqual(asyncio.run(db.get_conversions(1)), [])

    def test_numeric_migrations_are_applied_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "migrations.sqlite3"
            db = Database(path)
            asyncio.run(db.init(maintenance_default=False))
            connection = sqlite3.connect(path)
            try:
                versions = {
                    int(row[0]) for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
            finally:
                connection.close()
            self.assertEqual(versions, {4, 5})
            # Re-running init must not apply or duplicate anything.
            self.assertEqual(asyncio.run(db.apply_migrations()), [])
            asyncio.run(db.init(maintenance_default=False))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    2,
                )
            finally:
                connection.close()


class BackupTests(unittest.TestCase):
    """The periodic copy must be a consistent, verified SQLite snapshot."""

    def test_backup_contains_users_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live.sqlite3"
            db = Database(live)
            asyncio.run(db.init(maintenance_default=False))
            asyncio.run(db.touch_user(41, "backup", "Backup"))
            asyncio.run(db.set_user_preference(41, "auto_pack", False))

            copy = backups.create_backup(live, root / "backups", keep=3)
            self.assertTrue(copy.exists())
            connection = sqlite3.connect(copy)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT auto_pack FROM user_preferences WHERE user_id=41"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()
            # The live database is still usable after the copy.
            self.assertTrue(asyncio.run(db.get_user_preferences(41)))

    def test_old_backups_are_pruned_but_live_database_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live.sqlite3"
            db = Database(live)
            asyncio.run(db.init(maintenance_default=False))
            folder = root / "backups"
            for _ in range(6):
                backups.create_backup(live, folder, keep=2)
            files = sorted(folder.glob("sticker-studio-*.sqlite3"))
            self.assertEqual(len(files), 2)
            self.assertTrue(live.exists())

    def test_missing_database_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                backups.create_backup(
                    Path(temp_dir) / "missing.sqlite3", Path(temp_dir) / "backups"
                )


class RenderingTests(Harness):
    def test_settings_view_shows_toggle_state(self) -> None:
        self.add(self.sticker("a.webp", b"a"))
        text, keyboard = asyncio.run(auto_manager.settings_view(self.db, OWNER))
        self.assertIn("مفعّلة", text)
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard
            for button in row if button.callback_data
        ]
        self.assertIn("autopack:off", callbacks)
        asyncio.run(auto_manager.set_enabled(self.db, OWNER, False))
        text, keyboard = asyncio.run(auto_manager.settings_view(self.db, OWNER))
        self.assertIn("متوقفة", text)
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard
            for button in row if button.callback_data
        ]
        self.assertIn("autopack:on", callbacks)

    def test_packs_view_lists_links(self) -> None:
        outcome = self.add(self.sticker("a.webp", b"a"))
        text, keyboard = asyncio.run(auto_manager.packs_view(self.db, OWNER, "static"))
        self.assertIn("1/50", text)
        urls = [
            button.url for row in keyboard.inline_keyboard
            for button in row if button.url
        ]
        self.assertEqual(urls, [f"https://t.me/addstickers/{outcome.pack_name}"])

    def test_summary_keyboard_links_each_pack_once(self) -> None:
        first = self.add(self.sticker("a.webp", b"a"))
        second = self.add(self.sticker("b.webm", b"b"))
        outcomes = [first, second]
        text = auto_manager.summary_text(outcomes, converted=2, auto_enabled=True)
        self.assertIn("تم تحويل 2", text)
        keyboard = auto_manager.summary_keyboard(outcomes)
        self.assertIsNotNone(keyboard)
        urls = [
            button.url for row in keyboard.inline_keyboard
            for button in row if button.url
        ]
        self.assertEqual(len(urls), 2)

    def test_history_text_mentions_statuses(self) -> None:
        self.add(self.sticker("a.webp", b"a"))
        self.add(self.sticker("b.webp", b"b"))
        text = asyncio.run(auto_manager.history_text(self.db, OWNER))
        self.assertIn("سجل التحويلات", text)
        self.assertIn("أُضيف", text)

    def test_title_helpers(self) -> None:
        self.assertEqual(
            user_packs.default_title("محمد", user_packs.SLOT_STATIC),
            "ملصقات محمد — ثابتة",
        )
        self.assertTrue(
            user_packs.default_title(None, user_packs.SLOT_ANIMATED, 2).endswith("(2)")
        )
        self.assertEqual(user_packs.slot_for_format("video"), user_packs.SLOT_ANIMATED)
        self.assertEqual(user_packs.slot_for_format("animated"), user_packs.SLOT_ANIMATED_TGS)
        with self.assertRaises(user_packs.UserPackError):
            user_packs.slot_for_format("pdf")


if __name__ == "__main__":
    unittest.main()
