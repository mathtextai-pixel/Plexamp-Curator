#!/usr/bin/env python3
"""
Music Excavator: Mine your unplayed Plex vault and discover new music
powered by Tautulli play history and Plex Media Server.
"""

import json
import os
import random
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from config import CONFIG, get_int

DB_PATH = os.path.join(CONFIG["DATA_DIR"], "recommendations.db")
_CACHED_MACHINE_ID: Optional[str] = None


def _plex_url() -> str:
    return CONFIG["PLEX_URL"].rstrip("/")


def _tautulli_url() -> str:
    return CONFIG["TAUTULLI_URL"].rstrip("/")


def _music_section_id() -> str:
    return str(CONFIG.get("MUSIC_SECTION_ID", "2"))


def get_plex_headers() -> Dict[str, str]:
    return {
        "X-Plex-Token": CONFIG["PLEX_TOKEN"],
        "Accept": "application/json",
    }


def get_tautulli_recent_plays(limit: int = 500) -> List[dict]:
    """Fetch recent track plays from Tautulli."""
    url = (
        f"{_tautulli_url()}/api/v2?"
        f"apikey={CONFIG['TAUTULLI_KEY']}&cmd=get_history&media_type=track&length={limit}"
    )
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data.get("response", {}).get("data", {}).get("data", [])
    except Exception as e:
        print(f"Warning fetching Tautulli history: {e}")
        return []


