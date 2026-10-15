//! Axum web application exposing REST API, WebSockets, HTTP Range media streaming, and static UI.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{
    body::Body,
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Multipart, Path as AxumPath, Query, State,
    },
    http::{header, HeaderMap, StatusCode},
    response::{Html, IntoResponse, Json, Response},
    routing::{delete, get, post, put},
    Router,
};
use chrono::{TimeZone, Utc};
use regex::Regex;
use serde::Deserialize;
use serde_json::{json, Value};
use tower_http::cors::CorsLayer;
use tower_http::services::ServeDir;
use tracing::info;

use crate::auth::{AuthConfig, EmailSender, GoogleVerifier, OTPManager, SessionManager};
use crate::db::{now_unix_secs, Database};
use crate::engine::limits::{
    large_torrent_bytes, max_ongoing_pieces, max_peers, pipeline_per_peer, stream_buffer_limit,
    telemetry_interval_secs, verify_on_startup,
};
use crate::engine::manager::EngineManager;
use crate::engine::monitor::{get_system_health, write_heartbeat};
use crate::engine::speed::{ScheduleEntry, SpeedProfile};
use crate::metadata::cache::DiscoverCacheManager;
use crate::metadata::{keys_configured, lookup_media};
use crate::search::cache::SearchCacheManager;
use crate::search::service::SearchService;
use crate::subtitles::service::{clean_video_title, SubtitleService};
use crate::torrent::fetch_torrent_from_caches;
use crate::web::ws::WebSocketManager;
use crate::VERSION;

pub const MAX_TORRENT_UPLOAD_BYTES: usize = 10 * 1024 * 1024; // 10 MB

#[derive(Clone)]
pub struct AppState {
    pub auth_config: AuthConfig,
    pub database: Database,
    pub engine_manager: EngineManager,
    pub search_cache: SearchCacheManager,
    pub discover_cache: DiscoverCacheManager,
    pub ws_manager: WebSocketManager,
    pub session_manager: SessionManager,
    pub otp_manager: OTPManager,
    pub email_sender: EmailSender,
    pub google_verifier: GoogleVerifier,
    pub search_service: SearchService,
    pub static_dir: PathBuf,
    rate_limits: Arc<Mutex<HashMap<String, Vec<f64>>>>,
}

impl AppState {
    pub fn new(data_dir: Option<PathBuf>, download_dir: Option<PathBuf>) -> anyhow::Result<Self> {
        let auth_config = AuthConfig::new(data_dir);
        let db_path = auth_config.data_dir.join("eva.db");
        let database = Database::new(&db_path)?;
        let engine_manager = EngineManager::new(download_dir, Some(database.clone()));
        let cache_dir = engine_manager.download_dir.join("cached_results");
        let search_cache = SearchCacheManager::new(&cache_dir, 100);
        let discover_cache = DiscoverCacheManager::new(&cache_dir, 100);
        let ws_manager = WebSocketManager::new();
        let session_manager = SessionManager::new(auth_config.clone());
        let otp_manager = OTPManager::new(600.0, Some(database.clone()));
        let email_sender = EmailSender::new(auth_config.clone());
        let google_verifier = GoogleVerifier::new(auth_config.clone());
        let search_service = SearchService::new(Some(search_cache.clone()));

        let static_dir = if PathBuf::from("static").exists() {
            PathBuf::from("static")
        } else if PathBuf::from("src/static").exists() {
            PathBuf::from("src/static")
        } else {
            // Fallback for dev: executable dir + static
            std::env::current_exe()
                .ok()
                .and_then(|p| p.parent().map(|d| d.join("static")))
                .unwrap_or_else(|| PathBuf::from("static"))
        };

        Ok(Self {
            auth_config,
            database,
            engine_manager,
            search_cache,
            discover_cache,
            ws_manager,
            session_manager,
            otp_manager,
            email_sender,
            google_verifier,
            search_service,
            static_dir,
            rate_limits: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    fn extract_token(&self, headers: &HeaderMap, query_token: Option<&str>) -> Option<String> {
        if let Some(cookie_hdr) = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()) {
            for part in cookie_hdr.split(';') {
                let trimmed = part.trim();
                if let Some(val) = trimmed.strip_prefix("evatorrent_session=") {
                    if !val.is_empty() {
                        return Some(val.to_string());
                    }
                }
            }
        }
        if let Some(auth_hdr) = headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
        {
            if let Some(token) = auth_hdr.strip_prefix("Bearer ") {
                let clean = token.trim();
                if !clean.is_empty() {
                    return Some(clean.to_string());
                }
            }
        }
        query_token
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.trim().to_string())
    }

    fn require_user(
        &self,
        headers: &HeaderMap,
        query_token: Option<&str>,
    ) -> Result<String, (StatusCode, Json<Value>)> {
        if !self.auth_config.is_setup_done() {
            return Err((
                StatusCode::UNAUTHORIZED,
                Json(json!({"detail": "SETUP_REQUIRED"})),
            ));
        }
        let Some(token) = self.extract_token(headers, query_token) else {
            return Err((
                StatusCode::UNAUTHORIZED,
                Json(json!({"detail": "UNAUTHORIZED"})),
            ));
        };
        self.session_manager.verify_token(&token).ok_or_else(|| {
            (
                StatusCode::UNAUTHORIZED,
                Json(json!({"detail": "INVALID_OR_EXPIRED_SESSION"})),
            )
        })
    }

    fn check_rate_limit(
        &self,
        headers: &HeaderMap,
        max_requests: usize,
        window_secs: f64,
    ) -> Result<(), (StatusCode, Json<Value>)> {
        let ip = headers
            .get("x-forwarded-for")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.split(',').next())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "127.0.0.1".to_string());

