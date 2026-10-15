//! Subtitle service: video scan, filename parser (`SxxExx` & year aware), English subtitle search,
//! and zip-slip-guarded `.srt`/`.vtt` extraction.

use std::collections::HashSet;
use std::io::{Cursor, Read};
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::subtitles::providers::{
    search_opensubtitles_com, search_opensubtitles_org, search_yify, SubtitleResult,
};

pub const VIDEO_EXTENSIONS: &[&str] = &[
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "ts", "mpg", "mpeg",
];
pub const SUBTITLE_EXTENSIONS: &[&str] = &["srt", "vtt", "sub", "ssa", "ass"];
pub const SKIP_DIRS: &[&str] = &[".torrent_cache", "cached_results", ".git", "__pycache__"];

pub const MAX_SUBTITLE_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_ZIP_BYTES: usize = 20 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VideoEntry {
    pub path: String,
    pub name: String,
    pub size: u64,
    pub has_subtitle: bool,
}

pub fn clean_video_title(filename: &str) -> (String, Option<i64>, Option<u32>, Option<u32>) {
    let raw = Path::new(filename)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(filename);

    let re_se = Regex::new(r"(?i)[Ss](\d{1,2})[Ee](\d{1,3})").unwrap();
    let re_year = Regex::new(r"\b(19\d{2}|20\d{2})\b").unwrap();
    let re_brackets = Regex::new(r"\[[^\]]*\]|\{[^}]*\}").unwrap();
    let re_tags = Regex::new(
        r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|hd|web-?dl|webrip|bluray|brrip|bdrip|dvdrip|hdtv|amzn|netflix|hmax|dsnp|x264|x265|h\.?264|h\.?265|hevc|xvid|aac|ac3|dts|ddp|yify|yts|psa|evo|rartv|ettv|proper|repack|extended|remux|dual|multi|eng|hin|hindi|group)\b",
    )
    .unwrap();
    let re_sep = Regex::new(r"[._\-+]+").unwrap();
    let re_ws = Regex::new(r"\s+").unwrap();

    let mut season = None;
    let mut episode = None;
    if let Some(m) = re_se.captures(raw) {
        season = m[1].parse::<u32>().ok();
        episode = m[2].parse::<u32>().ok();
    }
    let year = re_year.captures(raw).and_then(|m| m[1].parse::<i64>().ok());

    let stem = re_brackets.replace_all(raw, " ");
    // Strip trailing "- GROUP" scene suffix (but never a trailing S01E01 or year).
    // Handles both " - Ozlem" and "x264-GROUP" (no-space dash) forms.
    let re_tail = Regex::new(r"[\s._-]+[-–—][\s._]*([A-Za-z0-9]+)\s*$").unwrap();
    let re_se_full = Regex::new(r"(?i)^[Ss]\d{1,2}[Ee]\d{1,3}$").unwrap();
    let re_year_full = Regex::new(r"^(19\d{2}|20\d{2})$").unwrap();
    let stem_owned: String = if let Some(m) = re_tail.captures(&stem) {
        let tok = m.get(1).map(|g| g.as_str()).unwrap_or("");
        if !re_se_full.is_match(tok) && !re_year_full.is_match(tok) {
            let cut = m.get(0).map(|g| g.start()).unwrap_or(stem.len());
            stem[..cut].trim_end().to_string()
        } else {
            stem.to_string()
        }
    } else {
        stem.to_string()
    };
    let text = re_sep.replace_all(&stem_owned, " ");
    let text = re_se.replace_all(&text, " ");
    let text = re_year.replace_all(&text, " ");
    let text = re_tags.replace_all(&text, " ");
    let clean = re_ws
        .replace_all(&text, " ")
        .trim_matches(|c: char| " -_().[]".contains(c))
        .to_string();

    (
        if clean.is_empty() {
            raw.to_string()
        } else {
            clean
        },
        year,
        season,
        episode,
    )
}

pub fn scan_videos(download_dir: &Path, limit: usize) -> Vec<VideoEntry> {
    let mut entries = Vec::new();
    if !download_dir.exists() {
        return entries;
    }
    scan_dir_recursive(download_dir, download_dir, &mut entries, limit);
    entries.sort_by_key(|a| a.name.to_lowercase());
    entries.truncate(limit);
    entries
}

fn scan_dir_recursive(base: &Path, current: &Path, entries: &mut Vec<VideoEntry>, limit: usize) {
    if entries.len() >= limit {
        return;
    }
    let Ok(read_dir) = std::fs::read_dir(current) else {
        return;
    };
    let mut paths: Vec<PathBuf> = read_dir.filter_map(|e| e.ok().map(|x| x.path())).collect();
    paths.sort();

    for path in paths {
        let Some(name_str) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.is_dir() {
            if !name_str.starts_with('.') && !SKIP_DIRS.contains(&name_str) {
                scan_dir_recursive(base, &path, entries, limit);
            }
            continue;
        }
        let ext = path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        if !VIDEO_EXTENSIONS.contains(&ext.as_str()) {
            continue;
        }
        let Ok(rel) = path.strip_prefix(base) else {
            continue;
        };
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        let size = path.metadata().map(|m| m.len()).unwrap_or(0);
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or(name_str);
        let parent = path.parent().unwrap_or(base);
        let has_subtitle = SUBTITLE_EXTENSIONS.iter().any(|s_ext| {
            parent.join(format!("{}.{}", stem, s_ext)).exists()
                || parent.join(format!("{}.en.{}", stem, s_ext)).exists()
        });
        entries.push(VideoEntry {
            path: rel_str,
            name: name_str.to_string(),
            size,
            has_subtitle,
        });
        if entries.len() >= limit {
            return;
        }
    }
}

