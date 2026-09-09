"""LimeTorrents search provider."""

from __future__ import annotations

import logging
import re
import urllib.parse
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes
from evatorrent.search.piratebay import build_magnet

logger = logging.getLogger("evaTorrent.search.limetorrents")

CATEGORY_SLUG_MAP = {
    SearchCategory.ALL: "all",
    SearchCategory.MOVIES: "movies",
    SearchCategory.SERIES: "tv",
    SearchCategory.SOFTWARE: "applications",
    SearchCategory.GAMES: "games",
    SearchCategory.BOOKS: "other",
}

ROW_PATTERN = re.compile(
    r'<div class="tt-name">.*?href="([^"]*itorrents\.net/torrent/([a-fA-F0-9]{40})\.torrent[^"]*)"[^>]*>.*?<a href="([^"]+)"[^>]*>([^<]+)</a>.*?<td class="tdnormal">(.*?)</td>.*?<td class="tdnormal">([^<]+)</td>.*?<td class="tdseed">([^<]+)</td>.*?<td class="tdleech">([^<]+)</td>',
    re.DOTALL,
)


def parse_size_to_bytes(size_str: str) -> int:
    """Converts a size string like '10.99 MB' or '1.45 GB' to integer bytes."""
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
    elif unit_upper.startswith("B"):
        return int(val)
    return int(val)


class LimeTorrentsSearchProvider(BaseSearchProvider):
    """Scrapes search results from LimeTorrents public mirror."""

    name: str = "LimeTorrents"
    BASE_URL: str = "https://www.limetorrents.lol"

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

        # Sanitize query for URL path
        safe_q = re.sub(r"[^\w\s-]", "", q).strip().replace(" ", "-")
        if not safe_q:
            safe_q = urllib.parse.quote_plus(q)

        cat_slug = CATEGORY_SLUG_MAP.get(category, "all")
        url = f"{self.BASE_URL}/search/{cat_slug}/{safe_q}/1/"

        effective_timeout = float(timeout) if timeout is not None and timeout > 0 else self.timeout
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        try:
            async with httpx.AsyncClient(timeout=effective_timeout, follow_redirects=True, verify=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.debug(f"LimeTorrents returned HTTP {resp.status_code} for query: {q}")
                    return []

                html = resp.text
                matches = ROW_PATTERN.findall(html)
                results: list[SearchResult] = []

                for match in matches:
                    raw_torrent_url, raw_hash, detail_path, title, age_cat, size_str, seed_str, leech_str = match
                    info_hash = raw_hash.strip().lower()
                    title_clean = re.sub(r"\s+", " ", title).strip()

                    try:
                        seeders = max(0, int(seed_str.replace(",", "").strip()))
                    except (ValueError, TypeError):
                        seeders = 0

                    try:
                        leechers = max(0, int(leech_str.replace(",", "").strip()))
                    except (ValueError, TypeError):
                        leechers = 0

                    size_bytes = parse_size_to_bytes(size_str)
                    size_disp = size_str.strip() or format_bytes(size_bytes)

                    # Extract category if possible
                    cat_match = re.search(r"in\s+([A-Za-z]+)", age_cat)
                    cat_display = cat_match.group(1).title() if cat_match else "Other"

                    magnet = build_magnet(info_hash, title_clean)
                    detail_url = f"{self.BASE_URL}{detail_path}" if detail_path.startswith("/") else detail_path
                    # Enforce secure itorrents.net HTTPS CDN
                    torrent_dl = f"https://itorrents.net/torrent/{info_hash.upper()}.torrent"

                    results.append(
                        SearchResult(
                            title=title_clean,
                            info_hash=info_hash,
                            magnet_uri=magnet,
                            size_bytes=size_bytes,
                            size_formatted=size_disp,
                            seeders=seeders,
                            leechers=leechers,
                            category=cat_display,
                            provider=self.name,
                            added_date="",
                            source_url=detail_url,
                            torrent_url=torrent_dl,
                        )
                    )

                logger.info(f"LimeTorrents returned {len(results)} results for '{q}'")
                return results

        except Exception as e:
            logger.warning(f"Error querying LimeTorrents for '{q}': {e}")
            return []
