#!/usr/bin/env python3
"""
Home Stations: 5 dedicated daily-routine playlists for home automation.
Completely separate from the 5 taste Crate channels.

Playlists:
  1. 🏡 Home: Wake Up (Hyped & energized, upbeat yet melodic, not too loud)
  2. 🏡 Home: Morning Coffee (Gentle acoustic picking, warm mellow soul)
  3. 🏡 Home: Coming Home (Productive & energetic pre-dinner kitchen transition)
  4. 🏡 Home: Family Dinner (Warm, calm, happy crowd-pleasers for conversation)
  5. 🏡 Home: Bedtime (Hushed, calm, slow tempo wind-down lullabies)

Each playlist updates in-place via create_or_replace_plex_playlist, preserving
its ratingKey and Plexamp/Sonos/Home Assistant integrations forever.
"""

import json
import logging
import random
import re
import sqlite3
from typing import Callable, Dict, List, Optional, Set, Tuple

import db
from config import CONFIG, get_int
from http_client import get_json
from music_excavator import (
    create_or_replace_plex_playlist,
    get_album_tracks,
    get_recently_played_tracks,
)
from textmatch import contains_any_phrase

log = logging.getLogger(__name__)

HOME_STATION_DEFINITIONS: Dict[str, dict] = {
    "wake_up": {
        "title": "🏡 Home: Wake Up",
        "name": "Wake Up Momentum",
        "icon": "☀️",
        "target_count": 35,
        "description": (
            "Hyped, energized, and propulsive for the day, but melodic and not jarring or abrasive. "
            "Anthemic indie-punk, cathartic heartland rock, driving acoustic strums, and buoyant hooks."
        ),
        "primary_artists": [
            "The Menzingers", "Talking Heads", "Frank Turner", "Jason Isbell",
            "The Gaslight Anthem", "Sam Fender", "The Front Bottoms", "Amyl and the Sniffers",
            "Alkaline Trio", "Turnstile", "American Football"
        ],
        "keywords": [
            "menzingers", "talking heads", "frank turner", "isbell", "gaslight",
            "fender", "turnstile", "alkaline", "front bottoms", "amyl", "american football"
        ],
    },
    "morning_coffee": {
        "title": "🏡 Home: Morning Coffee",
        "name": "Morning Coffee",
        "icon": "☕",
        "target_count": 35,
        "description": (
            "Gentle acoustic warmth, laid-back mountain picking, soft jazz/folk piano, and warm analog soul. "
            "Unrushed, peaceful, and sun-drenched to ease into the morning."
        ),
        "primary_artists": [
            "Norah Jones", "Caamp", "Gillian Welch", "Gillian Welch and David Rawlings",
            "Khruangbin & Leon Bridges", "Gregory Alan Isakov", "Ray LaMontagne",
            "John Prine", "Colter Wall", "Tyler Childers", "Tracy Chapman", "Midlake"
        ],
        "keywords": [
            "norah jones", "caamp", "gillian welch", "khruangbin", "leon bridges",
            "isakov", "lamontagne", "prine", "colter wall", "childers", "chapman", "midlake"
        ],
    },
    "coming_home": {
        "title": "🏡 Home: Coming Home",
        "name": "Coming Home Transition",
        "icon": "🏠",
        "target_count": 35,
        "description": (
            "Productive, upbeat, and motivating for the kitchen, tidying up, and shaking off the workday. "
            "Energetic but not overwhelming: jangle pop, bright rhythm sections, breezy hooks, and indie soul."
        ),
        "primary_artists": [
            "Flipturn", "Peach Pit", "The Head And The Heart", "Vance Joy", "Dayglow",
            "Lake Street Dive", "Hippo Campus", "Faye Webster", "The 1975", "Alvvays",
            "The Ballroom Thieves", "Bleachers", "Snail Mail"
        ],
        "keywords": [
            "flipturn", "peach pit", "head and the heart", "vance joy", "dayglow",
            "lake street", "hippo campus", "faye webster", "the 1975", "alvvays",
            "ballroom thieves", "bleachers", "snail mail"
        ],
    },
    "family_dinner": {
        "title": "🏡 Home: Family Dinner",
        "name": "Family Dinner",
        "icon": "🍽️",
        "target_count": 35,
        "description": (
            "Warm, happy, uplifting, and calm crowd-pleaser mix for the whole family around the table. "
            "Melodic sing-along folk-pop, acoustic favorites, feel-good classics, and friendly modern pop."
        ),
        "primary_artists": [
            "The Avett Brothers", "The Lumineers", "Harry Styles", "The Beatles",
            "Olivia Rodrigo", "Ashe", "Maggie Rogers", "Plain White T's", "Leon Bridges",
            "Mumford & Sons", "Lord Huron", "Hozier", "Vance Joy", "Caamp"
        ],
        "keywords": [
            "avett", "lumineers", "harry styles", "beatles", "olivia rodrigo",
            "ashe", "maggie rogers", "plain white", "leon bridges", "mumford",
            "lord huron", "hozier", "vance joy", "caamp"
        ],
    },
    "bedtime": {
        "title": "🏡 Home: Bedtime",
        "name": "Bedtime Calm",
        "icon": "🌙",
        "target_count": 35,
        "description": (
            "Completely calm, slow-tempo, gentle wind-down soundscape for sleep and getting in a quiet headspace. "
            "Hushed vocals, slowcore lullabies, delicate piano, and zero jarring volume or tempo spikes."
        ),
        "primary_artists": [
            "Low", "Bon Iver", "Phoebe Bridges", "Fiona Apple", "Norah Jones",
            "Spain", "Red House Painters", "Cat Power", "The Blue Moods of Spain",
            "Gregory Alan Isakov", "The xx", "Men I Trust"
        ],
        "keywords": [
            "low", "bon iver", "phoebe bridgers", "fiona apple", "norah jones",
            "spain", "red house painters", "cat power", "isakov", "the xx", "men i trust"
        ],
    },
}


