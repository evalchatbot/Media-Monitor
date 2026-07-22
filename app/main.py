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
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from config import BASE_DIR, settings
from app.db.base import SessionLocal, init_db
from app.db.models import BulletinSlot, EPaperPage, Keyword, Mention, YouTubeChannel
from app.core import keyword_scan_queue, result_policy
from app.core.keywords import script_language
from app.epaper import scan_runner, sources
from app.newspaper import scan_manager
from app.newspaper.pipeline import run_newspaper_scan, run_quick_match
from app.youtube import scan_runner as yt_scan_runner
from app.scrapers.sites import SITE_CONFIGS
from app.scheduler import shutdown_scheduler, start_scheduler
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
    init_db()
    if settings.scheduler_enabled:
        start_scheduler()
    import threading

    from app.newspaper.pipeline import warm_quick_corpus

    threading.Thread(target=warm_quick_corpus, daemon=True).start()
    yield
    shutdown_scheduler()


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
.yt-status{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:.5rem;margin:0 0 1rem}
.yt-status .cell{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-sm);
  padding:.65rem .75rem;font-size:.8rem}
.yt-status .cell b{display:block;font-size:.88rem;color:var(--ink);margin-bottom:.2rem}
.yt-status .st{font-weight:700;color:var(--blue-deep);text-transform:capitalize}
.yt-status .st.missing,.yt-status .st.failed{color:var(--warn)}
.yt-status .st.ready{color:var(--ok)}
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
#yt-ch-slots{margin:.65rem 0 0;padding:0;list-style:none;max-height:11rem;overflow:auto}
#yt-ch-slots li{display:flex;gap:.5rem;align-items:flex-start;padding:.45rem 0;border-bottom:1px solid var(--line);
  font-size:.82rem;font-weight:500}
#yt-ch-slots li:last-child{border-bottom:0}
#yt-ch-slots .slot-meta{color:var(--faint);font-size:.76rem;font-weight:500;margin-top:.15rem;line-height:1.35}

#yt-period-modal{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;
  padding:1rem;background:rgba(44,58,72,.45);backdrop-filter:blur(6px)}
#yt-period-modal.open{display:flex}
#yt-period-modal .box{width:min(520px,100%);background:linear-gradient(180deg,#fffdf9,#faf4ea);
  border:1px solid var(--line);border-radius:var(--r);padding:1.35rem 1.4rem;box-shadow:var(--shadow)}
#yt-period-modal h3{margin:0 0 .35rem;font-size:1.15rem;color:var(--blue-deep)}
#yt-period-modal .sub{margin:0 0 1rem;color:var(--muted);font-size:.88rem;line-height:1.45}
#yt-period-modal .period-grid{display:grid;grid-template-columns:1fr 1fr;gap:.65rem .75rem;margin-bottom:.75rem}
#yt-period-modal label{font-size:.78rem;font-weight:600;color:var(--muted);display:block;margin-bottom:.25rem}
#yt-period-modal input[type=date],#yt-period-modal input[type=time]{width:100%;padding:.55rem .75rem;
  border:1px solid var(--line-strong);border-radius:999px;font:inherit;background:#fffdf9}
#yt-period-modal .row-btns{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.5rem}
#yt-period-modal #yt-period-result{margin-top:.75rem;padding:.75rem .9rem;border-radius:var(--r-sm);font-size:.88rem;
  font-weight:600;line-height:1.45;display:none}
#yt-period-modal #yt-period-result.show{display:block}
#yt-period-modal #yt-period-result.bad{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn)}

.ch-bar{margin:.65rem 0 .35rem}
.ch-tags{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.35rem}
.ch-tag{display:inline-flex;align-items:center;padding:.22rem .65rem;border-radius:999px;font-size:.78rem;
  font-weight:600;background:var(--blue-soft);color:var(--blue-deep);border:1px solid var(--blue-mist)}

