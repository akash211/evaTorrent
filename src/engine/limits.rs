//! Central resource limits for safe operation on small hosts (LXC / Proxmox / Docker).

pub fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|v| v.max(0.0) as usize)
        .unwrap_or(default)
}

pub fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|v| v.max(0.0) as u64)
        .unwrap_or(default)
}

pub fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(default)
}

pub fn env_str(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| default.to_string())
}

pub fn max_peers() -> usize {
    env_usize("EVA_MAX_PEERS", 50)
}

pub fn pipeline_per_peer() -> usize {
    env_usize("EVA_PIPELINE_PER_PEER", 10).max(1)
}

pub fn max_ongoing_pieces() -> usize {
    env_usize("EVA_MAX_ONGOING_PIECES", 32).max(1)
}

pub fn stream_buffer_limit() -> usize {
    env_usize("EVA_STREAM_BUFFER_LIMIT", 2 * 1024 * 1024).max(65536)
}

pub fn telemetry_interval_secs() -> f64 {
    env_f64("EVA_TELEMETRY_SECS", 0.8).clamp(0.5, 10.0)
}

pub fn verify_on_startup() -> String {
    env_str("EVA_VERIFY_ON_STARTUP", "quick").to_lowercase()
}

pub fn large_torrent_bytes() -> u64 {
    env_u64("EVA_LARGE_TORRENT_BYTES", 20 * 1024 * 1024 * 1024)
}

pub fn rarest_sort_cap() -> usize {
    env_usize("EVA_RAREST_SORT_CAP", 2000).max(100)
}

pub fn rarest_refresh_secs() -> f64 {
    env_f64("EVA_RAREST_REFRESH_SECS", 10.0)
}

/// Returns `(max_peers, pipeline)` auto-tuned for torrent size.
pub fn effective_peer_settings(total_bytes: u64) -> (usize, usize) {
    let mut peers = max_peers();
    let mut pipe = pipeline_per_peer();
    if total_bytes >= large_torrent_bytes() {
        peers = peers.min(env_usize("EVA_LARGE_MAX_PEERS", 15));
        pipe = pipe.min(env_usize("EVA_LARGE_PIPELINE", 4));
    }
    (peers.max(1), pipe.max(1))
}
