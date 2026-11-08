#!/usr/bin/env python3
"""
Crate Excavator: Unified Music Discovery, Vault Mining & AI DJ Server.
Runs on desktop and mobile (with automatic client detection and manual toggle).
Includes an in-container background scheduler for nightly library re-indexing
and weekly channel/vault/discovery refreshes.
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
from flask import Flask, jsonify, render_template, request

import discovery_engine
import gemini_curator
import weekly_refresh
from config import CONFIG, get_int, public_config
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

app = Flask(__name__, template_folder=os.path.join(SCRIPT_DIR, "templates"))
_SCHEDULER_LOCK_FD = None


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


@app.route("/api/channels")
def api_channels():
    return jsonify(_public_channel_defs())


@app.route("/api/llm-status")
def api_llm_status():
    return jsonify(gemini_curator.get_llm_status())


@app.route("/api/llm-provider", methods=["POST"])
def api_set_llm_provider():
    data = request.json or {}
    provider = data.get("provider", "").strip().lower()
    if provider not in ("gemini", "ollama"):
        return jsonify({"success": False, "message": "Invalid provider (must be 'gemini' or 'ollama')"}), 400
    active = gemini_curator.set_active_provider(provider)
    return jsonify({
        "success": True,
        "provider": active,
        "status": gemini_curator.get_llm_status(),
    })


@app.route("/api/recommendations")
def api_recommendations():
    recs = discovery_engine.get_all_recommendations()
    return jsonify(recs)


@app.route("/api/discover-more", methods=["POST"])
def api_discover_more():
    data = request.json or {}
    count = int(data.get("count") or get_int("DISCOVERY_PICKS_PER_WEEK"))
    added = discovery_engine.run_weekly_discovery_crawl(count=count)
    return jsonify({
        "success": True,
        "added_count": len(added),
        "added": added,
        "message": f"Discovered {len(added)} new album recommendations!",
    })


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    count = reindex_library_tracks()
    return jsonify({
        "success": count > 0,
        "track_count": count,
        "message": f"Indexed {count:,} tracks from Plex library.",
    })


@app.route("/api/playlists/generate-from-prompt", methods=["POST"])
def api_generate_from_prompt():
    data = request.json or {}
    user_prompt = data.get("prompt", "").strip()
    target_count = int(data.get("count", 20))
    provider_override = data.get("provider")
    if not user_prompt:
        return jsonify({"success": False, "message": "No prompt provided"}), 400

    result = gemini_curator.generate_playlist_from_user_prompt(
        user_prompt,
        target_count=target_count,
        provider_override=provider_override,
    )
    if result:
        return jsonify({
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
        })
    return jsonify({"success": False, "message": "Could not match songs in library for this prompt"}), 500


@app.route("/api/mobile/surprise-me", methods=["POST"])
def api_surprise_me():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    cur = conn.cursor()
    try:
        cur.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")
        artists = [r[0] for r in cur.fetchall()]
    except sqlite3.OperationalError:
        artists = []
    conn.close()

    if not artists:
        return jsonify({"success": False, "message": "No library tracks indexed"}), 500

    prompt = f"""You are a musical director picking a "Surprise Me" sonic journey.
From this listener's library artists:
{json.dumps(random.sample(artists, min(len(artists), 120)))}

