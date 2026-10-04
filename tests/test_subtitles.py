import io
import zipfile

from evatorrent.subtitles.service import (
    clean_video_title,
    extract_subtitle_from_bytes,
    pick_zip_member,
    scan_videos,
)


def test_clean_video_title_movie():
    q, year, season, episode = clean_video_title("Dune.Part.Two.2024.1080p.WEB-DL.x264.mkv")
    assert "dune" in q.lower()
    assert year == 2024
    assert season is None


def test_clean_video_title_series():
    q, year, season, episode = clean_video_title("Breaking.Bad.S01E02.720p.HDTV.x264.mkv")
    assert season == 1 and episode == 2
    assert "breaking bad" in q.lower()


def test_scan_videos_lists_and_detects_subs(tmp_path):
    (tmp_path / "Movie.2024.mkv").write_bytes(b"x" * 10)
    (tmp_path / "Movie.2024.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (tmp_path / ".torrent_cache").mkdir()
    (tmp_path / ".torrent_cache" / "a.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("nope")
    entries = scan_videos(tmp_path)
    assert len(entries) == 1
    assert entries[0].name == "Movie.2024.mkv"
    assert entries[0].has_subtitle is True


def test_pick_zip_member_prefers_english_srt():
    names = ["movie/french.srt", "movie/english.srt", "readme.nfo"]
    assert pick_zip_member(names) == "movie/english.srt"
    assert pick_zip_member(["readme.nfo"]) is None


def _srt_bytes() -> bytes:
    return b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def test_extract_raw_srt():
    content, ext = extract_subtitle_from_bytes(_srt_bytes(), url="https://x/subs/1.srt")
    assert ext == ".srt"
    assert b"-->" in content


def test_extract_zip_server_side():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("movie.en.srt", _srt_bytes())
        zf.writestr("readme.txt", b"hello")
    content, ext = extract_subtitle_from_bytes(buf.getvalue(), url="https://x/subs/1.zip")
    assert ext == ".srt"
    assert b"Hello" in content


def test_extract_zip_slip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.srt", _srt_bytes())
    try:
        extract_subtitle_from_bytes(buf.getvalue(), url="https://x/subs/evil.zip")
    except ValueError as e:
        assert "Unsafe" in str(e) or "no subtitle" in str(e).lower()
    else:
        raise AssertionError("zip-slip must be rejected")


def test_extract_non_subtitle_rejected():
    try:
        extract_subtitle_from_bytes(b"<html>not subs</html>", url="https://x/subs/1.srt")
    except ValueError as e:
        assert "timestamp" in str(e)
    else:
        raise AssertionError("non-subtitle must be rejected")
