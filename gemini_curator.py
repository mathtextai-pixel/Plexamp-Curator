#!/usr/bin/env python3
"""
Gemini & Local Ollama Curator: Sequences cohesive, flowing playlists
from Plex library tracks and generates external music discovery recommendations,
with a strict SQLite-based monthly Gemini budget limiter (< $1.50/mo) and
automatic fallback between Gemini Flash and local Ollama (hermes3:8b).
"""

import json
import logging
import os
import random
import re
import sqlite3
import urllib.error
from typing import Any, Callable, Dict, List, Optional

import db
from config import CONFIG, get_float, get_int
from http_client import request_json

log = logging.getLogger(__name__)

_VALID_PROVIDERS = ("gemini", "ollama")
_PROVIDER_SETTING_KEY = "llm_provider"


def _get_conn() -> sqlite3.Connection:
    return db.connect()


def _default_provider() -> str:
    p = (CONFIG.get("LLM_PROVIDER") or "gemini").strip().lower()
    return p if p in _VALID_PROVIDERS else "gemini"


def get_active_provider() -> str:
    """Active provider as last set from the UI (persisted in SQLite), else LLM_PROVIDER."""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_PROVIDER_SETTING_KEY,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning(f"reading LLM provider setting: {e}")
        row = None
    if row and row[0] in _VALID_PROVIDERS:
        return row[0]
    return _default_provider()


def set_active_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in _VALID_PROVIDERS:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (_PROVIDER_SETTING_KEY, p),
            )
            conn.commit()
        finally:
            conn.close()
    return get_active_provider()


# Budget months follow UTC (SQLite's CURRENT_TIMESTAMP is UTC): spend resets at 00:00 UTC on the 1st.
_MONTH_FILTER = "timestamp >= datetime('now', 'start of month')"


def get_current_month_usage_usd() -> float:
    """
    Total Gemini cost this (UTC) calendar month, including reservations for calls
    that are in flight or whose outcome is unknown.
    """
    with db.session() as conn:
        res = conn.execute(f"SELECT SUM(cost_usd) FROM gemini_usage WHERE {_MONTH_FILTER}").fetchone()[0]
    return float(res) if res is not None else 0.0


def get_llm_status() -> dict:
    """Return current LLM provider settings and monthly Gemini spend."""
    spend = get_current_month_usage_usd()
    budget = get_float("GEMINI_MONTHLY_BUDGET_USD")
    return {
        "provider": get_active_provider(),
        "gemini_model": CONFIG.get("GEMINI_MODEL", "gemini-flash-latest"),
        "ollama_model": CONFIG.get("OLLAMA_MODEL", "hermes3:8b"),
        "month_spend_usd": round(spend, 4),
        "monthly_budget_usd": budget,
    }


def _cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens / 1_000_000.0) * get_float("GEMINI_PRICE_IN_PER_1M") + (
        completion_tokens / 1_000_000.0
    ) * get_float("GEMINI_PRICE_OUT_PER_1M")


def estimate_max_cost_usd(prompt: str) -> float:
    """Worst-case cost of one call: generous input-token estimate + the full output token cap."""
    est_prompt_tokens = len(prompt) // 3 + 50  # ~4 chars/token in English; 3 leaves headroom
    return _cost_usd(est_prompt_tokens, get_int("GEMINI_MAX_OUTPUT_TOKENS"))


