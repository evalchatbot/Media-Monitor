"""Read a scanned e-paper page into text with Claude vision.

E-paper pages are images of the printed paper — Urdu ones in Nastaliq script,
which conventional OCR (tesseract) can't read reliably. Claude's vision reads
both English and Nastaliq Urdu well, so one call per page yields the text that
keyword matching then runs on (with the exact same matcher the website articles
use). The text is cached on the EPaperPage row, so each page is read ONCE ever;
re-matching new keywords later costs nothing.

Needs ANTHROPIC_API_KEY. Without it the pipeline marks pages 'no_key' and the
UI explains what to add — pages are still fetched and browsable.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# Newsprint is dense; cap the long edge so token cost stays sane while headlines
# and body text stay legible to the model.
_MAX_EDGE = 1568

_PROMPT = """This image is one full page of a Pakistani newspaper's print edition. \
It may be in English or in Urdu (Nastaliq script).

Transcribe ALL readable text on the page, top to bottom:
- every headline and sub-headline
- article body text
- photo captions and boxed items
- keep Urdu in Urdu script exactly as printed; keep English in English
- separate items with blank lines
- if a region is too blurry to read, skip it silently

Output the transcription only — no commentary, no translation."""


def has_key() -> bool:
    return bool(settings.anthropic_api_key)


def read_page(image_path: str | Path) -> str:
    """Return the page's text. Raises on API failure (caller records status)."""
    import anthropic

    b64, media_type = _encode(Path(image_path))
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    model = settings.epaper_ocr_model or settings.llm_model
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    logger.info("e-paper read: %s -> %d chars", Path(image_path).name, len(text))
    return text


def _encode(path: Path) -> tuple[str, str]:
    """Downscale to a token-sane size and return (base64, media_type)."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = _MAX_EDGE / max(w, h)
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
