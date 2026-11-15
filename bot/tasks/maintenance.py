"""Temporary-file cleanup shared by startup, admin tools and the job queue.

The 4.0 plan requires that temp files never pile up. Every rule here is
conservative: only files older than the cutoff inside directories the bot owns
are deleted, and failures are reported as counts instead of raising.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path


def _age_cutoff(max_age_hours: float) -> float:
    return time.time() - max(0.0, float(max_age_hours)) * 3600


def purge_directory(path: Path, max_age_hours: float = 6.0, *, keep_root: bool = True) -> int:
    """Delete entries older than ``max_age_hours`` inside ``path``.

    Returns the number of removed entries. ``path`` itself survives unless
    ``keep_root`` is false.
    """
    if not path.exists():
        return 0
    cutoff = _age_cutoff(max_age_hours)
    removed = 0
    for entry in list(path.iterdir()):
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if not keep_root:
        shutil.rmtree(path, ignore_errors=True)
    return removed


def purge_feature_sessions(data_dir: Path, max_age_hours: float = 24.0) -> int:
    """Remove abandoned editor/animation workspaces (``<data>/feature_sessions/<uid>/<id>``)."""
    root = data_dir / "feature_sessions"
    if not root.exists():
        return 0
    cutoff = _age_cutoff(max_age_hours)
    removed = 0
    for user_dir in root.glob("*"):
        if not user_dir.is_dir():
            continue
        for session in list(user_dir.iterdir()):
            try:
                if session.is_dir() and session.stat().st_mtime < cutoff:
                    shutil.rmtree(session, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        try:
            if not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            continue
    return removed


def purge_work_dir(work_dir: Path, max_age_hours: float = 6.0) -> int:
    """Remove orphaned conversion work directories."""
    return purge_directory(work_dir, max_age_hours)


def directory_size(path: Path) -> int:
    """Total bytes under ``path``; 0 when it does not exist."""
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total