def _reserve_budget(estimate: float, purpose: str) -> Optional[int]:
    """
    Atomically check the monthly cap and record a reservation for one call.
    BEGIN IMMEDIATE takes SQLite's write lock, so concurrent calls from any worker
    are checked one at a time and can't all slip under the cap together.
    Returns the reservation row id, or None if the call would exceed the budget.
    """
    budget = get_float("GEMINI_MONTHLY_BUDGET_USD")
    conn = db.connect()
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        spent = conn.execute(f"SELECT COALESCE(SUM(cost_usd), 0) FROM gemini_usage WHERE {_MONTH_FILTER}").fetchone()[0]
        if spent + estimate > budget:
            conn.execute("ROLLBACK")
            log.warning(
                f"Monthly Gemini budget would be exceeded (${spent:.4f} spent + up to ${estimate:.4f} "
                f"for this call > ${budget:.2f}). Skipping Gemini call ({purpose})."
            )
            return None
        cur = conn.execute(
            "INSERT INTO gemini_usage (prompt_tokens, completion_tokens, cost_usd, purpose, status) "
            "VALUES (0, 0, ?, ?, 'reserved')",
            (estimate, purpose),
        )
        conn.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _settle_reservation(row_id: int, prompt_tokens: int, completion_tokens: int, cost: float, status: str) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE gemini_usage SET prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, status = ? WHERE id = ?",
            (prompt_tokens, completion_tokens, cost, status, row_id),
        )
        conn.commit()


def _parse_json_lenient(text: str) -> Optional[object]:
    """Parse JSON response, stripping optional markdown fences if present."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        # Try extracting first {...} or [...] block
        m = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


def _clean_str(value: Any, max_len: int = 300) -> str:
    """Return a stripped string from LLM output, or "" for anything that isn't a string."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _clean_str_list(value: Any, max_items: int = 50, max_len: int = 200) -> List[str]:
    """Keep only non-empty, de-duplicated (case-insensitive) strings from an LLM-returned list."""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            item = str(item)
        s = _clean_str(item, max_len)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def call_gemini_raw_json(prompt: str, purpose: str = "curation", timeout: int = 20) -> Optional[object]:
    """
    Call Google Gemini Flash with structured JSON output, a small thinking budget,
    an output-token cap, and the monthly budget cap enforced via a reservation.
    """
    api_key = CONFIG.get("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("Gemini API key is not configured.")
        return None

    estimate = estimate_max_cost_usd(prompt)
    reservation = _reserve_budget(estimate, purpose)
    if reservation is None:
        return None

    model_name = CONFIG.get("GEMINI_MODEL", "gemini-flash-latest")
    # The key goes in a header, not the URL, so it never appears in logs or proxies.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.35,
            "maxOutputTokens": get_int("GEMINI_MAX_OUTPUT_TOKENS"),
            "thinkingConfig": {"thinkingBudget": 50},
        },
    }

    try:
        # 429 / 5xx / connection errors are retried (those requests are not billed);
        # read timeouts are not, since the request may still have been processed and billed.
        data = request_json(
            "POST", url, headers={"x-goog-api-key": api_key}, body=payload,
            timeout=timeout, retries=2, backoff=1.5,
        ) or {}
    except urllib.error.HTTPError as e:
        # Error responses aren't billed: release the reservation.
        _settle_reservation(reservation, 0, 0, 0.0, "failed")
        log.error(f"Error calling Gemini API ({purpose}): HTTP {e.code} {e.reason}")
        return None
    except Exception as e:
        # Timeout / dropped connection: it may have been billed, so keep the reservation.
        _settle_reservation(reservation, 0, 0, estimate, "unknown")
        log.error(f"Error calling Gemini API ({purpose}): {type(e).__name__}: {e}")
        return None

    usage = data.get("usageMetadata", {}) or {}
    prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
    # Thinking tokens are billed as output
    candidates_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
    thoughts_tokens = int(usage.get("thoughtsTokenCount", 0) or 0)
    completion_tokens = candidates_tokens + thoughts_tokens
    cost = _cost_usd(prompt_tokens, completion_tokens)
    _settle_reservation(reservation, prompt_tokens, completion_tokens, cost, "final")
    log.info(
        f"Gemini {purpose} -> {prompt_tokens} in / {completion_tokens} out "
        f"(incl. {thoughts_tokens} thinking) | Cost: ${cost:.5f} | Month: ${get_current_month_usage_usd():.4f}"
    )

    candidates = data.get("candidates") or []
    if candidates:
        if candidates[0].get("finishReason") == "MAX_TOKENS":
            log.warning(f"Gemini {purpose} hit the {get_int('GEMINI_MAX_OUTPUT_TOKENS')}-token output cap; reply may be cut off.")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        for part in parts:
            if "text" in part and not part.get("thought"):
                parsed = _parse_json_lenient(part["text"])
                if parsed is not None:
                    return parsed
        if parts and "text" in parts[-1]:
            return _parse_json_lenient(parts[-1]["text"])
    return None


