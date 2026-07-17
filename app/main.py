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

from config import settings
from app.db.base import SessionLocal, init_db
from app.db.models import EPaperPage, Keyword, Mention
from app.core import result_policy
from app.epaper import scan_runner, sources
from app.newspaper import scan_manager
from app.newspaper.pipeline import run_newspaper_scan, run_quick_match
from app.scrapers.sites import SITE_CONFIGS
from app.scheduler import shutdown_scheduler, start_scheduler
from app import sources_probe

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_PKT = timezone(timedelta(hours=5))


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
.results-head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;margin-bottom:.85rem}
.results-head h2{margin:0;font-size:1.15rem;color:var(--blue-deep)}
.results-head .count{color:var(--muted);font-size:.88rem;font-weight:600}
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
.kw-del,.kw-play-form{margin:0;display:inline-flex}
.kw-pick.kw-all{border:1px solid var(--line);border-radius:999px;padding:.28rem .7rem;background:#fffdf9}
.kw-pick.kw-all.on{background:var(--blue-deep);border-color:transparent;color:#fff;
  box-shadow:0 4px 12px -6px rgba(74,138,176,.55)}
.kw-pick.kw-all:hover{background:var(--blue-soft);border-color:var(--blue);color:var(--blue-deep)}
.kw-pick.kw-all.on:hover{background:var(--blue);color:#fff}
.kw-add{display:flex;gap:.45rem;flex-wrap:wrap;align-items:center;margin:0}
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

  /* Watchlist tags → fill keyword + run search (× is a separate delete form) */
  var q=document.getElementById('q'), form=document.getElementById('search');
  document.querySelectorAll('.kw-pick').forEach(function(btn){
    btn.addEventListener('click',function(){
      if(!q||!form)return;
      q.value=btn.getAttribute('data-kw')||'';
      document.querySelectorAll('.kw-chip,.kw-pick.kw-all').forEach(function(el){
        el.classList.remove('on');
      });
      var chip=btn.closest('.kw-chip');
      if(chip)chip.classList.add('on');else btn.classList.add('on');
      form.requestSubmit?form.requestSubmit():form.submit();
    });
  });

  var wasScanning=__SCANNING__;
  async function poll(){
    try{
      var n=await fetch('/api/scan/status').then(function(r){return r.json()});
      var e=await fetch('/api/scan/epaper/status').then(function(r){return r.json()});
      var running=n.running||e.running;
      var side=document.getElementById('live-state');
      var b=document.getElementById('scanbtn');
      if(running){
        if(side)side.innerHTML='<span class="dot busy"></span>Working…';
        if(b){b.disabled=true;b.innerHTML='<span class="spin"></span> Scanning…'}
      }else if(wasScanning){location.reload()}
      wasScanning=running;
    }catch(err){}
  }
  setInterval(poll,3000);

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


def _shell(title: str, body: str) -> str:
    news = scan_manager.status()
    ep = scan_runner.status()
    scanning = bool(news["running"] or ep["running"])
    state = ('<span class="dot busy"></span>Working…' if scanning
             else '<span class="dot live"></span>Live')
    scan_btn = (
        '<button class="ghost" id="scanbtn" disabled><span class="spin"></span> Scanning…</button>'
        if scanning
        else '<form method="post" action="/ui/scan" style="margin:0">'
             '<button class="ghost" id="scanbtn" type="submit">Scan now</button></form>'
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>{_FONTS}<style>{_CSS}</style></head><body>
<div class="top"><div class="top-inner">
  <a class="brand" href="/"><span class="mark">◎</span>
    <span><b>Media Monitor</b><small>Press desk</small></span></a>
  <span class="spacer"></span>
  <span class="live" id="live-state">{state}</span>
  {scan_btn}
</div></div>
<main class="page"><div class="wrap">{body}</div></main>
<script>{_JS.replace('__SCANNING__', 'true' if scanning else 'false')}</script>
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


def _detection_card(m: Mention, highlight_keywords: list[str] | None = None) -> str:
    hl = list(highlight_keywords or [])
    keyword_path = None
    if len(hl) == 1:
        needle = hl[0].casefold()
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
        zoom = html.escape(full or thumb)
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
    kind = "E-Paper" if m.module == "epaper" else "Web"
    meta = " · ".join(x for x in [kind, m.source, m.sentiment, when] if x)
    excerpt = _highlight_excerpt(m.snippet, hl)
    excerpt_html = f'<div class="excerpt">…{excerpt}…</div>' if excerpt else ""
    return (f'<div class="det">{img}<div class="body">'
            f'<a class="ttl" href="{html.escape(m.url)}" target="_blank" rel="noopener">'
            f'{html.escape(m.title)}</a>{excerpt_html}'
            f'<div class="meta">{meta}</div><div>{tags}</div></div></div>')


def _known_keyword_fold(db: Session) -> set[str]:
    """All keyword strings still in the watchlist table (active or paused)."""
    return {
        (k.text or "").casefold()
        for k in db.execute(select(Keyword)).scalars().all()
        if k.text
    }


def _active_keyword_fold(db: Session) -> dict[str, str]:
    """casefold -> canonical text for active watchlist keywords."""
    out: dict[str, str] = {}
    for k in db.execute(select(Keyword).where(Keyword.active.is_(True))).scalars().all():
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
    """Instant corpus match + background fresh scan for ONE keyword only.

    Dedup is enforced by Mention(module, external_id) uniqueness — re-scans
    update the same row (merge keywords / refresh shot) instead of duplicating.
    """
    res = run_quick_match(keyword_ids=[kw.id])
    news_ok = scan_manager.start_scan(
        keyword_ids=[kw.id], keyword_label=kw.text, capped=True)
    ep_ok = scan_runner.start_scan(keyword_ids=[kw.id], label=kw.text)
    res["live_started"] = bool(news_ok or ep_ok)
    return res


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
    news_st = scan_manager.status()
    ep_st = scan_runner.status()
    scanning_now = bool(news_st.get("running") or ep_st.get("running"))
    if qp.get("removed"):
        banner = (
            f'<div class="banner ok">Hidden <b>{html.escape(qp.get("removed"))}</b> from the '
            "watchlist. Its results remain safely retained for 90 days and return if you add it again."
            "</div>"
        )
    elif qp.get("scanning") or (scanning_now and keyword):
        who = html.escape(keyword or news_st.get("keyword") or ep_st.get("label") or "keyword")
        if qp.get("busy"):
            banner = (
                f'<div class="banner ok"><span class="spin"></span> Instant match for '
                f'<b>“{who}”</b> done'
                + (f' · <b>{html.escape(qp.get("found") or "0")}</b> hit(s) from storage' if qp.get("found") is not None else "")
                + ". A live scan is already running — new hits merge into the same records "
                f"(no duplicates) when it finishes.</div>"
            )
        else:
            banner = (
                f'<div class="banner ok"><span class="spin"></span> Scanning for <b>“{who}”</b>… '
                f"Matching stored articles &amp; e-paper pages now; a fresh live scan is running. "
                f"New hits merge with existing ones (deduped by article/page) — no duplicates.</div>"
            )
    elif qp.get("checked") is not None:
        banner = (
            f'<div class="banner ok">Quick check finished — {html.escape(qp.get("checked") or "0")} stored '
            f'article(s) and {html.escape(qp.get("pages") or "0")} e-paper page(s)'
            + (f' · <b>{html.escape(qp.get("found") or "0")}</b> new hit(s)' if qp.get("found") is not None else "")
            + ". Fresh scans keep filling in.</div>"
        )

    results_html = ""
    # Drop labels for keywords that were deleted earlier (before purge existed).
    _scrub_deleted_keywords(db)
    active_fold = _active_keyword_fold(db)

    if searched:
        first_date = (
            show_date - timedelta(days=settings.keyword_result_retention_days - 1)
            if keyword else show_date
        )
        day_start = datetime(first_date.year, first_date.month, first_date.day, tzinfo=_PKT)
        day_end = datetime(
            show_date.year, show_date.month, show_date.day, tzinfo=_PKT
        ) + timedelta(days=1)
        start_utc = day_start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = day_end.astimezone(timezone.utc).replace(tzinfo=None)

        mentions = db.execute(
            select(Mention).where(
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

        # Only watchlist-active keywords count — never resurface a removed label
        # via title/snippet text alone.
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

        mentions.sort(key=result_policy.effective_time, reverse=True)
        shown = mentions[:settings.keyword_result_limit if keyword else 80]
        if shown:
            cards = []
            for m in shown:
                live = _live_matched(m, active_fold)
                if keyword:
                    hl = [active_fold[keyword.casefold()]]
                else:
                    hl = live
                cards.append(_detection_card(m, highlight_keywords=hl))
            grid = f'<div class="grid">{"".join(cards)}</div>'
            more = (f'<p class="hint" style="margin-top:.9rem">Showing {len(shown)} of '
                    f"{len(mentions)}.</p>" if len(mentions) > len(shown) else "")
        else:
            scope = "the 90 days through this date" if keyword else "this date"
            grid = (f'<div class="empty">No matches for {scope}, keyword, and paper selection.'
                    "<br>Try another date or clear the keyword.</div>")
            more = ""
        results_html = f"""
        <section class="results" id="results">
          <div class="results-head">
            <h2>Results</h2>
            <span class="count">{len(mentions)} match{'es' if len(mentions) != 1 else ''}</span>
          </div>
          {grid}{more}
        </section>
        """
    else:
        results_html = (
            '<div class="empty">Pick a date, type a keyword if you like, choose newspapers, '
            "then show results.</div>"
        )

    active_kws = db.execute(
        select(Keyword).where(Keyword.active.is_(True)).order_by(Keyword.text)
    ).scalars().all()
    kw_l = keyword.casefold()
    kw_tags = (
        f'<button type="button" class="kw-pick kw-all{" on" if not keyword else ""}" data-kw="">All</button>'
        + "".join(
            f'<span class="kw-chip{" on" if kw_l == k.text.casefold() else ""}">'
            f'<button type="button" class="kw-pick" '
            f'data-kw="{html.escape(k.text, quote=True)}">{html.escape(k.text)}</button>'
            f'<form class="kw-play-form" method="post" action="/ui/keywords/{k.id}/scan">'
            f'<button type="submit" class="kw-play" title="Scan this keyword now" '
            f'aria-label="Scan">▶</button></form>'
            f'<form class="kw-del" method="post" action="/ui/keywords/{k.id}/delete" '
            f"onsubmit=\"return confirm('Hide “{html.escape(k.text, quote=True)}” from the watchlist? Its results stay retained for 90 days.')\">"
            f'<button type="submit" class="kw-x" title="Remove" aria-label="Remove">'
            f'×</button></form></span>'
            for k in active_kws
        )
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
        <form class="kw-add" method="post" action="/ui/keywords">
          <input name="text" placeholder="New keyword — scan starts immediately" required maxlength="120">
          <select name="language"><option value="en">EN</option><option value="ur">UR</option></select>
          <button type="submit">+ Add</button>
        </form>
        <div class="kw-bar">
          <div class="cap">Watchlist · click to filter · ▶ scan · × hide</div>
          <div class="kw-tags">{kw_tags or '<span class="hint">No keywords yet — add one above.</span>'}</div>
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
    return _shell("Media Monitor", body)


@app.get("/newspapers")
@app.get("/epaper")
@app.get("/mentions")
def _gone_pages():
    return RedirectResponse("/", status_code=303)


# ==========================================================================
# UI actions (same behaviour; land back on the single page)
# ==========================================================================
@app.post("/ui/keywords")
def ui_add_keyword(text: str = Form(...), language: str = Form("en"),
                   db: Session = Depends(get_db)):
    text = text.strip()
    if not text:
        return RedirectResponse("/", status_code=303)
    language = language if language in ("en", "ur") else "en"
    existing = db.execute(
        select(Keyword).where(
            func.lower(Keyword.text) == text.lower(),
            Keyword.language == language,
            Keyword.module == "newspaper",
        )
    ).scalar_one_or_none()
    today = datetime.now(_PKT).date().isoformat()
    if existing:
        if existing.active:
            return _home_redirect({"q": existing.text, "go": "1", "date": today})
        # Soft-deleted keywords retain their associations. Reactivation restores
        # them immediately, then searches current-to-oldest for any missing hits.
        existing.active = True
        db.commit()
        res = _start_keyword_scan(existing)
        return _home_redirect({
            "q": existing.text,
            "go": "1",
            "date": today,
            "scanning": "1",
            "busy": "1" if not res.get("live_started") else None,
            "checked": res.get("articles_checked", 0),
            "pages": res.get("pages_checked", 0),
            "found": res.get("mentions", 0),
        })

    kw = Keyword(text=text, language=language, module="newspaper", active=True)
    db.add(kw)
    db.commit()
    db.refresh(kw)

    res = _start_keyword_scan(kw)
    return _home_redirect({
        "q": kw.text,
        "go": "1",
        "date": today,
        "scanning": "1",
        "busy": "1" if not res.get("live_started") else None,
        "checked": res.get("articles_checked", 0),
        "pages": res.get("pages_checked", 0),
        "found": res.get("mentions", 0),
    })


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
    if not kw:
        return RedirectResponse("/", status_code=303)
    text = kw.text
    kw.active = False
    db.commit()
    return _home_redirect({
        "removed": text,
        "go": "1",
        "date": datetime.now(_PKT).date().isoformat(),
    })


@app.post("/ui/keywords/{kid}/scan")
def ui_scan_keyword(kid: int, db: Session = Depends(get_db)):
    kw = db.get(Keyword, kid)
    if not kw:
        raise HTTPException(404, "keyword not found")
    res = _start_keyword_scan(kw)
    return _home_redirect({
        "q": kw.text,
        "go": "1",
        "date": datetime.now(_PKT).date().isoformat(),
        "scanning": "1",
        "busy": "1" if not res.get("live_started") else None,
        "checked": res.get("articles_checked", 0),
        "pages": res.get("pages_checked", 0),
        "found": res.get("mentions", 0),
    })


@app.post("/ui/scan")
def ui_scan_all():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    scan_runner.start_scan()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/scan/newspaper")
def ui_scan_newspapers():
    scan_manager.start_scan(keyword_ids=None, keyword_label=None, capped=True)
    return RedirectResponse("/", status_code=303)


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
def list_keywords(db: Session = Depends(get_db)):
    rows = db.execute(select(Keyword).order_by(Keyword.created_at.desc())).scalars().all()
    return [{"id": k.id, "text": k.text, "language": k.language, "active": k.active}
            for k in rows]


@app.get("/api/mentions")
def list_mentions(keyword: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 500))
    rows = db.execute(
        select(Mention).where(
            func.coalesce(Mention.published_at, Mention.detected_at)
            >= result_policy.cutoff()
        ).order_by(Mention.detected_at.desc())
    ).scalars().all()
    active = _active_keyword_fold(db)
    if keyword:
        folded = keyword.casefold()
        if folded not in active:
            rows = []
        else:
            rows = [
                m for m in rows
                if any((label or "").casefold() == folded for label in (m.matched_keywords or []))
            ]
    else:
        rows = [m for m in rows if _live_matched(m, active)]
    rows.sort(key=result_policy.effective_time, reverse=True)
    rows = rows[:limit]
    return [
        {
            "id": m.id, "module": m.module, "source": m.source, "title": m.title,
            "url": m.url, "matched_keywords": _live_matched(m, active),
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
    return run_newspaper_scan(keyword_ids=keyword_ids, uncapped=True)
