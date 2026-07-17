"""FIFO keyword scan queue — batch when possible, one live crawl for the set.

Confirm / ▶ enqueue keywords. The worker drains everything currently queued,
runs ONE stored-corpus match + ONE newspaper scan + ONE e-paper cycle for the
whole batch, then picks up anything added while that ran. That stops
"Aleema Khan then BLA then …" from each re-scraping every site.
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
    # Persist waiting + in-flight so a restart can resume.
    seen: set[int] = set()
    payload: list[dict] = []
    for item in list(_current_batch) + list(_queue):
        kid = int(item.get("id", -1))
        if kid < 0 or kid in seen:
            continue
        seen.add(kid)
        payload.append(item)
    _QUEUE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _ensure_loaded() -> None:
    if not _queue and not _current_batch and _QUEUE_FILE.exists():
        _load()


def enqueue(keyword_id: int, text: str) -> dict:
    """Append a keyword if not already queued/in-flight. Starts the worker."""
    global _worker
    with _lock:
        _ensure_loaded()
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


def _wait_scans_idle(settle_s: float = 1.0) -> None:
    from app.epaper import scan_runner
    from app.newspaper import scan_manager

    time.sleep(settle_s)
    while scan_manager.is_running() or scan_runner.is_running():
        time.sleep(2)


def _run_batch(batch: list[dict]) -> None:
    from app.epaper import scan_runner
    from app.newspaper import scan_manager
    from app.newspaper.pipeline import run_quick_match

    ids = [int(x["id"]) for x in batch]
    texts = [x.get("text") or f"keyword-{x['id']}" for x in batch]
    label = ", ".join(texts[:3]) + (f" +{len(texts) - 3}" if len(texts) > 3 else "")
    logger.info("keyword queue: batch of %d — %s", len(ids), label)

    # Wait out any unrelated scan before claiming the slots.
    _wait_scans_idle(settle_s=0.3)

    # 1) Instant: match ALL queued keywords against stored articles + e-paper text.
    try:
        summary = run_quick_match(keyword_ids=ids)
        logger.info("keyword queue: quick match done %s", summary)
    except Exception:
        logger.exception("keyword queue: quick match failed for %s", ids)

    # 2) ONE live newspaper crawl matching every keyword in the batch.
    news_ok = scan_manager.start_scan(
        keyword_ids=ids, keyword_label=label, capped=True)

    # 3) ONE e-paper cycle (fetch today once, match all batch keywords).
    ep_ok = scan_runner.start_scan(keyword_ids=ids, label=label, fetch=True)

    if not (news_ok or ep_ok):
        _wait_scans_idle()
        news_ok = scan_manager.start_scan(
            keyword_ids=ids, keyword_label=label, capped=True)
        ep_ok = scan_runner.start_scan(keyword_ids=ids, label=label, fetch=True)

    if news_ok or ep_ok:
        _wait_scans_idle()
    logger.info("keyword queue: batch finished (%s)", label)


def _worker_loop() -> None:
    global _current_batch
    while True:
        with _lock:
            if not _queue:
                _current_batch = []
                _save()
                return
            # Drain everything waiting so N keywords share one crawl.
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


# Warm from disk on import so a redeploy can resume pending IDs.
with _lock:
    _load()
    if _queue:
        t = threading.Thread(target=_worker_loop, daemon=True, name="keyword-scan-queue")
        _worker = t
        t.start()
