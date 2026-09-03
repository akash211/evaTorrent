"""BitTorrent Peer Wire Protocol (BEP 0003) message packing and unpacking."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Union

# Protocol constants
PSTR = b"BitTorrent protocol"
PSTRLEN = len(PSTR)
HANDSHAKE_LEN = 1 + PSTRLEN + 8 + 20 + 20  # 68 bytes

# Message IDs
ID_CHOKE = 0
ID_UNCHOKE = 1
ID_INTERESTED = 2
ID_NOT_INTERESTED = 3
ID_HAVE = 4
ID_BITFIELD = 5
ID_REQUEST = 6
ID_PIECE = 7
ID_CANCEL = 8
ID_PORT = 9


@dataclass
class Handshake:
    info_hash: bytes
    peer_id: bytes
    reserved: bytes = b"\x00" * 8

    def encode(self) -> bytes:
        return struct.pack(f">B{PSTRLEN}s8s20s20s", PSTRLEN, PSTR, self.reserved, self.info_hash, self.peer_id)

    @classmethod
    def decode(cls, data: bytes) -> Handshake:
        if len(data) < HANDSHAKE_LEN:
            raise ValueError(f"Handshake must be at least {HANDSHAKE_LEN} bytes, got {len(data)}")
        pstrlen, pstr, reserved, info_hash, peer_id = struct.unpack(
            f">B{PSTRLEN}s8s20s20s", data[:HANDSHAKE_LEN]
        )
        if pstrlen != PSTRLEN or pstr != PSTR:
            raise ValueError(f"Invalid protocol in handshake: {pstr!r}")
        return cls(info_hash=info_hash, peer_id=peer_id, reserved=reserved)


@dataclass
class PeerMessage:
    """Base class for BitTorrent peer wire messages."""
    def encode(self) -> bytes:
        raise NotImplementedError


@dataclass
class KeepAlive(PeerMessage):
    def encode(self) -> bytes:
        return struct.pack(">I", 0)


@dataclass
class Choke(PeerMessage):
    def encode(self) -> bytes:
        return struct.pack(">IB", 1, ID_CHOKE)


@dataclass
class Unchoke(PeerMessage):
    def encode(self) -> bytes:
        return struct.pack(">IB", 1, ID_UNCHOKE)


@dataclass
class Interested(PeerMessage):
    def encode(self) -> bytes:
        return struct.pack(">IB", 1, ID_INTERESTED)


@dataclass
class NotInterested(PeerMessage):
    def encode(self) -> bytes:
        return struct.pack(">IB", 1, ID_NOT_INTERESTED)


@dataclass
class Have(PeerMessage):
    piece_index: int

    def encode(self) -> bytes:
        return struct.pack(">IBI", 5, ID_HAVE, self.piece_index)


@dataclass
class Bitfield(PeerMessage):
    data: bytes

    def encode(self) -> bytes:
        return struct.pack(f">IB{len(self.data)}s", 1 + len(self.data), ID_BITFIELD, self.data)

    def has_piece(self, piece_index: int) -> bool:
        byte_index = piece_index // 8
        if byte_index >= len(self.data):
            return False
        bit_index = 7 - (piece_index % 8)
        return bool((self.data[byte_index] >> bit_index) & 1)


@dataclass
class Request(PeerMessage):
    index: int
    begin: int
    length: int

    def encode(self) -> bytes:
        return struct.pack(">IBIII", 13, ID_REQUEST, self.index, self.begin, self.length)


@dataclass
class Piece(PeerMessage):
    index: int
    begin: int
    block: bytes

    def encode(self) -> bytes:
        return struct.pack(
            f">IBII{len(self.block)}s",
            9 + len(self.block),
            ID_PIECE,
            self.index,
            self.begin,
            self.block,
        )


@dataclass
class Cancel(PeerMessage):
    index: int
    begin: int
    length: int

    def encode(self) -> bytes:
        return struct.pack(">IBIII", 13, ID_CANCEL, self.index, self.begin, self.length)


@dataclass
class Port(PeerMessage):
    port: int

    def encode(self) -> bytes:
        return struct.pack(">IBH", 3, ID_PORT, self.port)


def decode_message(payload_length: int, msg_id: int, payload: bytes) -> Optional[PeerMessage]:
    """Parses a single peer message given its length, ID, and raw payload."""
    if msg_id == ID_CHOKE:
        return Choke()
    elif msg_id == ID_UNCHOKE:
        return Unchoke()
    elif msg_id == ID_INTERESTED:
        return Interested()
    elif msg_id == ID_NOT_INTERESTED:
        return NotInterested()
    elif msg_id == ID_HAVE:
        if len(payload) != 4:
            return None
        return Have(struct.unpack(">I", payload)[0])
    elif msg_id == ID_BITFIELD:
        return Bitfield(payload)
    elif msg_id == ID_REQUEST:
        if len(payload) != 12:
            return None
        index, begin, length = struct.unpack(">III", payload)
        return Request(index=index, begin=begin, length=length)
    elif msg_id == ID_PIECE:
        if len(payload) < 8:
            return None
        index, begin = struct.unpack(">II", payload[:8])
        block = payload[8:]
        return Piece(index=index, begin=begin, block=block)
    elif msg_id == ID_CANCEL:
        if len(payload) != 12:
            return None
        index, begin, length = struct.unpack(">III", payload)
        return Cancel(index=index, begin=begin, length=length)
    elif msg_id == ID_PORT:
        if len(payload) != 2:
            return None
        port = struct.unpack(">H", payload)[0]
        return Port(port=port)
    return None
