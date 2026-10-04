"""Resource-pressure telemetry and crash forensics for small hosts.

Answers "what killed evaTorrent?" after the fact and warns *before* death:

- RSS monitor: logs process memory vs the container cgroup limit every
  EVA_RSS_LOG_SECS (default 60s); WARN at 80%, ERROR at 90% with swarm
  details so docker logs show the climb that preceded an OOM-kill.
- Event-loop lag: a blocked loop (sync hashing/sorting) shows up as sleep
  drift; WARN when a tick drifts > 3s.
- Cgroup counters at startup: oom_kill + cpu-throttle readings (they survive
  container *restarts*, unlike logs which rotate) — after an OOM the boot
  line states how many kills this container has seen.
- Heartbeat file: written every EVA_HEARTBEAT_SECS into EVA_DATA_DIR. On
  boot, a fresh heartbeat with no clean-shutdown marker means the previous
  run died uncleanly (OOM-kill/crash/host loss) — logged as a warning with
  the last-seen timestamp. All file writes are best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from evatorrent.engine.manager import EngineManager

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def read_rss_mb() -> Optional[float]:
    """Current process RSS in MB (procfs; None when unavailable)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def _read_cgroup_file(*names: str) -> Optional[str]:
    for name in names:
        for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/memory"):
            try:
                p = Path(base) / name
                if p.exists():
                    return p.read_text().strip()
            except Exception:
                continue
    return None


def cgroup_memory_limit_mb() -> Optional[float]:
    """Container memory cap in MB (None = unlimited/unavailable)."""
    raw = _read_cgroup_file("memory.max", "memory.limit_in_bytes")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0 or value >= 1 << 62:  # "max" on cgroup v2
        return None
    return value / (1024.0 * 1024.0)


def cgroup_oom_kills() -> Optional[int]:
    """OOM-kill counter for this container's cgroup (survives restarts)."""
    raw = _read_cgroup_file("memory.events", "memory.oom_control")
    if not raw:
        return None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("oom_kill", "oom_kill_disable"):
            try:
                if parts[0] == "oom_kill":
                    return int(parts[1])
            except ValueError:
                return None
    return None


def cgroup_cpu_throttled_secs() -> Optional[float]:
    """Total CPU-throttled time for this container (None when unavailable)."""
    raw = _read_cgroup_file("cpu.stat")
    if not raw:
        return None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "throttled_usec":
            try:
                return int(parts[1]) / 1e6
            except ValueError:
                return None
    return None


def log_cgroup_boot_line() -> None:
    """One-line boot forensics: past OOM kills + CPU throttling of this container."""
    oom = cgroup_oom_kills()
    throttle = cgroup_cpu_throttled_secs()
    limit = cgroup_memory_limit_mb()
    if oom is None and throttle is None and limit is None:
        logger.debug("[RESOURCE] cgroup stats unavailable (non-Linux or no cgroupfs)")
        return
    logger.info(
        "[RESOURCE] container stats at boot: oom_kills=%s cpu_throttled=%ss mem_limit=%s",
        oom if oom is not None else "?",
        f"{throttle:.0f}" if throttle is not None else "?",
        f"{limit:.0f}MB" if limit is not None else "unlimited/?",
    )
    if oom:
        logger.warning(
            "[RESOURCE] This container was OOM-killed %d time(s) before (see `docker events`/`dmesg`). "
            "If it happens again, lower EVA_MAX_PEERS / EVA_MAX_ONGOING_PIECES.",
            oom,
        )


def check_previous_shutdown(data_dir: Path) -> None:
    """Warns when the previous run never shut down cleanly (crash/OOM/host loss)."""
    try:
        hb = data_dir / ".heartbeat"
        clean = data_dir / ".clean_shutdown"
        if not hb.exists():
            return  # first boot ever (or heartbeat unwritable) — nothing to infer
        try:
            last = float(hb.read_text().strip().split()[0])
        except Exception:
            return
        age = time.time() - last
        clean_exists = clean.exists()
        try:
            if clean_exists:
                clean.unlink()
        except Exception:
            pass
        if clean_exists:
            logger.info("[RESOURCE] Previous run shut down cleanly.")
            return
        if age < 24 * 3600:
            logger.warning(
                "[RESOURCE] Previous run did NOT shut down cleanly — last heartbeat %.0fs ago "
                "(likely OOM-kill, crash, or host/container loss). Check `docker events` and host dmesg.",
                age,
            )
        else:
            logger.info(f"[RESOURCE] Stale heartbeat ({age / 3600:.1f}h old); treating as fresh start.")
    except Exception as e:
        logger.debug(f"[RESOURCE] Shutdown forensics skipped: {e}")


def mark_clean_shutdown(data_dir: Path) -> None:
    """Records an orderly shutdown so the next boot stays quiet."""
    try:
        (data_dir / ".clean_shutdown").write_text(f"{time.time():.0f}\n")
        try:
            (data_dir / ".heartbeat").unlink()
        except FileNotFoundError:
            pass
    except Exception as e:
        logger.debug(f"[RESOURCE] Could not write clean-shutdown marker: {e}")


def write_heartbeat(data_dir: Path) -> None:
    try:
        (data_dir / ".heartbeat").write_text(f"{time.time():.0f}\n")
    except Exception:
        pass


class ResourceMonitor:
    """Periodic RSS + loop-lag watchdog. One instance per EngineManager."""

    def __init__(self, manager: EngineManager, data_dir: Optional[Path] = None):
        self.manager = manager
        self.data_dir = Path(data_dir) if data_dir else None
        self.interval = _env_float("EVA_RSS_LOG_SECS", 60.0)
        self.heartbeat_interval = _env_float("EVA_HEARTBEAT_SECS", 30.0)
        self._task: Optional[asyncio.Task] = None
        self._last_beat = 0.0

    def start(self) -> None:
        if self.interval <= 0:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def _swarm_summary(self) -> Dict[str, float]:
        sessions = list(self.manager.sessions.values())
        return {
            "torrents": len(sessions),
            "peers": sum(len(s.active_peers) for s in sessions),
            "ongoing": sum(len(s.piece_manager.ongoing_pieces) for s in sessions),
        }

    async def _run(self) -> None:
        limit = cgroup_memory_limit_mb()
        try:
            while True:
                tick_start = time.monotonic()
                await asyncio.sleep(self.interval)
                lag = time.monotonic() - tick_start - self.interval
                rss = read_rss_mb()
                swarm = self._swarm_summary()
                pct = (rss / limit * 100.0) if (rss and limit) else None
                msg = (
                    "[RESOURCE] rss=%s mem=%s loop_lag=%.1fs torrents=%d peers=%d ongoing_pieces=%d" % (
                        f"{rss:.0f}MB" if rss else "?",
                        f"{pct:.0f}% of {limit:.0f}MB" if pct is not None and limit else "?",
                        max(0.0, lag),
                        swarm["torrents"],
                        swarm["peers"],
                        swarm["ongoing"],
                    )
                )
                if pct is not None and pct >= 90:
                    logger.error(msg + " — CRITICAL: near the container memory cap, OOM-kill likely. Pause big torrents!")
                elif pct is not None and pct >= 80:
                    logger.warning(msg + " — high memory pressure.")
                elif lag > 3.0:
                    logger.warning(msg + " — event loop blocked (sync hashing/sorting?).")
                else:
                    logger.info(msg)
                if self.data_dir and time.time() - self._last_beat >= self.heartbeat_interval:
                    self._last_beat = time.time()
                    write_heartbeat(self.data_dir)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[RESOURCE] Monitor loop died: {e}")
