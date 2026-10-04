"""English subtitle search + safe download for downloaded movies/series."""

from __future__ import annotations

from evatorrent.subtitles.service import SubtitleService, clean_video_title, scan_videos

__all__ = ["SubtitleService", "clean_video_title", "scan_videos"]
