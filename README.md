# Media Monitoring

Watches Pakistani newspapers — **websites and daily e-paper print editions** —
for keywords, captures evidence (screenshots / page scans), scores relevance +
sentiment, sends real-time alerts, and emails a daily digest.

**Status: code-complete end-to-end.** Everything runs today in a credential-free
default mode; going fully live is just adding credentials and flipping a flag
(see the table at the bottom).

## Stack (local build, no Docker)

| Concern      | Choice                                   |
|--------------|------------------------------------------|
| API / admin  | FastAPI + Uvicorn (server-rendered console, cream/green design) |
| Scheduling   | APScheduler, persistent SQLAlchemy jobstore (jobs resume after restart) |
| Database     | SQLAlchemy — SQLite (dev) or Supabase Postgres (`DATABASE_URL`) |
| Scraping     | Playwright (real browser) + BeautifulSoup |
| Matching     | Word-boundary + length-scaled fuzzy (rapidfuzz), EN + UR normalization |
| E-paper OCR  | Vision LLM — Groq Llama-4 (default) or Claude; reads scanned pages incl. Urdu Nastaliq |
| Scoring      | Groq/Claude relevance + sentiment (config flag) |
| Alerts       | console + `data/alerts.log` always; WhatsApp (Meta Cloud API) optional |
| Digest       | SMTP email at 07:00 PKT; writes HTML file if no SMTP |

## Modules

- **Newspaper websites** — Dawn, The News, Express Tribune, Jang, Nawa-i-Waqt,
  ARY News, Dunya News, Express Urdu (8 sites, national + world + opinion
  sections). Scrape → cache → keyword match → screenshot (metadata footer) →
  store → alert. *(Geo is API-rendered — see [docs/RUNBOOK.md](docs/RUNBOOK.md) §6.)*
- **E-paper (print editions)** — Dawn, Express Tribune, The News, Jang, Express
  Urdu, Nawa-i-Waqt. Every morning each paper's page scans are fetched, read
  once with Claude vision (English + Urdu Nastaliq), and matched with the same
  keywords; detections carry the page image + a link to the e-paper viewer.
  *(Dunya's e-paper site has a broken TLS cert; ARY/Geo are TV-only.)*
- **Digest** — daily 07:00 PKT summary grouped by source with sentiment counts +
  thumbnails.
- **Retention** — daily cleanup (screenshots 90d, cached text 12m, logs 24m).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system diagram, components, stack decisions, **env var reference**, API summary
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — setup, enabling credentials, adding a publication, fixing a broken scraper, recovery, troubleshooting
- [docs/SUPABASE.md](docs/SUPABASE.md) + [docs/supabase_schema.sql](docs/supabase_schema.sql) — hosted-Postgres setup

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env          # runs fine unedited; add credentials to go live
```

## Run

```powershell
uvicorn app.main:app --reload
#   Console:   http://127.0.0.1:8000/      (Overview · Newspapers · E-Paper · Detections)
#   API docs:  http://127.0.0.1:8000/docs
```

Scheduler starts automatically: newspapers every 30 min, e-paper daily 08:15 PKT,
digest 07:00 PKT, retention 04:00 PKT.

### Manual / testing commands
```powershell
python -m scripts.run_newspaper_once     # one newspaper scan
python -m scripts.run_epaper             # fetch + read + match today's e-papers
python -m scripts.send_digest            # build digest (emails or writes HTML file)
python -m scripts.seed_keywords          # sample keywords
python -m tests.test_keywords            # keyword-matching precision suite
```

## What each feature needs to go fully live

| Feature                    | Works now as…                    | To go live, add…                        |
|----------------------------|----------------------------------|-----------------------------------------|
| Newspaper website monitoring | fully live                     | — (nothing)                             |
| E-paper page fetching + browsing | fully live                 | — (nothing)                             |
| E-paper keyword matching (page reading) | pages archived, awaiting key | `GROQ_API_KEY` (or `ANTHROPIC_API_KEY`) |
| Relevance + sentiment      | off                              | `ENABLE_LLM_SCORING=true` + a provider key |
| WhatsApp alerts            | dry-run (logs payload)           | `NOTIFIER=whatsapp` + Meta creds + approved template |
| Email digest               | writes HTML file                 | `SMTP_HOST`/creds + `DIGEST_*`          |

## Layout
```
config.py                       # env-driven settings
app/db/                         # engine + ORM (Keyword, Mention, ArticleCache, EPaperPage, ScrapeRun)
app/core/                       # keyword matching + Claude scoring
app/scrapers/                   # base, dawn, configurable, sites, footer
app/newspaper/                  # website pipeline + subprocess scan_manager
app/epaper/                     # sources (per-paper adapters), reader (vision), pipeline, scan_runner
app/digest/                     # builder + sender
app/maintenance/                # retention cleanup
app/notifiers/                  # console, whatsapp, multi
app/scheduler.py, app/main.py   # APScheduler + FastAPI console
scripts/                        # run_newspaper_once, run_scan, run_epaper, send_digest, seed_keywords, init_db
tests/                          # keyword-matching precision suite
```
