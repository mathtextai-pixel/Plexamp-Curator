#!/usr/bin/env python3
"""
Weekly Automatic Crate Playlist & Discovery Refresh Script.
Re-indexes the Plex music library, refreshes all 5 Channel Discovery Playlists
and the Large Vault Playlist in Plex (in-place, preserving ratingKeys), and
crawls for new stepping-stone album recommendations.

Each step runs independently: a failure in one is logged and the rest still run.
A summary is appended to DATA_DIR/weekly_refresh.log.
"""

import datetime
import logging
import os
import sys
from typing import Callable, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from config import CONFIG
from discovery_engine import run_weekly_discovery_crawl
from locks import REFRESH_JOB_LOCK, REINDEX_JOB_LOCK, LockBusy, file_lock
from music_excavator import (
    generate_channel_playlists,
    generate_large_vault_playlist,
    reindex_library_tracks,
)

log = logging.getLogger(__name__)

LOG_FILE = os.path.join(CONFIG["DATA_DIR"], "weekly_refresh.log")

# How long a scheduled refresh waits for a manual refresh that is already running.
REFRESH_LOCK_WAIT_SECONDS = 30 * 60
REINDEX_LOCK_WAIT_SECONDS = 15 * 60


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log_file(lines: List[str]) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.writelines(lines)


def run_refresh(include_discovery: bool = True) -> bool:
    """
    Run the full refresh while holding the shared refresh lock (waits for any manual run).
    Returns True only if every step succeeded.
    """
    try:
        with file_lock(REFRESH_JOB_LOCK, timeout=REFRESH_LOCK_WAIT_SECONDS):
            return _run_refresh_locked(include_discovery)
    except LockBusy:
        msg = (
            f"[{_timestamp()}] ❌ Skipped weekly refresh: another playlist refresh held the lock "
            f"for {REFRESH_LOCK_WAIT_SECONDS // 60} min.\n\n"
        )
        log.error(msg.strip())
        _append_log_file([msg])
        return False


# --- steps: each returns a one-line summary, or raises -----------------------

def _step_reindex() -> str:
    with file_lock(REINDEX_JOB_LOCK, timeout=REINDEX_LOCK_WAIT_SECONDS):
        indexed = reindex_library_tracks()
    if indexed <= 0:
        raise RuntimeError("no tracks indexed (is Plex reachable?)")
    return f"Library re-index: {indexed:,} tracks synced from Plex"


def _step_channels() -> str:
    results = generate_channel_playlists(rotate=True, use_gemini_flow=True)
    if not results:
        raise RuntimeError("no channel playlists were updated")
    lines = [f"{r['title']} -> {r['track_count']} tracks ({r['album_count']} albums)" for r in results]
    return f"Channels ({len(results)} updated): " + "; ".join(lines)


def _step_vault() -> str:
    res = generate_large_vault_playlist(rotate=True)
    if not res:
        raise RuntimeError("vault playlist was not updated")
    return f"Vault master: {res['title']} -> {res['track_count']} tracks ({res['album_count']} albums)"


def _step_discovery() -> str:
    added = run_weekly_discovery_crawl()
    return f"Discovery crawl: {len(added)} new albums added to Crate"


def _run_refresh_locked(include_discovery: bool) -> bool:
    steps: List[Tuple[str, Callable[[], str]]] = [
        ("Library re-index", _step_reindex),
        ("Channel playlists", _step_channels),
        ("Vault playlist", _step_vault),
    ]
    if include_discovery:
        steps.append(("Discovery crawl", _step_discovery))

    header = f"[{_timestamp()}] === STARTING WEEKLY PLAYLIST REFRESH ===\n"
    log.info(header.strip())
    lines = [header]
    failed = []

    for name, step in steps:
        try:
            summary = step()
            lines.append(f"  ✓ {summary}\n")
            log.info(summary)
        except Exception as e:
            failed.append(name)
            lines.append(f"  ❌ {name} failed: {e}\n")
            log.exception(f"{name} failed: {e}")

    if failed:
        footer = f"[{_timestamp()}] === REFRESH FINISHED WITH ERRORS ({len(failed)}/{len(steps)} failed: {', '.join(failed)}) ===\n\n"
        log.error(footer.strip())
    else:
        footer = f"[{_timestamp()}] === REFRESH COMPLETE: all {len(steps)} steps succeeded ===\n\n"
        log.info(footer.strip())
    lines.append(footer)
    _append_log_file(lines)
    return not failed


if __name__ == "__main__":
    from logging_setup import configure_logging

    configure_logging()
    sys.exit(0 if run_refresh() else 1)
