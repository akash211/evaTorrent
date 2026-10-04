"""Auto-updated public tracker fallback list (ngosang/trackerslist).

The hardcoded fallback trackers rot over time; this module keeps a fresh copy
of the community-maintained ``trackers_best.txt`` (top ~20 by popularity and
latency, refreshed daily upstream) on disk and serves it to the engine:

- Cache lives in ``{DOWNLOAD_DIR}/.tracker_cache/best.txt`` (+ ``meta.json``).
- Refreshed at boot and daily when older than EVA_TRACKER_CACHE_DAYS (7).
- Three mirrors tried in order; results validated before replacing the cache.
- On any failure the previous cache (or the baked-in defaults) is kept, so
  tracker enrichment can never break downloading.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

MIRRORS = [
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://ngosang.github.io/trackerslist/trackers_best.txt",
]

CACHE_FILENAME = "best.txt"
META_FILENAME = "meta.json"
MIN_TRACKERS = 5
FETCH_TIMEOUT = 15.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def cache_paths(base_dir: Path) -> Tuple[Path, Path]:
    d = Path(base_dir) / ".tracker_cache"
    return d / CACHE_FILENAME, d / META_FILENAME


def _validate(lines: List[str]) -> List[str]:
    """Keeps plausible announce URLs only (scheme + no spaces + sane length)."""
    seen: List[str] = []
    for raw in lines:
        url = raw.strip()
        if not url or len(url) > 256 or " " in url:
            continue
        if not url.startswith(("udp://", "http://", "https://")):
            continue
        if url not in seen:
            seen.append(url)
    return seen


def read_cached(base_dir: Path) -> Tuple[List[str], float, str]:
    """Returns (trackers, age_days, source). Empty list when no cache exists."""
    cache_file, meta_file = cache_paths(Path(base_dir))
    if not cache_file.exists():
        return [], float("inf"), "none"
    try:
        trackers = _validate(cache_file.read_text().splitlines())
    except Exception:
        return [], float("inf"), "corrupt"
    age_days = float("inf")
    source = "cache"
    try:
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            age_days = (time.time() - float(meta.get("fetched_at", 0))) / 86400.0
            source = str(meta.get("source", "cache"))
    except Exception:
        try:
            age_days = (time.time() - cache_file.stat().st_mtime) / 86400.0
        except Exception:
            pass
    return trackers, age_days, source


async def refresh_fallback_trackers(base_dir: Path, client: Optional[httpx.AsyncClient] = None) -> List[str]:
    """Fetches a fresh best-list from mirrors, validates, caches, returns it.

    Raises the last error when every mirror fails or yields too few trackers;
    callers should fall back to the previous cache / baked-in defaults.
    """
    last_err: Optional[Exception] = None
    close_client = False
    if client is None:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "evaTorrent/0.9"},
        )
        close_client = True
    try:
        for url in MIRRORS:
            try:
                resp = await client.get(url)
                if resp.status_code != 200 or not resp.text:
                    logger.debug(f"[TRACKERS] Mirror {url} returned HTTP {resp.status_code}")
                    continue
                trackers = _validate(resp.text.splitlines())
                if len(trackers) < MIN_TRACKERS:
                    logger.warning(f"[TRACKERS] Mirror {url} yielded only {len(trackers)} trackers; trying next")
                    continue
                cache_file, meta_file = cache_paths(Path(base_dir))
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text("\n".join(trackers) + "\n")
                    meta_file.write_text(
                        json.dumps({"fetched_at": time.time(), "source": url, "count": len(trackers)})
                    )
                except Exception as e:
                    logger.warning(f"[TRACKERS] Fetched {len(trackers)} trackers but failed to cache: {e}")
                logger.info(f"[TRACKERS] Refreshed fallback list: {len(trackers)} trackers via {url}")
                return trackers
            except Exception as e:
                last_err = e
                logger.debug(f"[TRACKERS] Mirror {url} failed: {e}")
    finally:
        if close_client:
            try:
                await client.aclose()
            except Exception:
                pass
    raise RuntimeError(f"All tracker mirrors failed: {last_err}")


async def ensure_fresh_trackers(base_dir: Path, max_age_days: Optional[float] = None) -> List[str]:
    """Returns the cached list, refreshing from mirrors when stale/missing."""
    if max_age_days is None:
        max_age_days = _env_float("EVA_TRACKER_CACHE_DAYS", 7.0)
    cached, age_days, _source = read_cached(base_dir)
    if cached and age_days <= max_age_days:
        return cached
    try:
        return await refresh_fallback_trackers(base_dir)
    except Exception as e:
        if cached:
            logger.warning(f"[TRACKERS] Refresh failed ({e}); keeping {len(cached)} cached trackers")
            return cached
        raise
