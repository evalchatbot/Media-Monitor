"""FastAPI application: the monitoring console.

Run:  uvicorn app.main:app --reload
  - Console:   http://127.0.0.1:8000/
  - API docs:  http://127.0.0.1:8000/docs  (kept for scripts; not linked in UI)

Two pipelines feed one Mention table:
  newspapers — website articles, scraped every N minutes (Playwright subprocess)
  e-paper    — each paper's daily PRINT edition: page scans fetched every
               morning, read with vision, matched with the same keywords

Manual scans run as subprocesses so the UI never blocks and Playwright stays
stable; a tiny poller keeps status live and reloads once on finish.
"""
from __future__ import annotations

import html
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from config import BASE_DIR, settings
from app.db.base import SessionLocal, init_db
from app.db.models import EPaperPage, Keyword, Mention, NewsSource, YouTubeChannel
from app.live import jobs as live_jobs, search as live_search
from app.core.keywords import script_language
from app import sources_probe

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_PKT = timezone(timedelta(hours=5))


def _build_version() -> str:
    """Short id of the running code, to tell what a host actually deployed.

    Railway sets RAILWAY_GIT_COMMIT_SHA to the deployed commit; fall back to the
    local git HEAD in dev. "unknown" if neither is available.
    """
    import os

    sha = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("GIT_COMMIT")
        or ""
    ).strip()
    if not sha:
        try:
            import subprocess

            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(BASE_DIR),
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode().strip()
        except Exception:
            sha = ""
    return sha[:7] if sha else "unknown"


BUILD_VERSION = _build_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Live-only model: no scheduler, no background scans, no warm-up crawl.
    # Scraping happens ONLY when the user clicks "Search live results", and
    # nothing is persisted. init_db still runs so the keyword watchlist (the one
    # thing we keep) has its table.
    init_db()
    # E-paper clippings are written per job and deleted when that job is
    # evicted — but a restart strands whatever the previous process was holding,
    # so clear those before serving.
    try:
        from app.epaper.livescan import sweep_orphans
        sweep_orphans(max_age_seconds=0)
    except Exception as exc:  # pragma: no cover
        logger.warning("live storage sweep at startup failed: %s", exc)
    yield


app = FastAPI(title="Media Monitoring", version="0.3.0", lifespan=lifespan)

