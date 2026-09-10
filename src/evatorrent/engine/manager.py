"""Top-level EngineManager coordinating multiple TorrentSession instances."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from evatorrent.bencoding import bdecode
from evatorrent.db.database import Database
from evatorrent.engine.session import TorrentSession
from evatorrent.torrent import Magnet, Torrent, fetch_torrent_from_caches

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR") or (Path.home() / "Downloads" / "evaTorrent"))


class EngineManager:
    """Manages all active BitTorrent sessions in the evaTorrent engine."""

    def __init__(self, default_download_dir: Optional[Path] = None, db: Optional[Database] = None):
        self.download_dir = Path(default_download_dir or os.environ.get("DOWNLOAD_DIR") or DEFAULT_DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: Dict[str, TorrentSession] = {}
        self.db = db

    def _cache_path(self, info_hash_hex: str) -> Path:
        cache_dir = self.download_dir / ".torrent_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{info_hash_hex.lower()}.torrent"

    def _ensure_torrent_cached(self, info_hash_hex: str, raw_data: bytes) -> None:
        """Persists raw .torrent bytes so sessions can be rebuilt after restarts."""
        try:
            p = self._cache_path(info_hash_hex)
            if not p.exists():
                p.write_bytes(raw_data)
                logger.info(f"[ENGINE] Cached .torrent for {info_hash_hex[:8]} ({len(raw_data)} bytes)")
        except Exception as e:
            logger.warning(f"[ENGINE] Failed to cache .torrent for {info_hash_hex[:8]}: {e}")

    def add_torrent(
        self, torrent: Torrent, output_dir: Optional[Path] = None, magnet_uri: Optional[str] = None
    ) -> TorrentSession:
        info_hash_hex = torrent.info_hash_hex
        if info_hash_hex in self.sessions:
            return self.sessions[info_hash_hex]

        dest_dir = output_dir or self.download_dir
        session = TorrentSession(torrent=torrent, download_dir=dest_dir, db=self.db)
        self.sessions[info_hash_hex] = session

        # Always persist raw metainfo so a container recreate can rebuild this session.
        try:
            self._ensure_torrent_cached(info_hash_hex, torrent.raw_data)
        except Exception:
            pass

        if self.db:
            # Upsert before start (captures PENDING/COMPLETED from disk check)...
            self.db.upsert_torrent(
                info_hash=info_hash_hex,
                name=torrent.name,
                total_size=torrent.total_length,
                download_dir=str(dest_dir),
                status=session.status.value,
                magnet_uri=magnet_uri,
            )

        session.start()
        if self.db:
            # ...then immediately record the live state so a crash/recreate
            # seconds later still resumes (speed loop only persists every 5s).
            try:
                if session.piece_manager.is_complete:
                    self.db.mark_torrent_completed(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                    )
                else:
                    self.db.update_torrent_progress(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        session.status.value,
                    )
            except Exception:
                pass
        return session

    def add_torrent_bytes(self, data: bytes, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent(data)
        return self.add_torrent(torrent, output_dir)

    def add_torrent_file(self, filepath: Path, output_dir: Optional[Path] = None) -> TorrentSession:
        torrent = Torrent.from_file(filepath)
        return self.add_torrent(torrent, output_dir)

    async def add_magnet(self, magnet_uri: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Resolves magnet link metadata and enrolls the download in the engine."""
        magnet = Magnet(magnet_uri)
        info_hash_hex = magnet.info_hash_hex.lower()
        if info_hash_hex in self.sessions:
            logger.info(f"[ENGINE] Magnet {info_hash_hex[:8]} is already active in swarm sessions")
            return self.sessions[info_hash_hex]

        cache_dir = self.download_dir / ".torrent_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        torrent_file = cache_dir / f"{info_hash_hex}.torrent"

        raw_bytes: Optional[bytes] = None
        if torrent_file.exists():
            try:
                raw_bytes = torrent_file.read_bytes()
                logger.info(
                    f"[ENGINE] Loaded cached .torrent for {info_hash_hex[:8]} from disk ({len(raw_bytes)} bytes)"
                )
            except Exception as e:
                logger.warning(f"[ENGINE] Failed reading local cached .torrent for {info_hash_hex[:8]}: {e}")
                raw_bytes = None

        if not raw_bytes:
            logger.info(
                f"[ENGINE] Fetching metainfo for magnet '{magnet.name or info_hash_hex[:8]}' from public caches..."
            )
            raw_bytes = await fetch_torrent_from_caches(info_hash_hex)
            if raw_bytes:
                try:
                    torrent_file.write_bytes(raw_bytes)
                    logger.info(f"[ENGINE] Successfully saved .torrent for {info_hash_hex[:8]} to local cache")
                except Exception as e:
                    logger.warning(f"[ENGINE] Failed to cache torrent file: {e}")

        if not raw_bytes:
            name_hint = f' "{magnet.name}"' if magnet.name else ""
            err_msg = (
                f"Could not retrieve metadata for magnet{name_hint} ({info_hash_hex[:8]}...). "
                f"The torrent metainfo was not found in public torrent caches. Please upload the .torrent file directly."
            )
            logger.warning(f"[ENGINE] {err_msg}")
            raise ValueError(err_msg)

        torrent = Torrent(raw_bytes)
        for tr in magnet.trackers:
            if tr not in torrent.trackers:
                torrent.trackers.append(tr)

        logger.info(f"[ENGINE] Successfully enrolled magnet '{torrent.name}' ({len(torrent.trackers)} trackers)")
        return self.add_torrent(torrent, output_dir, magnet_uri=magnet_uri)

    async def add_url(self, url: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Downloads a .torrent file from an HTTP/HTTPS URL and enrolls it."""
        clean_url = url.strip()
        logger.info(f"[ENGINE] Fetching torrent from remote URL: {clean_url}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0, follow_redirects=True, verify=False) as client:
                resp = await client.get(clean_url)
                if resp.status_code != 200:
                    raise ValueError(f"HTTP request to torrent URL failed with status {resp.status_code}")

                content = resp.content
                # Check if it is a valid bencoded torrent
                is_bencoded = False
                try:
                    decoded = bdecode(content)
                    if isinstance(decoded, dict) and b"info" in decoded:
                        is_bencoded = True
                except Exception:
                    is_bencoded = False

                if is_bencoded:
                    logger.info(f"[ENGINE] URL returned valid .torrent ({len(content)} bytes)")
                    return self.add_torrent_bytes(content, output_dir)

                # If not direct torrent bytes, check if response text contains a magnet link
                text = resp.text
                magnet_match = re.search(r'magnet:\?[^"\'\s<>]+', text)
                if magnet_match:
                    logger.info("[ENGINE] URL response contained embedded magnet link. Enrolling magnet...")
                    return await self.add_magnet(magnet_match.group(0), output_dir)

                raise ValueError("URL did not return a valid .torrent file or magnet link.")
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"Failed to fetch torrent from URL: {e}")

    async def add_torrent_or_url(self, input_str: str, output_dir: Optional[Path] = None) -> TorrentSession:
        """Accepts a magnet link, HTTP/HTTPS URL, or file path and enrolls it."""
        logger.info(f"[ENGINE] add_torrent_or_url received input: {input_str[:60]}...")
        s = input_str.strip()
        if s.startswith("magnet:?"):
            return await self.add_magnet(s, output_dir)
        elif s.startswith("http://") or s.startswith("https://"):
            return await self.add_url(s, output_dir)
        elif len(s) == 40 and all(c in "0123456789abcdefABCDEF" for c in s):
            return await self.add_magnet(f"magnet:?xt=urn:btih:{s}", output_dir)
        else:
            path = Path(s)
            if path.is_file():
                return self.add_torrent_file(path, output_dir)
            raise ValueError(
                "Input must be a valid magnet link (magnet:?), Web URL (http/https), or .torrent file path."
            )

    def get_session(self, info_hash_hex: str) -> Optional[TorrentSession]:
        return self.sessions.get(info_hash_hex.lower())

    async def pause_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            await session.pause()
            if self.db:
                try:
                    self.db.update_torrent_progress(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        "paused",
                    )
                except Exception:
                    pass
                self.db.log_event(info_hash_hex, "PAUSED", "Torrent paused by user")
            return True
        return False

    def resume_torrent(self, info_hash_hex: str) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.resume()
            if self.db:
                try:
                    self.db.update_torrent_progress(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        session.status.value,
                    )
                except Exception:
                    pass
                self.db.log_event(info_hash_hex, "RESUMED", "Torrent resumed by user")
            return True
        return False

    async def resume_db_torrent(self, info_hash_hex: str) -> bool:
        """Resumes a DB-only torrent (Live Swarm fallback) by rebuilding its session.

        Returns True if a live session now exists (either pre-existing or rebuilt).
        """
        key = info_hash_hex.lower()
        if key in self.sessions:
            self.sessions[key].resume()
            return True
        if not self.db:
            return False
        row = self.db.get_torrent_history(key)
        if not row:
            return False
        if str(row.get("status", "")).lower() in ("completed", "removed"):
            return False
        rebuilt = await self._rebuild_session_from_row(row)
        if rebuilt is not None:
            # _rebuild already started the session (or paused it); ensure downloading unless paused.
            if str(row.get("status", "")).lower() != "paused":
                rebuilt.resume()
            if self.db:
                self.db.log_event(key, "RESUMED", "Torrent resumed from database fallback")
            return True
        return False

    async def _rebuild_session_from_row(self, row: dict) -> Optional[TorrentSession]:
        """Rebuilds a TorrentSession from a DB row + cached .torrent file."""
        info_hash_hex = str(row.get("info_hash", "")).lower()
        if not info_hash_hex or info_hash_hex in self.sessions:
            return self.sessions.get(info_hash_hex)
        raw_bytes: Optional[bytes] = None
        cache_file = self._cache_path(info_hash_hex)
        if cache_file.exists():
            try:
                raw_bytes = cache_file.read_bytes()
            except Exception as e:
                logger.warning(f"[ENGINE] Failed reading cached .torrent for {info_hash_hex[:8]}: {e}")
        if not raw_bytes and row.get("magnet_uri"):
            try:
                raw_bytes = await fetch_torrent_from_caches(info_hash_hex)
                if raw_bytes:
                    try:
                        cache_file.write_bytes(raw_bytes)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[ENGINE] Magnet re-resolve failed for {info_hash_hex[:8]}: {e}")
        if not raw_bytes:
            logger.warning(
                f"[ENGINE] Cannot auto-resume '{row.get('name')}' ({info_hash_hex[:8]}): "
                f"cached .torrent missing. Re-add the .torrent/magnet manually."
            )
            return None
        try:
            torrent = Torrent(raw_bytes)
        except Exception as e:
            logger.warning(f"[ENGINE] Cached metainfo corrupt for {info_hash_hex[:8]}: {e}")
            return None
        # Use the download dir recorded at add-time when it still exists.
        dest_dir = self.download_dir
        recorded = row.get("download_dir")
        if recorded:
            try:
                p = Path(recorded)
                # Only honour absolute persisted dirs; otherwise stay on current download_dir
                # so container path changes (/downloads vs ~/Downloads) keep working.
                if p.is_absolute():
                    p.mkdir(parents=True, exist_ok=True)
                    dest_dir = p
            except Exception:
                dest_dir = self.download_dir
        try:
            session = TorrentSession(torrent=torrent, download_dir=dest_dir, db=self.db)
        except Exception as e:
            logger.warning(f"[ENGINE] Failed to init session for {info_hash_hex[:8]}: {e}")
            return None
        # If files already complete on disk, mark completed and skip swarm.
        if session.piece_manager.is_complete:
            self.sessions[info_hash_hex] = session
            if self.db:
                self.db.mark_torrent_completed(
                    info_hash_hex,
                    session.piece_manager.bytes_downloaded,
                    session.piece_manager.bytes_uploaded,
                )
            logger.info(f"[ENGINE] '{torrent.name}' already complete on disk — marked completed, no swarm needed.")
            return session
        self.sessions[info_hash_hex] = session
        prev_status = str(row.get("status", "downloading")).lower()
        if prev_status == "paused":
            # Recreate in paused state so user intent survives restarts.
            session.status = session.status.__class__("paused")
            if self.db:
                self.db.log_event(
                    info_hash_hex, "RESUMED", "Session rebuilt from DB in paused state (auto-resume on boot)"
                )
        else:
            session.start()
            if self.db:
                # Refresh progress baseline so Analytics stops showing stale rows.
                try:
                    self.db.update_torrent_progress(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        "downloading",
                    )
                except Exception:
                    pass
                self.db.log_event(info_hash_hex, "RESUMED", "Session auto-resumed after restart")
        return session

    async def restore_from_db(self) -> dict:
        """Rebuilds interrupted sessions from SQLite. Called once at server boot.

        Returns summary counts: {restored, paused, completed, skipped}.
        Never raises — per-torrent failures are logged and skipped so one bad
        row cannot prevent the rest of the swarm from resuming.
        """
        summary = {"restored": 0, "paused": 0, "completed": 0, "skipped": 0}
        if not self.db:
            return summary
        try:
            rows = self.db.get_resumable_torrents()
        except Exception as e:
            logger.warning(f"[ENGINE] DB resume query failed: {e}")
            return summary
        if not rows:
            logger.info("[ENGINE] No resumable torrents in DB — Live Swarm starts empty.")
            return summary
        logger.info(f"[ENGINE] Attempting to auto-resume {len(rows)} torrent(s) from DB...")
        for row in rows:
            try:
                before = len(self.sessions)
                sess = await self._rebuild_session_from_row(row)
                if sess is None:
                    summary["skipped"] += 1
                    continue
                if len(self.sessions) > before or row.get("info_hash", "").lower() in self.sessions:
                    st = str(getattr(sess.status, "value", sess.status)).lower()
                    if st == "completed":
                        summary["completed"] += 1
                    elif st == "paused":
                        summary["paused"] += 1
                    else:
                        summary["restored"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as e:
                logger.warning(f"[ENGINE] Auto-resume failed for {row.get('info_hash', '?')[:8]}: {e}")
                summary["skipped"] += 1
        logger.info(f"[ENGINE] Auto-resume summary: {summary}")
        return summary

    def set_speed_limit(self, info_hash_hex: str, limit_bytes_per_sec: Optional[int]) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.set_download_limit(limit_bytes_per_sec)
            return True
        return False

    async def remove_torrent(self, info_hash_hex: str, delete_files: bool = False) -> bool:
        key = info_hash_hex.lower()
        session = self.sessions.pop(key, None)
        if session:
            await session.stop()

            if self.db:
                self.db.mark_torrent_removed(
                    info_hash=key,
                    deleted_files=delete_files,
                    downloaded_bytes=session.piece_manager.bytes_downloaded,
                    uploaded_bytes=session.piece_manager.bytes_uploaded,
                )

            if delete_files:
                self._delete_session_files(session)
            return True
        # DB-only fallback: a 💾 saved row with no live session (e.g. pending
        # after restart, or cached metainfo missing). Remove the DB row so the
        # card disappears from Live Swarm instead of 404-looping.
        if self.db:
            row = self.db.get_torrent_history(key)
            if row:
                if delete_files:
                    self._delete_cached_files_best_effort(key, row)
                self.db.mark_torrent_removed(info_hash=key, deleted_files=delete_files)
                return True
        return False

    def _delete_session_files(self, session: TorrentSession) -> None:
        try:
            for f in session.torrent.files:
                fp = session.download_dir / f.path
                if fp.exists():
                    fp.unlink(missing_ok=True)
            if session.torrent.is_multi_file:
                top_dir = session.download_dir / session.torrent.name
                if top_dir.exists() and top_dir.is_dir():
                    shutil.rmtree(top_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Error deleting files: {e}")

    def _delete_cached_files_best_effort(self, info_hash_hex: str, row: dict) -> None:
        """Deletes partial files for a DB-only row using cached metainfo (best effort)."""
        try:
            cache_file = self._cache_path(info_hash_hex)
            if not cache_file.exists():
                return
            torrent = Torrent(cache_file.read_bytes())
            recorded = row.get("download_dir")
            dest_dir = Path(recorded) if recorded and Path(recorded).is_absolute() else self.download_dir
            for f in torrent.files:
                fp = dest_dir / f.path
                if fp.exists() and fp.is_file():
                    fp.unlink(missing_ok=True)
                part = dest_dir / f"{f.path}.part"
                if part.exists() and part.is_file():
                    part.unlink(missing_ok=True)
            if torrent.is_multi_file:
                top_dir = dest_dir / torrent.name
                if top_dir.exists() and top_dir.is_dir() and not any(top_dir.iterdir()):
                    top_dir.rmdir()
        except Exception as e:
            logger.warning(f"Best-effort file cleanup failed for {info_hash_hex[:8]}: {e}")

    async def shutdown(self) -> None:
        """Stops all running torrent sessions, persisting progress first."""
        if self.db:
            for key, session in list(self.sessions.items()):
                try:
                    # Don't overwrite terminal completed/removed rows on shutdown.
                    existing = self.db.get_torrent_history(key)
                    if existing and str(existing.get("status", "")).lower() in ("completed", "removed"):
                        continue
                    status = session.status.value if hasattr(session.status, "value") else str(session.status)
                    if session.piece_manager.is_complete:
                        status = "completed"
                    elif status not in ("paused", "error", "completed"):
                        status = "paused"
                    self.db.update_torrent_progress(
                        key,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        status,
                    )
                except Exception:
                    pass
        for session in list(self.sessions.values()):
            try:
                await session.stop()
            except Exception:
                pass
        self.sessions.clear()

    def get_all_torrents(self) -> List[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    def get_swarm_snapshot(self) -> List[dict]:
        """Live Swarm payload: in-memory sessions + DB fallback rows.

        Memory is authoritative while running; DB rows fill the gap after a
        restart/recreate before (or when) auto-resume cannot rebuild a session
        (e.g. cached .torrent missing). Fallback entries carry
        ``source='db'`` and ``resumable=True`` so the UI can offer Resume.
        """
        live = [s.to_dict() for s in self.sessions.values()]
        for item in live:
            item["source"] = "live"
            item["resumable"] = False
        if not self.db:
            return live
        try:
            rows = self.db.get_resumable_torrents()
        except Exception:
            return live
        live_hashes = {d.get("info_hash", "").lower() for d in live}
        merged = list(live)
        for r in rows:
            h = str(r.get("info_hash", "")).lower()
            if not h or h in live_hashes or h in self.sessions:
                continue
            total = int(r.get("total_size") or 0)
            dl = int(r.get("downloaded_bytes") or 0)
            ul = int(r.get("uploaded_bytes") or 0)
            progress = round((dl / total * 100.0) if total > 0 else 0.0, 2)
            merged.append(
                {
                    "info_hash": h,
                    "name": r.get("name") or h[:12],
                    "status": str(r.get("status") or "paused").lower(),
                    "error_message": r.get("error_message"),
                    "total_size": total,
                    "downloaded": dl,
                    "uploaded": ul,
                    "progress": progress,
                    "download_speed": 0.0,
                    "upload_speed": 0.0,
                    "download_limit": None,
                    "eta": None,
                    "peers_connected": 0,
                    "peers_total": 0,
                    "piece_count": 0,
                    "pieces_completed": 0,
                    "piece_length": 0,
                    "is_multi_file": False,
                    "files": [],
                    "trackers": [],
                    "source": "db",
                    "resumable": True,
                }
            )
        return merged

    def get_global_stats(self) -> dict:
        total_dl = sum(s.download_speed for s in self.sessions.values())
        total_ul = sum(s.upload_speed for s in self.sessions.values())
        total_bytes_dl = sum(s.piece_manager.bytes_downloaded for s in self.sessions.values())
        total_bytes_ul = sum(s.piece_manager.bytes_uploaded for s in self.sessions.values())

        return {
            "active_torrents": len(self.sessions),
            "total_download_speed": round(total_dl, 2),
            "total_upload_speed": round(total_ul, 2),
            "total_downloaded": total_bytes_dl,
            "total_uploaded": total_bytes_ul,
        }
