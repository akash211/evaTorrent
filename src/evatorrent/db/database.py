"""SQLite persistence layer for evaTorrent.

Persists:
1. One-Time Passwords (OTPs) and lockout protection across server restarts.
2. Complete historical archive of all torrents (even after removal) for analysis.
3. Event logs tracking every lifecycle event (ADDED, COMPLETED, PAUSED, RESUMED, ERROR, REMOVED).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("evatorrent.db")


class Database:
    """Thread-safe SQLite database manager for evaTorrent."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_schema(self) -> None:
        """Initializes database tables and indexes."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS otps (
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
                    download_dir TEXT
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
                """
            )

    # -------------------------------------------------------------------------
    # OTP Storage
    # -------------------------------------------------------------------------

    def get_otp_record(self, email: str) -> Optional[Dict[str, Any]]:
        clean_email = email.strip().lower()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM otps WHERE email = ?",
                (clean_email,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def save_otp(
        self,
        email: str,
        otp: str,
        expires_at: float,
        last_requested: float,
    ) -> None:
        clean_email = email.strip().lower()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO otps (email, otp, expires_at, last_requested, failed_attempts, locked_until)
                VALUES (?, ?, ?, ?, 0, 0)
                ON CONFLICT(email) DO UPDATE SET
                    otp = excluded.otp,
                    expires_at = excluded.expires_at,
                    last_requested = excluded.last_requested,
                    failed_attempts = 0,
                    locked_until = 0
                """,
                (clean_email, otp, expires_at, last_requested),
            )

    def record_otp_failure(self, email: str, max_attempts: int, lockout_seconds: float) -> Tuple[int, float]:
        """Increments failed attempts; sets lockout if max reached. Returns (failed_attempts, locked_until)."""
        clean_email = email.strip().lower()
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT failed_attempts FROM otps WHERE email = ?",
                (clean_email,),
            )
            row = cursor.fetchone()
            if not row:
                return 0, 0.0

            attempts = row["failed_attempts"] + 1
            locked_until = 0.0
            if attempts >= max_attempts:
                locked_until = now + lockout_seconds

            conn.execute(
                "UPDATE otps SET failed_attempts = ?, locked_until = ? WHERE email = ?",
                (attempts, locked_until, clean_email),
            )
            return attempts, locked_until

    def delete_otp(self, email: str) -> None:
        clean_email = email.strip().lower()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM otps WHERE email = ?", (clean_email,))

    # -------------------------------------------------------------------------
    # Torrent History & Analytics Persistence
    # -------------------------------------------------------------------------

    def upsert_torrent(
        self,
        info_hash: str,
        name: str,
        total_size: int,
        download_dir: str,
        status: str = "downloading",
    ) -> None:
        """Records addition of a torrent or updates its status on resume."""
        now = time.time()
        info_hash_lower = info_hash.lower()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO torrents_history (
                    info_hash, name, total_size, downloaded_bytes, uploaded_bytes,
                    status, added_at, completed_at, removed_at, error_message, download_dir
                )
                VALUES (?, ?, ?, 0, 0, ?, ?, NULL, NULL, NULL, ?)
                ON CONFLICT(info_hash) DO UPDATE SET
                    status = excluded.status,
                    error_message = NULL,
                    removed_at = NULL
                """,
                (info_hash_lower, name, total_size, status, now, download_dir),
            )
            conn.execute(
                """
                INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                VALUES (?, ?, 'ADDED', ?)
                """,
                (info_hash_lower, now, f"Torrent added: {name} ({total_size} bytes)"),
            )

    def update_torrent_progress(
        self,
        info_hash: str,
        downloaded_bytes: int,
        uploaded_bytes: int,
        status: Optional[str] = None,
    ) -> None:
        info_hash_lower = info_hash.lower()
        with self._get_connection() as conn:
            if status:
                conn.execute(
                    """
                    UPDATE torrents_history
                    SET downloaded_bytes = ?, uploaded_bytes = ?, status = ?
                    WHERE info_hash = ?
                    """,
                    (downloaded_bytes, uploaded_bytes, status, info_hash_lower),
                )
            else:
                conn.execute(
                    """
                    UPDATE torrents_history
                    SET downloaded_bytes = ?, uploaded_bytes = ?
                    WHERE info_hash = ?
                    """,
                    (downloaded_bytes, uploaded_bytes, info_hash_lower),
                )

    def mark_torrent_completed(
        self,
        info_hash: str,
        downloaded_bytes: int,
        uploaded_bytes: int,
    ) -> None:
        now = time.time()
        info_hash_lower = info_hash.lower()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE torrents_history
                SET status = 'completed',
                    completed_at = ?,
                    downloaded_bytes = ?,
                    uploaded_bytes = ?
                WHERE info_hash = ?
                """,
                (now, downloaded_bytes, uploaded_bytes, info_hash_lower),
            )
            conn.execute(
                """
                INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                VALUES (?, ?, 'COMPLETED', ?)
                """,
                (info_hash_lower, now, f"Download completed. Total uploaded: {uploaded_bytes} bytes"),
            )

    def mark_torrent_error(self, info_hash: str, error_message: str) -> None:
        now = time.time()
        info_hash_lower = info_hash.lower()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE torrents_history
                SET status = 'error',
                    error_message = ?
                WHERE info_hash = ?
                """,
                (error_message, info_hash_lower),
            )
            conn.execute(
                """
                INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                VALUES (?, ?, 'ERROR', ?)
                """,
                (info_hash_lower, now, f"Error occurred: {error_message}"),
            )

    def mark_torrent_removed(
        self,
        info_hash: str,
        deleted_files: bool = False,
        downloaded_bytes: Optional[int] = None,
        uploaded_bytes: Optional[int] = None,
    ) -> None:
        """Never deletes historical row! Updates status to 'removed' and sets removed_at timestamp."""
        now = time.time()
        info_hash_lower = info_hash.lower()
        with self._get_connection() as conn:
            if downloaded_bytes is not None and uploaded_bytes is not None:
                conn.execute(
                    """
                    UPDATE torrents_history
                    SET status = 'removed',
                        removed_at = ?,
                        downloaded_bytes = ?,
                        uploaded_bytes = ?
                    WHERE info_hash = ?
                    """,
                    (now, downloaded_bytes, uploaded_bytes, info_hash_lower),
                )
            else:
                conn.execute(
                    """
                    UPDATE torrents_history
                    SET status = 'removed',
                        removed_at = ?
                    WHERE info_hash = ?
                    """,
                    (now, info_hash_lower),
                )

            detail = f"Torrent removed from engine (files_deleted={deleted_files})"
            conn.execute(
                """
                INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                VALUES (?, ?, 'REMOVED', ?)
                """,
                (info_hash_lower, now, detail),
            )

    def log_event(self, info_hash: str, event_type: str, details: str) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO torrent_events (info_hash, timestamp, event_type, details)
                VALUES (?, ?, ?, ?)
                """,
                (info_hash.lower(), now, event_type, details),
            )

    def get_all_history(
        self,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM torrents_history WHERE 1=1"
        params: List[Any] = []

        if status_filter and status_filter.lower() != "all":
            query += " AND LOWER(status) = ?"
            params.append(status_filter.lower())

        if search and search.strip():
            query += " AND (name LIKE ? OR info_hash LIKE ?)"
            term = f"%{search.strip()}%"
            params.extend([term, term])

        query += " ORDER BY added_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_torrent_history(self, info_hash: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM torrents_history WHERE info_hash = ?",
                (info_hash.lower(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_torrent_events(self, info_hash: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM torrent_events WHERE info_hash = ? ORDER BY timestamp DESC LIMIT ?",
                (info_hash.lower(), limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_analytics_summary(self) -> Dict[str, Any]:
        """Calculates global lifetime statistics across all tracked torrents."""
        with self._get_connection() as conn:
            total_count = conn.execute("SELECT COUNT(*) FROM torrents_history").fetchone()[0]
            completed_count = conn.execute("SELECT COUNT(*) FROM torrents_history WHERE status = 'completed'").fetchone()[0]
            removed_count = conn.execute("SELECT COUNT(*) FROM torrents_history WHERE status = 'removed'").fetchone()[0]
            error_count = conn.execute("SELECT COUNT(*) FROM torrents_history WHERE status = 'error'").fetchone()[0]

            sum_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(total_size), 0) as total_size,
                    COALESCE(SUM(downloaded_bytes), 0) as total_downloaded,
                    COALESCE(SUM(uploaded_bytes), 0) as total_uploaded
                FROM torrents_history
                """
            ).fetchone()

            avg_completion_time_row = conn.execute(
                """
                SELECT AVG(completed_at - added_at)
                FROM torrents_history
                WHERE completed_at IS NOT NULL AND completed_at >= added_at
                """
            ).fetchone()

            avg_time = avg_completion_time_row[0] if avg_completion_time_row and avg_completion_time_row[0] else 0.0

            success_rate = (completed_count / total_count * 100.0) if total_count > 0 else 0.0

            return {
                "total_torrents": total_count,
                "completed_torrents": completed_count,
                "removed_torrents": removed_count,
                "error_torrents": error_count,
                "total_size": sum_row["total_size"],
                "total_downloaded_bytes": sum_row["total_downloaded"],
                "total_uploaded_bytes": sum_row["total_uploaded"],
                "success_rate": round(success_rate, 1),
                "avg_completion_time_seconds": round(avg_time, 1),
            }
