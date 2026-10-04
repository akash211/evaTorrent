import logging

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_subtitle_routes_and_validation(tmp_path, monkeypatch):
    from evatorrent.web.app import app, auth_config, engine_manager, session_manager

    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    # Point engine download dir at an isolated tmp dir with one video.
    monkeypatch.setattr(engine_manager, "download_dir", tmp_path)
    (tmp_path / "Dune.Part.Two.2024.1080p.mkv").write_bytes(b"fake-video")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        # /subtitle serves the SPA (contains the new tab).
        resp = await client.get("/subtitle")
        assert resp.status_code == 200
        assert "Subtitles" in resp.text

        # Videos dropdown source.
        resp = await client.get("/api/subtitles/videos")
        assert resp.status_code == 200
        videos = resp.json()["videos"]
        assert len(videos) == 1
        assert videos[0]["name"].endswith(".mkv")

        # Search requires q or video.
        resp = await client.get("/api/subtitles/search")
        assert resp.status_code == 400

        # Path traversal on download is rejected (400, not 500).
        resp = await client.post(
            "/api/subtitles/download",
            json={"video": "../evil.mkv", "download_url": "https://example.com/a.srt"},
        )
        assert resp.status_code == 400

        # Pieces endpoint paginates (limit param honored).
        sessions = list(engine_manager.sessions.values())
        if sessions:
            h = sessions[0].torrent.info_hash_hex
            resp = await client.get(f"/api/torrents/{h}/pieces", params={"limit": 10})
            assert resp.status_code == 200
            body = resp.json()
            assert "completed_total" in body
            assert len(body["completed_indices"]) <= 10


@pytest.mark.asyncio
async def test_ui_event_beacon_requires_auth_and_logs(caplog):
    from evatorrent.web.app import app, auth_config, session_manager

    auth_config.set_admin_email("admin@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        resp = await anon.post("/api/ui/event", json={"tab": "search", "action": "tab_switch"})
        assert resp.status_code == 401

    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        with caplog.at_level(logging.INFO, logger="evaTorrent.web"):
            resp = await client.post(
                "/api/ui/event",
                json={"tab": "subtitles", "action": "tab_switch", "detail": ""},
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True
        assert "[UI]" in caplog.text
        assert "subtitles" in caplog.text

        resp = await client.post("/api/ui/event", json={"tab": "", "action": "x"})
        assert resp.status_code == 400
