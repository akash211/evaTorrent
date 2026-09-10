"""Regression tests for DB-backed swarm persistence, audio filter, and Discover."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from evatorrent.bencoding import bencode
from evatorrent.db.database import Database
from evatorrent.engine.manager import EngineManager
from evatorrent.search.base import SearchResult
from evatorrent.search.service import SearchService, apply_audio_language_filter
from evatorrent.torrent import Torrent


def _make_torrent(name: str = "persist.iso", seed: bytes = b"persist") -> tuple[Torrent, bytes]:
    fake = hashlib.sha1(seed).digest()
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": name.encode(),
            b"piece length": 16384,
            b"pieces": fake,
            b"length": 5000,
        },
    }
    raw = bencode(meta)
    return Torrent(raw), raw


def _mk_result(title: str) -> SearchResult:
    h = hashlib.sha1(title.encode()).hexdigest()
    return SearchResult(
        title=title,
        info_hash=h,
        magnet_uri="magnet:?xt=urn:btih:" + h,
        size_bytes=100,
        size_formatted="100 B",
        seeders=5,
        leechers=1,
        category="movies",
        provider="test",
    )


def test_db_migration_and_resumable():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eva.db")
        t, _ = _make_torrent()
        db.upsert_torrent(
            t.info_hash_hex, t.name, t.total_length, "/tmp/dl", "downloading", magnet_uri="magnet:?xt=test"
        )
        row = db.get_torrent_history(t.info_hash_hex)
        assert row is not None
        assert row["magnet_uri"] == "magnet:?xt=test"
        # upsert without magnet preserves it
        db.upsert_torrent(t.info_hash_hex, t.name, t.total_length, "/tmp/dl", "paused")
        row2 = db.get_torrent_history(t.info_hash_hex)
        assert row2 is not None
        assert row2["magnet_uri"] == "magnet:?xt=test"
        resumable = db.get_resumable_torrents()
        assert any(r["info_hash"] == t.info_hash_hex for r in resumable)
        db.mark_torrent_completed(t.info_hash_hex, 5000, 0)
        assert all(r["info_hash"] != t.info_hash_hex for r in db.get_resumable_torrents())


@pytest.mark.asyncio
async def test_engine_restore_and_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        dl = tmp_p / "dl"
        dl.mkdir()
        db = Database(tmp_p / "eva.db")
        m = EngineManager(default_download_dir=dl, db=db)
        t, _ = _make_torrent()
        m.add_torrent(t, magnet_uri="magnet:?xt=urn:btih:" + t.info_hash_hex)
        assert (dl / ".torrent_cache" / f"{t.info_hash_hex}.torrent").exists()
        await m.shutdown()
        # simulate container recreate: new manager, same dl + db
        m2 = EngineManager(default_download_dir=dl, db=db)
        snap = m2.get_swarm_snapshot()
        assert len(snap) == 1 and snap[0]["source"] == "db" and snap[0]["resumable"] is True
        summary = await m2.restore_from_db()
        assert summary["paused"] + summary["restored"] == 1
        assert len(m2.sessions) == 1
        assert m2.get_swarm_snapshot()[0]["source"] == "live"
        # resume_db_torrent on already-live returns True
        assert await m2.resume_db_torrent(t.info_hash_hex) is True
        await m2.shutdown()


@pytest.mark.asyncio
async def test_remove_db_only_torrent():
    """DELETE on a 💾 saved row with no live session must succeed, not 404."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        dl = tmp_p / "dl"
        dl.mkdir()
        db = Database(tmp_p / "eva.db")
        m = EngineManager(default_download_dir=dl, db=db)
        t, _ = _make_torrent(seed=b"delete-me")
        m.add_torrent(t)
        m.sessions.clear()  # simulate restart: DB row survives, memory gone
        assert m.get_swarm_snapshot()[0]["source"] == "db"
        assert await m.remove_torrent(t.info_hash_hex) is True
        row = db.get_torrent_history(t.info_hash_hex)
        assert row is not None and row["status"] == "removed"
        assert m.get_swarm_snapshot() == []
        # unknown hash still reports False (API → 404)
        assert await m.remove_torrent("0" * 40) is False
        await m.shutdown()


