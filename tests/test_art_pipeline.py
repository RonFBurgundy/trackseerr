"""Lidarr-style art: derivatives pre-generated when art lands, versioned immutable URLs, backfill, pre-cache."""

import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from plex_playlist_sync.api.dependencies import get_discovery_client, get_mbid_enricher
from plex_playlist_sync import art_pipeline, art_thumbs, lidarr_library
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.storage import SCHEMA_VERSION, Database
from tests.test_library_api import (  # noqa: F401  (fixtures + helpers shared with the library API suite)
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

# This module exercises the real schedulers (conftest stubs them for every other test); each test drains its pools.
pytestmark = pytest.mark.real_art_pipeline

IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATED = "private, max-age=86400"


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    base = tmp_path / "cfg"
    monkeypatch.setattr(mediacover_service, "base_dir", base)
    monkeypatch.setattr(mediacover_service, "artists_dir", base / "mediacover" / "artists")
    monkeypatch.setattr(mediacover_service, "albums_dir", base / "mediacover" / "albums")


def _jpeg(path: Path, w=900, h=900, color=(200, 30, 30)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path, "JPEG")


def _settle(db: Database, kind: str, item_id: str) -> dict:
    """Registers the art the route would serve (queued thumbnails and the published version included)."""
    row = db.get_library_artist(item_id) if kind == "artist" else db.get_library_album(item_id)
    art_pipeline.register_served_art(db, kind, row, db.get_media_management_settings())
    assert art_thumbs.wait_idle(5)
    return db.get_library_artist(item_id) if kind == "artist" else db.get_library_album(item_id)


def _thumb_files(tmp_path) -> list[Path]:
    d = tmp_path / "cfg" / "mediacover" / "thumbs"
    return sorted(d.glob("*.jpg")) if d.exists() else []


@pytest.fixture
def album(test_db, tmp_path):
    d = tmp_path / "music" / "Band" / "Alb"
    _jpeg(d / "cover.jpg")
    test_db.update_media_management_settings({"root_folder_path": str(tmp_path / "music")})
    art = test_db.upsert_library_artist({"id": "art-p", "name": "Band", "path": str(d.parent)})
    alb = test_db.upsert_library_album(
        {"id": "alb-p", "artist_id": art["id"], "title": "Alb", "path": str(d), "cover_url": "/api/library/albums/alb-p/cover"}
    )
    return alb, d / "cover.jpg"


# ------------------------------------------------------------------------------------------------ migration


def test_migration_adds_art_version_columns_and_is_idempotent(tmp_path):
    assert SCHEMA_VERSION >= 47
    db = Database(str(tmp_path / "m.db"))
    try:
        for table in ("library_artists", "library_albums"):
            cols = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
            assert "art_version" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        with db._lock:
            cur = db.conn.cursor()
            db._migration_v47(cur)  # re-running must not fail on the existing column
    finally:
        db.close()


def test_migration_upgrades_a_v46_database_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    db = Database(str(path))
    db.upsert_library_artist({"id": "a1", "name": "Keep"})
    db.conn.execute("ALTER TABLE library_artists DROP COLUMN art_version")
    db.conn.execute("ALTER TABLE library_albums DROP COLUMN art_version")
    db.conn.execute("DELETE FROM schema_migrations WHERE version >= 47")
    db.conn.commit()
    db.close()
    db = Database(str(path))
    try:
        assert db.get_library_artist("a1")["art_version"] is None
        assert db.set_library_art_version("artist", "a1", "abc") is True
        assert db.set_library_art_version("artist", "a1", "abc") is False  # unchanged -> no write
        assert db.get_library_artist("a1")["art_version"] == "abc"
    finally:
        db.close()


# ------------------------------------------------------------------------------------------------ pre-generation


def test_derivatives_generated_when_remote_art_is_cached_and_no_lazy_resize_afterwards(
    app_and_client, test_db, test_config, seeded_users, tmp_path
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    art = test_db.upsert_library_artist({"id": "art-r", "name": "R"})
    test_db.upsert_library_album(
        {"id": "alb-r", "artist_id": art["id"], "title": "Remote", "cover_url": "https://img.example/c.jpg"}
    )

    def fake_cache(self, target, url, *a, **kw):
        _jpeg(target)
        return True

    with patch("plex_playlist_sync.mediacover.MediaCoverService.cache_image", fake_cache):
        art_pipeline.cache_remote_art(
            test_db, "album", test_db.get_library_album("alb-r"), test_db.get_media_management_settings()
        )
        assert mediacover_service.wait_idle(5)
    assert len(_thumb_files(tmp_path)) == 2  # 250 + 500, made by the download hook
    version = test_db.get_library_album("alb-r")["art_version"]
    assert version

    with patch.object(art_thumbs, "_resize", wraps=art_thumbs._resize) as spy:
        for size in (250, 500):
            r = client.get(f"/api/library/albums/alb-r/cover?v={version}&size={size}", headers=h)
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert spy.call_count == 0


def test_scanner_registers_folder_art_version_and_thumbs(test_db, album, tmp_path):
    alb, cover = album
    library_scanner._register_art(test_db, "album", alb, test_db.get_media_management_settings())
    assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == art_thumbs.art_version(cover)
    assert art_thumbs.has_all_thumbs(cover, art_pipeline.thumb_dir())


# ------------------------------------------------------------------------------------------------ backfill


def test_backfill_is_idempotent_and_resumable(test_db, album, tmp_path):
    alb, cover = album
    _jpeg(mediacover_service.get_artist_poster_path("art-p"))
    first = art_pipeline.backfill(test_db, delay=0)
    assert first["scanned"] >= 2 and first["generated"] == 2 and first["versioned"] == 2
    before = {p: p.stat().st_mtime_ns for p in _thumb_files(tmp_path)}
    assert len(before) == 4

    with patch.object(art_thumbs, "_resize") as resize:
        second = art_pipeline.backfill(test_db, delay=0)
    assert resize.call_count == 0
    assert second["generated"] == 0 and second["versioned"] == 0
    assert {p: p.stat().st_mtime_ns for p in _thumb_files(tmp_path)} == before

    # Resumable: lose one derivative and one version, the next pass repairs only that.
    _thumb_files(tmp_path)[0].unlink()
    test_db.set_library_art_version("album", alb["id"], None)
    third = art_pipeline.backfill(test_db, delay=0)
    assert third["generated"] == 1 and third["versioned"] == 1
    assert len(_thumb_files(tmp_path)) == 4


def test_backfill_stops_when_asked(test_db, album):
    assert art_pipeline.backfill(test_db, delay=0, should_stop=lambda: True)["scanned"] == 0


def test_backfill_task_is_registered_and_dispatchable(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    tasks = client.get("/api/system/tasks", headers=h).json()
    assert "art_thumbnail_backfill" in {t["id"] for t in tasks}
    with patch.object(art_pipeline, "backfill", return_value={"scanned": 0, "generated": 0, "versioned": 0}) as bf:
        r = client.post("/api/system/tasks/art_thumbnail_backfill/run", headers=h)
        assert r.status_code == 200
        import time

        for _ in range(100):
            if bf.called:
                break
            time.sleep(0.02)
    assert bf.called


# ------------------------------------------------------------------------------------------------ pre-cache


def test_precache_downloads_artist_image_and_every_album_cover(test_db, tmp_path):
    test_db.upsert_library_artist({"id": "pa", "name": "P", "image_url": "https://img.example/a.jpg"})
    for i in range(3):
        test_db.upsert_library_album(
            {"id": f"pb{i}", "artist_id": "pa", "title": f"T{i}", "cover_url": f"https://img.example/{i}.jpg"}
        )
    test_db.upsert_library_album({"id": "local", "artist_id": "pa", "title": "L", "cover_url": "/api/library/albums/local/cover"})
    seen: list[str] = []

    def fake_cache(self, target, url, *a, **kw):
        seen.append(url)
        _jpeg(target)
        return True

    with patch("plex_playlist_sync.mediacover.MediaCoverService.cache_image", fake_cache):
        fut = art_pipeline.schedule_precache(test_db, "pa", delay=0)
        assert fut is not None and fut.result(timeout=10) == 4
    ours = [u for u in seen if u.startswith("https://img.example/")]  # assert only on this test's own downloads
    assert sorted(ours) == sorted(
        ["https://img.example/a.jpg", *(f"https://img.example/{i}.jpg" for i in range(3))]
    )
    assert test_db.get_library_artist("pa")["art_version"]
    assert all(test_db.get_library_album(f"pb{i}")["art_version"] for i in range(3))
    assert art_thumbs.wait_idle(5) and len(_thumb_files(tmp_path)) == 8


def test_ingest_and_refresh_schedule_precache(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    discovery, enricher = MagicMock(), MagicMock()  # no real Deezer / MusicBrainz in tests
    discovery.get_artist_details.return_value = None
    discovery.get_artist_albums.return_value = []
    app.dependency_overrides[get_discovery_client] = lambda: discovery
    app.dependency_overrides[get_mbid_enricher] = lambda: enricher
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.upsert_library_artist({"id": "ra", "name": "Refreshable", "foreign_artist_id": "deezer:1", "mbid": "m-1"})
    with patch.object(art_pipeline, "schedule_precache") as sched:
        client.post("/api/library/artists/ra/refresh", headers=h)
    assert sched.called and sched.call_args.args[1] == "ra"


# ------------------------------------------------------------------------------------------------ versions in payloads


def test_payloads_carry_v_and_it_changes_when_art_changes(
    app_and_client, test_db, test_config, seeded_users, album
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, cover = album
    # Unversioned until the art has been seen: the stored URL is returned untouched.
    r = client.get("/api/library/albums", headers=h).json()
    rec = next(x for x in (r if isinstance(r, list) else r["records"]) if x["id"] == alb["id"])
    assert rec["cover_url"] == "/api/library/albums/alb-p/cover"

    shutil.copy2(cover, Path(test_db.get_library_artist("art-p")["path"]) / "artist.jpg")  # same mtime+size -> same token
    _settle(test_db, "album", alb["id"])
    _settle(test_db, "artist", "art-p")
    v1 = art_thumbs.art_version(cover)
    expected = f"/api/library/albums/alb-p/cover?v={v1}"
    page = client.get("/api/library/albums", params={"page": 1}, headers=h).json()
    recs = page if isinstance(page, list) else page["records"]
    assert next(x for x in recs if x["id"] == alb["id"])["cover_url"] == expected
    assert client.get("/api/library/albums/alb-p", headers=h).json()["cover_url"] == expected
    detail = client.get("/api/library/artists/art-p", headers=h).json()
    assert detail["albums"][0]["cover_url"] == expected
    assert detail["image_url"] == f"/api/library/artists/art-p/image?v={v1}"
    artists = client.get("/api/library/artists", headers=h).json()
    arecs = artists if isinstance(artists, list) else artists["records"]
    assert next(x for x in arecs if x["id"] == "art-p")["image_url"].endswith(f"?v={v1}")

    _jpeg(cover, w=700, h=700, color=(10, 200, 10))  # replaced art: new size and mtime
    os.utime(cover, ns=(cover.stat().st_atime_ns, cover.stat().st_mtime_ns + 5_000_000_000))
    _settle(test_db, "album", alb["id"])
    v2 = art_thumbs.art_version(cover)
    assert v2 != v1
    assert client.get("/api/library/albums/alb-p", headers=h).json()["cover_url"].endswith(f"?v={v2}")


# ------------------------------------------------------------------------------------------------ cache headers


def test_immutable_header_only_when_v_matches_the_served_file(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, cover = album
    url = f"/api/library/albums/{alb['id']}/cover"
    good = art_thumbs.art_version(cover)
    with_v = client.get(url, params={"size": 250, "v": good}, headers=h)
    assert with_v.status_code == 200 and with_v.headers["cache-control"] == IMMUTABLE
    assert "etag" in with_v.headers
    original = client.get(url, params={"v": good}, headers=h)
    assert original.headers["cache-control"] == IMMUTABLE
    without = client.get(url, params={"size": 250}, headers=h)
    assert without.headers["cache-control"] == REVALIDATED and "etag" in without.headers
    revalidated = client.get(url, params={"size": 250, "v": good}, headers={**h, "If-None-Match": with_v.headers["etag"]})
    assert revalidated.status_code == 304 and revalidated.headers["cache-control"] == IMMUTABLE
    junk = client.get(url, params={"size": 250, "v": "bad value;<x>"}, headers=h)
    assert junk.headers["cache-control"] == REVALIDATED  # malformed token never earns "immutable"


def test_well_formed_but_mismatching_v_never_gets_immutable(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, cover = album
    url = f"/api/library/albums/{alb['id']}/cover"
    stale = "1-1"  # well-formed, but not the version of the file on disk
    for params in ({"size": 250, "v": stale}, {"v": stale}, {"size": 500, "v": "abc123-1f"}):
        r = client.get(url, params=params, headers=h)
        assert r.status_code == 200 and r.headers["cache-control"] == REVALIDATED and "etag" in r.headers
    r304 = client.get(url, params={"size": 250, "v": stale}, headers={**h, "If-None-Match": r.headers["etag"]})
    assert r304.headers["cache-control"] == REVALIDATED
    # The file is replaced: the old token stops being immutable, the new one starts.
    old = art_thumbs.art_version(cover)
    _jpeg(cover, w=700, h=700, color=(10, 200, 10))
    os.utime(cover, ns=(cover.stat().st_atime_ns, cover.stat().st_mtime_ns + 5_000_000_000))
    assert client.get(url, params={"v": old}, headers=h).headers["cache-control"] == REVALIDATED
    assert client.get(url, params={"v": art_thumbs.art_version(cover)}, headers=h).headers["cache-control"] == IMMUTABLE


def test_artist_image_immutable_only_with_matching_v(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    img = Path(test_db.get_library_artist("art-p")["path"]) / "artist.jpg"
    _jpeg(img)
    v = art_thumbs.art_version(img)
    r = client.get("/api/library/artists/art-p/image", params={"size": 250, "v": v}, headers=h)
    assert r.status_code == 200 and r.headers["cache-control"] == IMMUTABLE
    r = client.get("/api/library/artists/art-p/image", params={"size": 250, "v": "x1"}, headers=h)
    assert r.headers["cache-control"] == REVALIDATED
    r = client.get("/api/library/artists/art-p/image", params={"size": 250}, headers=h)
    assert r.headers["cache-control"] == REVALIDATED


# ------------------------------------------------------------------------------------------------ lidarr tokens


def _lidarr_album(last_write: str, info_sync: str = "2024-01-01T00:00:00Z") -> dict:
    return {
        "id": 7, "title": "A", "artistId": 3, "lastInfoSync": info_sync, "monitored": True,
        "images": [{"coverType": "cover", "url": f"/MediaCover/Albums/7/cover.jpg?lastWrite={last_write}"}],
    }


def test_lidarr_records_carry_a_version_that_changes_with_the_cover():
    a = lidarr_library.album_row(_lidarr_album("638000000000000000")).record["cover_url"]
    b = lidarr_library.album_row(_lidarr_album("638000000000000001")).record["cover_url"]
    c = lidarr_library.album_row(_lidarr_album("638000000000000000", "2025-02-02T00:00:00Z")).record["cover_url"]
    again = lidarr_library.album_row(_lidarr_album("638000000000000000")).record["cover_url"]
    assert a.startswith("/api/library/albums/7/cover?v=") and a == again
    assert len({a, b, c}) == 3
    artist = lidarr_library.artist_row(
        {"id": 3, "artistName": "X", "images": [{"coverType": "poster", "url": "/MediaCover/3/poster.jpg?lastWrite=5"}]}
    ).record
    assert artist["image_url"].startswith("/api/library/artists/3/image?v=")
    bare = lidarr_library.album_row({"id": 8, "title": "N", "images": []}).record
    assert bare["cover_url"] is None


def test_frontend_contract_size_is_appended_with_ampersand():
    # artSrc.ts: base.includes('?') ? '&' : '?' -- guard the exact rule so a versioned URL keeps its token.
    src = (Path(__file__).resolve().parents[1] / "frontend/src/components/library/artSrc.ts").read_text()
    assert "base.includes('?') ? '&' : '?'" in src


# ------------------------------------------------------------------------------------------------ served-file resolver


def _settings(db: Database, prefer_local: bool) -> dict:
    db.update_media_management_settings({"prefer_local_artwork": prefer_local})
    return db.get_media_management_settings()


def test_resolver_follows_prefer_local_artwork_and_version_follows_the_served_file(
    app_and_client, test_db, test_config, seeded_users, album
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, local = album
    _jpeg(mediacover_service.get_album_cover_path(alb["id"]), w=300, h=300, color=(1, 2, 3))
    cached = mediacover_service.get_album_cover_path(alb["id"])
    assert art_thumbs.art_version(local) != art_thumbs.art_version(cached)

    _settings(test_db, True)
    assert art_pipeline.resolve_served_art("album", alb, test_db.get_media_management_settings(), test_db) == local
    assert client.get(f"/api/library/albums/{alb['id']}/cover", headers=h).status_code == 200
    assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == art_thumbs.art_version(local)

    _settings(test_db, False)
    assert art_pipeline.resolve_served_art("album", alb, test_db.get_media_management_settings(), test_db) == cached
    assert client.get(f"/api/library/albums/{alb['id']}/cover", headers=h).status_code == 200
    assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == art_thumbs.art_version(cached)

    _settings(test_db, True)  # and back
    client.get(f"/api/library/albums/{alb['id']}/cover", headers=h)
    assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == art_thumbs.art_version(local)


def test_resolver_rejects_unapproved_paths(test_db, album):
    alb, _ = album
    with patch(
        "plex_playlist_sync.api.routes.library._shared.validate_media_path",
        side_effect=__import__("fastapi").HTTPException(status_code=403, detail="no"),
    ):
        assert art_pipeline.resolve_served_art("album", alb, {"prefer_local_artwork": True}, test_db) is None


@pytest.mark.parametrize("prefer_local", [True, False])
def test_scan_then_image_request_leaves_art_version_stable(
    app_and_client, test_db, test_config, seeded_users, album, prefer_local
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, _local = album
    _jpeg(mediacover_service.get_album_cover_path(alb["id"]), w=300, h=300, color=(1, 2, 3))
    settings = _settings(test_db, prefer_local)

    library_scanner._register_art(test_db, "album", test_db.get_library_album(alb["id"]), settings)
    assert art_thumbs.wait_idle(5)
    after_scan = test_db.get_library_album(alb["id"])["art_version"]
    assert after_scan
    with patch.object(test_db, "set_library_art_version", wraps=test_db.set_library_art_version) as setter:
        assert client.get(f"/api/library/albums/{alb['id']}/cover?size=250", headers=h).status_code == 200
        assert art_thumbs.wait_idle(5)
        library_scanner._register_art(test_db, "album", test_db.get_library_album(alb["id"]), settings)
        assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == after_scan
    assert setter.call_count == 0  # neither side rewrote the stored token


def test_register_publishes_version_only_after_thumbnails_exist(test_db, album):
    alb, cover = album
    gate = threading.Event()
    real = art_thumbs.pregenerate

    def slow(src, cache_dir, stop=None):
        gate.wait(5)
        return real(src, cache_dir, stop)

    with patch.object(art_thumbs, "pregenerate", slow):
        art_pipeline.register_served_art(test_db, "album", alb, test_db.get_media_management_settings())
        assert test_db.get_library_album(alb["id"])["art_version"] is None  # queued, nothing published yet
        gate.set()
        assert art_thumbs.wait_idle(5)
    assert test_db.get_library_album(alb["id"])["art_version"] == art_thumbs.art_version(cover)


def test_pregen_backlog_drop_is_logged_and_leaves_version_unpublished(test_db, album, caplog):
    alb, _ = album
    with patch.object(art_thumbs, "_PREGEN_MAX_PENDING", 0), caplog.at_level("INFO", logger=art_thumbs.logger.name):
        art_pipeline.register_served_art(test_db, "album", alb, test_db.get_media_management_settings())
    assert any("backlog full" in r.getMessage() and r.levelname == "INFO" for r in caplog.records)
    assert test_db.get_library_album(alb["id"])["art_version"] is None
    row = _settle(test_db, "album", alb["id"])  # the next request heals it
    assert row["art_version"]


# ------------------------------------------------------------------------------------------------ request-time guard


def test_cached_art_requests_schedule_no_jobs(test_db, album):
    alb, _ = album
    _settle(test_db, "album", alb["id"])
    _jpeg(mediacover_service.get_album_cover_path(alb["id"]))
    settings = _settings(test_db, False)  # the cache file is the served one
    row = _settle(test_db, "album", alb["id"])
    with patch.object(art_thumbs, "schedule_pregenerate") as sched, patch.object(
        test_db, "set_library_art_version"
    ) as setter:
        for _ in range(100):
            assert art_pipeline.cache_remote_art(test_db, "album", row, settings) is not None
            assert art_pipeline.serve_art(test_db, "album", row, settings) is not None
    assert sched.call_count == 0 and setter.call_count == 0


# ------------------------------------------------------------------------------------------------ shutdown


def test_shutdown_returns_promptly_with_a_long_queue(test_db):
    for i in range(300):
        test_db.upsert_library_artist({"id": f"q{i}", "name": f"Q{i}"})
    release = threading.Event()

    def slow_cache(self, target, url, *a, **kw):
        release.wait(5)
        return False

    test_db.upsert_library_album({"id": "qa", "artist_id": "q0", "title": "T", "cover_url": "https://img.example/x.jpg"})
    with patch("plex_playlist_sync.mediacover.MediaCoverService.cache_image", slow_cache):
        for i in range(300):
            art_pipeline.schedule_precache(test_db, f"q{i}", delay=0.5)
        assert len(art_pipeline._precache_pending) <= art_pipeline._PRECACHE_MAX_PENDING
        for i in range(2000):
            art_thumbs.schedule_pregenerate(Path(f"/nonexistent/{i}.jpg"), Path("/nonexistent"))
        started = time.monotonic()
        art_pipeline.shutdown()
        elapsed = time.monotonic() - started
        release.set()
    assert elapsed < 1.0
    assert art_pipeline._precache_executor is None and art_thumbs._pregen_executor is None
    assert not art_pipeline._precache_pending and not art_thumbs._pregen_pending


def test_precache_dedupes_per_artist_and_caps_pending_with_a_log(test_db, caplog):
    release = threading.Event()
    with patch.object(art_pipeline, "_precache_artist", lambda *a, **kw: release.wait(5) or 0), patch.object(
        art_pipeline, "_PRECACHE_MAX_PENDING", 3
    ), caplog.at_level("INFO", logger=art_pipeline.logger.name):
        futs = [art_pipeline.schedule_precache(test_db, f"a{i}") for i in range(3)]
        assert all(f is not None for f in futs)
        assert art_pipeline.schedule_precache(test_db, "a0") is None  # deduped
        assert art_pipeline.schedule_precache(test_db, "overflow") is None  # capped
        assert any("backlog full" in r.getMessage() for r in caplog.records)
        release.set()
        for f in futs:
            f.result(timeout=5)
    assert not art_pipeline._precache_pending
    art_pipeline.shutdown()


def test_stop_event_ends_a_running_precache_between_items(test_db):
    test_db.upsert_library_artist({"id": "sa", "name": "S", "image_url": "https://img.example/a.jpg"})
    for i in range(5):
        test_db.upsert_library_album({"id": f"sb{i}", "artist_id": "sa", "title": f"T{i}", "cover_url": f"https://img.example/{i}.jpg"})
    stop = threading.Event()
    calls: list[str] = []

    def fake_cache(self, target, url, *a, **kw):
        calls.append(url)
        stop.set()
        _jpeg(target)
        return True

    with patch("plex_playlist_sync.mediacover.MediaCoverService.cache_image", fake_cache):
        assert art_pipeline._precache_artist(test_db, "sa", 0, stop) == 1
    assert len(calls) == 1


# ------------------------------------------------------------------------------------------------ mediacover callbacks


def test_second_schedule_with_callback_fires_while_first_is_queued(tmp_path):
    from plex_playlist_sync.mediacover import MediaCoverService

    svc = MediaCoverService(base_dir=tmp_path / "mc")
    gate = threading.Event()
    fired: list[str] = []

    def fake_cache(target, url, *a, **kw):
        gate.wait(5)
        _jpeg(Path(target))
        return True

    target = svc.get_album_cover_path("dup")
    with patch.object(svc, "cache_image", fake_cache), patch.object(svc, "_should_skip", return_value=False):
        assert svc.schedule_cache(target, "https://img.example/1.jpg") is True  # no callback
        assert svc.schedule_cache(target, "https://img.example/1.jpg", lambda p: fired.append("b")) is False
        assert svc.schedule_cache(target, "https://img.example/1.jpg", lambda p: fired.append("c")) is False
        gate.set()
        assert svc.wait_idle(5)
    assert fired == ["b", "c"]
