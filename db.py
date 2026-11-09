"""
SQLite access for Crate Excavator: one database file (DATA_DIR/recommendations.db,
WAL mode), one schema, one connection helper.

The schema is created / migrated once per process, on the first `connect()`.
All connections use sqlite3.Row, so rows work both as tuples (r[0]) and dicts (r["artist"]).
"""

import contextlib
import logging
import os
import sqlite3
import threading
from typing import Iterator

from config import CONFIG

log = logging.getLogger(__name__)

DB_PATH = os.path.join(CONFIG["DATA_DIR"], "recommendations.db")
BUSY_TIMEOUT_SECONDS = 20.0

_schema_lock = threading.Lock()
_schema_ready = False

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist TEXT NOT NULL,
        album TEXT NOT NULL,
        year INTEGER,
        genre TEXT,
        reason TEXT,
        cover_url TEXT,
        preview_url TEXT,
        deezer_id TEXT,
        deezer_url TEXT,
        deemix_url TEXT,
        bandcamp_url TEXT,
        lidarr_status TEXT DEFAULT 'Not Added',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        channel TEXT DEFAULT 'General',
        UNIQUE(artist, album)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS library_tracks (
        rating_key TEXT PRIMARY KEY,
        artist TEXT,
        album TEXT,
        title TEXT,
        year INTEGER,
        genre TEXT
    )
    """,
    # Artist lookups are case-insensitive (`artist = ? COLLATE NOCASE`); this index serves them.
    "CREATE INDEX IF NOT EXISTS idx_lib_artist_nocase ON library_tracks(artist COLLATE NOCASE)",
    """
    CREATE TABLE IF NOT EXISTS gemini_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cost_usd REAL,
        purpose TEXT,
        status TEXT NOT NULL DEFAULT 'final'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gemini_usage_ts ON gemini_usage(timestamp)",
    # Runtime settings shared by all Gunicorn workers (e.g. the active LLM provider).
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        progress TEXT,
        http_status INTEGER,
        result TEXT,
        pid INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

# Columns added after the first release: (table, column, definition)
_ADDED_COLUMNS = [
    ("recommendations", "channel", "TEXT DEFAULT 'General'"),
    ("gemini_usage", "status", "TEXT NOT NULL DEFAULT 'final'"),
]

# Indexes from earlier versions that queries no longer use.
_DROPPED_INDEXES = ["idx_lib_artist", "idx_lib_title"]


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _SCHEMA:
        conn.execute(stmt)
    for table, column, definition in _ADDED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            log.info("Migrating database: adding %s.%s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for index in _DROPPED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    # Housekeeping: finished jobs older than a week.
    conn.execute("DELETE FROM jobs WHERE status != 'running' AND updated_at < datetime('now', '-7 days')")
    conn.commit()


def connect() -> sqlite3.Connection:
    """Open a connection (schema guaranteed to exist). The caller closes it; prefer `session()`."""
    global _schema_ready
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                _migrate(conn)
                _schema_ready = True
    return conn


@contextlib.contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """`with db.session() as conn:` -- closes the connection afterwards (commit is up to the caller)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
