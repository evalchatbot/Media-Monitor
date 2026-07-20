"""YouTube bulletin pipeline: discover → classify → transcribe → match → frame."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.core import result_policy
from app.core.keywords import find_matches
from app.db.base import SessionLocal
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


def slot_date_is_past(slot_date: str) -> bool:
    """True when the bulletin calendar day is before today (PKT)."""
    try:
        return date.fromisoformat(slot_date) < datetime.now(_PKT).date()
    except ValueError:
        return False


def run_youtube_scan(
    *,
    channel_ids: list[int] | None = None,
    keyword_ids: list[int] | None = None,
    slot_date: str | None = None,
    force: bool = False,
    match_only: bool = False,
) -> dict:
    """Process due bulletins (or rematch cached transcripts when match_only)."""
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
        if match_only:
            summary.update(
                _match_cached(
                    session, notifier,
                    keyword_ids=keyword_ids,
                    slot_date=slot_date,
                )
            )
            result_policy.enforce_limits(session)
            return summary

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
        summary["channels"] = len(channels)

        keywords = _active_youtube_keywords(session, keyword_ids)
        historical = bool(slot_date and slot_date_is_past(slot_date))
        effective_force = force or historical
        for b in bulletins:
            summary["bulletins_checked"] += 1
            ch = channels.get(b.channel_db_id)
            slot = slots.get(b.slot_id)
            if not ch or not slot or not slot.enabled:
                continue
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
) -> dict:
    """Match keywords against cached transcripts (optionally one bulletin date)."""
    return run_youtube_scan(
        keyword_ids=keyword_ids,
        slot_date=slot_date,
        match_only=True,
    )


def run_youtube_date_search(
    slot_date: str,
    keyword_ids: list[int] | None = None,
) -> dict:
    """Discover, transcribe if needed, and match every bulletin slot on one date."""
    return run_youtube_scan(
        slot_date=slot_date,
        keyword_ids=keyword_ids,
        force=slot_date_is_past(slot_date),
        match_only=False,
    )


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
                prompt = ", ".join(k[0] for k in keywords[:20])
                text, segments, meta = transcribe.transcribe_audio(
                    asset.path,
                    language="ur",
                    prompt=prompt,
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
    # Also admit title matches without timestamps.
    for m in find_matches(f"{v.title}\n{v.description}", keywords):
        hits_map.setdefault(m.keyword, [])

    _emit_mention(session, notifier, b, ch, slot, v, hits_map, summary, transcript=tr)
    b.discovery_status = "ready" if hits_map else "no_match"
    b.transcription_status = "done"
    b.last_processed_at = datetime.now(timezone.utc)
    session.commit()


def _emit_mention(
    session, notifier, b, ch, slot, v, hits_map, summary, transcript=None,
) -> None:
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
    hits_json = matcher.hits_to_json({k: v for k, v in hits_map.items() if v})

    # One frame per video (first timed hit) — avoid N×slow captures per keyword.
    media: dict[str, str] = {}
    frame_path: str | None = None
    if first_hit is not None:
        out = settings.storage_dir / "youtube" / f"{v.video_id}_{first_hit.start}s.jpg"
        ok = frame.capture_frame(
            v.video_id,
            first_hit.start,
            out,
            mode=ch.media_source or settings.youtube_media_source,
        )
        if ok:
            frame.stamp_frame(
                out,
                channel=ch.name,
                slot_label=slot.label or slot.local_time,
                slot_date=b.slot_date,
                mmss=frame.format_mmss(first_hit.start),
            )
            frame_path = str(out)
    if frame_path:
        for kw, hits in hits_map.items():
            if not hits:
                continue
            media[kw] = frame_path
            for item in hits_json.get(kw, [])[:1]:
                item["screenshot"] = frame_path

    existing = session.execute(
        select(Mention).where(Mention.module == "youtube", Mention.external_id == v.video_id)
    ).scalar_one_or_none()

    section = f"{slot.label or slot.local_time} · {b.slot_date}"
    if existing:
        present = {(k or "").casefold() for k in (existing.matched_keywords or [])}
        new_kw = [k for k in labels if k.casefold() not in present]
        existing.matched_keywords = sorted(set(existing.matched_keywords or []) | set(labels))
        merged_hits = dict(existing.keyword_hits or {})
        merged_hits.update(hits_json)
        existing.keyword_hits = merged_hits
        merged_media = dict(existing.keyword_media or {})
        merged_media.update(media)
        existing.keyword_media = merged_media
        if deeplink_s is not None and existing.deeplink_seconds is None:
            existing.deeplink_seconds = deeplink_s
            existing.url = url
        if media and not existing.screenshot_path:
            existing.screenshot_path = next(iter(media.values()))
        if not existing.snippet:
            existing.snippet = snippet
        existing.section = section
        session.commit()
        if new_kw:
            _alert(notifier, session, existing, new_kw, summary)
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
