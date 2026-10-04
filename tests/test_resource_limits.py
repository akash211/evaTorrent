import hashlib
import logging
import tempfile
from pathlib import Path

import pytest

from evatorrent.bencoding import bencode
from evatorrent.engine.limits import effective_peer_settings
from evatorrent.storage.manager import PieceManager
from evatorrent.torrent import Torrent


def _make_torrent(total_length: int, piece_length: int = 16384) -> Torrent:
    import math

    count = math.ceil(total_length / piece_length)
    hashes = b"".join(hashlib.sha1(bytes([i % 256]) * piece_length).digest() for i in range(count))
    # Last piece may be shorter; hash accordingly for correctness of size only.
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"big.bin",
            b"piece length": piece_length,
            b"pieces": hashes,
            b"length": total_length,
        },
    }
    return Torrent(bencode(meta))


def test_effective_peer_settings_small_torrent_untouched(monkeypatch):
    monkeypatch.delenv("EVA_MAX_PEERS", raising=False)
    monkeypatch.delenv("EVA_PIPELINE_PER_PEER", raising=False)
    peers, pipe = effective_peer_settings(100 * 1024 * 1024)
    assert peers == 50 and pipe == 10


def test_effective_peer_settings_large_torrent_clamped(monkeypatch):
    monkeypatch.delenv("EVA_MAX_PEERS", raising=False)
    monkeypatch.delenv("EVA_PIPELINE_PER_PEER", raising=False)
    peers, pipe = effective_peer_settings(155 * 1024 * 1024 * 1024)
    assert peers <= 15 and pipe <= 4


def test_session_clamps_max_peers_for_large_torrent(monkeypatch):
    from evatorrent.engine.session import TorrentSession

    torrent = _make_torrent(total_length=16384 * 4)
    # Fake a huge total_length without materializing pieces.
    object.__setattr__(torrent, "total_length", 155 * 1024 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as tmpdir:
        s = TorrentSession(torrent, Path(tmpdir))
        assert s.max_peers <= 15
        assert s.to_dict()["max_peers"] <= 15


def test_seeder_sentinel_does_not_duplicate_sets():
    torrent = _make_torrent(total_length=16384 * 2)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.peer_has_all_pieces("1.2.3.4:6881")
        assert pm.peers["1.2.3.4:6881"] is None
        req = pm.next_request("1.2.3.4:6881")
        assert req is not None


def test_check_existing_files_off_mode_is_fast():
    torrent = _make_torrent(total_length=16384 * 2)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        assert pm.check_existing_files(verify_mode="off") == 0
        assert pm.progress_percentage == 0.0


def test_check_existing_files_quick_skips_empty_part(monkeypatch):
    torrent = _make_torrent(total_length=16384 * 2)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        # DiskWriter created empty .part; quick mode must not hash zeros as valid.
        assert pm.check_existing_files(verify_mode="quick") == 0


def test_verify_completion_is_logged(caplog):
    import math

    piece_length = 16384
    data_p0 = b"A" * piece_length
    data_p1 = b"B" * 1000
    h0 = hashlib.sha1(data_p0).digest()
    h1 = hashlib.sha1(data_p1).digest()
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"logged.bin",
            b"piece length": piece_length,
            b"pieces": h0 + h1,
            b"length": len(data_p0) + len(data_p1),
        },
    }
    torrent = Torrent(bencode(meta))
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = PieceManager(torrent, Path(tmpdir))
        pm.disk_writer.write_piece(0, data_p0)
        pm.disk_writer.write_piece(1, data_p1)
        with caplog.at_level(logging.INFO):
            assert pm.check_existing_files(verify_mode="full") == 2
        assert "[VERIFY]" in caplog.text
        assert "logged.bin" in caplog.text


@pytest.mark.asyncio
async def test_engine_add_logs_torrent_details(caplog):
    from evatorrent.engine.manager import EngineManager

    torrent = _make_torrent(total_length=16384 * 2)
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = EngineManager(default_download_dir=Path(tmpdir))
        with caplog.at_level(logging.INFO):
            session = manager.add_torrent(torrent)
        assert "[ENGINE] Added" in caplog.text
        assert "big.bin" in caplog.text
        assert session.max_peers >= 1
        await manager.shutdown()
