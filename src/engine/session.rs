//! `TorrentSession` orchestrating peer connections, tracker announces, sequential/rarest
//! downloading, stall recovery, and telemetry serialization.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::task::JoinHandle;
use tracing::{info, warn};

use crate::db::{now_unix_secs, Database};
use crate::engine::connection::PeerConnection;
use crate::engine::limits::{effective_peer_settings, env_f64};
use crate::storage::manager::PieceManager;
use crate::torrent::Torrent;
use crate::tracker::{Peer, TrackerManager};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TorrentStatus {
    Pending,
    Checking,
    Queued,
    Downloading,
    Seeding,
    Paused,
    Completed,
    Error,
}

impl TorrentStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            TorrentStatus::Pending => "pending",
            TorrentStatus::Checking => "checking",
            TorrentStatus::Queued => "queued",
            TorrentStatus::Downloading => "downloading",
            TorrentStatus::Seeding => "seeding",
            TorrentStatus::Paused => "paused",
            TorrentStatus::Completed => "completed",
            TorrentStatus::Error => "error",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "checking" => TorrentStatus::Checking,
            "queued" => TorrentStatus::Queued,
            "downloading" => TorrentStatus::Downloading,
            "seeding" => TorrentStatus::Seeding,
            "paused" => TorrentStatus::Paused,
            "completed" => TorrentStatus::Completed,
            "error" => TorrentStatus::Error,
            _ => TorrentStatus::Pending,
        }
    }
}

struct SessionMutable {
    status: TorrentStatus,
    error_message: Option<String>,
    download_limit: Option<u64>,
    queue_position: Option<usize>,
    download_speed: f64,
    upload_speed: f64,
    eta_seconds: Option<u64>,
    last_speed_check: f64,
    last_downloaded_bytes: u64,
    last_uploaded_bytes: u64,
    last_data_received_time: f64,
    stall_retries: u32,
    announce_failures: u32,
    active_peers: HashMap<String, Arc<PeerConnection>>,
    peer_queue: VecDeque<Peer>,
    seen_peers: HashSet<String>,
    main_task: Option<JoinHandle<()>>,
    speed_task: Option<JoinHandle<()>>,
    external_throttle: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
    on_slot_event: Option<Arc<dyn Fn() + Send + Sync>>,
    completion_notifier: Option<Arc<dyn Fn(String, u64, u64, u64, usize) + Send + Sync>>,
}

pub struct TorrentSession {
    pub torrent: Torrent,
    pub download_dir: PathBuf,
    pub max_peers: usize,
    pub port: u16,
    pub db: Option<Database>,
    pub piece_manager: PieceManager,
    pub tracker_manager: TrackerManager,
    pub stall_timeout_seconds: f64,
    pub stall_retry_delay_seconds: f64,
    pub progress_log_seconds: f64,
    running: AtomicBool,
    inner: Mutex<SessionMutable>,
}

