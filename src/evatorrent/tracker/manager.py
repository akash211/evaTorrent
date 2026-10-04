"""Tracker manager coordinating announces across HTTP and UDP trackers."""

from __future__ import annotations

import asyncio
import logging
import random
import string
import time
from typing import Dict, List, Optional, Set

from evatorrent.tracker import Peer, TrackerResponse
from evatorrent.tracker.http import HttpTracker
from evatorrent.tracker.udp import UdpTracker

logger = logging.getLogger(__name__)


def generate_peer_id() -> bytes:
    """Generates a standard 20-byte client peer_id (Azureus style: -ET0100-<12 random chars>)."""
    random_part = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"-ET0100-{random_part}".encode("ascii")


DEFAULT_FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
]

# Quarantine a tracker after this many consecutive failures; retry after cooldown.
FAIL_QUARANTINE_THRESHOLD = 5
FAIL_QUARANTINE_COOLDOWN_SECS = 600.0


class TrackerManager:
    """Coordinates requests to multiple BitTorrent trackers."""

    def __init__(
        self,
        tracker_urls: List[str],
        port: int = 6881,
        add_fallbacks: bool = False,
        extra_fallbacks: Optional[List[str]] = None,
    ):
        self.tracker_urls = list(tracker_urls)
        if add_fallbacks:
            for fb in DEFAULT_FALLBACK_TRACKERS:
                if fb not in self.tracker_urls:
                    self.tracker_urls.append(fb)
        if extra_fallbacks:
            added = 0
            for fb in extra_fallbacks:
                if fb not in self.tracker_urls and len(self.tracker_urls) < len(tracker_urls) + 30:
                    self.tracker_urls.append(fb)
                    added += 1
            if added:
                logger.debug(f"Added {added} auto-updated fallback trackers")

        self.port = port
        self.peer_id = generate_peer_id()
        self.trackers = []
        self.tracker_names: List[str] = []

        for url in self.tracker_urls:
            url_clean = url.strip()
            if url_clean.startswith(("http://", "https://")):
                self.trackers.append(HttpTracker(url_clean))
                self.tracker_names.append(url_clean)
            elif url_clean.startswith("udp://"):
                self.trackers.append(UdpTracker(url_clean))
                self.tracker_names.append(url_clean)
            else:
                logger.debug(f"Unsupported tracker scheme for URL: {url}")

        # Health scoring: url -> {ok, fail, consec_fail, last_ok, last_fail}.
        self.stats: Dict[str, Dict[str, float]] = {}
        for name in self.tracker_names:
            self.stats[name] = {"ok": 0, "fail": 0, "consec_fail": 0, "last_ok": 0.0, "last_fail": 0.0}

    async def announce(
        self,
        info_hash: bytes,
        uploaded: int = 0,
        downloaded: int = 0,
        left: int = 0,
        event: str = "started",
    ) -> TrackerResponse:
        """Announces to healthy trackers and aggregates discovered peers.

        Trackers with >=5 consecutive failures are quarantined for 10 minutes
        (still retried after cooldown) so dead trackers stop costing time
        every cycle while recoveries are picked up automatically.
        """
        if not self.trackers:
            return TrackerResponse(interval=1800, peers=[])

        now = time.time()
        active: List[int] = []
        skipped = 0
        for i, name in enumerate(self.tracker_names):
            st = self.stats[name]
            if st["consec_fail"] >= FAIL_QUARANTINE_THRESHOLD and now - st["last_fail"] < FAIL_QUARANTINE_COOLDOWN_SECS:
                skipped += 1
                continue
            active.append(i)
        if skipped:
            logger.debug(f"Skipping {skipped} quarantined trackers this cycle")

        tasks = [
            self.trackers[i].announce(
                info_hash=info_hash,
                peer_id=self.peer_id,
                port=self.port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
                event=event,
            )
            for i in active
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_peers: List[Peer] = []
        seen_peers: Set[str] = set()
        min_interval = 1800
        total_complete = 0
        total_incomplete = 0

        for idx, r in zip(active, results):
            name = self.tracker_names[idx]
            st = self.stats[name]
            if isinstance(r, TrackerResponse):
                was_quarantined = st["consec_fail"] >= FAIL_QUARANTINE_THRESHOLD
                st["ok"] += 1
                st["consec_fail"] = 0
                st["last_ok"] = now
                if was_quarantined:
                    logger.info(f"[TRACKERS] Recovered: {name} answering again")
                min_interval = min(min_interval, r.interval if r.interval > 0 else 1800)
                total_complete = max(total_complete, r.complete)
                total_incomplete = max(total_incomplete, r.incomplete)
                for peer in r.peers:
                    key = f"{peer.ip}:{peer.port}"
                    if key not in seen_peers:
                        seen_peers.add(key)
                        all_peers.append(peer)
            else:
                st["fail"] += 1
                st["consec_fail"] += 1
                st["last_fail"] = now
                if st["consec_fail"] == FAIL_QUARANTINE_THRESHOLD:
                    logger.warning(f"[TRACKERS] Quarantining dead tracker for 10m: {name} ({r})")
                else:
                    logger.debug(f"Tracker {name} failed: {r}")

        return TrackerResponse(
            interval=min_interval,
            peers=all_peers,
            complete=total_complete,
            incomplete=total_incomplete,
        )

    def get_health(self) -> List[dict]:
        """Per-tracker health snapshot for the API (ok/fail counters, quarantined flag)."""
        now = time.time()
        out = []
        for name in self.tracker_names:
            st = self.stats[name]
            quarantined = (
                st["consec_fail"] >= FAIL_QUARANTINE_THRESHOLD
                and now - st["last_fail"] < FAIL_QUARANTINE_COOLDOWN_SECS
            )
            out.append(
                {
                    "url": name,
                    "ok": int(st["ok"]),
                    "fail": int(st["fail"]),
                    "quarantined": quarantined,
                    "last_ok": st["last_ok"] or None,
                }
            )
        return out
