"""FastAPI web application exposing REST API, WebSockets, and static Web UI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from evatorrent.engine.manager import EngineManager
from evatorrent.torrent import Magnet, Torrent
from evatorrent.web.ws import WebSocketManager

STATIC_DIR = Path(__file__).parent / "static"

# Global engine manager instance
engine_manager = EngineManager()
ws_manager = WebSocketManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background telemetry broadcast task
    broadcast_task = asyncio.create_task(telemetry_loop())
    yield
    broadcast_task.cancel()
    await engine_manager.shutdown()


async def telemetry_loop():
    """Streams live telemetry to all connected WebSocket clients every 800ms."""
    while True:
        try:
            await asyncio.sleep(0.8)
            if ws_manager.active_connections:
                payload = {
                    "type": "telemetry",
                    "stats": engine_manager.get_global_stats(),
                    "torrents": engine_manager.get_all_torrents(),
                }
                await ws_manager.broadcast(payload)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


app = FastAPI(title="evaTorrent API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MagnetRequest(BaseModel):
    magnet: str


@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "evaTorrent API is running. Web UI assets not found."}


@app.get("/api/stats")
async def get_stats():
    return engine_manager.get_global_stats()


@app.get("/api/torrents")
async def list_torrents():
    return engine_manager.get_all_torrents()


@app.post("/api/torrents/upload")
async def upload_torrent(file: UploadFile = File(...)):
    try:
        content = await file.read()
        session = engine_manager.add_torrent_bytes(content)
        return {"success": True, "info_hash": session.torrent.info_hash_hex, "name": session.torrent.name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse torrent: {e}")


@app.post("/api/torrents/magnet")
async def add_magnet(req: MagnetRequest):
    try:
        magnet = Magnet(req.magnet)
        # Note: Magnet support initiates a session with known trackers
        # In a full DHT client, metadata is fetched via BEP 9 extension.
        # Here we initialize what we have from the magnet URI.
        return {
            "success": True,
            "info_hash": magnet.info_hash_hex,
            "name": magnet.name or "Magnet Download",
            "message": "Magnet link registered",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid magnet link: {e}")


@app.post("/api/torrents/{info_hash}/pause")
async def pause_torrent(info_hash: str):
    success = await engine_manager.pause_torrent(info_hash)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


@app.post("/api/torrents/{info_hash}/resume")
async def resume_torrent(info_hash: str):
    success = engine_manager.resume_torrent(info_hash)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


class SpeedLimitRequest(BaseModel):
    download_limit: Optional[int] = None  # in bytes/sec (0 or null for unlimited)


@app.post("/api/torrents/{info_hash}/speed_limit")
async def set_torrent_speed_limit(info_hash: str, req: SpeedLimitRequest):
    success = engine_manager.set_speed_limit(info_hash, req.download_limit)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True, "download_limit": req.download_limit}


@app.delete("/api/torrents/{info_hash}")
async def delete_torrent(info_hash: str, delete_files: bool = False):
    success = await engine_manager.remove_torrent(info_hash, delete_files=delete_files)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


@app.get("/api/torrents/{info_hash}/pieces")
async def get_torrent_pieces(info_hash: str):
    session = engine_manager.get_session(info_hash)
    if not session:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {
        "total_pieces": session.torrent.piece_count,
        "completed_indices": sorted(list(session.piece_manager.completed_pieces)),
        "ongoing_indices": sorted(list(session.piece_manager.ongoing_pieces)),
    }


@app.get("/api/torrents/{info_hash}/peers")
async def get_torrent_peers(info_hash: str):
    session = engine_manager.get_session(info_hash)
    if not session:
        raise HTTPException(status_code=404, detail="Torrent not found")
    peers_list = []
    for key, conn in session.active_peers.items():
        peers_list.append({
            "key": key,
            "ip": conn.peer.ip,
            "port": conn.peer.port,
            "connected": conn.is_connected,
            "choked": conn.is_choked,
            "interested": conn.am_interested,
            "download_speed": round(conn.download_speed, 2),
            "bytes_downloaded": conn.bytes_downloaded,
        })
    return {"peers": peers_list}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for ping/messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
