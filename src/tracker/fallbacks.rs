//! Auto-updated public tracker fallback list (`ngosang/trackerslist`).

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};
use tracing::{debug, info, warn};

use crate::db::now_unix_secs;

pub const MIRRORS: &[&str] = &[
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://ngosang.github.io/trackerslist/trackers_best.txt",
];

pub const CACHE_FILENAME: &str = "best.txt";
pub const META_FILENAME: &str = "meta.json";
pub const MIN_TRACKERS: usize = 5;

pub fn cache_paths(base_dir: &Path) -> (PathBuf, PathBuf) {
    let dir = base_dir.join(".tracker_cache");
    (dir.join(CACHE_FILENAME), dir.join(META_FILENAME))
}

pub fn validate_tracker_lines(lines: &[&str]) -> Vec<String> {
    let mut seen = Vec::new();
    for raw in lines {
        let url = raw.trim();
        if url.is_empty() || url.len() > 256 || url.contains(' ') {
            continue;
        }
        if !url.starts_with("udp://") && !url.starts_with("http://") && !url.starts_with("https://")
        {
            continue;
        }
        let s = url.to_string();
        if !seen.contains(&s) {
            seen.push(s);
        }
    }
    seen
}

pub fn read_cached(base_dir: &Path) -> (Vec<String>, f64, String) {
    let (cache_file, meta_file) = cache_paths(base_dir);
    if !cache_file.exists() {
        return (Vec::new(), f64::INFINITY, "none".to_string());
    }
    let Ok(text) = std::fs::read_to_string(&cache_file) else {
        return (Vec::new(), f64::INFINITY, "corrupt".to_string());
    };
    let lines: Vec<&str> = text.lines().collect();
    let trackers = validate_tracker_lines(&lines);
    let mut age_days = f64::INFINITY;
    let mut source = "cache".to_string();

    if let Ok(meta_raw) = std::fs::read_to_string(&meta_file) {
        if let Ok(meta) = serde_json::from_str::<Value>(&meta_raw) {
            if let Some(ts) = meta.get("fetched_at").and_then(|v| v.as_f64()) {
                age_days = (now_unix_secs() - ts) / 86400.0;
            }
            if let Some(src) = meta.get("source").and_then(|v| v.as_str()) {
                source = src.to_string();
            }
        }
    }
    (trackers, age_days, source)
}

pub async fn refresh_fallback_trackers(base_dir: &Path) -> anyhow::Result<Vec<String>> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .user_agent("evaTorrent/1.0")
        .build()?;

    let mut last_err = String::from("No mirrors tried");
    for &url in MIRRORS {
        match client.get(url).send().await {
            Ok(resp) if resp.status().is_success() => {
                if let Ok(text) = resp.text().await {
                    let lines: Vec<&str> = text.lines().collect();
                    let trackers = validate_tracker_lines(&lines);
                    if trackers.len() < MIN_TRACKERS {
                        warn!(
                            "[TRACKERS] Mirror {} yielded only {} trackers; trying next",
                            url,
                            trackers.len()
                        );
                        continue;
                    }
                    let (cache_file, meta_file) = cache_paths(base_dir);
                    if let Some(parent) = cache_file.parent() {
                        let _ = std::fs::create_dir_all(parent);
                    }
                    let _ = std::fs::write(&cache_file, format!("{}\n", trackers.join("\n")));
                    let _ = std::fs::write(
                        &meta_file,
                        serde_json::to_string(&json!({
                            "fetched_at": now_unix_secs(),
                            "source": url,
                            "count": trackers.len(),
                        }))
                        .unwrap_or_default(),
                    );
                    info!(
                        "[TRACKERS] Refreshed fallback list: {} trackers via {}",
                        trackers.len(),
                        url
                    );
                    return Ok(trackers);
                }
            }
            Ok(resp) => {
                last_err = format!("HTTP {}", resp.status());
                debug!("[TRACKERS] Mirror {} returned {}", url, last_err);
            }
            Err(e) => {
                last_err = e.to_string();
                debug!("[TRACKERS] Mirror {} failed: {}", url, last_err);
            }
        }
    }

    anyhow::bail!("All tracker mirrors failed: {}", last_err)
}

pub async fn ensure_fresh_trackers(
    base_dir: &Path,
    max_age_days: Option<f64>,
) -> anyhow::Result<Vec<String>> {
    let max_age = max_age_days.unwrap_or_else(|| {
        std::env::var("EVA_TRACKER_CACHE_DAYS")
            .ok()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(7.0)
    });
    let (cached, age_days, _) = read_cached(base_dir);
    if !cached.is_empty() && age_days <= max_age {
        return Ok(cached);
    }
    match refresh_fallback_trackers(base_dir).await {
        Ok(fresh) => Ok(fresh),
        Err(e) => {
            if !cached.is_empty() {
                warn!(
                    "[TRACKERS] Refresh failed ({}); keeping {} cached trackers",
                    e,
                    cached.len()
                );
                Ok(cached)
            } else {
                Err(e)
            }
        }
    }
}
