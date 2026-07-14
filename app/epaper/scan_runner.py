"""Runs e-paper cycles as a background subprocess (same rationale as the
newspaper scan_manager: Playwright — used by the Dawn adapter — is only stable
on a process main thread, and the UI must never block on a long fetch/read).

Only one e-paper cycle runs at a time; a second request is ignored.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading

from config import BASE_DIR

logger = logging.getLogger(__name__)

_STATUS_FILE = BASE_DIR / "data" / "last_epaper_scan.json"
_LOG = BASE_DIR / "data" / "epaper_subprocess.log"

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_current_label: str | None = None


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


def start_scan(keyword_ids: list[int] | None = None, label: str | None = None,
               papers: list[str] | None = None, fetch: bool = True) -> bool:
    """Launch an e-paper cycle subprocess. False if one is already running."""
    global _proc, _current_label
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return False
        cmd = [sys.executable, "-m", "scripts.run_epaper"]
        if keyword_ids:
            cmd += ["--keyword-ids", ",".join(str(i) for i in keyword_ids)]
        if label:
            cmd += ["--label", label]
        if papers:
            cmd += ["--papers", ",".join(papers)]
        if not fetch:
            cmd += ["--no-fetch"]
        logger.info("Launching e-paper subprocess: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), **_detached_kwargs())
        _current_label = label
    return True


def status() -> dict:
    running = is_running()
    last_summary = None
    last_label = None
    if _STATUS_FILE.exists():
        try:
            data = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
            last_summary = data.get("summary")
            last_label = data.get("label")
        except Exception:
            pass
    return {
        "running": running,
        "label": _current_label if running else None,
        "last_summary": None if running else last_summary,
        "last_label": None if running else last_label,
    }
