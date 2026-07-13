"""Runs YouTube scans as a background subprocess (like the newspaper scan_manager).

A subprocess keeps heavy transcription (yt-dlp + Whisper) off the web server and
guarantees a single scan at a time. In the default `stub` transcriber the scan
is light (just RSS), but the subprocess model keeps behaviour consistent and
non-blocking either way.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading

from config import BASE_DIR

logger = logging.getLogger(__name__)

_STATUS_FILE = BASE_DIR / "data" / "last_youtube_scan.json"
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_label: str | None = None


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def start_scan(keyword_ids=None, channel_ids=None, label=None, live=False) -> bool:
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
        if live:
            cmd += ["--live"]
        logger.info("Launching YouTube scan subprocess: %s", " ".join(cmd))
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), **kwargs)
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
    }
