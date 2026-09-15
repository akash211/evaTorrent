"""Tests for v0.8.0: Discover cache/year/Steam/saved, stall retry, speed tuning."""

from __future__ import annotations

import hashlib
import inspect
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from evatorrent.bencoding import bencode
from evatorrent.db.database import Database
from evatorrent.engine.manager import EngineManager
from evatorrent.metadata.cache import DiscoverCacheManager
from evatorrent.metadata.service import apply_year_filter
from evatorrent.torrent import Torrent


def _make_torrent(total_length: int = 5000, seed: bytes = b"v080", piece_length: int = 16384) -> Torrent:
    import math

    count = math.ceil(total_length / piece_length)
    fake = hashlib.sha1(seed).digest()
    meta = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"name": b"v080.iso",
            b"piece length": piece_length,
            b"pieces": fake * count,
            b"length": total_length,
        },
    }
    return Torrent(bencode(meta))


# --- Discover cache ---


def test_discover_cache_roundtrip_and_recent(tmp_path):
    cache = DiscoverCacheManager(tmp_path / "cached_results")
    assert cache.get("Dune", "movie", None) is None
    payload = {"query": "Dune", "type": "movie", "total_found": 1, "results": [{"title": "Dune"}]}
    cache.set("Dune", "movie", None, payload)
    hit = cache.get("Dune", "movie", None)
    assert hit is not None and hit["is_cached"] is True and hit["results"][0]["title"] == "Dune"
    # year is part of the key
    assert cache.get("Dune", "movie", 2021) is None
    cache.set("Dune", "movie", 2021, payload)
    hit_y = cache.get("Dune", "movie", 2021)
    assert hit_y is not None and hit_y["results"][0]["title"] == "Dune"
    recent = cache.get_recent()
    assert len(recent) == 2
    assert recent[0]["query"] == "Dune"


# --- Year filter ---


def test_year_filter_movies_books_exact():
    rows = [
        {"type": "movie", "title": "Dune", "year": 2021},
        {"type": "movie", "title": "Dune Part Two", "year": 2024},
        {"type": "book", "title": "Dune", "year": 1965},
        {"type": "movie", "title": "Mystery", "year": None},
    ]
    kept, applied = apply_year_filter(rows, 2021)
    assert applied is True
    assert [r["title"] for r in kept] == ["Dune"]
    kept, _ = apply_year_filter(rows, None)
    assert len(kept) == 4


def test_year_filter_tv_span_boundaries():
    rows = [
        {"type": "tv", "title": "Span Show", "year": 2010, "year_end": 2020},
        {"type": "tv", "title": "Ongoing Show", "year": 2018, "year_end": None},
        {"type": "tv", "title": "Old Show", "year": 2000, "year_end": 2005},
    ]
    for y in (2010, 2016, 2020):
        kept, _ = apply_year_filter(rows, y)
        assert "Span Show" in [r["title"] for r in kept], y
    for y in (2009, 2021):
        kept, _ = apply_year_filter(rows, y)
        assert "Span Show" not in [r["title"] for r in kept], y
    kept, _ = apply_year_filter(rows, 2024)
    assert "Ongoing Show" in [r["title"] for r in kept]
    kept, _ = apply_year_filter(rows, 2010)
    assert "Ongoing Show" not in [r["title"] for r in kept]


# --- Steam provider ---


