# Operations Runbook

Practical procedures for running and maintaining Media Monitoring. Commands are
Windows PowerShell from the project root.

## 1. First-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env            # runs fine unedited; add credentials to go live
python -m scripts.init_db
python -m scripts.seed_keywords   # optional sample keywords
```

`ffmpeg` on PATH is required only for YouTube transcription / live streams.

## 2. Running

```powershell
uvicorn app.main:app --reload
#   Admin UI:  http://127.0.0.1:8000/
#   API docs:  http://127.0.0.1:8000/docs
```

The scheduler starts with the app (newspapers 30 min, YouTube 10 min, digest
07:00 PKT, retention 04:00 PKT). To run the UI **without** background scans:
`set SCHEDULER_ENABLED=false` before starting.

Manual / testing without the server:
```powershell
python -m scripts.run_newspaper_once     # one newspaper scan
python -m scripts.run_youtube            # one YouTube upload scan
python -m scripts.run_youtube --live     # one live-stream check
python -m scripts.send_digest            # build + send/preview the digest
```

## 3. Turning on the live integrations (credentials)

Each is a paste into `.env`, then restart the app. Nothing else to change.

**Claude scoring (relevance + sentiment)**
```
ENABLE_LLM_SCORING=true
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5      # cheapest; or claude-sonnet-5 for higher quality
```

**Email digest (Gmail example)** — enable 2-Step Verification, then create an
App Password (Google Account → Security → App passwords):
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-char app password>
DIGEST_SENDER=you@gmail.com
DIGEST_RECIPIENTS=person1@x.com,person2@y.com
```
Without SMTP, the digest is written to `data/digests/digest_YYYY-MM-DD.html`.

**WhatsApp (Meta Cloud API)** — from developers.facebook.com → your WhatsApp
Business app:
```
NOTIFIER=whatsapp
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_RECIPIENT=+9230xxxxxxxx
```
Note: business-initiated alerts outside the 24-hour window require an **approved
message template** registered in the Meta dashboard. Without credentials the
WhatsApp path dry-runs (logs the exact payload); console/`alerts.log` always records.

**YouTube transcription** (in-video keyword detection + timestamps):
```
YOUTUBE_TRANSCRIBER=openai        # needs OPENAI_API_KEY + ffmpeg (~$0.006/min)
OPENAI_API_KEY=sk-...
# or, with a GPU:
# YOUTUBE_TRANSCRIBER=local       # pip install faster-whisper + CUDA + ffmpeg
```

**Live streams:** additionally set `YOUTUBE_LIVE_ENABLED=true` (needs ffmpeg + a
non-stub transcriber). The live-check job is only registered when this is true.

## 4. Add a keyword
Admin UI → **Keywords** → type the word, pick language, **+ Add keyword**.
Edit / pause (⏸) / delete inline. Paused keywords are skipped by scans. Changes
take effect on the next scan (≤ the scan interval). No restart, no deploy.

## 5. Add a YouTube channel
Admin UI → **Channels** → paste a channel URL, `@handle`, or `UC…` id → **Add**.
If it can't resolve, paste the full channel URL or the `UC…` id from the
channel's page source.

## 6. Add a newspaper publication
Most sites are one config entry. Edit `app/scrapers/sites.py` and append a
`SiteConfig` to `SITE_CONFIGS`:

```python
SiteConfig(
    name="slug",                       # short id, e.g. "newpaper"
    source="Display Name",
    base_url="https://example.com",
    sections={"front": "https://example.com/", "opinion": "https://example.com/opinion"},
    language="en",                     # or "ur"
    article_url_pattern=r"example\.com/story/\d+",  # regex an article href must match
    # link_selector="h2.title a",      # optional: CSS for headline anchors (else URL pattern)
    crop_selector="article, main",     # optional: element to crop for the screenshot
),
```

Find the right values by probing the live listing page:
```powershell
python -c "from playwright.sync_api import sync_playwright as s;
p=s().start();b=p.chromium.launch();pg=b.new_context().new_page();
pg.goto('https://example.com/', wait_until='domcontentloaded');pg.wait_for_timeout(2500);
print([a for a in pg.eval_on_selector_all('a[href]','e=>e.map(x=>x.getAttribute(\"href\"))') if a][:40])"
```
Then verify end-to-end:
```powershell
python -c "from app.scrapers.configurable import ConfigurableScraper as C;
from app.scrapers.sites import SITE_CONFIGS as S;
sc=C(next(c for c in S if c.name=='slug'), respect_robots=False);
a=sc.list_articles();print('articles',len(a));print(sc.fetch_body(a[0])[:200]);sc.close()"
```
A bespoke site (custom logic, e.g. Dawn) gets its own subclass of `BaseScraper`
added in `sites.py:build_scrapers()`.

> **Geo (urdu.geo.tv)** is intentionally not scraped — it serves headless
> browsers an empty shell and loads articles from an internal API with no clean
> public endpoint. Monitor Geo via its YouTube channel instead, or build a
> dedicated API scraper if the endpoint can be authenticated.

## 7. Fix a broken scraper
Symptom: a site's scans return 0 articles, or bodies are empty.

1. Check `ScrapeRun` rows (status `error`/`blocked`) or the app log.
2. Re-run the probe + verify commands from §6 for that slug.
3. If the site redesigned, update `link_selector` / `article_url_pattern` /
   `crop_selector` in its `SiteConfig`.
4. If it now blocks bots: scraping already uses a real browser; if still blocked,
   it will raise `ScrapeBlockedError` and send an ADMIN alert — the site likely
   added stronger protection and needs a bespoke approach.
5. If bodies are short (generic extractor under-pulls), add a `body_selector` to
   the `SiteConfig`.

## 8. Data retention & backup
- Retention runs daily 04:00 PKT: screenshots > `RETENTION_SCREENSHOTS_DAYS`,
  cache/transcripts > `RETENTION_TRANSCRIPTS_DAYS`, scrape logs > `RETENTION_LOGS_DAYS`.
  Run on demand: `python -c "from app.maintenance.retention import run_retention; print(run_retention())"`
- **Backup** = copy `data/media_monitoring.db` (+ `data/storage/` for images).
  SQLite is a single file; stop the app or copy the `-wal`/`-shm` files too.

## 9. Restart / recovery
No special steps. On restart the SQLite data persists and APScheduler reloads its
persisted jobs from the DB, so all monitoring resumes automatically (a past-due
scan may fire immediately). Verify: after `uvicorn` starts, the log prints
"Scheduler started: newspaper/…, youtube/…, digest …, retention …".

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Clicked "Run scan", nothing appears | First scan of a cold site fetches bounded new bodies; coverage grows over successive scans. Watch the status bar; results auto-load when done. |
| A keyword's article isn't detected | It may be beyond the per-scan fetch cap on a cold cache — scan again or wait for the scheduled scans to warm the cache. |
| WhatsApp not delivering | Without Meta creds it dry-runs (see `data/alerts.log`). With creds, confirm an **approved template** and the recipient. |
| Digest went to a file, not email | `SMTP_HOST`/creds not set — see §3. |
| YouTube shows videos but no in-video hits | `YOUTUBE_TRANSCRIBER=stub` (default) matches title/description only; set `openai`/`local` + ffmpeg. |
| Server unreachable right after a scan starts | Ensure scans run via the subprocess runners (they do by default) — never call the pipeline inline in the web process. |
| Port already in use | An old `uvicorn` is still running; stop it before restarting. |
