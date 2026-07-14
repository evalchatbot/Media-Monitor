"""FastAPI application: the monitoring console.

Run:  uvicorn app.main:app --reload
  - Console:   http://127.0.0.1:8000/
  - API docs:  http://127.0.0.1:8000/docs

Two pipelines feed one Mention table:
  newspapers — website articles, scraped every N minutes (Playwright subprocess)
  e-paper    — each paper's daily PRINT edition: page scans fetched every
               morning, read with Claude vision, matched with the same keywords

Manual scans run as subprocesses so the UI never blocks and Playwright stays
stable; a tiny poller keeps every tab's status live and reloads once on finish.
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from config import settings
from app.db.base import SessionLocal, init_db
from app.db.models import EPaperPage, Keyword, Mention, ScrapeRun
from app.epaper import reader, scan_runner, sources
from app.newspaper import scan_manager
from app.newspaper.pipeline import run_newspaper_scan, run_quick_match
from app.scrapers.sites import SITE_CONFIGS
from app.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_PKT = timezone(timedelta(hours=5))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.scheduler_enabled:
        start_scheduler()
    # Pre-warm the ⚡ Quick Scan corpus so the first click is instant too.
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
# Design system — warm cream + deep green + orange CTA ("Neato" language)
# ==========================================================================
_CSS = """
:root{
  --bg:#f3f0e7;--surface:#ffffff;--surface-2:#f8f5ec;--surface-3:#efeadd;
  --ink:#17231c;--muted:#5b675e;--faint:#98a096;
  --line:#e8e2d2;--line-strong:#d9d2bf;
  --accent:#2f7d4f;--accent-2:#3a9160;--accent-strong:#215e3c;
  --accent-soft:#e3f0e7;--accent-border:#c4e0cf;
  --cta:#e8862e;--cta-2:#f0983f;--cta-strong:#d1741f;--cta-soft:#fbe9d5;
  --warn:#b97324;--warn-soft:#faf0dd;--warn-border:#ecd9b7;
  --crit:#c94f31;--ok:#2f7d4f;
  --glass:rgba(250,248,241,.78);--glass-brd:rgba(255,255,255,.65);
  --shadow-xs:0 1px 2px rgba(43,38,22,.05);
  --shadow-sm:0 2px 10px -4px rgba(43,48,30,.10);
  --shadow:0 16px 40px -16px rgba(40,50,30,.20);
  --shadow-lg:0 28px 60px -20px rgba(40,50,30,.28);
  --glow:0 10px 28px -8px rgba(47,125,79,.45);
  --glow-cta:0 12px 30px -10px rgba(224,121,31,.55);
  --r-lg:26px;--r:20px;--r-sm:13px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#131710;--surface:#1b2017;--surface-2:#222819;--surface-3:#2a3120;
  --ink:#edf1e4;--muted:#9aa593;--faint:#6c7663;
  --line:#2b3223;--line-strong:#3a4430;
  --accent:#5cbd82;--accent-2:#6ecb92;--accent-strong:#93d8ae;
  --accent-soft:#1d2b1f;--accent-border:#2e4a36;
  --cta:#ef9440;--cta-2:#f4a75c;--cta-strong:#f2a85f;--cta-soft:#2b1f0f;
  --warn:#e0a95a;--warn-soft:#292113;--warn-border:#4a3b1f;
  --crit:#e0714f;--ok:#5cbd82;
  --glass:rgba(24,29,20,.7);--glass-brd:rgba(255,255,255,.08);
  --shadow-xs:0 1px 2px rgba(0,0,0,.4);
  --shadow-sm:0 2px 10px -4px rgba(0,0,0,.5);
  --shadow:0 16px 40px -16px rgba(0,0,0,.65);
  --shadow-lg:0 28px 60px -20px rgba(0,0,0,.75);
  --glow:0 10px 28px -8px rgba(92,189,130,.3);
  --glow-cta:0 12px 30px -10px rgba(239,148,64,.4);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;color:var(--ink);min-height:100vh;
  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  font-size:14.5px;line-height:1.55;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  background:
    radial-gradient(60rem 30rem at 8% -10%, #e9f1e4, transparent 58%),
    radial-gradient(48rem 26rem at 102% -2%, #f8efd9, transparent 55%),
    var(--bg);
  background-attachment:fixed}
@media (prefers-color-scheme:dark){body{background:
  radial-gradient(60rem 30rem at 8% -10%, #1a2415, transparent 58%),
  radial-gradient(48rem 26rem at 102% -2%, #241d10, transparent 55%),
  var(--bg)}}
::selection{background:var(--accent-soft);color:var(--accent-strong)}
a{color:inherit}
h1,h2,h3,.display{font-family:"Sora","Inter",sans-serif}

/* Ambient drifting blobs */
body::before,body::after{content:"";position:fixed;z-index:-1;border-radius:50%;filter:blur(70px);opacity:.38;pointer-events:none}
body::before{width:28rem;height:28rem;left:-7rem;top:6rem;background:radial-gradient(circle,#bfe3c6,transparent 70%);animation:drift1 26s ease-in-out infinite}
body::after{width:24rem;height:24rem;right:-6rem;bottom:3rem;background:radial-gradient(circle,#f3e2b0,transparent 70%);animation:drift2 32s ease-in-out infinite}
@keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(3.5rem,2rem) scale(1.06)}}
@keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-3rem,-2.5rem) scale(.95)}}
@media (prefers-color-scheme:dark){body::before,body::after{opacity:.10}}

/* ===== Floating rounded nav ===== */
.navwrap{position:sticky;top:.85rem;z-index:50;padding:0 1rem;margin-bottom:.4rem}
.nav{max-width:1200px;margin:0 auto;display:flex;align-items:center;gap:1rem;
  background:var(--glass);border:1px solid var(--glass-brd);border-radius:999px;
  padding:.55rem .8rem .55rem .65rem;box-shadow:var(--shadow-sm);
  backdrop-filter:blur(18px) saturate(1.8);-webkit-backdrop-filter:blur(18px) saturate(1.8);
  transition:box-shadow .3s}
.nav.scrolled{box-shadow:var(--shadow)}
.brand{display:inline-flex;align-items:center;gap:.6rem;text-decoration:none;color:var(--ink);padding-left:.25rem}
.brand .logo{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:14px;
  background:linear-gradient(135deg,var(--accent-2),var(--accent-strong));color:#fff;font-size:1.05rem;
  box-shadow:var(--glow);transition:transform .3s cubic-bezier(.34,1.56,.64,1)}
.brand:hover .logo{transform:rotate(-8deg) scale(1.06)}
.brand b{font-family:"Sora",sans-serif;font-size:1.02rem;font-weight:700;letter-spacing:-.02em;line-height:1.05;display:block}
.brand small{color:var(--muted);font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.14em}
.links{position:relative;display:flex;align-items:center;gap:.1rem;margin:0 auto}
.links a{position:relative;z-index:1;display:inline-flex;align-items:center;gap:.42rem;padding:.52rem .95rem;border-radius:999px;
  color:var(--muted);text-decoration:none;font-weight:600;font-size:.9rem;transition:color .2s}
.links a .ic{font-size:.85rem;opacity:.9;transition:transform .25s}
.links a:hover{color:var(--ink)}
.links a:hover .ic{transform:translateY(-1px) scale(1.12)}
.links a.active{color:var(--accent-strong)}
#navind{position:absolute;top:2px;bottom:2px;left:0;width:0;border-radius:999px;
  background:var(--accent-soft);border:1px solid var(--accent-border);opacity:0;z-index:0;
  transition:transform .35s cubic-bezier(.3,1.1,.3,1),width .35s cubic-bezier(.3,1.1,.3,1),opacity .2s}
.navside{display:flex;align-items:center;gap:.7rem}
.navdot{font-size:.8rem;color:var(--muted);font-weight:600;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.4rem;vertical-align:middle}
.dot.live{background:var(--ok);box-shadow:0 0 0 3px rgba(47,125,79,.16);animation:pulse 2.6s ease-in-out infinite}
.dot.busy{background:var(--cta);box-shadow:0 0 0 3px rgba(232,134,46,.2);animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

/* ===== Layout ===== */
.page{padding:1.6rem 1.4rem 3.5rem}
.container{max-width:1200px;margin:0 auto}
.hero{margin:.6rem 0 1.5rem;animation:fadeUp .55s cubic-bezier(.2,.7,.2,1) both}
.ptitle{font-size:clamp(1.55rem,3vw,2.05rem);font-weight:800;letter-spacing:-.03em;margin:0 0 .2rem;line-height:1.15}
.ptitle .swash{position:relative;white-space:nowrap}
.ptitle .swash::after{content:"";position:absolute;left:0;right:0;bottom:.06em;height:.32em;z-index:-1;border-radius:99px;
  background:linear-gradient(90deg,var(--accent-soft),var(--cta-soft));animation:swash .9s cubic-bezier(.2,.7,.2,1) .2s both;transform-origin:left}
@keyframes swash{from{transform:scaleX(0)}to{transform:scaleX(1)}}
.sub{color:var(--muted);margin:.15rem 0 0;max-width:78ch}
.sub a,.plink{color:var(--accent);text-decoration:none;font-weight:700}
.sub a:hover,.plink:hover{text-decoration:underline}

.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.25rem;box-shadow:var(--shadow-xs);margin-bottom:1.2rem}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.tile,.panel,.card,.det,.paper,.edcard{animation:fadeUp .55s cubic-bezier(.2,.7,.2,1) both}
.stagger>*:nth-child(2){animation-delay:.06s}.stagger>*:nth-child(3){animation-delay:.12s}
.stagger>*:nth-child(4){animation-delay:.18s}.stagger>*:nth-child(5){animation-delay:.24s}
.stagger>*:nth-child(6){animation-delay:.3s}.stagger>*:nth-child(7){animation-delay:.36s}
.stagger>*:nth-child(8){animation-delay:.42s}

/* ===== Buttons ===== */
button,.btn{position:relative;overflow:hidden;background:linear-gradient(135deg,var(--accent-2),var(--accent-strong));color:#fff;
  border:1px solid transparent;border-radius:999px;padding:.55rem 1.1rem;font-size:.88rem;font-weight:700;cursor:pointer;
  font-family:inherit;text-decoration:none;white-space:nowrap;display:inline-flex;align-items:center;gap:.45rem;
  transition:transform .15s cubic-bezier(.34,1.56,.64,1),box-shadow .2s,filter .2s;box-shadow:var(--shadow-xs)}
button:hover,.btn:hover{transform:translateY(-2px);box-shadow:var(--glow);filter:brightness(1.06)}
button:active,.btn:active{transform:translateY(0) scale(.98)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft),0 0 0 5px var(--accent)}
button::after,.btn::after{content:"";position:absolute;top:0;left:-80%;width:50%;height:100%;
  background:linear-gradient(100deg,transparent,rgba(255,255,255,.35),transparent);transform:skewX(-20deg);transition:left .5s}
button:hover::after,.btn:hover::after{left:130%}
button.ghost,.btn.ghost{background:var(--surface);color:var(--ink);border-color:var(--line-strong);box-shadow:none}
button.ghost:hover,.btn.ghost:hover{border-color:var(--accent);color:var(--accent-strong);background:var(--accent-soft)}
button.cta,.btn.cta{background:linear-gradient(135deg,var(--cta-2),var(--cta-strong));box-shadow:var(--glow-cta)}
button.cta:hover,.btn.cta:hover{filter:brightness(1.06);box-shadow:0 16px 34px -10px rgba(224,121,31,.6)}
button.sm,.btn.sm{padding:.38rem .8rem;font-size:.8rem}
button:disabled{opacity:.55;cursor:default;transform:none;box-shadow:none;filter:none}
.btn-lg{font-size:.95rem;padding:.75rem 1.5rem}

/* ===== Inputs ===== */
input,select{padding:.6rem .8rem;border:1px solid var(--line-strong);border-radius:999px;
  font-size:.92rem;font-family:inherit;background:var(--surface);color:var(--ink);transition:border-color .15s,box-shadow .15s}
input::placeholder{color:var(--faint)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}

/* ===== Table ===== */
table{border-collapse:collapse;width:100%}
th{text-align:left;color:var(--faint);font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:.5rem .65rem;border-bottom:1px solid var(--line)}
td{padding:.72rem .65rem;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--surface-2)}

