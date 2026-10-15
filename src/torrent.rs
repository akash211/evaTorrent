//! Torrent metainfo (`.torrent`) and Magnet link parser (BEP 0003 & BEP 0009).

use std::path::{Path, PathBuf};
use std::time::Duration;

use futures::stream::{FuturesUnordered, StreamExt};
use serde::{Deserialize, Serialize};
use sha1::{Digest, Sha1};
use thiserror::Error;
use tracing::{debug, info, warn};

use crate::bencoding::{bdecode, bencode, BencodeError};

#[derive(Error, Debug)]
pub enum TorrentError {
    #[error("Bencode error: {0}")]
    Bencode(#[from] BencodeError),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Invalid torrent metainfo: {0}")]
    InvalidMetainfo(String),
    #[error("Invalid magnet URI: {0}")]
    InvalidMagnet(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileInfo {
    pub path: String,
    pub length: u64,
    pub offset: u64,
}

#[derive(Debug, Clone)]
pub struct Torrent {
    pub raw_data: Vec<u8>,
    pub info_hash: [u8; 20],
    pub info_hash_hex: String,
    pub name: String,
    pub trackers: Vec<String>,
    pub files: Vec<FileInfo>,
    pub total_length: u64,
    pub is_multi_file: bool,
    pub piece_length: u64,
    pub piece_hashes: Vec<[u8; 20]>,
    pub piece_count: usize,
    pub comment: Option<String>,
    pub created_by: Option<String>,
}

impl Torrent {
    pub fn from_bytes(raw_data: Vec<u8>) -> Result<Self, TorrentError> {
        let meta = bdecode(&raw_data)?;
        let root = meta.as_dict().ok_or_else(|| {
            TorrentError::InvalidMetainfo(
                "Root element of .torrent must be a bencoded dictionary".into(),
            )
        })?;

        let raw_info = root.get(b"info".as_slice()).ok_or_else(|| {
            TorrentError::InvalidMetainfo("Metainfo dictionary missing 'info' key".into())
        })?;

        let info_dict = raw_info.as_dict().ok_or_else(|| {
            TorrentError::InvalidMetainfo("'info' value must be a dictionary".into())
        })?;

        let encoded_info = bencode(raw_info);
        let mut hasher = Sha1::new();
        hasher.update(&encoded_info);
        let digest = hasher.finalize();
        let mut info_hash = [0u8; 20];
        info_hash.copy_from_slice(&digest);
        let info_hash_hex = hex::encode(info_hash);

        // Parse trackers (announce + announce-list)
        let mut trackers = Vec::new();
        if let Some(primary) = root.get(b"announce".as_slice()).and_then(|v| v.as_str()) {
            let clean = primary.trim().to_string();
            if !clean.is_empty() {
                trackers.push(clean);
            }
        }
        if let Some(tiers) = root
            .get(b"announce-list".as_slice())
            .and_then(|v| v.as_list())
        {
            for tier in tiers {
                if let Some(list) = tier.as_list() {
                    for tr in list {
                        if let Some(url) = tr.as_str() {
                            let clean = url.trim().to_string();
                            if !clean.is_empty() && !trackers.contains(&clean) {
                                trackers.push(clean);
                            }
                        }
                    }
                }
            }
        }

        // Parse name and files
        let name = info_dict
            .get(b"name.utf-8".as_slice())
            .or_else(|| info_dict.get(b"name".as_slice()))
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "evaTorrent_download".to_string());

        let mut files = Vec::new();
        let (total_length, is_multi_file) =
            if let Some(file_list) = info_dict.get(b"files".as_slice()).and_then(|v| v.as_list()) {
                let mut current_offset = 0u64;
                for f_val in file_list {
                    let f_dict = f_val.as_dict().ok_or_else(|| {
                        TorrentError::InvalidMetainfo("Entry in 'files' must be a dict".into())
                    })?;
                    let length = f_dict
                        .get(b"length".as_slice())
                        .and_then(|v| v.as_int())
                        .ok_or_else(|| {
                            TorrentError::InvalidMetainfo("File entry missing 'length'".into())
                        })?
                        .max(0) as u64;

                    let path_list = f_dict
                        .get(b"path.utf-8".as_slice())
                        .or_else(|| f_dict.get(b"path".as_slice()))
                        .and_then(|v| v.as_list())
                        .ok_or_else(|| {
                            TorrentError::InvalidMetainfo("File entry missing 'path'".into())
                        })?;

                    let mut rel = PathBuf::from(&name);
                    for part in path_list {
                        if let Some(seg) = part.as_str() {
                            // Guard against path traversal components
                            if seg != ".." && seg != "." && !seg.is_empty() {
                                rel.push(seg);
                            }
                        }
                    }
                    let rel_str = rel.to_string_lossy().replace('\\', "/");
                    files.push(FileInfo {
                        path: rel_str,
                        length,
                        offset: current_offset,
                    });
                    current_offset += length;
                }
                (current_offset, true)
            } else {
                let length = info_dict
                    .get(b"length".as_slice())
                    .and_then(|v| v.as_int())
                    .ok_or_else(|| {
                        TorrentError::InvalidMetainfo("Single-file torrent missing 'length'".into())
                    })?
                    .max(0) as u64;
                files.push(FileInfo {
                    path: name.clone(),
                    length,
                    offset: 0,
                });
                (length, false)
            };

        let piece_length = info_dict
            .get(b"piece length".as_slice())
            .and_then(|v| v.as_int())
            .ok_or_else(|| TorrentError::InvalidMetainfo("Missing 'piece length'".into()))?
            as u64;
        if piece_length == 0 {
            return Err(TorrentError::InvalidMetainfo(
                "Piece length must be > 0".into(),
            ));
        }

        let raw_pieces = info_dict
            .get(b"pieces".as_slice())
            .and_then(|v| v.as_bytes())
            .ok_or_else(|| {
                TorrentError::InvalidMetainfo("Malformed 'pieces' field in info dictionary".into())
            })?;

        if raw_pieces.len() % 20 != 0 {
            return Err(TorrentError::InvalidMetainfo(
                "Malformed pieces field: length is not a multiple of 20".into(),
            ));
        }

        let piece_hashes: Vec<[u8; 20]> = raw_pieces
            .as_chunks::<20>()
            .0
            .iter()
            .map(|chunk| {
                let mut arr = [0u8; 20];
                arr.copy_from_slice(chunk);
                arr
            })
            .collect();
        let piece_count = piece_hashes.len();

        let expected_pieces = if total_length > 0 {
            total_length.div_ceil(piece_length) as usize
        } else {
            0
        };
        if piece_count != expected_pieces {
            return Err(TorrentError::InvalidMetainfo(format!(
                "Piece count mismatch: {} hashes found, but {} expected for total length {} with piece size {}",
                piece_count, expected_pieces, total_length, piece_length
            )));
        }

        let comment = root.get(b"comment".as_slice()).and_then(|v| v.as_str());
        let created_by = root.get(b"created by".as_slice()).and_then(|v| v.as_str());

        Ok(Self {
            raw_data,
            info_hash,
            info_hash_hex,
            name,
            trackers,
            files,
            total_length,
            is_multi_file,
            piece_length,
            piece_hashes,
            piece_count,
            comment,
            created_by,
        })
    }

    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, TorrentError> {
        let bytes = std::fs::read(path)?;
        Self::from_bytes(bytes)
    }

    pub fn piece_size(&self, piece_index: usize) -> Result<u64, TorrentError> {
        if piece_index >= self.piece_count {
            return Err(TorrentError::InvalidMetainfo(format!(
                "Piece index {} out of bounds (total {})",
                piece_index, self.piece_count
            )));
        }
        if piece_index == self.piece_count - 1 {
            let rem = self.total_length % self.piece_length;
            Ok(if rem != 0 { rem } else { self.piece_length })
        } else {
            Ok(self.piece_length)
        }
    }
}

/// Decodes an RFC 4648 Base32 string (case-insensitive) into bytes.
fn decode_base32(input: &str) -> Option<Vec<u8>> {
    let mut bits: u32 = 0;
    let mut bit_count: u32 = 0;
    let mut out = Vec::with_capacity(input.len() * 5 / 8);

    for ch in input.bytes() {
        if ch == b'=' {
            break;
        }
        let val: u32 = match ch.to_ascii_uppercase() {
            b'A'..=b'Z' => (ch.to_ascii_uppercase() - b'A') as u32,
            b'2'..=b'7' => (ch - b'2' + 26) as u32,
            _ => return None,
        };
        bits = (bits << 5) | val;
        bit_count += 5;
        if bit_count >= 8 {
            bit_count -= 8;
            out.push(((bits >> bit_count) & 0xFF) as u8);
        }
    }
    Some(out)
}

#[derive(Debug, Clone)]
pub struct Magnet {
    pub uri: String,
    pub info_hash: [u8; 20],
    pub info_hash_hex: String,
    pub name: Option<String>,
    pub trackers: Vec<String>,
}

impl Magnet {
    pub fn parse(uri: &str) -> Result<Self, TorrentError> {
        let trimmed = uri.trim();
        if !trimmed.starts_with("magnet:?") {
            return Err(TorrentError::InvalidMagnet(
                "Invalid magnet link: must start with 'magnet:?'".into(),
            ));
        }

        let query = &trimmed[8..];
        let mut info_hash_opt: Option<[u8; 20]> = None;
        let mut name: Option<String> = None;
        let mut trackers: Vec<String> = Vec::new();

        for pair in query.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (k, v) = match pair.split_once('=') {
                Some((k, v)) => (k, v),
                None => continue,
            };
            let decoded_v = urlencoding_decode(&v.replace('+', " "));
            match k {
                "xt" | "xt.1" => {
                    if let Some(raw_hash) = decoded_v.strip_prefix("urn:btih:") {
                        if raw_hash.len() == 40 {
                            if let Ok(bytes) = hex::decode(raw_hash) {
                                if bytes.len() == 20 {
                                    let mut arr = [0u8; 20];
                                    arr.copy_from_slice(&bytes);
                                    info_hash_opt = Some(arr);
                                }
                            }
                        } else if raw_hash.len() == 32 {
                            if let Some(bytes) = decode_base32(raw_hash) {
                                if bytes.len() == 20 {
                                    let mut arr = [0u8; 20];
                                    arr.copy_from_slice(&bytes);
                                    info_hash_opt = Some(arr);
                                }
                            }
                        }
                    }
                }
                "dn" => {
                    if name.is_none() && !decoded_v.is_empty() {
                        name = Some(decoded_v);
                    }
                }
                "tr" if !decoded_v.is_empty() && !trackers.contains(&decoded_v) => {
                    trackers.push(decoded_v);
                }
                _ => {}
            }
        }

        let info_hash = info_hash_opt.ok_or_else(|| {
            TorrentError::InvalidMagnet("Magnet link does not contain a valid urn:btih hash".into())
        })?;
        let info_hash_hex = hex::encode(info_hash);

        Ok(Self {
            uri: trimmed.to_string(),
            info_hash,
            info_hash_hex,
            name,
            trackers,
        })
    }
}

pub fn urlencoding_decode(input: &str) -> String {
    let mut out = Vec::with_capacity(input.len());
    let bytes = input.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(hex_str) = std::str::from_utf8(&bytes[i + 1..i + 3]) {
                if let Ok(val) = u8::from_str_radix(hex_str, 16) {
                    out.push(val);
                    i += 3;
                    continue;
                }
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub fn urlencoding_encode(input: &str) -> String {
    urlencoding_encode_bytes(input.as_bytes())
}

pub fn urlencoding_encode_bytes(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 3);
    for &b in bytes {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => {
                out.push_str(&format!("%{:02X}", b));
            }
        }
    }
    out
}

/// Fetches `.torrent` file bytes concurrently from public torrent cache mirrors.
pub async fn fetch_torrent_from_caches(info_hash_hex: &str, timeout_secs: f64) -> Option<Vec<u8>> {
    let clean_hash = info_hash_hex.trim().to_uppercase();
    let cache_urls = [
        format!("https://itorrents.net/torrent/{}.torrent", clean_hash),
        format!("http://itorrents.net/torrent/{}.torrent", clean_hash),
        format!("https://itorrents.org/torrent/{}.torrent", clean_hash),
        format!(
            "https://cache.torrentstorage.com/gettorrent.php?h={}",
            clean_hash
        ),
    ];

    info!(
        "[CACHE] Resolving metainfo for info_hash={}... across {} mirrors",
        &clean_hash[..clean_hash.len().min(8)],
        cache_urls.len()
    );

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout_secs.max(1.0)))
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        .danger_accept_invalid_certs(true)
        .build()
        .ok()?;

    let mut futures = FuturesUnordered::new();
    for url in cache_urls {
        let client_clone = client.clone();
        futures.push(async move {
            match client_clone
                .get(&url)
                .header("Accept", "application/x-bittorrent, */*")
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    if let Ok(bytes) = resp.bytes().await {
                        if bytes.len() > 100 {
                            if let Ok(decoded) = bdecode(&bytes) {
                                if decoded.dict_get(b"info").is_some() {
                                    info!(
                                        "[CACHE SUCCESS] Found valid .torrent ({} bytes) via {}",
                                        bytes.len(),
                                        url
                                    );
                                    return Some(bytes.to_vec());
                                }
                            }
                        }
                    }
                }
                Ok(resp) if resp.status().as_u16() == 451 => {
                    warn!(
                        "[CACHE BLOCKED] Mirror {} returned HTTP 451 (ISP/network block)",
                        url
                    );
                }
                Ok(resp) => {
                    debug!("[CACHE] Mirror {} returned HTTP {}", url, resp.status());
                }
                Err(e) => {
                    debug!("[CACHE ERROR] Mirror {} connection failed: {}", url, e);
                }
            }
            None
        });
    }

    while let Some(res) = futures.next().await {
        if let Some(valid_bytes) = res {
            return Some(valid_bytes);
        }
    }

    warn!(
        "[CACHE EXHAUSTED] Could not retrieve .torrent metainfo for {} from any cache mirror",
        &clean_hash[..clean_hash.len().min(8)]
    );
    None
}
