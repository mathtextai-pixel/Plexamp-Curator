#!/usr/bin/env python3
"""
Music Excavator: Mine your unplayed Plex vault and discover new music
powered by Tautulli play history and Plex Media Server.
"""

import logging
import os
import random
import sqlite3
import time
import urllib.parse
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import db
from config import CONFIG, get_int
from http_client import get_json, request_json, request_raw
from locks import file_lock
from textmatch import contains_any_phrase, contains_phrase

log = logging.getLogger(__name__)

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
    try:
        data = get_json(url, timeout=10)
        return (data or {}).get("response", {}).get("data", {}).get("data", []) or []
    except Exception as e:
        log.warning(f"Could not fetch Tautulli history: {e}")
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


VAULT_PAGE_SIZE = 500
VAULT_MAX_ALBUMS = 20000  # safety stop for pathological libraries


def get_unplayed_vault_albums(limit: Optional[int] = None) -> List[dict]:
    """
    Fetch unplayed albums from the Plex music library, paging through the whole
    section (Plex sorts by title, so a single capped request only ever saw the
    first part of the alphabet). `limit` caps the total; None means all.
    """
    cap = min(limit, VAULT_MAX_ALBUMS) if limit else VAULT_MAX_ALBUMS
    items: List[dict] = []
    start = 0
    while start < cap:
        size = min(VAULT_PAGE_SIZE, cap - start)
        url = (
            f"{_plex_url()}/library/sections/{_music_section_id()}/all?"
            f"type=9&unwatched=1&X-Plex-Container-Start={start}&X-Plex-Container-Size={size}"
        )
        try:
            data = get_json(url, headers=get_plex_headers(), timeout=20)
        except Exception as e:
            log.error(f"Error fetching unplayed vault albums (offset {start}): {e}")
            if not items:
                return []
            break  # keep what we have rather than failing the whole refresh
        container = (data or {}).get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        items.extend(page)
        total = container.get("totalSize")
        start += len(page)
        if len(page) < size or (isinstance(total, int) and start >= total):
            break

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


_MACHINE_ID_FAILED_AT = 0.0
MACHINE_ID_RETRY_AFTER_SECONDS = 30


def get_plex_machine_id(timeout: float = 10, retries: int = 2, skip_if_recently_failed: bool = False) -> Optional[str]:
    """
    Retrieve and cache the Plex machine identifier.
    With skip_if_recently_failed=True (page loads), return None straight away if a lookup
    failed in the last MACHINE_ID_RETRY_AFTER_SECONDS instead of waiting on Plex again.
    """
    global _CACHED_MACHINE_ID, _MACHINE_ID_FAILED_AT
    if _CACHED_MACHINE_ID:
        return _CACHED_MACHINE_ID
    if skip_if_recently_failed and time.monotonic() - _MACHINE_ID_FAILED_AT < MACHINE_ID_RETRY_AFTER_SECONDS:
        return None
    try:
        data = get_json(f"{_plex_url()}/identity", headers=get_plex_headers(), timeout=timeout, retries=retries)
        _CACHED_MACHINE_ID = (data or {}).get("MediaContainer", {}).get("machineIdentifier") or None
    except Exception as e:
        log.error(f"Error fetching Plex machine ID: {e}")
    if not _CACHED_MACHINE_ID:
        _MACHINE_ID_FAILED_AT = time.monotonic()
    return _CACHED_MACHINE_ID


class PlexError(Exception):
    """A Plex request failed (as opposed to a lookup that found nothing)."""


