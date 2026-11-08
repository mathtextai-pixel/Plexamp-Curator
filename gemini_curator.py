#!/usr/bin/env python3
"""
Gemini & Local Ollama Curator: Sequences cohesive, flowing playlists
from Plex library tracks and generates external music discovery recommendations,
with a strict SQLite-based monthly Gemini budget limiter (< $1.50/mo) and
automatic fallback between Gemini Flash and local Ollama (hermes3:8b).
"""

import datetime
import json
import os
import random
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from config import CONFIG, get_float, get_int

DB_PATH = os.path.join(CONFIG["DATA_DIR"], "recommendations.db")
_ACTIVE_PROVIDER: str = (CONFIG.get("LLM_PROVIDER") or "gemini").strip().lower()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_budget_table():
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gemini_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd REAL,
            purpose TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_budget_table()


def get_active_provider() -> str:
    return _ACTIVE_PROVIDER if _ACTIVE_PROVIDER in ("gemini", "ollama") else "gemini"


def set_active_provider(provider: str) -> str:
    global _ACTIVE_PROVIDER
    p = (provider or "").strip().lower()
    if p in ("gemini", "ollama"):
        _ACTIVE_PROVIDER = p
    return _ACTIVE_PROVIDER


def get_current_month_usage_usd() -> float:
    """Return total cost spent on Gemini calls this calendar month."""
    conn = _get_conn()
    cur = conn.cursor()
    first_of_month = datetime.datetime.now().strftime("%Y-%m-01 00:00:00")
    cur.execute(
        "SELECT SUM(cost_usd) FROM gemini_usage WHERE timestamp >= ?",
        (first_of_month,),
    )
    res = cur.fetchone()[0]
    conn.close()
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


def record_usage(prompt_tokens: int, completion_tokens: int, purpose: str) -> float:
    price_in = get_float("GEMINI_PRICE_IN_PER_1M")
    price_out = get_float("GEMINI_PRICE_OUT_PER_1M")
    cost = (prompt_tokens / 1_000_000.0) * price_in + (
        completion_tokens / 1_000_000.0
    ) * price_out
    conn = _get_conn()
    conn.execute(
        "INSERT INTO gemini_usage (prompt_tokens, completion_tokens, cost_usd, purpose) VALUES (?, ?, ?, ?)",
        (prompt_tokens, completion_tokens, cost, purpose),
    )
    conn.commit()
    conn.close()
    return cost


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


def call_gemini_raw_json(prompt: str, purpose: str = "curation", timeout: int = 20) -> Optional[object]:
    """Call Google Gemini Flash with structured JSON output, 50-token thinking budget, and budget cap."""
    api_key = CONFIG.get("GEMINI_API_KEY", "")
    if not api_key:
        print("Gemini API key is not configured.")
        return None

    budget = get_float("GEMINI_MONTHLY_BUDGET_USD")
    current_spend = get_current_month_usage_usd()
    if current_spend >= budget:
        print(
            f"⚠️ Monthly Gemini budget limit reached (${current_spend:.4f} / ${budget:.2f}). "
            "Skipping Gemini call."
        )
        return None

    model_name = CONFIG.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.35,
            "thinkingConfig": {"thinkingBudget": 50},
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        usage = data.get("usageMetadata", {})
        prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
        # Include thoughtsTokenCount in output tokens for accurate billing
        candidates_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
        thoughts_tokens = int(usage.get("thoughtsTokenCount", 0) or 0)
        completion_tokens = candidates_tokens + thoughts_tokens

        cost = record_usage(prompt_tokens, completion_tokens, purpose)
        print(
            f"  [Gemini] {purpose} -> {prompt_tokens} in / {completion_tokens} out "
            f"(incl. {thoughts_tokens} thinking) | Cost: ${cost:.5f} | Month: ${current_spend + cost:.4f}"
        )

        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part and not part.get("thought"):
                    parsed = _parse_json_lenient(part["text"])
                    if parsed is not None:
                        return parsed
            if parts and "text" in parts[-1]:
                return _parse_json_lenient(parts[-1]["text"])
    except Exception as e:
        print(f"Error calling Gemini API ({purpose}): {e}")
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

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        response_text = data.get("response", "")
        parsed = _parse_json_lenient(response_text)
        if parsed is not None:
            print(f"  [Ollama:{model}] {purpose} -> OK ($0.00)")
            return parsed
    except Exception as e:
        print(f"Error calling local Ollama ({purpose}): {e}")
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
        print(f"  ↻ Ollama unavailable for '{purpose}', falling back to Gemini Flash...")
        return call_gemini_raw_json(prompt, purpose=purpose, timeout=timeout)
    else:
        res = call_gemini_raw_json(prompt, purpose=purpose, timeout=timeout)
        if res is not None:
            return res
        print(f"  ↻ Gemini unavailable for '{purpose}', falling back to local Ollama...")
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
    if isinstance(res, dict) and res.get("sequenced_keys"):
        key_map = {str(t["ratingKey"]): t for t in candidate_tracks}
        sequenced = []
        for k in res["sequenced_keys"]:
            if str(k) in key_map and key_map[str(k)] not in sequenced:
                sequenced.append(key_map[str(k)])
        if sequenced:
            print(f"  ✓ Sequenced {len(sequenced)} tracks for '{channel_name}'")
            if res.get("liner_notes"):
                print(f"    Liner notes: \"{res['liner_notes']}\"")
            return sequenced

    # Fallback if LLM unavailable: balanced round-robin across artists
    print("  Falling back to heuristic track selection.")
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
    if isinstance(res, list):
        return res
    if isinstance(res, dict) and isinstance(res.get("recommendations"), list):
        return res["recommendations"]
    return []