        let now = now_unix_secs();
        let mut guard = self.rate_limits.lock().unwrap();
        let list = guard.entry(ip).or_default();
        list.retain(|&ts| now - ts < window_secs);
        if list.len() >= max_requests {
            let retry_after = (window_secs - (now - list[0])).max(1.0) as u64;
            return Err((
                StatusCode::TOO_MANY_REQUESTS,
                Json(json!({
                    "detail": format!(
                        "Rate limit exceeded: maximum {} requests per minute. Try again in {}s.",
                        max_requests, retry_after
                    )
                })),
            ));
        }
        list.push(now);
        Ok(())
    }
}

fn api_err(status: StatusCode, detail: impl Into<String>) -> (StatusCode, Json<Value>) {
    (status, Json(json!({ "detail": detail.into() })))
}

fn build_session_cookie(token: &str, headers: &HeaderMap) -> String {
    let secure_env = std::env::var("SECURE_COOKIES")
        .map(|s| matches!(s.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false);
    let proto_https = headers
        .get("x-forwarded-proto")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.eq_ignore_ascii_case("https"))
        .unwrap_or(false);
    let secure_flag = if secure_env || proto_https {
        "; Secure"
    } else {
        ""
    };
    format!(
        "evatorrent_session={}; HttpOnly; SameSite=Lax; Path=/; Max-Age={}{}",
        token,
        86400 * 30,
        secure_flag
    )
}

pub fn build_router(state: AppState) -> Router {
    let static_service = ServeDir::new(state.static_dir.clone());

    Router::new()
        // Index routes
        .route("/", get(serve_index))
        .route("/index.html", get(serve_index))
        .route("/home", get(serve_index))
        .route("/search", get(serve_index))
        .route("/discover", get(serve_index))
        .route("/subtitle", get(serve_index))
        .route("/report", get(serve_index))
        // Auth endpoints
        .route("/api/auth/status", get(auth_status))
        .route("/api/auth/setup", post(initial_setup))
        .route("/api/auth/otp/request", post(request_otp))
        .route("/api/auth/otp/verify", post(verify_otp))
        .route("/api/auth/google", post(google_login))
        .route("/api/auth/logout", post(logout))
        // System & Analytics endpoints
        .route("/api/system/health", get(system_health_handler))
        .route("/api/stats", get(get_stats))
        .route("/api/analysis/summary", get(get_analysis_summary))
        .route("/api/analysis/torrents", get(get_analysis_torrents))
        .route("/api/analysis/export.csv", get(export_analysis_csv))
        // Torrent endpoints
        .route("/api/torrents", get(list_torrents))
        .route("/api/torrents/upload", post(upload_torrent))
        .route("/api/torrents/magnet", post(add_magnet))
        .route("/api/torrents/add", post(add_magnet))
        .route("/api/torrents/url", post(add_magnet))
        .route("/api/torrents/batch", post(add_batch_torrents))
        .route("/api/torrents/bulk", post(bulk_torrent_action))
        .route("/api/torrents/download_file", get(download_torrent_file))
        .route("/api/torrents/file", get(download_torrent_file))
        .route("/api/search/download_torrent", get(download_torrent_file))
        .route("/api/torrents/:info_hash", delete(delete_torrent))
        .route("/api/torrents/:info_hash/pause", post(pause_torrent))
        .route("/api/torrents/:info_hash/resume", post(resume_torrent))
        .route(
            "/api/torrents/:info_hash/speed_limit",
            post(set_torrent_speed_limit),
        )
        .route(
            "/api/torrents/:info_hash/sequential",
            post(set_torrent_sequential),
        )
        .route(
            "/api/torrents/:info_hash/files",
            get(get_torrent_files).post(set_torrent_files),
        )
        .route("/api/torrents/:info_hash/pieces", get(get_torrent_pieces))
        .route("/api/torrents/:info_hash/peers", get(get_torrent_peers))
        .route("/api/torrents/:info_hash/events", get(get_torrent_events))
        .route(
            "/api/torrents/:info_hash/stream/:file_idx",
            get(stream_torrent_file),
        )
        // Search, Discover, Speed, Subtitles, UI events, WebSocket
        .route("/api/search", get(search_torrents))
        .route("/api/search/recent", get(get_recent_searches))
        .route("/api/discover", get(discover_media))
        .route("/api/discover/keys", get(discover_keys))
        .route("/api/discover/recent", get(get_discover_recent))
        .route(
            "/api/discover/saved",
            get(list_saved_discover).post(save_discover_item),
        )
        .route(
            "/api/discover/saved/:saved_id",
            put(update_saved_remarks).delete(delete_saved_discover),
        )
        .route("/api/speed", get(get_speed_config).put(put_speed_config))
        .route("/api/speed/override", post(post_speed_override))
        .route("/api/subtitles/videos", get(list_subtitle_videos))
        .route("/api/subtitles/search", get(search_subtitles))
        .route("/api/subtitles/download", post(download_subtitle))
        .route("/api/ui/event", post(ui_event))
        .route("/ws", get(ws_handler))
        .nest_service("/static", static_service)
        .layer(CorsLayer::permissive())
        .with_state(state)
}

