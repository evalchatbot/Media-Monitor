"""YouTube upload discovery — Data API preferred, public RSS as fallback."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

from config import settings

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
_API = "https://www.googleapis.com/youtube/v3"


@dataclass
class Video:
    video_id: str
    title: str
    description: str
    published: datetime | None
    channel_name: str
    channel_id: str
    url: str
    duration_seconds: int | None = None
    live: bool = False


def resolve_channel_id(url_or_handle: str) -> tuple[str | None, str]:
    """Return (channel_id, channel_name) for a channel URL / @handle / UC-id."""
    s = url_or_handle.strip()
    if re.fullmatch(r"UC[\w-]{20,}", s):
        return s, ""

    if s.startswith("http"):
        url = s
    elif s.startswith("@"):
        url = f"https://www.youtube.com/{s}"
    else:
        url = f"https://www.youtube.com/@{s}"

    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            params={"hl": "en"},
            cookies={"CONSENT": "YES+cb", "SOCS": "CAI"},
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("resolve_channel_id failed for %s: %s", url_or_handle, exc)
        return None, ""

    m = (
        re.search(r'"channelId":"(UC[\w-]{20,})"', html)
        or re.search(r'"externalId":"(UC[\w-]{20,})"', html)
        or re.search(r'"browseId":"(UC[\w-]{20,})"', html)
        or re.search(r"channel/(UC[\w-]{20,})", html)
    )
    cid = m.group(1) if m else None
    nm = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    name = nm.group(1) if nm else ""
    return cid, name


def uploads_playlist_id(channel_id: str) -> str:
    """YouTube uploads playlist = UU + channel_id[2:]."""
    if channel_id.startswith("UC") and len(channel_id) > 2:
        return "UU" + channel_id[2:]
    return ""


def fetch_uploads(channel_id: str, *, max_results: int = 30,
                  playlist_id: str = "") -> list[Video]:
    """Fetch recent uploads. Prefer Data API playlistItems; fall back to RSS."""
    if settings.youtube_api_key:
        vids = _fetch_via_api(channel_id, playlist_id or uploads_playlist_id(channel_id),
                              max_results=max_results)
        if vids:
            return vids
    return _fetch_via_rss(channel_id)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_publish_range(
    published: datetime | None,
    published_after: datetime,
    published_before: datetime,
) -> bool:
    if published is None:
        return False
    pub = _as_utc(published)
    return published_after <= pub <= published_before


def fetch_uploads_in_range(
    channel_id: str,
    *,
    published_after: datetime,
    published_before: datetime,
    playlist_id: str = "",
    max_pages: int = 15,
) -> list[Video]:
    """Fetch uploads whose publish time falls in [after, before], excluding live streams."""
    after = _as_utc(published_after)
    before = _as_utc(published_before)
    if settings.youtube_api_key:
        vids = _fetch_range_via_api(
            channel_id,
            playlist_id or uploads_playlist_id(channel_id),
            published_after=after,
            published_before=before,
            max_pages=max_pages,
        )
        if vids is not None:
            return vids
    return _filter_uploads_in_range(
        _fetch_via_rss(channel_id),
        published_after=after,
        published_before=before,
    )


def _filter_uploads_in_range(
    videos: list[Video],
    *,
    published_after: datetime,
    published_before: datetime,
) -> list[Video]:
    return [
        v for v in videos
        if not v.live and _in_publish_range(v.published, published_after, published_before)
    ]


def _fetch_via_rss(channel_id: str) -> list[Video]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", channel_id, exc)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning("RSS parse failed for %s: %s", channel_id, exc)
        return []

    channel_name = root.findtext("atom:title", default="", namespaces=_NS) or ""
    videos: list[Video] = []
    for entry in root.findall("atom:entry", _NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=_NS) or ""
        title = entry.findtext("atom:title", default="", namespaces=_NS) or ""
        group = entry.find("media:group", _NS)
        desc = (
            group.findtext("media:description", default="", namespaces=_NS) if group is not None else ""
        ) or ""
        pub_raw = entry.findtext("atom:published", default="", namespaces=_NS) or ""
        published = None
        if pub_raw:
            try:
                published = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        if vid:
            videos.append(
                Video(
                    video_id=vid,
                    title=title,
                    description=desc,
                    published=published,
                    channel_name=channel_name,
                    channel_id=channel_id,
                    url=f"https://www.youtube.com/watch?v={vid}",
                )
            )
    return videos


def _fetch_via_api(channel_id: str, playlist_id: str, *, max_results: int) -> list[Video]:
    if not playlist_id:
        return []
    key = settings.youtube_api_key
    try:
        items_resp = httpx.get(
            f"{_API}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": min(max_results, 50),
                "key": key,
            },
            timeout=30,
        )
        items_resp.raise_for_status()
        items = items_resp.json().get("items") or []
    except Exception as exc:
        logger.warning("YouTube playlistItems failed for %s: %s", channel_id, exc)
        return []

    video_ids = []
    meta: dict[str, dict] = {}
    for it in items:
        sn = it.get("snippet") or {}
        vid = (it.get("contentDetails") or {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
        if not vid:
            continue
        video_ids.append(vid)
        published = None
        raw = sn.get("publishedAt") or ""
        if raw:
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        meta[vid] = {
            "title": sn.get("title") or "",
            "description": sn.get("description") or "",
            "channel_name": sn.get("channelTitle") or "",
            "published": published,
        }

    durations: dict[str, int] = {}
    live_flags: dict[str, bool] = {}
    if video_ids:
        try:
            v_resp = httpx.get(
                f"{_API}/videos",
                params={
                    "part": "contentDetails,snippet",
                    "id": ",".join(video_ids[:50]),
                    "key": key,
                },
                timeout=30,
            )
            v_resp.raise_for_status()
            for v in v_resp.json().get("items") or []:
                vid = v.get("id")
                if not vid:
                    continue
                durations[vid] = _parse_iso_duration(
                    ((v.get("contentDetails") or {}).get("duration")) or ""
                )
                live = ((v.get("snippet") or {}).get("liveBroadcastContent") or "none") != "none"
                live_flags[vid] = live
        except Exception as exc:
            logger.warning("YouTube videos.list failed: %s", exc)

    out: list[Video] = []
    for vid in video_ids:
        m = meta.get(vid) or {}
        out.append(
            Video(
                video_id=vid,
                title=m.get("title") or "",
                description=m.get("description") or "",
                published=m.get("published"),
                channel_name=m.get("channel_name") or "",
                channel_id=channel_id,
                url=f"https://www.youtube.com/watch?v={vid}",
                duration_seconds=durations.get(vid),
                live=live_flags.get(vid, False),
            )
        )
    return out


def _fetch_range_via_api(
    channel_id: str,
    playlist_id: str,
    *,
    published_after: datetime,
    published_before: datetime,
    max_pages: int,
) -> list[Video] | None:
    """Paginate uploads playlist newest-first; stop when past the range start."""
    if not playlist_id:
        return None
    key = settings.youtube_api_key
    page_token = ""
    out: list[Video] = []
    for _ in range(max_pages):
        params: dict = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": key,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            items_resp = httpx.get(f"{_API}/playlistItems", params=params, timeout=30)
            items_resp.raise_for_status()
            payload = items_resp.json()
        except Exception as exc:
            logger.warning("YouTube playlistItems (range) failed for %s: %s", channel_id, exc)
            return None

        items = payload.get("items") or []
        if not items:
            break

        video_ids: list[str] = []
        meta: dict[str, dict] = {}
        oldest_in_page: datetime | None = None
        for it in items:
            sn = it.get("snippet") or {}
            vid = (it.get("contentDetails") or {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            published = None
            raw = sn.get("publishedAt") or ""
            if raw:
                try:
                    published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
            if published is not None:
                pub_u = _as_utc(published)
                if oldest_in_page is None or pub_u < oldest_in_page:
                    oldest_in_page = pub_u
            if _in_publish_range(published, published_after, published_before):
                video_ids.append(vid)
                meta[vid] = {
                    "title": sn.get("title") or "",
                    "description": sn.get("description") or "",
                    "channel_name": sn.get("channelTitle") or "",
                    "published": published,
                }

        durations: dict[str, int] = {}
        live_flags: dict[str, bool] = {}
        if video_ids:
            try:
                v_resp = httpx.get(
                    f"{_API}/videos",
                    params={
                        "part": "contentDetails,snippet",
                        "id": ",".join(video_ids[:50]),
                        "key": key,
                    },
                    timeout=30,
                )
                v_resp.raise_for_status()
                for v in v_resp.json().get("items") or []:
                    vid = v.get("id")
                    if not vid:
                        continue
                    durations[vid] = _parse_iso_duration(
                        ((v.get("contentDetails") or {}).get("duration")) or ""
                    )
                    live = ((v.get("snippet") or {}).get("liveBroadcastContent") or "none") != "none"
                    live_flags[vid] = live
            except Exception as exc:
                logger.warning("YouTube videos.list (range) failed: %s", exc)

        for vid in video_ids:
            if live_flags.get(vid, False):
                continue
            m = meta.get(vid) or {}
            out.append(
                Video(
                    video_id=vid,
                    title=m.get("title") or "",
                    description=m.get("description") or "",
                    published=m.get("published"),
                    channel_name=m.get("channel_name") or "",
                    channel_id=channel_id,
                    url=f"https://www.youtube.com/watch?v={vid}",
                    duration_seconds=durations.get(vid),
                    live=False,
                )
            )

        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break
        if oldest_in_page is not None and oldest_in_page < published_after:
            break
    return out


def _parse_iso_duration(raw: str) -> int | None:
    """Parse ISO-8601 duration like PT15M42S -> seconds."""
    if not raw or not raw.startswith("P"):
        return None
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        raw,
    )
    if not m:
        return None
    days, hours, mins, secs = (int(x or 0) for x in m.groups())
    return days * 86400 + hours * 3600 + mins * 60 + secs


def deep_link(video_id: str, seconds: int | None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    if seconds is None or seconds < 0:
        return base
    return f"{base}&t={int(seconds)}s"
