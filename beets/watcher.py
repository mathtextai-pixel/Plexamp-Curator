#!/usr/bin/env python3
"""
Beets Auto-Importer Watcher for Deemix downloads.
Watches INBOX_DIR for settled album folders, runs `beet import -q`, cleans up
imported directories, moves skipped items to NEEDS_REVIEW_DIR, and notifies
Plex and Crate Excavator on successful imports.
"""

import argparse
import datetime
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [beets-watcher] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("beets-watcher")

INBOX_DIR = os.environ.get("INBOX_DIR", "/inbox")
NEEDS_REVIEW_DIR = os.environ.get("NEEDS_REVIEW_DIR", "/inbox/_needs_review")
BEETS_CONFIG = os.environ.get("BEETS_CONFIG", "/app/config.yaml")
AUTO_IMPORT = os.environ.get("AUTO_IMPORT", "0").strip()
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "60"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
MUSIC_SECTION_ID = os.environ.get("MUSIC_SECTION_ID", "2")
CRATE_URL = os.environ.get("CRATE_URL", "http://crate-excavator:5050").rstrip("/")

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".ogg", ".opus",
    ".wav", ".aiff", ".aac", ".alac", ".wma",
}


def find_audio_files(folder: str) -> List[str]:
    """Recursively find all audio files inside `folder`."""
    found: List[str] = []
    if not os.path.isdir(folder):
        return found
    for root, _dirs, files in os.walk(folder):
        for fname in files:
            if fname.startswith("._"):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in AUDIO_EXTS:
                found.append(os.path.join(root, fname))
    return found


def newest_mtime(folder: str) -> float:
    """Return the most recent modification time across `folder` and all descendants."""
    latest = 0.0
    try:
        latest = max(latest, os.path.getmtime(folder))
    except OSError:
        pass
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            p = os.path.join(root, name)
            try:
                latest = max(latest, os.path.getmtime(p))
            except OSError:
                pass
    return latest


def notify_plex_and_crate() -> None:
    """Trigger Plex music library scan and Crate Excavator re-index (best-effort)."""
    if PLEX_URL and PLEX_TOKEN:
        plex_refresh_url = f"{PLEX_URL}/library/sections/{MUSIC_SECTION_ID}/refresh"
        req = urllib.request.Request(
            plex_refresh_url,
            headers={"X-Plex-Token": PLEX_TOKEN},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                log.info(f"✓ Triggered Plex library scan (HTTP {resp.status})")
        except Exception as e:
            log.warning(f"Could not trigger Plex library scan: {e}")
    else:
        log.warning("PLEX_URL or PLEX_TOKEN not set; skipping Plex scan trigger.")

    time.sleep(8)

    if CRATE_URL:
        crate_reindex_url = f"{CRATE_URL}/api/reindex"
        req = urllib.request.Request(
            crate_reindex_url,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "CrateExcavator",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
                log.info(f"✓ Triggered Crate Excavator re-index (HTTP {resp.status}): {body[:200]}")
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="replace") if he.fp else str(he)
            log.warning(f"Crate Excavator re-index returned HTTP {he.code}: {body[:200]}")
        except Exception as e:
            log.warning(f"Could not trigger Crate Excavator re-index: {e}")


def _write_review_note(dest_folder: str, proc: subprocess.CompletedProcess) -> None:
    """Save a brief note explaining why Beets did not auto-import this folder."""
    note_path = os.path.join(dest_folder, "_beets_note.txt")
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"Beets auto-import skipped this folder at {now}.\n"
        f"Return code: {proc.returncode}\n\n"
        f"--- stdout (truncated) ---\n{stdout[-2000:]}\n\n"
        f"--- stderr (truncated) ---\n{stderr[-2000:]}\n"
    )
    try:
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        log.warning(f"Could not write _beets_note.txt in {dest_folder}: {e}")


def process_folder(folder_path: str, trigger_refresh: bool = True) -> str:
    """
    Run `beet import -q` on `folder_path`.
    If all audio files were moved, remove remaining clutter and optionally notify Plex/Crate.
    If audio files remain, move the folder to NEEDS_REVIEW_DIR with a diagnostic note.
    """
    folder_path = os.path.abspath(folder_path)
    name = os.path.basename(folder_path.rstrip(os.sep))
    if not os.path.isdir(folder_path):
        log.error(f"Folder does not exist: {folder_path}")
        return "missing"

    if not find_audio_files(folder_path):
        log.info(f"No audio files in {name}; skipping.")
        return "empty"

    log.info(f"Importing '{name}' with Beets...")
    os.makedirs("/beets-data", exist_ok=True)
    proc = subprocess.run(
        ["beet", "-c", BEETS_CONFIG, "import", "-q", folder_path],
        capture_output=True,
        text=True,
    )

    remaining = find_audio_files(folder_path)
    if not remaining:
        shutil.rmtree(folder_path, ignore_errors=True)
        log.info(f"✓ Imported {name}")
        if trigger_refresh:
            notify_plex_and_crate()
        return "imported"

    # Low-confidence skip or partial import: quarantine to _needs_review
    os.makedirs(NEEDS_REVIEW_DIR, exist_ok=True)
    dest = os.path.join(NEEDS_REVIEW_DIR, name)
    if os.path.exists(dest):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(NEEDS_REVIEW_DIR, f"{name}_{ts}")

    shutil.move(folder_path, dest)
    _write_review_note(dest, proc)
    log.warning(f"⚠ Moved {name} to _needs_review/ ({len(remaining)} audio file(s) remaining)")
    return "needs_review"


def run_watch_loop() -> None:
    """Continuously watch INBOX_DIR for settled download folders when AUTO_IMPORT == '1'."""
    if AUTO_IMPORT != "1":
        log.info(
            "beets-importer is running in standby mode (AUTO_IMPORT=%r). "
            "Set BEETS_AUTO_IMPORT=1 to enable automatic inbox sweeping.",
            AUTO_IMPORT,
        )
        while True:
            time.sleep(60)

    log.info(
        f"beets-importer watching {INBOX_DIR} (settle={SETTLE_SECONDS}s, poll={POLL_INTERVAL}s)"
    )
    while True:
        try:
            entries = sorted(os.listdir(INBOX_DIR))
        except Exception as e:
            log.error(f"Error listing {INBOX_DIR}: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        now = time.time()
        imported_any = False

        for entry in entries:
            if entry.startswith(".") or entry.startswith("_"):
                continue
            full_path = os.path.join(INBOX_DIR, entry)
            if not os.path.isdir(full_path):
                continue
            if not find_audio_files(full_path):
                continue
            age = now - newest_mtime(full_path)
            if age < SETTLE_SECONDS:
                log.debug(f"Waiting for '{entry}' to settle ({age:.0f}s < {SETTLE_SECONDS}s)")
                continue

            status = process_folder(full_path, trigger_refresh=False)
            if status == "imported":
                imported_any = True

        if imported_any:
            notify_plex_and_crate()

        time.sleep(POLL_INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Beets auto-importer watcher")
    parser.add_argument(
        "--once",
        metavar="FOLDER",
        help="Import a single folder through the watcher pipeline and exit.",
    )
    args = parser.parse_args()

    if args.once:
        res = process_folder(args.once, trigger_refresh=True)
        sys.exit(0 if res in ("imported", "needs_review") else 1)

    run_watch_loop()


if __name__ == "__main__":
    main()
