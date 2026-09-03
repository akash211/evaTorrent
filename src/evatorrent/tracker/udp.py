"""UDP tracker client implementation (BEP 0015)."""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
from typing import Optional
from urllib.parse import urlparse

from evatorrent.tracker import Peer, TrackerResponse
from evatorrent.tracker.http import parse_compact_peers

logger = logging.getLogger(__name__)

PROTOCOL_MAGIC = 0x41727101980  # Magic constant for BitTorrent UDP Tracker
ACTION_CONNECT = 0
ACTION_ANNOUNCE = 1
ACTION_ERROR = 3

EVENT_NONE = 0
EVENT_COMPLETED = 1
EVENT_STARTED = 2
EVENT_STOPPED = 3

EVENTS_MAP = {
    "started": EVENT_STARTED,
    "completed": EVENT_COMPLETED,
    "stopped": EVENT_STOPPED,
}


class UdpTrackerProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.queue.put_nowait(data)

    def error_received(self, exc):
        logger.debug(f"UDP tracker datagram error: {exc}")


class UdpTracker:
    """Client for UDP BitTorrent trackers conforming to BEP 15."""

    def __init__(self, announce_url: str):
        self.url = announce_url
        parsed = urlparse(announce_url)
        self.hostname = parsed.hostname or ""
        self.port = parsed.port or 80

    async def announce(
        self,
        info_hash: bytes,
        peer_id: bytes,
        port: int,
        uploaded: int,
        downloaded: int,
        left: int,
        event: Optional[str] = None,
        timeout: float = 8.0,
    ) -> Optional[TrackerResponse]:
        if not self.hostname:
            return None

        loop = asyncio.get_running_loop()
        try:
            # Resolve IP to avoid blocking in connect
            addr_info = await loop.getaddrinfo(self.hostname, self.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            if not addr_info:
                return None
            remote_addr = addr_info[0][4]
        except Exception as e:
            logger.debug(f"Failed to resolve UDP tracker host {self.hostname}: {e}")
            return None

        transport, protocol = None, None
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: UdpTrackerProtocol(),
                remote_addr=remote_addr,
            )

            # Step 1: Connect Request
            transaction_id = random.randint(0, 0x7FFFFFFF)
            connect_req = struct.pack(">QII", PROTOCOL_MAGIC, ACTION_CONNECT, transaction_id)
            transport.sendto(connect_req)

            # Await Connect Response
            raw_conn_resp = await asyncio.wait_for(protocol.queue.get(), timeout=timeout / 2)
            if len(raw_conn_resp) < 16:
                return None

            action, resp_trans_id, connection_id = struct.unpack(">IIQ", raw_conn_resp[:16])
            if action != ACTION_CONNECT or resp_trans_id != transaction_id:
                return None

            # Step 2: Announce Request
            event_code = EVENTS_MAP.get(event, EVENT_NONE) if event else EVENT_NONE
            announce_trans_id = random.randint(0, 0x7FFFFFFF)
            key = random.randint(0, 0x7FFFFFFF)
            num_want = 50

            announce_req = struct.pack(
                ">QII20s20sQQQIIIiH",
                connection_id,
                ACTION_ANNOUNCE,
                announce_trans_id,
                info_hash,
                peer_id,
                downloaded,
                left,
                uploaded,
                event_code,
                0,  # IP address (default 0)
                key,
                num_want,
                port,
            )
            transport.sendto(announce_req)

            # Await Announce Response
            raw_ann_resp = await asyncio.wait_for(protocol.queue.get(), timeout=timeout / 2)
            if len(raw_ann_resp) < 20:
                return None

            action, resp_trans_id, interval, leechers, seeders = struct.unpack(">IIIII", raw_ann_resp[:20])
            if action == ACTION_ERROR:
                err_msg = raw_ann_resp[8:].decode("utf-8", errors="replace")
                logger.debug(f"UDP tracker returned error: {err_msg}")
                return None

            if action != ACTION_ANNOUNCE or resp_trans_id != announce_trans_id:
                return None

            peers_bytes = raw_ann_resp[20:]
            peers = parse_compact_peers(peers_bytes)
            return TrackerResponse(
                interval=interval,
                peers=peers,
                complete=seeders,
                incomplete=leechers,
            )

        except (asyncio.TimeoutError, TimeoutError):
            logger.debug(f"Timeout querying UDP tracker {self.url}")
            return None
        except Exception as e:
            logger.debug(f"Error querying UDP tracker {self.url}: {e}")
            return None
        finally:
            if transport is not None:
                transport.close()
