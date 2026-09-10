"""Search package for evaTorrent multi-indexer torrent aggregation."""

from __future__ import annotations

from evatorrent.search.base import SearchCategory, SearchResult
from evatorrent.search.cache import SearchCacheManager
from evatorrent.search.eztv import EztvSearchProvider
from evatorrent.search.health import compute_health_score
from evatorrent.search.limetorrents import LimeTorrentsSearchProvider
from evatorrent.search.nyaa import NyaaSearchProvider
from evatorrent.search.piratebay import PirateBaySearchProvider
from evatorrent.search.service import SearchService
from evatorrent.search.x1337 import X1337SearchProvider
from evatorrent.search.yts import YtsSearchProvider

__all__ = [
    "SearchCategory",
    "SearchResult",
    "SearchService",
    "SearchCacheManager",
    "PirateBaySearchProvider",
    "LimeTorrentsSearchProvider",
    "YtsSearchProvider",
    "EztvSearchProvider",
    "NyaaSearchProvider",
    "X1337SearchProvider",
    "compute_health_score",
]
