import hashlib
import tempfile
from pathlib import Path
import pytest
from evatorrent.bencoding import bencode
from evatorrent.engine.manager import EngineManager
from evatorrent.engine.session import TorrentSession, TorrentStatus
from evatorrent.torrent import Torrent


def create_dummy_torrent() -> Torrent:
    piece_len = 16384
    fake_hash = hashlib.sha1(b"testpiece").digest()
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"dummy.iso",
            b"piece length": piece_len,
            b"pieces": fake_hash,
            b"length": 10000,
        },
    }
    return Torrent(bencode(meta))


@pytest.mark.asyncio
async def test_torrent_session_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        torrent = create_dummy_torrent()
        session = TorrentSession(torrent, Path(tmpdir))

        assert session.status == TorrentStatus.PENDING
        d = session.to_dict()
        assert d["name"] == "dummy.iso"
        assert d["info_hash"] == torrent.info_hash_hex
        assert d["progress"] == 0.0
        assert d["total_size"] == 10000

        session.start()
        assert session.status == TorrentStatus.DOWNLOADING

        await session.pause()
        assert session.status == TorrentStatus.PAUSED

        session.resume()
        assert session.status == TorrentStatus.DOWNLOADING

        await session.stop()


@pytest.mark.asyncio
async def test_engine_manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = EngineManager(default_download_dir=Path(tmpdir))
        torrent = create_dummy_torrent()

        session = manager.add_torrent(torrent)
        assert session.torrent.info_hash_hex in manager.sessions

        fetched = manager.get_session(torrent.info_hash_hex)
        assert fetched is session

        all_torrents = manager.get_all_torrents()
        assert len(all_torrents) == 1
        assert all_torrents[0]["name"] == "dummy.iso"

        stats = manager.get_global_stats()
        assert stats["active_torrents"] == 1

        # Pause and resume via manager
        paused = await manager.pause_torrent(torrent.info_hash_hex)
        assert paused is True
        assert session.status == TorrentStatus.PAUSED

        resumed = manager.resume_torrent(torrent.info_hash_hex)
        assert resumed is True
        assert session.status == TorrentStatus.DOWNLOADING

        removed = await manager.remove_torrent(torrent.info_hash_hex)
        assert removed is True
        assert len(manager.sessions) == 0

        await manager.shutdown()
