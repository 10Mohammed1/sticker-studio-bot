"""Safe auto-add of a converted sticker into the user's permanent pack.

Guarantees encoded here:

* nothing is written to the database before Telegram confirms the upload;
* a retry never duplicates a sticker: after an ambiguous network failure the
  live set is re-read and the addition is adopted instead of repeated;
* the 50-sticker limit rotates to the next part of the same slot instead of
  deleting anything;
* a deleted set is recreated transparently under a fresh name;
* duplicates are detected with SHA-256 of the converted file.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, InputSticker

from ..stickers import pack_short_name, sticker_format_for_path
from . import deduplication, user_packs
from .user_packs import MAX_STICKERS_PER_PACK, UserPack

log = logging.getLogger("sticker-studio.autoadd")

DEFAULT_EMOJI = "✨"
FALLBACK_EMOJI = "🙂"
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.5

STATUS_CREATED = "created"
STATUS_ADDED = "added"
STATUS_DUPLICATE = "duplicate"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class AutoAddError(RuntimeError):
    """The sticker could not be added; the message is safe to show the user."""


class _NeedsRecreate(AutoAddError):
    """Telegram no longer knows the set stored in the database."""


class _PackFull(AutoAddError):
    """The set already holds 50 stickers."""


@dataclass(frozen=True)
class AddOutcome:
    status: str
    message: str = ""
    slot: str | None = None
    title: str | None = None
    pack_name: str | None = None
    pack_id: int | None = None

    @property
    def link(self) -> str | None:
        return user_packs.pack_link(self.pack_name)

    @property
    def added(self) -> bool:
        return self.status in {STATUS_CREATED, STATUS_ADDED}


_locks: dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[user_id] = lock
    return lock


async def add_to_user_pack(
    bot: Any,
    db: Any,
    user_id: int,
    path: Path,
    *,
    bot_username: str,
    first_name: str | None = None,
    source_kind: str = "sticker",
    source_name: str | None = None,
    task_id: str | None = None,
) -> AddOutcome:
    """Turn one output file into a confirmed sticker in the permanent pack."""
    sticker_format = sticker_format_for_path(path)
    name = source_name or path.name
    if sticker_format is None:
        await _log(db, user_id, source_kind, name, path, STATUS_SKIPPED,
                   "not a sticker format", task_id)
        return AddOutcome(
            STATUS_SKIPPED,
            "ℹ️ الملف ليس بصيغة ملصق، لذلك لم يُضف إلى الحزمة.",
        )

    digest = await deduplication.sha256_async(path)

    async with _lock(user_id):
        existing = await deduplication.find(db, user_id, digest)
        if existing:
            pack_row = (
                await db.get_user_pack(int(existing["pack_id"]), user_id)
                if existing.get("pack_id") else None
            )
            pack = user_packs.from_row(pack_row) if pack_row else None
            log.info(
                "auto-add duplicate user=%s sha=%s pack=%s",
                user_id, digest[:12], getattr(pack, "name", None),
            )
            await _log(
                db, user_id, source_kind, name, path, STATUS_DUPLICATE,
                "already present in the user's pack", task_id, digest,
                pack.id if pack else None,
            )
            return AddOutcome(
                STATUS_DUPLICATE,
                "ℹ️ هذا الملصق موجود في حزمتك بالفعل، لم يُضف مرة ثانية.",
                slot=pack.slot if pack else None,
                title=pack.title if pack else None,
                pack_name=pack.name if pack else None,
                pack_id=pack.id if pack else None,
            )

        try:
            outcome = await _add_confirmed(
                bot, db, user_id, path, sticker_format, digest,
                bot_username=bot_username, first_name=first_name,
            )
        except AutoAddError as exc:
            log.warning("auto-add failed user=%s file=%s: %s", user_id, path.name, exc)
            await _log(db, user_id, source_kind, name, path, STATUS_FAILED,
                       str(exc), task_id, digest)
            return AddOutcome(STATUS_FAILED, f"⚠️ التحويل نجح، لكن: {exc}")
        except Exception as exc:  # noqa: BLE001 - never break the conversion
            log.exception("auto-add crashed user=%s file=%s", user_id, path.name)
            await _log(db, user_id, source_kind, name, path, STATUS_FAILED,
                       f"{type(exc).__name__}: {exc}", task_id, digest)
            return AddOutcome(
                STATUS_FAILED,
                "⚠️ التحويل نجح لكن حدث خطأ غير متوقع أثناء الإضافة إلى الحزمة.",
            )

        await _log(db, user_id, source_kind, name, path, outcome.status,
                   outcome.message, task_id, digest, outcome.pack_id)
        return outcome


async def _add_confirmed(
    bot: Any,
    db: Any,
    user_id: int,
    path: Path,
    sticker_format: str,
    digest: str,
    *,
    bot_username: str,
    first_name: str | None,
) -> AddOutcome:
    rotations_left = 2
    recreated = False
    while True:
        pack = await user_packs.ensure_pack(db, user_id, sticker_format, first_name)
        if pack.is_full:
            pack = await user_packs.rotate(db, pack, sticker_format, first_name)
        try:
            if not pack.name:
                return await _create(bot, db, user_id, pack, path, sticker_format,
                                     digest, bot_username)
            return await _append(bot, db, user_id, pack, path, sticker_format, digest)
        except _NeedsRecreate:
            if recreated:
                raise AutoAddError(
                    "الحزمة اختفت من تيليجرام ولم أتمكن من إنشاء بديل. أعد المحاولة."
                )
            recreated = True
            log.warning("set %s is gone; recreating a fresh one", pack.name)
            await db.set_user_pack_name(pack.id, None, sticker_count=0)
            row = await db.get_user_pack(pack.id, user_id)
            if row is None:  # pragma: no cover - defensive
                raise AutoAddError("تعذر تجهيز حزمة بديلة في قاعدة البيانات.")
            pack = user_packs.from_row(row)
            return await _create(bot, db, user_id, pack, path, sticker_format,
                                 digest, bot_username)
        except _PackFull:
            rotations_left -= 1
            if rotations_left < 0:
                raise AutoAddError(
                    "وصلت حزمك إلى الحد الأقصى لتيليجرام. راجع ⚙️ إعدادات الحزم."
                )
            log.info("pack %s is full; rotating to the next part", pack.name)
            await db.set_user_pack_count(pack.id, MAX_STICKERS_PER_PACK)
            continue


async def _create(
    bot: Any,
    db: Any,
    user_id: int,
    pack: UserPack,
    path: Path,
    sticker_format: str,
    digest: str,
    bot_username: str,
) -> AddOutcome:
    """Create the set exactly once, even if the first response is lost.

    The name is generated **before** the retry loop. After an ambiguous error
    the live set is read back: if Telegram already created it, the sticker is
    adopted instead of opening a second set and orphaning the first one.
    """
    sticker = _input_sticker(path, sticker_format)
    name = pack_short_name(user_id, bot_username)
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            await bot.create_new_sticker_set(
                user_id=user_id,
                name=name,
                title=pack.title[:64],
                stickers=[sticker],
                sticker_format=sticker_format,
            )
        except TelegramRetryAfter as exc:
            last_error = exc
            await asyncio.sleep(float(exc.retry_after) + 1.0)
            continue
        except TelegramForbiddenError as exc:
            raise AutoAddError(
                "تيليجرام رفض إنشاء الحزمة. تأكد أنك بدأت محادثة مع البوت ثم أعد المحاولة."
            ) from exc
        except TelegramBadRequest as exc:
            last_error = exc
            lowered = str(exc).lower()
            if "already" in lowered or "occupied" in lowered:
                adopted = await _adopt_created(
                    bot, db, pack, name, sticker_format, digest
                )
                if adopted is not None:
                    return adopted
                # A (very unlikely) foreign random-name collision: only then
                # pick another name and try again.
                log.warning("set name collision on attempt %d: %s", attempt + 1, exc)
                name = pack_short_name(user_id, bot_username)
                continue
            if "invalid" in lowered:
                raise AutoAddError(
                    f"تعذر إنشاء الحزمة في تيليجرام ({_short(exc)})."
                ) from exc
            raise AutoAddError(
                f"تعذر إنشاء الحزمة في تيليجرام ({_short(exc)})."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - network hiccup, maybe created
            last_error = exc
            adopted = await _adopt_created(
                bot, db, pack, name, sticker_format, digest
            )
            if adopted is not None:
                return adopted
            await asyncio.sleep(RETRY_BASE_SECONDS * (attempt + 1))
            continue

        # The API confirmed the set. Read it back to store the real sticker id.
        adopted = await _adopt_created(
            bot, db, pack, name, sticker_format, digest
        )
        if adopted is not None:
            return adopted
        # The API confirmed creation but the follow-up read failed; record the
        # confirmed set without inventing a sticker id.
        await _persist(db, pack, name, "", f"sha256:{digest}", 1, sticker_format, digest)
        return _created(pack, name, 1)

    # The last attempt may have landed even though every response was lost.
    adopted = await _adopt_created(bot, db, pack, name, sticker_format, digest)
    if adopted is not None:
        return adopted
    raise AutoAddError(f"تعذر إنشاء الحزمة في تيليجرام ({_short(last_error)}).")


async def _adopt_created(
    bot: Any,
    db: Any,
    pack: UserPack,
    name: str,
    sticker_format: str,
    digest: str,
) -> AddOutcome | None:
    """Return the freshly created set, verifying its title when possible."""
    try:
        result = await bot.get_sticker_set(name=name)
    except Exception:  # noqa: BLE001 - readback is best-effort
        return None
    stickers = list(getattr(result, "stickers", []) or [])
    if not stickers:
        return None
    title = getattr(result, "title", None)
    if title is not None and str(title) != pack.title:
        # Someone else owns this exact random name; never write into it.
        log.warning("set %s belongs to another title %r", name, title)
        return None
    last = stickers[-1]
    file_id = getattr(last, "file_id", "") or ""
    unique_id = getattr(last, "file_unique_id", "") or f"sha256:{digest}"
    await _persist(
        db, pack, name, file_id, unique_id, len(stickers), sticker_format, digest
    )
    log.info(
        "auto-pack created user=%s name=%s slot=%s title=%r",
        pack.user_id, name, pack.slot, pack.title,
    )
    return _created(pack, name, len(stickers))


def _created(pack: UserPack, name: str, count: int) -> AddOutcome:
    link = user_packs.pack_link(name)
    return AddOutcome(
        STATUS_CREATED,
        f"📦 تم إنشاء حزمتك {pack.caption} وأُضيف أول ملصق.\n🔗 {link}",
        slot=pack.slot, title=pack.title, pack_name=name, pack_id=pack.id,
    )


async def _append(
    bot: Any,
    db: Any,
    user_id: int,
    pack: UserPack,
    path: Path,
    sticker_format: str,
    digest: str,
) -> AddOutcome:
    sticker = _input_sticker(path, sticker_format)
    expected = pack.sticker_count
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            result = await bot.add_sticker_to_set(
                user_id=user_id, name=pack.name, sticker=sticker
            )
        except TelegramRetryAfter as exc:
            last_error = exc
            await asyncio.sleep(float(exc.retry_after) + 1.0)
            continue
        except TelegramBadRequest as exc:
            lowered = str(exc).lower()
            if "stickerset_invalid" in lowered or "not found" in lowered:
                raise _NeedsRecreate(str(exc)) from exc
            if any(
                token in lowered
                for token in ("too much", "too_much", "limit", "full", "maximum")
            ):
                raise _PackFull(str(exc)) from exc
            if "emoji" in lowered and "invalid" in lowered:
                sticker = _input_sticker(path, sticker_format, emoji=FALLBACK_EMOJI)
                last_error = exc
                continue
            raise AutoAddError(
                f"تيليجرام رفض إضافة الملصق ({_short(exc)})."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - ambiguous: maybe it landed
            last_error = exc
            adopted = await _adopt_if_present(
                bot, db, pack, sticker_format, digest, expected
            )
            if adopted is not None:
                return adopted
            await asyncio.sleep(RETRY_BASE_SECONDS * (attempt + 1))
            continue

        unique_id = getattr(result, "file_unique_id", "") or f"sha256:{digest}"
        file_id = getattr(result, "file_id", "") or ""
        live = await _live(bot, pack.name)
        count = live[0] if live else expected + 1
        await _persist(db, pack, pack.name, file_id, unique_id, count,
                       sticker_format, digest)
        log.info("auto-add ok user=%s pack=%s count=%d", user_id, pack.name, count)
        return _added(pack, pack.name, count)

    adopted = await _adopt_if_present(bot, db, pack, sticker_format, digest, expected)
    if adopted is not None:
        return adopted
    raise AutoAddError(
        f"تعذر إضافة الملصق إلى الحزمة ({_short(last_error)}). أعد إرسال الملف للمحاولة."
    )


async def _adopt_if_present(
    bot: Any,
    db: Any,
    pack: UserPack,
    sticker_format: str,
    digest: str,
    expected: int,
) -> AddOutcome | None:
    """After an ambiguous failure, treat a grown set as a successful add."""
    live = await _live(bot, pack.name)
    if not live or live[0] <= expected:
        return None
    count, sticker = live
    await _persist(
        db, pack, pack.name,
        getattr(sticker, "file_id", "") or "",
        getattr(sticker, "file_unique_id", "") or f"sha256:{digest}",
        count, sticker_format, digest,
    )
    log.warning(
        "auto-add adopted %s after a network error (count %d -> %d)",
        pack.name, expected, count,
    )
    return _added(pack, pack.name, count)


def _added(pack: UserPack, name: str, count: int) -> AddOutcome:
    link = user_packs.pack_link(name)
    return AddOutcome(
        STATUS_ADDED,
        f"📦 تمت إضافتها إلى حزمتك {pack.caption} ({min(count, MAX_STICKERS_PER_PACK)}/"
        f"{MAX_STICKERS_PER_PACK}).\n🔗 {link}",
        slot=pack.slot, title=pack.title, pack_name=name, pack_id=pack.id,
    )


async def _persist(
    db: Any,
    pack: UserPack,
    name: str,
    file_id: str,
    file_unique_id: str,
    count: int,
    sticker_format: str,
    digest: str,
) -> None:
    """Write the confirmed state: pack row, legacy mirror, dedupe index."""
    await db.mark_user_pack_added(
        pack.id, name, sticker_count=count, file_unique_id=file_unique_id
    )
    legacy_id = await db.register_pack(
        pack.user_id, name, pack.title, sticker_format, auto=True
    )
    await db.record_pack_item(
        legacy_id, file_id, file_unique_id, sticker_format,
        position=max(0, count - 1), emoji=DEFAULT_EMOJI,
    )
    await deduplication.remember(
        db, pack.user_id, digest, pack.id, file_id, file_unique_id
    )


async def _live(bot: Any, name: str | None) -> tuple[int, Any] | None:
    if not name:
        return None
    try:
        result = await bot.get_sticker_set(name=name)
    except Exception:  # noqa: BLE001 - verification is best-effort
        return None
    stickers = list(getattr(result, "stickers", []) or [])
    if not stickers:
        return 0, None
    return len(stickers), stickers[-1]


def _input_sticker(
    path: Path, sticker_format: str, *, emoji: str = DEFAULT_EMOJI
) -> InputSticker:
    return InputSticker(
        sticker=FSInputFile(path, filename=path.name),
        format=sticker_format,
        emoji_list=[emoji],
    )


def _short(error: Any, limit: int = 160) -> str:
    text = " ".join(str(error or "خطأ غير معروف").split())
    return text[:limit]


async def _log(
    db: Any,
    user_id: int,
    source_kind: str,
    source_name: str,
    path: Path,
    status: str,
    detail: str,
    task_id: str | None,
    digest: str | None = None,
    pack_id: int | None = None,
) -> None:
    try:
        await db.record_conversion(
            user_id, source_kind, source_name, path.suffix.lstrip(".").upper() or "?",
            status, pack_id=pack_id, task_id=task_id, detail=detail, sha256=digest,
        )
    except Exception:  # noqa: BLE001 - logging must never break the conversion
        log.warning("could not record conversion for %s", user_id, exc_info=True)


__all__ = [
    "AddOutcome",
    "AutoAddError",
    "MAX_STICKERS_PER_PACK",
    "STATUS_ADDED",
    "STATUS_CREATED",
    "STATUS_DUPLICATE",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "add_to_user_pack",
]
