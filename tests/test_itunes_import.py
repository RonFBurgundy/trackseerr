"""iTunes / Apple Music library import: parsing, skip rules, path mapping, matching, API flow, safety guards."""

from __future__ import annotations

import plistlib
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trackseerr import itunes_import as imp
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

WIN = "file://localhost/C:/Users/Aaron/iTunes/iTunes%20Media/Music"
MAC = "file://localhost/Users/aaron/Music/iTunes/iTunes%20Media/Music"


def _track(tid: int, name: str, artist: str, album: str, location: str | None, **extra: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "Track ID": tid, "Name": name, "Artist": artist, "Album": album, "Total Time": 200000,
        "Persistent ID": f"T{tid:04d}", "Date Added": datetime(2020, 1, 2, 3, 4, 5),
    }
    if location is not None:
        t["Location"] = location
    t.update(extra)
    return t


def _playlist(pid: str, name: str, ids: list[int], **extra: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"Name": name, "Playlist Persistent ID": pid, "Playlist ID": int(pid[-3:] or 0)}
    if ids:
        p["Playlist Items"] = [{"Track ID": i} for i in ids]
    p.update(extra)
    return p


def build_export() -> bytes:
    tracks = [
        _track(1, "One More Time", "Daft Punk", "Discovery", f"{WIN}/Daft%20Punk/Discovery/01%20One%20More%20Time.mp3",
               **{"Play Count": 7, "Rating": 80, "Track Number": 1, "Total Time": 320000, "Album Artist": "Daft Punk"}),
        _track(2, "Aerodynamic", "Daft Punk", "Discovery", f"{MAC}/Daft%20Punk/Discovery/02%20Aerodynamic.mp3"),
        _track(3, "Cloud Only", "Somebody", "Nowhere", None),
        _track(4, "Halo", "Beyonc\u00e9", "I Am", f"{WIN}/Beyonc%C3%A9/I%20Am/Halo.mp3"),
        _track(5, "Song", "Live Band", "", None, **{"Total Time": 399000}),
        _track(6, "Not In Library", "Ghost", "Boo", f"{WIN}/Ghost/Boo/Not%20In%20Library.mp3"),
    ]
    playlists = [
        _playlist("AAAA000000000001", "Library", [1, 2, 3], Master=True),
        _playlist("AAAA000000000002", "Music", [1, 2], **{"Distinguished Kind": 4}),
        _playlist("AAAA000000000003", "Favourites", [1, 2, 3, 4, 5, 6]),
        _playlist("AAAA000000000004", "Recently Added", [1, 4], **{"Smart Info": b"\x01\x02", "Smart Criteria": b"\x03"}),
        _playlist("AAAA000000000005", "Rock", [], Folder=True),
        _playlist("AAAA000000000006", "Empty One", []),
        _playlist("AAAA000000000007", "Favourites", [1], **{"Parent Persistent ID": "AAAA000000000005"}),
    ]
    root = {
        "Major Version": 1, "Minor Version": 1, "Application Version": "12.12",
        "Tracks": {str(t["Track ID"]): t for t in tracks},
        "Playlists": playlists,
    }
    return plistlib.dumps(root, fmt=plistlib.FMT_XML)


# --------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------

def test_parse_tracks_fields():
    parsed = imp.parse_library(build_export())
    assert len(parsed["tracks"]) == 6
    t = parsed["tracks"]["1"]
    assert t["name"] == "One More Time" and t["album_artist"] == "Daft Punk"
    assert t["play_count"] == 7 and t["rating"] == 4 and t["total_time_ms"] == 320000
    assert t["date_added"].startswith("2020-01-02") and t["persistent_id"] == "T0001"
    assert t["track_number"] == 1
    assert parsed["tracks"]["3"]["location"] == ""
    assert parsed["tracks"]["4"]["artist"] == "Beyonc\u00e9"


