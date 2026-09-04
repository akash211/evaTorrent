"""PieceManager tracking piece state, block scheduling, hash verification, and persistence."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from evatorrent.peer.protocol import Bitfield
from evatorrent.storage.disk import DiskWriter
from evatorrent.storage.piece import Block, Piece
from evatorrent.torrent import Torrent

logger = logging.getLogger(__name__)

BLOCK_TIMEOUT = 12.0  # seconds before in-flight block request can be reassigned


class PieceManager:
    """Coordinates parallel block requests, piece validation, and disk saving."""

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

        # Peer availability mapping: peer_key (str) -> Set of piece indices
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

    def peer_has_all_pieces(self, peer_key: str) -> None:
        """Marks that a peer (e.g. an unchoking seeder) possesses all pieces."""
        self.peers[peer_key] = set(range(len(self.pieces)))

    def check_existing_files(self) -> int:
        """Verifies files on disk against torrent piece hashes to restore completed pieces or detect deletion."""
        any_file_exists = any(
            (self.disk_writer.output_dir / f.path).exists() or (self.disk_writer.output_dir / f"{f.path}.part").exists()
            for f in self.torrent.files
        )
        if not any_file_exists:
            self.completed_pieces.clear()
            self.ongoing_pieces.clear()
            self.missing_pieces = set(range(self.torrent.piece_count))
            for p in self.pieces:
                p.reset()
            self.bytes_downloaded = 0
            return 0

        verified = 0
        self.completed_pieces.clear()
        self.ongoing_pieces.clear()
        self.missing_pieces = set(range(self.torrent.piece_count))
        for p in self.pieces:
            p.reset()

        for idx, piece in enumerate(self.pieces):
            try:
                data = self.disk_writer.read_piece(idx)
                if len(data) == piece.length and hashlib.sha1(data).digest() == piece.expected_hash:
                    self.completed_pieces.add(idx)
                    self.missing_pieces.discard(idx)
                    verified += 1
            except Exception:
                pass

        self.bytes_downloaded = sum(self.torrent.piece_size(i) for i in self.completed_pieces)
        return verified

    def read_block(self, piece_index: int, begin: int, length: int) -> Optional[bytes]:
        """Reads block from verified piece on disk for serving to peers (seeding)."""
        if piece_index not in self.completed_pieces:
            return None
        if piece_index < 0 or piece_index >= len(self.pieces):
            return None
        piece = self.pieces[piece_index]
        if begin < 0 or begin + length > piece.length:
            return None

        try:
            full_piece = self.disk_writer.read_piece(piece_index)
            block_data = full_piece[begin : begin + length]
            self.bytes_uploaded += len(block_data)
            return block_data
        except Exception as e:
            logger.debug(f"Failed to read block for piece {piece_index}: {e}")
            return None

    def next_requests(self, peer_key: str, max_count: int = 4) -> List[Block]:
        """Pipelined block selector: returns up to max_count blocks to request from this peer."""
        peer_pieces = self.peers.get(peer_key)
        if not peer_pieces:
            return []

        now = time.time()
        blocks_to_request: List[Block] = []

        # 1. First priority: timed-out blocks in ongoing pieces this peer has
        for piece_idx in list(self.ongoing_pieces):
            if piece_idx in peer_pieces:
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete:
                        if block.requested_time > 0.0 and (now - block.requested_time > BLOCK_TIMEOUT):
                            block.mark_requested()
                            blocks_to_request.append(block)
                            if len(blocks_to_request) >= max_count:
                                return blocks_to_request

        # 2. Second priority: unrequested blocks in ongoing pieces
        for piece_idx in list(self.ongoing_pieces):
            if piece_idx in peer_pieces:
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete and block.requested_time == 0.0:
                        block.mark_requested()
                        blocks_to_request.append(block)
                        if len(blocks_to_request) >= max_count:
                            return blocks_to_request

        # 3. Third priority: start new missing pieces that this peer has (rarest-first)
        # Sort by availability (fewest peers → most rare → download first)
        def _availability(idx: int) -> int:
            return sum(1 for avail in self.peers.values() if idx in avail)

        rarest_missing = sorted(
            (idx for idx in self.missing_pieces if idx in peer_pieces and idx not in self.ongoing_pieces),
            key=_availability,
        )
        for piece_idx in rarest_missing:
                self.missing_pieces.remove(piece_idx)
                self.ongoing_pieces.add(piece_idx)
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete and block.requested_time == 0.0:
                        block.mark_requested()
                        blocks_to_request.append(block)
                        if len(blocks_to_request) >= max_count:
                            return blocks_to_request
                if len(blocks_to_request) >= max_count:
                    break

        return blocks_to_request

    def next_request(self, peer_key: str) -> Optional[Block]:
        """Single block request helper."""
        reqs = self.next_requests(peer_key, max_count=1)
        return reqs[0] if reqs else None

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

                # If all pieces are complete, finalize files (remove .part extensions)
                if self.is_complete:
                    self.disk_writer.finalize()

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
