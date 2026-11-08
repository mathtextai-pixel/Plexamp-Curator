#!/usr/bin/env python3
"""
Weekly Automatic Crate Playlist & Discovery Refresh Script.
Re-indexes the Plex music library, refreshes all 5 Channel Discovery Playlists
and the Large Vault Playlist in Plex (in-place, preserving ratingKeys), and
crawls for new stepping-stone album recommendations.
"""

import datetime
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from config import CONFIG
from discovery_engine import run_weekly_discovery_crawl
from music_excavator import (
    generate_channel_playlists,
    generate_large_vault_playlist,
    reindex_library_tracks,
)

LOG_FILE = os.path.join(CONFIG["DATA_DIR"], "weekly_refresh.log")


def run_refresh(include_discovery: bool = True):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{now}] === STARTING AUTOMATED WEEKLY PLAYLIST & DISCOVERY REFRESH ===\n"
    print(header, end="")

    lines = [header]
    try:
        # 0. Re-index library_tracks from Plex
        indexed = reindex_library_tracks()
        msg0 = f"  ✓ Library Re-index: {indexed} tracks synced from Plex\n"
        print(msg0, end="")
        lines.append(msg0)

        # 1. Refresh all 5 Taste Channels (with rotation & LLM mixtape flow)
        channel_results = generate_channel_playlists(rotate=True, use_gemini_flow=True)
        for res in channel_results:
            msg = f"  ✓ Channel: {res['title']} -> {res['track_count']} tracks ({res['album_count']} albums)\n"
            print(msg, end="")
            lines.append(msg)

        # 2. Refresh the Large Vault Playlist
        vault_res = generate_large_vault_playlist(rotate=True)
        if vault_res:
            msg = f"  ✓ Vault Master: {vault_res['title']} -> {vault_res['track_count']} tracks ({vault_res['album_count']} albums)\n"
            print(msg, end="")
            lines.append(msg)

        # 3. Crawl for new stepping-stone recommendations
        if include_discovery:
            new_recs = run_weekly_discovery_crawl()
            msg_disc = f"  ✓ Discovery Crawl: {len(new_recs)} new albums added to Crate\n"
            print(msg_disc, end="")
            lines.append(msg_disc)

        summary = (
            f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"=== REFRESH COMPLETE: {len(channel_results)} channels + Large Vault updated! ===\n\n"
        )
        print(summary, end="")
        lines.append(summary)

    except Exception as e:
        err = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ ERROR during weekly refresh: {e}\n\n"
        print(err, file=sys.stderr, end="")
        lines.append(err)

    with open(LOG_FILE, "a") as f:
        f.writelines(lines)


if __name__ == "__main__":
    run_refresh()
