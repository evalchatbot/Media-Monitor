"""LLM relevance + sentiment scoring via Claude.

Off by default (ENABLE_LLM_SCORING=false / no API key). When enabled, each
mention gets:
  - relevance: Directly Relevant | Tangentially Relevant | Not Relevant
  - sentiment: Positive | Critical | Neutral
  - summary:   one-line summary for the alert

Kept as a pure function of (title, text, keywords) so it works identically for
newspaper articles and (later) YouTube transcript segments.
"""
from __future__ import annotations

import json
import logging

from config import settings

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a media-monitoring analyst. Given a news item and the keywords "
    "that matched it, judge relevance and sentiment. Respond ONLY with JSON: "
    '{"relevance": "Directly Relevant|Tangentially Relevant|Not Relevant", '
    '"sentiment": "Positive|Critical|Neutral", "summary": "one sentence"}.'
)


def score(title: str, text: str, keywords: list[str]) -> dict | None:
    """Return {relevance, sentiment, summary} or None if scoring is disabled."""
    if not (settings.enable_llm_scoring and settings.anthropic_api_key):
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        user = (
            f"Keywords matched: {', '.join(keywords)}\n\n"
            f"Title: {title}\n\nText:\n{text[:6000]}"
        )
        msg = client.messages.create(
            model=settings.llm_model,
            max_tokens=300,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        raw = msg.content[0].text.strip()
        # Be tolerant of code fences.
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("LLM scoring failed: %s", exc)
        return None
