"""WebSocket connection manager for real-time live telemetry streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages active WebSockets and broadcasts live updates to frontend clients."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def broadcast(self, data: dict) -> None:
        if not self.active_connections:
            return
        payload = json.dumps(data)
        dead = []
        for ws in list(self.active_connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