def _is_truthy(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True")


def _playlist_lock_name(title: str) -> str:
    return f"plex-playlist:{title.strip().lower()}"


def _items_uri(machine_id: str, rating_keys: List[str]) -> str:
    return f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{','.join(rating_keys)}"


def find_plex_playlist_by_title(title: str) -> Optional[dict]:
    """
    Find an existing regular (non-smart) audio playlist in Plex by title.
    Returns None only when Plex answered and no playlist matched.
    Raises PlexError when the playlist list could not be fetched, so callers never
    mistake a transient Plex error for "playlist doesn't exist" and create a duplicate.
    """
    url = f"{_plex_url()}/playlists?playlistType=audio"
    try:
        data = get_json(url, headers=get_plex_headers(), timeout=10)
    except Exception as e:
        raise PlexError(f"could not list Plex playlists: {e}") from e

    wanted = title.strip().lower()
    for pl in (data or {}).get("MediaContainer", {}).get("Metadata", []) or []:
        if (pl.get("playlistType") or "audio") != "audio" or _is_truthy(pl.get("smart")):
            continue
        if (pl.get("title") or "").strip().lower() == wanted:
            return pl
    return None


def _fetch_playlist_info(pl_id: str) -> dict:
    data = get_json(f"{_plex_url()}/playlists/{pl_id}", headers=get_plex_headers(), timeout=10)
    return ((data or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0]


def _playlist_result(info: dict, fallback_title: str, fallback_key: str, fallback_count: int) -> dict:
    return {
        "title": info.get("title") or fallback_title,
        "ratingKey": str(info.get("ratingKey") or fallback_key),
        "leafCount": info.get("leafCount", fallback_count),
    }


def _replace_playlist_items_in_place(pl_id: str, title: str, items_uri: str, expected_count: int) -> Optional[dict]:
    """
    Clear and refill an existing playlist, keeping its ratingKey (so Plexamp pins,
    shortcuts and downloads survive). Never falls back to creating a new playlist:
    on failure the playlist is left as-is and None is returned.
    """
    base = f"{_plex_url()}/playlists/{pl_id}"

    # 1. Clear existing items. Clearing twice is harmless, so timeouts may be retried.
    try:
        request_raw("DELETE", f"{base}/items", headers=get_plex_headers(), timeout=15,
                    retries=2, retry_on_timeout=True)
    except Exception as e:
        log.error(f"Error clearing playlist '{title}' (ratingKey={pl_id}): {e}. Playlist left unchanged.")
        return None

    # 2. Refill. PUT appends, so it is only re-sent once Plex confirms the playlist is still empty.
    put_url = f"{base}/items?uri={urllib.parse.quote(items_uri, safe='')}"
    for attempt in (1, 2):
        try:
            request_raw("PUT", put_url, headers=get_plex_headers(), timeout=30, retries=0)
            break
        except Exception as e:
            log.warning(f"Adding items to '{title}' failed on attempt {attempt} ({e}); checking playlist state...")
            try:
                current = _fetch_playlist_info(pl_id)
            except Exception as e2:
                log.error(f"Could not verify playlist '{title}' (ratingKey={pl_id}) after failed update: {e2}")
                return None
            if int(current.get("leafCount") or 0) > 0:
                break  # Plex applied the PUT even though the response was lost
            if attempt == 2:
                log.error(
                    f"Playlist '{title}' (ratingKey={pl_id}) is empty after a failed update. "
                    "It was NOT recreated, so its ratingKey and pins are preserved; re-run to refill it."
                )
                return None

    # 3. Read back updated metadata (best effort; the update itself already succeeded).
    try:
        info = _fetch_playlist_info(pl_id)
    except Exception as e:
        log.warning(f"Updated playlist '{title}' but could not read it back: {e}")
        info = {}
    return _playlist_result(info, title, pl_id, expected_count)


def _create_playlist(title: str, items_uri: str) -> Optional[dict]:
    params = urllib.parse.urlencode({
        "type": "audio",
        "title": title,
        "smart": 0,
        "uri": items_uri,
    })
    try:
        # Not idempotent: a retried POST could create a second playlist.
        data = request_json("POST", f"{_plex_url()}/playlists?{params}", headers=get_plex_headers(),
                            timeout=15, retries=0)
    except Exception as e:
        log.error(f"Error creating playlist '{title}': {e}")
        return None
    pl = ((data or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0]
    if not pl.get("ratingKey"):
        log.error(f"Error creating playlist '{title}': Plex returned no ratingKey")
        return None
    return _playlist_result(pl, title, "", 0)


def create_or_replace_plex_playlist(title: str, rating_keys: List[str]) -> Optional[dict]:
    """
    Create or update a playlist in Plex containing albums/tracks by rating key.
    Existing playlists are updated IN-PLACE (clear items, then add new ones) so the
    playlist ratingKey, Plexamp pins, and downloads are preserved. A new playlist is
    only created when Plex confirms none with this title exists.
    Writes to the same playlist are serialised across workers and threads.
    """
    # Plex rating keys are numeric; anything else is dropped so it can't alter the item URI.
    clean_keys = [str(k).strip() for k in rating_keys if str(k).strip().isdigit()]
    if not clean_keys:
        return None

    machine_id = get_plex_machine_id()
    if not machine_id:
        return None
    items_uri = _items_uri(machine_id, clean_keys)

    with file_lock(_playlist_lock_name(title)):
        try:
            existing = find_plex_playlist_by_title(title)
        except PlexError as e:
            log.error(f"{e}. Not touching playlist '{title}' to avoid creating a duplicate.")
            return None

        if existing and existing.get("ratingKey"):
            return _replace_playlist_items_in_place(str(existing["ratingKey"]), title, items_uri, len(clean_keys))
        return _create_playlist(title, items_uri)


def append_to_plex_playlist(title: str, rating_key: str) -> Optional[dict]:
    """Append a single album or track to an existing playlist, or create it if not found."""
    if not str(rating_key).strip().isdigit():
        return None
    rating_key = str(rating_key).strip()
    machine_id = get_plex_machine_id()
    if not machine_id:
        return None
    item_uri = _items_uri(machine_id, [str(rating_key)])

    with file_lock(_playlist_lock_name(title)):
        try:
            existing = find_plex_playlist_by_title(title)
        except PlexError as e:
            log.error(f"{e}. Not appending to '{title}' to avoid creating a duplicate.")
            return None
        if not existing or not existing.get("ratingKey"):
            return _create_playlist(title, item_uri)

        pl_id = str(existing["ratingKey"])
        put_url = f"{_plex_url()}/playlists/{pl_id}/items?uri={urllib.parse.quote(item_uri, safe='')}"
        try:
            # PUT appends: never retried, or the item could be added twice.
            request_raw("PUT", put_url, headers=get_plex_headers(), timeout=10, retries=0)
        except Exception as e:
            log.error(f"Error appending to playlist '{title}': {e}")
            return None
        try:
            info = _fetch_playlist_info(pl_id)
        except Exception as e:
            log.warning(f"Appended to '{title}' but could not read it back: {e}")
            info = {}
        return _playlist_result(info, title, pl_id, int(existing.get("leafCount") or 0) + 1)


# Keep alias for backward compatibility
create_plex_playlist = create_or_replace_plex_playlist


REINDEX_PAGE_SIZE = 5000
# Refuse to delete stale rows if this would remove more than this share of the index:
# a sudden drop usually means a wrong MUSIC_SECTION_ID or a partial Plex response, not
# that you deleted a fifth of your library overnight.
REINDEX_MAX_PRUNE_FRACTION = 0.2


def _fetch_all_library_tracks() -> Tuple[List[dict], bool]:
    """
    Page through every audio track (type=10) in the music section.
    Returns (items, complete). `complete` is False if any page failed, in which
    case the caller must not treat missing tracks as deleted.
    """
    items: List[dict] = []
    start = 0
    while True:
        url = (
            f"{_plex_url()}/library/sections/{_music_section_id()}/all?type=10"
            f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={REINDEX_PAGE_SIZE}"
        )
        try:
            data = get_json(url, headers=get_plex_headers(), timeout=45, retries=1)
        except Exception as e:
            log.error(f"Error fetching library tracks from Plex for re-index (offset {start}): {e}")
            return items, False
        container = (data or {}).get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        items.extend(page)
        start += len(page)
        total = container.get("totalSize")
        if not page or len(page) < REINDEX_PAGE_SIZE or (isinstance(total, int) and start >= total):
            if isinstance(total, int) and start < total:
                log.warning(f"Plex reported {total} tracks but returned {start}; treating re-index as incomplete.")
                return items, False
            return items, True


def reindex_library_tracks() -> int:
    """
    Sync every audio track in the Plex music section into the `library_tracks` table.
    New and changed tracks are upserted. Tracks missing from Plex are removed only when
    the fetch was complete and the removal is not suspiciously large.
    Returns the number of tracks fetched from Plex (0 if nothing could be fetched).
    """
    items, complete = _fetch_all_library_tracks()
    if not items:
        return 0

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

    removed = 0
    with db.session() as conn:
        with conn:
            existing_keys = {r[0] for r in conn.execute("SELECT rating_key FROM library_tracks")}
            conn.executemany(
                """
                INSERT OR REPLACE INTO library_tracks (rating_key, artist, album, title, year, genre)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            stale_keys = existing_keys - seen_keys
            if stale_keys and not complete:
                log.warning(f"Re-index incomplete: kept {len(stale_keys)} tracks that Plex did not return.")
            elif stale_keys and len(stale_keys) > REINDEX_MAX_PRUNE_FRACTION * len(existing_keys):
                log.error(
                    f"Re-index would remove {len(stale_keys)} of {len(existing_keys)} indexed tracks "
                    f"(more than {REINDEX_MAX_PRUNE_FRACTION:.0%}); keeping them. Check MUSIC_SECTION_ID / Plex. "
                    "If the library really shrank, clear the library_tracks table to re-index from scratch."
                )
            elif stale_keys:
                conn.executemany("DELETE FROM library_tracks WHERE rating_key = ?", [(k,) for k in stale_keys])
                removed = len(stale_keys)

    log.info(f"✓ Indexed {len(rows)} tracks from Plex into library_tracks (removed {removed} stale).")
    return len(rows)


def get_library_track_count() -> int:
    """Return total number of indexed tracks in library_tracks."""
    try:
        with db.session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM library_tracks").fetchone()[0]
        return int(count or 0)
    except sqlite3.Error:
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
    tracks = []
    try:
        data = get_json(url, headers=get_plex_headers(), timeout=10)
        for t in (data or {}).get("MediaContainer", {}).get("Metadata", []) or []:
            tracks.append({
                "ratingKey": str(t.get("ratingKey")),
                "title": t.get("title"),
                "index": t.get("index"),
                "duration": t.get("duration", 0),
            })
    except Exception as e:
        log.error(f"Error fetching tracks for album {rating_key}: {e}")
    return tracks


def _build_channel_playlist(
    key: str,
    cdef: dict,
    vault: List[dict],
    recent_keys: Set[str],
    recent_pairs: Set[Tuple[str, str]],
    rotate: bool,
    use_gemini_flow: bool,
) -> Optional[dict]:
    """Pick albums and tracks for one channel and update its Plex playlist in place."""
    import gemini_curator

    title = cdef["title"]

    matched_albums = []

    # 1. Match specific target masterpieces
    targets = list(cdef["targets"])
    if rotate:
        random.shuffle(targets)
    for art, alb in targets:
        for a in vault:
            if contains_phrase(a.get("artist") or "", art) and contains_phrase(a.get("album") or "", alb):
                if a not in matched_albums:
                    matched_albums.append(a)
                    break

    # 2. Gather more albums by keywords
    keyword_matches = []
    for a in vault:
        if contains_any_phrase(a.get("artist") or "", cdef["keywords"]):
            if a not in matched_albums and a not in keyword_matches:
                keyword_matches.append(a)

    if rotate:
        random.shuffle(keyword_matches)

    for a in keyword_matches:
        if len(matched_albums) >= 10:
            break
        matched_albums.append(a)

    if not matched_albums:
        return None

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
        log.info(f"✓ Updated {title} (ratingKey={pl['ratingKey']}): {pl['leafCount']} tracks ({len(matched_albums)} albums)")
        return {
            "channel_key": key,
            "title": pl["title"],
            "rating_key": pl["ratingKey"],
            "track_count": pl["leafCount"],
            "album_count": len(matched_albums),
            "icon": cdef["icon"],
        }
    return None


def generate_channel_playlists(
    channel_key: Optional[str] = None,
    rotate: bool = False,
    use_gemini_flow: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """
    Generate curated, flowing playlists for all channels or a single specified channel.
    Samples across whole albums (excluding tracks played in the last RECENTLY_PLAYED_DAYS)
    and asks the configured LLM to sequence a cohesive ~20-track mixtape.
    """
    report = progress or (lambda _msg: None)
    report("Scanning your unplayed vault in Plex…")
    vault = get_unplayed_vault_albums()
    recent_keys, recent_pairs = get_recently_played_tracks()

    selected_channels = (
        {channel_key: CHANNEL_DEFINITIONS[channel_key]}
        if channel_key and channel_key in CHANNEL_DEFINITIONS
        else CHANNEL_DEFINITIONS
    )

    results = []
    for idx, (key, cdef) in enumerate(selected_channels.items(), 1):
        report(f"Curating {cdef['icon']} {cdef['name']} ({idx}/{len(selected_channels)})…")
        try:
            res = _build_channel_playlist(key, cdef, vault, recent_keys, recent_pairs, rotate, use_gemini_flow)
        except Exception as e:
            # One broken channel must not stop the others from refreshing.
            log.exception(f"Channel '{cdef['name']}' failed: {e}")
            continue
        if res:
            results.append(res)

    return results


def generate_large_vault_playlist(rotate: bool = False) -> Optional[dict]:
    """Generate a large curated master playlist (200+ tracks) from unplayed albums across the library."""
    vault = get_unplayed_vault_albums()
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
                if contains_phrase(a.get("artist") or "", art) and contains_phrase(a.get("album") or "", alb):
                    rk = str(a.get("rating_key"))
                    if rk not in selected_keys:
                        ch_matches.append(a)
                        break

        kw_matches = []
        for a in vault:
            if contains_any_phrase(a.get("artist") or "", cdef["keywords"]):
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
        rk = str(a.get("rating_key"))
        if rk not in selected_keys and contains_any_phrase(a.get("artist") or "", other_gems):
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
        log.info(f"✓ Updated {title} (ratingKey={pl['ratingKey']}): {pl['leafCount']} tracks ({len(selected_keys)} albums)")
        return {
            "title": pl["title"],
            "track_count": pl["leafCount"],
            "album_count": len(selected_keys),
            "rating_key": pl["ratingKey"],
        }
    return None


def generate_playlists():
    """Create or refresh all discovery playlists in Plex."""
    log.info("=== REFRESHING ALL CRATE CHANNEL PLAYLISTS ===")
    results = generate_channel_playlists()
    large = generate_large_vault_playlist()
    return {"channels": results, "vault": large}


if __name__ == "__main__":
    generate_playlists()