settings.storage_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(settings.storage_dir)), name="media")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================================================
# Design — calm daylight (single page)
# ==========================================================================
_CSS = """
:root{
  --cream:#faf6ef;
  --cream-deep:#f3ebe0;
  --blue:#6ba3c4;
  --blue-deep:#4a8ab0;
  --blue-soft:#e4f0f7;
  --blue-mist:#d0e4f0;
  --bg:var(--cream);
  --bg-glow:#e8f2f8;
  --surface:#fffdf9;
  --ink:#2c3a48;
  --muted:#6a7a8a;
  --faint:#97a6b4;
  --line:#e6ddd0;
  --line-strong:#d9cdbc;
  --accent:var(--blue-deep);
  --accent-soft:var(--blue-soft);
  --ok:#4a9a6a;
  --warn:#9a7a3a;
  --warn-soft:#f7f0e0;
  --warn-border:#e8dcc0;
  --shadow:0 14px 36px -18px rgba(74,138,176,.35);
  --shadow-sm:0 3px 12px -6px rgba(74,138,176,.18);
  --r:18px;--r-sm:13px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--ink);min-height:100vh;
  background:
    radial-gradient(900px 520px at 8% -8%,var(--bg-glow),transparent 58%),
    radial-gradient(700px 420px at 96% 4%,#f0e8da,transparent 52%),
    linear-gradient(180deg,var(--cream) 0%,#f7f1e6 100%);
  font-family:"Manrope",system-ui,sans-serif;font-size:15px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
::selection{background:var(--blue-mist);color:var(--ink)}
a{color:inherit}
h1,h2{font-family:"Fraunces","Manrope",serif;font-weight:600;letter-spacing:-.02em}

.top{position:sticky;top:0;z-index:40;padding:.85rem 1.25rem .4rem;
  background:linear-gradient(var(--cream) 55%,transparent)}
.top-inner{max-width:880px;margin:0 auto;display:flex;align-items:center;gap:1rem;
  background:rgba(255,253,249,.9);border:1px solid var(--line);border-radius:999px;
  padding:.45rem .55rem .45rem .7rem;box-shadow:var(--shadow-sm);
  backdrop-filter:blur(14px)}
.brand{display:inline-flex;align-items:center;gap:.65rem;text-decoration:none;color:var(--ink)}
.mod-nav{display:inline-flex;gap:.3rem;margin-left:.15rem}
.mod-nav a{padding:.32rem .75rem;border-radius:999px;font-size:.8rem;font-weight:700;
  text-decoration:none;color:var(--muted);border:1px solid transparent}
.mod-nav a:hover{color:var(--blue-deep);background:var(--blue-soft)}
.mod-nav a.on{color:#fff;background:var(--blue-deep);border-color:var(--blue-deep)}
.yt-tabs{display:flex;gap:.35rem;margin:1rem 0 0;flex-wrap:wrap}
.yt-tab{padding:.5rem 1rem;border-radius:999px 999px 0 0;border:1px solid var(--line);border-bottom:none;
  background:var(--surface);color:var(--muted);font-weight:700;font-size:.85rem;cursor:pointer}
.yt-tab:hover{color:var(--blue-deep)}
.yt-tab.on{background:linear-gradient(180deg,#fffdf9,#faf4ea);color:var(--blue-deep);border-color:var(--line)}
.yt-panel{margin-top:0}
.yt-panel .panel{border-top-left-radius:0}
.jump{display:inline-flex;margin-top:.25rem;padding:.2rem .55rem;border-radius:999px;
  background:var(--blue-soft);color:var(--blue-deep);font-size:.72rem;font-weight:700;
  text-decoration:none;border:1px solid var(--blue-mist);margin-right:.35rem}
.jump:hover{background:var(--blue-mist)}
.hits{display:flex;flex-direction:column;gap:.2rem;margin-top:.3rem}
.hitrow{display:flex;flex-wrap:wrap;align-items:center;gap:.15rem}
.hitkw{font-size:.68rem;font-weight:700;color:var(--muted);margin-right:.3rem;
  max-width:14rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hitrow .jump{margin-top:0}
.more-wrap{display:flex;align-items:center;gap:.8rem;justify-content:center;margin:1.1rem 0 .3rem}
.more-btn{padding:.5rem 1.4rem;border-radius:999px;border:1px solid var(--blue-mist);
  background:var(--blue-soft);color:var(--blue-deep);font-weight:700;font-size:.82rem;cursor:pointer}
.more-btn:hover{background:var(--blue-mist)}
.more-btn:disabled{opacity:.6;cursor:default}
.more-count{font-size:.75rem;color:var(--muted)}
.build-tag{font-size:.6rem;color:var(--faint);letter-spacing:.03em;opacity:.6}
#results{transition:opacity .12s ease}
.brand .mark{width:34px;height:34px;border-radius:11px;
  background:linear-gradient(145deg,var(--blue) 0%,var(--blue-deep) 100%);color:#fff;
  display:inline-flex;align-items:center;justify-content:center;font-size:.95rem;
  box-shadow:0 4px 12px -4px rgba(74,138,176,.55)}
.brand b{display:block;font-size:.98rem;font-weight:700;line-height:1.1}
.brand small{display:block;color:var(--muted);font-size:.68rem;font-weight:600;
  text-transform:uppercase;letter-spacing:.1em;margin-top:.08rem}
.spacer{flex:1}
.live{font-size:.82rem;color:var(--muted);font-weight:600;white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:.35rem;vertical-align:middle}
.dot.live{background:var(--ok);box-shadow:0 0 0 3px rgba(74,154,106,.18)}
.dot.busy{background:var(--warn);box-shadow:0 0 0 3px rgba(154,122,58,.18);animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

button,.btn{background:linear-gradient(145deg,var(--blue) 0%,var(--blue-deep) 100%);color:#fff;
  border:1px solid transparent;border-radius:999px;
  padding:.55rem 1.15rem;font-size:.88rem;font-weight:700;cursor:pointer;font-family:inherit;
  display:inline-flex;align-items:center;gap:.4rem;transition:opacity .15s,transform .15s,box-shadow .15s;
  box-shadow:0 6px 16px -8px rgba(74,138,176,.55)}
button:hover,.btn:hover{opacity:.95;transform:translateY(-1px);box-shadow:0 8px 20px -8px rgba(74,138,176,.65)}
button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
button.ghost{background:var(--surface);color:var(--ink);border-color:var(--line-strong);box-shadow:none}
button.ghost:hover{background:var(--blue-soft);border-color:var(--blue);color:var(--blue-deep)}

.page{padding:1.2rem 1.25rem 3.5rem}
.wrap{max-width:880px;margin:0 auto}
.hero{margin:.4rem 0 1.35rem}
.hero h1{margin:0;font-size:clamp(1.55rem,3.2vw,2rem);line-height:1.2;color:var(--ink)}
.hero p{margin:.35rem 0 0;color:var(--muted);max-width:48ch;font-size:.95rem}

.panel{background:linear-gradient(180deg,#fffdf9 0%,#faf4ea 100%);border:1px solid var(--line);
  border-radius:var(--r);padding:1.3rem 1.4rem;box-shadow:var(--shadow-sm);margin-bottom:1.1rem}
.panel h2{margin:0 0 1rem;font-size:1.05rem;color:var(--blue-deep)}
.field{margin-bottom:1.05rem}
.field:last-child{margin-bottom:0}
.field label{display:block;font-size:.78rem;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:.4rem}
.field input[type=date],.field input[type=text]{width:100%;max-width:420px;padding:.65rem .9rem;
  border:1px solid var(--line-strong);border-radius:999px;font:inherit;
  background:#fffdf9;color:var(--ink)}
.field input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-soft)}

.papers{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.45rem .55rem}
.papers label{display:flex;align-items:center;gap:.45rem;padding:.5rem .65rem;border:1px solid var(--line);
  border-radius:var(--r-sm);background:#fffdf9;font-size:.88rem;font-weight:600;color:var(--ink);
  cursor:pointer;text-transform:none;letter-spacing:0;margin:0;transition:border-color .15s,background .15s,box-shadow .15s}
.papers label:hover{border-color:var(--blue);background:var(--blue-soft);box-shadow:var(--shadow-sm)}
.papers input{accent-color:var(--blue-deep);width:15px;height:15px}
.paper-tools{display:flex;gap:.5rem;margin:.35rem 0 .75rem;flex-wrap:wrap}
.paper-tools button{padding:.32rem .75rem;font-size:.78rem}

.actions{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-top:1.15rem}
.hint{color:var(--faint);font-size:.82rem;margin-top:.7rem}

.results{margin-top:.2rem}
.results-head{display:flex;align-items:center;justify-content:space-between;gap:.55rem;flex-wrap:wrap;margin-bottom:.85rem}
.results-head h2{margin:0;font-size:1.15rem;color:var(--blue-deep);flex:0 1 auto}
.results-head .count{color:var(--muted);font-size:.82rem;font-weight:600}
.results-head .results-actions{margin-left:auto;display:flex;gap:.45rem;align-items:center}
.results-filter-hint{width:100%;margin:-.25rem 0 .35rem;font-size:.78rem;color:var(--faint)}
.results-head .spin{margin-left:.35rem;vertical-align:-1px;color:var(--blue-deep)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem}
.det{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
  display:flex;flex-direction:column;box-shadow:var(--shadow-sm);transition:box-shadow .2s,transform .2s,border-color .2s}
.det:hover{box-shadow:var(--shadow);transform:translateY(-3px);border-color:var(--blue-mist)}
.det .shot{position:relative;background:linear-gradient(160deg,var(--blue-soft),var(--cream-deep));
  border-bottom:1px solid var(--line);min-height:190px}
.det .shot.missing{display:flex;align-items:center;justify-content:center}
.det .noprev{color:var(--faint);font-size:.82rem;font-weight:600}
.det img{width:100%;height:190px;object-fit:cover;object-position:top center;cursor:zoom-in;display:block}
.det .pagebadge{position:absolute;top:.55rem;left:.55rem;
  background:rgba(74,138,176,.9);color:#fff;font-size:.68rem;font-weight:700;
  border-radius:999px;padding:.18rem .55rem;backdrop-filter:blur(4px)}
.det .body{padding:.9rem 1rem;display:flex;flex-direction:column;gap:.45rem;background:#fffdf9}
.det .ttl{font-weight:700;line-height:1.35;text-decoration:none;color:var(--ink)}
.det .ttl:hover{color:var(--blue-deep)}
.det .meta{color:var(--faint);font-size:.76rem;font-weight:500}
.det .excerpt{color:var(--muted);font-size:.82rem;line-height:1.45;max-height:3.8em;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.tag{display:inline-flex;background:var(--blue-soft);color:var(--blue-deep);border-radius:999px;
  padding:.12rem .5rem;font-size:.72rem;font-weight:700;margin:2px 3px 2px 0;border:1px solid var(--blue-mist)}
.sent{display:inline-flex;align-items:center;gap:.25rem;border-radius:999px;padding:.12rem .55rem;
  font-size:.72rem;font-weight:800;margin:2px 5px 2px 0;border:1px solid transparent}
.sent::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.sent-pos{background:#e7f4ec;color:#2e8b57;border-color:#cbe7d5}
.sent-neg{background:#fbecea;color:#c0492b;border-color:#f0d2cb}
.sent-neu{background:#eef1f4;color:#5a6b7a;border-color:#dde3e9}
.kw-bar{margin-top:.65rem}
.kw-bar .cap{font-size:.72rem;font-weight:700;color:var(--faint);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:.4rem}
.kw-tags{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center}
.kw-chip{display:inline-flex;align-items:stretch;border-radius:999px;overflow:hidden;
  border:1px solid var(--line);background:#fffdf9;transition:border-color .15s,background .15s,box-shadow .15s}
.kw-chip:hover{border-color:var(--blue);background:var(--blue-soft)}
.kw-chip.on{border-color:transparent;background:var(--blue-deep);
  box-shadow:0 4px 12px -6px rgba(74,138,176,.55)}
.kw-pick{display:inline-flex;align-items:center;padding:.28rem .55rem .28rem .7rem;border:0;border-radius:0;
  background:transparent;color:var(--ink);font-size:.8rem;font-weight:600;
  cursor:pointer;font-family:inherit;box-shadow:none}
.kw-pick:hover{transform:none;box-shadow:none;opacity:1;background:transparent}
.kw-chip.on .kw-pick{color:#fff}
.kw-chip.on:hover{background:var(--blue)}
.kw-x{display:inline-flex;align-items:center;justify-content:center;width:1.55rem;padding:0;
  border:0;border-left:1px solid var(--line);border-radius:0;background:transparent;
  color:var(--faint);font-size:.95rem;font-weight:700;line-height:1;cursor:pointer;
  font-family:inherit;box-shadow:none}
.kw-x:hover{background:rgba(176,69,43,.12);color:#b0452b;transform:none;box-shadow:none;opacity:1}
.kw-chip.on .kw-x{border-left-color:rgba(255,255,255,.28);color:rgba(255,255,255,.85)}
.kw-chip.on .kw-x:hover{background:rgba(0,0,0,.18);color:#fff}
.kw-play{display:inline-flex;align-items:center;justify-content:center;width:1.55rem;padding:0;
  border:0;border-left:1px solid var(--line);border-radius:0;background:transparent;
  color:var(--blue-deep);font-size:.72rem;font-weight:700;line-height:1;cursor:pointer;
  font-family:inherit;box-shadow:none}
.kw-play:hover{background:rgba(74,138,176,.15);color:var(--blue-deep);transform:none;box-shadow:none;opacity:1}
.kw-chip.on .kw-play{border-left-color:rgba(255,255,255,.28);color:#fff}
.kw-chip.on .kw-play:hover{background:rgba(0,0,0,.18);color:#fff}
.kw-play .spin,.kw-busy .spin{width:11px;height:11px;border-width:2px}
.kw-chip.busy .kw-play{cursor:default;pointer-events:none}
.kw-chip.on.busy .kw-play{color:#fff}
.kw-chip.sel{border-color:var(--blue);background:var(--blue-soft);
  box-shadow:0 0 0 2px rgba(74,138,176,.25)}
.kw-chip.sel .kw-toggle::before{content:"✓ ";font-size:.72rem;opacity:.85}
.kw-toggle{display:inline-flex;align-items:center;padding:.28rem .55rem .28rem .7rem;border:0;border-radius:0;
  background:transparent;color:var(--ink);font-size:.8rem;font-weight:600;
  cursor:pointer;font-family:inherit;box-shadow:none}
.kw-toggle:hover{transform:none;box-shadow:none;opacity:1;background:transparent}
.kw-chip.on .kw-toggle{color:#fff}
.kw-chip.sel.on{border-color:var(--blue-deep);background:var(--blue-deep)}
.kw-chip.sel.on .kw-toggle{color:#fff}
.kw-confirm .ghost{margin-right:.35rem}
.slot-picks{display:flex;flex-wrap:wrap;gap:.35rem .55rem;margin-top:.35rem}
.slot-picks label{display:inline-flex;align-items:center;gap:.35rem;font-size:.8rem;font-weight:600;
  padding:.25rem .45rem;border-radius:8px;border:1px solid var(--line);background:#fffdf9;cursor:pointer}
.slot-picks label:hover{border-color:var(--blue);background:var(--blue-soft)}
.slot-picks input{margin:0}
.live-detail{font-size:.78rem;color:var(--faint);max-width:28rem;text-align:right;line-height:1.25}
.kw-del,.kw-play-form{margin:0;display:inline-flex}
.det.scanning{position:relative}
.det.scanning::after{content:"";position:absolute;top:.55rem;right:.55rem;width:12px;height:12px;
  border:2px solid var(--blue-deep);border-top-color:transparent;border-radius:50%;
  animation:s .7s linear infinite;background:rgba(255,253,249,.85);box-shadow:0 0 0 3px rgba(255,253,249,.85)}
.empty.loading{display:flex;align-items:center;justify-content:center;gap:.55rem;min-height:7rem}
.kw-pick.kw-all{border:1px solid var(--line);border-radius:999px;padding:.28rem .7rem;background:#fffdf9}
.kw-pick.kw-all.on{background:var(--blue-deep);border-color:transparent;color:#fff;
  box-shadow:0 4px 12px -6px rgba(74,138,176,.55)}
.kw-pick.kw-all:hover{background:var(--blue-soft);border-color:var(--blue);color:var(--blue-deep)}
.kw-pick.kw-all.on:hover{background:var(--blue);color:#fff}
.kw-add{display:flex;gap:.45rem;flex-wrap:wrap;align-items:center;margin:0}
.kw-pending{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.55rem 0 .35rem;min-height:0}
.kw-pending:empty{display:none}
.kw-draft{display:inline-flex;align-items:center;gap:.25rem;border:1px dashed var(--blue);
  background:var(--blue-soft);color:var(--blue-deep);border-radius:999px;padding:.22rem .35rem .22rem .7rem;
  font-size:.8rem;font-weight:600}
.kw-draft button{border:0;background:transparent;color:var(--blue-deep);cursor:pointer;font:inherit;
  width:1.35rem;padding:0;box-shadow:none;border-radius:999px}
.kw-draft button:hover{background:rgba(74,138,176,.18);transform:none;opacity:1}
.kw-confirm{margin-top:.35rem;display:none}
.kw-confirm.show{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.cap{font-size:.72rem;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
.kw-add input{flex:1;min-width:160px;max-width:100%;padding:.65rem .9rem;border:1px solid var(--line-strong);
  border-radius:999px;font:inherit;background:#fffdf9;color:var(--ink)}
.kw-add input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-soft)}
.kw-add select{padding:.65rem .7rem;border:1px solid var(--line-strong);border-radius:999px;
  font:inherit;background:#fffdf9;color:var(--ink)}
.kw-add button{padding:.55rem 1rem;font-size:.88rem}
mark{background:#ffe9a8;color:var(--ink);border-radius:3px;padding:0 .1em;font-weight:700}

.empty{color:var(--muted);text-align:center;padding:2.2rem 1.2rem;border:1.5px dashed var(--line-strong);
  border-radius:var(--r);background:rgba(255,253,249,.75)}
.banner{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn);
  border-radius:var(--r-sm);padding:.75rem 1rem;margin-bottom:1rem;font-weight:600;font-size:.88rem}
.banner.ok{background:var(--blue-soft);border-color:var(--blue-mist);color:var(--blue-deep)}

/* Add-source modal */
#src-modal{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;
  padding:1rem;background:rgba(44,58,72,.45);backdrop-filter:blur(6px)}
#src-modal.open{display:flex}
#src-modal .box{width:min(440px,100%);background:linear-gradient(180deg,#fffdf9,#faf4ea);
  border:1px solid var(--line);border-radius:var(--r);padding:1.35rem 1.4rem;box-shadow:var(--shadow)}
#src-modal h3{margin:0 0 .35rem;font-size:1.15rem;color:var(--blue-deep)}
#src-modal .sub{margin:0 0 1rem;color:var(--muted);font-size:.88rem}
#src-modal .kinds{display:flex;gap:.5rem;margin-bottom:.9rem}
#src-modal .kinds label{flex:1;display:flex;align-items:center;justify-content:center;gap:.4rem;
  padding:.65rem;border:1px solid var(--line);border-radius:var(--r-sm);background:#fffdf9;
  cursor:pointer;font-weight:600;font-size:.88rem;text-transform:none;letter-spacing:0;margin:0}
#src-modal .kinds label:has(input:checked){border-color:var(--blue);background:var(--blue-soft);color:var(--blue-deep)}
#src-modal input[type=url],#src-modal input[type=text]{width:100%;padding:.65rem .9rem;margin-bottom:.7rem;
  border:1px solid var(--line-strong);border-radius:999px;font:inherit;background:#fffdf9}
#src-modal .row-btns{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.4rem}
#src-modal #src-result{margin-top:.9rem;padding:.75rem .9rem;border-radius:var(--r-sm);font-size:.88rem;
  font-weight:600;line-height:1.45;display:none}
#src-modal #src-result.show{display:block}
#src-modal #src-result.ok{background:var(--blue-soft);border:1px solid var(--blue-mist);color:var(--blue-deep)}
#src-modal #src-result.bad{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn)}

#yt-ch-modal{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;
  padding:1rem;background:rgba(44,58,72,.45);backdrop-filter:blur(6px)}
#yt-ch-modal.open{display:flex}
#yt-ch-modal .box{width:min(480px,100%);background:linear-gradient(180deg,#fffdf9,#faf4ea);
  border:1px solid var(--line);border-radius:var(--r);padding:1.35rem 1.4rem;box-shadow:var(--shadow)}
#yt-ch-modal h3{margin:0 0 .35rem;font-size:1.15rem;color:var(--blue-deep)}
#yt-ch-modal .sub{margin:0 0 1rem;color:var(--muted);font-size:.88rem}
#yt-ch-modal input[type=url],#yt-ch-modal input[type=text]{width:100%;padding:.65rem .9rem;margin-bottom:.7rem;
  border:1px solid var(--line-strong);border-radius:999px;font:inherit;background:#fffdf9}
#yt-ch-modal .row-btns{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.4rem}
#yt-ch-modal #yt-ch-result{margin-top:.9rem;padding:.75rem .9rem;border-radius:var(--r-sm);font-size:.88rem;
  font-weight:600;line-height:1.45;display:none}
#yt-ch-modal #yt-ch-result.show{display:block}
#yt-ch-modal #yt-ch-result.ok{background:var(--blue-soft);border:1px solid var(--blue-mist);color:var(--blue-deep)}
#yt-ch-modal #yt-ch-result.bad{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn)}

#yt-live-list .live-pick{display:block;padding:.5rem .6rem;margin:.3rem 0;border:1px solid var(--line);
  border-radius:var(--r-sm);background:#fffdf9;font-size:.85rem;line-height:1.4;cursor:pointer}
#yt-live-list .live-pick:hover{border-color:var(--blue-mist);background:var(--blue-soft)}
#yt-live-list input[type=radio]{margin-right:.35rem}
#yt-live-results .hitrow{margin:.25rem 0}
.ticker-list{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm);padding:.35rem .5rem;background:#fffdf9}
.ticker-row{display:flex;gap:.55rem;align-items:flex-start;padding:.45rem 0;border-bottom:1px solid var(--line)}
.ticker-row:last-child{border-bottom:none}
.ticker-row span{font-size:.9rem;line-height:1.5;color:var(--ink)}
.ticker-body{display:flex;flex-direction:column;gap:.3rem;min-width:0;flex:1}
.ticker-cut{display:block;max-width:100%;height:auto;border:1px solid var(--line);border-radius:4px;background:#000}
.hitcuts{display:flex;flex-direction:column;gap:.45rem;flex:1 1 100%;margin-top:.15rem}
.hitcut{display:flex;gap:.5rem;align-items:flex-start}
.ticker-row .jump{margin-top:0;flex:none}
.live-slider{margin:.45rem 0}
.live-slider label{display:block;font-size:.78rem;font-weight:600;color:var(--muted);margin-bottom:.2rem}
.live-slider b{color:var(--blue-deep);font-weight:700}
.live-slider input[type=range]{width:100%;accent-color:var(--blue-deep);height:22px}

.ch-bar{margin:.65rem 0 .35rem}
.ch-tags{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.35rem}
.ch-tag{display:inline-flex;align-items:center;padding:.22rem .65rem;border-radius:999px;font-size:.78rem;
  font-weight:600;background:var(--blue-soft);color:var(--blue-deep);border:1px solid var(--blue-mist)}
.chip-x{border:none;background:none;cursor:pointer;font-size:1rem;line-height:1;margin-left:.3rem;
  padding:0 .12rem;color:inherit;opacity:.5;border-radius:999px}
.chip-x:hover{opacity:1;color:#c0392b}
.src-x{border:none;background:none;cursor:pointer;font-size:.95rem;line-height:1;margin-left:.15rem;
  padding:0 .15rem;color:var(--muted);opacity:.55;border-radius:999px}
.src-x:hover{opacity:1;color:#c0392b}
.paper-item{display:inline-flex;align-items:center;gap:.1rem}

.spin{display:inline-block;width:13px;height:13px;border:2px solid currentColor;border-top-color:transparent;
  border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}

/* Top loading bar — slides in while a live fetch / scan is producing results */
#loadbar{position:fixed;top:0;left:0;right:0;height:3px;z-index:120;background:transparent;
  overflow:hidden;opacity:0;transition:opacity .25s;pointer-events:none}
#loadbar.on{opacity:1}
#loadbar::before{content:"";position:absolute;top:0;left:-40%;height:100%;width:40%;
  background:linear-gradient(90deg,transparent,var(--blue-deep),var(--blue));
  border-radius:999px;animation:lbslide 1.05s cubic-bezier(.4,0,.2,1) infinite}
@keyframes lbslide{0%{left:-40%}50%{left:30%}100%{left:100%}}

/* Page transition — a ~1s branded wipe that covers the whole navigation.
   The panel slides UP to cover the old page, then keeps sliding up on the new
   page to reveal it — one continuous motion across the load. */
@keyframes pageIn{from{opacity:0}to{opacity:1}}
main.page{animation:pageIn .4s ease}
#pt{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1.1rem;color:#fff;
  background:linear-gradient(160deg,var(--blue) 0%,var(--blue-deep) 55%,var(--accent) 100%);
  transform:translateY(100%)}
#pt .pt-mark{width:66px;height:66px;border-radius:20px;display:flex;align-items:center;
  justify-content:center;font-size:2rem;background:rgba(255,255,255,.16);
  box-shadow:0 12px 34px -8px rgba(0,0,0,.4);animation:ptSpin 1.5s cubic-bezier(.5,0,.5,1) infinite}
#pt .pt-label{font-family:"Fraunces","Manrope",serif;font-size:1.7rem;font-weight:600;
  letter-spacing:-.02em;opacity:.96}
#pt .pt-dots{font-size:.8rem;opacity:.75;letter-spacing:.3em;text-transform:uppercase}
#pt.cover{transform:translateY(0)}
#pt.in{animation:ptIn .5s cubic-bezier(.55,0,.35,1) forwards}
#pt.out{animation:ptOut .55s cubic-bezier(.35,0,.25,1) forwards}
@keyframes ptIn{from{transform:translateY(100%)}to{transform:translateY(0)}}
@keyframes ptOut{from{transform:translateY(0)}to{transform:translateY(-100%)}}
@keyframes ptSpin{to{transform:rotate(360deg)}}

/* Live-search progress: a real bar + what's happening + time remaining */
.live-progress{margin:.15rem 0 1.1rem}
.live-bar{height:9px;border-radius:999px;background:var(--cream-deep);
  border:1px solid var(--line);overflow:hidden;position:relative}
.live-bar-fill{height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--blue),var(--blue-deep));
  transition:width .45s cubic-bezier(.4,0,.2,1);min-width:2%}
.live-bar.indet .live-bar-fill{position:absolute;width:38%;min-width:0;
  animation:indet 1.15s ease-in-out infinite}
@keyframes indet{0%{left:-40%}100%{left:100%}}
.live-sub{margin-top:.5rem;display:flex;justify-content:space-between;gap:1rem;
  color:var(--muted);font-size:.82rem;font-weight:600;flex-wrap:wrap}
.live-sub .now{color:var(--ink)}
.live-sub .eta{color:var(--blue-deep);white-space:nowrap}

@media (prefers-reduced-motion:reduce){
  main.page{animation:none}
  #pt{display:none!important}
  .live-bar-fill{transition:none}
}

/* Confirm dialog (delete keyword) — reuses the daylight modal look */
#confirm-modal{position:fixed;inset:0;z-index:130;display:none;align-items:center;justify-content:center;
  background:rgba(20,28,36,.32);backdrop-filter:blur(2px);padding:1rem}
#confirm-modal.open{display:flex}
#confirm-modal .box{width:min(380px,100%);background:linear-gradient(180deg,#fffdf9,#faf4ea);
  border:1px solid var(--line-strong);border-radius:var(--r);box-shadow:var(--shadow);padding:1.4rem 1.35rem}
#confirm-modal h3{margin:0 0 .5rem;font-size:1.1rem;color:var(--blue-deep)}
#confirm-modal p{margin:0 0 1.2rem;color:var(--muted);font-size:.9rem;line-height:1.5}
#confirm-modal p b{color:var(--ink)}
#confirm-modal .row-btns{display:flex;gap:.55rem;justify-content:flex-end}
#confirm-modal button{border-radius:999px;padding:.5rem 1.1rem;font-size:.86rem;font-weight:700;cursor:pointer}
#confirm-modal .danger{background:#b0452b;color:#fff;border:1px solid #9a3a22}
#confirm-modal .danger:hover{background:#9a3a22;transform:none}

#lb{position:fixed;inset:0;z-index:100;background:rgba(16,24,32,.92);display:none;opacity:0;transition:opacity .2s}
#lb.open{display:block;opacity:1}
#lbscroll{position:absolute;inset:0;overflow:auto;text-align:center}
#lbwrap{display:inline-block;padding:3.2rem 1rem 2.2rem}
#lbscroll img{display:block;border-radius:10px;box-shadow:0 30px 80px -20px rgba(0,0,0,.7);
  cursor:zoom-in;user-select:none}
#lbbar{position:fixed;top:.7rem;right:.85rem;display:flex;gap:.35rem;z-index:102;align-items:center}
#lbbar button{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);color:#fff;
  border-radius:999px;padding:.38rem .8rem;font-size:.82rem;font-weight:700;box-shadow:none}
#lbbar button:hover{background:rgba(255,255,255,.26);transform:none;opacity:1}
#lbpct{color:#eee;font-size:.78rem;font-weight:700;min-width:3rem;text-align:center}
#lbhint{position:fixed;bottom:.75rem;left:50%;transform:translateX(-50%);z-index:102;color:#ccc;
  font-size:.74rem;font-weight:600;background:rgba(0,0,0,.5);padding:.28rem .8rem;border-radius:999px;pointer-events:none}
@media (max-width:700px){#lbhint{display:none}.top-inner{border-radius:18px}.live{display:none}}

/* --- Mobile / small screens --- */
@media (max-width:760px){
  .wrap,.top-inner{max-width:100%}
  .top{padding:.5rem .55rem .3rem}
  .top-inner{gap:.4rem;padding:.4rem .5rem;flex-wrap:wrap}
  .spacer{display:none}
  .brand small{display:none}
  .live-wrap{display:none}
  #scanbtn{margin-left:auto}
  .page{padding:1rem .7rem 2.4rem}
  .panel{padding:1.05rem .85rem}
  .hero{margin:.3rem 0 1rem}
  .grid{grid-template-columns:1fr;gap:.85rem}
  .papers{grid-template-columns:1fr 1fr}
  .field input[type=date],.field input[type=text]{max-width:100%}
  .live-detail{display:none}
  /* wide content must scroll inside itself, never the page body */
  .grid,.papers,.kw-tags{max-width:100%}
  img{max-width:100%}
}
@media (max-width:430px){
  .mod-nav a{padding:.3rem .58rem;font-size:.75rem}
  .papers{grid-template-columns:1fr}
  .hero h1{font-size:1.5rem}
  .hero p{font-size:.88rem}
  .det .body{padding:.8rem .85rem}
  .brand .mark{width:30px;height:30px}
}
"""

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700'
    '&family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">'
)

