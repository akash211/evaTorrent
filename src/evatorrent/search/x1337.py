"""1337x general search provider."""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes
from evatorrent.search.piratebay import build_magnet

logger = logging.getLogger("evaTorrent.search.x1337")


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


class X1337SearchProvider(BaseSearchProvider):
    """Searches 1337x using a 2-step HTML scrape."""

    name: str = "1337x"
    BASE_URL = "https://1337x.to"

    def __init__(self, timeout: float = 12.0):
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

        # 1337x uses standard URL encoding, spaces become + or %20
        safe_q = urllib.parse.quote_plus(q)
        url = f"{self.BASE_URL}/sort-search/{safe_q}/seeders/desc/1/"

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
                    logger.warning(f"1337x returned HTTP {resp.status_code} for query: {q}")
                    return []

                html = resp.text

                # regex to find rows
                # <td class="coll-1 name"><a href="/sub/...">...</a><a href="/torrent/...">Title</a></td>
                # <td class="coll-2 seeds">123</td>
                # <td class="coll-3 leeches">12</td>
                # <td class="coll-4 size">1.2 GB</td>

                rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)

                parsed_items = []
                for row in rows:
                    if 'class="coll-1 name"' not in row:
                        continue

                    links = re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', row, re.IGNORECASE)
                    if len(links) < 2:
                        continue

                    detail_path, title = links[1]
                    title = re.sub(r'<[^>]+>', '', title).strip()

                    seed_match = re.search(r'<td[^>]*class="[^"]*seeds[^"]*"[^>]*>(\d+)</td>', row, re.IGNORECASE)
                    seeders = int(seed_match.group(1)) if seed_match else 0

                    leech_match = re.search(r'<td[^>]*class="[^"]*leeches[^"]*"[^>]*>(\d+)</td>', row, re.IGNORECASE)
                    leechers = int(leech_match.group(1)) if leech_match else 0

                    size_match = re.search(r'<td[^>]*class="[^"]*size[^"]*"[^>]*>(.*?)(?:<span|<\/td>)', row, re.IGNORECASE)
                    size_disp = size_match.group(1).strip() if size_match else ""

                    parsed_items.append({
                        "title": title,
                        "detail_path": detail_path,
                        "seeders": seeders,
                        "leechers": leechers,
                        "size_disp": size_disp,
                    })

                # limit to top 15 results
                parsed_items = parsed_items[:15]
                if not parsed_items:
                    return []

                semaphore = asyncio.Semaphore(5)

                async def fetch_detail(item: dict) -> SearchResult | None:
                    detail_url = f"{self.BASE_URL}{item['detail_path']}"
                    async with semaphore:
                        try:
                            d_resp = await client.get(detail_url, headers=headers)
                            if d_resp.status_code != 200:
                                return None

                            d_html = d_resp.text
                            magnet_match = re.search(r'href="(magnet:\?[^"]+)"', d_html, re.IGNORECASE)
                            if not magnet_match:
                                return None

                            magnet_uri = magnet_match.group(1)
                            hash_match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_uri, re.IGNORECASE)
                            if not hash_match:
                                return None

                            info_hash = hash_match.group(1).lower()
                            size_bytes = parse_size_to_bytes(item["size_disp"])

                            magnet = build_magnet(info_hash, item["title"])

                            return SearchResult(
                                title=item["title"],
                                info_hash=info_hash,
                                magnet_uri=magnet,
                                size_bytes=size_bytes,
                                size_formatted=item["size_disp"] or format_bytes(size_bytes),
                                seeders=item["seeders"],
                                leechers=item["leechers"],
                                category="Other", # Optional logic to infer category
                                provider=self.name,
                                source_url=detail_url,
                                torrent_url="",
                            )
                        except Exception:
                            return None

                results = await asyncio.gather(*(fetch_detail(item) for item in parsed_items))

                return [r for r in results if r is not None]

        except Exception as e:
            logger.warning(f"Error querying 1337x for '{q}': {e}")
            return []
