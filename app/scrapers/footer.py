"""Metadata footer overlay for captured screenshots.

Stamps every screenshot (full-page + cropped) with a footer bar carrying:
publication | date/time PKT | section | source URL | scrape timestamp.

Footer text is ASCII/Latin (publication names, URL, timestamps) so no Urdu
shaping is needed. Uses Pillow; degrades gracefully (leaves the image as-is) if
anything fails, so a footer problem never loses a screenshot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Pakistan Standard Time is a fixed UTC+5 (no DST) — avoids tzdata on Windows.
PKT = timezone(timedelta(hours=5))

_FOOTER_H = 84
_PAD = 14
_BG = (17, 17, 17)
_FG = (245, 245, 245)
_ACCENT = (198, 40, 40)  # matches the app's red


def _load_font(size: int):
    from PIL import ImageFont

    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "Verdana.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def add_footer(
    image_path: Path,
    publication: str,
    section: str | None,
    url: str,
    scraped_at: datetime | None = None,
) -> bool:
    """Add the metadata footer to the image at `image_path` (in place)."""
    try:
        from PIL import Image, ImageDraw

        scraped_at = scraped_at or datetime.now(timezone.utc)
        pkt = scraped_at.astimezone(PKT)

        src = Image.open(image_path).convert("RGB")
        w, h = src.size
        canvas = Image.new("RGB", (w, h + _FOOTER_H), _BG)
        canvas.paste(src, (0, 0))

        draw = ImageDraw.Draw(canvas)
        # Red accent line separating the shot from the footer.
        draw.rectangle([0, h, w, h + 3], fill=_ACCENT)

        bold = _load_font(22)
        reg = _load_font(16)
        small = _load_font(14)

        y = h + 10
        line1 = f"{publication}"
        if section:
            line1 += f"   |   {section}"
        line1 += f"   |   {pkt:%d %b %Y, %H:%M} PKT"
        draw.text((_PAD, y), line1, font=bold, fill=_FG)

        # URL (truncated) + scrape timestamp
        url_txt = url if len(url) <= 96 else url[:93] + "..."
        draw.text((_PAD, y + 30), url_txt, font=reg, fill=(200, 200, 200))
        draw.text(
            (_PAD, y + 52),
            f"captured {scraped_at.astimezone(PKT):%Y-%m-%d %H:%M:%S} PKT  ·  Media Monitor",
            font=small,
            fill=(150, 150, 150),
        )

        # PIL's default JPEG quality (75) visibly softens newsprint — save high.
        if str(image_path).lower().endswith((".jpg", ".jpeg")):
            canvas.save(image_path, quality=92, subsampling=1)
        else:
            canvas.save(image_path)
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("Footer overlay failed for %s: %s", image_path, exc)
        return False
