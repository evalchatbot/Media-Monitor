"""Rolling-window and per-keyword result limits shared by every pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from config import settings
from app.db.models import EPaperPage, Mention


def cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=settings.keyword_result_retention_days)


def effective_time(mention: Mention) -> datetime:
    """The content date when known, otherwise when the result was detected."""
    value = mention.published_at or mention.detected_at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def keyword_counts(session, keyword_texts: list[str] | None = None) -> dict[str, int]:
    """Count retained results by case-folded keyword inside the rolling window."""
    threshold = cutoff()
    stmt = select(Mention).where(
        func.coalesce(Mention.published_at, Mention.detected_at) >= threshold
    )
    wanted = {(k or "").casefold() for k in (keyword_texts or []) if k}
    counts: dict[str, int] = {k: 0 for k in wanted}
    for mention in session.execute(stmt).scalars():
        seen: set[str] = set()
        for label in mention.matched_keywords or []:
            folded = (label or "").casefold()
            if not folded or folded in seen or (wanted and folded not in wanted):
                continue
            seen.add(folded)
            counts[folded] = counts.get(folded, 0) + 1
    return counts


def admit_new(
    candidates: list[str],
    existing: list[str],
    counts: dict[str, int],
) -> list[str]:
    """Return new candidate labels that still have room, updating counts."""
    present = {(k or "").casefold() for k in existing if k}
    admitted: list[str] = []
    for label in candidates:
        folded = (label or "").casefold()
        if not folded or folded in present:
            continue
        if counts.get(folded, 0) >= settings.keyword_result_limit:
            continue
        admitted.append(label)
        present.add(folded)
        counts[folded] = counts.get(folded, 0) + 1
    return admitted


def all_full(counts: dict[str, int]) -> bool:
    return bool(counts) and all(
        value >= settings.keyword_result_limit for value in counts.values()
    )


def enforce_limits(session, *, delete_expired: bool = True) -> dict[str, int]:
    """Keep only the newest N results per keyword and optionally delete expiry.

    Keyword labels are independent: trimming one label from a shared result does
    not remove another keyword's retained result.
    """
    rows = session.execute(select(Mention)).scalars().all()
    before_paths = {path for row in rows for path in _media_paths(row)}
    rows.sort(key=effective_time, reverse=True)
    threshold = cutoff()
    kept: dict[str, int] = {}
    trimmed = expired = deleted = 0
    deleted_ids: set[int] = set()

    for mention in rows:
        if delete_expired and effective_time(mention) < threshold:
            session.delete(mention)
            deleted_ids.add(mention.id)
            expired += 1
            continue

        labels: list[str] = []
        removed: set[str] = set()
        for label in mention.matched_keywords or []:
            folded = (label or "").casefold()
            if not folded:
                continue
            if kept.get(folded, 0) >= settings.keyword_result_limit:
                removed.add(folded)
                trimmed += 1
                continue
            kept[folded] = kept.get(folded, 0) + 1
            labels.append(label)

        if removed:
            mention.matched_keywords = labels
            media = dict(mention.keyword_media or {})
            mention.keyword_media = {
                label: path
                for label, path in media.items()
                if (label or "").casefold() not in removed
            }
            if mention.keyword_media:
                mention.screenshot_path = next(iter(mention.keyword_media.values()))
            if (
                mention.module == "epaper"
                and labels
                and not mention.keyword_media
                and mention.full_screenshot_path
            ):
                mention.screenshot_path = mention.full_screenshot_path
        if not labels:
            session.delete(mention)
            deleted_ids.add(mention.id)
            deleted += 1

    session.commit()
    retained_paths = {
        path for row in rows if row.id not in deleted_ids for path in _media_paths(row)
    }
    page_images = {
        path for path in session.execute(
            select(EPaperPage.image_path).where(EPaperPage.image_path.is_not(None))
        ).scalars()
        if path
    }
    files_deleted = 0
    for path in before_paths - retained_paths - page_images:
        resolved = _storage_file(path)
        if not resolved:
            continue
        try:
            resolved.unlink()
            files_deleted += 1
        except OSError:
            pass
    return {
        "expired": expired, "trimmed": trimmed, "deleted": deleted,
        "files_deleted": files_deleted,
    }


def _media_paths(mention: Mention) -> set[str]:
    return {
        path for path in (
            mention.screenshot_path,
            mention.full_screenshot_path,
            *(mention.keyword_media or {}).values(),
        )
        if path
    }


def _storage_file(path: str) -> Path | None:
    candidate = Path(path)
    if candidate.exists():
        return candidate
    raw = str(path).replace("\\", "/")
    marker = "/storage/"
    idx = raw.lower().rfind(marker)
    if idx >= 0:
        candidate = settings.storage_dir / raw[idx + len(marker):].lstrip("/")
        if candidate.exists():
            return candidate
    return None
