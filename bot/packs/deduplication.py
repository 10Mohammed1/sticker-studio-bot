"""Content-addressed deduplication for auto-added stickers.

The digest is computed from the **converted** file, so two different uploads
that produce the same sticker are recognised too (e.g. the same image sent
once as a photo and once as a document). The ``(user_id, sha256)`` pair is
unique in SQLite, which makes the check safe even if two album items race.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

CHUNK_SIZE = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def sha256_async(path: Path) -> str:
    """Hash without blocking the event loop (stickers can be a few MB)."""
    return await asyncio.to_thread(sha256_file, path)


async def find(db: Any, user_id: int, digest: str) -> dict[str, Any] | None:
    return await db.get_dedupe(user_id, digest)


async def remember(
    db: Any,
    user_id: int,
    digest: str,
    pack_id: int | None,
    file_id: str,
    file_unique_id: str,
) -> None:
    await db.save_dedupe(user_id, digest, pack_id, file_id, file_unique_id)


__all__ = ["find", "remember", "sha256_async", "sha256_file"]
