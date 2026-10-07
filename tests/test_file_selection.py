import hashlib
import logging
import tempfile
from pathlib import Path

import pytest

from evatorrent.bencoding import bencode
from evatorrent.storage.manager import PieceManager
from evatorrent.torrent import Torrent


def make_multi_torrent():
    """3 files mapping cleanly onto 4 pieces (16 KiB pieces).

    f1: bytes 0..16383 (piece 0), f2: 16384..49151 (pieces 1-2), f3: 1000 B (piece 3).
    """
    piece_length = 16384
    f1 = b"A" * 16384
    f2 = b"B" * 32768
    f3 = b"C" * 1000
    payloads = [f1[:16384], f2[:16384], f2[16384:], f3]
    hashes = b"".join(hashlib.sha1(p).digest() for p in payloads)
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"series",
            b"piece length": piece_length,
            b"pieces": hashes,
            b"files": [
                {b"length": 16384, b"path": [b"ep1.mkv"]},
                {b"length": 32768, b"path": [b"ep2.mkv"]},
                {b"length": 1000, b"path": [b"ep3.mkv"]},
            ],
        },
    }
    return Torrent(bencode(meta)), [f1, f2, f3]


def test_deselect_file_skips_its_pieces():
    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        paths = [f.path for f in torrent.files]
        summary = pm.set_selected_files([paths[0], paths[2]])
        assert summary["selected_count"] == 2
        assert pm.skipped_pieces == {1, 2}
        # Peer with everything offers only selected pieces.
        pm.peer_has_all_pieces("1.2.3.4:6881")
        req = pm.next_request("1.2.3.4:6881")
        assert req is not None
        assert req.piece_index in (0, 3)
        # Progress counts selected pieces only (2 of 2 pending -> 0%).
        assert pm.progress_percentage == 0.0
        assert pm.selected_total_bytes == 16384 + 1000


def test_reselect_restores_missing_pieces():
    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        paths = [f.path for f in torrent.files]
        pm.set_selected_files([paths[0]])
        assert pm.skipped_pieces == {1, 2, 3}
        pm.set_selected_files(None)  # all again
        assert pm.skipped_pieces == set()
        assert pm.selected_files is None
        assert pm.is_complete is False


def test_selection_validation():
    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        with pytest.raises(ValueError):
            pm.set_selected_files([])
        with pytest.raises(ValueError):
            pm.set_selected_files(["nope.mkv"])


def test_completed_piece_buffer_released():
    """Completed pieces must not retain data in heap (the 4 GB OOM cause)."""
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.peer_has_all_pieces("1.2.3.4:6881")
        assert pm.on_block_received(0, 0, payloads[0]) is True
        assert 0 in pm.completed_pieces
        assert all(b.data is None for b in pm.pieces[0].blocks)
        # Seeding still works: served from disk, not memory.
        assert pm.read_block(0, 0, 16384) == payloads[0]


def test_finalize_only_selected_files():
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        paths = [f.path for f in torrent.files]
        pm.set_selected_files([paths[0]])
        pm.peer_has_all_pieces("1.2.3.4:6881")
        assert pm.on_block_received(0, 0, payloads[0]) is True
        assert pm.is_complete is True  # only selected piece needed
        selected_final = Path(tmpdir) / "series" / "ep1.mkv"
        deselected_part = Path(tmpdir) / "series" / "ep2.mkv.part"
        assert selected_final.exists()
        # Deselected file keeps .part state (empty), not a bogus final file.
        assert not (Path(tmpdir) / "series" / "ep2.mkv").exists()


def test_is_complete_ignores_skipped():
    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        assert pm.is_complete is False
        paths = [f.path for f in torrent.files]
        pm.set_selected_files([paths[0]])
        # Nothing downloaded yet: missing (0,3... wait 3 not skipped) -> not complete
        assert pm.is_complete is False


def test_selection_persisted_in_db():
    from evatorrent.db.database import Database

    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "t.db")
        db.upsert_torrent(
            info_hash=torrent.info_hash_hex,
            name=torrent.name,
            total_size=torrent.total_length,
            download_dir=str(tmpdir),
            status="downloading",
        )
        paths = [f.path for f in torrent.files]
        assert db.get_file_selection(torrent.info_hash_hex) is None
        db.set_file_selection(torrent.info_hash_hex, [paths[0]])
        assert db.get_file_selection(torrent.info_hash_hex) == [paths[0]]
        db.set_file_selection(torrent.info_hash_hex, None)
        assert db.get_file_selection(torrent.info_hash_hex) is None


