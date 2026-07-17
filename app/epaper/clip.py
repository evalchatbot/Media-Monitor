"""Cut the matched news item out of an e-paper page — a verified press clipping.

Locating an article on a page is a GROUNDING task, and small vision models are
much better at reading than pointing. So the clipper is built as a pipeline
where every stage can only make the result safer:

1. LOCATE — best available provider:
     - Gemini (GEMINI_API_KEY set): asked for a native bounding box; Google's
       models are trained for detection and return precise boxes.
     - Groq/Llama otherwise: raw coordinates hallucinate, so the page gets a
       LABELED GRID overlay (rows A-J, cols 1-8) and the model just names the
       top-left and bottom-right cells the item spans — recognition, not
       regression, which weak models handle far better.
2. REPAIR — the crop is shown back to the model: "is the headline visible?
   is text cut at any edge?" Flagged edges are extended (up to 2 rounds), so
   mid-article cuts get healed instead of shipped.
3. VERIFY — final crop must be confirmed to contain the matched text, or the
   whole attempt is discarded and the detection keeps the stamped full page.

Everything crops from the ORIGINAL full-resolution scan (grid/downscaling is
only ever on the model's copy), and saves at JPEG q92.
"""
from __future__ import annotations

import io
import hashlib
import json
import logging
import time
from base64 import standard_b64encode
from pathlib import Path

import httpx

from config import settings

from app.epaper.reader import _MAX_EDGE, _encode

logger = logging.getLogger(__name__)

# Set when a call gives up after exhausting 429 retries, so a batch job (reclip)
# can tell "rate-limited" apart from "genuinely couldn't clip" and stop early.
_RATE_LIMITED = False


def pop_rate_limited() -> bool:
    """True if the last make_clipping() failed due to rate limiting (consumes)."""
    global _RATE_LIMITED
    v = _RATE_LIMITED
    _RATE_LIMITED = False
    return v


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")

_ROWS = "ABCDEFGHIJ"   # 10 rows -> 10% bands
_COLS = 8              # 8 cols  -> 12.5% bands

_GRID_PROMPT = """This newspaper page has a labeled red grid: rows {rows} (top to
bottom) and columns 1-{cols} (left to right). Find the ONE news item (headline +
its body columns) that contains this text{lang_hint}:

"{needle}"

Answer with the grid cells the WHOLE item spans (include its headline; exclude
advertisements and unrelated neighbouring items).
Respond ONLY with JSON: {{"found": true|false, "top_left": "B2", "bottom_right": "D4"}}"""

_GEMINI_PROMPT = """Find the single news item (its headline plus all its body
columns) on this newspaper page that contains this text{lang_hint}:

"{needle}"

Exclude advertisements and unrelated neighbouring items. Respond ONLY with JSON:
{{"found": true|false, "box_2d": [ymin, xmin, ymax, xmax]}} with coordinates
normalized to 0-1000. found=false if the text is not on this page."""

_REPAIR_PROMPT = """This image is a clipping cut from a newspaper page, meant to
show the complete news item containing{lang_hint}: "{needle}"

Inspect the EDGES. Respond ONLY with JSON:
{{"headline_visible": true|false, "cut_top": true|false, "cut_bottom": true|false,
"cut_left": true|false, "cut_right": true|false}}
A side is "cut" when text/columns of THIS item continue past that edge."""

_VERIFY_PROMPT = """This image is a clipping cut from a newspaper page.
1) Does it contain this passage or its headline{lang_hint}: "{needle}"?
2) If yes, give the bounding box around ONE clear occurrence of the exact term
   "{keyword}" inside it (prefer an occurrence in a headline; a tight box around
   just those words, not the whole article).
Respond ONLY with JSON: {{"contains": true|false, "box_2d": [ymin, xmin, ymax, xmax]}}
box_2d normalized 0-1000, or null if that term isn't visible."""