def get_recently_played_tracks(days: Optional[int] = None) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """
    Return (rating_keys_set, (artist_lower, title_lower)_set) for tracks played
    within the last `days` days according to Tautulli.
    """
    if days is None:
        days = get_int("RECENTLY_PLAYED_DAYS")
    if days <= 0:
        return set(), set()

    cutoff = time.time() - (days * 86400)
    plays = get_tautulli_recent_plays(limit=1000)
    recent_keys: Set[str] = set()
    recent_pairs: Set[Tuple[str, str]] = set()

    for p in plays:
        ts = p.get("date") or p.get("stopped") or 0
        try:
            if float(ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue

        rk = p.get("rating_key")
        if rk:
            recent_keys.add(str(rk))
        art = (p.get("grandparent_title") or "").strip().lower()
        trk = (p.get("title") or "").strip().lower()
        if art and trk:
            recent_pairs.add((art, trk))

    return recent_keys, recent_pairs


def analyze_recent_habits(limit: int = 500) -> dict:
    """Analyze top artists, albums, and tracks from recent history."""
    plays = get_tautulli_recent_plays(limit=limit)
    artists: Counter = Counter()
    albums: Counter = Counter()
    tracks: Counter = Counter()

    for p in plays:
        art = p.get("grandparent_title")
        alb = p.get("parent_title")
        trk = p.get("title")
        if art:
            artists[art] += 1
        if art and alb:
            albums[f"{art} — {alb}"] += 1
        if art and trk:
            tracks[f"{art} — {trk}"] += 1

    return {
        "total_plays_analyzed": len(plays),
        "top_artists": artists.most_common(15),
        "top_albums": albums.most_common(15),
        "top_tracks": tracks.most_common(10),
    }


def get_unplayed_vault_albums(limit: int = 1000) -> List[dict]:
    """Fetch unplayed or zero-play albums from Plex Music library."""
    url = (
        f"{_plex_url()}/library/sections/{_music_section_id()}/all?"
        f"type=9&unwatched=1&X-Plex-Container-Start=0&X-Plex-Container-Size={limit}"
    )
    req = urllib.request.Request(url, headers=get_plex_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = data.get("MediaContainer", {}).get("Metadata", [])
    except Exception as e:
        print(f"Error fetching unplayed vault albums: {e}")
        return []

    results = []
    for it in items:
        results.append({
            "rating_key": it.get("ratingKey"),
            "artist": it.get("parentTitle"),
            "album": it.get("title"),
            "year": it.get("year"),
            "genres": [g.get("tag") for g in it.get("Genre", [])],
            "track_count": it.get("leafCount"),
        })
    return results


def get_plex_machine_id() -> Optional[str]:
    """Retrieve and cache machine identifier from Plex Media Server."""
    global _CACHED_MACHINE_ID
    if _CACHED_MACHINE_ID:
        return _CACHED_MACHINE_ID
    id_url = f"{_plex_url()}/identity"
    req_id = urllib.request.Request(id_url, headers=get_plex_headers())
    try:
        with urllib.request.urlopen(req_id, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            _CACHED_MACHINE_ID = data.get("MediaContainer", {}).get("machineIdentifier")
            return _CACHED_MACHINE_ID
    except Exception as e:
        print(f"Error fetching Plex machine ID: {e}")
        return None


def find_plex_playlist_by_title(title: str) -> Optional[dict]:
    """Find an existing playlist in Plex matching title."""
    url = f"{_plex_url()}/playlists"
    req = urllib.request.Request(url, headers=get_plex_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for pl in data.get("MediaContainer", {}).get("Metadata", []):
                if pl.get("title", "").strip().lower() == title.strip().lower():
                    return pl
    except Exception as e:
        print(f"Error searching playlists: {e}")
    return None


def create_or_replace_plex_playlist(title: str, rating_keys: List[str]) -> Optional[dict]:
    """
    Create or update a playlist in Plex containing albums/tracks by rating key.
    Updates existing playlists IN-PLACE (clearing items then adding new ones)
    so the playlist ratingKey, Plexamp pins, and downloads are preserved.
    """
    clean_keys = [str(k) for k in rating_keys if k]
    if not clean_keys:
        return None

    machine_id = get_plex_machine_id()
    if not machine_id:
        return None

    items_uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{','.join(clean_keys)}"
    existing = find_plex_playlist_by_title(title)

    if existing and existing.get("ratingKey"):
        pl_id = existing["ratingKey"]
        try:
            # 1. Clear existing items without deleting the playlist container
            clear_url = f"{_plex_url()}/playlists/{pl_id}/items"
            req_clear = urllib.request.Request(clear_url, headers=get_plex_headers(), method="DELETE")
            with urllib.request.urlopen(req_clear, timeout=15):
                pass

            # 2. Repopulate with new items in-place
            put_url = f"{_plex_url()}/playlists/{pl_id}/items?uri={urllib.parse.quote(items_uri, safe='')}"
            req_put = urllib.request.Request(put_url, headers=get_plex_headers(), method="PUT")
            with urllib.request.urlopen(req_put, timeout=15):
                pass

            # 3. Fetch updated metadata
            info_url = f"{_plex_url()}/playlists/{pl_id}"
            req_info = urllib.request.Request(info_url, headers=get_plex_headers())
            with urllib.request.urlopen(req_info, timeout=10) as rinfo:
                info_data = json.loads(rinfo.read().decode("utf-8", errors="replace"))
                pl = info_data.get("MediaContainer", {}).get("Metadata", [{}])[0]
                return {
                    "title": pl.get("title", title),
                    "ratingKey": str(pl.get("ratingKey", pl_id)),
                    "leafCount": pl.get("leafCount", len(clean_keys)),
                }
        except Exception as e:
            print(f"Warning updating playlist '{title}' in-place ({e}); falling back to recreate.")

    # Create fresh playlist if none existed (or in-place failed)
    params = urllib.parse.urlencode({
        "type": "audio",
        "title": title,
        "smart": 0,
        "uri": items_uri,
    })
    post_url = f"{_plex_url()}/playlists?{params}"
    req_post = urllib.request.Request(post_url, headers=get_plex_headers(), method="POST")
    try:
        with urllib.request.urlopen(req_post, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            pl = data.get("MediaContainer", {}).get("Metadata", [{}])[0]
            return {
                "title": pl.get("title"),
                "ratingKey": str(pl.get("ratingKey")),
                "leafCount": pl.get("leafCount"),
            }
    except Exception as e:
        print(f"Error creating playlist: {e}")
        return None


def append_to_plex_playlist(title: str, rating_key: str) -> Optional[dict]:
    """Append a single album or track to an existing playlist, or create it if not found."""
    existing = find_plex_playlist_by_title(title)
    if not existing:
        return create_or_replace_plex_playlist(title, [rating_key])

    pl_id = existing["ratingKey"]
    machine_id = get_plex_machine_id()
    if not machine_id:
        return None
    item_uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{rating_key}"

    put_url = f"{_plex_url()}/playlists/{pl_id}/items?uri={urllib.parse.quote(item_uri, safe='')}"
    req_put = urllib.request.Request(put_url, headers=get_plex_headers(), method="PUT")
    try:
        with urllib.request.urlopen(req_put, timeout=10):
            info_url = f"{_plex_url()}/playlists/{pl_id}"
            req_info = urllib.request.Request(info_url, headers=get_plex_headers())
            with urllib.request.urlopen(req_info, timeout=10) as rinfo:
                info_data = json.loads(rinfo.read().decode("utf-8", errors="replace"))
                pl = info_data.get("MediaContainer", {}).get("Metadata", [{}])[0]
                return {
                    "title": pl.get("title"),
                    "ratingKey": str(pl.get("ratingKey")),
                    "leafCount": pl.get("leafCount"),
                }
    except Exception as e:
        print(f"Error appending to playlist: {e}")
        return None


# Keep alias for backward compatibility
create_plex_playlist = create_or_replace_plex_playlist


def reindex_library_tracks() -> int:
    """
    Fetch all audio tracks (type=10) from the Plex music section and sync them
    into the SQLite `library_tracks` table in `data/recommendations.db`.
    Returns the total number of indexed tracks.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    url = f"{_plex_url()}/library/sections/{_music_section_id()}/all?type=10"
    req = urllib.request.Request(url, headers=get_plex_headers())
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            items = data.get("MediaContainer", {}).get("Metadata", [])
    except Exception as e:
        print(f"Error fetching library tracks from Plex for re-index: {e}")
        return 0

    if not items:
        return 0

    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS library_tracks (
            rating_key TEXT PRIMARY KEY,
            artist TEXT,
            album TEXT,
            title TEXT,
            year INTEGER,
            genre TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_artist ON library_tracks(artist)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_title ON library_tracks(title)")

    rows = []
    seen_keys = set()
    for it in items:
        rk = str(it.get("ratingKey") or "").strip()
        if not rk:
            continue
        seen_keys.add(rk)
        artist = (it.get("grandparentTitle") or it.get("originalTitle") or "").strip()
        album = (it.get("parentTitle") or "").strip()
        title = (it.get("title") or "").strip()
        year = it.get("year") or it.get("parentYear")
        genres = ", ".join(g.get("tag", "") for g in it.get("Genre", []) if g.get("tag"))
        rows.append((rk, artist, album, title, year, genres))

    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO library_tracks (rating_key, artist, album, title, year, genre)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # Remove tracks that no longer exist in Plex
        cur = conn.execute("SELECT rating_key FROM library_tracks")
        existing_keys = {r[0] for r in cur.fetchall()}
        stale_keys = existing_keys - seen_keys
        if stale_keys:
            conn.executemany(
                "DELETE FROM library_tracks WHERE rating_key = ?",
                [(k,) for k in stale_keys],
            )

    conn.close()
    print(f"  ✓ Indexed {len(rows)} tracks from Plex into library_tracks (removed {len(stale_keys)} stale).")
    return len(rows)


def get_library_track_count() -> int:
    """Return total number of indexed tracks in library_tracks."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        cur = conn.execute("SELECT COUNT(*) FROM library_tracks")
        count = cur.fetchone()[0]
        conn.close()
        return int(count or 0)
    except Exception:
        return 0


CHANNEL_DEFINITIONS = {
    "alt_country": {
        "title": "🎯 Crate: Alt-Country & Dusty Strings",
        "name": "Alt & Prime Country",
        "icon": "🌾",
        "description": "Appalachian picking, outlaw honky-tonk, barstool poetry, and rustic acoustic soul.",
        "targets": [
            ("Tyler Childers", "Country Squire"),
            ("The Red Clay Strays", "Live AF Session"),
            ("Colter Wall", "Songs of the Plains"),
            ("Colter Wall", "Colter Wall"),
            ("John Prine", "John Prine"),
            ("Drive-By Truckers", "Decoration Day"),
            ("Dwight Yoakam", "This Time"),
            ("Gillian Welch and David Rawlings", "Woodland"),
            ("Sturgill Simpson", "The Ballad of Dood & Juanita"),
            ("Lydia Loveless", "Indestructible Machine"),
            ("Midlake", "The Trials of Van Occupanther"),
            ("Ray LaMontagne", "Supernova"),
        ],
        "keywords": [
            "childers", "colter wall", "prine", "truckers", "red clay", "yoakam",
            "welch", "sturgill", "loveless", "midlake", "lamontagne", "calexico", "isbell",
        ],
    },
    "pop_punk": {
        "title": "🎯 Crate: Pop-Punk & Acoustic Fire",
        "name": "Pop & Acoustic Punk",
        "icon": "🎸",
        "description": "Anthemic singalongs, roaring guitars, frantic acoustic strums, and bruised-heart catharsis.",
        "targets": [
            ("The Menzingers", "Chamberlain Waits"),
            ("Frank Turner", "Positive Songs for Negative People"),
            ("Alkaline Trio", "From Here To Infirmary"),
            ("American Football", "American Football"),
            ("Amyl and the Sniffers", "Comfort to Me"),
            ("Turnstile", "Glow On"),
            ("Frank Turner", "Undefeated"),
        ],
        "keywords": [
            "menzingers", "frank turner", "alkaline", "american football", "amyl",
            "turnstile", "alexsucks", "gaslight", "front bottoms", "pup", "fugazi", "hot water",
        ],
    },
    "indie_pop": {
        "title": "🎯 Crate: Indie Pop & Sun-Drenched",
        "name": "Indie Pop & Shimmer",
        "icon": "✨",
        "description": "Sunny guitar jangle, shimmering dream-pop melodies, and breezy hooks.",
        "targets": [
            ("Peach Pit", "You and Your Friends"),
            ("Hippo Campus", "Bambi"),
            ("Faye Webster", "Underdressed at the Symphony"),
            ("Dayglow", "Close to You"),
            ("Beirut", "The Rip Tide"),
            ("Snail Mail", "Ricochet"),
            ("Mallrat", "Butterfly Blue"),
            ("The 1975", "Being Funny in a Foreign Language"),
        ],
        "keywords": [
            "peach pit", "hippo campus", "faye webster", "dayglow", "beirut", "snail mail",
            "mallrat", "wild party", "the 1975", "alvvays", "flipturn", "vacations", "men i trust", "soccer mommy",
        ],
    },
    "indie_soul": {
        "title": "🎯 Crate: Indie Soul & Late-Night Grooves",
        "name": "Indie Soul & Grooves",
        "icon": "🎷",
        "description": "Warm analog tape, pocket basslines, falsetto harmonies, and late-night funk.",
        "targets": [
            ("Khruangbin & Leon Bridges", "Texas Sun"),
            ("Leon Bridges", "Leon"),
            ("The Internet", "Ego Death"),
            ("Khruangbin", "The Universe Smiles Upon You"),
            ("Blood Orange", "Cupid Deluxe"),
            ("Tracy Chapman", "Where You Live"),
        ],
        "keywords": [
            "khruangbin", "leon bridges", "internet", "blood orange", "tracy chapman",
            "durand jones", "sacred souls", "black pumas", "sault", "say she she", "norah jones",
        ],
    },
    "canon": {
        "title": "🎯 Crate: The Canon (Classics & New Wave)",
        "name": "The Canon Classics",
        "icon": "⚡",
        "description": "The quintessential post-punk, new wave, and alternative rock masterworks every collection needs.",
        "targets": [
            ("The Cure", "Faith"),
            ("New Order", "Low-Life"),
            ("The Smiths", "Hatful of Hollow"),
            ("The Replacements", "Pleased to Meet Me"),
            ("R.E.M.", "Document"),
            ("Joy Division", "The Best of Joy Division"),
            ("David Bowie", "The Man Who Sold The World"),
            ("Violent Femmes", "Add It Up"),
            ("Pavement", "Terror Twilight"),
        ],
        "keywords": [
            "the cure", "new order", "the smiths", "the replacements", "r.e.m.", "joy division",
            "david bowie", "violent femmes", "pavement", "pixies", "talking heads", "clash",
            "television", "wire", "interpol", "radiohead",
        ],
    },
}


def get_album_tracks(rating_key: str) -> List[dict]:
    """Fetch individual tracks for an album rating key."""
    url = f"{_plex_url()}/library/metadata/{rating_key}/children"
    req = urllib.request.Request(url, headers=get_plex_headers())
    tracks = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for t in data.get("MediaContainer", {}).get("Metadata", []):
                tracks.append({
                    "ratingKey": str(t.get("ratingKey")),
                    "title": t.get("title"),
                    "index": t.get("index"),
                    "duration": t.get("duration", 0),
                })
    except Exception as e:
        print(f"Error fetching tracks for album {rating_key}: {e}")
    return tracks


def generate_channel_playlists(
    channel_key: Optional[str] = None,
    rotate: bool = False,
    use_gemini_flow: bool = True,
) -> List[dict]:
    """
    Generate curated, flowing playlists for all channels or a single specified channel.
    Samples across whole albums (excluding tracks played in the last RECENTLY_PLAYED_DAYS)
    and asks the configured LLM to sequence a cohesive ~20-track mixtape.
    """
    import gemini_curator

    vault = get_unplayed_vault_albums(limit=1000)
    recent_keys, recent_pairs = get_recently_played_tracks()

    selected_channels = (
        {channel_key: CHANNEL_DEFINITIONS[channel_key]}
        if channel_key and channel_key in CHANNEL_DEFINITIONS
        else CHANNEL_DEFINITIONS
    )

    results = []
    for key, cdef in selected_channels.items():
        title = cdef["title"]
        matched_albums = []

        # 1. Match specific target masterpieces
        targets = list(cdef["targets"])
        if rotate:
            random.shuffle(targets)
        for art, alb in targets:
            for a in vault:
                if art.lower() in (a.get("artist") or "").lower() and alb.lower() in (a.get("album") or "").lower():
                    if a not in matched_albums:
                        matched_albums.append(a)
                        break

        # 2. Gather more albums by keywords
        keyword_matches = []
        for a in vault:
            art = (a.get("artist") or "").lower()
            if any(k in art for k in cdef["keywords"]):
                if a not in matched_albums and a not in keyword_matches:
                    keyword_matches.append(a)

        if rotate:
            random.shuffle(keyword_matches)

        for a in keyword_matches:
            if len(matched_albums) >= 10:
                break
            matched_albums.append(a)

        if not matched_albums:
            continue

        # 3. Collect candidate tracks across whole albums (skipping recently played tracks)
        candidate_tracks = []
        for alb in matched_albums:
            rk = alb.get("rating_key")
            if not rk:
                continue
            al_tracks = get_album_tracks(str(rk))
            if not al_tracks:
                continue
            art_lower = (alb.get("artist") or "").strip().lower()
            fresh_tracks = [
                t for t in al_tracks
                if t["ratingKey"] not in recent_keys
                and (art_lower, (t.get("title") or "").strip().lower()) not in recent_pairs
            ]
            pool = fresh_tracks if fresh_tracks else al_tracks
            sampled = random.sample(pool, min(4, len(pool))) if rotate else pool[:4]
            for t in sampled:
                candidate_tracks.append({
                    "ratingKey": t["ratingKey"],
                    "artist": alb.get("artist"),
                    "album": alb.get("album"),
                    "title": t["title"],
                    "year": alb.get("year"),
                    "duration": t["duration"],
                })

        track_rating_keys = []
        if use_gemini_flow and candidate_tracks:
            sequenced = gemini_curator.sequence_flowing_playlist(
                channel_name=cdef["name"],
                channel_desc=cdef["description"],
                candidate_tracks=candidate_tracks,
                target_count=20,
            )
            track_rating_keys = [str(s["ratingKey"]) for s in sequenced]

        # Fallback to album keys if track keys failed
        if not track_rating_keys:
            track_rating_keys = [str(a["rating_key"]) for a in matched_albums[:8] if a.get("rating_key")]

        pl = create_or_replace_plex_playlist(title, track_rating_keys)
        if pl:
            results.append({
                "channel_key": key,
                "title": pl["title"],
                "rating_key": pl["ratingKey"],
                "track_count": pl["leafCount"],
                "album_count": len(matched_albums),
                "icon": cdef["icon"],
            })
            print(f" ✓ Updated {title} (ratingKey={pl['ratingKey']}): {pl['leafCount']} tracks ({len(matched_albums)} albums)")

    return results


def generate_large_vault_playlist(rotate: bool = False) -> Optional[dict]:
    """Generate a large curated master playlist (200+ tracks) from unplayed albums across the library."""
    vault = get_unplayed_vault_albums(limit=1000)
    if not vault:
        return None

    selected_keys = []
    selected_albums = []

    # 1. Collect 4 distinct unplayed albums per channel
    for ch_key, cdef in CHANNEL_DEFINITIONS.items():
        ch_matches = []
        targets = list(cdef["targets"])
        if rotate:
            random.shuffle(targets)
        for art, alb in targets:
            for a in vault:
                if art.lower() in (a.get("artist") or "").lower() and alb.lower() in (a.get("album") or "").lower():
                    rk = str(a.get("rating_key"))
                    if rk not in selected_keys:
                        ch_matches.append(a)
                        break

        kw_matches = []
        for a in vault:
            art = (a.get("artist") or "").lower()
            if any(k in art for k in cdef["keywords"]):
                rk = str(a.get("rating_key"))
                if rk not in selected_keys and a not in ch_matches:
                    kw_matches.append(a)
        if rotate:
            random.shuffle(kw_matches)

        combined = (ch_matches + kw_matches)[:4]
        for p in combined:
            selected_keys.append(str(p["rating_key"]))
            selected_albums.append(p)

    # 2. Add unplayed acclaimed gems from across the library
    other_gems = [
        "elbow", "bon iver", "courtney barnett", "war on drugs", "avett", "weyes blood",
        "frightened rabbit", "waxahatchee", "sierra ferrell", "sam fender", "fleet foxes", "sufjan",
    ]
    gem_matches = []
    for a in vault:
        art = (a.get("artist") or "").lower()
        rk = str(a.get("rating_key"))
        if rk not in selected_keys and any(g in art for g in other_gems):
            gem_matches.append(a)

    if rotate:
        random.shuffle(gem_matches)

    for a in gem_matches:
        rk = str(a.get("rating_key"))
        selected_keys.append(rk)
        selected_albums.append(a)
        if len(selected_keys) >= 25:
            break

    # 3. Fill up to 25 albums if needed from general vault
    if len(selected_keys) < 25:
        remaining_pool = [a for a in vault if str(a.get("rating_key")) not in selected_keys]
        if rotate:
            random.shuffle(remaining_pool)
        for a in remaining_pool:
            selected_keys.append(str(a.get("rating_key")))
            selected_albums.append(a)
            if len(selected_keys) >= 25:
                break

    title = "🏛️ Crate: The Vault (Large Unplayed Gems)"
    pl = create_or_replace_plex_playlist(title, selected_keys)
    if pl:
        print(f" ✓ Updated {title} (ratingKey={pl['ratingKey']}): {pl['leafCount']} tracks ({len(selected_keys)} albums)")
        return {
            "title": pl["title"],
            "track_count": pl["leafCount"],
            "album_count": len(selected_keys),
            "rating_key": pl["ratingKey"],
        }
    return None


def generate_playlists():
    """Create or refresh all discovery playlists in Plex."""
    print("=== REFRESHING ALL CRATE CHANNEL PLAYLISTS ===")
    results = generate_channel_playlists()
    large = generate_large_vault_playlist()
    return {"channels": results, "vault": large}


if __name__ == "__main__":
    generate_playlists()
