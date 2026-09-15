"""Disk cache for Discover media lookups (mirrors the torrent search cache pattern)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evatorrent.metadata.cache")


def format_cache_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins} min{'s' if mins > 1 else ''} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days > 1 else ''} ago"


class DiscoverCacheManager:
    """Caches Discover results in {DOWNLOAD_DIR}/cached_results as discover_*.json."""

    def __init__(self, cache_dir: Path, max_cached: int = 100):
        self.cache_dir = Path(cache_dir)
        self.max_cached = max_cached
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.cache_dir / "discover_index.json"
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
            logger.warning(f"Error loading discover cache index: {e}")
        return []

    def _save_index(self, entries: List[Dict[str, Any]]) -> None:
        try:
            tmp = self.index_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
            tmp.replace(self.index_file)
        except Exception as e:
            logger.warning(f"Error saving discover cache index: {e}")

    def _filename(self, query: str, media_type: str, year: Optional[int]) -> str:
        clean = re.sub(r"[^\w\s-]", "", query.strip().lower())
        clean = re.sub(r"[-\s]+", "_", clean)[:40]
        suffix = f"_{year}" if year else ""
        return f"discover_{clean}_{media_type.strip().lower()}{suffix}.json"

    def get(self, query: str, media_type: str = "all", year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        cache_file = self.cache_dir / self._filename(query, media_type, year)
        if not cache_file.exists():
            return None
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            age = max(0.0, time.time() - data.get("cached_timestamp", 0))
            data["cache_age_seconds"] = round(age)
            data["cache_age_human"] = format_cache_age(age)
            data["is_cached"] = True
            return data
        except Exception as e:
            logger.warning(f"Error reading discover cache: {e}")
            return None

    def set(
        self,
        query: str,
        media_type: str,
        year: Optional[int],
        payload: Dict[str, Any],
    ) -> None:
        results = payload.get("results") or []
        if not results:
            return
        now = time.time()
        filename = self._filename(query, media_type, year)
        cache_file = self.cache_dir / filename
        stored = dict(payload)
        stored.update(
            {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "cached_timestamp": now,
            }
        )
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(stored, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write discover cache {filename}: {e}")
            return

        index = self._load_index()
        qn, tn = query.strip().lower(), media_type.strip().lower()
        index = [
            it
            for it in index
            if not (
                it.get("query", "").strip().lower() == qn
                and it.get("type", "").strip().lower() == tn
                and it.get("year") == year
            )
        ]
        index.insert(
            0,
            {
                "query": query.strip(),
                "type": tn,
                "year": year,
                "cached_at": stored["cached_at"],
                "cached_timestamp": now,
                "total_results": payload.get("total_found", len(results)),
                "filename": filename,
            },
        )
        if len(index) > self.max_cached:
            for item in index[self.max_cached :]:
                old = self.cache_dir / item.get("filename", "")
                if old.exists():
                    try:
                        old.unlink(missing_ok=True)
                    except Exception:
                        pass
            index = index[: self.max_cached]
        self._save_index(index)

    def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        now = time.time()
        recent = []
        for item in self._load_index()[:limit]:
            age = max(0.0, now - item.get("cached_timestamp", now))
            c = dict(item)
            c["cache_age_seconds"] = round(age)
            c["cache_age_human"] = format_cache_age(age)
            recent.append(c)
        return recent
