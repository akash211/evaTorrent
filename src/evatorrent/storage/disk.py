"""Disk storage manager for single-file and multi-file torrents.

During download, files are appended with '.part'. Once the torrent download is
complete and verified, '.part' files are atomically renamed to their final filenames.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import List

from evatorrent.torrent import FileInfo, Torrent

logger = logging.getLogger(__name__)


class DiskWriter:
    """Handles writing verified pieces to the file system with .part support."""

    def __init__(self, torrent: Torrent, output_dir: Path):
        self.torrent = torrent
        self.output_dir = Path(output_dir)
        self.files: List[FileInfo] = torrent.files
        self._init_paths()

    def _init_paths(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for f in self.files:
            final_path = self.output_dir / f.path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            part_path = self.output_dir / f"{f.path}.part"

            # Only pre-allocate .part if neither final nor .part already exists
            if not final_path.exists() and not part_path.exists():
                with part_path.open("wb") as fp:
                    pass

    def _get_active_file_path(self, file_info: FileInfo) -> Path:
        """Returns final_path if already finalized, otherwise part_path."""
        final_path = self.output_dir / file_info.path
        if final_path.exists():
            return final_path
        return self.output_dir / f"{file_info.path}.part"

    def write_piece(self, piece_index: int, data: bytes) -> None:
        """Writes data of a verified piece to the appropriate file(s)."""
        piece_offset = piece_index * self.torrent.piece_length
        piece_end = piece_offset + len(data)

        bytes_written = 0
        for f in self.files:
            file_start = f.offset
            file_end = f.offset + f.length

            # Check if this piece overlaps with the current file
            if piece_offset < file_end and piece_end > file_start:
                overlap_start = max(piece_offset, file_start)
                overlap_end = min(piece_end, file_end)

                data_slice_start = overlap_start - piece_offset
                data_slice_end = overlap_end - piece_offset
                slice_to_write = data[data_slice_start:data_slice_end]

                file_seek_pos = overlap_start - file_start
                target_path = self._get_active_file_path(f)
                target_path.parent.mkdir(parents=True, exist_ok=True)

                mode = "r+b" if target_path.exists() else "w+b"
                with target_path.open(mode) as fp:
                    fp.seek(file_seek_pos)
                    fp.write(slice_to_write)
                    fp.flush()

                bytes_written += len(slice_to_write)

        if bytes_written != len(data):
            logger.warning(
                f"Piece {piece_index}: Expected to write {len(data)} bytes, wrote {bytes_written}"
            )

    def finalize(self) -> None:
        """Renames all .part files to their final filenames once download is verified."""
        for f in self.files:
            part_path = self.output_dir / f"{f.path}.part"
            final_path = self.output_dir / f.path
            if part_path.exists():
                final_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomically replace final file
                shutil.move(str(part_path), str(final_path))
                logger.info(f"Finalized '{f.path}' (removed .part extension)")

    def read_piece(self, piece_index: int) -> bytes:
        """Reads a piece from disk (used for hashing/seeding)."""
        piece_size = self.torrent.piece_size(piece_index)
        piece_offset = piece_index * self.torrent.piece_length
        piece_end = piece_offset + piece_size

        buffer = bytearray()
        for f in self.files:
            file_start = f.offset
            file_end = f.offset + f.length

            if piece_offset < file_end and piece_end > file_start:
                overlap_start = max(piece_offset, file_start)
                overlap_end = min(piece_end, file_end)
                length_to_read = overlap_end - overlap_start

                file_seek_pos = overlap_start - file_start
                target_path = self._get_active_file_path(f)

                if target_path.exists():
                    with target_path.open("rb") as fp:
                        fp.seek(file_seek_pos)
                        chunk = fp.read(length_to_read)
                        buffer.extend(chunk)
                else:
                    buffer.extend(b"\x00" * length_to_read)

        return bytes(buffer)
