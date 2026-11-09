"""The weekly refresh runs every step even when one fails, and reports the failure."""

import pytest

import music_excavator as me
import weekly_refresh as wr


@pytest.fixture
def steps(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(wr, "LOG_FILE", str(tmp_path / "weekly_refresh.log"))
    monkeypatch.setattr(wr, "reindex_library_tracks", lambda: calls.append("reindex") or 10)
    monkeypatch.setattr(wr, "generate_channel_playlists",
                        lambda **k: calls.append("channels") or [{"title": "C", "track_count": 1, "album_count": 1}])
    monkeypatch.setattr(wr, "generate_large_vault_playlist",
                        lambda **k: calls.append("vault") or {"title": "V", "track_count": 1, "album_count": 1})
    monkeypatch.setattr(wr, "run_weekly_discovery_crawl", lambda: calls.append("discovery") or [])
    return calls


def test_all_steps_succeed(steps):
    assert wr.run_refresh(include_discovery=True) is True
    assert steps == ["reindex", "channels", "vault", "discovery"]


def test_failed_step_does_not_stop_the_rest(steps, monkeypatch):
    def broken(**k):
        steps.append("channels")
        raise RuntimeError("Gemini exploded")
    monkeypatch.setattr(wr, "generate_channel_playlists", broken)
    assert wr.run_refresh(include_discovery=True) is False
    assert steps == ["reindex", "channels", "vault", "discovery"]
    log_text = open(wr.LOG_FILE).read()
    assert "❌ Channel playlists failed: Gemini exploded" in log_text
    assert "FINISHED WITH ERRORS" in log_text


def test_reindex_with_no_tracks_counts_as_failure(steps, monkeypatch):
    monkeypatch.setattr(wr, "reindex_library_tracks", lambda: 0)
    assert wr.run_refresh(include_discovery=False) is False
    assert "vault" in steps


def test_one_broken_channel_does_not_stop_other_channels(monkeypatch):
    built = []

    def build(key, cdef, *args):
        built.append(key)
        if key == "pop_punk":
            raise RuntimeError("bad channel")
        return {"channel_key": key}

    monkeypatch.setattr(me, "get_unplayed_vault_albums", lambda: [])
    monkeypatch.setattr(me, "get_recently_played_tracks", lambda: (set(), set()))
    monkeypatch.setattr(me, "_build_channel_playlist", build)
    results = me.generate_channel_playlists()
    assert built == list(me.CHANNEL_DEFINITIONS)
    assert [r["channel_key"] for r in results] == [k for k in me.CHANNEL_DEFINITIONS if k != "pop_punk"]
