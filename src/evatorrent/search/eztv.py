"""EZTV TV show search provider."""

from __future__ import annotations

import logging
import re
import urllib.parse
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes
from evatorrent.search.piratebay import build_magnet

logger = logging.getLogger("evaTorrent.search.eztv")


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
    return int(val)


class EztvSearchProvider(BaseSearchProvider):
    """Searches EZTV.re for TV shows via HTML scraping."""

    name: str = "EZTV"
    BASE_URL = "https://eztv.re"

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

        # EZTV replaces spaces with hyphens in search URL
        safe_q = re.sub(r"\s+", "-", q).strip("-").lower()
        if not safe_q:
            return []

        url = f"{self.BASE_URL}/search/{safe_q}"
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
                    logger.warning(f"EZTV returned HTTP {resp.status_code} for query: {q}")
                    return []

                html = resp.text

                # regex approach to find rows with magnet links and seeders
                # Typical EZTV row has <a href="...magnet:?xt=urn:btih:... " class="magnet">
                # title is in <a ... class="epinfo">Title</a>
                # seeders in <td ...><font color="green">123</font></td>

                # Simplest pattern capturing entire TR to parse locally
                rows = re.findall(
                    r'<tr name="hover" class="forum_header_border">(.*?)</tr>', html, re.DOTALL | re.IGNORECASE
                )

                results: list[SearchResult] = []
                for row in rows:
                    title_match = re.search(r'<a[^>]+class="epinfo"[^>]*>(.*?)</a>', row, re.IGNORECASE)
                    if not title_match:
                        continue
                    title = title_match.group(1).strip()
                    # Strip out any inner HTML tags (e.g. bold or font)
                    title = re.sub(r"<[^>]+>", "", title)

                    magnet_match = re.search(r'href="(magnet:\?[^"]+)"', row, re.IGNORECASE)
                    if not magnet_match:
                        continue
                    magnet_uri = magnet_match.group(1)

                    hash_match = re.search(r"urn:btih:([a-fA-F0-9]{40})", magnet_uri, re.IGNORECASE)
                    if not hash_match:
                        continue
                    info_hash = hash_match.group(1).lower()

                    torrent_url = ""
                    torrent_match = re.search(r'href="([^"]+\.torrent)"[^>]*class="download_1"', row, re.IGNORECASE)
                    if torrent_match:
                        torrent_url = torrent_match.group(1)

                    seeders = 0
                    seeder_match = re.search(r'<font color="green">([\d,]+)</font>', row, re.IGNORECASE)
                    if seeder_match:
                        try:
                            seeders = int(seeder_match.group(1).replace(",", ""))
                        except ValueError:
                            pass

                    # EZTV doesn't usually show leechers reliably in search, assume 0
                    leechers = 0

                    size_bytes = 0
                    size_disp = ""
                    # Size is usually in a td next to seeders, but let's try a heuristic
                    # look for something like 1.2 GB or 500 MB
                    size_match = re.search(r">\s*([\d.]+\s*[KMGT]B)\s*<", row, re.IGNORECASE)
                    if size_match:
                        size_disp = size_match.group(1).strip()
                        size_bytes = parse_size_to_bytes(size_disp)

                    source_url = ""
                    source_match = re.search(r'<a href="([^"]+)"[^>]*class="epinfo"', row, re.IGNORECASE)
                    if source_match:
                        source_url = source_match.group(1)
                        if source_url.startswith("/"):
                            source_url = f"{self.BASE_URL}{source_url}"

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
                            category="TV / Series",
                            provider=self.name,
                            source_url=source_url,
                            torrent_url=torrent_url,
                        )
                    )

                return results

        except Exception as e:
            logger.warning(f"Error querying EZTV for '{q}': {e}")
            return []
