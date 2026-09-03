"""TorrentSession orchestrating peers, tracker announces, and progress tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

from evatorrent.engine.connection import PeerConnection
from evatorrent.peer.protocol import Have
from evatorrent.storage.manager import PieceManager
from evatorrent.torrent import Torrent
from evatorrent.tracker import Peer, TrackerResponse
from evatorrent.tracker.manager import TrackerManager

logger = logging.getLogger(__name__)


class TorrentStatus(str, Enum):
    PENDING = "pending"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class TorrentSession:
    """Manages the download and seeding lifecycle of a single torrent."""

    def __init__(
        self,
        torrent: Torrent,
        download_dir: Path,
        max_peers: int = 35,
        port: int = 6881,
    ):
        self.torrent = torrent
        self.download_dir = Path(download_dir)
        self.max_peers = max_peers
        self.port = port

        self.piece_manager = PieceManager(
            torrent=torrent,
            download_dir=self.download_dir,
            on_piece_complete=self._on_piece_completed,
        )
        self.tracker_manager = TrackerManager(torrent.trackers, port=port)

        self.status: TorrentStatus = TorrentStatus.PENDING
        self.error_message: Optional[str] = None

        self.active_peers: Dict[str, PeerConnection] = {}
        self.peer_queue: asyncio.Queue[Peer] = asyncio.Queue()
        self.seen_peers: Set[str] = set()

        self._running: bool = False
        self._main_task: Optional[asyncio.Task] = None
        self._speed_task: Optional[asyncio.Task] = None

        # Speed and ETA calculations
        self.download_speed: float = 0.0  # Bytes/sec
        self.upload_speed: float = 0.0  # Bytes/sec
        self.eta_seconds: Optional[int] = None
        self._last_speed_check: float = time.time()
        self._last_downloaded_bytes: int = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.status = TorrentStatus.DOWNLOADING
        self._main_task = asyncio.create_task(self._main_loop())
        self._speed_task = asyncio.create_task(self._speed_meter_loop())

    async def pause(self) -> None:
        if self.status == TorrentStatus.PAUSED:
            return
        self.status = TorrentStatus.PAUSED
        self._running = False
        for peer_conn in list(self.active_peers.values()):
            await peer_conn.stop()
        self.active_peers.clear()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
        if self._speed_task and not self._speed_task.done():
            self._speed_task.cancel()

    def resume(self) -> None:
        if self.status != TorrentStatus.PAUSED:
            return
        self.start()

    async def stop(self) -> None:
        self._running = False
        self.status = TorrentStatus.COMPLETED if self.piece_manager.is_complete else TorrentStatus.PAUSED
        for peer_conn in list(self.active_peers.values()):
            await peer_conn.stop()
        self.active_peers.clear()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
        if self._speed_task and not self._speed_task.done():
            self._speed_task.cancel()

    def _on_piece_completed(self, piece_index: int) -> None:
        """Broadcasts Have message to all connected peers upon piece verification."""
        have_msg = Have(piece_index=piece_index)
        for peer_conn in self.active_peers.values():
            if peer_conn.is_connected:
                asyncio.create_task(peer_conn.send_message(have_msg))

        if self.piece_manager.is_complete:
            self.status = TorrentStatus.COMPLETED
            logger.info(f"Torrent '{self.torrent.name}' download completed successfully!")

    def _on_peer_disconnected(self, peer_key: str) -> None:
        self.active_peers.pop(peer_key, None)

    async def _main_loop(self) -> None:
        """Main swarm maintenance loop: announces to trackers and maintains peer connections."""
        last_announce: float = 0.0
        announce_interval: float = 30.0  # Initial quick interval

        while self._running:
            try:
                now = time.time()
                # 1. Announce to tracker if interval expired
                if now - last_announce >= announce_interval:
                    left = max(0, self.torrent.total_length - self.piece_manager.bytes_downloaded)
                    event = "completed" if self.piece_manager.is_complete else ("started" if last_announce == 0 else "")
                    
                    try:
                        response: TrackerResponse = await self.tracker_manager.announce(
                            info_hash=self.torrent.info_hash,
                            uploaded=self.piece_manager.bytes_uploaded,
                            downloaded=self.piece_manager.bytes_downloaded,
                            left=left,
                            event=event,
                        )
                        last_announce = now
                        if response and response.peers:
                            announce_interval = max(60.0, float(response.interval))
                            for p in response.peers:
                                p_key = f"{p.ip}:{p.port}"
                                if p_key not in self.seen_peers:
                                    self.seen_peers.add(p_key)
                                    self.peer_queue.put_nowait(p)
                    except Exception as e:
                        logger.debug(f"Tracker announce failed: {e}")
                        announce_interval = 60.0

                # 2. Replenish peer connections if below max_peers
                while len(self.active_peers) < self.max_peers and not self.peer_queue.empty():
                    peer = self.peer_queue.get_nowait()
                    peer_key = f"{peer.ip}:{peer.port}"
                    if peer_key not in self.active_peers:
                        conn = PeerConnection(
                            peer=peer,
                            info_hash=self.torrent.info_hash,
                            peer_id=self.tracker_manager.peer_id,
                            piece_manager=self.piece_manager,
                            on_disconnect=self._on_peer_disconnected,
                        )
                        self.active_peers[peer_key] = conn
                        conn.start()

                # 3. Check completion status
                if self.piece_manager.is_complete:
                    self.status = TorrentStatus.COMPLETED
                    self.download_speed = 0.0
                    self.eta_seconds = 0

                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TorrentSession main loop: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _speed_meter_loop(self) -> None:
        """Calculates download speed and ETA every second."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
                now = time.time()
                elapsed = now - self._last_speed_check
                if elapsed <= 0:
                    continue

                curr_downloaded = self.piece_manager.bytes_downloaded
                delta = curr_downloaded - self._last_downloaded_bytes
                self.download_speed = max(0.0, delta / elapsed)

                self._last_downloaded_bytes = curr_downloaded
                self._last_speed_check = now

                # Update speed on individual peer connections
                for conn in self.active_peers.values():
                    conn.update_speed()

                # Calculate ETA
                bytes_left = max(0, self.torrent.total_length - curr_downloaded)
                if self.download_speed > 0 and bytes_left > 0:
                    self.eta_seconds = int(bytes_left / self.download_speed)
                else:
                    self.eta_seconds = 0 if bytes_left == 0 else None

            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def to_dict(self) -> dict:
        """Returns JSON-serializable status snapshot of the torrent."""
        connected_count = sum(1 for c in self.active_peers.values() if c.is_connected)
        return {
            "info_hash": self.torrent.info_hash_hex,
            "name": self.torrent.name,
            "status": self.status.value,
            "total_size": self.torrent.total_length,
            "downloaded": self.piece_manager.bytes_downloaded,
            "uploaded": self.piece_manager.bytes_uploaded,
            "progress": round(self.piece_manager.progress_percentage, 2),
            "download_speed": round(self.download_speed, 2),
            "upload_speed": round(self.upload_speed, 2),
            "eta": self.eta_seconds,
            "peers_connected": connected_count,
            "peers_total": len(self.seen_peers),
            "piece_count": self.torrent.piece_count,
            "pieces_completed": len(self.piece_manager.completed_pieces),
            "piece_length": self.torrent.piece_length,
            "is_multi_file": self.torrent.is_multi_file,
            "files": [
                {"path": f.path, "length": f.length, "offset": f.offset}
                for f in self.torrent.files
            ],
            "trackers": self.torrent.trackers,
        }
