"""Coordinating search service managing multiple indexer providers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Sequence

from evatorrent.search.base import (
    BaseSearchProvider,
    SearchCategory,
    SearchResult,
)
from evatorrent.search.cache import SearchCacheManager
from evatorrent.search.eztv import EztvSearchProvider
from evatorrent.search.health import compute_health_score
from evatorrent.search.limetorrents import LimeTorrentsSearchProvider
from evatorrent.search.nyaa import NyaaSearchProvider
from evatorrent.search.piratebay import PirateBaySearchProvider
from evatorrent.search.x1337 import X1337SearchProvider
from evatorrent.search.yts import YtsSearchProvider

import re

logger = logging.getLogger("evaTorrent.search.service")


def get_query_variations(query: str) -> list[str]:
    """Generates common spelling and keyword variations to maximize search matches."""
    variations: list[str] = []
    q_clean = query.strip()

    # 1. Common British vs American spelling swaps
    spelling_swaps = [
        (r"\btraveller\b", "traveler"),
        (r"\btravellers\b", "travelers"),
        (r"\btravelling\b", "traveling"),
        (r"\btheatre\b", "theater"),
        (r"\btheatres\b", "theaters"),
        (r"\bcolour\b", "color"),
        (r"\bcolours\b", "colors"),
        (r"\bneighbour\b", "neighbor"),
        (r"\bneighbours\b", "neighbors"),
        (r"\bgrey\b", "gray"),
    ]
    cur = q_clean
    for pat, rep in spelling_swaps:
        if re.search(pat, cur, re.IGNORECASE):
            cur = re.sub(pat, rep, cur, flags=re.IGNORECASE)
            if cur.lower() != q_clean.lower() and cur not in variations:
                variations.append(cur)

    # 2. Punctuation stripping (e.g. apostrophes, commas, dashes)
    no_punct = re.sub(r"['\":,\-!?_]", " ", q_clean)
    no_punct = re.sub(r"\s+", " ", no_punct).strip()
    if no_punct.lower() != q_clean.lower() and no_punct not in variations:
        variations.append(no_punct)

    # 3. Toggling leading "the "
    if q_clean.lower().startswith("the "):
        without_the = q_clean[4:].strip()
        if without_the and without_the not in variations:
            variations.append(without_the)
    else:
        with_the = f"the {q_clean}"
        if with_the not in variations:
            variations.append(with_the)

    return variations


class SearchService:
    """Coordinates parallel querying of torrent indexers, ranking, and deduplication."""

    def __init__(
        self,
        providers: Sequence[BaseSearchProvider] | None = None,
        cache_manager: SearchCacheManager | None = None,
    ):
        if providers is None:
            self.providers = [
                PirateBaySearchProvider(),
                LimeTorrentsSearchProvider(),
                YtsSearchProvider(),
                EztvSearchProvider(),
                NyaaSearchProvider(),
                X1337SearchProvider(),
            ]
        else:
            self.providers = list(providers)
        self.cache_manager = cache_manager

    async def search(
        self,
        query: str,
        category: str = "all",
        hide_dead: bool = True,
        limit: int = 100,
        timeout: float = 30.0,
        refresh: bool = False,
    ) -> dict:
        """Searches across configured providers with fallback spelling suggestions and caching."""
        q = query.strip()
        if not q:
            return {
                "query": q,
                "category": category,
                "total_found": 0,
                "returned": 0,
                "fallback_applied": False,
                "suggestion": None,
                "is_cached": False,
                "results": [],
            }

        try:
            cat_enum = SearchCategory(category.lower())
        except ValueError:
            cat_enum = SearchCategory.ALL

        # 0. Check cache if not explicitly refreshing
        if not refresh and self.cache_manager:
            cached_entry = self.cache_manager.get(q, cat_enum.value)
            if cached_entry:
                logger.info(f"Serving search for '{q}' ({cat_enum.value}) from cache ({cached_entry.get('cache_age_human')})")
                return cached_entry

        async def _query_providers(search_term: str) -> list[SearchResult]:
            tasks = [p.search(search_term, category=cat_enum, timeout=timeout) for p in self.providers]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            res_list: list[SearchResult] = []
            seen: set[str] = set()
            for r in gathered:
                if isinstance(r, Exception):
                    logger.error(f"Provider search error for '{search_term}': {r}")
                    continue
                for item in r:
                    h = item.info_hash.lower()
                    if h not in seen:
                        seen.add(h)
                        res_list.append(item)
            return res_list

        # 1. Primary search
        all_results = await _query_providers(q)
        suggestion_used: str | None = None

        # 2. Smart fallback if 0 results found
        if not all_results:
            for alt in get_query_variations(q):
                alt_results = await _query_providers(alt)
                if alt_results:
                    all_results = alt_results
                    suggestion_used = alt
                    break

        # 3. Compute health scores for each result
        now = datetime.now(timezone.utc)
        for r in all_results:
            # Estimate age_days from added_date if available
            if r.added_date and r.age_days == 0:
                try:
                    # Handle various date formats
                    date_str = r.added_date.strip()
                    if len(date_str) == 10:  # YYYY-MM-DD
                        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    elif len(date_str) == 19:  # YYYY-MM-DD HH:MM:SS
                        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    else:
                        dt = None
                    if dt:
                        r.age_days = max(0, (now - dt).days)
                except (ValueError, TypeError):
                    pass
            r.health_score = compute_health_score(
                seeders=r.seeders,
                leechers=r.leechers,
                age_days=r.age_days,
                size_bytes=r.size_bytes,
            )

        # Sort by health_score descending (primary), then seeders descending (secondary)
        all_results.sort(key=lambda x: (x.health_score, x.seeders, x.leechers), reverse=True)

        total_found = len(all_results)
        fallback_applied = False

        if hide_dead:
            # Filter out confirmed dead & ghost torrents (health_score < 0.05)
            healthy = [r for r in all_results if r.health_score >= 0.05]
            if healthy:
                final_results = healthy
            else:
                # Fallback: if no healthy torrents found, show what was found
                final_results = all_results
                fallback_applied = True
        else:
            final_results = all_results

        final_slice = final_results[:limit]

        ret = {
            "query": q,
            "category": cat_enum.value,
            "total_found": total_found,
            "returned": len(final_slice),
            "fallback_applied": fallback_applied,
            "suggestion": suggestion_used,
            "is_cached": False,
            "results": [item.to_dict() for item in final_slice],
        }

        # Cache results if any found
        if total_found > 0 and self.cache_manager:
            self.cache_manager.set(
                query=q,
                category=cat_enum.value,
                results=ret["results"],
                suggestion=suggestion_used,
                total_found=total_found,
                fallback_applied=fallback_applied,
            )

        return ret

