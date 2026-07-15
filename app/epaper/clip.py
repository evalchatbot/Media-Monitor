"""Cut the matched news item out of an e-paper page — a press clipping.

A detection on a print page is far more useful as the actual clipping than as
a whole broadsheet squeezed into a card. The page is just pixels, so the vision
model that already read it is asked once more: "where on this page is the item
containing this text?" The returned box (percent coordinates) is padded,
sanity-checked, cropped with PIL at full source resolution, and footer-stamped.

Anything questionable — no key, model can't find it, box fails sanity checks —
returns None and the caller falls back to the full stamped page, so clippings
can only improve a detection, never lose one.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from config import settings

from app.epaper.reader import _encode  # same downscaling the reader uses

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

_PROMPT = """This image is a full newspaper page. Find the ONE news item (article,
headline block, or notice) that contains this text{lang_hint}:

"{needle}"

Respond ONLY with JSON:
{{"found": true|false, "left": N, "top": N, "right": N, "bottom": N}}

Rules:
- left/top/right/bottom are percentages (0-100) of image width/height
- the box must include the item's HEADLINE and all its body columns
- use the SMALLEST box that fully contains just that one item
- EXCLUDE advertisements and neighbouring unrelated items
- if the text is not on this page, use found=false."""

_VERIFY_PROMPT = """This image is a clipping cut from a newspaper page. Does it
contain this text (or its headline){lang_hint}: "{needle}"?
Respond ONLY with JSON: {{"contains": true|false}}"""


def make_clipping(page_path: str | Path, keyword: str, snippet: str,
                  source: str, page_no: int, link: str,
                  language: str = "en") -> str | None:
    """Return the path of a stamped clipping for `keyword`, or None."""
    page_path = Path(page_path)
    if not (settings.groq_api_key and page_path.exists()):
        return None
    box = _locate(page_path, keyword, snippet, language)
    if not box:
        return None
    out = page_path.with_name(f"{page_path.stem}_clip_{abs(hash(keyword)) % 99999}.jpg")
    if not _crop(page_path, box, out):
        return None
    # Trust nothing: the model confirms the CROP actually contains the text.
    # A wrong-position sliver is worse than the full page, so it gets rejected.
    if not _verify(out, keyword, snippet, language):
        out.unlink(missing_ok=True)
        logger.info("clip rejected on verification (%s p%s, %r)", source, page_no, keyword)
        return None
    from app.scrapers.footer import add_footer

    add_footer(out, source, f"E-Paper p{page_no} · clipping", link)
    return str(out)


def _locate(page_path: Path, keyword: str, snippet: str, language: str) -> dict | None:
    b64, media = _encode(page_path)
    needle = snippet.strip() if len(snippet.strip()) >= len(keyword) + 8 else keyword
    hint = " (Urdu, Nastaliq script)" if language == "ur" else ""
    payload = {
        "model": settings.groq_model,
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}},
                {"type": "text", "text": _PROMPT.format(needle=needle[:220], lang_hint=hint)},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    for attempt in range(3):
        try:
            r = httpx.post(_GROQ_URL, headers=headers, json=payload, timeout=120)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(min(float(r.headers.get("retry-after") or 3 * (attempt + 1)), 45))
            continue
        if r.status_code != 200:
            logger.warning("clip locate: groq %s", r.status_code)
            return None
        try:
            data = json.loads(r.json()["choices"][0]["message"]["content"])
        except Exception:
            return None
        return _sane(data)
    return None


def _sane(d: dict) -> dict | None:
    """Reject boxes that smell hallucinated; a bad crop is worse than none."""
    if not d.get("found"):
        return None
    try:
        l, t, rr, b = (float(d["left"]), float(d["top"]),
                       float(d["right"]), float(d["bottom"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= l < rr <= 100 and 0 <= t < b <= 100):
        return None
    w, h = rr - l, b - t
    if w < 14 or h < 6:         # implausibly small — likely a hallucinated sliver
        return None
    if w * h > 70 * 100:        # most of the page — clipping adds nothing
        return None
    return {"l": l, "t": t, "r": rr, "b": b}


def _verify(clip_path: Path, keyword: str, snippet: str, language: str) -> bool:
    """Second opinion on the crop itself: does it really contain the text?"""
    b64, media = _encode(clip_path)
    needle = snippet.strip() if len(snippet.strip()) >= len(keyword) + 8 else keyword
    hint = " (Urdu, Nastaliq script)" if language == "ur" else ""
    payload = {
        "model": settings.groq_model,
        "temperature": 0,
        "max_tokens": 60,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}},
                {"type": "text",
                 "text": _VERIFY_PROMPT.format(needle=needle[:220], lang_hint=hint)},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    for attempt in range(3):
        try:
            r = httpx.post(_GROQ_URL, headers=headers, json=payload, timeout=90)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(min(float(r.headers.get("retry-after") or 3 * (attempt + 1)), 45))
            continue
        if r.status_code != 200:
            return False
        try:
            return bool(json.loads(r.json()["choices"][0]["message"]["content"]).get("contains"))
        except Exception:
            return False
    return False


def _crop(src: Path, box: dict, out: Path, pad_pct: float = 3.0) -> bool:
    try:
        from PIL import Image

        img = Image.open(src).convert("RGB")
        W, H = img.size
        l = max(0, int((box["l"] - pad_pct) / 100 * W))
        t = max(0, int((box["t"] - pad_pct) / 100 * H))
        r = min(W, int((box["r"] + pad_pct) / 100 * W))
        b = min(H, int((box["b"] + pad_pct) / 100 * H))
        img.crop((l, t, r, b)).save(out, quality=92, subsampling=1)
        return True
    except Exception as exc:
        logger.warning("clip crop failed for %s: %s", src, exc)
        return False
