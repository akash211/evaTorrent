//! Top-level `EngineManager` coordinating multiple `TorrentSession` instances,
//! FIFO download queueing, global speed scheduling, and SQLite auto-resume.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use regex::Regex;
use serde_json::{json, Value};
use tracing::{info, warn};

use crate::bencoding::bdecode;
use crate::db::{now_unix_secs, Database, TorrentHistoryRow};
use crate::engine::limits::{effective_peer_settings, env_usize, large_torrent_bytes};
use crate::engine::monitor::{
    check_previous_shutdown, get_system_health, log_cgroup_boot_line, mark_clean_shutdown,
};
use crate::engine::session::{TorrentSession, TorrentStatus};
use crate::engine::speed::{
    default_profiles, load_speed_config, match_schedule, profile_limit, validate_profiles,
    validate_schedule, ScheduleEntry, SpeedProfile,
};
use crate::torrent::{fetch_torrent_from_caches, Magnet, Torrent};
use crate::tracker::fallbacks::{ensure_fresh_trackers, read_cached};

struct EngineState {
    sessions: HashMap<String, Arc<TorrentSession>>,
    session_order: Vec<String>,
    fallback_trackers: Vec<String>,
    fallback_checked_at: f64,
    global_download_limit: Option<u64>,
    active_speed_profile: Option<String>,
    speed_profiles: Vec<SpeedProfile>,
    speed_schedule: Vec<ScheduleEntry>,
    speed_override: Option<String>,
    last_schedule_eval: f64,
}

#[derive(Clone)]
pub struct EngineManager {
    pub download_dir: PathBuf,
    pub db: Option<Database>,
    pub max_active_downloads: usize,
    inner: Arc<Mutex<EngineState>>,
}

impl EngineManager {
    pub fn new(default_download_dir: Option<PathBuf>, db: Option<Database>) -> Self {
        let download_dir = default_download_dir
            .or_else(|| {
                std::env::var("DOWNLOAD_DIR")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .map(PathBuf::from)
            })
            .unwrap_or_else(|| {
                let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
                PathBuf::from(home).join("Downloads").join("evaTorrent")
            });
        let _ = std::fs::create_dir_all(&download_dir);

        let max_active_downloads = env_usize("EVA_MAX_ACTIVE_DOWNLOADS", 2);

        Self {
            download_dir,
            db,
            max_active_downloads,
            inner: Arc::new(Mutex::new(EngineState {
                sessions: HashMap::new(),
                session_order: Vec::new(),
                fallback_trackers: Vec::new(),
                fallback_checked_at: 0.0,
                global_download_limit: None,
                active_speed_profile: None,
                speed_profiles: default_profiles(),
                speed_schedule: Vec::new(),
                speed_override: None,
                last_schedule_eval: 0.0,
            })),
        }
    }

    pub fn data_dir(&self) -> PathBuf {
        self.db
            .as_ref()
            .and_then(|d| d.db_path.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| self.download_dir.clone())
    }

    pub fn start_monitor(&self) {
        log_cgroup_boot_line();
        check_previous_shutdown(&self.data_dir());
    }

    pub async fn refresh_fallback_trackers(&self, force: bool) -> Vec<String> {
        if !force {
            let (cached, age_days, _) = read_cached(&self.download_dir);
            if !cached.is_empty() {
                let mut g = self.inner.lock().unwrap();
                g.fallback_trackers = cached.clone();
                if age_days <= 7.0 {
                    g.fallback_checked_at = now_unix_secs();
                    return cached;
                }
            }
        }
        match ensure_fresh_trackers(&self.download_dir, None).await {
            Ok(list) => {
                let mut g = self.inner.lock().unwrap();
                g.fallback_trackers = list.clone();
                g.fallback_checked_at = now_unix_secs();
                list
            }
            Err(e) => {
                warn!(
                    "[TRACKERS] Fallback refresh failed, using cached/baked-in defaults: {}",
                    e
                );
                let (cached, _, _) = read_cached(&self.download_dir);
                let mut g = self.inner.lock().unwrap();
                if g.fallback_trackers.is_empty() {
                    g.fallback_trackers = cached;
                }
                g.fallback_trackers.clone()
            }
        }
    }