_JS = """
(function(){
  var lb=document.createElement('div');lb.id='lb';
  lb.innerHTML='<div id="lbscroll"><div id="lbwrap"><img draggable="false"></div></div>'
    +'<div id="lbbar"><button data-z="out">−</button><span id="lbpct"></span>'
    +'<button data-z="in">+</button><button data-z="fit">Fit</button>'
    +'<button data-z="full">1:1</button><button data-z="x">✕</button></div>'
    +'<div id="lbhint">scroll · Ctrl+wheel zoom · Esc closes</div>';
  document.body.appendChild(lb);
  var lbimg=lb.querySelector('img'),lbscroll=lb.querySelector('#lbscroll'),
      lbpct=lb.querySelector('#lbpct'),scale=1,fitScale=1;
  function apply(){lbimg.style.width=Math.round(lbimg.naturalWidth*scale)+'px';
    lbpct.textContent=Math.round(scale*100)+'%';}
  function setScale(s){scale=Math.min(Math.max(s,Math.min(fitScale,1)*0.4),8);apply()}
  function zoomAt(f,cx,cy){
    var r=lbscroll.getBoundingClientRect();
    var ox=lbscroll.scrollLeft+(cx-r.left),oy=lbscroll.scrollTop+(cy-r.top);
    var s0=scale;setScale(scale*f);var k=scale/s0;
    lbscroll.scrollLeft=ox*k-(cx-r.left);lbscroll.scrollTop=oy*k-(cy-r.top);
  }
  function openLb(src){
    lb.classList.add('open');document.documentElement.style.overflow='hidden';
    lbimg.src='';lbimg.src=src;
    lbimg.onload=function(){
      fitScale=Math.min(1,(lbscroll.clientWidth-40)/lbimg.naturalWidth);
      setScale(fitScale);lbscroll.scrollTop=0;lbscroll.scrollLeft=0;
    };
  }
  function closeLb(){lb.classList.remove('open');document.documentElement.style.overflow=''}
  document.addEventListener('click',function(e){
    var z=e.target.closest('.zoom');
    if(z){e.preventDefault();openLb(z.getAttribute('data-full')||z.src);return}
    if(!lb.classList.contains('open'))return;
    var b=e.target.closest('#lbbar button');
    if(b){
      if(b.dataset.z==='in')zoomAt(1.3,innerWidth/2,innerHeight/2);
      else if(b.dataset.z==='out')zoomAt(1/1.3,innerWidth/2,innerHeight/2);
      else if(b.dataset.z==='fit')setScale(fitScale);
      else if(b.dataset.z==='full')setScale(1);
      else closeLb();
      return;
    }
    if(e.target===lbimg){
      if(scale<0.999)zoomAt(1/scale,e.clientX,e.clientY);else setScale(fitScale);
      return;
    }
    if(e.target.id==='lbscroll'||e.target.id==='lbwrap')closeLb();
  });
  lbscroll.addEventListener('wheel',function(e){
    if(!lb.classList.contains('open')||!e.ctrlKey)return;
    e.preventDefault();zoomAt(e.deltaY<0?1.18:1/1.18,e.clientX,e.clientY);
  },{passive:false});
  document.addEventListener('keydown',function(e){
    if(!lb.classList.contains('open'))return;
    if(e.key==='Escape')closeLb();
    else if(e.key==='+'||e.key==='=')zoomAt(1.3,innerWidth/2,innerHeight/2);
    else if(e.key==='-')zoomAt(1/1.3,innerWidth/2,innerHeight/2);
    else if(e.key==='0')setScale(fitScale);
  });

  var all=document.getElementById('papers-all'),none=document.getElementById('papers-none');
  if(all)all.addEventListener('click',function(){
    document.querySelectorAll('input[name=paper]').forEach(function(c){c.checked=true});
  });
  if(none)none.addEventListener('click',function(){
    document.querySelectorAll('input[name=paper]').forEach(function(c){c.checked=false});
  });

  /* Watchlist tags — newspaper: click filters; YouTube: click toggles search selection */
  var pageModule=(location.pathname||'').indexOf('/youtube')===0?'youtube':'newspaper';
  var q=document.getElementById('q');
  var form=document.getElementById('search')||document.getElementById('yt-search');
  // Delegated so chips added live (after an Add) work without re-binding.
  document.addEventListener('click',function(ev){
    var btn=ev.target.closest?ev.target.closest('.kw-pick'):null;
    if(!btn)return;
    if(!q||!form)return;
    var kw=btn.getAttribute('data-kw')||'';
    q.value=kw;
    document.querySelectorAll('.kw-chip,.kw-pick.kw-all').forEach(function(el){
      el.classList.remove('on');
    });
    var chip=btn.closest('.kw-chip');
    if(chip)chip.classList.add('on');else btn.classList.add('on');
    // Newspaper: filter the stored results in place, like the YouTube page —
    // no full reload. Other modules keep the plain form submit.
    if(pageModule==='newspaper'){autoShowNewspaper(kw, btn.getAttribute('data-kw-id'));}
    else{form.requestSubmit?form.requestSubmit():form.submit();}
  });
  var autoShowTimer=null;
  function autoShowNewspaper(kw, kwId){
    // Live-only: clicking a saved keyword fills the search box with that word,
    // ready for the next live search. No DB fetch, no scan.
    var p=new URLSearchParams(location.search);
    if(kw){p.set('q',kw);}else{p.delete('q');}
    p.set('module','newspaper');
    history.replaceState(null,'','?'+p.toString());
    var draft=document.getElementById('kw-draft-text');
    if(draft)draft.value=kw||'';
  }
  function autoShowYoutube(){
    // Live-only: selection is tracked by the .sel class on chips; the live
    // search reads it when the button is clicked. No DB fetch here.
  }
  if(pageModule==='youtube'){
    document.addEventListener('click',function(e){
      var btn=e.target.closest?e.target.closest('.kw-toggle'):null;
      if(!btn)return;
      e.preventDefault();
      var chip=btn.closest('.kw-chip');
      if(!chip)return;
      chip.classList.toggle('sel');
      autoShowYoutube();
    });
    var selAll=document.getElementById('kw-sel-all');
    var selNone=document.getElementById('kw-sel-none');
    if(selAll)selAll.addEventListener('click',function(){
      document.querySelectorAll('.kw-chip[data-kw-id]').forEach(function(c){c.classList.add('sel')});
      if(pageModule==='youtube')autoShowYoutube();
    });
    if(selNone)selNone.addEventListener('click',function(){
      document.querySelectorAll('.kw-chip[data-kw-id]').forEach(function(c){c.classList.remove('sel')});
      if(pageModule==='youtube')autoShowYoutube();
    });
  }

  /* Live streams — its own flow: list what's live, pick a window, transcribe
     and match just that. Results render only inside this panel. */
  (function(){
    var list=document.getElementById('yt-live-list');
    if(!list)return;   // not the YouTube page
    var winBox=document.getElementById('yt-live-window'),
        fromI=document.getElementById('yt-live-from'),
        toI=document.getElementById('yt-live-to'),
        runBtn=document.getElementById('yt-live-run'),
        heading=document.getElementById('yt-live-heading'),
        statusEl=document.getElementById('yt-live-status'),
        resultsEl=document.getElementById('yt-live-results');
    var picked=null,pollT=null,probeAt=0,headSecs=0,loadedMode=null;
    // 'Live stream' and 'Live ticker' tabs share this picker; only the run
    // endpoint, labels and source list differ.
    var mode={kind:'audio',endpoint:'/api/youtube/live/run',maxMin:30};
    var fromLbl=document.getElementById('yt-live-from-label'),
        toLbl=document.getElementById('yt-live-to-label'),
        spanEl=document.getElementById('yt-live-span'),
        winInfo=document.getElementById('yt-live-window-info');
    function esc(s){return (s||'').replace(/[<>&"]/g,function(c){return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c];});}
    function fmt(s){s=Math.max(0,Math.floor(s||0));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
      return h?(h+':'+String(m).padStart(2,'0')+':'+String(x).padStart(2,'0')):(m+':'+String(x).padStart(2,'0'));}
    function wallDate(off){
      // slider offset -> real wall-clock moment: the stream head was "now" at
      // probe time, so offset o happened (headSecs-o) seconds before that.
      return new Date(probeAt-(headSecs-off)*1000);
    }
    function wallFmt(off){
      return wallDate(off).toLocaleString('en-GB',{timeZone:'Asia/Karachi',
        day:'2-digit',month:'short',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true});
    }
    function syncLabels(){
      var a=parseInt(fromI.value,10),b=parseInt(toI.value,10);
      var rec=picked&&picked.kind==='recorded';
      if(fromLbl)fromLbl.textContent=rec?(fmt(a)+' into the recording'):(wallFmt(a)+'  ('+fmt(a)+' into stream)');
      if(toLbl)toLbl.textContent=rec?(fmt(b)+' into the recording'):(wallFmt(b)+'  ('+fmt(b)+' into stream)');
      if(winInfo){
        var w=b-a,maxW=mode.maxMin*60;
        winInfo.textContent=w>0
          ? ('Selected window: '+fmt(w)+(w>maxW?(' — too long, '+mode.maxMin+':00 is the max'):''))
          : 'The To handle must be after the From handle.';
        winInfo.style.color=(w<=0||w>maxW)?'var(--warn)':'';
      }
    }
    var dateBar=document.getElementById('yt-live-datebar'),
        dateInput=document.getElementById('yt-live-date');
    if(dateInput&&!dateInput.value){var _n=new Date();
      dateInput.value=new Date(_n.getTime()-_n.getTimezoneOffset()*60000).toISOString().slice(0,10);}
    function setMode(kind){
      if(kind==='ticker'){
        mode={kind:'ticker',endpoint:'/api/youtube/live/ticker',maxMin:15};
        if(heading)heading.textContent='Live ticker — read the on-screen Urdu ticker';
        runBtn.textContent='Read Urdu ticker & match';
      }else{
        mode={kind:'audio',endpoint:'/api/youtube/live/run',maxMin:30};
        if(heading)heading.textContent='Live stream — read audio';
        runBtn.textContent='Transcribe audio & match';
      }
      if(dateBar)dateBar.style.display=(kind==='ticker')?'':'none';
      resultsEl.innerHTML='';statusEl.textContent='';winBox.style.display='none';runBtn.style.display='none';
    }
    // Called by the tab switcher when the Live stream / Live ticker tab opens.
    window.__ytLiveActivate=function(kind){
      setMode(kind);
      if(loadedMode!==kind){loadedMode=kind;load();}
    };
    if(dateInput)dateInput.addEventListener('change',load);
    function renderList(items){
      if(!items.length){
        list.innerHTML='<div class="empty" style="padding:.8rem">'
          +(mode.kind==='ticker'?'No live streams (and no recorded videos) for that date.':'No live streams on your channels right now.')+'</div>';
        return;
      }
      list.innerHTML=items.map(function(s,i){
        var meta = s.kind==='recorded'
          ? '<span class="hint">recorded · '+esc(s.slot||'')+(s.duration_seconds?(' · '+fmt(s.duration_seconds)+' long'):'')+'</span>'
          : '<span class="hint">🔴 LIVE · '+fmt(s.elapsed_seconds)+(s.viewers?(' · '+esc(String(s.viewers))+' watching'):'')+'</span>';
        return '<label class="live-pick"><input type="radio" name="yt-live-pick" value="'+i+'">'
          +'<b>'+esc(s.channel)+'</b> — '+esc((s.title||'').slice(0,72))+' '+meta+'</label>';
      }).join('');
      list.querySelectorAll('input[name=yt-live-pick]').forEach(function(r){
        r.addEventListener('change',function(){onPick(items[parseInt(r.value,10)]);});
      });
    }
    function onPick(item){
      picked=item;resultsEl.innerHTML='';winBox.style.display='none';runBtn.style.display='none';
      if(item.kind==='recorded'){
        var dur=item.duration_seconds||0;
        if(!dur){statusEl.textContent='This recording has no known length yet — pick another.';return;}
        headSecs=dur;probeAt=0;statusEl.textContent='';
        if(spanEl)spanEl.innerHTML='Recording length: <b>'+fmt(dur)+'</b> · slide to choose the part to read.';
        fromI.min=0;fromI.max=dur;fromI.step=5;fromI.value=0;
        toI.min=0;toI.max=dur;toI.step=5;toI.value=Math.min(dur,600);
        syncLabels();winBox.style.display='block';runBtn.style.display='inline-flex';
        return;
      }
      // Live: probe the true DVR length (streams restart, Data API start can be stale).
      statusEl.innerHTML='<span class="spin"></span> checking how much of the stream is rewindable…';
      fetch('/api/youtube/live/timeline/'+encodeURIComponent(item.video_id))
      .then(function(r2){return r2.json().then(function(j){return {ok:r2.ok,j:j};});})
      .then(function(x){
        if(!x.ok){statusEl.textContent=esc(x.j.detail||'Could not read the stream timeline.');return;}
        headSecs=x.j.head_seconds||0;probeAt=Date.now();statusEl.textContent='';
        if(spanEl)spanEl.innerHTML='Stream timeline: <b>'+esc(wallFmt(0))+'</b> → <b>'+esc(wallFmt(headSecs))+'</b> (Pakistan time) · slide both handles.';
        fromI.min=0;fromI.max=headSecs;fromI.step=5;fromI.value=Math.max(0,headSecs-600);
        toI.min=0;toI.max=headSecs;toI.step=5;toI.value=headSecs;
        syncLabels();winBox.style.display='block';runBtn.style.display='inline-flex';
      }).catch(function(){statusEl.textContent='Could not read the stream timeline.';});
    }
    function load(){
      picked=null;winBox.style.display='none';runBtn.style.display='none';resultsEl.innerHTML='';
      list.innerHTML='<span class="hint">Checking…</span>';
      if(mode.kind==='ticker'){
        var d=(dateInput&&dateInput.value)||'';
        fetch('/api/youtube/ticker-sources'+(d?('?date='+encodeURIComponent(d)):''))
        .then(function(r){return r.json();}).then(function(j){
          renderList([].concat(j.live||[],j.bulletins||[]));
        }).catch(function(){list.innerHTML='<div class="empty" style="padding:.8rem">Could not load sources.</div>';});
      }else{
        fetch('/api/youtube/live').then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
        .then(function(x){
          if(!x.ok){list.innerHTML='<div class="empty" style="padding:.8rem">'+esc(x.j.detail||'Could not check.')+'</div>';return;}
          renderList(((x.j&&x.j.streams)||[]).map(function(s){s.kind='live';return s;}));
        }).catch(function(){list.innerHTML='<div class="empty" style="padding:.8rem">Could not check live streams.</div>';});
      }
    }
    // Sliding one handle past the other drags the other along, so the window
    // always stays valid; labels re-render with real dates as you slide.
    if(fromI)fromI.addEventListener('input',function(){
      if(parseInt(fromI.value,10)>=parseInt(toI.value,10)){toI.value=Math.min(parseInt(fromI.value,10)+5,parseInt(toI.max,10));}
      syncLabels();
    });
    if(toI)toI.addEventListener('input',function(){
      if(parseInt(toI.value,10)<=parseInt(fromI.value,10)){fromI.value=Math.max(parseInt(toI.value,10)-5,0);}
      syncLabels();
    });
    function launch(){
      if(!picked){statusEl.textContent='Pick a source first.';return;}
      var a=parseInt(fromI.value,10),b=parseInt(toI.value,10);
      if(isNaN(a)||isNaN(b)||b<=a){statusEl.textContent='Slide the handles to choose a window first.';return;}
      if(b-a>mode.maxMin*60){statusEl.textContent='That window is over '+mode.maxMin+' minutes — shrink it.';return;}
      resultsEl.innerHTML='';statusEl.textContent='Starting…';
      runBtn.disabled=true;
      fetch(mode.endpoint,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({video_id:picked.video_id,start_seconds:a,end_seconds:b,
          is_live:(picked.kind!=='recorded')})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(x){
        if(!x.ok){statusEl.textContent=esc(x.j.detail||'Failed to start.');runBtn.disabled=false;return;}
        poll(x.j.job);
      }).catch(function(){statusEl.textContent='Failed to start.';runBtn.disabled=false;});
    }
    if(runBtn)runBtn.addEventListener('click',launch);
    function poll(id){
      fetch('/api/youtube/live/jobs/'+encodeURIComponent(id))
      .then(function(r){return r.json();}).then(function(j){
        if(j.state==='done'){runBtn.disabled=false;statusEl.textContent='';render(j);return;}
        if(j.state==='error'){runBtn.disabled=false;statusEl.textContent=esc(j.error||'Failed.');return;}
        statusEl.innerHTML='<span class="spin"></span> '+esc(j.state)+(j.detail?(' — '+esc(j.detail)):'');
        pollT=setTimeout(function(){poll(id);},2000);
      }).catch(function(){pollT=setTimeout(function(){poll(id);},3000);});
    }
    function render(j){
      var m=j.matches||{},kws=Object.keys(m),ticker=j.ticker||[],html='';
      function cut(src){return src?('<img class="ticker-cut" loading="lazy" src="'+esc(src)+'" alt="ticker cutout">'):'';}
      if(kws.length){
        html+='<div class="cap" style="margin:.2rem 0 .3rem">Keyword matches — each with its ticker cutout to verify</div><div class="hits">'+kws.map(function(k){
          return '<div class="hitrow"><span class="hitkw">'+esc(k)+'</span><div class="hitcuts">'+m[k].map(function(h){
            return '<div class="hitcut"><a class="jump" target="_blank" rel="noopener" href="'+esc(h.url)+'">'+fmt(h.start)+'</a>'+cut(h.img)+'</div>';
          }).join('')+'</div></div>';
        }).join('')+'</div>';
      }
      if(ticker.length){
        // The whole ticker read, earliest first; each line shows its own cutout.
        html+='<div class="cap" style="margin:.7rem 0 .3rem">Ticker — '+ticker.length+' lines, earliest first</div>'
          +'<div class="ticker-list">'+ticker.map(function(row){
            return '<div class="ticker-row"><a class="jump" target="_blank" rel="noopener" href="'+esc(row.url)+'">'+fmt(row.start)+'</a>'
              +'<div class="ticker-body">'+cut(row.img)+'<span dir="rtl">'+esc(row.text)+'</span></div></div>';
          }).join('')+'</div>';
      }
      if(!html){
        html='<div class="empty" style="padding:.8rem">Nothing readable in that window'
          +(kws.length?'':' — no watchlist keyword matched either')+'.</div>';
      }
      resultsEl.innerHTML=html;
    }
  })();

  /* YouTube tabs: Uploads | Live stream | Live ticker. Keywords sit
     above the tabs and are shared across all three (same watchlist). */
  (function(){
    var tabs=document.querySelectorAll('.yt-tab');
    if(!tabs.length)return;
    var panels={
      search:document.getElementById('yt-tab-search'),
      live:document.getElementById('yt-tab-live'),
      ticker:document.getElementById('yt-tab-live')  // shares the live panel
    };
    function show(name){
      tabs.forEach(function(t){t.classList.toggle('on',t.getAttribute('data-tab')===name);});
      document.getElementById('yt-tab-search').style.display=(name==='search')?'':'none';
      document.getElementById('yt-tab-live').style.display=(name==='search')?'none':'';
      if(name==='live'&&window.__ytLiveActivate)window.__ytLiveActivate('audio');
      if(name==='ticker'&&window.__ytLiveActivate)window.__ytLiveActivate('ticker');
    }
    tabs.forEach(function(t){
      t.addEventListener('click',function(){show(t.getAttribute('data-tab'));});
    });
  })();

  /* ---- Loading bar ---- */
  var loadbar=document.createElement('div');loadbar.id='loadbar';document.body.appendChild(loadbar);
  function showLoad(){loadbar.classList.add('on');}
  function hideLoad(){loadbar.classList.remove('on');}

  /* ---- Styled confirm dialog (delete keyword) ---- */
  var cmodal=document.createElement('div');cmodal.id='confirm-modal';
  cmodal.innerHTML='<div class="box"><h3 id="cm-title">Please confirm</h3>'
    +'<p id="cm-text"></p><div class="row-btns">'
    +'<button type="button" class="ghost" id="cm-cancel">Cancel</button>'
    +'<button type="button" class="danger" id="cm-ok">Remove</button></div></div>';
  document.body.appendChild(cmodal);
  var cmTitle=cmodal.querySelector('#cm-title'),cmText=cmodal.querySelector('#cm-text'),
      cmOk=cmodal.querySelector('#cm-ok'),cmCancel=cmodal.querySelector('#cm-cancel'),cmAction=null;
  function closeConfirm(){cmodal.classList.remove('open');cmAction=null;}
  // Generic styled confirm reused by keyword / newspaper / channel removal.
  function openConfirm(opts){
    cmTitle.textContent=opts.title||'Please confirm';
    cmText.innerHTML=opts.html||'';
    cmOk.textContent=opts.okLabel||'Remove';
    cmAction=opts.onOk||null;
    cmodal.classList.add('open');
  }
  window.openConfirm=openConfirm;
  cmCancel.addEventListener('click',closeConfirm);
  cmodal.addEventListener('click',function(e){if(e.target===cmodal)closeConfirm();});
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&cmodal.classList.contains('open'))closeConfirm();});
  cmOk.addEventListener('click',function(){var fn=cmAction;closeConfirm();if(fn)fn();});
  function bold(name){var b=document.createElement('b');b.textContent=name||'';return b.outerHTML;}

  // Keyword chip × — styled confirm, AJAX delete, instant DOM removal.
  document.addEventListener('click',function(e){
    var x=e.target.closest?e.target.closest('[data-del-id]'):null;
    if(!x)return;
    e.preventDefault();
    var chip=x.closest('.kw-chip'), id=x.getAttribute('data-del-id');
    openConfirm({title:'Remove keyword',okLabel:'Remove',
      html:'Remove '+bold(x.getAttribute('data-del-text')||'this keyword')+' from the watchlist?',
      onOk:function(){
        if(chip){chip.style.opacity='.4';chip.style.pointerEvents='none';}
        fetch('/ui/keywords/'+encodeURIComponent(id)+'/delete',
          {method:'POST',headers:{'Accept':'application/json'}})
          .then(function(r){return r.json();})
          .then(function(){
            var wasOn=chip&&chip.classList.contains('on');
            if(chip)chip.remove();
            var tags=document.querySelector('.kw-tags');
            if(tags&&!tags.querySelector('.kw-chip'))
              tags.innerHTML='<span class="hint">No keywords yet — add some above.</span>';
            if(wasOn&&pageModule==='newspaper'&&q)q.value='';
          })
          .catch(function(){if(chip){chip.style.opacity='';chip.style.pointerEvents='';}});
      }});
  });

  // Newspaper × — styled confirm, AJAX remove, instant DOM removal (no reload).
  document.addEventListener('click',function(e){
    var x=e.target.closest?e.target.closest('[data-paper-del]'):null;
    if(!x)return;
    e.preventDefault();
    var name=x.getAttribute('data-paper-del'), item=x.closest('.paper-item');
    openConfirm({title:'Remove newspaper',okLabel:'Remove',
      html:'Remove '+bold(name)+' from the list? It will stop being searched.',
      onOk:function(){
        if(item)item.style.opacity='.4';
        var b=new URLSearchParams();b.append('name',name);
        fetch('/ui/papers/delete',{method:'POST',
          headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},
          body:b.toString()})
          .then(function(){if(item)item.remove();})
          .catch(function(){if(item)item.style.opacity='';});
      }});
  });

  // Channel × — styled confirm, AJAX remove, instant DOM removal (no reload).
  document.addEventListener('click',function(e){
    var x=e.target.closest?e.target.closest('[data-ch-del]'):null;
    if(!x)return;
    e.preventDefault();
    var id=x.getAttribute('data-ch-del'), name=x.getAttribute('data-ch-name')||'this channel',
        tag=x.closest('.ch-tag');
    openConfirm({title:'Remove channel',okLabel:'Remove',
      html:'Remove '+bold(name)+' from your channels?',
      onOk:function(){
        if(tag)tag.style.opacity='.4';
        fetch('/ui/youtube/channels/'+encodeURIComponent(id)+'/delete',
          {method:'POST',headers:{'Accept':'application/json'}})
          .then(function(){
            if(tag)tag.remove();
            var box=document.querySelector('.ch-tags');
            if(box&&!box.querySelector('.ch-tag'))
              box.innerHTML='<span class="hint">No channels yet.</span>';
          })
          .catch(function(){if(tag)tag.style.opacity='';});
      }});
  });

  /* ~1s branded wipe between Newspaper and YouTube. The overlay slides up to
     cover the old page, we navigate, then it slides the rest of the way up on
     the new page to reveal it — one continuous motion across the load. */
  var reduceMotion=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var pt=document.createElement('div');pt.id='pt';
  pt.innerHTML='<div class="pt-mark">\\u25ce</div><div class="pt-label"></div>'
    +'<div class="pt-dots">loading</div>';
  document.body.appendChild(pt);
  var ptLabel=pt.querySelector('.pt-label');
  // Arriving from a transition? cover instantly, then reveal.
  try{
    var dest=sessionStorage.getItem('pt-dest');
    if(dest && !reduceMotion){
      sessionStorage.removeItem('pt-dest');
      ptLabel.textContent=dest;
      pt.classList.add('cover');
      // Timer, not rAF: rAF is throttled when the tab isn't compositing, which
      // would leave the overlay stuck covering the page. A timer always fires.
      setTimeout(function(){
        pt.classList.remove('cover');pt.classList.add('out');   // slide away to reveal
        setTimeout(function(){pt.classList.remove('out');},650); // then park it hidden
      },40);
    }else{
      try{sessionStorage.removeItem('pt-dest');}catch(e3){}
    }
  }catch(e){}
  document.addEventListener('click',function(e){
    var a=e.target.closest?e.target.closest('.mod-link'):null;
    if(!a||a.classList.contains('on'))return;      // ignore the current page
    var href=a.getAttribute('href');
    if(!href||e.metaKey||e.ctrlKey||e.shiftKey)return;   // let new-tab clicks through
    e.preventDefault();
    if(reduceMotion){location.href=href;return;}
    var label=(a.textContent||'').trim();
    ptLabel.textContent=label;
    try{sessionStorage.setItem('pt-dest',label);}catch(e2){}
    pt.classList.add('in');                          // slide up to cover
    setTimeout(function(){location.href=href;},500); // navigate once covered
  });

  /* The old DB poller (poll/refreshResults) is gone with the live-only
     model: nothing scans in the background, so there is nothing to poll
     for. Results arrive only from the live search below. */

  /* ---- Search live results — the ONLY thing that scrapes ---- */
  function escHtml(s){return (s||'').replace(/[<>&"]/g,function(c){
    return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c];});}
  function setResults(markup){
    var el=document.getElementById('results'); if(!el)return;
    var wrap=document.createElement('div'); wrap.innerHTML=(markup||'').trim();
    var next=wrap.firstElementChild; if(next)el.replaceWith(next);
  }
  var liveJob=null, liveTimer=null;
  function pollLive(){
    if(!liveJob)return;
    fetch('/api/live/search/'+encodeURIComponent(liveJob))
      .then(function(r){return r.json();})
      .then(function(j){
        if(j&&j.html)setResults(j.html);
        if(j&&j.status==='running'){showLoad();liveTimer=setTimeout(pollLive,1500);}
        else{hideLoad();}
      })
      .catch(function(){liveTimer=setTimeout(pollLive,2500);});
  }
  function liveSearch(){
    var body=new URLSearchParams();
    body.append('module',pageModule);
    var d=document.getElementById('date'); if(d&&d.value)body.append('date',d.value);
    if(pageModule==='youtube'){
      var sel=document.querySelectorAll('.kw-chip.sel[data-kw-id]');
      if(!sel.length){
        setResults('<section class="results" id="results"><div class="results-head">'
          +'<h2>Live results</h2></div><div class="empty">Select at least one keyword '
          +'(click the chips) — a live YouTube search downloads and transcribes videos.</div></section>');
        return;
      }
      if(!confirm('Live YouTube search downloads and transcribes recent videos on your '
        +'channels. This uses paid transcription (Groq) and can take a few minutes. Continue?'))return;
      sel.forEach(function(c){
        var id=c.getAttribute('data-kw-id'); if(id)body.append('kw_id',id);
      });
    }else{
      // Search the word typed in the box directly (no need to Add it first);
      // fall back to a clicked watchlist chip, else the whole watchlist.
      var draft=document.getElementById('kw-draft-text');
      var word=(((draft&&draft.value)||'').trim())||(((q&&q.value)||'').trim());
      if(word)body.append('q',word);
      document.querySelectorAll('input[name=paper]:checked').forEach(function(c){
        body.append('paper',c.value);
      });
    }
    if(liveJob){fetch('/api/live/search/'+encodeURIComponent(liveJob)+'/cancel',{method:'POST'}).catch(function(){});}
    clearTimeout(liveTimer);
    showLoad();
    setResults('<section class="results" id="results"><div class="results-head">'
      +'<h2>Live results <span class="spin"></span></h2></div>'
      +'<div class="empty loading"><span class="spin"></span> Starting live search…</div></section>');
    fetch('/api/live/search',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},
      body:body.toString()})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        if(!res.ok||!res.j||!res.j.job){
          hideLoad();
          setResults('<section class="results" id="results"><div class="results-head">'
            +'<h2>Live results</h2></div><div class="empty">'
            +escHtml((res.j&&res.j.error)||'Could not start the live search.')+'</div></section>');
          return;
        }
        liveJob=res.j.job; pollLive();
      })
      .catch(function(){hideLoad();});
  }
  var liveBtn=document.getElementById('live-search-btn');
  if(liveBtn)liveBtn.addEventListener('click',liveSearch);

  /* One "Add" button: type a keyword (or a comma list), Add — it's saved and
     matched against stored data straight away. */
  (function(){
    var input=document.getElementById('kw-draft-text'),
        lang=document.getElementById('kw-draft-lang'),
        cfg=document.getElementById('kw-confirm'),
        addBtn=document.getElementById('kw-add-btn');
    var moduleField=cfg?cfg.querySelector('input[name=module]'):null;
    var addModule=moduleField&&moduleField.value?moduleField.value:pageModule;
    function esc(s){return (s||'').replace(/[<>&"]/g,function(c){
      return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c];});}
    function chipHtml(id,text){
      var t=esc(text);
      var del='<button type="button" class="kw-x" data-del-id="'+id+'" data-del-text="'+t
        +'" title="Remove" aria-label="Remove">\\u00d7</button>';
      if(addModule==='youtube'){
        return '<span class="kw-chip" data-kw-id="'+id+'">'
          +'<button type="button" class="kw-toggle" data-kw="'+t+'">'+t+'</button>'+del+'</span>';
      }
      return '<span class="kw-chip" data-kw-id="'+id+'">'
        +'<button type="button" class="kw-pick" data-kw-id="'+id+'" data-kw="'+t+'">'+t+'</button>'
        +del+'</span>';
    }
    function insertChips(list){
      var tags=document.querySelector('.kw-tags');
      if(!tags)return;
      var hint=tags.querySelector('.hint');if(hint)hint.remove();
      list.forEach(function(c){
        if(tags.querySelector('[data-kw-id="'+c.id+'"]'))return;  // already shown
        var tmp=document.createElement('div');tmp.innerHTML=chipHtml(c.id,c.text);
        if(tmp.firstChild)tags.appendChild(tmp.firstChild);
      });
    }
    function submitAdd(){
      if(!input)return;
      var raw=(input.value||'').trim();
      if(!raw)return;
      var body=new URLSearchParams();
      body.append('texts',raw);                       // endpoint splits on comma/newline
      body.append('language',(lang&&lang.value)||'en');
      body.append('module',addModule);
      body.append('scan','1');
      input.disabled=true;
      if(addBtn){addBtn.disabled=true;addBtn.innerHTML='<span class="spin"></span>';}
      // Add ONLY saves to the watchlist — no scan, no cost. Scraping happens
      // later, when the user clicks "Search live results".
      fetch('/ui/keywords/batch',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},
        body:body.toString()})
        .then(function(r){return r.json();})
        .then(function(j){
          input.disabled=false;input.value='';input.focus();
          if(addBtn){addBtn.disabled=false;addBtn.textContent='Add';}
          if(!j||!j.ok)return;
          insertChips(j.created||[]);
        })
        .catch(function(){
          input.disabled=false;
          if(addBtn){addBtn.disabled=false;addBtn.textContent='Add';}
        });
    }
    if(addBtn)addBtn.addEventListener('click',submitAdd);
    if(input)input.addEventListener('keydown',function(e){
      if(e.key==='Enter'){e.preventDefault();submitAdd();}
    });
  })();

  /* Add newspaper / e-paper modal */
  var modal=document.getElementById('src-modal');
  var openBtn=document.getElementById('papers-add');
  var closeBtn=document.getElementById('src-close');
  var checkBtn=document.getElementById('src-check');
  var saveBtn=document.getElementById('src-save');
  var result=document.getElementById('src-result');
  var lastProbe=null;
  function openModal(){
    if(!modal)return;
    modal.classList.add('open');
    lastProbe=null;
    if(result){result.className='';result.textContent='';result.classList.remove('show')}
    if(saveBtn)saveBtn.style.display='none';
  }
  function closeModal(){if(modal)modal.classList.remove('open')}
  if(openBtn)openBtn.addEventListener('click',openModal);
  if(closeBtn)closeBtn.addEventListener('click',closeModal);
  if(modal)modal.addEventListener('click',function(e){if(e.target===modal)closeModal()});
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&modal&&modal.classList.contains('open'))closeModal();
  });
  if(checkBtn)checkBtn.addEventListener('click',async function(){
    var kind=(document.querySelector('input[name=src-kind]:checked')||{}).value;
    var url=(document.getElementById('src-url')||{}).value||'';
    var name=(document.getElementById('src-name')||{}).value||'';
    checkBtn.disabled=true;checkBtn.innerHTML='<span class="spin"></span> Checking…';
    try{
      var r=await fetch('/api/probe-source',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({kind:kind,url:url,name:name})}).then(function(x){return x.json()});
      lastProbe=r;
      if(result){
        result.textContent=r.summary||'No result';
        result.className='show '+(r.ok?'ok':'bad');
      }
      if(saveBtn)saveBtn.style.display=r.ok?'inline-flex':'none';
    }catch(err){
      if(result){result.textContent='Check failed — try again.';result.className='show bad'}
      if(saveBtn)saveBtn.style.display='none';
    }
    checkBtn.disabled=false;checkBtn.textContent='Check link';
  });
  if(saveBtn)saveBtn.addEventListener('click',async function(){
    if(!lastProbe||!lastProbe.ok)return;
    var name=(document.getElementById('src-name')||{}).value||'';
    var kind=(document.querySelector('input[name=src-kind]:checked')||{}).value;
    saveBtn.disabled=true;
    try{
      var r=await fetch('/api/custom-sources',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:name,kind:kind,url:lastProbe.url||(document.getElementById('src-url')||{}).value,
          summary:lastProbe.summary,detail:lastProbe.detail||{}})}).then(function(x){return x.json()});
      if(r.ok){
        // Insert the paper into the picker in place — no reload.
        var papers=document.querySelector('.papers');
        if(papers && !papers.querySelector('input[name=paper][value="'+name.replace(/"/g,'\\\\"')+'"]')){
          var span=document.createElement('span');span.className='paper-item';
          span.innerHTML='<label><input type="checkbox" name="paper" value="'+escHtml(name)+'" checked>'
            +escHtml(name)+'</label>'
            +'<button type="button" class="src-x" data-paper-del="'+escHtml(name)+'" '
            +'title="Remove this paper">\\u00d7</button>';
          papers.appendChild(span);
        }
        closeModal();
      }
      else if(result){result.textContent=r.summary||'Could not save.';result.className='show bad'}
    }catch(err){
      if(result){result.textContent='Save failed.';result.className='show bad'}
    }
    saveBtn.disabled=false;
  });

  /* Add YouTube channel modal */
  (function(){
    if(pageModule!=='youtube')return;
    var modal=document.getElementById('yt-ch-modal');
    var openBtn=document.getElementById('yt-ch-add');
    var closeBtn=document.getElementById('yt-ch-close');
    var checkBtn=document.getElementById('yt-ch-check');
    var saveBtn=document.getElementById('yt-ch-save');
    var result=document.getElementById('yt-ch-result');
    var lastProbe=null;
    function openModal(){
      if(!modal)return;
      modal.classList.add('open');
      lastProbe=null;
      if(result){result.className='';result.textContent='';result.classList.remove('show')}
      if(saveBtn)saveBtn.style.display='none';
    }
    function closeModal(){if(modal)modal.classList.remove('open')}
    if(openBtn)openBtn.addEventListener('click',openModal);
    if(closeBtn)closeBtn.addEventListener('click',closeModal);
    if(modal)modal.addEventListener('click',function(e){if(e.target===modal)closeModal()});
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape'&&modal&&modal.classList.contains('open'))closeModal();
    });
    if(checkBtn)checkBtn.addEventListener('click',async function(){
      var url=(document.getElementById('yt-ch-url')||{}).value||'';
      checkBtn.disabled=true;checkBtn.innerHTML='<span class="spin"></span> Checking…';
      try{
        var r=await fetch('/api/probe-youtube-channel',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({url:url})}).then(function(x){return x.json()});
        lastProbe=r;
        if(result){
          result.textContent=r.summary||'No result';
          result.className='show '+(r.ok?'ok':'bad');
        }
        if(saveBtn)saveBtn.style.display=r.ok?'inline-flex':'none';
      }catch(err){
        if(result){result.textContent='Check failed — try again.';result.className='show bad'}
        if(saveBtn)saveBtn.style.display='none';
      }
      checkBtn.disabled=false;checkBtn.textContent='Check channel';
    });
    if(saveBtn)saveBtn.addEventListener('click',async function(){
      if(!lastProbe||!lastProbe.ok||!lastProbe.detail)return;
      saveBtn.disabled=true;
      try{
        var body=Object.assign({},lastProbe.detail,{slots:[]});
        var r=await fetch('/api/youtube/channels',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(body)}).then(function(x){return x.json()});
        if(r.ok){
          // Insert the channel tag in place — no reload.
          var box=document.querySelector('.ch-tags');
          if(box){
            var hint=box.querySelector('.hint');if(hint)hint.remove();
            if(r.id && !box.querySelector('[data-ch-del="'+r.id+'"]')){
              var span=document.createElement('span');span.className='ch-tag';
              span.innerHTML=escHtml(r.name||'')
                +'<button type="button" class="chip-x" data-ch-del="'+r.id+'" '
                +'data-ch-name="'+escHtml(r.name||'')+'" title="Remove channel">\\u00d7</button>';
              box.appendChild(span);
            }
          }
          closeModal();
        }
        else if(result){result.textContent=r.summary||'Could not add channel.';result.className='show bad'}
      }catch(err){
        if(result){result.textContent='Save failed.';result.className='show bad'}
      }
      saveBtn.disabled=false;
    });
  })();
})();
"""