pub fn pick_zip_member(names: &[String]) -> Option<String> {
    let mut cands: Vec<String> = names
        .iter()
        .filter(|n| {
            let ext = Path::new(n)
                .extension()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_lowercase();
            SUBTITLE_EXTENSIONS.contains(&ext.as_str())
        })
        .cloned()
        .collect();
    if cands.is_empty() {
        return None;
    }
    cands.sort_by_key(|n| {
        let low = n.to_lowercase();
        let is_en = if low.contains("eng") || low.contains(".en.") || low.ends_with(".en.srt") {
            0
        } else {
            1
        };
        let is_srt = if low.ends_with(".srt") { 0 } else { 1 };
        (is_en, is_srt, low)
    });
    cands.into_iter().next()
}

pub fn extract_subtitle_from_bytes(payload: &[u8], url: &str) -> Result<(Vec<u8>, String), String> {
    if payload.len() > MAX_ZIP_BYTES {
        return Err("Subtitle download exceeds 20 MB size limit.".to_string());
    }
    let clean_url = url.split('?').next().unwrap_or(url);
    let mut ext = Path::new(clean_url)
        .extension()
        .and_then(|s| s.to_str())
        .map(|s| format!(".{}", s.to_lowercase()))
        .unwrap_or_default();

    let data = if payload.starts_with(b"PK") || ext == ".zip" {
        let reader = Cursor::new(payload);
        let mut archive =
            zip::ZipArchive::new(reader).map_err(|e| format!("Invalid subtitle zip: {}", e))?;
        let mut names = Vec::new();
        for i in 0..archive.len() {
            if let Ok(file) = archive.by_index(i) {
                if !file.is_dir() {
                    names.push(file.name().to_string());
                }
            }
        }
        if names.is_empty() {
            return Err("Zip archive contains no files.".to_string());
        }
        let member = pick_zip_member(&names)
            .ok_or_else(|| "Zip archive contains no subtitle (.srt/.vtt/.sub) file.".to_string())?;

        // Zip-slip guard
        let member_path = Path::new(&member);
        if member_path.is_absolute()
            || member_path
                .components()
                .any(|c| matches!(c, Component::ParentDir))
        {
            return Err("Unsafe path inside subtitle zip.".to_string());
        }

        let mut zfile = archive
            .by_name(&member)
            .map_err(|e| format!("Zip read error: {}", e))?;
        if zfile.size() as usize > MAX_SUBTITLE_BYTES {
            return Err("Subtitle inside zip exceeds 2 MB size limit.".to_string());
        }
        let mut buf = Vec::with_capacity(zfile.size() as usize);
        zfile
            .read_to_end(&mut buf)
            .map_err(|e| format!("Failed reading zip entry: {}", e))?;
        ext = member_path
            .extension()
            .and_then(|s| s.to_str())
            .map(|s| format!(".{}", s.to_lowercase()))
            .unwrap_or_else(|| ".srt".to_string());
        buf
    } else {
        payload.to_vec()
    };

    let bare_ext = ext.trim_start_matches('.');
    if !SUBTITLE_EXTENSIONS.contains(&bare_ext) {
        ext = ".srt".to_string();
    }
    if data.len() > MAX_SUBTITLE_BYTES {
        return Err("Subtitle exceeds 2 MB size limit.".to_string());
    }

    let text = match String::from_utf8(data.clone()) {
        Ok(s) => s,
        Err(_) => {
            // Latin-1 fallback converted to UTF-8
            data.iter().map(|&b| b as char).collect()
        }
    };
    if !text.contains("-->") {
        return Err(
            "Downloaded file does not look like subtitles (no timestamp '-->').".to_string(),
        );
    }

    Ok((text.into_bytes(), ext))
}

pub struct SubtitleService {
    pub download_dir: PathBuf,
    pub timeout_secs: f64,
}

