"""Permanent, per-user sticker packs.

Sticker Studio 4.1 keeps **one static pack and one animated pack per user**
and reuses them for every conversion instead of opening a new set per upload.
Only when Telegram's hard limit (50 stickers per set) is reached does the
service rotate to ``part 2``, ``part 3``… of the same slot, so the link the
user already has keeps working for everything that was added before.

The database row (``user_packs``) is the source of truth for the mapping
``user -> slot -> part -> Telegram set``. Rows survive restarts, so the bot
reuses the same sets forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_STICKERS_PER_PACK = 50

SLOT_STATIC = "static"
SLOT_ANIMATED = "animated"
SLOT_ANIMATED_TGS = "animated_tgs"
SLOT_ORDER = (SLOT_STATIC, SLOT_ANIMATED, SLOT_ANIMATED_TGS)

SLOT_LABELS = {
    SLOT_STATIC: "الثابتة",
    SLOT_ANIMATED: "المتحركة",
    SLOT_ANIMATED_TGS: "المتحركة (TGS)",
}
SLOT_EMOJI = {
    SLOT_STATIC: "📦",
    SLOT_ANIMATED: "🎞️",
    SLOT_ANIMATED_TGS: "🎬",
}
TITLE_WORDS = {
    SLOT_STATIC: "ثابتة",
    SLOT_ANIMATED: "متحركة",
    SLOT_ANIMATED_TGS: "TGS",
}

# Telegram sticker format -> permanent slot.
FORMAT_TO_SLOT = {
    "static": SLOT_STATIC,
    "video": SLOT_ANIMATED,
    "animated": SLOT_ANIMATED_TGS,
}
SLOT_TO_FORMAT = {
    SLOT_STATIC: "static",
    SLOT_ANIMATED: "video",
    SLOT_ANIMATED_TGS: "animated",
}


class UserPackError(RuntimeError):
    """A user-facing explanation of why a permanent pack cannot be used."""


@dataclass(frozen=True)
class UserPack:
    id: int
    user_id: int
    slot: str
    part: int
    name: str | None
    title: str
    sticker_format: str
    sticker_count: int
    last_file_unique_id: str | None
    last_added_at: str | None = None

    @property
    def is_full(self) -> bool:
        return self.sticker_count >= MAX_STICKERS_PER_PACK

    @property
    def link(self) -> str | None:
        return pack_link(self.name) if self.name else None

    @property
    def label(self) -> str:
        return slot_label(self.slot)

    @property
    def caption(self) -> str:
        suffix = f" ({self.part})" if self.part > 1 else ""
        return f"{self.label}{suffix}"


def from_row(row: dict[str, Any]) -> UserPack:
    return UserPack(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        slot=str(row["slot"]),
        part=int(row["part"]),
        name=row.get("name"),
        title=str(row["title"]),
        sticker_format=str(row["sticker_format"]),
        sticker_count=int(row.get("sticker_count") or 0),
        last_file_unique_id=row.get("last_file_unique_id"),
        last_added_at=row.get("last_added_at"),
    )


def slot_for_format(sticker_format: str | None) -> str:
    slot = FORMAT_TO_SLOT.get(sticker_format or "")
    if slot is None:
        raise UserPackError(f"صيغة غير مدعومة للحزم الدائمة: {sticker_format!r}")
    return slot


def slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot)


def slot_emoji(slot: str) -> str:
    return SLOT_EMOJI.get(slot, "📦")


def pack_link(name: str | None) -> str | None:
    return f"https://t.me/addstickers/{name}" if name else None


def default_title(first_name: str | None, slot: str, part: int = 1) -> str:
    """Readable set title, e.g. ``ملصقات محمد — ثابتة``."""
    name = " ".join((first_name or "").split())
    base = f"ملصقات {name}" if name else "ملصقاتي"
    word = TITLE_WORDS.get(slot)
    suffix = f" — {word}" if word else ""
    if part > 1:
        suffix += f" ({part})"
    return f"{base}{suffix}"[:64]


async def list_packs(db: Any, user_id: int) -> list[UserPack]:
    rows = await db.get_user_packs(user_id)
    return [from_row(row) for row in rows]


async def packs_by_slot(db: Any, user_id: int, slot: str) -> list[UserPack]:
    return [pack for pack in await list_packs(db, user_id) if pack.slot == slot]


async def ensure_pack(
    db: Any, user_id: int, sticker_format: str, first_name: str | None,
) -> UserPack:
    """Return the pack that must receive the next sticker of this format.

    Creates the first row of a slot on demand and rotates to a new part when
    the current set is full. Never touches Telegram, so it is safe to call
    before the upload is attempted.
    """
    slot = slot_for_format(sticker_format)
    existing = await packs_by_slot(db, user_id, slot)
    if not existing:
        await adopt_legacy(db, user_id)
        existing = await packs_by_slot(db, user_id, slot)
    if existing:
        latest = max(existing, key=lambda item: item.part)
        if not latest.is_full:
            return latest
        next_part = latest.part + 1
    else:
        next_part = 1
    title = default_title(first_name, slot, next_part)
    pack_id = await db.create_user_pack(
        user_id, slot, next_part, title, sticker_format
    )
    row = await db.get_user_pack(pack_id, user_id)
    if row is None:  # pragma: no cover - defensive, DB just wrote it
        raise UserPackError("تعذر تجهيز حزمة دائمة في قاعدة البيانات.")
    return from_row(row)


async def rotate(
    db: Any, pack: UserPack, sticker_format: str, first_name: str | None
) -> UserPack:
    """Open ``part + 1`` of the same slot after Telegram's 50-sticker limit."""
    next_part = pack.part + 1
    title = default_title(first_name, pack.slot, next_part)
    pack_id = await db.create_user_pack(
        pack.user_id, pack.slot, next_part, title, sticker_format
    )
    row = await db.get_user_pack(pack_id, pack.user_id)
    if row is None:  # pragma: no cover
        raise UserPackError("تعذر إنشاء جزء جديد من الحزمة.")
    return from_row(row)


