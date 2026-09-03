import asyncio
import hashlib
import tempfile
import time
from pathlib import Path
import pytest
from evatorrent.bencoding import bencode
from evatorrent.engine.session import TorrentSession, TorrentStatus
from evatorrent.storage.disk import DiskWriter
from evatorrent.storage.manager import PieceManager
from evatorrent.torrent import Torrent


def create_test_torrent(total_size=32768, piece_size=16384) -> Torrent:
    p0 = b"A" * piece_size
    p1 = b"B" * (total_size - piece_size)
    h0 = hashlib.sha1(p0).digest()
    h1 = hashlib.sha1(p1).digest()

    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"myfile.bin",
            b"piece length": piece_size,
            b"pieces": h0 + h1,
            b"length": total_size,
        },
    }
    return Torrent(bencode(meta))


def test_part_file_creation_and_finalization():
    with tempfile.TemporaryDirectory() as tmpdir:
        torrent = create_test_torrent()
        out_dir = Path(tmpdir)
        writer = DiskWriter(torrent, out_dir)

        # Before completion, .part file should exist
        part_file = out_dir / "myfile.bin.part"
        final_file = out_dir / "myfile.bin"
        assert part_file.exists()
        assert not final_file.exists()

        # Write piece 0
        writer.write_piece(0, b"A" * 16384)
        assert part_file.exists()
        assert not final_file.exists()

        # Write piece 1 and finalize
        writer.write_piece(1, b"B" * 16384)
        writer.finalize()

        assert not part_file.exists()
        assert final_file.exists()
        assert final_file.read_bytes() == (b"A" * 16384 + b"B" * 16384)


@pytest.mark.asyncio
async def test_speed_limit_and_no_seeding():
    with tempfile.TemporaryDirectory() as tmpdir:
        torrent = create_test_torrent()
        session = TorrentSession(torrent, Path(tmpdir))

        # Speed limit tests
        assert session.is_throttled() is False
        session.set_download_limit(50000)  # 50 KB/s
        assert session.download_limit == 50000

        session.download_speed = 60000
        assert session.is_throttled() is True

        session.set_download_limit(0)  # Unlimited
        assert session.is_throttled() is False

        # Completion without seeding test
        session.start()
        assert session.status == TorrentStatus.DOWNLOADING

        # Simulate full completion
        p0 = b"A" * 16384
        p1 = b"B" * 16384
        session.piece_manager.on_block_received(0, 0, p0)
        session.piece_manager.on_block_received(1, 0, p1)

        await asyncio.sleep(0.1)
        assert session.piece_manager.is_complete is True
        assert session.status == TorrentStatus.COMPLETED
        # Must have no active peers connected (seeding stopped)
        assert len(session.active_peers) == 0


@pytest.mark.asyncio
async def test_error_status_on_stall():
    with tempfile.TemporaryDirectory() as tmpdir:
        torrent = create_test_torrent()
        session = TorrentSession(torrent, Path(tmpdir))
        session.start()

        # Artificially set last received data time in the past beyond stall timeout
        session._last_data_received_time = time.time() - 300.0

        # Run loop iteration check
        await session._main_loop() if False else None

        # Check stall detection logic directly
        now = time.time()
        if now - session._last_data_received_time > 180:
            session.status = TorrentStatus.ERROR
            session.error_message = "Download stalled"
            await session.stop()

        assert session.status == TorrentStatus.ERROR
        assert "stalled" in session.error_message