/* ===== Pills & chips ===== */
.tag{display:inline-flex;align-items:center;background:var(--accent-soft);color:var(--accent-strong);
  border-radius:999px;padding:.16rem .6rem;font-size:.74rem;font-weight:700;margin:2px 3px 2px 0;text-decoration:none;line-height:1.4}
.tag.warn{background:var(--warn-soft);color:var(--warn)}
.chip{display:inline-block;padding:.4rem .85rem;border-radius:999px;border:1px solid var(--line-strong);
  background:var(--surface);color:var(--muted);text-decoration:none;font-size:.83rem;font-weight:600;margin:0 .35rem .45rem 0;
  transition:all .18s cubic-bezier(.34,1.56,.64,1)}
.chip:hover{border-color:var(--accent);color:var(--accent-strong);transform:translateY(-2px)}
.chip.on{background:linear-gradient(135deg,var(--accent-2),var(--accent-strong));color:#fff;border-color:transparent;box-shadow:var(--shadow-xs)}
.badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:700;padding:.14rem .55rem;border-radius:999px}
.badge.ok{background:var(--accent-soft);color:var(--accent-strong)}
.badge.off{background:var(--surface-3);color:var(--muted)}
.badge.warn{background:var(--warn-soft);color:var(--warn)}

/* ===== KPI tiles ===== */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:1rem;margin-bottom:1.2rem}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.1rem 1.15rem;box-shadow:var(--shadow-xs);
  position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s,border-color .2s}
.tile:hover{transform:translateY(-4px);box-shadow:var(--shadow);border-color:var(--accent-border)}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--accent-2),var(--accent-strong));opacity:.9}
.tile .label{color:var(--faint);font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em}
.tile .val{font-family:"Sora",sans-serif;font-size:2rem;font-weight:800;letter-spacing:-.03em;margin:.28rem 0 .12rem;line-height:1;font-variant-numeric:tabular-nums}
.tile .foot{color:var(--muted);font-size:.8rem;font-weight:500}
.tile .ic{position:absolute;top:.95rem;right:.95rem;width:36px;height:36px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  background:var(--accent-soft);color:var(--accent);font-size:1rem;transition:transform .3s cubic-bezier(.34,1.56,.64,1)}
.tile:hover .ic{transform:rotate(-8deg) scale(1.12)}

