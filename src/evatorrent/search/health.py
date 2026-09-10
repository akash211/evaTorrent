"""Health scoring engine for filtering stale and dead torrents."""

from __future__ import annotations

import math


def compute_health_score(
    seeders: int,
    leechers: int,
    age_days: int = 0,
    size_bytes: int = 0,
) -> float:
    """Computes a 0.0–1.0 health confidence score for a torrent search result.

    Scoring strategy:
    - Base score from log10(seeders + 1) normalized to ~1.0 at 1000 seeders.
    - Age decay penalty: older torrents with few seeders get penalized.
    - Leecher momentum bonus: active leechers indicate a live swarm.
    - Hard zero for confirmed dead torrents (0 seeds, 0 leechers).
    """
    # Dead torrent: no seeds and no leechers
    if seeders <= 0 and leechers <= 0:
        return 0.0

    # Ghost seeder: 1 seed, 0 leechers, very old
    if seeders <= 1 and leechers == 0 and age_days > 90:
        return 0.02

    # Base score from seeders (log scale, normalized so 1000 seeds ≈ 1.0)
    base = math.log10(seeders + 1) / 3.0  # log10(1001) ≈ 3.0

    # Age decay: penalize old torrents with low seed counts
    if age_days > 0:
        alpha = 0.3
        age_factor = 1.0 / (1.0 + alpha * math.log10(age_days + 1))
    else:
        age_factor = 1.0

    # Leecher momentum bonus: active leechers suggest swarm activity
    if leechers > 0 and seeders > 0:
        leech_bonus = min(0.15, math.log10(leechers + 1) * 0.05)
    else:
        leech_bonus = 0.0

    score = (base * age_factor) + leech_bonus

    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, score))
