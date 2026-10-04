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
from evatorrent.metadata.cache import DiscoverCacheManager
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
discover_cache_manager = DiscoverCacheManager(cache_dir=search_cache_dir, max_cached=100)
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
    from evatorrent.engine import limits as _limits

    logger.info(
        "[STARTUP] evaTorrent v%s | download_dir=%s | limits: max_peers=%d pipeline=%d ongoing=%d "
        "stream_buf=%dB telemetry=%.1fs verify=%s large_at=%dGB",
        __version__,
        engine_manager.download_dir,
        _limits.MAX_PEERS,
        _limits.PIPELINE_PER_PEER,
        _limits.MAX_ONGOING_PIECES,
        _limits.STREAM_BUFFER_LIMIT,
        _limits.TELEMETRY_INTERVAL_SECS,
        _limits.VERIFY_ON_STARTUP,
        _limits.LARGE_TORRENT_BYTES // (1024**3),
    )
    engine_manager.start_monitor()
    try:
        summary = await engine_manager.restore_from_db()
        logger.info(f"[STARTUP] Swarm auto-resume from DB: {summary}")
    except Exception as e:
        logger.warning(f"[STARTUP] Auto-resume failed: {e}")
    broadcast_task = asyncio.create_task(telemetry_loop())
    yield
    broadcast_task.cancel()
    await engine_manager.shutdown()


async def telemetry_loop():
    """Streams live telemetry to all connected authenticated WebSocket clients."""
    try:
        interval = float(os.environ.get("EVA_TELEMETRY_SECS", 0.8))
    except (ValueError, TypeError):
        interval = 0.8
    interval = min(max(interval, 0.5), 10.0)
    while True:
        try:
            await asyncio.sleep(interval)
            if ws_manager.active_connections:
                payload = {
                    "type": "telemetry",
                    "stats": engine_manager.get_global_stats(),
                    "torrents": engine_manager.get_swarm_snapshot(),
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
    if request.url.path.startswith("/static/") or request.url.path in (
        "/",
        "/index.html",
        "/home",
        "/search",
        "/discover",
        "/subtitle",
        "/report",
    ):
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
    logger.info(f"[AUTH] Initial setup completed for admin '{email}' (google_configured={bool(req.google_client_id)})")

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
        logger.warning(f"[AUTH] OTP verification failed for '{email}' (wrong or expired code)")
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    logger.info(f"[AUTH] OTP login success for '{email}'")

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
        logger.warning("[AUTH] Google sign-in failed: token invalid or email unauthorized")
        raise HTTPException(
            status_code=403,
            detail="Google sign-in failed: Account email is not authorized for this instance.",
        )
    logger.info(f"[AUTH] Google login success for '{verified_email}'")

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
    writer.writerow(
        [
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
        ]
    )
    for r in records:
        writer.writerow(
            [
                r.get("info_hash", ""),
                r.get("name", ""),
                r.get("total_size", 0),
                r.get("downloaded_bytes", 0),
                r.get("uploaded_bytes", 0),
                r.get("status", ""),
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["added_at"])) if r.get("added_at") else "",
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["completed_at"])) if r.get("completed_at") else "",
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["removed_at"])) if r.get("removed_at") else "",
                r.get("error_message") or "",
                r.get("download_dir") or "",
            ]
        )

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
@app.api_route("/home", methods=["GET", "HEAD"])
@app.api_route("/search", methods=["GET", "HEAD"])
@app.api_route("/discover", methods=["GET", "HEAD"])
@app.api_route("/subtitle", methods=["GET", "HEAD"])
@app.api_route("/report", methods=["GET", "HEAD"])
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
    # DB-backed swarm: live sessions first, DB fallback rows when memory is empty/stale.
    return engine_manager.get_swarm_snapshot()


