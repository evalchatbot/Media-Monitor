"""Read a scanned e-paper page into text with a vision LLM.

E-paper pages are images of the printed paper — Urdu ones in Nastaliq script,
which conventional OCR (tesseract) can't read. A multimodal LLM reads both
English and Nastaliq, so one call per page yields the text keyword matching then
runs on (with the exact same matcher the website articles use).

Provider reality (measured against live pages, see `_QUALITY` notes below):

  GEMINI_API_KEY     — reads Nastaliq Urdu AND English reliably, and returns
                       native bounding boxes for the clipper. Preferred, and the
                       ONLY provider that works for Urdu. Free key at
                       https://aistudio.google.com/apikey
  ANTHROPIC_API_KEY  — Claude vision; good on both scripts, paid, no grounding.
  GROQ_API_KEY       — Qwen vision. Fine on ENGLISH. It CANNOT read Urdu
                       Nastaliq: it recognises the masthead, invents a plausible
                       date, then repeats one line hundreds of times. Never
                       trusted for Urdu, and its English output is guarded too.

Because a degenerate transcription silently fabricates keyword matches — far
worse than reading nothing — every result passes `looks_degenerate()` before it
is returned. Failing text is discarded and the next provider is tried.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import time
from collections import Counter
from pathlib import Path

import httpx
from PIL import Image, ImageFile

from config import settings

# Some publisher scans (Express Urdu) are served slightly truncated; without this
# PIL raises "image file is truncated" and the page is never read at all.
ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger(__name__)

# Newsprint is dense: at 1568px the body text was illegible and models only
# transcribed headlines. 2600 keeps fine print readable.
#
# Do NOT upscale small scans. It was the obvious idea (Dawn serves 660x934) and
# it measurably backfires: LANCZOS-upscaled newsprint pushed Tribune's front page
# from a clean 3.2k-char read straight into a repetition loop (979 of 998 lines
# identical). Publishers' native pixels are what the encoder handles best.
_MAX_EDGE = 2600

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")


def _groq_vision_model() -> str:
    return getattr(settings, "groq_vision_model", None) or settings.groq_model


_PROMPT = """This image is one page of a Pakistani newspaper's print edition. \
It may be in English or in Urdu (Nastaliq script).

Transcribe the text on the page, top to bottom:
- every headline and sub-headline
- the body text of each article and column
- photo captions, boxed items and tables

Rules:
- Keep Urdu in Urdu script exactly as printed; keep English in English.
- Separate distinct items with a blank line.
- Transcribe ONLY what you can actually read. If part of the page is unreadable,
  skip it — do NOT guess, and do NOT invent dates, names or headlines.
- Never repeat a line. If you find yourself repeating, stop instead.

Output the transcription only, no commentary and no translation."""


def has_key() -> bool:
    return bool(settings.gemini_api_key or settings.groq_api_key or settings.anthropic_api_key)


def provider() -> str:
    """The provider that would be tried first for a mixed/unknown page."""
    if settings.gemini_api_key:
        return "gemini"
    if settings.anthropic_api_key:
        return "anthropic"
    if settings.groq_api_key:
        return "groq"
    return "none"


def can_read_urdu() -> bool:
    """True if a provider that can actually read Nastaliq is configured.

    Groq alone is NOT enough — it produces confident garbage on Urdu, which
    turns into fabricated keyword matches. Callers use this to tell the user
    what's missing rather than silently returning nothing (or worse, nonsense).
    """
    return bool(settings.gemini_api_key or settings.anthropic_api_key)


# ------------------------------------------------------------ quality gate ---
def looks_degenerate(text: str) -> str:
    """Return a reason string if `text` is a degenerate transcription, else "".

    Vision models under greedy decoding fall into repetition loops on dense
    newsprint: they emit one plausible line hundreds of times. The output looks
    substantial (tens of thousands of characters) but carries almost no real
    content, and any keyword found in the looping line is a false positive.
    """
    if not text or not text.strip():
        return "empty"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 8:
        counts = Counter(lines)
        unique_ratio = len(counts) / len(lines)
        if unique_ratio < 0.55:
            top, n = counts.most_common(1)[0]
            return (f"repetition loop — {len(counts)} unique of {len(lines)} lines; "
                    f"{n}x {top[:40]!r}")
    # Loops that never emit a newline: look for a phrase repeated back to back.
    squashed = re.sub(r"\s+", " ", text)
    if len(squashed) > 800:
        window = squashed[-600:]
        for size in (40, 80, 160):
            chunk = window[-size:]
            if len(chunk) == size and squashed.count(chunk) >= 5:
                return f"repeated {size}-char phrase x{squashed.count(chunk)}"
    return ""


def _accept(text: str, who: str, page: str) -> str | None:
    """Validate one provider's output; return it, or None if unusable."""
    text = (text or "").strip()
    if not text:
        logger.info("e-paper read (%s): %s -> empty", who, page)
        return None
    bad = looks_degenerate(text)
    if bad:
        logger.warning("e-paper read (%s): %s DISCARDED — %s", who, page, bad)
        return None
    logger.info("e-paper read (%s): %s -> %d chars", who, page, len(text))
    return text