.spin{display:inline-block;width:13px;height:13px;border:2px solid currentColor;border-top-color:transparent;
  border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}

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
  .yt-status{grid-template-columns:1fr 1fr}
  #yt-period-modal .period-grid{grid-template-columns:1fr}
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
  document.querySelectorAll('.kw-pick').forEach(function(btn){
    btn.addEventListener('click',function(){
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
  });
  var autoShowTimer=null;
  function autoShowNewspaper(kw, kwId){
    // Filter the results to this keyword in place — no page reload. Keep the
    // current date and newspaper selection by serialising the search form.
    var p=new URLSearchParams(form?new FormData(form):location.search);
    if(kw){p.set('q',kw);}else{p.delete('q');}
    p.set('go','1');
    p.set('module','newspaper');
    history.replaceState(null,'','?'+p.toString());
    refreshResults(true);  // show whatever is already matched, at once
    // Then match this keyword against the stored article + e-paper text, so
    // results appear even for a keyword that was never scanned (e.g. it exists
    // in 110 articles but had no mentions yet). The poll loop streams the new
    // hits in; kick it now so it doesn't wait out the idle interval.
    if(kwId){
      fetch('/api/keywords/'+encodeURIComponent(kwId)+'/match',{method:'POST'})
        .then(function(){ clearTimeout(pollTimer); pollTimer=setTimeout(poll,700); })
        .catch(function(){});
    }
  }
  function autoShowYoutube(){
    // Selecting a keyword shows its matches straight away — no Show results
    // click, and an in-place fragment swap instead of a full page reload.
    clearTimeout(autoShowTimer);
    autoShowTimer=setTimeout(function(){
      var ids=[];
      document.querySelectorAll('.kw-chip.sel[data-kw-id]').forEach(function(c){
        var id=c.getAttribute('data-kw-id'); if(id)ids.push(id);
      });
      var p=new URLSearchParams(location.search);
      p.delete('kw_id'); p.delete('ymax');  // new selection resets pagination
      ids.forEach(function(id){p.append('kw_id',id)});
      p.set('module','youtube');
      if(ids.length){p.set('filter','1');p.set('go','1');}
      else{p.delete('filter');}
      history.replaceState(null,'','?'+p.toString());
      refreshResults(true);
    },200);  // debounce so selecting several fires one query
  }
  if(pageModule==='youtube'){
    document.querySelectorAll('.kw-toggle').forEach(function(btn){
      btn.addEventListener('click',function(e){
        e.preventDefault();
        var chip=btn.closest('.kw-chip');
        if(!chip)return;
        chip.classList.toggle('sel');
        autoShowYoutube();
      });
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
    var ytForm=document.getElementById('yt-search');
    function syncPeriodHidden(){
      var sd=document.getElementById('yt-p-start-date');
      var ed=document.getElementById('yt-p-end-date');
      var st=document.getElementById('yt-p-start-time');
      var et=document.getElementById('yt-p-end-time');
      var hs=document.getElementById('yt-period-start');
      var he=document.getElementById('yt-period-end');
      if(!sd||!ed||!st||!et||!hs||!he)return;
      hs.value=sd.value+'T'+st.value+':00+05:00';
      he.value=ed.value+'T'+et.value+':59+05:00';
    }
    if(ytForm)ytForm.addEventListener('submit',function(){
      syncPeriodHidden();
      var box=document.getElementById('yt-kw-hidden');
      if(!box)return;
      box.innerHTML='';
      document.querySelectorAll('.kw-chip.sel[data-kw-id]').forEach(function(chip){
        var id=chip.getAttribute('data-kw-id');
        if(!id)return;
        var inp=document.createElement('input');
        inp.type='hidden';inp.name='kw_id';inp.value=id;
        box.appendChild(inp);
      });
      var filterInp=document.getElementById('yt-filter');
      var showBtn=document.getElementById('yt-show-results');
      if(filterInp&&showBtn&&document.activeElement===showBtn){
        filterInp.value='1';
      }else if(filterInp){
        filterInp.value='';
      }
    });
    var periodOpen=document.getElementById('yt-period-open');
    var periodModal=document.getElementById('yt-period-modal');
    var periodClose=document.getElementById('yt-period-close');
    var periodRun=document.getElementById('yt-period-run');
    var periodForm=document.getElementById('yt-period-form');
    var periodResult=document.getElementById('yt-period-result');
    function openPeriodModal(){
      if(!periodModal)return;
      periodModal.classList.add('open');
      if(periodResult){periodResult.className='';periodResult.textContent='';periodResult.classList.remove('show')}
    }
    function closePeriodModal(){if(periodModal)periodModal.classList.remove('open')}
    if(periodOpen)periodOpen.addEventListener('click',openPeriodModal);
    if(periodClose)periodClose.addEventListener('click',closePeriodModal);
    if(periodModal)periodModal.addEventListener('click',function(e){if(e.target===periodModal)closePeriodModal()});
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape'&&periodModal&&periodModal.classList.contains('open'))closePeriodModal();
    });
    if(periodRun)periodRun.addEventListener('click',function(){
      if(!periodForm)return;
      var sel=document.querySelectorAll('.kw-chip.sel[data-kw-id]');
      if(!sel.length){
        if(periodResult){periodResult.textContent='Select at least one keyword on the watchlist.';periodResult.className='show bad'}
        return;
      }
      syncPeriodHidden();
      var box=document.getElementById('yt-period-kw-hidden');
      if(box){
        box.innerHTML='';
        sel.forEach(function(chip){
          var id=chip.getAttribute('data-kw-id');
          if(!id)return;
          var inp=document.createElement('input');
          inp.type='hidden';inp.name='kw_id';inp.value=id;
          box.appendChild(inp);
        });
      }
      periodForm.requestSubmit?periodForm.requestSubmit():periodForm.submit();
    });
  }

  var wasScanning=__SCANNING__;
  var wasQueue=__QUEUE__;
  var lastResultsSig=(document.getElementById('results')||{}).getAttribute
    ? (document.getElementById('results').getAttribute('data-sig')||'')
    : '';
  async function refreshResults(force){
    var el=document.getElementById('results');
    if(!el)return;
    var params=new URLSearchParams(location.search);
    if(!force && !params.has('go')&&!(params.get('q')||'').trim()&&!params.get('start'))return;
    params.set('module', pageModule);
    if(force)el.style.opacity='0.45';  // instant feedback on click
    try{
      var r=await fetch('/ui/results?'+params.toString(),{headers:{'Accept':'text/html'}});
      if(!r.ok){el.style.opacity='';return;}
      var sig=r.headers.get('X-Results-Sig')||'';
      var html=await r.text();
      if(!force && sig && sig===lastResultsSig){el.style.opacity='';return;}
      lastResultsSig=sig||lastResultsSig;
      var wrap=document.createElement('div');
      wrap.innerHTML=html.trim();
      var next=wrap.firstElementChild;
      if(next)el.replaceWith(next);
    }catch(err){}
    var back=document.getElementById('results');
    if(back)back.style.opacity='';
  }
  var IDLE_MS=6000, BUSY_MS=1500, pollTimer=null;
  async function poll(){
    var running=false, queueOn=false;
    try{
      // Only the status feeds this page cares about — the other module's poll
      // was pure waste. These read a progress file, not the database.
      var q=await fetch('/api/scan/queue').then(function(r){return r.json()});
      var queueItems=[].concat(q.batch||[], q.pending||[]);
      queueOn=queueItems.some(function(x){
        var m=(x&&x.module)||'newspaper';
        return pageModule==='youtube'?m==='youtube':m!=='youtube';
      });
      if(pageModule==='youtube'){
        var y=await fetch('/api/scan/youtube/status').then(function(r){return r.json()}).catch(function(){return {}});
        running=!!(y.running||queueOn);
      }else{
        var n=await fetch('/api/scan/status').then(function(r){return r.json()});
        var e=await fetch('/api/scan/epaper/status').then(function(r){return r.json()});
        running=!!(n.running||e.running||queueOn);
      }
      var b=document.getElementById('scanbtn');
      if(running && b){b.disabled=true;b.innerHTML='<span class="spin"></span> Scanning…'}
      // Only re-query results while something is producing them; the idle case
      // was re-running the full results query every tick for no change.
      if(running) await refreshResults();
      if(wasQueue && !queueOn){location.reload();return}
      if(!running && wasScanning){location.reload();return}
      wasScanning=running;
      wasQueue=queueOn;
    }catch(err){}
    finally{
      // Fast heartbeat while working, slow one when idle — cuts steady-state
      // background requests roughly fourfold.
      pollTimer=setTimeout(poll, running?BUSY_MS:IDLE_MS);
    }
  }
  pollTimer=setTimeout(poll, BUSY_MS);
  if(document.getElementById('results'))setTimeout(refreshResults,400);
  // "Show more" lives inside the results fragment, which is swapped out on each
  // refresh, so bind it by delegation on the document rather than directly.
  // #yt-more paginates YouTube (ymax); #news-more paginates newspaper (nmax).
  document.addEventListener('click',function(ev){
    var more=ev.target.closest?ev.target.closest('.more-btn'):null;
    if(!more)return;
    var next=more.getAttribute('data-next');
    var p=new URLSearchParams(location.search);
    if(next)p.set(more.id==='news-more'?'nmax':'ymax', next);
    p.set('module',pageModule);
    history.replaceState(null,'','?'+p.toString());
    more.disabled=true;more.textContent='Loading…';
    refreshResults(true);
  });

  /* One "Add" button: type a keyword (or a comma list), Add — it's saved and
     matched against stored data straight away. */
  (function(){
    var input=document.getElementById('kw-draft-text'),
        lang=document.getElementById('kw-draft-lang'),
        form=document.getElementById('kw-confirm'),
        texts=document.getElementById('kw-pending-texts'),
        langH=document.getElementById('kw-pending-lang'),
        addBtn=document.getElementById('kw-add-btn');
    function submitAdd(){
      if(!input||!form||!texts)return;
      var raw=(input.value||'').trim();
      if(!raw)return;
      texts.value=raw;                       // endpoint splits on comma/newline
      if(langH&&lang)langH.value=lang.value||'en';
      form.requestSubmit?form.requestSubmit():form.submit();
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
      if(r.ok)location.reload();
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
    var slotsBox=document.getElementById('yt-ch-slots');
    var lastProbe=null;
    function openModal(){
      if(!modal)return;
      modal.classList.add('open');
      lastProbe=null;
      if(result){result.className='';result.textContent='';result.classList.remove('show')}
      if(slotsBox)slotsBox.innerHTML='';
      if(saveBtn)saveBtn.style.display='none';
    }
    function closeModal(){if(modal)modal.classList.remove('open')}
    function renderSlots(slots){
      if(!slotsBox||!slots||!slots.length){if(slotsBox)slotsBox.innerHTML='';return}
      slotsBox.innerHTML=slots.map(function(s,i){
        var ex=(s.example_title||'').replace(/</g,'&lt;');
        return '<li><label><input type="checkbox" class="yt-slot-pick" data-i="'+i+'" checked> '
          +'<span><b>'+s.label+'</b> · '+s.samples+' recent upload'+(s.samples===1?'':'s')
          +(ex?'<div class="slot-meta">e.g. '+ex+'</div>':'')+'</span></label></li>';
      }).join('');
    }
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
        renderSlots(r.detail&&r.detail.slots);
        if(saveBtn)saveBtn.style.display=r.ok?'inline-flex':'none';
      }catch(err){
        if(result){result.textContent='Check failed — try again.';result.className='show bad'}
        if(saveBtn)saveBtn.style.display='none';
        if(slotsBox)slotsBox.innerHTML='';
      }
      checkBtn.disabled=false;checkBtn.textContent='Find bulletins';
    });
    if(saveBtn)saveBtn.addEventListener('click',async function(){
      if(!lastProbe||!lastProbe.ok||!lastProbe.detail)return;
      var picks=[].slice.call(document.querySelectorAll('.yt-slot-pick:checked'))
        .map(function(el){return parseInt(el.getAttribute('data-i'),10)})
        .filter(function(n){return !isNaN(n)});
      var slots=(lastProbe.detail.slots||[]).filter(function(_,i){return picks.indexOf(i)>=0});
      if(!slots.length){
        if(result){result.textContent='Select at least one bulletin slot.';result.className='show bad'}
        return;
      }
      saveBtn.disabled=true;
      try{
        var body=Object.assign({},lastProbe.detail,{slots:slots});
        var r=await fetch('/api/youtube/channels',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(body)}).then(function(x){return x.json()});
        if(r.ok)location.href='/youtube?channel_added='+encodeURIComponent(r.name||'');
        else if(result){result.textContent=r.summary||'Could not add channel.';result.className='show bad'}
      }catch(err){
        if(result){result.textContent='Save failed.';result.className='show bad'}
      }
      saveBtn.disabled=false;
    });
  })();
})();
"""


def _paper_names() -> list[str]:
    """Unique publication names shown in the filter (websites + e-papers)."""
    names: list[str] = ["Dawn"]
    names.extend(c.source for c in SITE_CONFIGS)
    names.extend(meta[0] for meta in sources.SOURCES.values())
    names.extend(r["name"] for r in sources_probe.custom_sources() if r.get("name"))
    out, seen = [], set()
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _shell(title: str, body: str, *, module: str = "newspaper") -> str:
    news = scan_manager.status()
    ep = scan_runner.status()
    yt = yt_scan_runner.status() if settings.youtube_enabled else {"running": False}
    q = keyword_scan_queue.status()
    queue_items = list(q.get("batch") or []) + list(q.get("pending") or [])
    yt_queue_on = any((x.get("module") or "newspaper") == "youtube" for x in queue_items)
    news_queue_on = any((x.get("module") or "newspaper") != "youtube" for x in queue_items)
    if module == "youtube":
        scanning = bool(yt.get("running") or yt_queue_on)
        queue_on = yt_queue_on
    else:
        scanning = bool(news["running"] or ep["running"] or news_queue_on)
        queue_on = news_queue_on
    # Deliberately does not announce scanning — progress belongs in the results
    # grid, not the header.
    state = '<span class="dot live"></span>Live'
    if module == "youtube":
        scan_btn = (
            '<button class="ghost" id="scanbtn" disabled><span class="spin"></span> Scanning…</button>'
            if scanning
            else '<form method="post" action="/ui/scan/youtube" style="margin:0">'
                 '<button class="ghost" id="scanbtn" type="submit">Scan now</button></form>'
        )
    else:
        scan_btn = (
            '<button class="ghost" id="scanbtn" disabled><span class="spin"></span> Scanning…</button>'
            if scanning
            else '<form method="post" action="/ui/scan" style="margin:0">'
                 '<button class="ghost" id="scanbtn" type="submit">Scan now</button></form>'
        )
    nav = (
        '<nav class="mod-nav">'
        f'<a href="/" class="{"on" if module == "newspaper" else ""}">Newspaper</a>'
        f'<a href="/youtube" class="{"on" if module == "youtube" else ""}">YouTube</a>'
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
  <span class="live-wrap" style="display:flex;flex-direction:column;align-items:flex-end;gap:.15rem">
    <span class="live" id="live-state">{state}</span>
    <span class="live-detail" id="live-detail"></span>
    <span class="build-tag" title="Deployed build">build {html.escape(BUILD_VERSION)}</span>
  </span>
  {scan_btn}
</div></div>
<main class="page"><div class="wrap">{body}</div></main>
<script>{_JS.replace('__SCANNING__', 'true' if scanning else 'false').replace('__QUEUE__', 'true' if queue_on else 'false')}</script>
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


def _known_keyword_fold(db: Session) -> set[str]:
    """All keyword strings still in the watchlist table (active or paused)."""
    return {
        (k.text or "").casefold()
        for k in db.execute(select(Keyword)).scalars().all()
        if k.text
    }


def _active_keyword_fold(db: Session, module: str | None = "newspaper") -> dict[str, str]:
    """casefold -> canonical text for active watchlist keywords."""
    out: dict[str, str] = {}
    q = select(Keyword).where(Keyword.active.is_(True))
    if module:
        q = q.where(Keyword.module == module)
    for k in db.execute(q).scalars().all():
        if k.text:
            out[(k.text or "").casefold()] = k.text
    return out


def _live_matched(m: Mention, active_fold: dict[str, str]) -> list[str]:
    """matched_keywords that are still on the active watchlist (canonical text)."""
    seen, out = set(), []
    for k in (m.matched_keywords or []):
        fold = (k or "").casefold()
        if fold in active_fold and fold not in seen:
            seen.add(fold)
            out.append(active_fold[fold])
    return out


def _refresh_mention_visuals(db: Session, m: Mention) -> bool:
    """Rebuild cutout/screenshot so baked-in highlights match current keywords only."""
    kws = [k for k in (m.matched_keywords or []) if k]
    if not kws:
        return False
    old_shot, old_full = m.screenshot_path, m.full_screenshot_path
    old_keyword_paths = set((m.keyword_media or {}).values())

    if m.module == "epaper":
        parts = (m.external_id or "").split(":")
        if len(parts) < 4 or not parts[3].startswith("p"):
            return False
        try:
            page_no = int(parts[3][1:])
        except ValueError:
            return False
        row = db.execute(
            select(EPaperPage).where(
                EPaperPage.paper == parts[0],
                EPaperPage.city == parts[1],
                EPaperPage.date == parts[2],
                EPaperPage.page_no == page_no,
            )
        ).scalar_one_or_none()
        if not row or not row.image_path:
            return False
        from app.epaper.pipeline import _detection_shot, _make_clip, _snippet

        langs = {
            k.text: k.language
            for k in db.execute(select(Keyword)).scalars().all()
            if k.text
        }
        kw_lang = {k: langs.get(k, "en") for k in kws}
        media = {}
        for keyword in kws:
            snippet = _snippet(row.ocr_text or "", [keyword])
            clip = _make_clip(
                row, [keyword], {keyword: kw_lang.get(keyword, "en")}, snippet
            )
            if clip:
                media[keyword] = clip
        shot = _detection_shot(row)
        m.keyword_media = media
        m.screenshot_path = next(iter(media.values()), None) or shot
        m.full_screenshot_path = shot or m.full_screenshot_path
        db.flush()
        if old_shot and old_shot != m.screenshot_path:
            _unlink_orphan_media(db, old_shot, m.id)
        if old_full and old_full != m.full_screenshot_path:
            _unlink_orphan_media(db, old_full, m.id)
        current = set(media.values())
        for old in old_keyword_paths - current:
            _unlink_orphan_media(db, old, m.id)
        return True

    # Website: drop old baked highlights; backfill re-captures with current keywords.
    m.screenshot_path = None
    m.full_screenshot_path = None
    m.keyword_media = {}
    db.flush()
    _unlink_orphan_media(db, old_shot, m.id)
    _unlink_orphan_media(db, old_full, m.id)
    for old in old_keyword_paths:
        _unlink_orphan_media(db, old, m.id)
    return True


def _purge_keyword_results(db: Session, keyword_text: str) -> dict:
    """Strict delete: remove EVERY mention that matched this keyword (even if
    other keywords were also on the same card). Caller expects a fresh ▶ / scan
    to rebuild hits for any remaining watchlist terms."""
    needle = keyword_text.casefold()
    deleted = files = 0
    rows = db.execute(select(Mention)).scalars().all()
    for m in rows:
        kws = list(m.matched_keywords or [])
        if not any((k or "").casefold() == needle for k in kws):
            continue
        paths = {
            m.screenshot_path, m.full_screenshot_path,
            *(m.keyword_media or {}).values(),
        }
        for path in paths:
            if _unlink_orphan_media(db, path, m.id):
                files += 1
        db.delete(m)
        deleted += 1
    if deleted:
        db.commit()
    return {"mentions_deleted": deleted, "mentions_updated": 0,
            "files_deleted": files, "visuals_refreshed": 0}


def _scrub_deleted_keywords(db: Session) -> dict:
    """Strip keyword labels that no longer exist in the watchlist; drop empty mentions.
    Rebuild visuals on rows that keep other keywords so old highlights disappear."""
    known = _known_keyword_fold(db)
    deleted = updated = files = refreshed = 0
    need_web_backfill = False
    rows = db.execute(select(Mention)).scalars().all()
    for m in rows:
        kws = list(m.matched_keywords or [])
        remaining = [k for k in kws if (k or "").casefold() in known]
        if len(remaining) == len(kws):
            continue
        if remaining:
            m.matched_keywords = remaining
            updated += 1
            if _refresh_mention_visuals(db, m):
                refreshed += 1
                if m.module == "newspaper":
                    need_web_backfill = True
            continue
        paths = {
            m.screenshot_path, m.full_screenshot_path,
            *(m.keyword_media or {}).values(),
        }
        for path in paths:
            if _unlink_orphan_media(db, path, m.id):
                files += 1
        db.delete(m)
        deleted += 1
    if deleted or updated:
        db.commit()
    if need_web_backfill:
        try:
            from app.newspaper.screenshots import backfill_screenshots
            backfill_screenshots(limit=40)
        except Exception as exc:
            logger.warning("post-scrub screenshot backfill failed: %s", exc)
    return {"mentions_deleted": deleted, "mentions_updated": updated,
            "files_deleted": files, "visuals_refreshed": refreshed}


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if (dt and dt.tzinfo is None) else dt


def _home_redirect(extra: dict | None = None) -> RedirectResponse:
    q = urlencode({k: str(v) for k, v in (extra or {}).items() if v is not None})
    return RedirectResponse("/" + (f"?{q}" if q else ""), status_code=303)


def _results_section_html(
    db: Session,
    *,
    show_date,
    keyword: str,
    selected_set: set[str],
    results_scanning: bool,
    max_results: int | None = None,
) -> tuple[str, str]:
    """Build the Results panel HTML and a cheap change-detection signature."""
    active_fold = _active_keyword_fold(db, module="newspaper")
    first_date = show_date - timedelta(
        days=settings.keyword_search_days - 1
    )
    day_start = datetime(first_date.year, first_date.month, first_date.day, tzinfo=_PKT)
    day_end = datetime(
        show_date.year, show_date.month, show_date.day, tzinfo=_PKT
    ) + timedelta(days=1)
    start_utc = day_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = day_end.astimezone(timezone.utc).replace(tzinfo=None)

    mentions = db.execute(
        select(Mention).where(
            Mention.module.in_(("newspaper", "epaper")),
            or_(
                and_(
                    Mention.published_at.is_not(None),
                    Mention.published_at >= start_utc,
                    Mention.published_at < end_utc,
                ),
                and_(
                    Mention.published_at.is_(None),
                    Mention.detected_at >= start_utc,
                    Mention.detected_at < end_utc,
                ),
            )
        ).order_by(Mention.detected_at.desc())
    ).scalars().all()

    if selected_set:
        mentions = [m for m in mentions if (m.source or "") in selected_set]
    else:
        mentions = []

    if keyword:
        kw_l = keyword.casefold()
        if kw_l not in active_fold:
            mentions = []
        else:
            mentions = [
                m for m in mentions
                if any((k or "").casefold() == kw_l for k in (m.matched_keywords or []))
            ]
    else:
        mentions = [m for m in mentions if _live_matched(m, active_fold)]

    page = max(1, settings.keyword_result_limit)
    # max_results paginates: each "Show more" click asks for one more page.
    show_limit = max_results if max_results and max_results > 0 else page

    mentions.sort(key=result_policy.effective_time, reverse=True)
    total = len(mentions)
    shown = mentions[:show_limit]
    spin = ' <span class="spin" title="Scanning"></span>' if results_scanning else ""
    if shown:
        cards = []
        for m in shown:
            live = _live_matched(m, active_fold)
            if keyword:
                hl = [active_fold[keyword.casefold()]]
            else:
                hl = live
            cards.append(_detection_card(
                m, highlight_keywords=hl, scanning=results_scanning))
        grid = f'<div class="grid">{"".join(cards)}</div>'
        if total > len(shown):
            remaining = total - len(shown)
            step = min(page, remaining)
            more = (
                '<div class="more-wrap">'
                f'<button type="button" class="more-btn" id="news-more" '
                f'data-next="{len(shown) + step}">Show next {step}</button>'
                f'<span class="more-count">Showing {len(shown)} of {total}</span>'
                "</div>"
            )
        else:
            more = (f'<p class="hint" style="margin-top:.9rem">Showing {len(shown)} of '
                    f"{total}.</p>" if total > page else "")
    elif results_scanning:
        grid = '<div class="empty loading"><span class="spin"></span></div>'
        more = ""
    elif not active_fold:
        grid = ('<div class="empty">No active keywords on the watchlist yet.'
                "<br>Add keywords above, then Confirm &amp; scan.</div>")
        more = ""
    else:
        grid = (f'<div class="empty">No matches for the '
                f'{settings.keyword_search_days} days through this date '
                "and paper selection."
                "<br>Try another date or run ▶ on a keyword.</div>")
        more = ""

    max_id = max((m.id for m in shown), default=0)
    shots = sum(1 for m in shown if m.screenshot_path)
    # len(shown) in the signature so paginating (more shown, same total) counts
    # as a change and the fragment actually swaps in.
    sig = f"{total}:{len(shown)}:{max_id}:{shots}:{int(results_scanning)}"
    html_out = f"""
        <section class="results" id="results" data-sig="{html.escape(sig)}">
          <div class="results-head">
            <h2>Results{spin}</h2>
            <span class="count">{len(mentions)} match{'es' if len(mentions) != 1 else ''}</span>
          </div>
          {grid}{more}
        </section>
        """
    return html_out, sig


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


def _start_keyword_scan(kw: Keyword) -> dict:
    """Queue this keyword for a FIFO one-at-a-time scan (safe with multiple adds)."""
    st = keyword_scan_queue.enqueue(kw.id, kw.text, module=kw.module or "newspaper")
    return {
        "live_started": True,
        "queued": st.get("queued", 1),
        "articles_checked": 0,
        "pages_checked": 0,
        "mentions": 0,
    }


def _start_youtube_period_scan(
    start_date: str,
    end_date: str,
    keyword_ids: list[int],
    *,
    start_time: str = "00:00",
    end_time: str = "23:59",
    label: str | None = None,
) -> bool:
    """User-triggered scan: all non-live uploads in a date/time window."""
    from app.youtube.pipeline import period_bounds_from_parts, period_label

    try:
        p_start, p_end = period_bounds_from_parts(
            start_date, end_date, start_time, end_time,
        )
    except ValueError:
        return False
    return yt_scan_runner.start_scan(
        keyword_ids=keyword_ids or None,
        period_start=p_start.isoformat(),
        period_end=p_end.isoformat(),
        label=label or f"period:{period_label(p_start, p_end)}",
    )


def _youtube_period_from_query(qp) -> tuple[datetime | None, datetime | None, str]:
    """Parse ?start=&end= ISO bounds from the URL (UTC-aware)."""
    from app.youtube.pipeline import parse_period_iso, period_label

    start_s = (qp.get("start") or "").strip()
    end_s = (qp.get("end") or "").strip()
    if not start_s or not end_s:
        return None, None, ""
    try:
        p_start, p_end = parse_period_iso(start_s, end_s)
        return p_start, p_end, period_label(p_start, p_end)
    except ValueError:
        return None, None, ""


def _youtube_period_form_defaults(
    qp,
    *,
    today=None,
) -> dict[str, str]:
    """Default date/time fields for the custom scan modal."""
    today = today or datetime.now(_PKT).date()
    p_start, p_end, _ = _youtube_period_from_query(qp)
    if p_start and p_end:
        s = p_start.astimezone(_PKT)
        e = p_end.astimezone(_PKT)
        return {
            "start_date": s.date().isoformat(),
            "end_date": e.date().isoformat(),
            "start_time": s.strftime("%H:%M"),
            "end_time": e.strftime("%H:%M"),
        }
    week_ago = today - timedelta(days=6)
    return {
        "start_date": week_ago.isoformat(),
        "end_date": today.isoformat(),
        "start_time": "00:00",
        "end_time": "23:59",
    }


def _youtube_keyword_langs(
    db: Session,
    keyword_ids: list[int] | None = None,
) -> dict[str, str]:
    q = select(Keyword.text, Keyword.language).where(Keyword.module == "youtube")
    if keyword_ids:
        q = q.where(Keyword.id.in_(keyword_ids))
    return {t: lang or "ur" for t, lang in db.execute(q).all() if t}


def _youtube_verified_labels(
    m: Mention,
    keyword_langs: dict[str, str],
    active_fold: dict[str, str],
    allowed_fold: set[str] | None = None,
) -> list[str]:
    from app.youtube.matcher import mention_verified_keywords

    labels = mention_verified_keywords(
        m.matched_keywords,
        m.keyword_hits,
        keyword_langs,
        active_fold=active_fold,
    )
    if allowed_fold is None:
        return labels
    return [k for k in labels if (k or "").casefold() in allowed_fold]


def _filter_youtube_mentions(
    mentions: list[Mention],
    keyword_langs: dict[str, str],
    active_fold: dict[str, str],
    allowed_fold: set[str] | None = None,
) -> list[Mention]:
    return [
        m for m in mentions
        if _youtube_verified_labels(m, keyword_langs, active_fold, allowed_fold)
    ]


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


def _dedupe_youtube_mentions(mentions: list[Mention]) -> list[Mention]:
    """One card per video — keep the newest row."""
    best: dict[str, Mention] = {}
    for m in mentions:
        key = (m.external_id or "").strip() or f"id:{m.id}"
        prev = best.get(key)
        if prev is None or result_policy.effective_time(m) > result_policy.effective_time(prev):
            best[key] = m
    out = list(best.values())
    out.sort(key=result_policy.effective_time, reverse=True)
    return out


def _youtube_keyword_ids_from_query(
    db: Session,
    *,
    kw_id_params: list[str],
    keyword: str,
    module: str = "youtube",
) -> list[int]:
    ids = [int(x) for x in kw_id_params if str(x).isdigit()]
    if ids:
        return ids
    if keyword:
        row = db.execute(
            select(Keyword.id).where(
                Keyword.active.is_(True),
                Keyword.module == module,
                func.lower(Keyword.text) == keyword.casefold(),
            )
        ).scalar_one_or_none()
        if row:
            return [int(row)]
    return []


_instant_match_lock = threading.Lock()

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


def start_instant_youtube_match(keyword_ids: list[int]) -> None:
    """Match new keywords against stored transcripts without waiting for a scan.

    The scan runner allows one subprocess at a time, so while a bulletin scan is
    working through its backlog — which is most of the time — a request to match
    is refused and dropped. A newly added keyword would then find nothing until
    some later scan happened to re-match it.

    Matching cached transcripts needs no download, so it runs in-process on a
    worker thread instead and cannot be blocked by that lock.
    """
    if not keyword_ids:
        return

    def _run() -> None:
        # One at a time: several adds in quick succession should queue, not
        # pile up concurrent passes over the same transcripts.
        with _instant_match_lock:
            try:
                from app.youtube.pipeline import run_quick_youtube_match

                summary = run_quick_youtube_match(keyword_ids)
                logger.info("instant youtube match done: %s", summary)
            except Exception:
                logger.exception("instant youtube match failed")

    threading.Thread(target=_run, name="yt-instant-match", daemon=True).start()


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
    papers_all = _paper_names()
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
    boxes = ""
    for name in papers_all:
        chk = " checked" if name in selected_set else ""
        boxes += (
            f'<label><input type="checkbox" name="paper" value="{html.escape(name)}"{chk}>'
            f"{html.escape(name)}</label>"
        )

    banner = ""
    q_st = keyword_scan_queue.status()
    queued_ids = {
        int(x["id"])
        for x in list(q_st.get("batch") or []) + list(q_st.get("pending") or [])
        if x.get("id") is not None
    }
    queued_folds = {
        (x.get("text") or "").casefold()
        for x in list(q_st.get("batch") or []) + list(q_st.get("pending") or [])
        if x and x.get("text")
    }
    # Only the keyword queue drives chip/results spinners — scheduled crawls
    # must not leave the UI spinning forever.
    results_scanning = bool(q_st.get("running"))
    if qp.get("removed"):
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

    results_html = ""
    # Drop labels for keywords that were deleted earlier (before purge existed).
    _scrub_deleted_keywords(db)
    active_fold = _active_keyword_fold(db, module="newspaper")

    if searched:
        results_html, _ = _results_section_html(
            db,
            show_date=show_date,
            keyword=keyword,
            selected_set=selected_set,
            results_scanning=results_scanning,
        )
    else:
        # Always ship a #results container, even empty, so clicking a keyword can
        # swap results in place instead of finding nothing to update.
        results_html = (
            '<section class="results" id="results">'
            '<div class="empty">Pick a date, type a keyword if you like, choose '
            "newspapers, then show results — or click a keyword above to filter "
            "what's already stored.</div></section>"
        )

    active_kws = db.execute(
        select(Keyword).where(
            Keyword.active.is_(True), Keyword.module == "newspaper"
        ).order_by(Keyword.text)
    ).scalars().all()
    kw_l = keyword.casefold()

    def _kw_chip(k: Keyword) -> str:
        on = " on" if kw_l == k.text.casefold() else ""
        this_busy = (
            k.id in queued_ids
            or k.text.casefold() in queued_folds
        )
        busy = " busy" if this_busy else ""
        play = (
            '<span class="kw-play" title="Scanning" aria-label="Scanning">'
            '<span class="spin"></span></span>'
            if this_busy else
            f'<form class="kw-play-form" method="post" action="/ui/keywords/{k.id}/scan">'
            f'<button type="submit" class="kw-play" title="Scan this keyword now" '
            f'aria-label="Scan">▶</button></form>'
        )
        return (
            f'<span class="kw-chip{on}{busy}">'
            f'<button type="button" class="kw-pick" data-kw-id="{k.id}" '
            f'data-kw="{html.escape(k.text, quote=True)}">{html.escape(k.text)}</button>'
            f'{play}'
            f'<form class="kw-del" method="post" action="/ui/keywords/{k.id}/delete" '
            f"onsubmit=\"return confirm('Hide “{html.escape(k.text, quote=True)}” from the watchlist? Its results stay retained for 90 days.')\">"
            f'<button type="submit" class="kw-x" title="Remove" aria-label="Remove">'
            f'×</button></form></span>'
        )

    kw_tags = (
        f'<button type="button" class="kw-pick kw-all{" on" if not keyword else ""}" data-kw="">All</button>'
        + "".join(_kw_chip(k) for k in active_kws)
    )

    body = f"""
    {banner}
    <div class="hero">
      <h1>Find coverage</h1>
      <p>Filter by date, keyword, and newspapers. Scheduled scans keep filling this quietly.</p>
    </div>
    <div class="panel">
      <h2>Search</h2>
      <div class="field">
        <label for="date">Date</label>
        <input form="search" type="date" id="date" name="date" value="{html.escape(date_s)}" required>
      </div>
      <div class="field">
        <label>Add to watchlist</label>
        <div class="kw-add" id="kw-draft-row">
          <input id="kw-draft-text" type="text" placeholder="Type a keyword, then Add" maxlength="120">
          <select id="kw-draft-lang"><option value="en">EN</option><option value="ur">UR</option></select>
          <button type="button" id="kw-add-btn">Add</button>
        </div>
        <form id="kw-confirm" method="post" action="/ui/keywords/batch" style="display:none">
          <input type="hidden" name="texts" id="kw-pending-texts" value="">
          <input type="hidden" name="language" id="kw-pending-lang" value="en">
          <input type="hidden" name="scan" value="1">
        </form>
        <p class="hint" style="margin-top:.45rem">Adds the keyword and matches it against everything already stored.</p>
        <div class="kw-bar">
          <div class="cap">Watchlist · click to filter · ▶ scan · × hide</div>
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
          <button type="submit">Show results</button>
          <a class="btn ghost" href="/">Reset</a>
        </div>
        <p class="hint">Jobs and schedules are unchanged — this page only browses what they already found.</p>
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
    """YouTube bulletin monitoring workspace (separate keyword watchlist)."""
    if not settings.youtube_enabled:
        return RedirectResponse("/", status_code=303)
    from app.youtube.pipeline import bulletin_status_for_date, ensure_due_bulletins, repair_youtube_mentions

    today = datetime.now(_PKT).date()
    qp = request.query_params
    keyword = (qp.get("q") or "").strip()
    search_requested = bool(qp.get("go"))
    filter_only = bool(qp.get("filter"))
    selected_kw_ids = [int(x) for x in qp.getlist("kw_id") if str(x).isdigit()]
    period_start, period_end, period_label_s = _youtube_period_from_query(qp)
    period_defaults = _youtube_period_form_defaults(qp, today=today)
    ensure_due_bulletins(db, for_date=today.isoformat())
    if search_requested:
        repair_youtube_mentions(db)

    active_kws = db.execute(
        select(Keyword).where(
            Keyword.active.is_(True), Keyword.module == "youtube"
        ).order_by(Keyword.text)
    ).scalars().all()

    status_rows = bulletin_status_for_date(db, today)
    status_html = '<div class="yt-status">' + "".join(
        f'<div class="cell"><b>{html.escape(r["channel"])}</b>'
        f'<span class="st {html.escape((r["status"] or "").split("/")[0].split()[0])}">{html.escape(r["status"] or "waiting")}</span>'
        f'<div class="hint" style="margin:0">{html.escape(r.get("slot") or "—")}'
        + (f' · {html.escape((r.get("title") or "")[:48])}' if r.get("title") else "")
        + "</div></div>"
        for r in status_rows
    ) + "</div>"

    q_st = keyword_scan_queue.status()
    yt_st = yt_scan_runner.status()
    queue_items = list(q_st.get("batch") or []) + list(q_st.get("pending") or [])
    yt_queue = [x for x in queue_items if (x.get("module") or "newspaper") == "youtube"]
    results_scanning = bool(yt_st.get("running") or yt_queue) and not filter_only
    selected_kw_set = set(selected_kw_ids)
    selected_kw_labels = [k.text for k in active_kws if k.id in selected_kw_set]
    results_html, _ = _youtube_results_html(
        db,
        keyword=keyword,
        keyword_ids=selected_kw_ids or None,
        period_start=period_start,
        period_end=period_end,
        period_label=period_label_s,
        strict=search_requested,
        filter_only=filter_only,
        selected_kw_labels=selected_kw_labels,
        results_scanning=results_scanning,
    )

    queued_ids = {
        int(x["id"])
        for x in yt_queue
        if x.get("id") is not None
    }
    queued_folds = {
        (x.get("text") or "").casefold()
        for x in yt_queue
        if x and x.get("text")
    }

    def _kw_chip(k: Keyword) -> str:
        sel = " sel" if k.id in selected_kw_set else ""
        this_busy = k.id in queued_ids or k.text.casefold() in queued_folds
        busy = " busy" if this_busy else ""
        return (
            f'<span class="kw-chip{sel}{busy}" data-kw-id="{k.id}">'
            f'<button type="button" class="kw-toggle" '
            f'data-kw="{html.escape(k.text, quote=True)}">{html.escape(k.text)}</button>'
            f'<form class="kw-del" method="post" action="/ui/keywords/{k.id}/delete" '
            f"onsubmit=\"return confirm('Hide “{html.escape(k.text, quote=True)}” from the YouTube watchlist? Results stay retained for 90 days.')\">"
            f'<button type="submit" class="kw-x" title="Remove" aria-label="Remove">'
            f'×</button></form></span>'
        )

    kw_tags = "".join(_kw_chip(k) for k in active_kws)

    banner = ""
    if qp.get("removed"):
        banner = (
            f'<div class="banner ok">Hidden <b>{html.escape(qp.get("removed"))}</b> from the '
            "YouTube watchlist. Results remain retained for 90 days.</div>"
        )
    elif qp.get("added"):
        banner = (
            f'<div class="banner ok">Added <b>{html.escape(qp.get("added"))}</b> to the YouTube '
            "watchlist — use Custom scan to search a time period.</div>"
        )
    elif qp.get("channel_added"):
        banner = (
            f'<div class="banner ok">Added channel <b>{html.escape(qp.get("channel_added"))}</b> '
            "with auto-detected bulletin slots. Daily bulletin scans run on schedule.</div>"
        )
    elif qp.get("scan_started"):
        banner = (
            f'<div class="banner ok">Scanning <b>{html.escape(qp.get("scan_started"))}</b> — '
            "all non-live uploads in that window will be transcribed and matched.</div>"
        )

    channels = db.execute(
        select(YouTubeChannel).where(YouTubeChannel.active.is_(True)).order_by(YouTubeChannel.name)
    ).scalars().all()
    ch_tags = "".join(
        f'<span class="ch-tag">{html.escape(c.name)}</span>' for c in channels
    ) or '<span class="hint">No channels yet.</span>'

    p_start_iso = period_start.isoformat() if period_start else ""
    p_end_iso = period_end.isoformat() if period_end else ""

    body = f"""
    {banner}
    <div class="hero">
      <h1>YouTube bulletins</h1>
      <p>Daily bulletin slots are scanned automatically on schedule. Use <b>Custom scan</b> to pick a
      date/time range — every non-live upload in that window is transcribed and matched to your keywords.</p>
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
        <p class="hint" style="margin-top:.45rem">Adding searches transcripts already stored — no rescan needed.
        Click keywords to select (✓), then open <b>Custom scan</b> or <b>Show results</b>.</p>
        <div class="kw-bar">
          <div class="cap">Watchlist · click to select · × hide
            <button type="button" class="ghost" id="kw-sel-all" style="margin-left:.5rem;font-size:.72rem">Select all</button>
            <button type="button" class="ghost" id="kw-sel-none" style="font-size:.72rem">Clear</button>
          </div>
          <div class="kw-tags">{kw_tags or '<span class="hint">No keywords yet — add some above.</span>'}</div>
        </div>
      </div>
      <div class="field">
        <label>Today's bulletin auto-scan</label>
        <p class="hint" style="margin:.25rem 0 .5rem">Scheduled scans pick up each channel's daily bulletin slots automatically.</p>
        {status_html}
      </div>
      <form method="get" action="/youtube" id="yt-search">
        <div id="yt-kw-hidden"></div>
        <input type="hidden" name="q" id="q" value="">
        <input type="hidden" name="go" value="1">
        <input type="hidden" name="filter" id="yt-filter" value="{html.escape(qp.get("filter") or "")}">
        <input type="hidden" name="start" id="yt-period-start" value="{html.escape(p_start_iso)}">
        <input type="hidden" name="end" id="yt-period-end" value="{html.escape(p_end_iso)}">
        <div class="actions">
          <button type="button" id="yt-period-open">Custom scan…</button>
          <a class="btn ghost" href="/youtube">Reset</a>
        </div>
      </form>
    </div>
    {results_html}
    <div id="yt-period-modal" role="dialog" aria-modal="true" aria-labelledby="yt-period-title">
      <div class="box">
        <h3 id="yt-period-title">Custom scan period</h3>
        <p class="sub">Pick a date and time range (Pakistan time). Every non-live upload published in that window
        on all channels will be transcribed and matched to the keywords you selected on the watchlist.</p>
        <form method="post" action="/ui/scan/youtube/period" id="yt-period-form">
          <div id="yt-period-kw-hidden"></div>
          <div class="period-grid">
            <div><label for="yt-p-start-date">From date</label>
              <input type="date" id="yt-p-start-date" name="start_date"
                value="{html.escape(period_defaults["start_date"])}" required></div>
            <div><label for="yt-p-end-date">To date</label>
              <input type="date" id="yt-p-end-date" name="end_date"
                value="{html.escape(period_defaults["end_date"])}" required></div>
            <div><label for="yt-p-start-time">From time</label>
              <input type="time" id="yt-p-start-time" name="start_time"
                value="{html.escape(period_defaults["start_time"])}" required></div>
            <div><label for="yt-p-end-time">To time</label>
              <input type="time" id="yt-p-end-time" name="end_time"
                value="{html.escape(period_defaults["end_time"])}" required></div>
          </div>
          <div class="row-btns">
            <button type="button" id="yt-period-run">Scan period</button>
            <button type="button" class="ghost" id="yt-period-close">Close</button>
          </div>
          <div id="yt-period-result"></div>
        </form>
      </div>
    </div>
    <div id="yt-ch-modal" role="dialog" aria-modal="true" aria-labelledby="yt-ch-title">
      <div class="box">
        <h3 id="yt-ch-title">Add YouTube channel</h3>
        <p class="sub">Paste a channel URL or @handle. We scan recent uploads and suggest up to five daily bulletin times.</p>
        <input type="url" id="yt-ch-url" placeholder="https://www.youtube.com/@ChannelName" autocomplete="off">
        <div class="row-btns">
          <button type="button" id="yt-ch-check">Find bulletins</button>
          <button type="button" id="yt-ch-save" style="display:none">Add channel</button>
          <button type="button" class="ghost" id="yt-ch-close">Close</button>
        </div>
        <ul id="yt-ch-slots"></ul>
        <div id="yt-ch-result"></div>
      </div>
    </div>
    """
    return _shell("YouTube · Media Monitor", body, module="youtube")


