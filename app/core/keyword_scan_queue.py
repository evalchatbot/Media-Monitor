"""Fast keyword scan queue — match stored content, then screenshot hits only.

Confirm / ▶ enqueue keywords. The worker drains the queue and for the whole
batch:
  1) exact-match against stored articles + e-paper text (creates mentions/clips)
  2) screenshot ONLY those new web hits (no full site crawl)

Full live newspaper/e-paper crawls stay on the scheduler — they were why
keyword searches felt endless. The queue also self-heals stuck "running" state
so spinners always stop.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from config import BASE_DIR

logger = logging.getLogger(__name__)

_QUEUE_FILE = BASE_DIR / "data" / "keyword_scan_queue.json"
_lock = threading.Lock()
_queue: list[dict] = []  # {id, text, enqueued_at}
_current_batch: list[dict] = []
_worker: threading.Thread | None = None
_BACKFILL_WAIT_S = 8 * 60  # never leave the UI spinning forever


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
    """Persist only WAITING items. Never re-queue an in-flight batch after crash
    (that left spinners running forever)."""
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    seen: set[int] = set()
    payload: list[dict] = []
    for item in _queue:
        kid = int(item.get("id", -1))
        if kid < 0 or kid in seen:
            continue
        seen.add(kid)
        payload.append(item)
    _QUEUE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _ensure_loaded() -> None:
    if not _queue and not _current_batch and _QUEUE_FILE.exists():
        _load()


def _worker_alive() -> bool:
    return _worker is not None and _worker.is_alive()


def _heal_stuck() -> None:
    """If the worker died mid-job, drop the ghost batch so the UI stops spinning."""
    global _current_batch
    if _current_batch and not _worker_alive():
        logger.warning("keyword queue: clearing stuck batch after worker exit")
        _current_batch = []
        _save()


def enqueue(keyword_id: int, text: str) -> dict:
    """Append a keyword if not already queued/in-flight. Starts the worker."""
    global _worker
    with _lock:
        _ensure_loaded()
        _heal_stuck()
        kid = int(keyword_id)
        if any(int(x.get("id", -1)) == kid for x in _current_batch):
            return status_unlocked()
        if any(int(x.get("id", -1)) == kid for x in _queue):
            return status_unlocked()
        _queue.append({
            "id": kid,
            "text": text,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        })
        _save()
        need_worker = not _worker_alive()
    if need_worker:
        t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
        with _lock:
            _worker = t
        t.start()
    return status()


def enqueue_many(items: list[tuple[int, str]]) -> dict:
    for kid, text in items:
        enqueue(kid, text)
    return status()


def status() -> dict:
    with _lock:
        _heal_stuck()
        # If work is waiting but the worker died, restart it.
        global _worker
        if _queue and not _worker_alive() and not _current_batch:
            t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
            _worker = t
            t.start()
        return status_unlocked()


def status_unlocked() -> dict:
    batch = [{"id": x["id"], "text": x.get("text") or ""} for x in _current_batch]
    pending = [{"id": x["id"], "text": x.get("text") or ""} for x in _queue]
    current = batch[0] if batch else None
    label = ", ".join(x["text"] for x in batch[:3] if x.get("text"))
    if len(batch) > 3:
        label += f" +{len(batch) - 3}"
    return {
        "running": bool(batch or pending),
        "current": (
            {"id": current["id"], "text": label or current.get("text") or ""}
            if current else None
        ),
        "batch": batch,
        "pending": pending,
        "queued": len(batch) + len(pending),
    }


def is_keyword_busy(keyword_id: int | None = None, text: str | None = None) -> bool:
    st = status()
    for item in list(st.get("batch") or []) + list(st.get("pending") or []):
        if keyword_id is not None and item.get("id") == keyword_id:
            return True
        if text and (item.get("text") or "").casefold() == text.casefold():
            return True
    return False


def _wait_backfill_idle(timeout_s: float = _BACKFILL_WAIT_S) -> None:
    from app.newspaper import scan_manager

    deadline = time.time() + timeout_s
    time.sleep(0.8)
    while scan_manager.is_running():
        if time.time() >= deadline:
            logger.warning("keyword queue: screenshot backfill still running after %ss — releasing UI",
                           int(timeout_s))
            return
        time.sleep(1.5)


def _run_batch(batch: list[dict]) -> None:
    from app.newspaper import scan_manager
    from app.newspaper.pipeline import run_quick_match

    ids = [int(x["id"]) for x in batch]
    texts = [x.get("text") or f"keyword-{x['id']}" for x in batch]
    label = ", ".join(texts[:3]) + (f" +{len(texts) - 3}" if len(texts) > 3 else "")
    logger.info("keyword queue: fast batch of %d — %s", len(ids), label)

    # 1) Exact match on stored articles + e-paper pages (clips created here).
    try:
        summary = run_quick_match(keyword_ids=ids)
        logger.info("keyword queue: quick match done %s", summary)
    except Exception:
        logger.exception("keyword queue: quick match failed for %s", ids)
        return

    # 2) Screenshot ONLY the web hits that still need one (no full crawl).
    started = scan_manager.start_scan(
        keyword_ids=ids, keyword_label=label, capped=True, backfill_only=True,
    )
    if started:
        _wait_backfill_idle()
    else:
        # Another scan owns the browser — try once more after a short wait.
        time.sleep(3)
        if scan_manager.start_scan(
            keyword_ids=ids, keyword_label=label, capped=True, backfill_only=True,
        ):
            _wait_backfill_idle()

    logger.info("keyword queue: fast batch finished (%s)", label)


def _worker_loop() -> None:
    global _current_batch
    while True:
        with _lock:
            if not _queue:
                _current_batch = []
                _save()
                return
            _current_batch = list(_queue)
            _queue.clear()
            _save()
            batch = [dict(x) for x in _current_batch]
        try:
            _run_batch(batch)
        except Exception:
            logger.exception("keyword queue: batch failed")
        with _lock:
            _current_batch = []
            _save()


with _lock:
    _load()
    if _queue:
        t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
        _worker = t
        t.start()