def test_verify_skips_deselected_pieces(caplog):
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir), selected_files=["series/ep1.mkv"])
        # Even with ep2 data on disk, deselected pieces are never hashed.
        pm.disk_writer.write_piece(1, payloads[1][:16384])
        with caplog.at_level(logging.INFO):
            verified = pm.check_existing_files(verify_mode="full")
        assert verified == 0
        assert 1 in pm.skipped_pieces


def test_high_priority_pieces_requested_first():
    from evatorrent.storage.manager import PRIO_HIGH, PRIO_NORMAL, PRIO_SKIP

    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        paths = [f.path for f in torrent.files]
        # ep3 (piece 3) high, ep1 normal, ep2 skipped.
        summary = pm.set_file_priorities({paths[0]: PRIO_NORMAL, paths[1]: PRIO_SKIP, paths[2]: PRIO_HIGH})
        assert summary["high_priority_count"] == 1
        assert pm.skipped_pieces == {1, 2}
        assert pm.piece_priority(3) == PRIO_HIGH
        assert pm.piece_priority(0) == PRIO_NORMAL
        pm.peer_has_all_pieces("1.2.3.4:6881")
        # First request must come from the high-priority piece 3, not piece 0.
        req = pm.next_request("1.2.3.4:6881")
        assert req is not None
        assert req.piece_index == 3


def test_file_progress_per_file():
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.peer_has_all_pieces("1.2.3.4:6881")
        assert pm.on_block_received(0, 0, payloads[0]) is True
        prog = {p["path"]: p for p in pm.file_progress()}
        assert prog["series/ep1.mkv"]["progress"] == 100.0
        assert prog["series/ep1.mkv"]["done_bytes"] == 16384
        assert prog["series/ep2.mkv"]["progress"] == 0.0
        assert prog["series/ep3.mkv"]["progress"] == 0.0


def test_priorities_persisted_with_legacy_fallback():
    from evatorrent.db.database import Database

    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "t.db")
        db.upsert_torrent(
            info_hash=torrent.info_hash_hex,
            name=torrent.name,
            total_size=torrent.total_length,
            download_dir=str(tmpdir),
            status="downloading",
        )
        paths = [f.path for f in torrent.files]
        assert db.get_file_priorities(torrent.info_hash_hex) is None
        db.set_file_priorities(torrent.info_hash_hex, {paths[2]: 2})
        assert db.get_file_priorities(torrent.info_hash_hex) == {paths[2]: 2}
        effective = db.get_effective_file_priorities(torrent.info_hash_hex, paths)
        assert effective == {paths[0]: 1, paths[1]: 1, paths[2]: 2}
        # Legacy selection list converts to 1/0 map.
        db.set_file_selection(torrent.info_hash_hex, [paths[0]])
        db.set_file_priorities(torrent.info_hash_hex, None)
        effective = db.get_effective_file_priorities(torrent.info_hash_hex, paths)
        assert effective == {paths[0]: 1, paths[1]: 0, paths[2]: 0}


def test_priority_validation():
    torrent, _ = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        with pytest.raises(ValueError):
            pm.set_file_priorities({"series/ep1.mkv": 5})
        with pytest.raises(ValueError):
            pm.set_file_priorities({"nope.mkv": 1})
        # Reverting to all-normal clears the skip set.
        paths = [f.path for f in torrent.files]
        pm.set_file_priorities({paths[0]: 2, paths[1]: 0, paths[2]: 0})
        assert pm.skipped_pieces
        pm.set_file_priorities({})
        assert pm.skipped_pieces == set()
        assert pm.selected_files is None


def test_file_finalized_as_soon_as_done():
    """A finished file drops .part immediately, without waiting for the torrent."""
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.peer_has_all_pieces("1.2.3.4:6881")
        assert pm.on_block_received(0, 0, payloads[0]) is True
        assert (Path(tmpdir) / "series" / "ep1.mkv").exists()
        assert not (Path(tmpdir) / "series" / "ep1.mkv.part").exists()
        # ep2/ep3 untouched: still .part, torrent incomplete.
        assert pm.is_complete is False
        assert (Path(tmpdir) / "series" / "ep2.mkv.part").exists()


def test_verify_finalizes_complete_files_on_boot():
    torrent, payloads = make_multi_torrent()
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.disk_writer.write_piece(0, payloads[0])
        # Simulate a restart: fresh manager verifies disk...
        pm2 = PieceManager(torrent, Path(tmpdir))
        assert pm2.check_existing_files(verify_mode="full") == 1
        # ...and the finished file is already a plain .mkv for Filebrowser.
        assert (Path(tmpdir) / "series" / "ep1.mkv").exists()
