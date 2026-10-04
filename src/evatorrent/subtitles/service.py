"""Subtitle service: video scan, English search, safe SRT download.

Security model (per request):
- Never serves upstream ZIP bytes to the browser.
- ZIPs are extracted server-side in memory; only a single validated
  .srt/.vtt/.sub/.ssa/.ass text track is written next to the video.
- Zip-slip guarded (rejects absolute paths, "..", oversized entries).
- Subtitle content validated (timestamp marker "-->", size <= 2 MB).
- Saved filename always matches the video stem so players pick it up
  without renaming (e.g. ``Dune.Part.Two.2024.mkv`` -> ``Dune.Part.Two.2024.srt``).
"""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".mpg", ".mpeg"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".sub", ".ssa", ".ass"}
SKIP_DIRS = {".torrent_cache", "cached_results", ".git", "__pycache__"}

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 20 * 1024 * 1024

_RELEASE_TAGS = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hd|web-?dl|webrip|bluray|brrip|bdrip|dvdrip|"
    r"hdtv|amzn|netflix|hmax|dsnp|x264|x265|h\.?264|h\.?265|hevc|xvid|aac|ac3|dts|ddp|"
    r"yify|yts|psa|evo|rartv|ettv|proper|repack|extended|remux|dual|multi|eng|hin|hindi)\b",
    re.IGNORECASE,
)
_SEASON_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


@dataclass
class VideoEntry:
    path: str  # relative to download dir, posix
    name: str  # file name
    size: int
    has_subtitle: bool


@dataclass
class SubtitleResult:
    id: str
    title: str
    language: str
    release: str
    rating: float
    downloads: int
    provider: str
    download_url: str
    detail_url: str = ""


def clean_video_title(filename: str) -> tuple[str, Optional[int], Optional[int], Optional[int]]:
    """Returns (query, year, season, episode) derived from a video filename."""
    stem = Path(filename).stem
    season = episode = None
    m = _SEASON_EP.search(stem)
    if m:
        try:
            season, episode = int(m.group(1)), int(m.group(2))
        except ValueError:
            pass
    year = None
    ym = _YEAR.search(stem)
    if ym:
        try:
            year = int(ym.group(1))
        except ValueError:
            pass
    text = re.sub(r"[._\-+]+", " ", stem)
    text = _SEASON_EP.sub(" ", text)
    text = _YEAR.sub(" ", text)
    text = _RELEASE_TAGS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_().[]")
    return (text or stem), year, season, episode


def scan_videos(download_dir: Path, limit: int = 500) -> List[VideoEntry]:
    """Lists video files under download_dir for the subtitle dropdown."""
    base = Path(download_dir)
    entries: List[VideoEntry] = []
    if not base.exists():
        return entries
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(files):
            ext = Path(fn).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            full = Path(root) / fn
            try:
                rel = full.relative_to(base).as_posix()
            except ValueError:
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            stem = full.stem
            has_sub = any((full.parent / f"{stem}{s}").exists() for s in SUBTITLE_EXTENSIONS) or any(
                (full.parent / f"{stem}.en{s}").exists() for s in SUBTITLE_EXTENSIONS
            )
            entries.append(VideoEntry(path=rel, name=fn, size=size, has_subtitle=has_sub))
            if len(entries) >= limit:
                return sorted(entries, key=lambda e: e.name.lower())
    return sorted(entries, key=lambda e: e.name.lower())


def pick_zip_member(names: List[str]) -> Optional[str]:
    """Picks the best subtitle file inside a zip (prefers English SRT)."""
    if not names:
        return None
    cands = [n for n in names if Path(n).suffix.lower() in SUBTITLE_EXTENSIONS]
    if not cands:
        return None

    def score(n: str) -> tuple[int, int, str]:
        low = n.lower()
        is_srt = 0 if low.endswith(".srt") else 1
        is_en = 0 if ("eng" in low or ".en." in low or low.endswith(".en.srt")) else 1
        return (is_en, is_srt, n.lower())

    return sorted(cands, key=score)[0]


