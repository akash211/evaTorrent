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

logger = logging.getLogger("evaTorrent.search.service")


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
    ) -> dict:
        """Searches across configured providers.

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
        """
        q = query.strip()
        if not q:
            return {
                "query": q,
                "category": category,
                "total_found": 0,
                "returned": 0,
                "fallback_applied": False,
                "results": [],
            }

        try:
            cat_enum = SearchCategory(category.lower())
        except ValueError:
            cat_enum = SearchCategory.ALL

        tasks = [provider.search(q, category=cat_enum) for provider in self.providers]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        all_results: list[SearchResult] = []
        seen_hashes: set[str] = set()

        for res in gathered:
            if isinstance(res, Exception):
                logger.error(f"Provider search error: {res}")
                continue
            for item in res:
                h = item.info_hash.lower()
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_results.append(item)

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
            "results": [item.to_dict() for item in final_slice],
        }
