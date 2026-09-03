"""Bencoding encoder and decoder according to the BitTorrent specification.

Supports:
- Integers: i<num>e -> int
- Byte strings: <len>:<content> -> bytes
- Lists: l<item>...e -> list
- Dictionaries: d<key><val>...e -> dict (keys must be bytes)
"""

from typing import Any, Union

TOKEN_INTEGER = b"i"[0]
TOKEN_LIST = b"l"[0]
TOKEN_DICT = b"d"[0]
TOKEN_END = b"e"[0]
TOKEN_COLON = b":"[0]


class BencodeError(ValueError):
    """Exception raised when bencoding / bdecoding fails."""
    pass


class Decoder:
    """Decodes bencoded byte strings into Python objects."""

    def __init__(self, data: Union[bytes, bytearray, memoryview]):
        if isinstance(data, (bytearray, memoryview)):
            self._data = bytes(data)
        elif isinstance(data, bytes):
            self._data = data
        else:
            raise TypeError(f"Expected bytes-like object, got {type(data).__name__}")
        self._index = 0
        self._length = len(self._data)

    def decode(self) -> Any:
        if self._index >= self._length:
            raise BencodeError("Empty bencoded data")
        res = self._decode_next()
        if self._index != self._length:
            raise BencodeError(f"Extra data at end of input: {self._index} < {self._length}")
        return res

    def _peek(self) -> int:
        if self._index >= self._length:
            raise BencodeError("Unexpected end of bencoded data")
        return self._data[self._index]

    def _decode_next(self) -> Any:
        token = self._peek()
        if token == TOKEN_INTEGER:
            return self._decode_int()
        elif token == TOKEN_LIST:
            return self._decode_list()
        elif token == TOKEN_DICT:
            return self._decode_dict()
        elif ord(b"0") <= token <= ord(b"9"):
            return self._decode_string()
        else:
            raise BencodeError(f"Invalid token '{chr(token)}' ({token}) at index {self._index}")

    def _decode_int(self) -> int:
        self._index += 1  # Skip 'i'
        end_idx = self._data.find(b"e", self._index)
        if end_idx == -1:
            raise BencodeError("Unterminated integer")
        raw_num = self._data[self._index:end_idx]
        if not raw_num:
            raise BencodeError("Empty integer value")
        # Validate leading zero rules: "0" is valid, but "03" or "-0" are not
        if raw_num == b"-0":
            raise BencodeError("Negative zero is invalid in bencoding")
        if len(raw_num) > 1 and raw_num.startswith(b"0"):
            raise BencodeError("Leading zeroes are invalid in bencoding integers")
        if len(raw_num) > 2 and raw_num.startswith(b"-0"):
            raise BencodeError("Leading zeroes in negative numbers are invalid in bencoding")
        try:
            val = int(raw_num)
        except ValueError as e:
            raise BencodeError(f"Malformed integer: {raw_num!r}") from e
        self._index = end_idx + 1  # Skip 'e'
        return val

    def _decode_string(self) -> bytes:
        colon_idx = self._data.find(b":", self._index)
        if colon_idx == -1:
            raise BencodeError("Unterminated string length")
        raw_len = self._data[self._index:colon_idx]
        if not raw_len or (len(raw_len) > 1 and raw_len.startswith(b"0")):
            raise BencodeError("Invalid string length format")
        try:
            length = int(raw_len)
        except ValueError as e:
            raise BencodeError(f"Malformed string length: {raw_len!r}") from e

        if length < 0:
            raise BencodeError(f"Negative string length: {length}")

        start = colon_idx + 1
        end = start + length
        if end > self._length:
            raise BencodeError(
                f"String length {length} exceeds remaining data ({self._length - start} bytes)"
            )
        self._index = end
        return self._data[start:end]

    def _decode_list(self) -> list:
        self._index += 1  # Skip 'l'
        items = []
        while self._peek() != TOKEN_END:
            items.append(self._decode_next())
        self._index += 1  # Skip 'e'
        return items

    def _decode_dict(self) -> dict:
        self._index += 1  # Skip 'd'
        result = {}
        last_key = None
        while self._peek() != TOKEN_END:
            if not (ord(b"0") <= self._peek() <= ord(b"9")):
                raise BencodeError(f"Dictionary key must be a string, got token {chr(self._peek())}")
            key = self._decode_string()
            if last_key is not None and key < last_key:
                # Strictly speaking keys should be lexicographically sorted
                pass
            last_key = key
            val = self._decode_next()
            result[key] = val
        self._index += 1  # Skip 'e'
        return result


class Encoder:
    """Encodes Python objects into bencoded bytes."""

    def __init__(self, data: Any):
        self._data = data

    def encode(self) -> bytes:
        return self._encode_item(self._data)

    def _encode_item(self, item: Any) -> bytes:
        if isinstance(item, int) and not isinstance(item, bool):
            return f"i{item}e".encode("ascii")
        elif isinstance(item, bytes):
            return f"{len(item)}:".encode("ascii") + item
        elif isinstance(item, str):
            raw = item.encode("utf-8")
            return f"{len(raw)}:".encode("ascii") + raw
        elif isinstance(item, (list, tuple)):
            out = bytearray(b"l")
            for sub in item:
                out.extend(self._encode_item(sub))
            out.append(TOKEN_END)
            return bytes(out)
        elif isinstance(item, dict):
            out = bytearray(b"d")
            # Keys in bencoding dictionaries must be sorted as raw strings
            def sort_key(k: Union[bytes, str]) -> bytes:
                if isinstance(k, bytes):
                    return k
                elif isinstance(k, str):
                    return k.encode("utf-8")
                raise TypeError(f"Dictionary keys must be bytes or str, not {type(k).__name__}")

            sorted_keys = sorted(item.keys(), key=sort_key)
            for k in sorted_keys:
                out.extend(self._encode_item(k))
                out.extend(self._encode_item(item[k]))
            out.append(TOKEN_END)
            return bytes(out)
        else:
            raise TypeError(f"Cannot bencode object of type {type(item).__name__}: {item!r}")


def bdecode(data: Union[bytes, bytearray, memoryview]) -> Any:
    """Convenience function to decode bencoded bytes."""
    return Decoder(data).decode()


def bencode(data: Any) -> bytes:
    """Convenience function to encode a Python object into bencoded bytes."""
    return Encoder(data).encode()
