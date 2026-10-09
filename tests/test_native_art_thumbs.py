"""Native library art: ?size=250|500 cached JPEG thumbnails, private revalidated caching, strong ETag + 304."""

import io
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import pytest

from trackseerr import art_thumbs
from trackseerr.mediacover import mediacover_service
from tests.test_library_api import (  # noqa: F401  (fixtures + helpers shared with the library API suite)
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

IMMUTABLE = "private, max-age=86400"  # name kept for the call sites below; the policy is private + 1 day, never immutable


@pytest.fixture(autouse=True)
def _thumb_dir(tmp_path, monkeypatch, test_db):
    test_db.update_media_management_settings({"root_folder_path": str(tmp_path / "music")})
    monkeypatch.setattr(mediacover_service, "base_dir", tmp_path / "cfg")


def _big_jpeg(path: Path, w=2400, h=2400) -> None:
    Image.effect_noise((w, h), 60).convert("RGB").save(path, "JPEG", quality=95)


@pytest.fixture
def album(test_db, tmp_path):
    d = tmp_path / "music" / "Band" / "Alb"
    d.mkdir(parents=True)
    _big_jpeg(d / "cover.jpg")
    art = test_db.upsert_library_artist({"id": "art-t", "name": "Band"})
    alb = test_db.upsert_library_album({"id": "alb-t", "artist_id": art["id"], "title": "Alb", "path": str(d)})
    return alb, d / "cover.jpg"


def _get(client, hdrs, album_id, **params):
    return client.get(f"/api/library/albums/{album_id}/cover", params=params, headers=hdrs)


def test_thumbnail_generated_small_with_headers(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, original = album
    t0 = time.perf_counter()
    r = _get(client, h, alb["id"], size=250)
    first = time.perf_counter() - t0
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content))
    assert max(img.size) == 250
    assert len(r.content) < original.stat().st_size / 10
    assert r.headers["cache-control"] == IMMUTABLE
    etag = r.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    t0 = time.perf_counter()
    assert _get(client, h, alb["id"], size=250).status_code == 200
    print(f"first={first * 1000:.0f}ms cached={(time.perf_counter() - t0) * 1000:.0f}ms")
    r500 = _get(client, h, alb["id"], size=500)
    assert max(Image.open(io.BytesIO(r500.content)).size) == 500
    assert r500.headers["etag"] != etag


def test_cache_hit_does_not_resize_again(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, _ = album
    with patch.object(art_thumbs, "_resize", wraps=art_thumbs._resize) as spy:
        assert _get(client, h, alb["id"], size=250).status_code == 200
        assert _get(client, h, alb["id"], size=250).status_code == 200
    assert spy.call_count == 1


def test_if_none_match_returns_304_without_resize(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, _ = album
    etag = _get(client, h, alb["id"], size=250).headers["etag"]
    with patch.object(art_thumbs, "_resize") as spy:
        r = client.get(f"/api/library/albums/{alb['id']}/cover?size=250", headers={**h, "If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""
    assert r.headers["etag"] == etag and r.headers["cache-control"] == IMMUTABLE
    spy.assert_not_called()


@pytest.mark.parametrize("params", [{}, {"size": 999}, {"size": 0}, {"size": -5}])
def test_invalid_or_missing_size_serves_original(app_and_client, test_db, test_config, seeded_users, album, params):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, original = album
    with patch.object(art_thumbs, "_resize") as spy:
        r = _get(client, h, alb["id"], **params)
    assert r.status_code == 200 and r.content == original.read_bytes()
    assert r.headers["cache-control"] == IMMUTABLE
    assert "immutable" not in r.headers["cache-control"] and "public" not in r.headers["cache-control"]
    assert r.headers["etag"] == art_thumbs.original_etag(original)
    spy.assert_not_called()


def test_changed_original_regenerates_and_prunes(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, original = album
    e1 = _get(client, h, alb["id"], size=250).headers["etag"]
    _big_jpeg(original, 1800, 1200)
    r = _get(client, h, alb["id"], size=250)
    assert r.headers["etag"] != e1
    assert max(Image.open(io.BytesIO(r.content)).size) == 250
    thumbs = list((mediacover_service.base_dir / "mediacover" / "thumbs").glob("*-250.jpg"))
    assert len(thumbs) == 1


def test_corrupt_original_falls_back_to_original(app_and_client, test_db, test_config, seeded_users, tmp_path):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    d = tmp_path / "music" / "Bad" / "Alb"
    d.mkdir(parents=True)
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0not really a jpeg")
    art = test_db.upsert_library_artist({"id": "art-bad", "name": "Bad"})
    alb = test_db.upsert_library_album({"id": "alb-bad", "artist_id": art["id"], "title": "Alb", "path": str(d)})
    r = _get(client, h, alb["id"], size=250)
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8\xff\xe0not")


def test_artist_image_thumbnail(app_and_client, test_db, test_config, seeded_users, tmp_path):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    d = tmp_path / "music" / "Solo"
    d.mkdir(parents=True)
    _big_jpeg(d / "artist.jpg")
    art = test_db.upsert_library_artist({"id": "art-solo", "name": "Solo", "path": str(d)})
    r = client.get(f"/api/library/artists/{art['id']}/image?size=500", headers=h)
    assert r.status_code == 200 and max(Image.open(io.BytesIO(r.content)).size) == 500
    assert r.headers["cache-control"] == IMMUTABLE
    r2 = client.get(f"/api/library/artists/{art['id']}/image?size=500", headers={**h, "If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304


def test_original_gets_strong_etag_and_304_without_reading_or_resizing(
    app_and_client, test_db, test_config, seeded_users, album
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, original = album
    r = _get(client, h, alb["id"])
    etag = r.headers["etag"]
    assert r.status_code == 200 and not etag.startswith("W/")
    with patch.object(art_thumbs, "_resize") as spy:
        r2 = _get(client, {**h, "If-None-Match": etag}, alb["id"])
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == etag
    assert r2.headers["cache-control"] == IMMUTABLE
    spy.assert_not_called()


def test_replaced_original_changes_etag(app_and_client, test_db, test_config, seeded_users, album):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb, original = album
    old = _get(client, h, alb["id"]).headers["etag"]
    _big_jpeg(original, 1200, 1200)
    r = _get(client, {**h, "If-None-Match": old}, alb["id"])
    assert r.status_code == 200 and r.headers["etag"] != old


def test_artist_banner_is_private_with_etag_and_304(app_and_client, test_db, test_config, seeded_users, tmp_path):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    d = tmp_path / "music" / "Banner"
    d.mkdir(parents=True)
    _big_jpeg(d / "banner.jpg", 600, 200)
    art = test_db.upsert_library_artist({"id": "art-ban", "name": "Banner", "path": str(d)})
    r = client.get(f"/api/library/artists/{art['id']}/banner", headers=h)
    assert r.status_code == 200 and r.headers["cache-control"] == IMMUTABLE
    r2 = client.get(f"/api/library/artists/{art['id']}/banner", headers={**h, "If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
