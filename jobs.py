"""
Background jobs for long-running actions (playlist refreshes, AI DJ prompts,
Lidarr sync, discovery, re-index).

The HTTP request only starts the job and returns 202 + job_id; the work runs in a
daemon thread in that worker. Job state lives in SQLite so a status poll can be
answered by any Gunicorn worker:

    GET /api/jobs/<id>  ->  {"status": "running" | "done" | "error", "progress": ..., "result": {...}}

A job function receives a `progress(message)` callback and returns
(http_status, payload_dict) -- the same response the old synchronous endpoint sent.
"""

import json
import logging
import os
import sqlite3
import threading
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

import db
from locks import LockHandle

log = logging.getLogger(__name__)

# A job still "running" after this long is reported as stale (its worker most likely died).
STALE_AFTER_SECONDS = 2 * 60 * 60

JobResult = Tuple[int, Dict[str, Any]]
JobFn = Callable[[Callable[[str], None]], JobResult]


def _conn() -> sqlite3.Connection:
    return db.connect()


def _update(job_id: str, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE jobs SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*fields.values(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def start_job(kind: str, fn: JobFn, lock: Optional[LockHandle] = None) -> str:
    """
    Record a new job and run `fn` in a background thread.
    If `lock` is given, it is released when the job finishes (the caller acquires it
    first so it can answer 409 synchronously when the lock is busy).
    """
    job_id = uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO jobs (id, kind, status, progress, pid, owner) VALUES (?, ?, 'running', ?, ?, ?)",
            (job_id, kind, "Starting…", os.getpid(), process_identity(os.getpid())),
        )
        conn.commit()
    finally:
        conn.close()

    def progress(message: str) -> None:
        try:
            _update(job_id, progress=str(message)[:300])
        except sqlite3.Error as e:
            log.error(f"could not record progress for {kind}: {e}")

    def runner() -> None:
        try:
            http_status, payload = fn(progress)
            _update(job_id, status="done", http_status=int(http_status), result=json.dumps(payload))
        except Exception as e:
            log.exception(f"Job {kind} failed")
            _update(
                job_id,
                status="error",
                http_status=500,
                result=json.dumps({"success": False, "message": f"{kind} failed: {e}"}),
            )
        finally:
            if lock is not None:
                lock.release()

    threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
    return job_id


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_identity(pid: int) -> Optional[str]:
    """
    "pid:start-time" for a running process, or None if it isn't running.
    The start time (from /proc on Linux) tells a restarted worker apart from the old one:
    with a single Gunicorn worker the pid is the same after every container restart, so a
    pid alone would make jobs interrupted by a restart look alive. Without /proc (macOS
    dev runs) this falls back to the pid.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        # Field 22 (starttime) comes after the parenthesised command name, which may contain spaces.
        start_time = stat.rsplit(")", 1)[1].split()[19]
        return f"{pid}:{start_time}"
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return None  # Linux, and the process is gone
        return str(pid) if _pid_alive(pid) else None
    except (OSError, IndexError):
        return str(pid) if _pid_alive(pid) else None


def _owner_alive(job: Dict[str, Any]) -> bool:
    if job.get("owner"):
        return process_identity(job["pid"]) == job["owner"]
    return _pid_alive(job["pid"])  # rows created before the owner column existed


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT *, (strftime('%s','now') - strftime('%s', updated_at)) AS age_seconds
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None

    job = dict(row)
    if job["status"] == "running" and (
        not _owner_alive(job) or (job["age_seconds"] or 0) > STALE_AFTER_SECONDS
    ):
        message = "This job was interrupted (the server restarted or the worker stopped). Please try again."
        _update(job_id, status="error", http_status=500,
                result=json.dumps({"success": False, "message": message}))
        job.update(status="error", http_status=500, result=json.dumps({"success": False, "message": message}))

    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": job["progress"],
        "http_status": job["http_status"],
        "result": json.loads(job["result"]) if job["result"] else None,
    }

