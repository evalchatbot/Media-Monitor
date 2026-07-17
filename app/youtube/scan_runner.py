"""Runs YouTube bulletin scans as a background subprocess."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading

from config import BASE_DIR

logger = logging.getLogger(__name__)

_STATUS_FILE = BASE_DIR / "data" / "last_youtube_scan.json"
_LOG = BASE_DIR / "data" / "youtube_subprocess.log"
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_label: str | None = None


def _detached_kwargs() -> dict:
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
    channel_ids: list[int] | None = None,
    label: str | None = None,
    match_only: bool = False,
    slot_date: str | None = None,
    force: bool = False,
) -> bool:
    """Launch a YouTube scan subprocess. False if one is already running."""
    global _proc, _label
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return False
        cmd = [sys.executable, "-m", "scripts.run_youtube"]
        if keyword_ids:
            cmd += ["--keyword-ids", ",".join(str(i) for i in keyword_ids)]
        if channel_ids:
            cmd += ["--channel-ids", ",".join(str(i) for i in channel_ids)]
        if label:
            cmd += ["--label", label]
        if match_only:
            cmd += ["--match-only"]
        if slot_date:
            cmd += ["--slot-date", slot_date]
        if force:
            cmd += ["--force"]
        logger.info("Launching YouTube scan subprocess: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), **_detached_kwargs())
        _label = label
    return True


def status() -> dict:
    running = is_running()
    last = None
    if _STATUS_FILE.exists():
        try:
            last = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "running": running,
        "label": _label if running else None,
        "last_summary": None if running else (last or {}).get("summary"),
        "last_keyword": None if running else (last or {}).get("label"),
    }
