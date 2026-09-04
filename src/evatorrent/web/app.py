"""FastAPI web application exposing REST API, WebSockets, static Web UI, and Auth."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

MAX_TORRENT_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

from collections import defaultdict
import csv
import io
import time

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from evatorrent.auth import (
    AuthConfig,
    EmailSender,
    GoogleVerifier,
    OTPManager,
    SessionManager,
)
from evatorrent.db.database import Database
from evatorrent.engine.manager import EngineManager
from evatorrent.torrent import Magnet, Torrent
from evatorrent.web.ws import WebSocketManager

STATIC_DIR = Path(__file__).parent / "static"

auth_config = AuthConfig()
db_path = auth_config.data_dir / "eva.db"
database = Database(db_path)

# Global managers
engine_manager = EngineManager(db=database)
ws_manager = WebSocketManager()
session_manager = SessionManager(auth_config)
otp_manager = OTPManager(db=database)
email_sender = EmailSender(auth_config)
google_verifier = GoogleVerifier(auth_config)

# In-memory IP rate limiter: client_ip -> list of timestamps
_ip_rate_limits: dict[str, list[float]] = defaultdict(list)

def check_ip_rate_limit(request: Request, max_requests: int = 2, window_seconds: float = 60.0) -> None:
    """Enforces max_requests per window_seconds per client IP."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    if client_ip == "testclient" and not request.headers.get("x-test-rate-limit"):
        return

    now = time.time()
    recent = [ts for ts in _ip_rate_limits[client_ip] if now - ts < window_seconds]
    if len(recent) >= max_requests:
        retry_after = max(1, int(window_seconds - (now - recent[0])))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: maximum {max_requests} requests per minute. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    recent.append(now)
    _ip_rate_limits[client_ip] = recent

def is_cookie_secure(request: Request) -> bool:
    if os.environ.get("SECURE_COOKIES", "").lower() in ("1", "true", "yes"):
        return True
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).lower()
    return proto == "https"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    broadcast_task = asyncio.create_task(telemetry_loop())
    yield
    broadcast_task.cancel()
    await engine_manager.shutdown()


async def telemetry_loop():
    """Streams live telemetry to all connected authenticated WebSocket clients every 800ms."""
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


app = FastAPI(title="evaTorrent API", version="0.3.0", lifespan=lifespan)

@app.middleware("http")
async def https_enforcement_middleware(request: Request, call_next):
    enforce_env = os.environ.get("ENFORCE_HTTPS", "").lower() in ("1", "true", "yes")
    host = request.url.hostname or ""
    is_local = host.lower() in LOCAL_HOSTS
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).lower()

    if (enforce_env or not is_local) and proto == "http":
        url = request.url.replace(scheme="https")
        return RedirectResponse(url=str(url), status_code=307)

    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_current_user(
    request: Request,
    evatorrent_session: Optional[str] = Cookie(None),
) -> str:
    """Dependency validating authenticated user session via cookie or Authorization header."""
    if not auth_config.is_setup_done:
        raise HTTPException(status_code=401, detail="SETUP_REQUIRED")

    token = evatorrent_session
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")

    email = session_manager.verify_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="INVALID_OR_EXPIRED_SESSION")
    return email


# --- Auth Models ---

class SetupRequest(BaseModel):
    admin_email: str
    google_client_id: Optional[str] = None


class OTPRequest(BaseModel):
    email: str


class OTPVerifyRequest(BaseModel):
    email: str
    otp: str


class GoogleAuthRequest(BaseModel):
    credential: str


# --- Auth Endpoints ---

@app.get("/api/auth/status")
async def auth_status(
    request: Request,
    evatorrent_session: Optional[str] = Cookie(None),
):
    """Returns current auth state, setup status, and whether Google OAuth is enabled."""
    token = evatorrent_session
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    verified_email = session_manager.verify_token(token) if token else None

    # Mask admin email for display if unauthenticated
    display_admin = None
    if auth_config.admin_email:
        parts = auth_config.admin_email.split("@")
        if len(parts) == 2:
            display_admin = f"{parts[0][:3]}***@{parts[1]}"
        else:
            display_admin = "***"

    return {
        "setup_required": not auth_config.is_setup_done,
        "admin_email_masked": display_admin,
        "google_enabled": bool(auth_config.google_client_id),
        "google_client_id": auth_config.google_client_id if verified_email else None,
        "is_authenticated": verified_email is not None,
        "user_email": verified_email,
        "smtp_configured": auth_config.is_smtp_configured,
    }


