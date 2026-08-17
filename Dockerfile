# Media Monitor — container for always-on hosts (Railway/Render/Fly/VPS).
#
# Notes:
# - `playwright install --with-deps chromium` pulls the browser AND its system
#   libraries — required for the website scrapers and screenshots.
# - ffmpeg is required for YouTube audio normalization before Groq Whisper.
# - fonts-dejavu-core guarantees the screenshot footer has a real font.
# - Run exactly ONE uvicorn worker: live-search jobs and their result cards live
#   in process memory, so a second worker would serve job ids it cannot see.
# - The host must provide env vars (DATABASE_URL, GROQ_API_KEY, …). STORAGE_DIR
#   holds only ephemeral e-paper clippings now (swept on restart and hourly), so
#   a persistent volume is optional — a container-local path is fine.
# - Each concurrent article screenshot runs its own Chromium; on a small
#   container set EPAPER_SHOT_WORKERS=2 (or 1) to cap memory.

# -bookworm pin matters: plain -slim now tracks Debian 13 (trixie), where the
# font packages Playwright 1.49's --with-deps requests (ttf-unifont et al) were
# renamed — the browser install dies with "no installation candidate".
# Playwright 1.49 officially supports Debian 12.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Railway/Render inject PORT; default for local `docker run`.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
