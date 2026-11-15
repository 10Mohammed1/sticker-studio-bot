"""SQLite persistence: users, jobs, admin settings, and active sticker packs."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger("sticker-studio.database")

# SQL files are applied in numeric order exactly once. The schema is also
# created idempotently below, so an older deployment upgrades in place.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@dataclass(frozen=True)
class Stats:
    users: int
    jobs: int
    failed: int
    packs: int
    blocked: int


@dataclass(frozen=True)
class UserRow:
    user_id: int
    username: str | None
    first_name: str | None
    blocked: bool
    pack_title: str | None
    last_seen: str


@dataclass(frozen=True)
class PackRow:
    id: int
    name: str
    title: str
    sticker_format: str
    created_at: str


@dataclass(frozen=True)
class ActivePack:
    name: str | None
    title: str | None
    sticker_format: str | None
    auto: bool = False

    @property
    def exists(self) -> bool:
        return bool(self.title)


EMPTY_PACK = ActivePack(None, None, None)


class Database:
    def __init__(self, path: Path):
        self.path = str(path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        try:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            await db.close()

    async def init(self, maintenance_default: bool = False) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    active_pack_name TEXT,
                    active_pack_title TEXT,
                    active_pack_format TEXT,
                    active_pack_auto INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    input_kind TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_user_created
                    ON jobs(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_users_last_seen
                    ON users(last_seen);
                CREATE TABLE IF NOT EXISTS packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    sticker_format TEXT NOT NULL,
                    auto INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_packs_user
                    ON packs(user_id, id);
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT NOT NULL DEFAULT 'ar',
                    bg_removal INTEGER NOT NULL DEFAULT 1,
                    auto_pack INTEGER NOT NULL DEFAULT 1,
                    output_quality TEXT NOT NULL DEFAULT 'balanced',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, file_unique_id)
                );
                CREATE INDEX IF NOT EXISTS idx_favorites_user
                    ON favorites(user_id, id DESC);
                CREATE TABLE IF NOT EXISTS task_queue (
                    task_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sticker_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    sha256 TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, file_unique_id)
                );
                CREATE TABLE IF NOT EXISTS pack_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id INTEGER NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    sticker_format TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    emoji TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(pack_id, file_unique_id),
                    FOREIGN KEY(pack_id) REFERENCES packs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS edit_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    original_path TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_queue_user
                    ON task_queue(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS edit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot TEXT NOT NULL,
                    part INTEGER NOT NULL DEFAULT 1,
                    name TEXT UNIQUE,
                    title TEXT NOT NULL,
                    sticker_format TEXT NOT NULL,
                    sticker_count INTEGER NOT NULL DEFAULT 0,
                    last_file_unique_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_added_at TEXT,
                    UNIQUE(user_id, slot, part)
                );
                CREATE INDEX IF NOT EXISTS idx_user_packs_user
                    ON user_packs(user_id, slot, part);
                CREATE TABLE IF NOT EXISTS sticker_dedupe (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    pack_id INTEGER,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS conversion_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    task_id TEXT,
                    source_kind TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    pack_id INTEGER,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    sha256 TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversion_user
                    ON conversion_log(user_id, id DESC);
                """
            )
            # Databases created before auto packs existed gain the new column.
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "active_pack_auto" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN active_pack_auto INTEGER NOT NULL DEFAULT 0"
                )
            # 4.1 stores the date of the last confirmed addition; an early 4.1
            # build created the table without this column.
            cursor = await db.execute("PRAGMA table_info(user_packs)")
            pack_columns = {row[1] for row in await cursor.fetchall()}
            if "last_added_at" not in pack_columns:
                await db.execute("ALTER TABLE user_packs ADD COLUMN last_added_at TEXT")
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance', ?)",
                ("1" if maintenance_default else "0",),
            )
            # Older databases only kept the active pack in the users table.
            await db.execute(
                """INSERT OR IGNORE INTO packs
                     (name, user_id, title, sticker_format, auto, created_at)
                   SELECT active_pack_name, user_id,
                          COALESCE(NULLIF(active_pack_title, ''), 'ملصقاتي'),
                          COALESCE(NULLIF(active_pack_format, ''), 'static'),
                          active_pack_auto, ?
                   FROM users WHERE active_pack_name IS NOT NULL""",
                (datetime.now(UTC).isoformat(),),
            )
            # A task cannot continue after process restart because the active
            # in-memory worker and temporary input may no longer exist.
            await db.execute(
                "UPDATE task_queue SET status='failed', progress=0, "
                "detail='توقفت المهمة بسبب إعادة تشغيل الخدمة؛ أعد إرسال الملف.', updated_at=? "
                "WHERE status IN ('queued', 'running')",
                (datetime.now(UTC).isoformat(),),
            )
            await db.commit()
        await self.apply_migrations()

    async def apply_migrations(self, folder: Path | None = None) -> list[int]:
        """Apply every pending ``migrations/NNN_*.sql`` file exactly once.

        Existing tables are never dropped: all shipped migrations are additive
        and idempotent. A failure is raised so a broken deployment stops instead
        of silently running with a half-migrated database.
        """
        directory = Path(folder or MIGRATIONS_DIR)
        if not directory.is_dir():
            log.warning("migrations folder %s is missing; schema came from init()", directory)
            return []

        applied: list[int] = []
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL)"""
            )
            cursor = await db.execute("SELECT version FROM schema_migrations")
            versions = {int(row[0]) for row in await cursor.fetchall()}

            for path in sorted(directory.glob("*.sql")):
                try:
                    version = int(path.name.split("_", 1)[0])
                except ValueError:
                    log.warning("ignoring migration with no numeric version: %s", path.name)
                    continue
                if version in versions:
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    await db.executescript(sql)
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, datetime.now(UTC).isoformat()),
                    )
                    await db.commit()
                except Exception as exc:  # noqa: BLE001 - fail loudly, keep old data
                    log.exception("migration %s failed", path.name)
                    raise RuntimeError(
                        f"فشل تطبيق ترحيل قاعدة البيانات {path.name}: {exc}"
                    ) from exc
                versions.add(version)
                applied.append(version)
                log.info("applied database migration %s", path.name)

        return applied

    async def touch_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=COALESCE(excluded.username, users.username),
                     first_name=COALESCE(excluded.first_name, users.first_name),
                     last_seen=excluded.last_seen""",
                (user_id, username, first_name, now, now),
            )
            await db.commit()

    async def get_stats(self) -> Stats:
        async with self._connect() as db:
            users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            jobs = (await (await db.execute("SELECT COUNT(*) FROM jobs")).fetchone())[0]
            failed = (
                await (await db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'")).fetchone()
            )[0]
            packs = (
                await (await db.execute("SELECT COUNT(*) FROM packs")).fetchone()
            )[0]
            blocked = (
                await (await db.execute("SELECT COUNT(*) FROM users WHERE blocked=1")).fetchone()
            )[0]
            return Stats(int(users), int(jobs), int(failed), int(packs), int(blocked))

    async def get_users(self, limit: int = 15) -> list[UserRow]:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT user_id, username, first_name, blocked, active_pack_title, last_seen
                   FROM users ORDER BY last_seen DESC LIMIT ?""",
                (max(1, min(limit, 100)),),
            )
            rows = await cursor.fetchall()
            return [
                UserRow(int(row[0]), row[1], row[2], bool(row[3]), row[4], row[5])
                for row in rows
            ]

    async def all_user_ids(self) -> list[int]:
        async with self._connect() as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE blocked=0 ORDER BY user_id")
            rows = await cursor.fetchall()
            return [int(row[0]) for row in rows]

    async def set_blocked(self, user_id: int, blocked: bool) -> bool:
        """Return False when the user is not known yet."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE users SET blocked=? WHERE user_id=?",
                (1 if blocked else 0, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_blocked(self, user_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,))
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def add_job(
        self, user_id: int, input_kind: str, output_format: str, status: str
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO jobs(user_id, input_kind, output_format, status, created_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (
                    user_id,
                    (input_kind or "unknown")[:32],
                    (output_format or "none")[:32],
                    status[:16],
                    datetime.now(UTC).isoformat(),
                ),
            )
            await db.commit()

    async def get_maintenance(self) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key='maintenance'")
            row = await cursor.fetchone()
            return bool(row and row[0] == "1")

    async def set_maintenance(self, enabled: bool) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES('maintenance', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )
            await db.commit()

    async def get_setting(self, key: str) -> str | None:
        """Read one runtime setting (admin toggles) without touching the env."""
        async with self._connect() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cursor.fetchone()
            return str(row[0]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            await db.commit()

    async def get_flag(self, key: str, default: bool = False) -> bool:
        raw = await self.get_setting(key)
        return default if raw is None else raw == "1"

    async def recent_failed_jobs(self, limit: int = 10) -> list[dict[str, object]]:
        """Last failed tasks for the admin error log."""
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT task_id, user_id, task_type, status, progress, detail, updated_at
                   FROM task_queue WHERE status='failed'
                   ORDER BY updated_at DESC LIMIT ?""",
                (int(limit),),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "task_id": row[0], "user_id": row[1], "task_type": row[2],
                    "status": row[3], "progress": row[4], "detail": row[5],
                    "updated_at": row[6],
                }
                for row in rows
            ]

    async def start_pack(self, user_id: int, title: str, *, auto: bool = False) -> None:
        """Prepare a new, not-yet-created pack and forget the previous pack name."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE users SET
                     active_pack_name=NULL,
                     active_pack_title=?,
                     active_pack_format=NULL,
                     active_pack_auto=?
                   WHERE user_id=?""",
                (title[:64], 1 if auto else 0, user_id),
            )
            await db.commit()

    async def get_active_pack(self, user_id: int) -> ActivePack:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT active_pack_name, active_pack_title, active_pack_format,
                          active_pack_auto
                   FROM users WHERE user_id=?""",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return EMPTY_PACK
            return ActivePack(row[0], row[1], row[2], bool(row[3]))

    async def save_active_pack(
        self, user_id: int, name: str, title: str, sticker_format: str,
        *, auto: bool = False,
    ) -> None:
        title = title[:64]
        async with self._connect() as db:
            await db.execute(
                """UPDATE users SET active_pack_name=?, active_pack_title=?,
                   active_pack_format=?, active_pack_auto=? WHERE user_id=?""",
                (name, title, sticker_format, 1 if auto else 0, user_id),
            )
            await db.execute(
                """INSERT INTO packs(name, user_id, title, sticker_format, auto, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET title=excluded.title,
                     sticker_format=excluded.sticker_format, auto=excluded.auto""",
                (name, user_id, title, sticker_format, 1 if auto else 0,
                 datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def get_packs(self, user_id: int) -> list[PackRow]:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, name, title, sticker_format, created_at
                   FROM packs WHERE user_id=? ORDER BY id DESC""",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [PackRow(int(row[0]), row[1], row[2], row[3], row[4]) for row in rows]

    async def get_pack(self, pack_id: int, user_id: int) -> PackRow | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, name, title, sticker_format, created_at
                   FROM packs WHERE id=? AND user_id=?""",
                (pack_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return PackRow(int(row[0]), row[1], row[2], row[3], row[4])

    async def update_pack_title(self, pack_id: int, user_id: int, title: str) -> bool:
        title = title[:64]
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE packs SET title=? WHERE id=? AND user_id=?",
                (title, pack_id, user_id),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                return False
            await db.execute(
                """UPDATE users SET active_pack_title=?
                   WHERE user_id=? AND active_pack_name=(
                     SELECT name FROM packs WHERE id=? AND user_id=?)""",
                (title, user_id, pack_id, user_id),
            )
            await db.commit()
            return True

    async def activate_pack(self, user_id: int, pack: PackRow) -> None:
        async with self._connect() as db:
            await db.execute(
                """UPDATE users SET active_pack_name=?, active_pack_title=?,
                   active_pack_format=?, active_pack_auto=0 WHERE user_id=?""",
                (pack.name, pack.title, pack.sticker_format, user_id),
            )
            await db.commit()

    async def set_active_pack_format(self, user_id: int, sticker_format: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE users SET active_pack_format=? WHERE user_id=?",
                (sticker_format, user_id),
            )
            await db.commit()

    async def forget_pack_name(self, user_id: int) -> None:
        """Detach a pack Telegram no longer knows and drop its stale registry row.

        The chosen title is kept so the next upload recreates the same pack,
        but the dead entry disappears from the /packs panel.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT active_pack_name FROM users WHERE user_id=?", (user_id,)
            )
            row = await cursor.fetchone()
            stale_name = row[0] if row else None
            if stale_name:
                await db.execute(
                    "DELETE FROM packs WHERE user_id=? AND name=?",
                    (user_id, stale_name),
                )
            await db.execute(
                """UPDATE users SET active_pack_name=NULL, active_pack_format=NULL
                   WHERE user_id=?""",
                (user_id,),
            )
            await db.commit()

    async def clear_active_pack(self, user_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                """UPDATE users SET active_pack_name=NULL, active_pack_title=NULL,
                   active_pack_format=NULL, active_pack_auto=0 WHERE user_id=?""",
                (user_id,),
            )
            await db.commit()

    async def get_user_preferences(self, user_id: int) -> dict[str, object]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT language, bg_removal, auto_pack, output_quality FROM user_preferences WHERE user_id=?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return {"language": "ar", "bg_removal": True, "auto_pack": True,
                        "output_quality": "balanced"}
            return {"language": row[0], "bg_removal": bool(row[1]),
                    "auto_pack": bool(row[2]), "output_quality": row[3]}

    async def set_user_preference(self, user_id: int, key: str, value: object) -> None:
        allowed = {"language", "bg_removal", "auto_pack", "output_quality"}
        if key not in allowed:
            raise ValueError("Unsupported user preference")
        current = await self.get_user_preferences(user_id)
        current[key] = value
        language = str(current["language"])
        if language not in {"ar", "en"}:
            raise ValueError("language must be ar or en")
        quality = str(current["output_quality"])
        if quality not in {"balanced", "high", "small"}:
            raise ValueError("output_quality must be balanced, high, or small")
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO user_preferences(user_id, language, bg_removal, auto_pack, output_quality, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
                     bg_removal=excluded.bg_removal, auto_pack=excluded.auto_pack,
                     output_quality=excluded.output_quality, updated_at=excluded.updated_at""",
                (user_id, language, int(bool(current["bg_removal"])),
                 int(bool(current["auto_pack"])), quality, now),
            )
            await db.commit()

    async def add_favorite(self, user_id: int, file_id: str, file_unique_id: str,
                           media_type: str, display_name: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO favorites
                   (user_id, file_id, file_unique_id, media_type, display_name, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (user_id, file_id, file_unique_id, media_type[:16], display_name[:120],
                 datetime.now(UTC).isoformat()),
            )
            if cursor.rowcount > 0:
                await db.execute(
                    """INSERT OR IGNORE INTO sticker_assets
                       (user_id, file_id, file_unique_id, media_type, display_name, created_at)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (user_id, file_id, file_unique_id, media_type[:16], display_name[:120],
                     datetime.now(UTC).isoformat()),
                )
            await db.commit()
            return cursor.rowcount > 0

    async def get_favorites(self, user_id: int, limit: int = 20) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, file_id, media_type, display_name, created_at FROM favorites "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, max(1, min(limit, 50))),
            )
            rows = await cursor.fetchall()
            return [{"id": int(row[0]), "file_id": row[1], "media_type": row[2],
                     "display_name": row[3], "created_at": row[4]} for row in rows]

    async def get_favorite(self, favorite_id: int, user_id: int) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, file_id, media_type, display_name FROM favorites WHERE id=? AND user_id=?",
                (favorite_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"id": int(row[0]), "file_id": row[1], "media_type": row[2],
                    "display_name": row[3]}

    async def delete_favorite(self, favorite_id: int, user_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM favorites WHERE id=? AND user_id=?", (favorite_id, user_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def create_task(self, task_id: str, user_id: int, task_type: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO task_queue(task_id, user_id, task_type, status, progress, created_at, updated_at) "
                "VALUES(?, ?, ?, 'queued', 0, ?, ?)",
                (task_id, user_id, task_type[:32], now, now),
            )
            await db.commit()

    async def update_task(self, task_id: str, status: str, progress: int = 0, detail: str = "") -> None:
        now = datetime.now(UTC).isoformat()
        allowed_status = {"queued", "running", "completed", "failed", "cancelled"}
        if status not in allowed_status:
            raise ValueError("Invalid task status")
        async with self._connect() as db:
            await db.execute(
                "UPDATE task_queue SET status=?, progress=?, detail=?, updated_at=? WHERE task_id=?",
                (status, max(0, min(100, int(progress))), detail[:240], now, task_id),
            )
            await db.commit()

    async def get_recent_tasks(self, user_id: int, limit: int = 10) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT task_id, task_type, status, progress, detail, created_at FROM task_queue "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 20))),
            )
            rows = await cursor.fetchall()
            return [{"task_id": row[0], "task_type": row[1], "status": row[2],
                     "progress": int(row[3]), "detail": row[4], "created_at": row[5]}
                    for row in rows]

    async def create_edit_session(self, session_id: str, user_id: int,
                                  original_path: str, current_path: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO edit_sessions
                   (session_id, user_id, original_path, current_path, status, created_at, updated_at)
                   VALUES(?, ?, ?, ?, 'active', ?, ?)""",
                (session_id, user_id, original_path, current_path, now, now),
            )
            await db.commit()

    async def update_edit_session(self, session_id: str, current_path: str,
                                  status: str = "active") -> None:
        if status not in {"active", "completed", "cancelled", "expired"}:
            raise ValueError("Invalid edit session status")
        async with self._connect() as db:
            await db.execute(
                "UPDATE edit_sessions SET current_path=?, status=?, updated_at=? WHERE session_id=?",
                (current_path, status, datetime.now(UTC).isoformat(), session_id),
            )
            await db.commit()

    async def get_edit_history(self, user_id: int, limit: int = 10) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT operation, source_name, created_at FROM edit_history "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, max(1, min(limit, 30))),
            )
            rows = await cursor.fetchall()
            return [{"operation": row[0], "source_name": row[1], "created_at": row[2]}
                    for row in rows]

    async def record_edit(self, user_id: int, operation: str, source_name: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO edit_history(user_id, operation, source_name, created_at) VALUES(?, ?, ?, ?)",
                (user_id, operation[:32], source_name[:120], datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def get_task(self, task_id: str, user_id: int) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT task_id, task_type, status, progress, detail, created_at "
                "FROM task_queue WHERE task_id=? AND user_id=?",
                (task_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"task_id": row[0], "task_type": row[1], "status": row[2],
                    "progress": int(row[3]), "detail": row[4], "created_at": row[5]}

    async def replace_pack_items(self, pack_id: int, items: Sequence[dict[str, object]]) -> None:
        """Mirror the Telegram sticker order for one pack (read-only cache)."""
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute("DELETE FROM pack_items WHERE pack_id=?", (pack_id,))
            await db.executemany(
                """INSERT OR REPLACE INTO pack_items
                   (pack_id, file_unique_id, file_id, sticker_format, position, emoji, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        pack_id,
                        str(item["file_unique_id"])[:120],
                        str(item["file_id"])[:200],
                        str(item["sticker_format"])[:16],
                        index,
                        (str(item["emoji"])[:16] if item.get("emoji") else None),
                        now,
                    )
                    for index, item in enumerate(items)
                ],
            )
            await db.commit()

    async def get_pack_items(self, pack_id: int) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT file_unique_id, file_id, sticker_format, position, emoji "
                "FROM pack_items WHERE pack_id=? ORDER BY position ASC",
                (pack_id,),
            )
            rows = await cursor.fetchall()
            return [{"file_unique_id": row[0], "file_id": row[1], "sticker_format": row[2],
                     "position": int(row[3]), "emoji": row[4]} for row in rows]

    async def add_template(self, user_id: int, title: str, config_json: str) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT INTO templates(user_id, title, config_json, created_at)
                   VALUES(?, ?, ?, ?)""",
                (user_id, title[:64], config_json, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def get_templates(self, user_id: int, limit: int = 20) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, title, config_json, created_at FROM templates
                   WHERE user_id=? ORDER BY id DESC LIMIT ?""",
                (user_id, int(limit)),
            )
            rows = await cursor.fetchall()
            return [
                {"id": int(row[0]), "title": row[1], "config_json": row[2],
                 "created_at": row[3]}
                for row in rows
            ]

    async def get_template(self, template_id: int, user_id: int) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, title, config_json, created_at FROM templates "
                "WHERE id=? AND user_id=?",
                (template_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"id": int(row[0]), "title": row[1], "config_json": row[2],
                    "created_at": row[3]}

    async def delete_template(self, template_id: int, user_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM templates WHERE id=? AND user_id=?",
                (template_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Permanent per-user packs (4.1)
    # ------------------------------------------------------------------
    async def get_user_packs(self, user_id: int) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, user_id, slot, part, name, title, sticker_format,
                          sticker_count, last_file_unique_id, created_at,
                          updated_at, last_added_at
                   FROM user_packs WHERE user_id=?
                   ORDER BY slot, part""",
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [_user_pack_row(row) for row in rows]

    async def get_user_pack(self, pack_id: int, user_id: int) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, user_id, slot, part, name, title, sticker_format,
                          sticker_count, last_file_unique_id, created_at,
                          updated_at, last_added_at
                   FROM user_packs WHERE id=? AND user_id=?""",
                (pack_id, user_id),
            )
            row = await cursor.fetchone()
        return _user_pack_row(row) if row else None

    async def find_user_pack(
        self, user_id: int, slot: str, part: int
    ) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, user_id, slot, part, name, title, sticker_format,
                          sticker_count, last_file_unique_id, created_at,
                          updated_at, last_added_at
                   FROM user_packs WHERE user_id=? AND slot=? AND part=?""",
                (user_id, slot, part),
            )
            row = await cursor.fetchone()
        return _user_pack_row(row) if row else None

    async def create_user_pack(
        self, user_id: int, slot: str, part: int, title: str, sticker_format: str
    ) -> int:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO user_packs
                     (user_id, slot, part, name, title, sticker_format,
                      sticker_count, created_at, updated_at)
                   VALUES(?, ?, ?, NULL, ?, ?, 0, ?, ?)
                   ON CONFLICT(user_id, slot, part) DO UPDATE SET
                     title=excluded.title,
                     sticker_format=excluded.sticker_format,
                     updated_at=excluded.updated_at""",
                (user_id, slot, part, title[:64], sticker_format, now, now),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT id FROM user_packs WHERE user_id=? AND slot=? AND part=?",
                (user_id, slot, part),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def set_user_pack_name(
        self, pack_id: int, name: str | None, *, sticker_count: int | None = None,
        sticker_format: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """UPDATE user_packs SET name=?, updated_at=?,
                     sticker_count=COALESCE(?, sticker_count),
                     sticker_format=COALESCE(?, sticker_format)
                   WHERE id=?""",
                (name, now, sticker_count, sticker_format, pack_id),
            )
            await db.commit()

    async def note_user_pack_add(
        self, pack_id: int, file_unique_id: str | None, *, increment: int = 1
    ) -> int:
        """Record a confirmed sticker addition and return the stored count."""
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """UPDATE user_packs SET
                     sticker_count=MAX(0, sticker_count + ?),
                     last_file_unique_id=COALESCE(?, last_file_unique_id),
                     last_added_at=?,
                     updated_at=?
                   WHERE id=?""",
                (max(0, int(increment)), file_unique_id, now, now, pack_id),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT sticker_count FROM user_packs WHERE id=?", (pack_id,)
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def set_user_pack_count(self, pack_id: int, count: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE user_packs SET sticker_count=?, updated_at=? WHERE id=?",
                (max(0, int(count)), datetime.now(UTC).isoformat(), pack_id),
            )
            await db.commit()

    async def mark_user_pack_added(
        self,
        pack_id: int,
        name: str,
        *,
        sticker_count: int,
        file_unique_id: str | None,
    ) -> None:
        """Persist a **Telegram-confirmed** addition in one atomic statement.

        Called only after the Bot API confirmed the sticker, so the stored
        count, the set name, the last sticker id and ``last_added_at`` always
        describe a real state and survive a restart.
        """
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            await db.execute(
                """UPDATE user_packs SET
                     name=?,
                     sticker_count=?,
                     last_file_unique_id=COALESCE(?, last_file_unique_id),
                     last_added_at=?,
                     updated_at=?
                   WHERE id=?""",
                (name, max(0, int(sticker_count)), file_unique_id, now, now, pack_id),
            )
            await db.commit()

    async def delete_user_pack(self, pack_id: int, user_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM user_packs WHERE id=? AND user_id=?", (pack_id, user_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def register_pack(
        self, user_id: int, name: str, title: str, sticker_format: str, *, auto: bool = True
    ) -> int:
        """Mirror a Telegram set into the legacy ``packs`` table for /packs."""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO packs(name, user_id, title, sticker_format, auto, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET title=excluded.title,
                     sticker_format=excluded.sticker_format""",
                (name, user_id, title[:64], sticker_format, 1 if auto else 0,
                 datetime.now(UTC).isoformat()),
            )
            await db.commit()
            cursor = await db.execute("SELECT id FROM packs WHERE name=?", (name,))
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def record_pack_item(
        self, pack_id: int, file_id: str, file_unique_id: str, sticker_format: str,
        *, position: int = 0, emoji: str | None = None,
    ) -> None:
        if not pack_id:
            return
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO pack_items
                     (pack_id, file_unique_id, file_id, sticker_format, position,
                      emoji, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pack_id, file_unique_id) DO UPDATE SET
                     file_id=excluded.file_id, position=excluded.position""",
                (pack_id, file_unique_id, file_id, sticker_format, int(position),
                 emoji, datetime.now(UTC).isoformat()),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Deduplication and conversion history
    # ------------------------------------------------------------------
    async def get_dedupe(self, user_id: int, sha256: str) -> dict[str, object] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, user_id, sha256, pack_id, file_id, file_unique_id, created_at
                   FROM sticker_dedupe WHERE user_id=? AND sha256=?""",
                (user_id, sha256),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {"id": int(row[0]), "user_id": int(row[1]), "sha256": row[2],
                "pack_id": row[3], "file_id": row[4], "file_unique_id": row[5],
                "created_at": row[6]}

    async def save_dedupe(
        self, user_id: int, sha256: str, pack_id: int | None, file_id: str,
        file_unique_id: str,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO sticker_dedupe
                     (user_id, sha256, pack_id, file_id, file_unique_id, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, sha256) DO UPDATE SET
                     pack_id=excluded.pack_id, file_id=excluded.file_id,
                     file_unique_id=excluded.file_unique_id""",
                (user_id, sha256, pack_id, file_id, file_unique_id,
                 datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def record_conversion(
        self, user_id: int, source_kind: str, source_name: str, output_format: str,
        status: str, *, pack_id: int | None = None, task_id: str | None = None,
        detail: str = "", sha256: str | None = None,
    ) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT INTO conversion_log
                     (user_id, task_id, source_kind, source_name, output_format,
                      pack_id, status, detail, sha256, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, task_id, source_kind[:32], source_name[:128],
                 output_format[:16], pack_id, status[:16], detail[:500], sha256,
                 datetime.now(UTC).isoformat()),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def get_conversions(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, object]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """SELECT id, task_id, source_kind, source_name, output_format,
                          pack_id, status, detail, sha256, created_at
                   FROM conversion_log WHERE user_id=?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
        return [
            {"id": int(row[0]), "task_id": row[1], "source_kind": row[2],
             "source_name": row[3], "output_format": row[4], "pack_id": row[5],
             "status": row[6], "detail": row[7], "sha256": row[8],
             "created_at": row[9]}
            for row in rows
        ]


def _user_pack_row(row: Sequence[object]) -> dict[str, object]:
    return {
        "id": int(row[0]), "user_id": int(row[1]), "slot": row[2],
        "part": int(row[3]), "name": row[4], "title": row[5],
        "sticker_format": row[6], "sticker_count": int(row[7]),
        "last_file_unique_id": row[8], "created_at": row[9],
        "updated_at": row[10], "last_added_at": row[11],
    }

