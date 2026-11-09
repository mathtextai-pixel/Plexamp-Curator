"""Flask endpoints: header check, input validation, background jobs and their locks."""

import os
import subprocess
import sys
import threading
import time

import pytest

import app as app_module
import discovery_engine as de
import gemini_curator as gc
import jobs
from locks import REFRESH_JOB_LOCK, file_lock

HEADERS = {"X-Requested-With": "CrateExcavator"}


@pytest.fixture
def client():
    return app_module.app.test_client()


def post(client, url, json=None, headers=HEADERS):
    return client.post(url, json=json if json is not None else {}, headers=headers)


def wait_for_job(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# --- cross-site protection --------------------------------------------------

def test_post_without_header_is_rejected(client):
    r = post(client, "/api/playlists/generate-all", headers={})
    assert r.status_code == 403


def test_get_does_not_need_header(client):
    assert client.get("/api/llm-status").status_code == 200


# --- input validation -------------------------------------------------------

@pytest.mark.parametrize("url,body", [
    ("/api/discover-more", {"count": "lots"}),
    ("/api/discover-more", {"count": 5000}),
    ("/api/discover-more", {"count": True}),
    ("/api/playlists/generate-from-prompt", {"prompt": ""}),
    ("/api/playlists/generate-from-prompt", {"prompt": "x" * 501}),
    ("/api/playlists/generate-from-prompt", {"prompt": "ok", "count": 1000}),
    ("/api/playlists/generate-from-prompt", {"prompt": "ok", "provider": "openai"}),
    ("/api/playlists/generate-from-prompt", {"prompt": ["not", "text"]}),
    ("/api/playlists/save-permanent", {"title": "T", "rating_keys": ["1", "2/../x"]}),
    ("/api/playlists/save-permanent", {"title": "T", "rating_keys": "1,2"}),
    ("/api/playlists/save-permanent", {"title": "T", "rating_keys": [str(i) for i in range(501)]}),
    ("/api/vault/add-track", {"rating_key": "12&x=1"}),
    ("/api/add-to-lidarr", {"artist": ""}),
    ("/api/llm-provider", {"provider": "openai"}),
])
def test_bad_input_is_400(client, url, body):
    r = post(client, url, body)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["success"] is False


def test_non_json_body_is_400_not_500(client):
    r = client.post("/api/playlists/generate-from-prompt", data="nope", headers=HEADERS)
    assert r.status_code == 400


# --- background jobs --------------------------------------------------------

def test_long_action_returns_202_and_job_completes(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "generate_channel_playlists",
        lambda *a, progress=None, **k: (progress("halfway"), [{"title": "X", "track_count": 1}])[1],
    )
    r = post(client, "/api/playlists/generate-all")
    assert r.status_code == 202
    job = wait_for_job(client, r.get_json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["success"] is True
    assert job["result"]["playlists"][0]["title"] == "X"


def test_job_exception_is_reported_as_error(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(app_module, "generate_large_vault_playlist", boom)
    r = post(client, "/api/vault/large-playlist")
    job = wait_for_job(client, r.get_json()["job_id"])
    assert job["status"] == "error"
    assert "kaboom" in job["result"]["message"]


def test_refresh_lock_released_after_job(client, monkeypatch):
    monkeypatch.setattr(app_module, "generate_large_vault_playlist", lambda **k: None)
    first = post(client, "/api/vault/large-playlist").get_json()["job_id"]
    wait_for_job(client, first)
    assert post(client, "/api/vault/large-playlist").status_code == 202


@pytest.mark.parametrize("url", [
    "/api/playlists/generate-all",
    "/api/playlists/generate/canon",
    "/api/vault/large-playlist",
])
def test_second_refresh_gets_409(client, url):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with file_lock(REFRESH_JOB_LOCK):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        r = post(client, url)
        assert r.status_code == 409
        assert "already running" in r.get_json()["message"]
    finally:
        release.set()
        t.join()


def test_unknown_channel_is_404(client):
    assert post(client, "/api/playlists/generate/nope").status_code == 404


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_job_from_dead_worker_is_marked_error(client):
    job_id = jobs.start_job("test", lambda progress: (200, {"success": True}))
    wait_for_job(client, job_id)
    conn = jobs._conn()
    conn.execute("UPDATE jobs SET status='running', pid=? WHERE id=?", (2 ** 22 + 12345, job_id))
    conn.commit()
    conn.close()
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "error"


# --- Lidarr sync ------------------------------------------------------------

def _one_downloaded_rec():
    conn = de.get_db()
    conn.execute("DELETE FROM recommendations")
    conn.execute("INSERT INTO recommendations (artist, album, lidarr_status) VALUES ('Waxahatchee', 'Tigers Blood', 'Downloaded')")
    conn.commit()
    conn.close()


def test_sync_with_lidarr_down_keeps_statuses(client, fake):
    _one_downloaded_rec()
    fake.state["lidarr_down"] = True
    job = wait_for_job(client, post(client, "/api/sync-lidarr").get_json()["job_id"])
    assert job["http_status"] == 502
    assert job["result"]["success"] is False
    assert [r["lidarr_status"] for r in de.get_all_recommendations()] == ["Downloaded"]


def test_sync_updates_status(client, fake):
    _one_downloaded_rec()
    fake.state["lidarr_artists"] = [{"id": 7, "artistName": "Waxahatchee"}]
    fake.state["lidarr_albums"] = {7: [{"title": "Tigers Blood", "hasFile": False, "monitored": True}]}
    job = wait_for_job(client, post(client, "/api/sync-lidarr").get_json()["job_id"])
    assert job["result"]["success"] is True
    assert [r["lidarr_status"] for r in de.get_all_recommendations()] == ["Wanted"]


# --- AI provider setting ----------------------------------------------------

def test_provider_setting_is_shared_across_processes(client):
    try:
        assert post(client, "/api/llm-provider", {"provider": "ollama"}).get_json()["provider"] == "ollama"
        # A separate process stands in for the other Gunicorn worker.
        out = subprocess.run(
            [sys.executable, "-c", "import gemini_curator as g; print(g.get_active_provider())"],
            env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))},
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[-1]
        assert out == "ollama"
    finally:
        gc.set_active_provider("gemini")
