"""
Shared HTTP helper for calls to Plex, Tautulli, Lidarr, Deezer, Gemini and Ollama.

One place for timeouts, JSON decoding and retries:
  - Connection errors (refused, DNS, reset) and HTTP 429 / 5xx are retried with
    exponential backoff, up to `retries` extra attempts.
  - Read timeouts are retried only when `retry_on_timeout=True`. Leave it off for
    calls where the server may have done the work even though we gave up waiting
    (e.g. Plex `PUT /playlists/{id}/items`, which appends, or billed LLM calls).
  - Other HTTP errors (4xx) are raised immediately as `urllib.error.HTTPError`.

Failures are raised, never swallowed, so callers can tell "request failed"
apart from "empty result".
"""

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_sleep = time.sleep  # backoff sleep; tests replace this to run instantly
DEFAULT_USER_AGENT = "CrateExcavator/1.0"


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _retry_after_seconds(err: urllib.error.HTTPError) -> Optional[float]:
    value = err.headers.get("Retry-After") if err.headers else None
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


def request_raw(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    timeout: float = 10,
    retries: int = 2,
    retry_on_timeout: bool = False,
    backoff: float = 0.75,
    max_backoff: float = 8.0,
) -> bytes:
    """
    Perform an HTTP request and return the raw response body.
    `body` may be bytes, or any JSON-serialisable object (sent as application/json).
    """
    hdrs = {"User-Agent": DEFAULT_USER_AGENT}
    hdrs.update(headers or {})
    data: Optional[bytes] = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS or attempt >= retries:
                raise
            delay = _retry_after_seconds(e)
        except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as e:
            if attempt >= retries or (_is_timeout(e) and not retry_on_timeout):
                raise
            delay = None
        attempt += 1
        if delay is None:
            delay = backoff * (2 ** (attempt - 1))
        _sleep(min(delay, max_backoff))


def request_json(method: str, url: str, **kwargs) -> Any:
    """Like `request_raw`, but decode the body as JSON. Returns None for an empty body."""
    raw = request_raw(method, url, **kwargs)
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return json.loads(text)


def get_json(url: str, *, retry_on_timeout: bool = True, **kwargs) -> Any:
    """GET is idempotent, so timeouts are retried by default."""
    return request_json("GET", url, retry_on_timeout=retry_on_timeout, **kwargs)


def http_error_body(err: urllib.error.HTTPError) -> str:
    """Best-effort read of an HTTPError's response body for error messages."""
    try:
        return err.read().decode("utf-8", errors="replace") if err.fp else str(err)
    except Exception:
        return str(err)
