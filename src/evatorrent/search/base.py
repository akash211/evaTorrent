"""Data models and abstract interface for torrent search providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class SearchCategory(str, Enum):
    ALL = "all"
    MOVIES = "movies"
    SERIES = "series"
    SOFTWARE = "software"
    GAMES = "games"
    BOOKS = "books"


def format_bytes(num_bytes: int) -> str:
    """Formats raw bytes into human-readable representation."""
    if num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}" if idx > 0 else f"{int(size)} B"


@dataclass
class SearchResult:
    """Represents a unified torrent search match from an indexer."""
    title: str
    info_hash: str
    magnet_uri: str
    size_bytes: int
    size_formatted: str
    seeders: int
    leechers: int
    category: str
    provider: str
    added_date: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "info_hash": self.info_hash,
            "magnet_uri": self.magnet_uri,
            "size_bytes": self.size_bytes,
            "size_formatted": self.size_formatted,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "category": self.category,
            "provider": self.provider,
            "added_date": self.added_date,
        }


class BaseSearchProvider(ABC):
    """Abstract interface implemented by all torrent search providers."""

    name: str = "base"

    @abstractmethod
    async def search(
        self,
        query: str,
        category: SearchCategory = SearchCategory.ALL,
        timeout: float | None = None,
    ) -> list[SearchResult]:
        """Performs search on provider and returns list of SearchResult objects."""
        pass