@pytest.mark.asyncio
async def test_engine_crash_downloading_resumes_downloading():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        dl = tmp_p / "dl"
        dl.mkdir()
        db = Database(tmp_p / "eva.db")
        m = EngineManager(default_download_dir=dl, db=db)
        t, _ = _make_torrent(seed=b"crash-test")
        m.add_torrent(t)
        m.sessions.clear()  # crash without graceful shutdown
        m2 = EngineManager(default_download_dir=dl, db=db)
        summary = await m2.restore_from_db()
        assert summary["restored"] == 1
        assert list(m2.sessions.values())[0].status.value == "downloading"
        await m2.shutdown()


def test_audio_language_filter():
    res = [
        _mk_result("Dune Part Two 2024 1080p BluRay"),
        _mk_result("Dune Part Two 2024 Hindi Dubbed 1080p"),
        _mk_result("Dune 2024 Dual Audio Hindi English 4K"),
    ]
    hindi_only, applied = apply_audio_language_filter(res, hindi=True, english=False)
    assert applied is True
    assert len(hindi_only) == 2
    assert hindi_only[0].title.startswith("Dune 2024 Dual Audio")
    eng_only, _ = apply_audio_language_filter(res, hindi=False, english=True)
    assert len(eng_only) == 2
    assert all("Hindi Dubbed" not in r.title for r in eng_only)
    both, applied_both = apply_audio_language_filter(res, hindi=True, english=True)
    assert len(both) == 3 and applied_both is False


@pytest.mark.asyncio
async def test_search_service_hindi_param():
    svc = SearchService(providers=[], cache_manager=None)
    out = await svc.search("dune", hindi=True)
    assert out["audio_filter"]["hindi"] is True
    assert out["total_found"] == 0


@pytest.mark.asyncio
async def test_discover_lookup_mocked():
    from evatorrent.metadata import lookup_media

    with (
        patch("evatorrent.metadata.service.search_yts", new_callable=AsyncMock) as myts,
        patch("evatorrent.metadata.service.search_tvmaze", new_callable=AsyncMock) as mtv,
        patch("evatorrent.metadata.service.search_openlibrary", new_callable=AsyncMock) as mol,
        patch("evatorrent.metadata.service.search_wikipedia", new_callable=AsyncMock) as mwiki,
    ):
        myts.return_value = [
            {
                "type": "movie",
                "title": "Dune",
                "year": 2021,
                "release_date": "2021",
                "runtime": "155 min",
                "runtime_mins": 155,
                "genres": ["Sci-Fi"],
                "languages": ["English"],
                "overview": "Desert epic",
                "poster_url": None,
                "imdb_id": "tt123",
                "imdb_url": "https://www.imdb.com/title/tt123/",
                "imdb_rating": 8.0,
                "rotten_tomatoes": None,
                "rotten_tomatoes_url": "https://x",
                "wiki_url": "https://x",
                "budget": None,
                "revenue_box_office": None,
                "ott_india": [],
                "justwatch_in_url": "https://x",
                "ott_note": "n",
                "youtube_trailer_url": "https://youtube.com/watch?v=abc",
                "youtube_trailer_embed": "https://youtube.com/embed/abc",
                "provider": "YTS",
                "source_url": "https://yts.mx",
            }
        ]
        mtv.return_value = []
        mol.return_value = []
        mwiki.return_value = []
        data = await lookup_media("dune", media_type="movie", limit=5, timeout=5.0)
        assert data["total_found"] == 1
        item = data["results"][0]
        assert item["title"] == "Dune"
        assert "youtube_trailer_embed" in item
        assert "justwatch_in_url" in item


@pytest.mark.asyncio
async def test_web_swarm_fallback_and_discover_routes():
    from httpx import ASGITransport, AsyncClient
    from evatorrent.web.app import app, auth_config, session_manager, engine_manager

    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        for path in ("/", "/home", "/search", "/discover", "/report"):
            resp = await client.get(path)
            assert resp.status_code == 200
        # search accepts hindi/english
        with patch("evatorrent.web.app.search_service.search", new_callable=AsyncMock) as ms:
            ms.return_value = {"results": [], "returned": 0}
            resp = await client.get("/api/search", params={"q": "dune", "hindi": "true"})
            assert resp.status_code == 200
            assert ms.call_args.kwargs.get("hindi") is True