@app.post("/api/torrents/upload")
async def upload_torrent(
    file: UploadFile = File(...),
    user: str = Depends(get_current_user),
):
    try:
        content = await file.read(MAX_TORRENT_UPLOAD_BYTES + 1)
        if len(content) > MAX_TORRENT_UPLOAD_BYTES:
            logger.warning(f"[TORRENT] User '{user}' upload rejected: '{file.filename}' exceeds 10 MB")
            raise HTTPException(status_code=413, detail="Torrent file exceeds 10 MB size limit.")
        session = engine_manager.add_torrent_bytes(content)
        t = session.torrent
        logger.info(
            "[TORRENT] User '%s' uploaded '%s' (%s, %.2f GB, %d pieces, %d trackers, max_peers=%d)",
            user,
            file.filename,
            t.info_hash_hex[:8],
            t.total_length / (1024**3),
            t.piece_count,
            len(t.trackers),
            session.max_peers,
        )
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
        t = session.torrent
        logger.info(
            "[API SUCCESS] User '%s' enrolled '%s' (%s, %.2f GB, %d pieces, %d trackers, max_peers=%d)",
            user,
            t.name,
            t.info_hash_hex[:8],
            t.total_length / (1024**3),
            t.piece_count,
            len(t.trackers),
            session.max_peers,
        )
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

    logger.info(
        f"[API] Download .torrent requested for info_hash={clean_hash[:8]} ('{name or ''}') by '{verified_email}'"
    )
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
    user: str = Depends(get_current_user),
):
    session = engine_manager.get_session(info_hash)
    name = session.torrent.name if session else (database.get_torrent_history(info_hash) or {}).get("name", info_hash[:12])
    success = await engine_manager.pause_torrent(info_hash)
    if not success:
        # DB-only row (e.g. after restart before rebuild): mark paused in DB.
        row = database.get_torrent_history(info_hash)
        if row:
            database.update_torrent_progress(
                info_hash,
                int(row.get("downloaded_bytes") or 0),
                int(row.get("uploaded_bytes") or 0),
                "paused",
            )
            logger.info(f"[TORRENT] User '{user}' paused '{name}' ({info_hash[:8]}, source=db)")
            return {"success": True, "source": "db"}
        logger.warning(f"[TORRENT] User '{user}' pause failed: torrent {info_hash[:8]} not found")
        raise HTTPException(status_code=404, detail="Torrent not found")
    logger.info(f"[TORRENT] User '{user}' paused '{name}' ({info_hash[:8]})")
    return {"success": True}


