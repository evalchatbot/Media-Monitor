"""Classify recent uploads into expected bulletin slots."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.youtube.discovery import Video
from config import settings

_PKT = ZoneInfo(settings.youtube_timezone)


@dataclass
class Classification:
    video: Video
    score: float
    reasons: list[str]


def slot_airtime(slot_date: str, local_time: str, tz: str | None = None) -> datetime:
    """Return timezone-aware airtime for a slot on a calendar date."""
    zone = ZoneInfo(tz or settings.youtube_timezone)
    d = date.fromisoformat(slot_date)
    hh, mm, ss = (int(x) for x in local_time.split(":"))
    # Midnight (00:00) belongs to slot_date as the late-night edition for that day.
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=zone)


def classify_candidates(
    videos: list[Video],
    *,
    slot_date: str,
    local_time: str,
    title_rules: list[str],
    min_duration_sec: int = 120,
    max_duration_sec: int | None = None,
    window_minutes: int | None = None,
    tz: str | None = None,
) -> list[Classification]:
    """Score videos for a bulletin slot. Highest score first."""
    zone = ZoneInfo(tz or settings.youtube_timezone)
    air = slot_airtime(slot_date, local_time, tz)
    window = timedelta(minutes=window_minutes or settings.youtube_discovery_window_minutes)
    max_dur = max_duration_sec or settings.youtube_max_duration_seconds
    rules = [r.casefold() for r in (title_rules or []) if r]

    scored: list[Classification] = []
    for v in videos:
        if v.live:
            continue
        reasons: list[str] = []
        score = 0.0
        title = (v.title or "").casefold()

        # Duration gates — skip shorts / multi-hour livestream VODs.
        if v.duration_seconds is not None:
            if v.duration_seconds < min_duration_sec:
                continue
            if v.duration_seconds > max_dur:
                continue
            if 300 <= v.duration_seconds <= 1800:
                score += 2.0
                reasons.append("typical_duration")
            elif v.duration_seconds <= 2400:
                score += 1.0
                reasons.append("ok_duration")

        # Title bulletin tokens.
        hit_rules = [r for r in rules if r and r in title]
        if hit_rules:
            score += min(4.0, 1.5 * len(hit_rules))
            reasons.append("title:" + ",".join(hit_rules[:4]))
        if any(tok in title for tok in ("headline", "headlines", "bulletin")):
            score += 2.0
            reasons.append("bulletin_word")

        # Publication window relative to expected airtime.
        if v.published is not None:
            pub = v.published
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            pub_local = pub.astimezone(zone)
            delta = abs((pub_local - air).total_seconds())
            if delta <= window.total_seconds():
                # Closer uploads score higher.
                proximity = 1.0 - (delta / max(window.total_seconds(), 1))
                score += 3.0 + 2.0 * proximity
                reasons.append(f"pub_within_{int(delta // 60)}m")
            elif delta <= window.total_seconds() * 2:
                score += 1.0
                reasons.append("pub_near")
            else:
                # Far outside the window — only keep if title is a strong match.
                if not hit_rules:
                    continue
                reasons.append("pub_far")

            # Calendar ownership for midnight / late uploads.
            if _same_slot_date(pub_local, air, local_time):
                score += 1.0
                reasons.append("date_match")

        if score <= 0:
            continue
        scored.append(Classification(video=v, score=score, reasons=reasons))

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def pick_best(scored: list[Classification], *, tie_margin: float = 0.75) -> tuple[
    Classification | None, list[Classification], bool
]:
    """Return (winner, rejected, needs_review).

    If the top two scores are within `tie_margin`, mark needs_review and do not
    auto-select a winner.
    """
    if not scored:
        return None, [], False
    if len(scored) >= 2 and (scored[0].score - scored[1].score) < tie_margin:
        return None, scored, True
    return scored[0], scored[1:], False


def _same_slot_date(pub_local: datetime, air: datetime, local_time: str) -> bool:
    """Midnight slots may publish just after midnight; still own that calendar day."""
    if local_time.startswith("00:"):
        # Accept late-night uploads from air-30m through air+6h on the slot date.
        return pub_local.date() == air.date() or (
            pub_local.date() == (air.date() - timedelta(days=1))
            and pub_local.hour >= 22
        )
    return pub_local.date() == air.date()


def parse_retry_offsets() -> list[int]:
    raw = settings.youtube_retry_offsets_minutes or ""
    out = [settings.youtube_process_delay_minutes]
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    # Unique, sorted.
    return sorted({m for m in out if m > 0})
