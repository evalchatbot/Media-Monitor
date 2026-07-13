"""Notifier interface shared by all alert channels."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class Alert:
    """A single detection ready to be sent to a person."""

    source: str
    title: str
    summary: str
    sentiment: str | None
    url: str
    # Deep-link (YouTube) OR image path (newspaper). Either may be set.
    deeplink: str | None = None
    image_path: Path | None = None
    matched_keywords: list[str] | None = None


class Notifier(Protocol):
    def send(self, alert: Alert) -> bool:
        """Deliver one alert. Return True on success."""
        ...
