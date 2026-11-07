"""Tests for the exclusions admin API (trackseerr/api/routes/exclusions.py)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, require_core_tier
from trackseerr.api.routes import exclusions
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    d = Database(str(tmp_path / "exclusions.db"))
    (tmp_path / "music").mkdir(exist_ok=True)
    d.update_media_management_settings({"root_folder_path": str(tmp_path / "music"), "library_mode": "native"})
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db: Database, config: Config, *, admin: bool = True) -> dict[str, str]:
    user = db.upsert_user("a-1" if admin else "u-1", "admin_user" if admin else "user", "u@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def test_list_empty(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)
    res = client.get("/api/exclusions", headers=headers)
    assert res.status_code == 200
    assert res.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_create_by_names_and_list(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)
    res = client.post(
        "/api/exclusions",
        json={"scope": "artist", "artist_name": "Radiohead", "reason": "No Thom"},
        headers=headers,
    )
    assert res.status_code == 201
    data = res.json()
    assert data["item"]["scope"] == "artist"
    assert data["item"]["artist_name"] == "Radiohead"
    assert data["item"]["reason"] == "No Thom"
    assert data["item"]["created_by"] == "admin_user"
    assert data["unmonitored"] == 0

    list_res = client.get("/api/exclusions", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1
    assert list_res.json()["items"][0]["id"] == data["item"]["id"]


def test_create_is_idempotent(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)
    res1 = client.post(
        "/api/exclusions",
        json={"scope": "artist", "artist_name": "Radiohead"},
        headers=headers,
    )
    assert res1.status_code == 201
    item1 = res1.json()["item"]
    assert item1["mbid"] is None

    res2 = client.post(
        "/api/exclusions",
        json={"scope": "artist", "artist_name": "Radiohead", "mbid": "mbid-radiohead"},
        headers=headers,
    )
    assert res2.status_code == 201
    item2 = res2.json()["item"]
    assert item2["id"] == item1["id"]
    assert item2["mbid"] == "mbid-radiohead"
    assert res2.json()["unmonitored"] == 0


def test_create_from_library_artist_unmonitors_artist_and_albums(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    db.upsert_library_artist({"id": "101", "name": "Pink Floyd", "monitored": True, "foreign_artist_id": "mbid-pf"})
    db.upsert_library_album({"id": "201", "artist_id": "101", "title": "The Wall", "monitored": True})
    db.upsert_library_album({"id": "202", "artist_id": "101", "title": "Animals", "monitored": True})
    db.upsert_library_track({"id": "301", "album_id": "201", "artist_id": "101", "title": "Hey You", "monitored": True})
    db.upsert_library_track({"id": "302", "album_id": "201", "artist_id": "101", "title": "Mother", "monitored": True})
    db.upsert_library_track({"id": "303", "album_id": "202", "artist_id": "101", "title": "Dogs", "monitored": True})
    db.upsert_library_track({"id": "304", "album_id": "202", "artist_id": "101", "title": "Pigs", "monitored": True})

    res = client.post("/api/exclusions", json={"library_artist_id": 101}, headers=headers)
    assert res.status_code == 201
    data = res.json()
    assert data["unmonitored"] == 7
    assert data["item"]["scope"] == "artist"
    assert data["item"]["artist_name"] == "Pink Floyd"
    assert data["item"]["mbid"] == "mbid-pf"

    assert db.get_library_artist("101")["monitored"] is False
    assert db.get_library_album("201")["monitored"] is False
    assert db.get_library_album("202")["monitored"] is False
    assert db.get_library_track("301")["monitored"] is False
    assert db.get_library_track("302")["monitored"] is False
    assert db.get_library_track("303")["monitored"] is False
    assert db.get_library_track("304")["monitored"] is False


def test_create_from_library_album_unmonitors_album_tracks(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    db.upsert_library_artist({"id": "102", "name": "Pink Floyd", "monitored": True})
    db.upsert_library_album({"id": "203", "artist_id": "102", "title": "Meddle", "monitored": True, "foreign_album_id": "mbid-meddle"})
    db.upsert_library_track({"id": "305", "album_id": "203", "artist_id": "102", "title": "One of These Days", "monitored": True})
    db.upsert_library_track({"id": "306", "album_id": "203", "artist_id": "102", "title": "Echoes", "monitored": True})

    res = client.post("/api/exclusions", json={"library_album_id": 203}, headers=headers)
    assert res.status_code == 201
    data = res.json()
    assert data["unmonitored"] == 3
    assert data["item"]["scope"] == "album"
    assert data["item"]["artist_name"] == "Pink Floyd"
    assert data["item"]["title"] == "Meddle"
    assert data["item"]["mbid"] == "mbid-meddle"

    assert db.get_library_artist("102")["monitored"] is True
    assert db.get_library_album("203")["monitored"] is False
    assert db.get_library_track("305")["monitored"] is False
    assert db.get_library_track("306")["monitored"] is False


def test_create_from_library_track_unmonitors_track(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    db.upsert_library_artist({"id": "103", "name": "Pink Floyd", "monitored": True})
    db.upsert_library_album({"id": "204", "artist_id": "103", "title": "Wish You Were Here", "monitored": True})
    db.upsert_library_track({"id": "307", "album_id": "204", "artist_id": "103", "title": "Shine On", "monitored": True, "mb_recording_id": "rec-shine"})

    res = client.post("/api/exclusions", json={"library_track_id": 307}, headers=headers)
    assert res.status_code == 201
    data = res.json()
    assert data["unmonitored"] == 1
    assert data["item"]["scope"] == "track"
    assert data["item"]["artist_name"] == "Pink Floyd"
    assert data["item"]["title"] == "Shine On"
    assert data["item"]["mbid"] == "rec-shine"

    assert db.get_library_artist("103")["monitored"] is True
    assert db.get_library_album("204")["monitored"] is True
    assert db.get_library_track("307")["monitored"] is False


def test_create_from_missing_track_marks_applied(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    db.upsert_playlist("pl-1", "My Playlist")
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO missing_tracks (playlist_id, title, artist, album) VALUES (?, ?, ?, ?)",
            ("pl-1", "Creep", "Radiohead", "Pablo Honey"),
        )
        db.conn.commit()
        mt_id = cur.lastrowid

    res = client.post("/api/exclusions", json={"missing_track_id": mt_id}, headers=headers)
    assert res.status_code == 201
    data = res.json()
    assert data["item"]["scope"] == "track"
    assert data["item"]["artist_name"] == "Radiohead"
    assert data["item"]["title"] == "Creep"
    assert data["item"]["mbid"] is None

    missing = db.get_missing_track(mt_id)
    assert missing is not None
    assert missing["list_applied_at"] is not None


def test_create_unknown_ref_404(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    assert client.post("/api/exclusions", json={"library_artist_id": 999999}, headers=headers).status_code == 404
    assert client.post("/api/exclusions", json={"library_album_id": 999999}, headers=headers).status_code == 404
    assert client.post("/api/exclusions", json={"library_track_id": 999999}, headers=headers).status_code == 404
    assert client.post("/api/exclusions", json={"missing_track_id": 999999}, headers=headers).status_code == 404


def test_create_bad_names_422(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    res1 = client.post("/api/exclusions", json={"scope": "artist", "artist_name": "!!!"}, headers=headers)
    assert res1.status_code == 422
    assert "Artist name must not be empty" in res1.text

    res2 = client.post("/api/exclusions", json={"scope": "album", "artist_name": "Radiohead", "title": "   "}, headers=headers)
    assert res2.status_code == 422
    assert "Title required" in res2.text

    res3 = client.post("/api/exclusions", json={"scope": "track", "artist_name": "Radiohead", "title": ""}, headers=headers)
    assert res3.status_code == 422


def test_create_requires_names_or_one_ref_422(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    assert client.post("/api/exclusions", json={}, headers=headers).status_code == 422
    assert client.post("/api/exclusions", json={"library_artist_id": 1, "library_album_id": 2}, headers=headers).status_code == 422
    assert client.post("/api/exclusions", json={"artist_name": "Radiohead", "library_artist_id": 1}, headers=headers).status_code == 422
    assert client.post("/api/exclusions", json={"title": "Song", "library_track_id": 1}, headers=headers).status_code == 422
    assert client.post("/api/exclusions", json={"artist_name": "Radiohead"}, headers=headers).status_code == 422


def test_delete_and_404(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    res = client.post("/api/exclusions", json={"scope": "artist", "artist_name": "Nirvana"}, headers=headers)
    assert res.status_code == 201
    excl_id = res.json()["item"]["id"]

    del_res = client.delete(f"/api/exclusions/{excl_id}", headers=headers)
    assert del_res.status_code == 204

    del_res2 = client.delete(f"/api/exclusions/{excl_id}", headers=headers)
    assert del_res2.status_code == 404

    del_res3 = client.delete("/api/exclusions/excl-nonexistent", headers=headers)
    assert del_res3.status_code == 404


def test_delete_does_not_remonitor(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    db.upsert_library_artist({"id": "105", "name": "Nirvana", "monitored": True})
    res = client.post("/api/exclusions", json={"library_artist_id": 105}, headers=headers)
    assert res.status_code == 201
    excl_id = res.json()["item"]["id"]
    assert db.get_library_artist("105")["monitored"] is False

    del_res = client.delete(f"/api/exclusions/{excl_id}", headers=headers)
    assert del_res.status_code == 204
    assert db.get_library_artist("105")["monitored"] is False


def test_check_endpoint(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    res1 = client.get("/api/exclusions/check?artist=Radiohead", headers=headers)
    assert res1.status_code == 200
    assert res1.json() == {"excluded": False, "reason": None}

    client.post(
        "/api/exclusions",
        json={"scope": "artist", "artist_name": "Radiohead", "reason": "Band on hiatus"},
        headers=headers,
    )

    res2 = client.get("/api/exclusions/check?artist=Radiohead", headers=headers)
    assert res2.status_code == 200
    assert res2.json()["excluded"] is True
    assert res2.json()["reason"] == 'Excluded artist "Radiohead": Band on hiatus'

    client.post(
        "/api/exclusions",
        json={"scope": "album", "artist_name": "Radiohead", "title": "The Bends"},
        headers=headers,
    )

    res3 = client.get("/api/exclusions/check?artist=Radiohead&album=The Bends", headers=headers)
    assert res3.status_code == 200
    assert res3.json()["excluded"] is True


def test_search_and_scope_filters(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    client.post("/api/exclusions", json={"scope": "artist", "artist_name": "Radiohead"}, headers=headers)
    client.post("/api/exclusions", json={"scope": "album", "artist_name": "Pink Floyd", "title": "The Wall"}, headers=headers)
    client.post("/api/exclusions", json={"scope": "track", "artist_name": "Daft Punk", "title": "Get Lucky"}, headers=headers)

    res_scope = client.get("/api/exclusions?scope=artist", headers=headers)
    assert res_scope.status_code == 200
    assert res_scope.json()["total"] == 1
    assert res_scope.json()["items"][0]["artist_name"] == "Radiohead"

    res_search1 = client.get("/api/exclusions?search=lucky", headers=headers)
    assert res_search1.status_code == 200
    assert res_search1.json()["total"] == 1
    assert res_search1.json()["items"][0]["title"] == "Get Lucky"

    res_search2 = client.get("/api/exclusions?search=pink", headers=headers)
    assert res_search2.status_code == 200
    assert res_search2.json()["total"] == 1
    assert res_search2.json()["items"][0]["artist_name"] == "Pink Floyd"


def test_paging(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    for i in range(5):
        client.post("/api/exclusions", json={"scope": "artist", "artist_name": f"Band {i}"}, headers=headers)

    res1 = client.get("/api/exclusions?page=1&page_size=2", headers=headers)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["total"] == 5
    assert len(data1["items"]) == 2
    assert data1["page"] == 1
    assert data1["page_size"] == 2

    res2 = client.get("/api/exclusions?page=2&page_size=2", headers=headers)
    assert res2.status_code == 200
    data2 = res2.json()
    assert len(data2["items"]) == 2
    assert data2["page"] == 2

    res3 = client.get("/api/exclusions?page=3&page_size=2", headers=headers)
    assert res3.status_code == 200
    data3 = res3.json()
    assert len(data3["items"]) == 1
    assert data3["page"] == 3


def test_non_admin_forbidden(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=False)

    assert client.get("/api/exclusions", headers=headers).status_code == 403
    assert client.post("/api/exclusions", json={"scope": "artist", "artist_name": "Test"}, headers=headers).status_code == 403
    assert client.get("/api/exclusions/check?artist=Test", headers=headers).status_code == 403
    assert client.delete("/api/exclusions/excl-1", headers=headers).status_code == 403


def test_requests_tier_blocked(db: Database, config: Config) -> None:
    # 1. exclusions router has require_core_tier in dependencies
    dep_calls = [d.dependency for d in exclusions.router.dependencies]
    assert require_core_tier in dep_calls

    # 2. require_core_tier raises 403 on gateway role
    gateway_cfg = Config(plex_url="http://x", plex_token="t", role="gateway", internal_core_secret="s" * 40)
    with pytest.raises(Exception) as exc_info:
        require_core_tier(gateway_cfg)
    assert getattr(exc_info.value, "status_code", None) == 403

    # 3. Client calls on gateway tier return 403 or 404
    client = _client(db, config)
    client.app.dependency_overrides[get_config] = lambda: gateway_cfg
    headers = _headers(db, config, admin=True)
    assert client.get("/api/exclusions", headers=headers).status_code in (403, 404)
    assert client.post("/api/exclusions", json={"scope": "artist", "artist_name": "Test"}, headers=headers).status_code in (403, 404)
    assert client.get("/api/exclusions/check?artist=Test", headers=headers).status_code in (403, 404)
    assert client.delete("/api/exclusions/excl-1", headers=headers).status_code in (403, 404)
