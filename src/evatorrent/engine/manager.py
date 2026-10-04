"""Top-level EngineManager coordinating multiple TorrentSession instances."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
        self._monitor = None
        # Async completion hook (email notification); wired by the web app.
        self.completion_notifier: Optional[Callable[..., Any]] = None
        # Auto-updated public tracker fallbacks (trackerslist best-list cache).
        self.fallback_trackers: List[str] = []
        self._fallback_checked_at: float = 0.0
        # Global speed profile state (aggregate cap across all torrents).
        self.global_download_limit: Optional[int] = None
        self.active_speed_profile: Optional[str] = None
        self.speed_profiles: List[dict] = [{"name": "Unlimited", "limit": None}]
        self.speed_schedule: List[dict] = []
        self.speed_override: Optional[str] = None
        self._last_schedule_eval: float = 0.0
        # Download queue: at most this many torrents download concurrently
        # (0 = unlimited). Extra torrents wait as QUEUED — this is what stops
        # several huge torrents from melting a small host at once.
        try:
            self.max_active_downloads = int(float(os.environ.get("EVA_MAX_ACTIVE_DOWNLOADS", 2)))
        except (ValueError, TypeError):
            self.max_active_downloads = 2

    def _data_dir(self) -> Path:
        """Best-effort EVA_DATA_DIR (SQLite parent), used for heartbeat markers."""
        try:
            if self.db and getattr(self.db, "db_path", None):
                return Path(self.db.db_path).parent
        except Exception:
            pass
        return self.download_dir

    def start_monitor(self) -> None:
        """Starts crash forensics + RSS watchdog (called once at server boot)."""
        try:
            from evatorrent.engine.monitor import (
                check_previous_shutdown,
                log_cgroup_boot_line,
            )
            from evatorrent.engine.monitor import ResourceMonitor

            log_cgroup_boot_line()
            check_previous_shutdown(self._data_dir())
            self._monitor = ResourceMonitor(self, self._data_dir())
            self._monitor.start()
        except Exception as e:
            logger.warning(f"[ENGINE] Resource monitor failed to start: {e}")

    async def refresh_fallback_trackers(self, force: bool = False) -> List[str]:
        """Loads the cached tracker best-list, refreshing from mirrors when stale.

        Called at boot and daily; failures keep the previous cache (or the
        baked-in defaults), so enrichment can never break downloading.
        """
        from evatorrent.tracker.fallbacks import ensure_fresh_trackers, read_cached

        try:
            if not force:
                cached, age_days, source = read_cached(self.download_dir)
                if cached:
                    self.fallback_trackers = cached
                    if age_days <= 7.0:
                        self._fallback_checked_at = time.time()
                        logger.debug(f"[TRACKERS] Using cached fallback list ({len(cached)} trackers, {source})")
                        return cached
            self.fallback_trackers = await ensure_fresh_trackers(self.download_dir)
            self._fallback_checked_at = time.time()
        except Exception as e:
            logger.warning(f"[TRACKERS] Fallback refresh failed, using baked-in defaults: {e}")
            if not self.fallback_trackers:
                try:
                    cached, _, _ = read_cached(self.download_dir)
                    self.fallback_trackers = cached
                except Exception:
                    self.fallback_trackers = []
        return self.fallback_trackers

    async def maybe_refresh_fallback_trackers(self) -> None:
        """Daily refresh check (called from the resource monitor tick)."""
        try:
            if time.time() - self._fallback_checked_at >= 86400:
                await self.refresh_fallback_trackers()
        except Exception as e:
            logger.debug(f"[TRACKERS] Daily refresh check skipped: {e}")

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
        try:
            from evatorrent.engine.limits import LARGE_TORRENT_BYTES, effective_peer_settings

            if torrent.total_length >= LARGE_TORRENT_BYTES:
                _peers, _pipe = effective_peer_settings(torrent.total_length)
                logger.warning(
                    "[ENGINE] Large torrent '%s' (%.1f GB): auto-tuned to max_peers=%d pipeline=%d "
                    "to protect small hosts. Override with EVA_MAX_PEERS/EVA_PIPELINE_PER_PEER.",
                    torrent.name,
                    torrent.total_length / (1024**3),
                    _peers,
                    _pipe,
                )
        except Exception:
            pass
        session = TorrentSession(
            torrent=torrent,
            download_dir=dest_dir,
            db=self.db,
            fallback_trackers=self.fallback_trackers or None,
        )
        session.external_throttle = self.is_globally_throttled
        session.completion_notifier = self.completion_notifier
        session.on_slot_event = self._promote_queued
        logger.info(
            "[ENGINE] Added '%s' (%.2f GB, %d pieces, %d trackers, max_peers=%d) -> %s",
            torrent.name,
            torrent.total_length / (1024**3),
            torrent.piece_count,
            len(torrent.trackers),
            session.max_peers,
            dest_dir,
        )
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

        placement = self._start_or_queue(session)
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
        # Magnet bootstrap: trackerless magnets get the auto-updated best-list
        # so announces find peers immediately instead of waiting on caches.
        added = 0
        for tr in self.fallback_trackers:
            if tr not in torrent.trackers:
                torrent.trackers.append(tr)
                added += 1
        if added:
            logger.info(f"[ENGINE] Magnet bootstrap: added {added} fallback trackers to '{torrent.name}'")

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
            self._promote_queued()
            return True
        return False

    def resume_torrent(self, info_hash_hex: str) -> bool:
        from evatorrent.engine.session import TorrentStatus

        session = self.get_session(info_hash_hex)
        if session:
            if session.status == TorrentStatus.QUEUED:
                # Resume on a queued torrent = jump the queue when a slot is free.
                if self._slot_free():
                    session.start()
                    if self.db:
                        self.db.log_event(info_hash_hex, "RESUMED", "Queued torrent started by user")
                    return True
                logger.info(
                    f"[QUEUE] '{session.torrent.name}' stays queued #{session.queue_position} (no free slot)"
                )
                return True
            if session.status == TorrentStatus.COMPLETED:
                session.resume()
                return True
            if not self._slot_free() and session.status != TorrentStatus.DOWNLOADING:
                session.status = TorrentStatus.QUEUED
                self._refresh_queue_positions()
                if self.db:
                    try:
                        self.db.update_torrent_progress(
                            info_hash_hex,
                            session.piece_manager.bytes_downloaded,
                            session.piece_manager.bytes_uploaded,
                            "queued",
                        )
                    except Exception:
                        pass
                logger.info(f"[QUEUE] '{session.torrent.name}' queued #{session.queue_position} (slots full)")
                return True
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
        # Restore persisted per-file priorities BEFORE init so deselected
        # pieces are never hashed or requested after a restart.
        restored_prio: Optional[Dict[str, int]] = None
        if self.db:
            try:
                restored_prio = self.db.get_effective_file_priorities(
                    info_hash_hex, [f.path for f in torrent.files]
                )
                if restored_prio:
                    n_high = sum(1 for v in restored_prio.values() if v == 2)
                    n_skip = sum(1 for v in restored_prio.values() if v == 0)
                    logger.info(
                        f"[ENGINE] Restoring file priorities for '{torrent.name}': "
                        f"{n_high} high, {n_skip} skipped"
                    )
            except Exception as e:
                logger.warning(f"[ENGINE] Failed reading file priorities for {info_hash_hex[:8]}: {e}")
        try:
            session = TorrentSession(
                torrent=torrent, download_dir=dest_dir, db=self.db, file_priorities=restored_prio,
                fallback_trackers=self.fallback_trackers or None,
            )
            session.external_throttle = self.is_globally_throttled
            session.completion_notifier = self.completion_notifier
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
        elif prev_status == "queued":
            session.status = session.status.__class__("queued")
            self._refresh_queue_positions()
            if self.db:
                self.db.log_event(info_hash_hex, "QUEUED", "Session rebuilt from DB in queued state")
        else:
            placement = self._start_or_queue(session, reason="auto-resume after restart")
            if self.db:
                # Refresh progress baseline so Analytics stops showing stale rows.
                try:
                    self.db.update_torrent_progress(
                        info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        session.status.value,
                    )
                except Exception:
                    pass
                self.db.log_event(
                    info_hash_hex, "RESUMED", f"Session auto-resumed after restart ({placement})"
                )
        return session

    async def restore_from_db(self) -> dict:
        """Rebuilds interrupted sessions from SQLite. Called once at server boot.

        Returns summary counts: {restored, paused, queued, completed, skipped}.
        Never raises — per-torrent failures are logged and skipped so one bad
        row cannot prevent the rest of the swarm from resuming.
        """
        summary = {"restored": 0, "paused": 0, "queued": 0, "completed": 0, "skipped": 0}
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
                    elif st == "queued":
                        summary["queued"] += 1
                    else:
                        summary["restored"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as e:
                logger.warning(f"[ENGINE] Auto-resume failed for {row.get('info_hash', '?')[:8]}: {e}")
                summary["skipped"] += 1
        logger.info(f"[ENGINE] Auto-resume summary: {summary}")
        return summary

    def active_download_count(self) -> int:
        from evatorrent.engine.session import TorrentStatus

        return sum(1 for s in self.sessions.values() if s.status == TorrentStatus.DOWNLOADING)

    def queued_sessions(self) -> List[TorrentSession]:
        from evatorrent.engine.session import TorrentStatus

        return [s for s in self.sessions.values() if s.status == TorrentStatus.QUEUED]

    def _refresh_queue_positions(self) -> None:
        for pos, s in enumerate(self.queued_sessions(), start=1):
            s.queue_position = pos
        for s in self.sessions.values():
            from evatorrent.engine.session import TorrentStatus

            if s.status != TorrentStatus.QUEUED:
                s.queue_position = None

    def _slot_free(self) -> bool:
        return self.max_active_downloads <= 0 or self.active_download_count() < self.max_active_downloads

    def _start_or_queue(self, session: TorrentSession, reason: str = "added") -> str:
        """Starts the session now or parks it as QUEUED. Returns 'started'|'queued'."""
        from evatorrent.engine.session import TorrentStatus

        session.on_slot_event = self._promote_queued
        if session.piece_manager.is_complete:
            session.start()
            return "started"
        if not self._slot_free():
            session.status = TorrentStatus.QUEUED
            self._refresh_queue_positions()
            if self.db:
                try:
                    self.db.update_torrent_progress(
                        session.torrent.info_hash_hex,
                        session.piece_manager.bytes_downloaded,
                        session.piece_manager.bytes_uploaded,
                        "queued",
                    )
                except Exception:
                    pass
            logger.info(
                "[QUEUE] '%s' (%s) queued #%d (%s; %d active, max %d)",
                session.torrent.name,
                session.torrent.info_hash_hex[:8],
                session.queue_position or 0,
                reason,
                self.active_download_count(),
                self.max_active_downloads,
            )
            return "queued"
        session.start()
        return "started"

    def _promote_queued(self) -> None:
        """Starts waiting torrents while download slots are free (FIFO)."""
        try:
            while self._slot_free():
                queued = self.queued_sessions()
                if not queued:
                    break
                nxt = queued[0]
                nxt.start()
                logger.info(
                    "[QUEUE] Promoted '%s' (%s) from queue (%d active, max %d)",
                    nxt.torrent.name,
                    nxt.torrent.info_hash_hex[:8],
                    self.active_download_count(),
                    self.max_active_downloads,
                )
                if self.db:
                    try:
                        self.db.update_torrent_progress(
                            nxt.torrent.info_hash_hex,
                            nxt.piece_manager.bytes_downloaded,
                            nxt.piece_manager.bytes_uploaded,
                            "downloading",
                        )
                        self.db.log_event(nxt.torrent.info_hash_hex, "RESUMED", "Promoted from download queue")
                    except Exception:
                        pass
            self._refresh_queue_positions()
        except Exception as e:
            logger.warning(f"[QUEUE] Promotion failed: {e}")

    def set_speed_limit(self, info_hash_hex: str, limit_bytes_per_sec: Optional[int]) -> bool:
        session = self.get_session(info_hash_hex)
        if session:
            session.set_download_limit(limit_bytes_per_sec)
            return True
        return False

    def set_file_selection(self, info_hash_hex: str, selected_paths: Optional[List[str]]) -> Optional[dict]:
        """Updates per-file download selection; returns summary or None when unknown."""
        session = self.get_session(info_hash_hex)
        if not session:
            return None
        return session.set_selected_files(selected_paths)

    def set_file_priorities(self, info_hash_hex: str, priorities: Dict[str, int]) -> Optional[dict]:
        """Updates per-file priorities; returns summary or None when unknown."""
        session = self.get_session(info_hash_hex)
        if not session:
            return None
        return session.set_file_priorities(priorities)

    def is_globally_throttled(self) -> bool:
        """True when aggregate download speed hits the active profile cap."""
        if not self.global_download_limit or self.global_download_limit <= 0:
            return False
        total = sum(s.download_speed for s in self.sessions.values())
        return total >= self.global_download_limit

    def set_global_download_limit(self, limit_bytes_per_sec: Optional[int]) -> None:
        self.global_download_limit = limit_bytes_per_sec if (limit_bytes_per_sec and limit_bytes_per_sec > 0) else None
        for session in self.sessions.values():
            session.external_throttle = self.is_globally_throttled

    def load_speed_config(self) -> None:
        """Loads profiles/schedule/override from DB (safe defaults when absent)."""
        try:
            from evatorrent.engine.speed import load_speed_config

            profiles, schedule, override = load_speed_config(self.db)
            self.speed_profiles = profiles
            self.speed_schedule = schedule
            self.speed_override = override or None
        except Exception as e:
            logger.warning(f"[SPEED] Failed loading speed config: {e}")

    def save_speed_config(self, profiles: Optional[List[dict]] = None, schedule: Optional[List[dict]] = None) -> None:
        """Validates + persists profiles/schedule, then re-evaluates the active cap."""
        import json

        from evatorrent.engine.speed import validate_profiles, validate_schedule

        if profiles is not None:
            profiles = validate_profiles(profiles)
            self.speed_profiles = profiles
            if self.db:
                self.db.set_setting("speed_profiles", json.dumps(profiles))
        if schedule is not None:
            schedule = validate_schedule(schedule, [p["name"] for p in self.speed_profiles])
            self.speed_schedule = schedule
            if self.db:
                self.db.set_setting("speed_schedule", json.dumps(schedule))
        self.evaluate_speed_schedule(force=True)

    def set_speed_override(self, profile_name: Optional[str]) -> Optional[str]:
        """Manual profile hold (None = back to schedule). Persists across restarts."""
        if profile_name is not None:
            if not any(p["name"].lower() == profile_name.lower() for p in self.speed_profiles):
                raise ValueError(f"Unknown speed profile: {profile_name}")
        self.speed_override = profile_name
        if self.db:
            try:
                self.db.set_setting("speed_override", profile_name or "")
            except Exception:
                pass
        self.evaluate_speed_schedule(force=True)
        return self.active_speed_profile

    def evaluate_speed_schedule(self, force: bool = False) -> Optional[str]:
        """Applies override-or-schedule to the global cap. Returns active profile name."""
        from evatorrent.engine.speed import match_schedule, profile_limit

        now = time.time()
        if not force and now - self._last_schedule_eval < 60:
            return self.active_speed_profile
        self._last_schedule_eval = now
        previous = (self.active_speed_profile, self.global_download_limit)
        if self.speed_override:
            limit = profile_limit(self.speed_profiles, self.speed_override)
            self.active_speed_profile = self.speed_override
            self.set_global_download_limit(limit)
        elif self.speed_schedule:
            matched = match_schedule(self.speed_schedule)
            if matched:
                self.active_speed_profile = matched
                self.set_global_download_limit(profile_limit(self.speed_profiles, matched))
            else:
                self.active_speed_profile = None
                self.set_global_download_limit(None)
        else:
            self.active_speed_profile = None
            self.set_global_download_limit(None)
        current = (self.active_speed_profile, self.global_download_limit)
        if current != previous:
            if self.active_speed_profile:
                logger.info(
                    "[SPEED] Active profile '%s' (cap %s)",
                    self.active_speed_profile,
                    f"{self.global_download_limit / 1024 / 1024:.1f} MB/s"
                    if self.global_download_limit
                    else "unlimited",
                )
            else:
                logger.info("[SPEED] Schedule idle: no cap (unlimited)")
        return self.active_speed_profile

    async def maybe_evaluate_schedule(self) -> None:
        """Minute-granularity scheduler tick (called from the monitor loop)."""
        try:
            self.evaluate_speed_schedule()
        except Exception as e:
            logger.debug(f"[SPEED] Schedule tick skipped: {e}")

    async def remove_torrent(self, info_hash_hex: str, delete_files: bool = False) -> bool:
        key = info_hash_hex.lower()
        session = self.sessions.pop(key, None)
        if session:
            await session.stop(notify_slot=False)

            if self.db:
                self.db.mark_torrent_removed(
                    info_hash=key,
                    deleted_files=delete_files,
                    downloaded_bytes=session.piece_manager.bytes_downloaded,
                    uploaded_bytes=session.piece_manager.bytes_uploaded,
                )

            if delete_files:
                self._delete_session_files(session)
            self._refresh_queue_positions()
            self._promote_queued()
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
        try:
            if self._monitor:
                await self._monitor.stop()
        except Exception:
            pass
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
                await session.stop(notify_slot=False)
            except Exception:
                pass
        self.sessions.clear()
        try:
            from evatorrent.engine.monitor import mark_clean_shutdown

            mark_clean_shutdown(self._data_dir())
        except Exception:
            pass

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
