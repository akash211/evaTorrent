"""Free metadata providers for Discover (movies, TV, books, games)."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("evatorrent.metadata")

UA = {"User-Agent": "evaTorrent/0.7.0 (Discover metadata lookup)"}
DEFAULT_TIMEOUT = 10.0


def _clean_html(text: str) -> str:
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", no_tags)).strip()


def youtube_links(title: str, year: Any = None, trailer_code: Optional[str] = None) -> Dict[str, str]:
    if trailer_code:
        watch = f"https://www.youtube.com/watch?v={trailer_code}"
        embed = f"https://www.youtube.com/embed/{trailer_code}"
        return {"youtube_trailer_url": watch, "youtube_trailer_embed": embed}
    q = f"{title} {year or ''} official trailer".strip()
    search = f"https://www.youtube.com/results?search_query={quote_plus(q)}"
    # Embeddable search-based playlist (no API key needed, plays inline in iframe).
    embed = f"https://www.youtube.com/embed?listType=search&list={quote_plus(q)}"
    return {"youtube_trailer_url": search, "youtube_trailer_embed": embed}


def justwatch_links(title: str) -> Dict[str, str]:
    q = quote_plus(title)
    return {
        "justwatch_in_url": f"https://www.justwatch.com/in/search?q={q}",
        "ott_note": "India OTT availability is approximate — verify on JustWatch. "
        "TMDB_API_KEY unlocks accurate provider data.",
    }


async def _get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> Optional[Any]:
    try:
        resp = await client.get(url, params=params)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug(f"Metadata fetch failed {url}: {e}")
    return None


async def search_yts(client: httpx.AsyncClient, query: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Movies via YTS (no key): rating, runtime, genres, imdb_code, yt_trailer_code."""
    data = await _get_json(
        client,
        "https://yts.mx/api/v2/list_movies.json",
        {"query_term": query, "limit": min(limit, 20), "sort_by": "like_count", "order_by": "desc"},
    )
    out: List[Dict[str, Any]] = []
    try:
        movies = (data or {}).get("data", {}).get("movies", []) or []
    except Exception:
        movies = []
    for m in movies[:limit]:
        title = m.get("title_english") or m.get("title") or query
        year = m.get("year")
        imdb_code = m.get("imdb_code") or ""
        yt_code = m.get("yt_trailer_code") or None
        yt = youtube_links(title, year, yt_code)
        jw = justwatch_links(title)
        out.append(
            {
                "type": "movie",
                "title": title,
                "year": year,
                "release_date": str(year) if year else None,
                "runtime_mins": m.get("runtime") or None,
                "runtime": f"{m.get('runtime')} min" if m.get("runtime") else None,
                "genres": m.get("genres") or [],
                "languages": [m.get("language")] if m.get("language") else ["English"],
                "overview": m.get("summary") or m.get("description_full") or "",
                "poster_url": m.get("large_cover_image") or m.get("medium_cover_image"),
                "imdb_id": imdb_code,
                "imdb_url": f"https://www.imdb.com/title/{imdb_code}/"
                if imdb_code
                else f"https://www.imdb.com/find?q={quote_plus(title)}",
                "imdb_rating": m.get("rating"),
                "rotten_tomatoes": None,
                "rotten_tomatoes_url": f"https://www.rottentomatoes.com/search?search={quote_plus(title)}",
                "wiki_url": None,
                "budget": None,
                "revenue_box_office": None,
                "ott_india": [],
                **jw,
                **yt,
                "provider": "YTS",
                "source_url": m.get("url") or "https://yts.mx",
            }
        )
    return out


