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

# Stall timeout: if download is active but no data received for 5 minutes, transition to ERROR
STALL_TIMEOUT_SECONDS = 300.0


from evatorrent.db.database import Database


class TorrentStatus(str, Enum):
    PENDING = "pending"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class TorrentSession:
    """Manages the download lifecycle of a single torrent."""

    def __init__(
        self,
        torrent: Torrent,
        download_dir: Path,
        max_peers: int = 35,
        port: int = 6881,
        download_limit: Optional[int] = None,  # Bytes/sec, None or 0 for unlimited
        db: Optional[Database] = None,
    ):
        self.torrent = torrent
        self.download_dir = Path(download_dir)
        self.max_peers = max_peers
        self.port = port
        self.download_limit = download_limit
        self.db = db

        self.piece_manager = PieceManager(
            torrent=torrent,
            download_dir=self.download_dir,
            on_piece_complete=self._on_piece_completed,
        )
        self.tracker_manager = TrackerManager(torrent.trackers, port=port, add_fallbacks=True)

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
        self._last_uploaded_bytes: int = 0
        self._last_data_received_time: float = time.time()

        # Check existing files on disk
        self.piece_manager.check_existing_files()
        if self.piece_manager.is_complete:
            self.status = TorrentStatus.COMPLETED

    def touch_activity(self) -> None:
        """Records data activity from swarm to prevent stall timeout."""
        self._last_data_received_time = time.time()

    def is_throttled(self) -> bool:
        """Returns True if the current download speed exceeds the configured per-torrent limit."""
        if not self.download_limit or self.download_limit <= 0:
            return False
        return self.download_speed >= self.download_limit

    def set_download_limit(self, limit_bytes_per_sec: Optional[int]) -> None:
        """Sets or clears the download rate limit for this torrent."""
        self.download_limit = limit_bytes_per_sec if (limit_bytes_per_sec and limit_bytes_per_sec > 0) else None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.status = TorrentStatus.DOWNLOADING
        self.error_message = None
        self._last_data_received_time = time.time()
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
        if self.status == TorrentStatus.COMPLETED:
            # If downloaded files were deleted from disk, re-verify and resume download
            files_exist = any(
                (self.download_dir / f.path).exists() or (self.download_dir / f"{f.path}.part").exists()
                for f in self.torrent.files
            )
            if files_exist and self.piece_manager.is_complete:
                return
            logger.info(f"Torrent '{self.torrent.name}' files were removed from disk. Re-downloading...")
            self.piece_manager.check_existing_files()
            self.status = TorrentStatus.DOWNLOADING

        self.error_message = None
        self._last_data_received_time = time.time()
        self.start()

    async def stop(self) -> None:
        self._running = False
        if self.piece_manager.is_complete:
            self.status = TorrentStatus.COMPLETED
        elif self.status != TorrentStatus.ERROR:
            self.status = TorrentStatus.PAUSED

        for peer_conn in list(self.active_peers.values()):
            await peer_conn.stop()
        self.active_peers.clear()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
        if self._speed_task and not self._speed_task.done():
            self._speed_task.cancel()

    async def _stop_seeding_and_complete(self) -> None:
        """Stops peer connections immediately once download is complete - no seeding."""
        self.status = TorrentStatus.COMPLETED
        self._running = False
        logger.info(f"Torrent '{self.torrent.name}' fully downloaded. Stopping peer connections (no seeding).")

        if self.db:
            self.db.mark_torrent_completed(
                self.torrent.info_hash_hex,
                self.piece_manager.bytes_downloaded,
                self.piece_manager.bytes_uploaded,
            )

        # Notify tracker of completion
        try:
            await self.tracker_manager.announce(
                info_hash=self.torrent.info_hash,
                uploaded=self.piece_manager.bytes_uploaded,
                downloaded=self.piece_manager.bytes_downloaded,
                left=0,
                event="completed",
            )
        except Exception:
            pass

        # Disconnect all peers to prevent seeding
        for peer_conn in list(self.active_peers.values()):
            await peer_conn.stop()
        self.active_peers.clear()

        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
        if self._speed_task and not self._speed_task.done():
            self._speed_task.cancel()

    def _on_piece_completed(self, piece_index: int) -> None:
        """Broadcasts Have message to peers upon piece verification, or triggers completion."""
        self._last_data_received_time = time.time()
        have_msg = Have(piece_index=piece_index)
        for peer_conn in self.active_peers.values():
            if peer_conn.is_connected:
                asyncio.create_task(peer_conn.send_message(have_msg))

        if self.piece_manager.is_complete:
            asyncio.create_task(self._stop_seeding_and_complete())

    def _on_peer_disconnected(self, peer_key: str) -> None:
        self.active_peers.pop(peer_key, None)

    async def _main_loop(self) -> None:
        """Main swarm maintenance loop: announces to trackers and maintains peer connections."""
        last_announce: float = 0.0
        announce_interval: float = 30.0

        while self._running:
            try:
                now = time.time()

                # Check if download stalled for over STALL_TIMEOUT_SECONDS
                if not self.piece_manager.is_complete and self.status == TorrentStatus.DOWNLOADING:
                    if now - self._last_data_received_time > STALL_TIMEOUT_SECONDS:
                        logger.warning(
                            f"Torrent '{self.torrent.name}' stalled for >{int(STALL_TIMEOUT_SECONDS)}s. Marking errored."
                        )
                        self.status = TorrentStatus.ERROR
                        self.error_message = f"Download stalled: no data received for {int(STALL_TIMEOUT_SECONDS / 60)} minutes"
                        if self.db:
                            self.db.mark_torrent_error(self.torrent.info_hash_hex, self.error_message)
                        await self.stop()
                        break

                # 1. Announce to tracker if interval expired
                if now - last_announce >= announce_interval:
                    left = max(0, self.torrent.total_length - self.piece_manager.bytes_downloaded)
                    event = "started" if last_announce == 0 else ""

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
                            for p in response.peers:
                                p_key = f"{p.ip}:{p.port}"
                                if p_key not in self.seen_peers:
                                    self.seen_peers.add(p_key)
                                    self.peer_queue.put_nowait(p)
                            if len(self.active_peers) < 5 and self.peer_queue.qsize() < 10:
                                announce_interval = 60.0
                            else:
                                announce_interval = max(120.0, float(response.interval))
                        else:
                            announce_interval = 60.0
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
                            is_throttled=self.is_throttled,
                            on_data_received=self.touch_activity,
                        )
                        self.active_peers[peer_key] = conn
                        conn.start()

                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TorrentSession main loop: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _speed_meter_loop(self) -> None:
        """Calculates download speed, upload speed, and ETA every second."""
        tick = 0
        while self._running:
            try:
                await asyncio.sleep(1.0)
                tick += 1
                now = time.time()
                elapsed = now - self._last_speed_check
                if elapsed <= 0:
                    continue

                curr_downloaded = self.piece_manager.bytes_downloaded
                curr_uploaded = self.piece_manager.bytes_uploaded
                delta_dl = curr_downloaded - self._last_downloaded_bytes
                delta_ul = curr_uploaded - self._last_uploaded_bytes
                if delta_dl > 0:
                    self._last_data_received_time = now

                self.download_speed = max(0.0, delta_dl / elapsed)
                self.upload_speed = max(0.0, delta_ul / elapsed)
                self._last_downloaded_bytes = curr_downloaded
                self._last_uploaded_bytes = curr_uploaded
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

                # Persist progress to DB every 5 seconds
                if self.db and tick % 5 == 0:
                    self.db.update_torrent_progress(
                        self.torrent.info_hash_hex,
                        curr_downloaded,
                        curr_uploaded,
                        self.status.value,
                    )

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
            "error_message": self.error_message,
            "total_size": self.torrent.total_length,
            "downloaded": self.piece_manager.bytes_downloaded,
            "uploaded": self.piece_manager.bytes_uploaded,
            "progress": round(self.piece_manager.progress_percentage, 2),
            "download_speed": round(self.download_speed, 2),
            "upload_speed": round(self.upload_speed, 2),
            "download_limit": self.download_limit,
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
