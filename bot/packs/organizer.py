"""Bridge between Telegram pack state and the local pack cache.

The database never decides what a pack contains — Telegram does. These helpers
only mirror what Telegram returned so the panel can render a pack without a
network round-trip, and they collapse duplicate stickers for reporting.
"""
from __future__ import annotations

from typing import Any, Iterable

from .manager import PackInfo, StickerInfo


async def sync_pack(db: Any, pack_id: int, pack: PackInfo) -> int:
    """Persist the current Telegram order. Returns the number of items stored."""
    await db.replace_pack_items(
        pack_id,
        [
            {
                "file_unique_id": sticker.file_unique_id,
                "file_id": sticker.file_id,
                "emoji": sticker.emoji,
                "sticker_format": sticker.sticker_format,
            }
            for sticker in pack.stickers
        ],
    )
    return pack.count


def ordered_stickers(pack: PackInfo) -> list[StickerInfo]:
    return list(pack.stickers)


def duplicates_in(pack: PackInfo) -> list[StickerInfo]:
    """Stickers repeated inside one pack (Telegram allows the same file twice)."""
    seen: dict[str, int] = {}
    for sticker in pack.stickers:
        seen[sticker.file_unique_id] = seen.get(sticker.file_unique_id, 0) + 1
    return [sticker for sticker in pack.stickers if seen[sticker.file_unique_id] > 1]


def duplicate_groups(packs: Iterable[PackInfo]) -> dict[str, list[str]]:
    """Cross-pack duplicates keyed by ``file_unique_id``."""
    seen: dict[str, list[str]] = {}
    for pack in packs:
        for sticker in pack.stickers:
            squash = seen.setdefault(sticker.file_unique_id, [])
            if pack.name not in squash:
                squash.append(pack.name)
    return {unique: names for unique, names in seen.items() if len(names) > 1}


def position_label(index: int) -> str:
    """Sticker numbers are shown 1-based, matching Telegram's UI."""
    return str(index + 1)