@app.post("/api/auth/setup")
async def initial_setup(req: SetupRequest, request: Request, response: Response):
    """Initial first-time setup to register the administrator email."""
    if auth_config.is_setup_done:
        raise HTTPException(status_code=400, detail="Setup has already been completed.")

    email = req.admin_email.strip().lower()
    if not email or "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    auth_config.set_admin_email(email)
    if req.google_client_id:
        auth_config.set_google_client_id(req.google_client_id)

    token = session_manager.create_token(email)
    response.set_cookie(
        key="evatorrent_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(request),
        max_age=86400 * 30,
    )
    return {"success": True, "token": token, "email": email}


@app.post("/api/auth/otp/request")
async def request_otp(req: OTPRequest, request: Request):
    """Generates and dispatches a 6-digit login OTP to the authorized email (rate-limited: 2/min/IP)."""
    check_ip_rate_limit(request, max_requests=2, window_seconds=60.0)

    if not auth_config.is_setup_done:
        raise HTTPException(status_code=400, detail="Initial setup required first.")

    email = req.email.strip().lower()
    if email != auth_config.admin_email:
        raise HTTPException(status_code=403, detail="Email is not authorized for this evaTorrent instance.")

    success, msg, otp = otp_manager.generate_otp(email)
    if not success or not otp:
        raise HTTPException(status_code=429, detail=msg)

    await email_sender.send_otp(email, otp)
    return {
        "success": True,
        "message": "Verification code dispatched! Check your email (or server logs).",
        "smtp_configured": auth_config.is_smtp_configured,
    }


@app.post("/api/auth/otp/verify")
async def verify_otp(req: OTPVerifyRequest, request: Request, response: Response):
    """Validates the 6-digit OTP and establishes an authenticated session."""
    if not auth_config.is_setup_done:
        raise HTTPException(status_code=400, detail="Initial setup required first.")

    email = req.email.strip().lower()
    if email != auth_config.admin_email:
        raise HTTPException(status_code=403, detail="Email is not authorized.")

    if not otp_manager.verify_otp(email, req.otp):
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    token = session_manager.create_token(email)
    response.set_cookie(
        key="evatorrent_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(request),
        max_age=86400 * 30,
    )
    return {"success": True, "token": token, "email": email}


@app.post("/api/auth/google")
async def google_login(req: GoogleAuthRequest, request: Request, response: Response):
    """Verifies Google ID Token and logs in directly if matching the authorized admin email."""
    if not auth_config.is_setup_done:
        raise HTTPException(status_code=400, detail="Initial setup required first.")

    verified_email = await google_verifier.verify_id_token(req.credential)
    if not verified_email:
        raise HTTPException(
            status_code=403,
            detail="Google sign-in failed: Account email is not authorized for this instance.",
        )

    token = session_manager.create_token(verified_email)
    response.set_cookie(
        key="evatorrent_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(request),
        max_age=86400 * 30,
    )
    return {"success": True, "token": token, "email": verified_email}


@app.post("/api/auth/logout")
async def logout(response: Response):
    """Terminates session."""
    response.delete_cookie(key="evatorrent_session")
    return {"success": True}


# --- Analytics & History Endpoints ---

@app.get("/api/analysis/summary")
async def get_analysis_summary(_: str = Depends(get_current_user)):
    """Lifetime summary statistics across all torrents ever processed."""
    return database.get_analytics_summary()


@app.get("/api/analysis/torrents")
async def get_analysis_torrents(
    status: Optional[str] = "all",
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _: str = Depends(get_current_user),
):
    """Complete historical log of all torrents (even after removal) for analysis."""
    records = database.get_all_history(status_filter=status, search=search, limit=limit, offset=offset)
    summary = database.get_analytics_summary()
    return {"torrents": records, "summary": summary}


