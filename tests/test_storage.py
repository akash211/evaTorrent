import hashlib
import tempfile
from pathlib import Path
import pytest
from evatorrent.bencoding import bencode
from evatorrent.storage.disk import DiskWriter
from evatorrent.storage.manager import PieceManager
from evatorrent.storage.piece import Piece, BLOCK_SIZE
from evatorrent.torrent import Torrent


def test_piece_blocks():
    length = 36864  # 16384 + 16384 + 4096
    expected_hash = hashlib.sha1(b"x" * length).digest()
    piece = Piece(0, length, expected_hash)

    assert len(piece.blocks) == 3
    assert piece.blocks[0].length == 16384
    assert piece.blocks[1].length == 16384
    assert piece.blocks[2].length == 4096

    assert piece.is_complete is False
    piece.set_block_data(0, b"x" * 16384)
    piece.set_block_data(16384, b"x" * 16384)
    piece.set_block_data(32768, b"x" * 4096)

    assert piece.is_complete is True
    assert piece.verify_hash() is True


def test_piece_manager_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a small torrent with 2 pieces
        piece_len = 16384
        data_p0 = b"A" * piece_len
        data_p1 = b"B" * 1000
        full_payload = data_p0 + data_p1

        hash0 = hashlib.sha1(data_p0).digest()
        hash1 = hashlib.sha1(data_p1).digest()

        meta = {
            b"announce": b"http://tracker.example.com/announce",
            b"info": {
                b"name": b"output.bin",
                b"piece length": piece_len,
                b"pieces": hash0 + hash1,
                b"length": len(full_payload),
            },
        }
        torrent = Torrent(bencode(meta))
        pm = PieceManager(torrent, Path(tmpdir))

        assert pm.is_complete is False
        assert pm.progress_percentage == 0.0

        # Simulate peer advertising pieces 0 and 1
        peer_key = "127.0.0.1:6881"
        pm.peer_has_piece(peer_key, 0)
        pm.peer_has_piece(peer_key, 1)

        # Request block
        req1 = pm.next_request(peer_key)
        assert req1 is not None
        assert req1.piece_index == 0
        assert req1.begin == 0
        assert req1.length == piece_len

        # Send block 0
        completed = pm.on_block_received(0, 0, data_p0)
        assert completed is True
        assert 0 in pm.completed_pieces

        # Request block for piece 1
        req2 = pm.next_request(peer_key)
        assert req2 is not None
        assert req2.piece_index == 1
        assert req2.begin == 0
        assert req2.length == 1000

        # Send block 1
        completed = pm.on_block_received(1, 0, data_p1)
        assert completed is True
        assert pm.is_complete is True
        assert pm.progress_percentage == 100.0

        # Verify output file on disk
        out_file = Path(tmpdir) / "output.bin"
        assert out_file.exists()
        assert out_file.read_bytes() == full_payload