pub async fn run_web_server(host: &str, port: u16) -> anyhow::Result<()> {
    let state = AppState::new(None, None)?;

    info!(
        "[STARTUP] evaTorrent v{} (Rust/Tokio) | download_dir={} | limits: max_peers={} pipeline={} ongoing={} stream_buf={}B telemetry={:.1}s verify={} large_at={}GB",
        VERSION,
        state.engine_manager.download_dir.display(),
        max_peers(),
        pipeline_per_peer(),
        max_ongoing_pieces(),
        stream_buffer_limit(),
        telemetry_interval_secs(),
        verify_on_startup(),
        large_torrent_bytes() / (1024 * 1024 * 1024)
    );

    state.engine_manager.start_monitor();
    state.engine_manager.load_speed_config();
    state.engine_manager.evaluate_speed_schedule(true);

    let eng_boot = state.engine_manager.clone();
    tokio::spawn(async move {
        let trackers = eng_boot.refresh_fallback_trackers(false).await;
        info!(
            "[STARTUP] Tracker fallbacks ready: {} auto-updated + baked-in defaults",
            trackers.len()
        );
        let summary = eng_boot.restore_from_db().await;
        info!("[STARTUP] Swarm auto-resume from DB: {}", summary);
    });

    // Spawn WebSocket telemetry loop
    let tel_state = state.clone();
    tokio::spawn(async move {
        let interval = Duration::from_secs_f64(telemetry_interval_secs());
        let mut tick = 0u64;
        loop {
            tokio::time::sleep(interval).await;
            tick += 1;
            if tel_state.ws_manager.has_connections() {
                let payload = json!({
                    "type": "telemetry",
                    "stats": tel_state.engine_manager.get_global_stats(),
                    "torrents": tel_state.engine_manager.get_swarm_snapshot(),
                });
                tel_state.ws_manager.broadcast(&payload);
            }
            if tick.is_multiple_of(35) {
                write_heartbeat(&tel_state.engine_manager.data_dir());
                tel_state.engine_manager.evaluate_speed_schedule(false);
            }
        }
    });

    let app = build_router(state.clone());
    let bind_addr = format!("{}:{}", host, port);
    info!("Starting evaTorrent Web UI on http://{}", bind_addr);
    let listener = tokio::net::TcpListener::bind(&bind_addr).await?;
    axum::serve(listener, app).await?;
    state.engine_manager.shutdown().await;
    Ok(())
}

// -----------------------------------------------------------------------------
// Route Handlers
// -----------------------------------------------------------------------------

async fn serve_index(State(state): State<AppState>) -> Response {
    let index_path = state.static_dir.join("index.html");
    if let Ok(content) = std::fs::read_to_string(&index_path) {
        (
            [
                (header::CACHE_CONTROL, "no-cache, no-store, must-revalidate"),
                (header::PRAGMA, "no-cache"),
            ],
            Html(content),
        )
            .into_response()
    } else {
        Json(json!({"message": "evaTorrent Rust API is running."})).into_response()
    }
}

async fn auth_status(State(state): State<AppState>, headers: HeaderMap) -> Json<Value> {
    let token = state.extract_token(&headers, None);
    let verified_email = token.and_then(|t| state.session_manager.verify_token(&t));

    let display_admin = state.auth_config.admin_email().map(|adm| {
        if let Some((user, domain)) = adm.split_once('@') {
            let prefix: String = user.chars().take(3).collect();
            format!("{}***@{}", prefix, domain)
        } else {
            "***".to_string()
        }
    });

    Json(json!({
        "setup_required": !state.auth_config.is_setup_done(),
        "admin_email_masked": display_admin,
        "google_enabled": state.auth_config.google_client_id().is_some(),
        "google_client_id": state.auth_config.google_client_id(),
        "is_authenticated": verified_email.is_some(),
        "user_email": verified_email,
        "smtp_configured": state.auth_config.is_smtp_configured(),
    }))
}

#[derive(Deserialize)]
struct SetupRequest {
    admin_email: String,
    google_client_id: Option<String>,
}

async fn initial_setup(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<SetupRequest>,
) -> Result<Response, (StatusCode, Json<Value>)> {
    if state.auth_config.is_setup_done() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Setup has already been completed.",
        ));
    }
    let email = req.admin_email.trim().to_lowercase();
    if email.is_empty() || !email.contains('@') || !email.contains('.') {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Please enter a valid email address.",
        ));
    }
    state.auth_config.set_admin_email(&email);
    if let Some(ref gid) = req.google_client_id {
        state.auth_config.set_google_client_id(Some(gid));
    }

    let token = state.session_manager.create_token(&email, 86400 * 30);
    let cookie = build_session_cookie(&token, &headers);
    Ok((
        [(header::SET_COOKIE, cookie)],
        Json(json!({
            "success": true,
            "token": token,
            "email": email,
        })),
    )
        .into_response())
}

#[derive(Deserialize)]
struct OTPRequest {
    email: String,
}

async fn request_otp(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<OTPRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    state.check_rate_limit(&headers, 2, 60.0)?;
    if !state.auth_config.is_setup_done() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Initial setup required first.",
        ));
    }
    let email = req.email.trim().to_lowercase();
    if Some(email.clone()) != state.auth_config.admin_email() {
        return Err(api_err(
            StatusCode::FORBIDDEN,
            "Email is not authorized for this evaTorrent instance.",
        ));
    }
    let (ok, msg, otp_opt) = state.otp_manager.generate_otp(&email);
    let Some(otp) = otp_opt.filter(|_| ok) else {
        return Err(api_err(StatusCode::TOO_MANY_REQUESTS, msg));
    };
    state.email_sender.send_otp(&email, &otp).await;
    Ok(Json(json!({
        "success": true,
        "message": "Verification code dispatched! Check your email (or server logs).",
        "smtp_configured": state.auth_config.is_smtp_configured(),
    })))
}

#[derive(Deserialize)]
struct OTPVerifyRequest {
    email: String,
    otp: String,
}

async fn verify_otp(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<OTPVerifyRequest>,
) -> Result<Response, (StatusCode, Json<Value>)> {
    if !state.auth_config.is_setup_done() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Initial setup required first.",
        ));
    }
    let email = req.email.trim().to_lowercase();
    if Some(email.clone()) != state.auth_config.admin_email() {
        return Err(api_err(StatusCode::FORBIDDEN, "Email is not authorized."));
    }
    if !state.otp_manager.verify_otp(&email, &req.otp) {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Invalid or expired verification code.",
        ));
    }
    let token = state.session_manager.create_token(&email, 86400 * 30);
    let cookie = build_session_cookie(&token, &headers);
    Ok((
        [(header::SET_COOKIE, cookie)],
        Json(json!({
            "success": true,
            "token": token,
            "email": email,
        })),
    )
        .into_response())
}

