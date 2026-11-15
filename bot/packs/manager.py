"""Telegram-side sticker pack operations (Pack Manager Pro).

Telegram is the source of truth: every action re-reads the set before touching
it, so a stale local row can never delete or move the wrong sticker.

Bot API restrictions encoded here:

* a bot may only edit sets whose name ends with ``_by_<its own username>``;
* one set holds at most 50 stickers and a single format (WEBP/WEBM/TGS);
* sticker order is the order returned by ``getStickerSet``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputSticker

MAX_STICKERS = 50
DEFAULT_EMOJI = "✨"
FORMAT_BY_EXTENSION = {".webp": "static", ".webm": "video", ".tgs": "animated"}
EXTENSION_BY_FORMAT = {"static": ".webp", "video": ".webm", "animated": ".tgs"}
FORMAT_AR = {"static": "ثابت WEBP", "video": "فيديو WEBM", "animated": "متحرك TGS"}


class PackManagerError(RuntimeError):
    """A user-facing explanation of why a pack action cannot be completed."""


@dataclass(frozen=True)
class StickerInfo:
    file_id: str
    file_unique_id: str
    emoji: str | None
    sticker_format: str

    @property
    def label(self) -> str:
        return FORMAT_AR.get(self.sticker_format, self.sticker_format)


@dataclass(frozen=True)
class PackInfo:
    name: str
    title: str
    stickers: tuple[StickerInfo, ...]
    bot_owned: bool

    @property
    def count(self) -> int:
        return len(self.stickers)

    @property
    def sticker_format(self) -> str:
        return pack_format(self.stickers)

    @property
    def format_label(self) -> str:
        kind = self.sticker_format
        return "مختلطة (غير صالحة)" if kind == "mixed" else FORMAT_AR.get(kind, kind)


def sticker_format(sticker: Any) -> str:
    if getattr(sticker, "is_animated", False):
        return "animated"
    if getattr(sticker, "is_video", False):
        return "video"
    return "static"


def pack_format(stickers: Sequence[StickerInfo]) -> str:
    formats = {item.sticker_format for item in stickers}
    if not formats:
        return "static"
    return formats.pop() if len(formats) == 1 else "mixed"


def is_bot_owned(name: str | None, bot_username: str) -> bool:
    """Can this bot edit the set? Telegram allows only the creator bot."""
    if not name or not bot_username:
        return False
    return name.lower().endswith(f"_by_{bot_username.lower()}")


def pack_info_from_set(sticker_set: Any, bot_username: str) -> PackInfo:
    stickers = tuple(
        StickerInfo(
            file_id=sticker.file_id,
            file_unique_id=sticker.file_unique_id,
            emoji=(sticker.emoji or None),
            sticker_format=sticker_format(sticker),
        )
        for sticker in sticker_set.stickers
    )
    return PackInfo(
        name=sticker_set.name,
        title=sticker_set.title,
        stickers=stickers,
        bot_owned=is_bot_owned(sticker_set.name, bot_username),
    )


def normalize_title(value: str) -> str:
    title = " ".join((value or "").split())
    if not title:
        raise PackManagerError("اسم الحزمة فارغ.")
    if len(title) > 64:
        raise PackManagerError("اسم الحزمة يجب ألا يتجاوز 64 حرفًا.")
    return title


def validate_index(index: int, pack: PackInfo) -> int:
    if not 0 <= index < pack.count:
        raise PackManagerError("رقم الملصق غير موجود في الحزمة.")
    return index


def merge_plan(target: PackInfo, source: PackInfo) -> list[StickerInfo]:
    """Validate a merge and return the stickers that must be copied."""
    if target.name == source.name:
        raise PackManagerError("لا يمكن دمج الحزمة مع نفسها.")
    if not target.bot_owned or not source.bot_owned:
        raise PackManagerError(
            "تيليجرام يسمح للبوت بتعديل الحزم التي أنشأها هو فقط "
            "(الاسم ينتهي بـ _by_<اسم البوت>)."
        )
    if target.sticker_format != source.sticker_format:
        raise PackManagerError(
            f"لا يمكن الدمج: الحزمة الهدف {target.format_label} "
            f"والمصدر {source.format_label}. تيليجرام لا يسمح بخلط الصيغ."
        )
    arriving = source.count
    if target.count + arriving > MAX_STICKERS:
        free = MAX_STICKERS - target.count
        raise PackManagerError(
            f"الحد الأقصى {MAX_STICKERS} ملصقًا في الحزمة. "
            f"المتاح {free} والمصدر يحتوي {arriving}."
        )
    return list(source.stickers)


def find_duplicates(packs: Iterable[PackInfo]) -> dict[str, list[str]]:
    """Map file_unique_id -> pack names, only for stickers present more than once."""
    seen: dict[str, list[str]] = {}
    for pack in packs:
        for sticker in pack.stickers:
            names = seen.setdefault(sticker.file_unique_id, [])
            if pack.name not in names:
                names.append(pack.name)
    return {unique_id: names for unique_id, names in seen.items() if len(names) > 1}


async def fetch_pack(bot: Any, name: str, bot_username: str) -> PackInfo:
    try:
        sticker_set = await bot.get_sticker_set(name=name)
    except TelegramBadRequest as exc:
        raise PackManagerError(_friendly_error(exc, "قراءة الحزمة")) from exc
    except Exception as exc:  # noqa: BLE001 - network failures are user-visible too
        raise PackManagerError("تعذر الاتصال بتيليجرام لقراءة الحزمة.") from exc
    return pack_info_from_set(sticker_set, bot_username)


async def delete_sticker(bot: Any, pack: PackInfo, index: int) -> StickerInfo:
    _require_owned(pack)
    validate_index(index, pack)
    sticker = pack.stickers[index]
    try:
        await bot.delete_sticker_from_set(sticker=sticker.file_id)
    except TelegramBadRequest as exc:
        raise PackManagerError(_friendly_error(exc, "حذف الملصق")) from exc
    return sticker


async def move_sticker(
    bot: Any, pack: PackInfo, index: int, new_position: int
) -> StickerInfo:
    _require_owned(pack)
    validate_index(index, pack)
    if not 0 <= new_position < pack.count:
        raise PackManagerError("الموضع الجديد خارج نطاق الحزمة.")
    sticker = pack.stickers[index]
    try:
        await bot.set_sticker_position_in_set(
            sticker=sticker.file_id, position=new_position
        )
    except TelegramBadRequest as exc:
        raise PackManagerError(_friendly_error(exc, "تحريك الملصق")) from exc
    return sticker


async def rename_pack(bot: Any, pack: PackInfo, title: str) -> str:
    _require_owned(pack)
    new_title = normalize_title(title)
    try:
        await bot.set_sticker_set_title(name=pack.name, title=new_title)
    except TelegramBadRequest as exc:
        raise PackManagerError(_friendly_error(exc, "تغيير الاسم")) from exc
    return new_title


async def copy_pack(
    bot: Any,
    user_id: int,
    pack: PackInfo,
    title: str,
    bot_username: str,
    new_name: str | None = None,
) -> str:
    """Duplicate a set this bot owns into a new set with the same stickers."""
    from ..stickers import pack_short_name  # local import avoids a cycle

    _require_owned(pack)
    if pack.count == 0:
        raise PackManagerError("الحزمة فارغة، لا يوجد ما يُنسخ.")
    if pack.count > MAX_STICKERS:
        raise PackManagerError(f"لا يمكن نسخ أكثر من {MAX_STICKERS} ملصقًا.")
    new_title = normalize_title(title)
    name = new_name or pack_short_name(user_id, bot_username)
    stickers = await _input_stickers(bot, pack)
    try:
        await bot.create_new_sticker_set(
            user_id=user_id,
            name=name,
            title=new_title,
            stickers=stickers,
            sticker_format=pack.sticker_format,
        )
    except TelegramBadRequest as exc:
        raise PackManagerError(_friendly_error(exc, "نسخ الحزمة")) from exc
    return name


async def merge_into(
    bot: Any, user_id: int, target: PackInfo, source: PackInfo
) -> int:
    """Merge with the owning user id required by ``add_sticker_to_set``."""
    arriving = merge_plan(target, source)
    added = 0
    for sticker_info in arriving:
        data, filename = await _download(bot, sticker_info)
        try:
            await bot.add_sticker_to_set(
                user_id=user_id,
                name=target.name,
                sticker=InputSticker(
                    sticker=BufferedInputFile(data, filename=filename),
                    format=sticker_info.sticker_format,
                    emoji_list=[sticker_info.emoji or DEFAULT_EMOJI],
                ),
            )
        except TelegramBadRequest as exc:
            raise PackManagerError(_friendly_error(exc, "دمج الحزم")) from exc
        added += 1
    return added


async def download_sticker(
    bot: Any, sticker: StickerInfo, destination: Path
) -> Path:
    """Save one sticker locally; used by previews and ZIP export fallbacks."""
    try:
        telegram_file = await bot.get_file(sticker.file_id)
        if not telegram_file.file_path:
            raise PackManagerError("تيليجرام لم يُرجع مسار الملصق.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        await bot.download_file(telegram_file.file_path, destination=destination)
    except PackManagerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PackManagerError("تعذر تنزيل الملصق من تيليجرام.") from exc
    return destination


async def _input_stickers(bot: Any, pack: PackInfo) -> list[InputSticker]:
    stickers: list[InputSticker] = []
    for sticker in pack.stickers:
        data, filename = await _download(bot, sticker)
        stickers.append(
            InputSticker(
                sticker=BufferedInputFile(data, filename=filename),
                format=sticker.sticker_format,
                emoji_list=[sticker.emoji or DEFAULT_EMOJI],
            )
        )
    return stickers


async def _download(bot: Any, sticker: StickerInfo) -> tuple[bytes, str]:
    try:
        telegram_file = await bot.get_file(sticker.file_id)
        if not telegram_file.file_path:
            raise PackManagerError("تيليجرام لم يُرجع مسار الملصق.")
        buffer = await bot.download_file(telegram_file.file_path)
        data = buffer.read() if hasattr(buffer, "read") else bytes(buffer)
    except PackManagerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PackManagerError("تعذر تنزيل أحد الملصقات من تيليجرام.") from exc
    if not data:
        raise PackManagerError("ملف الملصق فارغ؛ لا يمكن نسخه.")
    extension = EXTENSION_BY_FORMAT.get(sticker.sticker_format, ".bin")
    return data, f"sticker{extension}"


def pack_manifest(pack: PackInfo) -> str:
    """JSON manifest that mirrors the ZIP export format for backups."""
    return json.dumps(
        {
            "name": pack.name,
            "title": pack.title,
            "sticker_format": pack.sticker_format,
            "sticker_count": pack.count,
            "stickers": [
                {
                    "file_unique_id": item.file_unique_id,
                    "emoji": item.emoji,
                    "format": item.sticker_format,
                }
                for item in pack.stickers
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _require_owned(pack: PackInfo) -> None:
    if not pack.bot_owned:
        raise PackManagerError(
            "هذه الحزمة لم ينشئها هذا البوت، وتيليجرام يمنع تعديلها من البوت. "
            "يمكنك تصديرها بـ /packexport أو تعديلها من تطبيق تيليجرام."
        )


def _friendly_error(error: Exception, action: str) -> str:
    text = str(error)
    lowered = text.lower()
    if "stickerset_invalid" in lowered or "not found" in lowered:
        return "الحزمة غير موجودة في تيليجرام (قد تكون حُذفت)."
    if "too much" in lowered or "limit" in lowered or "full" in lowered:
        return f"وصلت الحزمة إلى حد تيليجرام أثناء {action}."
    if "not enough rights" in lowered or "not the owner" in lowered:
        return f"لا يملك البوت صلاحية {action} على هذه الحزمة."
    if "emoji" in lowered and "invalid" in lowered:
        return "إيموجي أحد الملصقات غير مقبول من تيليجرام."
    return f"فشلت عملية {action}. السبب: {text[:200]}"
