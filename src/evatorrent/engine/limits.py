"""Central resource limits for safe operation on small hosts (LXC / Proxmox).

All values are env-overridable so homelab operators can tune without code changes.
Defaults are chosen for a 4-core / 6 GB LXC that must survive 100 GB+ torrents.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


# Max concurrent peer connections per torrent (was hardcoded 50).
MAX_PEERS = _env_int("EVA_MAX_PEERS", 50)
# Pipelined block requests per peer (was hardcoded 10).
PIPELINE_PER_PEER = _env_int("EVA_PIPELINE_PER_PEER", 10)
# Max pieces with in-flight blocks held in RAM at once.
MAX_ONGOING_PIECES = _env_int("EVA_MAX_ONGOING_PIECES", 32)
# Peer stream read buffer per connection (was 10 MB).
STREAM_BUFFER_LIMIT = _env_int("EVA_STREAM_BUFFER_LIMIT", 2 * 1024 * 1024)
# Telemetry broadcast interval in seconds (was 0.8s).
TELEMETRY_INTERVAL_SECS = _env_float("EVA_TELEMETRY_SECS", 0.8)
# Startup verification mode: "full" | "quick" | "off".
#  - full: hash every piece (correct but reads entire dataset, hot on 155 GB).
#  - quick: size/existence check + hash only pieces that look complete (default).
#  - off: trust DB progress, verify lazily as pieces complete.
VERIFY_ON_STARTUP = _env_str("EVA_VERIFY_ON_STARTUP", "quick").lower()
# Torrents at/above this size get automatic down-tuning (fewer peers/pipeline).
LARGE_TORRENT_BYTES = _env_int("EVA_LARGE_TORRENT_BYTES", 20 * 1024 * 1024 * 1024)
# Hard cap for piece-count sorts: beyond this, rarest-first uses sampling.
RAREST_SORT_CAP = _env_int("EVA_RAREST_SORT_CAP", 2000)
# Seconds between rarity-count recomputes (avoids O(P*N) sort on every request).
RAREST_REFRESH_SECS = _env_float("EVA_RAREST_REFRESH_SECS", 10.0)


def effective_peer_settings(total_bytes: int) -> tuple[int, int]:
    """Returns (max_peers, pipeline) auto-tuned for torrent size.

    Large torrents get fewer peers/pipeline so a 155 GB download cannot
    spawn 50 connections x 10 in-flight blocks and OOM a 6 GB LXC.
    Explicit env vars always win; auto-tune only clamps downward.
    """
    max_peers = MAX_PEERS
    pipeline = PIPELINE_PER_PEER
    if total_bytes >= LARGE_TORRENT_BYTES:
        max_peers = min(max_peers, _env_int("EVA_LARGE_MAX_PEERS", 15))
        pipeline = min(pipeline, _env_int("EVA_LARGE_PIPELINE", 4))
    return max(1, max_peers), max(1, pipeline)
