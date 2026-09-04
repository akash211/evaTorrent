"""HTTP and HTTPS tracker client implementation (BEP 0003)."""

from __future__ import annotations

import logging
import socket
import struct
from typing import List, Optional
from urllib.parse import quote_from_bytes

import httpx

from evatorrent.bencoding import bdecode
from evatorrent.tracker import Peer, TrackerResponse

logger = logging.getLogger(__name__)


def parse_compact_peers(data: bytes) -> List[Peer]:
    """Parses binary compact peer list (6 bytes per peer: 4 bytes IP, 2 bytes port)."""
    peers: List[Peer] = []
    if len(data) % 6 != 0:
        logger.warning(f"Malformed compact peer list length: {len(data)}")
    for offset in range(0, len(data) - len(data) % 6, 6):
        ip_bytes = data[offset : offset + 4]
        port = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
        ip = socket.inet_ntoa(ip_bytes)
        peers.append(Peer(ip=ip, port=port))
    return peers


class HttpTracker:
    """Client for HTTP / HTTPS BitTorrent trackers."""

    def __init__(self, announce_url: str):
        self.url = announce_url

    async def announce(
        self,
        info_hash: bytes,
        peer_id: bytes,
        port: int,
        uploaded: int,
        downloaded: int,
        left: int,
        event: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Optional[TrackerResponse]:
        """Sends an announce request to the HTTP tracker."""
        params = [
            f"info_hash={quote_from_bytes(info_hash)}",
            f"peer_id={quote_from_bytes(peer_id)}",
            f"port={port}",
            f"uploaded={uploaded}",
            f"downloaded={downloaded}",
            f"left={left}",
            "compact=1",
            "numwant=100",
        ]
        if event:
            params.append(f"event={event}")

        delimiter = "&" if "?" in self.url else "?"
        full_url = f"{self.url}{delimiter}{'&'.join(params)}"
        headers = {"User-Agent": "evaTorrent/0.3.0"}

        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                resp = await client.get(full_url)
                if resp.status_code != 200:
                    logger.debug(f"HTTP tracker {self.url} responded with status {resp.status_code}")
                    return None

                data = bdecode(resp.content)
                if not isinstance(data, dict):
                    return None

                if b"failure reason" in data:
                    reason = data[b"failure reason"].decode("utf-8", errors="replace")
                    logger.debug(f"Tracker failure from {self.url}: {reason}")
                    return TrackerResponse(interval=1800, peers=[], failure_reason=reason)

                interval = int(data.get(b"interval", 1800))
                complete = int(data.get(b"complete", 0))
                incomplete = int(data.get(b"incomplete", 0))

                peers_raw = data.get(b"peers")
                peers: List[Peer] = []
                if isinstance(peers_raw, bytes):
                    peers = parse_compact_peers(peers_raw)
                elif isinstance(peers_raw, list):
                    for p in peers_raw:
                        if isinstance(p, dict):
                            raw_ip = p.get(b"ip", b"").decode("utf-8", errors="replace")
                            raw_port = int(p.get(b"port", 0))
                            if raw_ip and raw_port > 0:
                                peers.append(Peer(ip=raw_ip, port=raw_port))

                return TrackerResponse(
                    interval=interval,
                    peers=peers,
                    complete=complete,
                    incomplete=incomplete,
                )
        except Exception as e:
            logger.debug(f"Error querying HTTP tracker {self.url}: {e}")
            return None
