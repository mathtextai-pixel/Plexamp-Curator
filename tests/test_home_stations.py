import pytest
import app as app_module
from home_stations import (
    HOME_STATION_DEFINITIONS,
    _gather_candidate_tracks_for_station,
)

HEADERS = {"X-Requested-With": "CrateExcavator"}


@pytest.fixture
def client():
    return app_module.app.test_client()


def test_home_station_definitions_structure():
    assert len(HOME_STATION_DEFINITIONS) == 5
    for key in ("wake_up", "morning_coffee", "coming_home", "family_dinner", "bedtime"):
        sdef = HOME_STATION_DEFINITIONS[key]
        assert sdef["title"].startswith("🏡 Home: ")
        assert "icon" in sdef
        assert "name" in sdef
        assert "description" in sdef
        assert len(sdef["primary_artists"]) >= 5
        assert len(sdef["keywords"]) >= 5
        assert sdef.get("target_count") == 35


def test_home_station_gather_candidates(library):
    wake_up = HOME_STATION_DEFINITIONS["wake_up"]
    candidates = _gather_candidate_tracks_for_station(wake_up, recent_keys=set(), recent_pairs=set())
    assert isinstance(candidates, list)


def test_api_home_stations_endpoints(client):
    res = client.get("/api/home-stations")
    assert res.status_code == 200
    data = res.get_json()
    assert "wake_up" in data
    assert "morning_coffee" in data
    assert "coming_home" in data
    assert "family_dinner" in data
    assert "bedtime" in data


def test_api_home_stations_invalid_station(client):
    res = client.post(
        "/api/home-stations/generate/non_existent_station",
        headers=HEADERS,
    )
    assert res.status_code == 404
