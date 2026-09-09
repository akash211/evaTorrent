"""Top-level EngineManager coordinating multiple TorrentSession instances."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from evatorrent.bencoding import bdecode
from evatorrent.db.database import Database
from evatorrent.engine.session import TorrentSession
from evatorrent.torrent import Magnet, Torrent, fetch_torrent_from_caches

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_DIR = Path(
    os.environ.get("DOWNLOAD_DIR") or (Path.home() / "Downloads" / "evaTorrent")
)


class EngineManager:
    """Manages all active BitTorrent sessions in the evaTorrent engine."""

    def __init__(self, default_download_dir: Optional[Path] = None, db: Optional[Database] = None):
        self.download_dir = Path(
            default_download_dir
            or os.environ.get("DOWNLOAD_DIR")
            or DEFAULT_DOWNLOAD_DIR
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: Dict[str, TorrentSession] = {}
        self.db = db

    def add_torrent(self, torrent: Torrent, output_dir: Optional[Path] = None) -> TorrentSession:
        info_hash_hex = torrent.info_hash_hex
        if info_hash_hex in self.sessions:
            return self.sessions[info_hash_hex]

        dest_dir = output_dir or self.download_dir
        session = TorrentSession(torrent=torrent, download_dir=dest_dir, db=self.db)
        self.sessions[info_hash_hex] = session

        if self.db:
            self.db.upsert_torrent(
                info_hash=info_hash_hex,
                name=torrent.name,
                total_size=torrent.total_length,
                download_dir=str(dest_dir),
                status=session.status.value,
            )

        session.start()
        return session

    def add_torrent_bytes(self, data: bytes, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent(data)
        return self.add_torrent(torrent, output_dir)

    def add_torrent_file(self, filepath: Path, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent.from_file(filepath)
        return self.add_torrent(torrent, output_dir)

    async def add_magnet(self, magnet_uri: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Resolves magnet link metadata and enrolls the download in the engine."""
        magnet = Magnet(magnet_uri)
        info_hash_hex = magnet.info_hash_hex.lower()
        if info_hash_hex in self.sessions:
            return self.sessions[info_hash_hex]

        cache_dir = self.download_dir / ".torrent_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        torrent_file = cache_dir / f"{info_hash_hex}.torrent"

        raw_bytes: Optional[bytes] = None
        if torrent_file.exists():
            try:
                raw_bytes = torrent_file.read_bytes()
            except Exception:
                raw_bytes = None

        if not raw_bytes:
            raw_bytes = await fetch_torrent_from_caches(info_hash_hex)
            if raw_bytes:
                try:
                    torrent_file.write_bytes(raw_bytes)
                except Exception as e:
                    logger.warning(f"Failed to cache torrent file: {e}")

        if not raw_bytes:
            name_hint = f' "{magnet.name}"' if magnet.name else ""
            raise ValueError(
                f"Could not retrieve metadata for magnet{name_hint} ({info_hash_hex[:8]}...). "
                f"The torrent metainfo was not found in public torrent caches. Please upload the .torrent file directly."
            )

        torrent = Torrent(raw_bytes)
        for tr in magnet.trackers:
            if tr not in torrent.trackers:
                torrent.trackers.append(tr)

        return self.add_torrent(torrent, output_dir)

    async def add_url(self, url: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Downloads a .torrent file from an HTTP/HTTPS URL and enrolls it."""
        clean_url = url.strip()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(clean_url)
                if resp.status_code != 200:
                    raise ValueError(f"HTTP request to torrent URL failed with status {resp.status_code}")

                content = resp.content
                # Check if it is a valid bencoded torrent
                is_bencoded = False
                try:
                    decoded = bdecode(content)
                    if isinstance(decoded, dict) and b"info" in decoded:
                        is_bencoded = True
                except Exception:
                    is_bencoded = False

                if is_bencoded:
                    return self.add_torrent_bytes(content, output_dir)

                # If not direct torrent bytes, check if response text contains a magnet link
                text = resp.text
                magnet_match = re.search(r'magnet:\?[^"\'\s<>]+', text)
                if magnet_match:
                    return await self.add_magnet(magnet_match.group(0), output_dir)

                raise ValueError("URL did not return a valid .torrent file or magnet link.")
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"Failed to fetch torrent from URL: {e}")

    async def add_torrent_or_url(self, input_str: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Accepts a magnet link, HTTP/HTTPS URL, or file path and enrolls it."""
        s = input_str.strip()
        if s.startswith("magnet:?"):
            return await self.add_magnet(s, output_dir)
        elif s.startswith("http://") or s.startswith("https://"):
            return await self.add_url(s, output_dir)
        elif len(s) == 40 and all(c in "0123456789abcdefABCDEF" for c in s):
            return await self.add_magnet(f"magnet:?xt=urn:btih:{s}", output_dir)
        else:
            path = Path(s)
            if path.is_file():
                return self.add_torrent_file(path, output_dir)
            raise ValueError(
                "Input must be a valid magnet link (magnet:?), Web URL (http/https), or .torrent file path."
            )

    def get_session(self, info_hash_hex: str) -> Optional[TorrentSession]:
        return self.sessions.get(info_hash_hex.lower())

    async def pause_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            await session.pause()
            if self.db:
                self.db.log_event(info_hash_hex, "PAUSED", "Torrent paused by user")
            return True
        return False

    def resume_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.resume()
            if self.db:
                self.db.log_event(info_hash_hex, "RESUMED", "Torrent resumed by user")
            return True
        return False

    def set_speed_limit(self, info_hash_hex: str, limit_bytes_per_sec: Optional[int]) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.set_download_limit(limit_bytes_per_sec)
            return True
        return False

    async def remove_torrent(self, info_hash_hex: str, delete_files: bool = False) -> bool:
        key = info_hash_hex.lower()
        session = self.sessions.pop(key, None)
        if session:
            await session.stop()

            if self.db:
                self.db.mark_torrent_removed(
                    info_hash=key,
                    deleted_files=delete_files,
                    downloaded_bytes=session.piece_manager.bytes_downloaded,
                    uploaded_bytes=session.piece_manager.bytes_uploaded,
                )

            if delete_files:
                try:
                    for f in session.torrent.files:
                        fp = session.download_dir / f.path
                        if fp.exists():
                            fp.unlink(missing_ok=True)
                    if session.torrent.is_multi_file:
                        top_dir = session.download_dir / session.torrent.name
                        if top_dir.exists() and top_dir.is_dir():
                            shutil.rmtree(top_dir, ignore_errors=True)
                except Exception as e:
                    logger.warning(f"Error deleting files for {info_hash_hex}: {e}")
            return True
        return False

    async def shutdown(self) -> None:
        """Stops all running torrent sessions."""
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()

    def get_all_torrents(self) -> List[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    def get_global_stats(self) -> dict:
        total_dl = sum(s.download_speed for s in self.sessions.values())
        total_ul = sum(s.upload_speed for s in self.sessions.values())
        total_bytes_dl = sum(s.piece_manager.bytes_downloaded for s in self.sessions.values())
        total_bytes_ul = sum(s.piece_manager.bytes_uploaded for s in self.sessions.values())

        return {
            "active_torrents": len(self.sessions),
            "total_download_speed": round(total_dl, 2),
            "total_upload_speed": round(total_ul, 2),
            "total_downloaded": total_bytes_dl,
            "total_uploaded": total_bytes_ul,
        }
