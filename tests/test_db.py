"""Schema creation, migration of older databases, and index use."""

import sqlite3

import pytest

import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "recommendations.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_schema_ready", False)
    return path


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_creates_all_tables(fresh_db):
    with db.session() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"recommendations", "library_tracks", "gemini_usage", "app_settings", "jobs"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrates_database_from_older_version(fresh_db):
    old = sqlite3.connect(fresh_db)
    old.executescript(
        """
        CREATE TABLE recommendations (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT NOT NULL,
            album TEXT NOT NULL, year INTEGER, genre TEXT, reason TEXT, cover_url TEXT, preview_url TEXT,
            deezer_id TEXT, deezer_url TEXT, deemix_url TEXT, bandcamp_url TEXT,
            lidarr_status TEXT DEFAULT 'Not Added', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist, album));
        INSERT INTO recommendations (artist, album) VALUES ('Deer Tick', 'Emotional Contracts');
        CREATE TABLE gemini_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, purpose TEXT);
        INSERT INTO gemini_usage (prompt_tokens, completion_tokens, cost_usd, purpose) VALUES (1, 1, 0.25, 'x');
        CREATE TABLE library_tracks (rating_key TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT, year INTEGER, genre TEXT);
        CREATE INDEX idx_lib_artist ON library_tracks(artist);
        CREATE INDEX idx_lib_title ON library_tracks(title);
        """
    )
    old.commit()
    old.close()

    with db.session() as conn:
        assert "channel" in _columns(conn, "recommendations")
        assert "status" in _columns(conn, "gemini_usage")
        row = conn.execute("SELECT channel FROM recommendations").fetchone()
        assert row["channel"] == "General"
        assert tuple(conn.execute("SELECT status, cost_usd FROM gemini_usage").fetchone()) == ("final", 0.25)
        idx = _indexes(conn)
        assert "idx_lib_artist_nocase" in idx
        assert not {"idx_lib_artist", "idx_lib_title"} & idx


def test_case_insensitive_artist_lookup_uses_index(fresh_db):
    with db.session() as conn:
        plan = " ".join(
            r[-1] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM library_tracks WHERE artist = ? COLLATE NOCASE", ("x",)
            )
        )
    assert "idx_lib_artist_nocase" in plan
