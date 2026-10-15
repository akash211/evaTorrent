//! Tracker subsystem coordinating HTTP/HTTPS (BEP 0003) and UDP (BEP 0015) announces,
//! automatic quarantine of dead trackers, and daily fallback list refreshes.

pub mod fallbacks;
pub mod http;
pub mod udp;

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use futures::future::join_all;
use rand::distributions::Alphanumeric;
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tracing::{debug, info, warn};

use crate::db::now_unix_secs;
use crate::tracker::http::HttpTracker;
use crate::tracker::udp::UdpTracker;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Peer {
    pub ip: String,
    pub port: u16,
}

#[derive(Debug, Clone, Default)]
pub struct TrackerResponse {
    pub interval: u32,
    pub peers: Vec<Peer>,
    pub complete: u32,
    pub incomplete: u32,
    pub failure_reason: Option<String>,
}

pub fn generate_peer_id() -> [u8; 20] {
    let rand_str: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(12)
        .map(char::from)
        .collect();
    let s = format!("-ET1000-{}", rand_str);
    let mut out = [0u8; 20];
    out.copy_from_slice(&s.as_bytes()[..20]);
    out
}

pub const DEFAULT_FALLBACK_TRACKERS: &[&str] = &[
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
];

pub const FAIL_QUARANTINE_THRESHOLD: u32 = 5;
pub const FAIL_QUARANTINE_COOLDOWN_SECS: f64 = 600.0;

#[derive(Debug, Clone)]
enum TrackerClient {
    Http(HttpTracker),
    Udp(UdpTracker),
}

impl TrackerClient {
    fn url(&self) -> &str {
        match self {
            TrackerClient::Http(t) => &t.url,
            TrackerClient::Udp(t) => &t.url,
        }
    }

    async fn announce(
        &self,
        info_hash: &[u8; 20],
        peer_id: &[u8; 20],
        port: u16,
        uploaded: u64,
        downloaded: u64,
        left: u64,
        event: Option<&str>,
    ) -> Option<TrackerResponse> {
        match self {
            TrackerClient::Http(t) => {
                t.announce(
                    info_hash, peer_id, port, uploaded, downloaded, left, event, 10.0,
                )
                .await
            }
            TrackerClient::Udp(t) => {
                t.announce(
                    info_hash, peer_id, port, uploaded, downloaded, left, event, 8.0,
                )
                .await
            }
        }
    }
}

#[derive(Debug, Clone, Default)]
struct TrackerStat {
    ok: u32,
    fail: u32,
    consec_fail: u32,
    last_ok: f64,
    last_fail: f64,
}

#[derive(Clone)]
pub struct TrackerManager {
    pub port: u16,
    pub peer_id: [u8; 20],
    clients: Vec<TrackerClient>,
    stats: Arc<Mutex<HashMap<String, TrackerStat>>>,
}

impl TrackerManager {
    pub fn new(
        tracker_urls: &[String],
        port: u16,
        add_fallbacks: bool,
        extra_fallbacks: Option<&[String]>,
    ) -> Self {
        let mut urls: Vec<String> = tracker_urls.to_vec();
        if add_fallbacks {
            for &fb in DEFAULT_FALLBACK_TRACKERS {
                let s = fb.to_string();
                if !urls.contains(&s) {
                    urls.push(s);
                }
            }
        }
        if let Some(extras) = extra_fallbacks {
            let max_total = tracker_urls.len() + 30;
            for fb in extras {
                if !urls.contains(fb) && urls.len() < max_total {
                    urls.push(fb.clone());
                }
            }
        }

        let mut clients = Vec::new();
        let mut stats_map = HashMap::new();

        for raw in urls {
            let clean = raw.trim().to_string();
            if clean.starts_with("http://") || clean.starts_with("https://") {
                stats_map.insert(clean.clone(), TrackerStat::default());
                clients.push(TrackerClient::Http(HttpTracker::new(clean)));
            } else if clean.starts_with("udp://") {
                stats_map.insert(clean.clone(), TrackerStat::default());
                clients.push(TrackerClient::Udp(UdpTracker::new(clean)));
            }
        }

        Self {
            port,
            peer_id: generate_peer_id(),
            clients,
            stats: Arc::new(Mutex::new(stats_map)),
        }
    }

