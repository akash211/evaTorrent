"""Tracker manager coordinating announces across HTTP and UDP trackers."""

from __future__ import annotations

import asyncio
import logging
import random
import string
from typing import List, Set

from evatorrent.tracker import Peer, TrackerResponse
from evatorrent.tracker.http import HttpTracker
from evatorrent.tracker.udp import UdpTracker

logger = logging.getLogger(__name__)


def generate_peer_id() -> bytes:
    """Generates a standard 20-byte client peer_id (Azureus style: -ET0100-<12 random chars>)."""
    random_part = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"-ET0100-{random_part}".encode("ascii")


class TrackerManager:
    """Coordinates requests to multiple BitTorrent trackers."""

    def __init__(self, tracker_urls: List[str], port: int = 6881):
        self.tracker_urls = tracker_urls
        self.port = port
        self.peer_id = generate_peer_id()
        self.trackers = []

        for url in tracker_urls:
            url_clean = url.strip()
            if url_clean.startswith(("http://", "https://")):
                self.trackers.append(HttpTracker(url_clean))
            elif url_clean.startswith("udp://"):
                self.trackers.append(UdpTracker(url_clean))
            else:
                logger.debug(f"Unsupported tracker scheme for URL: {url}")

    async def announce(
        self,
        info_hash: bytes,
        uploaded: int = 0,
        downloaded: int = 0,
        left: int = 0,
        event: str = "started",
    ) -> TrackerResponse:
        """Announces to all configured trackers concurrently and aggregates discovered peers."""
        if not self.trackers:
            return TrackerResponse(interval=1800, peers=[])

        tasks = [
            tracker.announce(
                info_hash=info_hash,
                peer_id=self.peer_id,
                port=self.port,
                uploaded=uploaded,
                downloaded=downloaded,
                left=left,
                event=event,
            )
            for tracker in self.trackers
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_peers: List[Peer] = []
        seen_peers: Set[str] = set()
        min_interval = 1800
        total_complete = 0
        total_incomplete = 0

        for r in results:
            if isinstance(r, TrackerResponse):
                min_interval = min(min_interval, r.interval if r.interval > 0 else 1800)
                total_complete = max(total_complete, r.complete)
                total_incomplete = max(total_incomplete, r.incomplete)
                for peer in r.peers:
                    key = f"{peer.ip}:{peer.port}"
                    if key not in seen_peers:
                        seen_peers.add(key)
                        all_peers.append(peer)

        return TrackerResponse(
            interval=min_interval,
            peers=all_peers,
            complete=total_complete,
            incomplete=total_incomplete,
        )