def _shell(title: str, body: str, *, module: str = "newspaper") -> str:
    # Live-only model: no background scans, so no status badge and no "Scan now".
    nav = (
        '<nav class="mod-nav">'
        f'<a href="/" class="mod-link {"on" if module == "newspaper" else ""}">Newspaper</a>'
        f'<a href="/youtube" class="mod-link {"on" if module == "youtube" else ""}">YouTube</a>'
        '</nav>'
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>{_FONTS}<style>{_CSS}</style></head><body>
<div class="top"><div class="top-inner">
  <a class="brand" href="/"><span class="mark">◎</span>
    <span><b>Media Monitor</b><small>Press desk</small></span></a>
  {nav}
  <span class="spacer"></span>
</div></div>
<main class="page" id="page"><div class="wrap">{body}</div></main>
<script>{_JS}</script>
</body></html>"""


# ==========================================================================
# Shared renderers
# ==========================================================================
def _media_url(abs_path: str | None) -> str | None:
    """Map a stored filesystem path to /media/... for StaticFiles.

    DB rows often keep host-absolute paths (e.g. Railway `/data/storage/epaper/...`).
    Those still resolve as long as the tail under `storage/` matches STORAGE_DIR.
    """
    if not abs_path:
        return None
    raw = str(abs_path).replace("\\", "/")
    storage = settings.storage_dir.resolve()
    try:
        rel = Path(abs_path).resolve().relative_to(storage)
        return "/media/" + str(rel).replace("\\", "/")
    except Exception:
        pass
    # Cross-host / odd mounts: keep everything after the last "/storage/"
    marker = "/storage/"
    idx = raw.lower().rfind(marker)
    if idx >= 0:
        rel = raw[idx + len(marker):].lstrip("/")
        if rel:
            return "/media/" + rel
    # Already relative to the storage root
    if not Path(raw).is_absolute() and ":" not in raw[:3]:
        return "/media/" + raw.lstrip("./")
    return None


def _highlight_excerpt(text: str | None, keywords: list[str]) -> str:
    if not text:
        return ""
    import re as _re

    from app.core.keywords import normalize

    text = _re.sub(r"[#*`_]{1,}", " ", text)
    text = _re.sub(r"\s{2,}", " ", text).strip()
    variants = set()
    for kw in keywords or []:
        kw = (kw or "").strip()
        if not kw:
            continue
        variants.add(kw)
        for lang in ("en", "ur"):
            nk = normalize(kw, lang)
            if nk:
                variants.add(nk)
    if not variants:
        return html.escape(text)
    pat = _re.compile("(" + "|".join(_re.escape(v) for v in
                      sorted(variants, key=len, reverse=True)) + ")", _re.IGNORECASE)
    out, i = [], 0
    for mt in pat.finditer(text):
        out.append(html.escape(text[i:mt.start()]))
        out.append(f"<mark>{html.escape(mt.group(0))}</mark>")
        i = mt.end()
    out.append(html.escape(text[i:]))
    return "".join(out)


def _youtube_watch_url(m: Mention, seconds: int | None) -> str:
    from app.youtube.discovery import deep_link

    vid = (m.external_id or "").strip()
    if not vid:
        return m.url or "#"
    if seconds is None:
        return m.url or deep_link(vid, None)
    return deep_link(vid, int(seconds))


def _detection_card(m: Mention, highlight_keywords: list[str] | None = None,
                    scanning: bool = False,
                    keyword_langs: dict[str, str] | None = None) -> str:
    hl = list(highlight_keywords or [])
    keyword_path = None
    if len(hl) == 1:
        needle = hl[0].casefold()
        if m.module == "youtube":
            from app.youtube.matcher import verified_json_hits

            lang = (keyword_langs or {}).get(hl[0], "ur")
            for hit in verified_json_hits(hl[0], lang, (m.keyword_hits or {}).get(hl[0]) or []):
                shot = hit.get("screenshot")
                if shot and _storage_file(shot):
                    keyword_path = shot
                    break
        if keyword_path is None:
            keyword_path = next(
                (
                    path for label, path in (m.keyword_media or {}).items()
                    if (label or "").casefold() == needle
                ),
                None,
            )
        if keyword_path and not _storage_file(keyword_path):
            keyword_path = None
    # Legacy multi-keyword e-paper rows may predate keyword_media. Their one
    # shared clipping could belong to a different article on the same page, so
    # prefer the full page until a rescan creates a verified per-keyword clip.
    safe_fallback = m.screenshot_path
    if (
        m.module == "epaper"
        and len(m.matched_keywords or []) > 1
        and len(hl) == 1
        and not keyword_path
    ):
        safe_fallback = m.full_screenshot_path
    thumb = (
        _media_url(keyword_path)
        or _media_url(safe_fallback)
        or _media_url(m.full_screenshot_path)
    )
    full = _media_url(m.full_screenshot_path) or thumb
    badge = ""
    if m.module == "epaper" and m.section:
        pg = m.section.rsplit("page", 1)[-1].strip()
        if full and thumb and full != thumb:
            badge = (f'<span class="pagebadge zoom" style="cursor:zoom-in" '
                     f'data-full="{html.escape(full)}" title="Open the full page">'
                     f'p.{html.escape(pg)} · full</span>')
        else:
            badge = f'<span class="pagebadge">p.{html.escape(pg)}</span>'
    if thumb:
        # Zoom the preview itself (cutout). Full page stays on the "p.N · full" badge.
        zoom = html.escape(thumb)
        img = (f'<div class="shot">{badge}'
               f'<img loading="lazy" class="zoom" src="{html.escape(thumb)}" '
               f'data-full="{zoom}" alt=""></div>')
    else:
        img = ('<div class="shot missing"><span class="noprev">No preview</span></div>')
    # Only highlight / tag watchlist keywords that are still active (or the filter).
    show_tags = hl
    tags = "".join(f'<span class="tag">{html.escape(k)}</span>' for k in show_tags)
    occurred = m.published_at or m.detected_at
    when = _utc(occurred).astimezone(_PKT).strftime("%d %b %Y, %H:%M") if occurred else ""
    if m.module == "epaper":
        kind = "E-Paper"
    elif m.module == "youtube":
        kind = "YouTube"
    else:
        kind = "Web"
    meta = " · ".join(x for x in [kind, m.source, m.section if m.module == "youtube" else None,
                                    m.sentiment, when] if x)
    snippet_src = (
        _youtube_snippet_for(m, hl, keyword_langs)
        if m.module == "youtube" else (m.snippet or "")
    )
    excerpt = _highlight_excerpt(snippet_src, hl)
    excerpt_html = f'<div class="excerpt">…{excerpt}…</div>' if excerpt else ""
    jump = ""
    yt_sec: int | None = None
    if m.module == "youtube":
        from app.youtube.matcher import verified_json_hits

        # Every keyword gets its own timestamps, so a video matching two terms
        # shows which term was said when instead of one ambiguous jump link.
        rows: list[str] = []
        for label in hl or (m.matched_keywords or []):
            lang = (keyword_langs or {}).get(label, "ur")
            hits = verified_json_hits(label, lang, (m.keyword_hits or {}).get(label) or [])
            if not hits:
                continue
            links = []
            for hit in hits:
                start = hit.get("start")
                if start is None:
                    continue
                s = int(start)
                if yt_sec is None:
                    yt_sec = s
                mm, ss = divmod(s, 60)
                href = (
                    _youtube_watch_url(m, s)
                    if (m.external_id or "").strip()
                    else (m.url or "#")
                )
                links.append(
                    f'<a class="jump" href="{html.escape(href)}" target="_blank" '
                    f'rel="noopener">{mm}:{ss:02d}</a>'
                )
            if links:
                rows.append(
                    f'<div class="hitrow"><span class="hitkw">{html.escape(label)}</span>'
                    f'{"".join(links)}</div>'
                )

        if rows:
            jump = f'<div class="hits">{"".join(rows)}</div>'
        elif m.deeplink_seconds is not None:
            yt_sec = int(m.deeplink_seconds)
            mm, ss = divmod(yt_sec, 60)
            href = _youtube_watch_url(m, yt_sec) if (m.external_id or "").strip() else (m.url or "#")
            jump = (f'<a class="jump" href="{html.escape(href)}" target="_blank" '
                    f'rel="noopener">Watch at {mm}:{ss:02d}</a>')
    busy = " scanning" if scanning else ""
    title_href = m.url or "#"
    if m.module == "youtube" and yt_sec is not None and (m.external_id or "").strip():
        title_href = _youtube_watch_url(m, yt_sec)
    return (f'<div class="det{busy}">{img}<div class="body">'
            f'<a class="ttl" href="{html.escape(title_href)}" target="_blank" rel="noopener">'
            f'{html.escape(m.title)}</a>{excerpt_html}{jump}'
            f'<div class="meta">{meta}</div><div>{tags}</div></div></div>')




def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if (dt and dt.tzinfo is None) else dt


def _home_redirect(extra: dict | None = None) -> RedirectResponse:
    q = urlencode({k: str(v) for k, v in (extra or {}).items() if v is not None})
    return RedirectResponse("/" + (f"?{q}" if q else ""), status_code=303)




def _storage_file(path: str | None) -> Path | None:
    """Resolve a DB media path to a local Path if the file exists."""
    if not path:
        return None
    p = Path(path)
    if p.exists():
        return p
    raw = str(path).replace("\\", "/")
    idx = raw.lower().rfind("/storage/")
    if idx >= 0:
        cand = settings.storage_dir / raw[idx + len("/storage/"):].lstrip("/")
        if cand.exists():
            return cand
    return None


def _unlink_orphan_media(db: Session, path: str | None, except_id: int) -> bool:
    """Delete a media file only if no other mention still references it."""
    if not path:
        return False
    still = db.execute(
        select(Mention.id).where(
            Mention.id != except_id,
            or_(Mention.screenshot_path == path, Mention.full_screenshot_path == path),
        ).limit(1)
    ).first()
    if still:
        return False
    # JSON keyword-specific clips are references too; SQL JSON containment is
    # not portable across SQLite/Postgres, so check the small retained set here.
    for mention in db.execute(select(Mention)).scalars():
        if mention.id == except_id:
            continue
        if path in (mention.keyword_media or {}).values():
            return False
    f = _storage_file(path)
    if not f:
        return False
    try:
        f.unlink()
        return True
    except OSError:
        return False





def _youtube_snippet_for(
    m: Mention,
    highlight_keywords: list[str],
    keyword_langs: dict[str, str] | None = None,
) -> str:
    """Excerpt for the filtered keyword, not another keyword on the same video."""
    from app.youtube.matcher import verified_json_hits

    langs = keyword_langs or {}
    for label in highlight_keywords or []:
        lang = langs.get(label, "ur")
        for hit in verified_json_hits(label, lang, (m.keyword_hits or {}).get(label) or []):
            excerpt = (hit.get("excerpt") or "").strip()
            if excerpt:
                return excerpt
    return m.snippet or ""


def detect_keyword_language(text: str, requested: str = "en") -> str:
    """Language to match a keyword under, from its own script.

    The add form defaults to English, so an Urdu term typed without touching
    the selector was stored as English — which skips Urdu letter folding and
    silently misses any transcript spelling the word with Arabic yeh or kaf.
    The script is unambiguous, so trust it over the form.
    """
    if script_language(text) == "ur":
        return "ur"
    if requested == "ur":
        # Latin text explicitly marked Urdu — folding would do nothing useful.
        return "en"
    return requested if requested in ("en", "ur") else "en"


def _upsert_watch_keywords(db: Session, texts: list[str], language: str,
                           module: str = "newspaper") -> list[Keyword]:
    """Create or reactivate several watchlist keywords in one round trip.

    Adding them one at a time cost a SELECT plus a COMMIT each, and against a
    remote database that latency is the whole of the perceived delay.
    """
    module = module if module in ("newspaper", "youtube") else "newspaper"
    cleaned = [t.strip() for t in texts if (t or "").strip()]
    if not cleaned:
        return []

    # Per keyword: one batch may mix scripts, and the script decides.
    langs = {t: detect_keyword_language(t, language) for t in cleaned}

    lowered = [t.lower() for t in cleaned]
    # Keyed on text alone: a row stored under the wrong language must be
    # repaired in place, not shadowed by a duplicate under the right one.
    existing: dict[str, list[Keyword]] = {}
    for k in db.execute(
        select(Keyword).where(
            func.lower(Keyword.text).in_(lowered),
            Keyword.module == module,
        )
    ).scalars():
        existing.setdefault((k.text or "").lower(), []).append(k)

    out: list[Keyword] = []
    for text in cleaned:
        lang = langs[text]
        rows = existing.get(text.lower()) or []
        kw = next((r for r in rows if r.language == lang), None) or (rows[0] if rows else None)
        if kw is None:
            kw = Keyword(text=text, language=lang, module=module, active=True)
            db.add(kw)
            existing.setdefault(text.lower(), []).append(kw)
        else:
            if not kw.active:
                kw.active = True
            if kw.language != lang:
                kw.language = lang  # repair a previously mis-tagged row
        out.append(kw)
    db.commit()  # one commit for the batch, not one per keyword
    return out


def _upsert_watch_keyword(db: Session, text: str, language: str,
                          module: str = "newspaper") -> Keyword | None:
    """Create or reactivate a watchlist keyword. Does not start a scan."""
    text = (text or "").strip()
    if not text:
        return None
    language = detect_keyword_language(text, language)
    module = module if module in ("newspaper", "youtube") else "newspaper"
    existing = db.execute(
        select(Keyword).where(
            func.lower(Keyword.text) == text.lower(),
            Keyword.language == language,
            Keyword.module == module,
        )
    ).scalar_one_or_none()
    if existing:
        if not existing.active:
            existing.active = True
            db.commit()
            db.refresh(existing)
        return existing
    kw = Keyword(text=text, language=language, module=module, active=True)
    db.add(kw)
    db.commit()
    db.refresh(kw)
    return kw


# ==========================================================================
# Single page — search + results
# ==========================================================================
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    today = datetime.now(_PKT).date()
    qp = request.query_params

    date_s = (qp.get("date") or today.isoformat()).strip()
    try:
        show_date = datetime.strptime(date_s, "%Y-%m-%d").date()
    except ValueError:
        show_date = today
        date_s = show_date.isoformat()

    keyword = (qp.get("q") or qp.get("kw") or "").strip()
    sources_all = db.execute(
        select(NewsSource).where(NewsSource.active.is_(True)).order_by(NewsSource.name)
    ).scalars().all()
    papers_all = [s.name for s in sources_all]
    selected = qp.getlist("paper")
    searched = "go" in qp or bool(keyword) or bool(selected) or ("date" in qp)

    if not selected and searched:
        # Explicit empty selection → no papers; first visit with no params → all
        if "paper" in qp:
            selected = []
        else:
            selected = list(papers_all)
    elif not searched:
        selected = list(papers_all)

    selected_set = set(selected)
    if sources_all:
        boxes = ""
        for s in sources_all:
            chk = " checked" if s.name in selected_set else ""
            kind = "E-paper" if s.kind == "epaper" else "Website"
            boxes += (
                f'<span class="paper-item">'
                f'<label><input type="checkbox" name="paper" value="{html.escape(s.name)}"{chk}>'
                f'{html.escape(s.name)} <small class="src-kind">{kind}</small></label>'
                f'<button type="button" class="src-x" data-paper-del="{html.escape(s.name, quote=True)}" '
                f'title="Remove this source">&times;</button>'
                f'</span>'
            )
    else:
        boxes = ('<span class="hint">No newspapers or e-papers yet — click '
                 "“+ Add more” to add one by its link.</span>")

    banner = ""
    if qp.get("paper_removed"):
        banner = (
            f'<div class="banner ok">Removed <b>{html.escape(qp.get("paper_removed"))}</b> from the '
            "newspapers. It no longer appears, is no longer scanned, and its stored results were "
            "cleared. Add it again from “+ Add more” to bring it back.</div>"
        )
    elif qp.get("removed"):
        banner = (
            f'<div class="banner ok">Hidden <b>{html.escape(qp.get("removed"))}</b> from the '
            "watchlist. Its results remain safely retained for 90 days and return if you add it again."
            "</div>"
        )
    elif qp.get("added"):
        banner = (
            f'<div class="banner ok">Added <b>{html.escape(qp.get("added"))}</b> to the watchlist '
            "(no scan yet).</div>"
        )

    # Live-only: there are no stored newspaper results to render. Ship an empty
    # #results container so the live search can swap its own section in place.
    results_html = (
        '<section class="results" id="results">'
        '<div class="empty">Pick a date, type a word (or click a saved one), '
        "choose your papers, then <b>Search live results</b>.</div></section>"
    )

    active_kws = db.execute(
        select(Keyword).where(
            Keyword.active.is_(True), Keyword.module == "newspaper"
        ).order_by(Keyword.text)
    ).scalars().all()
    kw_l = keyword.casefold()

    def _kw_chip(k: Keyword) -> str:
        on = " on" if kw_l == k.text.casefold() else ""
        return (
            f'<span class="kw-chip{on}" data-kw-id="{k.id}">'
            f'<button type="button" class="kw-pick" data-kw-id="{k.id}" '
            f'data-kw="{html.escape(k.text, quote=True)}">{html.escape(k.text)}</button>'
            f'<button type="button" class="kw-x" data-del-id="{k.id}" '
            f'data-del-text="{html.escape(k.text, quote=True)}" '
            f'title="Remove" aria-label="Remove">×</button></span>'
        )

    kw_tags = (
        f'<button type="button" class="kw-pick kw-all{" on" if not keyword else ""}" data-kw="">All</button>'
        + "".join(_kw_chip(k) for k in active_kws)
    )

    body = f"""
    {banner}
    <div class="hero">
      <h1>Find coverage</h1>
      <p>Add keywords, pick newspapers, then <b>Search live results</b> — the sites and
      e-papers are scraped live, right now. Nothing is scanned in the background and
      nothing is stored.</p>
    </div>
    <div class="panel">
      <h2>Search</h2>
      <div class="field">
        <label for="date">Date</label>
        <input form="search" type="date" id="date" name="date" value="{html.escape(date_s)}" required>
      </div>
      <div class="field">
        <label>Search a word</label>
        <div class="kw-add" id="kw-draft-row">
          <input id="kw-draft-text" type="text" placeholder="Type a word — then Search live results below" maxlength="120">
          <select id="kw-draft-lang"><option value="en">EN</option><option value="ur">UR</option></select>
          <button type="button" id="kw-add-btn">Save to watchlist</button>
        </div>
        <form id="kw-confirm" method="post" action="/ui/keywords/batch" style="display:none">
          <input type="hidden" name="texts" id="kw-pending-texts" value="">
          <input type="hidden" name="language" id="kw-pending-lang" value="en">
          <input type="hidden" name="scan" value="1">
        </form>
        <p class="hint" style="margin-top:.45rem">Type a word and hit <b>Search live results</b> to search it right now — nothing is saved. Or <b>Save to watchlist</b> to keep it for later (click a saved word to reuse it).</p>
        <div class="kw-bar">
          <div class="cap">Watchlist · click to search · × remove</div>
          <div class="kw-tags">{kw_tags or '<span class="hint">No keywords yet — add some above.</span>'}</div>
        </div>
      </div>
      <form method="get" action="/" id="search">
        <input type="hidden" name="q" id="q" value="{html.escape(keyword)}">
        <input type="hidden" name="go" value="1">
        <div class="field">
          <label>Newspapers</label>
          <div class="paper-tools">
            <button type="button" class="ghost" id="papers-all">Select all</button>
            <button type="button" class="ghost" id="papers-none">Clear</button>
            <button type="button" class="ghost" id="papers-add">+ Add more</button>
          </div>
          <div class="papers">{boxes}</div>
        </div>
        <div class="actions">
          <button type="button" id="live-search-btn">Search live results</button>
          <a class="btn ghost" href="/">Reset</a>
        </div>
        <p class="hint">Scrapes the selected newspapers and their e-papers live, on demand. Results are shown here and never saved.</p>
      </form>
    </div>
    <div id="src-modal" role="dialog" aria-modal="true" aria-labelledby="src-title">
      <div class="box">
        <h3 id="src-title">Add a publication</h3>
        <p class="sub">We check the link and tell you quickly whether it looks usable.</p>
        <div class="kinds">
          <label><input type="radio" name="src-kind" value="newspaper" checked> Newspaper</label>
          <label><input type="radio" name="src-kind" value="epaper"> E-Paper</label>
        </div>
        <label class="cap" for="src-name" style="display:block;margin-bottom:.35rem">Display name</label>
        <input type="text" id="src-name" placeholder="e.g. Geo News" maxlength="80">
        <label class="cap" for="src-url" style="display:block;margin-bottom:.35rem">Link</label>
        <input type="url" id="src-url" placeholder="https://…">
        <div id="src-result"></div>
        <div class="row-btns">
          <button type="button" id="src-check">Check link</button>
          <button type="button" id="src-save" style="display:none">Save to list</button>
          <button type="button" class="ghost" id="src-close">Close</button>
        </div>
        <p class="hint" style="margin-top:.85rem">Saved names appear in the filter list. Full automatic scraping still needs a site adapter for most new papers.</p>
      </div>
    </div>
    {results_html}
    """
    return _shell("Media Monitor", body, module="newspaper")


@app.get("/youtube", response_class=HTMLResponse)
def youtube_home(request: Request, db: Session = Depends(get_db)):
    """YouTube live search workspace (separate keyword watchlist)."""
    if not settings.youtube_enabled:
        return RedirectResponse("/", status_code=303)

    today = datetime.now(_PKT).date()
    qp = request.query_params
    date_s = (qp.get("date") or today.isoformat()).strip()

    active_kws = db.execute(
        select(Keyword).where(
            Keyword.active.is_(True), Keyword.module == "youtube"
        ).order_by(Keyword.text)
    ).scalars().all()

    def _kw_chip(k: Keyword) -> str:
        return (
            f'<span class="kw-chip" data-kw-id="{k.id}">'
            f'<button type="button" class="kw-toggle" '
            f'data-kw="{html.escape(k.text, quote=True)}">{html.escape(k.text)}</button>'
            f'<button type="button" class="kw-x" data-del-id="{k.id}" '
            f'data-del-text="{html.escape(k.text, quote=True)}" '
            f'title="Remove" aria-label="Remove">×</button></span>'
        )

    kw_tags = "".join(_kw_chip(k) for k in active_kws)

    banner = ""
    if qp.get("removed"):
        banner = (
            f'<div class="banner ok">Hidden <b>{html.escape(qp.get("removed"))}</b> from the '
            "YouTube watchlist.</div>"
        )
    elif qp.get("added"):
        banner = (
            f'<div class="banner ok">Added <b>{html.escape(qp.get("added"))}</b> to the YouTube '
            "watchlist. Select it, pick a day, then Search live results.</div>"
        )
    elif qp.get("channel_added"):
        banner = (
            f'<div class="banner ok">Added channel <b>{html.escape(qp.get("channel_added"))}</b>. '
            "It is included in the next live search.</div>"
        )
    elif qp.get("channel_removed"):
        banner = (
            f'<div class="banner ok">Removed channel <b>{html.escape(qp.get("channel_removed"))}</b> '
            "from live search. Re-add it any time.</div>"
        )

    channels = db.execute(
        select(YouTubeChannel).where(YouTubeChannel.active.is_(True)).order_by(YouTubeChannel.name)
    ).scalars().all()
    ch_tags = "".join(
        f'<span class="ch-tag">{html.escape(c.name)}'
        f'<button type="button" class="chip-x" data-ch-del="{c.id}" '
        f'data-ch-name="{html.escape(c.name, quote=True)}" title="Remove channel">&times;</button>'
        f'</span>'
        for c in channels
    ) or '<span class="hint">No channels yet.</span>'

    results_html = _youtube_live_placeholder()

    body = f"""
    {banner}
    <div class="hero">
      <h1>YouTube</h1>
      <p>Select keywords, pick a day, then <b>Search live results</b> — recent uploads on your
      channels are downloaded and transcribed live and matched to your keywords. Nothing runs in
      the background and nothing is stored (transcription still costs at click time).</p>
    </div>
    <div class="panel">
      <h2>Keyword search</h2>
      <div class="ch-bar">
        <div class="cap">Channels
          <button type="button" class="ghost" id="yt-ch-add" style="margin-left:.5rem;font-size:.72rem">+ Add channel</button>
        </div>
        <div class="ch-tags">{ch_tags}</div>
      </div>
      <div class="field">
        <label>Add to YouTube watchlist</label>
        <div class="kw-add" id="kw-draft-row">
          <input id="kw-draft-text" type="text" placeholder="Type a keyword, then Add" maxlength="120">
          <select id="kw-draft-lang"><option value="en">EN</option><option value="ur">UR</option></select>
          <button type="button" id="kw-add-btn">Add</button>
        </div>
        <form id="kw-confirm" method="post" action="/ui/keywords/batch" style="display:none">
          <input type="hidden" name="texts" id="kw-pending-texts" value="">
          <input type="hidden" name="language" id="kw-pending-lang" value="en">
          <input type="hidden" name="module" value="youtube">
          <input type="hidden" name="scan" value="1">
        </form>
        <p class="hint" style="margin-top:.45rem">Adding only saves the keyword. Click chips to select (✓),
        pick a day, then <b>Search live results</b>.</p>
        <div class="kw-bar">
          <div class="cap">Watchlist · click to select · × hide
            <button type="button" class="ghost" id="kw-sel-all" style="margin-left:.5rem;font-size:.72rem">Select all</button>
            <button type="button" class="ghost" id="kw-sel-none" style="font-size:.72rem">Clear</button>
          </div>
          <div class="kw-tags">{kw_tags or '<span class="hint">No keywords yet — add some above.</span>'}</div>
        </div>
      </div>
      <div class="yt-tabs" role="tablist">
        <button type="button" class="yt-tab on" data-tab="search">Uploads</button>
        <button type="button" class="yt-tab" data-tab="live">Live stream</button>
        <button type="button" class="yt-tab" data-tab="ticker">Live ticker</button>
      </div>
    </div>
    <div id="yt-tab-search" class="yt-panel">
      <div class="panel">
        <form id="yt-search" onsubmit="return false">
          <div class="field">
            <label for="date">Day to search</label>
            <input type="date" id="date" name="date" value="{html.escape(date_s)}">
          </div>
          <div class="actions">
            <button type="button" id="live-search-btn">Search live results</button>
            <a class="btn ghost" href="/youtube">Reset</a>
          </div>
          <p class="hint">Downloads &amp; transcribes recent uploads on your channels for the selected keywords, live. Results are shown here and never saved.</p>
        </form>
      </div>
      {results_html}
    </div>
    <div id="yt-tab-live" class="yt-panel" style="display:none">
      <div class="panel">
        <h2 style="margin-top:0" id="yt-live-heading">Live stream — read audio</h2>
        <p class="sub" id="yt-live-sub" style="color:var(--muted);font-size:.9rem;margin:.1rem 0 .9rem">Streams live right now on your channels. Pick one, choose a window of its timeline,
        and that portion is transcribed and matched against the same watchlist. Results appear only here and are not saved.</p>
        <div id="yt-live-datebar" style="display:none;margin:.1rem 0 .7rem">
          <label style="font-size:.8rem;font-weight:600;color:var(--muted);margin-right:.45rem">Date</label>
          <input type="date" id="yt-live-date" style="padding:.45rem .7rem;border:1px solid var(--line-strong);border-radius:999px;font:inherit;background:#fffdf9">
          <span class="hint" style="margin-left:.5rem">live streams now, plus recorded videos already known for that day</span>
        </div>
        <div id="yt-live-list"><span class="hint">Open this tab to check what's available…</span></div>
        <div id="yt-live-window" style="display:none">
          <p class="hint" id="yt-live-span" style="margin:.55rem 0 .45rem"></p>
          <div class="live-slider">
            <label>From&nbsp; <b id="yt-live-from-label"></b></label>
            <input type="range" id="yt-live-from" min="0" max="600" step="5" value="0">
          </div>
          <div class="live-slider">
            <label>To&nbsp; <b id="yt-live-to-label"></b></label>
            <input type="range" id="yt-live-to" min="0" max="600" step="5" value="600">
          </div>
          <p class="hint" id="yt-live-window-info" style="margin:.15rem 0 .6rem"></p>
        </div>
        <div class="row-btns">
          <button type="button" id="yt-live-run" style="display:none">Transcribe audio &amp; match</button>
        </div>
        <div id="yt-live-status" class="hint" style="margin-top:.55rem"></div>
        <div id="yt-live-results" style="margin-top:.55rem"></div>
      </div>
    </div>
    <div id="yt-ch-modal" role="dialog" aria-modal="true" aria-labelledby="yt-ch-title">
      <div class="box">
        <h3 id="yt-ch-title">Add YouTube channel</h3>
        <p class="sub">Paste a channel URL or @handle. We'll confirm it and add it to live search.</p>
        <input type="url" id="yt-ch-url" placeholder="https://www.youtube.com/@ChannelName" autocomplete="off">
        <div class="row-btns">
          <button type="button" id="yt-ch-check">Check channel</button>
          <button type="button" id="yt-ch-save" style="display:none">Add channel</button>
          <button type="button" class="ghost" id="yt-ch-close">Close</button>
        </div>
        <div id="yt-ch-result"></div>
      </div>
    </div>
    """
    return _shell("YouTube · Media Monitor", body, module="youtube")


def _youtube_live_placeholder() -> str:
    """Empty #results shell. Live search JS replaces this section in place."""
    return """
        <section class="results" id="results">
          <div class="results-head">
            <h2>Live results</h2>
          </div>
          <div class="empty">Select keywords, pick a day, then click <b>Search live results</b>.
          Hits appear here and are not saved.</div>
        </section>
        """



# ==========================================================================
# UI actions (same behaviour; land back on the single page)
# ==========================================================================


def _wants_json(request) -> bool:
    """True when the caller is the in-page fetch (AJAX), not a plain form post.

    The AJAX flow renders results in place; the plain form post (and direct
    unit-test calls, which pass no Request) keep the 303-redirect behaviour.
    """
    try:
        return "application/json" in (request.headers.get("accept") or "").lower()
    except Exception:
        return False


@app.post("/ui/keywords/batch")
def ui_batch_keywords(request: Request,
                      texts: str = Form(...), language: str = Form("en"),
                      module: str = Form("newspaper"),
                      date: str = Form(""),
                      scan: str = Form("1"),
                      db: Session = Depends(get_db)):
    """Create/reactivate many keywords; optionally scan them."""
    module = module if module in ("newspaper", "youtube") else "newspaper"
    wants_json = _wants_json(request)
    home = "/youtube" if module == "youtube" else "/"
    slot_date = (date or datetime.now(_PKT).date().isoformat()).strip()
    do_scan = scan.strip().lower() in ("1", "true", "yes", "on")
    raw = (texts or "").replace(",", "\n")
    seen: set[str] = set()
    ordered: list[str] = []
    for part in raw.splitlines():
        t = part.strip()
        if not t:
            continue
        fold = t.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        ordered.append(t)
    if not ordered:
        return RedirectResponse(home, status_code=303)

    created = _upsert_watch_keywords(db, ordered, language, module=module)

    if not created:
        return RedirectResponse(home, status_code=303)

    first = created[0]
    ids = [k.id for k in created]
    label = ", ".join(k.text for k in created[:3])
    if len(created) > 3:
        label += f" +{len(created) - 3}"

    chips = [{"id": k.id, "text": k.text} for k in created]

    # Live-only model: adding a keyword ONLY saves it to the watchlist. No scan,
    # no queue, no download, no Groq, no cost. Scraping happens later, and only
    # when the user clicks "Search live results".
    if wants_json:
        return JSONResponse({
            "ok": True,
            "module": module,
            "created": chips,
            "first": first.text,
            "scanning": False,
        })
    q = urlencode({"added": label, "date": slot_date})
    return RedirectResponse(f"{home}?{q}", status_code=303)


@app.post("/ui/keywords/{kid}/edit")
def ui_edit_keyword(kid: int, text: str = Form(...), language: str = Form("en"),
                    db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw and text.strip():
        kw.text = text.strip()
        kw.language = detect_keyword_language(kw.text, language)
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
def ui_delete_keyword(kid: int, db: Session = Depends(get_db), request: Request = None):
    wants_json = _wants_json(request)
    kw = db.get(Keyword, kid)
    if not kw:
        if wants_json:
            return JSONResponse({"ok": True, "id": kid})
        return RedirectResponse("/", status_code=303)
    text = kw.text
    module = kw.module or "newspaper"
    kw.active = False
    db.commit()
    if wants_json:
        # AJAX: the page drops the chip from the DOM instantly — no reload.
        return JSONResponse({"ok": True, "id": kid, "text": text, "module": module})
    home = "/youtube" if module == "youtube" else "/"
    if module == "youtube":
        q = urlencode({"removed": text})
    else:
        q = urlencode({
            "removed": text,
            "go": "1",
            "date": datetime.now(_PKT).date().isoformat(),
        })
    return RedirectResponse(f"{home}?{q}", status_code=303)


@app.post("/ui/papers/delete")
def ui_delete_paper(name: str = Form(...), db: Session = Depends(get_db),
                    request: Request = None):
    """Remove a user-added newspaper/e-paper from the DB so it's no longer in the
    picker or searched. Instant — nothing else to clean up."""
    name = (name or "").strip()
    if name:
        db.execute(delete(NewsSource).where(func.lower(NewsSource.name) == name.casefold()))
        db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "name": name})
    return RedirectResponse(f"/?{urlencode({'paper_removed': name})}", status_code=303)


