# -*- coding: utf-8 -*-
"""Web and e-paper results keep separate per-keyword quotas.

A shared count let a burst of recent web articles trim away every print-edition
hit for the same word (کشمیر: 25 articles kept, 21 e-paper pages dropped).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import result_policy
from app.db.base import Base
from app.db.models import Mention
from config import settings


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add(session, module: str, i: int, when: datetime):
    session.add(Mention(
        module=module, external_id=f"{module}-{i}", source="Test",
        title=f"{module} {i}", url=f"https://x/{module}/{i}",
        matched_keywords=["kashmir"], published_at=when, detected_at=when,
    ))


def test_web_and_epaper_each_keep_their_own_limit(session):
    limit = settings.keyword_result_limit
    now = datetime.now(timezone.utc)
    # All web articles are NEWER than all e-paper pages — the exact case that
    # used to let the web results evict every e-paper one.
    for i in range(limit + 10):
        _add(session, "newspaper", i, now - timedelta(hours=i))
    for i in range(limit + 5):
        _add(session, "epaper", i, now - timedelta(days=5, hours=i))
    session.commit()

    result_policy.enforce_limits(session)

    kept = session.execute(select(Mention)).scalars().all()
    web = [m for m in kept if m.module == "newspaper"]
    epaper = [m for m in kept if m.module == "epaper"]
    assert len(web) == limit, "web keeps its own quota"
    assert len(epaper) == limit, "e-paper keeps its own quota, not crowded out"


def test_a_keyword_over_the_limit_in_one_module_is_still_trimmed(session):
    limit = settings.keyword_result_limit
    now = datetime.now(timezone.utc)
    for i in range(limit + 8):
        _add(session, "newspaper", i, now - timedelta(hours=i))
    session.commit()

    result_policy.enforce_limits(session)

    web = session.execute(select(Mention)).scalars().all()
    assert len(web) == limit, "still bounded within a module"
