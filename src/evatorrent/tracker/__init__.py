"""evaTorrent tracker module supporting HTTP, HTTPS, and UDP (BEP 15) trackers."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Peer:
    ip: str
    port: int

    def __str__(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class TrackerResponse:
    interval: int
    peers: List[Peer]
    complete: int = 0  # Seeders
    incomplete: int = 0  # Leechers
    failure_reason: Optional[str] = None
