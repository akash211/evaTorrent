import hashlib
import tempfile
from pathlib import Path

import pytest

from evatorrent.bencoding import bencode
from evatorrent.engine.manager import EngineManager
from evatorrent.engine.session import TorrentStatus
from evatorrent.torrent import Torrent


def make_torrent(name: str, seed: int) -> Torrent:
    piece_length = 16384
    payload = bytes([seed % 256]) * 10000
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": name.encode(),
            b"piece length": piece_length,
            b"pieces": hashlib.sha1(payload).digest(),
            b"length": len(payload),
        },
    }
    return Torrent(bencode(meta))


@pytest.mark.asyncio
async def test_queue_parks_extra_torrents(monkeypatch):
    monkeypatch.setenv("EVA_MAX_ACTIVE_DOWNLOADS", "2")
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = EngineManager(default_download_dir=Path(tmpdir))
        assert mgr.max_active_downloads == 2
        s1 = mgr.add_torrent(make_torrent("one", 1))
        s2 = mgr.add_torrent(make_torrent("two", 2))
        s3 = mgr.add_torrent(make_torrent("three", 3))
        assert s1.status == TorrentStatus.DOWNLOADING
        assert s2.status == TorrentStatus.DOWNLOADING
        assert s3.status == TorrentStatus.QUEUED
        assert s3.queue_position == 1
        assert mgr.active_download_count() == 2
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_pause_promotes_queued(monkeypatch):
    monkeypatch.setenv("EVA_MAX_ACTIVE_DOWNLOADS", "1")
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = EngineManager(default_download_dir=Path(tmpdir))
        s1 = mgr.add_torrent(make_torrent("one", 11))
        s2 = mgr.add_torrent(make_torrent("two", 22))
        assert s1.status == TorrentStatus.DOWNLOADING
        assert s2.status == TorrentStatus.QUEUED
        await mgr.pause_torrent(s1.torrent.info_hash_hex)
        assert s1.status == TorrentStatus.PAUSED
        assert s2.status == TorrentStatus.DOWNLOADING
        assert s2.queue_position is None
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_resume_queues_when_full(monkeypatch):
    monkeypatch.setenv("EVA_MAX_ACTIVE_DOWNLOADS", "1")
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = EngineManager(default_download_dir=Path(tmpdir))
        s1 = mgr.add_torrent(make_torrent("one", 31))
        s2 = mgr.add_torrent(make_torrent("two", 32))
        assert s2.status == TorrentStatus.QUEUED
        # Resume on queued with no slot: stays queued.
        assert mgr.resume_torrent(s2.torrent.info_hash_hex) is True
        assert s2.status == TorrentStatus.QUEUED
        # Pause the active one: queued gets promoted.
        await mgr.pause_torrent(s1.torrent.info_hash_hex)
        assert s2.status == TorrentStatus.DOWNLOADING
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_unlimited_when_zero(monkeypatch):
    monkeypatch.setenv("EVA_MAX_ACTIVE_DOWNLOADS", "0")
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = EngineManager(default_download_dir=Path(tmpdir))
        sessions = [mgr.add_torrent(make_torrent(f"t{i}", i)) for i in range(4)]
        assert all(s.status == TorrentStatus.DOWNLOADING for s in sessions)
        await mgr.shutdown()
