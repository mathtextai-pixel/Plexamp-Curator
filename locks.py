"""
Cross-process file locks (fcntl.flock) under DATA_DIR/locks.

Gunicorn runs several worker processes plus the in-process scheduler thread, so
an in-memory threading.Lock is not enough to stop two runs from rewriting the
same Plex playlist at once. flock locks belong to the open file description, so
they also exclude other threads in the same process that open the file separately.
"""

import contextlib
import fcntl
import hashlib
import os
import re
import time
from typing import Iterator, Optional

from config import CONFIG


class LockBusy(Exception):
    """Raised when a lock could not be acquired in time."""


def _lock_path(name: str) -> str:
    lock_dir = os.path.join(CONFIG["DATA_DIR"], "locks")
    os.makedirs(lock_dir, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "lock"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return os.path.join(lock_dir, f"{slug}-{digest}.lock")


@contextlib.contextmanager
def file_lock(name: str, timeout: Optional[float] = None, poll: float = 0.25) -> Iterator[None]:
    """
    Hold an exclusive lock named `name` for the duration of the block.
      timeout=None -> wait indefinitely
      timeout=0    -> fail immediately with LockBusy if held elsewhere
      timeout=N    -> wait up to N seconds, then raise LockBusy
    """
    fd = open(_lock_path(name), "a")
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockBusy(name)
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


# Job-level lock shared by the manual "refresh all" endpoints and the weekly scheduler.
REFRESH_JOB_LOCK = "job:refresh-playlists"