def extract_subtitle_from_bytes(payload: bytes, url: str = "") -> tuple[bytes, str]:
    """Extracts a single validated subtitle track from raw download bytes.

    Accepts raw .srt/.vtt/... bytes or a .zip archive (extracted server-side
    so the browser never receives a zip). Returns (content, ext).
    Raises ValueError on unsafe/invalid content.
    """
    if len(payload) > MAX_ZIP_BYTES:
        raise ValueError("Subtitle download exceeds 20 MB size limit.")
    data = payload
    ext = Path(url.split("?")[0]).suffix.lower()
    if payload[:2] == b"PK" or ext == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if not infos:
                    raise ValueError("Zip archive contains no files.")
                member = pick_zip_member([i.filename for i in infos])
                if not member:
                    raise ValueError("Zip archive contains no subtitle (.srt/.vtt/.sub) file.")
                info = next(i for i in infos if i.filename == member)
                if info.file_size > MAX_SUBTITLE_BYTES:
                    raise ValueError("Subtitle inside zip exceeds 2 MB size limit.")
                # Zip-slip guard.
                member_path = Path(member)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("Unsafe path inside subtitle zip.")
                data = zf.read(member)
                ext = member_path.suffix.lower() or ".srt"
        except zipfile.BadZipFile as e:
            raise ValueError(f"Invalid subtitle zip: {e}") from e
    if ext not in SUBTITLE_EXTENSIONS:
        # Sniff: most SRT/VTT payloads contain a timestamp arrow.
        ext = ".srt"
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("Subtitle exceeds 2 MB size limit.")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
            data = text.encode("utf-8")
        except Exception as e:
            raise ValueError(f"Subtitle is not decodable text: {e}") from e
    if "-->" not in text:
        raise ValueError("Downloaded file does not look like subtitles (no timestamp '-->').")
    # Strip executable-ish stragglers: keep text only (already decoded).
    return data, ext


class SubtitleService:
    """Coordinates providers; network via httpx (already a dependency)."""

    def __init__(self, download_dir: Path, timeout: float = 15.0):
        self.download_dir = Path(download_dir)
        self.timeout = timeout

    def videos(self, limit: int = 500) -> List[dict]:
        return [vars(v) for v in scan_videos(self.download_dir, limit=limit)]

    async def search(self, query: str, limit: int = 20) -> dict:
        """Searches English subtitles across providers (keyless first)."""
        from evatorrent.subtitles.providers import search_opensubtitles_org, search_yify

        q = query.strip()
        if not q:
            raise ValueError("Search query is required.")
        results: List[SubtitleResult] = []
        errors: List[str] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) evaTorrent/0.9"},
        ) as client:
            for provider_fn in (search_yify, search_opensubtitles_org):
                try:
                    found = await provider_fn(client, q, limit=limit)
                    results.extend(found)
                except Exception as e:
                    errors.append(f"{provider_fn.__name__}: {e}")
                    logger.debug("Subtitle provider %s failed: %s", provider_fn.__name__, e)
                if len(results) >= limit:
                    break
        # Optional keyed provider last (higher quality when configured).
        if os.environ.get("OPENSUBTITLES_API_KEY"):
            try:
                from evatorrent.subtitles.providers import search_opensubtitles_com

                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "evaTorrent/0.9"},
                ) as client:
                    results.extend(await search_opensubtitles_com(client, q, limit=limit))
            except Exception as e:
                errors.append(f"search_opensubtitles_com: {e}")
        # Rank: downloads desc, rating desc; dedupe by download_url.
        seen: set[str] = set()
        ranked: List[SubtitleResult] = []
        for r in sorted(results, key=lambda r: (r.downloads, r.rating), reverse=True):
            if r.download_url in seen:
                continue
            seen.add(r.download_url)
            ranked.append(r)
            if len(ranked) >= limit:
                break
        return {
            "query": q,
            "language": "en",
            "results": [vars(r) for r in ranked],
            "providers_queried": ["yify", "opensubtitles_org"]
            + (["opensubtitles_com"] if os.environ.get("OPENSUBTITLES_API_KEY") else []),
            "errors": errors,
        }

    async def download_for_video(self, video_rel: str, download_url: str, provider: str = "") -> dict:
        """Downloads, extracts, validates and saves subtitle next to the video.

        Saved name always matches the video stem (e.g. ``Movie.2024.srt``).
        """
        base = self.download_dir.resolve()
        target_video = (base / video_rel).resolve()
        try:
            target_video.relative_to(base)
        except ValueError:
            raise ValueError("Video path escapes the download directory.")
        if not target_video.is_file():
            raise ValueError("Selected video not found in Downloads.")
        if target_video.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("Selected file is not a video.")
        if not download_url.startswith(("http://", "https://")):
            raise ValueError("Invalid subtitle download URL.")

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) evaTorrent/0.9"},
        ) as client:
            resp = await client.get(download_url)
            if resp.status_code != 200 or not resp.content:
                raise ValueError(f"Subtitle download failed (HTTP {resp.status_code}).")
            content, ext = extract_subtitle_from_bytes(bytes(resp.content), url=download_url)

        # Filename matches the video stem so players auto-load it.
        sibling = target_video.parent / f"{target_video.stem}.srt"
        if ext != ".srt":
            # Keep original format alongside a .srt-compatible name when possible;
            # players widest-support .srt, so prefer .srt name with validated content.
            sibling = target_video.parent / f"{target_video.stem}{ext}"
        if sibling.exists():
            sibling = target_video.parent / f"{target_video.stem}.en{ext}"
        sibling.write_bytes(content)
        try:
            rel_saved = sibling.resolve().relative_to(base).as_posix()
        except ValueError:
            rel_saved = sibling.name
        return {
            "success": True,
            "provider": provider,
            "video": video_rel,
            "saved_as": rel_saved,
            "size_bytes": len(content),
            "format": ext.lstrip("."),
        }
