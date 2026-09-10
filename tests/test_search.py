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
from evatorrent.web.app import app, auth_config, engine_manager, session_manager


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
        mock_resp = AsyncMock()
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

        async def search(self, query, category=SearchCategory.ALL, timeout=None):
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
async def test_search_service_spelling_variation():
    class DynamicMockProvider(BaseSearchProvider):
        name = "DynamicMock"

        async def search(self, query, category=SearchCategory.ALL, timeout=None):
            if "traveler" in query:
                return [
                    SearchResult("The Time Traveler's Wife", "e" * 40, "mag:e", 500, "500 MB", 25, 2, "Movies", "Mock")
                ]
            return []

    service = SearchService(providers=[DynamicMockProvider()])
    res = await service.search("time travellers wife")
    assert res["total_found"] == 1
    assert res["suggestion"] == "time travelers wife"
    assert res["results"][0]["title"] == "The Time Traveler's Wife"


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


def test_search_cache_manager(tmp_path):
    from evatorrent.search.cache import SearchCacheManager, format_cache_age

    assert format_cache_age(10) == "just now"
    assert format_cache_age(120) == "2 mins ago"
    assert format_cache_age(7200) == "2 hours ago"
    assert format_cache_age(172800) == "2 days ago"

    cache_dir = tmp_path / "cached_results"
    cm = SearchCacheManager(cache_dir=cache_dir, max_cached=3)

    # Initially empty
    assert cm.get("ubuntu", "all") is None
    assert cm.get_recent(10) == []

    # Save search results
    res1 = [{"title": "Ubuntu 24.04", "info_hash": "1" * 40, "seeders": 100}]
    cm.set("ubuntu", "all", res1, total_found=1)

    cached = cm.get("ubuntu", "all")
    assert cached is not None
    assert cached["query"] == "ubuntu"
    assert cached["is_cached"] is True
    assert cached["total_found"] == 1
    assert cached["results"][0]["title"] == "Ubuntu 24.04"

    recent = cm.get_recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["query"] == "ubuntu"

    # Save more items to test pruning up to max_cached=3
    cm.set("debian", "all", [{"title": "Debian", "info_hash": "2" * 40}])
    cm.set("arch", "all", [{"title": "Arch", "info_hash": "3" * 40}])
    cm.set("fedora", "all", [{"title": "Fedora", "info_hash": "4" * 40}])

    # With max_cached=3, 'ubuntu' (oldest) should have been pruned
    index = cm._load_index()
    assert len(index) == 3
    queries = [i["query"] for i in index]
    assert queries == ["fedora", "arch", "debian"]
    assert cm.get("ubuntu", "all") is None
    assert cm.get("fedora", "all") is not None


@pytest.mark.asyncio
async def test_search_service_caching_and_refresh(tmp_path):
    from evatorrent.search.cache import SearchCacheManager

    cm = SearchCacheManager(cache_dir=tmp_path / "cached_results")
    provider_calls = 0

    class CountingProvider(BaseSearchProvider):
        name = "CountingProvider"

        async def search(self, query, category=SearchCategory.ALL, timeout=None):
            nonlocal provider_calls
            provider_calls += 1
            return [SearchResult("Item", "f" * 40, "mag:f", 100, "100 B", 5, 1, "All", "Counting")]

    service = SearchService(providers=[CountingProvider()], cache_manager=cm)

    # 1. First search: calls provider and populates cache
    res1 = await service.search("hello", refresh=False)
    assert res1["is_cached"] is False
    assert provider_calls == 1

    # 2. Second search: should be served directly from cache (provider not called)
    res2 = await service.search("hello", refresh=False)
    assert res2["is_cached"] is True
    assert provider_calls == 1

    # 3. Third search with refresh=True: forces live query and bypasses cache
    res3 = await service.search("hello", refresh=True)
    assert res3["is_cached"] is False
    assert provider_calls == 2


@pytest.mark.asyncio
async def test_api_recent_searches_endpoint():
    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    with patch("evatorrent.web.app.search_cache_manager.get_recent") as mock_recent:
        mock_recent.return_value = [
            {
                "query": "ted lasso",
                "category": "series",
                "cached_timestamp": 1234567890,
                "total_results": 5,
                "cache_age_human": "5 mins ago",
            }
        ]

        async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            resp = await client.get("/api/search/recent")
            assert resp.status_code == 200
            data = resp.json()
            assert "recent_searches" in data
            assert len(data["recent_searches"]) == 1
            assert data["recent_searches"][0]["query"] == "ted lasso"


