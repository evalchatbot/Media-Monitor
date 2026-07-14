"""Build the daily digest HTML from the last 24h of detections.

Groups mentions by source, shows per-source sentiment counts, and embeds each
screenshot as an inline base64 thumbnail so the HTML is fully self-contained
(renders in an email client or a saved file with no external requests).
"""
from __future__ import annotations

import base64
import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Mention

logger = logging.getLogger(__name__)

PKT = timezone(timedelta(hours=5))
_THUMB_W = 240


def _thumb_data_uri(path_str: str | None) -> str | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.exists():
        return None
    try:
        from PIL import Image

        img = Image.open(p).convert("RGB")
        w, h = img.size
        if w > _THUMB_W:
            img = img.resize((_THUMB_W, int(h * _THUMB_W / w)))
        # Cap height so tall full-page shots become a consistent thumbnail (crop top).
        max_h = 170
        if img.size[1] > max_h:
            img = img.crop((0, 0, img.size[0], max_h))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception as exc:
        logger.warning("thumbnail failed for %s: %s", path_str, exc)
        return None


def build_digest(hours: int = 24) -> tuple[str, int]:
    """Return (html_document, mention_count) for the last `hours`."""
    session = SessionLocal()
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        mentions = (
            session.execute(
                select(Mention).where(Mention.detected_at >= since).order_by(Mention.detected_at.desc())
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    by_source: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        by_source[m.source].append(m)

    now_pkt = datetime.now(PKT)
    parts = [_head(now_pkt, len(mentions), len(by_source))]

    for source in sorted(by_source):
        items = by_source[source]
        sent = _sentiment_counts(items)
        parts.append(
            f'<h2 style="margin:24px 0 6px;color:#c62828">{html.escape(source)} '
            f'<span style="font-size:14px;color:#666">({len(items)})</span></h2>'
            f'<div style="color:#555;font-size:13px;margin-bottom:8px">{sent}</div>'
        )
        for m in items:
            parts.append(_mention_row(m))

    if not mentions:
        parts.append('<p style="color:#666">No detections in the last 24 hours.</p>')

    parts.append("</div></body></html>")
    return "".join(parts), len(mentions)


def _sentiment_counts(items: list[Mention]) -> str:
    counts = {"Positive": 0, "Critical": 0, "Neutral": 0, "Unscored": 0}
    for m in items:
        counts[m.sentiment if m.sentiment in counts else "Unscored"] += 1
    chips = []
    colors = {"Positive": "#1c6b1c", "Critical": "#b71c1c", "Neutral": "#555", "Unscored": "#999"}
    for k, v in counts.items():
        if v:
            chips.append(f'<span style="color:{colors[k]}">{k}: {v}</span>')
    return " &nbsp;·&nbsp; ".join(chips) or "—"


def _mention_row(m: Mention) -> str:
    thumb = _thumb_data_uri(m.screenshot_path) or _thumb_data_uri(m.full_screenshot_path)
    img = (
        f'<img src="{thumb}" width="{_THUMB_W}" style="border:1px solid #ddd;border-radius:6px;display:block">'
        if thumb
        else ""
    )
    kws = ", ".join(m.matched_keywords or [])
    when = m.detected_at.astimezone(PKT).strftime("%d %b %H:%M PKT") if m.detected_at else ""
    tag = "🗞 e-paper" if m.module == "epaper" else "📰 article"
    return f"""
    <table style="margin:10px 0;border-collapse:collapse"><tr>
      <td style="vertical-align:top;padding-right:12px">{img}</td>
      <td style="vertical-align:top">
        <a href="{html.escape(m.url)}" style="font-weight:600;color:#111;text-decoration:none;font-size:15px">{html.escape(m.title)}</a>
        <div style="color:#666;font-size:12px;margin:3px 0">{tag} · {when} · {html.escape(m.sentiment or 'unscored')}</div>
        <div style="color:#c62828;font-size:12px">keywords: {html.escape(kws)}</div>
        <div style="color:#444;font-size:13px;margin-top:4px">{html.escape(m.summary or '')}</div>
      </td>
    </tr></table>"""


def _head(now_pkt: datetime, total: int, sources: int) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
    <body style="margin:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif">
    <div style="max-width:680px;margin:0 auto;background:#fff;padding:24px">
    <div style="background:#c62828;color:#fff;padding:16px 20px;border-radius:8px">
      <div style="font-size:20px;font-weight:700">📡 Media Monitor — Daily Digest</div>
      <div style="opacity:.9;font-size:13px;margin-top:4px">{now_pkt:%A, %d %B %Y} · {total} detection(s) across {sources} source(s)</div>
    </div>"""
