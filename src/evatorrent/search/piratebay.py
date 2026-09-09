"""The Pirate Bay (Apibay) torrent search provider."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import urllib.parse
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes

logger = logging.getLogger("evaTorrent.search.piratebay")

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
]

CATEGORY_PARAM_MAP = {
    SearchCategory.ALL: 0,
    SearchCategory.MOVIES: 201,
    SearchCategory.SERIES: 205,
    SearchCategory.SOFTWARE: 300,
    SearchCategory.GAMES: 400,
    SearchCategory.BOOKS: 601,
}

TPB_CAT_NAMES = {
    "100": "Audio",
    "201": "Movies",
    "202": "Movies",
    "207": "Movies HD",
    "205": "TV / Series",
    "208": "TV / Series HD",
    "200": "Video",
    "300": "Software",
    "301": "Windows App",
    "302": "Mac App",
    "303": "Linux App",
    "400": "Games",
    "401": "PC Game",
    "402": "Mac Game",
    "601": "E-Books",
}


def build_magnet(info_hash: str, title: str) -> str:
    """Builds an enriched magnet link with standard announce trackers."""
    dn = urllib.parse.quote(title)
    trackers = "".join(f"&tr={urllib.parse.quote(t)}" for t in DEFAULT_TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash.strip().lower()}&dn={dn}{trackers}"


class PirateBaySearchProvider(BaseSearchProvider):
    """Searches The Pirate Bay via the public Apibay JSON API."""

    name: str = "The Pirate Bay"
    API_URL = "https://apibay.org/q.php"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    async def search(
        self,
        query: str,
        category: SearchCategory = SearchCategory.ALL,
        timeout: float | None = None,
    ) -> list[SearchResult]:
        q = query.strip()
        if not q:
            return []

        cat_id = CATEGORY_PARAM_MAP.get(category, 0)
        params = {"q": q, "cat": str(cat_id)}
        headers = {
            "User-Agent": "evaTorrent/0.4.0 (Autonomous BitTorrent Client; https://github.com/akash211/evaTorrent)",
            "Accept": "application/json",
        }
        effective_timeout = float(timeout) if timeout is not None and timeout > 0 else self.timeout

        try:
            async with httpx.AsyncClient(timeout=effective_timeout, follow_redirects=True) as client:
                resp = await client.get(self.API_URL, params=params, headers=headers)
                if resp.status_code != 200:
                    logger.warning(f"Apibay responded with HTTP {resp.status_code}")
                    return []

                data = resp.json()
                if not isinstance(data, list):
                    return []

                results: list[SearchResult] = []
                for item in data:
                    item_id = str(item.get("id", ""))
                    name = str(item.get("name", "")).strip()
                    if item_id == "0" and "no results" in name.lower():
                        continue

                    info_hash = str(item.get("info_hash", "")).strip().lower()
                    if not info_hash or len(info_hash) != 40:
                        continue

                    try:
                        seeders = max(0, int(item.get("seeders", 0)))
                    except (ValueError, TypeError):
                        seeders = 0

                    try:
                        leechers = max(0, int(item.get("leechers", 0)))
                    except (ValueError, TypeError):
                        leechers = 0

                    try:
                        size_bytes = max(0, int(item.get("size", 0)))
                    except (ValueError, TypeError):
                        size_bytes = 0

                    cat_raw = str(item.get("category", ""))
                    cat_display = TPB_CAT_NAMES.get(cat_raw, "Other")
                    if category == SearchCategory.MOVIES and "movie" not in cat_display.lower():
                        cat_display = "Movies"
                    elif category == SearchCategory.SERIES and "series" not in cat_display.lower() and "tv" not in cat_display.lower():
                        cat_display = "TV / Series"

                    added_raw = item.get("added", 0)
                    added_str = ""
                    try:
                        if added_raw and int(added_raw) > 0:
                            added_str = datetime.fromtimestamp(int(added_raw), timezone.utc).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                    magnet = build_magnet(info_hash, name)
                    source_link = f"https://thepiratebay.org/description.php?id={item_id}" if item_id and item_id != "0" else f"https://apibay.org/q.php?q={urllib.parse.quote(name)}"
                    direct_torrent_link = f"https://itorrents.org/torrent/{info_hash.upper()}.torrent"

                    results.append(
                        SearchResult(
                            title=name,
                            info_hash=info_hash,
                            magnet_uri=magnet,
                            size_bytes=size_bytes,
                            size_formatted=format_bytes(size_bytes),
                            seeders=seeders,
                            leechers=leechers,
                            category=cat_display,
                            provider=self.name,
                            added_date=added_str,
                            source_url=source_link,
                            torrent_url=direct_torrent_link,
                        )
                    )

                return results
        except Exception as e:
            logger.error(f"Search error querying Apibay: {e}")
            return []
