import hashlib
import pytest
from evatorrent.bencoding import bencode
from evatorrent.torrent import Torrent, Magnet


def create_test_torrent_data(piece_len=16384, file_size=40000, multi_file=False):
    # Calculate pieces
    num_pieces = (file_size + piece_len - 1) // piece_len
    fake_piece_hashes = b"".join(hashlib.sha1(f"piece{i}".encode()).digest() for i in range(num_pieces))

    if not multi_file:
        info = {
            b"name": b"single_file.iso",
            b"piece length": piece_len,
            b"pieces": fake_piece_hashes,
            b"length": file_size,
        }
    else:
        file1_size = 25000
        file2_size = file_size - file1_size
        info = {
            b"name": b"multi_dir",
            b"piece length": piece_len,
            b"pieces": fake_piece_hashes,
            b"files": [
                {b"length": file1_size, b"path": [b"sub", b"file1.txt"]},
                {b"length": file2_size, b"path": [b"file2.txt"]},
            ],
        }

    meta = {
        b"announce": b"http://tracker.example.com:6969/announce",
        b"announce-list": [
            [b"http://tracker.example.com:6969/announce"],
            [b"udp://tracker.opentrackr.org:1337/announce"],
        ],
        b"comment": b"evaTorrent test",
        b"created by": b"evaTorrent",
        b"info": info,
    }
    return bencode(meta)


def test_single_file_torrent():
    data = create_test_torrent_data(piece_len=16384, file_size=40000)
    torrent = Torrent(data)

    assert torrent.name == "single_file.iso"
    assert torrent.total_length == 40000
    assert torrent.piece_length == 16384
    assert torrent.piece_count == 3  # 16384, 16384, 7232
    assert torrent.piece_size(0) == 16384
    assert torrent.piece_size(1) == 16384
    assert torrent.piece_size(2) == 40000 - 32768
    assert len(torrent.info_hash) == 20
    assert len(torrent.info_hash_hex) == 40
    assert torrent.is_multi_file is False
    assert len(torrent.files) == 1
    assert "http://tracker.example.com:6969/announce" in torrent.trackers
    assert "udp://tracker.opentrackr.org:1337/announce" in torrent.trackers


def test_multi_file_torrent():
    data = create_test_torrent_data(piece_len=16384, file_size=40000, multi_file=True)
    torrent = Torrent(data)

    assert torrent.name == "multi_dir"
    assert torrent.total_length == 40000
    assert torrent.is_multi_file is True
    assert len(torrent.files) == 2
    assert torrent.files[0].length == 25000
    assert torrent.files[0].offset == 0
    assert torrent.files[1].length == 15000
    assert torrent.files[1].offset == 25000


def test_magnet_link_parsing():
    magnet_uri = (
        "magnet:?xt=urn:btih:3b245504d6f3a401d45d070b4fed40b3c66287f9"
        "&dn=Ubuntu+Desktop"
        "&tr=http%3A%2F%2Ftracker.example.com%2Fannounce"
        "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    )
    magnet = Magnet(magnet_uri)
    assert magnet.info_hash_hex == "3b245504d6f3a401d45d070b4fed40b3c66287f9"
    assert magnet.name == "Ubuntu Desktop"
    assert len(magnet.trackers) == 2
    assert "udp://tracker.opentrackr.org:1337/announce" in magnet.trackers
