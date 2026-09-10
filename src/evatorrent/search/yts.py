"""YTS movie search provider."""

from __future__ import annotations

import logging
import httpx

from evatorrent.search.base import BaseSearchProvider, SearchCategory, SearchResult, format_bytes
from evatorrent.search.piratebay import build_magnet

logger = logging.getLogger("evaTorrent.search.yts")


class YtsSearchProvider(BaseSearchProvider):
    """Searches YTS.mx for movies via JSON API."""

    name: str = "YTS"
    API_URL = "https://yts.mx/api/v2/list_movies.json"

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

        params = {
            "query_term": q,
            "sort_by": "seeds",
            "order_by": "desc",
            "limit": 50,
        }

        effective_timeout = float(timeout) if timeout is not None and timeout > 0 else self.timeout

        try:
            async with httpx.AsyncClient(timeout=effective_timeout, follow_redirects=True, verify=False) as client:
                resp = await client.get(self.API_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(f"YTS responded with HTTP {resp.status_code}")
                    return []

                data = resp.json()
                if "data" not in data or "movies" not in data["data"]:
                    return []

                movies = data["data"]["movies"]
                if not movies:
                    return []

                results: list[SearchResult] = []
                for movie in movies:
                    movie_title = movie.get("title", "")
                    movie_url = movie.get("url", "")
                    added_date = movie.get("date_uploaded", "")
                    torrents = movie.get("torrents", [])

                    for torrent in torrents:
                        quality = torrent.get("quality", "")
                        type_str = torrent.get("type", "")
                        title = f"{movie_title} [{quality}] [{type_str}]".strip()

                        info_hash = str(torrent.get("hash", "")).strip().lower()
                        if not info_hash or len(info_hash) != 40:
                            continue

                        try:
                            seeders = max(0, int(torrent.get("seeds", 0)))
                        except (ValueError, TypeError):
                            seeders = 0

                        try:
                            leechers = max(0, int(torrent.get("peers", 0)))
                        except (ValueError, TypeError):
                            leechers = 0

                        try:
                            size_bytes = max(0, int(torrent.get("size_bytes", 0)))
                        except (ValueError, TypeError):
                            size_bytes = 0

                        size_disp = str(torrent.get("size", "")).strip() or format_bytes(size_bytes)
                        torrent_url = torrent.get("url", "")
                        magnet = build_magnet(info_hash, title)

                        results.append(
                            SearchResult(
                                title=title,
                                info_hash=info_hash,
                                magnet_uri=magnet,
                                size_bytes=size_bytes,
                                size_formatted=size_disp,
                                seeders=seeders,
                                leechers=leechers,
                                category="Movies",
                                provider=self.name,
                                added_date=added_date,
                                source_url=movie_url,
                                torrent_url=torrent_url,
                            )
                        )

                return results
        except Exception as e:
            logger.error(f"Search error querying YTS: {e}")
            return []
