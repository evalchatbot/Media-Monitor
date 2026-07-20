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
_PROGRESS_FILE = BASE_DIR / "data" / "youtube_scan_progress.json"
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
    slot_times: list[str] | None = None,
    force: bool = False,
    period_start: str | None = None,
    period_end: str | None = None,
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
        if slot_times:
            cmd += ["--slot-times", ",".join(slot_times)]
        if force:
            cmd += ["--force"]
        if period_start:
            cmd += ["--period-start", period_start]
        if period_end:
            cmd += ["--period-end", period_end]
        logger.info("Launching YouTube scan subprocess: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), **_detached_kwargs())
        _label = label
    return True


def _human_detail(running: bool, label: str | None, progress: dict | None, last: dict | None) -> str:
    if not running:
        return ""
    if progress:
        phase = progress.get("phase") or "working"
        current = progress.get("current") or ""
        checked = progress.get("checked")
        total = progress.get("total")
        slot_date = progress.get("slot_date") or ""
        bits = []
        if slot_date:
            bits.append(slot_date)
        if current:
            bits.append(current)
        if isinstance(checked, int) and isinstance(total, int) and total:
            unit = "videos" if phase == "period" else "bulletins"
            bits.append(f"{checked}/{total} {unit}")
        msg = " · ".join(bits) if bits else (label or "YouTube scan")
        return f"{phase}: {msg}"
    if label:
        return label.replace("search:", "Searching ").replace("date:", "Date ")
    return "YouTube scan running"


def status() -> dict:
    running = is_running()
    last = None
    progress = None
    if _STATUS_FILE.exists():
        try:
            last = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if running and _PROGRESS_FILE.exists():
        try:
            progress = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            progress = None
    elif not running:
        try:
            _PROGRESS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    detail = _human_detail(running, _label if running else None, progress, last)
    return {
        "running": running,
        "label": _label if running else None,
        "detail": detail,
        "progress": progress,
        "last_summary": None if running else (last or {}).get("summary"),
        "last_keyword": None if running else (last or {}).get("label"),
    }
