"""Notifier factory.

The console notifier ALWAYS runs (so every alert is recorded to data/alerts.log).
When NOTIFIER=whatsapp, a WhatsApp notifier is added on top — it sends for real
if credentials exist, or dry-runs (logs the payload) if they don't.
"""
from __future__ import annotations

from config import settings

from app.notifiers.base import Alert, Notifier
from app.notifiers.console import ConsoleNotifier


class MultiNotifier:
    """Fan an alert out to several channels; True if any delivered."""

    def __init__(self, notifiers: list[Notifier]):
        self._notifiers = notifiers

    def send(self, alert: Alert) -> bool:
        results = []
        for n in self._notifiers:
            try:
                results.append(bool(n.send(alert)))
            except Exception:
                results.append(False)
        return any(results)


def get_notifier() -> Notifier:
    channels: list[Notifier] = [ConsoleNotifier()]  # always keep a local record
    if settings.notifier == "whatsapp":
        from app.notifiers.whatsapp import WhatsAppNotifier

        channels.append(WhatsAppNotifier())
    return channels[0] if len(channels) == 1 else MultiNotifier(channels)
