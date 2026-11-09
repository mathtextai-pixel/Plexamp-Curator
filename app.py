#!/usr/bin/env python3
"""
Crate Excavator: Unified Music Discovery, Vault Mining & AI DJ Server.
Runs on desktop and mobile (with automatic client detection and manual toggle).
Includes an in-container background scheduler (cron expressions from config) for
library re-indexing, playlist refreshes and the discovery crawl.

Long-running actions run as background jobs: the POST returns 202 + job_id and the
UI polls GET /api/jobs/<id> (see jobs.py).
"""

import datetime
import fcntl
import json
import os
import random
import sqlite3
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

import discovery_engine
import gemini_curator
import jobs
import weekly_refresh
from config import CONFIG, get_int, public_config
from cron import CronError, CronSchedule
from locks import REFRESH_JOB_LOCK, LockBusy, acquire, file_lock
from music_excavator import (
    CHANNEL_DEFINITIONS,
    append_to_plex_playlist,
    create_or_replace_plex_playlist,
    generate_channel_playlists,
    generate_large_vault_playlist,
    get_library_track_count,
    get_plex_machine_id,
    get_unplayed_vault_albums,
    reindex_library_tracks,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CONFIG["DATA_DIR"], "recommendations.db")

# Every state-changing API call must carry this header. Browsers won't add a custom
# header to a cross-site form post, so other web pages can't trigger these endpoints.
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "CrateExcavator"

REINDEX_JOB_LOCK = "job:reindex"
LIDARR_SYNC_JOB_LOCK = "job:lidarr-sync"
DISCOVERY_JOB_LOCK = "job:discovery"

# Input limits
MAX_PROMPT_CHARS = 500
MAX_TITLE_CHARS = 150
MAX_NAME_CHARS = 200
MAX_PLAYLIST_KEYS = 500

app = Flask(__name__, template_folder=os.path.join(SCRIPT_DIR, "templates"))
_SCHEDULER_LOCK_FD = None


# ---------------------------------------------------------------------------
# Request validation helpers
# ---------------------------------------------------------------------------

class InvalidInput(Exception):
    """Client sent a malformed or out-of-range value (-> HTTP 400)."""


@app.errorhandler(InvalidInput)
def _handle_invalid_input(err):
    return jsonify({"success": False, "message": str(err)}), 400


@app.errorhandler(500)
def _handle_server_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "message": "Internal server error (see server log)"}), 500
    return err


