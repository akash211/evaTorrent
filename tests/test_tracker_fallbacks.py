import time

import pytest

from evatorrent.tracker import TrackerResponse
from evatorrent.tracker.fallbacks import _validate, cache_paths, read_cached
from evatorrent.tracker.manager import TrackerManager


def test_validate_trackers():
    raw = [
        "udp://tracker.example.com:1337/announce",
        "https://tracker.example.com/announce",
        "  udp://tracker.example.com:1337/announce  ",
        "not a url",
        "ftp://x.com/announce",
        "",
        "udp://has space.com:1337/announce",
    ]
    out = _validate(raw)
    assert out == [
        "udp://tracker.example.com:1337/announce",
        "https://tracker.example.com/announce",
    ]


def test_read_cached_missing(tmp_path):
    trackers, age, source = read_cached(tmp_path)
    assert trackers == []
    assert source == "none"


def test_read_cached_roundtrip(tmp_path):
    cache_file, meta_file = cache_paths(tmp_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("udp://a.example:1337/announce\nhttps://b.example/announce\n")
    import json

    meta_file.write_text(json.dumps({"fetched_at": time.time(), "source": "test", "count": 2}))
    trackers, age, source = read_cached(tmp_path)
    assert len(trackers) == 2
    assert age < 1.0
    assert source == "test"


@pytest.mark.asyncio
async def test_refresh_fetches_and_caches(tmp_path):
    from evatorrent.tracker.fallbacks import refresh_fallback_trackers

    body = "".join(f"udp://t{i}.example:1337/announce\n" for i in range(6))

    class FakeResp:
        status_code = 200
        text = body

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url):
            self.calls.append(url)
            return FakeResp()

        async def aclose(self):
            pass

    trackers = await refresh_fallback_trackers(tmp_path, client=FakeClient())
    assert len(trackers) == 6
    cache_file, _ = cache_paths(tmp_path)
    assert cache_file.exists()


@pytest.mark.asyncio
async def test_refresh_failure_keeps_stale_cache(tmp_path, monkeypatch):
    import evatorrent.tracker.fallbacks as fb

    cache_file, _ = cache_paths(tmp_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("udp://stale.example:1337/announce\n" * 6)

    async def _boom(_base_dir, client=None):
        raise RuntimeError("all mirrors failed")

    monkeypatch.setattr(fb, "refresh_fallback_trackers", _boom)
    # Stale cache (max_age_days=0 forces refresh attempt) is kept on failure.
    trackers = await fb.ensure_fresh_trackers(tmp_path, max_age_days=0)
    assert trackers == ["udp://stale.example:1337/announce"]


@pytest.mark.asyncio
async def test_tracker_quarantine_and_recovery():
    from unittest.mock import AsyncMock

    mgr = TrackerManager(["udp://dead.example:1337/announce", "udp://alsodead.example:80/announce"])
    for t in mgr.trackers:
        t.announce = AsyncMock(side_effect=TimeoutError("nope"))

    ih = b"\x00" * 20
    for _ in range(5):
        resp = await mgr.announce(info_hash=ih)
        assert resp.peers == []
    health = mgr.get_health()
    assert all(h["fail"] == 5 for h in health)
    assert all(h["quarantined"] for h in health)

    # 6th cycle skips quarantined trackers: no new failures recorded.
    resp = await mgr.announce(info_hash=ih)
    assert resp.peers == []
    assert all(h["fail"] == 5 for h in mgr.get_health())

    # Recovery clears quarantine.
    ok_resp = TrackerResponse(interval=60, peers=[])
    for t in mgr.trackers:
        t.announce = AsyncMock(return_value=ok_resp)
    # Force cooldown expiry.
    for st in mgr.stats.values():
        st["last_fail"] = 0.0
    await mgr.announce(info_hash=ih)
    health = mgr.get_health()
    assert all(h["ok"] == 1 for h in health)
    assert not any(h["quarantined"] for h in health)


def test_extra_fallbacks_capped_and_merged():
    extra = [f"udp://extra{i}.example:1337/announce" for i in range(50)]
    mgr = TrackerManager(["udp://base.example:1337/announce"], add_fallbacks=True, extra_fallbacks=extra)
    assert len(mgr.tracker_names) <= 1 + 4 + 30
    assert "udp://base.example:1337/announce" in mgr.tracker_names
