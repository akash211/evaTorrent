"""Keyless subtitle providers (English only).

- YifySubtitles: best for movies, no API key (HTML scrape).
- OpenSubtitles.org HTML search: fallback covering TV series, direct links.
- OpenSubtitles.com REST: only when OPENSUBTITLES_API_KEY is configured.

All parsers are defensive: upstream HTML changes degrade to empty results,
never exceptions (callers aggregate per-provider errors).
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import List

import httpx

from evatorrent.subtitles.service import SubtitleResult

logger = logging.getLogger(__name__)

_YIFY_BASES = [
    "https://yifysubtitles.ch",
    "https://yifysubtitles.org",
]

_OS_ORG_SEARCH = "https://www.opensubtitles.org/en/search2/sublanguageid-eng/moviename-{q}"


def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base + "/", href)


async def search_yify(client: httpx.AsyncClient, query: str, limit: int = 20) -> List[SubtitleResult]:
    """Searches YifySubtitles mirrors for English movie subtitles."""
    q = urllib.parse.quote_plus(query)
    last_err: Exception | None = None
    for base in _YIFY_BASES:
        try:
            resp = await client.get(f"{base}/search?q={q}")
            if resp.status_code != 200:
                continue
            html = resp.text
            # Movie links look like /movie-imdb/tt1234567
            movies: list[tuple[str, str]] = []
            for m in re.finditer(
                r'href="(/movie-imdb/tt\d+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL
            ):
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if m.group(1) not in [x[0] for x in movies]:
                    movies.append((m.group(1), title or query))
                if len(movies) >= 5:
                    break
            results: List[SubtitleResult] = []
            for href, title in movies:
                try:
                    page = await client.get(_abs(base, href))
                    if page.status_code != 200:
                        continue
                    # English subtitle download rows: /subtitle/....zip with language English
                    for sm in re.finditer(
                        r'href="(/subtitle/[^"]+\.zip)"[^>]*>(.*?)</a>', page.text, re.IGNORECASE | re.DOTALL
                    ):
                        row = sm.group(0)
                        cell = re.sub(r"<[^>]+>", " ", sm.group(2))
                        if not re.search(r"\benglish\b", row + cell, re.IGNORECASE):
                            continue
                        dl = _abs(base, sm.group(1))
                        rating_m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", cell)
                        results.append(
                            SubtitleResult(
                                id=f"yify:{urllib.parse.quote(dl)}",
                                title=title,
                                language="en",
                                release=re.sub(r"\s+", " ", cell).strip()[:120],
                                rating=float(rating_m.group(1)) if rating_m else 0.0,
                                downloads=0,
                                provider="yify",
                                download_url=dl,
                                detail_url=_abs(base, href),
                            )
                        )
                        if len(results) >= limit:
                            return results
                except Exception as e:
                    logger.debug("yify movie page %s failed: %s", href, e)
            if results:
                return results[:limit]
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        logger.debug("yify search failed: %s", last_err)
    return []


async def search_opensubtitles_org(client: httpx.AsyncClient, query: str, limit: int = 20) -> List[SubtitleResult]:
    """Falls back to opensubtitles.org HTML search (English, direct links)."""
    url = _OS_ORG_SEARCH.format(q=urllib.parse.quote(query))
    try:
        resp = await client.get(url, headers={"Accept-Language": "en"})
    except Exception as e:
        logger.debug("opensubtitles.org search failed: %s", e)
        return []
    if resp.status_code != 200:
        return []
    html = resp.text
    results: List[SubtitleResult] = []
    # Rows link to /en/subtitles/<id>/... and offer /en/download/sub/<id>
    for m in re.finditer(
        r'href="(/en/subtitles/\d+/[^"]*)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL
    ):
        detail = _abs("https://www.opensubtitles.org", m.group(1))
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        id_m = re.search(r"/subtitles/(\d+)/", m.group(1))
        if not id_m:
            continue
        sub_id = id_m.group(1)
        dl = f"https://dl.opensubtitles.org/en/download/sub/{sub_id}"
        results.append(
            SubtitleResult(
                id=f"osorg:{sub_id}",
                title=title or query,
                language="en",
                release=title[:120],
                rating=0.0,
                downloads=0,
                provider="opensubtitles_org",
                download_url=dl,
                detail_url=detail,
            )
        )
        if len(results) >= limit:
            break
    return results


async def search_opensubtitles_com(client: httpx.AsyncClient, query: str, limit: int = 20) -> List[SubtitleResult]:
    """Keyed OpenSubtitles.com search (only called when API key is set)."""
    api_key = os.environ.get("OPENSUBTITLES_API_KEY", "")
    if not api_key:
        return []
    resp = await client.get(
        "https://api.opensubtitles.com/api/v1/subtitles",
        params={"query": query, "languages": "en", "order_by": "download_count", "order_direction": "desc"},
        headers={"Api-Key": api_key, "Accept": "application/json"},
    )
    if resp.status_code != 200:
        return []
    payload = resp.json()
    results: List[SubtitleResult] = []
    for item in (payload.get("data") or [])[:limit]:
        attrs = item.get("attributes", {}) or {}
        files = attrs.get("files") or []
        file_id = files[0].get("file_id") if files else None
        dl = f"https://api.opensubtitles.com/api/v1/download/{file_id}" if file_id else attrs.get("url", "")
        results.append(
            SubtitleResult(
                id=f"oscom:{item.get('id', '')}",
                title=(attrs.get("release") or attrs.get("movie_name") or query),
                language="en",
                release=str(attrs.get("release") or "")[:120],
                rating=float(attrs.get("ratings") or 0.0),
                downloads=int(attrs.get("download_count") or 0),
                provider="opensubtitles_com",
                download_url=dl,
                detail_url=attrs.get("url") or "",
            )
        )
    return results
