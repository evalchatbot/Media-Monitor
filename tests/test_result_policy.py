"""Result retention/capping regression tests.

Run: python -m tests.test_result_policy
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import result_policy
from app.db.base import Base
from app.db.models import Keyword, Mention
from config import settings


def _mention(day: int, labels: list[str]) -> Mention:
    when = datetime.now(timezone.utc) - timedelta(days=day)
    return Mention(
        module="newspaper",
        external_id=f"article-{day}-{'-'.join(labels)}",
        source="Test",
        title=f"Result {day}",
        url=f"https://example.test/{day}",
        matched_keywords=labels,
        published_at=when,
        detected_at=when,
    )


engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)

with Session(engine) as session:
    # Newest-first admission stops exactly at the configured per-keyword cap.
    counts = {"alpha": 24}
    assert result_policy.admit_new(["Alpha"], [], counts) == ["Alpha"]
    assert result_policy.admit_new(["Alpha"], [], counts) == []
    assert counts["alpha"] == settings.keyword_result_limit

    # A shared result remains available to Beta when Alpha is trimmed.
    for day in range(settings.keyword_result_limit + 2):
        labels = ["Alpha", "Beta"] if day == settings.keyword_result_limit + 1 else ["Alpha"]
        session.add(_mention(day, labels))
    session.add(_mention(settings.keyword_result_retention_days + 1, ["Old"]))
    session.commit()

    summary = result_policy.enforce_limits(session)
    assert summary["expired"] == 1
    assert summary["trimmed"] == 2

    rows = session.execute(select(Mention)).scalars().all()
    alpha = [m for m in rows if "Alpha" in (m.matched_keywords or [])]
    beta = [m for m in rows if "Beta" in (m.matched_keywords or [])]
    old = [m for m in rows if "Old" in (m.matched_keywords or [])]
    assert len(alpha) == settings.keyword_result_limit
    assert len(beta) == 1
    assert not old

    # UI × is a soft delete; re-adding reactivates the same keyword and keeps
    # its retained Mention associations.
    keyword = Keyword(text="Retained", language="en", module="newspaper", active=True)
    retained = _mention(0, ["Retained"])
    session.add_all([keyword, retained])
    session.commit()

    from app import main

    main.ui_delete_keyword(keyword.id, session)
    session.refresh(keyword)
    assert keyword.active is False
    assert session.get(Mention, retained.id) is not None

    original_start = main._start_keyword_scan
    main._start_keyword_scan = lambda kw: {
        "live_started": False, "articles_checked": 0,
        "pages_checked": 0, "mentions": 0,
    }
    try:
        main.ui_add_keyword("retained", "en", session)
    finally:
        main._start_keyword_scan = original_start
    session.refresh(keyword)
    assert keyword.active is True
    assert session.get(Mention, retained.id) is not None

print("ALL PASS: retention, cap, isolation, soft delete, and reactivation")