#[derive(Deserialize)]
struct GoogleAuthRequest {
    credential: String,
}

async fn google_login(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<GoogleAuthRequest>,
) -> Result<Response, (StatusCode, Json<Value>)> {
    if !state.auth_config.is_setup_done() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Initial setup required first.",
        ));
    }
    let Some(email) = state.google_verifier.verify_id_token(&req.credential).await else {
        return Err(api_err(
            StatusCode::FORBIDDEN,
            "Google sign-in failed: Account email is not authorized for this instance.",
        ));
    };
    let token = state.session_manager.create_token(&email, 86400 * 30);
    let cookie = build_session_cookie(&token, &headers);
    Ok((
        [(header::SET_COOKIE, cookie)],
        Json(json!({
            "success": true,
            "token": token,
            "email": email,
        })),
    )
        .into_response())
}

async fn logout() -> Response {
    (
        [(
            header::SET_COOKIE,
            "evatorrent_session=; HttpOnly; Path=/; Max-Age=0",
        )],
        Json(json!({"success": true})),
    )
        .into_response()
}

async fn system_health_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let health = get_system_health(&state.engine_manager.download_dir);
    Ok(Json(json!({
        "version": VERSION,
        "engine": "rust-tokio",
        "rss_mb": health.rss_mb,
        "cgroup_limit_mb": health.cgroup_limit_mb,
        "cgroup_oom_kills": health.cgroup_oom_kills,
        "cpu_throttled_secs": health.cpu_throttled_secs,
        "free_disk_mb": health.free_disk_mb,
        "total_disk_mb": health.total_disk_mb,
    })))
}

async fn get_stats(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(state.engine_manager.get_global_stats()))
}

async fn get_analysis_summary(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!(state.database.get_analytics_summary())))
}

#[derive(Deserialize)]
struct AnalysisQuery {
    status: Option<String>,
    search: Option<String>,
    limit: Option<usize>,
    offset: Option<usize>,
}

async fn get_analysis_torrents(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<AnalysisQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let records = state.database.get_all_history(
        q.status.as_deref(),
        q.search.as_deref(),
        q.limit.unwrap_or(100),
        q.offset.unwrap_or(0),
    );
    let summary = state.database.get_analytics_summary();
    Ok(Json(json!({
        "torrents": records,
        "summary": summary,
    })))
}

fn csv_escape(s: &str) -> String {
    if s.contains(',') || s.contains('"') || s.contains('\n') {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

fn fmt_utc(ts: Option<f64>) -> String {
    ts.and_then(|t| Utc.timestamp_opt(t as i64, 0).single())
        .map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string())
        .unwrap_or_default()
}

async fn export_analysis_csv(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let records = state.database.get_all_history(Some("all"), None, 50000, 0);
    let mut out = String::from(
        "Info Hash,Name,Total Size (Bytes),Downloaded (Bytes),Uploaded (Bytes),Status,Added At (UTC),Completed At (UTC),Removed At (UTC),Error Message,Download Directory\n",
    );
    for r in records {
        let row = [
            csv_escape(&r.info_hash),
            csv_escape(&r.name),
            r.total_size.to_string(),
            r.downloaded_bytes.to_string(),
            r.uploaded_bytes.to_string(),
            csv_escape(&r.status),
            fmt_utc(Some(r.added_at)),
            fmt_utc(r.completed_at),
            fmt_utc(r.removed_at),
            csv_escape(r.error_message.as_deref().unwrap_or("")),
            csv_escape(r.download_dir.as_deref().unwrap_or("")),
        ];
        out.push_str(&row.join(","));
        out.push('\n');
    }
    Ok((
        [
            (header::CONTENT_TYPE, "text/csv; charset=utf-8"),
            (
                header::CONTENT_DISPOSITION,
                "attachment; filename=evatorrent_analysis.csv",
            ),
        ],
        out,
    )
        .into_response())
}

async fn list_torrents(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!(state.engine_manager.get_swarm_snapshot())))
}

async fn upload_torrent(
    State(state): State<AppState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _user = state.require_user(&headers, None)?;
    while let Ok(Some(field)) = multipart.next_field().await {
        if field.name() == Some("file") {
            let bytes = field
                .bytes()
                .await
                .map_err(|e| api_err(StatusCode::BAD_REQUEST, e.to_string()))?;
            if bytes.len() > MAX_TORRENT_UPLOAD_BYTES {
                return Err(api_err(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "Torrent file exceeds 10 MB size limit.",
                ));
            }
            let session = state
                .engine_manager
                .add_torrent_bytes(bytes.to_vec(), None)
                .map_err(|e| {
                    api_err(
                        StatusCode::BAD_REQUEST,
                        format!("Failed to parse torrent: {}", e),
                    )
                })?;
            return Ok(Json(json!({
                "success": true,
                "info_hash": session.torrent.info_hash_hex,
                "name": session.torrent.name,
            })));
        }
    }
    Err(api_err(StatusCode::BAD_REQUEST, "No torrent file uploaded"))
}

#[derive(Deserialize)]
struct MagnetRequest {
    magnet: String,
    #[serde(default)]
    sequential: bool,
}

async fn add_magnet(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<MagnetRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _user = state.require_user(&headers, None)?;
    let session = state
        .engine_manager
        .add_torrent_or_url(&req.magnet, None)
        .await
        .map_err(|e| api_err(StatusCode::UNPROCESSABLE_ENTITY, e.to_string()))?;
    if req.sequential {
        session.set_sequential(true);
    }
    Ok(Json(json!({
        "success": true,
        "info_hash": session.torrent.info_hash_hex,
        "name": session.torrent.name,
        "message": format!("Started download: {}", session.torrent.name),
    })))
}

