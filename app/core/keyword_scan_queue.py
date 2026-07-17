"""FIFO keyword scan queue — one keyword at a time.

Add / Confirm / ▶ enqueue keywords; a background worker runs each one only
after the previous newspaper + e-paper jobs have finished. Prevents the
'second keyword couldn't start' race when only one subprocess may run.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import BASE_DIR

logger = logging.getLogger(__name__)

_QUEUE_FILE = BASE_DIR / "data" / "keyword_scan_queue.json"
_lock = threading.Lock()
_queue: list[dict] = []  # {id, text, enqueued_at}
_current: dict | None = None
_worker: threading.Thread | None = None


def _load() -> None:
    global _queue
    if not _QUEUE_FILE.exists():
        return
    try:
        data = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _queue = [x for x in data if isinstance(x, dict) and x.get("id")]
    except Exception:
        _queue = []


def _save() -> None:
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = list(_queue)
    if _current:
        # Keep the in-flight item at the front so a restart can resume it.
        payload = [_current] + [x for x in payload if x.get("id") != _current.get("id")]
    _QUEUE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _ensure_loaded() -> None:
    if not _queue and _QUEUE_FILE.exists() and _current is None:
        _load()


def enqueue(keyword_id: int, text: str) -> dict:
    """Append a keyword if not already queued/current. Starts the worker."""
    global _worker
    with _lock:
        _ensure_loaded()
        kid = int(keyword_id)
        if _current and int(_current.get("id", -1)) == kid:
            return status_unlocked()
        if any(int(x.get("id", -1)) == kid for x in _queue):
            return status_unlocked()
        _queue.append({
            "id": kid,
            "text": text,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        })
        _save()
        need_worker = _worker is None or not _worker.is_alive()
    if need_worker:
        t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
        with _lock:
            _worker = t
        t.start()
    return status()


def enqueue_many(items: list[tuple[int, str]]) -> dict:
    """Enqueue several keywords in given order (earliest first)."""
    for kid, text in items:
        enqueue(kid, text)
    return status()


def status() -> dict:
    with _lock:
        return status_unlocked()


def status_unlocked() -> dict:
    pending = [{"id": x["id"], "text": x.get("text") or ""} for x in _queue]
    return {
        "running": _current is not None or bool(pending),
        "current": (
            {"id": _current["id"], "text": _current.get("text") or ""}
            if _current else None
        ),
        "pending": pending,
        "queued": len(pending) + (1 if _current else 0),
    }


def is_keyword_busy(keyword_id: int | None = None, text: str | None = None) -> bool:
    st = status()
    cur = st.get("current") or {}
    if keyword_id is not None and cur.get("id") == keyword_id:
        return True
    if text and (cur.get("text") or "").casefold() == text.casefold():
        return True
    for item in st.get("pending") or []:
        if keyword_id is not None and item.get("id") == keyword_id:
            return True
        if text and (item.get("text") or "").casefold() == text.casefold():
            return True
    return False


def _wait_scans_idle(settle_s: float = 1.5) -> None:
    from app.epaper import scan_runner
    from app.newspaper import scan_manager

    time.sleep(settle_s)
    while scan_manager.is_running() or scan_runner.is_running():
        time.sleep(2)


def _run_one(item: dict) -> None:
    from app.epaper import scan_runner
    from app.newspaper import scan_manager
    from app.newspaper.pipeline import run_quick_match

    kid = int(item["id"])
    text = item.get("text") or f"keyword-{kid}"
    logger.info("keyword queue: starting %s (%s)", text, kid)

    # Wait out any unrelated scan (scheduled / manual) before claiming the slots.
    _wait_scans_idle(settle_s=0.5)

    try:
        run_quick_match(keyword_ids=[kid])
    except Exception:
        logger.exception("keyword queue: quick match failed for %s", kid)

    news_ok = scan_manager.start_scan(
        keyword_ids=[kid], keyword_label=text, capped=True)
    ep_ok = scan_runner.start_scan(keyword_ids=[kid], label=text)
    if not (news_ok or ep_ok):
        # Another job grabbed the slot between wait and start — wait and retry once.
        _wait_scans_idle()
        news_ok = scan_manager.start_scan(
            keyword_ids=[kid], keyword_label=text, capped=True)
        ep_ok = scan_runner.start_scan(keyword_ids=[kid], label=text)

    if news_ok or ep_ok:
        _wait_scans_idle()
    logger.info("keyword queue: finished %s (%s)", text, kid)


def _worker_loop() -> None:
    global _current
    while True:
        with _lock:
            if not _queue:
                _current = None
                _save()
                return
            _current = _queue.pop(0)
            _save()
            item = dict(_current)
        try:
            _run_one(item)
        except Exception:
            logger.exception("keyword queue: job failed for %s", item)
        with _lock:
            _current = None
            _save()


# Warm from disk on import so a redeploy can resume pending IDs.
with _lock:
    _load()
    if _queue:
        t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
        _worker = t
        t.start()