def test_search_result_source_and_torrent_urls():
    sr = SearchResult(
        title="Test Movie 2025",
        info_hash="a" * 40,
        magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
        size_bytes=1000,
        size_formatted="1000 B",
        seeders=10,
        leechers=2,
        category="Movies",
        provider="The Pirate Bay",
        source_url="https://thepiratebay.org/description.php?id=12345",
        torrent_url="https://itorrents.org/torrent/" + "A" * 40 + ".torrent",
    )
    d = sr.to_dict()
    assert d["source_url"] == "https://thepiratebay.org/description.php?id=12345"
    assert d["torrent_url"].startswith("https://itorrents.org/torrent/")


@pytest.mark.asyncio
async def test_download_torrent_file_endpoint(tmp_path):
    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    test_hash = "f" * 40
    # Mock cache file
    cache_dir = engine_manager.download_dir / ".torrent_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fake_torrent_bytes = b"d4:infod4:name9:test_item6:lengthi100e12:piece lengthi16e6:pieces20:12345678901234567890ee"
    (cache_dir / f"{test_hash}.torrent").write_bytes(fake_torrent_bytes)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        resp = await client.get(f"/api/torrents/download_file?info_hash={test_hash}&name=My_Movie")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-bittorrent"
        assert 'attachment; filename="My_Movie.torrent"' in resp.headers["content-disposition"]
        assert resp.content == fake_torrent_bytes