def test_skip_rules_and_smart_flag():
    parsed = imp.parse_library(build_export())
    assert parsed["skipped"] == {"builtin": 2, "folders": 1, "empty": 1}
    names = {p["key"]: p for p in parsed["playlists"]}
    assert set(names) == {"AAAA000000000003", "AAAA000000000004", "AAAA000000000007"}
    assert names["AAAA000000000004"]["is_smart"] is True
    assert names["AAAA000000000003"]["is_smart"] is False
    assert names["AAAA000000000007"]["folder_path"] == ["Rock"]


def test_duplicate_names_get_distinct_keys():
    parsed = imp.parse_library(build_export())
    fav = [p for p in parsed["playlists"] if p["name"] == "Favourites"]
    assert len(fav) == 2 and len({p["key"] for p in fav}) == 2


def test_display_name_prefix_and_folders():
    pl = {"name": "Mix", "folder_path": ["A", "B"]}
    assert imp.display_name(pl, None, False) == "Mix"
    assert imp.display_name(pl, None, True) == "A / B / Mix"
    assert imp.display_name(pl, "iTunes: ", True) == "iTunes: A / B / Mix"


def test_binary_plist_rejected():
    blob = plistlib.dumps({"Tracks": {}}, fmt=plistlib.FMT_BINARY)
    with pytest.raises(imp.ItunesImportError, match="Export Library"):
        imp.parse_library(blob)
    with pytest.raises(imp.ItunesImportError):
        imp.parse_library(b"\x00\x01itl-binary-garbage")


def test_not_a_library_rejected():
    with pytest.raises(imp.ItunesImportError):
        imp.parse_library(plistlib.dumps({"hello": "world"}, fmt=plistlib.FMT_XML))
    with pytest.raises(imp.ItunesImportError):
        imp.parse_library(b"<plist><dict><key>Tracks</key>")


ENTITY_XML = b"""<?xml version="1.0"?>
<!DOCTYPE plist [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<plist version="1.0"><dict><key>Tracks</key><dict><key>1</key><dict><key>Name</key><string>&lol2;</string></dict></dict></dict></plist>"""

EXTERNAL_XML = b"""<?xml version="1.0"?>
<!DOCTYPE plist SYSTEM "file:///etc/passwd" [ ]>
<plist version="1.0"><dict><key>Tracks</key><dict/></dict></plist>"""


def test_entity_and_internal_subset_rejected():
    with pytest.raises(imp.ItunesImportError, match="entit"):
        imp.parse_library(ENTITY_XML)
    with pytest.raises(imp.ItunesImportError, match="internal subset"):
        imp.parse_library(EXTERNAL_XML)


def test_standard_apple_doctype_accepted():
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        b'<plist version="1.0"><dict><key>Tracks</key><dict/><key>Playlists</key><array/></dict></plist>'
    )
    assert imp.parse_library(xml)["playlists"] == []


def test_max_import_bytes_env(monkeypatch):
    monkeypatch.delenv(imp.ENV_MAX_MB, raising=False)
    assert imp.max_import_bytes() == 100 * 1024 * 1024
    monkeypatch.setenv(imp.ENV_MAX_MB, "5")
    assert imp.max_import_bytes() == 5 * 1024 * 1024
    monkeypatch.setenv(imp.ENV_MAX_MB, "junk")
    assert imp.max_import_bytes() == 100 * 1024 * 1024


# --------------------------------------------------------------------------------------------------
# Locations, mappings
# --------------------------------------------------------------------------------------------------

def test_location_to_path_variants():
    assert imp.location_to_path("file://localhost/C:/Users/Aaron/Music/a%20b.mp3") == "C:/Users/Aaron/Music/a b.mp3"
    assert imp.location_to_path("file:///C:/Music/x.mp3") == "C:/Music/x.mp3"
    assert imp.location_to_path("file://localhost/Volumes/Media/Music/x%26y.mp3") == "/Volumes/Media/Music/x&y.mp3"
    assert imp.location_to_path("file://localhost/Users/me/Beyonc%C3%A9/Halo.mp3") == "/Users/me/Beyonc\u00e9/Halo.mp3"
    assert imp.location_to_path("file://nas/share/x.mp3") == "//nas/share/x.mp3"
    assert imp.location_to_path("") is None
    assert imp.location_to_path("http://stream.example/x.mp3") is None