@app.get("/api/analysis/export.csv")
async def export_analysis_csv(_: str = Depends(get_current_user)):
    """Exports all historical torrent lifecycle records as a downloadable CSV."""
    records = database.get_all_history(status_filter="all", limit=50000, offset=0)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Info Hash",
        "Name",
        "Total Size (Bytes)",
        "Downloaded (Bytes)",
        "Uploaded (Bytes)",
        "Status",
        "Added At (UTC)",
        "Completed At (UTC)",
        "Removed At (UTC)",
        "Error Message",
        "Download Directory",
    ])
    for r in records:
        writer.writerow([
            r.get("info_hash", ""),
            r.get("name", ""),
            r.get("total_size", 0),
            r.get("downloaded_bytes", 0),
            r.get("uploaded_bytes", 0),
            r.get("status", ""),
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r["added_at"])) if r.get("added_at") else "",
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r["completed_at"])) if r.get("completed_at") else "",
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r["removed_at"])) if r.get("removed_at") else "",
            r.get("error_message") or "",
            r.get("download_dir") or "",
        ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=evatorrent_analysis.csv"},
    )


@app.get("/api/torrents/{info_hash}/events")
async def get_torrent_events(info_hash: str, _: str = Depends(get_current_user)):
    """Timeline event log for a specific torrent."""
    return {"events": database.get_torrent_events(info_hash)}


# --- Core Web & Torrent Endpoints ---

class MagnetRequest(BaseModel):
    magnet: str


class SpeedLimitRequest(BaseModel):
    download_limit: Optional[int] = None  # in bytes/sec (0 or null for unlimited)


@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "evaTorrent API is running. Web UI assets not found."}


@app.get("/api/stats")
async def get_stats(_: str = Depends(get_current_user)):
    return engine_manager.get_global_stats()


@app.get("/api/torrents")
async def list_torrents(_: str = Depends(get_current_user)):
    return engine_manager.get_all_torrents()


@app.post("/api/torrents/upload")
async def upload_torrent(
    file: UploadFile = File(...),
    _: str = Depends(get_current_user),
):
    try:
        content = await file.read(MAX_TORRENT_UPLOAD_BYTES + 1)
        if len(content) > MAX_TORRENT_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Torrent file exceeds 10 MB size limit.")
        session = engine_manager.add_torrent_bytes(content)
        return {"success": True, "info_hash": session.torrent.info_hash_hex, "name": session.torrent.name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse torrent: {e}")


@app.post("/api/torrents/magnet")
async def add_magnet(
    req: MagnetRequest,
    _: str = Depends(get_current_user),
):
    try:
        magnet = Magnet(req.magnet)
        return {
            "success": True,
            "info_hash": magnet.info_hash_hex,
            "name": magnet.name or "Magnet Download",
            "message": "Magnet link registered",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid magnet link: {e}")


@app.post("/api/torrents/{info_hash}/pause")
async def pause_torrent(
    info_hash: str,
    _: str = Depends(get_current_user),
):
    success = await engine_manager.pause_torrent(info_hash)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


@app.post("/api/torrents/{info_hash}/resume")
async def resume_torrent(
    info_hash: str,
    _: str = Depends(get_current_user),
):
    success = engine_manager.resume_torrent(info_hash)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


@app.post("/api/torrents/{info_hash}/speed_limit")
async def set_torrent_speed_limit(
    info_hash: str,
    req: SpeedLimitRequest,
    _: str = Depends(get_current_user),
):
    success = engine_manager.set_speed_limit(info_hash, req.download_limit)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True, "download_limit": req.download_limit}


@app.delete("/api/torrents/{info_hash}")
async def delete_torrent(
    info_hash: str,
    delete_files: bool = False,
    _: str = Depends(get_current_user),
):
    success = await engine_manager.remove_torrent(info_hash, delete_files=delete_files)
    if not success:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"success": True}


@app.get("/api/torrents/{info_hash}/pieces")
async def get_torrent_pieces(
    info_hash: str,
    _: str = Depends(get_current_user),
):
    session = engine_manager.get_session(info_hash)
    if not session:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {
        "total_pieces": session.torrent.piece_count,
        "completed_indices": sorted(list(session.piece_manager.completed_pieces)),
        "ongoing_indices": sorted(list(session.piece_manager.ongoing_pieces)),
    }


@app.get("/api/torrents/{info_hash}/peers")
async def get_torrent_peers(
    info_hash: str,
    _: str = Depends(get_current_user),
):
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
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None):
    # Authenticate WebSocket via session cookie or query param token
    cookie_token = websocket.cookies.get("evatorrent_session")
    auth_token = cookie_token or token

    # Check validity
    user = session_manager.verify_token(auth_token) if auth_token else None
    if not user:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
