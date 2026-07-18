PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS sources (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    last_seen_message_id INTEGER,
    last_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_channel_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    source_author_id INTEGER,
    mega_url TEXT NOT NULL,
    game_name TEXT,
    canonical_name TEXT,
    app_id INTEGER,
    filename TEXT,
    size_bytes INTEGER,
    is_folder INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT,
    is_stale INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    raw_text_excerpt TEXT,
    UNIQUE(source_channel_id, source_message_id, mega_url),
    FOREIGN KEY (source_channel_id) REFERENCES sources(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_game_name ON entries(game_name);
CREATE INDEX IF NOT EXISTS idx_entries_canonical ON entries(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entries_app_id ON entries(app_id);
CREATE INDEX IF NOT EXISTS idx_entries_mega_url ON entries(mega_url);
CREATE INDEX IF NOT EXISTS idx_entries_posted_at ON entries(posted_at);

CREATE TABLE IF NOT EXISTS apps (
    app_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apps_name ON apps(name);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