Craft an eclectic, high-contrast prompt that bridges 3-4 disparate artists in their collection (e.g. outlaw country with vintage soul and 90s alt-rock) into a cohesive sonic adventure.
Return JSON:
{{
  "prompt": "1-2 sentence prompt describing this surprise mix"
}}"""

    res = gemini_curator.call_llm_json(prompt, purpose="mobile_surprise_me")
    if isinstance(res, dict) and res.get("prompt"):
        return jsonify({"success": True, "prompt": res["prompt"]})

    fallback_prompts = [
        "Warm vintage acoustic soul and rustic mountain picking like Gillian Welch and Khruangbin",
        "Fast anthemic singalongs and cathartic emo choruses like The Menzingers and Frank Turner",
        "Sun-drenched guitar jangle and dreamy indie melodies like Flipturn and Men I Trust",
    ]
    return jsonify({"success": True, "prompt": random.choice(fallback_prompts)})


@app.route("/api/playlists/save-permanent", methods=["POST"])
def api_save_permanent():
    data = request.json or {}
    title = data.get("title", "").strip()
    rating_keys = data.get("rating_keys", [])
    if not title or not rating_keys:
        return jsonify({"success": False, "message": "Missing title or rating keys"}), 400

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


@app.route("/api/playlists/generate-all", methods=["POST"])
def api_generate_all():
    playlists = generate_channel_playlists(rotate=True, use_gemini_flow=True)
    return jsonify({
        "success": True,
        "playlists": playlists,
        "llm_status": gemini_curator.get_llm_status(),
    })


@app.route("/api/playlists/generate/<channel_key>", methods=["POST"])
def api_generate_single(channel_key):
    pls = generate_channel_playlists(channel_key, rotate=True, use_gemini_flow=True)
    if pls:
        return jsonify({
            "success": True,
            "playlist": pls[0]["title"],
            "rating_key": pls[0].get("rating_key"),
            "track_count": pls[0]["track_count"],
            "album_count": pls[0]["album_count"],
            "llm_status": gemini_curator.get_llm_status(),
        })
    return jsonify({"success": False, "message": "Channel not found"}), 404


@app.route("/api/vault/large-playlist", methods=["POST"])
def api_vault_large_playlist():
    result = generate_large_vault_playlist(rotate=True)
    if result:
        return jsonify({
            "success": True,
            "playlist": result["title"],
            "rating_key": result.get("rating_key"),
            "track_count": result["track_count"],
            "album_count": result["album_count"],
        })
    return jsonify({"success": False, "message": "Failed to create large vault playlist"}), 500


@app.route("/api/vault")
def api_vault():
    vault = get_unplayed_vault_albums(limit=600)
    return jsonify(vault)


@app.route("/api/add-to-lidarr", methods=["POST"])
def api_add_to_lidarr():
    data = request.json or {}
    artist = data.get("artist")
    album = data.get("album")
    if not artist:
        return jsonify({"success": False, "message": "Missing artist name"}), 400

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


@app.route("/api/sync-lidarr", methods=["POST"])
def api_sync_lidarr():
    recs = discovery_engine.get_all_recommendations()
    conn = discovery_engine.get_db()
    updated = 0
    for r in recs:
        status = discovery_engine.check_lidarr_status_for_album(r["artist"], r["album"])
        if status != r["lidarr_status"]:
            conn.execute("UPDATE recommendations SET lidarr_status=? WHERE id=?", (status, r["id"]))
            updated += 1
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Updated Lidarr status for {updated} albums"})


@app.route("/api/vault/add-track", methods=["POST"])
def api_vault_add():
    data = request.json or {}
    rk = data.get("rating_key")
    if not rk:
        return jsonify({"success": False, "message": "No rating key"}), 400
    playlist_name = "🎯 Vault Excavator: Current Mood"
    result = append_to_plex_playlist(playlist_name, str(rk))
    if result:
        return jsonify({"success": True, "leafCount": result["leafCount"]})
    return jsonify({"success": False, "message": "Could not add album to playlist"}), 500


def _start_background_scheduler():
    """
    Start a single background scheduler thread (guarded by an advisory file lock
    in DATA_DIR so multiple Gunicorn workers never duplicate scheduled tasks).
    Handles:
      - Startup initialization (seed recommendations + initial library track index if empty)
      - Nightly library track re-indexing (03:30 daily)
      - Weekly channel, vault & discovery refresh (Monday 09:00)
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
                reindex_library_tracks()
        except Exception as e:
            print(f"[Scheduler] Startup check error: {e}")

        last_reindex_date = None
        last_weekly_date = None

        while True:
            time.sleep(30)
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # Nightly library re-index at 03:30
            if now.hour == 3 and now.minute == 30 and last_reindex_date != today_str:
                last_reindex_date = today_str
                try:
                    print(f"[{now}] [Scheduler] Running nightly Plex library track re-index...")
                    reindex_library_tracks()
                except Exception as e:
                    print(f"[Scheduler] Nightly re-index error: {e}")

            # Weekly refresh on Monday (weekday == 0) at 09:00
            if now.weekday() == 0 and now.hour == 9 and now.minute == 0 and last_weekly_date != today_str:
                last_weekly_date = today_str
                try:
                    print(f"[{now}] [Scheduler] Running automated Monday weekly refresh & discovery...")
                    weekly_refresh.run_refresh(include_discovery=True)
                except Exception as e:
                    print(f"[Scheduler] Weekly refresh error: {e}")

    t = threading.Thread(target=_scheduler_loop, name="crate-scheduler", daemon=True)
    t.start()
    print("✓ Background scheduler started (Nightly 03:30 re-index | Monday 09:00 refresh + discovery).")


_start_background_scheduler()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", CONFIG.get("PORT", 5050)))
    print(f"Starting Crate Excavator Unified Server on http://{host}:{port} ...")
    app.run(host=host, port=port, debug=False)
