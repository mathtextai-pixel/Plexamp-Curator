#!/usr/bin/env python3
"""
Discovery Engine: Stepping-stone music discovery, Deezer enrichment,
and Lidarr / Deemix sync.
"""

import os
import sqlite3
import sys
import urllib.error
import urllib.parse
from typing import List, Optional, Tuple

from config import CONFIG, get_int
from http_client import get_json, http_error_body, request_json

DB_PATH = os.path.join(CONFIG["DATA_DIR"], "recommendations.db")
_LIDARR_DEFAULTS_CACHE: Optional[Tuple[str, int, int]] = None


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            year INTEGER,
            genre TEXT,
            reason TEXT,
            cover_url TEXT,
            preview_url TEXT,
            deezer_id TEXT,
            deezer_url TEXT,
            deemix_url TEXT,
            bandcamp_url TEXT,
            lidarr_status TEXT DEFAULT 'Not Added',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            channel TEXT DEFAULT 'General',
            UNIQUE(artist, album)
        )
        """
    )
    # Ensure `channel` column exists on older databases
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
    if "channel" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN channel TEXT DEFAULT 'General'")
    conn.commit()
    return conn


def _deemix_public_url() -> str:
    return (CONFIG.get("DEEMIX_PUBLIC_URL") or CONFIG.get("DEEMIX_URL") or "http://127.0.0.1:6595").rstrip("/")


def _lidarr_url() -> str:
    return CONFIG["LIDARR_URL"].rstrip("/")


def fetch_deezer_metadata(artist: str, album: str) -> dict:
    """Fetch cover art, preview audio MP3, and Deezer ID from Deezer API."""
    q = urllib.parse.quote(f'artist:"{artist}" album:"{album}"')
    url = f"https://api.deezer.com/search/album?q={q}&limit=1"
    try:
        data = get_json(url, timeout=6) or {}
        if data.get("data"):
            item = data["data"][0]
            deezer_id = str(item.get("id"))
            cover = item.get("cover_big") or item.get("cover_medium") or item.get("cover")
            link = item.get("link")

            preview_url = None
            try:
                tracks_url = f"https://api.deezer.com/album/{deezer_id}/tracks?limit=1"
                tdata = get_json(tracks_url, timeout=5, retries=1) or {}
                if tdata.get("data"):
                    preview_url = tdata["data"][0].get("preview")
            except Exception:
                pass

            deemix_url = f"{_deemix_public_url()}/#/album/{deezer_id}"
            return {
                "cover_url": cover,
                "preview_url": preview_url,
                "deezer_id": deezer_id,
                "deezer_url": link,
                "deemix_url": deemix_url,
            }
    except Exception as e:
        print(f"Deezer fetch error for {artist} - {album}: {e}", file=sys.stderr)

    deemix_search = f"{_deemix_public_url()}/#/search/albums/{urllib.parse.quote(artist + ' ' + album)}"
    return {
        "cover_url": None,
        "preview_url": None,
        "deezer_id": None,
        "deezer_url": f"https://www.deezer.com/search/{urllib.parse.quote(artist + ' ' + album)}",
        "deemix_url": deemix_search,
    }


def get_lidarr_headers() -> dict:
    return {"X-Api-Key": CONFIG["LIDARR_API_KEY"], "Accept": "application/json"}


def _resolve_lidarr_defaults() -> Tuple[str, int, int]:
    """
    Resolve (rootFolderPath, qualityProfileId, metadataProfileId) from CONFIG or
    auto-detect from the Lidarr API, caching the result in memory.
    """
    global _LIDARR_DEFAULTS_CACHE
    if _LIDARR_DEFAULTS_CACHE:
        return _LIDARR_DEFAULTS_CACHE

    root_folder = (CONFIG.get("LIDARR_ROOT_FOLDER") or "").strip()
    qp_str = (CONFIG.get("LIDARR_QUALITY_PROFILE_ID") or "").strip()
    mp_str = (CONFIG.get("LIDARR_METADATA_PROFILE_ID") or "").strip()

    quality_id = int(qp_str) if qp_str.isdigit() else 0
    metadata_id = int(mp_str) if mp_str.isdigit() else 0

    try:
        if not root_folder:
            folders = get_json(f"{_lidarr_url()}/api/v1/rootfolder", headers=get_lidarr_headers(), timeout=5)
            if folders and isinstance(folders, list):
                root_folder = folders[0].get("path") or "/data/media/Music"
        if not quality_id:
            qps = get_json(f"{_lidarr_url()}/api/v1/qualityprofile", headers=get_lidarr_headers(), timeout=5)
            if qps and isinstance(qps, list):
                quality_id = int(qps[0].get("id", 1))
        if not metadata_id:
            mps = get_json(f"{_lidarr_url()}/api/v1/metadataprofile", headers=get_lidarr_headers(), timeout=5)
            if mps and isinstance(mps, list):
                metadata_id = int(mps[0].get("id", 1))
    except Exception as e:
        print(f"Lidarr profile auto-detect fallback ({e})", file=sys.stderr)

    _LIDARR_DEFAULTS_CACHE = (
        root_folder or "/data/media/Music",
        quality_id or 1,
        metadata_id or 1,
    )
    return _LIDARR_DEFAULTS_CACHE


def check_lidarr_status_for_album(artist: str, album: str) -> Optional[str]:
    """
    Check whether Lidarr has downloaded this album, has it wanted, or not added.
    Returns None if Lidarr could not be reached or answered with something unexpected,
    so callers can keep the last known status instead of overwriting it.
    """
    try:
        q = urllib.parse.quote(artist)
        artists_data = get_json(f"{_lidarr_url()}/api/v1/artist?name={q}", headers=get_lidarr_headers(), timeout=5)
        if not isinstance(artists_data, list):
            raise ValueError(f"unexpected artist response type {type(artists_data).__name__}")

        wanted_artist = artist.strip().lower()
        matched_artist = next(
            (a for a in artists_data if (a.get("artistName") or "").strip().lower() == wanted_artist),
            None,
        )
        if not matched_artist:
            return "Not Added"

        albums_data = get_json(
            f"{_lidarr_url()}/api/v1/album?artistId={matched_artist['id']}",
            headers=get_lidarr_headers(),
            timeout=5,
        )
        if not isinstance(albums_data, list):
            raise ValueError(f"unexpected album response type {type(albums_data).__name__}")

        wanted_album = album.strip().lower()
        for alb in albums_data:
            title = (alb.get("title") or "").strip().lower()
            if not title or not wanted_album:
                continue  # an empty string is a substring of everything
            if wanted_album in title or title in wanted_album:
                has_files = alb.get("hasFile") or ((alb.get("statistics") or {}).get("trackFileCount", 0) > 0)
                if has_files:
                    return "Downloaded"
                if alb.get("monitored"):
                    return "Wanted"
                return "Not Monitored"

        return "Artist Added (Album Missing)"
    except Exception as e:
        print(f"Lidarr check error for {artist} - {album}: {e}", file=sys.stderr)
        return None


def add_to_lidarr(artist_name: str, album_name: Optional[str] = None) -> dict:
    """Add an artist to Lidarr and trigger album search."""
    try:
        q = urllib.parse.quote(artist_name)
        results = get_json(f"{_lidarr_url()}/api/v1/artist/lookup?term={q}", headers=get_lidarr_headers(), timeout=8)

        if not results:
            return {"success": False, "message": f"Artist '{artist_name}' not found on MusicBrainz/Lidarr"}

        artist_meta = results[0]
        root_folder, quality_id, metadata_id = _resolve_lidarr_defaults()

        payload = {
            "artistName": artist_meta.get("artistName"),
            "foreignArtistId": artist_meta.get("foreignArtistId"),
            "qualityProfileId": quality_id,
            "metadataProfileId": metadata_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "addOptions": {
                "monitor": "all",
                "searchForMissingAlbums": True,
            },
        }

        # Not retried: a lost response would turn into an "already added" error on retry.
        res_data = request_json(
            "POST",
            f"{_lidarr_url()}/api/v1/artist",
            headers=get_lidarr_headers(),
            body=payload,
            timeout=10,
            retries=0,
        )
        return {"success": True, "data": res_data, "message": f"Added {artist_name} to Lidarr!"}

    except urllib.error.HTTPError as he:
        err_msg = http_error_body(he)
        if "already been added" in err_msg.lower():
            return {"success": True, "message": f"Artist {artist_name} is already in Lidarr. Searching missing albums..."}
        return {"success": False, "message": f"Lidarr API error: {he.code} - {err_msg}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


SEED_STEPPING_STONES = [
    {
        "artist": "MJ Lenderman",
        "album": "Manning Fireworks",
        "year": 2024,
        "genre": "Indie Country / Rust-Belt Rock",
        "channel": "Alt & Prime Country",
        "reason": "The breakout Americana-indie crossover of 2024. Fuzzed-out Neil Young guitars, lap steel, and sardonic, poignant songwriting perfect for Jason Isbell and Avett Brothers fans.",
    },
    {
        "artist": "Sierra Ferrell",
        "album": "Trail of Flowers",
        "year": 2024,
        "genre": "Americana / Bluegrass / Gypsy Folk",
        "channel": "Alt & Prime Country",
        "reason": "Breathtaking mountain vocals, virtuosic acoustic picking, and vintage Southern charm. Direct companion to Tyler Childers and The Avett Brothers.",
    },
    {
        "artist": "Frightened Rabbit",
        "album": "The Midnight Organ Fight",
        "year": 2008,
        "genre": "Scottish Indie Rock / Anthemic Folk",
        "channel": "Pop & Acoustic Punk",
        "reason": "Raw, heart-on-sleeve catharsis with colossal, driving builds. The gold standard for fans of Gang of Youths and Mumford & Sons.",
    },
    {
        "artist": "Waxahatchee",
        "album": "Tigers Blood",
        "year": 2024,
        "genre": "Americana / Alt-Country",
        "channel": "Alt & Prime Country",
        "reason": "Katie Crutchfield's warm Southern storytelling recorded alongside Brad Cook and MJ Lenderman. Pure melodic roots perfection.",
    },
    {
        "artist": "The Felice Brothers",
        "album": "Valley of Abandoned Songs",
        "year": 2024,
        "genre": "Americana / Roots Rock",
        "channel": "Alt & Prime Country",
        "reason": "Accordion, fiddle, and shambolic Dylan-esque basement rock. Exactly what makes The New Basement Tapes and The Avett Brothers so endearing.",
    },
    {
        "artist": "Charley Crockett",
        "album": "$10 Cowboy",
        "year": 2024,
        "genre": "Gulf & Western / Country Blues",
        "channel": "Alt & Prime Country",
        "reason": "Swaggering vintage country-blues recorded live to tape. A refreshing, dusty antidote to over-polished modern roots music.",
    },
    {
        "artist": "Say She She",
        "album": "Silver",
        "year": 2023,
        "genre": "Discodelic Soul / Funk",
        "channel": "Indie Soul & Grooves",
        "reason": "Boundary-pushing harmonies, intoxicating funk grooves, and vibrant vintage rhythm production—satisfies that Remi Wolf high-energy itch.",
    },
    {
        "artist": "Sam Fender",
        "album": "Seventeen Going Under",
        "year": 2021,
        "genre": "Heartland Rock / Indie Rock",
        "channel": "Pop & Acoustic Punk",
        "reason": "Geordie heartland rock packed with Bruce Springsteen/War on Drugs propulsive energy, soaring sax solos, and working-class anthems.",
    },
    {
        "artist": "Hiss Golden Messenger",
        "album": "Jump for Joy",
        "year": 2023,
        "genre": "Folk-Funk / Southern Rock",
        "channel": "Alt & Prime Country",
        "reason": "M.C. Taylor crafts buoyant, optimistic grooves driven by syncopated basslines, warm organs, and reflective Southern lyrics.",
    },
    {
        "artist": "Deer Tick",
        "album": "Emotional Contracts",
        "year": 2023,
        "genre": "Alt-Country / Indie Garage Rock",
        "channel": "Alt & Prime Country",
        "reason": "Gritty, booze-soaked barroom rock meets tender songwriting. Produced by Dave Fridmann (Flaming Lips, Spoon).",
    },
]


def _insert_recommendation_item(conn: sqlite3.Connection, item: dict) -> Optional[dict]:
    artist = (item.get("artist") or "").strip()
    album = (item.get("album") or "").strip()
    if not artist or not album:
        return None

    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM recommendations WHERE lower(artist)=lower(?) AND lower(album)=lower(?)",
        (artist, album),
    )
    if cur.fetchone():
        return None

    print(f"Discovering: {artist} - {album}...")
    meta = fetch_deezer_metadata(artist, album)
    # Unknown (Lidarr unreachable) is stored as "Not Added"; the next successful sync corrects it.
    status = check_lidarr_status_for_album(artist, album) or "Not Added"
    bandcamp_url = f"https://bandcamp.com/search?q={urllib.parse.quote(artist + ' ' + album)}"
    channel = item.get("channel") or item.get("genre") or "General"

    cur.execute(
        """
        INSERT OR REPLACE INTO recommendations
        (artist, album, year, genre, reason, cover_url, preview_url, deezer_id, deezer_url, deemix_url, bandcamp_url, lidarr_status, channel)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artist,
            album,
            item.get("year"),
            item.get("genre") or channel,
            item.get("reason"),
            meta.get("cover_url"),
            meta.get("preview_url"),
            meta.get("deezer_id"),
            meta.get("deezer_url"),
            meta.get("deemix_url"),
            bandcamp_url,
            status,
            channel,
        ),
    )
    conn.commit()
    return {"artist": artist, "album": album, "channel": channel, "lidarr_status": status}