    fn cache_path(&self, info_hash_hex: &str) -> PathBuf {
        let dir = self.download_dir.join(".torrent_cache");
        let _ = std::fs::create_dir_all(&dir);
        dir.join(format!("{}.torrent", info_hash_hex.to_lowercase()))
    }

    fn ensure_torrent_cached(&self, info_hash_hex: &str, raw_data: &[u8]) {
        let p = self.cache_path(info_hash_hex);
        if !p.exists() {
            let _ = std::fs::write(&p, raw_data);
        }
    }

    pub fn is_globally_throttled(&self) -> bool {
        let g = self.inner.lock().unwrap();
        let Some(limit) = g.global_download_limit.filter(|&l| l > 0) else {
            return false;
        };
        let total: f64 = g.sessions.values().map(|s| s.download_speed()).sum();
        total >= limit as f64
    }

    pub fn add_torrent(
        &self,
        torrent: Torrent,
        output_dir: Option<PathBuf>,
        magnet_uri: Option<&str>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let key = torrent.info_hash_hex.to_lowercase();
        if let Some(existing) = self.get_session(&key) {
            return Ok(existing);
        }

        let dest_dir = output_dir.unwrap_or_else(|| self.download_dir.clone());
        if torrent.total_length >= large_torrent_bytes() {
            let (peers, pipe) = effective_peer_settings(torrent.total_length);
            warn!(
                "[ENGINE] Large torrent '{}' ({:.1} GB): auto-tuned to max_peers={} pipeline={}",
                torrent.name,
                torrent.total_length as f64 / (1024.0 * 1024.0 * 1024.0),
                peers,
                pipe
            );
        }

        let fb = self.inner.lock().unwrap().fallback_trackers.clone();
        let session = Arc::new(TorrentSession::new(
            torrent.clone(),
            dest_dir.clone(),
            50,
            6881,
            None,
            self.db.clone(),
            None,
            None,
            if fb.is_empty() { None } else { Some(fb) },
        )?);

        let me_throt = self.clone();
        session.set_external_throttle(Arc::new(move || me_throt.is_globally_throttled()));

        let me_slot = self.clone();
        session.set_on_slot_event(Arc::new(move || {
            me_slot.promote_queued();
        }));

        {
            let mut g = self.inner.lock().unwrap();
            if !g.session_order.contains(&key) {
                g.session_order.push(key.clone());
            }
            g.sessions.insert(key.clone(), Arc::clone(&session));
        }

        self.ensure_torrent_cached(&key, &torrent.raw_data);

        if let Some(ref db) = self.db {
            db.upsert_torrent(
                &key,
                &torrent.name,
                torrent.total_length,
                &dest_dir.to_string_lossy(),
                session.status().as_str(),
                magnet_uri,
            );
        }

        self.start_or_queue(&session, "added");

        if let Some(ref db) = self.db {
            if session.piece_manager.is_complete() {
                db.mark_torrent_completed(
                    &key,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                );
            } else {
                db.update_torrent_progress(
                    &key,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                    Some(session.status().as_str()),
                );
            }
        }

        Ok(session)
    }

    pub fn add_torrent_bytes(
        &self,
        data: Vec<u8>,
        output_dir: Option<PathBuf>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let torrent = Torrent::from_bytes(data)?;
        self.add_torrent(torrent, output_dir, None)
    }

    pub fn add_torrent_file(
        &self,
        filepath: &Path,
        output_dir: Option<PathBuf>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let torrent = Torrent::from_file(filepath)?;
        self.add_torrent(torrent, output_dir, None)
    }