@app.post("/ui/youtube/channels/{cid}/delete")
def ui_delete_channel(cid: int, db: Session = Depends(get_db),
                      request: Request = None):
    """Remove a YouTube channel: deactivate it so it drops out of the picker and
    live search. No stored result cards to purge — instant."""
    ch = db.get(YouTubeChannel, cid)
    if not ch:
        if _wants_json(request):
            return JSONResponse({"ok": True, "id": cid})
        return RedirectResponse("/youtube", status_code=303)
    name = ch.name
    ch.active = False
    db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "id": cid, "name": name})
    q = urlencode({"channel_removed": name, "date": datetime.now(_PKT).date().isoformat()})
    return RedirectResponse(f"/youtube?{q}", status_code=303)


def _fmt_secs(s: float) -> str:
    s = int(max(0, round(s)))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


_PHASE_LABELS = {
    "starting": "Starting up",
    "newspapers": "Reading newspaper sites",
    "epaper": "Reading e-paper pages",
    "youtube": "Transcribing YouTube",
    "done": "Done",
    "error": "Error",
}


def _render_live_results(job) -> str:
    """Render a live-search job's in-memory result cards as the #results section.

    Same markup/classes as the stored renderer, minus screenshots (we store no
    media). Streams: called repeatedly while the job runs, results grow."""
    import time
    results = list(job.results)
    running = job.status == "running"
    spin = ' <span class="spin"></span>' if running else ""
    kindmap = {"newspaper": "Web", "epaper": "E-Paper", "youtube": "YouTube"}
    cards = []
    for r in results:
        hl = r.get("keywords") or []
        excerpt = _highlight_excerpt(r.get("snippet") or "", hl)
        excerpt_html = f'<div class="excerpt">{excerpt}</div>' if excerpt else ""
        tags = "".join(f'<span class="tag">{html.escape(k)}</span>' for k in hl)
        meta = " · ".join(
            x for x in [kindmap.get(r.get("module"), ""), r.get("source"), r.get("meta")] if x
        )
        sent = (r.get("sentiment") or "").strip()
        sent_html = ""
        if sent:
            scls = {"Positive": "sent-pos", "Negative": "sent-neg"}.get(sent, "sent-neu")
            sent_html = f'<span class="sent {scls}">{html.escape(sent)}</span>'
        title = html.escape(r.get("title") or "")
        href = html.escape(r.get("url") or "#")
        img = (r.get("image") or "").strip()
        if img:
            # Remote page scan (e-paper) or article screenshot (website).
            e = html.escape(img, quote=True)
            shot = (f'<div class="shot"><img loading="lazy" class="zoom" src="{e}" '
                    f'data-full="{e}" alt="preview"></div>')
        else:
            shot = ('<div class="shot missing"><span class="noprev">No preview · live</span></div>')
        open_link = ""
        if r.get("module") == "newspaper" and href and href != "#":
            open_link = (
                f'<a class="jump" href="{href}" target="_blank" rel="noopener">'
                f'Open article</a>'
            )
        cards.append(
            f'<div class="det">{shot}<div class="body">'
            f'<a class="ttl" href="{href}" target="_blank" rel="noopener">{title}</a>'
            f'{excerpt_html}{open_link}<div class="meta">{html.escape(meta)}</div>'
            f'<div>{sent_html}{tags}</div></div></div>'
        )
    prog = job.progress or {}
    if cards:
        grid = f'<div class="grid">{"".join(cards)}</div>'
    elif running:
        grid = ('<div class="empty loading"><span class="spin"></span> '
                f'Searching live… {html.escape(prog.get("current") or "")}</div>')
    elif job.error:
        grid = f'<div class="empty">Live search failed: {html.escape(job.error)}</div>'
    else:
        grid = '<div class="empty">No live matches found. Nothing was stored.</div>'
    count = len(results)
    head = (f'<div class="results-head"><h2>Live results{spin}</h2>'
            f'<span class="count">{count} match{"es" if count != 1 else ""}</span></div>')
    note = getattr(job, "note", "") or ""
    note_html = (f'<div class="banner warn" style="margin:.2rem 0 1rem">{html.escape(note)}</div>'
                 if note else "")
    sub = ""
    if running:
        elapsed = max(0.0, time.time() - getattr(job, "created", time.time()))
        checked = prog.get("checked") if isinstance(prog.get("checked"), int) else 0
        total = prog.get("total") if isinstance(prog.get("total"), int) else 0
        phase_label = _PHASE_LABELS.get(prog.get("phase") or "", prog.get("phase") or "Searching")
        current = prog.get("current") or ""

        # Animate (indeterminate) until at least one unit finishes, so a single
        # slow source never looks frozen at 0%.
        if total and checked > 0:
            pct = 100 if checked >= total else int(checked / total * 100)
            bar_cls = "live-bar"
        else:
            pct, bar_cls = 0, "live-bar indet"

        if total and 0 < checked < total and elapsed > 1.5:
            eta_txt = f"~{_fmt_secs((elapsed / checked) * (total - checked))} left"
        elif total and checked >= total:
            eta_txt = "finishing…"
        else:
            eta_txt = "estimating time…"

        now_txt = phase_label + (f" · {current}" if current else "")
        if total:
            now_txt += f" · {checked}/{total}"
        sub = (
            '<div class="live-progress">'
            f'<div class="{bar_cls}"><div class="live-bar-fill" style="width:{pct}%"></div></div>'
            '<div class="live-sub">'
            f'<span class="now">{html.escape(now_txt)}</span>'
            f'<span class="eta">{html.escape(eta_txt)} · {_fmt_secs(elapsed)} elapsed</span>'
            '</div></div>'
        )
    return (f'<section class="results" id="results" data-live="1" '
            f'data-status="{job.status}">{head}{note_html}{sub}{grid}</section>')