# -------------------------------------------------------------------- read ---
def read_page(image_path: str | Path, language: str = "auto") -> str:
    """Return the page's text, trying providers best-first and validating each.

    `language` ("ur" | "en" | "auto") only orders the providers — Groq is never
    tried first for Urdu because it cannot read Nastaliq. A provider whose output
    fails the quality gate is skipped as if it had errored, so one hallucinating
    model can't poison the results.

    Raises RuntimeError if every provider fails or returns unusable text.
    """
    path = Path(image_path)
    providers: list[tuple[str, object]] = []
    if settings.gemini_api_key:
        providers.append(("gemini", _read_gemini))
    if settings.anthropic_api_key:
        providers.append(("anthropic", _read_anthropic))
    if settings.groq_api_key:
        # Groq is competent on English and useless on Urdu. For an Urdu page it
        # stays in the list only as a last resort, where the quality gate will
        # almost certainly reject its output anyway.
        if language == "ur":
            providers.append(("groq", _read_groq))
        else:
            providers.insert(0, ("groq", _read_groq))
    if not providers:
        raise RuntimeError("no vision API key configured")

    errors = []
    for name, fn in providers:
        try:
            text = fn(path)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        good = _accept(text, name, path.name)
        if good:
            return good
        errors.append(f"{name}: unusable output")
    raise RuntimeError("all vision providers failed — " + "; ".join(errors))


# ----------------------------------------------------------------- groq ------
def _read_groq(image_path: Path) -> str:
    b64, media_type = _encode(image_path)
    payload = {
        "model": _groq_vision_model(),
        # NOT zero. Greedy decoding on dense newsprint reliably collapses into a
        # repetition loop (a Dawn front page produced 73k chars at 90% repeated
        # lines); light sampling produced a clean 6k-char read of the same page.
        "temperature": 0.35,
        "top_p": 0.9,
        # A real broadsheet page is ~6-15k chars. A huge budget just gives a
        # looping model more room to loop.
        "max_tokens": 6000,
        # Qwen3 is a reasoning model — with thinking ON it burns the whole budget
        # "thinking" and never emits the transcription.
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
    for attempt in range(4):
        try:
            r = httpx.post(_GROQ_URL, headers=headers, json=payload, timeout=180)
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            wait = float(r.headers.get("retry-after") or (3 * (attempt + 1)))
            logger.info("groq: %s — retrying in %.0fs", r.status_code, wait)
            time.sleep(min(wait, 45))
            continue
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return _strip_reasoning(text).strip()
    raise RuntimeError(f"groq read failed after retries: {last}")


def _strip_reasoning(text: str) -> str:
    """Drop any <think>…</think> a reasoning model leaks into content."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text and "</think>" not in text:
        return ""
    return text


# --------------------------------------------------------------- gemini ------
def _read_gemini(image_path: Path) -> str:
    b64, media_type = _encode(image_path)
    model = settings.gemini_model or "gemini-flash-latest"
    url = _GEMINI_URL.format(model=model)
    body = {
        "contents": [{"parts": [
            {"text": _PROMPT},
            {"inline_data": {"mime_type": media_type, "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16384},
    }
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": settings.gemini_api_key},
                           json=body, timeout=180)
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            wait = _gemini_wait(r, attempt)
            logger.info("gemini: %s — retrying in %.0fs", r.status_code, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError):
            return ""
    raise RuntimeError(f"gemini read failed after retries: {last}")


def _gemini_wait(resp, attempt: int) -> float:
    """Honour Google's RetryInfo (free tier is per-minute), else escalate."""
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if "RetryInfo" in d.get("@type", ""):
                s = d.get("retryDelay", "")
                if s.endswith("s"):
                    return min(float(s[:-1]) + 1, 65)
    except Exception:
        pass
    hdr = resp.headers.get("retry-after")
    if hdr and hdr.isdigit():
        return min(float(hdr) + 1, 65)
    return min(8 * (attempt + 1), 65)


# ------------------------------------------------------------- anthropic -----
def _read_anthropic(image_path: Path) -> str:
    import anthropic

    b64, media_type = _encode(image_path)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    model = settings.epaper_ocr_model or settings.llm_model
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        temperature=0.2,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# ---------------------------------------------------------------- encode -----
def _encode(path: Path) -> tuple[str, str]:
    """Return (base64 JPEG, media_type). Downscale only — never upscale."""
    img = Image.open(path)
    img.load()
    img = img.convert("RGB")
    w, h = img.size
    scale = _MAX_EDGE / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
