import logging
import time
from pathlib import Path

from evatorrent.engine import monitor as mon


def test_cgroup_helpers_never_crash():
    # On any platform these return a value or None — never raise.
    rss = mon.read_rss_mb()
    assert rss is None or rss > 0
    limit = mon.cgroup_memory_limit_mb()
    assert limit is None or limit > 0
    kills = mon.cgroup_oom_kills()
    assert kills is None or kills >= 0
    throttle = mon.cgroup_cpu_throttled_secs()
    assert throttle is None or throttle >= 0


def test_unclean_shutdown_detected(tmp_path, caplog):
    (tmp_path / ".heartbeat").write_text(f"{time.time():.0f}\n")
    with caplog.at_level(logging.WARNING, logger="evatorrent.engine.monitor"):
        mon.check_previous_shutdown(tmp_path)
    assert "did NOT shut down cleanly" in caplog.text


def test_clean_shutdown_quiet(tmp_path, caplog):
    (tmp_path / ".heartbeat").write_text(f"{time.time():.0f}\n")
    mon.mark_clean_shutdown(tmp_path)
    assert (tmp_path / ".clean_shutdown").exists()
    assert not (tmp_path / ".heartbeat").exists()
    with caplog.at_level(logging.WARNING, logger="evatorrent.engine.monitor"):
        mon.check_previous_shutdown(tmp_path)
    assert "did NOT shut down cleanly" not in caplog.text


def test_stale_heartbeat_treated_as_fresh(tmp_path, caplog):
    (tmp_path / ".heartbeat").write_text(f"{time.time() - 100000:.0f}\n")
    with caplog.at_level(logging.INFO, logger="evatorrent.engine.monitor"):
        mon.check_previous_shutdown(tmp_path)
    assert "did NOT shut down cleanly" not in caplog.text


def test_boot_line_logs_without_crash(caplog, tmp_path):
    with caplog.at_level(logging.INFO, logger="evatorrent.engine.monitor"):
        mon.log_cgroup_boot_line()
    # Either stats or a debug-only skip; must not raise and logs something at INFO only if available.
    assert True


def test_heartbeat_roundtrip(tmp_path):
    mon.write_heartbeat(tmp_path)
    content = (tmp_path / ".heartbeat").read_text().strip()
    assert abs(float(content) - time.time()) < 5
