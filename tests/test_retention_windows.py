"""Scan artefacts (frames, transcripts, bulletins) must expire together."""
from __future__ import annotations

import inspect

from app.maintenance import retention


def test_transcripts_and_screenshots_share_one_cutoff():
    """A transcript whose frames are gone is unusable, and vice versa."""
    src = inspect.getsource(retention.run_retention)
    for target in ("Transcript.created_at", "YouTubeBulletin.created_at"):
        assert f"{target} < cutoff_media" in src, (
            f"{target} must use the same cutoff as screenshots"
        )


def test_cached_text_keeps_its_own_longer_window():
    """Article text is cheap to keep and has no paired media — stays separate."""
    src = inspect.getsource(retention.run_retention)
    assert "ArticleCache.fetched_at < cutoff_tx" in src
    assert "retention_transcripts_days" in src
