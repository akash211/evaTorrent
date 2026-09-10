"""Media metadata lookup for evaTorrent Discover tab.

Aggregates free, no-key public APIs:
- Movies: YTS API (rating, runtime, genres, IMDb code, YouTube trailer code)
- TV/Series: TVMaze API (premiered, runtime, rating, network, language)
- Books: OpenLibrary (author, first publish year, cover, languages)
- Games: RAWG when RAWG_API_KEY is set, otherwise Wikipedia fallback
- Generic enrichment: Wikipedia REST summary, IMDb/RT/JustWatch/YouTube links

Optional enrichment when keys are configured:
- TMDB_API_KEY: accurate budget/revenue + India OTT providers
- OMDB_API_KEY: IMDb/RT ratings, runtime, box office, languages

All lookups are best-effort with short timeouts; failures degrade to links.
"""

from __future__ import annotations

from evatorrent.metadata.service import keys_configured, lookup_media

__all__ = ["keys_configured", "lookup_media"]
