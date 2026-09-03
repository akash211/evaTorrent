"""Top-level EngineManager coordinating multiple TorrentSession instances."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from evatorrent.engine.session import TorrentSession
from evatorrent.torrent import Torrent

logger = logging.getLogger(__name__)


class EngineManager:
    """Manages all active BitTorrent sessions in the evaTorrent engine."""

    def __init__(self, default_download_dir: Optional[Path] = None):
        self.download_dir = Path(default_download_dir or Path.home() / "Downloads" / "evaTorrent")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: Dict[str, TorrentSession] = {}

    def add_torrent(self, torrent: Torrent, output_dir: Optional[Path] = None) -> TorrentSession:
        info_hash_hex = torrent.info_hash_hex
        if info_hash_hex in self.sessions:
            return self.sessions[info_hash_hex]

        dest_dir = output_dir or self.download_dir
        session = TorrentSession(torrent=torrent, download_dir=dest_dir)
        self.sessions[info_hash_hex] = session
        session.start()
        return session

    def add_torrent_bytes(self, data: bytes, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent(data)
        return self.add_torrent(torrent, output_dir)

    def add_torrent_file(self, filepath: Path, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent.from_file(filepath)
        return self.add_torrent(torrent, output_dir)

    def get_session(self, info_hash_hex: str) -> Optional[TorrentSession]:
        return self.sessions.get(info_hash_hex.lower())

    async def pause_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            await session.pause()
            return True
        return False

    def resume_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.resume()
            return True
        return False

    async def remove_torrent(self, info_hash_hex: str, delete_files: bool = False) -> bool:
        key = info_hash_hex.lower()
        session = self.sessions.pop(key, None)
        if session:
            await session.stop()
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
