"""In-memory registry for live-search jobs.

The whole point of the live model: a search runs in a background thread, streams
result cards into an in-memory job, and the browser polls for them. NOTHING is
written to the database or the storage volume — when a job is evicted (TTL or
cap), its results are simply gone. Keywords (the watchlist) are the only thing
that persists, and that happens elsewhere.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_JOBS: dict[str, "Job"] = {}
_lock = threading.Lock()

_TTL_SECONDS = 1800   # forget a job 30 min after its last update
_MAX_JOBS = 40        # hard cap so a long session can't grow memory unbounded


class Job:
    __slots__ = ("id", "module", "status", "progress", "results",
                 "error", "note", "created", "updated", "cancelled")

    def __init__(self, jid: str, module: str):
        self.id = jid
        self.module = module
        self.status = "running"          # running | done | error
        self.progress = {"phase": "starting", "checked": 0, "total": 0, "current": ""}
        self.results: list[dict] = []
        self.error: str | None = None
        self.note: str = ""              # non-fatal diagnostic (e.g. OCR failed)
        self.created = time.time()
        self.updated = time.time()
        self.cancelled = False

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "module": self.module,
            "status": self.status,
            "progress": dict(self.progress),
            "count": len(self.results),
            "error": self.error,
        }


def _forget(jid: str) -> None:
    """Drop a job and the clipping images it wrote."""
    _JOBS.pop(jid, None)
    try:
        from app.epaper.livescan import cleanup_job
        cleanup_job(jid)
    except Exception:  # pragma: no cover - cleanup must never break eviction
        pass


def _evict_locked() -> None:
    now = time.time()
    for jid in [j for j, job in _JOBS.items() if now - job.updated > _TTL_SECONDS]:
        _forget(jid)
    if len(_JOBS) > _MAX_JOBS:
        for jid in sorted(_JOBS, key=lambda k: _JOBS[k].updated)[: len(_JOBS) - _MAX_JOBS]:
            _forget(jid)


def create(module: str) -> str:
    with _lock:
        _evict_locked()
        jid = uuid.uuid4().hex[:12]
        _JOBS[jid] = Job(jid, module)
    return jid


def get(jid: str) -> "Job | None":
    return _JOBS.get(jid)


def add_result(jid: str, card: dict) -> None:
    job = _JOBS.get(jid)
    if job is not None:
        job.results.append(card)
        job.updated = time.time()


def set_progress(jid: str, **fields) -> None:
    job = _JOBS.get(jid)
    if job is not None:
        job.progress.update(fields)
        job.updated = time.time()


def set_note(jid: str, note: str) -> None:
    job = _JOBS.get(jid)
    if job is not None:
        job.note = note
        job.updated = time.time()


def finish(jid: str, error: str | None = None) -> None:
    job = _JOBS.get(jid)
    if job is not None:
        job.status = "error" if error else "done"
        job.error = error
        job.progress["phase"] = "error" if error else "done"
        job.updated = time.time()


def is_cancelled(jid: str) -> bool:
    job = _JOBS.get(jid)
    return bool(job and job.cancelled)


def cancel(jid: str) -> bool:
    job = _JOBS.get(jid)
    if job is None:
        return False
    job.cancelled = True
    job.updated = time.time()
    return True


def run(module: str, target, *args) -> str:
    """Start `target(job_id, *args)` on a daemon thread; return the job id."""
    jid = create(module)

    def _wrap() -> None:
        try:
            target(jid, *args)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("live job %s (%s) crashed", jid, module)
            finish(jid, str(exc))
        else:
            if _JOBS.get(jid) and _JOBS[jid].status == "running":
                finish(jid)

    threading.Thread(target=_wrap, name=f"live-{module}", daemon=True).start()
    return jid
