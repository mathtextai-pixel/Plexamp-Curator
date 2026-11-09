"""Lidarr status checks and the Add-to-Lidarr flow."""

import pytest

import discovery_engine as de


@pytest.fixture
def lidarr(fake):
    fake.state["lidarr_artists"] = [{"id": 7, "artistName": "Waxahatchee", "foreignArtistId": "mb-wax"}]
    fake.state["lidarr_albums"] = {7: [
        {"title": "Tigers Blood", "hasFile": True, "monitored": True},
        {"title": "", "monitored": True},
        {"title": "Saint Cloud", "hasFile": False, "monitored": True},
    ]}
    return fake


def test_status_downloaded(lidarr):
    assert de.check_lidarr_status_for_album("Waxahatchee", "Tigers Blood") == "Downloaded"


def test_status_wanted(lidarr):
    assert de.check_lidarr_status_for_album("waxahatchee", "Saint Cloud") == "Wanted"


def test_empty_lidarr_album_title_does_not_match_everything(lidarr):
    assert de.check_lidarr_status_for_album("Waxahatchee", "Out in the Storm") == "Artist Added (Album Missing)"


def test_status_not_added(lidarr):
    assert de.check_lidarr_status_for_album("MJ Lenderman", "Manning Fireworks") == "Not Added"


def test_lidarr_down_returns_none_not_not_added(lidarr):
    lidarr.state["lidarr_down"] = True
    assert de.check_lidarr_status_for_album("Waxahatchee", "Tigers Blood") is None


@pytest.mark.parametrize("a,b,same", [
    ("The Menzingers", "Menzingers", True),
    ("Khruangbin & Leon Bridges", "Khruangbin and Leon Bridges", True),
    ("R.E.M.", "REM", True),
    ("PUP", "Pup", True),
    ("PUP", "Puppy", False),
    ("Wire", "Wireless", False),
])
def test_artist_name_normalisation(a, b, same):
    assert (de._normalize_artist_name(a) == de._normalize_artist_name(b)) is same


def test_add_requires_exact_match(fake):
    fake.state["lidarr_lookup"] = [
        {"artistName": "Puppy", "foreignArtistId": "mb-puppy"},
        {"artistName": "Pup Fiction", "foreignArtistId": "mb-pf"},
    ]
    result = de.add_to_lidarr("PUP")
    assert result["success"] is False
    assert "Puppy" in result["message"]
    assert not fake.state["lidarr_added"]


def test_add_picks_exact_match_even_if_not_first(fake):
    fake.state["lidarr_lookup"] = [
        {"artistName": "Puppy", "foreignArtistId": "mb-puppy"},
        {"artistName": "PUP", "foreignArtistId": "mb-pup"},
    ]
    result = de.add_to_lidarr("Pup")
    assert result["success"] is True
    added = fake.state["lidarr_added"][0]
    assert added["foreignArtistId"] == "mb-pup"
    # Whole-catalogue monitoring is intentional (kept as before).
    assert added["addOptions"] == {"monitor": "all", "searchForMissingAlbums": True}


def test_already_added_triggers_artist_search(fake):
    fake.state["lidarr_lookup"] = [{"artistName": "Waxahatchee", "foreignArtistId": "mb-wax"}]
    fake.state["lidarr_artists"] = [{"id": 7, "artistName": "Waxahatchee", "foreignArtistId": "mb-wax"}]
    fake.state["lidarr_add_error"] = (400, [{"errorMessage": "This artist has already been added."}])
    result = de.add_to_lidarr("Waxahatchee")
    assert result["success"] is True
    assert fake.state["lidarr_commands"] == [{"name": "ArtistSearch", "artistId": 7}]


def test_lookup_result_with_id_is_searched_not_re_added(fake):
    fake.state["lidarr_lookup"] = [{"artistName": "Waxahatchee", "foreignArtistId": "mb-wax", "id": 7}]
    fake.state["lidarr_artists"] = [{"id": 7, "artistName": "Waxahatchee", "foreignArtistId": "mb-wax"}]
    result = de.add_to_lidarr("Waxahatchee")
    assert result["success"] is True
    assert not fake.state["lidarr_added"]
    assert fake.state["lidarr_commands"] == [{"name": "ArtistSearch", "artistId": 7}]


