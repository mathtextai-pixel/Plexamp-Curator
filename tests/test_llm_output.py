"""LLM output is untrusted: malformed shapes must be dropped, never crash a request."""

import sqlite3

import pytest

import discovery_engine as de
import gemini_curator as gc


def test_clean_str_list_drops_garbage_and_dedupes():
    raw = ["Khruangbin", None, {"a": 1}, "", "  ", "khruangbin", 42, True, "Leon Bridges"]
    assert gc._clean_str_list(raw) == ["Khruangbin", "42", "Leon Bridges"]


def test_clean_str_list_non_list():
    assert gc._clean_str_list("Khruangbin") == []
    assert gc._clean_str_list(None) == []


def test_clean_recommendations():
    raw = [
        "just a string",
        {"artist": "MJ Lenderman", "album": "Manning Fireworks", "year": "2024", "channel": "Alt & Prime Country"},
        {"artist": "", "album": "No Artist"},
        {"artist": ["x"], "album": "Bad Type"},
        {"artist": "Say She She", "album": "Silver", "year": 99999, "channel": "Made Up Channel"},
    ]
    cleaned = gc._clean_recommendations(raw, count=10)
    assert [(r["artist"], r["album"]) for r in cleaned] == [("MJ Lenderman", "Manning Fireworks"), ("Say She She", "Silver")]
    assert cleaned[0]["year"] == 2024 and cleaned[0]["channel"] == "Alt & Prime Country"
    assert cleaned[1]["year"] is None and cleaned[1]["channel"] == "General"


def test_clean_recommendations_respects_count():
    raw = [{"artist": f"A{i}", "album": "B"} for i in range(10)]
    assert len(gc._clean_recommendations(raw, count=3)) == 3


def test_insert_recommendation_ignores_non_dicts():
    conn = de.get_db()
    try:
        assert de._insert_recommendation_item(conn, "not a dict") is None
        assert de._insert_recommendation_item(conn, {"artist": 5, "album": None}) is None
    finally:
        conn.close()


def test_escape_like():
    assert gc._escape_like("100%_pure\\") == "100\\%\\_pure\\\\"


@pytest.fixture
def capture_playlist(monkeypatch):
    calls = []

    def fake_create(title, keys):
        calls.append((title, list(keys)))
        return {"title": title, "ratingKey": "500", "leafCount": len(keys)}

    import music_excavator
    monkeypatch.setattr(music_excavator, "create_or_replace_plex_playlist", fake_create)
    monkeypatch.setattr(music_excavator, "get_recently_played_tracks", lambda: (set(), set()))
    return calls


def _llm_replies(monkeypatch, replies):
    seq = list(replies)
    monkeypatch.setattr(gc, "call_llm_json", lambda *a, **k: seq.pop(0) if seq else None)


def test_prompt_playlist_survives_garbage_artists(monkeypatch, library, capture_playlist):
    library([("1", "Khruangbin", "Maria También", "Con Todo"), ("2", "Leon Bridges", "River", "Coming Home")])
    _llm_replies(monkeypatch, [
        {"title": ["not", "a", "string"], "vibe": None,
         "matched_artists": [None, {"x": 1}, "", "Khruangbin", 7, "Leon Bridges"]},
        {"sequenced_keys": ["2", 1, "999", None, "2"]},
    ])
    result = gc.generate_playlist_from_user_prompt("late night soul", target_count=10)
    assert result is not None
    assert result["rating_keys"][:2] == ["2", "1"]
    assert "999" not in result["rating_keys"]
    assert result["vibe"] == "late night soul"


def test_prompt_playlist_like_fallback_escapes_wildcards(monkeypatch, library, capture_playlist):
    library([("1", "Khruangbin", "Maria", "Con Todo")])
    _llm_replies(monkeypatch, [{"matched_artists": ["%"]}, {"sequenced_keys": []}])
    # "%" would match every artist if passed through unescaped.
    assert gc.generate_playlist_from_user_prompt("anything") is None


def test_prompt_playlist_with_no_matched_artists_returns_none(monkeypatch, library, capture_playlist):
    library([("1", "Khruangbin", "Maria", "Con Todo")])
    _llm_replies(monkeypatch, [{"matched_artists": "Khruangbin"}])
    assert gc.generate_playlist_from_user_prompt("anything") is None
    assert not capture_playlist


def test_prompt_playlist_when_plex_unreachable_on_fresh_install(monkeypatch, no_library):
    import music_excavator
    monkeypatch.setattr(music_excavator, "reindex_library_tracks", lambda: 0)
    # Previously raised sqlite3.OperationalError (no such table) -> HTTP 500.
    assert gc.generate_playlist_from_user_prompt("anything") is None


def test_invalid_provider_override_is_ignored(monkeypatch, library, capture_playlist):
    library([("1", "Khruangbin", "Maria", "Con Todo")])
    seen = []
    monkeypatch.setattr(gc, "call_llm_json", lambda *a, **k: seen.append(k.get("provider_override")) or None)
    gc.generate_playlist_from_user_prompt("anything", provider_override=123)
    assert seen and seen[0] in ("gemini", "ollama")