@app.post("/api/live/search")
def api_live_search(request: Request,
                    module: str = Form("newspaper"),
                    q: str = Form(""),
                    kw_id: list[str] = Form(default=[]),
                    paper: list[str] = Form(default=[]),
                    date: str = Form(""),
                    db: Session = Depends(get_db)):
    """Start a live scrape/transcribe. Nothing is stored — results live in an
    in-memory job the browser polls. This is the ONLY thing that scrapes."""
    module = "youtube" if module == "youtube" else "newspaper"
    today = datetime.now(_PKT).date().isoformat()

    if module == "youtube":
        ids = [int(x) for x in kw_id if str(x).isdigit()]
        # YouTube live search downloads + transcribes (paid). Require an explicit
        # keyword selection so a click can never accidentally transcribe the whole
        # watchlist across every channel.
        if not ids:
            return JSONResponse(
                {"error": "Select at least one keyword (click the chips) — a live "
                          "YouTube search downloads and transcribes videos."}, 400)
        kq = select(Keyword).where(
            Keyword.active.is_(True), Keyword.module == "youtube", Keyword.id.in_(ids))
        rows = db.execute(kq.order_by(Keyword.text)).scalars().all()
        keywords = [(k.text, k.language or "ur") for k in rows if k.text]
        if not keywords:
            return JSONResponse({"error": "Those keywords are no longer on the watchlist."}, 400)
        channels = [
            {"channel_id": c.channel_id, "name": c.name, "playlist_id": c.uploads_playlist_id or ""}
            for c in db.execute(
                select(YouTubeChannel).where(YouTubeChannel.active.is_(True))
            ).scalars().all()
            if c.channel_id
        ]
        if not channels:
            return JSONResponse({"error": "No active YouTube channels to search."}, 400)
        day = (date or today).strip()
        try:
            base = datetime.strptime(day, "%Y-%m-%d").date()
            after = datetime(base.year, base.month, base.day, tzinfo=_PKT)
            before = after + timedelta(days=1)
        except ValueError:
            before = datetime.now(_PKT)
            after = before - timedelta(days=1)
        jid = live_jobs.run("youtube", live_search.search_youtube,
                            keywords, channels, after.isoformat(), before.isoformat())
        return {"job": jid, "module": "youtube"}

    # Newspaper page → websites + e-papers.
    kw = (q or "").strip()
    if kw:
        # Search the typed word directly — it does NOT need to be a saved
        # keyword. This is what lets you just type a word and search it live.
        keywords = [(kw, detect_keyword_language(kw))]
    else:
        # No word typed: search the whole active watchlist at once.
        rows = db.execute(
            select(Keyword).where(
                Keyword.active.is_(True), Keyword.module == "newspaper"
            ).order_by(Keyword.text)
        ).scalars().all()
        keywords = [(k.text, k.language or "en") for k in rows if k.text]
    if not keywords:
        return JSONResponse(
            {"error": "Type a word to search, or add keywords to your watchlist first."}, 400)

    # Sources are ONLY what the user added (by link). No built-in papers.
    rows = db.execute(
        select(NewsSource).where(NewsSource.active.is_(True))
    ).scalars().all()
    sel_names = {p.strip().casefold() for p in paper if p and p.strip()}
    if sel_names:
        rows = [r for r in rows if (r.name or "").casefold() in sel_names]
    sources_list = [
        {"name": r.name, "url": r.url, "kind": r.kind, "language": r.language or "en"}
        for r in rows
    ]
    if not sources_list:
        return JSONResponse(
            {"error": "Add a newspaper or e-paper first (＋ Add more) and select it, then search."}, 400)
    jid = live_jobs.run("newspaper", live_search.search_press,
                        keywords, sources_list, (date or today).strip())
    return {"job": jid, "module": "newspaper"}


