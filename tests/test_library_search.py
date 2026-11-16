"""Tests for the library-wide search (artists, albums, tracks, close-match suggestion)."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes.library._shared import NATIVE_ONLY_DETAIL
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_search import search_library
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def users(test_db):
    return {
        "admin": test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False),
    }


@pytest.fixture
def api(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_h(test_db, test_config, users):
    return _headers(users["admin"], test_db, test_config)


@pytest.fixture
def alice_h(test_db, test_config, users):
    return _headers(users["alice"], test_db, test_config)


def _seed_zach(db, *, has_file=1):
    db.upsert_library_artist({"id": "ar-z", "name": "Zach Bryan"})
    db.upsert_library_album({"id": "al-z", "artist_id": "ar-z", "title": "American Heartbreak", "year": 2022})
    db.upsert_library_track({"id": "tr-z", "album_id": "al-z", "artist_id": "ar-z", "title": "Sun to Me"})
    db.conn.execute("UPDATE library_tracks SET has_file = ? WHERE id = 'tr-z'", (has_file,))
    db.conn.commit()


def test_search_returns_all_three_kinds(test_db):
    _seed_zach(test_db)
    res = search_library(test_db, "zach")
    assert [a["name"] for a in res["artists"]] == ["Zach Bryan"]
    assert res["artists"][0]["album_count"] == 1
    assert [a["title"] for a in res["albums"]] == ["American Heartbreak"]
    assert res["albums"][0]["artist_name"] == "Zach Bryan"
    assert [t["title"] for t in res["tracks"]] == ["Sun to Me"]
    assert res["tracks"][0]["album_title"] == "American Heartbreak"
    assert res["suggestion"] is None


def test_search_tokens_span_artist_and_title(test_db):
    _seed_zach(test_db)
    res = search_library(test_db, "bryan sun")
    assert [t["id"] for t in res["tracks"]] == ["tr-z"]
    assert res["artists"] == []
    assert res["albums"] == []


def test_search_ranks_exact_then_prefix(test_db):
    for i, name in enumerate(["Below", "Lower Than Atlantis", "Low"]):
        test_db.upsert_library_artist({"id": f"ar-{i}", "name": name})
    res = search_library(test_db, "low")
    assert [a["name"] for a in res["artists"]] == ["Low", "Lower Than Atlantis", "Below"]


def test_search_suggests_close_artist(test_db):
    _seed_zach(test_db)
    res = search_library(test_db, "Zac Brian")
    assert res["artists"] == [] and res["albums"] == [] and res["tracks"] == []
    assert res["suggestion"] == "Zach Bryan"


def test_search_tracks_without_files_are_hidden(test_db):
    _seed_zach(test_db, has_file=0)
    res = search_library(test_db, "sun")
    assert res["tracks"] == []
    test_db.conn.execute("UPDATE library_tracks SET has_file = 1 WHERE id = 'tr-z'")
    test_db.conn.commit()
    assert [t["id"] for t in search_library(test_db, "sun")["tracks"]] == ["tr-z"]


def test_search_empty_after_cleaning(test_db):
    _seed_zach(test_db)
    res = search_library(test_db, "!!!")
    assert res["artists"] == [] and res["suggestion"] is None


def test_search_route_requires_admin_and_validates_q(api, test_db, admin_h, alice_h):
    _seed_zach(test_db)
    assert api.get("/api/library/search", headers=admin_h, params={"q": ""}).status_code == 422
    ok = api.get("/api/library/search", headers=admin_h, params={"q": "zach"})
    assert ok.status_code == 200
    assert ok.json()["artists"][0]["name"] == "Zach Bryan"
    paged = api.get("/api/library/artists/paged", headers=alice_h)
    denied = api.get("/api/library/search", headers=alice_h, params={"q": "zach"})
    assert denied.status_code == paged.status_code
    assert denied.status_code in (401, 403)


def test_search_route_lidarr_mode_conflict(api, test_db, admin_h):
    test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": "lidarr-secret-key-abcdef123456"})
    test_db.update_media_management_settings({"library_mode": "lidarr"})
    res = api.get("/api/library/search", headers=admin_h, params={"q": "zach"})
    assert res.status_code == 409
    assert res.json()["detail"] == NATIVE_ONLY_DETAIL
