"""Read a scanned e-paper page into text with a vision LLM.

E-paper pages are images of the printed paper — Urdu ones in Nastaliq script,
which conventional OCR (tesseract) can't read reliably. A multimodal LLM reads
both English and Nastaliq Urdu, so one call per page yields the text that
keyword matching then runs on (with the exact same matcher the website articles
use). The text is cached on the EPaperPage row, so each page is read ONCE ever;
re-matching new keywords later costs nothing.

Providers (first available key wins):
  GROQ_API_KEY       — Groq, Llama-4 multimodal (default GROQ_MODEL, with an
                       automatic fallback to Scout if the model is unavailable)
  ANTHROPIC_API_KEY  — Claude vision

Without any key the pipeline marks pages 'no_key' and the UI explains what to
add — pages are still fetched and browsable.
"""
from __future__ import annotations

import base64
import io
import logging
import time
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Newsprint is dense; cap the long edge so token cost stays sane while headlines
# and body text stay legible to the model.
_MAX_EDGE = 1568

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_FALLBACK_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def _groq_vision_model() -> str:
    return getattr(settings, "groq_vision_model", None) or settings.groq_model

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
    return bool(settings.gemini_api_key or settings.groq_api_key or settings.anthropic_api_key)


def provider() -> str:
    if settings.gemini_api_key:
        return "gemini"
    if settings.groq_api_key:
        return "groq"
    if settings.anthropic_api_key:
        return "anthropic"
    return "none"


def read_page(image_path: str | Path) -> str:
    """Return the page's text. Raises on API failure (caller records status).

    Gemini is preferred: Groq dropped its Llama-4 vision models, so the Groq path
    now 404s. Gemini reads Urdu Nastaliq well and is already used for tickers and
    clippings."""
    if settings.gemini_api_key:
        return _read_gemini(Path(image_path))
    if settings.groq_api_key:
        return _read_groq(Path(image_path))
    if settings.anthropic_api_key:
        return _read_anthropic(Path(image_path))
    raise RuntimeError("no vision API key configured")


# ----------------------------------------------------------------- groq -----
def _read_groq(image_path: Path) -> str:
    b64, media_type = _encode(image_path)
    payload = {
        "model": _groq_vision_model(),
        "temperature": 0,
        "max_tokens": 16000,
        # Qwen3 is a reasoning model — with thinking ON it burns the entire token
        # budget "thinking" and never emits the transcription. Turning reasoning
        # off makes it transcribe directly.
        "reasoning_effort": "none",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    last: Exception | None = None
    for attempt in range(5):
        try:
            r = httpx.post(_GROQ_URL, headers=headers, json=payload, timeout=180)
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404 and payload["model"] != _GROQ_FALLBACK_MODEL:
            logger.info("groq: %s unavailable — falling back to %s",
                        payload["model"], _GROQ_FALLBACK_MODEL)
            payload["model"] = _GROQ_FALLBACK_MODEL
            continue
        if r.status_code in (429, 500, 502, 503):
            # Rate limit / transient — honour Retry-After when present.
            wait = float(r.headers.get("retry-after") or (3 * (attempt + 1)))
            logger.info("groq: %s — retrying in %.0fs", r.status_code, wait)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        text = _strip_reasoning(text).strip()
        logger.info("e-paper read (groq): %s -> %d chars", image_path.name, len(text))
        return text
    raise RuntimeError(f"groq read failed after retries: {last}")


def _strip_reasoning(text: str) -> str:
    """Drop any <think>…</think> a reasoning model leaks into content."""
    import re as _re
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.S)
    # Truncated/unclosed reasoning (model spent the whole budget thinking).
    if "<think>" in text and "</think>" not in text:
        return ""
    return text


# --------------------------------------------------------------- gemini -----
def _read_gemini(image_path: Path) -> str:
    b64, media_type = _encode(image_path)
    model = settings.gemini_model or "gemini-flash-latest"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [
            {"text": _PROMPT},
            {"inline_data": {"mime_type": media_type, "data": b64}},
        ]}],
        # A dense full page can run long; keep the cap high so text isn't cut off
        # (truncated text = missed keyword matches).
        "generationConfig": {"temperature": 0, "maxOutputTokens": 16384},
    }
    last: Exception | None = None
    for attempt in range(5):
        try:
            r = httpx.post(url, params={"key": settings.gemini_api_key},
                           json=body, timeout=180)
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            wait = float(r.headers.get("retry-after") or (3 * (attempt + 1)))
            logger.info("gemini: %s — retrying in %.0fs", r.status_code, wait)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError):
            text = ""
        logger.info("e-paper read (gemini): %s -> %d chars", image_path.name, len(text))
        return text
    raise RuntimeError(f"gemini read failed after retries: {last}")


# ------------------------------------------------------------- anthropic ----
def _read_anthropic(image_path: Path) -> str:
    import anthropic

    b64, media_type = _encode(image_path)
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
    logger.info("e-paper read (claude): %s -> %d chars", image_path.name, len(text))
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
