from datetime import datetime

import pytest

from evatorrent.engine.speed import (
    match_schedule,
    profile_limit,
    validate_profiles,
    validate_schedule,
)


def test_validate_profiles():
    out = validate_profiles([{"name": "Day", "limit": 5 * 1024 * 1024}, {"name": "Night", "limit": None}])
    assert out[0]["limit"] == 5242880
    assert out[1]["limit"] is None
    with pytest.raises(ValueError):
        validate_profiles([])
    with pytest.raises(ValueError):
        validate_profiles([{"name": "A", "limit": 1}, {"name": "a", "limit": 2}])
    with pytest.raises(ValueError):
        validate_profiles([{"name": "A", "limit": -5}])
    with pytest.raises(ValueError):
        validate_profiles([{"name": "  "}])


def test_validate_schedule():
    profiles = ["Day", "Night"]
    sched = validate_schedule(
        [
            {"profile": "Day", "start": "07:00", "end": "23:00"},
            {"profile": "Night", "start": "23:00", "end": "07:00", "days": [5, 6]},
        ],
        profiles,
    )
    assert len(sched) == 2
    assert sched[1]["days"] == [5, 6]
    with pytest.raises(ValueError):
        validate_schedule([{"profile": "Nope", "start": "07:00", "end": "08:00"}], profiles)
    with pytest.raises(ValueError):
        validate_schedule([{"profile": "Day", "start": "7am", "end": "08:00"}], profiles)
    with pytest.raises(ValueError):
        validate_schedule([{"profile": "Day", "start": "07:00", "end": "08:00", "days": [9]}], profiles)


def test_match_schedule_windows():
    sched = [
        {"profile": "Day", "start": "07:00", "end": "23:00", "days": [0, 1, 2, 3, 4, 5, 6]},
        {"profile": "Night", "start": "23:00", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    ]
    assert match_schedule(sched, datetime(2026, 1, 5, 12, 0)) == "Day"  # Monday noon
    assert match_schedule(sched, datetime(2026, 1, 5, 23, 30)) == "Night"
    assert match_schedule(sched, datetime(2026, 1, 6, 2, 0)) == "Night"  # overnight
    assert match_schedule(sched, datetime(2026, 1, 5, 7, 0)) == "Day"  # boundary inclusive
    weekend = [{"profile": "W", "start": "00:00", "end": "23:59", "days": [5, 6]}]
    assert match_schedule(weekend, datetime(2026, 1, 5, 12, 0)) is None  # Monday
    assert match_schedule(weekend, datetime(2026, 1, 10, 12, 0)) == "W"  # Saturday


def test_profile_limit_lookup():
    profiles = [{"name": "Day", "limit": 100}, {"name": "Night", "limit": None}]
    assert profile_limit(profiles, "day") == 100
    assert profile_limit(profiles, "NIGHT") is None
    assert profile_limit(profiles, "missing") is None


def test_manager_global_throttle_and_schedule(tmp_path):
    import tempfile
    from pathlib import Path

    from evatorrent.engine.manager import EngineManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = EngineManager(default_download_dir=Path(tmpdir))
        assert mgr.is_globally_throttled() is False
        mgr.set_global_download_limit(1000)

        class FakeSession:
            download_speed = 2000.0

        mgr.sessions["x"] = FakeSession()
        assert mgr.is_globally_throttled() is True
        FakeSession.download_speed = 10.0
        assert mgr.is_globally_throttled() is False
        mgr.sessions.clear()

        # Schedule evaluation applies caps.
        mgr.speed_profiles = [{"name": "Day", "limit": 1000}, {"name": "Night", "limit": None}]
        mgr.speed_schedule = [
            {"profile": "Day", "start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]}
        ]
        mgr.evaluate_speed_schedule(force=True)
        assert mgr.active_speed_profile == "Day"
        assert mgr.global_download_limit == 1000

        # Manual override wins; clearing returns to schedule.
        mgr.set_speed_override("Night")
        assert mgr.global_download_limit is None
        mgr.set_speed_override(None)
        mgr.evaluate_speed_schedule(force=True)
        assert mgr.active_speed_profile == "Day"

        with pytest.raises(ValueError):
            mgr.set_speed_override("Nope")


@pytest.mark.asyncio
async def test_speed_api_endpoints():
    from httpx import ASGITransport, AsyncClient

    from evatorrent.web.app import app, auth_config, session_manager

    auth_config.set_admin_email("admin@example.com")
    token = session_manager.create_token("admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
        resp = await client.get("/api/speed")
        assert resp.status_code == 200
        body = resp.json()
        assert "profiles" in body and "server_time" in body

        # Invalid config rejected.
        resp = await client.put("/api/speed", json={"profiles": []})
        assert resp.status_code == 400
        resp = await client.put("/api/speed", json={"schedule": [{"profile": "Nope", "start": "07:00", "end": "08:00"}]})
        assert resp.status_code == 400

        # Valid profiles saved; unknown override rejected.
        resp = await client.put(
            "/api/speed",
            json={"profiles": [{"name": "Day", "limit": 1000}, {"name": "Night", "limit": None}]},
        )
        assert resp.status_code == 200
        resp = await client.post("/api/speed/override", json={"profile": "Nope"})
        assert resp.status_code == 400
        resp = await client.post("/api/speed/override", json={"profile": "Day"})
        assert resp.status_code == 200
        assert resp.json()["active_profile"] == "Day"
        resp = await client.post("/api/speed/override", json={"profile": None})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_notify_no_smtp_no_crash(caplog):
    import logging

    from evatorrent.web.app import auth_config, notify_torrent_completed

    auth_config.set_admin_email("admin@example.com")

    class FakeTorrent:
        name = "Demo"
        total_length = 1000
        info_hash_hex = "ab" * 20

    class FakePM:
        bytes_downloaded = 1000
        bytes_uploaded = 10

    class FakeSession:
        torrent = FakeTorrent()
        piece_manager = FakePM()

    with caplog.at_level(logging.WARNING, logger="evaTorrent.web"):
        await notify_torrent_completed(FakeSession())
    # Either skipped (no SMTP in test env) or attempted — must not raise.
    assert True
