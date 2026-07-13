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
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from config import settings
from app.db.base import SessionLocal, init_db
from app.db.models import Keyword, Mention, YouTubeChannel
from app.newspaper import scan_manager
from app.newspaper.pipeline import run_newspaper_scan
from app.youtube import scan_runner
from app.youtube import rss
from app.scrapers.sites import SITE_CONFIGS
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
  --bg:#f4f5f7;--surface:#ffffff;--surface-2:#f7f8fa;
  --ink:#101319;--muted:#5a6472;--faint:#9099a6;
  --line:#e8eaee;--line-strong:#dcdfe5;
  --accent:#e11d2a;--accent-strong:#c0111d;--accent-soft:#fdecec;--accent-border:#f6cfcf;
  --ok:#16a34a;--ok-soft:#eefbf2;--ok-border:#c6ecd3;
  --warn:#d97706;--info:#2563eb;
  --side-bg:#12141b;--side-2:#1a1d26;--side-ink:#e9ebf0;--side-muted:#8b93a3;--side-line:#242732;
  --shadow-sm:0 1px 2px rgba(16,19,25,.06);
  --shadow:0 10px 30px -10px rgba(16,19,25,.14);
  --shadow-lg:0 18px 44px -14px rgba(16,19,25,.24);
  --r:14px;--r-sm:10px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0c10;--surface:#14161c;--surface-2:#1b1e26;
  --ink:#eef0f4;--muted:#9aa2b0;--faint:#6b7280;
  --line:#242832;--line-strong:#323744;
  --accent:#f2555c;--accent-strong:#f87179;--accent-soft:#251518;--accent-border:#3e2024;
  --ok:#4ade80;--ok-soft:#122019;--ok-border:#1f3d2b;
  --side-bg:#0e1015;--side-2:#171a22;--side-ink:#e6e8ee;--side-muted:#818a9b;--side-line:#1f232d;
  --shadow-sm:0 1px 2px rgba(0,0,0,.5);
  --shadow:0 10px 30px -10px rgba(0,0,0,.6);
  --shadow-lg:0 18px 44px -14px rgba(0,0,0,.7);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14.5px;line-height:1.55;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--accent-soft);color:var(--accent-strong)}
a{color:inherit}

/* App shell: sidebar + main */
.app{display:flex;min-height:100vh}
.side{width:250px;flex:0 0 250px;background:var(--side-bg);color:var(--side-ink);position:sticky;top:0;height:100vh;
  display:flex;flex-direction:column;padding:1rem .75rem;border-right:1px solid var(--side-line)}
.side .brand{display:flex;align-items:center;gap:.6rem;padding:.4rem .6rem 1rem;text-decoration:none;color:var(--side-ink)}
.side .logo{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:10px;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong));color:#fff;font-size:1rem;box-shadow:0 4px 12px -2px rgba(225,29,42,.5)}
.side .brand b{font-size:1.02rem;font-weight:800;letter-spacing:-.01em;display:block;line-height:1.15}
.side .brand small{color:var(--side-muted);font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.navlist{display:flex;flex-direction:column;gap:2px;margin-top:.3rem}
.navlist a{display:flex;align-items:center;gap:.65rem;padding:.6rem .7rem;border-radius:9px;color:var(--side-muted);
  text-decoration:none;font-weight:600;font-size:.92rem;transition:background .15s,color .15s}
.navlist a .ic{width:18px;text-align:center;font-size:.95rem;opacity:.9}
.navlist a:hover{color:var(--side-ink);background:var(--side-2)}
.navlist a.active{color:#fff;background:var(--side-2);box-shadow:inset 3px 0 0 var(--accent)}
.side .foot{margin-top:auto;padding:.7rem;border-top:1px solid var(--side-line);color:var(--side-muted);font-size:.78rem}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.45rem;vertical-align:middle}
.dot.live{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.18)}
.dot.busy{background:var(--accent);box-shadow:0 0 0 3px rgba(225,29,42,.2)}

.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:1rem;padding:.85rem 1.6rem;
  background:color-mix(in srgb,var(--surface) 88%,transparent);backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
.topbar h1{font-size:1.15rem;font-weight:800;letter-spacing:-.01em;margin:0}
.topbar .spacer{margin-left:auto}
.content{padding:1.6rem;max-width:1200px;width:100%;margin:0 auto}
.sub{color:var(--muted);margin:.1rem 0 1.4rem;max-width:70ch}
.sub a{color:var(--accent);text-decoration:none;font-weight:600}.sub a:hover{text-decoration:underline}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.25rem;box-shadow:var(--shadow-sm);margin-bottom:1.25rem}

