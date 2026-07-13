"""Send (or preview) the daily digest.

If SMTP is configured, email the digest to the recipient list. If not, write it
to data/digests/digest_YYYY-MM-DD.html so it can be previewed before email is
wired up — the job never fails just because credentials are missing.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import BASE_DIR, settings

from app.digest.builder import PKT, build_digest

logger = logging.getLogger(__name__)

_DIGEST_DIR = BASE_DIR / "data" / "digests"


def send_daily_digest(hours: int = 24) -> dict:
    """Build and deliver the digest. Returns a summary dict."""
    html_doc, count = build_digest(hours=hours)
    subject = f"Media Monitor — Daily Digest ({datetime.now(PKT):%d %b %Y}) — {count} detections"

    if settings.smtp_configured:
        try:
            _send_smtp(subject, html_doc)
            logger.info("Digest emailed to %s (%d detections)", settings.digest_recipient_list, count)
            return {"delivered": "email", "recipients": settings.digest_recipient_list, "count": count}
        except Exception as exc:
            logger.exception("Digest email failed; saving to file instead")
            path = _save_file(html_doc)
            return {"delivered": "file (email failed)", "path": str(path), "count": count, "error": str(exc)}

    path = _save_file(html_doc)
    logger.info("SMTP not configured; digest written to %s (%d detections)", path, count)
    return {"delivered": "file", "path": str(path), "count": count}


def _send_smtp(subject: str, html_doc: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.digest_sender
    msg["To"] = ", ".join(settings.digest_recipient_list)
    msg.attach(MIMEText("Your mail client does not support HTML.", "plain"))
    msg.attach(MIMEText(html_doc, "html"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.digest_sender, settings.digest_recipient_list, msg.as_string())


def _save_file(html_doc: str) -> Path:
    _DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = _DIGEST_DIR / f"digest_{datetime.now(timezone.utc).astimezone(PKT):%Y-%m-%d}.html"
    path.write_text(html_doc, encoding="utf-8")
    return path