#[derive(Deserialize)]
struct BatchMagnetRequest {
    items: Vec<String>,
    #[serde(default)]
    sequential: bool,
}

async fn add_batch_torrents(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<BatchMagnetRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _user = state.require_user(&headers, None)?;
    let mut added = Vec::new();
    let mut errors = Vec::new();
    for raw in req.items {
        let clean = raw.trim();
        if clean.is_empty() {
            continue;
        }
        match state.engine_manager.add_torrent_or_url(clean, None).await {
            Ok(sess) => {
                if req.sequential {
                    sess.set_sequential(true);
                }
                added.push(json!({
                    "info_hash": sess.torrent.info_hash_hex,
                    "name": sess.torrent.name,
                }));
            }
            Err(e) => {
                errors.push(json!({"input": clean, "error": e.to_string()}));
            }
        }
    }
    Ok(Json(json!({
        "success": !added.is_empty(),
        "added_count": added.len(),
        "added": added,
        "errors": errors,
    })))
}

#[derive(Deserialize)]
struct BulkActionRequest {
    action: String, // "pause_all" | "resume_all" | "clear_completed"
}

async fn bulk_torrent_action(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<BulkActionRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _user = state.require_user(&headers, None)?;
    let affected = match req.action.trim().to_lowercase().as_str() {
        "pause_all" => state.engine_manager.pause_all().await,
        "resume_all" => state.engine_manager.resume_all(),
        "clear_completed" => state.engine_manager.clear_completed().await,
        other => {
            return Err(api_err(
                StatusCode::BAD_REQUEST,
                format!("Unknown bulk action: {}", other),
            ))
        }
    };
    Ok(Json(json!({
        "success": true,
        "action": req.action,
        "affected": affected,
    })))
}

#[derive(Deserialize)]
struct DownloadTorrentQuery {
    info_hash: String,
    name: Option<String>,
    token: Option<String>,
}

async fn download_torrent_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<DownloadTorrentQuery>,
) -> Result<Response, (StatusCode, Json<Value>)> {
    let _user = state.require_user(&headers, q.token.as_deref())?;
    let clean_hash = q.info_hash.trim().to_lowercase();
    if clean_hash.len() != 40 || !clean_hash.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Invalid 40-character info hash.",
        ));
    }

    let cache_dir = state.engine_manager.download_dir.join(".torrent_cache");
    let _ = std::fs::create_dir_all(&cache_dir);
    let torrent_file = cache_dir.join(format!("{}.torrent", clean_hash));

    let mut raw_bytes = if torrent_file.exists() {
        std::fs::read(&torrent_file).ok()
    } else {
        None
    };

    if raw_bytes.is_none() {
        raw_bytes = fetch_torrent_from_caches(&clean_hash, 8.0).await;
        if let Some(ref b) = raw_bytes {
            let _ = std::fs::write(&torrent_file, b);
        }
    }

    let Some(bytes) = raw_bytes else {
        return Err(api_err(
            StatusCode::NOT_FOUND,
            "Metainfo file (.torrent) is not yet available in public caches. You can still download via Magnet link directly in evaTorrent.",
        ));
    };

    let re_bad = Regex::new(r#"[/\\?%*:|"<>]+"#).unwrap();
    let mut clean_name = q
        .name
        .as_deref()
        .map(|n| re_bad.replace_all(n.trim(), "_").into_owned())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| clean_hash[..12].to_string());
    if !clean_name.to_lowercase().ends_with(".torrent") {
        clean_name.push_str(".torrent");
    }

    let disp = format!("attachment; filename=\"{}\"", clean_name);
    Ok((
        [
            (header::CONTENT_TYPE, "application/x-bittorrent".to_string()),
            (header::CONTENT_DISPOSITION, disp),
        ],
        bytes,
    )
        .into_response())
}

async fn pause_torrent(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if state.engine_manager.pause_torrent(&info_hash).await {
        return Ok(Json(json!({"success": true})));
    }
    if let Some(row) = state.database.get_torrent_history(&info_hash) {
        state.database.update_torrent_progress(
            &info_hash,
            row.downloaded_bytes,
            row.uploaded_bytes,
            Some("paused"),
        );
        return Ok(Json(json!({"success": true, "source": "db"})));
    }
    Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"))
}

async fn resume_torrent(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if state.engine_manager.resume_torrent(&info_hash) {
        return Ok(Json(json!({"success": true})));
    }
    if state.engine_manager.resume_db_torrent(&info_hash).await {
        return Ok(Json(json!({
            "success": true,
            "source": "db",
            "message": "Session rebuilt from database and resumed."
        })));
    }
    Err(api_err(
        StatusCode::NOT_FOUND,
        "Torrent not found in live swarm or database. Re-add the .torrent/magnet.",
    ))
}

#[derive(Deserialize)]
struct SpeedLimitRequest {
    download_limit: Option<u64>,
}

async fn set_torrent_speed_limit(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
    Json(req): Json<SpeedLimitRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if !state
        .engine_manager
        .set_speed_limit(&info_hash, req.download_limit)
    {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    }
    Ok(Json(json!({
        "success": true,
        "download_limit": req.download_limit,
    })))
}

#[derive(Deserialize)]
struct SequentialRequest {
    sequential: bool,
}

async fn set_torrent_sequential(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
    Json(req): Json<SequentialRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if !state
        .engine_manager
        .set_sequential(&info_hash, req.sequential)
    {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    }
    Ok(Json(json!({
        "success": true,
        "sequential": req.sequential,
    })))
}