@pytest.mark.asyncio
async def test_steam_search_and_details(monkeypatch):
    from evatorrent.metadata import service as md

    async def fake_get(client, url, params=None):
        if "storesearch" in url:
            return {
                "total": 1,
                "items": [
                    {
                        "id": 730,
                        "name": "Counter-Strike 2",
                        "tiny_image": "http://img",
                        "metascore": "82",
                        "price": {"final_str": "Free"},
                        "platforms": {"windows": True, "mac": False, "linux": True},
                    },
                ],
            }
        if "appdetails" in url:
            return {
                "730": {
                    "success": True,
                    "data": {
                        "short_description": "Tactical shooter",
                        "release_date": {"date": "27 Sep 2023"},
                        "genres": [{"description": "Action"}, {"description": "Free to Play"}],
                        "developers": ["Valve"],
                        "header_image": "http://hdr",
                    },
                }
            }
        return None

    monkeypatch.setattr(md, "_get_json", fake_get)
    async with httpx.AsyncClient() as http_client:
        results = await md.search_steam(http_client, "counter strike")
    assert len(results) == 1
    assert results[0]["type"] == "game"
    assert results[0]["steam_appid"] == 730
    enriched = await md.enrich_steam_details(http_client, results[0])
    assert enriched["year"] == 2023
    assert enriched["genres"] == ["Action", "Free to Play"]
    kept, _ = apply_year_filter([enriched], 2023)
    assert len(kept) == 1


@pytest.mark.asyncio
async def test_lookup_media_game_uses_steam(monkeypatch):
    from evatorrent.metadata import lookup_media
    from evatorrent.metadata import service as md

    monkeypatch.setattr(
        md,
        "search_steam",
        AsyncMock(
            return_value=[
                {
                    "type": "game",
                    "title": "Doom",
                    "year": 2016,
                    "release_date": "13 May 2016",
                    "runtime": None,
                    "runtime_mins": None,
                    "genres": ["Action"],
                    "languages": [],
                    "overview": "Rip and tear",
                    "poster_url": None,
                    "steam_appid": 379720,
                    "provider": "Steam",
                    "source_url": "https://x",
                    "youtube_trailer_url": "https://x",
                    "youtube_trailer_embed": "https://x",
                    "ott_india": [],
                    "ott_note": "n",
                    "imdb_id": None,
                    "imdb_url": None,
                    "imdb_rating": None,
                    "rotten_tomatoes": None,
                    "rotten_tomatoes_url": None,
                    "wiki_url": None,
                    "budget": None,
                    "revenue_box_office": None,
                }
            ]
        ),
    )
    monkeypatch.setattr(md, "search_wikipedia", AsyncMock(return_value=[]))
    monkeypatch.setattr(md, "enrich_steam_details", AsyncMock(side_effect=lambda c, it: it))
    data = await lookup_media("doom", media_type="game", limit=5, timeout=5.0)
    assert data["total_found"] >= 1
    assert data["results"][0]["title"] == "Doom"


# --- Saved library ---


def test_saved_discover_crud(tmp_path):
    db = Database(tmp_path / "eva.db")
    item = {"type": "movie", "title": "Dhurandhar", "year": 2025, "overview": "Spy epic"}
    saved = db.save_discover_item(item, remarks="Watch list")
    assert saved["title"] == "Dhurandhar" and saved["remarks"] == "Watch list"
    # re-save refreshes details, preserves remarks
    item2 = dict(item, overview="Updated")
    saved2 = db.save_discover_item(item2)
    assert saved2["id"] == saved["id"]
    assert saved2["remarks"] == "Watch list"
    rows = db.get_saved_discover()
    assert len(rows) == 1
    assert db.get_saved_discover(search="watch") and not db.get_saved_discover(search="nope")
    updated = db.update_discover_remarks(saved["id"], "Must watch!")
    assert updated and updated["remarks"] == "Must watch!"
    assert db.delete_saved_discover(saved["id"]) is True
    assert db.get_saved_discover() == []
    assert db.delete_saved_discover(9999) is False


# --- Stall auto-retry ---


