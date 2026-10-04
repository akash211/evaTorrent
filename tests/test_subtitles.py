import io
import zipfile

import pytest

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


def test_clean_video_title_strips_uploader_suffix():
    q, year, season, episode = clean_video_title(
        "Perfume The Story of a Murderer 2006 1080p BluRay x264 AAC - Ozlem.mp4"
    )
    assert "ozlem" not in q.lower()
    assert year == 2006
    assert "perfume" in q.lower()


def test_clean_video_title_keeps_episode_suffix():
    q, year, season, episode = clean_video_title("Some.Show - S01E02.mkv")
    assert season == 1 and episode == 2


def test_clean_video_title_strips_brackets():
    q, year, season, episode = clean_video_title("Just.Like.Heaven.2005.1080p[MAX.WEB-DL][TGx].mkv")
    assert "tgx" not in q.lower()
    assert year == 2005


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


@pytest.mark.asyncio
async def test_yify_by_imdb_parses_live_structure():
    from evatorrent.subtitles.providers import yify_by_imdb

    movie_html = """
    <html><body>
    <a href="/subtitles/some-movie-2020-french-yify-111">French</a>
    <a href="/subtitles/some-movie-2020-english-yify-222">English subtitle</a>
    <a href="/subtitles/some-movie-2020-english-yify-333">English HI</a>
    </body></html>
    """
    detail_html = '<html><body><a href="/subtitle/some-movie-2020-english-yify-222.zip">Download</a> 8.5/10</body></html>'

    class FakeResp:
        def __init__(self, text, status=200):
            self.text = text
            self.status_code = status

    class FakeClient:
        async def get(self, url):
            if url.endswith("/movie-imdb/tt1234567"):
                return FakeResp(movie_html)
            if "/subtitles/" in url:
                return FakeResp(detail_html)
            return FakeResp("", status=404)

    results = await yify_by_imdb(FakeClient(), "tt1234567", "Some Movie", limit=10)
    assert len(results) == 2
    assert all(r.provider == "yify" and r.language == "en" for r in results)
    assert all(r.download_url.endswith(".zip") for r in results)
    assert "french" not in " ".join(r.release for r in results).lower()


@pytest.mark.asyncio
async def test_yify_by_imdb_invalid_and_missing():
    from evatorrent.subtitles.providers import yify_by_imdb

    class FakeClient:
        async def get(self, url):
            class R:
                status_code = 404
                text = ""

            return R()

    assert await yify_by_imdb(FakeClient(), "not-an-id", "X") == []
    assert await yify_by_imdb(FakeClient(), "tt1234567", "X") == []


@pytest.mark.asyncio
async def test_download_sends_referer(tmp_path):
    import io
    import zipfile

    from evatorrent.subtitles.service import SubtitleService

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("m.en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    payload = buf.getvalue()
    seen: dict = {}

    class FakeResp:
        status_code = 200
        content = payload

    class FakeClient:
        def __init__(self, *a, **kw):
            seen.update(kw.get("headers", {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return FakeResp()

    import evatorrent.subtitles.service as svc_mod

    real_client = svc_mod.httpx.AsyncClient
    svc_mod.httpx.AsyncClient = FakeClient
    try:
        (tmp_path / "Movie.2024.mkv").write_bytes(b"fake")
        svc = SubtitleService(download_dir=tmp_path)
        out = await svc.download_for_video(
            "Movie.2024.mkv",
            "https://subs.example.com/subtitle/1.zip",
            "yify",
            "https://subs.example.com/subtitles/movie-english-1",
        )
        assert out["success"] is True
        assert out["saved_as"] == "Movie.2024.srt"
        assert seen.get("Referer") == "https://subs.example.com/subtitles/movie-english-1"
    finally:
        svc_mod.httpx.AsyncClient = real_client


@pytest.mark.asyncio
async def test_download_referer_falls_back_to_origin(tmp_path):
    import io
    import zipfile

    from evatorrent.subtitles.service import SubtitleService

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("m.en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    seen: dict = {}

    class FakeResp:
        status_code = 200
        content = buf.getvalue()

    class FakeClient:
        def __init__(self, *a, **kw):
            seen.update(kw.get("headers", {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    import evatorrent.subtitles.service as svc_mod

    real_client = svc_mod.httpx.AsyncClient
    svc_mod.httpx.AsyncClient = FakeClient
    try:
        (tmp_path / "Movie.2024.mkv").write_bytes(b"fake")
        svc = SubtitleService(download_dir=tmp_path)
        await svc.download_for_video("Movie.2024.mkv", "https://subs.example.com/subtitle/1.zip", "yify", "")
        assert seen.get("Referer") == "https://subs.example.com/"
    finally:
        svc_mod.httpx.AsyncClient = real_client


@pytest.mark.asyncio
async def test_search_prefers_imdb_path(tmp_path, monkeypatch):
    import evatorrent.subtitles.service as svc_mod
    from evatorrent.subtitles.service import SubtitleService

    async def fake_resolve(self, title, media_type, year):
        assert media_type in ("movie", "tv")
        return "tt1234567"

    async def fake_yify(client, imdb_id, title, limit=20):
        from evatorrent.subtitles.service import SubtitleResult

        assert imdb_id == "tt1234567"
        return [
            SubtitleResult(
                id="yify:x", title=title, language="en", release="R", rating=9.0,
                downloads=0, provider="yify", download_url="https://x/1.zip",
            )
        ]

    async def boom(client, q, limit=20):
        raise AssertionError("legacy providers must not run when imdb path fills results")

    monkeypatch.setattr(svc_mod.SubtitleService, "_resolve_imdb", fake_resolve)
    monkeypatch.setattr("evatorrent.subtitles.providers.yify_by_imdb", fake_yify)
    monkeypatch.setattr("evatorrent.subtitles.providers.search_yify", boom)
    monkeypatch.setattr("evatorrent.subtitles.providers.search_opensubtitles_org", boom)

    svc = SubtitleService(download_dir=tmp_path)
    out = await svc.search("Some Movie", limit=1, media_type="movie", year=2020)
    assert len(out["results"]) == 1
    assert out["imdb_id"] == "tt1234567"
    assert "yify_imdb" in out["providers_queried"]