@app.get("/api/live/search/{job_id}")
def api_live_search_status(job_id: str):
    job = live_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found or expired")
    return {
        "status": job.status,
        "progress": job.progress,
        "count": len(job.results),
        "error": job.error,
        "note": getattr(job, "note", "") or "",
        "html": _render_live_results(job),
    }


@app.post("/api/live/search/{job_id}/cancel")
def api_live_search_cancel(job_id: str):
    return {"cancelled": live_jobs.cancel(job_id)}


@app.get("/api/youtube/live")
def api_live_streams(db: Session = Depends(get_db)):
    """Streams live right now on the active channels — for the Live panel."""
    from app.youtube import livestream

    try:
        return {"streams": livestream.list_live(db)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/youtube/ticker-sources")
def api_ticker_sources(date: str = "", db: Session = Depends(get_db)):
    """Live streams now + that day's bulletins — sources for the ticker tab."""
    from app.youtube import livestream

    day = (date or datetime.now(_PKT).date().isoformat()).strip()
    live = []
    if day == datetime.now(_PKT).date().isoformat():
        try:
            live = livestream.list_live(db)
            for s in live:
                s["kind"] = "live"
        except RuntimeError:
            live = []
    bulletins = livestream.list_bulletins_for_date(db, day)
    return {"date": day, "live": live, "bulletins": bulletins}


@app.get("/api/youtube/live/timeline/{video_id}")
def api_live_timeline(video_id: str):
    """True addressable DVR length for a live stream (streams restart, so the
    Data API's start time can be a whole session stale)."""
    from app.youtube import livestream

    try:
        return livestream.stream_timeline(video_id)
    except livestream.LiveError as exc:
        raise HTTPException(400, str(exc))


class _LiveRunIn(BaseModel):
    video_id: str = Field(min_length=5, max_length=32)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    is_live: bool = True


@app.post("/api/youtube/live/run")
def api_live_run(inp: _LiveRunIn):
    """Transcribe+match one window of a live stream (background job)."""
    from app.youtube import livestream

    try:
        job_id = livestream.start_job(inp.video_id, inp.start_seconds, inp.end_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job": job_id}


@app.post("/api/youtube/live/ticker")
def api_live_ticker(inp: _LiveRunIn):
    """Read the Urdu ticker over a live-stream window and match the watchlist."""
    from app.youtube import livestream

    try:
        job_id = livestream.start_ticker_job(
            inp.video_id, inp.start_seconds, inp.end_seconds, is_live=inp.is_live)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job": job_id}


@app.get("/api/youtube/live/jobs/{job_id}")
def api_live_job(job_id: str):
    from app.youtube import livestream

    st = livestream.job_status(job_id)
    if st is None:
        raise HTTPException(404, "job not found")
    return st










# ==========================================================================
# JSON API (unchanged — for scripts / poller)
# ==========================================================================
class _ProbeIn(BaseModel):
    kind: str
    url: str
    name: str = ""


class _CustomSourceIn(BaseModel):
    name: str
    kind: str
    url: str
    summary: str = ""
    detail: dict = Field(default_factory=dict)


@app.post("/api/probe-source")
def api_probe_source(body: _ProbeIn):
    """Check a newspaper or e-paper URL and return a short capability summary."""
    return sources_probe.probe(body.kind, body.url)


@app.post("/api/custom-sources")
def api_save_custom_source(body: _CustomSourceIn, db: Session = Depends(get_db)):
    """Save a user-added newspaper / e-paper to the DB. Its URL is what live
    search scrapes — there are no built-in papers."""
    name = body.name.strip()
    if not name:
        return {"ok": False, "summary": "Enter a display name before saving."}
    if not body.url.startswith(("http://", "https://")):
        return {"ok": False, "summary": "Need a valid link (http/https) to save."}
    kind = "epaper" if (body.kind or "").strip().lower() == "epaper" else "newspaper"
    existing = db.execute(
        select(NewsSource).where(func.lower(NewsSource.name) == name.casefold())
    ).scalar_one_or_none()
    if existing:
        existing.url = body.url.strip()
        existing.kind = kind
        existing.active = True
        db.commit()
        row_id = existing.id
    else:
        row = NewsSource(name=name, url=body.url.strip(), kind=kind,
                         language=detect_keyword_language(name))
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
    label = "e-paper" if kind == "epaper" else "newspaper"
    return {"ok": True, "id": row_id, "name": name, "kind": kind,
            "summary": f"Saved {label} “{name}”. It will be searched live."}


@app.get("/api/keywords")
def list_keywords(module: str | None = None, db: Session = Depends(get_db)):
    q = select(Keyword).order_by(Keyword.created_at.desc())
    if module in ("newspaper", "youtube"):
        q = q.where(Keyword.module == module)
    rows = db.execute(q).scalars().all()
    return [{"id": k.id, "text": k.text, "language": k.language,
             "module": k.module, "active": k.active}
            for k in rows]






@app.get("/api/version")
def app_version():
    """What code this host is actually running — for confirming a deploy."""
    return {"version": BUILD_VERSION}












class _YoutubeProbeIn(BaseModel):
    url: str = ""


class _YoutubeChannelSlotIn(BaseModel):
    local_time: str
    label: str = ""
    title_rules: list[str] = Field(default_factory=list)
    samples: int = 0
    example_title: str = ""


class _YoutubeChannelIn(BaseModel):
    channel_id: str
    name: str = ""
    url: str = ""
    handle: str = ""
    uploads_playlist_id: str = ""
    slots: list[_YoutubeChannelSlotIn] = Field(default_factory=list)


@app.post("/api/probe-youtube-channel")
def api_probe_youtube_channel(body: _YoutubeProbeIn):
    from app.youtube import channel_probe
    return channel_probe.probe_channel(body.url)


@app.post("/api/youtube/channels")
def api_add_youtube_channel(body: _YoutubeChannelIn, db: Session = Depends(get_db)):
    from app.youtube import channel_probe

    if not body.channel_id.strip():
        return {"ok": False, "summary": "Missing channel id."}
    existing = db.execute(
        select(YouTubeChannel).where(YouTubeChannel.channel_id == body.channel_id.strip())
    ).scalar_one_or_none()
    if existing and existing.active:
        return {"ok": False, "summary": f"“{existing.name}” is already added."}
    if existing and not existing.active:
        existing.active = True          # re-adding a removed channel reactivates it
        db.commit()
        return {"ok": True, "summary": f"Re-added “{existing.name}”.",
                "name": existing.name, "id": existing.id}
    row = channel_probe.save_channel(
        db,
        channel_id=body.channel_id.strip(),
        name=body.name.strip(),
        url=body.url.strip(),
        handle=body.handle.strip(),
        uploads_playlist_id=body.uploads_playlist_id.strip(),
        slots=[],
    )
    return {"ok": True, "name": row.name, "id": row.id, "summary": f"Added {row.name}."}


@app.get("/api/youtube/channels")
def list_youtube_channels(db: Session = Depends(get_db)):
    rows = db.execute(
        select(YouTubeChannel).order_by(YouTubeChannel.name)
    ).scalars().all()
    return [
        {
            "id": c.id, "channel_id": c.channel_id, "name": c.name,
            "handle": c.handle, "url": c.url, "active": c.active,
            "media_source": c.media_source,
        }
        for c in rows
    ]