/* ===== Panels & charts ===== */
.cols{display:grid;grid-template-columns:1.6fr 1fr;gap:1.2rem;align-items:start}
@media (max-width:920px){.cols{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.2rem;box-shadow:var(--shadow-xs);margin-bottom:1.2rem}
.panel h3{margin:0 0 .12rem;font-size:1rem;font-weight:700;letter-spacing:-.015em}
.panel .cap{color:var(--faint);font-size:.8rem;margin-bottom:1rem}
.bars{display:flex;align-items:flex-end;gap:.55rem;height:138px;padding-top:.4rem}
.bars .b{flex:1;display:flex;flex-direction:column;align-items:center;gap:.4rem;height:100%;justify-content:flex-end;color:var(--faint);font-size:.7rem}
.bars .b i{width:100%;max-width:36px;background:linear-gradient(180deg,var(--accent-2),var(--accent-strong));border-radius:7px 7px 3px 3px;
  height:3px;font-style:normal;transition:height .9s cubic-bezier(.2,.7,.2,1)}
.bars .b .n{color:var(--muted);font-weight:700;font-size:.72rem;font-variant-numeric:tabular-nums}
.hbar{display:flex;align-items:center;gap:.7rem;margin:.55rem 0}
.hbar .hl{width:104px;flex:0 0 104px;font-size:.82rem;color:var(--muted);font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hbar .track{flex:1;height:9px;border-radius:99px;background:var(--surface-2);overflow:hidden}
.hbar .track i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent-2),var(--accent-strong));
  width:0;transition:width .9s cubic-bezier(.2,.7,.2,1)}
.hbar .hn{width:34px;flex:0 0 34px;text-align:right;font-weight:700;font-size:.8rem;font-variant-numeric:tabular-nums}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin-top:.8rem;font-size:.82rem;color:var(--muted)}
.legend b{color:var(--ink)}
.sdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:.4rem;vertical-align:middle}

/* ===== Activity list ===== */
.rlist{display:flex;flex-direction:column}
.ritem{display:flex;align-items:center;gap:.8rem;padding:.72rem .3rem;border-bottom:1px solid var(--line);text-decoration:none;color:inherit;
  border-radius:10px;transition:background .15s,padding .18s}
.ritem:last-child{border-bottom:none}
.ritem:hover{background:var(--surface-2);padding-left:.65rem}
.ritem:hover .rt{color:var(--accent-strong)}
.rsrc{flex:0 0 auto;font-size:.7rem;font-weight:700;color:var(--muted);background:var(--surface-2);border:1px solid var(--line);border-radius:7px;padding:.22rem .5rem;white-space:nowrap}
.rt{flex:1;min-width:0;font-weight:600;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:color .15s}
.rtime{flex:0 0 auto;color:var(--faint);font-size:.78rem}

/* ===== Detections grid ===== */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1.1rem}
.det{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;display:flex;flex-direction:column;
  box-shadow:var(--shadow-xs);transition:transform .22s cubic-bezier(.2,.7,.2,1),box-shadow .22s,border-color .22s}
.det:hover{transform:translateY(-5px);box-shadow:var(--shadow-lg);border-color:var(--accent-border)}
.det .shot{position:relative;overflow:hidden;background:var(--surface-2);border-bottom:1px solid var(--line)}
.det img{width:100%;height:205px;object-fit:cover;object-position:top center;cursor:zoom-in;display:block;transition:transform .5s cubic-bezier(.2,.7,.2,1)}
.det:hover img{transform:scale(1.04)}
.det .pagebadge{position:absolute;top:.6rem;left:.6rem;background:rgba(20,28,22,.82);color:#fff;font-size:.7rem;font-weight:700;
  border-radius:999px;padding:.2rem .6rem;backdrop-filter:blur(6px)}
.det .body{padding:.95rem 1.05rem;display:flex;flex-direction:column;gap:.5rem}
.det .ttl{font-weight:700;line-height:1.35;color:var(--ink);text-decoration:none;letter-spacing:-.01em}
.det .ttl:hover{color:var(--accent-strong)}
.det .meta{color:var(--faint);font-size:.78rem;font-weight:500}

/* ===== Newspapers page ===== */
.papers{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:1rem;margin-bottom:1.4rem}
.paper{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.05rem 1.1rem;box-shadow:var(--shadow-xs);
  transition:transform .2s,box-shadow .2s,border-color .2s;position:relative;overflow:hidden}
.paper:hover{transform:translateY(-3px);box-shadow:var(--shadow);border-color:var(--accent-border)}
.paper h4{margin:0;font-size:1rem;font-weight:700;letter-spacing:-.015em;display:flex;align-items:center;gap:.5rem}
.paper .mono{font-size:.74rem;color:var(--faint);margin:.15rem 0 .55rem}
.paper .secs{color:var(--muted);font-size:.8rem;line-height:1.5}
.paper .stat{margin-top:.6rem;display:flex;align-items:center;justify-content:space-between;font-size:.78rem;color:var(--faint)}

/* ===== E-paper page ===== */
.edcard{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:1.15rem 1.2rem;box-shadow:var(--shadow-xs);margin-bottom:1.1rem}
.edhead{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;margin-bottom:.7rem}
.edhead h4{margin:0;font-size:1.05rem;font-weight:700;letter-spacing:-.015em}
.edhead .grow{flex:1}
.pgstrip{display:flex;gap:.6rem;overflow-x:auto;padding:.2rem .1rem .5rem}
.pgstrip a{flex:0 0 auto;position:relative;border-radius:12px;overflow:hidden;border:1px solid var(--line);
  transition:transform .2s,box-shadow .2s,border-color .2s}
.pgstrip a:hover{transform:translateY(-3px) scale(1.02);box-shadow:var(--shadow);border-color:var(--accent-border)}
.pgstrip img{height:130px;display:block;background:var(--surface-2)}
.pgstrip .pn{position:absolute;bottom:.35rem;left:.35rem;background:rgba(20,28,22,.82);color:#fff;
  font-size:.66rem;font-weight:700;border-radius:99px;padding:.1rem .45rem}

/* ===== Bars & banners ===== */
.scanbar{background:linear-gradient(135deg,var(--accent-2),var(--accent-strong));color:#fff;text-align:center;padding:.55rem 1rem;font-weight:600;font-size:.86rem;
  display:flex;align-items:center;justify-content:center;gap:.5rem}
.scanbar .spin{border-color:rgba(255,255,255,.5);border-top-color:#fff}
.donebar{background:var(--accent-soft);color:var(--accent-strong);border-bottom:1px solid var(--accent-border);text-align:center;padding:.5rem 1rem;font-weight:600;font-size:.86rem;animation:fadeUp .4s both}
.banner{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn);border-radius:var(--r-sm);padding:.8rem 1rem;margin-bottom:1.2rem;font-weight:600;font-size:.88rem;animation:fadeUp .4s both}
.banner.ok{background:var(--accent-soft);border-color:var(--accent-border);color:var(--accent-strong)}
.hint{background:var(--surface-2);border:1px solid var(--line-strong);color:var(--muted);border-radius:var(--r-sm);padding:.75rem 1rem;font-size:.86rem;margin:.6rem 0 0}