    pub async fn announce(
        &self,
        info_hash: &[u8; 20],
        uploaded: u64,
        downloaded: u64,
        left: u64,
        event: &str,
    ) -> TrackerResponse {
        if self.clients.is_empty() {
            return TrackerResponse {
                interval: 1800,
                ..Default::default()
            };
        }

        let now = now_unix_secs();
        let mut active_indices = Vec::new();
        {
            let guard = self.stats.lock().unwrap();
            for (i, client) in self.clients.iter().enumerate() {
                if let Some(st) = guard.get(client.url()) {
                    if st.consec_fail >= FAIL_QUARANTINE_THRESHOLD
                        && (now - st.last_fail) < FAIL_QUARANTINE_COOLDOWN_SECS
                    {
                        continue;
                    }
                }
                active_indices.push(i);
            }
        }

        let event_opt = if event.is_empty() { None } else { Some(event) };
        let futs = active_indices.iter().map(|&idx| {
            let c = self.clients[idx].clone();
            let ih = *info_hash;
            let pid = self.peer_id;
            let port = self.port;
            let ev = event_opt.map(|s| s.to_string());
            async move {
                (
                    idx,
                    c.announce(&ih, &pid, port, uploaded, downloaded, left, ev.as_deref())
                        .await,
                )
            }
        });

        let results = join_all(futs).await;

        let mut all_peers = Vec::new();
        let mut seen = HashSet::new();
        let mut min_interval = 1800u32;
        let mut total_complete = 0u32;
        let mut total_incomplete = 0u32;

        let mut guard = self.stats.lock().unwrap();
        for (idx, res) in results {
            let name = self.clients[idx].url().to_string();
            let st = guard.entry(name.clone()).or_default();
            if let Some(resp) = res.filter(|r| r.failure_reason.is_none()) {
                let was_quarantined = st.consec_fail >= FAIL_QUARANTINE_THRESHOLD;
                st.ok += 1;
                st.consec_fail = 0;
                st.last_ok = now;
                if was_quarantined {
                    info!("[TRACKERS] Recovered: {} answering again", name);
                }
                if resp.interval > 0 {
                    min_interval = min_interval.min(resp.interval);
                }
                total_complete = total_complete.max(resp.complete);
                total_incomplete = total_incomplete.max(resp.incomplete);
                for p in resp.peers {
                    let key = format!("{}:{}", p.ip, p.port);
                    if seen.insert(key) {
                        all_peers.push(p);
                    }
                }
            } else {
                st.fail += 1;
                st.consec_fail += 1;
                st.last_fail = now;
                if st.consec_fail == FAIL_QUARANTINE_THRESHOLD {
                    warn!("[TRACKERS] Quarantining dead tracker for 10m: {}", name);
                } else {
                    debug!("Tracker {} failed", name);
                }
            }
        }

        TrackerResponse {
            interval: min_interval,
            peers: all_peers,
            complete: total_complete,
            incomplete: total_incomplete,
            failure_reason: None,
        }
    }

    pub fn get_health(&self) -> Vec<Value> {
        let now = now_unix_secs();
        let guard = self.stats.lock().unwrap();
        let mut out = Vec::with_capacity(self.clients.len());
        for client in &self.clients {
            let name = client.url();
            let st = guard.get(name).cloned().unwrap_or_default();
            let quarantined = st.consec_fail >= FAIL_QUARANTINE_THRESHOLD
                && (now - st.last_fail) < FAIL_QUARANTINE_COOLDOWN_SECS;
            out.push(json!({
                "url": name,
                "ok": st.ok,
                "fail": st.fail,
                "quarantined": quarantined,
                "last_ok": if st.last_ok > 0.0 { Some(st.last_ok) } else { None },
            }));
        }
        out
    }
}
