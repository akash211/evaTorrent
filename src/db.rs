//! Thread-safe SQLite persistence layer (`eva.db`) compatible with existing evaTorrent databases.
//!
//! Persists:
//! 1. One-Time Passwords (OTPs) and brute-force lockout counters across restarts.
//! 2. Complete lifetime archive of all torrents (`torrents_history`) and file selection/priorities.
//! 3. Lifecycle audit trail (`torrent_events`: ADDED, COMPLETED, PAUSED, RESUMED, ERROR, REMOVED).
//! 4. Global key-value settings (`kv_settings`: speed profiles, schedules, overrides).
//! 5. Personal Discover library (`discover_saved`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tracing::warn;

pub fn now_unix_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OtpRecord {
    pub email: String,
    pub otp: String,
    pub expires_at: f64,
    pub last_requested: f64,
    pub failed_attempts: i64,
    pub locked_until: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TorrentHistoryRow {
    pub info_hash: String,
    pub name: String,
    pub total_size: u64,
    pub downloaded_bytes: u64,
    pub uploaded_bytes: u64,
    pub status: String,
    pub added_at: f64,
    pub completed_at: Option<f64>,
    pub removed_at: Option<f64>,
    pub error_message: Option<String>,
    pub download_dir: Option<String>,
    pub magnet_uri: Option<String>,
    pub file_selection: Option<String>,
    pub file_priorities: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TorrentEventRow {
    pub id: i64,
    pub info_hash: String,
    pub timestamp: f64,
    pub event_type: String,
    pub details: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalyticsSummary {
    pub total_torrents: i64,
    pub completed_torrents: i64,
    pub removed_torrents: i64,
    pub error_torrents: i64,
    pub total_size: u64,
    pub total_downloaded_bytes: u64,
    pub total_uploaded_bytes: u64,
    pub success_rate: f64,
    pub avg_completion_time_seconds: f64,
}

#[derive(Clone)]
pub struct Database {
    pub db_path: PathBuf,
    conn: Arc<Mutex<Connection>>,
}

impl Database {
    pub fn new(db_path: impl AsRef<Path>) -> anyhow::Result<Self> {
        let db_path = db_path.as_ref().to_path_buf();
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(&db_path)?;
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA busy_timeout = 5000;
             PRAGMA synchronous = NORMAL;",
        )?;

        let db = Self {
            db_path,
            conn: Arc::new(Mutex::new(conn)),
        };
        db.init_schema()?;
        Ok(db)
    }

    fn init_schema(&self) -> anyhow::Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS otps (
                email TEXT PRIMARY KEY,
                otp TEXT NOT NULL,
                expires_at REAL NOT NULL,
                last_requested REAL NOT NULL,
                failed_attempts INTEGER DEFAULT 0,
                locked_until REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS torrents_history (
                info_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                total_size INTEGER NOT NULL,
                downloaded_bytes INTEGER DEFAULT 0,
                uploaded_bytes INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                added_at REAL NOT NULL,
                completed_at REAL,
                removed_at REAL,
                error_message TEXT,
                download_dir TEXT,
                magnet_uri TEXT,
                file_selection TEXT,
                file_priorities TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_torrents_history_status ON torrents_history(status);
            CREATE INDEX IF NOT EXISTS idx_torrents_history_added_at ON torrents_history(added_at);

            CREATE TABLE IF NOT EXISTS torrent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info_hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                FOREIGN KEY (info_hash) REFERENCES torrents_history(info_hash) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_torrent_events_hash ON torrent_events(info_hash);
            CREATE INDEX IF NOT EXISTS idx_torrent_events_type ON torrent_events(event_type);

            CREATE TABLE IF NOT EXISTS kv_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discover_saved (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                item_json TEXT NOT NULL,
                remarks TEXT DEFAULT '',
                saved_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(media_type, title)
            );

            CREATE INDEX IF NOT EXISTS idx_discover_saved_type ON discover_saved(media_type);
            CREATE INDEX IF NOT EXISTS idx_discover_saved_saved_at ON discover_saved(saved_at);",
        )?;

        // Lightweight column migrations for older DBs
        for col in [
            "magnet_uri",
            "download_dir",
            "file_selection",
            "file_priorities",
        ] {
            let sql = format!("ALTER TABLE torrents_history ADD COLUMN {} TEXT", col);
            let _ = conn.execute(&sql, []);
        }

        Ok(())
    }

    // -------------------------------------------------------------------------
    // OTP Storage
    // -------------------------------------------------------------------------

    pub fn get_otp_record(&self, email: &str) -> Option<OtpRecord> {
        let clean = email.trim().to_lowercase();
        let conn = self.conn.lock().ok()?;
        conn.query_row(
            "SELECT email, otp, expires_at, last_requested, failed_attempts, locked_until
             FROM otps WHERE email = ?1",
            params![clean],
            |row| {
                Ok(OtpRecord {
                    email: row.get(0)?,
                    otp: row.get(1)?,
                    expires_at: row.get(2)?,
                    last_requested: row.get(3)?,
                    failed_attempts: row.get(4)?,
                    locked_until: row.get(5)?,
                })
            },
        )
        .optional()
        .ok()
        .flatten()
    }

    pub fn save_otp(&self, email: &str, otp: &str, expires_at: f64, last_requested: f64) {
        let clean = email.trim().to_lowercase();
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute(
                "INSERT INTO otps (email, otp, expires_at, last_requested, failed_attempts, locked_until)
                 VALUES (?1, ?2, ?3, ?4, 0, 0)
                 ON CONFLICT(email) DO UPDATE SET
                    otp = excluded.otp,
                    expires_at = excluded.expires_at,
                    last_requested = excluded.last_requested,
                    failed_attempts = 0,
                    locked_until = 0",
                params![clean, otp, expires_at, last_requested],
            );
        }
    }

    pub fn record_otp_failure(
        &self,
        email: &str,
        max_attempts: i64,
        lockout_seconds: f64,
    ) -> (i64, f64) {
        let clean = email.trim().to_lowercase();
        let now = now_unix_secs();
        let Ok(conn) = self.conn.lock() else {
            return (0, 0.0);
        };
        let current: Option<i64> = conn
            .query_row(
                "SELECT failed_attempts FROM otps WHERE email = ?1",
                params![clean],
                |r| r.get(0),
            )
            .optional()
            .ok()
            .flatten();

        let Some(prev) = current else {
            return (0, 0.0);
        };
        let attempts = prev + 1;
        let locked_until = if attempts >= max_attempts {
            now + lockout_seconds
        } else {
            0.0
        };
        let _ = conn.execute(
            "UPDATE otps SET failed_attempts = ?1, locked_until = ?2 WHERE email = ?3",
            params![attempts, locked_until, clean],
        );
        (attempts, locked_until)
    }

    pub fn delete_otp(&self, email: &str) {
        let clean = email.trim().to_lowercase();
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute("DELETE FROM otps WHERE email = ?1", params![clean]);
        }
    }

    // -------------------------------------------------------------------------
    // Torrent History & Analytics Persistence
    // -------------------------------------------------------------------------

    pub fn upsert_torrent(
        &self,
        info_hash: &str,
        name: &str,
        total_size: u64,
        download_dir: &str,
        status: &str,
        magnet_uri: Option<&str>,
    ) {
        let now = now_unix_secs();
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else { return };

        let existing_magnet: Option<String> = conn
            .query_row(
                "SELECT magnet_uri FROM torrents_history WHERE info_hash = ?1",
                params![h],
                |r| r.get(0),
            )
            .optional()
            .ok()
            .flatten();
        let final_magnet = magnet_uri.map(|s| s.to_string()).or(existing_magnet);

        let _ = conn.execute(
            "INSERT INTO torrents_history (
                info_hash, name, total_size, downloaded_bytes, uploaded_bytes,
                status, added_at, completed_at, removed_at, error_message, download_dir, magnet_uri
             )
             VALUES (?1, ?2, ?3, 0, 0, ?4, ?5, NULL, NULL, NULL, ?6, ?7)
             ON CONFLICT(info_hash) DO UPDATE SET
                status = excluded.status,
                error_message = NULL,
                removed_at = NULL,
                magnet_uri = COALESCE(excluded.magnet_uri, torrents_history.magnet_uri),
                download_dir = COALESCE(excluded.download_dir, torrents_history.download_dir)",
            params![
                h,
                name,
                total_size as i64,
                status,
                now,
                download_dir,
                final_magnet
            ],
        );

        let _ = conn.execute(
            "INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
             VALUES (?1, ?2, 'ADDED', ?3)",
            params![
                h,
                now,
                format!("Torrent added: {} ({} bytes)", name, total_size)
            ],
        );
    }

    pub fn update_torrent_progress(
        &self,
        info_hash: &str,
        downloaded_bytes: u64,
        uploaded_bytes: u64,
        status: Option<&str>,
    ) {
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else { return };
        if let Some(st) = status {
            let _ = conn.execute(
                "UPDATE torrents_history
                 SET downloaded_bytes = ?1, uploaded_bytes = ?2, status = ?3
                 WHERE info_hash = ?4",
                params![downloaded_bytes as i64, uploaded_bytes as i64, st, h],
            );
        } else {
            let _ = conn.execute(
                "UPDATE torrents_history
                 SET downloaded_bytes = ?1, uploaded_bytes = ?2
                 WHERE info_hash = ?3",
                params![downloaded_bytes as i64, uploaded_bytes as i64, h],
            );
        }
    }

    pub fn mark_torrent_completed(
        &self,
        info_hash: &str,
        downloaded_bytes: u64,
        uploaded_bytes: u64,
    ) {
        let now = now_unix_secs();
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else { return };
        let _ = conn.execute(
            "UPDATE torrents_history
             SET status = 'completed',
                 completed_at = ?1,
                 downloaded_bytes = ?2,
                 uploaded_bytes = ?3
             WHERE info_hash = ?4",
            params![now, downloaded_bytes as i64, uploaded_bytes as i64, h],
        );
        let _ = conn.execute(
            "INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
             VALUES (?1, ?2, 'COMPLETED', ?3)",
            params![
                h,
                now,
                format!(
                    "Download completed. Total uploaded: {} bytes",
                    uploaded_bytes
                )
            ],
        );
    }

    pub fn mark_torrent_error(&self, info_hash: &str, error_message: &str) {
        let now = now_unix_secs();
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else { return };
        let _ = conn.execute(
            "UPDATE torrents_history
             SET status = 'error', error_message = ?1
             WHERE info_hash = ?2",
            params![error_message, h],
        );
        let _ = conn.execute(
            "INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
             VALUES (?1, ?2, 'ERROR', ?3)",
            params![h, now, format!("Error occurred: {}", error_message)],
        );
    }

    pub fn mark_torrent_removed(
        &self,
        info_hash: &str,
        deleted_files: bool,
        downloaded_bytes: Option<u64>,
        uploaded_bytes: Option<u64>,
    ) {
        let now = now_unix_secs();
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else { return };
        if let (Some(dl), Some(ul)) = (downloaded_bytes, uploaded_bytes) {
            let _ = conn.execute(
                "UPDATE torrents_history
                 SET status = 'removed',
                     removed_at = ?1,
                     downloaded_bytes = ?2,
                     uploaded_bytes = ?3
                 WHERE info_hash = ?4",
                params![now, dl as i64, ul as i64, h],
            );
        } else {
            let _ = conn.execute(
                "UPDATE torrents_history
                 SET status = 'removed', removed_at = ?1
                 WHERE info_hash = ?2",
                params![now, h],
            );
        }
        let detail = format!(
            "Torrent removed from engine (files_deleted={})",
            deleted_files
        );
        let _ = conn.execute(
            "INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
             VALUES (?1, ?2, 'REMOVED', ?3)",
            params![h, now, detail],
        );
    }

    pub fn log_event(&self, info_hash: &str, event_type: &str, details: &str) {
        let now = now_unix_secs();
        let h = info_hash.to_lowercase();
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute(
                "INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                 VALUES (?1, ?2, ?3, ?4)",
                params![h, now, event_type, details],
            );
        }
    }

    fn map_history_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TorrentHistoryRow> {
        Ok(TorrentHistoryRow {
            info_hash: row.get(0)?,
            name: row.get(1)?,
            total_size: row.get::<_, i64>(2)?.max(0) as u64,
            downloaded_bytes: row.get::<_, Option<i64>>(3)?.unwrap_or(0).max(0) as u64,
            uploaded_bytes: row.get::<_, Option<i64>>(4)?.unwrap_or(0).max(0) as u64,
            status: row.get(5)?,
            added_at: row.get(6)?,
            completed_at: row.get(7)?,
            removed_at: row.get(8)?,
            error_message: row.get(9)?,
            download_dir: row.get(10)?,
            magnet_uri: row.get(11)?,
            file_selection: row.get(12).ok().flatten(),
            file_priorities: row.get(13).ok().flatten(),
        })
    }

    pub fn get_all_history(
        &self,
        status_filter: Option<&str>,
        search: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Vec<TorrentHistoryRow> {
        let Ok(conn) = self.conn.lock() else {
            return Vec::new();
        };

        let status_clean = status_filter
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty() && s != "all");
        let search_clean = search
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());

        let sql = "SELECT info_hash, name, total_size, downloaded_bytes, uploaded_bytes,
                          status, added_at, completed_at, removed_at, error_message,
                          download_dir, magnet_uri, file_selection, file_priorities
                   FROM torrents_history
                   WHERE (?1 IS NULL OR LOWER(status) = ?1)
                     AND (?2 IS NULL OR name LIKE ?2 OR info_hash LIKE ?2)
                   ORDER BY added_at DESC
                   LIMIT ?3 OFFSET ?4";

        let like_pattern = search_clean.map(|s| format!("%{}%", s));
        let Ok(mut stmt) = conn.prepare(sql) else {
            return Vec::new();
        };
        let Ok(rows) = stmt.query_map(
            params![status_clean, like_pattern, limit as i64, offset as i64],
            Self::map_history_row,
        ) else {
            return Vec::new();
        };

        rows.filter_map(Result::ok).collect()
    }

    pub fn get_torrent_history(&self, info_hash: &str) -> Option<TorrentHistoryRow> {
        let h = info_hash.to_lowercase();
        let conn = self.conn.lock().ok()?;
        conn.query_row(
            "SELECT info_hash, name, total_size, downloaded_bytes, uploaded_bytes,
                    status, added_at, completed_at, removed_at, error_message,
                    download_dir, magnet_uri, file_selection, file_priorities
             FROM torrents_history WHERE info_hash = ?1",
            params![h],
            Self::map_history_row,
        )
        .optional()
        .ok()
        .flatten()
    }

    pub fn get_resumable_torrents(&self, limit: usize) -> Vec<TorrentHistoryRow> {
        let Ok(conn) = self.conn.lock() else {
            return Vec::new();
        };
        let Ok(mut stmt) = conn.prepare(
            "SELECT info_hash, name, total_size, downloaded_bytes, uploaded_bytes,
                    status, added_at, completed_at, removed_at, error_message,
                    download_dir, magnet_uri, file_selection, file_priorities
             FROM torrents_history
             WHERE LOWER(status) IN ('downloading', 'pending', 'paused', 'queued', 'error', 'seeding')
             ORDER BY added_at ASC
             LIMIT ?1",
        ) else {
            return Vec::new();
        };
        let Ok(rows) = stmt.query_map(params![limit as i64], Self::map_history_row) else {
            return Vec::new();
        };
        rows.filter_map(Result::ok).collect()
    }

    pub fn get_torrent_events(&self, info_hash: &str, limit: usize) -> Vec<TorrentEventRow> {
        let h = info_hash.to_lowercase();
        let Ok(conn) = self.conn.lock() else {
            return Vec::new();
        };
        let Ok(mut stmt) = conn.prepare(
            "SELECT id, info_hash, timestamp, event_type, details
             FROM torrent_events WHERE info_hash = ?1
             ORDER BY timestamp DESC LIMIT ?2",
        ) else {
            return Vec::new();
        };
        let Ok(rows) = stmt.query_map(params![h, limit as i64], |r| {
            Ok(TorrentEventRow {
                id: r.get(0)?,
                info_hash: r.get(1)?,
                timestamp: r.get(2)?,
                event_type: r.get(3)?,
                details: r.get(4)?,
            })
        }) else {
            return Vec::new();
        };
        rows.filter_map(Result::ok).collect()
    }

    pub fn get_analytics_summary(&self) -> AnalyticsSummary {
        let Ok(conn) = self.conn.lock() else {
            return AnalyticsSummary {
                total_torrents: 0,
                completed_torrents: 0,
                removed_torrents: 0,
                error_torrents: 0,
                total_size: 0,
                total_downloaded_bytes: 0,
                total_uploaded_bytes: 0,
                success_rate: 0.0,
                avg_completion_time_seconds: 0.0,
            };
        };

        let count_query = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get(0)).unwrap_or(0) };

        let total_count = count_query("SELECT COUNT(*) FROM torrents_history");
        let completed_count =
            count_query("SELECT COUNT(*) FROM torrents_history WHERE status = 'completed'");
        let removed_count =
            count_query("SELECT COUNT(*) FROM torrents_history WHERE status = 'removed'");
        let error_count =
            count_query("SELECT COUNT(*) FROM torrents_history WHERE status = 'error'");

        let (total_size, total_dl, total_ul): (i64, i64, i64) = conn
            .query_row(
                "SELECT COALESCE(SUM(total_size), 0),
                        COALESCE(SUM(downloaded_bytes), 0),
                        COALESCE(SUM(uploaded_bytes), 0)
                 FROM torrents_history",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap_or((0, 0, 0));

        let avg_time: f64 = conn
            .query_row(
                "SELECT COALESCE(AVG(completed_at - added_at), 0.0)
                 FROM torrents_history
                 WHERE completed_at IS NOT NULL AND completed_at >= added_at",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0.0);

        let success_rate = if total_count > 0 {
            ((completed_count as f64 / total_count as f64) * 1000.0).round() / 10.0
        } else {
            0.0
        };

        AnalyticsSummary {
            total_torrents: total_count,
            completed_torrents: completed_count,
            removed_torrents: removed_count,
            error_torrents: error_count,
            total_size: total_size.max(0) as u64,
            total_downloaded_bytes: total_dl.max(0) as u64,
            total_uploaded_bytes: total_ul.max(0) as u64,
            success_rate,
            avg_completion_time_seconds: (avg_time * 10.0).round() / 10.0,
        }
    }

    // -------------------------------------------------------------------------
    // Generic KV Settings & File Priorities
    // -------------------------------------------------------------------------

    pub fn get_setting(&self, key: &str) -> Option<String> {
        let conn = self.conn.lock().ok()?;
        conn.query_row(
            "SELECT value FROM kv_settings WHERE key = ?1",
            params![key],
            |r| r.get(0),
        )
        .optional()
        .ok()
        .flatten()
    }

    pub fn set_setting(&self, key: &str, value: &str) {
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute(
                "INSERT INTO kv_settings (key, value) VALUES (?1, ?2)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                params![key, value],
            );
        }
    }

    pub fn set_file_selection(&self, info_hash: &str, selected_files: Option<&[String]>) {
        let payload = selected_files.and_then(|list| {
            let mut sorted = list.to_vec();
            sorted.sort();
            serde_json::to_string(&sorted).ok()
        });
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute(
                "UPDATE torrents_history SET file_selection = ?1 WHERE info_hash = ?2",
                params![payload, info_hash.to_lowercase()],
            );
        }
    }

    pub fn get_file_selection(&self, info_hash: &str) -> Option<Vec<String>> {
        let conn = self.conn.lock().ok()?;
        let raw: Option<String> = conn
            .query_row(
                "SELECT file_selection FROM torrents_history WHERE info_hash = ?1",
                params![info_hash.to_lowercase()],
                |r| r.get(0),
            )
            .optional()
            .ok()
            .flatten()?;
        serde_json::from_str::<Vec<String>>(&raw?).ok()
    }

    pub fn set_file_priorities(&self, info_hash: &str, priorities: Option<&HashMap<String, u8>>) {
        let payload = priorities
            .filter(|m| !m.is_empty())
            .and_then(|m| serde_json::to_string(m).ok());
        if let Ok(conn) = self.conn.lock() {
            let _ = conn.execute(
                "UPDATE torrents_history SET file_priorities = ?1 WHERE info_hash = ?2",
                params![payload, info_hash.to_lowercase()],
            );
        }
    }

    pub fn get_file_priorities(&self, info_hash: &str) -> Option<HashMap<String, u8>> {
        let conn = self.conn.lock().ok()?;
        let raw: Option<String> = conn
            .query_row(
                "SELECT file_priorities FROM torrents_history WHERE info_hash = ?1",
                params![info_hash.to_lowercase()],
                |r| r.get(0),
            )
            .optional()
            .ok()
            .flatten()?;
        serde_json::from_str::<HashMap<String, u8>>(&raw?).ok()
    }

    pub fn get_effective_file_priorities(
        &self,
        info_hash: &str,
        all_paths: &[String],
    ) -> Option<HashMap<String, u8>> {
        if let Some(stored) = self.get_file_priorities(info_hash) {
            if !stored.is_empty() {
                let mut map = HashMap::with_capacity(all_paths.len());
                for p in all_paths {
                    map.insert(p.clone(), *stored.get(p).unwrap_or(&1));
                }
                return Some(map);
            }
        }
        if let Some(selected) = self.get_file_selection(info_hash) {
            let mut map = HashMap::with_capacity(all_paths.len());
            for p in all_paths {
                map.insert(p.clone(), if selected.contains(p) { 1 } else { 0 });
            }
            return Some(map);
        }
        None
    }

    // -------------------------------------------------------------------------
    // Discover Saved Library
    // -------------------------------------------------------------------------

    pub fn save_discover_item(&self, item: &Value, remarks: &str) -> Value {
        let now = now_unix_secs();
        let media_type = item
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("info")
            .to_lowercase();
        let title = item
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("Untitled")
            .to_string();
        let year = item.get("year").and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse::<i64>().ok()))
        });
        let blob = serde_json::to_string(item).unwrap_or_else(|_| "{}".to_string());

        let Ok(conn) = self.conn.lock() else {
            return json!({});
        };

        let existing: Option<(i64, String)> = conn
            .query_row(
                "SELECT id, COALESCE(remarks, '') FROM discover_saved WHERE media_type = ?1 AND title = ?2",
                params![media_type, title],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .ok()
            .flatten();

        let saved_id = if let Some((id, old_remarks)) = existing {
            let keep_remarks = if !remarks.is_empty() {
                remarks
            } else {
                &old_remarks
            };
            let _ = conn.execute(
                "UPDATE discover_saved
                 SET year = ?1, item_json = ?2, remarks = ?3, updated_at = ?4
                 WHERE id = ?5",
                params![year, blob, keep_remarks, now, id],
            );
            id
        } else {
            let _ = conn.execute(
                "INSERT INTO discover_saved (media_type, title, year, item_json, remarks, saved_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![media_type, title, year, blob, remarks, now, now],
            );
            conn.last_insert_rowid()
        };

        Self::get_saved_by_id(&conn, saved_id).unwrap_or_else(|| json!({}))
    }

    fn get_saved_by_id(conn: &Connection, id: i64) -> Option<Value> {
        conn.query_row(
            "SELECT id, media_type, title, year, item_json, remarks, saved_at, updated_at
             FROM discover_saved WHERE id = ?1",
            params![id],
            |r| {
                Ok(json!({
                    "id": r.get::<_, i64>(0)?,
                    "media_type": r.get::<_, String>(1)?,
                    "title": r.get::<_, String>(2)?,
                    "year": r.get::<_, Option<i64>>(3)?,
                    "item_json": r.get::<_, String>(4)?,
                    "remarks": r.get::<_, Option<String>>(5)?.unwrap_or_default(),
                    "saved_at": r.get::<_, f64>(6)?,
                    "updated_at": r.get::<_, f64>(7)?,
                }))
            },
        )
        .optional()
        .ok()
        .flatten()
    }

    pub fn get_saved_discover(
        &self,
        media_type: Option<&str>,
        search: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Vec<Value> {
        let Ok(conn) = self.conn.lock() else {
            return Vec::new();
        };
        let mt = media_type
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty() && s != "all");
        let like_q = search
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .map(|s| format!("%{}%", s));

        let Ok(mut stmt) = conn.prepare(
            "SELECT id, media_type, title, year, item_json, remarks, saved_at, updated_at
             FROM discover_saved
             WHERE (?1 IS NULL OR media_type = ?1)
               AND (?2 IS NULL OR title LIKE ?2 OR remarks LIKE ?2)
             ORDER BY saved_at DESC
             LIMIT ?3 OFFSET ?4",
        ) else {
            return Vec::new();
        };

        let Ok(rows) = stmt.query_map(params![mt, like_q, limit as i64, offset as i64], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "media_type": r.get::<_, String>(1)?,
                "title": r.get::<_, String>(2)?,
                "year": r.get::<_, Option<i64>>(3)?,
                "item_json": r.get::<_, String>(4)?,
                "remarks": r.get::<_, Option<String>>(5)?.unwrap_or_default(),
                "saved_at": r.get::<_, f64>(6)?,
                "updated_at": r.get::<_, f64>(7)?,
            }))
        }) else {
            return Vec::new();
        };
        rows.filter_map(Result::ok).collect()
    }

    pub fn update_discover_remarks(&self, saved_id: i64, remarks: &str) -> Option<Value> {
        let conn = self.conn.lock().ok()?;
        let updated = conn
            .execute(
                "UPDATE discover_saved SET remarks = ?1, updated_at = ?2 WHERE id = ?3",
                params![remarks, now_unix_secs(), saved_id],
            )
            .ok()?;
        if updated == 0 {
            warn!("update_discover_remarks: id {} not found", saved_id);
            return None;
        }
        Self::get_saved_by_id(&conn, saved_id)
    }

    pub fn delete_saved_discover(&self, saved_id: i64) -> bool {
        let Ok(conn) = self.conn.lock() else {
            return false;
        };
        conn.execute(
            "DELETE FROM discover_saved WHERE id = ?1",
            params![saved_id],
        )
        .map(|n| n > 0)
        .unwrap_or(false)
    }
}