def call_ollama_json(prompt: str, purpose: str = "curation", timeout: Optional[int] = None) -> Optional[object]:
    """Call local Ollama instance with JSON format output ($0 cost)."""
    ollama_url = (CONFIG.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = CONFIG.get("OLLAMA_MODEL") or "hermes3:8b"
    if timeout is None:
        timeout = get_int("OLLAMA_TIMEOUT")

    url = f"{ollama_url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.35,
            "num_ctx": 4096,
        },
    }

    try:
        # One retry for connection errors (e.g. Ollama restarting); a slow model
        # load that hits the timeout is not retried, it falls back to Gemini instead.
        data = request_json("POST", url, body=payload, timeout=timeout, retries=1) or {}
        response_text = data.get("response", "")
        parsed = _parse_json_lenient(response_text)
        if parsed is not None:
            log.info(f"[Ollama:{model}] {purpose} -> OK ($0.00)")
            return parsed
    except Exception as e:
        log.error(f"Error calling local Ollama ({purpose}): {e}")
    return None


def call_llm_json(
    prompt: str,
    purpose: str = "curation",
    timeout: int = 20,
    provider_override: Optional[str] = None,
) -> Optional[object]:
    """
    Dispatch JSON generation to the configured LLM provider ('gemini' or 'ollama'),
    automatically falling back to the other provider on error.
    """
    primary = (provider_override or get_active_provider()).lower()
    if primary == "ollama":
        res = call_ollama_json(prompt, purpose=purpose)
        if res is not None:
            return res
        log.warning(f"↻ Ollama unavailable for '{purpose}', falling back to Gemini Flash...")
        return call_gemini_raw_json(prompt, purpose=purpose, timeout=timeout)
    else:
        res = call_gemini_raw_json(prompt, purpose=purpose, timeout=timeout)
        if res is not None:
            return res
        log.warning(f"↻ Gemini unavailable for '{purpose}', falling back to local Ollama...")
        return call_ollama_json(prompt, purpose=purpose)


# Keep backward-compatible name used by app.py / callers
call_gemini_json = call_llm_json