async def search_tvmaze(client: httpx.AsyncClient, query: str, limit: int = 6) -> List[Dict[str, Any]]:
    """TV/Series via TVMaze (no key)."""
    data = await _get_json(client, "https://api.tvmaze.com/search/shows", {"q": query})
    out: List[Dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for entry in data[:limit]:
        s = (entry or {}).get("show", {}) or {}
        title = s.get("name") or query
        premiered = s.get("premiered")
        year = int(premiered[:4]) if premiered and len(premiered) >= 4 else None
        rating = (s.get("rating") or {}).get("average")
        network = ((s.get("network") or {}).get("name")) or ((s.get("webChannel") or {}).get("name"))
        yt = youtube_links(title, year)
        jw = justwatch_links(title)
        out.append(
            {
                "type": "tv",
                "title": title,
                "year": year,
                "release_date": premiered,
                "runtime_mins": s.get("runtime"),
                "runtime": f"{s.get('runtime')} min/ep" if s.get("runtime") else None,
                "genres": s.get("genres") or [],
                "languages": [s.get("language")] if s.get("language") else [],
                "overview": _clean_html(s.get("summary") or ""),
                "poster_url": (s.get("image") or {}).get("medium") or (s.get("image") or {}).get("original"),
                "imdb_id": (s.get("externals") or {}).get("imdb"),
                "imdb_url": f"https://www.imdb.com/title/{(s.get('externals') or {}).get('imdb')}/"
                if (s.get("externals") or {}).get("imdb")
                else f"https://www.imdb.com/find?q={quote_plus(title)}",
                "imdb_rating": rating,
                "rotten_tomatoes": None,
                "rotten_tomatoes_url": f"https://www.rottentomatoes.com/search?search={quote_plus(title)}",
                "wiki_url": None,
                "budget": None,
                "revenue_box_office": None,
                "ott_india": ([network] if network else []),
                "network": network,
                "status": s.get("status"),
                "official_site": s.get("officialSite"),
                **jw,
                **yt,
                "provider": "TVMaze",
                "source_url": s.get("url") or "https://www.tvmaze.com",
            }
        )
    return out


async def search_openlibrary(client: httpx.AsyncClient, query: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Books via OpenLibrary (no key)."""
    data = await _get_json(client, "https://openlibrary.org/search.json", {"q": query, "limit": limit})
    out: List[Dict[str, Any]] = []
    docs = ((data or {}).get("docs", [])) if isinstance(data, dict) else []
    for d in docs[:limit]:
        title = d.get("title") or query
        authors = d.get("author_name") or []
        year = d.get("first_publish_year")
        cover_id = d.get("cover_i")
        langs = d.get("language") or []
        key = d.get("key") or ""
        yt = youtube_links(title, year)
        out.append(
            {
                "type": "book",
                "title": title,
                "year": year,
                "release_date": str(year) if year else None,
                "runtime_mins": None,
                "runtime": f"{d.get('number_of_pages_median')} pages" if d.get("number_of_pages_median") else None,
                "genres": d.get("subject")[:5] if d.get("subject") else [],
                "languages": langs,
                "authors": authors,
                "overview": "",
                "poster_url": f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
                "imdb_id": None,
                "imdb_url": None,
                "imdb_rating": None,
                "rotten_tomatoes": None,
                "rotten_tomatoes_url": None,
                "wiki_url": None,
                "budget": None,
                "revenue_box_office": None,
                "ott_india": [],
                "ott_note": "Books are not on OTT — check Kindle, Google Play Books, or local libraries.",
                "openlibrary_url": f"https://openlibrary.org{key}" if key else "https://openlibrary.org",
                **yt,
                "provider": "OpenLibrary",
                "source_url": f"https://openlibrary.org{key}" if key else "https://openlibrary.org",
            }
        )
    return out


async def search_wikipedia(client: httpx.AsyncClient, query: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Generic/Wikipedia fallback — works for movies, TV, books, games."""
    search = await _get_json(
        client,
        "https://en.wikipedia.org/w/api.php",
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": limit, "origin": "*"},
    )
    out: List[Dict[str, Any]] = []
    try:
        hits = ((search or {}).get("query", {}).get("search", [])) or []
    except Exception:
        hits = []
    for h in hits[:limit]:
        page_title = h.get("title") or query
        summary = await _get_json(client, f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(page_title)}")
        yt = youtube_links(page_title)
        jw = justwatch_links(page_title)
        thumb = ((summary or {}).get("thumbnail") or {}).get("source")
        out.append(
            {
                "type": "info",
                "title": (summary or {}).get("title") or page_title,
                "year": None,
                "release_date": None,
                "runtime_mins": None,
                "runtime": None,
                "genres": [],
                "languages": [],
                "overview": (summary or {}).get("extract") or _clean_html(h.get("snippet") or ""),
                "poster_url": thumb,
                "imdb_id": None,
                "imdb_url": f"https://www.imdb.com/find?q={quote_plus(page_title)}",
                "imdb_rating": None,
                "rotten_tomatoes": None,
                "rotten_tomatoes_url": f"https://www.rottentomatoes.com/search?search={quote_plus(page_title)}",
                "wiki_url": ((summary or {}).get("content_urls") or {}).get("desktop", {}).get("page")
                or f"https://en.wikipedia.org/wiki/{quote_plus(page_title)}",
                "budget": None,
                "revenue_box_office": None,
                "ott_india": [],
                **jw,
                **yt,
                "provider": "Wikipedia",
                "source_url": ((summary or {}).get("content_urls") or {}).get("desktop", {}).get("page")
                or "https://en.wikipedia.org",
            }
        )
    return out


async def enrich_with_tmdb(client: httpx.AsyncClient, item: Dict[str, Any]) -> Dict[str, Any]:
    """Adds budget/revenue + India OTT providers when TMDB_API_KEY is set."""
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key or item.get("type") not in ("movie", "tv"):
        return item
    try:
        kind = "movie" if item["type"] == "movie" else "tv"
        search = await _get_json(
            client,
            f"https://api.themoviedb.org/3/search/{kind}",
            {"api_key": api_key, "query": item.get("title", ""), "include_adult": "false"},
        )
        results = ((search or {}).get("results", [])) if isinstance(search, dict) else []
        if not results:
            return item
        best = results[0]
        tmdb_id = best.get("id")
        if item.get("type") == "movie":
            item["imdb_rating"] = item.get("imdb_rating") or best.get("vote_average")
            item["poster_url"] = item.get("poster_url") or (
                f"https://image.tmdb.org/t/p/w500{best.get('poster_path')}" if best.get("poster_path") else None
            )
        detail = await _get_json(client, f"https://api.themoviedb.org/3/{kind}/{tmdb_id}", {"api_key": api_key}) or {}
        if detail.get("budget"):
            item["budget"] = detail["budget"]
            item["budget_formatted"] = f"${detail['budget']:,}"
        if detail.get("revenue"):
            item["revenue_box_office"] = detail["revenue"]
            item["box_office_formatted"] = f"${detail['revenue']:,}"
        if detail.get("overview") and not item.get("overview"):
            item["overview"] = detail["overview"]
        if detail.get("runtime"):
            item["runtime_mins"] = detail["runtime"]
            item["runtime"] = f"{detail['runtime']} min"
        if detail.get("spoken_languages"):
            item["languages"] = [
                lang.get("english_name") or lang.get("name")
                for lang in detail["spoken_languages"]
                if lang.get("english_name") or lang.get("name")
            ] or item.get("languages", [])
        # India watch providers
        prov = (
            await _get_json(
                client, f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/watch/providers", {"api_key": api_key}
            )
            or {}
        )
        in_providers = (prov.get("results") or {}).get("IN") or {}
        flat: List[str] = []
        for bucket in ("flatrate", "rent", "buy", "free", "ads"):
            for p in in_providers.get(bucket, []) or []:
                name = p.get("provider_name")
                if name and name not in flat:
                    flat.append(name)
        if flat:
            item["ott_india"] = flat
            item["ott_note"] = "India OTT providers via TMDB."
    except Exception as e:
        logger.debug(f"TMDB enrich failed: {e}")
    return item


async def enrich_with_omdb(client: httpx.AsyncClient, item: Dict[str, Any]) -> Dict[str, Any]:
    """Adds IMDb/RT ratings, box office, languages when OMDB_API_KEY is set."""
    api_key = os.environ.get("OMDB_API_KEY", "").strip()
    if not api_key or item.get("type") not in ("movie", "tv", "info"):
        return item
    try:
        params: Dict[str, str] = {"apikey": api_key, "t": str(item.get("title", ""))}
        if item.get("year"):
            params["y"] = str(item["year"])
        data = await _get_json(client, "https://www.omdbapi.com/", params) or {}
        if str(data.get("Response")).lower() != "true":
            return item
        if data.get("imdbRating") and data["imdbRating"] != "N/A" and not item.get("imdb_rating"):
            try:
                item["imdb_rating"] = float(data["imdbRating"])
            except Exception:
                pass
        for r in data.get("Ratings") or []:
            if r.get("Source") == "Rotten Tomatoes" and not item.get("rotten_tomatoes"):
                item["rotten_tomatoes"] = r.get("Value")
        if data.get("BoxOffice") and data["BoxOffice"] != "N/A":
            item["revenue_box_office"] = item.get("revenue_box_office") or data["BoxOffice"]
            item["box_office_formatted"] = data["BoxOffice"]
        if data.get("Runtime") and data["Runtime"] != "N/A" and not item.get("runtime"):
            item["runtime"] = data["Runtime"]
        if data.get("Language") and data["Language"] != "N/A":
            item["languages"] = [part.strip() for part in data["Language"].split(",")]
        if data.get("Released") and data["Released"] != "N/A" and not item.get("release_date"):
            item["release_date"] = data["Released"]
        if data.get("imdbID") and not item.get("imdb_id"):
            item["imdb_id"] = data["imdbID"]
            item["imdb_url"] = f"https://www.imdb.com/title/{data['imdbID']}/"
    except Exception as e:
        logger.debug(f"OMDb enrich failed: {e}")
    return item


async def enrich_wiki_url(client: httpx.AsyncClient, item: Dict[str, Any]) -> Dict[str, Any]:
    if item.get("wiki_url"):
        return item
    try:
        summary = await _get_json(
            client, f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(str(item.get('title', '')))}"
        )
        if summary and summary.get("type") != "disambiguation":
            page = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page")
            if page:
                item["wiki_url"] = page
            if summary.get("extract") and not item.get("overview"):
                item["overview"] = summary["extract"]
    except Exception:
        pass
    if not item.get("wiki_url"):
        item["wiki_url"] = (
            f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(str(item.get('title', '')))}"
        )
    return item


