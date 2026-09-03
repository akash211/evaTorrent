import asyncio
import pytest
from evatorrent.peer.protocol import (
    Bitfield,
    Cancel,
    Choke,
    Handshake,
    Have,
    Interested,
    KeepAlive,
    NotInterested,
    Piece,
    Port,
    Request,
    Unchoke,
    decode_message,
)
from evatorrent.peer.stream import PeerStreamIterator


def test_handshake_roundtrip():
    info_hash = b"01234567890123456789"
    peer_id = b"-ET0100-abcdef123456"
    hs = Handshake(info_hash=info_hash, peer_id=peer_id)
    encoded = hs.encode()
    assert len(encoded) == 68

    decoded = Handshake.decode(encoded)
    assert decoded.info_hash == info_hash
    assert decoded.peer_id == peer_id


def test_simple_messages():
    assert KeepAlive().encode() == b"\x00\x00\x00\x00"
    assert Choke().encode() == b"\x00\x00\x00\x01\x00"
    assert Unchoke().encode() == b"\x00\x00\x00\x01\x01"
    assert Interested().encode() == b"\x00\x00\x00\x01\x02"
    assert NotInterested().encode() == b"\x00\x00\x00\x01\x03"

    assert isinstance(decode_message(1, 0, b""), Choke)
    assert isinstance(decode_message(1, 1, b""), Unchoke)
    assert isinstance(decode_message(1, 2, b""), Interested)
    assert isinstance(decode_message(1, 3, b""), NotInterested)


def test_have_message():
    have = Have(piece_index=42)
    encoded = have.encode()
    assert len(encoded) == 9
    decoded = decode_message(5, 4, encoded[5:])
    assert isinstance(decoded, Have)
    assert decoded.piece_index == 42


def test_bitfield():
    # Byte 0b10100000 -> pieces 0 and 2 are present
    bf = Bitfield(b"\xa0")
    encoded = bf.encode()
    decoded = decode_message(2, 5, encoded[5:])
    assert isinstance(decoded, Bitfield)
    assert decoded.has_piece(0) is True
    assert decoded.has_piece(1) is False
    assert decoded.has_piece(2) is True
    assert decoded.has_piece(3) is False
    assert decoded.has_piece(8) is False


def test_request_and_piece():
    req = Request(index=5, begin=16384, length=16384)
    enc_req = req.encode()
    dec_req = decode_message(13, 6, enc_req[5:])
    assert isinstance(dec_req, Request)
    assert dec_req.index == 5
    assert dec_req.begin == 16384
    assert dec_req.length == 16384

    block_data = b"DATA" * 4
    piece = Piece(index=5, begin=16384, block=block_data)
    enc_piece = piece.encode()
    dec_piece = decode_message(9 + len(block_data), 7, enc_piece[5:])
    assert isinstance(dec_piece, Piece)
    assert dec_piece.index == 5
    assert dec_piece.begin == 16384
    assert dec_piece.block == block_data


@pytest.mark.asyncio
async def test_peer_stream_iterator():
    reader = asyncio.StreamReader()
    # Feed multiple messages into reader: KeepAlive, Unchoke, Have(12)
    data = (
        KeepAlive().encode()
        + Unchoke().encode()
        + Have(piece_index=12).encode()
    )
    reader.feed_data(data)
    reader.feed_eof()

    it = PeerStreamIterator(reader)
    messages = [msg async for msg in it]

    assert len(messages) == 3
    assert isinstance(messages[0], KeepAlive)
    assert isinstance(messages[1], Unchoke)
    assert isinstance(messages[2], Have)
    assert messages[2].piece_index == 12