@pytest.mark.asyncio
async def test_stall_retries_instead_of_error(tmp_path):
    from evatorrent.engine.session import TorrentSession

    torrent = _make_torrent()
    session = TorrentSession(torrent, tmp_path)
    session.stall_timeout_seconds = 0.05
    session.stall_retry_delay_seconds = 0.1

    class FakeResp:
        peers = []
        interval = 60.0

    with patch.object(session.tracker_manager, "announce", new=AsyncMock(return_value=FakeResp())):
        session.start()
        import asyncio as _aio

        await _aio.sleep(2.6)
        assert session.stall_retries >= 1
        assert session.status.value == "downloading"
        assert session.error_message and session.error_message.startswith("Stalled")
        await session.pause()
        assert session.status.value == "paused"
        # paused sessions must not keep retrying
        retries_frozen = session.stall_retries
        await _aio.sleep(0.4)
        assert session.stall_retries == retries_frozen


# --- Speed tuning + endgame ---


def test_speed_tuning_constants():
    from evatorrent.engine import connection as conn_mod
    from evatorrent.engine.session import TorrentSession

    assert conn_mod.PIPELINE_CAPACITY >= 8
    assert inspect.signature(TorrentSession.__init__).parameters["max_peers"].default >= 50


def test_duplicate_block_not_double_counted():
    from evatorrent.storage.manager import PieceManager

    # 64 KiB pieces → 4 blocks per piece, so one block leaves the piece incomplete.
    torrent = _make_torrent(total_length=70000, seed=b"dup", piece_length=65536)
    pm = PieceManager(torrent=torrent, download_dir=Path(tempfile.mkdtemp()))
    data = b"x" * 16384
    assert pm.on_block_received(0, 0, data) is False  # piece incomplete
    assert pm.bytes_downloaded == 16384
    assert pm.on_block_received(0, 0, data) is False  # duplicate ignored
    assert pm.bytes_downloaded == 16384


def test_endgame_rerequests_inflight_blocks():
    from evatorrent.storage.manager import PieceManager

    torrent = _make_torrent(total_length=6 * 16384, seed=b"end")
    assert torrent.piece_count == 6
    pm = PieceManager(torrent=torrent, download_dir=Path(tempfile.mkdtemp()))
    pm.missing_pieces = set()
    pm.ongoing_pieces = {5}
    pm.peers["peer-a"] = set(range(6))
    pm.pieces[5].blocks[0].mark_requested()
    blocks = pm.next_requests("peer-a", max_count=4)
    assert blocks, "endgame should re-request the in-flight block from another peer"


# --- Web API ---


@pytest.mark.asyncio
async def test_discover_year_refresh_passthrough():
    from httpx import ASGITransport, AsyncClient
    from evatorrent.web.app import app, auth_config, session_manager

    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    payload = {"query": "Dune", "type": "movie", "total_found": 0, "results": [], "keys_configured": {}}
    with patch("evatorrent.metadata.lookup_media", new=AsyncMock(return_value=dict(payload))) as mock_lookup:
        async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            resp = await client.get("/api/discover", params={"q": "Dune", "year": "2021", "refresh": "true"})
            assert resp.status_code == 200
            assert mock_lookup.call_args.kwargs.get("year") == 2021


@pytest.mark.asyncio
async def test_discover_saved_endpoints():
    from httpx import ASGITransport, AsyncClient
    from evatorrent.web.app import app, auth_config, session_manager

    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        resp = await client.post(
            "/api/discover/saved",
            json={"item": {"type": "movie", "title": "ZZZ Test Film 12345", "year": 2025}, "remarks": "tbr"},
        )
        assert resp.status_code == 200
        saved_id = resp.json()["saved"]["id"]
        resp = await client.get("/api/discover/saved", params={"search": "ZZZ Test Film"})
        assert len(resp.json()["saved"]) >= 1
        resp = await client.put(f"/api/discover/saved/{saved_id}", json={"remarks": "weekend"})
        assert resp.json()["saved"]["remarks"] == "weekend"
        resp = await client.delete(f"/api/discover/saved/{saved_id}")
        assert resp.json()["success"] is True
        resp = await client.get("/api/discover/recent")
        assert resp.status_code == 200
