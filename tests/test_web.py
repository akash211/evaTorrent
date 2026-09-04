import hashlib
import pytest
from httpx import ASGITransport, AsyncClient
from evatorrent.bencoding import bencode
from evatorrent.web.app import app


def create_sample_torrent_bytes() -> bytes:
    piece_len = 16384
    fake_hash = hashlib.sha1(b"web_test").digest()
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"web_sample.iso",
            b"piece length": piece_len,
            b"pieces": fake_hash,
            b"length": 5000,
        },
    }
    return bencode(meta)


@pytest.mark.asyncio
async def test_web_endpoints():
    from evatorrent.web.app import auth_config, session_manager
    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        # 1. Index page (public)
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "evaTorrent" in resp.text

        # 2. Stats
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_torrents" in data

        # 3. List torrents
        resp = await client.get("/api/torrents")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        # 4. Upload torrent
        sample_bytes = create_sample_torrent_bytes()
        files = {"file": ("test.torrent", sample_bytes, "application/x-bittorrent")}
        resp = await client.post("/api/torrents/upload", files=files)
        assert resp.status_code == 200
        upload_data = resp.json()
        assert upload_data["success"] is True
        info_hash = upload_data["info_hash"]

        # 5. Verify it appears in list
        resp = await client.get("/api/torrents")
        torrents = resp.json()
        assert any(t["info_hash"] == info_hash for t in torrents)

        # 6. Pieces map endpoint
        resp = await client.get(f"/api/torrents/{info_hash}/pieces")
        assert resp.status_code == 200
        assert resp.json()["total_pieces"] == 1

        # 7. Peers endpoint
        resp = await client.get(f"/api/torrents/{info_hash}/peers")
        assert resp.status_code == 200

        # 8. Pause & Resume
        resp = await client.post(f"/api/torrents/{info_hash}/pause")
        assert resp.status_code == 200

        resp = await client.post(f"/api/torrents/{info_hash}/resume")
        assert resp.status_code == 200

        # 9. Delete
        resp = await client.delete(f"/api/torrents/{info_hash}")
        assert resp.status_code == 200