    pub async fn add_magnet(
        &self,
        magnet_uri: &str,
        output_dir: Option<PathBuf>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let magnet = Magnet::parse(magnet_uri)?;
        let key = magnet.info_hash_hex.to_lowercase();
        if let Some(existing) = self.get_session(&key) {
            return Ok(existing);
        }

        let cache_file = self.cache_path(&key);
        let mut raw_bytes = if cache_file.exists() {
            std::fs::read(&cache_file).ok()
        } else {
            None
        };

        if raw_bytes.is_none() {
            raw_bytes = fetch_torrent_from_caches(&key, 8.0).await;
            if let Some(ref bytes) = raw_bytes {
                let _ = std::fs::write(&cache_file, bytes);
            }
        }

        let Some(raw) = raw_bytes else {
            let name_hint = magnet
                .name
                .as_ref()
                .map(|n| format!(" \"{}\"", n))
                .unwrap_or_default();
            anyhow::bail!(
                "Could not retrieve metadata for magnet{} ({}...). The torrent metainfo was not found in public torrent caches. Please upload the .torrent file directly.",
                name_hint,
                &key[..8]
            );
        };

        let mut torrent = Torrent::from_bytes(raw)?;
        for tr in magnet.trackers {
            if !torrent.trackers.contains(&tr) {
                torrent.trackers.push(tr);
            }
        }
        let fb = self.inner.lock().unwrap().fallback_trackers.clone();
        for tr in fb {
            if !torrent.trackers.contains(&tr) {
                torrent.trackers.push(tr);
            }
        }

        self.add_torrent(torrent, output_dir, Some(magnet_uri))
    }

    pub async fn add_url(
        &self,
        url: &str,
        output_dir: Option<PathBuf>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let clean_url = url.trim();
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) evaTorrent/1.0")
            .danger_accept_invalid_certs(true)
            .build()?;

        let resp = client.get(clean_url).send().await?;
        if !resp.status().is_success() {
            anyhow::bail!(
                "HTTP request to torrent URL failed with status {}",
                resp.status()
            );
        }
        let content = resp.bytes().await?;
        if let Ok(decoded) = bdecode(&content) {
            if decoded.dict_get(b"info").is_some() {
                return self.add_torrent_bytes(content.to_vec(), output_dir);
            }
        }

        let text = String::from_utf8_lossy(&content);
        let re = Regex::new(r#"magnet:\?[^"'\s<>]+"#).unwrap();
        if let Some(m) = re.find(&text) {
            return self.add_magnet(m.as_str(), output_dir).await;
        }

        anyhow::bail!("URL did not return a valid .torrent file or magnet link.")
    }

    pub async fn add_torrent_or_url(
        &self,
        input_str: &str,
        output_dir: Option<PathBuf>,
    ) -> anyhow::Result<Arc<TorrentSession>> {
        let s = input_str.trim();
        if s.starts_with("magnet:?") {
            self.add_magnet(s, output_dir).await
        } else if s.starts_with("http://") || s.starts_with("https://") {
            self.add_url(s, output_dir).await
        } else if s.len() == 40 && s.chars().all(|c| c.is_ascii_hexdigit()) {
            self.add_magnet(&format!("magnet:?xt=urn:btih:{}", s), output_dir)
                .await
        } else {
            let p = Path::new(s);
            if p.is_file() {
                self.add_torrent_file(p, output_dir)
            } else {
                anyhow::bail!("Input must be a valid magnet link (magnet:?), Web URL (http/https), or .torrent file path.")
            }
        }
    }

    pub fn get_session(&self, info_hash_hex: &str) -> Option<Arc<TorrentSession>> {
        self.inner
            .lock()
            .unwrap()
            .sessions
            .get(&info_hash_hex.to_lowercase())
            .cloned()
    }

    pub fn active_download_count(&self) -> usize {
        let g = self.inner.lock().unwrap();
        g.sessions
            .values()
            .filter(|s| s.status() == TorrentStatus::Downloading)
            .count()
    }