def _youtube_results_html(
    db: Session,
    *,
    keyword: str,
    keyword_ids: list[int] | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    period_label: str = "",
    strict: bool = False,
    filter_only: bool = False,
    selected_kw_labels: list[str] | None = None,
    results_scanning: bool,
    max_results: int | None = None,
) -> tuple[str, str]:
    active_fold = _active_keyword_fold(db, module="youtube")
    today = datetime.now(_PKT).date()
    if period_start and period_end:
        start_utc = period_start.replace(tzinfo=None) if period_start.tzinfo else period_start
        end_utc = period_end.replace(tzinfo=None) if period_end.tzinfo else period_end
    else:
        first_date = today - timedelta(days=settings.keyword_search_days - 1)
        day_start = datetime(first_date.year, first_date.month, first_date.day, tzinfo=_PKT)
        day_end = datetime(today.year, today.month, today.day, tzinfo=_PKT) + timedelta(days=1)
        start_utc = day_start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = day_end.astimezone(timezone.utc).replace(tzinfo=None)

    mentions = db.execute(
        select(Mention).where(
            Mention.module == "youtube",
            or_(
                and_(
                    Mention.published_at.is_not(None),
                    Mention.published_at >= start_utc,
                    Mention.published_at <= end_utc,
                ),
                and_(
                    Mention.published_at.is_(None),
                    Mention.detected_at >= start_utc,
                    Mention.detected_at <= end_utc,
                ),
            ),
        ).order_by(Mention.detected_at.desc())
    ).scalars().all()

    allowed_labels: set[str] | None = None
    keyword_langs: dict[str, str] = {}
    allowed_fold: set[str] | None = None
    paused_labels: list[str] = []
    if strict and not keyword_ids:
        mentions = []
        allowed_labels = set()
    elif keyword_ids:
        rows = db.execute(
            select(Keyword.text, Keyword.language, Keyword.active).where(
                Keyword.id.in_(keyword_ids),
                Keyword.module == "youtube",
            )
        ).all()
        allowed_labels = {t for t, _, _ in rows if t}
        keyword_langs = {t: lang or "ur" for t, lang, _ in rows if t}
        allowed_fold = {t.casefold() for t in allowed_labels}
        # A paused keyword is never matched, so it can only ever come back
        # empty. Saying "run a Custom scan" here sends the user to rescan
        # bulletins that would still be searched without it.
        paused_labels = [t for t, _, act in rows if t and not act]
    elif keyword:
        kw_l = keyword.casefold()
        if kw_l not in active_fold:
            mentions = []
        else:
            allowed_fold = {kw_l}

    if not keyword_langs:
        keyword_langs = _youtube_keyword_langs(db)

    if mentions:
        mentions = _filter_youtube_mentions(
            mentions, keyword_langs, active_fold, allowed_fold,
        )

    page = max(1, settings.keyword_result_limit)
    # max_results paginates: each "Show more" click asks for one more page.
    show_limit = max_results if max_results and max_results > 0 else page

    mentions.sort(key=result_policy.effective_time, reverse=True)
    mentions = _dedupe_youtube_mentions(mentions)
    total = len(mentions)
    shown = mentions[:show_limit]
    spin = ' <span class="spin" title="Scanning"></span>' if results_scanning else ""

    period_hint = ""
    if period_label:
        period_hint = f'<p class="hint results-filter-hint">Period: <b>{html.escape(period_label)}</b></p>'

    filter_hint = ""
    if selected_kw_labels:
        kw_line = html.escape(", ".join(selected_kw_labels[:6]))
        if len(selected_kw_labels) > 6:
            kw_line += html.escape(f" +{len(selected_kw_labels) - 6}")
        filter_hint = (
            f'<p class="hint results-filter-hint">Exact transcript match only for: '
            f"<b>{kw_line}</b> · one result per video</p>"
        )
    elif strict:
        filter_hint = (
            '<p class="hint results-filter-hint">Select keyword(s) on the watchlist, '
            "then click <b>Show results</b> or run <b>Custom scan</b>.</p>"
        )

    show_results_btn = (
        '<button type="submit" form="yt-search" id="yt-show-results" '
        'title="Show exact matches for selected keywords (no rescan)">Show results</button>'
    )
    if shown:
        cards = []
        for m in shown:
            if allowed_labels:
                fold_filter = {(t or "").casefold() for t in allowed_labels}
                hl = _youtube_verified_labels(m, keyword_langs, active_fold, fold_filter)
            elif keyword:
                hl = _youtube_verified_labels(
                    m, keyword_langs, active_fold, {keyword.casefold()},
                )
            else:
                hl = _youtube_verified_labels(m, keyword_langs, active_fold)
            cards.append(_detection_card(
                m, highlight_keywords=hl, scanning=results_scanning,
                keyword_langs=keyword_langs,
            ))
        grid = f'<div class="grid">{"".join(cards)}</div>'
        if total > len(shown):
            remaining = total - len(shown)
            step = min(page, remaining)
            more = (
                '<div class="more-wrap">'
                f'<button type="button" class="more-btn" id="yt-more" '
                f'data-next="{len(shown) + step}">Show next {step}</button>'
                f'<span class="more-count">Showing {len(shown)} of {total}</span>'
                "</div>"
            )
        else:
            more = (f'<p class="hint" style="margin-top:.9rem">Showing {len(shown)} of '
                    f"{total}.</p>" if total > page else "")
    elif results_scanning:
        grid = '<div class="empty loading"><span class="spin"></span></div>'
        more = ""
    elif not active_fold:
        grid = ('<div class="empty">No YouTube keywords yet.'
                "<br>Add keywords above, then run Custom scan.</div>")
        more = ""
    elif strict and not keyword_ids:
        grid = ('<div class="empty">Select one or more keywords from the watchlist '
                '(click to mark ✓), then <b>Show results</b> or <b>Custom scan</b>.</div>')
        more = ""
    elif keyword_ids and paused_labels and not shown:
        names = ", ".join(html.escape(t) for t in sorted(paused_labels))
        grid = (f'<div class="empty">Paused: <b>{names}</b>.'
                "<br>Paused keywords are never matched. Resume on the watchlist "
                "above, and past bulletins are searched straight away.</div>")
        more = ""
    elif filter_only and keyword_ids and not shown:
        grid = ('<div class="empty">No exact matches for the selected keyword(s) in this period.'
                "<br>Run <b>Custom scan</b> to transcribe uploads in the window.</div>")
        more = ""
    elif strict and period_label:
        grid = (f'<div class="empty">No matches in <b>{html.escape(period_label)}</b> yet.'
                "<br>Scanning non-live uploads in that window — results appear as they are found.</div>")
        more = ""
    else:
        grid = ('<div class="empty">Select keywords and run <b>Custom scan</b> to search a time period, '
                "or click <b>Show results</b> to filter existing matches.</div>")
        more = ""

    max_id = max((m.id for m in shown), default=0)
    shots = sum(1 for m in shown if m.screenshot_path)
    # len(shown) is in the signature so paginating (same total, more shown)
    # counts as a change and the fragment actually swaps in.
    sig = f"yt:{len(mentions)}:{len(shown)}:{max_id}:{shots}:{int(results_scanning)}"
    html_out = f"""
        <section class="results" id="results" data-sig="{html.escape(sig)}">
          <div class="results-head">
            <h2>Results{spin}</h2>
            <span class="count">{len(mentions)} match{'es' if len(mentions) != 1 else ''}</span>
            <div class="results-actions">{show_results_btn}</div>
          </div>
          {period_hint}
          {filter_hint}
          {grid}{more}
        </section>
        """
    return html_out, sig


