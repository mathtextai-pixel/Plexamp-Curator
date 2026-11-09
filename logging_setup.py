"""Log configuration shared by the web app and the standalone weekly_refresh.py script."""

import logging
import os
import sys

from config import CONFIG

_configured = False


def configure_logging() -> None:
    """Timestamped logs to stderr (captured by `docker logs`). Level from LOG_LEVEL (default INFO)."""
    global _configured
    if _configured:
        return
    level_name = str(CONFIG.get("LOG_LEVEL") or os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s [%(name)s] [pid %(process)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _configured = True