async fn get_torrent_files(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(session) = state.engine_manager.get_session(&info_hash) else {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    };
    let pm = &session.piece_manager;
    let prog_map: HashMap<String, _> = pm
        .file_progress()
        .into_iter()
        .map(|p| (p.path.clone(), p))
        .collect();

    let files: Vec<Value> = session
        .torrent
        .files
        .iter()
        .enumerate()
        .map(|(idx, f)| {
            let p = prog_map.get(&f.path);
            json!({
                "index": idx,
                "path": f.path,
                "length": f.length,
                "offset": f.offset,
                "selected": pm.is_file_selected(&f.path),
                "priority": pm.get_file_priority(&f.path),
                "done_bytes": p.map(|x| x.done_bytes).unwrap_or(0),
                "progress": p.map(|x| x.progress).unwrap_or(0.0),
            })
        })
        .collect();

    Ok(Json(json!({
        "files": files,
        "selected_bytes": pm.selected_total_bytes(),
        "total_bytes": session.torrent.total_length,
    })))
}

#[derive(Deserialize)]
struct FileSelectionRequest {
    selected_paths: Option<Vec<String>>,
    priorities: Option<HashMap<String, u8>>,
}

async fn set_torrent_files(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
    Json(req): Json<FileSelectionRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(session) = state.engine_manager.get_session(&info_hash) else {
        if state.database.get_torrent_history(&info_hash).is_some() {
            if let Some(ref prio) = req.priorities {
                state.database.set_file_priorities(&info_hash, Some(prio));
            } else {
                state
                    .database
                    .set_file_selection(&info_hash, req.selected_paths.as_deref());
            }
            return Ok(Json(json!({"success": true, "source": "db"})));
        }
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    };

    let summary = if let Some(prio) = req.priorities {
        session
            .set_file_priorities(prio)
            .map_err(|e| api_err(StatusCode::BAD_REQUEST, e))?
    } else {
        session
            .set_selected_files(req.selected_paths)
            .map_err(|e| api_err(StatusCode::BAD_REQUEST, e))?
    };

    let mut resp = summary.as_object().cloned().unwrap_or_default();
    resp.insert("success".to_string(), json!(true));
    Ok(Json(Value::Object(resp)))
}

#[derive(Deserialize)]
struct DeleteTorrentQuery {
    #[serde(default)]
    delete_files: bool,
}

async fn delete_torrent(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
    Query(q): Query<DeleteTorrentQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if !state
        .engine_manager
        .remove_torrent(&info_hash, q.delete_files)
        .await
    {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    }
    Ok(Json(json!({"success": true})))
}

#[derive(Deserialize)]
struct PiecesQuery {
    limit: Option<usize>,
    offset: Option<usize>,
}

async fn get_torrent_pieces(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
    Query(q): Query<PiecesQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(session) = state.engine_manager.get_session(&info_hash) else {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    };
    let limit = q.limit.unwrap_or(5000).clamp(1, 100_000);
    let offset = q.offset.unwrap_or(0);
    let (completed, ongoing) = session.piece_manager.completed_and_ongoing_snapshot();
    let total_comp = completed.len();
    let page: Vec<usize> = completed.into_iter().skip(offset).take(limit).collect();
    let ongoing_slice: Vec<usize> = ongoing.into_iter().take(limit).collect();

    Ok(Json(json!({
        "total_pieces": session.torrent.piece_count,
        "completed_total": total_comp,
        "completed_indices": page,
        "offset": offset,
        "limit": limit,
        "truncated": offset + page.len() < total_comp,
        "ongoing_indices": ongoing_slice,
    })))
}

async fn get_torrent_peers(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(session) = state.engine_manager.get_session(&info_hash) else {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    };
    Ok(Json(json!({
        "peers": session.active_peers_snapshot(),
        "trackers": session.tracker_manager.get_health(),
    })))
}

async fn get_torrent_events(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(info_hash): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!({
        "events": state.database.get_torrent_events(&info_hash, 50),
    })))
}

/// New feature: HTTP `206 Partial Content` byte-range streaming & direct file download.
async fn stream_torrent_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath((info_hash, file_idx)): AxumPath<(String, usize)>,
) -> Result<Response, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(session) = state.engine_manager.get_session(&info_hash) else {
        return Err(api_err(StatusCode::NOT_FOUND, "Torrent not found"));
    };
    let Some(file_info) = session.torrent.files.get(file_idx) else {
        return Err(api_err(StatusCode::NOT_FOUND, "File index not found"));
    };

    let total_len = file_info.length;
    let ext = std::path::Path::new(&file_info.path)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_lowercase();
    let content_type = match ext.as_str() {
        "mp4" | "m4v" => "video/mp4",
        "webm" => "video/webm",
        "mkv" => "video/x-matroska",
        "mp3" => "audio/mpeg",
        "srt" => "text/plain; charset=utf-8",
        "vtt" => "text/vtt; charset=utf-8",
        _ => "application/octet-stream",
    };

    let max_chunk: u64 = 4 * 1024 * 1024; // 4 MiB per range chunk
    if let Some(range_hdr) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()) {
        if let Some(spec) = range_hdr.strip_prefix("bytes=") {
            let (start_s, end_s) = spec.split_once('-').unwrap_or((spec, ""));
            let start = start_s
                .parse::<u64>()
                .unwrap_or(0)
                .min(total_len.saturating_sub(1));
            let end = end_s
                .parse::<u64>()
                .ok()
                .unwrap_or_else(|| (start + max_chunk - 1).min(total_len.saturating_sub(1)))
                .min(total_len.saturating_sub(1));
            let len = (end.saturating_sub(start) + 1) as usize;
            let bytes = session
                .piece_manager
                .disk_writer
                .read_file_range(file_idx, start, len)
                .map_err(|e| api_err(StatusCode::NOT_FOUND, e.to_string()))?;
            let content_range = format!(
                "bytes {}-{}/{}",
                start,
                start + (bytes.len() as u64).saturating_sub(1),
                total_len
            );
            return Ok(Response::builder()
                .status(StatusCode::PARTIAL_CONTENT)
                .header(header::CONTENT_TYPE, content_type)
                .header(header::ACCEPT_RANGES, "bytes")
                .header(header::CONTENT_RANGE, content_range)
                .header(header::CONTENT_LENGTH, bytes.len().to_string())
                .body(Body::from(bytes))
                .unwrap());
        }
    }

    let read_len = (total_len.min(16 * 1024 * 1024)) as usize;
    let bytes = session
        .piece_manager
        .disk_writer
        .read_file_range(file_idx, 0, read_len)
        .map_err(|e| api_err(StatusCode::NOT_FOUND, e.to_string()))?;
    let filename = std::path::Path::new(&file_info.path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("download.bin");
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, content_type)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(
            header::CONTENT_DISPOSITION,
            format!("inline; filename=\"{}\"", filename),
        )
        .body(Body::from(bytes))
        .unwrap())
}

