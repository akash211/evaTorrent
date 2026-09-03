import pytest
from evatorrent.bencoding import bencode, bdecode, BencodeError


def test_decode_integers():
    assert bdecode(b"i0e") == 0
    assert bdecode(b"i42e") == 42
    assert bdecode(b"i-42e") == -42
    assert bdecode(b"i1234567890123456789e") == 1234567890123456789

    with pytest.raises(BencodeError):
        bdecode(b"i03e")  # leading zero
    with pytest.raises(BencodeError):
        bdecode(b"i-0e")  # negative zero
    with pytest.raises(BencodeError):
        bdecode(b"ie")  # empty


def test_decode_strings():
    assert bdecode(b"4:spam") == b"spam"
    assert bdecode(b"0:") == b""
    assert bdecode(b"12:Middle Earth") == b"Middle Earth"

    with pytest.raises(BencodeError):
        bdecode(b"4:spa")  # too short
    with pytest.raises(BencodeError):
        bdecode(b"04:spam")  # leading zero in length


def test_decode_lists():
    assert bdecode(b"le") == []
    assert bdecode(b"l4:spam4:eggsi123ee") == [b"spam", b"eggs", 123]
    assert bdecode(b"ll4:spamei1ee") == [[b"spam"], 1]


def test_decode_dicts():
    assert bdecode(b"de") == {}
    assert bdecode(b"d3:cow3:moo4:spam4:eggse") == {b"cow": b"moo", b"spam": b"eggs"}
    nested = bdecode(b"d4:spaml1:a1:bee")
    assert nested == {b"spam": [b"a", b"b"]}


def test_encode_decode_roundtrip():
    data = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": {
            b"length": 1048576,
            b"name": b"testfile.iso",
            b"piece length": 262144,
            b"pieces": b"x" * 80,
        },
    }
    encoded = bencode(data)
    decoded = bdecode(encoded)
    assert decoded == data


def test_encode_types():
    assert bencode(123) == b"i123e"
    assert bencode(-5) == b"i-5e"
    assert bencode("hello") == b"5:hello"
    assert bencode(b"world") == b"5:world"
    assert bencode([1, "two", b"three"]) == b"li1e3:two5:threee"
    assert bencode({"b": 2, "a": 1}) == b"d1:ai1e1:bi2ee"