def generate_playlist_from_user_prompt(
    user_prompt: str,
    target_count: int = 20,
    provider_override: Optional[str] = None,
) -> Optional[dict]:
    """
    Generate an on-demand, custom-curated Plex playlist from a freeform user prompt,
    mood, feeling, or reference songs using the configured LLM and the user's library.
    """
    from music_excavator import (
        create_or_replace_plex_playlist,
        get_recently_played_tracks,
        reindex_library_tracks,
    )

    conn = _get_conn()
    conn.row_factory = sqlite3.Row
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
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''")
        library_artists = [r[0] for r in cur.fetchall()]
        if not library_artists:
            conn.close()
            return None

    provider = (provider_override or get_active_provider()).lower()

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
    if not isinstance(res1, dict) or not res1.get("matched_artists"):
        conn.close()
        return None

    title = res1.get("title") or "🎯 Crate: Mood Selection"
    if not any(char in title for char in ["🎯", "✨", "🌙", "⚡", "🌾", "☕", "🔥"]):
        title = f"✨ Crate: {title}"
    vibe = res1.get("vibe") or user_prompt

    recent_keys, recent_pairs = get_recently_played_tracks()

    # Step 2: Gather randomized, fresh candidate tracks for matched artists
    candidates = []
    seen_candidate_keys = set()

    for art in res1["matched_artists"]:
        cur.execute(
            """
            SELECT rating_key, artist, title, album
            FROM library_tracks
            WHERE lower(artist) = lower(?)
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
                WHERE lower(artist) LIKE ?
                ORDER BY RANDOM()
                LIMIT 10
                """,
                (f"%{art.lower()}%",),
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
    res2 = call_llm_json(prompt2, purpose="prompt_sequence_flow", provider_override=provider)
    key_dict = {c["key"]: c for c in candidates}
    sequenced_keys = []
    if isinstance(res2, dict) and res2.get("sequenced_keys"):
        for k in res2["sequenced_keys"]:
            if str(k) in key_dict and str(k) not in sequenced_keys:
                sequenced_keys.append(str(k))

    # Fallback: balanced round-robin if LLM returned too few keys
    if len(sequenced_keys) < min(8, len(candidates)):
        for c in candidates:
            if c["key"] not in sequenced_keys:
                sequenced_keys.append(c["key"])
            if len(sequenced_keys) >= target_count:
                break

    # Step 4: Update dedicated playlist in Plex IN-PLACE (preserves ratingKey)
    dedicated_playlist_name = "✨ Crate: The Daily Vibe"
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
            "provider": get_active_provider() if not provider_override else provider_override,
        }
    return None
