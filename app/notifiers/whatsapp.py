"""WhatsApp notifier via the Meta (WhatsApp Business) Cloud API.

Behaviour
---------
- Credentials present  -> sends a real message (image + caption if a screenshot
  is available, else text) via the Cloud API.
- Credentials missing  -> DRY RUN: logs the exact payload it would send, so the
  pipeline runs today and going live later is just adding the token. The console
  notifier still records every alert to data/alerts.log regardless.

Credentials (see .env.example):
  WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_RECIPIENT

IMPORTANT (production): Meta requires a pre-approved message *template* to start
a conversation outside the 24-hour customer-service window. Free-text/image
sends below only work inside an open session. For always-on alerting you must
register an alert template and send via the template payload — flagged here so
it's handled before go-live.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from config import settings

from app.notifiers.base import Alert

logger = logging.getLogger(__name__)

_API = "https://graph.facebook.com/v21.0"


class WhatsAppNotifier:
    def __init__(self):
        self.phone_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_access_token
        self.recipient = settings.whatsapp_recipient
        self.enabled = bool(self.phone_id and self.token and self.recipient)
        if not self.enabled:
            logger.info("WhatsApp notifier in DRY-RUN mode (no credentials set).")

    # -- public ----------------------------------------------------------
    def send(self, alert: Alert) -> bool:
        caption = self._format(alert)
        image = alert.image_path if alert.image_path and Path(alert.image_path).exists() else None

        if not self.enabled:
            logger.info(
                "[WhatsApp DRY-RUN] -> %s\n  %s\n  image: %s",
                self.recipient or "<no recipient>",
                caption.replace("\n", " | "),
                image,
            )
            return False  # not delivered; console notifier keeps the record

        try:
            if image:
                media_id = self._upload_media(image)
                if media_id:
                    return self._post_message(
                        {"type": "image", "image": {"id": media_id, "caption": caption}}
                    )
            return self._post_message({"type": "text", "text": {"body": caption}})
        except Exception as exc:
            logger.error("WhatsApp send failed: %s", exc)
            return False

    # -- internals -------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _upload_media(self, path: Path) -> str | None:
        with open(path, "rb") as fh:
            files = {"file": (Path(path).name, fh, "image/png")}
            data = {"messaging_product": "whatsapp", "type": "image/png"}
            resp = httpx.post(
                f"{_API}/{self.phone_id}/media",
                headers=self._headers(),
                data=data,
                files=files,
                timeout=60,
            )
        resp.raise_for_status()
        return resp.json().get("id")

    def _post_message(self, payload: dict) -> bool:
        body = {"messaging_product": "whatsapp", "to": self.recipient, **payload}
        resp = httpx.post(
            f"{_API}/{self.phone_id}/messages",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        return True

    def _format(self, alert: Alert) -> str:
        parts = [f"*{alert.source}*", alert.title]
        if alert.sentiment:
            parts.append(f"Sentiment: {alert.sentiment}")
        if alert.matched_keywords:
            parts.append("Keywords: " + ", ".join(alert.matched_keywords))
        if alert.summary:
            parts.append(alert.summary)
        parts.append(alert.deeplink or alert.url)
        return "\n".join(p for p in parts if p)