def _gather_candidate_tracks_for_station(
    station_def: dict,
    recent_keys: Set[str],
    recent_pairs: Set[Tuple[str, str]],
    sample_size: int = 70,
) -> List[dict]:
    """
    Gather a diverse, randomized pool of candidate tracks matching the station's
    primary artists and keywords, filtering out tracks recently played in Tautulli.
    """
    with db.session() as conn:
        cur = conn.cursor()

        matched_keys: Set[str] = set()
        candidate_tracks: List[dict] = []

        # 1. First priority: Exact or direct matches to primary artists
        for artist_name in station_def["primary_artists"]:
            cur.execute(
                """
                SELECT rating_key, artist, album, title, year
                FROM library_tracks
                WHERE lower(artist) = lower(?)
                ORDER BY RANDOM()
                LIMIT 10
                """,
                (artist_name,),
            )
            for row in cur.fetchall():
                rk = str(row["rating_key"])
                art_lower = (row["artist"] or "").strip().lower()
                title_lower = (row["title"] or "").strip().lower()
                if rk in recent_keys or (art_lower, title_lower) in recent_pairs:
                    continue
                if rk not in matched_keys:
                    matched_keys.add(rk)
                    candidate_tracks.append({
                        "ratingKey": rk,
                        "artist": row["artist"],
                        "album": row["album"],
                        "title": row["title"],
                        "year": row["year"],
                    })

        # 2. Second priority: Keyword / substring artist matches
        for kw in station_def["keywords"]:
            cur.execute(
                """
                SELECT rating_key, artist, album, title, year
                FROM library_tracks
                WHERE lower(artist) LIKE ?
                ORDER BY RANDOM()
                LIMIT 6
                """,
                (f"%{kw}%",),
            )
            for row in cur.fetchall():
                rk = str(row["rating_key"])
                art_lower = (row["artist"] or "").strip().lower()
                title_lower = (row["title"] or "").strip().lower()
                if rk in recent_keys or (art_lower, title_lower) in recent_pairs:
                    continue
                if rk not in matched_keys:
                    matched_keys.add(rk)
                    candidate_tracks.append({
                        "ratingKey": rk,
                        "artist": row["artist"],
                        "album": row["album"],
                        "title": row["title"],
                        "year": row["year"],
                    })

    # Shuffle pool for variety
    random.shuffle(candidate_tracks)
    return candidate_tracks[:sample_size]


def generate_home_station_playlist(
    station_key: str,
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """
    Generate or refresh a single home station playlist in Plex in-place.
    Uses LLM curation to sequence the dynamic arc, with balanced round-robin fallback.
    """
    report = progress or (lambda _msg: None)
    station_def = HOME_STATION_DEFINITIONS.get(station_key)
    if not station_def:
        log.warning(f"Unknown home station key: {station_key}")
        return None

    title = station_def["title"]
    target_count = station_def.get("target_count", 35)

    report(f"Gathering candidates for {title}…")
    recent_keys, recent_pairs = get_recently_played_tracks()
    candidates = _gather_candidate_tracks_for_station(station_def, recent_keys, recent_pairs)

    if not candidates:
        # If freshness filter was too strict, loosen it without Tautulli filter
        candidates = _gather_candidate_tracks_for_station(station_def, set(), set())
    if not candidates:
        log.error(f"No library tracks matched station {station_key}")
        return None

    report(f"Curating sequence for {title} ({len(candidates)} candidates)…")
    import gemini_curator
    sequenced = gemini_curator.sequence_flowing_playlist(
        channel_name=station_def["name"],
        channel_desc=station_def["description"],
        candidate_tracks=candidates,
        target_count=target_count,
    )

    track_rating_keys = [str(s["ratingKey"]) for s in sequenced]
    if not track_rating_keys:
        track_rating_keys = [str(c["ratingKey"]) for c in candidates[:target_count]]

    report(f"Updating Plex playlist '{title}' in-place…")
    pl = create_or_replace_plex_playlist(title, track_rating_keys)
    if pl:
        log.info(f"✓ Updated Home Station '{title}' (ratingKey={pl['ratingKey']}): {pl['leafCount']} tracks")
        return {
            "station_key": station_key,
            "title": pl["title"],
            "rating_key": pl["ratingKey"],
            "track_count": pl["leafCount"],
            "icon": station_def["icon"],
        }
    return None


def generate_all_home_stations(
    progress: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Refresh all 5 Home Station playlists in Plex sequentially."""
    report = progress or (lambda _msg: None)
    results = []
    total = len(HOME_STATION_DEFINITIONS)

    for i, (key, sdef) in enumerate(HOME_STATION_DEFINITIONS.items(), 1):
        report(f"Curating {sdef['icon']} {sdef['name']} ({i}/{total})…")
        res = generate_home_station_playlist(key, progress=report)
        if res:
            results.append(res)

    return results