@app.post("/api/torrents/{info_hash}/resume")
async def resume_torrent(
    info_hash: str,
    user: str = Depends(get_current_user),
):
    success = engine_manager.resume_torrent(info_hash)
    if not success:
        # Fallback: rebuild session from DB + cached .torrent (survives restarts).
        try:
            rebuilt = await engine_manager.resume_db_torrent(info_hash)
        except Exception as e:
            logger.error(f"[TORRENT] User '{user}' resume failed for {info_hash[:8]}: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail=f"Resume failed: {e}")
        if not rebuilt:
            logger.warning(f"[TORRENT] User '{user}' resume failed: {info_hash[:8]} not in swarm or database")
            raise HTTPException(
                status_code=404,
                detail="Torrent not found in live swarm or database. Re-add the .torrent/magnet.",
            )
        logger.info(f"[TORRENT] User '{user}' resumed {info_hash[:8]} (session rebuilt from database)")
        return {"success": True, "source": "db", "message": "Session rebuilt from database and resumed."}
    session = engine_manager.get_session(info_hash)
    name = session.torrent.name if session else info_hash[:12]
    logger.info(f"[TORRENT] User '{user}' resumed '{name}' ({info_hash[:8]})")
    return {"success": True}


@app.post("/api/torrents/{info_hash}/speed_limit")
async def set_torrent_speed_limit(
    info_hash: str,
    req: SpeedLimitRequest,
    user: str = Depends(get_current_user),
):
    success = engine_manager.set_speed_limit(info_hash, req.download_limit)
    if not success:
        logger.warning(f"[TORRENT] User '{user}' speed-limit failed: {info_hash[:8]} not found")
        raise HTTPException(status_code=404, detail="Torrent not found")
    logger.info(f"[TORRENT] User '{user}' set download_limit={req.download_limit} B/s on {info_hash[:8]}")
    return {"success": True, "download_limit": req.download_limit}


class FileSelectionRequest(BaseModel):
    selected_paths: Optional[list] = None  # None or full list = all files


@app.get("/api/torrents/{info_hash}/files")
async def get_torrent_files(
    info_hash: str,
    user: str = Depends(get_current_user),
):
    """Lists torrent files with per-file selection state."""
    session = engine_manager.get_session(info_hash)
    if not session:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {
        "files": [
            {
                "path": f.path,
                "length": f.length,
                "offset": f.offset,
                "selected": session.piece_manager.is_file_selected(f.path),
            }
            for f in session.torrent.files
        ],
        "selected_bytes": session.piece_manager.selected_total_bytes,
        "total_bytes": session.torrent.total_length,
    }


@app.post("/api/torrents/{info_hash}/files")
async def set_torrent_files(
    info_hash: str,
    req: FileSelectionRequest,
    user: str = Depends(get_current_user),
):
    """Selects which files to download (deselect the rest). At least one required."""
    session = engine_manager.get_session(info_hash)
    if not session:
        # DB-only row: persist selection so the rebuild honors it.
        row = database.get_torrent_history(info_hash)
        if not row:
            raise HTTPException(status_code=404, detail="Torrent not found")
        if engine_manager.db:
            try:
                engine_manager.db.set_file_selection(info_hash, req.selected_paths)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to persist selection: {e}")
        logger.info(f"[TORRENT] User '{user}' staged file selection for {info_hash[:8]} (source=db)")
        return {"success": True, "source": "db"}
    try:
        summary = engine_manager.set_file_selection(info_hash, req.selected_paths)
    except ValueError as e:
        logger.warning(f"[TORRENT] User '{user}' file selection rejected for {info_hash[:8]}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    if summary is None:
        raise HTTPException(status_code=404, detail="Torrent not found")
    logger.info(
        "[TORRENT] User '%s' set file selection on '%s' (%s): %d/%d files, %d pieces skipped",
        user,
        session.torrent.name,
        info_hash[:8],
        summary["selected_count"],
        summary["total_count"],
        summary["skipped_pieces"],
    )
    return {"success": True, **summary}


@app.delete("/api/torrents/{info_hash}")
async def delete_torrent(
    info_hash: str,
    delete_files: bool = False,
    user: str = Depends(get_current_user),
):
    session = engine_manager.get_session(info_hash)
    name = session.torrent.name if session else (database.get_torrent_history(info_hash) or {}).get("name", info_hash[:12])
    success = await engine_manager.remove_torrent(info_hash, delete_files=delete_files)
    if not success:
        logger.warning(f"[TORRENT] User '{user}' delete failed: {info_hash[:8]} not found")
        raise HTTPException(status_code=404, detail="Torrent not found")
    logger.info(f"[TORRENT] User '{user}' removed '{name}' ({info_hash[:8]}, delete_files={delete_files})")
    return {"success": True}


@app.get("/api/torrents/{info_hash}/pieces")
async def get_torrent_pieces(
    info_hash: str,
    limit: int = Query(5000, ge=1, le=100000, description="Max piece indices returned"),
    offset: int = Query(0, ge=0, description="Offset into sorted completed indices"),
    _: str = Depends(get_current_user),
):
    session = engine_manager.get_session(info_hash)
    if not session:
        raise HTTPException(status_code=404, detail="Torrent not found")
    completed = sorted(session.piece_manager.completed_pieces)
    total = len(completed)
    # Paginate: a 155 GB torrent has ~40-150k pieces; serializing all of them
    # every poll kept CPUs hot and payloads huge.
    page = completed[offset : offset + limit]
    return {
        "total_pieces": session.torrent.piece_count,
        "completed_total": total,
        "completed_indices": page,
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(page) < total,
        "ongoing_indices": sorted(list(session.piece_manager.ongoing_pieces))[:limit],
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
        peers_list.append(
            {
                "key": key,
                "ip": conn.peer.ip,
                "port": conn.peer.port,
                "connected": conn.is_connected,
                "choked": conn.is_choked,
                "interested": conn.am_interested,
                "download_speed": round(conn.download_speed, 2),
                "bytes_downloaded": conn.bytes_downloaded,
            }
        )
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
    hindi: bool = Query(False, description="Prefer Hindi / dual-audio results"),
    english: bool = Query(False, description="Prefer English results"),
    user: str = Depends(get_current_user),
):
    """Searches external torrent indexers cleanly with caching and returns ranked results."""
    t0 = time.time()
    logger.info(
        f"[SEARCH API] User '{user}' searching '{q}' (cat={category}, refresh={refresh}, hindi={hindi}, english={english})"
    )
    try:
        results = await search_service.search(
            query=q,
            category=category,
            hide_dead=hide_dead,
            limit=limit,
            timeout=timeout,
            refresh=refresh,
            hindi=hindi,
            english=english,
        )
        elapsed = round(time.time() - t0, 2)
        returned = results.get("returned", len(results.get("results", [])))
        logger.info(
            "[SEARCH] User '%s' q='%s' cat=%s hindi=%s english=%s refresh=%s -> returned=%d/%d total (cached=%s) in %.2fs",
            user,
            q,
            category,
            hindi,
            english,
            refresh,
            returned,
            results.get("total_found", returned),
            results.get("is_cached"),
            elapsed,
        )
        return results
    except Exception as e:
        logger.error(f"[SEARCH API ERROR] Search failed for '{q}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


@app.get("/api/discover/keys")
async def discover_keys(_: str = Depends(get_current_user)):
    """Reports which optional Discover enrichment keys are live (booleans only)."""
    from evatorrent.metadata import keys_configured

    return {"keys_configured": keys_configured()}


@app.get("/api/discover")
async def discover_media(
    q: str = Query(..., min_length=1, description="Movie / show / book / game title"),
    type: str = Query("all", description="Media type: all, movie, tv, book, game"),
    limit: int = Query(8, ge=1, le=20),
    timeout: float = Query(20.0, ge=5.0, le=60.0),
    year: Optional[int] = Query(None, ge=1900, le=2100, description="Filter by release/publish year"),
    refresh: bool = Query(False, description="Bypass cache and force a fresh lookup"),
    _: str = Depends(get_current_user),
):
    """Free media-info lookup (no API key required).

    Returns release date, runtime, IMDb/RT ratings + links, Wikipedia page,
    budget/box-office (when TMDB/OMDb keys are configured), India OTT hints
    via JustWatch (+accurate TMDB providers when TMDB_API_KEY is set),
    languages, and an embeddable YouTube trailer. Results are cached on disk.
    """
    from evatorrent.metadata import lookup_media

    t0 = time.time()
    try:
        if not refresh:
            cached = discover_cache_manager.get(q, type, year)
            if cached:
                logger.info(f"[DISCOVER] Serving '{q}' from cache ({cached.get('cache_age_human')})")
                return cached
        data = await lookup_media(query=q, media_type=type, limit=limit, timeout=timeout, year=year)
        data["elapsed_seconds"] = round(time.time() - t0, 2)
        n_results = len(data.get("results", [])) if isinstance(data.get("results"), list) else 0
        logger.info(
            "[DISCOVER] q='%s' type=%s year=%s refresh=%s -> %d results in %.2fs (keys=%s)",
            q,
            type,
            year,
            refresh,
            n_results,
            data["elapsed_seconds"],
            data.get("keys_configured", "?"),
        )
        data["ott_accuracy_note"] = (
            "India OTT data is approximate (JustWatch search) unless TMDB_API_KEY is configured, "
            "in which case live TMDB India providers are returned."
        )
        discover_cache_manager.set(q, type, year, data)
        return data
    except Exception as e:
        logger.error(f"[DISCOVER ERROR] lookup failed for '{q}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Discover lookup failed: {e}")


@app.get("/api/discover/recent")
async def get_discover_recent(_: str = Depends(get_current_user)):
    """Returns up to 10 recent Discover searches from the persistent cache."""
    return {"recent_searches": discover_cache_manager.get_recent(limit=10)}


class DiscoverSaveRequest(BaseModel):
    item: dict
    remarks: Optional[str] = ""


class DiscoverRemarksRequest(BaseModel):
    remarks: Optional[str] = ""


@app.get("/api/discover/saved")
async def list_saved_discover(
    type: Optional[str] = "all",
    search: Optional[str] = None,
    _: str = Depends(get_current_user),
):
    """Personal Discover library saved in SQLite."""
    return {"saved": database.get_saved_discover(media_type=type, search=search)}


@app.post("/api/discover/saved")
async def save_discover_item(req: DiscoverSaveRequest, user: str = Depends(get_current_user)):
    """Saves a Discover result card into the personal library."""
    if not req.item or not req.item.get("title"):
        logger.warning(f"[DISCOVER] User '{user}' save rejected: result item has no title")
        raise HTTPException(status_code=400, detail="A result item with a title is required.")
    saved = database.save_discover_item(req.item, req.remarks or "")
    logger.info(f"[DISCOVER] User '{user}' saved '{req.item.get('title')}' (id={saved.get('id')}) to library")
    return {"success": True, "saved": saved}


@app.put("/api/discover/saved/{saved_id}")
async def update_saved_remarks(saved_id: int, req: DiscoverRemarksRequest, user: str = Depends(get_current_user)):
    """Updates the editable remarks on a saved library entry."""
    updated = database.update_discover_remarks(saved_id, req.remarks or "")
    if not updated:
        logger.warning(f"[DISCOVER] User '{user}' remarks update failed: saved id={saved_id} not found")
        raise HTTPException(status_code=404, detail="Saved entry not found")
    logger.info(f"[DISCOVER] User '{user}' updated remarks on saved id={saved_id} ('{updated.get('title', '')}')")
    return {"success": True, "saved": updated}


@app.delete("/api/discover/saved/{saved_id}")
async def delete_saved_discover(saved_id: int, user: str = Depends(get_current_user)):
    """Removes an entry from the personal Discover library."""
    if not database.delete_saved_discover(saved_id):
        logger.warning(f"[DISCOVER] User '{user}' delete failed: saved id={saved_id} not found")
        raise HTTPException(status_code=404, detail="Saved entry not found")
    logger.info(f"[DISCOVER] User '{user}' removed saved id={saved_id} from library")
    return {"success": True}


@app.get("/api/search/recent")
async def get_recent_searches(_: str = Depends(get_current_user)):
    """Returns up to 10 recent searches from the persistent search cache."""
    return {"recent_searches": search_cache_manager.get_recent(limit=10)}


# --- Subtitles (movies / series in Downloads, English only) ---


def _subtitle_service():
    from evatorrent.subtitles.service import SubtitleService

    return SubtitleService(download_dir=engine_manager.download_dir)


@app.get("/api/subtitles/videos")
async def list_subtitle_videos(
    limit: int = Query(500, ge=1, le=2000),
    _: str = Depends(get_current_user),
):
    """Dropdown source: all video files under DOWNLOAD_DIR with subtitle presence."""
    svc = _subtitle_service()
    return {"videos": svc.videos(limit=limit), "download_dir": str(engine_manager.download_dir)}


@app.get("/api/subtitles/search")
async def search_subtitles(
    q: Optional[str] = Query(None, description="Title query (optional when video= is given)"),
    video: Optional[str] = Query(None, description="Relative video path in Downloads; title auto-derived"),
    limit: int = Query(20, ge=1, le=50),
    user: str = Depends(get_current_user),
):
    """Searches English subtitles. Pass video=<relpath> to auto-derive the title."""
    from evatorrent.subtitles.service import clean_video_title

    query = (q or "").strip()
    meta: dict = {}
    if video:
        from pathlib import Path as _P

        meta = {"video": video, "video_name": _P(video).name}
        if not query:
            title, year, season, episode = clean_video_title(_P(video).name)
            query = title
            meta.update({"derived_title": title, "year": year, "season": season, "episode": episode})
            if season and episode:
                query = f"{title} S{season:02d}E{episode:02d}"
            elif year:
                query = f"{title} {year}"
    if not query:
        logger.warning(f"[SUBTITLES] User '{user}' search rejected: no q= or video= given")
        raise HTTPException(status_code=400, detail="Provide q= or video= so a title can be derived.")
    logger.info(f"[SUBTITLES] User '{user}' searching English subs: q='{query}' video='{video or ''}' override='{q or ''}'")
    try:
        result = await _subtitle_service().search(query, limit=limit)
    except ValueError as e:
        logger.warning(f"[SUBTITLES] User '{user}' search failed for '{query}': {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[SUBTITLES] User '{user}' search errored for '{query}': {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Subtitle search failed: {e}")
    n = len(result.get("results", []))
    logger.info(
        "[SUBTITLES] User '%s' search q='%s' -> %d results via %s%s",
        user,
        query,
        n,
        ",".join(result.get("providers_queried", [])),
        f" (provider errors: {result.get('errors')})" if result.get("errors") else "",
    )
    result["meta"] = meta
    return result


class SubtitleDownloadRequest(BaseModel):
    video: str
    download_url: str
    provider: Optional[str] = ""


@app.post("/api/subtitles/download")
async def download_subtitle(req: SubtitleDownloadRequest, user: str = Depends(get_current_user)):
    """Downloads an English subtitle, extracts it server-side (never serves zip),
    validates it, and saves it next to the video with a matching filename."""
    if not req.video or not req.download_url:
        logger.warning(f"[SUBTITLES] User '{user}' download rejected: video/download_url missing")
        raise HTTPException(status_code=400, detail="video and download_url are required.")
    logger.info(
        f"[SUBTITLES] User '{user}' downloading sub for '{req.video}' via {req.provider or 'unknown'}: {req.download_url[:120]}"
    )
    try:
        result = await _subtitle_service().download_for_video(req.video, req.download_url, req.provider or "")
    except ValueError as e:
        logger.warning(f"[SUBTITLES] User '{user}' download for '{req.video}' rejected: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[SUBTITLES] User '{user}' download failed for '{req.video}': {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Subtitle download failed: {e}")
    logger.info(
        "[SUBTITLES] User '%s' saved '%s' (%d bytes, .%s) for video '%s'",
        user,
        result["saved_as"],
        result["size_bytes"],
        result["format"],
        req.video,
    )
    return result


class UIEventRequest(BaseModel):
    tab: str
    action: Optional[str] = ""
    detail: Optional[str] = ""


@app.post("/api/ui/event")
async def ui_event(req: UIEventRequest, user: str = Depends(get_current_user)):
    """Client-side breadcrumb beacon: tab switches, button clicks, modal opens.

    Lets docker logs show what the user was doing (e.g. which tab was open)
    when something went wrong — previously invisible because it never hit the API.
    """
    tab = (req.tab or "").strip()[:32]
    action = (req.action or "").strip()[:64]
    detail = (req.detail or "").strip()[:200]
    if not tab:
        raise HTTPException(status_code=400, detail="tab is required.")
    logger.info(f"[UI] user='{user}' tab='{tab}' action='{action}' detail='{detail}'")
    return {"success": True}


# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
