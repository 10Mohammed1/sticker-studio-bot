"""Consistent SQLite backups for the permanent sticker data.

The bot keeps every user, pack mapping, dedupe digest and conversion status in
one SQLite file. On Railway that file lives on the ``/app/data`` volume, and
this module adds a second safety net: a periodic copy built with SQLite's
online backup API (safe while WAL writes continue), verified, and rotated.

Nothing here touches Telegram or deletes user data: old *backup files* are
pruned, the live database and its tables are never modified.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("sticker-studio.backups")

BACKUP_PREFIX = "sticker-studio-"
BACKUP_SUFFIX = ".sqlite3"
DEFAULT_KEEP = 7


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def _copy_database(source: Path, destination: Path) -> None:
    """Build one consistent copy using SQLite's online backup API."""
    source_connection = sqlite3.connect(str(source), timeout=30)
    try:
        target_connection = sqlite3.connect(str(destination), timeout=30)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _check_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError(f"backup integrity check failed: {result!r}")


def prune_backups(backups_dir: Path, keep: int = DEFAULT_KEEP) -> int:
    """Delete only the oldest backup files beyond ``keep``; return the count."""
    if keep < 1:
        keep = 1
    folder = Path(backups_dir)
    if not folder.is_dir():
        return 0
    files = sorted(
        (
            item for item in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
            if item.is_file()
        ),
        key=lambda item: item.name,
    )
    removed = 0
    for item in files[:-keep]:
        try:
            item.unlink()
            removed += 1
        except OSError:
            log.warning("could not remove old backup %s", item, exc_info=True)
    return removed


def create_backup(
    db_path: Path,
    backups_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
) -> Path:
    """Create, verify and rotate one backup; returns the new file path.

    A partially written copy is removed before the error is re-raised, so a
    failed backup can never be mistaken for a valid one.
    """
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"database does not exist: {source}")
    folder = Path(backups_dir)
    folder.mkdir(parents=True, exist_ok=True)

    destination = folder / f"{BACKUP_PREFIX}{_timestamp()}{BACKUP_SUFFIX}"
    partial = destination.with_suffix(".partial")
    try:
        _copy_database(source, partial)
        _check_database(partial)
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    prune_backups(folder, keep)
    log.info("database backup written to %s", destination)
    return destination


async def backup_loop(
    db_path: Path,
    backups_dir: Path,
    *,
    interval_hours: int = 6,
    keep: int = DEFAULT_KEEP,
    run_immediately: bool = True,
) -> None:
    """Periodic backup task; a backup failure never stops the bot.

    The first copy is taken right after startup so a redeploy immediately has
    a verified snapshot of the volume, then the loop sleeps ``interval_hours``.
    """
    interval = max(1, int(interval_hours)) * 3600
    while True:
        if run_immediately:
            try:
                await asyncio.to_thread(create_backup, db_path, backups_dir, keep=keep)
            except Exception:
                log.warning("periodic database backup failed", exc_info=True)
            run_immediately = False
        await asyncio.sleep(interval)


__all__ = [
    "BACKUP_PREFIX",
    "BACKUP_SUFFIX",
    "DEFAULT_KEEP",
    "backup_loop",
    "create_backup",
    "prune_backups",
]
