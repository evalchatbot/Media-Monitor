"""YouTube channel monitoring via public RSS — no API key required.

Every channel exposes an Atom feed at
  https://www.youtube.com/feeds/videos.xml?channel_id=UC...
listing its latest ~15 uploads (id, title, description, published). This gives
us credential-free new-upload detection. Live streams appear here too once
YouTube lists them; true low-latency live tapping is a later, GPU-gated add-on.

`resolve_channel_id` turns a channel URL / @handle / UC-id into a channel_id by
reading the channel page (also no API key).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

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


@dataclass
class Video:
    video_id: str
    title: str
    description: str
    published: datetime | None
    channel_name: str
    url: str


def resolve_channel_id(url_or_handle: str) -> tuple[str | None, str]:
    """Return (channel_id, channel_name) for a channel URL / @handle / UC-id."""
    s = url_or_handle.strip()
    if re.fullmatch(r"UC[\w-]{20,}", s):
        return s, ""

    # Normalise to a fetchable channel URL.
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
            # Bypass the EU consent interstitial that hides the channelId.
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


def fetch_uploads(channel_id: str) -> list[Video]:
    """Fetch the latest uploads for a channel from its RSS feed."""
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

    channel_name = root.findtext("atom:title", default="", namespaces=_NS)
    videos: list[Video] = []
    for entry in root.findall("atom:entry", _NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=_NS)
        title = entry.findtext("atom:title", default="", namespaces=_NS)
        group = entry.find("media:group", _NS)
        desc = group.findtext("media:description", default="", namespaces=_NS) if group is not None else ""
        pub_raw = entry.findtext("atom:published", default="", namespaces=_NS)
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
                    url=f"https://www.youtube.com/watch?v={vid}",
                )
            )
    return videos
