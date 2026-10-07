"""PieceManager tracking piece state, block scheduling, hash verification, and persistence."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from evatorrent.peer.protocol import Bitfield
from evatorrent.storage.disk import DiskWriter
from evatorrent.storage.piece import Block, Piece
from evatorrent.torrent import Torrent

logger = logging.getLogger(__name__)

BLOCK_TIMEOUT = 12.0  # seconds before in-flight block request can be reassigned
ENDGAME_REMAINING_PIECES = 5  # last-N-pieces endgame: duplicate in-flight requests across peers

# Per-file download priorities (uTorrent-style): 0 = skip, 1 = normal, 2 = high.
PRIO_SKIP = 0
PRIO_NORMAL = 1
PRIO_HIGH = 2
VALID_PRIORITIES = (PRIO_SKIP, PRIO_NORMAL, PRIO_HIGH)


class PieceManager:
    """Coordinates parallel block requests, piece validation, and disk saving."""

    def __init__(
        self,
        torrent: Torrent,
        download_dir: Path,
        on_piece_complete: Optional[Callable[[int], None]] = None,
        selected_files: Optional[List[str]] = None,
        file_priorities: Optional[Dict[str, int]] = None,
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
        # Files already renamed from .part to final (per-file finalize).
        self.finalized_files: Set[str] = set()
        # Pieces excluded by per-file selection (never requested/hashed).
        self.skipped_pieces: Set[int] = set()
        # None = all files selected. Otherwise the set of selected file paths.
        self.selected_files: Optional[Set[str]] = None
        # Per-file priority: path -> 0 skip / 1 normal / 2 high. Missing keys = normal.
        self.file_priorities: Dict[str, int] = {}
        # Piece priority cache: piece idx -> max priority of overlapping files.
        self.piece_priorities: Dict[int, int] = {}
        self.selected_bytes: int = torrent.total_length

        # Peer availability mapping: peer_key -> Set of piece indices,
        # or None meaning "seeder has everything" (avoids duplicating a
        # 155 GB / 100k-piece set per seeder -> 100s of MB on small hosts).
        self.peers: Dict[str, Optional[Set[int]]] = {}

        # Cached rarest-first availability counts (recomputed at most every
        # RAREST_REFRESH_SECS to avoid O(P*N) sorts on every request loop tick).
        self._rarity_counts: Dict[int, int] = {}
        self._rarity_computed_at: float = 0.0

        self.bytes_downloaded: int = 0
        self.bytes_uploaded: int = 0

        if selected_files is not None:
            self._apply_selection(set(selected_files))
        if file_priorities:
            self._apply_priorities(dict(file_priorities))

    @property
    def is_complete(self) -> bool:
        # Selected-only completion: skipped (deselected) pieces don't block it.
        return not self.missing_pieces and not self.ongoing_pieces

    @property
    def progress_percentage(self) -> float:
        selected_total = self.torrent.piece_count - len(self.skipped_pieces)
        if selected_total <= 0:
            return 100.0
        done = len(self.completed_pieces - self.skipped_pieces)
        return (done / selected_total) * 100.0

    @property
    def selected_total_bytes(self) -> int:
        return self.selected_bytes

    def _pieces_overlapping_files(self, paths: Set[str]) -> Set[int]:
        """Returns piece indices overlapping any of the given torrent file paths."""
        wanted = set(paths)
        out: Set[int] = set()
        for f in self.torrent.files:
            if f.path not in wanted:
                continue
            first = f.offset // self.torrent.piece_length
            last = (f.offset + max(f.length, 1) - 1) // self.torrent.piece_length
            out.update(range(first, min(last, self.torrent.piece_count - 1) + 1))
        return out

    def _file_is_complete(self, path: str) -> bool:
        """True when every piece overlapping this file is verified."""
        if path in self.finalized_files:
            return True
        for idx in self._pieces_overlapping_files({path}):
            if idx not in self.completed_pieces:
                return False
        return True

    def _maybe_finalize_files(self) -> None:
        """Renames .part -> final for each fully-verified selected file.

        Files appear as .mkv in Filebrowser the moment their own pieces are
        done, instead of waiting for the whole torrent.
        """
        for f in self.torrent.files:
            if f.path in self.finalized_files:
                continue
            if not self.is_file_selected(f.path):
                continue
            if self._file_is_complete(f.path):
                try:
                    if self.disk_writer.finalize_file(f.path):
                        self.finalized_files.add(f.path)
                except Exception as e:
                    logger.debug(f"Finalize of '{f.path}' deferred: {e}")

    def is_file_selected(self, path: str) -> bool:
        return self.selected_files is None or path in self.selected_files

    def get_file_priority(self, path: str) -> int:
        """0=skip, 1=normal, 2=high (default normal)."""
        return self.file_priorities.get(path, PRIO_NORMAL)

    def piece_priority(self, piece_idx: int) -> int:
        """Max priority of files overlapping this piece (skip pieces -> 0)."""
        if piece_idx in self.skipped_pieces:
            return PRIO_SKIP
        return self.piece_priorities.get(piece_idx, PRIO_NORMAL)

    def _recompute_selected_bytes(self) -> None:
        if self.selected_files is None:
            self.selected_bytes = self.torrent.total_length
            return
        skip = self.skipped_pieces
        total = 0
        for i in range(self.torrent.piece_count):
            if i not in skip:
                total += self.torrent.piece_size(i)
        self.selected_bytes = total

    def _apply_priorities(self, prio: Dict[str, int]) -> None:
        """Recomputes skipped pieces + piece priorities (internal).

        Pieces fully covered by priority-0 files move to skipped_pieces;
        in-flight ones are reset (with byte accounting) so they can be
        re-requested if re-selected later.
        """
        all_paths = {f.path for f in self.torrent.files}
        unknown = set(prio) - all_paths
        if unknown:
            raise ValueError(f"Unknown files in selection: {sorted(unknown)[:5]}")
        full = {path: PRIO_NORMAL for path in all_paths}
        for path, p in prio.items():
            if p not in VALID_PRIORITIES:
                raise ValueError(f"Invalid priority {p!r} for '{path}': use 0=skip, 1=normal, 2=high")
            full[path] = p
        selected = {path for path, p in full.items() if p > PRIO_SKIP}
        if not selected:
            raise ValueError("At least one file must stay selected (priority > 0).")
        keep_pieces = self._pieces_overlapping_files(selected)
        new_skipped = set(range(self.torrent.piece_count)) - keep_pieces
        # Piece priority = max priority of overlapping files.
        piece_prio: Dict[int, int] = {}
        for f in self.torrent.files:
            p = full[f.path]
            if p == PRIO_SKIP:
                continue
            first = f.offset // self.torrent.piece_length
            last = (f.offset + max(f.length, 1) - 1) // self.torrent.piece_length
            for idx in range(first, min(last, self.torrent.piece_count - 1) + 1):
                if p > piece_prio.get(idx, PRIO_SKIP):
                    piece_prio[idx] = p
        # Reset in-flight pieces that are now fully skipped (refund bytes so a
        # later re-select doesn't double-count them on re-download).
        for piece_idx in list(self.ongoing_pieces):
            if piece_idx in new_skipped:
                piece = self.pieces[piece_idx]
                refunded = sum(b.length for b in piece.blocks if b.is_complete)
                self.bytes_downloaded = max(0, self.bytes_downloaded - refunded)
                piece.reset()
                self.ongoing_pieces.discard(piece_idx)
        self.missing_pieces -= new_skipped
        self.skipped_pieces = new_skipped
        # Completed pieces stay completed (their bytes are on disk); a piece
        # completed before deselect keeps its data only until release below.
        for piece_idx in list(self.completed_pieces):
            if piece_idx in new_skipped:
                self.pieces[piece_idx].reset()
        self.file_priorities = {path: p for path, p in full.items() if p != PRIO_NORMAL}
        self.selected_files = None if len(selected) == len(all_paths) else set(selected)
        self.piece_priorities = piece_prio
        self._recompute_selected_bytes()
        self._rarity_computed_at = 0.0
        try:
            self._maybe_finalize_files()
        except Exception:
            pass

    def _apply_selection(self, selected: Set[str]) -> None:
        """Legacy include/exclude path (all selected files get normal priority)."""
        all_paths = {f.path for f in self.torrent.files}
        self._apply_priorities({path: (PRIO_NORMAL if path in selected else PRIO_SKIP) for path in all_paths})

    def set_selected_files(self, selected_paths: Optional[List[str]]) -> dict:
        """Legacy include/exclude update (all selected files get normal priority)."""
        all_paths = [f.path for f in self.torrent.files]
        if selected_paths is None or set(selected_paths) == set(all_paths):
            return self.set_file_priorities({})
        return self.set_file_priorities(
            {path: (PRIO_NORMAL if path in set(selected_paths) else PRIO_SKIP) for path in all_paths}
        )

    def set_file_priorities(self, priorities: Dict[str, int]) -> dict:
        """Sets per-file priorities {path: 0 skip | 1 normal | 2 high}.

        Empty dict = all normal. High-priority files' pieces are requested
        first, so episode 1 finishes before episode 2 starts.
        """
        all_paths = [f.path for f in self.torrent.files]
        if not priorities:
            self.selected_files = None
            self.file_priorities = {}
            self.piece_priorities = {}
            self.skipped_pieces.clear()
            self.missing_pieces |= set(range(self.torrent.piece_count)) - self.completed_pieces - self.ongoing_pieces
            self._recompute_selected_bytes()
            self._rarity_computed_at = 0.0
            return {
                "selected_count": len(all_paths),
                "total_count": len(all_paths),
                "high_priority_count": 0,
                "skipped_pieces": 0,
            }
        before = len(self.skipped_pieces)
        self._apply_priorities(dict(priorities))
        if not self.skipped_pieces and all(p == PRIO_NORMAL for p in self.file_priorities.values()):
            self.selected_files = None
        return {
            "selected_count": len(all_paths) - sum(1 for f in all_paths if self.get_file_priority(f) == PRIO_SKIP),
            "total_count": len(all_paths),
            "high_priority_count": sum(1 for f in all_paths if self.get_file_priority(f) == PRIO_HIGH),
            "skipped_pieces": len(self.skipped_pieces),
            "skipped_delta": len(self.skipped_pieces) - before,
        }

    def file_progress(self) -> List[dict]:
        """Per-file progress (uTorrent-style): byte-exact done/total from completed pieces."""
        out: List[dict] = []
        for f in self.torrent.files:
            if f.length <= 0:
                out.append({"path": f.path, "done_bytes": 0, "total_bytes": 0, "progress": 100.0})
                continue
            first = f.offset // self.torrent.piece_length
            last = (f.offset + f.length - 1) // self.torrent.piece_length
            done = 0
            for idx in range(first, min(last, self.torrent.piece_count - 1) + 1):
                if idx in self.completed_pieces:
                    p_start = idx * self.torrent.piece_length
                    p_end = p_start + self.torrent.piece_size(idx)
                    done += max(0, min(p_end, f.offset + f.length) - max(p_start, f.offset))
            out.append(
                {
                    "path": f.path,
                    "done_bytes": done,
                    "total_bytes": f.length,
                    "progress": round(done / f.length * 100.0, 1),
                }
            )
        return out

    def add_peer(self, peer_key: str, bitfield: Bitfield) -> None:
        """Records the pieces advertised by a peer via Bitfield."""
        pieces_set: Set[int] = set()
        for idx in range(self.torrent.piece_count):
            if bitfield.has_piece(idx):
                pieces_set.add(idx)
        self.peers[peer_key] = pieces_set
        self._rarity_computed_at = 0.0  # invalidate cached counts

    def peer_has_piece(self, peer_key: str, piece_index: int) -> None:
        """Records an individual piece advertised by a peer via Have message."""
        avail = self.peers.get(peer_key)
        if avail is None:
            if peer_key in self.peers:
                return  # seeder sentinel: already has everything
            self.peers[peer_key] = {piece_index}
        else:
            avail.add(piece_index)
        # Incremental rarity update; full recompute happens lazily.
        self._rarity_counts[piece_index] = self._rarity_counts.get(piece_index, 0) + 1

    def _peer_has(self, peer_pieces: Optional[Set[int]], piece_idx: int) -> bool:
        """None sentinel means seeder (has all pieces)."""
        if peer_pieces is None:
            return True
        return piece_idx in peer_pieces

    def _refresh_rarity_counts(self) -> None:
        """Recomputes per-piece availability at most every RAREST_REFRESH_SECS."""
        try:
            refresh = float(os.environ.get("EVA_RAREST_REFRESH_SECS", 10.0))
        except (ValueError, TypeError):
            refresh = 10.0
        now = time.time()
        if now - self._rarity_computed_at < refresh and self._rarity_counts:
            return
        counts: Dict[int, int] = {}
        for avail in self.peers.values():
            if avail is None:
                continue  # seeder: every piece +1 would be O(P*N); skip, rarity still relative
            for idx in avail:
                counts[idx] = counts.get(idx, 0) + 1
        # Seeders counted implicitly: treat unlisted pieces as rarer (correct bias).
        self._rarity_counts = counts
        self._rarity_computed_at = now

    def remove_peer(self, peer_key: str) -> None:
        self.peers.pop(peer_key, None)
        self._rarity_computed_at = 0.0

    def get_bitfield(self) -> Bitfield:
        """Returns our current bitfield representing completed pieces."""
        num_bytes = (self.torrent.piece_count + 7) // 8
        buf = bytearray(num_bytes)
        for idx in self.completed_pieces:
            byte_idx = idx // 8
            bit_idx = 7 - (idx % 8)
            buf[byte_idx] |= 1 << bit_idx
        return Bitfield(bytes(buf))

    def peer_has_all_pieces(self, peer_key: str) -> None:
        """Marks that a peer (e.g. an unchoking seeder) possesses all pieces.

        Uses a None sentinel instead of duplicating a 100k-entry set per
        seeder (a 155 GB torrent would otherwise cost 100s of MB with 50 peers).
        """
        self.peers[peer_key] = None
        self._rarity_computed_at = 0.0

    def check_existing_files(self, verify_mode: Optional[str] = None) -> int:
        """Verifies files on disk against torrent piece hashes to restore completed pieces or detect deletion.

        verify_mode: "full" | "quick" | "off" (default from EVA_VERIFY_ON_STARTUP).
        - off: skip hashing entirely (fast boot for huge torrents; pieces
          re-verify as they complete). Marks everything missing.
        - quick (default): full correctness, but skips SHA-1 for sparse/empty
          regions. A fresh 155 GB add creates empty .part files; without this
          fast path boot would SHA-1 155 GB of zeros and peg the CPU.
        - full: legacy behaviour, hashes every piece.
        """
        mode = (verify_mode or os.environ.get("EVA_VERIFY_ON_STARTUP", "quick")).lower()
        if mode == "off":
            logger.warning(
                "[VERIFY] '%s': EVA_VERIFY_ON_STARTUP=off, skipping disk verification "
                "(%d pieces marked missing; progress restarts from 0)",
                self.torrent.name,
                self.torrent.piece_count,
            )
            self.completed_pieces.clear()
            self.ongoing_pieces.clear()
            self.missing_pieces = set(range(self.torrent.piece_count))
            for p in self.pieces:
                p.reset()
            self.bytes_downloaded = 0
            return 0
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

        total = len(self.pieces)
        t_start = time.time()
        # A 155 GB verify reads 155 GB and previously logged nothing until done —
        # if the host died mid-verify the logs showed no trace. Log progress.
        log_progress = total >= 2000
        if self.torrent.piece_count >= 500:
            logger.info(
                "[VERIFY] '%s': verifying %d pieces (%.2f GB, mode=%s)...",
                self.torrent.name,
                total,
                self.torrent.total_length / (1024**3),
                mode,
            )
        next_log_at = 0.1
        for idx, piece in enumerate(self.pieces):
            if idx in self.skipped_pieces:
                continue  # deselected files: never hashed, never downloaded
            try:
                data = self.disk_writer.read_piece(idx)
                if len(data) != piece.length:
                    continue
                if mode != "full" and len(data) > 0 and not any(data):
                    # All-zero buffer = unwritten sparse region. Skip SHA-1:
                    # hashing 155 GB of zeros is what pegged CPUs on fresh adds.
                    continue
                if hashlib.sha1(data).digest() == piece.expected_hash:
                    self.completed_pieces.add(idx)
                    self.missing_pieces.discard(idx)
                    verified += 1
            except Exception as e:
                logger.debug(f"[VERIFY] '{self.torrent.name}' piece {idx}: unreadable ({e})")
            if log_progress:
                frac = (idx + 1) / total
                if frac >= next_log_at:
                    logger.info(
                        "[VERIFY] '%s': %d%% (%d/%d pieces, %d verified so far)",
                        self.torrent.name,
                        int(frac * 100),
                        idx + 1,
                        total,
                        verified,
                    )
                    next_log_at += 0.1

        self.bytes_downloaded = sum(self.torrent.piece_size(i) for i in self.completed_pieces)
        if self.torrent.piece_count >= 500 or verified:
            logger.info(
                "[VERIFY] '%s': done in %.1fs — %d/%d pieces verified (%.1f%%)",
                self.torrent.name,
                time.time() - t_start,
                verified,
                total,
                (verified / total * 100.0) if total else 100.0,
            )
        # Files already complete on disk lose .part immediately (Filebrowser
        # shows .mkv right after boot, not after the whole torrent finishes).
        try:
            self._maybe_finalize_files()
        except Exception as e:
            logger.debug(f"[VERIFY] post-verify finalize deferred: {e}")
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
        if peer_key not in self.peers:
            return []
        peer_pieces = self.peers.get(peer_key)

        now = time.time()
        blocks_to_request: List[Block] = []

        # High-priority files first: ongoing pieces sorted so episode 1's
        # blocks are requested before episode 2's.
        def _ongoing_by_priority() -> List[int]:
            return sorted(list(self.ongoing_pieces), key=lambda i: -self.piece_priority(i))

        # 1. First priority: timed-out blocks in ongoing pieces this peer has
        for piece_idx in _ongoing_by_priority():
            if self._peer_has(peer_pieces, piece_idx):
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete:
                        if block.requested_time > 0.0 and (now - block.requested_time > BLOCK_TIMEOUT):
                            block.mark_requested()
                            blocks_to_request.append(block)
                            if len(blocks_to_request) >= max_count:
                                return blocks_to_request

        # 2. Second priority: unrequested blocks in ongoing pieces
        for piece_idx in _ongoing_by_priority():
            if self._peer_has(peer_pieces, piece_idx):
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete and block.requested_time == 0.0:
                        block.mark_requested()
                        blocks_to_request.append(block)
                        if len(blocks_to_request) >= max_count:
                            return blocks_to_request

        # 3. Third priority: start new missing pieces that this peer has (rarest-first).
        # Cached availability + sampling keeps this O(K log K) instead of
        # O(P*N) sorted(all_missing) on every request-loop tick (50 peers x
        # 100k pieces melted CPUs on 155 GB torrents).
        try:
            sort_cap = int(float(os.environ.get("EVA_RAREST_SORT_CAP", 2000)))
        except (ValueError, TypeError):
            sort_cap = 2000
        try:
            max_ongoing = int(float(os.environ.get("EVA_MAX_ONGOING_PIECES", 32)))
        except (ValueError, TypeError):
            max_ongoing = 32
        if len(self.ongoing_pieces) < max(1, max_ongoing):
            self._refresh_rarity_counts()
            counts = self._rarity_counts
            if peer_pieces is None:
                candidates = [i for i in self.missing_pieces if i not in self.ongoing_pieces]
            else:
                candidates = [i for i in self.missing_pieces if i in peer_pieces and i not in self.ongoing_pieces]
            if len(candidates) > sort_cap:
                # Deterministic head-sample: bounds CPU while keeping progress.
                # Keep high-priority pieces in the sample.
                candidates.sort(key=lambda idx: -self.piece_priority(idx))
                candidates = candidates[:sort_cap]
            # High-priority files first, then rarest-first within each tier.
            candidates.sort(key=lambda idx: (-self.piece_priority(idx), counts.get(idx, 0)))
            for piece_idx in candidates:
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

        # 4. Endgame: when only a few pieces remain, re-request in-flight blocks
        # from additional peers. First response wins; duplicates are ignored
        # on receipt (see on_block_received), so this only costs bandwidth.
        if not blocks_to_request and len(self.missing_pieces) + len(self.ongoing_pieces) <= ENDGAME_REMAINING_PIECES:
            for piece_idx in _ongoing_by_priority():
                if not self._peer_has(peer_pieces, piece_idx):
                    continue
                piece = self.pieces[piece_idx]
                for block in piece.blocks:
                    if not block.is_complete:
                        block.mark_requested()
                        blocks_to_request.append(block)
                        if len(blocks_to_request) >= max_count:
                            return blocks_to_request

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

        # Ignore duplicate arrivals (endgame duplicates / timeout reassignment):
        # without this, bytes_downloaded would be inflated.
        for block in piece.blocks:
            if block.begin == begin and block.is_complete:
                return False

        success = piece.set_block_data(begin, data)
        if not success:
            return False

        self.bytes_downloaded += len(data)

        if piece.is_complete:
            if piece.verify_hash():
                # Write to disk
                self.disk_writer.write_piece(index, piece.get_data())
                # Release the piece buffer immediately: completed pieces kept
                # their full data in heap forever, which OOM-killed a 4 GB
                # container on a 158 GB torrent (16 MB pieces x 100s retained).
                # Served-from-disk reads (seeding) re-read per block instead.
                piece.reset()
                if index in self.ongoing_pieces:
                    self.ongoing_pieces.remove(index)
                if index in self.missing_pieces:
                    self.missing_pieces.remove(index)
                self.completed_pieces.add(index)
                if self.torrent.piece_count < 2000 or index % 50 == 0 or self.is_complete:
                    logger.info(
                        f"Piece {index}/{self.torrent.piece_count} verified & saved. "
                        f"Progress: {self.progress_percentage:.1f}%"
                    )

                # If all (selected) pieces are complete, finalize files (remove .part extensions)
                if self.is_complete:
                    selected = None if self.selected_files is None else set(self.selected_files)
                    self.disk_writer.finalize(selected_paths=selected)
                    for f in self.torrent.files:
                        if selected is None or f.path in selected:
                            self.finalized_files.add(f.path)
                else:
                    # Per-file finalize: finished files drop .part immediately.
                    self._maybe_finalize_files()

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
