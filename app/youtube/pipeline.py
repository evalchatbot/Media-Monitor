"""YouTube bulletin pipeline: discover → classify → transcribe → match → frame."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.core import result_policy
from app.core.keywords import find_matches
from app.db.base import SessionLocal
from config import BASE_DIR
from app.db.models import (
    ArticleCache,
    BulletinSlot,
    Keyword,
    Mention,
    Transcript,
    YouTubeBulletin,
    YouTubeChannel,
)
from app.notifiers import get_notifier
from app.youtube import classifier, discovery, frame, matcher, media_source, transcribe
from config import settings

logger = logging.getLogger(__name__)
_PKT = ZoneInfo(settings.youtube_timezone)
_PROGRESS_FILE = BASE_DIR / "data" / "youtube_scan_progress.json"


def _write_progress(**payload) -> None:
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROGRESS_FILE.write_text(
            json.dumps({**payload, "at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_progress() -> None:
    try:
        _PROGRESS_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def slot_date_is_past(slot_date: str) -> bool:
    """True when the bulletin calendar day is before today (PKT)."""
    try:
        return date.fromisoformat(slot_date) < datetime.now(_PKT).date()
    except ValueError:
        return False


def repair_youtube_mentions(session, *, rematch: bool = True) -> dict:
    """Re-locate transcript hits and drop unverified keyword tags.

    `rematch=False` skips the per-mention transcript re-scan and only prunes
    unverified tags. A scan already re-matches everything it touches, so paying
    the full re-scan over the whole mention history first only delays results.
    """
    repaired = deleted = rematched = 0
    keywords = _active_youtube_keywords(session)
    kw_by_text = {t: lang for t, lang in keywords}
    rows = session.execute(
        select(Mention).where(Mention.module == "youtube")
    ).scalars().all()
    for m in rows:
        before = list(m.matched_keywords or [])
        tr = None
        if rematch:
            tr = session.execute(
                select(Transcript).where(Transcript.video_id == m.external_id)
            ).scalar_one_or_none()
        if tr and tr.text and tr.segments and m.matched_keywords:
            labels = [k for k in (m.matched_keywords or []) if k in kw_by_text]
            if labels:
                search_kws = [(k, kw_by_text[k]) for k in labels]
                fresh = matcher.find_all_hits(tr.text, tr.segments or [], search_kws)
                if fresh:
                    m.keyword_hits = matcher.hits_to_json(fresh)
                    m.matched_keywords = sorted(fresh.keys())
                    first = next((fresh[k][0] for k in sorted(fresh) if fresh[k]), None)
                    if first:
                        m.snippet = (first.excerpt or m.snippet or "")[:240]
                        m.deeplink_seconds = first.start
                        if m.external_id:
                            m.url = discovery.deep_link(m.external_id, first.start)
                    rematched += 1
        if _sanitize_youtube_mention(session, m):
            if list(m.matched_keywords or []) != before:
                repaired += 1
        else:
            session.delete(m)
            deleted += 1
    session.commit()
    return {"repaired": repaired, "deleted": deleted, "rematched": rematched}


def run_youtube_scan(
    *,
    channel_ids: list[int] | None = None,
    keyword_ids: list[int] | None = None,
    slot_date: str | None = None,
    slot_times: list[str] | None = None,
    force: bool = False,
    match_only: bool = False,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """Process due bulletins, a custom upload period, or rematch cached transcripts."""
    if period_start and period_end and not match_only:
        start_dt, end_dt = parse_period_iso(period_start, period_end)
        return run_youtube_period_scan(
            start_dt,
            end_dt,
            channel_ids=channel_ids,
            keyword_ids=keyword_ids,
            force=force,
        )
    summary = {
        "channels": 0,
        "bulletins_checked": 0,
        "discovered": 0,
        "transcribed": 0,
        "mentions": 0,
        "alerts": 0,
        "missing": 0,
        "failed": 0,
        "needs_review": 0,
        "cost_usd_est": 0.0,
    }
    session = SessionLocal()
    notifier = get_notifier()
    try:
        repair_youtube_mentions(session, rematch=False)
        if match_only:
            summary.update(
                _match_cached(
                    session, notifier,
                    keyword_ids=keyword_ids,
                    slot_date=slot_date,
                    slot_times=slot_times,
                    period_start=period_start,
                    period_end=period_end,
                )
            )
            result_policy.enforce_limits(session)
            _clear_progress()
            return summary

        _write_progress(
            phase="starting",
            slot_date=slot_date or "",
            checked=0,
            total=0,
            current="",
        )

        ensure_due_bulletins(session, channel_ids=channel_ids, for_date=slot_date)
        q = select(YouTubeBulletin).order_by(YouTubeBulletin.slot_date.desc(), YouTubeBulletin.id)
        if channel_ids:
            q = q.where(YouTubeBulletin.channel_db_id.in_(channel_ids))
        if slot_date:
            q = q.where(YouTubeBulletin.slot_date == slot_date)
        else:
            # Process recent open bulletins (today + yesterday for midnight).
            today = datetime.now(_PKT).date()
            days = {(today - timedelta(days=i)).isoformat() for i in range(0, 3)}
            q = q.where(YouTubeBulletin.slot_date.in_(days))

        bulletins = session.execute(q).scalars().all()
        channels = {
            c.id: c
            for c in session.execute(select(YouTubeChannel).where(YouTubeChannel.active.is_(True))).scalars()
        }
        slots = {s.id: s for s in session.execute(select(BulletinSlot)).scalars()}
        if slot_times:
            allowed = set(slot_times)
            bulletins = [
                b for b in bulletins
                if (s := slots.get(b.slot_id)) and s.local_time in allowed
            ]
        summary["channels"] = len(channels)

        keywords = _active_youtube_keywords(session, keyword_ids)
        historical = bool(slot_date and slot_date_is_past(slot_date))
        effective_force = force or historical
        total = len(bulletins)
        _write_progress(
            phase="scanning",
            slot_date=slot_date or "",
            checked=0,
            total=total,
            current="",
        )
        for b in bulletins:
            summary["bulletins_checked"] += 1
            ch = channels.get(b.channel_db_id)
            slot = slots.get(b.slot_id)
            if not ch or not slot or not slot.enabled:
                continue
            _write_progress(
                phase="scanning",
                slot_date=slot_date or b.slot_date,
                checked=summary["bulletins_checked"],
                total=total,
                current=f"{ch.name} · {slot.label or slot.local_time}",
            )
            upgrade_meta = False
            if not effective_force and b.discovery_status in ("ready", "no_match") and b.transcription_status == "done":
                # Metadata-only stubs must be upgraded to real Groq audio transcripts.
                existing = None
                if b.video_id:
                    existing = session.execute(
                        select(Transcript).where(Transcript.video_id == b.video_id)
                    ).scalar_one_or_none()
                if _is_metadata_only(existing):
                    upgrade_meta = True  # fall through → download bulletin URL → Groq
                else:
                    # Still rematch if new keywords arrived.
                    if keywords:
                        _rematch_bulletin(session, notifier, b, ch, slot, keywords, summary)
                    continue
            if not effective_force and not upgrade_meta and not _is_due(b, slot, ch):
                continue
            try:
                _process_bulletin(
                    session, notifier, b, ch, slot, keywords, summary,
                    force=effective_force or upgrade_meta,
                )
            except Exception as exc:
                logger.exception("bulletin %s failed: %s", b.id, exc)
                b.discovery_status = "failed"
                b.error = str(exc)[:500]
                b.attempts = (b.attempts or 0) + 1
                b.last_processed_at = datetime.now(timezone.utc)
                session.commit()
                summary["failed"] += 1

        result_policy.enforce_limits(session)
        _clear_progress()
        return summary
    finally:
        session.close()


def ensure_due_bulletins(
    session,
    *,
    channel_ids: list[int] | None = None,
    for_date: str | None = None,
) -> int:
    """Create waiting bulletin rows for today (and yesterday midnight) as needed."""
    today = date.fromisoformat(for_date) if for_date else datetime.now(_PKT).date()
    dates = [today]
    if not for_date:
        dates.append(today - timedelta(days=1))
    created = 0
    q = select(YouTubeChannel).where(YouTubeChannel.active.is_(True))
    if channel_ids:
        q = q.where(YouTubeChannel.id.in_(channel_ids))
    for ch in session.execute(q).scalars():
        slots = session.execute(
            select(BulletinSlot).where(
                BulletinSlot.channel_id == ch.id, BulletinSlot.enabled.is_(True)
            )
        ).scalars().all()
        for d in dates:
            for slot in slots:
                if not _slot_effective(slot, d.isoformat()):
                    continue
                exists = session.execute(
                    select(YouTubeBulletin).where(
                        YouTubeBulletin.channel_db_id == ch.id,
                        YouTubeBulletin.slot_id == slot.id,
                        YouTubeBulletin.slot_date == d.isoformat(),
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                session.add(
                    YouTubeBulletin(
                        channel_db_id=ch.id,
                        slot_id=slot.id,
                        slot_date=d.isoformat(),
                        discovery_status="waiting",
                        transcription_status="pending",
                    )
                )
                created += 1
    if created:
        session.commit()
    return created


def run_quick_youtube_match(
    keyword_ids: list[int] | None = None,
    *,
    slot_date: str | None = None,
    slot_times: list[str] | None = None,
) -> dict:
    """Match keywords against cached transcripts (optionally one bulletin date)."""
    return run_youtube_scan(
        keyword_ids=keyword_ids,
        slot_date=slot_date,
        slot_times=slot_times,
        match_only=True,
    )


def run_youtube_date_search(
    slot_date: str,
    keyword_ids: list[int] | None = None,
    slot_times: list[str] | None = None,
) -> dict:
    """Discover, transcribe if needed, and match bulletin slots on one date."""
    return run_youtube_scan(
        slot_date=slot_date,
        keyword_ids=keyword_ids,
        slot_times=slot_times,
        force=slot_date_is_past(slot_date),
        match_only=False,
    )


def parse_period_iso(start: str, end: str) -> tuple[datetime, datetime]:
    """Parse period bounds (ISO-8601, timezone-aware)."""
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid period bounds") from exc
    if s.tzinfo is None:
        s = s.replace(tzinfo=_PKT)
    if e.tzinfo is None:
        e = e.replace(tzinfo=_PKT)
    if e < s:
        raise ValueError("period end must be after start")
    return s.astimezone(timezone.utc), e.astimezone(timezone.utc)


def period_bounds_from_parts(
    start_date: str,
    end_date: str,
    start_time: str = "00:00",
    end_time: str = "23:59",
) -> tuple[datetime, datetime]:
    """Build UTC bounds from PKT calendar date + clock times."""
    def _combine(d: str, t: str) -> datetime:
        parts = d.strip().split("-")
        hh, mm = (t.strip() or "00:00").split(":")[:2]
        return datetime(
            int(parts[0]), int(parts[1]), int(parts[2]),
            int(hh), int(mm),
            tzinfo=_PKT,
        )

    start = _combine(start_date.strip(), start_time)
    end = _combine(end_date.strip(), end_time)
    t_end = end_time.strip()
    if t_end.startswith("23:59"):
        end = end.replace(second=59)
    if end < start:
        raise ValueError("end must be after start")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def period_label(start: datetime, end: datetime) -> str:
    s = start.astimezone(_PKT)
    e = end.astimezone(_PKT)
    if s.date() == e.date():
        return f"{s.strftime('%d %b %Y')} · {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
    return (
        f"{s.strftime('%d %b %Y %H:%M')} – {e.strftime('%d %b %Y %H:%M')}"
    )


def run_youtube_period_scan(
    period_start: datetime,
    period_end: datetime,
    *,
    channel_ids: list[int] | None = None,
    keyword_ids: list[int] | None = None,
    force: bool = False,
) -> dict:
    """Discover all non-live uploads in a time window, transcribe, and match keywords."""
    summary = {
        "channels": 0,
        "videos_checked": 0,
        "transcribed": 0,
        "mentions": 0,
        "alerts": 0,
        "skipped_live": 0,
        "failed": 0,
        "cost_usd_est": 0.0,
        "period": period_label(period_start, period_end),
    }
    session = SessionLocal()
    notifier = get_notifier()
    label = summary["period"]
    try:
        repair_youtube_mentions(session, rematch=False)
        keywords = _active_youtube_keywords(session, keyword_ids)
        if not keywords:
            logger.warning("period scan skipped: no keywords selected")
            return summary

        q = select(YouTubeChannel).where(YouTubeChannel.active.is_(True)).order_by(YouTubeChannel.name)
        if channel_ids:
            q = q.where(YouTubeChannel.id.in_(channel_ids))
        channels = list(session.execute(q).scalars())
        summary["channels"] = len(channels)

        work: list[tuple[YouTubeChannel, discovery.Video]] = []
        for ch in channels:
            videos = discovery.fetch_uploads_in_range(
                ch.channel_id,
                published_after=period_start,
                published_before=period_end,
                playlist_id=ch.uploads_playlist_id or "",
            )
            for v in videos:
                if v.live:
                    summary["skipped_live"] += 1
                    continue
                work.append((ch, v))

        total = len(work)
        _write_progress(
            phase="period",
            slot_date=label,
            checked=0,
            total=total,
            current="",
        )
        for idx, (ch, v) in enumerate(work, start=1):
            summary["videos_checked"] += 1
            _write_progress(
                phase="period",
                slot_date=label,
                checked=idx,
                total=total,
                current=f"{ch.name} · {(v.title or '')[:48]}",
            )
            try:
                _process_period_video(
                    session,
                    notifier,
                    ch,
                    v,
                    keywords,
                    summary,
                    section_label=f"Period · {label}",
                    force=force,
                )
            except Exception as exc:
                logger.exception("period video %s failed: %s", v.video_id, exc)
                summary["failed"] += 1

        result_policy.enforce_limits(session)
        _clear_progress()
        return summary
    finally:
        session.close()


def bulletin_status_for_date(session, show_date: date) -> list[dict]:
    """Per-channel latest bulletin status strip for the YouTube UI."""
    rows = []
    channels = session.execute(
        select(YouTubeChannel).where(YouTubeChannel.active.is_(True)).order_by(YouTubeChannel.name)
    ).scalars().all()
    for ch in channels:
        bulletins = session.execute(
            select(YouTubeBulletin)
            .where(
                YouTubeBulletin.channel_db_id == ch.id,
                YouTubeBulletin.slot_date == show_date.isoformat(),
            )
        ).scalars().all()
        slots = {
            s.id: s
            for s in session.execute(
                select(BulletinSlot).where(BulletinSlot.channel_id == ch.id)
            ).scalars()
        }
        # Latest expected slot relative to now (or end of selected day).
        now_local = datetime.now(_PKT)
        ordered = sorted(
            bulletins,
            key=lambda b: classifier.slot_airtime(b.slot_date, slots[b.slot_id].local_time, ch.timezone)
            if b.slot_id in slots else datetime.min.replace(tzinfo=_PKT),
        )
        latest = None
        for b in ordered:
            slot = slots.get(b.slot_id)
            if not slot:
                continue
            air = classifier.slot_airtime(b.slot_date, slot.local_time, ch.timezone)
            if show_date == now_local.date() and air > now_local + timedelta(minutes=5):
                continue
            latest = b
        if latest is None and ordered:
            latest = ordered[-1]
        if latest is None:
            rows.append({
                "channel": ch.name,
                "channel_id": ch.id,
                "slot": None,
                "status": "waiting",
                "title": "",
                "video_id": None,
            })
            continue
        slot = slots.get(latest.slot_id)
        rows.append({
            "channel": ch.name,
            "channel_id": ch.id,
            "slot": slot.label if slot else None,
            "slot_time": slot.local_time if slot else None,
            "status": _public_status(latest),
            "title": latest.title or "",
            "video_id": latest.video_id,
            "attempts": latest.attempts,
            "error": latest.error,
        })
    return rows


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _process_bulletin(session, notifier, b, ch, slot, keywords, summary, *, force: bool) -> None:
    b.discovery_status = "discovering"
    b.attempts = (b.attempts or 0) + 1
    b.last_processed_at = datetime.now(timezone.utc)
    session.commit()

    videos = discovery.fetch_uploads(
        ch.channel_id,
        playlist_id=ch.uploads_playlist_id or "",
        max_results=40,
    )
    scored = classifier.classify_candidates(
        videos,
        slot_date=b.slot_date,
        local_time=slot.local_time,
        title_rules=slot.title_rules or [],
        min_duration_sec=slot.min_duration_sec or 120,
        max_duration_sec=slot.max_duration_sec or settings.youtube_max_duration_seconds,
        tz=ch.timezone or settings.youtube_timezone,
    )
    winner, rejected, needs_review = classifier.pick_best(scored)
    b.candidates = [
        {
            "video_id": c.video.video_id,
            "title": c.video.title,
            "score": c.score,
            "reasons": c.reasons,
            "duration": c.video.duration_seconds,
            "published": c.video.published.isoformat() if c.video.published else None,
        }
        for c in scored[:8]
    ]

    if needs_review:
        b.discovery_status = "needs_review"
        b.error = "ambiguous candidates"
        session.commit()
        summary["needs_review"] += 1
        return

    if winner is None:
        if _past_missing_deadline(b, slot, ch):
            b.discovery_status = "missing"
            b.error = "no bulletin upload found"
            summary["missing"] += 1
        else:
            b.discovery_status = "waiting"
            b.error = "not uploaded yet"
        session.commit()
        return

    v = winner.video
    b.video_id = v.video_id
    b.title = v.title
    b.published_at = v.published
    b.duration_seconds = v.duration_seconds
    b.discovery_status = "discovered"
    b.error = None
    session.commit()
    summary["discovered"] += 1

    # Existing real transcript? skip Groq. Metadata stubs get upgraded.
    tr = session.execute(
        select(Transcript).where(Transcript.video_id == v.video_id)
    ).scalar_one_or_none()
    upgrade_metadata = _is_metadata_only(tr)

    if (tr is None or upgrade_metadata) and not settings.youtube_metadata_only:
        b.discovery_status = "transcribing"
        b.transcription_status = "running"
        session.commit()
        asset = media_source.acquire_audio(
            video_id=v.video_id,
            video_url=v.url or f"https://www.youtube.com/watch?v={v.video_id}",
            media_source=ch.media_source or settings.youtube_media_source,
            media_source_config=ch.media_source_config or {},
        )
        if asset is None:
            # Metadata-only path: match title/description, no timestamps.
            text = f"{v.title}\n{v.description}"
            segments: list[dict] = []
            meta = {"model": "metadata", "confidence": {}}
            b.transcription_status = "skipped"
        else:
            try:
                text, segments, meta = transcribe.transcribe_audio(
                    asset.path,
                    language="ur",
                    prompt=settings.youtube_transcribe_prompt,
                )
                summary["cost_usd_est"] += transcribe.estimate_cost_usd(
                    float(v.duration_seconds or meta.get("duration") or 0),
                    model=meta.get("model"),
                )
                summary["transcribed"] += 1
                b.transcription_status = "done" if text else "failed"
            finally:
                media_source.cleanup_asset(asset)

        if text or segments:
            if upgrade_metadata and tr is not None:
                tr.bulletin_id = b.id
                tr.channel_id = ch.channel_id
                tr.source = ch.name
                tr.title = v.title
                tr.url = v.url
                tr.language = meta.get("language") or "ur"
                tr.text = text
                tr.segments = segments
                tr.duration_seconds = v.duration_seconds
                tr.transcriber = (
                    "groq" if meta.get("model") not in (None, "metadata") else "metadata"
                )
                tr.model = meta.get("model") or ""
                tr.confidence = meta.get("confidence") or {}
                _upsert_cache(session, v, ch, text)
                session.commit()
            else:
                tr = Transcript(
                    video_id=v.video_id,
                    bulletin_id=b.id,
                    channel_id=ch.channel_id,
                    source=ch.name,
                    title=v.title,
                    url=v.url,
                    language=meta.get("language") or "ur",
                    text=text,
                    segments=segments,
                    duration_seconds=v.duration_seconds,
                    transcriber="groq" if meta.get("model") not in (None, "metadata") else "metadata",
                    model=meta.get("model") or "",
                    confidence=meta.get("confidence") or {},
                )
                session.add(tr)
                _upsert_cache(session, v, ch, text)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    tr = session.execute(
                        select(Transcript).where(Transcript.video_id == v.video_id)
                    ).scalar_one_or_none()
    elif tr is not None:
        b.transcription_status = "done"

    if tr is None and settings.youtube_metadata_only:
        text = f"{v.title}\n{v.description}"
        segments = []
        _upsert_cache(session, v, ch, text)
        session.commit()
        hits_map = {}
        matched_labels = [m.keyword for m in find_matches(text, keywords)]
        if matched_labels:
            # No timestamps in metadata-only mode.
            hits_map = {
                label: [matcher.KeywordHit(label, "en", 0, 0, v.title[:200])]
                for label in matched_labels
            }
        _emit_mention(session, notifier, b, ch, slot, v, hits_map, summary)
        b.discovery_status = "ready" if matched_labels else "no_match"
        session.commit()
        return

    if tr is None:
        b.discovery_status = "failed"
        b.error = b.error or "transcription unavailable (configure authorized media source)"
        session.commit()
        summary["failed"] += 1
        return

    hits_map = matcher.find_all_hits(tr.text or "", tr.segments or [], keywords)

    _emit_mention(session, notifier, b, ch, slot, v, hits_map, summary, transcript=tr)
    b.discovery_status = "ready" if hits_map else "no_match"
    b.transcription_status = "done"
    b.last_processed_at = datetime.now(timezone.utc)
    session.commit()


def _youtube_keyword_langs(session, labels: list[str] | set[str] | None = None) -> dict[str, str]:
    q = select(Keyword.text, Keyword.language).where(Keyword.module == "youtube")
    if labels:
        q = q.where(Keyword.text.in_(list(labels)))
    return {t: lang or "ur" for t, lang in session.execute(q).all() if t}


def _sanitize_youtube_mention(session, mention: Mention) -> bool:
    """Keep only verified transcript hits on a YouTube mention row."""
    labels = list(mention.matched_keywords or [])
    langs = _youtube_keyword_langs(session, labels)
    verified, clean_hits = matcher.prune_stored_hits(mention.keyword_hits or {}, langs)
    mention.matched_keywords = verified
    mention.keyword_hits = clean_hits
    if mention.keyword_media:
        mention.keyword_media = {
            k: v for k, v in mention.keyword_media.items() if k in verified
        }
    if not verified:
        return False
    first_hit = None
    for kw in verified:
        for hit in clean_hits.get(kw) or []:
            if isinstance(hit, dict) and hit.get("excerpt"):
                first_hit = hit
                break
        if first_hit:
            break
    if first_hit:
        mention.snippet = (first_hit.get("excerpt") or mention.snippet or "")[:240]
        if first_hit.get("start") is not None:
            mention.deeplink_seconds = int(first_hit["start"])
            if mention.external_id:
                mention.url = discovery.deep_link(mention.external_id, mention.deeplink_seconds)
    return True


def _utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _process_period_video(
    session,
    notifier,
    ch: YouTubeChannel,
    v: discovery.Video,
    keywords: list[tuple[str, str]],
    summary: dict,
    *,
    section_label: str,
    force: bool,
) -> None:
    if v.live:
        summary["skipped_live"] = summary.get("skipped_live", 0) + 1
        return

    tr = session.execute(
        select(Transcript).where(Transcript.video_id == v.video_id)
    ).scalar_one_or_none()
    upgrade_metadata = _is_metadata_only(tr)
    needs_transcribe = tr is None or upgrade_metadata

    if needs_transcribe and not settings.youtube_metadata_only:
        asset = media_source.acquire_audio(
            video_id=v.video_id,
            video_url=v.url or f"https://www.youtube.com/watch?v={v.video_id}",
            media_source=ch.media_source or settings.youtube_media_source,
            media_source_config=ch.media_source_config or {},
        )
        if asset is None:
            text = f"{v.title}\n{v.description}"
            segments: list[dict] = []
            meta = {"model": "metadata", "confidence": {}}
        else:
            try:
                text, segments, meta = transcribe.transcribe_audio(
                    asset.path,
                    language="ur",
                    prompt=settings.youtube_transcribe_prompt,
                )
                summary["cost_usd_est"] += transcribe.estimate_cost_usd(
                    float(v.duration_seconds or meta.get("duration") or 0),
                    model=meta.get("model"),
                )
                summary["transcribed"] = summary.get("transcribed", 0) + 1
            finally:
                media_source.cleanup_asset(asset)

        if text or segments:
            if upgrade_metadata and tr is not None:
                tr.bulletin_id = None
                tr.channel_id = ch.channel_id
                tr.source = ch.name
                tr.title = v.title
                tr.url = v.url
                tr.language = meta.get("language") or "ur"
                tr.text = text
                tr.segments = segments
                tr.duration_seconds = v.duration_seconds
                tr.is_live = False
                tr.transcriber = (
                    "groq" if meta.get("model") not in (None, "metadata") else "metadata"
                )
                tr.model = meta.get("model") or ""
                tr.confidence = meta.get("confidence") or {}
                _upsert_cache(session, v, ch, text)
                session.commit()
            elif tr is None:
                tr = Transcript(
                    video_id=v.video_id,
                    bulletin_id=None,
                    channel_id=ch.channel_id,
                    source=ch.name,
                    title=v.title,
                    url=v.url,
                    language=meta.get("language") or "ur",
                    text=text,
                    segments=segments,
                    duration_seconds=v.duration_seconds,
                    is_live=False,
                    transcriber="groq" if meta.get("model") not in (None, "metadata") else "metadata",
                    model=meta.get("model") or "",
                    confidence=meta.get("confidence") or {},
                )
                session.add(tr)
                _upsert_cache(session, v, ch, text)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    tr = session.execute(
                        select(Transcript).where(Transcript.video_id == v.video_id)
                    ).scalar_one_or_none()
    elif tr is None and settings.youtube_metadata_only:
        text = f"{v.title}\n{v.description}"
        segments = []
        tr = Transcript(
            video_id=v.video_id,
            channel_id=ch.channel_id,
            source=ch.name,
            title=v.title,
            url=v.url,
            language="ur",
            text=text,
            segments=segments,
            duration_seconds=v.duration_seconds,
            is_live=False,
            transcriber="metadata",
            model="metadata",
        )
        session.add(tr)
        _upsert_cache(session, v, ch, text)
        session.commit()
    elif tr is not None and not force and not upgrade_metadata:
        pass  # use existing transcript
    elif tr is None:
        summary["failed"] = summary.get("failed", 0) + 1
        return

    if tr is None:
        return

    hits_map = matcher.find_all_hits(tr.text or "", tr.segments or [], keywords)
    if not hits_map:
        return

    class _B:
        slot_date = (
            v.published.astimezone(_PKT).date().isoformat()
            if v.published else datetime.now(_PKT).date().isoformat()
        )
        id = None

    class _S:
        label = "Period"
        local_time = "00:00:00"

    _emit_mention(
        session, notifier, _B(), ch, _S(), v, hits_map, summary,
        transcript=tr, section_override=section_label,
    )


def _emit_mention(
    session, notifier, b, ch, slot, v, hits_map, summary, transcript=None,
    section_override: str | None = None,
) -> None:
    hits_map = {k: v for k, v in hits_map.items() if v}
    if not hits_map:
        return
    labels = sorted(hits_map.keys())
    first_hit = None
    for kw in labels:
        if hits_map[kw]:
            first_hit = hits_map[kw][0]
            break
    deeplink_s = first_hit.start if first_hit else None
    url = discovery.deep_link(v.video_id, deeplink_s)
    snippet = (first_hit.excerpt if first_hit else "") or (v.title or "")[:240]
    hits_json = matcher.hits_to_json(hits_map)

    # One frame per keyword at that keyword's own timestamp.
    media: dict[str, str] = {}
    for kw, kw_hits in hits_map.items():
        if not kw_hits:
            continue
        h = kw_hits[0]
        safe_kw = re.sub(r"[^\w\-]+", "_", kw, flags=re.UNICODE)[:40]
        out = settings.storage_dir / "youtube" / f"{v.video_id}_{safe_kw}_{h.start}s.jpg"
        ok = frame.capture_frame(
            v.video_id,
            h.start,
            out,
            mode=ch.media_source or settings.youtube_media_source,
        )
        if not ok:
            continue
        frame.stamp_frame(
            out,
            channel=ch.name,
            slot_label=slot.label or slot.local_time or "Period",
            slot_date=b.slot_date,
            mmss=frame.format_mmss(h.start),
        )
        frame_path = str(out)
        media[kw] = frame_path
        for item in hits_json.get(kw, [])[:1]:
            item["screenshot"] = frame_path

    existing = session.execute(
        select(Mention).where(Mention.module == "youtube", Mention.external_id == v.video_id)
    ).scalar_one_or_none()

    section = section_override or f"{slot.label or slot.local_time} · {b.slot_date}"
    if existing:
        present = {(k or "").casefold() for k in (existing.matched_keywords or [])}
        new_kw = [k for k in labels if k.casefold() not in present]
        merged_hits = dict(existing.keyword_hits or {})
        merged_hits.update(hits_json)
        existing.keyword_hits = merged_hits
        merged_media = dict(existing.keyword_media or {})
        merged_media.update(media)
        existing.keyword_media = merged_media
        if not _sanitize_youtube_mention(session, existing):
            session.delete(existing)
            session.commit()
            return
        if deeplink_s is not None and existing.deeplink_seconds is None:
            existing.deeplink_seconds = deeplink_s
            existing.url = url
        if media and not existing.screenshot_path:
            existing.screenshot_path = next(iter(media.values()))
        existing.section = section
        session.commit()
        fresh_kw = [k for k in new_kw if k in (existing.matched_keywords or [])]
        if fresh_kw:
            _alert(notifier, session, existing, fresh_kw, summary)
        return

    mention = Mention(
        module="youtube",
        external_id=v.video_id,
        source=ch.name,
        section=section,
        title=v.title or f"{ch.name} bulletin",
        url=url,
        matched_keywords=labels,
        keyword_media=media,
        keyword_hits=hits_json,
        snippet=snippet,
        summary=snippet,
        screenshot_path=next(iter(media.values()), None),
        deeplink_seconds=deeplink_s,
        published_at=v.published or classifier.slot_airtime(
            b.slot_date, slot.local_time, ch.timezone
        ).astimezone(timezone.utc),
    )
    session.add(mention)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return
    summary["mentions"] += 1
    _alert(notifier, session, mention, labels, summary)


def _rematch_bulletin(session, notifier, b, ch, slot, keywords, summary) -> None:
    if not b.video_id:
        return
    tr = session.execute(
        select(Transcript).where(Transcript.video_id == b.video_id)
    ).scalar_one_or_none()
    text = tr.text if tr else ""
    segments = tr.segments if tr else []
    if not text:
        cache = session.execute(
            select(ArticleCache).where(
                ArticleCache.module == "youtube",
                ArticleCache.external_id == b.video_id,
            )
        ).scalar_one_or_none()
        text = cache.body if cache else ""
    if not text:
        return
    hits_map = matcher.find_all_hits(text, segments or [], keywords)
    class _V:
        pass
    v = _V()
    v.video_id = b.video_id
    v.title = b.title
    v.description = ""
    v.url = f"https://www.youtube.com/watch?v={b.video_id}"
    v.published = b.published_at
    v.duration_seconds = b.duration_seconds
    _emit_mention(session, notifier, b, ch, slot, v, hits_map, summary, transcript=tr)


def _match_cached(
    session, notifier, keyword_ids=None, slot_date: str | None = None,
    slot_times: list[str] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    summary = {"mentions": 0, "alerts": 0, "pages_checked": 0}
    keywords = _active_youtube_keywords(session, keyword_ids)
    if not keywords:
        return summary
    since = result_policy.search_cutoff()
    q = select(Transcript).where(Transcript.created_at >= since)
    if slot_date:
        b_rows = session.execute(
            select(YouTubeBulletin.id, YouTubeBulletin.video_id).where(
                YouTubeBulletin.slot_date == slot_date
            )
        ).all()
        b_ids = [r[0] for r in b_rows]
        v_ids = [r[1] for r in b_rows if r[1]]
        clauses = []
        if b_ids:
            clauses.append(Transcript.bulletin_id.in_(b_ids))
        if v_ids:
            clauses.append(Transcript.video_id.in_(v_ids))
        if not clauses:
            return summary
        q = q.where(or_(*clauses))
    transcripts = session.execute(q).scalars().all()
    if period_start and period_end:
        p_start, p_end = parse_period_iso(period_start, period_end)
        filtered: list[Transcript] = []
        for tr in transcripts:
            pub = None
            if tr.video_id:
                row = session.execute(
                    select(Mention.published_at).where(
                        Mention.module == "youtube",
                        Mention.external_id == tr.video_id,
                    ).limit(1)
                ).scalar_one_or_none()
                pub = row
            if pub is None:
                pub = tr.created_at
            if pub and _utc_naive(pub) >= p_start and _utc_naive(pub) <= p_end:
                filtered.append(tr)
        transcripts = filtered
    if slot_times and slot_date:
        slots = {s.id: s for s in session.execute(select(BulletinSlot)).scalars()}
        allowed = set(slot_times)
        allowed_bids: set[int] = set()
        for bid, sid in session.execute(
            select(YouTubeBulletin.id, YouTubeBulletin.slot_id).where(
                YouTubeBulletin.slot_date == slot_date
            )
        ).all():
            slot = slots.get(sid)
            if slot and slot.local_time in allowed:
                allowed_bids.add(bid)
        transcripts = [tr for tr in transcripts if tr.bulletin_id in allowed_bids]
    channels = {
        c.channel_id: c
        for c in session.execute(select(YouTubeChannel)).scalars()
    }
    for tr in transcripts:
        summary["pages_checked"] = summary.get("pages_checked", 0) + 1
        hits_map = matcher.find_all_hits(tr.text or "", tr.segments or [], keywords)
        if not hits_map:
            continue
        ch = channels.get(tr.channel_id or "")
        if ch is None:
            continue
        b = None
        if tr.bulletin_id:
            b = session.get(YouTubeBulletin, tr.bulletin_id)
        slot = session.get(BulletinSlot, b.slot_id) if b else None
        if b is None or slot is None:
            # Synthetic bulletin-less match still creates a mention.
            class _B:
                slot_date = (tr.created_at or datetime.now(timezone.utc)).astimezone(_PKT).date().isoformat()
                id = None
            class _S:
                label = "Cached"
                local_time = "00:00:00"
            b, slot = _B(), _S()
        class _V:
            pass
        v = _V()
        v.video_id = tr.video_id
        v.title = tr.title
        v.description = ""
        v.url = tr.url
        v.published = tr.created_at
        v.duration_seconds = tr.duration_seconds
        before = summary["mentions"]
        _emit_mention(session, notifier, b, ch, slot, v, hits_map, summary, transcript=tr)
        if summary["mentions"] == before:
            # Existing mention may have been updated; still count as activity.
            pass
    return summary


def _active_youtube_keywords(session, keyword_ids=None) -> list[tuple[str, str]]:
    q = select(Keyword).where(Keyword.active.is_(True), Keyword.module == "youtube")
    if keyword_ids:
        q = q.where(Keyword.id.in_(keyword_ids))
    return [(k.text, k.language) for k in session.execute(q).scalars() if k.text]


def _upsert_cache(session, v, ch, text: str) -> None:
    row = session.execute(
        select(ArticleCache).where(
            ArticleCache.module == "youtube",
            ArticleCache.external_id == v.video_id,
        )
    ).scalar_one_or_none()
    if row:
        row.body = text or row.body
        row.title = v.title or row.title
        row.fetched_at = datetime.now(timezone.utc)
    else:
        session.add(
            ArticleCache(
                module="youtube",
                external_id=v.video_id,
                source=ch.name,
                section="bulletin",
                title=v.title or "",
                url=v.url,
                body=text or "",
            )
        )


def _alert(notifier, session, mention, keywords, summary) -> None:
    from app.notifiers.base import Alert

    try:
        ok = notifier.send(Alert(
            source=mention.source,
            title=mention.title,
            summary=mention.summary or "",
            sentiment=mention.sentiment,
            url=mention.url,
            image_path=mention.screenshot_path,
            matched_keywords=keywords,
        ))
        if ok:
            mention.notified = True
            session.commit()
            summary["alerts"] = summary.get("alerts", 0) + 1
    except Exception as exc:
        logger.warning("youtube alert failed: %s", exc)


def _is_metadata_only(tr: Transcript | None) -> bool:
    """True when the row is title/description only — needs real audio → Groq."""
    if tr is None:
        return False
    model = (tr.model or "").lower()
    transcriber = (tr.transcriber or "").lower()
    return model == "metadata" or transcriber == "metadata"


def _is_due(b: YouTubeBulletin, slot: BulletinSlot, ch: YouTubeChannel) -> bool:
    if b.discovery_status in ("ready", "no_match", "missing", "needs_review") and not (
        b.transcription_status in ("pending", "failed")
    ):
        # Still allow retries for waiting/failed discovery.
        if b.discovery_status in ("missing", "needs_review", "ready", "no_match"):
            return False
    air = classifier.slot_airtime(b.slot_date, slot.local_time, ch.timezone)
    now = datetime.now(air.tzinfo)
    elapsed_min = (now - air).total_seconds() / 60.0
    if elapsed_min < settings.youtube_process_delay_minutes:
        return False
    offsets = classifier.parse_retry_offsets()
    # Due if we've crossed the next retry offset beyond attempts.
    attempt = b.attempts or 0
    # First due at delay; subsequent at retry offsets.
    needed = offsets[min(attempt, len(offsets) - 1)]
    return elapsed_min >= needed


def _past_missing_deadline(b, slot, ch) -> bool:
    air = classifier.slot_airtime(b.slot_date, slot.local_time, ch.timezone)
    now = datetime.now(air.tzinfo)
    return (now - air).total_seconds() >= settings.youtube_missing_after_minutes * 60


def _slot_effective(slot: BulletinSlot, day: str) -> bool:
    if slot.effective_from and day < slot.effective_from:
        return False
    if slot.effective_to and day > slot.effective_to:
        return False
    return True


def _public_status(b: YouTubeBulletin) -> str:
    if b.discovery_status == "transcribing" or b.transcription_status == "running":
        return "transcribing"
    if b.discovery_status == "discovering":
        return "discovering"
    if b.discovery_status == "ready":
        return "ready"
    if b.discovery_status == "no_match":
        return "no keyword match"
    if b.discovery_status == "missing":
        return "late/missing"
    if b.discovery_status == "failed":
        return "failed"
    if b.discovery_status == "needs_review":
        return "needs review"
    return "waiting"
