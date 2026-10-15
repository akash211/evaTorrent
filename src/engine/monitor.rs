//! Resource-pressure telemetry, crash forensics, and disk space monitor for small hosts.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::db::now_unix_secs;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemHealth {
    pub rss_mb: Option<f64>,
    pub cgroup_limit_mb: Option<f64>,
    pub cgroup_oom_kills: Option<u64>,
    pub cpu_throttled_secs: Option<f64>,
    pub free_disk_mb: Option<f64>,
    pub total_disk_mb: Option<f64>,
}

pub fn read_rss_mb() -> Option<f64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    for line in status.lines() {
        if line.starts_with("VmRSS:") {
            let kb_str = line.split_whitespace().nth(1)?;
            let kb: f64 = kb_str.parse().ok()?;
            return Some((kb / 1024.0 * 10.0).round() / 10.0);
        }
    }
    None
}

fn read_cgroup_file(names: &[&str]) -> Option<String> {
    for &name in names {
        for base in ["/sys/fs/cgroup", "/sys/fs/cgroup/memory"] {
            let p = Path::new(base).join(name);
            if p.exists() {
                if let Ok(txt) = std::fs::read_to_string(&p) {
                    return Some(txt.trim().to_string());
                }
            }
        }
    }
    None
}

pub fn cgroup_memory_limit_mb() -> Option<f64> {
    let raw = read_cgroup_file(&["memory.max", "memory.limit_in_bytes"])?;
    let val: u64 = raw.parse().ok()?;
    if val == 0 || val >= (1u64 << 62) {
        return None;
    }
    Some(((val as f64) / (1024.0 * 1024.0) * 10.0).round() / 10.0)
}

pub fn cgroup_oom_kills() -> Option<u64> {
    let raw = read_cgroup_file(&["memory.events", "memory.oom_control"])?;
    for line in raw.lines() {
        let mut parts = line.split_whitespace();
        if let (Some("oom_kill"), Some(v)) = (parts.next(), parts.next()) {
            if let Ok(kills) = v.parse::<u64>() {
                return Some(kills);
            }
        }
    }
    None
}

pub fn cgroup_cpu_throttled_secs() -> Option<f64> {
    let raw = read_cgroup_file(&["cpu.stat"])?;
    for line in raw.lines() {
        let mut parts = line.split_whitespace();
        if let (Some("throttled_usec"), Some(v)) = (parts.next(), parts.next()) {
            if let Ok(usec) = v.parse::<u64>() {
                return Some((usec as f64) / 1e6);
            }
        }
    }
    None
}

/// Queries available and total disk space (in MB) for `dir` using `df -Pk`.
pub fn disk_space_mb(dir: &Path) -> (Option<f64>, Option<f64>) {
    let out = Command::new("df").arg("-Pk").arg(dir).output().ok();
    let Some(output) = out.filter(|o| o.status.success()) else {
        return (None, None);
    };
    let text = String::from_utf8_lossy(&output.stdout);
    let Some(line) = text.lines().nth(1) else {
        return (None, None);
    };
    let cols: Vec<&str> = line.split_whitespace().collect();
    if cols.len() < 4 {
        return (None, None);
    }
    let total_kb = cols[1].parse::<f64>().ok();
    let avail_kb = cols[3].parse::<f64>().ok();
    (
        avail_kb.map(|k| (k / 1024.0 * 10.0).round() / 10.0),
        total_kb.map(|k| (k / 1024.0 * 10.0).round() / 10.0),
    )
}

pub fn get_system_health(download_dir: &Path) -> SystemHealth {
    let (free_disk_mb, total_disk_mb) = disk_space_mb(download_dir);
    SystemHealth {
        rss_mb: read_rss_mb(),
        cgroup_limit_mb: cgroup_memory_limit_mb(),
        cgroup_oom_kills: cgroup_oom_kills(),
        cpu_throttled_secs: cgroup_cpu_throttled_secs(),
        free_disk_mb,
        total_disk_mb,
    }
}

pub fn log_cgroup_boot_line() {
    let oom = cgroup_oom_kills();
    let throttle = cgroup_cpu_throttled_secs();
    let limit = cgroup_memory_limit_mb();
    if oom.is_none() && throttle.is_none() && limit.is_none() {
        return;
    }
    info!(
        "[RESOURCE] container stats at boot: oom_kills={:?} cpu_throttled={:?}s mem_limit={:?}MB",
        oom, throttle, limit
    );
    if let Some(k) = oom.filter(|&k| k > 0) {
        warn!(
            "[RESOURCE] This container was OOM-killed {} time(s) before. With the Rust engine, memory overhead is ~20x lower.",
            k
        );
    }
}

pub fn check_previous_shutdown(data_dir: &Path) {
    let hb = data_dir.join(".heartbeat");
    let clean = data_dir.join(".clean_shutdown");
    if !hb.exists() {
        return;
    }
    let last_ts = std::fs::read_to_string(&hb)
        .ok()
        .and_then(|s| s.split_whitespace().next()?.parse::<f64>().ok());
    let Some(last) = last_ts else {
        return;
    };
    let age = now_unix_secs() - last;
    let clean_exists = clean.exists();
    if clean_exists {
        let _ = std::fs::remove_file(&clean);
        info!("[RESOURCE] Previous run shut down cleanly.");
        return;
    }
    if age < 24.0 * 3600.0 {
        warn!(
            "[RESOURCE] Previous run did NOT shut down cleanly — last heartbeat {:.0}s ago.",
            age
        );
    }
}

pub fn mark_clean_shutdown(data_dir: &Path) {
    let _ = std::fs::write(
        data_dir.join(".clean_shutdown"),
        format!("{:.0}\n", now_unix_secs()),
    );
    let _ = std::fs::remove_file(data_dir.join(".heartbeat"));
}

pub fn write_heartbeat(data_dir: &Path) {
    let _ = std::fs::write(
        data_dir.join(".heartbeat"),
        format!("{:.0}\n", now_unix_secs()),
    );
}

#[allow(dead_code)]
pub struct ResourceMonitor {
    pub data_dir: PathBuf,
}