def test_apply_mappings_boundaries_and_case():
    maps = [{"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music"}]
    assert imp.apply_mappings("C:/Users/Aaron/iTunes/iTunes Media/Music/A/B/c.mp3", maps) == "/music/A/B/c.mp3"
    assert imp.apply_mappings("c:/users/aaron/itunes/itunes media/music/A/c.mp3", maps) == "/music/A/c.mp3"
    assert imp.apply_mappings("C:/Users/Aaron/iTunes/iTunes Media/MusicOther/c.mp3", maps) is None
    assert imp.apply_mappings("/Users/x/c.mp3", maps) is None
    assert imp.apply_mappings("C:\\Users\\Aaron\\iTunes\\iTunes Media\\Music\\A\\c.mp3", maps) == "/music/A/c.mp3"


def test_suggest_mappings_windows_and_mac():
    lib = [f"/music/Artist {i}/Album {i}/0{i} Song {i}.mp3" for i in range(5)]
    win = [f"C:/Users/Aaron/iTunes/iTunes Media/Music/Artist {i}/Album {i}/0{i} Song {i}.mp3" for i in range(5)]
    sug = imp.suggest_mappings(win, lib)
    assert sug[0] == {"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music", "sample_matches": 5}
    mac = [f"/Volumes/Media/Music/Artist {i}/Album {i}/0{i} Song {i}.mp3" for i in range(5)]
    sug = imp.suggest_mappings(mac, lib)
    assert sug[0]["from"] == "/Volumes/Media/Music" and sug[0]["to"] == "/music" and sug[0]["sample_matches"] == 5
    assert imp.suggest_mappings(["/nope/x/y.mp3"], lib) == []
    assert imp.suggest_mappings([], lib) == []


# --------------------------------------------------------------------------------------------------
# Library fixture + matching
# --------------------------------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


def _add(db: Database, artist_id: str, artist: str, album_id: str, album: str, tid: str, title: str,
         path: str | None, duration: float | None = None, number: int = 1) -> None:
    if not db.get_library_artist_by_name(artist):
        db.upsert_library_artist({"id": artist_id, "name": artist})
    if not db.get_library_album_by_title(artist_id, album):
        db.upsert_library_album({"id": album_id, "artist_id": artist_id, "title": album})
    db.upsert_library_track({"id": tid, "album_id": album_id, "artist_id": artist_id, "title": title,
                             "duration_seconds": duration, "track_number": number})
    if path:
        db.upsert_library_file({"id": f"f-{tid}", "track_id": tid, "file_path": path, "relative_path": path.split("/music/")[-1],
                                "codec": "mp3", "quality_name": "MP3-320"})


@pytest.fixture
def library(db: Database) -> Database:
    _add(db, "a1", "Daft Punk", "al1", "Discovery", "t1", "One More Time", "/music/Daft Punk/Discovery/01 One More Time.mp3", 320.0)
    _add(db, "a1", "Daft Punk", "al1", "Discovery", "t2", "Aerodynamic", "/music/Daft Punk/Discovery/02 Aerodynamic.mp3", 212.0, 2)
    nfd = unicodedata.normalize("NFD", "Beyonc\u00e9")
    _add(db, "a2", "Beyonc\u00e9", "al2", "I Am", "t4", "Halo", f"/music/{nfd}/I Am/Halo.mp3", 261.0)
    _add(db, "a3", "Live Band", "al3", "Studio", "t5", "Song", "/music/Live Band/Studio/Song.mp3", 200.0)
    _add(db, "a3", "Live Band", "al4", "Live", "t6", "Song", "/music/Live Band/Live/Song.mp3", 400.0)
    # catalog-only track (no file): must never count as matched
    _add(db, "a4", "Ghost", "al5", "Boo", "t7", "Not In Library", None, 100.0)
    return db


WIN_MAP = [{"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music"}]


def _entry(**kw: Any) -> dict[str, Any]:
    base = {"name": "", "artist": "", "album_artist": "", "album": "", "track_number": 0, "total_time_ms": 0, "location": ""}
    base.update(kw)
    return base