impl TorrentSession {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        torrent: Torrent,
        download_dir: PathBuf,
        max_peers_arg: usize,
        port: u16,
        download_limit: Option<u64>,
        db: Option<Database>,
        selected_files: Option<Vec<String>>,
        file_priorities: Option<HashMap<String, u8>>,
        fallback_trackers: Option<Vec<String>>,
    ) -> anyhow::Result<Self> {
        let (auto_peers, _) = effective_peer_settings(torrent.total_length);
        let max_peers = max_peers_arg.min(auto_peers).max(1);

        let tracker_manager =
            TrackerManager::new(&torrent.trackers, port, true, fallback_trackers.as_deref());

        let piece_manager = PieceManager::new(
            torrent.clone(),
            &download_dir,
            None,
            selected_files,
            file_priorities,
        )?;

        piece_manager.check_existing_files(None);
        let initial_status = if piece_manager.is_complete() {
            TorrentStatus::Completed
        } else {
            TorrentStatus::Pending
        };

        let now = now_unix_secs();
        Ok(Self {
            torrent,
            download_dir,
            max_peers,
            port,
            db,
            piece_manager,
            tracker_manager,
            stall_timeout_seconds: env_f64("EVA_STALL_TIMEOUT_SECS", 300.0),
            stall_retry_delay_seconds: env_f64("EVA_STALL_RETRY_SECS", 1800.0),
            progress_log_seconds: env_f64("EVA_PROGRESS_LOG_SECS", 300.0),
            running: AtomicBool::new(false),
            inner: Mutex::new(SessionMutable {
                status: initial_status,
                error_message: None,
                download_limit,
                queue_position: None,
                download_speed: 0.0,
                upload_speed: 0.0,
                eta_seconds: None,
                last_speed_check: now,
                last_downloaded_bytes: 0,
                last_uploaded_bytes: 0,
                last_data_received_time: now,
                stall_retries: 0,
                announce_failures: 0,
                active_peers: HashMap::new(),
                peer_queue: VecDeque::new(),
                seen_peers: HashSet::new(),
                main_task: None,
                speed_task: None,
                external_throttle: None,
                on_slot_event: None,
                completion_notifier: None,
            }),
        })
    }

    pub fn set_external_throttle(&self, f: Arc<dyn Fn() -> bool + Send + Sync>) {
        self.inner.lock().unwrap().external_throttle = Some(f);
    }

    pub fn set_on_slot_event(&self, f: Arc<dyn Fn() + Send + Sync>) {
        self.inner.lock().unwrap().on_slot_event = Some(f);
    }

    pub fn set_completion_notifier(
        &self,
        f: Arc<dyn Fn(String, u64, u64, u64, usize) + Send + Sync>,
    ) {
        self.inner.lock().unwrap().completion_notifier = Some(f);
    }

    pub fn status(&self) -> TorrentStatus {
        self.inner.lock().unwrap().status
    }

    pub fn set_status(&self, status: TorrentStatus) {
        self.inner.lock().unwrap().status = status;
    }

    pub fn queue_position(&self) -> Option<usize> {
        self.inner.lock().unwrap().queue_position
    }

    pub fn set_queue_position(&self, pos: Option<usize>) {
        self.inner.lock().unwrap().queue_position = pos;
    }

    pub fn download_speed(&self) -> f64 {
        self.inner.lock().unwrap().download_speed
    }

    pub fn upload_speed(&self) -> f64 {
        self.inner.lock().unwrap().upload_speed
    }

    pub fn eta_seconds(&self) -> Option<u64> {
        self.inner.lock().unwrap().eta_seconds
    }

    pub fn peer_counts(&self) -> (usize, usize) {
        let g = self.inner.lock().unwrap();
        let active = g
            .active_peers
            .values()
            .filter(|c| c.is_connected.load(Ordering::Relaxed))
            .count();
        (active, g.seen_peers.len())
    }

    pub fn touch_activity(&self) {
        self.inner.lock().unwrap().last_data_received_time = now_unix_secs();
    }

    pub fn is_throttled(&self) -> bool {
        let (dl_limit, dl_speed, ext) = {
            let g = self.inner.lock().unwrap();
            (
                g.download_limit,
                g.download_speed,
                g.external_throttle.clone(),
            )
        };
        if let Some(lim) = dl_limit.filter(|&l| l > 0) {
            if dl_speed >= lim as f64 {
                return true;
            }
        }
        if let Some(ext_fn) = ext {
            if ext_fn() {
                return true;
            }
        }
        false
    }

    pub fn set_download_limit(&self, limit: Option<u64>) {
        self.inner.lock().unwrap().download_limit = limit.filter(|&l| l > 0);
    }

    pub fn set_sequential(&self, sequential: bool) {
        self.piece_manager.set_sequential(sequential);
        if let Some(ref db) = self.db {
            db.log_event(
                &self.torrent.info_hash_hex,
                "MODE",
                &format!(
                    "Sequential streaming mode {}",
                    if sequential { "enabled" } else { "disabled" }
                ),
            );
        }
    }

    pub fn set_selected_files(
        self: &Arc<Self>,
        selected_paths: Option<Vec<String>>,
    ) -> Result<Value, String> {
        let summary = self.piece_manager.set_selected_files(selected_paths)?;
        self.persist_file_prefs(&summary);
        if self.piece_manager.is_complete() && self.running.load(Ordering::SeqCst) {
            let me = Arc::clone(self);
            tokio::spawn(async move {
                me.stop_seeding_and_complete().await;
            });
        }
        Ok(summary)
    }

    pub fn set_file_priorities(
        self: &Arc<Self>,
        priorities: HashMap<String, u8>,
    ) -> Result<Value, String> {
        let summary = self.piece_manager.set_file_priorities(priorities)?;
        self.persist_file_prefs(&summary);
        if self.piece_manager.is_complete() && self.running.load(Ordering::SeqCst) {
            let me = Arc::clone(self);
            tokio::spawn(async move {
                me.stop_seeding_and_complete().await;
            });
        }
        Ok(summary)
    }

    fn persist_file_prefs(&self, summary: &Value) {
        if let Some(ref db) = self.db {
            let prio = self.piece_manager.file_priorities_snapshot();
            db.set_file_priorities(&self.torrent.info_hash_hex, Some(&prio));
            let sel = self
                .piece_manager
                .selected_files_snapshot()
                .unwrap_or_else(|| self.torrent.files.iter().map(|f| f.path.clone()).collect());
            db.set_file_selection(&self.torrent.info_hash_hex, Some(&sel));
            db.log_event(
                &self.torrent.info_hash_hex,
                "FILES",
                &format!(
                    "File selection updated: {}/{} files, {} pieces skipped",
                    summary
                        .get("selected_count")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0),
                    summary
                        .get("total_count")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0),
                    self.piece_manager.skipped_count()
                ),
            );
        }
    }

    pub fn start(self: &Arc<Self>) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        {
            let mut g = self.inner.lock().unwrap();
            g.status = TorrentStatus::Downloading;
            g.queue_position = None;
            g.error_message = None;
            g.last_data_received_time = now_unix_secs();
        }

        let me_main = Arc::clone(self);
        let main_handle = tokio::spawn(async move {
            me_main.main_loop().await;
        });

        let me_speed = Arc::clone(self);
        let speed_handle = tokio::spawn(async move {
            me_speed.speed_meter_loop().await;
        });

        let mut g = self.inner.lock().unwrap();
        g.main_task = Some(main_handle);
        g.speed_task = Some(speed_handle);
    }

    fn fire_slot_event(&self) {
        let cb = self.inner.lock().unwrap().on_slot_event.clone();
        if let Some(f) = cb {
            f();
        }
    }

    pub async fn pause(&self) {
        if self.status() == TorrentStatus::Paused {
            return;
        }
        self.running.store(false, Ordering::SeqCst);
        let (peers, main_t, speed_t) = {
            let mut g = self.inner.lock().unwrap();
            g.status = TorrentStatus::Paused;
            g.download_speed = 0.0;
            g.upload_speed = 0.0;
            let p: Vec<Arc<PeerConnection>> = g.active_peers.values().cloned().collect();
            g.active_peers.clear();
            (p, g.main_task.take(), g.speed_task.take())
        };
        for p in peers {
            p.stop().await;
        }
        if let Some(t) = main_t {
            t.abort();
        }
        if let Some(t) = speed_t {
            t.abort();
        }
        self.fire_slot_event();
    }

    pub fn resume(self: &Arc<Self>) {
        if self.status() == TorrentStatus::Completed {
            let files_exist = self.torrent.files.iter().any(|f| {
                self.download_dir.join(&f.path).exists()
                    || self.download_dir.join(format!("{}.part", f.path)).exists()
            });
            if files_exist && self.piece_manager.is_complete() {
                return;
            }
            info!(
                "Torrent '{}' files were removed from disk. Re-downloading...",
                self.torrent.name
            );
            self.piece_manager.check_existing_files(None);
            self.set_status(TorrentStatus::Downloading);
        }
        {
            let mut g = self.inner.lock().unwrap();
            g.error_message = None;
            g.last_data_received_time = now_unix_secs();
        }
        self.start();
    }

    pub async fn stop(&self, notify_slot: bool) {
        self.running.store(false, Ordering::SeqCst);
        let (peers, main_t, speed_t) = {
            let mut g = self.inner.lock().unwrap();
            if self.piece_manager.is_complete() {
                g.status = TorrentStatus::Completed;
            } else if g.status != TorrentStatus::Error {
                g.status = TorrentStatus::Paused;
            }
            g.download_speed = 0.0;
            g.upload_speed = 0.0;
            let p: Vec<Arc<PeerConnection>> = g.active_peers.values().cloned().collect();
            g.active_peers.clear();
            (p, g.main_task.take(), g.speed_task.take())
        };
        for p in peers {
            p.stop().await;
        }
        if let Some(t) = main_t {
            t.abort();
        }
        if let Some(t) = speed_t {
            t.abort();
        }
        if notify_slot {
            self.fire_slot_event();
        }
    }

    pub async fn stop_seeding_and_complete(&self) {
        self.running.store(false, Ordering::SeqCst);
        info!(
            "Torrent '{}' fully downloaded. Stopping peer connections (no seeding).",
            self.torrent.name
        );

        let dl = self.piece_manager.bytes_downloaded();
        let ul = self.piece_manager.bytes_uploaded();

        if let Some(ref db) = self.db {
            db.mark_torrent_completed(&self.torrent.info_hash_hex, dl, ul);
        }

        let _ = self
            .tracker_manager
            .announce(&self.torrent.info_hash, ul, dl, 0, "completed")
            .await;

        let notifier = self.inner.lock().unwrap().completion_notifier.clone();
        if let Some(cb) = notifier {
            cb(
                self.torrent.name.clone(),
                self.torrent.total_length,
                dl,
                ul,
                self.torrent.files.len(),
            );
        }

        let (peers, main_t, speed_t) = {
            let mut g = self.inner.lock().unwrap();
            g.status = TorrentStatus::Completed;
            g.download_speed = 0.0;
            g.upload_speed = 0.0;
            g.eta_seconds = Some(0);
            let p: Vec<Arc<PeerConnection>> = g.active_peers.values().cloned().collect();
            g.active_peers.clear();
            (p, g.main_task.take(), g.speed_task.take())
        };
        for p in peers {
            p.stop().await;
        }
        if let Some(t) = main_t {
            t.abort();
        }
        if let Some(t) = speed_t {
            t.abort();
        }
        self.fire_slot_event();
    }

    async fn main_loop(self: Arc<Self>) {
        let mut last_announce = 0.0f64;
        let mut announce_interval = 30.0f64;

        while self.running.load(Ordering::SeqCst) {
            if self.piece_manager.is_complete() {
                self.stop_seeding_and_complete().await;
                break;
            }

            let now = now_unix_secs();

            // Check stall timeout
            let last_act = self.inner.lock().unwrap().last_data_received_time;
            if self.status() == TorrentStatus::Downloading
                && (now - last_act) > self.stall_timeout_seconds
            {
                if !self.handle_stall().await {
                    break;
                }
                last_announce = 0.0;
                continue;
            }

            // 1. Announce to trackers if interval elapsed
            if now - last_announce >= announce_interval {
                let dl = self.piece_manager.bytes_downloaded();
                let ul = self.piece_manager.bytes_uploaded();
                let left = self.torrent.total_length.saturating_sub(dl);
                let event = if last_announce == 0.0 { "started" } else { "" };

                let resp = self
                    .tracker_manager
                    .announce(&self.torrent.info_hash, ul, dl, left, event)
                    .await;
                last_announce = now;

                let mut g = self.inner.lock().unwrap();
                if !resp.peers.is_empty() {
                    if g.announce_failures > 0 {
                        info!(
                            "Tracker announce recovered for '{}' after {} failures",
                            self.torrent.name, g.announce_failures
                        );
                    }
                    g.announce_failures = 0;
                    for p in resp.peers {
                        let key = format!("{}:{}", p.ip, p.port);
                        if g.seen_peers.insert(key) {
                            g.peer_queue.push_back(p);
                        }
                    }
                    announce_interval = if g.active_peers.len() < 5 && g.peer_queue.len() < 10 {
                        30.0
                    } else {
                        (resp.interval as f64).max(120.0)
                    };
                } else {
                    g.announce_failures += 1;
                    announce_interval = 60.0;
                }
            }

            // 2. Replenish peer connections up to max_peers
            loop {
                let next_peer = {
                    let mut g = self.inner.lock().unwrap();
                    if g.active_peers.len() >= self.max_peers {
                        None
                    } else {
                        g.peer_queue.pop_front()
                    }
                };
                let Some(peer) = next_peer else { break };
                let peer_key = format!("{}:{}", peer.ip, peer.port);
                if self
                    .inner
                    .lock()
                    .unwrap()
                    .active_peers
                    .contains_key(&peer_key)
                {
                    continue;
                }

                let sess_disc = Arc::clone(&self);
                let on_disc = Arc::new(move |key: &str| {
                    sess_disc.inner.lock().unwrap().active_peers.remove(key);
                });

                let sess_throt = Arc::clone(&self);
                let is_throt = Arc::new(move || sess_throt.is_throttled());

                let sess_act = Arc::clone(&self);
                let on_act = Arc::new(move || {
                    sess_act.touch_activity();
                });

                let conn = PeerConnection::new(
                    peer,
                    self.torrent.info_hash,
                    self.tracker_manager.peer_id,
                    self.piece_manager.clone(),
                    Some(on_disc),
                    Some(is_throt),
                    Some(on_act),
                );
                self.inner
                    .lock()
                    .unwrap()
                    .active_peers
                    .insert(peer_key, Arc::clone(&conn));
                conn.start();
            }

            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    }

    async fn handle_stall(&self) -> bool {
        let idle_mins = (self.stall_timeout_seconds / 60.0) as u64;
        if self.stall_retry_delay_seconds <= 0.0 {
            let msg = format!(
                "Download stalled: no data received for {} minutes",
                idle_mins
            );
            {
                let mut g = self.inner.lock().unwrap();
                g.status = TorrentStatus::Error;
                g.error_message = Some(msg.clone());
            }
            if let Some(ref db) = self.db {
                db.mark_torrent_error(&self.torrent.info_hash_hex, &msg);
            }
            self.stop(true).await;
            return false;
        }

        let retries = {
            let mut g = self.inner.lock().unwrap();
            g.stall_retries += 1;
            let wait_mins = (self.stall_retry_delay_seconds / 60.0) as u64;
            let msg = format!(
                "Stalled — no data for {}m. Retry #{} in ~{}m.",
                idle_mins, g.stall_retries, wait_mins
            );
            g.error_message = Some(msg.clone());
            g.active_peers
                .retain(|_, c| c.is_connected.load(Ordering::Relaxed));
            if let Some(ref db) = self.db {
                db.log_event(&self.torrent.info_hash_hex, "STALLED", &msg);
            }
            g.stall_retries
        };
        warn!(
            "Torrent '{}' stalled (retry #{})",
            self.torrent.name, retries
        );

        let mut waited = 0.0;
        while waited < self.stall_retry_delay_seconds {
            if !self.running.load(Ordering::SeqCst) || self.status() != TorrentStatus::Downloading {
                return false;
            }
            let step = (self.stall_retry_delay_seconds - waited).min(5.0);
            tokio::time::sleep(Duration::from_secs_f64(step)).await;
            waited += step;
        }
        self.touch_activity();
        true
    }

    async fn speed_meter_loop(self: Arc<Self>) {
        let mut tick = 0u64;
        while self.running.load(Ordering::SeqCst) {
            tokio::time::sleep(Duration::from_secs(1)).await;
            tick += 1;

            if self.piece_manager.is_complete() {
                self.stop_seeding_and_complete().await;
                break;
            }

            let now = now_unix_secs();
            let curr_dl = self.piece_manager.bytes_downloaded();
            let curr_ul = self.piece_manager.bytes_uploaded();

            let (peers_to_update, status_str) = {
                let mut g = self.inner.lock().unwrap();
                let elapsed = (now - g.last_speed_check).max(0.001);
                let delta_dl = curr_dl.saturating_sub(g.last_downloaded_bytes);
                let delta_ul = curr_ul.saturating_sub(g.last_uploaded_bytes);
                if delta_dl > 0 {
                    g.last_data_received_time = now;
                    if g.error_message
                        .as_ref()
                        .map(|m| m.starts_with("Stalled"))
                        .unwrap_or(false)
                    {
                        g.error_message = None;
                        g.stall_retries = 0;
                    }
                }
                g.download_speed = (delta_dl as f64) / elapsed;
                g.upload_speed = (delta_ul as f64) / elapsed;
                g.last_downloaded_bytes = curr_dl;
                g.last_uploaded_bytes = curr_ul;
                g.last_speed_check = now;

                let bytes_left = self
                    .piece_manager
                    .selected_total_bytes()
                    .saturating_sub(curr_dl);
                g.eta_seconds = if g.download_speed > 0.0 && bytes_left > 0 {
                    Some((bytes_left as f64 / g.download_speed) as u64)
                } else if bytes_left == 0 {
                    Some(0)
                } else {
                    None
                };

                let peers: Vec<Arc<PeerConnection>> = g.active_peers.values().cloned().collect();
                (peers, g.status.as_str())
            };

            for p in peers_to_update {
                p.update_speed();
            }

            if tick.is_multiple_of(5) {
                if let Some(ref db) = self.db {
                    db.update_torrent_progress(
                        &self.torrent.info_hash_hex,
                        curr_dl,
                        curr_ul,
                        Some(status_str),
                    );
                }
            }
        }
    }

    pub fn active_peers_snapshot(&self) -> Vec<Value> {
        let g = self.inner.lock().unwrap();
        let mut out = Vec::with_capacity(g.active_peers.len());
        for (key, conn) in &g.active_peers {
            out.push(json!({
                "key": key,
                "ip": conn.peer.ip,
                "port": conn.peer.port,
                "connected": conn.is_connected.load(Ordering::Relaxed),
                "choked": conn.is_choked.load(Ordering::Relaxed),
                "interested": conn.am_interested.load(Ordering::Relaxed),
                "download_speed": (conn.download_speed() * 100.0).round() / 100.0,
                "bytes_downloaded": conn.bytes_downloaded.load(Ordering::Relaxed),
            }));
        }
        out
    }

    pub fn to_dict(&self) -> Value {
        let g = self.inner.lock().unwrap();
        let connected_count = g
            .active_peers
            .values()
            .filter(|c| c.is_connected.load(Ordering::Relaxed))
            .count();

        let files = &self.torrent.files;
        let truncated = files.len() > 500;
        let files_json: Vec<Value> = files
            .iter()
            .take(500)
            .enumerate()
            .map(|(idx, f)| {
                json!({
                    "index": idx,
                    "path": f.path,
                    "length": f.length,
                    "offset": f.offset,
                    "selected": self.piece_manager.is_file_selected(&f.path),
                    "priority": self.piece_manager.get_file_priority(&f.path),
                })
            })
            .collect();

        json!({
            "info_hash": self.torrent.info_hash_hex,
            "name": self.torrent.name,
            "status": g.status.as_str(),
            "error_message": g.error_message,
            "total_size": self.torrent.total_length,
            "downloaded": self.piece_manager.bytes_downloaded(),
            "uploaded": self.piece_manager.bytes_uploaded(),
            "progress": (self.piece_manager.progress_percentage() * 100.0).round() / 100.0,
            "download_speed": (g.download_speed * 100.0).round() / 100.0,
            "upload_speed": (g.upload_speed * 100.0).round() / 100.0,
            "download_limit": g.download_limit,
            "sequential": self.piece_manager.is_sequential(),
            "eta": g.eta_seconds,
            "peers_connected": connected_count,
            "peers_total": g.seen_peers.len(),
            "piece_count": self.torrent.piece_count,
            "pieces_completed": self.piece_manager.completed_count(),
            "piece_length": self.torrent.piece_length,
            "queue_position": g.queue_position,
            "is_multi_file": self.torrent.is_multi_file,
            "max_peers": self.max_peers,
            "files": files_json,
            "files_truncated": truncated,
            "files_total": files.len(),
            "selected_bytes": self.piece_manager.selected_total_bytes(),
            "skipped_pieces": self.piece_manager.skipped_count(),
            "trackers": self.torrent.trackers,
            "source": "live",
            "resumable": false,
        })
    }
}
