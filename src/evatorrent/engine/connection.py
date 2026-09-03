"""Peer connection worker managing TCP socket, handshake, and message exchange."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional, Set, Tuple

from evatorrent.peer.protocol import (
    Bitfield,
    Choke,
    Handshake,
    Have,
    Interested,
    KeepAlive,
    NotInterested,
    Piece,
    Request,
    Unchoke,
)
from evatorrent.peer.stream import PeerStreamIterator
from evatorrent.storage.manager import PieceManager
from evatorrent.tracker import Peer

logger = logging.getLogger(__name__)

PIPELINE_CAPACITY = 4  # Number of concurrent in-flight block requests per peer


class PeerConnection:
    """Manages an active peer connection in the swarm with request pipelining."""

    def __init__(
        self,
        peer: Peer,
        info_hash: bytes,
        peer_id: bytes,
        piece_manager: PieceManager,
        on_disconnect: Optional[Callable[[str], None]] = None,
        is_throttled: Optional[Callable[[], bool]] = None,
    ):
        self.peer = peer
        self.peer_key = f"{peer.ip}:{peer.port}"
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.piece_manager = piece_manager
        self.on_disconnect = on_disconnect
        self.is_throttled = is_throttled

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

        self.remote_peer_id: Optional[bytes] = None
        self.is_choked: bool = True  # Remote peer is choking us
        self.am_interested: bool = False
        self.is_connected: bool = False
        self.running: bool = False

        # Speed tracking
        self.bytes_downloaded: int = 0
        self.download_speed: float = 0.0  # Bytes/sec
        self._last_speed_check: float = time.time()
        self._bytes_since_last_check: int = 0

        self._task: Optional[asyncio.Task] = None
        # In-flight block requests: set of (piece_index, begin_offset)
        self._pending_requests: Set[Tuple[int, int]] = set()

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        await self._close()

    async def _close(self) -> None:
        self.is_connected = False
        self.piece_manager.remove_peer(self.peer_key)
        self._pending_requests.clear()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None
        if self.on_disconnect:
            self.on_disconnect(self.peer_key)

    async def send_message(self, message) -> None:
        if not self.writer or self.writer.is_closing():
            return
        try:
            self.writer.write(message.encode())
            await self.writer.drain()
        except Exception as e:
            logger.debug(f"Failed to send message to {self.peer_key}: {e}")
            await self._close()

    async def _run(self) -> None:
        try:
            # 1. Connect TCP socket with timeout
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.peer.ip, self.peer.port),
                timeout=8.0,
            )

            # 2. Send Handshake
            handshake = Handshake(info_hash=self.info_hash, peer_id=self.peer_id)
            self.writer.write(handshake.encode())
            await self.writer.drain()

            # 3. Read remote Handshake
            raw_hs = await asyncio.wait_for(self.reader.readexactly(68), timeout=8.0)
            remote_hs = Handshake.decode(raw_hs)
            if remote_hs.info_hash != self.info_hash:
                logger.debug(f"Info hash mismatch from {self.peer_key}")
                await self._close()
                return

            self.remote_peer_id = remote_hs.peer_id
            self.is_connected = True

            # 4. If we have pieces, send Bitfield
            our_bitfield = self.piece_manager.get_bitfield()
            if any(b != 0 for b in our_bitfield.data):
                await self.send_message(our_bitfield)

            # 5. Send Interested
            await self.send_message(Interested())
            self.am_interested = True

            # 6. Stream incoming messages and run request loop
            stream = PeerStreamIterator(self.reader)
            request_loop_task = asyncio.create_task(self._request_blocks_loop())

            try:
                async for msg in stream:
                    if not self.running or self.piece_manager.is_complete:
                        break
                    await self._handle_message(msg)
            finally:
                request_loop_task.cancel()

        except (asyncio.TimeoutError, TimeoutError):
            logger.debug(f"Connection timeout to {self.peer_key}")
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            logger.debug(f"Socket connection error to {self.peer_key}: {e}")
        except Exception as e:
            logger.debug(f"PeerConnection exception with {self.peer_key}: {e}")
        finally:
            await self._close()

    async def _handle_message(self, msg) -> None:
        if isinstance(msg, Choke):
            self.is_choked = True
        elif isinstance(msg, Unchoke):
            self.is_choked = False
        elif isinstance(msg, Interested):
            pass
        elif isinstance(msg, NotInterested):
            pass
        elif isinstance(msg, Have):
            self.piece_manager.peer_has_piece(self.peer_key, msg.piece_index)
        elif isinstance(msg, Bitfield):
            self.piece_manager.add_peer(self.peer_key, msg)
        elif isinstance(msg, Piece):
            self.bytes_downloaded += len(msg.block)
            self._bytes_since_last_check += len(msg.block)
            # Remove from in-flight requests set
            self._pending_requests.discard((msg.index, msg.begin))
            self.piece_manager.on_block_received(msg.index, msg.begin, msg.block)
        elif isinstance(msg, KeepAlive):
            pass

    async def _request_blocks_loop(self) -> None:
        """Pipelined loop requesting missing blocks in parallel while unchoked."""
        while self.running and not self.piece_manager.is_complete:
            if not self.is_connected or self.is_choked:
                await asyncio.sleep(0.1)
                continue

            # Check rate limiting
            if self.is_throttled and self.is_throttled():
                await asyncio.sleep(0.05)
                continue

            available_slots = PIPELINE_CAPACITY - len(self._pending_requests)
            if available_slots <= 0:
                await asyncio.sleep(0.03)
                continue

            blocks = self.piece_manager.next_requests(self.peer_key, max_count=available_slots)
            if blocks:
                for block in blocks:
                    req_key = (block.piece_index, block.begin)
                    self._pending_requests.add(req_key)
                    req = Request(index=block.piece_index, begin=block.begin, length=block.length)
                    await self.send_message(req)
            else:
                await asyncio.sleep(0.15)

    def update_speed(self) -> None:
        """Calculates current download speed over the elapsed time interval."""
        now = time.time()
        elapsed = now - self._last_speed_check
        if elapsed >= 1.0:
            self.download_speed = self._bytes_since_last_check / elapsed
            self._bytes_since_last_check = 0
            self._last_speed_check = now
