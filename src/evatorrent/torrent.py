"""Torrent metainfo (.torrent) and Magnet link parser."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import parse_qs, unquote

import httpx
from evatorrent.bencoding import bdecode, bencode


@dataclass(frozen=True)
class FileInfo:
    """Represents a single file within a torrent."""
    path: str
    length: int
    offset: int  # Starting byte offset within the torrent payload


class Torrent:
    """Represents the parsed metainfo from a .torrent file."""

    def __init__(self, raw_data: bytes):
        self.raw_data = raw_data
        self.meta_info = bdecode(raw_data)
        if not isinstance(self.meta_info, dict):
            raise ValueError("Root element of .torrent must be a bencoded dictionary")

        if b"info" not in self.meta_info:
            raise ValueError("Metainfo dictionary missing 'info' key")

        self.raw_info = self.meta_info[b"info"]
        # Exact 20-byte SHA-1 hash of the bencoded 'info' dictionary
        self.info_hash = hashlib.sha1(bencode(self.raw_info)).digest()
        self.info_hash_hex = self.info_hash.hex()

        self._parse_trackers()
        self._parse_files()
        self._parse_pieces()

    @classmethod
    def from_file(cls, filepath: Union[str, Path]) -> Torrent:
        path = Path(filepath)
        with path.open("rb") as f:
            return cls(f.read())

    def _parse_trackers(self) -> None:
        self.trackers: List[str] = []

        # Primary announce
        primary = self.meta_info.get(b"announce")
        if primary and isinstance(primary, bytes):
            self.trackers.append(primary.decode("utf-8", errors="replace"))

        # Announce-list (tiers)
        announce_list = self.meta_info.get(b"announce-list")
        if announce_list and isinstance(announce_list, list):
            for tier in announce_list:
                if isinstance(tier, list):
                    for tr in tier:
                        if isinstance(tr, bytes):
                            url = tr.decode("utf-8", errors="replace")
                            if url not in self.trackers:
                                self.trackers.append(url)

    def _parse_files(self) -> None:
        info = self.raw_info
        raw_name = info.get(b"name.utf-8") or info.get(b"name", b"evaTorrent_download")
        self.name = raw_name.decode("utf-8", errors="replace") if isinstance(raw_name, bytes) else str(raw_name)
        self.files: List[FileInfo] = []

        if b"files" in info:
            # Multi-file torrent
            current_offset = 0
            for file_dict in info[b"files"]:
                length = int(file_dict[b"length"])
                path_parts = [
                    p.decode("utf-8", errors="replace") if isinstance(p, bytes) else str(p)
                    for p in file_dict.get(b"path.utf-8") or file_dict[b"path"]
                ]
                rel_path = str(Path(self.name).joinpath(*path_parts))
                self.files.append(FileInfo(path=rel_path, length=length, offset=current_offset))
                current_offset += length
            self.total_length = current_offset
            self.is_multi_file = True
        else:
            # Single-file torrent
            self.total_length = int(info[b"length"])
            self.files.append(FileInfo(path=self.name, length=self.total_length, offset=0))
            self.is_multi_file = False

    def _parse_pieces(self) -> None:
        info = self.raw_info
        self.piece_length = int(info[b"piece length"])
        raw_pieces = info[b"pieces"]
        if not isinstance(raw_pieces, bytes) or len(raw_pieces) % 20 != 0:
            raise ValueError("Malformed pieces field in info dictionary")

        self.piece_hashes: List[bytes] = [
            raw_pieces[i : i + 20] for i in range(0, len(raw_pieces), 20)
        ]
        self.piece_count = len(self.piece_hashes)

        # Sanity check total length against piece count
        expected_pieces = math.ceil(self.total_length / self.piece_length) if self.total_length > 0 else 0
        if self.piece_count != expected_pieces:
            raise ValueError(
                f"Piece count mismatch: {self.piece_count} hashes found, but {expected_pieces} expected "
                f"for total length {self.total_length} with piece size {self.piece_length}"
            )

    def piece_size(self, piece_index: int) -> int:
        """Returns the size of the piece in bytes."""
        if piece_index < 0 or piece_index >= self.piece_count:
            raise IndexError(f"Piece index {piece_index} out of bounds (total {self.piece_count})")
        if piece_index == self.piece_count - 1:
            remainder = self.total_length % self.piece_length
            return remainder if remainder != 0 else self.piece_length
        return self.piece_length

    @property
    def comment(self) -> Optional[str]:
        raw = self.meta_info.get(b"comment")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else None

    @property
    def created_by(self) -> Optional[str]:
        raw = self.meta_info.get(b"created by")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else None


class Magnet:
    """Parses BitTorrent Magnet links (BEP 0009)."""

    def __init__(self, uri: str):
        self.uri = uri
        if not uri.startswith("magnet:?"):
            raise ValueError("Invalid magnet link: must start with 'magnet:?'")

        parsed = parse_qs(uri[8:])
        xt_list = parsed.get("xt", [])
        info_hash = None
        for xt in xt_list:
            if xt.startswith("urn:btih:"):
                raw_hash = xt[len("urn:btih:"):]
                if len(raw_hash) == 40:
                    info_hash = bytes.fromhex(raw_hash)
                elif len(raw_hash) == 32:
                    info_hash = base64.b32decode(raw_hash.upper())
                break

        if not info_hash:
            raise ValueError("Magnet link does not contain a valid urn:btih hash")

        self.info_hash: bytes = info_hash
        self.info_hash_hex: str = info_hash.hex()
        self.name: Optional[str] = parsed.get("dn", [None])[0]
        if self.name:
            self.name = unquote(self.name)

        self.trackers: List[str] = [unquote(tr) for tr in parsed.get("tr", [])]


async def fetch_torrent_from_caches(info_hash_hex: str, timeout: float = 8.0) -> Optional[bytes]:
    """Fetches .torrent file bytes from public torrent caches for a given info hash."""
    clean_hash = info_hash_hex.strip().upper()
    cache_urls = [
        f"https://itorrents.org/torrent/{clean_hash}.torrent",
        f"http://itorrents.org/torrent/{clean_hash}.torrent",
        f"https://torrage.info/torrent.php?h={clean_hash}",
        f"http://torrage.info/torrent.php?h={clean_hash}",
        f"https://btcache.me/torrent/{clean_hash}",
        f"https://cache.torrentstorage.com/gettorrent.php?h={clean_hash}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    async def _fetch_single(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 100:
                decoded = bdecode(resp.content)
                if isinstance(decoded, dict) and b"info" in decoded:
                    return resp.content
        except Exception:
            pass
        return None

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            tasks = [asyncio.create_task(_fetch_single(client, url)) for url in cache_urls]
            for coro in asyncio.as_completed(tasks):
                res = await coro
                if res is not None:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return res
    except Exception:
        pass
    return None

