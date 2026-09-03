import socket
import struct
import pytest
from evatorrent.tracker.http import parse_compact_peers
from evatorrent.tracker.manager import generate_peer_id, TrackerManager
from evatorrent.tracker.http import HttpTracker
from evatorrent.tracker.udp import UdpTracker


def test_generate_peer_id():
    peer_id = generate_peer_id()
    assert isinstance(peer_id, bytes)
    assert len(peer_id) == 20
    assert peer_id.startswith(b"-ET0100-")


def test_parse_compact_peers():
    # Peer 1: 127.0.0.1:6881
    ip1 = socket.inet_aton("127.0.0.1")
    port1 = struct.pack(">H", 6881)
    # Peer 2: 192.168.1.50:51413
    ip2 = socket.inet_aton("192.168.1.50")
    port2 = struct.pack(">H", 51413)

    raw_peers = ip1 + port1 + ip2 + port2
    peers = parse_compact_peers(raw_peers)

    assert len(peers) == 2
    assert peers[0].ip == "127.0.0.1"
    assert peers[0].port == 6881
    assert peers[1].ip == "192.168.1.50"
    assert peers[1].port == 51413


def test_tracker_manager_init():
    urls = [
        "http://tracker.example.com/announce",
        "https://securetracker.example.com/announce",
        "udp://tracker.opentrackr.org:1337/announce",
        "ftp://invalid.com",
    ]
    mgr = TrackerManager(urls)
    assert len(mgr.trackers) == 3
    assert isinstance(mgr.trackers[0], HttpTracker)
    assert isinstance(mgr.trackers[1], HttpTracker)
    assert isinstance(mgr.trackers[2], UdpTracker)
