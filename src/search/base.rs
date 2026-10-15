//! Data models and trait interface for torrent search providers.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::torrent::urlencoding_encode;

pub const DEFAULT_SEARCH_TRACKERS: &[&str] = &[
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SearchCategory {
    All,
    Movies,
    Series,
    Software,
    Games,
    Books,
}

impl SearchCategory {
    pub fn from_str(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "movies" => SearchCategory::Movies,
            "series" => SearchCategory::Series,
            "software" => SearchCategory::Software,
            "games" => SearchCategory::Games,
            "books" => SearchCategory::Books,
            _ => SearchCategory::All,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            SearchCategory::All => "all",
            SearchCategory::Movies => "movies",
            SearchCategory::Series => "series",
            SearchCategory::Software => "software",
            SearchCategory::Games => "games",
            SearchCategory::Books => "books",
        }
    }
}

pub fn format_bytes(num_bytes: u64) -> String {
    if num_bytes == 0 {
        return "0 B".to_string();
    }
    let units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let mut size = num_bytes as f64;
    let mut idx = 0usize;
    while size >= 1024.0 && idx < units.len() - 1 {
        size /= 1024.0;
        idx += 1;
    }
    if idx == 0 {
        format!("{} B", num_bytes)
    } else {
        format!("{:.2} {}", size, units[idx])
    }
}

pub fn parse_size_to_bytes(size_str: &str) -> u64 {
    let s = size_str.trim();
    let mut num_part = String::new();
    let mut unit_part = String::new();
    for ch in s.chars() {
        if ch.is_ascii_digit() || ch == '.' {
            if unit_part.is_empty() {
                num_part.push(ch);
            }
        } else if ch.is_ascii_alphabetic() {
            unit_part.push(ch);
        }
    }
    let Ok(val) = num_part.parse::<f64>() else {
        return 0;
    };
    let u = unit_part.to_uppercase();
    if u.starts_with('K') {
        (val * 1024.0) as u64
    } else if u.starts_with('M') {
        (val * 1024.0 * 1024.0) as u64
    } else if u.starts_with('G') {
        (val * 1024.0 * 1024.0 * 1024.0) as u64
    } else if u.starts_with('T') {
        (val * 1024.0 * 1024.0 * 1024.0 * 1024.0) as u64
    } else {
        val as u64
    }
}

pub fn build_magnet(info_hash: &str, title: &str) -> String {
    let dn = urlencoding_encode(title);
    let mut trackers = String::new();
    for &tr in DEFAULT_SEARCH_TRACKERS {
        trackers.push_str("&tr=");
        trackers.push_str(&urlencoding_encode(tr));
    }
    format!(
        "magnet:?xt=urn:btih:{}&dn={}{}",
        info_hash.trim().to_lowercase(),
        dn,
        trackers
    )
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub title: String,
    pub info_hash: String,
    pub magnet_uri: String,
    pub size_bytes: u64,
    pub size_formatted: String,
    pub seeders: u32,
    pub leechers: u32,
    pub category: String,
    pub provider: String,
    #[serde(default)]
    pub added_date: String,
    #[serde(default)]
    pub source_url: String,
    #[serde(default)]
    pub torrent_url: String,
    #[serde(default)]
    pub health_score: f64,
    #[serde(default)]
    pub age_days: u32,
}

impl SearchResult {
    pub fn to_dict(&self) -> Value {
        json!({
            "title": self.title,
            "info_hash": self.info_hash,
            "magnet_uri": self.magnet_uri,
            "size_bytes": self.size_bytes,
            "size_formatted": self.size_formatted,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "category": self.category,
            "provider": self.provider,
            "added_date": self.added_date,
            "source_url": self.source_url,
            "torrent_url": self.torrent_url,
            "health_score": (self.health_score * 100.0).round() / 100.0,
            "age_days": self.age_days,
        })
    }
}

#[async_trait]
pub trait SearchProvider: Send + Sync {
    fn name(&self) -> &'static str;
    async fn search(
        &self,
        query: &str,
        category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult>;
}
