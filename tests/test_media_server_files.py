"""iter_library_files / last_scan_at / library_roots on the Plex, Jellyfin and Subsonic adapters (mocked I/O)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from trackseerr.media_servers import MediaServerUnsupported
from trackseerr.media_servers import jellyfin as jf_mod
from trackseerr.media_servers import subsonic as sub_mod
from trackseerr.media_servers.base import (
    MediaServer,
    ServerCapabilities,
    ServerFileRef,
)
from trackseerr.media_servers.jellyfin import JellyfinMediaServer
from trackseerr.media_servers.plex import PlexMediaServer
from trackseerr.media_servers.subsonic import SubsonicMediaServer


def test_capabilities_to_dict_includes_file_paths():
    assert ServerCapabilities(file_paths=True).to_dict()["file_paths"] is True
    assert ServerCapabilities().to_dict()["file_paths"] is False


def test_base_defaults():
    class Bare(MediaServer):
        kind = "bare"
        capabilities = ServerCapabilities()

        def test_connection(self): ...
        def match_track(self, *a, **k): ...
        def match_playlist_tracks(self, *a, **k): ...
        def sync_playlist(self, *a, **k): ...
        def refresh_library(self): ...
        def search_tracks(self, *a, **k): ...

    b = Bare()
    assert b.paths_relative is False
    assert b.last_scan_at() is None
    assert b.library_roots() == []
    with pytest.raises(MediaServerUnsupported):
        next(b.iter_library_files())


# ------------------------------------------------------------------ Plex


def _track(key, title, parts_by_media, artist="Art", album="Alb"):
    media = [
        SimpleNamespace(container=mc, parts=[SimpleNamespace(file=f, container=pc) for f, pc in parts])
        for mc, parts in parts_by_media
    ]
    return SimpleNamespace(ratingKey=key, title=title, grandparentTitle=artist, parentTitle=album, media=media)


class FakeSection:
    def __init__(self, type_, tracks, locations=(), scanned=None):
        self.type = type_
        self._tracks = tracks
        self.locations = list(locations)
        self.scannedAt = scanned
        self.updatedAt = None
        self.calls = []

    def search(self, libtype=None, container_start=0, container_size=100):
        self.calls.append((libtype, container_start, container_size))
        return self._tracks[container_start : container_start + container_size]


def plex_server(sections):
    lib = SimpleNamespace(sections=lambda: sections)
    return PlexMediaServer(SimpleNamespace(server=SimpleNamespace(library=lib)))


def test_plex_capability_and_multipart_tracks():
    t = _track(1, "One", [("FLAC", [("/data/a/1.flac", "FLAC"), ("/data/a/1b.flac", "")]), ("mp3", [("/data/a/1.mp3", "MP3")])])
    sec = FakeSection("artist", [t], ["/data/a"])
    srv = plex_server([sec])
    assert srv.capabilities.file_paths is True
    refs = list(srv.iter_library_files())
    assert [(r.path, r.container) for r in refs] == [
        ("/data/a/1.flac", "flac"),
        ("/data/a/1b.flac", "flac"),
        ("/data/a/1.mp3", "mp3"),
    ]
    assert refs[0] == ServerFileRef("1", "/data/a/1.flac", "One", "Art", "Alb", "flac")


def test_plex_only_music_sections_and_roots():
    music1 = FakeSection("artist", [_track(1, "A", [("flac", [("/m1/a.flac", "flac")])])], ["/m1", "/m1b"])
    music2 = FakeSection("artist", [_track(2, "B", [("flac", [("/m2/b.flac", "flac")])])], ["/m2"])
    movies = FakeSection("movie", [_track(3, "M", [("mkv", [("/mov/m.mkv", "mkv")])])], ["/mov"])
    srv = plex_server([music1, movies, music2])
    assert [r.path for r in srv.iter_library_files()] == ["/m1/a.flac", "/m2/b.flac"]
    assert srv.library_roots() == ["/m1", "/m1b", "/m2"]
    assert movies.calls == []


def test_plex_pages_sections(monkeypatch):
    from trackseerr.media_servers import plex as plex_mod

    monkeypatch.setattr(plex_mod, "_FILE_PAGE", 2)
    tracks = [_track(i, f"T{i}", [("flac", [(f"/m/{i}.flac", "flac")])]) for i in range(5)]
    sec = FakeSection("artist", tracks)
    got = list(plex_server([sec]).iter_library_files())
    assert len(got) == 5
    assert [c[1] for c in sec.calls] == [0, 2, 4]
    assert all(c[0] == "track" for c in sec.calls)


def test_plex_last_scan_at_is_max_utc():
    older = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    newer = older + timedelta(days=3)
    srv = plex_server([FakeSection("artist", [], scanned=older), FakeSection("artist", [], scanned=newer)])
    assert srv.last_scan_at() == newer
    assert srv.last_scan_at().tzinfo is not None


def test_plex_last_scan_at_naive_and_missing():
    naive = datetime(2024, 1, 1, 12, 0)
    got = plex_server([FakeSection("artist", [], scanned=naive)]).last_scan_at()
    assert got is not None and got.utcoffset() == timedelta(0)
    assert plex_server([FakeSection("artist", [])]).last_scan_at() is None


# ------------------------------------------------------------------ Jellyfin


def jf(handler):
    return JellyfinMediaServer("http://jf", "key", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def _audio(i, path="auto", container="FLAC"):
    item = {"Id": f"id{i}", "Name": f"T{i}", "Album": "Alb", "Artists": ["Art"], "Container": container}
    if path == "auto":
        item["Path"] = f"/media/music/{i}.flac"
    elif path:
        item["Path"] = path
    return item


def test_jellyfin_paging_and_fields(monkeypatch):
    monkeypatch.setattr(jf_mod, "_FILE_PAGE", 2)
    items = [_audio(i) for i in range(5)]
    seen = []

    def handler(req):
        q = req.url.params
        seen.append(dict(q))
        start, limit = int(q["StartIndex"]), int(q["Limit"])
        return httpx.Response(200, json={"Items": items[start : start + limit], "TotalRecordCount": 5})

    refs = list(jf(handler).iter_library_files())
    assert [r.path for r in refs] == [f"/media/music/{i}.flac" for i in range(5)]
    assert refs[0].container == "flac" and refs[0].artist == "Art" and refs[0].server_id == "id0"
    assert len(seen) == 3
    assert seen[0]["IncludeItemTypes"] == "Audio" and seen[0]["Recursive"] == "true"
    assert "Path" in seen[0]["Fields"] and "MediaSources" in seen[0]["Fields"]


def test_jellyfin_container_falls_back_to_media_sources():
    item = _audio(1, container="")
    item["MediaSources"] = [{"Container": "OGG"}]
    body = {"Items": [item], "TotalRecordCount": 1}
    refs = list(jf(lambda r: httpx.Response(200, json=body)).iter_library_files())
    assert refs[0].container == "ogg"


def test_jellyfin_missing_path_is_unsupported():
    body = {"Items": [_audio(1, path=None)], "TotalRecordCount": 1}
    with pytest.raises(MediaServerUnsupported) as ei:
        list(jf(lambda r: httpx.Response(200, json=body)).iter_library_files())
    assert ei.value.safe_detail == "Jellyfin API key must belong to an administrator to read file paths"


def test_jellyfin_capability_and_roots():
    def handler(req):
        assert req.url.path == "/Library/VirtualFolders"
        return httpx.Response(
            200,
            json=[
                {"Name": "Music", "CollectionType": "music", "Locations": ["/media/music", "/media/more"]},
                {"Name": "Movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
                {"Name": "Mixed", "Locations": ["/media/mixed"]},
            ],
        )

    srv = jf(handler)
    assert srv.capabilities.file_paths is True
    assert srv.library_roots() == ["/media/music", "/media/more", "/media/mixed"]


def test_jellyfin_last_scan_at():
    tasks = [
        {"Name": "Other", "LastExecutionResult": {"EndTimeUtc": "2020-01-01T00:00:00Z"}},
        {"Name": "Scan Media Library", "LastExecutionResult": {"EndTimeUtc": "2024-05-06T07:08:09.1234567Z"}},
    ]
    got = jf(lambda r: httpx.Response(200, json=tasks)).last_scan_at()
    assert got == datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=timezone.utc)


def test_jellyfin_last_scan_at_absent():
    assert jf(lambda r: httpx.Response(200, json=[{"Name": "Scan Media Library"}])).last_scan_at() is None
    assert jf(lambda r: httpx.Response(200, json=[])).last_scan_at() is None


# ------------------------------------------------------------------ Subsonic


def sub(handler):
    return SubsonicMediaServer("http://sub", "u", "p", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def _ok(**extra):
    return httpx.Response(200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **extra}})


def _song(i, path=True):
    s = {"id": f"s{i}", "title": f"T{i}", "artist": "Art", "album": "Alb", "suffix": "FLAC"}
    if path:
        s["path"] = f"Art/Alb/{i:02d} T{i}.flac"
    return s


def test_subsonic_paging_relative_paths(monkeypatch):
    monkeypatch.setattr(sub_mod, "_FILE_PAGE", 2)
    songs = [_song(i) for i in range(5)]
    offsets = []

    def handler(req):
        assert req.url.path.endswith("/rest/search3")
        q = req.url.params
        assert q["query"] == ""
        off, cnt = int(q["songOffset"]), int(q["songCount"])
        offsets.append(off)
        return _ok(searchResult3={"song": songs[off : off + cnt]})

    srv = sub(handler)
    assert srv.paths_relative is True and srv.capabilities.file_paths is True
    refs = list(srv.iter_library_files())
    assert [r.path for r in refs] == [s["path"] for s in songs]
    assert refs[0].container == "flac" and refs[0].server_id == "s0"
    assert offsets == [0, 2, 4]
    assert srv.library_roots() == []


def test_subsonic_missing_path_is_unsupported():
    srv = sub(lambda r: _ok(searchResult3={"song": [_song(1, path=False)]}))
    with pytest.raises(MediaServerUnsupported) as ei:
        list(srv.iter_library_files())
    assert "does not expose file paths" in ei.value.safe_detail


def test_subsonic_empty_library_yields_nothing():
    assert list(sub(lambda r: _ok(searchResult3={})).iter_library_files()) == []


def test_subsonic_last_scan_at():
    srv = sub(lambda r: _ok(scanStatus={"scanning": False, "count": 3, "lastScan": "2024-03-04T05:06:07.5Z"}))
    assert srv.last_scan_at() == datetime(2024, 3, 4, 5, 6, 7, 500000, tzinfo=timezone.utc)


def test_subsonic_last_scan_at_absent_or_bad():
    assert sub(lambda r: _ok(scanStatus={"scanning": False})).last_scan_at() is None
    assert sub(lambda r: _ok(scanStatus={"lastScan": "garbage"})).last_scan_at() is None
