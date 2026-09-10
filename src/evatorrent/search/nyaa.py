"""Nyaa.si anime search provider."""

from __future__ import annotations

import logging
import re
import urllib.parse
from xml.etree import ElementTree
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes
from evatorrent.search.piratebay import build_magnet

logger = logging.getLogger("evaTorrent.search.nyaa")


def parse_size_to_bytes(size_str: str) -> int:
    """Converts a size string like '1.4 GiB' or '500 MiB' to integer bytes."""
    s = size_str.strip()
    match = re.match(r"^([\d.]+)\s*([A-Za-z]+)$", s)
    if not match:
        return 0
    val_str, unit = match.groups()
    try:
        val = float(val_str)
    except ValueError:
        return 0
    unit_upper = unit.upper()
    if unit_upper.startswith("K"):
        return int(val * 1024)
    elif unit_upper.startswith("M"):
        return int(val * 1024 * 1024)
    elif unit_upper.startswith("G"):
        return int(val * 1024 * 1024 * 1024)
    elif unit_upper.startswith("T"):
        return int(val * 1024 * 1024 * 1024 * 1024)
    return int(val)


class NyaaSearchProvider(BaseSearchProvider):
    """Searches Nyaa.si via structured XML RSS feed."""

    name: str = "Nyaa"
    API_URL = "https://nyaa.si/"

    def __init__(self, timeout: float = 10.0):
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

        params = {
            "page": "rss",
            "q": q,
            "c": "0_0",
            "f": "0"
        }

        effective_timeout = float(timeout) if timeout is not None and timeout > 0 else self.timeout

        try:
            async with httpx.AsyncClient(timeout=effective_timeout, follow_redirects=True, verify=False) as client:
                resp = await client.get(self.API_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(f"Nyaa responded with HTTP {resp.status_code}")
                    return []

                # Parse RSS XML
                root = ElementTree.fromstring(resp.text)
                ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

                channel = root.find("channel")
                if channel is None:
                    return []

                results: list[SearchResult] = []
                for item in channel.findall("item"):
                    title_elem = item.find("title")
                    title = title_elem.text if title_elem is not None and title_elem.text else ""

                    link_elem = item.find("link")
                    torrent_url = link_elem.text if link_elem is not None and link_elem.text else ""

                    guid_elem = item.find("guid")
                    source_url = guid_elem.text if guid_elem is not None and guid_elem.text else torrent_url

                    info_hash_elem = item.find("nyaa:infoHash", ns)
                    info_hash = info_hash_elem.text.lower() if info_hash_elem is not None and info_hash_elem.text else ""
                    if not info_hash:
                        continue

                    seeders_elem = item.find("nyaa:seeders", ns)
                    try:
                        seeders = int(seeders_elem.text) if seeders_elem is not None and seeders_elem.text else 0
                    except ValueError:
                        seeders = 0

                    leechers_elem = item.find("nyaa:leechers", ns)
                    try:
                        leechers = int(leechers_elem.text) if leechers_elem is not None and leechers_elem.text else 0
                    except ValueError:
                        leechers = 0

                    size_elem = item.find("nyaa:size", ns)
                    size_disp = size_elem.text if size_elem is not None and size_elem.text else ""
                    size_bytes = parse_size_to_bytes(size_disp)

                    cat_elem = item.find("nyaa:category", ns)
                    cat_name = cat_elem.text if cat_elem is not None and cat_elem.text else ""
                    category_display = "Anime" if "Anime" in cat_name else "Other"

                    magnet = build_magnet(info_hash, title)

                    results.append(
                        SearchResult(
                            title=title,
                            info_hash=info_hash,
                            magnet_uri=magnet,
                            size_bytes=size_bytes,
                            size_formatted=size_disp or format_bytes(size_bytes),
                            seeders=seeders,
                            leechers=leechers,
                            category=category_display,
                            provider=self.name,
                            source_url=source_url,
                            torrent_url=torrent_url,
                        )
                    )

                return results
        except Exception as e:
            logger.error(f"Search error querying Nyaa: {e}")
            return []