@app.get("/newspapers")
@app.get("/epaper")
@app.get("/mentions")
def _gone_pages():
    return RedirectResponse("/", status_code=303)


@app.get("/ui/results", response_class=HTMLResponse)
def ui_results_partial(request: Request, db: Session = Depends(get_db)):
    """Live Results panel fragment — polled while a scan fills in matches.

    Pass ``module=youtube`` (or be on the YouTube page) so newspaper cards never
    replace the YouTube results grid.
    """
    today = datetime.now(_PKT).date()
    qp = request.query_params
    module = (qp.get("module") or "newspaper").strip().lower()
    if module not in ("newspaper", "youtube"):
        module = "newspaper"
    date_s = (qp.get("date") or today.isoformat()).strip()
    try:
        show_date = datetime.strptime(date_s, "%Y-%m-%d").date()
    except ValueError:
        show_date = today
    keyword = (qp.get("q") or qp.get("kw") or "").strip()

    q_st = keyword_scan_queue.status()
    queue_items = list(q_st.get("batch") or []) + list(q_st.get("pending") or [])

    if module == "youtube":
        yt_st = yt_scan_runner.status() if settings.youtube_enabled else {"running": False}
        selected_kw_ids = [int(x) for x in qp.getlist("kw_id") if str(x).isdigit()]
        period_start, period_end, period_label_s = _youtube_period_from_query(qp)
        filter_only = bool(qp.get("filter"))
        results_scanning = bool(
            yt_st.get("running")
            or any((x.get("module") or "newspaper") == "youtube" for x in queue_items)
        ) and not filter_only
        sel_labels = []
        if selected_kw_ids:
            sel_labels = list(db.execute(
                select(Keyword.text).where(
                    Keyword.id.in_(selected_kw_ids), Keyword.module == "youtube",
                )
            ).scalars().all())
        html_out, sig = _youtube_results_html(
            db,
            keyword=keyword,
            keyword_ids=selected_kw_ids or None,
            period_start=period_start,
            period_end=period_end,
            period_label=period_label_s,
            strict=bool(qp.get("go")),
            filter_only=filter_only,
            selected_kw_labels=sel_labels or None,
            results_scanning=results_scanning,
            max_results=int(qp.get("ymax")) if str(qp.get("ymax") or "").isdigit() else None,
        )
        return HTMLResponse(html_out, headers={"X-Results-Sig": sig, "Cache-Control": "no-store"})

    papers_all = _paper_names()
    selected = qp.getlist("paper")
    if not selected and "paper" not in qp:
        selected = list(papers_all)
    selected_set = set(selected)

    results_scanning = bool(
        any((x.get("module") or "newspaper") != "youtube" for x in queue_items)
    )
    html_out, sig = _results_section_html(
        db,
        show_date=show_date,
        keyword=keyword,
        selected_set=selected_set,
        results_scanning=results_scanning,
        max_results=int(qp.get("nmax")) if str(qp.get("nmax") or "").isdigit() else None,
    )
    return HTMLResponse(html_out, headers={"X-Results-Sig": sig, "Cache-Control": "no-store"})