def test_path_match_windows_with_accents_nfc_vs_nfd(library):
    m = imp.TrackMatcher(library, WIN_MAP)
    e = _entry(name="Wrong Title", artist="Wrong", location=f"{WIN}/Beyonc%C3%A9/I%20Am/Halo.mp3")
    assert m.match(e) == ("t4", "path")


def test_path_match_mac_mapping(library):
    m = imp.TrackMatcher(library, [{"from": "/Users/aaron/Music/iTunes/iTunes Media/Music", "to": "/music"}])
    e = _entry(name="x", location=f"{MAC}/Daft%20Punk/Discovery/02%20Aerodynamic.mp3")
    assert m.match(e) == ("t2", "path")


def test_metadata_fallback_when_no_mapping_or_no_location(library):
    m = imp.TrackMatcher(library, [])
    assert m.match(_entry(name="Aerodynamic", artist="Daft Punk", album="Discovery")) == ("t2", "metadata")
    # album artist wins, track artist is a feature credit
    assert m.match(_entry(name="One more time!", artist="Daft Punk feat. Romanthony", album_artist="Daft Punk")) == ("t1", "metadata")
    assert m.match(_entry(name="Halo", artist="Beyonc\u00e9")) == ("t4", "metadata")


def test_metadata_duration_tiebreak(library):
    m = imp.TrackMatcher(library, [])
    assert m.match(_entry(name="Song", artist="Live Band", total_time_ms=399000)) == ("t6", "metadata")
    assert m.match(_entry(name="Song", artist="Live Band", total_time_ms=201000)) == ("t5", "metadata")
    # album outranks duration
    assert m.match(_entry(name="Song", artist="Live Band", album="Studio", total_time_ms=399000)) == ("t5", "metadata")


def test_unmatched_and_catalog_only_tracks(library):
    m = imp.TrackMatcher(library, WIN_MAP)
    assert m.match(_entry(name="Cloud Only", artist="Somebody")) is None
    assert m.match(_entry(name="Not In Library", artist="Ghost", album="Boo",
                          location=f"{WIN}/Ghost/Boo/Not%20In%20Library.mp3")) is None
    assert m.match(_entry(name="", artist="Daft Punk")) is None


# --------------------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------------------

@pytest.fixture
def env(library: Database, tmp_path: Path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="tok", data_dir=str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    admin = library.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True)
    alice = library.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    app = create_app(db=library, config=config)
    app.dependency_overrides[get_db] = lambda: library
    app.dependency_overrides[get_config] = lambda: config
    secret = get_or_create_secret_key(data_dir=config.data_dir)

    def headers(user: dict[str, Any]) -> dict[str, str]:
        token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret)
        library.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    class Env:
        pass

    e = Env()
    e.client, e.db, e.config = TestClient(app), library, config
    e.admin, e.user = headers(admin), headers(alice)
    return e


def _preview(env, body: bytes | None = None, headers: dict[str, str] | None = None):
    return env.client.post("/api/import/itunes/preview", content=body if body is not None else build_export(),
                           headers=headers or env.admin)


def _commit(env, import_id: str, **body: Any):
    return env.client.post(f"/api/import/itunes/{import_id}/commit", json=body, headers=env.admin)


