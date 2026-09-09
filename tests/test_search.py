import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient

from evatorrent.search.base import (
    BaseSearchProvider,
    SearchCategory,
    SearchResult,
    format_bytes,
)
from evatorrent.search.piratebay import PirateBaySearchProvider, build_magnet
from evatorrent.search.service import SearchService
from evatorrent.web.app import app, auth_config, session_manager


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(500) == "500 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1048576) == "1.00 MB"
    assert format_bytes(1073741824) == "1.00 GB"


def test_build_magnet():
    h = "4a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
    title = "Test Torrent"
    magnet = build_magnet(h, title)
    assert magnet.startswith(f"magnet:?xt=urn:btih:{h}")
    assert "dn=Test%20Torrent" in magnet
    assert "tr=udp" in magnet


@pytest.mark.asyncio
async def test_piratebay_provider_parsing():
    fake_json = [
        {
            "id": "123",
            "name": "Ubuntu 24.04 Desktop",
            "info_hash": "a" * 40,
            "seeders": "45",
            "leechers": "3",
            "size": "4294967296",
            "category": "303",
            "added": "1710000000",
        },
        {
            "id": "0",
            "name": "No results returned",
            "info_hash": "",
            "seeders": "0",
            "leechers": "0",
            "size": "0",
        },
        {
            "id": "456",
            "name": "Invalid Hash Item",
            "info_hash": "short",
            "seeders": "10",
            "leechers": "0",
            "size": "100",
        },
    ]

    provider = PirateBaySearchProvider()
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_json
        mock_get.return_value = mock_resp

        results = await provider.search("ubuntu", category=SearchCategory.SOFTWARE)
        assert len(results) == 1
        res = results[0]
        assert res.title == "Ubuntu 24.04 Desktop"
        assert res.info_hash == "a" * 40
        assert res.seeders == 45
        assert res.leechers == 3
        assert res.size_bytes == 4294967296
        assert res.category == "Linux App"


@pytest.mark.asyncio
async def test_search_service_ranking_and_fallback():
    class MockProvider(BaseSearchProvider):
        name = "Mock"

        def __init__(self, results):
            self._results = results

        async def search(self, query, category=SearchCategory.ALL):
            return self._results

    # Case 1: Active seeds available
    r1 = SearchResult("Item Low", "a" * 40, "mag:a", 100, "100 B", 2, 0, "All", "Mock")
    r2 = SearchResult("Item High", "b" * 40, "mag:b", 200, "200 B", 50, 10, "All", "Mock")
    r3 = SearchResult("Item Dead", "c" * 40, "mag:c", 300, "300 B", 0, 0, "All", "Mock")

    service = SearchService(providers=[MockProvider([r1, r2, r3])])
    res = await service.search("test", hide_dead=True)

    assert res["total_found"] == 3
    assert res["returned"] == 2
    assert res["fallback_applied"] is False
    assert res["results"][0]["info_hash"] == "b" * 40  # highest seeds first
    assert res["results"][1]["info_hash"] == "a" * 40

    # Case 2: No active seeds available -> fallback should return all
    dead_service = SearchService(providers=[MockProvider([r3])])
    res_fallback = await dead_service.search("test", hide_dead=True)
    assert res_fallback["total_found"] == 1
    assert res_fallback["returned"] == 1
    assert res_fallback["fallback_applied"] is True
    assert res_fallback["results"][0]["info_hash"] == "c" * 40


@pytest.mark.asyncio
async def test_api_search_endpoint():
    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    sample_results = [
        SearchResult(
            title="Blender 3D",
            info_hash="d" * 40,
            magnet_uri="magnet:?xt=urn:btih:" + "d" * 40,
            size_bytes=200000000,
            size_formatted="190.73 MB",
            seeders=30,
            leechers=5,
            category="Software",
            provider="The Pirate Bay",
        )
    ]

    transport = ASGITransport(app=app)
    with patch("evatorrent.web.app.search_service.search") as mock_search:
        mock_search.return_value = {
            "query": "blender",
            "category": "software",
            "total_found": 1,
            "returned": 1,
            "fallback_applied": False,
            "results": [r.to_dict() for r in sample_results],
        }

        async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            resp = await client.get("/api/search?q=blender&category=software")
            assert resp.status_code == 200
            data = resp.json()
            assert data["query"] == "blender"
            assert data["returned"] == 1
            assert data["results"][0]["title"] == "Blender 3D"