#[derive(Deserialize)]
struct SearchQuery {
    q: String,
    #[serde(default = "default_all")]
    category: String,
    #[serde(default = "default_true")]
    hide_dead: bool,
    #[serde(default = "default_limit_100")]
    limit: usize,
    #[serde(default = "default_timeout_30")]
    timeout: f64,
    #[serde(default)]
    refresh: bool,
    #[serde(default)]
    hindi: bool,
    #[serde(default)]
    english: bool,
}

fn default_all() -> String {
    "all".to_string()
}
fn default_true() -> bool {
    true
}
fn default_limit_100() -> usize {
    100
}
fn default_timeout_30() -> f64 {
    30.0
}

async fn search_torrents(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(sq): Query<SearchQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let res = state
        .search_service
        .search(
            &sq.q,
            &sq.category,
            sq.hide_dead,
            sq.limit.clamp(1, 200),
            sq.timeout.clamp(5.0, 300.0),
            sq.refresh,
            sq.hindi,
            sq.english,
        )
        .await;
    Ok(Json(res))
}

async fn get_recent_searches(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!({
        "recent_searches": state.search_cache.get_recent(10),
    })))
}

async fn discover_keys(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!({
        "keys_configured": keys_configured(),
    })))
}

#[derive(Deserialize)]
struct DiscoverQuery {
    q: String,
    #[serde(rename = "type", default = "default_all")]
    media_type: String,
    #[serde(default = "default_limit_8")]
    limit: usize,
    #[serde(default = "default_timeout_20")]
    timeout: f64,
    year: Option<i64>,
    #[serde(default)]
    refresh: bool,
}

fn default_limit_8() -> usize {
    8
}
fn default_timeout_20() -> f64 {
    20.0
}

async fn discover_media(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(dq): Query<DiscoverQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let t0 = now_unix_secs();
    if !dq.refresh {
        if let Some(cached) = state.discover_cache.get(&dq.q, &dq.media_type, dq.year) {
            return Ok(Json(cached));
        }
    }
    let mut data = lookup_media(
        &dq.q,
        &dq.media_type,
        dq.limit.clamp(1, 20),
        dq.timeout.clamp(5.0, 60.0),
        dq.year,
    )
    .await;
    let elapsed = ((now_unix_secs() - t0) * 100.0).round() / 100.0;
    if let Some(obj) = data.as_object_mut() {
        obj.insert("elapsed_seconds".to_string(), json!(elapsed));
        obj.insert(
            "ott_accuracy_note".to_string(),
            json!("India OTT data is approximate (JustWatch search) unless TMDB_API_KEY is configured."),
        );
    }
    state
        .discover_cache
        .set(&dq.q, &dq.media_type, dq.year, &data);
    Ok(Json(data))
}

async fn get_discover_recent(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!({
        "recent_searches": state.discover_cache.get_recent(10),
    })))
}

#[derive(Deserialize)]
struct SavedDiscoverQuery {
    #[serde(rename = "type")]
    media_type: Option<String>,
    search: Option<String>,
}

async fn list_saved_discover(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<SavedDiscoverQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(json!({
        "saved": state.database.get_saved_discover(q.media_type.as_deref(), q.search.as_deref(), 200, 0),
    })))
}

#[derive(Deserialize)]
struct DiscoverSaveRequest {
    item: Value,
    remarks: Option<String>,
}

async fn save_discover_item(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<DiscoverSaveRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let has_title = req
        .item
        .get("title")
        .and_then(|v| v.as_str())
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false);
    if !has_title {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "A result item with a title is required.",
        ));
    }
    let saved = state
        .database
        .save_discover_item(&req.item, req.remarks.as_deref().unwrap_or(""));
    Ok(Json(json!({"success": true, "saved": saved})))
}

#[derive(Deserialize)]
struct DiscoverRemarksRequest {
    remarks: Option<String>,
}

async fn update_saved_remarks(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(saved_id): AxumPath<i64>,
    Json(req): Json<DiscoverRemarksRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let Some(updated) = state
        .database
        .update_discover_remarks(saved_id, req.remarks.as_deref().unwrap_or(""))
    else {
        return Err(api_err(StatusCode::NOT_FOUND, "Saved entry not found"));
    };
    Ok(Json(json!({"success": true, "saved": updated})))
}

async fn delete_saved_discover(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(saved_id): AxumPath<i64>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if !state.database.delete_saved_discover(saved_id) {
        return Err(api_err(StatusCode::NOT_FOUND, "Saved entry not found"));
    }
    Ok(Json(json!({"success": true})))
}

async fn get_speed_config(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    Ok(Json(state.engine_manager.speed_state_json()))
}

#[derive(Deserialize)]
struct SpeedProfilesRequest {
    profiles: Option<Vec<SpeedProfile>>,
    schedule: Option<Vec<ScheduleEntry>>,
}

