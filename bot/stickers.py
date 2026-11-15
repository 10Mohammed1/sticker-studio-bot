"""Sticker-pack creation on top of the conversion engine.

The merged bot keeps two workflows independent:

* every converted file is delivered as a Document for manual upload to
  ``@Stickers`` (the original 2.1.4 behaviour);
* if the user started a pack with ``/newpack``, the same result is also added
  to their Telegram sticker set through the Bot API.

Telegram does not allow mixing static, animated, and video stickers in one set,
so the pack service enforces one format per pack and explains the limitation
instead of letting the API fail with a cryptic error.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputSticker

from .database import ActivePack, Database

log = logging.getLogger("sticker-studio.stickers")

FORMAT_LABELS = {
    "static": "ثابت (WEBP)",
    "animated": "متحرك (TGS)",
    "video": "فيديو (WEBM)",
}

FORMAT_BY_SUFFIX = {
    ".webp": "static",
    ".tgs": "animated",
    ".webm": "video",
}

DEFAULT_EMOJI = "✨"
PACK_TITLE_LIMIT = 64


@dataclass(frozen=True)
class PackResult:
    status: str  # skipped | created | added | failed
    message: str = ""


def sticker_format_for_path(path: Path) -> str | None:
    return FORMAT_BY_SUFFIX.get(path.suffix.lower())


def format_label(sticker_format: str | None) -> str:
    if not sticker_format:
        return "غير محددة بعد"
    if sticker_format == "mixed":
        return "متعددة الصيغ (غير مدعومة في تيليجرام)"
    return FORMAT_LABELS.get(sticker_format, sticker_format)


def normalize_pack_title(value: str) -> str | None:
    """Return a valid 1..64 character title, or None when the input is invalid."""
    title = (value or "").strip()
    if not title or len(title) > PACK_TITLE_LIMIT:
        return None
    return title


def auto_pack_title(first_name: str | None) -> str:
    """Default title used when the user did not create a pack explicitly."""
    name = " ".join((first_name or "").split())
    title = f"ملصقات {name}" if name else "ملصقاتي"
    return title[:PACK_TITLE_LIMIT]


AUTO_FORMAT_WORDS = {"static": "ثابت", "video": "فيديو", "animated": "متحرك"}


def format_auto_title(title: str, sticker_format: str | None) -> str:
    """Make auto-created packs distinguishable by their Telegram format."""
    word = AUTO_FORMAT_WORDS.get(sticker_format or "")
    if not word:
        return title[:PACK_TITLE_LIMIT]
    return f"{title} — {word}"[:PACK_TITLE_LIMIT]


def pack_short_name(user_id: int, bot_username: str) -> str:
    """Build a Bot API-safe sticker-set name ending in ``_by_<bot>``."""
    suffix = f"_by_{bot_username.lower()}"
    # Telegram only accepts ASCII a-z, 0-9 and underscores in set names.
    suffix = "".join(
        char if (char.isascii() and char.isalnum()) or char == "_" else "_"
        for char in suffix
    )
    budget = max(1, 64 - len(suffix))
    prefix = f"s{user_id % 10_000_000}_{secrets.token_hex(4)}"[:budget]
    return f"{prefix}{suffix}"


def pack_link(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


async def attach_to_pack(
    bot: Bot,
    db: Database,
    user_id: int,
    path: Path,
    bot_username: str,
    *,
    auto_title: str | None = None,
) -> PackResult:
    """Add one converted file to the user's active pack.

    When the user has no pack and ``auto_title`` is provided, a pack is started
    automatically so consecutive stickers land in one set without /newpack.

    Updates are serialized per user so two concurrent album items cannot both
    create the same sticker set with different names.
    """
    async with _pack_lock(user_id):
        return await _attach_to_pack(bot, db, user_id, path, bot_username, auto_title)


_pack_locks: dict[int, asyncio.Lock] = {}


def _pack_lock(user_id: int) -> asyncio.Lock:
    lock = _pack_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _pack_locks[user_id] = lock
    return lock


async def _attach_to_pack(
    bot: Bot,
    db: Database,
    user_id: int,
    path: Path,
    bot_username: str,
    auto_title: str | None = None,
) -> PackResult:
    """Implementation of :func:`attach_to_pack` without the per-user lock."""
    sticker_format = sticker_format_for_path(path)
    if sticker_format is None:
        log.info(
            "pack attach skipped user=%s file=%s: not a WEBP/TGS/WEBM sticker",
            user_id, path.name,
        )
        return PackResult(
            "skipped",
            f"الملف {path.name} ليس بصيغة ملصق (WEBP/TGS/WEBM)، لذلك لم يُضف إلى الحزمة.",
        )

    title_for_format = format_auto_title(auto_title, sticker_format) if auto_title else ""
    switched_from: str | None = None

    active: ActivePack = await db.get_active_pack(user_id)
    if not active.exists:
        if not auto_title:
            log.info("pack attach skipped user=%s: no active pack and auto mode off", user_id)
            return PackResult("skipped")
        # Auto mode: the first sticker opens a pack, and every following file
        # keeps using the same set until /endpack.
        await db.start_pack(user_id, title_for_format, auto=True)
        active = await db.get_active_pack(user_id)
        if not active.exists:  # user row missing; touch_user should have created it
            log.warning("pack attach skipped user=%s: users row is missing", user_id)
            return PackResult("skipped")
        log.info(
            "pack auto-started user=%s title=%r format=%s",
            user_id, title_for_format, sticker_format,
        )

    if active.sticker_format not in (None, "", "mixed") and active.sticker_format != sticker_format:
        # A pack the bot named itself may transparently continue in a second
        # pack for the new format; a pack the user named must never be hijacked.
        auto_pack = active.auto or (
            auto_title is not None
            and active.title
            in {auto_title, format_auto_title(auto_title, active.sticker_format)}
        )
        if not (auto_pack and auto_title):
            log.warning(
                "pack format mismatch user=%s pack=%s pack_format=%s new_format=%s",
                user_id, active.name or active.title, active.sticker_format, sticker_format,
            )
            return PackResult(
                "failed",
                "لم يُضف الملصق إلى الحزمة: تيليجرام لا يسمح بخلط الملصقات "
                f"{format_label(active.sticker_format)} مع {format_label(sticker_format)} "
                "في حزمة واحدة.\n"
                "أنهِ الحزمة بـ /endpack ثم أرسل الملف مرة أخرى لتبدأ حزمة جديدة تلقائيًا، "
                "أو استخدم /newpack لتسميتها.",
            )
        switched_from = active.sticker_format
        await db.start_pack(user_id, title_for_format, auto=True)
        active = await db.get_active_pack(user_id)
        if not active.exists:
            return PackResult("skipped")

    input_sticker = InputSticker(
        sticker=FSInputFile(path, filename=path.name),
        format=sticker_format,
        emoji_list=[DEFAULT_EMOJI],
    )

    if not active.name:
        last_error: Exception | None = None
        name = ""
        for attempt in range(2):
            name = pack_short_name(user_id, bot_username)
            try:
                await bot.create_new_sticker_set(
                    user_id=user_id,
                    name=name,
                    title=(active.title or "ملصقاتي")[:PACK_TITLE_LIMIT],
                    stickers=[input_sticker],
                    sticker_format=sticker_format,
                )
                last_error = None
                break
            except TelegramBadRequest as exc:
                last_error = exc
                log.warning(
                    "Could not create sticker set for user=%s (attempt %d): %s",
                    user_id, attempt + 1, exc,
                )
                if "already" not in str(exc).lower():
                    break
        if last_error is not None:
            return PackResult(
                "failed",
                "تعذر إنشاء حزمة الملصقات في تيليجرام.\n"
                f"السبب: {_html(str(last_error)[:250])}\n"
                "تأكد أنك بدأت محادثة مع البوت ثم أعد المحاولة.",
            )
        await db.save_active_pack(
            user_id, name, active.title or "ملصقاتي", sticker_format, auto=active.auto
        )
        log.info(
            "pack created user=%s name=%s title=%r format=%s",
            user_id, name, active.title, sticker_format,
        )
        if switched_from:
            note = (
                f"📦 بدأت حزمة جديدة <b>{_html(active.title or 'ملصقاتي')}</b> "
                f"بصيغة {format_label(sticker_format)} لأن تيليجرام لا يسمح بالخلط.\n"
                f"{pack_link(name)}"
            )
        else:
            note = (
                f"📦 أُنشئت الحزمة <b>{_html(active.title or 'ملصقاتي')}</b> "
                f"وأُضيف أول ملصق ({format_label(sticker_format)}).\n"
                f"{pack_link(name)}"
            )
        return PackResult("created", note)

    try:
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=active.name,
            sticker=input_sticker,
        )
    except TelegramBadRequest as exc:
        text = str(exc)
        log.warning("Could not add sticker to user=%s pack=%s: %s", user_id, active.name, text)
        lowered = text.lower()
        if "stickerset_invalid" in lowered or "not found" in lowered:
            await db.forget_pack_name(user_id)
            return PackResult(
                "failed",
                "حزمة تيليجرام السابقة لم تعد موجودة (ربما حُذفت).\n"
                "أرسل الملف مرة أخرى وسأُنشئ الحزمة من جديد بنفس الاسم.",
            )
        if "too much" in lowered or "limit" in lowered or "full" in lowered:
            return PackResult(
                "failed",
                "وصلت الحزمة إلى الحد الأقصى لعدد الملصقات في تيليجرام.\n"
                "أنهِ الحزمة بـ /endpack ثم أرسل الملف مرة أخرى لتبدأ حزمة جديدة.",
            )
        return PackResult(
            "failed",
            "تعذر إضافة الملصق إلى الحزمة.\n"
            f"السبب: {_html(text[:250])}",
        )

    if active.sticker_format in (None, "") and sticker_format:
        await db.set_active_pack_format(user_id, sticker_format)
    log.info(
        "pack sticker added user=%s pack=%s file=%s",
        user_id, active.name, path.name,
    )
    return PackResult(
        "added",
        f"📦 أُضيف الملصق إلى حزمتك <b>{_html(active.title or 'ملصقاتي')}</b>.\n"
        f"{pack_link(active.name)}",
    )


def _html(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