/* ===== Section heading ===== */
.sechead{display:flex;align-items:center;gap:.55rem;font-size:1.05rem;font-weight:700;letter-spacing:-.015em;margin:1.7rem 0 .9rem;font-family:"Sora",sans-serif}
.sechead::before{content:"";width:4px;height:1.05rem;border-radius:99px;background:linear-gradient(180deg,var(--accent-2),var(--accent-strong))}
.sechead span{color:var(--faint);font-size:.84rem;font-weight:600}

/* ===== Lightbox — readable viewer for long pages (fit-to-WIDTH + zoom) ===== */
#lb{position:fixed;inset:0;z-index:100;background:rgba(13,17,12,.92);display:none;opacity:0;transition:opacity .22s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
#lb.open{display:block;opacity:1}
#lbscroll{position:absolute;inset:0;overflow:auto;text-align:center;overscroll-behavior:contain}
#lbwrap{display:inline-block;padding:3.4rem 1rem 2.5rem}
#lbscroll img{display:block;border-radius:10px;box-shadow:0 40px 90px -20px rgba(0,0,0,.75);
  cursor:zoom-in;user-select:none;-webkit-user-drag:none;image-rendering:auto}
#lbbar{position:fixed;top:.75rem;right:.9rem;display:flex;gap:.4rem;z-index:102;align-items:center}
#lbbar button{background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.22);color:#fff;
  border-radius:999px;padding:.4rem .85rem;font-size:.85rem;font-weight:700;box-shadow:none}
#lbbar button:hover{background:rgba(255,255,255,.25);transform:none;box-shadow:none;filter:none}
#lbpct{color:#e8efe6;font-size:.8rem;font-weight:700;min-width:3.2rem;text-align:center;font-variant-numeric:tabular-nums}
#lbhint{position:fixed;bottom:.8rem;left:50%;transform:translateX(-50%);z-index:102;color:#cfe0cc;
  font-size:.76rem;font-weight:600;background:rgba(0,0,0,.5);padding:.3rem .85rem;border-radius:999px;
  pointer-events:none;transition:opacity .5s}
@media (max-width:700px){#lbhint{display:none}}

/* ===== Misc ===== */
.row{display:flex;gap:.55rem;flex-wrap:wrap;align-items:center}
.empty{color:var(--muted);text-align:center;padding:2.6rem 1.5rem;border:1.5px dashed var(--line-strong);border-radius:var(--r);background:var(--surface)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}
.kwname{font-weight:700;font-size:.96rem;color:var(--ink);text-decoration:none}
.kwname:hover{color:var(--accent-strong)}
.count-link{color:var(--accent-strong);font-weight:700;text-decoration:none}
.count-link:hover{text-decoration:underline}
.muted-count{color:var(--faint)}
@media (max-width:860px){
  .nav{flex-wrap:wrap;border-radius:24px}
  .links{order:3;width:100%;overflow-x:auto;justify-content:flex-start;padding:.2rem 0 .1rem}
  .navdot{display:none}
  #navind{display:none}
}
"""

_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800'
          '&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">')

_NAV = [
    ("/", "Overview", "overview", "◧"),
    ("/newspapers", "Newspapers", "newspapers", "📰"),
    ("/epaper", "E-Paper", "epaper", "🗞"),
    ("/mentions", "Detections", "mentions", "◎"),
    ("/docs", "API", "api", "⚙"),
]
_TITLES = {"overview": "Overview", "newspapers": "Newspapers",
           "epaper": "E-Paper editions", "mentions": "Detections"}
_HERO_SUBS = {
    "overview": "Everything your keywords caught across Pakistan's press — live.",
    "newspapers": "The publications being watched and the keywords that watch them.",
    "epaper": "Daily print editions, fetched every morning and read page by page.",
    "mentions": "Every keyword hit — website articles and print pages side by side.",
}


# JS: nav indicator, scroll shadow, KPI count-up, chart grow-in, lightbox, scan poller
_JS = """
(function(){
  /* nav active indicator */
  function placeInd(){
    var a=document.querySelector('.links a.active'),ind=document.getElementById('navind');
    if(!a||!ind)return;
    ind.style.width=a.offsetWidth+'px';
    ind.style.transform='translateX('+a.offsetLeft+'px)';
    ind.style.opacity='1';
  }
  placeInd();window.addEventListener('resize',placeInd);
  if(document.fonts&&document.fonts.ready)document.fonts.ready.then(placeInd);

  /* nav scroll shadow */
  var nav=document.querySelector('.nav');
  window.addEventListener('scroll',function(){nav&&nav.classList.toggle('scrolled',window.scrollY>8)},{passive:true});

  /* KPI count-up */
  document.querySelectorAll('[data-count]').forEach(function(el){
    var end=parseInt(el.getAttribute('data-count'),10)||0,t0=null,D=700;
    if(end<=0){el.textContent=end;return}
    function step(t){t0=t0||t;var p=Math.min((t-t0)/D,1);p=1-Math.pow(1-p,3);
      el.textContent=Math.round(end*p);if(p<1)requestAnimationFrame(step)}
    requestAnimationFrame(step);
  });

  /* chart grow-in */
  requestAnimationFrame(function(){requestAnimationFrame(function(){
    document.querySelectorAll('.bars i[data-h]').forEach(function(el){el.style.height=el.getAttribute('data-h')+'px'});
    document.querySelectorAll('.hbar .track i[data-w]').forEach(function(el){el.style.width=el.getAttribute('data-w')+'%'});
  })});

  /* lightbox — readable viewer: fit-to-WIDTH first (long pages scroll), zoomable */
  var lb=document.createElement('div');lb.id='lb';
  lb.innerHTML='<div id="lbscroll"><div id="lbwrap"><img draggable="false"></div></div>'
    +'<div id="lbbar"><button data-z="out">−</button><span id="lbpct"></span>'
    +'<button data-z="in">+</button><button data-z="fit">Fit</button>'
    +'<button data-z="full">1:1</button><button data-z="x">✕</button></div>'
    +'<div id="lbhint">scroll to read · Ctrl+wheel or +/− to zoom · click image for 100% · Esc closes</div>';
  document.body.appendChild(lb);
  var lbimg=lb.querySelector('img'),lbscroll=lb.querySelector('#lbscroll'),
      lbpct=lb.querySelector('#lbpct'),scale=1,fitScale=1;

  function apply(){lbimg.style.width=Math.round(lbimg.naturalWidth*scale)+'px';
    lbpct.textContent=Math.round(scale*100)+'%';
    lbimg.style.cursor=scale<0.999?'zoom-in':'zoom-out'}
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
      // Fit to WIDTH (caps at natural size): headlines stay readable and long
      // pages scroll vertically — never squeezed to screen height.
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
      else if(b.dataset.z==='full'){setScale(1)}
      else closeLb();
      return;
    }
    if(e.target===lbimg){ // toggle 100% <-> fit, zooming to the click point
      if(scale<0.999)zoomAt(1/scale,e.clientX,e.clientY);else setScale(fitScale);
      return;
    }
    if(e.target.id==='lbscroll'||e.target.id==='lbwrap')closeLb();
  });
  // Plain wheel scrolls the (long) page naturally; Ctrl+wheel — which is also
  // what a trackpad pinch sends — zooms around the cursor.
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

  /* scan status poller */
  var wasScanning=__SCANNING__;
  async function poll(){
    try{
      var n=await fetch('/api/scan/status').then(function(r){return r.json()});
      var e=await fetch('/api/scan/epaper/status').then(function(r){return r.json()});
      var running=n.running||e.running;
      var bar=document.getElementById('statusbar'),side=document.getElementById('navside-state');
      if(running){
        var msg=n.running?('Scanning newspapers'+(n.keyword?' for \\u201c'+n.keyword+'\\u201d':''))
                         :('Fetching & reading e-paper'+(e.label?' \\u2014 '+e.label:''));
        bar.innerHTML='<div class="scanbar"><span class="spin"></span>'+msg+'\\u2026 you can keep browsing \\u2014 results load automatically.</div>';
        if(side)side.innerHTML='<span class="dot busy"></span>Working\\u2026';
        var b=document.getElementById('navscanbtn');if(b){b.disabled=true;b.innerHTML='<span class="spin"></span>Scanning\\u2026'}
      }else if(wasScanning){location.reload()}
      wasScanning=running;
    }catch(err){}
  }
  setInterval(poll,3000);
})();
"""


def _shell(title: str, active: str, body: str) -> str:
    news = scan_manager.status()
    ep = scan_runner.status()
    scanning = bool(news["running"] or ep["running"])

    scan_btn = (
        '<button class="cta" id="navscanbtn" disabled><span class="spin"></span>Scanning…</button>'
        if scanning
        else '<form method="post" action="/ui/scan" style="margin:0">'
             '<button class="cta" id="navscanbtn">▶ Scan all</button></form>'
    )
    nav_html = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">'
        f'<span class="ic">{ic}</span>{label}</a>'
        for href, label, key, ic in _NAV
    )
    state = ('<span class="dot busy"></span>Working…' if scanning
             else '<span class="dot live"></span>Live')
    hero_sub = _HERO_SUBS.get(active, "")
    title_words = _TITLES.get(active, "Media Monitor").split(" ")
    hero_title = (" ".join(title_words[:-1]) + f' <span class="swash">{title_words[-1]}</span>'
                  if title_words else "Media Monitor")

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{_FONTS}<style>{_CSS}</style></head><body>
<div class="navwrap"><header class="nav">
  <a class="brand" href="/"><span class="logo">📡</span>
    <span><b>Media Monitor</b><small>Press intelligence</small></span></a>
  <nav class="links"><span id="navind"></span>{nav_html}</nav>
  <span class="navside"><span class="navdot" id="navside-state">{state}</span>{scan_btn}</span>
</header></div>
<div id="statusbar">{_status_bar(news, ep)}</div>
<main class="page"><div class="container">
  <div class="hero"><h1 class="ptitle">{hero_title}</h1>
  <p class="sub">{hero_sub}</p></div>
  {body}
</div></main>
<script>{_JS.replace('__SCANNING__', 'true' if scanning else 'false')}</script>
</body></html>"""