/* Buttons */
button,.btn{background:var(--accent);color:#fff;border:1px solid transparent;border-radius:var(--r-sm);
  padding:.5rem .95rem;font-size:.88rem;font-weight:600;cursor:pointer;font-family:inherit;text-decoration:none;white-space:nowrap;
  display:inline-flex;align-items:center;gap:.4rem;transition:background .15s,box-shadow .15s,transform .05s;box-shadow:var(--shadow-sm)}
button:hover,.btn:hover{background:var(--accent-strong)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft),0 0 0 4px var(--accent)}
button.ghost,.btn.ghost{background:var(--surface);color:var(--ink);border-color:var(--line-strong);box-shadow:none}
button.ghost:hover,.btn.ghost:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
button:disabled{opacity:.6;cursor:default;transform:none}
.btn-lg{font-size:.95rem;padding:.65rem 1.2rem}
.btn-lg .spin{border-color:#fff;border-top-color:transparent}

/* Inputs */
input,select{padding:.58rem .75rem;border:1px solid var(--line-strong);border-radius:var(--r-sm);
  font-size:.92rem;font-family:inherit;background:var(--surface);color:var(--ink);transition:border-color .15s,box-shadow .15s}
input::placeholder{color:var(--faint)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}

/* Tables */
table{border-collapse:collapse;width:100%}
th{text-align:left;color:var(--faint);font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:.5rem .6rem;border-bottom:1px solid var(--line)}
td{padding:.7rem .6rem;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--surface-2)}

/* Pills */
.tag{display:inline-flex;align-items:center;background:var(--accent-soft);color:var(--accent-strong);
  border-radius:999px;padding:.15rem .58rem;font-size:.74rem;font-weight:600;margin:2px 3px 2px 0;text-decoration:none;line-height:1.4}
.chip{display:inline-block;padding:.38rem .8rem;border-radius:999px;border:1px solid var(--line-strong);
  background:var(--surface);color:var(--muted);text-decoration:none;font-size:.83rem;font-weight:600;margin:0 .35rem .45rem 0;transition:all .15s}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent);box-shadow:var(--shadow-sm)}

/* Dashboard: stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1rem;margin-bottom:1.25rem}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.05rem 1.15rem;box-shadow:var(--shadow-sm);position:relative;overflow:hidden}
.tile .label{color:var(--faint);font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.tile .val{font-size:1.95rem;font-weight:800;letter-spacing:-.02em;margin:.25rem 0 .1rem;line-height:1}
.tile .foot{color:var(--muted);font-size:.8rem;font-weight:500}
.tile .ic{position:absolute;top:1rem;right:1rem;width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:var(--accent-soft);color:var(--accent);font-size:1rem}

/* Dashboard: panels + charts */
.cols{display:grid;grid-template-columns:1.6fr 1fr;gap:1.25rem;align-items:start}
@media (max-width:900px){.cols{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.2rem;box-shadow:var(--shadow-sm);margin-bottom:1.25rem}
.panel h3{margin:0 0 .1rem;font-size:.98rem;font-weight:800;letter-spacing:-.01em}
.panel .cap{color:var(--faint);font-size:.8rem;margin-bottom:1rem}
.bars{display:flex;align-items:flex-end;gap:.55rem;height:130px;padding-top:.5rem}
.bars .b{flex:1;display:flex;flex-direction:column;align-items:center;gap:.4rem;height:100%;justify-content:flex-end;color:var(--faint);font-size:.7rem}
.bars .b i{width:100%;max-width:34px;background:linear-gradient(180deg,var(--accent),var(--accent-strong));border-radius:6px 6px 3px 3px;min-height:3px;font-style:normal;transition:height .3s}
.bars .b .n{color:var(--muted);font-weight:700;font-size:.72rem}
.hbar{display:flex;align-items:center;gap:.7rem;margin:.55rem 0}
.hbar .hl{width:96px;flex:0 0 96px;font-size:.82rem;color:var(--muted);font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hbar .track{flex:1;height:9px;border-radius:99px;background:var(--surface-2);overflow:hidden}
.hbar .track i{display:block;height:100%;border-radius:99px;background:var(--accent)}
.hbar .hn{width:34px;flex:0 0 34px;text-align:right;font-weight:700;font-size:.8rem}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin-top:.8rem;font-size:.82rem;color:var(--muted)}
.legend b{color:var(--ink)}
.sdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:.4rem;vertical-align:middle}

