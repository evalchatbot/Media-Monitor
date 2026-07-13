# Media Monitoring

Watches Pakistani newspapers **and** YouTube channels for keywords, captures
evidence (screenshots / transcripts), scores relevance + sentiment, sends
real-time alerts, and emails a daily digest.

**Status: code-complete end-to-end.** Everything runs today in a credential-free
default mode; going fully live is just adding credentials and flipping a flag
(see the table at the bottom).

## Stack (local build, no Docker)

| Concern      | Choice                                   |
|--------------|------------------------------------------|
| API / admin  | FastAPI + Uvicorn (red/white HTML UI)    |
| Scheduling   | APScheduler, persistent SQLAlchemy jobstore (jobs resume after restart) |
| Database     | SQLite via SQLAlchemy (Postgres-ready)   |
| Scraping     | Playwright (real browser) + BeautifulSoup |
| Matching     | rapidfuzz (Levenshtein ≤ 2), EN + UR normalization |
| Transcription| Whisper — stub / OpenAI API / local GPU (config flag) |
| Scoring      | Claude relevance + sentiment (config flag) |
| Alerts       | console + `data/alerts.log` always; WhatsApp (Meta Cloud API) optional |
| Digest       | SMTP email at 07:00 PKT; writes HTML file if no SMTP |

## Modules

- **Newspaper** — Dawn, The News, Express Tribune, Jang, Nawa-i-Waqt, ARY News,
  Dunya News (7 sites). Scrape → cache → keyword match → screenshot (with
  metadata footer) → store → alert. *(Geo is JS/API-rendered — not yet scraped.)*
- **YouTube** — RSS-based new-upload detection (no API key), keyword match on
  title/description (+ transcript if enabled), timestamp deep-links, same alert +
  digest pipeline.
- **Digest** — daily 07:00 PKT summary grouped by source with sentiment counts +
  thumbnails.
- **Retention** — daily cleanup (screenshots 90d, transcripts 12m, logs 24m).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env          # runs fine unedited; add credentials to go live
```

`ffmpeg` on PATH is required only for YouTube audio transcription (openai/local).

## Run

```powershell
uvicorn app.main:app --reload
#   Keywords / Channels / Detections UI:  http://127.0.0.1:8000/
#   API docs:                             http://127.0.0.1:8000/docs
```

Scheduler starts automatically: newspapers every 30 min, YouTube every 10 min,
digest 07:00 PKT, retention 04:00 PKT.

### Manual / testing commands
```powershell
python -m scripts.run_newspaper_once     # one newspaper scan
python -m scripts.run_youtube            # one YouTube scan
python -m scripts.send_digest            # build digest (emails or writes HTML file)
python -m scripts.seed_keywords          # sample keywords
```

## What each feature needs to go fully live

| Feature                    | Works now as…                    | To go live, add…                        |
|----------------------------|----------------------------------|-----------------------------------------|
| Newspaper monitoring       | fully live                       | — (nothing)                             |
| YouTube upload detection   | fully live (RSS)                 | — (nothing)                             |
| In-video keyword + deeplink| title/description only (stub)    | `YOUTUBE_TRANSCRIBER=openai` + `OPENAI_API_KEY` (+ ffmpeg), or `local` + GPU |
| Relevance + sentiment      | off                              | `ENABLE_LLM_SCORING=true` + `ANTHROPIC_API_KEY` |
| WhatsApp alerts            | dry-run (logs payload)           | `NOTIFIER=whatsapp` + Meta creds + approved template |
| Email digest               | writes HTML file                 | `SMTP_HOST`/creds + `DIGEST_*`          |

## Layout
```
config.py                       # env-driven settings
app/db/                         # engine + ORM (Keyword, Mention, ArticleCache, YouTubeChannel, ScrapeRun)
app/core/                       # keyword matching + Claude scoring
app/scrapers/                   # base, dawn, configurable, sites, footer
app/newspaper/                  # pipeline + subprocess scan_manager
app/youtube/                    # rss, transcribe, pipeline + subprocess scan_runner
app/digest/                     # builder + sender
app/maintenance/                # retention cleanup
app/notifiers/                  # console, whatsapp, multi
app/scheduler.py, app/main.py   # APScheduler + FastAPI UI
scripts/                        # run_newspaper_once, run_scan, run_youtube, send_digest, seed_keywords, init_db
```