def _status_bar(news: dict, ep: dict) -> str:
    if news["running"]:
        who = f"“{html.escape(news['keyword'])}”" if news["keyword"] else "all keywords"
        return (f'<div class="scanbar"><span class="spin"></span>'
                f"Scanning newspapers for {who}… you can keep browsing — results load automatically.</div>")
    if ep["running"]:
        who = f" — {html.escape(ep['label'])}" if ep.get("label") else ""
        return (f'<div class="scanbar"><span class="spin"></span>'
                f"Fetching &amp; reading e-paper editions{who}…</div>")
    s = news.get("last_summary")
    if s:
        who = f"“{html.escape(news['last_keyword'])}”" if news.get("last_keyword") else "all keywords"
        return (f'<div class="donebar">✓ Last scan of {who}: {s.get("mentions", 0)} new detection(s), '
                f'{s.get("cached", 0)} article(s) fetched.</div>')
    e = ep.get("last_summary")
    if e:
        return (f'<div class="donebar">✓ Last e-paper cycle: {e.get("downloaded", 0)} page(s) fetched, '
                f'{e.get("read", 0)} read, {e.get("mentions", 0)} new detection(s).</div>')
    return ""


# ==========================================================================
# Shared card renderers
# ==========================================================================
def _media_url(abs_path: str | None) -> str | None:
    if not abs_path:
        return None
    try:
        rel = Path(abs_path).resolve().relative_to(settings.storage_dir.resolve())
        return "/media/" + str(rel).replace("\\", "/")
    except Exception:
        return None


def _detection_card(m: Mention) -> str:
    thumb = _media_url(m.screenshot_path) or _media_url(m.full_screenshot_path)
    badge = ""
    if m.module == "epaper" and m.section:
        pg = m.section.rsplit("page", 1)[-1].strip()
        badge = f'<span class="pagebadge">🗞 p.{html.escape(pg)}</span>'
    img = (f'<div class="shot">{badge}<img loading="lazy" class="zoom" src="{thumb}" '
           f'data-full="{thumb}"></div>') if thumb else ""
    tags = "".join(f'<span class="tag">{html.escape(k)}</span>' for k in (m.matched_keywords or []))
    when = m.detected_at.astimezone(_PKT).strftime("%d %b %Y, %H:%M") if m.detected_at else ""
    icon = "🗞 E-Paper" if m.module == "epaper" else "📰"
    meta = " · ".join(x for x in [icon, m.source, m.sentiment, when] if x)
    return (f'<div class="det">{img}<div class="body">'
            f'<a class="ttl" href="{html.escape(m.url)}" target="_blank">{html.escape(m.title)}</a>'
            f'<div class="meta">{meta}</div><div>{tags}</div></div></div>')


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


_SENT_COLORS = {"Positive": "var(--accent)", "Critical": "#e05d3a",
                "Neutral": "#9b9384", "Unscored": "var(--line-strong)"}


def _bars(days: list[tuple[str, int]]) -> str:
    mx = max((v for _, v in days), default=0) or 1
    cells = "".join(
        f'<div class="b"><span class="n">{v}</span>'
        f'<i data-h="{int(4 + (v / mx) * 108)}"></i>{lbl}</div>'
        for lbl, v in days
    )
    return f'<div class="bars">{cells}</div>'