    pub fn queued_sessions(&self) -> Vec<Arc<TorrentSession>> {
        let g = self.inner.lock().unwrap();
        g.session_order
            .iter()
            .filter_map(|k| g.sessions.get(k).cloned())
            .filter(|s| s.status() == TorrentStatus::Queued)
            .collect()
    }

    fn refresh_queue_positions(&self) {
        let queued = self.queued_sessions();
        for (idx, s) in queued.iter().enumerate() {
            s.set_queue_position(Some(idx + 1));
        }
        let all: Vec<Arc<TorrentSession>> = self
            .inner
            .lock()
            .unwrap()
            .sessions
            .values()
            .cloned()
            .collect();
        for s in all {
            if s.status() != TorrentStatus::Queued {
                s.set_queue_position(None);
            }
        }
    }

    fn slot_free(&self) -> bool {
        self.max_active_downloads == 0 || self.active_download_count() < self.max_active_downloads
    }

    fn start_or_queue(&self, session: &Arc<TorrentSession>, reason: &str) -> &'static str {
        if session.piece_manager.is_complete() {
            session.start();
            return "started";
        }
        if !self.slot_free() {
            session.set_status(TorrentStatus::Queued);
            self.refresh_queue_positions();
            if let Some(ref db) = self.db {
                db.update_torrent_progress(
                    &session.torrent.info_hash_hex,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                    Some("queued"),
                );
            }
            info!(
                "[QUEUE] '{}' queued #{:?} ({})",
                session.torrent.name,
                session.queue_position(),
                reason
            );
            return "queued";
        }
        session.start();
        "started"
    }

    pub fn promote_queued(&self) {
        while self.slot_free() {
            let queued = self.queued_sessions();
            let Some(next) = queued.first() else { break };
            next.start();
            if let Some(ref db) = self.db {
                db.update_torrent_progress(
                    &next.torrent.info_hash_hex,
                    next.piece_manager.bytes_downloaded(),
                    next.piece_manager.bytes_uploaded(),
                    Some("downloading"),
                );
                db.log_event(
                    &next.torrent.info_hash_hex,
                    "RESUMED",
                    "Promoted from download queue",
                );
            }
        }
        self.refresh_queue_positions();
    }

    pub async fn pause_torrent(&self, info_hash_hex: &str) -> bool {
        if let Some(session) = self.get_session(info_hash_hex) {
            session.pause().await;
            if let Some(ref db) = self.db {
                db.update_torrent_progress(
                    info_hash_hex,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                    Some("paused"),
                );
                db.log_event(info_hash_hex, "PAUSED", "Torrent paused by user");
            }
            self.promote_queued();
            return true;
        }
        false
    }

    pub fn resume_torrent(&self, info_hash_hex: &str) -> bool {
        let Some(session) = self.get_session(info_hash_hex) else {
            return false;
        };
        if session.status() == TorrentStatus::Queued {
            if self.slot_free() {
                session.start();
                if let Some(ref db) = self.db {
                    db.log_event(info_hash_hex, "RESUMED", "Queued torrent started by user");
                }
            }
            return true;
        }
        if session.status() == TorrentStatus::Completed {
            session.resume();
            return true;
        }
        if !self.slot_free() && session.status() != TorrentStatus::Downloading {
            session.set_status(TorrentStatus::Queued);
            self.refresh_queue_positions();
            if let Some(ref db) = self.db {
                db.update_torrent_progress(
                    info_hash_hex,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                    Some("queued"),
                );
            }
            return true;
        }
        session.resume();
        if let Some(ref db) = self.db {
            db.update_torrent_progress(
                info_hash_hex,
                session.piece_manager.bytes_downloaded(),
                session.piece_manager.bytes_uploaded(),
                Some(session.status().as_str()),
            );
            db.log_event(info_hash_hex, "RESUMED", "Torrent resumed by user");
        }
        true
    }

    pub async fn resume_db_torrent(&self, info_hash_hex: &str) -> bool {
        let key = info_hash_hex.to_lowercase();
        if let Some(s) = self.get_session(&key) {
            s.resume();
            return true;
        }
        let Some(ref db) = self.db else { return false };
        let Some(row) = db.get_torrent_history(&key) else {
            return false;
        };
        let st_low = row.status.to_lowercase();
        if st_low == "completed" || st_low == "removed" {
            return false;
        }
        if let Some(rebuilt) = self.rebuild_session_from_row(&row).await {
            if st_low != "paused" {
                rebuilt.resume();
            }
            db.log_event(&key, "RESUMED", "Torrent resumed from database fallback");
            return true;
        }
        false
    }

    async fn rebuild_session_from_row(
        &self,
        row: &TorrentHistoryRow,
    ) -> Option<Arc<TorrentSession>> {
        let key = row.info_hash.to_lowercase();
        if let Some(existing) = self.get_session(&key) {
            return Some(existing);
        }
        let cache_file = self.cache_path(&key);
        let mut raw_bytes = if cache_file.exists() {
            std::fs::read(&cache_file).ok()
        } else {
            None
        };
        if raw_bytes.is_none() && row.magnet_uri.is_some() {
            raw_bytes = fetch_torrent_from_caches(&key, 8.0).await;
            if let Some(ref b) = raw_bytes {
                let _ = std::fs::write(&cache_file, b);
            }
        }
        let raw = raw_bytes?;
        let torrent = Torrent::from_bytes(raw).ok()?;

        let mut dest_dir = self.download_dir.clone();
        if let Some(ref rec) = row.download_dir {
            let p = PathBuf::from(rec);
            if p.is_absolute() && std::fs::create_dir_all(&p).is_ok() {
                dest_dir = p;
            }
        }

        let all_paths: Vec<String> = torrent.files.iter().map(|f| f.path.clone()).collect();
        let restored_prio = self
            .db
            .as_ref()
            .and_then(|d| d.get_effective_file_priorities(&key, &all_paths));

        let fb = self.inner.lock().unwrap().fallback_trackers.clone();
        let session = Arc::new(
            TorrentSession::new(
                torrent.clone(),
                dest_dir,
                50,
                6881,
                None,
                self.db.clone(),
                None,
                restored_prio,
                if fb.is_empty() { None } else { Some(fb) },
            )
            .ok()?,
        );

        let me_throt = self.clone();
        session.set_external_throttle(Arc::new(move || me_throt.is_globally_throttled()));

        let me_slot = self.clone();
        session.set_on_slot_event(Arc::new(move || {
            me_slot.promote_queued();
        }));

        {
            let mut g = self.inner.lock().unwrap();
            if !g.session_order.contains(&key) {
                g.session_order.push(key.clone());
            }
            g.sessions.insert(key.clone(), Arc::clone(&session));
        }

        if session.piece_manager.is_complete() {
            if let Some(ref db) = self.db {
                db.mark_torrent_completed(
                    &key,
                    session.piece_manager.bytes_downloaded(),
                    session.piece_manager.bytes_uploaded(),
                );
            }
            return Some(session);
        }

        match row.status.to_lowercase().as_str() {
            "paused" => {
                session.set_status(TorrentStatus::Paused);
            }
            "queued" => {
                session.set_status(TorrentStatus::Queued);
                self.refresh_queue_positions();
            }
            _ => {
                self.start_or_queue(&session, "auto-resume after restart");
            }
        }
        Some(session)
    }

    pub async fn restore_from_db(&self) -> Value {
        let mut restored = 0usize;
        let mut paused = 0usize;
        let mut queued = 0usize;
        let mut completed = 0usize;
        let mut skipped = 0usize;

        let Some(ref db) = self.db else {
            return json!({"restored": 0, "paused": 0, "queued": 0, "completed": 0, "skipped": 0});
        };

        let rows = db.get_resumable_torrents(500);
        for row in rows {
            match self.rebuild_session_from_row(&row).await {
                Some(sess) => match sess.status() {
                    TorrentStatus::Completed => completed += 1,
                    TorrentStatus::Paused => paused += 1,
                    TorrentStatus::Queued => queued += 1,
                    _ => restored += 1,
                },
                None => skipped += 1,
            }
        }
        json!({
            "restored": restored,
            "paused": paused,
            "queued": queued,
            "completed": completed,
            "skipped": skipped,
        })
    }

    pub fn set_speed_limit(&self, info_hash_hex: &str, limit: Option<u64>) -> bool {
        if let Some(s) = self.get_session(info_hash_hex) {
            s.set_download_limit(limit);
            return true;
        }
        false
    }

    pub fn set_sequential(&self, info_hash_hex: &str, sequential: bool) -> bool {
        if let Some(s) = self.get_session(info_hash_hex) {
            s.set_sequential(sequential);
            return true;
        }
        false
    }

    pub fn load_speed_config(&self) {
        let (profiles, schedule, override_name) = load_speed_config(self.db.as_ref());
        {
            let mut g = self.inner.lock().unwrap();
            g.speed_profiles = profiles;
            g.speed_schedule = schedule;
            g.speed_override = override_name;
        }
        self.evaluate_speed_schedule(true);
    }

    pub fn save_speed_config(
        &self,
        profiles: Option<Vec<SpeedProfile>>,
        schedule: Option<Vec<ScheduleEntry>>,
    ) -> Result<(), String> {
        {
            let mut g = self.inner.lock().unwrap();
            if let Some(p) = profiles {
                let valid = validate_profiles(p)?;
                if let Some(ref db) = self.db {
                    if let Ok(s) = serde_json::to_string(&valid) {
                        db.set_setting("speed_profiles", &s);
                    }
                }
                g.speed_profiles = valid;
            }
            if let Some(s) = schedule {
                let names: Vec<String> = g.speed_profiles.iter().map(|p| p.name.clone()).collect();
                let valid = validate_schedule(s, &names)?;
                if let Some(ref db) = self.db {
                    if let Ok(ser) = serde_json::to_string(&valid) {
                        db.set_setting("speed_schedule", &ser);
                    }
                }
                g.speed_schedule = valid;
            }
        }
        self.evaluate_speed_schedule(true);
        Ok(())
    }

    pub fn set_speed_override(
        &self,
        profile_name: Option<String>,
    ) -> Result<Option<String>, String> {
        {
            let mut g = self.inner.lock().unwrap();
            if let Some(ref name) = profile_name {
                if !g
                    .speed_profiles
                    .iter()
                    .any(|p| p.name.eq_ignore_ascii_case(name))
                {
                    return Err(format!("Unknown speed profile: {}", name));
                }
            }
            g.speed_override = profile_name.clone();
            if let Some(ref db) = self.db {
                db.set_setting("speed_override", profile_name.as_deref().unwrap_or(""));
            }
        }
        Ok(self.evaluate_speed_schedule(true))
    }

    pub fn evaluate_speed_schedule(&self, force: bool) -> Option<String> {
        let now = now_unix_secs();
        let mut g = self.inner.lock().unwrap();
        if !force && (now - g.last_schedule_eval) < 60.0 {
            return g.active_speed_profile.clone();
        }
        g.last_schedule_eval = now;

        if let Some(ref ov) = g.speed_override.clone() {
            let lim = profile_limit(&g.speed_profiles, ov);
            g.active_speed_profile = Some(ov.clone());
            g.global_download_limit = lim;
        } else if !g.speed_schedule.is_empty() {
            if let Some(matched) = match_schedule(&g.speed_schedule) {
                let lim = profile_limit(&g.speed_profiles, &matched);
                g.active_speed_profile = Some(matched);
                g.global_download_limit = lim;
            } else {
                g.active_speed_profile = None;
                g.global_download_limit = None;
            }
        } else {
            g.active_speed_profile = None;
            g.global_download_limit = None;
        }
        g.active_speed_profile.clone()
    }

    pub fn speed_state_json(&self) -> Value {
        let g = self.inner.lock().unwrap();
        let server_time = chrono::Local::now()
            .format("%Y-%m-%d %H:%M (%A)")
            .to_string();
        json!({
            "profiles": g.speed_profiles,
            "schedule": g.speed_schedule,
            "active_profile": g.active_speed_profile,
            "manual_override": g.speed_override,
            "global_limit": g.global_download_limit,
            "server_time": server_time,
        })
    }

    pub async fn remove_torrent(&self, info_hash_hex: &str, delete_files: bool) -> bool {
        let key = info_hash_hex.to_lowercase();
        let removed_session = {
            let mut g = self.inner.lock().unwrap();
            g.session_order.retain(|k| k != &key);
            g.sessions.remove(&key)
        };

        if let Some(session) = removed_session {
            session.stop(false).await;
            if let Some(ref db) = self.db {
                db.mark_torrent_removed(
                    &key,
                    delete_files,
                    Some(session.piece_manager.bytes_downloaded()),
                    Some(session.piece_manager.bytes_uploaded()),
                );
            }
            if delete_files {
                self.delete_session_files(&session);
            }
            self.refresh_queue_positions();
            self.promote_queued();
            return true;
        }

        if let Some(ref db) = self.db {
            if let Some(row) = db.get_torrent_history(&key) {
                if delete_files {
                    self.delete_cached_files_best_effort(&key, &row);
                }
                db.mark_torrent_removed(&key, delete_files, None, None);
                return true;
            }
        }
        false
    }

    fn delete_session_files(&self, session: &TorrentSession) {
        for f in &session.torrent.files {
            let fp = session.download_dir.join(&f.path);
            let _ = std::fs::remove_file(&fp);
            let part = session.download_dir.join(format!("{}.part", f.path));
            let _ = std::fs::remove_file(&part);
        }
        if session.torrent.is_multi_file {
            let top = session.download_dir.join(&session.torrent.name);
            if top.is_dir() {
                let _ = std::fs::remove_dir_all(&top);
            }
        }
    }

    fn delete_cached_files_best_effort(&self, key: &str, row: &TorrentHistoryRow) {
        let cache_file = self.cache_path(key);
        let Ok(raw) = std::fs::read(&cache_file) else {
            return;
        };
        let Ok(torrent) = Torrent::from_bytes(raw) else {
            return;
        };
        let dest = row
            .download_dir
            .as_ref()
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .unwrap_or_else(|| self.download_dir.clone());
        for f in &torrent.files {
            let _ = std::fs::remove_file(dest.join(&f.path));
            let _ = std::fs::remove_file(dest.join(format!("{}.part", f.path)));
        }
    }

    pub async fn pause_all(&self) -> usize {
        let keys: Vec<String> = self
            .inner
            .lock()
            .unwrap()
            .sessions
            .keys()
            .cloned()
            .collect();
        let mut count = 0;
        for k in keys {
            if self.pause_torrent(&k).await {
                count += 1;
            }
        }
        count
    }

    pub fn resume_all(&self) -> usize {
        let keys: Vec<String> = self.inner.lock().unwrap().session_order.clone();
        let mut count = 0;
        for k in keys {
            if self.resume_torrent(&k) {
                count += 1;
            }
        }
        count
    }

    pub async fn clear_completed(&self) -> usize {
        let keys: Vec<String> = {
            let g = self.inner.lock().unwrap();
            g.sessions
                .iter()
                .filter(|(_, s)| s.status() == TorrentStatus::Completed)
                .map(|(k, _)| k.clone())
                .collect()
        };
        let mut count = 0;
        for k in keys {
            if self.remove_torrent(&k, false).await {
                count += 1;
            }
        }
        count
    }

    pub async fn shutdown(&self) {
        let sessions: Vec<(String, Arc<TorrentSession>)> = {
            let mut g = self.inner.lock().unwrap();
            let list: Vec<_> = g.sessions.drain().collect();
            g.session_order.clear();
            list
        };

        if let Some(ref db) = self.db {
            for (key, sess) in &sessions {
                if let Some(existing) = db.get_torrent_history(key) {
                    let st = existing.status.to_lowercase();
                    if st == "completed" || st == "removed" {
                        continue;
                    }
                }
                let st_str = if sess.piece_manager.is_complete() {
                    "completed"
                } else {
                    "paused"
                };
                db.update_torrent_progress(
                    key,
                    sess.piece_manager.bytes_downloaded(),
                    sess.piece_manager.bytes_uploaded(),
                    Some(st_str),
                );
            }
        }

        for (_, sess) in sessions {
            sess.stop(false).await;
        }
        mark_clean_shutdown(&self.data_dir());
    }

    pub fn get_swarm_snapshot(&self) -> Vec<Value> {
        let live_sessions: Vec<Arc<TorrentSession>> = {
            let g = self.inner.lock().unwrap();
            g.session_order
                .iter()
                .filter_map(|k| g.sessions.get(k).cloned())
                .collect()
        };

        let mut merged: Vec<Value> = live_sessions.iter().map(|s| s.to_dict()).collect();
        let live_hashes: HashSet<String> = live_sessions
            .iter()
            .map(|s| s.torrent.info_hash_hex.to_lowercase())
            .collect();

        if let Some(ref db) = self.db {
            for r in db.get_resumable_torrents(500) {
                let h = r.info_hash.to_lowercase();
                if h.is_empty() || live_hashes.contains(&h) {
                    continue;
                }
                let progress = if r.total_size > 0 {
                    ((r.downloaded_bytes as f64 / r.total_size as f64) * 10000.0).round() / 100.0
                } else {
                    0.0
                };
                merged.push(json!({
                    "info_hash": h,
                    "name": r.name,
                    "status": r.status.to_lowercase(),
                    "error_message": r.error_message,
                    "total_size": r.total_size,
                    "downloaded": r.downloaded_bytes,
                    "uploaded": r.uploaded_bytes,
                    "progress": progress,
                    "download_speed": 0.0,
                    "upload_speed": 0.0,
                    "download_limit": null,
                    "sequential": false,
                    "eta": null,
                    "peers_connected": 0,
                    "peers_total": 0,
                    "piece_count": 0,
                    "pieces_completed": 0,
                    "piece_length": 0,
                    "is_multi_file": false,
                    "files": [],
                    "trackers": [],
                    "source": "db",
                    "resumable": true,
                }));
            }
        }
        merged
    }

    pub fn get_global_stats(&self) -> Value {
        let sessions: Vec<Arc<TorrentSession>> = self
            .inner
            .lock()
            .unwrap()
            .sessions
            .values()
            .cloned()
            .collect();
        let total_dl: f64 = sessions.iter().map(|s| s.download_speed()).sum();
        let total_ul: f64 = sessions.iter().map(|s| s.upload_speed()).sum();
        let total_bytes_dl: u64 = sessions
            .iter()
            .map(|s| s.piece_manager.bytes_downloaded())
            .sum();
        let total_bytes_ul: u64 = sessions
            .iter()
            .map(|s| s.piece_manager.bytes_uploaded())
            .sum();

        let health = get_system_health(&self.download_dir);

        json!({
            "active_torrents": sessions.len(),
            "total_download_speed": (total_dl * 100.0).round() / 100.0,
            "total_upload_speed": (total_ul * 100.0).round() / 100.0,
            "total_downloaded": total_bytes_dl,
            "total_uploaded": total_bytes_ul,
            "rss_mb": health.rss_mb,
            "free_disk_mb": health.free_disk_mb,
            "engine": "rust-tokio",
        })
    }
}