/// OpenSubtitles.com file links (`/api/v1/download/{file_id}`) are not direct
/// downloads: they must be exchanged via POST + API key for a temporary link.
/// Any other URL passes through untouched (failure also falls back to it).
async fn resolve_oscom_link(client: &reqwest::Client, download_url: &str) -> String {
    if !download_url.contains("api.opensubtitles.com/api/v1/download/") {
        return download_url.to_string();
    }
    let api_key = std::env::var("OPENSUBTITLES_API_KEY").unwrap_or_default();
    if api_key.trim().is_empty() {
        return download_url.to_string();
    }
    let file_id: u64 = download_url
        .rsplit('/')
        .next()
        .and_then(|s| s.split(['?', '#']).next()?.parse().ok())
        .unwrap_or(0);
    if file_id == 0 {
        return download_url.to_string();
    }
    let Ok(resp) = client
        .post(download_url)
        .header("Api-Key", api_key.trim())
        .header("Accept", "application/json")
        .json(&serde_json::json!({"file_id": file_id}))
        .send()
        .await
    else {
        return download_url.to_string();
    };
    if !resp.status().is_success() {
        return download_url.to_string();
    }
    if let Ok(v) = resp.json::<serde_json::Value>().await {
        if let Some(link) = v.get("link").and_then(|l| l.as_str()) {
            if link.starts_with("http://") || link.starts_with("https://") {
                return link.to_string();
            }
        }
    }
    download_url.to_string()
}

impl SubtitleService {
    pub fn new(download_dir: impl AsRef<Path>, timeout_secs: f64) -> Self {
        Self {
            download_dir: download_dir.as_ref().to_path_buf(),
            timeout_secs,
        }
    }

    pub fn videos(&self, limit: usize) -> Vec<VideoEntry> {
        scan_videos(&self.download_dir, limit)
    }

    pub async fn search(&self, query: &str, limit: usize) -> Result<Value, String> {
        let q = query.trim();
        if q.is_empty() {
            return Err("Search query is required.".to_string());
        }
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(self.timeout_secs.max(5.0)))
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) evaTorrent/1.0")
            .build()
            .map_err(|e| e.to_string())?;

        let mut results: Vec<SubtitleResult> = Vec::new();
        results.extend(search_yify(&client, q, limit).await);
        let oscom_keyed = !std::env::var("OPENSUBTITLES_API_KEY")
            .unwrap_or_default()
            .trim()
            .is_empty();
        if oscom_keyed && results.len() < limit {
            results.extend(search_opensubtitles_com(&client, q, limit).await);
        }
        if results.len() < limit {
            results.extend(search_opensubtitles_org(&client, q, limit).await);
        }

        // Rank like the Python engine: most-downloaded first, then rating.
        results.sort_by(|a, b| {
            b.downloads.cmp(&a.downloads).then(
                b.rating
                    .partial_cmp(&a.rating)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
        });
        let mut seen = HashSet::new();
        let mut ranked = Vec::new();
        for r in results {
            if seen.insert(r.download_url.clone()) {
                ranked.push(r);
                if ranked.len() >= limit {
                    break;
                }
            }
        }

        let mut providers = vec!["yify", "opensubtitles_org"];
        if oscom_keyed {
            providers.push("opensubtitles_com");
        }
        Ok(json!({
            "query": q,
            "language": "en",
            "results": ranked,
            "providers_queried": providers,
            "errors": [],
        }))
    }

    pub async fn download_for_video(
        &self,
        video_rel: &str,
        download_url: &str,
        provider: &str,
        detail_url: &str,
    ) -> Result<Value, String> {
        if video_rel.contains("..") || Path::new(video_rel).is_absolute() {
            return Err("Video path escapes the download directory.".to_string());
        }
        let target_video = self.download_dir.join(video_rel);
        if !target_video.is_file() {
            return Err("Selected video not found in Downloads.".to_string());
        }
        let v_ext = target_video
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        if !VIDEO_EXTENSIONS.contains(&v_ext.as_str()) {
            return Err("Selected file is not a video.".to_string());
        }
        if !download_url.starts_with("http://") && !download_url.starts_with("https://") {
            return Err("Invalid subtitle download URL.".to_string());
        }

        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(self.timeout_secs.max(5.0)))
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) evaTorrent/1.0")
            .build()
            .map_err(|e| e.to_string())?;

        let effective_url = resolve_oscom_link(&client, download_url).await;
        let mut req = client.get(effective_url.clone());
        if detail_url.starts_with("http://") || detail_url.starts_with("https://") {
            req = req.header("Referer", detail_url);
        }
        let resp = req
            .send()
            .await
            .map_err(|e| format!("Subtitle download failed: {}", e))?;
        if !resp.status().is_success() {
            return Err(format!(
                "Subtitle download failed (HTTP {}).",
                resp.status()
            ));
        }
        let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
        let (content, ext) = extract_subtitle_from_bytes(&bytes, &effective_url)?;

        let stem = target_video
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("subtitle");
        let parent = target_video.parent().unwrap_or(&self.download_dir);
        let mut sibling = parent.join(format!("{}{}", stem, ext));
        if sibling.exists() {
            sibling = parent.join(format!("{}.en{}", stem, ext));
        }
        std::fs::write(&sibling, &content).map_err(|e| e.to_string())?;

        let rel_saved = sibling
            .strip_prefix(&self.download_dir)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| {
                sibling
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("subtitle.srt")
                    .to_string()
            });

        Ok(json!({
            "success": true,
            "provider": provider,
            "video": video_rel,
            "saved_as": rel_saved,
            "size_bytes": content.len(),
            "format": ext.trim_start_matches('.'),
        }))
    }
}
