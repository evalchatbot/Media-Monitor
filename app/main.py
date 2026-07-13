"""FastAPI application: keyword admin + detections dashboard + scheduler.

Run:  uvicorn app.main:app --reload
  - Admin UI:  http://127.0.0.1:8000/
  - API docs:  http://127.0.0.1:8000/docs

Manual scans (per-keyword or global) run uncapped in a background thread so one
click covers Dawn's whole front page without the browser hanging. The scheduler
keeps running bounded scans every N minutes. Keyword changes take effect on the
next scan with no redeploy — the pipeline reads active keywords from the DB each
run.
"""
from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from config import settings
from app.db.base import SessionLocal, init_db
from app.db.models import Keyword, Mention, YouTubeChannel
from app.newspaper import scan_manager
from app.newspaper.pipeline import run_newspaper_scan
from app.youtube import scan_runner
from app.youtube import rss
from app.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.scheduler_enabled:
        start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Media Monitoring", version="0.2.0", lifespan=lifespan)

settings.storage_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(settings.storage_dir)), name="media")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Shared UI shell (red + white theme)
# --------------------------------------------------------------------------
_CSS = """
:root{
  --bg:#f5f5f7;--surface:#ffffff;--surface-2:#fafafa;
  --ink:#18181b;--muted:#6b7280;--faint:#9ca3af;
  --line:#ececef;--line-strong:#e0e0e4;
  --accent:#dc2626;--accent-strong:#b91c1c;--accent-soft:#fef2f2;--accent-border:#f7d4d4;
  --ok:#15803d;--ok-soft:#f0fdf4;--ok-border:#c3eccf;
  --shadow-sm:0 1px 2px rgba(24,24,27,.05);
  --shadow:0 8px 24px -8px rgba(24,24,27,.12);
  --shadow-lg:0 16px 40px -12px rgba(24,24,27,.22);
  --r:16px;--r-sm:10px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e0e10;--surface:#18181b;--surface-2:#202024;
  --ink:#f4f4f5;--muted:#a1a1aa;--faint:#71717a;
  --line:#27272b;--line-strong:#34343a;
  --accent:#f2555a;--accent-strong:#f87171;--accent-soft:#241416;--accent-border:#3d1e20;
  --ok:#4ade80;--ok-soft:#12211a;--ok-border:#20402c;
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);
  --shadow:0 8px 24px -8px rgba(0,0,0,.55);
  --shadow-lg:0 16px 40px -12px rgba(0,0,0,.65);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--accent-soft);color:var(--accent-strong)}
a{color:inherit}

/* Header */
header{position:sticky;top:0;z-index:30;background:var(--surface);border-bottom:1px solid var(--line);backdrop-filter:saturate(1.4) blur(8px)}
.bar{max-width:1060px;margin:0 auto;display:flex;align-items:center;gap:.5rem;padding:.7rem 1.3rem}
.brand{display:inline-flex;align-items:center;gap:.55rem;font-weight:800;font-size:1.05rem;letter-spacing:-.01em;color:var(--ink);text-decoration:none;margin-right:1rem}
.brand .logo{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:9px;background:var(--accent);color:#fff;font-size:.95rem;box-shadow:var(--shadow-sm)}
.nav{display:flex;align-items:center;gap:.15rem}
.nav a{color:var(--muted);text-decoration:none;font-weight:600;font-size:.92rem;padding:.42rem .8rem;border-radius:9px;transition:background .15s,color .15s}
.nav a:hover{color:var(--ink);background:var(--surface-2)}
.nav a.active{color:var(--accent);background:var(--accent-soft)}
#navscan{margin-left:auto}

/* Layout */
.wrap{max-width:1060px;margin:2rem auto;padding:0 1.3rem}
h1{font-size:1.6rem;font-weight:800;letter-spacing:-.02em;margin:.1rem 0 .35rem}
.sub{color:var(--muted);margin:.2rem 0 1.5rem;max-width:60ch}
.sub a{color:var(--accent);text-decoration:none;font-weight:600}.sub a:hover{text-decoration:underline}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.3rem;box-shadow:var(--shadow-sm);margin-bottom:1.3rem}

/* Buttons */
button,.btn{background:var(--accent);color:#fff;border:1px solid transparent;border-radius:var(--r-sm);
  padding:.55rem 1rem;font-size:.9rem;font-weight:600;cursor:pointer;font-family:inherit;text-decoration:none;
  display:inline-flex;align-items:center;gap:.4rem;transition:background .15s,box-shadow .15s,transform .05s;box-shadow:var(--shadow-sm)}
button:hover,.btn:hover{background:var(--accent-strong)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft),0 0 0 4px var(--accent)}
button.ghost,.btn.ghost{background:var(--surface);color:var(--ink);border-color:var(--line-strong);box-shadow:none}
button.ghost:hover,.btn.ghost:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
button:disabled{opacity:.65;cursor:default;transform:none}
.btn-lg{font-size:.98rem;padding:.7rem 1.3rem}
.btn-lg .spin{border-color:#fff;border-top-color:transparent;margin-right:.15rem}

/* Inputs */
input,select{padding:.6rem .75rem;border:1px solid var(--line-strong);border-radius:var(--r-sm);
  font-size:.95rem;font-family:inherit;background:var(--surface);color:var(--ink);transition:border-color .15s,box-shadow .15s}
input::placeholder{color:var(--faint)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}

/* Tables */
table{border-collapse:collapse;width:100%}
th{text-align:left;color:var(--faint);font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:.5rem .6rem;border-bottom:1px solid var(--line)}
td{padding:.75rem .6rem;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--surface-2)}

/* Pills */
.tag{display:inline-flex;align-items:center;background:var(--accent-soft);color:var(--accent-strong);
  border-radius:999px;padding:.16rem .6rem;font-size:.75rem;font-weight:600;margin:2px 3px 2px 0;text-decoration:none;line-height:1.4}
.chip{display:inline-block;padding:.4rem .85rem;border-radius:999px;border:1px solid var(--line-strong);
  background:var(--surface);color:var(--muted);text-decoration:none;font-size:.85rem;font-weight:600;margin:0 .35rem .45rem 0;transition:all .15s}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent);box-shadow:var(--shadow-sm)}

/* Detection grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1.15rem}
.det{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
  display:flex;flex-direction:column;box-shadow:var(--shadow-sm);transition:transform .18s,box-shadow .18s,border-color .18s}
.det:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:var(--accent-border)}
.det img{width:100%;height:210px;object-fit:cover;object-position:top center;background:var(--surface-2);border-bottom:1px solid var(--line);cursor:zoom-in;display:block}
.det .body{padding:1rem 1.05rem;display:flex;flex-direction:column;gap:.5rem}
.det .ttl{font-weight:700;line-height:1.35;color:var(--ink);text-decoration:none;letter-spacing:-.01em}
.det .ttl:hover{color:var(--accent)}
.det .meta{color:var(--faint);font-size:.78rem;font-weight:500}

/* Keyword list */
.klist .kwname{font-weight:700;font-size:1rem;color:var(--ink);text-decoration:none}
.klist .kwname:hover{color:var(--accent)}
.count-link{color:var(--accent);font-weight:700;text-decoration:none}
.count-link:hover{text-decoration:underline}
.muted-count{color:var(--faint)}

/* Status bars */
.scanbar{background:var(--accent);color:#fff;text-align:center;padding:.6rem 1rem;font-weight:600;font-size:.88rem;
  display:flex;align-items:center;justify-content:center;gap:.5rem}
.scanbar .spin{border-color:rgba(255,255,255,.5);border-top-color:#fff}
.donebar{background:var(--ok-soft);color:var(--ok);border-bottom:1px solid var(--ok-border);text-align:center;padding:.55rem 1rem;font-weight:600;font-size:.88rem}
.banner{background:var(--accent-soft);border:1px solid var(--accent-border);color:var(--accent-strong);border-radius:var(--r-sm);padding:.8rem 1rem;margin-bottom:1.2rem;font-weight:600}
.hint{background:var(--surface-2);border:1px solid var(--line-strong);color:var(--muted);border-radius:var(--r-sm);padding:.75rem 1rem;font-size:.9rem;margin-bottom:1rem}

/* Section heading */
.sechead{display:flex;align-items:center;gap:.55rem;font-size:1.05rem;font-weight:800;letter-spacing:-.01em;margin:1.8rem 0 .9rem}
.sechead::before{content:"";width:4px;height:1.05rem;border-radius:99px;background:var(--accent)}
.sechead span{color:var(--faint);font-size:.85rem;font-weight:600}

/* Misc */
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.empty{color:var(--muted);text-align:center;padding:3rem 1.5rem;border:1px dashed var(--line-strong);border-radius:var(--r);background:var(--surface)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:.4rem}
@keyframes s{to{transform:rotate(360deg)}}
@media (max-width:560px){.bar{flex-wrap:wrap;gap:.3rem}.nav a{padding:.4rem .6rem}#navscan{margin-left:0}.wrap{margin:1.3rem auto}}
"""


