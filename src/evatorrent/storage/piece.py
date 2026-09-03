"""Block and Piece models for managing data chunking and hash verification."""

from __future__ import annotations

import hashlib
import math
import time
from typing import List, Optional

BLOCK_SIZE = 16384  # 16 KiB


class Block:
    """Represents a single requestable block within a piece."""

    def __init__(self, piece_index: int, begin: int, length: int):
        self.piece_index = piece_index
        self.begin = begin
        self.length = length
        self.data: Optional[bytes] = None
        self.requested_time: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.data is not None

    def mark_requested(self) -> None:
        self.requested_time = time.time()

    def reset(self) -> None:
        self.data = None
        self.requested_time = 0.0


class Piece:
    """Represents a piece of the torrent consisting of multiple blocks."""

    def __init__(self, index: int, length: int, expected_hash: bytes):
        self.index = index
        self.length = length
        self.expected_hash = expected_hash
        self.blocks: List[Block] = []
        self._init_blocks()

    def _init_blocks(self) -> None:
        num_blocks = math.ceil(self.length / BLOCK_SIZE)
        for i in range(num_blocks):
            begin = i * BLOCK_SIZE
            length = min(BLOCK_SIZE, self.length - begin)
            self.blocks.append(Block(self.index, begin, length))

    @property
    def is_complete(self) -> bool:
        return all(b.is_complete for b in self.blocks)

    def set_block_data(self, begin: int, data: bytes) -> bool:
        for block in self.blocks:
            if block.begin == begin and block.length == len(data):
                block.data = data
                return True
        return False

    def verify_hash(self) -> bool:
        """Validates the concatenated blocks against the expected SHA-1 hash."""
        if not self.is_complete:
            return False
        piece_data = self.get_data()
        actual_hash = hashlib.sha1(piece_data).digest()
        return actual_hash == self.expected_hash

    def get_data(self) -> bytes:
        return b"".join(b.data for b in self.blocks if b.data is not None)

    def reset(self) -> None:
        for block in self.blocks:
            block.reset()
