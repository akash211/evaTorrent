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
import logging
import re
import time

logger = logging.getLogger("evaTorrent.web")

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

from evatorrent import __version__
from evatorrent.auth import (
    AuthConfig,
    EmailSender,
    GoogleVerifier,
    OTPManager,
    SessionManager,
)
from evatorrent.db.database import Database
from evatorrent.engine.manager import EngineManager
from evatorrent.search.cache import SearchCacheManager
from evatorrent.search.service import SearchService
from evatorrent.torrent import fetch_torrent_from_caches
from evatorrent.web.ws import WebSocketManager

STATIC_DIR = Path(__file__).parent / "static"

auth_config = AuthConfig()
db_path = auth_config.data_dir / "eva.db"
database = Database(db_path)

# Global managers
download_dir_env = os.environ.get("DOWNLOAD_DIR")
engine_manager = EngineManager(
    default_download_dir=Path(download_dir_env) if download_dir_env else None,
    db=database,
)
search_cache_dir = engine_manager.download_dir / "cached_results"
search_cache_manager = SearchCacheManager(cache_dir=search_cache_dir, max_cached=100)
ws_manager = WebSocketManager()
session_manager = SessionManager(auth_config)
otp_manager = OTPManager(db=database)
email_sender = EmailSender(auth_config)
google_verifier = GoogleVerifier(auth_config)
search_service = SearchService(cache_manager=search_cache_manager)

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


app = FastAPI(title="evaTorrent API", version=__version__, lifespan=lifespan)

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

@app.middleware("http")
async def no_cache_static_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
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
        "google_client_id": auth_config.google_client_id,
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
@app.post("/api/torrents/add")
@app.post("/api/torrents/url")
async def add_magnet(
    req: MagnetRequest,
    user: str = Depends(get_current_user),
):
    logger.info(f"[API] User '{user}' requested adding torrent: {req.magnet[:70]}...")
    try:
        session = await engine_manager.add_torrent_or_url(req.magnet)
        logger.info(f"[API SUCCESS] Enrolled torrent '{session.torrent.name}' ({session.torrent.info_hash_hex[:8]}) for user '{user}'")
        return {
            "success": True,
            "info_hash": session.torrent.info_hash_hex,
            "name": session.torrent.name,
            "message": f"Started download: {session.torrent.name}",
        }
    except ValueError as e:
        logger.warning(f"[API 422] Failed enrolling torrent: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"[API 500] Unexpected error enrolling torrent: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to add torrent: {e}")


@app.get("/api/torrents/download_file")
@app.get("/api/torrents/file")
@app.get("/api/search/download_torrent")
async def download_torrent_file(
    info_hash: str,
    name: Optional[str] = None,
    token: Optional[str] = None,
    request: Request = None,
    evatorrent_session: Optional[str] = Cookie(None),
):
    """Provides direct download of a .torrent file for a given info hash."""
    effective_token = token or evatorrent_session
    if not effective_token and request:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            effective_token = auth_header[7:].strip()

    verified_email = session_manager.verify_token(effective_token) if effective_token else None
    if not verified_email:
        raise HTTPException(status_code=401, detail="Authentication required.")

    clean_hash = info_hash.strip().lower()
    if len(clean_hash) != 40:
        raise HTTPException(status_code=400, detail="Invalid 40-character info hash.")

    logger.info(f"[API] Download .torrent requested for info_hash={clean_hash[:8]} ('{name or ''}') by '{verified_email}'")
    cache_dir = engine_manager.download_dir / ".torrent_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    torrent_file = cache_dir / f"{clean_hash}.torrent"

    raw_bytes: Optional[bytes] = None
    if torrent_file.exists():
        try:
            raw_bytes = torrent_file.read_bytes()
            logger.info(f"[API] Serving .torrent file from local disk cache ({len(raw_bytes)} bytes)")
        except Exception:
            raw_bytes = None

    if not raw_bytes:
        logger.info(f"[API] .torrent file not cached on disk; querying public CDN caches for {clean_hash[:8]}...")
        raw_bytes = await fetch_torrent_from_caches(clean_hash)
        if raw_bytes:
            try:
                torrent_file.write_bytes(raw_bytes)
                logger.info(f"[API] Successfully downloaded and cached .torrent for {clean_hash[:8]}")
            except Exception as e:
                logger.warning(f"Failed to cache torrent file: {e}")

    if not raw_bytes:
        logger.warning(f"[API 404] Metainfo file (.torrent) not available in public caches for {clean_hash[:8]}")
        raise HTTPException(
            status_code=404,
            detail="Metainfo file (.torrent) is not yet available in public caches. You can still download via Magnet link directly in evaTorrent.",
        )

    clean_name = re.sub(r'[/\\?%*:|"<>]+', "_", name.strip()) if name else clean_hash[:12]
    if not clean_name.lower().endswith(".torrent"):
        clean_name = f"{clean_name}.torrent"

    return Response(
        content=raw_bytes,
        media_type="application/x-bittorrent",
        headers={
            "Content-Disposition": f'attachment; filename="{clean_name}"',
            "Cache-Control": "public, max-age=86400",
        },
    )


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



@app.get("/api/search")
async def search_torrents(
    q: str = Query(..., min_length=1, description="Search query string"),
    category: str = Query("all", description="Category: all, movies, series, software, games, books"),
    hide_dead: bool = Query(True, description="Filter for torrents with active seeds (with auto fallback)"),
    limit: int = Query(100, ge=1, le=200),
    timeout: float = Query(30.0, ge=5.0, le=300.0, description="Max search timeout in seconds"),
    refresh: bool = Query(False, description="Bypass cache and force fresh search across indexers"),
    user: str = Depends(get_current_user),
):
    """Searches external torrent indexers cleanly with caching and returns ranked results."""
    t0 = time.time()
    logger.info(f"[SEARCH API] User '{user}' searching '{q}' (cat={category}, refresh={refresh})")
    try:
        results = await search_service.search(
            query=q,
            category=category,
            hide_dead=hide_dead,
            limit=limit,
            timeout=timeout,
            refresh=refresh,
        )
        elapsed = round(time.time() - t0, 2)
        logger.info(f"[SEARCH API] Returned {results.get('returned', 0)} results for '{q}' in {elapsed}s (cached={results.get('is_cached')})")
        return results
    except Exception as e:
        logger.error(f"[SEARCH API ERROR] Search failed for '{q}': {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


@app.get("/api/search/recent")
async def get_recent_searches(_: str = Depends(get_current_user)):
    """Returns up to 10 recent searches from the persistent search cache."""
    return {"recent_searches": search_cache_manager.get_recent(limit=10)}



# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