@app.before_request
def _require_api_header():
    if request.method == "POST" and request.path.startswith("/api/"):
        if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
            return jsonify({"success": False, "message": f"Missing {CSRF_HEADER} header"}), 403
    return None


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int_field(data: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = data.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise InvalidInput(f"'{key}' must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise InvalidInput(f"'{key}' must be a whole number")
    if not lo <= n <= hi:
        raise InvalidInput(f"'{key}' must be between {lo} and {hi}")
    return n


def _str_field(data: Dict[str, Any], key: str, max_len: int, required: bool = True) -> str:
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidInput(f"'{key}' must be text")
    value = value.strip()
    if required and not value:
        raise InvalidInput(f"Missing '{key}'")
    if len(value) > max_len:
        raise InvalidInput(f"'{key}' is too long (max {max_len} characters)")
    return value


def _rating_key(value: Any, field: str = "rating_key") -> str:
    key = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not key.isdigit():
        raise InvalidInput(f"'{field}' must be a numeric Plex rating key")
    return key


# ---------------------------------------------------------------------------
# Background job helpers
# ---------------------------------------------------------------------------

def _busy_response(what: str = "A playlist refresh"):
    return jsonify({
        "success": False,
        "message": f"{what} is already running. Try again when it finishes.",
    }), 409


def _start_job(kind: str, fn: jobs.JobFn, lock_name: Optional[str] = None, busy_label: str = "A playlist refresh"):
    """Start `fn` as a background job; returns 202 + job_id, or 409 if `lock_name` is held."""
    handle = None
    if lock_name:
        try:
            handle = acquire(lock_name, timeout=0)
        except LockBusy:
            return _busy_response(busy_label)
    try:
        job_id = jobs.start_job(kind, fn, lock=handle)
    except Exception:
        if handle:
            handle.release()
        raise
    return jsonify({"success": True, "job_id": job_id, "status_url": f"/api/jobs/{job_id}"}), 202


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"success": False, "message": "Unknown job"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _compute_channel_counts(recs: list) -> dict:
    """Compute dynamic recommendation counts per channel key."""
    name_to_key = {cdef["name"]: key for key, cdef in CHANNEL_DEFINITIONS.items()}
    counts = {key: 0 for key in CHANNEL_DEFINITIONS}
    for r in recs:
        ch_name = r.get("channel") or ""
        key = name_to_key.get(ch_name)
        if key:
            counts[key] += 1
    return counts


def _public_channel_defs() -> dict:
    """Export channel metadata and keywords so templates have a single source of truth."""
    return {
        key: {
            "name": cdef["name"],
            "title": cdef["title"],
            "icon": cdef["icon"],
            "description": cdef["description"],
            "keywords": cdef["keywords"],
        }
        for key, cdef in CHANNEL_DEFINITIONS.items()
    }


def is_mobile_request(req) -> bool:
    """Detect if incoming request is from a mobile device or user requested mobile view."""
    view_param = req.args.get("view", "").lower()
    if view_param == "mobile":
        return True
    if view_param == "desktop":
        return False

    user_agent = req.headers.get("User-Agent", "").lower()
    mobile_patterns = ["mobile", "android", "iphone", "ipad", "ipod", "silk", "kindle", "opera mini"]
    return any(p in user_agent for p in mobile_patterns)


@app.route("/")
def index():
    pub_cfg = public_config()
    llm_status = gemini_curator.get_llm_status()
    track_count = get_library_track_count()
    machine_id = get_plex_machine_id() or ""

    if is_mobile_request(request):
        host_label = urllib.parse.urlparse(pub_cfg.get("PUBLIC_HOST", "")).hostname or "Tailscale"
        return render_template(
            "mobile.html",
            host_label=host_label,
            config=pub_cfg,
            plex_machine_id=machine_id,
            library_track_count=track_count,
            llm_status=llm_status,
        )

    recs = discovery_engine.get_all_recommendations()
    channel_counts = _compute_channel_counts(recs)
    return render_template(
        "desktop.html",
        recommendations=recs,
        channel_counts=channel_counts,
        channel_defs=_public_channel_defs(),
        config=pub_cfg,
        plex_machine_id=machine_id,
        library_track_count=track_count,
        llm_status=llm_status,
    )


# ---------------------------------------------------------------------------
# Quick, synchronous API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/channels")
def api_channels():
    return jsonify(_public_channel_defs())


@app.route("/api/llm-status")
def api_llm_status():
    return jsonify(gemini_curator.get_llm_status())


@app.route("/api/llm-provider", methods=["POST"])
def api_set_llm_provider():
    provider = _str_field(_json_body(), "provider", 20).lower()
    if provider not in ("gemini", "ollama"):
        raise InvalidInput("Invalid provider (must be 'gemini' or 'ollama')")
    active = gemini_curator.set_active_provider(provider)
    return jsonify({
        "success": True,
        "provider": active,
        "status": gemini_curator.get_llm_status(),
    })


@app.route("/api/recommendations")
def api_recommendations():
    return jsonify(discovery_engine.get_all_recommendations())


@app.route("/api/vault")
def api_vault():
    return jsonify(get_unplayed_vault_albums(limit=5000))


@app.route("/api/playlists/save-permanent", methods=["POST"])
def api_save_permanent():
    data = _json_body()
    title = _str_field(data, "title", MAX_TITLE_CHARS)
    raw_keys = data.get("rating_keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise InvalidInput("Missing 'rating_keys'")
    if len(raw_keys) > MAX_PLAYLIST_KEYS:
        raise InvalidInput(f"Too many tracks (max {MAX_PLAYLIST_KEYS})")
    rating_keys = [_rating_key(k, "rating_keys") for k in raw_keys]

    pl = create_or_replace_plex_playlist(title, rating_keys)
    if pl:
        return jsonify({
            "success": True,
            "playlist": pl["title"],
            "rating_key": pl["ratingKey"],
            "track_count": pl["leafCount"],
            "message": f"Saved permanent playlist '{pl['title']}' to Plexamp!",
        })
    return jsonify({"success": False, "message": "Failed to create permanent playlist"}), 500


@app.route("/api/add-to-lidarr", methods=["POST"])
def api_add_to_lidarr():
    data = _json_body()
    artist = _str_field(data, "artist", MAX_NAME_CHARS)
    album = _str_field(data, "album", MAX_NAME_CHARS, required=False) or None

    result = discovery_engine.add_to_lidarr(artist, album)

    if result.get("success"):
        conn = discovery_engine.get_db()
        conn.execute(
            "UPDATE recommendations SET lidarr_status='Wanted' WHERE artist=?",
            (artist,),
        )
        conn.commit()
        conn.close()

    return jsonify(result)


@app.route("/api/vault/add-track", methods=["POST"])
def api_vault_add():
    rk = _rating_key(_json_body().get("rating_key"))
    playlist_name = "🎯 Vault Excavator: Current Mood"
    result = append_to_plex_playlist(playlist_name, rk)
    if result:
        return jsonify({"success": True, "leafCount": result["leafCount"]})
    return jsonify({"success": False, "message": "Could not add album to playlist"}), 500


# ---------------------------------------------------------------------------
# Long-running API endpoints (background jobs)
# ---------------------------------------------------------------------------

@app.route("/api/discover-more", methods=["POST"])
def api_discover_more():
    count = _int_field(_json_body(), "count", get_int("DISCOVERY_PICKS_PER_WEEK"), 1, 20)

    def work(progress):
        added = discovery_engine.run_weekly_discovery_crawl(count=count, progress=progress)
        return 200, {
            "success": True,
            "added_count": len(added),
            "added": added,
            "message": f"Discovered {len(added)} new album recommendations!",
        }

    return _start_job("discover-more", work, DISCOVERY_JOB_LOCK, "A discovery crawl")


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    def work(progress):
        progress("Reading every track from Plex…")
        count = reindex_library_tracks()
        return (200 if count > 0 else 502), {
            "success": count > 0,
            "track_count": count,
            "message": f"Indexed {count:,} tracks from Plex library." if count else "Could not index tracks from Plex.",
        }

    return _start_job("reindex", work, REINDEX_JOB_LOCK, "A library re-index")


@app.route("/api/playlists/generate-from-prompt", methods=["POST"])
def api_generate_from_prompt():
    data = _json_body()
    user_prompt = _str_field(data, "prompt", MAX_PROMPT_CHARS)
    target_count = _int_field(data, "count", 20, 5, 50)
    provider_override = data.get("provider")
    if provider_override not in (None, "", "gemini", "ollama"):
        raise InvalidInput("Invalid provider (must be 'gemini' or 'ollama')")

    def work(progress):
        result = gemini_curator.generate_playlist_from_user_prompt(
            user_prompt,
            target_count=target_count,
            provider_override=provider_override or None,
            progress=progress,
        )
        if not result:
            return 500, {"success": False, "message": "Could not match songs in library for this prompt"}
        return 200, {
            "success": True,
            "title": result["title"],
            "creative_title": result.get("creative_title", result["title"]),
            "vibe": result["vibe"],
            "track_count": result["track_count"],
            "tracks": result["tracks"],
            "rating_keys": result.get("rating_keys", []),
            "rating_key": result.get("rating_key"),
            "provider": result.get("provider"),
            "llm_status": gemini_curator.get_llm_status(),
        }

    # No job-level lock: writes to "The Daily Vibe" are serialised by the per-playlist lock.
    return _start_job("prompt-playlist", work)


@app.route("/api/mobile/surprise-me", methods=["POST"])
def api_surprise_me():
    def work(progress):
        conn = sqlite3.connect(DB_PATH, timeout=20.0)
        try:
            artists = [r[0] for r in conn.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")]
        except sqlite3.OperationalError:
            artists = []
        finally:
            conn.close()
        if not artists:
            return 500, {"success": False, "message": "No library tracks indexed"}

        progress("Dreaming up a curveball…")
        prompt = f"""You are a musical director picking a "Surprise Me" sonic journey.
From this listener's library artists:
{json.dumps(random.sample(artists, min(len(artists), 120)))}

Craft an eclectic, high-contrast prompt that bridges 3-4 disparate artists in their collection (e.g. outlaw country with vintage soul and 90s alt-rock) into a cohesive sonic adventure.
Return JSON:
{{
  "prompt": "1-2 sentence prompt describing this surprise mix"
}}"""
        res = gemini_curator.call_llm_json(prompt, purpose="mobile_surprise_me")
        if isinstance(res, dict) and isinstance(res.get("prompt"), str) and res["prompt"].strip():
            return 200, {"success": True, "prompt": res["prompt"].strip()[:MAX_PROMPT_CHARS]}

        fallback_prompts = [
            "Warm vintage acoustic soul and rustic mountain picking like Gillian Welch and Khruangbin",
            "Fast anthemic singalongs and cathartic emo choruses like The Menzingers and Frank Turner",
            "Sun-drenched guitar jangle and dreamy indie melodies like Flipturn and Men I Trust",
        ]
        return 200, {"success": True, "prompt": random.choice(fallback_prompts)}

    return _start_job("surprise-me", work)


@app.route("/api/playlists/generate-all", methods=["POST"])
def api_generate_all():
    def work(progress):
        playlists = generate_channel_playlists(rotate=True, use_gemini_flow=True, progress=progress)
        return 200, {
            "success": True,
            "playlists": playlists,
            "llm_status": gemini_curator.get_llm_status(),
        }

    return _start_job("generate-all", work, REFRESH_JOB_LOCK)


@app.route("/api/playlists/generate/<channel_key>", methods=["POST"])
def api_generate_single(channel_key):
    if channel_key not in CHANNEL_DEFINITIONS:
        return jsonify({"success": False, "message": "Channel not found"}), 404

    def work(progress):
        pls = generate_channel_playlists(channel_key, rotate=True, use_gemini_flow=True, progress=progress)
        if not pls:
            return 500, {"success": False, "message": "Could not update channel playlist (see server log)"}
        return 200, {
            "success": True,
            "playlist": pls[0]["title"],
            "rating_key": pls[0].get("rating_key"),
            "track_count": pls[0]["track_count"],
            "album_count": pls[0]["album_count"],
            "llm_status": gemini_curator.get_llm_status(),
        }

    return _start_job(f"generate-{channel_key}", work, REFRESH_JOB_LOCK)


@app.route("/api/vault/large-playlist", methods=["POST"])
def api_vault_large_playlist():
    def work(progress):
        progress("Picking unplayed albums across the vault…")
        result = generate_large_vault_playlist(rotate=True)
        if not result:
            return 500, {"success": False, "message": "Failed to create large vault playlist"}
        return 200, {
            "success": True,
            "playlist": result["title"],
            "rating_key": result.get("rating_key"),
            "track_count": result["track_count"],
            "album_count": result["album_count"],
        }

    return _start_job("large-vault", work, REFRESH_JOB_LOCK)


@app.route("/api/sync-lidarr", methods=["POST"])
def api_sync_lidarr():
    def work(progress):
        recs = discovery_engine.get_all_recommendations()
        conn = discovery_engine.get_db()
        updated = 0
        unreachable = 0
        try:
            for i, r in enumerate(recs, 1):
                progress(f"Checking {i}/{len(recs)}: {r['artist']} – {r['album']}")
                status = discovery_engine.check_lidarr_status_for_album(r["artist"], r["album"])
                if status is None:
                    # Lidarr error: keep the last known status rather than resetting it to "Not Added".
                    unreachable += 1
                    continue
                if status != r["lidarr_status"]:
                    conn.execute("UPDATE recommendations SET lidarr_status=? WHERE id=?", (status, r["id"]))
                    updated += 1
            conn.commit()
        finally:
            conn.close()

        if recs and unreachable == len(recs):
            return 502, {"success": False, "message": "Could not reach Lidarr; no statuses were changed."}
        message = f"Updated Lidarr status for {updated} albums"
        if unreachable:
            message += f" ({unreachable} could not be checked and were left unchanged)"
        return 200, {"success": True, "message": message}

    return _start_job("sync-lidarr", work, LIDARR_SYNC_JOB_LOCK, "A Lidarr sync")


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

# Missed minutes (e.g. while a long task was running) are caught up, but only within this window.
SCHEDULER_CATCHUP_MINUTES = 6 * 60


def _scheduled_reindex() -> None:
    try:
        with file_lock(REINDEX_JOB_LOCK, timeout=15 * 60):
            reindex_library_tracks()
    except LockBusy:
        print("[Scheduler] Skipped re-index: another re-index held the lock for 15 min.")


def _scheduled_refresh() -> None:
    # Discovery has its own schedule (WEEKLY_DISCOVERY_CRON).
    weekly_refresh.run_refresh(include_discovery=False)


def _scheduled_discovery() -> None:
    try:
        with file_lock(DISCOVERY_JOB_LOCK, timeout=15 * 60):
            discovery_engine.run_weekly_discovery_crawl()
    except LockBusy:
        print("[Scheduler] Skipped discovery crawl: another crawl held the lock for 15 min.")


def _load_schedule() -> List[Tuple[str, CronSchedule, Callable[[], None]]]:
    """Build (name, schedule, fn) for each configured task; set a *_CRON value to 'off' to disable it."""
    tasks = []
    for key, name, fn in (
        ("NIGHTLY_REINDEX_CRON", "library re-index", _scheduled_reindex),
        ("WEEKLY_REFRESH_CRON", "channel & vault playlist refresh", _scheduled_refresh),
        ("WEEKLY_DISCOVERY_CRON", "discovery crawl", _scheduled_discovery),
    ):
        expr = str(CONFIG.get(key) or "").strip()
        if not expr or expr.lower() in ("off", "disabled", "none"):
            print(f"[Scheduler] {name} disabled ({key} is off).")
            continue
        try:
            tasks.append((name, CronSchedule(expr), fn))
        except CronError as e:
            print(f"[Scheduler] Invalid {key}={expr!r} ({e}); {name} is DISABLED until fixed.")
    return tasks


def _start_background_scheduler():
    """
    Start a single background scheduler thread (guarded by an advisory file lock
    in DATA_DIR so multiple Gunicorn workers never duplicate scheduled tasks).
    Handles:
      - Startup initialization (seed recommendations + initial library track index if empty)
      - NIGHTLY_REINDEX_CRON  (default 03:30 daily)  library track re-index
      - WEEKLY_REFRESH_CRON   (default Mon 09:00)    channel + vault playlist refresh
      - WEEKLY_DISCOVERY_CRON (default Mon 09:30)    discovery crawl
    Times use the container's local time zone (set TZ to change it).
    """
    global _SCHEDULER_LOCK_FD
    if str(CONFIG.get("RUN_SCHEDULER", "1")) != "1":
        return

    os.makedirs(CONFIG["DATA_DIR"], exist_ok=True)
    lock_path = os.path.join(CONFIG["DATA_DIR"], "scheduler.lock")
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _SCHEDULER_LOCK_FD = fd
    except (IOError, OSError):
        # Another worker already holds the scheduler lock
        return

    tasks = _load_schedule()

    def _scheduler_loop():
        # 1. Startup check (non-blocking after 2s)
        time.sleep(2)
        try:
            recs = discovery_engine.get_all_recommendations()
            if not recs:
                print("[Scheduler] Populating initial seed recommendations...")
                discovery_engine.populate_and_sync_db()
            if get_library_track_count() == 0:
                print("[Scheduler] library_tracks is empty; running initial Plex track index...")
                _scheduled_reindex()
        except Exception as e:
            print(f"[Scheduler] Startup check error: {e}")

        last_checked = datetime.datetime.now().replace(second=0, microsecond=0)

        while True:
            time.sleep(20)
            now = datetime.datetime.now().replace(second=0, microsecond=0)
            if now <= last_checked:
                continue  # same minute, or the clock moved backwards
            earliest = now - datetime.timedelta(minutes=SCHEDULER_CATCHUP_MINUTES)
            minute = max(last_checked + datetime.timedelta(minutes=1), earliest)
            last_checked = now

            # Walk every minute since the last check, so a minute skipped while a
            # long task was running still fires its tasks.
            while minute <= now:
                for name, schedule, fn in tasks:
                    if schedule.matches(minute):
                        print(f"[{datetime.datetime.now()}] [Scheduler] Running {name} (scheduled {minute:%a %H:%M})...")
                        try:
                            fn()
                        except Exception as e:
                            print(f"[Scheduler] {name} error: {e}")
                minute += datetime.timedelta(minutes=1)

    t = threading.Thread(target=_scheduler_loop, name="crate-scheduler", daemon=True)
    t.start()
    summary = " | ".join(f"{name}: {schedule.expr}" for name, schedule, _ in tasks) or "no tasks"
    print(f"✓ Background scheduler started ({summary}).")


_start_background_scheduler()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", CONFIG.get("PORT", 5050)))
    print(f"Starting Crate Excavator Unified Server on http://{host}:{port} ...")
    app.run(host=host, port=port, debug=False)