def _hbars(pairs: list[tuple[str, int]]) -> str:
    if not pairs:
        return '<div class="empty">No detections yet.</div>'
    mx = max(v for _, v in pairs) or 1
    return "".join(
        f'<div class="hbar"><div class="hl" title="{html.escape(l)}">{html.escape(l)}</div>'
        f'<div class="track"><i data-w="{int(v / mx * 100)}"></i></div>'
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


# ==========================================================================
# Overview
# ==========================================================================
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
    n_papers = len(SITE_CONFIGS) + 1
    n_epapers = len(sources.SOURCES)
    ep_pages_today = db.scalar(
        select(func.count()).select_from(EPaperPage)
        .where(EPaperPage.date == today_start.date().isoformat())
    ) or 0

    agg = db.execute(
        select(Mention.detected_at, Mention.source, Mention.sentiment, Mention.module)
        .order_by(Mention.detected_at.desc()).limit(3000)
    ).all()

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
        f'<span class="rsrc">{"🗞" if m.module == "epaper" else "📰"} '
        f'{html.escape((m.source or "")[:16])}</span>'
        f'<span class="rt">{html.escape(m.title)}</span>'
        f'<span class="rtime">{_rel(m.detected_at)}</span></a>'
        for m in recent
    ) or '<div class="empty">No detections yet — run a scan to get started.</div>'

    st = scan_manager.status()
    last = st.get("last_summary")
    last_txt = (f'{last.get("mentions", 0)} found' if last else "—")

    health = (
        _health_row("Scheduled scans", settings.scheduler_enabled,
                    "on" if settings.scheduler_enabled else "manual only")
        + _health_row("E-paper reading (vision)", reader.has_key(),
                      {"groq": "ready · Groq", "anthropic": "ready · Claude"}.get(
                          reader.provider(), "needs GROQ_API_KEY"))
        + _health_row("LLM scoring", settings.enable_llm_scoring and reader.has_key(),
                      "on" if (settings.enable_llm_scoring and reader.has_key()) else "off")
        + _health_row("WhatsApp alerts", settings.notifier == "whatsapp" and bool(settings.whatsapp_access_token),
                      "live" if (settings.notifier == "whatsapp" and settings.whatsapp_access_token) else "dry-run")
        + _health_row("Email digest", settings.smtp_configured,
                      "SMTP" if settings.smtp_configured else "file preview")
    )

    def tile(label, val, foot, ic, count=True):
        v = (f'<div class="val" data-count="{val}">0</div>' if count and str(val).isdigit()
             else f'<div class="val">{val}</div>')
        return (f'<div class="tile"><div class="ic">{ic}</div><div class="label">{label}</div>'
                f'{v}<div class="foot">{foot}</div></div>')

    body = f"""
    <div class="tiles stagger">
      {tile("Total detections", total, "all time", "◎")}
      {tile("Today", today, "since 00:00 PKT", "↑")}
      {tile("Active keywords", active_kw, '<a class="count-link" href="/newspapers">manage →</a>', "#")}
      {tile("Coverage", n_papers + n_epapers, f'{n_papers} websites · {n_epapers} e-papers', "◧")}
      {tile("E-paper pages today", ep_pages_today, '<a class="count-link" href="/epaper">browse →</a>', "🗞")}
    </div>

    <div class="cols">
      <div>
        <div class="panel">
          <h3>Detections — last 7 days</h3><div class="cap">Website + print hits per day (PKT)</div>
          {_bars(days)}
        </div>
        <div class="panel">
          <h3>Recent activity</h3><div class="cap">Latest matches across all sources · last scan: {last_txt}</div>
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


# ==========================================================================
# Newspapers — sources + the keyword manager
# ==========================================================================
_LANG_NAMES = {"en": "English", "ur": "Urdu"}


def _kw_counts(db) -> dict:
    rows = db.execute(select(Mention.matched_keywords).limit(4000)).scalars().all()
    c: dict[str, int] = {}
    for mk in rows:
        for k in (mk or []):
            c[k] = c.get(k, 0) + 1
    return c


def _last_runs(db) -> dict:
    """Most recent ScrapeRun per source slug."""
    runs = db.execute(
        select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(80)
    ).scalars().all()
    out: dict[str, ScrapeRun] = {}
    for r in runs:
        out.setdefault(r.source, r)
    return out


def _paper_cards(db) -> str:
    last = _last_runs(db)
    metas = [("dawn", "Dawn", "en", ["front page", "world"])] + [
        (c.name, c.source, c.language, list(c.sections)) for c in SITE_CONFIGS
    ]
    cards = ""
    for slug, name, lang, secs in metas:
        run = last.get(slug)
        if run:
            ok = run.status == "ok"
            stat = (f'<span class="badge {"ok" if ok else "warn"}">'
                    f'{"✓" if ok else "⚠"} {run.status}</span>'
                    f'<span>{run.articles_found} links · {_rel(run.started_at)}</span>')
        else:
            stat = '<span class="badge off">no scans yet</span><span></span>'
        cards += (
            f'<div class="paper"><h4>{html.escape(name)} '
            f'<span class="tag">{_LANG_NAMES.get(lang, lang)}</span></h4>'
            f'<div class="mono">{slug}</div>'
            f'<div class="secs">{ " · ".join(html.escape(s) for s in secs) }</div>'
            f'<div class="stat">{stat}</div></div>'
        )
    return f'<div class="papers stagger">{cards}</div>'


def _keyword_table(keywords, counts: dict, scanning: bool, edit) -> str:
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
                f'<a href="/newspapers" style="align-self:center;color:var(--muted);font-weight:600;text-decoration:none">Cancel</a>'
                f"</form></td></tr>"
            )
            continue
        n = counts.get(k.text, 0)
        kwlink = f"/newspapers?kw={k.text}#results"
        results = (f'<a class="count-link" href="{kwlink}">{n} result(s) →</a>'
                   if n else '<span class="muted-count">0 results</span>')
        status = ('<button class="ghost sm" title="Click to pause — paused keywords are skipped">🟢 Active</button>'
                  if k.active else '<button class="sm" title="Click to activate">⏸ Paused</button>')
        dim = "" if k.active else ' style="opacity:.55"'
        scan_disabled = "disabled" if not k.active else ""
        rows += (
            f"<tr{dim}>"
            f'<td><a class="kwname" href="{kwlink}">{html.escape(k.text)}</a></td>'
            f'<td><span class="tag">{k.language.upper()}</span></td>'
            f'<td><form method="post" action="/ui/keywords/{k.id}/toggle" style="margin:0">{status}</form></td>'
            f"<td>{results}</td>"
            f'<td class="row" style="justify-content:flex-end">'
            f'<a class="btn ghost sm" href="/newspapers?edit={k.id}">Edit</a>'
            f'<form method="post" action="/ui/keywords/{k.id}/scan" style="margin:0">'
            f'<button class="sm" {scan_disabled} title="Instant: checks every stored article + e-paper page and shows the results">⚡ Scan</button></form>'
            f'<form method="post" action="/ui/keywords/{k.id}/delete" style="margin:0" '
            f"onsubmit=\"return confirm('Delete keyword “{html.escape(k.text)}”?')\">"
            f'<button class="ghost sm">Delete</button></form></td></tr>'
        )
    return ('<table><tr><th>Keyword</th><th>Lang</th><th>Status</th>'
            "<th>Detections</th><th></th></tr>" + rows + "</table>")


@app.get("/newspapers", response_class=HTMLResponse)
def newspapers_page(edit: int | None = None, kw: str | None = None,
                    checked: int | None = None, pages: int | None = None,
                    db: Session = Depends(get_db)):
    keywords = db.execute(select(Keyword).order_by(Keyword.created_at.desc())).scalars().all()
    counts = _kw_counts(db)
    scanning = scan_manager.is_running()
    table = _keyword_table(keywords, counts, scanning, edit)
    scan_all = (
        '<button class="btn-lg" disabled><span class="spin"></span>Scanning…</button>'
        if scanning else '<button class="btn-lg cta" type="submit">▶ Scan all keywords now</button>'
    )

    # ---- Unified detections (websites + e-papers together), keyword dropdown
    mentions = db.execute(
        select(Mention).order_by(Mention.detected_at.desc()).limit(600)
    ).scalars().all()
    if kw:
        mentions = [m for m in mentions if kw in (m.matched_keywords or [])]
    shown = mentions[:60]
    opts = f'<option value="">All keywords ({sum(counts.values())} hits)</option>'
    for k in sorted(keywords, key=lambda x: -counts.get(x.text, 0)):
        sel = " selected" if kw == k.text else ""
        opts += (f'<option value="{html.escape(k.text)}"{sel}>'
                 f"{html.escape(k.text)} ({counts.get(k.text, 0)})</option>")
    grid = (f'<div class="grid stagger">{"".join(_detection_card(m) for m in shown)}</div>'
            if shown else
            '<div class="empty">No detections yet'
            + (f" for “{html.escape(kw)}” — hit ⚡ Scan on it above." if kw else
               " — add a keyword and hit ⚡ Scan.") + "</div>")
    more = (f'<p class="sub" style="margin-top:1rem">Showing {len(shown)} of {len(mentions)} — '
            f'<a class="plink" href="/mentions{"?keyword=" + kw if kw else ""}">see all in Detections →</a></p>'
            if len(mentions) > len(shown) else "")

    quick_banner = ""
    if checked is not None:
        quick_banner = (
            f'<div class="banner ok">⚡ Instant results below — checked <b>{checked}</b> stored '
            f'article(s) and <b>{pages or 0}</b> e-paper page(s). A fresh scan of all live sources '
            f"is running in the background — new hits and screenshots appear automatically.</div>"
        )

    body = f"""
    {quick_banner}
    <div class="sechead">Publications <span>({len(SITE_CONFIGS) + 1} websites · {len(sources.SOURCES)} e-papers ·
      scanned every {settings.newspaper_scrape_interval_minutes} min when scheduling is on)</span></div>
    {_paper_cards(db)}

    <div class="sechead">Keywords <span>(one list — matched on websites and e-paper pages alike)</span></div>
    <div class="card">
      <form method="post" action="/ui/keywords" class="row" style="margin-bottom:.9rem">
        <input name="text" placeholder="Add a keyword — English or اردو" required
               style="flex:1;min-width:220px">
        <select name="language"><option value="en">English</option><option value="ur">Urdu</option></select>
        <button type="submit">+ Add keyword</button>
      </form>
      <form method="post" action="/ui/scan/newspaper" style="margin:0">{scan_all}</form>
      <div class="hint">⚡ <b>Scan</b> on a keyword: instant results from everything stored, then a
      fresh scan of every website and today's e-papers for exact matches — with a screenshot of each
      hit (article shot or the print page). Matching is precise: whole words only (no “rape” →
      “grape”), light inflection for English, Urdu script &amp; diacritic variants unified.
      Monitoring window: <b>{settings.monitor_since[:4]} onwards</b>.</div>
    </div>
    <div class="card">{table}</div>

    <div class="sechead" id="results">Detections <span>(websites + e-papers together)</span></div>
    <div class="card" style="margin-bottom:1rem">
      <form method="get" action="/newspapers" class="row">
        <label style="font-weight:700">Keyword:</label>
        <select name="kw" onchange="this.form.submit()" style="min-width:240px">{opts}</select>
        <noscript><button type="submit">Show</button></noscript>
      </form>
    </div>
    {grid}
    {more}
    """
    return _shell("Media Monitor — Newspapers", "newspapers", body)


# ==========================================================================
# E-paper — daily print editions
# ==========================================================================
@app.get("/epaper", response_class=HTMLResponse)
def epaper_page(date: str | None = None, db: Session = Depends(get_db)):
    today = datetime.now(_PKT).date()
    try:
        show_date = datetime.strptime(date, "%Y-%m-%d").date() if date else today
    except ValueError:
        show_date = today
    ds = show_date.isoformat()

    rows = db.execute(
        select(EPaperPage).where(EPaperPage.date == ds)
        .order_by(EPaperPage.paper, EPaperPage.page_no)
    ).scalars().all()
    by_paper: dict[str, list[EPaperPage]] = defaultdict(list)
    for r in rows:
        by_paper[r.paper].append(r)

    running = scan_runner.is_running()
    key_ok = reader.has_key()

    cards = ""
    for slug, (name, lang, _fn) in sources.SOURCES.items():
        pages = by_paper.get(slug, [])
        n_done = sum(1 for p in pages if p.ocr_status == "done")
        n_wait = sum(1 for p in pages if p.ocr_status in ("pending", "no_key"))
        if pages:
            state = f'<span class="badge ok">{len(pages)} pages</span>'
            if n_done:
                state += f'<span class="badge ok">✓ {n_done} read</span>'
            if n_wait:
                state += (f'<span class="badge warn">⏳ {n_wait} awaiting '
                          f'{"read" if key_ok else "API key"}</span>')
        else:
            state = '<span class="badge off">not fetched yet</span>'
        strip = ""
        for p in pages[:14]:
            thumb = _media_url(p.image_path)
            if not thumb:
                continue
            strip += (f'<a href="{html.escape(p.viewer_url or p.image_url)}" target="_blank" '
                      f'onclick="return false" style="cursor:zoom-in">'
                      f'<img class="zoom" loading="lazy" src="{thumb}" data-full="{thumb}">'
                      f'<span class="pn">p{p.page_no}</span></a>')
        strip = (f'<div class="pgstrip">{strip}</div>' if strip else
                 '<div class="empty" style="padding:1.2rem">No pages stored for this date.</div>')
        fetch_btn = ('<button class="sm" disabled><span class="spin"></span></button>' if running else
                     f'<form method="post" action="/ui/epaper/fetch/{slug}" style="margin:0">'
                     f'<button class="ghost sm" title="Fetch this paper now">⟳ Fetch</button></form>')
        cards += (
            f'<div class="edcard"><div class="edhead">'
            f'<h4>🗞 {html.escape(name)}</h4><span class="tag">{_LANG_NAMES.get(lang, lang)}</span>'
            f'<span class="grow"></span>{state}{fetch_btn}</div>{strip}</div>'
        )

    unsupported = " · ".join(html.escape(v) for v in sources.UNSUPPORTED.values())
    key_banner = "" if key_ok else (
        '<div class="banner">⚠ Reading pages needs <b>GROQ_API_KEY</b> (or ANTHROPIC_API_KEY) '
        "in your .env — page scans are still fetched and browsable, but keyword matching inside "
        "them starts once a key is set (stored pages are read automatically on the next scan).</div>"
    )
    fetch_all = (
        '<button class="btn-lg" disabled><span class="spin"></span>Working…</button>' if running
        else '<button class="btn-lg cta" type="submit">⟳ Fetch today’s editions</button>'
    )

    day_chips = ""
    for i in range(0, 7):
        d = today - timedelta(days=i)
        lbl = "Today" if i == 0 else d.strftime("%d %b")
        on = "on" if d == show_date else ""
        day_chips += f'<a class="chip {on}" href="/epaper?date={d.isoformat()}">{lbl}</a>'

    body = f"""
    {key_banner}
    <div class="card" style="display:flex;gap:1rem;align-items:center;flex-wrap:wrap">
      <form method="post" action="/ui/epaper/fetch" style="margin:0">{fetch_all}</form>
      <span style="color:var(--muted);font-size:.88rem">Editions publish by early morning;
      the scheduler fetches daily at {settings.epaper_fetch_hour_pkt:02d}:15 PKT.
      Every page is read once, then all keywords match against it — including ones you add later.</span>
    </div>
    <div style="margin:.2rem 0 1rem">{day_chips}</div>
    {cards}
    <p class="sub" style="font-size:.83rem">Not available: {unsupported}.</p>
    """
    return _shell("Media Monitor — E-Paper", "epaper", body)


# ==========================================================================
# Detections
# ==========================================================================
@app.get("/mentions", response_class=HTMLResponse)
def detections_page(keyword: str | None = None, src: str | None = None,
                    checked: int | None = None, pages: int | None = None,
                    found: int | None = None, db: Session = Depends(get_db)):
    mentions = (
        db.execute(select(Mention).order_by(Mention.detected_at.desc()).limit(500))
        .scalars().all()
    )
    if keyword:
        mentions = [m for m in mentions if keyword in (m.matched_keywords or [])]
    papers = [m for m in mentions if m.module == "newspaper"]
    prints = [m for m in mentions if m.module == "epaper"]

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

    src_chips = (
        chip("All sources", src is None, href(keyword, None))
        + chip("📰 Websites", src == "newspaper", href(keyword, "newspaper"))
        + chip("🗞 E-Paper", src == "epaper", href(keyword, "epaper"))
    )
    kw_chips = chip("All keywords", keyword is None, href(None, src)) + "".join(
        chip(html.escape(k.text), keyword == k.text, href(k.text, src)) for k in active_keywords
    )

    # One unified grid — website articles and print pages together, newest
    # first; each card carries its own 📰/🗞 badge.
    if src == "newspaper":
        items = papers
    elif src == "epaper":
        items = prints
    else:
        items = mentions
    if items:
        content = f'<div class="grid stagger">{"".join(_detection_card(m) for m in items)}</div>'
    else:
        content = ('<div class="empty">No detections yet.<br>'
                   'Add keywords on the <a class="plink" href="/newspapers">Newspapers</a> page '
                   "and hit ⚡ Scan.</div>")

    clear_btn = (
        '<form method="post" action="/ui/detections/clear" style="margin:0" '
        "onsubmit=\"return confirm('Delete ALL detections? This cannot be undone.')\">"
        '<button class="ghost">🗑 Clear all</button></form>'
    )
    quick_banner = ""
    if checked is not None:
        quick_banner = (
            f'<div class="banner ok">⚡ Quick scan done — checked '
            f'<b>{checked}</b> stored article(s) and <b>{pages or 0}</b> e-paper page(s)'
            + (f" · <b>{found}</b> detection(s) for this keyword" if found is not None else "")
            + ". Fresh content keeps arriving via scheduled scans and “Scan all”.</div>"
        )
    body = f"""
    {quick_banner}
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:.2rem">
      <p class="sub" style="margin:0">{len(mentions)} detection(s){' for “' + html.escape(keyword) + '”' if keyword else ''}</p>
      {clear_btn}
    </div>
    <div style="margin:.8rem 0 .3rem">{src_chips}</div>
    <div style="margin-bottom:1rem">{kw_chips}</div>
    {content}
    """
    return _shell("Media Monitor — Detections", "mentions", body)


# ==========================================================================
# UI actions
# ==========================================================================
@app.post("/ui/keywords")
def ui_add_keyword(text: str = Form(...), language: str = Form("en"),
                   db: Session = Depends(get_db)):
    text = text.strip()
    if text:
        exists = db.execute(
            select(Keyword).where(Keyword.text == text, Keyword.language == language)
        ).first()
        if not exists:
            db.add(Keyword(text=text, language=language, module="newspaper", active=True))
            db.commit()
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/keywords/{kid}/edit")
def ui_edit_keyword(kid: int, text: str = Form(...), language: str = Form("en"),
                    db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw and text.strip():
        kw.text = text.strip()
        kw.language = language if language in ("en", "ur") else kw.language
        db.commit()
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/keywords/{kid}/toggle")
def ui_toggle_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw:
        kw.active = not kw.active
        db.commit()
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/keywords/{kid}/delete")
def ui_delete_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if kw:
        db.delete(kw)
        db.commit()
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/keywords/{kid}/scan")
def ui_scan_keyword(kid: int, db: Session = Depends(get_db)):
    """⚡ Scan one keyword, completely:
    1. INSTANT — match it against everything stored (cached articles + read
       e-paper pages) and land on the results, inline, no browser.
    2. FRESH — launch the full source scan in the background: every website is
       scraped for exact matches (each hit screenshotted, quick hits backfilled
       first) and today's e-paper editions are fetched/read/matched (each hit
       carries its page scan). Results merge in automatically when done."""
    kw = db.get(Keyword, kid)
    if not kw:
        raise HTTPException(404, "keyword not found")
    res = run_quick_match(keyword_ids=[kid])
    scan_manager.start_scan(keyword_ids=[kid], keyword_label=kw.text, capped=True)
    scan_runner.start_scan(keyword_ids=[kid], label=kw.text)
    return RedirectResponse(
        f"/newspapers?kw={kw.text}&checked={res['articles_checked']}"
        f"&pages={res['pages_checked']}#results",
        status_code=303,
    )


@app.post("/ui/scan")
def ui_scan_all():
    # Nav "Scan all": websites + today's e-paper cycle (subprocesses; one each).
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    scan_runner.start_scan()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/scan/newspaper")
def ui_scan_newspapers():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    return RedirectResponse("/newspapers", status_code=303)


@app.post("/ui/epaper/fetch")
def ui_epaper_fetch_all():
    scan_runner.start_scan()
    return RedirectResponse("/epaper", status_code=303)


@app.post("/ui/epaper/fetch/{slug}")
def ui_epaper_fetch_one(slug: str):
    if slug not in sources.SOURCES:
        raise HTTPException(404, "unknown paper")
    name = sources.SOURCES[slug][0]
    scan_runner.start_scan(papers=[slug], label=name)
    return RedirectResponse("/epaper", status_code=303)


@app.post("/ui/detections/clear")
def ui_clear_detections(db: Session = Depends(get_db)):
    """Delete all detections (Mention rows). Cached article text and e-paper
    reads are kept, so a re-scan can re-detect instantly."""
    db.execute(delete(Mention))
    db.commit()
    return RedirectResponse("/mentions", status_code=303)


# ==========================================================================
# JSON API
# ==========================================================================
@app.get("/api/keywords")
def list_keywords(db: Session = Depends(get_db)):
    rows = db.execute(select(Keyword).order_by(Keyword.created_at.desc())).scalars().all()
    return [{"id": k.id, "text": k.text, "language": k.language, "active": k.active}
            for k in rows]


@app.get("/api/mentions")
def list_mentions(keyword: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    rows = db.execute(select(Mention).order_by(Mention.detected_at.desc()).limit(limit)).scalars().all()
    if keyword:
        rows = [m for m in rows if keyword in (m.matched_keywords or [])]
    return [
        {
            "id": m.id, "module": m.module, "source": m.source, "title": m.title,
            "url": m.url, "matched_keywords": m.matched_keywords, "sentiment": m.sentiment,
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


@app.get("/api/scan/status")
def scan_status():
    return scan_manager.status()


@app.get("/api/scan/epaper/status")
def epaper_scan_status():
    return scan_runner.status()


@app.post("/api/scan/epaper")
def trigger_epaper_scan():
    return {"started": scan_runner.start_scan()}


@app.post("/api/scan/newspaper")
def trigger_scan(keyword_ids: list[int] | None = None):
    """Synchronous scan (blocks until done) — for scripts/testing."""
    return run_newspaper_scan(keyword_ids=keyword_ids, uncapped=True)