def _shell(title: str, active: str, body: str) -> str:
    # A scan runs in a subprocess, so its state is global. A tiny JS poller keeps
    # the status bar live on EVERY tab and reloads the page ONCE when a scan
    # finishes — smooth, no jarring full-page auto-refresh.
    news = scan_manager.status()
    yt = scan_runner.status()
    scanning = bool(news["running"] or yt["running"])

    scan_btn = (
        '<button disabled><span class="spin"></span>Scanning…</button>'
        if scanning
        else '<form method="post" action="/ui/scan" style="margin:0">'
        "<button>▶ Scan all</button></form>"
    )

    def nav(href, label, key):
        cls = "active" if key == active else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_CSS}</style></head><body>
<header><div class="bar">
  <a class="brand" href="/"><span class="logo">📡</span>Media Monitor</a>
  <nav class="nav">
    {nav('/', 'Keywords', 'keywords')}
    {nav('/channels', 'Channels', 'channels')}
    {nav('/mentions', 'Detections', 'mentions')}
    <a href="/docs">API</a>
  </nav>
  <span id="navscan">{scan_btn}</span>
</div></header>
<div id="statusbar">{_status_bar(news, yt)}</div>
<div class="wrap">{body}</div>
<script>{_STATUS_JS.replace('__SCANNING__', 'true' if scanning else 'false')}</script>
</body></html>"""


def _status_bar(news: dict, yt: dict) -> str:
    """Full-width bar under the header: live scan state, shown on all tabs."""
    if news["running"]:
        who = f"“{html.escape(news['keyword'])}”" if news["keyword"] else "all keywords"
        return (
            f'<div class="scanbar"><span class="spin"></span>'
            f"Scanning newspapers for {who}… you can switch tabs — results load automatically when done.</div>"
        )
    if yt["running"]:
        who = f"“{html.escape(yt['label'])}”" if yt.get("label") else "your channels"
        return f'<div class="scanbar"><span class="spin"></span>Scanning YouTube {who}…</div>'
    s = news.get("last_summary")
    if s:
        who = f"“{html.escape(news['last_keyword'])}”" if news.get("last_keyword") else "all keywords"
        return (
            f'<div class="donebar">✓ Last scan of {who}: {s.get("mentions",0)} new detection(s), '
            f'{s.get("cached",0)} article(s) fetched.</div>'
        )
    return ""


# Polls scan status every 3s; updates the bar live and reloads once when a scan
# finishes so fresh results appear without a jarring constant refresh.
_STATUS_JS = """
var wasScanning = __SCANNING__;
async function pollScan(){
  try{
    var n = await fetch('/api/scan/status').then(r=>r.json());
    var y = await fetch('/api/scan/youtube/status').then(r=>r.json());
    var running = n.running || y.running;
    var bar = document.getElementById('statusbar');
    var nav = document.getElementById('navscan');
    if(running){
      var who = n.running ? (n.keyword ? '“'+n.keyword+'”' : 'all keywords')
                          : (y.label ? '“'+y.label+'”' : 'your channels');
      var what = n.running ? 'newspapers for ' : 'YouTube ';
      bar.innerHTML = '<div class="scanbar"><span class="spin"></span>Scanning '+what+who+
        '… you can switch tabs — results load automatically when done.</div>';
      nav.innerHTML = '<button disabled><span class="spin"></span>Scanning…</button>';
    } else if(wasScanning){
      location.reload();      // scan just finished -> show results
    }
    wasScanning = running;
  }catch(e){}
}
setInterval(pollScan, 3000);
"""


def _detection_card(m: Mention) -> str:
    """One detection card (thumbnail + title + meta + keyword tags)."""
    thumb = _media_url(m.screenshot_path) or _media_url(m.full_screenshot_path)
    img = (
        f'<a href="{thumb}" target="_blank" title="Open full screenshot">'
        f'<img loading="lazy" src="{thumb}"></a>'
        if thumb
        else ""
    )
    tags = "".join(f'<span class="tag">{html.escape(k)}</span>' for k in (m.matched_keywords or []))
    when = m.detected_at.astimezone().strftime("%d %b %Y, %H:%M") if m.detected_at else ""
    icon = "▶ YouTube" if m.module == "youtube" else "📰"
    meta = " · ".join(x for x in [icon, m.source, m.sentiment] if x)
    return (
        f'<div class="det">{img}<div class="body">'
        f'<a class="ttl" href="{html.escape(m.url)}" target="_blank">{html.escape(m.title)}</a>'
        f'<div class="meta">{meta} · {when}</div><div>{tags}</div></div></div>'
    )


# --------------------------------------------------------------------------
# Keywords page
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def keywords_page(edit: int | None = None, db: Session = Depends(get_db)):
    keywords = db.execute(select(Keyword).order_by(Keyword.created_at.desc())).scalars().all()
    # Count detections per keyword (cheap: one pass over recent mentions).
    mentions = db.execute(select(Mention.matched_keywords).limit(1000)).scalars().all()
    counts: dict[str, int] = {}
    for mk in mentions:
        for k in (mk or []):
            counts[k] = counts.get(k, 0) + 1

    scanning = scan_manager.is_running()

    if keywords:
        rows = ""
        for k in keywords:
            if edit == k.id:
                # Inline edit form spanning the row.
                en = "selected" if k.language == "en" else ""
                ur = "selected" if k.language == "ur" else ""
                rows += (
                    f'<tr><td colspan="5">'
                    f'<form method="post" action="/ui/keywords/{k.id}/edit" class="row">'
                    f'<input name="text" value="{html.escape(k.text)}" required style="flex:1;min-width:200px">'
                    f'<select name="language"><option value="en" {en}>English</option>'
                    f'<option value="ur" {ur}>Urdu</option></select>'
                    f"<button type=\"submit\">Save</button>"
                    f'<a href="/" style="align-self:center;color:var(--muted);font-weight:600;text-decoration:none">Cancel</a>'
                    f"</form></td></tr>"
                )
                continue

            n = counts.get(k.text, 0)
            results = (
                f'<a class="count-link" href="/mentions?keyword={k.text}">{n} result(s) →</a>'
                if n else '<span class="muted-count">0 results</span>'
            )
            status = (
                f'<button class="ghost" title="Click to pause — paused keywords are skipped by scans">🟢 Active</button>'
                if k.active
                else '<button title="Click to activate">⏸ Paused</button>'
            )
            dim = "" if k.active else ' style="opacity:.55"'
            scan_disabled = "disabled" if (scanning or not k.active) else ""
            rows += (
                f"<tr{dim}>"
                f'<td><a class="kwname" href="/mentions?keyword={k.text}">{html.escape(k.text)}</a></td>'
                f'<td><span class="tag">{k.language.upper()}</span></td>'
                f'<td><form method="post" action="/ui/keywords/{k.id}/toggle" style="margin:0">{status}</form></td>'
                f"<td>{results}</td>"
                f'<td class="row" style="justify-content:flex-end">'
                f'<a class="btn ghost" href="/?edit={k.id}">Edit</a>'
                f'<form method="post" action="/ui/keywords/{k.id}/scan" style="margin:0">'
                f'<button {scan_disabled} title="Scan all sources for this keyword">▶ Scan</button></form>'
                f'<form method="post" action="/ui/keywords/{k.id}/delete" style="margin:0">'
                f'<button class="ghost">Delete</button></form></td></tr>'
            )
        listing = (
            '<table class="klist"><tr><th>Keyword</th><th>Lang</th><th>Status</th>'
            "<th>Detections</th><th></th></tr>" + rows + "</table>"
        )
    else:
        listing = '<div class="empty">No keywords yet — add your first one above.</div>'

    scan_all = (
        '<button class="btn-lg" disabled><span class="spin"></span>Scanning…</button>'
        if scanning
        else '<button class="btn-lg" type="submit">▶ Scan all keywords</button>'
    )

    body = f"""
    <h1>Keywords</h1>
    <p class="sub">Add the words you want to watch for, then scan. Detected articles
    &amp; videos appear on the <a href="/mentions">Detections</a> tab — click any
    keyword's result count to jump straight to its matches.</p>

    <div class="card">
      <form method="post" action="/ui/keywords" class="row" style="margin-bottom:.9rem">
        <input name="text" placeholder="Add a keyword to watch, e.g. Imran Khan" required
               style="flex:1;min-width:200px">
        <select name="language"><option value="en">English</option><option value="ur">Urdu</option></select>
        <button type="submit">+ Add keyword</button>
      </form>
      <form method="post" action="/ui/scan" style="margin:0">{scan_all}</form>
    </div>

    <div class="card">{listing}</div>
    """
    return _shell("Media Monitor — Keywords", "keywords", body)


@app.post("/ui/keywords")
def ui_add_keyword(text: str = Form(...), language: str = Form("en"), db: Session = Depends(get_db)):
    text = text.strip()
    if text:
        exists = db.execute(
            select(Keyword).where(Keyword.text == text, Keyword.language == language)
        ).first()
        if not exists:
            db.add(Keyword(text=text, language=language, active=True))
            db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/keywords/{kid}/edit")
def ui_edit_keyword(kid: int, text: str = Form(...), language: str = Form("en"),
                    db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw and text.strip():
        kw.text = text.strip()
        kw.language = language if language in ("en", "ur") else kw.language
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/keywords/{kid}/toggle")
def ui_toggle_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw:
        kw.active = not kw.active
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/keywords/{kid}/delete")
def ui_delete_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw:
        db.delete(kw)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/keywords/{kid}/scan")
def ui_scan_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if not kw:
        raise HTTPException(404, "keyword not found")
    scan_manager.start_scan(keyword_ids=[kid], keyword_label=kw.text, capped=True)
    return RedirectResponse(f"/mentions?keyword={kw.text}", status_code=303)


@app.post("/ui/scan")
def ui_scan_all():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# YouTube channels page
# --------------------------------------------------------------------------
@app.get("/channels", response_class=HTMLResponse)
def channels_page(error: str | None = None, db: Session = Depends(get_db)):
    channels = db.execute(select(YouTubeChannel).order_by(YouTubeChannel.created_at.desc())).scalars().all()
    if channels:
        rows = "".join(
            f"<tr><td><b>{html.escape(c.name or c.channel_id)}</b><br>"
            f'<span style="color:#888;font-size:.8rem">{c.channel_id}</span></td>'
            f"<td>{'🟢 active' if c.active else '⚪ off'}</td>"
            f'<td class="row">'
            f'<form method="post" action="/ui/channels/{c.id}/scan" style="margin:0">'
            f'<button title="Scan this channel now">▶ Run scan</button></form>'
            f'<form method="post" action="/ui/channels/{c.id}/delete" style="margin:0">'
            f'<button class="ghost">Delete</button></form></td></tr>'
            for c in channels
        )
        table = f"<table><tr><th>Channel</th><th>Status</th><th>Actions</th></tr>{rows}</table>"
    else:
        table = '<div class="empty">No channels yet. Add one above to start monitoring YouTube.</div>'

    # The global status bar (in _shell) shows "scanning YouTube"; only errors here.
    banner = f'<div class="banner">⚠ {html.escape(error)}</div>' if error else ""

    trans = settings.youtube_transcriber
    note = (
        f'Transcription mode: <b>{trans}</b>. '
        + ("In <b>stub</b> mode, matching runs on video title + description only "
           "(no GPU/API key needed). Set YOUTUBE_TRANSCRIBER=openai or local to transcribe audio."
           if trans == "stub" else "Audio is transcribed for in-video keyword detection.")
    )

    body = f"""
    {banner}
    <h1>YouTube Channels</h1>
    <p class="sub">Add a channel by URL, @handle, or channel ID (UC…). {note}</p>
    <div class="card">
      <form method="post" action="/ui/channels" class="row">
        <input name="channel" placeholder="https://youtube.com/@GeoNews  or  UC…" required style="flex:1;min-width:220px">
        <button type="submit">+ Add channel</button>
      </form>
    </div>
    <div class="card">{table}</div>
    """
    return _shell("Media Monitor — Channels", "channels", body)


@app.post("/ui/channels")
def ui_add_channel(channel: str = Form(...), db: Session = Depends(get_db)):
    channel_id, name = rss.resolve_channel_id(channel)
    if not channel_id:
        return RedirectResponse(
            "/channels?error=Could not resolve that channel. Paste the full channel URL or its UC… id.",
            status_code=303,
        )
    if not db.execute(select(YouTubeChannel).where(YouTubeChannel.channel_id == channel_id)).first():
        db.add(YouTubeChannel(channel_id=channel_id, name=name, url=channel, active=True))
        db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/ui/channels/{cid}/delete")
def ui_delete_channel(cid: int, db: Session = Depends(get_db)):
    ch = db.get(YouTubeChannel, cid)
    if ch:
        db.delete(ch)
        db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/ui/channels/{cid}/scan")
def ui_scan_channel(cid: int, db: Session = Depends(get_db)):
    ch = db.get(YouTubeChannel, cid)
    if not ch:
        raise HTTPException(404, "channel not found")
    scan_runner.start_scan(channel_ids=[cid], label=ch.name or ch.channel_id)
    return RedirectResponse("/mentions", status_code=303)


@app.post("/ui/youtube/scan")
def ui_scan_youtube_all():
    scan_runner.start_scan()
    return RedirectResponse("/mentions", status_code=303)


# --------------------------------------------------------------------------
# Detections page (filterable by keyword)
# --------------------------------------------------------------------------
@app.get("/mentions", response_class=HTMLResponse)
def detections_page(keyword: str | None = None, src: str | None = None, db: Session = Depends(get_db)):
    mentions = (
        db.execute(select(Mention).order_by(Mention.detected_at.desc()).limit(500))
        .scalars()
        .all()
    )
    if keyword:
        mentions = [m for m in mentions if keyword in (m.matched_keywords or [])]
    papers = [m for m in mentions if m.module != "youtube"]
    videos = [m for m in mentions if m.module == "youtube"]

    active_keywords = db.execute(
        select(Keyword).where(Keyword.active.is_(True)).order_by(Keyword.text)
    ).scalars().all()

    def href(kw, s):
        parts = []
        if kw:
            parts.append(f"keyword={kw}")
        if s:
            parts.append(f"src={s}")
        return "/mentions" + ("?" + "&".join(parts) if parts else "")

    def chip(label, active, link):
        return f'<a class="chip {"on" if active else ""}" href="{link}">{label}</a>'

    # Source filter (newspaper vs youtube)
    src_chips = (
        chip("All sources", src is None, href(keyword, None))
        + chip("📰 Newspapers", src == "newspaper", href(keyword, "newspaper"))
        + chip("▶ YouTube", src == "youtube", href(keyword, "youtube"))
    )
    # Keyword filter (preserves the source filter)
    kw_chips = chip("All keywords", keyword is None, href(None, src)) + "".join(
        chip(html.escape(k.text), keyword == k.text, href(k.text, src)) for k in active_keywords
    )

    def section(title, items):
        grid = (
            f'<div class="grid">{"".join(_detection_card(m) for m in items)}</div>'
            if items
            else '<div class="empty">Nothing here yet.</div>'
        )
        return f'<div class="sechead">{title} <span>({len(items)})</span></div>{grid}'

    if src == "newspaper":
        content = section("📰 Newspapers", papers)
    elif src == "youtube":
        content = section("▶ YouTube", videos)
    elif not papers and not videos:
        content = '<div class="empty">No detections yet.<br>Run a scan from the Keywords page.</div>'
    else:
        content = section("📰 Newspapers", papers) + section("▶ YouTube", videos)

    clear_btn = (
        '<form method="post" action="/ui/detections/clear" style="margin:0" '
        "onsubmit=\"return confirm('Delete ALL detections? This cannot be undone.')\">"
        '<button class="ghost">🗑 Clear all detections</button></form>'
    )

    body = f"""
    <div class="row" style="justify-content:space-between;align-items:center">
      <h1 style="margin:0">Detections</h1>
      {clear_btn}
    </div>
    <p class="sub">{len(mentions)} detection(s){' for “'+html.escape(keyword)+'”' if keyword else ''} —
    newspapers and YouTube shown separately below.</p>
    <div style="margin-bottom:.4rem">{src_chips}</div>
    <div style="margin-bottom:1rem">{kw_chips}</div>
    {content}
    """
    return _shell("Media Monitor — Detections", "mentions", body)


@app.post("/ui/detections/clear")
def ui_clear_detections(db: Session = Depends(get_db)):
    """Delete all detections (Mention rows). Cached article text is kept so a
    re-scan can re-detect; screenshots on disk are pruned by the retention job."""
    db.execute(delete(Mention))
    db.commit()
    return RedirectResponse("/mentions", status_code=303)


def _media_url(abs_path: str | None) -> str | None:
    if not abs_path:
        return None
    try:
        rel = Path(abs_path).resolve().relative_to(settings.storage_dir.resolve())
        return "/media/" + str(rel).replace("\\", "/")
    except Exception:
        return None


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@app.get("/api/keywords")
def list_keywords(db: Session = Depends(get_db)):
    rows = db.execute(select(Keyword).order_by(Keyword.created_at.desc())).scalars().all()
    return [{"id": k.id, "text": k.text, "language": k.language, "active": k.active} for k in rows]


@app.get("/api/mentions")
def list_mentions(keyword: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    rows = db.execute(select(Mention).order_by(Mention.detected_at.desc()).limit(limit)).scalars().all()
    if keyword:
        rows = [m for m in rows if keyword in (m.matched_keywords or [])]
    return [
        {
            "id": m.id, "source": m.source, "title": m.title, "url": m.url,
            "matched_keywords": m.matched_keywords, "sentiment": m.sentiment,
            "detected_at": m.detected_at.isoformat() if m.detected_at else None,
        }
        for m in rows
    ]


@app.get("/api/channels")
def list_channels(db: Session = Depends(get_db)):
    rows = db.execute(select(YouTubeChannel).order_by(YouTubeChannel.created_at.desc())).scalars().all()
    return [
        {"id": c.id, "channel_id": c.channel_id, "name": c.name, "active": c.active}
        for c in rows
    ]


@app.get("/api/scan/status")
def scan_status():
    return scan_manager.status()


@app.get("/api/scan/youtube/status")
def youtube_scan_status():
    return scan_runner.status()


@app.post("/api/scan/youtube")
def trigger_youtube_scan():
    started = scan_runner.start_scan()
    return {"started": started}


@app.post("/api/scan/newspaper")
def trigger_scan(keyword_ids: list[int] | None = None):
    """Synchronous scan (blocks until done) — for scripts/testing."""
    return run_newspaper_scan(keyword_ids=keyword_ids, uncapped=True)
