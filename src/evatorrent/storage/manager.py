"""PieceManager tracking piece state, block scheduling, hash verification, and persistence."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from evatorrent.peer.protocol import Bitfield
from evatorrent.storage.disk import DiskWriter
from evatorrent.storage.piece import Block, Piece
from evatorrent.torrent import Torrent

logger = logging.getLogger(__name__)

BLOCK_TIMEOUT = 15.0  # seconds before in-flight block request can be reassigned


class PieceManager:
    """Coordinates block requests, piece validation, and disk saving."""

    def __init__(
        self,
        torrent: Torrent,
        download_dir: Path,
        on_piece_complete: Optional[Callable[[int], None]] = None,
    ):
        self.torrent = torrent
        self.disk_writer = DiskWriter(torrent, download_dir)
        self.on_piece_complete = on_piece_complete

        # Instantiate Piece objects
        self.pieces: List[Piece] = [
            Piece(
                index=i,
                length=torrent.piece_size(i),
                expected_hash=torrent.piece_hashes[i],
            )
            for i in range(torrent.piece_count)
        ]

        self.missing_pieces: Set[int] = set(range(torrent.piece_count))
        self.ongoing_pieces: Set[int] = set()
        self.completed_pieces: Set[int] = set()

        # Peer availability mapping: peer_key (str) -> Bitfield or Set of piece indices
        self.peers: Dict[str, Set[int]] = {}

        self.bytes_downloaded: int = 0
        self.bytes_uploaded: int = 0

    @property
    def is_complete(self) -> bool:
        return len(self.completed_pieces) == self.torrent.piece_count

    @property
    def progress_percentage(self) -> float:
        if self.torrent.piece_count == 0:
            return 100.0
        return (len(self.completed_pieces) / self.torrent.piece_count) * 100.0

    def add_peer(self, peer_key: str, bitfield: Bitfield) -> None:
        """Records the pieces advertised by a peer via Bitfield."""
        pieces_set: Set[int] = set()
        for idx in range(self.torrent.piece_count):
            if bitfield.has_piece(idx):
                pieces_set.add(idx)
        self.peers[peer_key] = pieces_set

    def peer_has_piece(self, peer_key: str, piece_index: int) -> None:
        """Records an individual piece advertised by a peer via Have message."""
        if peer_key not in self.peers:
            self.peers[peer_key] = set()
        self.peers[peer_key].add(piece_index)

    def remove_peer(self, peer_key: str) -> None:
        self.peers.pop(peer_key, None)

    def get_bitfield(self) -> Bitfield:
        """Returns our current bitfield representing completed pieces."""
        num_bytes = (self.torrent.piece_count + 7) // 8
        buf = bytearray(num_bytes)
        for idx in self.completed_pieces:
            byte_idx = idx // 8
            bit_idx = 7 - (idx % 8)
            buf[byte_idx] |= (1 << bit_idx)
        return Bitfield(bytes(buf))

    def next_request(self, peer_key: str) -> Optional[Block]:
        """Selects the next block to request from a peer."""
        peer_pieces = self.peers.get(peer_key)
        if not peer_pieces:
            return None

        now = time.time()

        # 1. First priority: timed-out blocks in ongoing pieces this peer has
        for piece_idx in list(self.ongoing_pieces):
            if piece_idx in peer_pieces:
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete:
                        if block.requested_time == 0.0 or (now - block.requested_time > BLOCK_TIMEOUT):
                            block.mark_requested()
                            return block

        # 2. Second priority: next unrequested block in ongoing pieces
        for piece_idx in list(self.ongoing_pieces):
            if piece_idx in peer_pieces:
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete and block.requested_time == 0.0:
                        block.mark_requested()
                        return block

        # 3. Third priority: start a new missing piece that the peer has
        # (Sequential selection among available missing pieces)
        for piece_idx in sorted(self.missing_pieces):
            if piece_idx in peer_pieces and piece_idx not in self.ongoing_pieces:
                self.missing_pieces.remove(piece_idx)
                self.ongoing_pieces.add(piece_idx)
                piece = self.pieces[piece_idx]
                block = piece.blocks[0]
                block.mark_requested()
                return block

        return None

    def on_block_received(self, index: int, begin: int, data: bytes) -> bool:
        """Processes received block data and triggers piece verification if complete."""
        if index < 0 or index >= len(self.pieces):
            return False

        piece = self.pieces[index]
        if piece.index in self.completed_pieces:
            return False  # Already complete

        success = piece.set_block_data(begin, data)
        if not success:
            return False

        self.bytes_downloaded += len(data)

        if piece.is_complete:
            if piece.verify_hash():
                # Write to disk
                self.disk_writer.write_piece(index, piece.get_data())
                if index in self.ongoing_pieces:
                    self.ongoing_pieces.remove(index)
                if index in self.missing_pieces:
                    self.missing_pieces.remove(index)
                self.completed_pieces.add(index)
                logger.info(
                    f"Piece {index}/{self.torrent.piece_count} verified & saved. "
                    f"Progress: {self.progress_percentage:.1f}%"
                )
                if self.on_piece_complete:
                    self.on_piece_complete(index)
                return True
            else:
                logger.warning(f"Hash mismatch on piece {index}! Discarding and retrying.")
                piece.reset()
                if index in self.ongoing_pieces:
                    self.ongoing_pieces.remove(index)
                self.missing_pieces.add(index)
                return False

        return False
