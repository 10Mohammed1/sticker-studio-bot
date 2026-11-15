-- Sticker Studio 4.1 additive migration: permanent per-user packs.
-- Safe to run more than once: only CREATE ... IF NOT EXISTS is used and no
-- existing table, row or sticker is ever deleted or rewritten.
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

-- One row per (user, converted-file digest); this is what makes retries and
-- resends idempotent without deleting anything.
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

-- Auditable status of every conversion/add attempt: created, added,
-- duplicate, skipped, failed or disabled.
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

-- The per-user auto-add preference lives in the existing 4.0 table; add it only
-- when a very old database does not have user_preferences yet.
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'ar',
    bg_removal INTEGER NOT NULL DEFAULT 1,
    auto_pack INTEGER NOT NULL DEFAULT 1,
    output_quality TEXT NOT NULL DEFAULT 'balanced',
    updated_at TEXT NOT NULL
);