/* Recent activity list */
.rlist{display:flex;flex-direction:column}
.ritem{display:flex;align-items:center;gap:.8rem;padding:.7rem .2rem;border-bottom:1px solid var(--line);text-decoration:none;color:inherit}
.ritem:last-child{border-bottom:none}
.ritem:hover .rt{color:var(--accent)}
.rsrc{flex:0 0 auto;font-size:.7rem;font-weight:700;color:var(--muted);background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:.2rem .45rem;white-space:nowrap}
.rt{flex:1;min-width:0;font-weight:600;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rtime{flex:0 0 auto;color:var(--faint);font-size:.78rem}

/* Detection grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1.1rem}
.det{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
  display:flex;flex-direction:column;box-shadow:var(--shadow-sm);transition:transform .18s,box-shadow .18s,border-color .18s}
.det:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:var(--accent-border)}
.det img{width:100%;height:200px;object-fit:cover;object-position:top center;background:var(--surface-2);border-bottom:1px solid var(--line);cursor:zoom-in;display:block}
.det .body{padding:.95rem 1.05rem;display:flex;flex-direction:column;gap:.5rem}
.det .ttl{font-weight:700;line-height:1.35;color:var(--ink);text-decoration:none;letter-spacing:-.01em}
.det .ttl:hover{color:var(--accent)}
.det .meta{color:var(--faint);font-size:.78rem;font-weight:500}

/* Keyword list */
.klist .kwname{font-weight:700;font-size:.98rem;color:var(--ink);text-decoration:none}
.klist .kwname:hover{color:var(--accent)}
.count-link{color:var(--accent);font-weight:700;text-decoration:none}
.count-link:hover{text-decoration:underline}
.muted-count{color:var(--faint)}

/* Status bars */
.scanbar{background:var(--accent);color:#fff;text-align:center;padding:.55rem 1rem;font-weight:600;font-size:.86rem;
  display:flex;align-items:center;justify-content:center;gap:.5rem}
.scanbar .spin{border-color:rgba(255,255,255,.5);border-top-color:#fff}
.donebar{background:var(--ok-soft);color:var(--ok);border-bottom:1px solid var(--ok-border);text-align:center;padding:.5rem 1rem;font-weight:600;font-size:.86rem}
.banner{background:var(--accent-soft);border:1px solid var(--accent-border);color:var(--accent-strong);border-radius:var(--r-sm);padding:.8rem 1rem;margin-bottom:1.2rem;font-weight:600}
.hint{background:var(--surface-2);border:1px solid var(--line-strong);color:var(--muted);border-radius:var(--r-sm);padding:.75rem 1rem;font-size:.88rem;margin-bottom:1rem}

/* Section heading */
.sechead{display:flex;align-items:center;gap:.55rem;font-size:1.02rem;font-weight:800;letter-spacing:-.01em;margin:1.6rem 0 .9rem}
.sechead::before{content:"";width:4px;height:1rem;border-radius:99px;background:var(--accent)}
.sechead span{color:var(--faint);font-size:.84rem;font-weight:600}

/* Misc */
.row{display:flex;gap:.55rem;flex-wrap:wrap;align-items:center}
.empty{color:var(--muted);text-align:center;padding:2.6rem 1.5rem;border:1px dashed var(--line-strong);border-radius:var(--r);background:var(--surface)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:.4rem}
@keyframes s{to{transform:rotate(360deg)}}
@media (max-width:820px){
  .app{flex-direction:column}
  .side{width:100%;height:auto;position:static;flex-direction:column;padding:.6rem .8rem}
  .side .foot{display:none}
  .navlist{flex-direction:row;flex-wrap:wrap;gap:.25rem}
  .navlist a{padding:.45rem .7rem}
  .cols{grid-template-columns:1fr}
}
"""


_NAV = [
    ("/", "Overview", "overview", "◧"),
    ("/newspapers", "Newspapers", "newspapers", "📰"),
    ("/youtube", "YouTube", "youtube", "▶"),
    ("/mentions", "Detections", "mentions", "◎"),
    ("/docs", "API", "api", "⚙"),
]
_TITLES = {"overview": "Overview", "newspapers": "Newspapers",
           "youtube": "YouTube", "mentions": "Detections"}


def _shell(title: str, active: str, body: str) -> str:
    # A scan runs in a subprocess, so its state is global. A tiny JS poller keeps
    # the status live on EVERY tab and reloads the page ONCE when a scan finishes.
    news = scan_manager.status()
    yt = scan_runner.status()
    scanning = bool(news["running"] or yt["running"])

    scan_btn = (
        '<button disabled><span class="spin"></span>Scanning…</button>'
        if scanning
        else '<form method="post" action="/ui/scan" style="margin:0">'
        "<button>▶ Scan all</button></form>"
    )
    nav_html = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">'
        f'<span class="ic">{ic}</span>{label}</a>'
        for href, label, key, ic in _NAV
    )
    foot = (
        '<span class="dot busy"></span>Scanning…' if scanning
        else '<span class="dot live"></span>Monitoring · idle'
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_CSS}</style></head><body>
<div class="app">
  <aside class="side">
    <a class="brand" href="/"><span class="logo">📡</span>
      <span><b>Media Monitor</b><small>Live intelligence</small></span></a>
    <nav class="navlist">{nav_html}</nav>
    <div class="foot" id="sidefoot">{foot}</div>
  </aside>
  <div class="main">
    <div class="topbar">
      <h1>{_TITLES.get(active, "Media Monitor")}</h1>
      <span class="spacer"></span>
      <span id="navscan">{scan_btn}</span>
    </div>
    <div id="statusbar">{_status_bar(news, yt)}</div>
    <div class="content">{body}</div>
  </div>
</div>
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
      var f = document.getElementById('sidefoot');
      if(f) f.innerHTML = '<span class="dot busy"></span>Scanning…';
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
# Overview dashboard
# --------------------------------------------------------------------------
_PKT = timezone(timedelta(hours=5))
_SENT_COLORS = {"Positive": "var(--ok)", "Critical": "var(--accent)",
                "Neutral": "#8a93a3", "Unscored": "var(--line-strong)"}


def _utc(dt):
    """SQLite returns naive datetimes — treat them as UTC."""
    return dt.replace(tzinfo=timezone.utc) if (dt and dt.tzinfo is None) else dt


def _rel(dt) -> str:
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - _utc(dt)).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _bars(days: list[tuple[str, int]]) -> str:
    mx = max((v for _, v in days), default=0) or 1
    cells = "".join(
        f'<div class="b"><span class="n">{v}</span>'
        f'<i style="height:{int(4 + (v / mx) * 104)}px"></i>{lbl}</div>'
        for lbl, v in days
    )
    return f'<div class="bars">{cells}</div>'


def _hbars(pairs: list[tuple[str, int]]) -> str:
    if not pairs:
        return '<div class="empty">No detections yet.</div>'
    mx = max(v for _, v in pairs) or 1
    return "".join(
        f'<div class="hbar"><div class="hl" title="{html.escape(l)}">{html.escape(l)}</div>'
        f'<div class="track"><i style="width:{int(v / mx * 100)}%"></i></div>'
        f'<div class="hn">{v}</div></div>'
        for l, v in pairs
    )


def _sentiment_block(counts: dict) -> str:
    total = sum(counts.values())
    if not total:
        return '<div class="empty">No detections yet. Enable scoring to see sentiment.</div>'
    seg, leg = "", ""
    for name in ("Positive", "Critical", "Neutral", "Unscored"):
        v = counts.get(name, 0)
        if v:
            seg += f'<i style="width:{v / total * 100:.1f}%;background:{_SENT_COLORS[name]}"></i>'
            leg += (f'<span><span class="sdot" style="background:{_SENT_COLORS[name]}"></span>'
                    f"{name} <b>{v}</b></span>")
    return (f'<div class="track" style="height:14px;display:flex;border-radius:99px;overflow:hidden">'
            f'{seg}</div><div class="legend">{leg}</div>')


def _health_row(label: str, ok: bool, text: str) -> str:
    color = "var(--ok)" if ok else "var(--faint)"
    return (f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:.5rem 0;border-bottom:1px solid var(--line);font-size:.88rem">'
            f'<span style="color:var(--muted)">{label}</span>'
            f'<span style="font-weight:600"><span class="sdot" style="background:{color}"></span>'
            f"{text}</span></div>")


@app.get("/", response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(Mention)) or 0
    today_start = datetime.now(_PKT).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).replace(tzinfo=None)
    today = db.scalar(
        select(func.count()).select_from(Mention).where(Mention.detected_at >= today_start_utc)
    ) or 0
    active_kw = db.scalar(
        select(func.count()).select_from(Keyword).where(Keyword.active.is_(True))
    ) or 0
    np_kw = db.scalar(select(func.count()).select_from(Keyword).where(
        Keyword.active.is_(True), Keyword.module == "newspaper")) or 0
    yt_kw = db.scalar(select(func.count()).select_from(Keyword).where(
        Keyword.active.is_(True), Keyword.module == "youtube")) or 0
    n_channels = db.scalar(select(func.count()).select_from(YouTubeChannel)) or 0
    n_papers = len(SITE_CONFIGS) + 1  # Dawn + configurable sites

    # Aggregate over recent mentions for the charts.
    agg = db.execute(
        select(Mention.detected_at, Mention.source, Mention.sentiment, Mention.module)
        .order_by(Mention.detected_at.desc()).limit(3000)
    ).all()

    # 7-day trend (PKT days)
    day_counts: dict[str, int] = {}
    for i in range(6, -1, -1):
        d = (today_start - timedelta(days=i))
        day_counts[d.strftime("%a")] = 0
    order = list(day_counts.keys())
    week_ago = today_start - timedelta(days=6)
    for dt, *_ in agg:
        if dt and _utc(dt).astimezone(_PKT) >= week_ago:
            lbl = _utc(dt).astimezone(_PKT).strftime("%a")
            if lbl in day_counts:
                day_counts[lbl] += 1
    days = [(l, day_counts[l]) for l in order]

    # Top sources + sentiment
    src_counts: dict[str, int] = {}
    sent_counts = {"Positive": 0, "Critical": 0, "Neutral": 0, "Unscored": 0}
    for _dt, source, sentiment, _mod in agg:
        src_counts[source] = src_counts.get(source, 0) + 1
        key = sentiment if sentiment in sent_counts else "Unscored"
        sent_counts[key] += 1
    top_sources = sorted(src_counts.items(), key=lambda x: -x[1])[:6]

    recent = db.execute(
        select(Mention).order_by(Mention.detected_at.desc()).limit(8)
    ).scalars().all()
    recent_html = "".join(
        f'<a class="ritem" href="{html.escape(m.url)}" target="_blank">'
        f'<span class="rsrc">{"▶" if m.module == "youtube" else "📰"} '
        f'{html.escape((m.source or "")[:16])}</span>'
        f'<span class="rt">{html.escape(m.title)}</span>'
        f'<span class="rtime">{_rel(m.detected_at)}</span></a>'
        for m in recent
    ) or '<div class="empty">No detections yet — run a scan to get started.</div>'

    st = scan_manager.status()
    last = st.get("last_summary")
    last_txt = (f'{last.get("mentions", 0)} found' if last else "no scans yet")

    health = (
        _health_row("Newspaper + YouTube scans", settings.scheduler_enabled,
                    "scheduled" if settings.scheduler_enabled else "manual only")
        + _health_row("LLM scoring (Claude)", settings.enable_llm_scoring and bool(settings.anthropic_api_key),
                      "on" if (settings.enable_llm_scoring and settings.anthropic_api_key) else "off")
        + _health_row("WhatsApp alerts", settings.notifier == "whatsapp" and bool(settings.whatsapp_access_token),
                      "live" if (settings.notifier == "whatsapp" and settings.whatsapp_access_token) else "dry-run")
        + _health_row("Email digest", settings.smtp_configured,
                      "SMTP" if settings.smtp_configured else "file preview")
        + _health_row("YouTube transcription", settings.youtube_transcriber != "stub",
                      settings.youtube_transcriber)
    )

    def tile(label, val, foot, ic):
        return (f'<div class="tile"><div class="ic">{ic}</div><div class="label">{label}</div>'
                f'<div class="val">{val}</div><div class="foot">{foot}</div></div>')

    body = f"""
    <div class="tiles">
      {tile("Total detections", total, "all time", "◎")}
      {tile("Today", today, "since 00:00 PKT", "↑")}
      {tile("Active keywords", active_kw, f'{np_kw} <a class="count-link" href="/newspapers">news</a> · {yt_kw} <a class="count-link" href="/youtube">YT</a>', "#")}
      {tile("Sources", n_papers + n_channels, f'{n_papers} papers · {n_channels} channel{"" if n_channels == 1 else "s"}', "◧")}
      {tile("Last scan", last_txt, '<a class="count-link" href="/mentions">view →</a>', "▶")}
    </div>

    <div class="cols">
      <div>
        <div class="panel">
          <h3>Detections — last 7 days</h3><div class="cap">Newspaper + YouTube hits per day (PKT)</div>
          {_bars(days)}
        </div>
        <div class="panel">
          <h3>Recent activity</h3><div class="cap">Latest matches across all sources</div>
          <div class="rlist">{recent_html}</div>
        </div>
      </div>
      <div>
        <div class="panel"><h3>Top sources</h3><div class="cap">Where hits are coming from</div>{_hbars(top_sources)}</div>
        <div class="panel"><h3>Sentiment mix</h3><div class="cap">Across scored detections</div>{_sentiment_block(sent_counts)}</div>
        <div class="panel"><h3>System</h3><div class="cap">Live configuration &amp; health</div>{health}</div>
      </div>
    </div>
    """
    return _shell("Media Monitor — Overview", "overview", body)


# --------------------------------------------------------------------------
# Module pages (Newspapers / YouTube) — each has its OWN keyword search
# --------------------------------------------------------------------------
def _kw_counts(db, module: str) -> dict:
    rows = db.execute(
        select(Mention.matched_keywords).where(Mention.module == module).limit(3000)
    ).scalars().all()
    c: dict[str, int] = {}
    for mk in rows:
        for k in (mk or []):
            c[k] = c.get(k, 0) + 1
    return c


def _keyword_table(keywords, page_path: str, src: str, counts: dict, scanning: bool, edit) -> str:
    if not keywords:
        return '<div class="empty">No keywords yet — add your first one above.</div>'
    rows = ""
    for k in keywords:
        if edit == k.id:
            en = "selected" if k.language == "en" else ""
            ur = "selected" if k.language == "ur" else ""
            rows += (
                f'<tr><td colspan="5">'
                f'<form method="post" action="/ui/keywords/{k.id}/edit" class="row">'
                f'<input name="text" value="{html.escape(k.text)}" required style="flex:1;min-width:200px">'
                f'<select name="language"><option value="en" {en}>English</option>'
                f'<option value="ur" {ur}>Urdu</option></select>'
                f'<button type="submit">Save</button>'
                f'<a href="{page_path}" style="align-self:center;color:var(--muted);font-weight:600;text-decoration:none">Cancel</a>'
                f"</form></td></tr>"
            )
            continue
        n = counts.get(k.text, 0)
        kwlink = f"/mentions?keyword={k.text}&src={src}"
        results = (f'<a class="count-link" href="{kwlink}">{n} result(s) →</a>'
                   if n else '<span class="muted-count">0 results</span>')
        status = ('<button class="ghost" title="Click to pause — paused keywords are skipped by scans">🟢 Active</button>'
                  if k.active else '<button title="Click to activate">⏸ Paused</button>')
        dim = "" if k.active else ' style="opacity:.55"'
        scan_disabled = "disabled" if (scanning or not k.active) else ""
        rows += (
            f"<tr{dim}>"
            f'<td><a class="kwname" href="{kwlink}">{html.escape(k.text)}</a></td>'
            f'<td><span class="tag">{k.language.upper()}</span></td>'
            f'<td><form method="post" action="/ui/keywords/{k.id}/toggle" style="margin:0">{status}</form></td>'
            f"<td>{results}</td>"
            f'<td class="row" style="justify-content:flex-end">'
            f'<a class="btn ghost" href="{page_path}?edit={k.id}">Edit</a>'
            f'<form method="post" action="/ui/keywords/{k.id}/scan" style="margin:0">'
            f'<button {scan_disabled} title="Scan this keyword now">▶ Scan</button></form>'
            f'<form method="post" action="/ui/keywords/{k.id}/delete" style="margin:0">'
            f'<button class="ghost">Delete</button></form></td></tr>'
        )
    return ('<table class="klist"><tr><th>Keyword</th><th>Lang</th><th>Status</th>'
            "<th>Detections</th><th></th></tr>" + rows + "</table>")


@app.get("/newspapers", response_class=HTMLResponse)
def newspapers_page(edit: int | None = None, db: Session = Depends(get_db)):
    keywords = db.execute(
        select(Keyword).where(Keyword.module == "newspaper").order_by(Keyword.created_at.desc())
    ).scalars().all()
    counts = _kw_counts(db, "newspaper")
    scanning = scan_manager.is_running()
    table = _keyword_table(keywords, "/newspapers", "newspaper", counts, scanning, edit)
    scan_all = (
        '<button class="btn-lg" disabled><span class="spin"></span>Scanning…</button>'
        if scanning else '<button class="btn-lg" type="submit">▶ Scan all newspaper keywords</button>'
    )
    n_sites = len(SITE_CONFIGS) + 1
    body = f"""
    <p class="sub">Keywords watched across <b>{n_sites} newspapers</b> (Dawn, The News,
    Tribune, Jang, Nawa-i-Waqt, ARY, Dunya, Express Urdu). These keywords are searched
    <b>only in newspapers</b>. <a href="/mentions?src=newspaper">View newspaper detections →</a></p>

    <div class="card">
      <form method="post" action="/ui/keywords" class="row" style="margin-bottom:.9rem">
        <input type="hidden" name="module" value="newspaper">
        <input name="text" placeholder="Add a newspaper keyword, e.g. Imran Khan" required
               style="flex:1;min-width:220px">
        <select name="language"><option value="en">English</option><option value="ur">Urdu</option></select>
        <button type="submit">+ Add keyword</button>
      </form>
      <form method="post" action="/ui/scan/newspaper" style="margin:0">{scan_all}</form>
    </div>
    <div class="card">{table}</div>
    """
    return _shell("Media Monitor — Newspapers", "newspapers", body)


@app.post("/ui/keywords")
def ui_add_keyword(text: str = Form(...), language: str = Form("en"),
                   module: str = Form("newspaper"), db: Session = Depends(get_db)):
    text = text.strip()
    module = module if module in ("newspaper", "youtube") else "newspaper"
    if text:
        exists = db.execute(
            select(Keyword).where(Keyword.text == text, Keyword.language == language,
                                  Keyword.module == module)
        ).first()
        if not exists:
            db.add(Keyword(text=text, language=language, module=module, active=True))
            db.commit()
    return RedirectResponse(f"/{'youtube' if module == 'youtube' else 'newspapers'}", status_code=303)


@app.post("/ui/keywords/{kid}/edit")
def ui_edit_keyword(kid: int, text: str = Form(...), language: str = Form("en"),
                    db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw and text.strip():
        kw.text = text.strip()
        kw.language = language if language in ("en", "ur") else kw.language
        db.commit()
    return RedirectResponse(_kw_page(kw), status_code=303)


@app.post("/ui/keywords/{kid}/toggle")
def ui_toggle_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    dest = _kw_page(kw)
    if kw:
        kw.active = not kw.active
        db.commit()
    return RedirectResponse(dest, status_code=303)


@app.post("/ui/keywords/{kid}/delete")
def ui_delete_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    dest = _kw_page(kw)
    if kw:
        db.delete(kw)
        db.commit()
    return RedirectResponse(dest, status_code=303)


@app.post("/ui/keywords/{kid}/scan")
def ui_scan_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if not kw:
        raise HTTPException(404, "keyword not found")
    # Route to the matching module's scanner so a YouTube keyword only scans
    # YouTube, and a newspaper keyword only scans newspapers.
    if kw.module == "youtube":
        scan_runner.start_scan(keyword_ids=[kid], label=kw.text)
    else:
        scan_manager.start_scan(keyword_ids=[kid], keyword_label=kw.text, capped=True)
    return RedirectResponse(f"/mentions?keyword={kw.text}&src={kw.module}", status_code=303)


def _kw_page(kw) -> str:
    return "/youtube" if (kw and kw.module == "youtube") else "/newspapers"


@app.post("/ui/scan")
def ui_scan_all():
    # Topbar "Scan all" — refresh both modules (separate subprocesses).
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    scan_runner.start_scan()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/scan/newspaper")
def ui_scan_newspapers():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/scan/youtube")
def ui_scan_youtube_kw():
    scan_runner.start_scan()
    return RedirectResponse("/youtube", status_code=303)


# --------------------------------------------------------------------------
# YouTube page — channels + YouTube-scoped keyword search
# --------------------------------------------------------------------------
@app.get("/youtube", response_class=HTMLResponse)
def youtube_page(error: str | None = None, edit: int | None = None, db: Session = Depends(get_db)):
    channels = db.execute(
        select(YouTubeChannel).order_by(YouTubeChannel.created_at.desc())
    ).scalars().all()
    if channels:
        crows = "".join(
            f"<tr><td><b>{html.escape(c.name or c.channel_id)}</b><br>"
            f'<span style="color:var(--faint);font-size:.78rem">{c.channel_id}</span></td>'
            f"<td>{'🟢 active' if c.active else '⚪ off'}</td>"
            f'<td class="row" style="justify-content:flex-end">'
            f'<form method="post" action="/ui/channels/{c.id}/scan" style="margin:0">'
            f'<button title="Scan this channel now">▶ Scan</button></form>'
            f'<form method="post" action="/ui/channels/{c.id}/delete" style="margin:0">'
            f'<button class="ghost">Delete</button></form></td></tr>'
            for c in channels
        )
        ctable = f"<table><tr><th>Channel</th><th>Status</th><th></th></tr>{crows}</table>"
    else:
        ctable = '<div class="empty">No channels yet. Add one above to start monitoring YouTube.</div>'

    keywords = db.execute(
        select(Keyword).where(Keyword.module == "youtube").order_by(Keyword.created_at.desc())
    ).scalars().all()
    counts = _kw_counts(db, "youtube")
    scanning = scan_runner.is_running()
    ktable = _keyword_table(keywords, "/youtube", "youtube", counts, scanning, edit)
    scan_all = (
        '<button class="btn-lg" disabled><span class="spin"></span>Scanning…</button>'
        if scanning else '<button class="btn-lg" type="submit">▶ Scan all YouTube keywords</button>'
    )
    banner = f'<div class="banner">⚠ {html.escape(error)}</div>' if error else ""
    trans = settings.youtube_transcriber
    note = ("Matching runs on video <b>title + description</b> (stub mode). Set "
            "YOUTUBE_TRANSCRIBER=openai or local to also search inside audio."
            if trans == "stub" else "Audio is transcribed for in-video keyword detection.")

    body = f"""
    {banner}
    <p class="sub">Monitor YouTube channels for keywords searched <b>only in YouTube</b>.
    {note} <a href="/mentions?src=youtube">View YouTube detections →</a></p>

    <div class="sechead">▶ Channels</div>
    <div class="card">
      <form method="post" action="/ui/channels" class="row" style="margin-bottom:.9rem">
        <input name="channel" placeholder="https://youtube.com/@GeoNews  or  UC…" required style="flex:1;min-width:240px">
        <button type="submit">+ Add channel</button>
      </form>
      {ctable}
    </div>

    <div class="sechead"># YouTube keywords</div>
    <div class="card">
      <form method="post" action="/ui/keywords" class="row" style="margin-bottom:.9rem">
        <input type="hidden" name="module" value="youtube">
        <input name="text" placeholder="Add a YouTube keyword, e.g. Imran Khan" required style="flex:1;min-width:220px">
        <select name="language"><option value="en">English</option><option value="ur">Urdu</option></select>
        <button type="submit">+ Add keyword</button>
      </form>
      <form method="post" action="/ui/scan/youtube" style="margin:0">{scan_all}</form>
    </div>
    <div class="card">{ktable}</div>
    """
    return _shell("Media Monitor — YouTube", "youtube", body)


@app.post("/ui/channels")
def ui_add_channel(channel: str = Form(...), db: Session = Depends(get_db)):
    channel_id, name = rss.resolve_channel_id(channel)
    if not channel_id:
        return RedirectResponse(
            "/youtube?error=Could not resolve that channel. Paste the full channel URL or its UC… id.",
            status_code=303,
        )
    if not db.execute(select(YouTubeChannel).where(YouTubeChannel.channel_id == channel_id)).first():
        db.add(YouTubeChannel(channel_id=channel_id, name=name, url=channel, active=True))
        db.commit()
    return RedirectResponse("/youtube", status_code=303)


@app.post("/ui/channels/{cid}/delete")
def ui_delete_channel(cid: int, db: Session = Depends(get_db)):
    ch = db.get(YouTubeChannel, cid)
    if ch:
        db.delete(ch)
        db.commit()
    return RedirectResponse("/youtube", status_code=303)


@app.post("/ui/channels/{cid}/scan")
def ui_scan_channel(cid: int, db: Session = Depends(get_db)):
    ch = db.get(YouTubeChannel, cid)
    if not ch:
        raise HTTPException(404, "channel not found")
    scan_runner.start_scan(channel_ids=[cid], label=ch.name or ch.channel_id)
    return RedirectResponse("/mentions?src=youtube", status_code=303)


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
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:.2rem">
      <p class="sub" style="margin:0">{len(mentions)} detection(s){' for “'+html.escape(keyword)+'”' if keyword else ''} —
      newspapers and YouTube shown separately below.</p>
      {clear_btn}
    </div>
    <div style="margin:.8rem 0 .3rem">{src_chips}</div>
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
    return [{"id": k.id, "text": k.text, "language": k.language, "module": k.module,
             "active": k.active} for k in rows]


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
