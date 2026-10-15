//! Disk cache for Discover media lookups (`{DOWNLOAD_DIR}/cached_results/discover_*.json`).

use std::path::{Path, PathBuf};

use chrono::Utc;
use regex::Regex;
use serde_json::{json, Value};

use crate::db::now_unix_secs;
use crate::search::cache::format_cache_age;

#[derive(Clone)]
pub struct DiscoverCacheManager {
    pub cache_dir: PathBuf,
    pub max_cached: usize,
    pub index_file: PathBuf,
}

impl DiscoverCacheManager {
    pub fn new(cache_dir: impl AsRef<Path>, max_cached: usize) -> Self {
        let cache_dir = cache_dir.as_ref().to_path_buf();
        let _ = std::fs::create_dir_all(&cache_dir);
        let index_file = cache_dir.join("discover_index.json");
        let mgr = Self {
            cache_dir,
            max_cached,
            index_file,
        };
        if !mgr.index_file.exists() {
            mgr.save_index(&[]);
        }
        mgr
    }

    fn load_index(&self) -> Vec<Value> {
        if let Ok(raw) = std::fs::read_to_string(&self.index_file) {
            if let Ok(arr) = serde_json::from_str::<Vec<Value>>(&raw) {
                return arr;
            }
        }
        Vec::new()
    }

    fn save_index(&self, entries: &[Value]) {
        let tmp = self.index_file.with_extension("tmp");
        if let Ok(s) = serde_json::to_string_pretty(entries) {
            if std::fs::write(&tmp, s).is_ok() {
                let _ = std::fs::rename(&tmp, &self.index_file);
            }
        }
    }

    fn filename(&self, query: &str, media_type: &str, year: Option<i64>) -> String {
        let re_nonword = Regex::new(r"[^\w\s-]").unwrap();
        let re_spaces = Regex::new(r"[-\s]+").unwrap();
        let lowered = query.trim().to_lowercase();
        let clean = re_nonword.replace_all(&lowered, "");
        let slug: String = re_spaces
            .replace_all(&clean, "_")
            .chars()
            .take(40)
            .collect();
        let suffix = year.map(|y| format!("_{}", y)).unwrap_or_default();
        format!(
            "discover_{}_{}{}.json",
            slug,
            media_type.trim().to_lowercase(),
            suffix
        )
    }

    pub fn get(&self, query: &str, media_type: &str, year: Option<i64>) -> Option<Value> {
        let path = self.cache_dir.join(self.filename(query, media_type, year));
        let raw = std::fs::read_to_string(&path).ok()?;
        let mut data: Value = serde_json::from_str(&raw).ok()?;
        let ts = data
            .get("cached_timestamp")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let age = (now_unix_secs() - ts).max(0.0);
        if let Some(obj) = data.as_object_mut() {
            obj.insert("cache_age_seconds".to_string(), json!(age.round() as u64));
            obj.insert("cache_age_human".to_string(), json!(format_cache_age(age)));
            obj.insert("is_cached".to_string(), json!(true));
        }
        Some(data)
    }

    pub fn set(&self, query: &str, media_type: &str, year: Option<i64>, payload: &Value) {
        let has_results = payload
            .get("results")
            .and_then(|r| r.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false);
        if !has_results {
            return;
        }
        let now = now_unix_secs();
        let now_iso = Utc::now().to_rfc3339();
        let filename = self.filename(query, media_type, year);
        let cache_file = self.cache_dir.join(&filename);

        let mut stored = payload.clone();
        if let Some(obj) = stored.as_object_mut() {
            obj.insert("cached_at".to_string(), json!(now_iso));
            obj.insert("cached_timestamp".to_string(), json!(now));
        }
        if let Ok(s) = serde_json::to_string_pretty(&stored) {
            if std::fs::write(&cache_file, s).is_err() {
                return;
            }
        }

        let mut index = self.load_index();
        let qn = query.trim().to_lowercase();
        let tn = media_type.trim().to_lowercase();
        index.retain(|it| {
            let iq = it
                .get("query")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_lowercase();
            let itype = it
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_lowercase();
            let iy = it.get("year").and_then(|v| v.as_i64());
            !(iq == qn && itype == tn && iy == year)
        });

        let total_res = payload
            .get("total_found")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        index.insert(
            0,
            json!({
                "query": query.trim(),
                "type": tn,
                "year": year,
                "cached_at": now_iso,
                "cached_timestamp": now,
                "total_results": total_res,
                "filename": filename,
            }),
        );

        if index.len() > self.max_cached {
            for item in &index[self.max_cached..] {
                if let Some(fn_str) = item.get("filename").and_then(|v| v.as_str()) {
                    let _ = std::fs::remove_file(self.cache_dir.join(fn_str));
                }
            }
            index.truncate(self.max_cached);
        }
        self.save_index(&index);
    }

    pub fn get_recent(&self, limit: usize) -> Vec<Value> {
        let now = now_unix_secs();
        let mut recent = Vec::new();
        for mut item in self.load_index().into_iter().take(limit) {
            let ts = item
                .get("cached_timestamp")
                .and_then(|v| v.as_f64())
                .unwrap_or(now);
            let age = (now - ts).max(0.0);
            if let Some(obj) = item.as_object_mut() {
                obj.insert("cache_age_seconds".to_string(), json!(age.round() as u64));
                obj.insert("cache_age_human".to_string(), json!(format_cache_age(age)));
            }
            recent.push(item);
        }
        recent
    }
}