async fn put_speed_config(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<SpeedProfilesRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if req.profiles.is_none() && req.schedule.is_none() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Provide profiles and/or schedule.",
        ));
    }
    state
        .engine_manager
        .save_speed_config(req.profiles, req.schedule)
        .map_err(|e| api_err(StatusCode::BAD_REQUEST, e))?;
    let mut cur = state
        .engine_manager
        .speed_state_json()
        .as_object()
        .cloned()
        .unwrap_or_default();
    cur.insert("success".to_string(), json!(true));
    Ok(Json(Value::Object(cur)))
}

#[derive(Deserialize)]
struct SpeedOverrideRequest {
    profile: Option<String>,
}

async fn post_speed_override(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<SpeedOverrideRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let active = state
        .engine_manager
        .set_speed_override(req.profile)
        .map_err(|e| api_err(StatusCode::BAD_REQUEST, e))?;
    let cur = state.engine_manager.speed_state_json();
    Ok(Json(json!({
        "success": true,
        "active_profile": active,
        "global_limit": cur.get("global_limit"),
    })))
}

#[derive(Deserialize)]
struct SubtitleVideosQuery {
    limit: Option<usize>,
}

async fn list_subtitle_videos(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<SubtitleVideosQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let svc = SubtitleService::new(&state.engine_manager.download_dir, 15.0);
    Ok(Json(json!({
        "videos": svc.videos(q.limit.unwrap_or(500).clamp(1, 2000)),
        "download_dir": state.engine_manager.download_dir.to_string_lossy(),
    })))
}

#[derive(Deserialize)]
struct SubtitleSearchQuery {
    q: Option<String>,
    video: Option<String>,
    limit: Option<usize>,
}

async fn search_subtitles(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(sq): Query<SubtitleSearchQuery>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    let mut query = sq.q.unwrap_or_default().trim().to_string();
    let mut meta = json!({});
    if let Some(ref vid) = sq.video.as_ref().filter(|s| !s.trim().is_empty()) {
        let vname = std::path::Path::new(vid)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or(vid);
        if query.is_empty() {
            let (title, year, season, episode) = clean_video_title(vname);
            query = title.clone();
            meta = json!({
                "video": vid,
                "video_name": vname,
                "derived_title": title,
                "year": year,
                "season": season,
                "episode": episode,
            });
        } else {
            meta = json!({"video": vid, "video_name": vname});
        }
    }
    if query.is_empty() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "Provide q= or video= so a title can be derived.",
        ));
    }
    let svc = SubtitleService::new(&state.engine_manager.download_dir, 15.0);
    let mut res = svc
        .search(&query, sq.limit.unwrap_or(20).clamp(1, 50))
        .await
        .map_err(|e| api_err(StatusCode::BAD_GATEWAY, e))?;
    if let Some(obj) = res.as_object_mut() {
        obj.insert("meta".to_string(), meta);
    }
    Ok(Json(res))
}

#[derive(Deserialize)]
struct SubtitleDownloadRequest {
    video: String,
    download_url: String,
    provider: Option<String>,
    detail_url: Option<String>,
}

async fn download_subtitle(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<SubtitleDownloadRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let _ = state.require_user(&headers, None)?;
    if req.video.trim().is_empty() || req.download_url.trim().is_empty() {
        return Err(api_err(
            StatusCode::BAD_REQUEST,
            "video and download_url are required.",
        ));
    }
    let svc = SubtitleService::new(&state.engine_manager.download_dir, 15.0);
    let res = svc
        .download_for_video(
            &req.video,
            &req.download_url,
            req.provider.as_deref().unwrap_or(""),
            req.detail_url.as_deref().unwrap_or(""),
        )
        .await
        .map_err(|e| api_err(StatusCode::BAD_REQUEST, e))?;
    Ok(Json(res))
}

#[derive(Deserialize)]
struct UIEventRequest {
    tab: String,
    action: Option<String>,
    detail: Option<String>,
}

async fn ui_event(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<UIEventRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let user = state.require_user(&headers, None)?;
    let tab: String = req.tab.trim().chars().take(32).collect();
    if tab.is_empty() {
        return Err(api_err(StatusCode::BAD_REQUEST, "tab is required."));
    }
    let action: String = req
        .action
        .unwrap_or_default()
        .trim()
        .chars()
        .take(64)
        .collect();
    let detail: String = req
        .detail
        .unwrap_or_default()
        .trim()
        .chars()
        .take(200)
        .collect();
    info!(
        "[UI] user='{}' tab='{}' action='{}' detail='{}'",
        user, tab, action, detail
    );
    Ok(Json(json!({"success": true})))
}

#[derive(Deserialize)]
struct WsQuery {
    token: Option<String>,
}

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<WsQuery>,
) -> Response {
    if state.require_user(&headers, q.token.as_deref()).is_err() {
        return (StatusCode::UNAUTHORIZED, "Unauthorized").into_response();
    }
    ws.on_upgrade(move |socket| handle_ws_socket(socket, state))
}

async fn handle_ws_socket(mut socket: WebSocket, state: AppState) {
    let mut rx = state.ws_manager.subscribe();
    // Send immediate initial telemetry snapshot on connect
    let init_payload = json!({
        "type": "telemetry",
        "stats": state.engine_manager.get_global_stats(),
        "torrents": state.engine_manager.get_swarm_snapshot(),
    });
    if let Ok(txt) = serde_json::to_string(&init_payload) {
        let _ = socket.send(Message::Text(txt)).await;
    }

    loop {
        tokio::select! {
            Ok(msg_txt) = rx.recv() => {
                if socket.send(Message::Text(msg_txt)).await.is_err() {
                    break;
                }
            }
            incoming = socket.recv() => {
                match incoming {
                    Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                    _ => {}
                }
            }
        }
    }
    state.ws_manager.on_disconnect();
}