# ------------------------------------------------------------------ public --
def clip_from_box(page_path: str | Path, box: dict, source: str, page_no: int,
                  link: str) -> str | None:
    """Crop an EXACT, publisher-provided article box (from an image-map) — no AI.
    Enhanced + footer-stamped like a vision clip. `box` is percent {l,t,r,b}."""
    page_path = Path(page_path)
    if not page_path.exists():
        return None
    fingerprint = hashlib.sha1(
        json.dumps(box, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    out = page_path.with_name(f"{page_path.stem}_clip_box_{fingerprint}.jpg")
    if not _crop(page_path, box, out, pad_pct=1.0):
        return None
    _enhance(out)
    from app.scrapers.footer import add_footer

    add_footer(out, source, f"E-Paper p{page_no} · clipping", link)
    return str(out)


def make_clipping(page_path: str | Path, keyword: str, snippet: str,
                  source: str, page_no: int, link: str,
                  language: str = "en") -> str | None:
    """Return the path of a stamped, verified clipping — or None (use full page)."""
    global _RATE_LIMITED
    _RATE_LIMITED = False
    page_path = Path(page_path)
    if not page_path.exists() or not (settings.gemini_api_key or settings.groq_api_key):
        return None
    needle = snippet.strip() if len(snippet.strip()) >= len(keyword) + 8 else keyword
    needle = needle[:220]
    hint = " (Urdu, Nastaliq script)" if language == "ur" else ""

    use_gemini = bool(settings.gemini_api_key)
    box = (_locate_gemini(page_path, needle, hint) if use_gemini
           else _locate_grid(page_path, needle, hint))
    if not box:
        return None

    fingerprint = hashlib.sha1(
        f"{language}:{keyword}".encode("utf-8")
    ).hexdigest()[:12]
    out = page_path.with_name(f"{page_path.stem}_clip_kw_{fingerprint}.jpg")
    # Gemini boxes are precise — trust them with a little extra padding and skip
    # the (call-hungry) repair loop, so a clip is just locate + verify. The grid
    # path IS coordinate-guessy, so it keeps the completeness repair.
    if not _crop(page_path, box, out, pad_pct=3.5 if use_gemini else 2.0):
        return None
    if not use_gemini:
        for _ in range(2):
            flags = _ask_json(_REPAIR_PROMPT.format(needle=needle, lang_hint=hint), out)
            if not flags:
                break
            grow = {"t": 8 * bool(flags.get("cut_top") or not flags.get("headline_visible", True)),
                    "b": 8 * bool(flags.get("cut_bottom")),
                    "l": 7 * bool(flags.get("cut_left")),
                    "r": 7 * bool(flags.get("cut_right"))}
            if not any(grow.values()):
                break
            box = {"l": max(0, box["l"] - grow["l"]), "t": max(0, box["t"] - grow["t"]),
                   "r": min(100, box["r"] + grow["r"]), "b": min(100, box["b"] + grow["b"])}
            if not _crop(page_path, box, out):
                break

    v = _ask_json(_VERIFY_PROMPT.format(needle=needle, keyword=keyword[:60], lang_hint=hint), out)
    if not (v and v.get("contains")):
        out.unlink(missing_ok=True)
        logger.info("clip rejected on verification (%s p%s, %r)", source, page_no, keyword)
        return None

    # Light contrast pass so low-res source scans read clearly without mushy upscaling.
    _enhance(out)
    # Highlight the matched term on the clip (Gemini returns its box in verify —
    # free, no extra call). A slightly-off highlighter smear reads worse than a
    # box outline, so we outline.
    kwbox = _box2d(v.get("box_2d")) if use_gemini else None
    if kwbox:
        _highlight(out, kwbox)

    from app.scrapers.footer import add_footer

    add_footer(out, source, f"E-Paper p{page_no} · clipping", link)
    return str(out)


# ------------------------------------------------------------- locate: grid --
def _locate_grid(page_path: Path, needle: str, hint: str) -> dict | None:
    """Weak-model-friendly localization: name grid cells, not coordinates."""
    b64 = _grid_overlay_b64(page_path)
    if not b64:
        return None
    data = _ask_json(
        _GRID_PROMPT.format(rows=f"{_ROWS[0]}-{_ROWS[-1]}", cols=_COLS,
                            needle=needle, lang_hint=hint),
        None, image_b64=b64,
    )
    if not (data and data.get("found")):
        return None
    tl, br = _cell(data.get("top_left")), _cell(data.get("bottom_right"))
    if not (tl and br):
        return None
    (r1, c1), (r2, c2) = tl, br
    if r2 < r1 or c2 < c1:
        (r1, r2), (c1, c2) = (min(r1, r2), max(r1, r2)), (min(c1, c2), max(c1, c2))
    box = {"l": c1 * (100 / _COLS), "t": r1 * (100 / len(_ROWS)),
           "r": (c2 + 1) * (100 / _COLS), "b": (r2 + 1) * (100 / len(_ROWS))}
    return _sane(box)


def _cell(name) -> tuple[int, int] | None:
    """'C3' -> (row_index, col_index)."""
    if not isinstance(name, str) or len(name) < 2:
        return None
    row = _ROWS.find(name[0].upper())
    try:
        col = int(name[1:]) - 1
    except ValueError:
        return None
    if row < 0 or not (0 <= col < _COLS):
        return None
    return row, col


def _grid_overlay_b64(page_path: Path) -> str | None:
    """Downscaled copy with labeled red grid lines (model's eyes only)."""
    try:
        from PIL import Image, ImageDraw

        from app.scrapers.footer import _load_font

        img = Image.open(page_path).convert("RGB")
        w, h = img.size
        scale = _MAX_EDGE / max(w, h)
        if scale < 1:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = img.size
        draw = ImageDraw.Draw(img)
        font = _load_font(max(16, h // 60))
        red = (220, 30, 30)
        for i in range(1, _COLS):
            x = w * i / _COLS
            draw.line([(x, 0), (x, h)], fill=red, width=2)
        for j in range(1, len(_ROWS)):
            y = h * j / len(_ROWS)
            draw.line([(0, y), (w, y)], fill=red, width=2)
        for i in range(_COLS):          # column numbers along the top
            draw.text((w * (i + 0.5) / _COLS, 4), str(i + 1), fill=red, font=font, anchor="ma")
        for j in range(len(_ROWS)):     # row letters along the left
            draw.text((4, h * (j + 0.5) / len(_ROWS)), _ROWS[j], fill=red, font=font, anchor="lm")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return standard_b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logger.warning("grid overlay failed: %s", exc)
        return None


# ----------------------------------------------------------- locate: gemini --
def _locate_gemini(page_path: Path, needle: str, hint: str) -> dict | None:
    """Precise boxes from a model actually trained for detection."""
    b64, media = _encode(page_path)
    url = _GEMINI_URL.format(model=settings.gemini_model)
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": media, "data": b64}},
            {"text": _GEMINI_PROMPT.format(needle=needle, lang_hint=hint)},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    global _RATE_LIMITED
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": settings.gemini_api_key},
                           json=body, timeout=120)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 503):
            if r.status_code == 429 and attempt == 3:
                _RATE_LIMITED = True
            time.sleep(_gemini_wait(r, attempt))
            continue
        if r.status_code != 200:
            logger.warning("clip locate (gemini): %s %s", r.status_code, r.text[:120])
            return None
        try:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(txt)
        except Exception:
            return None
        if not data.get("found"):
            return None
        try:
            ymin, xmin, ymax, xmax = [float(v) / 10 for v in data["box_2d"]]
        except (KeyError, TypeError, ValueError):
            return None
        return _sane({"l": xmin, "t": ymin, "r": xmax, "b": ymax})
    return None


# ------------------------------------------------------------------ helpers --
def _ask_json(prompt: str, image_path: Path | None, image_b64: str | None = None) -> dict | None:
    """One JSON-mode vision call on the active provider (Gemini > Groq)."""
    if settings.gemini_api_key:
        return _ask_gemini_json(prompt, image_path, image_b64)
    return _ask_groq_json(prompt, image_path, image_b64)


def _ask_groq_json(prompt, image_path, image_b64=None) -> dict | None:
    if image_b64 is None:
        image_b64, media = _encode(image_path)
    else:
        media = "image/jpeg"
    payload = {
        "model": settings.groq_model, "temperature": 0, "max_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
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
            logger.warning("clip call (groq): %s", r.status_code)
            return None
        try:
            return json.loads(r.json()["choices"][0]["message"]["content"])
        except Exception:
            return None
    return None


def _ask_gemini_json(prompt, image_path, image_b64=None) -> dict | None:
    if image_b64 is None:
        image_b64, media = _encode(image_path)
    else:
        media = "image/jpeg"
    url = _GEMINI_URL.format(model=settings.gemini_model)
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": media, "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    global _RATE_LIMITED
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": settings.gemini_api_key}, json=body, timeout=120)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 503):
            if r.status_code == 429 and attempt == 3:
                _RATE_LIMITED = True
            time.sleep(_gemini_wait(r, attempt))
            continue
        if r.status_code != 200:
            return None
        try:
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception:
            return None
    return None


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
    return min(12 * (attempt + 1), 65)


def _sane(box: dict) -> dict | None:
    l, t, r, b = box["l"], box["t"], box["r"], box["b"]
    if not (0 <= l < r <= 100 and 0 <= t < b <= 100):
        return None
    w, h = r - l, b - t
    if w < 10 or h < 4:
        return None
    if w * h > 75 * 100:
        return None
    return box


def _box2d(v) -> dict | None:
    """Gemini box_2d [ymin,xmin,ymax,xmax] (0-1000) -> percent dict."""
    try:
        ymin, xmin, ymax, xmax = [float(x) / 10 for x in v]
    except (TypeError, ValueError):
        return None
    if not (0 <= xmin < xmax <= 100 and 0 <= ymin < ymax <= 100):
        return None
    return {"l": xmin, "t": ymin, "r": xmax, "b": ymax}


def _crop(src: Path, box: dict, out: Path, pad_pct: float = 2.0) -> bool:
    """Crop the ORIGINAL scan at full resolution (clean; enhancement is a later,
    single step so repair re-crops don't compound)."""
    try:
        from PIL import Image

        img = Image.open(src).convert("RGB")
        W, H = img.size
        l = max(0, int((box["l"] - pad_pct) / 100 * W))
        t = max(0, int((box["t"] - pad_pct) / 100 * H))
        r = min(W, int((box["r"] + pad_pct) / 100 * W))
        b = min(H, int((box["b"] + pad_pct) / 100 * H))
        if r - l < 40 or b - t < 40:
            return False
        img.crop((l, t, r, b)).save(out, quality=96, subsampling=0)
        return True
    except Exception as exc:
        logger.warning("clip crop failed for %s: %s", src, exc)
        return False


def _enhance(path: Path) -> None:
    """Native-resolution contrast + light sharpen — never upscale.

    Stretching newsprint creates mushy letters. Keep the scan's own pixels,
    gently separate ink from paper, then a small-radius unsharp so body text
    reads clearly without halos or plastic look."""
    try:
        import cv2
        from PIL import Image

        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            return

        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        # Mild CLAHE — readable ink without blowing paper white.
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        l = clahe.apply(l)
        img = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

        # Small-radius unsharp: sharper glyphs, not heavy "AI" crispness.
        blurred = cv2.GaussianBlur(img, (0, 0), 0.55)
        img = cv2.addWeighted(img, 1.28, blurred, -0.28, 0)

        # PIL saves 4:4:4 (subsampling=0) — OpenCV JPEG often softens chroma.
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        Image.fromarray(rgb).save(path, quality=96, subsampling=0, optimize=True)
    except Exception as exc:
        logger.warning("clip enhance failed for %s: %s", path, exc)


def _highlight(path: Path, box: dict) -> None:
    """Outline the matched term's region (semi-transparent — never obscures)."""
    try:
        from PIL import Image, ImageDraw

        img = Image.open(path).convert("RGB")
        W, H = img.size
        pad_x, pad_y = W * 0.006, H * 0.004
        l = max(0, box["l"] / 100 * W - pad_x)
        t = max(0, box["t"] / 100 * H - pad_y)
        r = min(W, box["r"] / 100 * W + pad_x)
        b = min(H, box["b"] / 100 * H + pad_y)
        if r - l < 4 or b - t < 4:
            return
        # If the "term" box covers most of the clip, the model didn't pinpoint —
        # skip rather than frame the whole article.
        if (box["r"] - box["l"]) * (box["b"] - box["t"]) > 55 * 100:
            return
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rectangle([l, t, r, b], fill=(255, 231, 0, 46))          # faint highlighter
        line = max(3, int(H / 320))
        od.rectangle([l, t, r, b], outline=(230, 150, 0, 235), width=line)  # amber frame
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        img.save(path, quality=95, subsampling=0)
    except Exception as exc:
        logger.warning("clip highlight failed for %s: %s", path, exc)
