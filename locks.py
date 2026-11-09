"""
Cross-process file locks (fcntl.flock) under LOCK_DIR.

LOCK_DIR defaults to a directory under the system temp dir, NOT DATA_DIR: on Docker
Desktop for Mac the ./data bind mount does not enforce file locks between processes
(each worker sees its own copy of the lock), so locks kept there silently do nothing.
All processes that need these locks (Gunicorn workers, the scheduler thread, a
`docker exec ... weekly_refresh.py`) run in the same container and share its /tmp.

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
import tempfile
import time
from typing import IO, Iterator, Optional

from config import CONFIG


class LockBusy(Exception):
    """Raised when a lock could not be acquired in time."""


def lock_dir() -> str:
    path = CONFIG.get("LOCK_DIR") or os.path.join(tempfile.gettempdir(), "crate-excavator-locks")
    os.makedirs(path, exist_ok=True)
    return path


def _lock_path(name: str) -> str:
    lock_dir_path = lock_dir()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "lock"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return os.path.join(lock_dir_path, f"{slug}-{digest}.lock")


class LockHandle:
    """An acquired lock. May be released from a different thread than the one that took it."""

    def __init__(self, name: str, fd: IO[str]):
        self.name = name
        self._fd: Optional[IO[str]] = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


def acquire(name: str, timeout: Optional[float] = None, poll: float = 0.25) -> LockHandle:
    """
    Take an exclusive lock named `name`.
      timeout=None -> wait indefinitely
      timeout=0    -> fail immediately with LockBusy if held elsewhere
      timeout=N    -> wait up to N seconds, then raise LockBusy
    """
    fd = open(_lock_path(name), "a")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return LockHandle(name, fd)
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                fd.close()
                raise LockBusy(name)
            time.sleep(poll)
        except BaseException:
            fd.close()
            raise


@contextlib.contextmanager
def file_lock(name: str, timeout: Optional[float] = None, poll: float = 0.25) -> Iterator[None]:
    """Hold the lock named `name` for the duration of the block (see `acquire` for timeout)."""
    handle = acquire(name, timeout=timeout, poll=poll)
    try:
        yield
    finally:
        handle.release()


# Job-level locks shared by the API endpoints and the scheduler.
REFRESH_JOB_LOCK = "job:refresh-playlists"   # channel / vault playlist refreshes
REINDEX_JOB_LOCK = "job:reindex"
LIDARR_SYNC_JOB_LOCK = "job:lidarr-sync"
DISCOVERY_JOB_LOCK = "job:discovery"
