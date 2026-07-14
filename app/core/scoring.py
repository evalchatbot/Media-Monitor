"""LLM relevance + sentiment scoring.

Off by default (ENABLE_LLM_SCORING=false / no API key). When enabled, each
mention gets:
  - relevance: Directly Relevant | Tangentially Relevant | Not Relevant
  - sentiment: Positive | Critical | Neutral
  - summary:   one-line summary for the alert

Provider: Groq (GROQ_API_KEY, default) or Anthropic (ANTHROPIC_API_KEY) — the
first available key wins, same policy as the e-paper reader. Kept as a pure
function of (title, text, keywords) so it works identically for website
articles and e-paper pages.
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

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def score(title: str, text: str, keywords: list[str]) -> dict | None:
    """Return {relevance, sentiment, summary} or None if scoring is disabled."""
    if not settings.enable_llm_scoring:
        return None
    if not (settings.groq_api_key or settings.anthropic_api_key):
        return None
    user = (
        f"Keywords matched: {', '.join(keywords)}\n\n"
        f"Title: {title}\n\nText:\n{text[:6000]}"
    )
    try:
        raw = _ask_groq(user) if settings.groq_api_key else _ask_anthropic(user)
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("LLM scoring failed: %s", exc)
        return None


def _ask_groq(user: str) -> str:
    import httpx

    r = httpx.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": settings.groq_model,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
        },
        timeout=60,
    )
    r.raise_for_status()
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _ask_anthropic(user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.llm_model,
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text