MEDIA_TYPES = ("movie", "tv", "book", "game", "all")


async def lookup_media(query: str, media_type: str = "all", limit: int = 8, timeout: float = 15.0) -> Dict[str, Any]:
    """Main Discover lookup — parallel free providers, optional TMDB/OMDb enrichment."""
    q = (query or "").strip()
    if not q:
        return {"query": q, "type": media_type, "total_found": 0, "results": []}
    mt = (media_type or "all").lower()
    if mt in ("series", "show", "shows"):
        mt = "tv"
    if mt not in MEDIA_TYPES:
        mt = "all"

    results: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(headers=UA, timeout=timeout, follow_redirects=True) as client:
            tasks = []
            if mt in ("movie", "all"):
                tasks.append(search_yts(client, q, limit=6))
            if mt in ("tv", "all"):
                tasks.append(search_tvmaze(client, q, limit=6))
            if mt in ("book", "all"):
                tasks.append(search_openlibrary(client, q, limit=6))
            if mt in ("game", "all") or (mt == "all" and len(tasks) < 3):
                # Games have no free no-key DB; Wikipedia covers them well.
                tasks.append(search_wikipedia(client, f"{q} {'video game' if mt == 'game' else ''}".strip(), limit=4))
            elif mt not in ("movie", "tv", "book"):
                tasks.append(search_wikipedia(client, q, limit=4))
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for g in gathered:
                if isinstance(g, Exception):
                    logger.debug(f"Discover provider error: {g}")
                    continue
                if isinstance(g, list):
                    results.extend(g)

            # Enrich top results (bounded to keep latency sane).
            enrich_tasks = []
            for item in results[:6]:
                enrich_tasks.append(enrich_wiki_url(client, item))
            if enrich_tasks:
                await asyncio.gather(*enrich_tasks, return_exceptions=True)
            if os.environ.get("TMDB_API_KEY"):
                await asyncio.gather(*[enrich_with_tmdb(client, it) for it in results[:4]], return_exceptions=True)
            if os.environ.get("OMDB_API_KEY"):
                await asyncio.gather(*[enrich_with_omdb(client, it) for it in results[:4]], return_exceptions=True)
    except Exception as e:
        logger.warning(f"Discover lookup failed for '{q}': {e}")

    # De-duplicate by (type, title.lower()).
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for r in results:
        key = (str(r.get("type")), str(r.get("title", "")).lower())
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # Rank: movies/tv with ratings first, then the rest.
    def _rank(r: Dict[str, Any]) -> tuple:
        rating = r.get("imdb_rating")
        try:
            rating_f = float(rating) if rating is not None else 0.0
        except Exception:
            rating_f = 0.0
        type_boost = {"movie": 2, "tv": 2, "book": 1, "info": 0, "game": 1}.get(str(r.get("type")), 0)
        return (type_boost, rating_f)

    deduped.sort(key=_rank, reverse=True)
    return {
        "query": q,
        "type": mt,
        "total_found": len(deduped),
        "returned": min(len(deduped), limit),
        "results": deduped[:limit],
    }