# ==========================================================================
# UI actions (same behaviour; land back on the single page)
# ==========================================================================
@app.post("/ui/keywords")
def ui_add_keyword(text: str = Form(...), language: str = Form("en"),
                   db: Session = Depends(get_db)):
    """Legacy single-add: create/reactivate and enqueue one keyword."""
    kw = _upsert_watch_keyword(db, text, language)
    if not kw:
        return RedirectResponse("/", status_code=303)
    today = datetime.now(_PKT).date().isoformat()
    _start_keyword_scan(kw)
    return _home_redirect({
        "q": kw.text,
        "go": "1",
        "date": today,
        "scanning": "1",
    })


@app.post("/ui/keywords/batch")
def ui_batch_keywords(texts: str = Form(...), language: str = Form("en"),
                      module: str = Form("newspaper"),
                      date: str = Form(""),
                      scan: str = Form("1"),
                      db: Session = Depends(get_db)):
    """Create/reactivate many keywords; optionally scan them."""
    module = module if module in ("newspaper", "youtube") else "newspaper"
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

    if module == "youtube":
        # Match the new keyword against transcripts already on disk before the
        # 15-minute scan comes round. This costs no download and no Groq call,
        # so it runs for "Add only" as well — gating it behind the scan button
        # left a saved keyword reading "no results" until some later scan
        # happened to pick it up.
        start_instant_youtube_match(ids)
        q = urlencode({"added": label, "scanning": "1"})
        return RedirectResponse(f"{home}?{q}", status_code=303)

    if not do_scan:
        q = urlencode({"added": label, "date": slot_date})
        return RedirectResponse(f"{home}?{q}", status_code=303)

    keyword_scan_queue.enqueue_many([(k.id, k.text) for k in created], module=module)
    q = urlencode({
        "q": first.text,
        "go": "1",
        "date": datetime.now(_PKT).date().isoformat(),
        "scanning": "1",
    })
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
def ui_delete_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if not kw:
        return RedirectResponse("/", status_code=303)
    text = kw.text
    module = kw.module or "newspaper"
    kw.active = False
    db.commit()
    home = "/youtube" if module == "youtube" else "/"
    q = urlencode({
        "removed": text,
        "go": "1",
        "date": datetime.now(_PKT).date().isoformat(),
    })
    return RedirectResponse(f"{home}?{q}", status_code=303)