def sequence_flowing_playlist(
    channel_name: str,
    channel_desc: str,
    candidate_tracks: List[dict],
    target_count: int = 24,
) -> List[dict]:
    """
    Given a pool of candidate tracks from unplayed albums, ask the LLM
    to act as an expert DJ / music director: pick ~20 tracks (at most 1-2 per artist)
    and sequence them with seamless tempo, energy, and mood progression.
    """
    if not candidate_tracks:
        return []

    compact_candidates = []
    for t in candidate_tracks:
        compact_candidates.append({
            "key": str(t["ratingKey"]),
            "artist": t["artist"],
            "album": t["album"],
            "title": t["title"],
            "year": t.get("year"),
            "duration_sec": int((t.get("duration") or 0) / 1000),
        })

    prompt = f"""You are a master radio DJ and music curator for a high-end personal music station.
Channel: "{channel_name}"
Vibe description: {channel_desc}

Below is a pool of {len(compact_candidates)} candidate tracks from unplayed albums in the listener's library.

Task:
1. Select {min(target_count, len(compact_candidates))} tracks that best embody this channel's vibe.
2. Select at most 1 or 2 tracks per artist (ensure wide variety).
3. Sequence them so the playlist flows like a masterful mixtape or concert setlist (engaging opener, building groove, peak emotional/energetic tracks in the middle, satisfying wind-down).
4. Return a JSON object with this exact structure:
{{
  "liner_notes": "1-2 sentence compelling curatorial description of the set's flow",
  "sequenced_keys": ["ratingKey1", "ratingKey2"]
}}

Candidates:
{json.dumps(compact_candidates, separators=(',', ':'))}
"""

    res = call_llm_json(prompt, purpose=f"playlist_flow_{channel_name}")
    if isinstance(res, dict):
        key_map = {str(t["ratingKey"]): t for t in candidate_tracks}
        sequenced = []
        used = set()
        for k in _clean_str_list(res.get("sequenced_keys"), max_items=200):
            if k in key_map and k not in used:
                used.add(k)
                sequenced.append(key_map[k])
        if sequenced:
            log.info(f"✓ Sequenced {len(sequenced)} tracks for '{channel_name}'")
            liner = _clean_str(res.get("liner_notes"))
            if liner:
                log.info(f"Liner notes: \"{liner}\"")
            return sequenced[:max(target_count, 1)]

    # Fallback if LLM unavailable: balanced round-robin across artists
    log.warning("Falling back to heuristic track selection.")
    by_artist: Dict[str, List[dict]] = {}
    for t in candidate_tracks:
        by_artist.setdefault(t["artist"], []).append(t)
    fallback = []
    while len(fallback) < target_count and by_artist:
        for art in list(by_artist.keys()):
            if by_artist[art]:
                fallback.append(by_artist[art].pop(0))
            else:
                del by_artist[art]
            if len(fallback) >= target_count:
                break
    return fallback


def discover_new_artists_with_gemini(
    top_artists: List[str],
    existing_library_artists: List[str],
    count: int = 6,
) -> List[dict]:
    """
    Ask the LLM to find exciting discovery recommendations (stepping-stones)
    based on the user's favorite artists that are NOT already in their library.
    """
    sample_excluded = existing_library_artists[:200]
    prompt = f"""You are an elite record store owner and musical tastemaker.
The listener's heavy-rotation artists: {', '.join(top_artists[:12])}.
They love:
- Indie Pop, Indie Shimmer, Dream Pop
- Alt-Country, Outlaw picking, Appalachian acoustic soul
- Pop-Punk, Acoustic Punk, Cathartic Heartland Rock
- Indie Soul, Warm Analog Grooves, Funk
- Essential Post-Punk / New Wave Staples

Recommend {count} incredible, acclaimed albums to acquire that are stepping stones from their tastes into adjacent or deeper sounds.
CRITICAL: Do NOT recommend any of these artists (already in their library or crate):
{json.dumps(sample_excluded)}

Return a JSON object with a "recommendations" array:
{{
  "recommendations": [
    {{
      "artist": "Artist Name",
      "album": "Album Title",
      "year": 2024,
      "channel": "Alt & Prime Country",
      "reason": "1-2 sentence vivid description connecting their sound to the listener's tastes"
    }}
  ]
}}
Valid values for "channel": "Alt & Prime Country", "Pop & Acoustic Punk", "Indie Pop & Shimmer", "Indie Soul & Grooves", "The Canon Classics".
"""
    res = call_llm_json(prompt, purpose="discover_new_artists")
    if isinstance(res, dict):
        res = res.get("recommendations")
    return _clean_recommendations(res, count)