async def adopt_legacy(db: Any, user_id: int) -> int:
    """Import 4.0 packs created by the old single-active-pack flow.

    Keeps the historical set (and its link) visible in the new permanent-pack
    view instead of starting from scratch, without deleting anything.
    """
    adopted = 0
    rows = await db.get_packs(user_id)
    if not rows:
        return 0
    known = {pack.name for pack in await list_packs(db, user_id) if pack.name}
    for row in rows:
        if row.name in known:
            continue
        slot = FORMAT_TO_SLOT.get(row.sticker_format)
        if slot is None:
            continue
        parts = await packs_by_slot(db, user_id, slot)
        part = max((item.part for item in parts), default=0) + 1
        pack_id = await db.create_user_pack(
            user_id, slot, part, row.title, row.sticker_format
        )
        items = await db.get_pack_items(row.id)
        await db.set_user_pack_name(
            pack_id, row.name, sticker_count=len(items), sticker_format=row.sticker_format
        )
        known.add(row.name)
        adopted += 1
    return adopted


def summary_rows(packs: list[UserPack]) -> list[tuple[str, str, int, str | None]]:
    """(slot, caption, count, link) rows ready for rendering."""
    rows: list[tuple[str, str, int, str | None]] = []
    for pack in sorted(packs, key=lambda item: (SLOT_ORDER.index(item.slot), item.part)):
        rows.append((pack.slot, pack.caption, pack.sticker_count, pack.link))
    return rows


__all__ = [
    "FORMAT_TO_SLOT",
    "MAX_STICKERS_PER_PACK",
    "SLOT_ANIMATED",
    "SLOT_ANIMATED_TGS",
    "SLOT_LABELS",
    "SLOT_ORDER",
    "SLOT_STATIC",
    "SLOT_TO_FORMAT",
    "UserPack",
    "UserPackError",
    "adopt_legacy",
    "default_title",
    "ensure_pack",
    "from_row",
    "list_packs",
    "pack_link",
    "packs_by_slot",
    "rotate",
    "slot_emoji",
    "slot_for_format",
    "slot_label",
    "summary_rows",
]
