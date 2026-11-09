"""The unplayed vault is paged through completely, not cut off alphabetically."""

import music_excavator as me


def _albums(n):
    return [{"ratingKey": str(i), "parentTitle": f"Artist {i:05d}", "title": f"Album {i}"} for i in range(n)]


def test_pages_through_whole_vault(fake):
    fake.state["vault_albums"] = _albums(1234)
    vault = me.get_unplayed_vault_albums()
    assert len(vault) == 1234
    assert vault[-1]["artist"] == "Artist 01233"
    assert len(fake.requests("GET", "/library/sections/")) == 3  # 500 + 500 + 234


def test_limit_caps_total(fake):
    fake.state["vault_albums"] = _albums(1234)
    assert len(me.get_unplayed_vault_albums(limit=600)) == 600


def test_exact_page_multiple_stops_on_total_size(fake):
    fake.state["vault_albums"] = _albums(1000)
    assert len(me.get_unplayed_vault_albums()) == 1000
    assert len(fake.requests("GET", "/library/sections/")) == 2


def test_plex_down_returns_empty(fake):
    fake.state["mode"]["vault_fail"] = True
    assert me.get_unplayed_vault_albums() == []
