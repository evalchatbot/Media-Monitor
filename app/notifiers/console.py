"""Console/file notifier — the default dev channel.

Prints a formatted alert to the log and appends it to data/alerts.log, so the
full pipeline is testable end-to-end with zero external accounts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from config import BASE_DIR

from app.notifiers.base import Alert

logger = logging.getLogger(__name__)

_ALERT_LOG = BASE_DIR / "data" / "alerts.log"


class ConsoleNotifier:
    def send(self, alert: Alert) -> bool:
        kws = ", ".join(alert.matched_keywords or [])
        lines = [
            "=" * 60,
            f"🔔 ALERT — {alert.source}",
            f"   {alert.title}",
            f"   sentiment: {alert.sentiment or 'n/a'} | keywords: {kws}",
            f"   {alert.summary}",
        ]
        if alert.deeplink:
            lines.append(f"   ▶ {alert.deeplink}")
        if alert.image_path:
            lines.append(f"   🖼 {alert.image_path}")
        lines.append(f"   {alert.url}")
        lines.append("=" * 60)
        message = "\n".join(lines)

        logger.info("\n%s", message)
        _ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ALERT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}]\n{message}\n")
        return True