@pytest.fixture
def autodetect(monkeypatch):
    """Clear the configured Lidarr folder/profiles so they must be auto-detected."""
    for key in ("LIDARR_ROOT_FOLDER", "LIDARR_QUALITY_PROFILE_ID", "LIDARR_METADATA_PROFILE_ID"):
        monkeypatch.setitem(de.CONFIG, key, "")
    monkeypatch.setattr(de, "_LIDARR_DEFAULTS_CACHE", None)


def test_add_refuses_when_lidarr_settings_unknown(fake, autodetect):
    fake.state["lidarr_lookup"] = [{"artistName": "PUP", "foreignArtistId": "mb-pup"}]
    real_handle = fake.server.RequestHandlerClass._lidarr

    def no_root_folder(self, method, path, q):
        if path == "/api/v1/rootfolder":
            return self._send(503, {})
        return real_handle(self, method, path, q)

    fake.server.RequestHandlerClass._lidarr = no_root_folder
    try:
        result = de.add_to_lidarr("PUP")
    finally:
        fake.server.RequestHandlerClass._lidarr = real_handle
    assert result["success"] is False and "root folder" in result["message"]
    assert not fake.state["lidarr_added"]
    assert de._LIDARR_DEFAULTS_CACHE is None  # a failure is not remembered


def test_autodetected_settings_are_used_and_cached(fake, autodetect):
    fake.state["lidarr_lookup"] = [{"artistName": "PUP", "foreignArtistId": "mb-pup"}]
    assert de.add_to_lidarr("PUP")["success"] is True
    added = fake.state["lidarr_added"][0]
    assert added["rootFolderPath"] == "/music" and added["qualityProfileId"] == 1
    assert de._LIDARR_DEFAULTS_CACHE == ("/music", 1, 1)


# Real Lidarr data (2026-11): MusicBrainz has three different artists named "PUP".
PUP_LOOKUP = [
    {"artistName": "Pup", "foreignArtistId": "mb-pup-90s", "disambiguation": "90s Canadian band"},
    {"artistName": "PUP", "foreignArtistId": "mb-pup-punk", "disambiguation": "Canadian punk band"},
    {"artistName": "Pup", "foreignArtistId": "mb-pup-rapper", "disambiguation": "Rapper"},
    {"artistName": "Pup Tent", "foreignArtistId": "mb-pup-tent"},
]
MORBID_STUFF_LOOKUP = [
    {"title": "Morbid Stuff", "artist": {"artistName": "PUP", "foreignArtistId": "mb-pup-punk"}},
    {"title": "Pup", "artist": {"artistName": "Fenton", "foreignArtistId": "mb-fenton"}},
]


def test_same_named_artists_are_resolved_by_album(fake):
    fake.state["lidarr_lookup"] = PUP_LOOKUP
    fake.state["lidarr_album_lookup"] = MORBID_STUFF_LOOKUP
    result = de.add_to_lidarr("PUP", "Morbid Stuff")
    assert result["success"] is True
    assert fake.state["lidarr_added"][0]["foreignArtistId"] == "mb-pup-punk"  # not the first "Pup"


def test_same_named_artists_prefer_the_one_already_in_lidarr(fake):
    lookup = [dict(c) for c in PUP_LOOKUP]
    lookup[1]["id"] = 42
    fake.state["lidarr_lookup"] = lookup
    fake.state["lidarr_artists"] = [{"id": 42, "artistName": "PUP", "foreignArtistId": "mb-pup-punk"}]
    result = de.add_to_lidarr("PUP", "Some Album Lidarr Doesn't Know")
    assert result["success"] is True
    assert not fake.state["lidarr_added"]
    assert fake.state["lidarr_commands"] == [{"name": "ArtistSearch", "artistId": 42}]


def test_same_named_artists_without_a_way_to_decide_are_refused(fake):
    fake.state["lidarr_lookup"] = PUP_LOOKUP
    result = de.add_to_lidarr("PUP", "Unknown Album")
    assert result["success"] is False
    assert "Canadian punk band" in result["message"] and "Rapper" in result["message"]
    assert not fake.state["lidarr_added"]