def _clean_recommendations(raw: Any, count: int) -> List[dict]:
    """Keep only well-formed {artist, album, ...} picks from LLM output, normalising field types."""
    from music_excavator import CHANNEL_DEFINITIONS

    if not isinstance(raw, list):
        return []
    valid_channels = {c["name"] for c in CHANNEL_DEFINITIONS.values()}
    cleaned: List[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        artist = _clean_str(item.get("artist"), 200)
        album = _clean_str(item.get("album"), 200)
        if not artist or not album:
            continue
        year_raw = item.get("year")
        year = None
        if isinstance(year_raw, (int, float)) and not isinstance(year_raw, bool):
            year = int(year_raw)
        elif isinstance(year_raw, str) and year_raw.strip()[:4].isdigit():
            year = int(year_raw.strip()[:4])
        if year is not None and not 1900 <= year <= 2100:
            year = None
        channel = _clean_str(item.get("channel"), 100)
        cleaned.append({
            "artist": artist,
            "album": album,
            "year": year,
            "genre": _clean_str(item.get("genre"), 100) or None,
            "channel": channel if channel in valid_channels else "General",
            "reason": _clean_str(item.get("reason"), 600) or None,
        })
        if len(cleaned) >= count:
            break
    return cleaned


def generate_playlist_from_user_prompt(
    user_prompt: str,
    target_count: int = 20,
    provider_override: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """
    Generate an on-demand, custom-curated Plex playlist from a freeform user prompt,
    mood, feeling, or reference songs using the configured LLM and the user's library.
    """
    report = progress or (lambda _msg: None)
    if provider_override not in _VALID_PROVIDERS:
        provider_override = None
    target_count = max(1, min(int(target_count), 100))
    from music_excavator import (
        create_or_replace_plex_playlist,
        get_recently_played_tracks,
        reindex_library_tracks,
    )

    conn = _get_conn()
    cur = conn.cursor()

    # Ensure library_tracks table is populated
    try:
        cur.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")
        library_artists = [r[0] for r in cur.fetchall()]
    except sqlite3.OperationalError:
        library_artists = []

    if not library_artists:
        conn.close()
        reindex_library_tracks()
        conn = _get_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")
            library_artists = [r[0] for r in cur.fetchall()]
        except sqlite3.OperationalError:
            # Table still missing: the re-index could not reach Plex.
            library_artists = []
        if not library_artists:
            conn.close()
            return None

    provider = provider_override or get_active_provider()
    report(f"Picking artists from your library with {provider.title()}…")

    # For local 8B Ollama, trim artist list to keep prompt compact & fast (~250 artists,
    # always keeping any artist whose name shares words with the user's prompt).
    if provider == "ollama" and len(library_artists) > 250:
        prompt_words = {
            w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", user_prompt)
        }
        priority_artists = [
            a for a in library_artists
            if any(w in a.lower() for w in prompt_words)
        ]
        remaining = [a for a in library_artists if a not in priority_artists]
        sampled_remaining = random.sample(remaining, min(len(remaining), 250 - len(priority_artists)))
        artist_pool_for_llm = priority_artists + sampled_remaining
    else:
        artist_pool_for_llm = library_artists

    prompt1 = f"""You are an elite music supervisor and DJ.
The listener's prompt or mood: "{user_prompt}"

Here is the list of {len(artist_pool_for_llm)} artists in their personal music collection:
{json.dumps(artist_pool_for_llm)}

Task:
1. Select 12-16 artists from this exact list that best fit this mood, sound, or reference.
2. If the user mentioned specific artists or bands that exist in their library, definitely include them.
3. Devise an evocative, creative playlist title (e.g. "Front Porch Fire", "Midnight Reverie", "Echoes & Embers").
4. Write a 1-sentence poetic liner note describing the musical arc.

Return JSON:
{{
  "title": "Creative Playlist Title",
  "vibe": "1 sentence description",
  "matched_artists": ["Artist 1", "Artist 2"]
}}
"""
    res1 = call_llm_json(prompt1, purpose="prompt_match_artists", provider_override=provider)
    matched_artists = _clean_str_list(res1.get("matched_artists") if isinstance(res1, dict) else None, max_items=30)
    if not matched_artists:
        conn.close()
        return None

    title = _clean_str(res1.get("title"), 120) or "🎯 Crate: Mood Selection"
    if not any(char in title for char in ["🎯", "✨", "🌙", "⚡", "🌾", "☕", "🔥"]):
        title = f"✨ Crate: {title}"
    vibe = _clean_str(res1.get("vibe"), 400) or user_prompt
    report(f"Gathering tracks from {len(matched_artists)} artists…")

    recent_keys, recent_pairs = get_recently_played_tracks()

    # Step 2: Gather randomized, fresh candidate tracks for matched artists
    candidates = []
    seen_candidate_keys = set()

    for art in matched_artists:
        cur.execute(
            """
            SELECT rating_key, artist, title, album
            FROM library_tracks
            WHERE artist = ? COLLATE NOCASE
            ORDER BY RANDOM()
            LIMIT 12
            """,
            (art,),
        )
        rows = cur.fetchall()
        if not rows:
            # Try substring match if LLM slightly altered artist punctuation
            cur.execute(
                """
                SELECT rating_key, artist, title, album
                FROM library_tracks
                WHERE lower(artist) LIKE ? ESCAPE '\\'
                ORDER BY RANDOM()
                LIMIT 10
                """,
                (f"%{_escape_like(art.lower())}%",),
            )
            rows = cur.fetchall()

        fresh_rows = [
            r for r in rows
            if str(r["rating_key"]) not in recent_keys
            and ((r["artist"] or "").strip().lower(), (r["title"] or "").strip().lower()) not in recent_pairs
        ]
        chosen_rows = (fresh_rows if len(fresh_rows) >= 2 else rows)[:6]

        for r in chosen_rows:
            rk = str(r["rating_key"])
            if rk not in seen_candidate_keys:
                seen_candidate_keys.add(rk)
                candidates.append({
                    "key": rk,
                    "artist": r["artist"],
                    "title": r["title"],
                    "album": r["album"],
                })

    conn.close()

    if not candidates:
        return None

    # Shuffle candidate list before passing to Step 3 so ordering isn't grouped by artist
    random.shuffle(candidates)

    # Step 3: Ask LLM to curate the best subset & sequence the flow
    prompt2 = f"""You are sequencing a mixtape.
User's request: "{user_prompt}"
Playlist title: "{title}"
Vibe: {vibe}

Candidate songs available in their library:
{json.dumps(candidates, separators=(',', ':'))}

Task:
Select the best {min(target_count, len(candidates))} tracks (max 2 per artist for variety) and order them for seamless emotional and dynamic energy flow (opening hook -> building pace -> emotional peak -> satisfying resolution).

Return JSON:
{{
  "sequenced_keys": ["key1", "key2"]
}}
"""
    report(f"Sequencing {len(candidates)} candidate tracks…")
    res2 = call_llm_json(prompt2, purpose="prompt_sequence_flow", provider_override=provider)
    key_dict = {c["key"]: c for c in candidates}
    sequenced_keys: List[str] = []
    raw_keys = res2.get("sequenced_keys") if isinstance(res2, dict) else None
    for k in _clean_str_list(raw_keys, max_items=200):
        if k in key_dict and k not in sequenced_keys:
            sequenced_keys.append(k)
    sequenced_keys = sequenced_keys[:target_count]

    # Fallback: balanced round-robin if LLM returned too few keys
    if len(sequenced_keys) < min(8, len(candidates)):
        for c in candidates:
            if c["key"] not in sequenced_keys:
                sequenced_keys.append(c["key"])
            if len(sequenced_keys) >= target_count:
                break

    # Step 4: Update dedicated playlist in Plex IN-PLACE (preserves ratingKey)
    dedicated_playlist_name = "✨ Crate: The Daily Vibe"
    report("Updating “✨ Crate: The Daily Vibe” in Plex…")
    pl = create_or_replace_plex_playlist(dedicated_playlist_name, sequenced_keys)
    if pl:
        return {
            "title": dedicated_playlist_name,
            "creative_title": title,
            "vibe": vibe,
            "track_count": pl["leafCount"],
            "tracks": [key_dict[k] for k in sequenced_keys if k in key_dict],
            "rating_keys": sequenced_keys,
            "rating_key": pl["ratingKey"],
            "provider": provider,
        }
    return None
