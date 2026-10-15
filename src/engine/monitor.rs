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
    // Fast path on Linux: /proc/self/status.
    if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
        for line in status.lines() {
            if line.starts_with("VmRSS:") {
                let kb_str = line.split_whitespace().nth(1)?;
                let kb: f64 = kb_str.parse().ok()?;
                return Some((kb / 1024.0 * 10.0).round() / 10.0);
            }
        }
    }
    // Portable fallback (Windows/macOS/small containers): sysinfo.
    sysinfo_rss_mb()
}

fn sysinfo_rss_mb() -> Option<f64> {
    let sys = sysinfo::System::new_all();
    let pid = sysinfo::get_current_pid().ok()?;
    let proc = sys.process(pid)?;
    let mb = proc.memory() as f64 / 1024.0 / 1024.0;
    Some((mb * 10.0).round() / 10.0)
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

/// Queries available and total disk space (in MB) for `dir`.
/// Tries `df -Pk` first (Unix), then falls back to sysinfo (Windows/macOS).
pub fn disk_space_mb(dir: &Path) -> (Option<f64>, Option<f64>) {
    if let Some(pair) = disk_space_df(dir) {
        return pair;
    }
    sysinfo_disk_space_mb(dir)
}

fn disk_space_df(dir: &Path) -> Option<(Option<f64>, Option<f64>)> {
    let out = Command::new("df").arg("-Pk").arg(dir).output().ok();
    let output = out.filter(|o| o.status.success())?;
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text.lines().nth(1)?;
    let cols: Vec<&str> = line.split_whitespace().collect();
    if cols.len() < 4 {
        return None;
    }
    let total_kb = cols[1].parse::<f64>().ok();
    let avail_kb = cols[3].parse::<f64>().ok();
    Some((
        avail_kb.map(|k| (k / 1024.0 * 10.0).round() / 10.0),
        total_kb.map(|k| (k / 1024.0 * 10.0).round() / 10.0),
    ))
}

fn sysinfo_disk_space_mb(dir: &Path) -> (Option<f64>, Option<f64>) {
    let target = norm_path(&dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf()));
    let disks = sysinfo::Disks::new_with_refreshed_list();
    // Longest mount-point prefix wins (handles overlapping mounts).
    let mut best: Option<(usize, u64, u64)> = None;
    for d in disks.list() {
        let mount = norm_path(d.mount_point());
        if !mount.is_empty() && target.starts_with(&mount) {
            let longer = best.map(|(l, _, _)| mount.len() > l).unwrap_or(true);
            if longer {
                best = Some((mount.len(), d.available_space(), d.total_space()));
            }
        }
    }
    match best {
        Some((_, avail_b, total_b)) => (
            Some((avail_b as f64 / 1024.0 / 1024.0 * 10.0).round() / 10.0),
            Some((total_b as f64 / 1024.0 / 1024.0 * 10.0).round() / 10.0),
        ),
        None => (None, None),
    }
}

/// Normalizes a path for prefix comparison: strips Windows `\\?\` verbatim
/// prefixes, unifies separators, lowercases (case-insensitive filesystems).
fn norm_path(p: &Path) -> String {
    let s = p.to_str().unwrap_or("").replace('/', "\\");
    let s = s.strip_prefix(r"\\?\").unwrap_or(&s);
    s.to_lowercase()
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rss_is_reported_on_all_platforms() {
        // Must be Some on Linux (/proc), Windows and macOS (sysinfo fallback).
        let rss = read_rss_mb();
        assert!(rss.is_some(), "RSS should be measurable on this platform");
        assert!(rss.unwrap() > 0.0);
    }

    #[test]
    fn disk_space_is_reported_on_all_platforms() {
        let dir = std::env::current_dir().unwrap();
        let (free_mb, total_mb) = disk_space_mb(&dir);
        assert!(free_mb.is_some(), "free disk should be measurable");
        assert!(total_mb.is_some(), "total disk should be measurable");
        assert!(total_mb.unwrap() >= free_mb.unwrap());
    }
}
