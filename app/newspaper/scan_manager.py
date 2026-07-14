"""Runs newspaper scans as a background subprocess so the UI never blocks and
Playwright stays stable.

Why a subprocess and not a thread: Playwright's sync API is unreliable when
driven from a thread spawned under the async web server (the browser can close
mid-scan). Running the scan as its own process — `python -m scripts.run_scan` —
puts Playwright on a process main thread, where it's solid. The web app polls
the process and reads the result the script writes to data/last_scan.json.

Only one scan runs at a time (a second request while one runs is ignored).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading

from config import BASE_DIR

logger = logging.getLogger(__name__)

_STATUS_FILE = BASE_DIR / "data" / "last_scan.json"

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_current_label: str | None = None


_LOG = BASE_DIR / "data" / "scan_subprocess.log"


def _detached_kwargs() -> dict:
    """Launch the scan in its own process group with output captured to a log
    file (so failures are diagnosable). CREATE_NEW_PROCESS_GROUP isolates it from
    the web server's console signals without the instability of DETACHED_PROCESS."""
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_LOG, "a", encoding="utf-8")
    kwargs = {"stdout": fh, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def start_scan(
    keyword_ids: list[int] | None = None,
    keyword_label: str | None = None,
    capped: bool = False,
    backfill_only: bool = False,
) -> bool:
    """Launch a scan as a subprocess. False if one is already running.

    capped=False (manual): crawl the whole front page.
    capped=True (scheduled): bounded fetch per run.
    backfill_only=True: no scanning — just capture missing detection screenshots
    (kicked automatically after a ⚡ Quick Scan finds something new).
    """
    global _proc, _current_label
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return False
        cmd = [sys.executable, "-m", "scripts.run_scan"]
        if keyword_ids:
            cmd += ["--keyword-ids", ",".join(str(i) for i in keyword_ids)]
        if keyword_label:
            cmd += ["--label", keyword_label]
        if capped:
            cmd += ["--capped"]
        if backfill_only:
            cmd += ["--backfill-only"]
        logger.info("Launching scan subprocess: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), **_detached_kwargs())
        _current_label = keyword_label
    return True


def status() -> dict:
    """Return {running, keyword, last_summary, last_keyword}."""
    running = is_running()
    last_summary = None
    last_keyword = None
    if _STATUS_FILE.exists():
        try:
            data = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
            last_summary = data.get("summary")
            last_keyword = data.get("label")
        except Exception:
            pass
    return {
        "running": running,
        "keyword": _current_label if running else None,
        "last_summary": None if running else last_summary,
        "last_keyword": None if running else last_keyword,
    }