def populate_and_sync_db():
    """Populate initial stepping stones and sync Deezer + Lidarr status."""
    conn = get_db()
    for item in SEED_STEPPING_STONES:
        _insert_recommendation_item(conn, item)
    conn.close()
    print("Database sync complete!")


def run_weekly_discovery_crawl(count: Optional[int] = None) -> List[dict]:
    """
    Use recent Tautulli listening habits + Gemini (or Ollama fallback) to discover
    brand-new stepping-stone albums not already in the library or recommendations DB.
    """
    import gemini_curator
    from music_excavator import analyze_recent_habits

    if count is None:
        count = get_int("DISCOVERY_PICKS_PER_WEEK")

    habits = analyze_recent_habits(limit=500)
    top_artists = [a for a, _ in habits.get("top_artists", [])]
    if not top_artists:
        top_artists = [
            "Tyler Childers", "The Avett Brothers", "The Menzingers",
            "Khruangbin", "Flipturn", "The Cure", "Jason Isbell",
        ]

    conn = get_db()
    cur = conn.cursor()

    existing_artists_set = set()
    try:
        cur.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")
        for r in cur.fetchall():
            existing_artists_set.add(r[0])
    except Exception:
        pass

    cur.execute("SELECT DISTINCT artist FROM recommendations WHERE artist != ''")
    for r in cur.fetchall():
        existing_artists_set.add(r[0])

    picks = gemini_curator.discover_new_artists_with_gemini(
        top_artists=top_artists,
        existing_library_artists=sorted(existing_artists_set),
        count=count,
    )

    added: List[dict] = []
    for item in picks or []:
        res = _insert_recommendation_item(conn, item)
        if res:
            added.append(res)

    conn.close()
    print(f"  ✓ Weekly discovery crawl added {len(added)} new album recommendations.")
    return added


def get_all_recommendations() -> List[dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM recommendations ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Rewrite legacy localhost / host.docker.internal Deemix URLs to browser-facing DEEMIX_PUBLIC_URL
    public_deemix = _deemix_public_url()
    for r in rows:
        durl = r.get("deemix_url") or ""
        if "/#/" in durl:
            suffix = durl.split("/#/", 1)[1]
            r["deemix_url"] = f"{public_deemix}/#/{suffix}"
    return rows


if __name__ == "__main__":
    populate_and_sync_db()
