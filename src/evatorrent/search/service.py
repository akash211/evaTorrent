"""Coordinating search service managing multiple indexer providers."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from evatorrent.search.base import (
    BaseSearchProvider,
    SearchCategory,
    SearchResult,
)
from evatorrent.search.piratebay import PirateBaySearchProvider

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

    def __init__(self, providers: Sequence[BaseSearchProvider] | None = None):
        if providers is None:
            self.providers = [PirateBaySearchProvider()]
        else:
            self.providers = list(providers)

    async def search(
        self,
        query: str,
        category: str = "all",
        hide_dead: bool = True,
        limit: int = 100,
        timeout: float = 30.0,
    ) -> dict:
        """Searches across configured providers with fallback spelling suggestions.

        Parameters
        ----------
        query : str
            The search query.
        category : str
            One of: all, movies, series, software, games, books.
        hide_dead : bool
            If True, prioritizes and filters for torrents with active seeders (> 0).
            If no active torrents exist, automatically falls back to showing all matches.
        limit : int
            Maximum number of results to return.
        timeout : float
            Timeout in seconds for provider queries.
        """
        q = query.strip()
        if not q:
            return {
                "query": q,
                "category": category,
                "total_found": 0,
                "returned": 0,
                "fallback_applied": False,
                "suggestion": None,
                "results": [],
            }

        try:
            cat_enum = SearchCategory(category.lower())
        except ValueError:
            cat_enum = SearchCategory.ALL

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

        # Sort by seeders descending, then leechers descending
        all_results.sort(key=lambda x: (x.seeders, x.leechers), reverse=True)

        total_found = len(all_results)
        active_results = [r for r in all_results if r.seeders > 0 or r.leechers > 0]
        fallback_applied = False

        if hide_dead:
            if active_results:
                final_results = active_results
            else:
                # Fallback: if no active torrents found, show what was found
                final_results = all_results
                fallback_applied = True
        else:
            final_results = all_results

        final_slice = final_results[:limit]

        return {
            "query": q,
            "category": cat_enum.value,
            "total_found": total_found,
            "returned": len(final_slice),
            "fallback_applied": fallback_applied,
            "suggestion": suggestion_used,
            "results": [item.to_dict() for item in final_slice],
        }