@pytest.mark.asyncio
async def test_add_torrent_via_web_url():
    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    fake_torrent_bytes = b"d4:infod4:name9:test_item6:lengthi16e12:piece lengthi16e6:pieces20:12345678901234567890ee"

    transport = ASGITransport(app=app)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = fake_torrent_bytes
        mock_resp.text = "fake"
        mock_get.return_value = mock_resp

        async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            resp = await client.post("/api/torrents/add", json={"magnet": "https://example.com/test.torrent"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["name"] == "test_item"


@pytest.mark.asyncio
async def test_limetorrents_provider_parsing():
    from evatorrent.search.limetorrents import LimeTorrentsSearchProvider, parse_size_to_bytes

    assert parse_size_to_bytes("10.99 MB") == int(10.99 * 1024 * 1024)
    assert parse_size_to_bytes("1.5 GB") == int(1.5 * 1024 * 1024 * 1024)
    assert parse_size_to_bytes("500 KB") == int(500 * 1024)

    provider = LimeTorrentsSearchProvider()
    fake_html = """
    <tr>
      <td class="tdleft">
        <div class="tt-name">
          <a href="http://itorrents.net/torrent/38A88D94BC8C3A7B4EC13C50AC05BB60FFDBBC0D.torrent?title=Just+Like+Heaven" rel="nofollow" class="csprite_dl14"></a>
          <a href="/Just-Like-Heaven-2005-torrent-12345.html">Just Like Heaven 2005 1080p</a>
        </div>
      </td>
      <td class="tdnormal">1 Year+ - in Movies</a></td>
      <td class="tdnormal">2.1 GB</td>
      <td class="tdseed">45</td>
      <td class="tdleech">12</td>
    </tr>
    """

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_html
        mock_get.return_value = mock_resp

        results = await provider.search("just like heaven")
        assert len(results) == 1
        r = results[0]
        assert r.title == "Just Like Heaven 2005 1080p"
        assert r.info_hash == "38a88d94bc8c3a7b4ec13c50ac05bb60ffdbbc0d"
        assert r.seeders == 45
        assert r.leechers == 12
        assert r.size_formatted == "2.1 GB"
        assert r.category == "Movies"
        assert r.provider == "LimeTorrents"
        assert r.torrent_url == "https://itorrents.net/torrent/38A88D94BC8C3A7B4EC13C50AC05BB60FFDBBC0D.torrent"
        assert "https://www.limetorrents.lol/Just-Like-Heaven" in r.source_url


@pytest.mark.asyncio
async def test_yts_provider_parsing():
    from evatorrent.search.yts import YtsSearchProvider

    provider = YtsSearchProvider()
    fake_json = {
        "status": "ok",
        "data": {
            "movie_count": 1,
            "movies": [
                {
                    "title": "Inception",
                    "title_long": "Inception (2010)",
                    "url": "https://yts.mx/movies/inception-2010",
                    "date_uploaded": "2023-01-15 12:00:00",
                    "genres": ["Sci-Fi", "Action"],
                    "torrents": [
                        {
                            "hash": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2",
                            "quality": "1080p",
                            "type": "bluray",
                            "size": "1.80 GB",
                            "size_bytes": 1932735283,
                            "seeds": 150,
                            "peers": 30,
                            "url": "https://yts.mx/torrent/download/A1B2C3D4",
                        }
                    ],
                }
            ],
        },
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_json
        mock_get.return_value = mock_resp

        results = await provider.search("inception")
        assert len(results) == 1
        r = results[0]
        assert "Inception" in r.title
        assert "1080p" in r.title
        assert r.info_hash == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert r.seeders == 150
        assert r.leechers == 30
        assert r.category == "Movies"
        assert r.provider == "YTS"
        assert r.source_url == "https://yts.mx/movies/inception-2010"


@pytest.mark.asyncio
async def test_nyaa_provider_parsing():
    from evatorrent.search.nyaa import NyaaSearchProvider, parse_size_to_bytes

    assert parse_size_to_bytes("1.4 GiB") == int(1.4 * 1024 * 1024 * 1024)
    assert parse_size_to_bytes("500 MiB") == int(500 * 1024 * 1024)

    provider = NyaaSearchProvider()
    fake_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
      <channel>
        <item>
          <title>Frieren Beyond Journey End S01E01 1080p</title>
          <link>https://nyaa.si/download/123456.torrent</link>
          <guid>https://nyaa.si/view/123456</guid>
          <nyaa:infoHash>AABB11223344556677889900AABB112233445566</nyaa:infoHash>
          <nyaa:seeders>200</nyaa:seeders>
          <nyaa:leechers>15</nyaa:leechers>
          <nyaa:size>1.4 GiB</nyaa:size>
          <nyaa:category>Anime - English-translated</nyaa:category>
        </item>
      </channel>
    </rss>"""

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_rss
        mock_get.return_value = mock_resp

        results = await provider.search("frieren")
        assert len(results) == 1
        r = results[0]
        assert "Frieren" in r.title
        assert r.info_hash == "aabb11223344556677889900aabb112233445566"
        assert r.seeders == 200
        assert r.leechers == 15
        assert r.category == "Anime"
        assert r.provider == "Nyaa"


@pytest.mark.asyncio
async def test_eztv_provider_parsing():
    from evatorrent.search.eztv import EztvSearchProvider

    provider = EztvSearchProvider()
    fake_html = """
    <table class="forum_header_border">
      <tr name="hover" class="forum_header_border">
        <td class="forum_thread_post"><a href="/ep/show1" class="epinfo">Breaking Bad S01E01 720p</a></td>
        <td class="forum_thread_post"><a href="magnet:?xt=urn:btih:CCDD11223344556677889900CCDD112233445566&dn=breaking" class="magnet">&nbsp;</a>
            <a href="https://eztv.re/download/1.torrent" class="download_1"></a></td>
        <td class="forum_thread_post">700 MB</td>
        <td class="forum_thread_post"><font color="green">85</font></td>
      </tr>
    </table>"""

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_html
        mock_get.return_value = mock_resp

        results = await provider.search("breaking bad")
        assert len(results) == 1
        r = results[0]
        assert "Breaking Bad" in r.title
        assert r.info_hash == "ccdd11223344556677889900ccdd112233445566"
        assert r.seeders == 85
        assert r.category == "TV / Series"
        assert r.provider == "EZTV"


def test_health_scoring():
    from evatorrent.search.health import compute_health_score

    # Dead torrent: 0 seeds, 0 leechers
    assert compute_health_score(0, 0) == 0.0

    # Ghost seeder: 1 seed, 0 leechers, >90 days old
    score_ghost = compute_health_score(1, 0, age_days=120)
    assert score_ghost < 0.05

    # Healthy torrent: many seeds
    score_healthy = compute_health_score(500, 50, age_days=7)
    assert score_healthy > 0.5

    # Very popular torrent
    score_popular = compute_health_score(5000, 200, age_days=1)
    assert score_popular > 0.8

    # Score decreases with age for low-seed torrents
    score_new = compute_health_score(2, 1, age_days=1)
    score_old = compute_health_score(2, 1, age_days=365)
    assert score_new > score_old

    # All scores within bounds
    for s, lch, a in [(0, 0, 0), (1, 0, 0), (100, 50, 30), (10000, 1000, 1)]:
        score = compute_health_score(s, lch, age_days=a)
        assert 0.0 <= score <= 1.0