@app.post("/api/keywords/{kid}/match")
def api_match_keyword(kid: int, db: Session = Depends(get_db)):
    """Match one keyword against already-stored article + e-paper text and return
    at once. Fired when a newspaper keyword is clicked so results reflect the
    stored data even for a keyword that was never scanned. No redirect — the
    page refreshes the results fragment itself."""
    kw = db.get(Keyword, kid)
    if not kw or (kw.module or "newspaper") == "youtube":
        raise HTTPException(404, "keyword not found")
    _start_keyword_scan(kw)
    return {"started": True, "keyword": kw.text}


@app.post("/ui/keywords/{kid}/scan")
def ui_scan_keyword(kid: int, date: str = Form(""), db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if not kw:
        raise HTTPException(404, "keyword not found")
    home = "/youtube" if (kw.module or "newspaper") == "youtube" else "/"
    slot_date = (date or datetime.now(_PKT).date().isoformat()).strip()
    if (kw.module or "newspaper") == "youtube":
        q = urlencode({"added": kw.text})
        return RedirectResponse(f"{home}?{q}", status_code=303)
    _start_keyword_scan(kw)
    q = urlencode({
        "q": kw.text,
        "go": "1",
        "date": slot_date,
        "scanning": "1",
    })
    return RedirectResponse(f"{home}?{q}", status_code=303)


@app.post("/ui/scan")
def ui_scan_all():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    scan_runner.start_scan()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/scan/newspaper")
def ui_scan_newspapers():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    return RedirectResponse("/", status_code=303)


@app.post("/ui/scan/youtube/period")
def ui_scan_youtube_period(
    start_date: str = Form(...),
    end_date: str = Form(...),
    start_time: str = Form("00:00"),
    end_time: str = Form("23:59"),
    kw_id: list[str] = Form(default=[]),
):
    """User-triggered scan of all non-live uploads in a custom date/time window."""
    if not settings.youtube_enabled:
        return RedirectResponse("/", status_code=303)
    from app.youtube.pipeline import period_bounds_from_parts, period_label

    kw_ids = [int(x) for x in kw_id if str(x).isdigit()]
    if not kw_ids:
        return RedirectResponse("/youtube?period_error=keywords", status_code=303)
    try:
        p_start, p_end = period_bounds_from_parts(
            start_date, end_date, start_time, end_time,
        )
    except ValueError:
        return RedirectResponse("/youtube?period_error=range", status_code=303)

    label = period_label(p_start, p_end)
    if yt_scan_runner.is_running():
        q = urlencode({"period_busy": label})
        return RedirectResponse(f"/youtube?{q}", status_code=303)

    _start_youtube_period_scan(
        start_date,
        end_date,
        kw_ids,
        start_time=start_time,
        end_time=end_time,
        label=label,
    )
    params: list[tuple[str, str]] = [
        ("go", "1"),
        ("start", p_start.isoformat()),
        ("end", p_end.isoformat()),
        ("scan_started", label),
    ]
    params += [("kw_id", str(i)) for i in kw_ids]
    return RedirectResponse(f"/youtube?{urlencode(params)}", status_code=303)


@app.post("/ui/scan/youtube")
def ui_scan_youtube():
    yt_scan_runner.start_scan(label="manual", force=True)
    return RedirectResponse("/youtube", status_code=303)


@app.post("/ui/epaper/fetch")
def ui_epaper_fetch_all():
    scan_runner.start_scan()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/epaper/fetch/{slug}")
def ui_epaper_fetch_one(slug: str):
    if slug not in sources.SOURCES:
        raise HTTPException(404, "unknown paper")
    name = sources.SOURCES[slug][0]
    scan_runner.start_scan(papers=[slug], label=name)
    return RedirectResponse("/", status_code=303)


@app.post("/ui/detections/clear")
def ui_clear_detections(db: Session = Depends(get_db)):
    db.execute(delete(Mention))
    db.commit()
    return RedirectResponse("/", status_code=303)


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
def api_save_custom_source(body: _CustomSourceIn):
    name = body.name.strip()
    if not name:
        return {"ok": False, "summary": "Enter a display name before saving."}
    if not body.url.startswith(("http://", "https://")):
        return {"ok": False, "summary": "Need a valid URL to save."}
    sources_probe.save_custom_source({
        "name": name,
        "kind": body.kind,
        "url": body.url,
        "summary": body.summary,
        "detail": body.detail,
    })
    return {"ok": True, "summary": f"Saved “{name}” to the filter list."}


@app.get("/api/keywords")
def list_keywords(module: str | None = None, db: Session = Depends(get_db)):
    q = select(Keyword).order_by(Keyword.created_at.desc())
    if module in ("newspaper", "youtube"):
        q = q.where(Keyword.module == module)
    rows = db.execute(q).scalars().all()
    return [{"id": k.id, "text": k.text, "language": k.language,
             "module": k.module, "active": k.active}
            for k in rows]


@app.get("/api/mentions")
def list_mentions(keyword: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 500))
    rows = db.execute(
        select(Mention).where(
            func.coalesce(Mention.published_at, Mention.detected_at)
            >= result_policy.search_cutoff()
        ).order_by(Mention.detected_at.desc())
    ).scalars().all()
    active = _active_keyword_fold(db)
    yt_langs = _youtube_keyword_langs(db)
    yt_active = _active_keyword_fold(db, module="youtube")
    if keyword:
        folded = keyword.casefold()
        if folded not in active:
            rows = []
        else:
            rows = [
                m for m in rows
                if (
                    m.module == "youtube"
                    and folded in {
                        (k or "").casefold()
                        for k in _youtube_verified_labels(m, yt_langs, yt_active)
                    }
                )
                or (
                    m.module != "youtube"
                    and any((label or "").casefold() == folded for label in (m.matched_keywords or []))
                )
            ]
    else:
        rows = [
            m for m in rows
            if (
                m.module == "youtube"
                and _youtube_verified_labels(m, yt_langs, yt_active)
            )
            or (m.module != "youtube" and _live_matched(m, active))
        ]
    rows.sort(key=result_policy.effective_time, reverse=True)
    rows = rows[:limit]
    return [
        {
            "id": m.id, "module": m.module, "source": m.source, "title": m.title,
            "url": m.url,
            "matched_keywords": (
                _youtube_verified_labels(m, yt_langs, yt_active)
                if m.module == "youtube" else _live_matched(m, active)
            ),
            "sentiment": m.sentiment,
            "detected_at": m.detected_at.isoformat() if m.detected_at else None,
        }
        for m in rows
    ]


