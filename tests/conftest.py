"""
Test setup. Environment variables must be set BEFORE any app module is imported,
because config.py builds CONFIG at import time. Real-environment env vars override
the repo's .env, so blanking the secrets here guarantees no test ever talks to the
real Gemini / Plex / Lidarr, even if a .env with real keys exists.
"""

import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_services import FakeServices  # noqa: E402

FAKE = FakeServices()
DATA_DIR = tempfile.mkdtemp(prefix="crate-excavator-tests-")

os.environ.update({
    "DATA_DIR": DATA_DIR,
    "LOCK_DIR": os.path.join(DATA_DIR, "locks"),
    "RUN_SCHEDULER": "0",
    "PLEX_URL": FAKE.url,
    "PLEX_TOKEN": "test-plex-token",
    "TAUTULLI_URL": FAKE.url,
    "TAUTULLI_KEY": "test-tautulli-key",
    "LIDARR_URL": FAKE.url,
    "LIDARR_API_KEY": "test-lidarr-key",
    "DEEMIX_URL": FAKE.url,
    "OLLAMA_URL": FAKE.url,
    "GEMINI_API_KEY": "",
    "LLM_PROVIDER": "gemini",
    "RECENTLY_PLAYED_DAYS": "0",
    "LIDARR_ROOT_FOLDER": "/music",
    "LIDARR_QUALITY_PROFILE_ID": "1",
    "LIDARR_METADATA_PROFILE_ID": "1",
})

import http_client  # noqa: E402
import music_excavator  # noqa: E402


@pytest.fixture
def fake():
    """The shared fake Plex/Lidarr server, reset to a clean state for each test."""
    FAKE.reset()
    music_excavator._CACHED_MACHINE_ID = None
    music_excavator._MACHINE_ID_FAILED_AT = 0.0
    return FAKE


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retries shouldn't make the suite slow."""
    monkeypatch.setattr(http_client, "_sleep", lambda _s: None)


@pytest.fixture
def db_path():
    return os.path.join(DATA_DIR, "recommendations.db")


@pytest.fixture
def library():
    """Replace library_tracks contents; returns a function taking (rating_key, artist, title, album) rows."""
    import db

    def _fill(rows):
        with db.session() as conn:
            conn.execute("DELETE FROM library_tracks")
            conn.executemany(
                "INSERT INTO library_tracks (rating_key, artist, title, album) VALUES (?, ?, ?, ?)", rows
            )
            conn.commit()
    return _fill


@pytest.fixture
def no_library(library):
    library([])