def _wait(env, import_id: str, timeout: float = 15.0) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        data = env.client.get(f"/api/import/itunes/{import_id}/status", headers=env.admin).json()
        if data["state"] in ("completed", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def test_preview_contract(env):
    r = _preview(env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["import_id"]) == 32 and body["track_count"] == 6
    assert body["skipped"] == {"builtin": 2, "folders": 1, "empty": 1}
    by_name = {(p["name"], p["item_count"]): p for p in body["playlists"]}
    assert by_name[("Recently Added", 2)]["is_smart"] is True
    assert by_name[("Favourites", 6)]["is_smart"] is False
    # no mapping is suggestable here: the export's Windows/Mac roots share path tails with /music
    sug = body["suggested_mappings"]
    assert sug and sug[0]["to"] == "/music" and sug[0]["sample_matches"] >= 1


def test_commit_defaults_to_none_and_runs_in_background(env, monkeypatch):
    seen: dict[str, Any] = {}
    original = imp.run_import

    def spy(*a: Any, **kw: Any) -> None:
        seen["thread"] = threading.current_thread()
        seen["kw"] = kw
        original(*a, **kw)

    monkeypatch.setattr(imp, "run_import", spy)
    iid = _preview(env).json()["import_id"]
    r = _commit(env, iid, playlists="all", path_mappings=[{"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music"}])
    assert r.status_code == 202 and r.json()["job_id"].startswith("itunes-")
    status = _wait(env, iid)
    assert seen["thread"] is not threading.main_thread() and seen["thread"].daemon
    assert seen["kw"]["monitor_mode"] is None
    assert status["state"] == "completed" and status["total"] == 3 and status["done"] == 3
    res = {(p["name"], p["matched"] + p["missing"]): p for p in status["playlists"]}
    fav = next(p for p in status["playlists"] if p["name"] == "Favourites" and p["matched"] + p["missing"] > 1)
    # tracks 1,2 (path), 4 (path, NFD), 5 (metadata, duration): matched; 3 cloud-only and 6 (no file) missing
    assert fav["matched"] == 4 and fav["missing"] == 2
    pl = env.db.get_playlist(fav["created_playlist_id"])
    assert pl["service"] == "itunes" and pl["monitor_mode"] == "none"
    assert pl["creator_id"] == "admin-1"
    missing = env.db.get_missing_tracks(fav["created_playlist_id"])
    assert {m["title"] for m in missing} == {"Cloud Only", "Not In Library"}
    assert status["play_stats"] == {"requested": False, "applied": False}
    assert res


def test_commit_selection_name_prefix_and_folders(env):
    parsed = imp.parse_library(build_export())
    keys = {p["name"]: p["key"] for p in parsed["playlists"] if p["name"] == "Recently Added"}
    iid = _preview(env).json()["import_id"]
    r = _commit(env, iid, playlists=[keys["Recently Added"], "AAAA000000000007"], name_prefix="iTunes: ",
                include_folders=True, monitor_mode="track", import_play_stats=True)
    assert r.status_code == 202
    status = _wait(env, iid)
    assert sorted(p["name"] for p in status["playlists"]) == ["Favourites", "Recently Added"]
    stored = sorted(p["name"] for p in env.db.list_playlists() if p["service"] == "itunes")
    assert stored == ["iTunes: Recently Added", "iTunes: Rock / Favourites"]
    assert all(p["monitor_mode"] == "track" for p in env.db.list_playlists() if p["service"] == "itunes")
    assert status["play_stats"]["applied"] is False and "scrobble" in status["play_stats"]["note"]


def test_reimport_updates_same_playlists(env):
    maps = [{"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music"}]
    iid = _preview(env).json()["import_id"]
    _commit(env, iid, path_mappings=maps)
    first = _wait(env, iid)
    before = sorted(p["id"] for p in env.db.list_playlists() if p["service"] == "itunes")
    # a fresh upload of the same export gets a new import id but the same persistent ids
    iid2 = _preview(env).json()["import_id"]
    assert iid2 != iid
    _commit(env, iid2, path_mappings=maps)
    second = _wait(env, iid2)
    after = sorted(p["id"] for p in env.db.list_playlists() if p["service"] == "itunes")
    assert before == after and len(after) == 3
    assert [p["created_playlist_id"] for p in first["playlists"]] == [p["created_playlist_id"] for p in second["playlists"]]
    assert all(p["updated"] for p in second["playlists"])
    # missing tracks are replaced, not appended
    fav_id = next(p["created_playlist_id"] for p in second["playlists"] if p["missing"] == 2)
    assert len(env.db.get_missing_tracks(fav_id)) == 2


def test_reimport_keeps_existing_monitor_mode_unless_given(env):
    iid = _preview(env).json()["import_id"]
    _commit(env, iid, monitor_mode="track")
    _wait(env, iid)
    _commit(env, iid)
    _wait(env, iid)
    assert {p["monitor_mode"] for p in env.db.list_playlists() if p["service"] == "itunes"} == {"track"}
    _commit(env, iid, monitor_mode="none")
    _wait(env, iid)
    assert {p["monitor_mode"] for p in env.db.list_playlists() if p["service"] == "itunes"} == {"none"}


def test_album_mode_applies_missing_via_existing_machinery(env, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(imp, "apply_playlist_missing_safely", lambda db, cfg, pid: calls.append(pid))
    iid = _preview(env).json()["import_id"]
    _commit(env, iid, monitor_mode="album")
    status = _wait(env, iid)
    assert sorted(calls) == sorted(p["created_playlist_id"] for p in status["playlists"])


def test_commit_errors(env):
    iid = _preview(env).json()["import_id"]
    assert _commit(env, "0" * 32).status_code == 404
    assert _commit(env, "../../etc/passwd").status_code == 404
    assert _commit(env, iid, playlists=["nope"]).status_code == 400
    assert _commit(env, iid, playlists=[]).status_code == 400
    assert _commit(env, iid, monitor_mode="everything").status_code == 422
    assert env.client.get(f"/api/import/itunes/{'1' * 32}/status", headers=env.admin).status_code == 404
    assert env.client.get(f"/api/import/itunes/{iid}/status", headers=env.admin).json()["state"] == "idle"


def test_expired_import_is_gone(env):
    iid = _preview(env).json()["import_id"]
    path = imp.import_dir(env.config.data_dir) / f"{iid}.json"
    old = time.time() - imp.IMPORT_TTL_SECONDS - 60
    import os
    os.utime(path, (old, old))
    assert _commit(env, iid).status_code == 404
    assert not path.exists()


def test_authz(env):
    assert _preview(env, headers=env.user).status_code == 403
    assert env.client.post("/api/import/itunes/preview", content=build_export()).status_code == 401
    iid = _preview(env).json()["import_id"]
    assert env.client.post(f"/api/import/itunes/{iid}/commit", json={}, headers=env.user).status_code == 403
    assert env.client.get(f"/api/import/itunes/{iid}/status", headers=env.user).status_code == 403
    env.config.role = "gateway"  # the gateway tier never exposes this route (blocked at the tier middleware)
    assert _preview(env).status_code in (403, 404)


def test_preview_rejections(env, monkeypatch):
    r = _preview(env, body=plistlib.dumps({"Tracks": {}}, fmt=plistlib.FMT_BINARY))
    assert r.status_code == 400 and "Export Library" in r.json()["detail"]
    assert _preview(env, body=ENTITY_XML).status_code == 400
    assert _preview(env, body=b"").status_code == 400
    monkeypatch.setenv(imp.ENV_MAX_MB, "1")
    big = build_export() + b" " * (2 * 1024 * 1024)
    r = _preview(env, body=big)
    assert r.status_code == 413
    # a body streamed without a honest Content-Length is capped too
    def chunks():
        for _ in range(3):
            yield b" " * (1024 * 1024)
    r = env.client.post("/api/import/itunes/preview", content=chunks(), headers=env.admin)
    assert r.status_code == 413
    assert not list(Path(env.config.data_dir).glob("itunes-upload-*"))


# --- audit regressions ---------------------------------------------------------------------------------------

def test_second_concurrent_preview_gets_429(env):
    from trackseerr.api.routes import itunes_import as route

    assert route._PREVIEW_LOCK.acquire(blocking=False)
    try:
        r = _preview(env)
    finally:
        route._PREVIEW_LOCK.release()
    assert r.status_code == 429 and "already being processed" in r.json()["detail"]
    assert not list(Path(env.config.data_dir).glob("itunes-upload-*"))
    assert _preview(env).status_code == 200  # lock was not leaked / is free again
    assert route._PREVIEW_LOCK.acquire(blocking=False)
    route._PREVIEW_LOCK.release()


def test_default_max_is_100_mb(monkeypatch):
    monkeypatch.delenv(imp.ENV_MAX_MB, raising=False)
    assert imp.DEFAULT_MAX_MB == 100 and imp.max_import_bytes() == 100 * 1024 * 1024


def test_apply_mappings_nfd_path_nfc_mapping():
    nfd = unicodedata.normalize("NFD", "/Users/Zo\u00eb/Music/a/b.mp3")
    out = imp.apply_mappings(nfd, [{"from": "/Users/Zo\u00eb/Music", "to": "/musicc"}])
    assert out == "/musicc/a/b.mp3"


def test_apply_mappings_casefold_changes_length():
    out = imp.apply_mappings("C:/Stra\u00dfe/x/y.mp3", [{"from": "C:/STRASSE", "to": "/m"}])
    assert out == "/m/x/y.mp3"


def test_apply_mappings_sibling_directory_boundary():
    maps = [{"from": "C:/Music", "to": "/m"}]
    assert imp.apply_mappings("C:/Music2/a.mp3", maps) is None
    assert imp.apply_mappings("C:/Music/a.mp3", maps) == "/m/a.mp3"
    assert imp.apply_mappings("C:/Music", maps) == "/m"


def test_one_playlist_typeerror_does_not_strand_the_rest(env, monkeypatch):
    original = imp._import_playlist

    def flaky(db, config, parsed, playlist, *a: Any, **kw: Any):
        if playlist["name"] == "Favourites":
            raise TypeError("boom")
        return original(db, config, parsed, playlist, *a, **kw)

    monkeypatch.setattr(imp, "_import_playlist", flaky)
    iid = _preview(env).json()["import_id"]
    _commit(env, iid)
    status = _wait(env, iid)
    states = {p["name"]: p["state"] for p in status["playlists"]}
    assert states["Favourites"] == "failed"
    assert "pending" not in states.values() and status["done"] == status["total"] == 3
    assert status["state"] == "completed"


def test_outer_crash_marks_pending_playlists_failed(env, monkeypatch):
    def crash(*a: Any, **kw: Any):
        raise TypeError("matcher exploded")

    monkeypatch.setattr(imp, "TrackMatcher", crash)
    iid = _preview(env).json()["import_id"]
    _commit(env, iid)
    status = _wait(env, iid)
    assert status["state"] == "failed" and status["error"]
    assert all(p["state"] == "failed" for p in status["playlists"])
    assert status["done"] == status["total"]


def test_thread_start_failure_releases_job(env, monkeypatch):
    iid = _preview(env).json()["import_id"]
    real_start = threading.Thread.start

    def broken(self):
        if self.name.startswith("itunes-import"):
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", broken)
    r = _commit(env, iid)
    assert r.status_code == 500
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert imp.job_status(iid)["state"] == "failed"
    r2 = _commit(env, iid)
    assert r2.status_code == 202
    assert _wait(env, iid)["state"] == "completed"


def test_reimport_preserves_user_edits(env):
    maps = [{"from": "C:/Users/Aaron/iTunes/iTunes Media/Music", "to": "/music"}]
    iid = _preview(env).json()["import_id"]
    _commit(env, iid, path_mappings=maps)
    first = _wait(env, iid)
    fav_id = next(p["created_playlist_id"] for p in first["playlists"] if p["missing"] == 2)
    env.db.set_playlist_enabled(fav_id, False)
    env.db.upsert_playlist(fav_id, name="My Renamed", service="itunes", poster_url="http://x/p.jpg", enabled=False)
    env.db.record_sync_result(fav_id, "imported", missing_tracks=[])
    iid2 = _preview(env).json()["import_id"]
    _commit(env, iid2, path_mappings=maps)
    _wait(env, iid2)
    pl = env.db.get_playlist(fav_id)
    assert pl["name"] == "My Renamed" and pl["poster_url"] == "http://x/p.jpg" and not pl["enabled"]
    assert len(env.db.get_missing_tracks(fav_id)) == 2  # missing refreshed
    assert len(__import__("json").loads(pl["tracks_json"])) == 6  # tracks refreshed