@app.get("/api/epaper/pages")
def list_epaper_pages(date: str | None = None, db: Session = Depends(get_db)):
    ds = date or datetime.now(_PKT).date().isoformat()
    rows = db.execute(
        select(EPaperPage).where(EPaperPage.date == ds)
        .order_by(EPaperPage.paper, EPaperPage.page_no)
    ).scalars().all()
    return [
        {"paper": r.paper, "source": r.source, "city": r.city, "date": r.date,
         "page": r.page_no, "ocr_status": r.ocr_status, "viewer_url": r.viewer_url}
        for r in rows
    ]


@app.get("/api/version")
def app_version():
    """What code this host is actually running — for confirming a deploy."""
    return {"version": BUILD_VERSION}


@app.get("/api/scan/status")
def scan_status():
    return scan_manager.status()


@app.get("/api/scan/epaper/status")
def epaper_scan_status():
    return scan_runner.status()


@app.get("/api/scan/queue")
def keyword_queue_status():
    return keyword_scan_queue.status()


@app.get("/api/scan/youtube/status")
def youtube_scan_status():
    return yt_scan_runner.status()


@app.post("/api/scan/youtube")
def trigger_youtube_scan():
    return {"started": yt_scan_runner.start_scan(label="api", force=True)}


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
    from app.youtube.pipeline import ensure_due_bulletins

    if not body.channel_id.strip():
        return {"ok": False, "summary": "Missing channel id."}
    if not body.slots:
        return {"ok": False, "summary": "Select at least one bulletin slot."}
    existing = db.execute(
        select(YouTubeChannel).where(YouTubeChannel.channel_id == body.channel_id.strip())
    ).scalar_one_or_none()
    if existing:
        return {"ok": False, "summary": f"“{existing.name}” is already added."}
    row = channel_probe.save_channel(
        db,
        channel_id=body.channel_id.strip(),
        name=body.name.strip(),
        url=body.url.strip(),
        handle=body.handle.strip(),
        uploads_playlist_id=body.uploads_playlist_id.strip(),
        slots=[s.model_dump() for s in body.slots],
    )
    ensure_due_bulletins(db)
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


@app.post("/api/scan/epaper")
def trigger_epaper_scan():
    return {"started": scan_runner.start_scan()}


@app.post("/api/scan/newspaper")
def trigger_scan(keyword_ids: list[int] | None = None):
    return run_newspaper_scan(keyword_ids=keyword_ids, uncapped=True)
