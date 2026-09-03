"""Asynchronous stream iterator for BitTorrent peer messages."""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import AsyncIterator, Optional

from evatorrent.peer.protocol import (
    KeepAlive,
    PeerMessage,
    decode_message,
)

logger = logging.getLogger(__name__)


class PeerStreamIterator:
    """Consumes an asyncio StreamReader and yields parsed PeerMessages."""

    def __init__(self, reader: asyncio.StreamReader, buffer_limit: int = 10 * 1024 * 1024):
        self.reader = reader
        self.buffer = bytearray()
        self.buffer_limit = buffer_limit

    def __aiter__(self) -> AsyncIterator[PeerMessage]:
        return self

    async def __anext__(self) -> PeerMessage:
        while True:
            # Check if we have at least 4 bytes for length prefix
            if len(self.buffer) >= 4:
                length = struct.unpack(">I", self.buffer[:4])[0]
                if length == 0:
                    # Keep-Alive message (4 zero bytes)
                    del self.buffer[:4]
                    return KeepAlive()

                # Check if entire message has arrived
                total_message_len = 4 + length
                if len(self.buffer) >= total_message_len:
                    msg_id = self.buffer[4]
                    payload = bytes(self.buffer[5:total_message_len])
                    del self.buffer[:total_message_len]

                    msg = decode_message(length, msg_id, payload)
                    if msg is not None:
                        return msg
                    else:
                        logger.debug(f"Skipping unknown or malformed peer message ID {msg_id}")
                        continue

            # Need more data from socket
            chunk = await self.reader.read(65536)
            if not chunk:
                # Connection closed by remote peer
                raise StopAsyncIteration
            self.buffer.extend(chunk)
            if len(self.buffer) > self.buffer_limit:
                raise ValueError("Peer stream buffer exceeded maximum limit")
