"""Unified configuration for the merged Sticker Studio bot.

The merged project accepts the environment variable names from both original
versions (``DATA_DIR``/``WORK_DIR``, ``MAX_DOWNLOAD_MB``, ``FFMPEG_PATH`` …), so
an existing deployment can be migrated without rewriting its ``.env``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Project .env wins; load_dotenv() keeps real hosting environment variables first.
load_dotenv(BASE_DIR / ".env")
load_dotenv()

PLACEHOLDER_TOKENS = {
    "",
    "PUT_YOUR_BOT_TOKEN_HERE",
    "PASTE_YOUR_BOTFATHER_TOKEN_HERE",
    "YOUR_BOT_TOKEN",
}


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None:
        value = min(value, maximum)
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def parse_admin_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ValueError(f"ADMIN_IDS contains a non-integer value: {part!r}") from exc
    return frozenset(result)


def parse_admin_usernames(value: str) -> frozenset[str]:
    """Parse ``ADMIN_USERNAMES``: comma/semicolon separated, ``@`` optional."""
    names: set[str] = set()
    for part in value.replace(";", ",").split(","):
        name = part.strip().lstrip("@").lower()
        if name:
            names.add(name)
    return frozenset(names)


def _resolve_path(value: str | None, default: Path) -> Path:
    if not value or not value.strip():
        path = default
    else:
        path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    data_dir: Path
    work_dir: Path
    database_path: Path
    max_download_mb: int
    max_expanded_mb: int
    max_archive_items: int
    max_pdf_pages: int
    max_processing_seconds: int
    enable_bg_removal: bool
    ffmpeg_path: str
    ffprobe_path: str
    user_cooldown_seconds: int
    maintenance_default: bool
    max_concurrent_jobs: int
    force_ipv4: bool
    auto_pack: bool
    telegram_proxy: str
    log_level: str
    admin_usernames: frozenset[str] = frozenset()
    backup_enabled: bool = True
    backup_interval_hours: int = 6
    backup_keep: int = 7

    @property
    def backup_dir(self) -> Path:
        """Backups sit next to the database on the persistent volume."""
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("BOT_TOKEN", "").strip()
        if token in PLACEHOLDER_TOKENS:
            raise RuntimeError(
                "BOT_TOKEN is missing. Copy .env.example to .env and add the token from @BotFather."
            )

        # WORK_DIR is the old conversion-engine name for the data directory.
        data_dir = _resolve_path(os.getenv("DATA_DIR") or os.getenv("WORK_DIR"), BASE_DIR / "data")
        work_dir = _resolve_path(os.getenv("TEMP_DIR"), data_dir / "tmp")
        database_path = _resolve_path(
            os.getenv("DATABASE_PATH"), data_dir / "sticker_studio.sqlite3"
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        database_path.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=token,
            admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS", "")),
            data_dir=data_dir,
            work_dir=work_dir,
            database_path=database_path,
            max_download_mb=env_int("MAX_DOWNLOAD_MB", 20, minimum=1, maximum=20),
            max_expanded_mb=env_int("MAX_EXPANDED_MB", 100, minimum=1),
            max_archive_items=env_int("MAX_ARCHIVE_ITEMS", 40, minimum=1, maximum=200),
            max_pdf_pages=env_int("MAX_PDF_PAGES", 12, minimum=1, maximum=100),
            max_processing_seconds=env_int("MAX_PROCESSING_SECONDS", 150, minimum=10),
            enable_bg_removal=env_bool("ENABLE_BG_REMOVAL", False),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg",
            ffprobe_path=os.getenv("FFPROBE_PATH", "ffprobe").strip() or "ffprobe",
            user_cooldown_seconds=env_int("USER_COOLDOWN_SECONDS", 2, minimum=0, maximum=120),
            maintenance_default=env_bool("MAINTENANCE_MODE", False),
            max_concurrent_jobs=env_int("MAX_CONCURRENT_JOBS", 1, minimum=1, maximum=16),
            force_ipv4=env_bool("FORCE_IPV4", True),
            auto_pack=env_bool("AUTO_PACK", False),
            telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            admin_usernames=parse_admin_usernames(os.getenv("ADMIN_USERNAMES", "")),
            backup_enabled=env_bool("BACKUP_ENABLED", True),
            backup_interval_hours=env_int(
                "BACKUP_INTERVAL_HOURS", 6, minimum=1, maximum=168
            ),
            backup_keep=env_int("BACKUP_KEEP", 7, minimum=1, maximum=60),
        )


def load_settings() -> Settings:
    return Settings.from_env()


# Imported by the bot modules; keeping one immutable instance avoids re-reading
# the environment on every handler call.
settings = load_settings()
