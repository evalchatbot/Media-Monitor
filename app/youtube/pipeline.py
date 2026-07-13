"""YouTube monitoring pipeline — parallel to the newspaper one.

Shares the same keyword system, LLM scoring, Mention table, and alert pipeline.
Videos are detected via RSS (no API key). Each new video's title + description
(+ transcript, if transcription is enabled) is matched against active keywords;
matches become Mentions tagged module="youtube" with a timestamp deep-link.

Transcripts are cached in ArticleCache (module="youtube") so a video is only
transcribed once. In the default `stub` transcriber, matching runs on title +
description alone — fully useful and runnable with no GPU/API key.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import scoring
from app.core.keywords import find_matches
from app.db.base import SessionLocal
from app.db.models import ArticleCache, Keyword, Mention, Transcript, YouTubeChannel
from app.youtube import rss, transcribe
from app.notifiers import get_notifier
from app.notifiers.base import Alert
from config import settings

logger = logging.getLogger(__name__)


def _active_keywords(session, keyword_ids):
    # YouTube scans only use keywords scoped to the youtube module.
    stmt = select(Keyword).where(Keyword.active.is_(True), Keyword.module == "youtube")
    if keyword_ids:
        stmt = stmt.where(Keyword.id.in_(keyword_ids))
    return [(k.text, k.language) for k in session.execute(stmt).scalars()]


def run_youtube_scan(keyword_ids=None, channel_ids=None) -> dict:
    session = SessionLocal()
    notifier = get_notifier()
    summary = {"channels": 0, "videos": 0, "cached": 0, "mentions": 0, "alerts": 0}
    try:
        keywords = _active_keywords(session, keyword_ids)
        if not keywords:
            logger.info("YouTube: no active keywords; nothing to do.")
            return summary

        stmt = select(YouTubeChannel).where(YouTubeChannel.active.is_(True))
        if channel_ids:
            stmt = stmt.where(YouTubeChannel.id.in_(channel_ids))
        channels = session.execute(stmt).scalars().all()
        if not channels:
            logger.info("YouTube: no active channels configured.")
            return summary

        for ch in channels:
            summary["channels"] += 1
            videos = rss.fetch_uploads(ch.channel_id)[: settings.youtube_max_videos_per_scan]
            logger.info("YouTube %s: %d recent videos", ch.name or ch.channel_id, len(videos))
            for v in videos:
                summary["videos"] += 1
                _process_video(session, ch, v, keywords, notifier, summary)
        return summary
    finally:
        session.close()


def _process_video(session, ch, v: "rss.Video", keywords, notifier, summary) -> None:
    cache = session.execute(
        select(ArticleCache).where(
            ArticleCache.module == "youtube", ArticleCache.external_id == v.video_id
        )
    ).scalar_one_or_none()

    segments: list[dict] = []
    if cache is None:
        transcript, segments = transcribe.transcribe(v.url)
        body = "\n".join(p for p in [v.description, transcript] if p)
        cache = ArticleCache(
            module="youtube",
            external_id=v.video_id,
            source=v.channel_name or ch.name,
            section="YouTube",
            title=v.title,
            url=v.url,
            body=body,
        )
        session.add(cache)
        try:
            session.commit()
            summary["cached"] += 1
        except IntegrityError:
            session.rollback()
            cache = session.execute(
                select(ArticleCache).where(
                    ArticleCache.module == "youtube", ArticleCache.external_id == v.video_id
                )
            ).scalar_one()
        # Persist a real transcript (with timestamps) as a first-class row.
        if transcript:
            _save_transcript(session, v, ch, transcript, segments)

    haystack = f"{v.title}\n{cache.body}"
    matches = find_matches(haystack, keywords)
    if not matches:
        return

    matched_kw = sorted({m.keyword for m in matches})

    mention = session.execute(
        select(Mention).where(
            Mention.module == "youtube", Mention.external_id == v.video_id
        )
    ).scalar_one_or_none()

    if mention is not None:
        new_kw = [k for k in matched_kw if k not in (mention.matched_keywords or [])]
        if not new_kw:
            return
        mention.matched_keywords = sorted(set(mention.matched_keywords or []) | set(matched_kw))
        session.commit()
        _alert(notifier, session, mention, new_kw, summary)
        return

    # Deep-link timestamp: exact second the keyword is first spoken.
    sec = None
    for kw in matched_kw:
        sec = transcribe.find_keyword_second(segments, kw)
        if sec is not None:
            break
    deeplink = v.url + (f"&t={sec}s" if sec else "")

    scored = scoring.score(v.title, cache.body, matched_kw) or {}
    if scored.get("relevance") == "Not Relevant":
        logger.info("YouTube: skipping '%s' — LLM Not Relevant", v.title[:50])
        return

    # If we know the exact second, grab the video frame at that moment so the
    # detection shows what was on screen when the keyword was said.
    shot = _capture_video_frame(v, ch, sec, deeplink)

    snippet = _snippet(haystack, matched_kw)
    mention = Mention(
        module="youtube",
        external_id=v.video_id,
        source=v.channel_name or ch.name,
        section="YouTube",
        title=v.title,
        url=deeplink,
        matched_keywords=matched_kw,
        snippet=snippet,
        summary=scored.get("summary") or snippet,
        relevance=scored.get("relevance"),
        sentiment=scored.get("sentiment"),
        screenshot_path=shot,
        deeplink_seconds=sec,
        published_at=v.published,
    )
    session.add(mention)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return
    summary["mentions"] += 1
    _alert(notifier, session, mention, matched_kw, summary)


def run_youtube_live_scan(keyword_ids=None, channel_ids=None) -> dict:
    """Check active channels for live broadcasts; tap a ~30s chunk, transcribe it,
    and alert on keyword hits with a deep-link to the live moment.

    No-op unless YOUTUBE_LIVE_ENABLED=true. Even then it degrades gracefully if
    ffmpeg or a non-stub transcriber is missing (logs, doesn't crash).
    """
    summary = {"channels": 0, "live": 0, "tapped": 0, "mentions": 0, "alerts": 0}
    if not settings.youtube_live_enabled:
        logger.info("YouTube live scan disabled (set YOUTUBE_LIVE_ENABLED=true to enable).")
        return summary

    import shutil

    from app.youtube import livestream

    session = SessionLocal()
    notifier = get_notifier()
    try:
        keywords = _active_keywords(session, keyword_ids)
        if not keywords:
            return summary
        stmt = select(YouTubeChannel).where(YouTubeChannel.active.is_(True))
        if channel_ids:
            stmt = stmt.where(YouTubeChannel.id.in_(channel_ids))
        channels = session.execute(stmt).scalars().all()

        for ch in channels:
            summary["channels"] += 1
            live = livestream.detect_live(ch.channel_id)
            if not live:
                continue
            summary["live"] += 1
            logger.info("LIVE: %s — %s", ch.name or ch.channel_id, live.title[:60])

            audio = livestream.tap_live_chunk(live.url, settings.youtube_live_chunk_seconds)
            if not audio:
                continue  # not tappable (ffmpeg missing) — already logged
            summary["tapped"] += 1
            try:
                transcript, segments = transcribe.transcribe_file(audio)
            finally:
                shutil.rmtree(audio.parent, ignore_errors=True)
            if transcript:
                _save_transcript(session, live, ch, transcript, segments, is_live=True)

            haystack = f"{live.title}\n{transcript}"
            matches = find_matches(haystack, keywords)
            if not matches:
                continue
            matched_kw = sorted({m.keyword for m in matches})
            deeplink = live.url + (f"&t={live.elapsed}s" if live.elapsed else "")

            ext_id = f"live:{live.video_id}"
            mention = session.execute(
                select(Mention).where(
                    Mention.module == "youtube", Mention.external_id == ext_id
                )
            ).scalar_one_or_none()

            if mention is not None:
                new_kw = [k for k in matched_kw if k not in (mention.matched_keywords or [])]
                if not new_kw:
                    continue
                mention.matched_keywords = sorted(set(mention.matched_keywords or []) | set(matched_kw))
                session.commit()
                _alert(notifier, session, mention, new_kw, summary)
                continue

            scored = scoring.score(live.title, transcript, matched_kw) or {}
            if scored.get("relevance") == "Not Relevant":
                continue
            snippet = _snippet(haystack, matched_kw)
            mention = Mention(
                module="youtube",
                external_id=ext_id,
                source=live.title and (ch.name or ch.channel_id),
                section="YouTube Live",
                title=f"🔴 LIVE: {live.title}",
                url=deeplink,
                matched_keywords=matched_kw,
                snippet=snippet,
                summary=scored.get("summary") or snippet,
                relevance=scored.get("relevance"),
                sentiment=scored.get("sentiment"),
                deeplink_seconds=live.elapsed,
            )
            session.add(mention)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            summary["mentions"] += 1
            _alert(notifier, session, mention, matched_kw, summary)

        return summary
    finally:
        session.close()


def _alert(notifier, session, mention: Mention, keywords, summary) -> None:
    ok = notifier.send(
        Alert(
            source=mention.source,
            title=mention.title,
            summary=mention.summary or "",
            sentiment=mention.sentiment,
            url=mention.url,
            deeplink=mention.url,
            matched_keywords=keywords,
        )
    )
    if ok:
        mention.notified = True
        session.commit()
        summary["alerts"] += 1


def _capture_video_frame(v, ch, sec, deeplink) -> str | None:
    """Grab the video frame at `sec` and stamp a footer. Returns a path or None.

    Best-effort: needs ffmpeg + a resolvable stream. Never blocks a scan.
    """
    if sec is None:
        return None
    from pathlib import Path

    from app.scrapers.footer import add_footer
    from app.youtube import frame

    out = settings.storage_dir / "youtube" / f"{v.video_id}_{int(sec)}s.jpg"
    if not frame.capture_frame(v.url, sec, out):
        return None
    ts = f"▶ @ {int(sec) // 60}:{int(sec) % 60:02d}"
    add_footer(Path(out), getattr(v, "channel_name", "") or getattr(ch, "name", ""), ts, deeplink)
    return str(out)


def _save_transcript(session, v, ch, text, segments, is_live=False) -> None:
    """Upsert a Transcript row for a video (best-effort; never blocks a scan)."""
    exists = session.execute(
        select(Transcript.id).where(Transcript.video_id == v.video_id)
    ).first()
    if exists:
        return
    session.add(
        Transcript(
            video_id=v.video_id,
            channel_id=getattr(ch, "channel_id", None),
            source=getattr(v, "channel_name", "") or getattr(ch, "name", ""),
            title=v.title,
            url=v.url,
            text=text,
            segments=segments or [],
            transcriber=settings.youtube_transcriber,
            is_live=is_live,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()


def _snippet(text: str, keywords, width: int = 160) -> str:
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw.lower())
        if idx != -1:
            start = max(0, idx - width // 2)
            end = min(len(text), idx + len(kw) + width // 2)
            return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return text[:width].strip()
