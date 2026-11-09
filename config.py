"""
Central configuration for Crate Excavator.

Precedence (lowest -> highest):
  1. Built-in defaults below (no secrets)
  2. A `.env` file next to this module (KEY=VALUE lines), if present
  3. Real environment variables (e.g. from docker compose `env_file`)

Secrets (tokens / API keys) have NO defaults and must come from `.env` or the environment.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_DEFAULTS = {
    # Plex / Tautulli / Lidarr / Deemix (server-side URLs used by the app itself)
    "PLEX_URL": "http://127.0.0.1:32400",
    "MUSIC_SECTION_ID": "2",
    "TAUTULLI_URL": "http://127.0.0.1:8181",
    "LIDARR_URL": "http://127.0.0.1:8686",
    "DEEMIX_URL": "http://127.0.0.1:6595",
    # Browser-facing URLs (used for links rendered in the UI)
    "PUBLIC_HOST": "http://127.0.0.1",
    # Lidarr add options (blank = auto-detect via Lidarr API)
    "LIDARR_ROOT_FOLDER": "",
    "LIDARR_QUALITY_PROFILE_ID": "",
    "LIDARR_METADATA_PROFILE_ID": "",
    # LLM
    "LLM_PROVIDER": "gemini",          # "gemini" or "ollama" (ollama falls back to gemini)
    "GEMINI_MODEL": "gemini-flash-latest",
    "GEMINI_PRICE_IN_PER_1M": "0.50",   # USD per 1M input tokens  (verify against your billing page)
    "GEMINI_PRICE_OUT_PER_1M": "3.00",  # USD per 1M output tokens (thinking tokens billed as output)
    "GEMINI_MONTHLY_BUDGET_USD": "1.50",   # UTC calendar month
    "GEMINI_MAX_OUTPUT_TOKENS": "4096",    # per call, thinking included; also used for the worst-case budget estimate
    "OLLAMA_URL": "http://127.0.0.1:11434",
    "OLLAMA_MODEL": "hermes3:8b",
    "OLLAMA_TIMEOUT": "60",
    # Storage
    "DATA_DIR": os.path.join(BASE_DIR, "data"),
    # Lock files: must be on a filesystem that enforces flock (default: system temp dir; see locks.py)
    "LOCK_DIR": "",
    # Scheduler
    "RUN_SCHEDULER": "1",
    "WEEKLY_REFRESH_CRON": "0 9 * * mon",   # minute hour day month day_of_week
    "NIGHTLY_REINDEX_CRON": "30 3 * * *",
    "WEEKLY_DISCOVERY_CRON": "30 9 * * mon",
    "DISCOVERY_PICKS_PER_WEEK": "6",
    # Freshness: skip tracks played within this many days when building playlists
    "RECENTLY_PLAYED_DAYS": "14",
    # Web
    "PORT": "5050",
    # Logging: DEBUG, INFO, WARNING, ERROR
    "LOG_LEVEL": "INFO",
}

_SECRETS = ("PLEX_TOKEN", "TAUTULLI_KEY", "LIDARR_API_KEY", "GEMINI_API_KEY")
_OPTIONAL = ("PLEX_PUBLIC_URL", "TAUTULLI_PUBLIC_URL", "LIDARR_PUBLIC_URL", "DEEMIX_PUBLIC_URL")


def _load_dotenv(path: str) -> dict:
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def _build() -> dict:
    cfg = dict(_DEFAULTS)
    cfg.update(_load_dotenv(os.path.join(BASE_DIR, ".env")))
    for key in list(_DEFAULTS) + list(_SECRETS) + list(_OPTIONAL):
        if key in os.environ:
            cfg[key] = os.environ[key]
    for key in _SECRETS:
        cfg.setdefault(key, "")
    # Derived browser-facing service links
    host = cfg["PUBLIC_HOST"].rstrip("/")
    cfg.setdefault("PLEX_PUBLIC_URL", f"{host}:32400")
    cfg.setdefault("TAUTULLI_PUBLIC_URL", f"{host}:8181")
    cfg.setdefault("LIDARR_PUBLIC_URL", f"{host}:8686")
    cfg.setdefault("DEEMIX_PUBLIC_URL", f"{host}:6595")
    return cfg


CONFIG = _build()


def get(key: str, default=None):
    return CONFIG.get(key, default)


def get_float(key: str) -> float:
    return float(CONFIG[key])


def get_int(key: str) -> int:
    return int(CONFIG[key])


def public_config() -> dict:
    """Non-secret values that are safe to hand to templates."""
    return {k: v for k, v in CONFIG.items() if k not in _SECRETS}


def missing_secrets() -> list:
    return [k for k in _SECRETS if not CONFIG.get(k)]
