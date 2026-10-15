//! Persistent cross-session search cache (`{DOWNLOAD_DIR}/cached_results`).

use std::path::{Path, PathBuf};

use chrono::Utc;
use regex::Regex;
use serde_json::{json, Value};

use crate::db::now_unix_secs;

pub fn format_cache_age(seconds: f64) -> String {
    if seconds < 60.0 {
        return "just now".to_string();
    }
    let mins = (seconds / 60.0) as u64;
    if mins < 60 {
        return format!("{} min{} ago", mins, if mins > 1 { "s" } else { "" });
    }
    let hours = mins / 60;
    if hours < 24 {
        return format!("{} hour{} ago", hours, if hours > 1 { "s" } else { "" });
    }
    let days = hours / 24;
    format!("{} day{} ago", days, if days > 1 { "s" } else { "" })
}

#[derive(Clone)]
pub struct SearchCacheManager {
    pub cache_dir: PathBuf,
    pub max_cached: usize,
    pub index_file: PathBuf,
}

impl SearchCacheManager {
    pub fn new(cache_dir: impl AsRef<Path>, max_cached: usize) -> Self {
        let cache_dir = cache_dir.as_ref().to_path_buf();
        let _ = std::fs::create_dir_all(&cache_dir);
        let index_file = cache_dir.join("index.json");
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

    fn cache_filename(&self, query: &str, category: &str) -> String {
        let re_nonword = Regex::new(r"[^\w\s-]").unwrap();
        let re_spaces = Regex::new(r"[-\s]+").unwrap();
        let lowered = query.trim().to_lowercase();
        let clean = re_nonword.replace_all(&lowered, "");
        let slug: String = re_spaces
            .replace_all(&clean, "_")
            .chars()
            .take(40)
            .collect();
        format!("search_{}_{}.json", slug, category.trim().to_lowercase())
    }

    pub fn get(&self, query: &str, category: &str) -> Option<Value> {
        let filename = self.cache_filename(query, category);
        let path = self.cache_dir.join(filename);
        let raw = std::fs::read_to_string(&path).ok()?;
        let mut data: Value = serde_json::from_str(&raw).ok()?;
        let cached_ts = data
            .get("cached_timestamp")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let age = (now_unix_secs() - cached_ts).max(0.0);
        if let Some(obj) = data.as_object_mut() {
            obj.insert("cache_age_seconds".to_string(), json!(age.round() as u64));
            obj.insert("cache_age_human".to_string(), json!(format_cache_age(age)));
            obj.insert("is_cached".to_string(), json!(true));
        }
        Some(data)
    }

    pub fn set(
        &self,
        query: &str,
        category: &str,
        results: &[Value],
        suggestion: Option<&str>,
        total_found: usize,
        fallback_applied: bool,
    ) {
        if results.is_empty() {
            return;
        }
        let now = now_unix_secs();
        let now_iso = Utc::now().to_rfc3339();
        let filename = self.cache_filename(query, category);
        let cache_file = self.cache_dir.join(&filename);

        let payload = json!({
            "query": query.trim(),
            "category": category.trim().to_lowercase(),
            "cached_at": now_iso,
            "cached_timestamp": now,
            "total_found": total_found,
            "returned": results.len(),
            "fallback_applied": fallback_applied,
            "suggestion": suggestion,
            "results": results,
        });

        if let Ok(s) = serde_json::to_string_pretty(&payload) {
            if std::fs::write(&cache_file, s).is_err() {
                return;
            }
        }

        let mut index = self.load_index();
        let q_norm = query.trim().to_lowercase();
        let cat_norm = category.trim().to_lowercase();

        index.retain(|item| {
            let iq = item
                .get("query")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_lowercase();
            let ic = item
                .get("category")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_lowercase();
            !(iq == q_norm && ic == cat_norm)
        });

        index.insert(
            0,
            json!({
                "query": query.trim(),
                "category": cat_norm,
                "cached_at": now_iso,
                "cached_timestamp": now,
                "total_results": total_found,
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
