-- Sticker Studio 4.0 additive migration.
-- The application also creates these tables idempotently in Database.init().
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
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, id DESC);
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
CREATE INDEX IF NOT EXISTS idx_task_queue_user ON task_queue(user_id, created_at DESC);
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
INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, CURRENT_TIMESTAMP);
