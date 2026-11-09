"""In-place Plex playlist updates: the ratingKey must survive every failure mode."""

import threading

import music_excavator as me

TITLE = "🎯 Crate: Test Channel"


def _existing(fake, items=("9", "9"), **extra):
    fake.state["playlists"]["1"] = {"title": TITLE, "items": list(items), **extra}


def _posts(fake):
    return fake.requests("POST", "/playlists")


def test_updates_existing_playlist_in_place(fake):
    _existing(fake)
    result = me.create_or_replace_plex_playlist(TITLE, ["1", "2", "3"])
    assert result["ratingKey"] == "1"
    assert fake.state["playlists"]["1"]["items"] == ["1", "2", "3"]
    assert not _posts(fake)


def test_title_match_is_case_and_whitespace_insensitive(fake):
    _existing(fake)
    result = me.create_or_replace_plex_playlist("  " + TITLE.upper() + " ", ["5"])
    assert result["ratingKey"] == "1"
    assert not _posts(fake)


def test_creates_playlist_when_plex_confirms_none_exists(fake):
    result = me.create_or_replace_plex_playlist("Brand New", ["5", "6"])
    assert result and result["leafCount"] == 2
    assert len(_posts(fake)) == 1


def test_lookup_failure_never_creates_a_duplicate(fake):
    _existing(fake)
    fake.state["mode"]["list_fail"] = True
    assert me.create_or_replace_plex_playlist(TITLE, ["1"]) is None
    assert not _posts(fake)
    assert fake.state["playlists"]["1"]["items"] == ["9", "9"]


def test_put_failure_is_retried_once_playlist_confirmed_empty(fake):
    _existing(fake)
    fake.state["mode"]["put_fail_n"] = 1
    result = me.create_or_replace_plex_playlist(TITLE, ["1", "2"])
    assert result["ratingKey"] == "1"
    assert fake.state["playlists"]["1"]["items"] == ["1", "2"]
    assert not _posts(fake)


def test_repeated_put_failure_leaves_playlist_and_creates_nothing(fake):
    _existing(fake)
    fake.state["mode"]["put_fail_n"] = 10
    assert me.create_or_replace_plex_playlist(TITLE, ["1", "2"]) is None
    assert not _posts(fake)
    assert list(fake.state["playlists"]) == ["1"]


def test_lost_put_response_is_not_resent(fake):
    """Plex applied the PUT but the reply was lost: re-sending would duplicate every track."""
    _existing(fake)
    fake.state["mode"]["put_drop_response"] = True
    result = me.create_or_replace_plex_playlist(TITLE, ["1", "2"])
    assert result["ratingKey"] == "1"
    assert fake.state["playlists"]["1"]["items"] == ["1", "2"]
    assert len(fake.requests("PUT")) == 1


def test_smart_playlist_with_same_title_is_left_alone(fake):
    _existing(fake, smart=True)
    me.create_or_replace_plex_playlist(TITLE, ["1"])
    assert fake.state["playlists"]["1"]["items"] == ["9", "9"]
    assert len(_posts(fake)) == 1


def test_non_numeric_rating_keys_are_dropped(fake):
    _existing(fake)
    result = me.create_or_replace_plex_playlist(TITLE, ["1", "2/../../x", "", None, "3&uri=evil"])
    assert result["ratingKey"] == "1"
    assert fake.state["playlists"]["1"]["items"] == ["1"]


def test_only_invalid_keys_does_nothing(fake):
    _existing(fake)
    assert me.create_or_replace_plex_playlist(TITLE, ["abc"]) is None
    assert not fake.requests("DELETE")


def test_concurrent_writers_are_serialised(fake):
    _existing(fake)
    fake.state["mode"]["delay"] = 0.1
    threads = [
        threading.Thread(target=me.create_or_replace_plex_playlist, args=(TITLE, [f"{i}1", f"{i}2"]))
        for i in range(1, 4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = fake.state["playlists"]["1"]["items"]
    # Exactly one writer's set, never a mix or a doubled list.
    assert len(items) == 2 and items[0][0] == items[1][0]


def test_append_adds_one_item(fake):
    _existing(fake, items=("1",))
    result = me.append_to_plex_playlist(TITLE, "2")
    assert result["leafCount"] == 2
    assert fake.state["playlists"]["1"]["items"] == ["1", "2"]


def test_append_on_lookup_failure_does_not_create(fake):
    fake.state["mode"]["list_fail"] = True
    assert me.append_to_plex_playlist(TITLE, "2") is None
    assert not _posts(fake)


def test_append_rejects_non_numeric_key(fake):
    _existing(fake)
    assert me.append_to_plex_playlist(TITLE, "1,2") is None
    assert not fake.requests("PUT")
