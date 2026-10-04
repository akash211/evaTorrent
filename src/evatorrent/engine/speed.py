"""Named download speed profiles with time-based scheduling.

Profiles cap *aggregate* download throughput across all torrents
(e.g. "Day" 5 MB/s while browsing, "Night" unlimited). Schedules map
local-time windows to profiles; a manual override holds until cleared.

Persisted in SQLite kv_settings (speed_profiles / speed_schedule /
speed_override). Out of the box there is a single "Unlimited" profile and
an empty schedule, i.e. scheduling is inert until configured.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_PROFILES = [{"name": "Unlimited", "limit": None}]


def _parse_hhmm(value: str) -> int:
    m = TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid time {value!r}: use HH:MM (24h)")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_profiles(profiles: List[dict]) -> List[dict]:
    """Validates [{name, limit}] (limit null|0 = unlimited, else bytes/sec)."""
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("profiles must be a non-empty list of {name, limit}")
    seen = set()
    clean: List[dict] = []
    for p in profiles:
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            raise ValueError("Each profile needs a non-empty name")
        name = str(p["name"]).strip()[:64]
        if name.lower() in seen:
            raise ValueError(f"Duplicate profile name: {name}")
        seen.add(name.lower())
        limit = p.get("limit")
        if limit is None:
            limit = None
        else:
            try:
                limit = int(limit)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid limit for profile '{name}': must be bytes/sec or null")
            if limit < 0:
                raise ValueError(f"Invalid limit for profile '{name}': must be >= 0")
            if limit == 0:
                limit = None
        clean.append({"name": name, "limit": limit})
    return clean


def validate_schedule(schedule: List[dict], profile_names: List[str]) -> List[dict]:
    """Validates [{profile, start, end, days?}] (days 0=Mon..6=Sun, default all)."""
    if not isinstance(schedule, list):
        raise ValueError("schedule must be a list")
    known = {n.lower() for n in profile_names}
    clean: List[dict] = []
    for entry in schedule:
        if not isinstance(entry, dict):
            raise ValueError("Each schedule entry must be an object")
        profile = str(entry.get("profile", "")).strip()
        if profile.lower() not in known:
            raise ValueError(f"Schedule references unknown profile '{profile}'")
        start = _parse_hhmm(str(entry.get("start", "")))
        end = _parse_hhmm(str(entry.get("end", "")))
        days = entry.get("days", [0, 1, 2, 3, 4, 5, 6])
        if not isinstance(days, list) or not days or any(not isinstance(d, int) or d < 0 or d > 6 for d in days):
            raise ValueError(f"Invalid days for schedule entry '{profile}': use 0=Mon..6=Sun")
        clean.append({"profile": profile, "start": entry["start"], "end": entry["end"], "days": sorted(set(days)),
                      "_start_min": start, "_end_min": end})
    for e in clean:
        e.pop("_start_min", None)
        e.pop("_end_min", None)
    return clean


def match_schedule(schedule: List[dict], now: Optional[datetime] = None) -> Optional[str]:
    """Returns the profile name whose window contains `now` (first match wins)."""
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    weekday = now.weekday()
    for entry in schedule:
        try:
            start = _parse_hhmm(str(entry["start"]))
            end = _parse_hhmm(str(entry["end"]))
        except ValueError:
            continue
        if weekday not in entry.get("days", [0, 1, 2, 3, 4, 5, 6]):
            continue
        if start <= end:
            inside = start <= minute < end
        else:  # overnight window, e.g. 22:00-07:00
            inside = minute >= start or minute < end
        if inside:
            return str(entry["profile"])
    return None


def load_speed_config(db) -> Tuple[List[dict], List[dict], Optional[str]]:
    """Reads (profiles, schedule, override) from DB with safe defaults."""
    profiles = DEFAULT_PROFILES
    schedule: List[dict] = []
    override: Optional[str] = None
    if db is None:
        return [dict(p) for p in profiles], schedule, override
    try:
        raw = db.get_setting("speed_profiles")
        if raw:
            profiles = validate_profiles(json.loads(raw))
    except Exception as e:
        logger.warning(f"[SPEED] Stored profiles invalid, using defaults: {e}")
        profiles = [dict(p) for p in DEFAULT_PROFILES]
    try:
        raw = db.get_setting("speed_schedule")
        if raw:
            schedule = validate_schedule(json.loads(raw), [p["name"] for p in profiles])
    except Exception as e:
        logger.warning(f"[SPEED] Stored schedule invalid, ignoring: {e}")
        schedule = []
    try:
        override = db.get_setting("speed_override")
    except Exception:
        override = None
    return profiles, schedule, override


def profile_limit(profiles: List[dict], name: str) -> Optional[int]:
    for p in profiles:
        if p["name"].lower() == name.lower():
            return p["limit"]
    return None
