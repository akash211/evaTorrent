import tempfile
import time
from pathlib import Path
import pytest

from evatorrent.db.database import Database
from evatorrent.auth import OTPManager


def test_sqlite_database_init_and_otp():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "eva.db"
        db = Database(db_path)
        assert db_path.exists()

        om = OTPManager(expiry_seconds=60, db=db)
        success, msg, otp = om.generate_otp("admin@example.com")
        assert success is True
        assert len(otp) == 6

        # Record exists in sqlite
        rec = db.get_otp_record("admin@example.com")
        assert rec is not None
        assert rec["otp"] == otp

        # Verification
        assert om.verify_otp("admin@example.com", "999999") is False
        assert om.verify_otp("admin@example.com", otp) is True

        # Once verified, it is deleted from DB
        assert db.get_otp_record("admin@example.com") is None


def test_sqlite_otp_lockout():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eva.db")
        om = OTPManager(expiry_seconds=60, db=db)
        success, _, otp = om.generate_otp("lockout@example.com")
        assert success is True

        # 5 failed attempts triggers lockout
        for _ in range(5):
            om.verify_otp("lockout@example.com", "000000")

        rec = db.get_otp_record("lockout@example.com")
        assert rec["failed_attempts"] >= 5
        assert rec["locked_until"] > time.time()

        # New OTP generation is rejected while locked
        success2, msg2, _ = om.generate_otp("lockout@example.com")
        assert success2 is False
        assert "Locked" in msg2


def test_torrents_history_and_events():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eva.db")

        # 1. Upsert torrent
        info_hash = "abcdef1234567890abcdef1234567890abcdef12"
        db.upsert_torrent(
            info_hash=info_hash,
            name="Ubuntu ISO",
            total_size=2000000,
            download_dir="/tmp/dl",
            status="downloading",
        )

        history = db.get_all_history()
        assert len(history) == 1
        assert history[0]["name"] == "Ubuntu ISO"
        assert history[0]["status"] == "downloading"

        # 2. Update progress
        db.update_torrent_progress(info_hash, downloaded_bytes=1000000, uploaded_bytes=50000)
        rec = db.get_torrent_history(info_hash)
        assert rec["downloaded_bytes"] == 1000000
        assert rec["uploaded_bytes"] == 50000

        # 3. Mark completed
        db.mark_torrent_completed(info_hash, downloaded_bytes=2000000, uploaded_bytes=100000)
        rec = db.get_torrent_history(info_hash)
        assert rec["status"] == "completed"
        assert rec["completed_at"] is not None

        # 4. Mark removed (record remains!)
        db.mark_torrent_removed(info_hash, deleted_files=False, downloaded_bytes=2000000, uploaded_bytes=100000)
        all_recs = db.get_all_history(status_filter="all")
        assert len(all_recs) == 1
        assert all_recs[0]["status"] == "removed"
        assert all_recs[0]["removed_at"] is not None

        # 5. Check events
        events = db.get_torrent_events(info_hash)
        assert len(events) >= 3  # ADDED, COMPLETED, REMOVED
        event_types = [e["event_type"] for e in events]
        assert "ADDED" in event_types
        assert "COMPLETED" in event_types
        assert "REMOVED" in event_types

        # 6. Analytics summary
        summary = db.get_analytics_summary()
        assert summary["total_torrents"] == 1
        assert summary["removed_torrents"] == 1
        assert summary["total_downloaded_bytes"] == 2000000
        assert summary["total_uploaded_bytes"] == 100000
