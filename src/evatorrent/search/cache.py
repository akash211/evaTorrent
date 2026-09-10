"""Search cache manager persisting search queries and results to Downloads folder."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evaTorrent.search.cache")


def format_cache_age(seconds: float) -> str:
    """Returns a friendly human-readable elapsed duration string."""
    if seconds < 60:
        return "just now"
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins} min{'s' if mins > 1 else ''} ago"
    hours = int(mins // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days > 1 else ''} ago"


class SearchCacheManager:
    """Manages cross-session, cross-device cached search results in {DOWNLOAD_DIR}/cached_results."""

    def __init__(self, cache_dir: Path, max_cached: int = 100):
        self.cache_dir = Path(cache_dir)
        self.max_cached = max_cached
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.cache_dir / "index.json"
        self._ensure_index()

    def _ensure_index(self) -> None:
        if not self.index_file.exists():
            self._save_index([])

    def _load_index(self) -> List[Dict[str, Any]]:
        try:
            if self.index_file.exists():
                with self.index_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            logger.warning(f"Error loading search cache index: {e}")
        return []

    def _save_index(self, entries: List[Dict[str, Any]]) -> None:
        try:
            temp_file = self.index_file.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
            temp_file.replace(self.index_file)
        except Exception as e:
            logger.warning(f"Error saving search cache index: {e}")

    def _cache_filename(self, query: str, category: str) -> str:
        clean_q = re.sub(r"[^\w\s-]", "", query.strip().lower())
        clean_q = re.sub(r"[-\s]+", "_", clean_q)[:40]
        cat = category.strip().lower()
        return f"search_{clean_q}_{cat}.json"

    def get(self, query: str, category: str = "all") -> Optional[Dict[str, Any]]:
        """Retrieves cached search results if present."""
        filename = self._cache_filename(query, category)
        cache_file = self.cache_dir / filename
        if not cache_file.exists():
            return None

        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            cached_ts = data.get("cached_timestamp", 0)
            age = max(0.0, time.time() - cached_ts)
            data["cache_age_seconds"] = round(age)
            data["cache_age_human"] = format_cache_age(age)
            data["is_cached"] = True
            return data
        except Exception as e:
            logger.warning(f"Error reading cache file {filename}: {e}")
            return None

    def set(
        self,
        query: str,
        category: str,
        results: List[Dict[str, Any]],
        suggestion: Optional[str] = None,
        total_found: Optional[int] = None,
        fallback_applied: bool = False,
    ) -> None:
        """Stores search results in cache and prunes entries beyond max_cached."""
        if not results:
            return  # Only cache searches where results were found

        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        filename = self._cache_filename(query, category)
        cache_file = self.cache_dir / filename

        total_cnt = total_found if total_found is not None else len(results)
        cache_payload = {
            "query": query.strip(),
            "category": category.strip().lower(),
            "cached_at": now_iso,
            "cached_timestamp": now,
            "total_found": total_cnt,
            "returned": len(results),
            "fallback_applied": fallback_applied,
            "suggestion": suggestion,
            "results": results,
        }

        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(cache_payload, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write search cache file {filename}: {e}")
            return

        # Update index (keep up to max_cached entries)
        index = self._load_index()
        q_norm = query.strip().lower()
        cat_norm = category.strip().lower()

        # Remove duplicate previous entry for same query+category if present
        index = [
            item
            for item in index
            if not (
                item.get("query", "").strip().lower() == q_norm and item.get("category", "").strip().lower() == cat_norm
            )
        ]

        # Insert newest entry at beginning
        index.insert(
            0,
            {
                "query": query.strip(),
                "category": cat_norm,
                "cached_at": now_iso,
                "cached_timestamp": now,
                "total_results": total_cnt,
                "filename": filename,
            },
        )

        # Prune if exceeding max_cached (last 100)
        if len(index) > self.max_cached:
            to_remove = index[self.max_cached :]
            index = index[: self.max_cached]
            for item in to_remove:
                old_file = self.cache_dir / item.get("filename", "")
                if old_file.exists():
                    try:
                        old_file.unlink(missing_ok=True)
                    except Exception:
                        pass

        self._save_index(index)

    def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Returns the most recent cached searches."""
        index = self._load_index()
        now = time.time()
        recent = []
        for item in index[:limit]:
            age = max(0.0, now - item.get("cached_timestamp", now))
            item_copy = dict(item)
            item_copy["cache_age_seconds"] = round(age)
            item_copy["cache_age_human"] = format_cache_age(age)
            recent.append(item_copy)
        return recent
