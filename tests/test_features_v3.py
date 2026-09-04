import tempfile
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from evatorrent.db.database import Database
from evatorrent.storage.manager import PieceManager
from evatorrent.torrent import Torrent
from evatorrent.bencoding import bencode
from evatorrent.web.app import app, auth_config, session_manager, check_ip_rate_limit, _ip_rate_limits


def create_dummy_torrent(piece_length=16384, file_size=32768):
    piece_count = (file_size + piece_length - 1) // piece_length
    dummy_hashes = b"\xaa" * 20 * piece_count
    info_dict = {
        b"name": b"test_seeding.bin",
        b"piece length": piece_length,
        b"pieces": dummy_hashes,
        b"length": file_size,
    }
    torrent_dict = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": info_dict,
    }
    raw = bencode(torrent_dict)
    return Torrent(raw)


def test_seeding_read_block():
    with tempfile.TemporaryDirectory() as tmp:
        torrent = create_dummy_torrent(piece_length=16384, file_size=16384)
        pm = PieceManager(torrent=torrent, download_dir=Path(tmp))

        # Initially piece 0 is missing
        assert 0 in pm.missing_pieces
        assert pm.read_block(0, 0, 1024) is None

        # Simulate writing a complete piece to disk
        data = b"\x42" * 16384
        pm.disk_writer.write_piece(0, data)
        pm.completed_pieces.add(0)
        pm.missing_pieces.remove(0)

        # Now read_block should serve the slice
        block = pm.read_block(0, 0, 512)
        assert block == b"\x42" * 512
        assert pm.bytes_uploaded == 512

        # Subsequent read
        block2 = pm.read_block(0, 512, 512)
        assert block2 == b"\x42" * 512
        assert pm.bytes_uploaded == 1024


def test_ip_rate_limiting():
    # Clear rate limit table
    _ip_rate_limits.clear()

    class MockClient:
        host = "192.168.1.100"

    class MockRequest:
        client = MockClient()
        headers = {}

    req = MockRequest()
    # 1st request - ok
    check_ip_rate_limit(req, max_requests=2, window_seconds=60.0)
    # 2nd request - ok
    check_ip_rate_limit(req, max_requests=2, window_seconds=60.0)

    # 3rd request - raises HTTP 429
    with pytest.raises(Exception) as exc_info:
        check_ip_rate_limit(req, max_requests=2, window_seconds=60.0)
    assert "429" in str(exc_info.value) or "Rate limit exceeded" in str(exc_info.value)


def test_analysis_endpoints_and_csv_export():
    with tempfile.TemporaryDirectory() as tmp:
        auth_config.data_dir = Path(tmp)
        auth_config.config_path = Path(tmp) / "config.json"
        auth_config.set_admin_email("admin@test.com")

        client = TestClient(app)
        token = session_manager.create_token("admin@test.com")
        client.cookies.set("evatorrent_session", token)

        # 1. Summary endpoint
        res = client.get("/api/analysis/summary")
        assert res.status_code == 200
        data = res.json()
        assert "total_torrents" in data
        assert "success_rate" in data

        # 2. Torrents endpoint
        res2 = client.get("/api/analysis/torrents?status=all")
        assert res2.status_code == 200
        assert "torrents" in res2.json()

        # 3. CSV Export
        res3 = client.get("/api/analysis/export.csv")
        assert res3.status_code == 200
        assert res3.headers["content-type"].startswith("text/csv")
        assert "Info Hash,Name,Total Size" in res3.text
