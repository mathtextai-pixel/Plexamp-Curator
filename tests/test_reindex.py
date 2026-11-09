"""Library re-index: paging, and never deleting rows on a partial or suspicious fetch."""

import pytest

import db
import music_excavator as me


def _tracks(n, prefix=""):
    return [
        {"ratingKey": f"{prefix}{i}", "grandparentTitle": f"Artist {i % 50}", "parentTitle": "Album", "title": f"Song {i}"}
        for i in range(1, n + 1)
    ]


def _indexed_keys():
    with db.session() as conn:
        return {r[0] for r in conn.execute("SELECT rating_key FROM library_tracks")}


@pytest.fixture(autouse=True)
def small_pages(monkeypatch, library):
    monkeypatch.setattr(me, "REINDEX_PAGE_SIZE", 100)
    library([])


def test_pages_through_all_tracks(fake):
    fake.state["library_tracks"] = _tracks(250)
    assert me.reindex_library_tracks() == 250
    assert len(_indexed_keys()) == 250
    assert len(fake.requests("GET", "/library/sections/")) == 3


def test_prunes_small_number_of_deleted_tracks(fake):
    fake.state["library_tracks"] = _tracks(100)
    me.reindex_library_tracks()
    fake.state["library_tracks"] = _tracks(95)  # 5% removed in Plex
    me.reindex_library_tracks()
    assert len(_indexed_keys()) == 95


def test_refuses_large_prune(fake):
    fake.state["library_tracks"] = _tracks(100)
    me.reindex_library_tracks()
    fake.state["library_tracks"] = _tracks(10)  # e.g. wrong section / partial response
    me.reindex_library_tracks()
    assert len(_indexed_keys()) == 100


def test_incomplete_fetch_upserts_but_never_prunes(fake):
    fake.state["library_tracks"] = _tracks(250)
    me.reindex_library_tracks()
    fake.state["library_tracks"] = _tracks(240, prefix="new-")
    fake.state["mode"]["tracks_fail_at"] = 200
    assert me.reindex_library_tracks() == 200
    keys = _indexed_keys()
    assert len(keys) == 250 + 200  # old rows kept, first two new pages added


def test_plex_down_returns_zero_and_changes_nothing(fake):
    fake.state["library_tracks"] = _tracks(50)
    me.reindex_library_tracks()
    fake.state["mode"]["tracks_fail_at"] = 0
    assert me.reindex_library_tracks() == 0
    assert len(_indexed_keys()) == 50
