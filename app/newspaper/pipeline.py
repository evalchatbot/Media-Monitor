"""Newspaper monitoring pipeline: scrape -> cache -> match -> score -> store -> alert.

`run_newspaper_scan()` is what the scheduler fires every N minutes (all active
keywords). `run_newspaper_scan(keyword_ids=[id])` is what a per-keyword "Run
scan" button fires — it re-matches just that keyword.

Article bodies are cached on first fetch (ArticleCache), so matching is cheap
and repeatable: a per-keyword scan, or a newly added keyword, re-checks already
fetched articles without re-scraping. Mentions are upserted — if an article was
already detected for one keyword and now matches another, the new keyword is
added (and an alert fires for it) rather than creating a duplicate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import settings

from app.core import scoring
from app.core.keywords import find_matches
from app.core import result_policy
from app.db.base import SessionLocal
from app.db.models import ArticleCache, Keyword, Mention, ScrapeRun
from app.notifiers import get_notifier
from app.notifiers.base import Alert
from app.scrapers.base import Article, ScrapeBlockedError
from app.scrapers.sites import build_scrapers

logger = logging.getLogger(__name__)


def _active_keywords(session, keyword_ids: list[int] | None) -> list[tuple[str, str]]:
    # Newspaper scans only use keywords scoped to the newspaper module.
    stmt = select(Keyword).where(Keyword.active.is_(True), Keyword.module == "newspaper")
    if keyword_ids:
        stmt = stmt.where(Keyword.id.in_(keyword_ids))
    rows = session.execute(stmt).scalars().all()
    return [(k.text, k.language) for k in rows]


def _registered_scrapers():
    """All publications to scan (Dawn + config-driven sites). See scrapers/sites.py."""
    return build_scrapers()


def run_newspaper_scan(keyword_ids: list[int] | None = None, uncapped: bool = False) -> dict:
    """Run one scan across all publications.

    keyword_ids : restrict matching to these keyword ids (per-keyword scan).
                  None = all active keywords (scheduled full scan).
    uncapped    : if True, fetch bodies for EVERY front-page article this run
                  (used for manual scans so one click covers the whole page).
                  Scheduled scans leave this False to stay bounded.
    """
    session = SessionLocal()
    notifier = get_notifier()
    summary = {"scrapers": 0, "articles": 0, "cached": 0, "mentions": 0, "alerts": 0, "blocked": 0}

    try:
        keywords = _active_keywords(session, keyword_ids)
        if not keywords:
            logger.info("No active keywords to match; nothing to do.")
            return summary
        # Live/current matches are allowed to enter, then the shared policy
        # keeps the newest 25 and evicts older associations.

        # PER-SITE new-fetch cap (None = unlimited on manual/uncapped scans).
        # This MUST be per-site: a single shared pool was drained entirely by the
        # first scraper (Dawn, 200+ articles), so every later site — including all
        # three Urdu papers — cached nothing and Urdu keywords could never match.
        per_site_cap = None if uncapped else settings.newspaper_max_articles_per_scan

        for scraper in _registered_scrapers():
            summary["scrapers"] += 1
            budget = {"remaining": per_site_cap}  # fresh budget for THIS site
            run = ScrapeRun(source=scraper.name, started_at=datetime.now(timezone.utc))
            session.add(run)
            session.commit()

            try:
                articles = scraper.list_articles()
            except ScrapeBlockedError as exc:
                _finish_run(session, run, "blocked", str(exc))
                summary["blocked"] += 1
                _notify_admin_blocked(notifier, scraper.name, str(exc))
                scraper.close()
                continue
            except Exception as exc:
                _finish_run(session, run, "error", str(exc))
                logger.exception("Scraper %s failed", scraper.name)
                scraper.close()
                continue

            try:
                run.articles_found = len(articles)
                summary["articles"] += len(articles)

                # 1. Ensure article bodies are cached (bounded by shared budget).
                cache = _ensure_cached(session, scraper, articles, summary, budget)

                # 2. Match keywords against cached text; upsert + alert.
                for art in articles:
                    ca = cache.get(art.external_id)
                    if ca is None:
                        continue  # body not fetched this run (cap); next run picks it up
                    if _match_and_store(session, scraper, ca, keywords, notifier, summary):
                        run.mentions_created += 1

                _finish_run(session, run, "ok", None)
            finally:
                scraper.close()

        summary["policy"] = result_policy.enforce_limits(session)
        return summary
    finally:
        session.close()


# In-process corpus cache so a ⚡ Quick Scan doesn't re-pull hundreds of article
# bodies from a REMOTE Postgres on every click. A cheap (count, max id) stamp
# detects when a scan subprocess added articles and triggers a reload.
_CORPUS: dict = {"stamp": None, "rows": []}


def _load_corpus(session) -> list:
    from sqlalchemy import func as _f

    stamp = tuple(session.execute(
        select(_f.count(ArticleCache.id), _f.coalesce(_f.max(ArticleCache.id), 0))
        .where(ArticleCache.module == "newspaper")
    ).one()) + (result_policy.cutoff().date().isoformat(),)
    if _CORPUS["stamp"] == stamp:
        return _CORPUS["rows"]
    rows = session.execute(
        select(ArticleCache.external_id, ArticleCache.source, ArticleCache.section,
               ArticleCache.title, ArticleCache.url, ArticleCache.body,
               ArticleCache.fetched_at)
        .where(
            ArticleCache.module == "newspaper",
            ArticleCache.fetched_at >= result_policy.cutoff(),
        )
        .order_by(ArticleCache.fetched_at.desc())
    ).all()
    _CORPUS["stamp"] = stamp
    _CORPUS["rows"] = rows
    logger.info("quick-scan corpus loaded: %d articles", len(rows))
    return rows


def warm_quick_corpus() -> None:
    """Pre-load the corpus (called in a background thread at app startup so the
    first ⚡ Quick Scan is as fast as every later one). DB-only; thread-safe."""
    try:
        session = SessionLocal()
        try:
            _load_corpus(session)
        finally:
            session.close()
    except Exception as exc:  # pragma: no cover
        logger.warning("quick-scan corpus warm-up failed: %s", exc)


def run_quick_match(keyword_ids: list[int] | None = None, _retry: bool = True) -> dict:
    """Instant scan: match keywords against EVERYTHING already stored — every
    cached website article and every read e-paper page. No scraping, no browser,
    no LLM, and (crucially, for a remote Postgres) no per-row round-trips: the
    corpus comes from an in-process cache, matching runs in memory, and all new
    hits land in ONE commit. This is what the per-keyword ⚡ Scan button calls,
    inline in the web process. New mentions look exactly like scan mentions,
    minus the screenshot (no page fetch)."""
    session = SessionLocal()
    notifier = get_notifier()
    summary = {"articles_checked": 0, "pages_checked": 0, "mentions": 0, "alerts": 0}
    try:
        keywords = _active_keywords(session, keyword_ids)
        if not keywords:
            return summary
        # This is a scan budget, not the number already retained. We inspect the
        # newest 25 matching article candidates even when a keyword already has
        # 25 older results; enforce_limits() then keeps the newest 25 globally.
        counts = {(k[0] or "").casefold(): 0 for k in keywords}

        articles = _load_corpus(session)
        existing = {
            m.external_id: m
            for m in session.execute(
                select(Mention).where(Mention.module == "newspaper")
            ).scalars()
        }

        to_alert: list[tuple[Mention, list[str]]] = []
        pending_commit = 0
        for ca in articles:
            if result_policy.all_full(counts):
                summary["limit_reached"] = True
                break
            summary["articles_checked"] += 1
            haystack = f"{ca.title}\n{ca.body}"
            matches = find_matches(haystack, keywords)
            if not matches:
                continue
            matched_kw = sorted({m.keyword for m in matches})
            admitted = []
            for label in matched_kw:
                folded = label.casefold()
                if counts.get(folded, 0) >= settings.keyword_result_limit:
                    continue
                counts[folded] = counts.get(folded, 0) + 1
                admitted.append(label)
            if not admitted:
                continue
            mention = existing.get(ca.external_id)
            if mention is not None:
                if mention.published_at is None:
                    mention.published_at = ca.fetched_at
                present = {(k or "").casefold() for k in (mention.matched_keywords or [])}
                new_kw = [k for k in admitted if k.casefold() not in present]
                if not new_kw:
                    continue
                mention.matched_keywords = sorted(
                    set(mention.matched_keywords or []) | set(new_kw)
                )
                to_alert.append((mention, new_kw))
                pending_commit += 1
            else:
                snippet = _make_snippet(haystack, admitted)
                mention = Mention(
                    module="newspaper",
                    external_id=ca.external_id,
                    source=ca.source,
                    section=ca.section,
                    title=ca.title,
                    url=ca.url,
                    matched_keywords=admitted,
                    snippet=snippet,
                    summary=snippet,
                    published_at=ca.fetched_at,
                )
                session.add(mention)
                existing[ca.external_id] = mention
                summary["mentions"] += 1
                to_alert.append((mention, admitted))
                pending_commit += 1

            # Flush early so the UI can stream cards while matching continues.
            if pending_commit >= 5:
                try:
                    session.commit()
                    pending_commit = 0
                except IntegrityError:
                    session.rollback()
                    session.close()
                    if _retry:
                        return run_quick_match(keyword_ids, _retry=False)
                    raise

        try:
            if pending_commit:
                session.commit()
        except IntegrityError:
            # A concurrent scan inserted one of the same articles between our
            # read and this commit. Start over on fresh state (once).
            session.rollback()
            session.close()
            if _retry:
                return run_quick_match(keyword_ids, _retry=False)
            raise

        deferred_alerts = [(mention.id, kws) for mention, kws in to_alert]

        from app.epaper.pipeline import match_stored_pages

        match_stored_pages(
            session,
            keywords,
            notifier,
            summary,
            since_days=settings.keyword_result_retention_days,
            cap_new=True,
            deferred_alerts=deferred_alerts,
        )
        result_policy.enforce_limits(session)

        # Alert only for associations that survived the global newest-25 policy.
        for mention_id, candidate_kws in deferred_alerts:
            mention = session.get(Mention, mention_id)
            if mention is None:
                continue
            retained = {
                (label or "").casefold(): label
                for label in (mention.matched_keywords or [])
            }
            kws = [
                retained[k.casefold()]
                for k in candidate_kws
                if k.casefold() in retained
            ]
            if not kws:
                continue
            media = mention.keyword_media or {}
            image_path = next(
                (
                    path for label, path in media.items()
                    if label.casefold() == kws[0].casefold()
                ),
                mention.screenshot_path,
            )
            _send_alert(notifier, session, mention, image_path, summary, kws)
        return summary
    finally:
        session.close()


def _finish_run(session, run: ScrapeRun, status: str, error: str | None) -> None:
    run.status = status
    run.error = error
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def _ensure_cached(session, scraper, articles: list[Article], summary, budget: dict) -> dict:
    """Return {external_id: ArticleCache} for the given articles, fetching and
    caching bodies for any not yet cached. `budget["remaining"]` bounds new
    fetches across the whole run (None = unlimited)."""
    ext_ids = [a.external_id for a in articles]
    existing = {
        c.external_id: c
        for c in session.execute(
            select(ArticleCache).where(
                ArticleCache.module == "newspaper", ArticleCache.external_id.in_(ext_ids)
            )
        ).scalars()
    }

    to_fetch = [a for a in articles if a.external_id not in existing]
    remaining = budget["remaining"]
    if remaining is not None:
        if remaining <= 0:
            logger.info("%s: fetch budget exhausted; matching cached only this run", scraper.name)
            to_fetch = []
        elif len(to_fetch) > remaining:
            logger.info(
                "%s: %d new articles; fetching %d (shared budget), rest next scan",
                scraper.name, len(to_fetch), remaining,
            )
            to_fetch = to_fetch[:remaining]
    elif to_fetch:
        logger.info("%s: full scan — fetching all %d new article bodies", scraper.name, len(to_fetch))

    for art in to_fetch:
        body = scraper.fetch_body(art) if hasattr(scraper, "fetch_body") else art.body
        ca = ArticleCache(
            module="newspaper",
            external_id=art.external_id,
            source=art.source,
            section=art.section,
            title=art.title,
            url=art.url,
            body=body,
        )
        session.add(ca)
        try:
            session.commit()
            existing[art.external_id] = ca
            summary["cached"] += 1
            if budget["remaining"] is not None:
                budget["remaining"] -= 1
        except IntegrityError:
            session.rollback()
            ca = session.execute(
                select(ArticleCache).where(
                    ArticleCache.module == "newspaper",
                    ArticleCache.external_id == art.external_id,
                )
            ).scalar_one()
            existing[art.external_id] = ca

    return existing


def _match_and_store(session, scraper, ca: ArticleCache, keywords, notifier, summary) -> bool:
    """Match keywords against one cached article; create or update its mention.

    `scraper=None` (Quick Scan) skips the screenshot capture — everything else
    is identical, so a quick hit and a scan hit are the same Mention row."""
    haystack = f"{ca.title}\n{ca.body}"
    matches = find_matches(haystack, keywords)
    if not matches:
        return False

    matched_kw = sorted({m.keyword for m in matches})
    snippet = _make_snippet(haystack, matched_kw)

    mention = session.execute(
        select(Mention).where(
            Mention.module == "newspaper", Mention.external_id == ca.external_id
        )
    ).scalar_one_or_none()

    if mention is not None:
        # Already detected — add any keywords not seen before, alert only on those.
        new_kw = [k for k in matched_kw if k not in (mention.matched_keywords or [])]
        if not new_kw:
            return False
        mention.matched_keywords = sorted(set(mention.matched_keywords or []) | set(matched_kw))
        if mention.published_at is None:
            mention.published_at = ca.fetched_at
        # Drop old screenshot so backfill re-captures with highlights for current keywords only.
        mention.screenshot_path = None
        mention.full_screenshot_path = None
        session.commit()
        _send_alert(notifier, session, mention, mention.screenshot_path, summary, new_kw)
        return False

    # New detection — optional LLM scoring, then screenshot + store + alert.
    scored = scoring.score(ca.title, ca.body, matched_kw) or {}
    relevance = scored.get("relevance")
    sentiment = scored.get("sentiment")
    llm_summary = scored.get("summary") or snippet
    if relevance == "Not Relevant":
        logger.info("Skipping '%s' — LLM marked Not Relevant", ca.title[:60])
        return False

    full_path = crop_path = None
    if scraper is not None:
        art = Article(source=ca.source, title=ca.title, url=ca.url, section=ca.section,
                      external_id=ca.external_id)
        crop_selector = getattr(scraper, "ARTICLE_CROP_SELECTOR", None)
        full_path, crop_path = scraper.capture_screenshots(
            art, settings.storage_dir / scraper.name, crop_selector,
            highlight=matched_kw,
        )

    mention = Mention(
        module="newspaper",
        external_id=ca.external_id,
        source=ca.source,
        section=ca.section,
        title=ca.title,
        url=ca.url,
        matched_keywords=matched_kw,
        snippet=snippet,
        summary=llm_summary,
        relevance=relevance,
        sentiment=sentiment,
        screenshot_path=str(crop_path) if crop_path else None,
        full_screenshot_path=str(full_path) if full_path else None,
        published_at=ca.fetched_at,
    )
    session.add(mention)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False

    summary["mentions"] += 1
    _send_alert(notifier, session, mention, crop_path or full_path, summary, matched_kw)
    return True


def _send_alert(notifier, session, mention: Mention, image_path, summary, keywords) -> None:
    ok = notifier.send(
        Alert(
            source=mention.source,
            title=mention.title,
            summary=mention.summary or "",
            sentiment=mention.sentiment,
            url=mention.url,
            image_path=image_path,
            matched_keywords=keywords,
        )
    )
    if ok:
        mention.notified = True
        session.commit()
        summary["alerts"] += 1


def _make_snippet(text: str, keywords: list[str], width: int = 160) -> str:
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw.lower())
        if idx != -1:
            start = max(0, idx - width // 2)
            end = min(len(text), idx + len(kw) + width // 2)
            return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return text[:width].strip()


def _notify_admin_blocked(notifier, source: str, detail: str) -> None:
    notifier.send(
        Alert(source="ADMIN", title=f"Scrape blocked: {source}", summary=detail,
              sentiment=None, url="")
    )
