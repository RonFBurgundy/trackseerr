"""Tests for exclusion flags on library records and refusing monitoring of excluded items."""

from collections.abc import Generator
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    d = Database(str(tmp_path / "test.db"))
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


def test_library_records_carry_exclusion_flags(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art1_id = str(uuid.uuid4())
    alb1_id = str(uuid.uuid4())
    trk1_id = str(uuid.uuid4())

    art2_id = str(uuid.uuid4())
    alb2_id = str(uuid.uuid4())
    trk2_id = str(uuid.uuid4())

    # Excluded artist
    db.upsert_library_artist({"id": art1_id, "name": "Radiohead", "monitored": False})
    db.upsert_library_album({"id": alb1_id, "artist_id": art1_id, "title": "OK Computer", "monitored": False})
    db.upsert_library_track({"id": trk1_id, "album_id": alb1_id, "artist_id": art1_id, "title": "Airbag", "monitored": False})
    db.add_exclusion(scope="artist", artist_name="Radiohead", reason="Excluded artist reason")

    # Non-excluded artist
    db.upsert_library_artist({"id": art2_id, "name": "Coldplay", "monitored": False})
    db.upsert_library_album({"id": alb2_id, "artist_id": art2_id, "title": "Parachutes", "monitored": False})
    db.upsert_library_track({"id": trk2_id, "album_id": alb2_id, "artist_id": art2_id, "title": "Yellow", "monitored": False})

    # 1. get_artist
    res_art1 = client.get(f"/api/library/artists/{art1_id}", headers=headers)
    assert res_art1.status_code == 200
    body_art1 = res_art1.json()
    assert body_art1["excluded"] is True
    assert "Excluded artist reason" in (body_art1["exclusion_reason"] or "")
    # Nested albums under excluded artist should also be flagged as excluded
    assert len(body_art1["albums"]) == 1
    assert body_art1["albums"][0]["excluded"] is True
    assert "Excluded artist reason" in (body_art1["albums"][0]["exclusion_reason"] or "")

    res_art2 = client.get(f"/api/library/artists/{art2_id}", headers=headers)
    assert res_art2.status_code == 200
    body_art2 = res_art2.json()
    assert body_art2["excluded"] is False
    assert body_art2["exclusion_reason"] is None
    assert body_art2["albums"][0]["excluded"] is False

    # 2. list_albums
    res_albs = client.get("/api/library/albums", headers=headers)
    assert res_albs.status_code == 200
    albs = {a["id"]: a for a in res_albs.json()}
    assert albs[alb1_id]["excluded"] is True
    assert albs[alb2_id]["excluded"] is False

    # 3. paged_albums
    res_paged_albs = client.get("/api/library/albums/paged?page=1&page_size=10", headers=headers)
    assert res_paged_albs.status_code == 200
    paged_albs_map = {a["id"]: a for a in res_paged_albs.json()["records"]}
    assert paged_albs_map[alb1_id]["excluded"] is True
    assert paged_albs_map[alb2_id]["excluded"] is False

    # 4. get_album
    res_alb1 = client.get(f"/api/library/albums/{alb1_id}", headers=headers)
    assert res_alb1.status_code == 200
    body_alb1 = res_alb1.json()
    assert body_alb1["excluded"] is True
    assert body_alb1["tracks"][0]["excluded"] is True

    res_alb2 = client.get(f"/api/library/albums/{alb2_id}", headers=headers)
    assert res_alb2.status_code == 200
    body_alb2 = res_alb2.json()
    assert body_alb2["excluded"] is False
    assert body_alb2["tracks"][0]["excluded"] is False

    # 5. paged_tracks
    res_paged_trks = client.get("/api/library/tracks/paged?page=1&page_size=10", headers=headers)
    assert res_paged_trks.status_code == 200
    paged_trks_map = {t["id"]: t for t in res_paged_trks.json()["records"]}
    assert paged_trks_map[trk1_id]["excluded"] is True
    assert paged_trks_map[trk2_id]["excluded"] is False

    # 6. list_tracks
    res_trks = client.get("/api/library/tracks", headers=headers)
    assert res_trks.status_code == 200
    trks_map = {t["id"]: t for t in res_trks.json()}
    assert trks_map[trk1_id]["excluded"] is True
    assert trks_map[trk2_id]["excluded"] is False


def test_monitor_excluded_artist_409(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art_id = str(uuid.uuid4())
    db.upsert_library_artist({"id": art_id, "name": "Radiohead", "monitored": False})
    db.add_exclusion(scope="artist", artist_name="Radiohead", reason="Blocked artist")

    res = client.put(f"/api/library/artists/{art_id}/monitored", json={"monitored": True}, headers=headers)
    assert res.status_code == 409
    assert "Remove the exclusion to monitor it." in res.json()["detail"]


def test_monitor_excluded_album_409(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art_id = str(uuid.uuid4())
    alb_id = str(uuid.uuid4())
    db.upsert_library_artist({"id": art_id, "name": "Radiohead", "monitored": False})
    db.upsert_library_album({"id": alb_id, "artist_id": art_id, "title": "OK Computer", "monitored": False})
    db.add_exclusion(scope="album", artist_name="Radiohead", title="OK Computer", reason="Blocked album")

    res = client.put(f"/api/library/albums/{alb_id}/monitored", json={"monitored": True}, headers=headers)
    assert res.status_code == 409
    assert "Remove the exclusion to monitor it." in res.json()["detail"]


def test_monitor_excluded_track_409(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art_id = str(uuid.uuid4())
    alb_id = str(uuid.uuid4())
    trk_id = str(uuid.uuid4())
    db.upsert_library_artist({"id": art_id, "name": "Radiohead", "monitored": False})
    db.upsert_library_album({"id": alb_id, "artist_id": art_id, "title": "OK Computer", "monitored": False})
    db.upsert_library_track({"id": trk_id, "album_id": alb_id, "artist_id": art_id, "title": "Airbag", "monitored": False})
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Airbag", reason="Blocked track")

    res = client.put(f"/api/library/tracks/{trk_id}/monitored", json={"monitored": True}, headers=headers)
    assert res.status_code == 409
    assert "Remove the exclusion to monitor it." in res.json()["detail"]


def test_unmonitor_excluded_allowed(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art_id = str(uuid.uuid4())
    alb_id = str(uuid.uuid4())
    trk_id = str(uuid.uuid4())
    db.upsert_library_artist({"id": art_id, "name": "Radiohead", "monitored": True})
    db.upsert_library_album({"id": alb_id, "artist_id": art_id, "title": "OK Computer", "monitored": True})
    db.upsert_library_track({"id": trk_id, "album_id": alb_id, "artist_id": art_id, "title": "Airbag", "monitored": True})
    db.add_exclusion(scope="artist", artist_name="Radiohead", reason="Blocked artist")

    # Unmonitoring is always allowed
    res_trk = client.put(f"/api/library/tracks/{trk_id}/monitored", json={"monitored": False}, headers=headers)
    assert res_trk.status_code == 200
    assert res_trk.json()["monitored"] is False

    res_alb = client.put(f"/api/library/albums/{alb_id}/monitored", json={"monitored": False}, headers=headers)
    assert res_alb.status_code == 200
    assert res_alb.json()["monitored"] is False

    res_art = client.put(f"/api/library/artists/{art_id}/monitored", json={"monitored": False}, headers=headers)
    assert res_art.status_code == 200
    assert res_art.json()["monitored"] is False


def test_bulk_monitor_skips_excluded(db: Database, config: Config) -> None:
    client = _client(db, config)
    headers = _headers(db, config, admin=True)

    art_id = str(uuid.uuid4())
    alb_excl_id = str(uuid.uuid4())
    alb_ok_id = str(uuid.uuid4())
    trk_excl_id = str(uuid.uuid4())
    trk_ok_id = str(uuid.uuid4())

    db.upsert_library_artist({"id": art_id, "name": "Radiohead", "monitored": False})
    db.upsert_library_album({"id": alb_excl_id, "artist_id": art_id, "title": "OK Computer", "monitored": False})
    db.upsert_library_album({"id": alb_ok_id, "artist_id": art_id, "title": "The Bends", "monitored": False})
    db.upsert_library_track({"id": trk_excl_id, "album_id": alb_ok_id, "artist_id": art_id, "title": "Airbag", "monitored": False})
    db.upsert_library_track({"id": trk_ok_id, "album_id": alb_ok_id, "artist_id": art_id, "title": "High and Dry", "monitored": False})

    db.add_exclusion(scope="album", artist_name="Radiohead", title="OK Computer")
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Airbag")

    # Bulk edit albums: alb_excl_id is excluded, alb_ok_id is not
    res_alb = client.post(
        "/api/library/albums/bulk-edit",
        json={"album_ids": [alb_excl_id, alb_ok_id], "monitored": True},
        headers=headers,
    )
    assert res_alb.status_code == 200
    data_alb = res_alb.json()
    assert data_alb["albums_updated"] == 1
    assert data_alb["skipped_excluded"] == 1

    # Bulk edit tracks: trk_excl_id is excluded, trk_ok_id is not
    res_trk = client.post(
        "/api/library/tracks/bulk-edit",
        json={"track_ids": [trk_excl_id, trk_ok_id], "monitored": True},
        headers=headers,
    )
    assert res_trk.status_code == 200
    data_trk = res_trk.json()
    assert data_trk["tracks_updated"] == 1
    assert data_trk["skipped_excluded"] == 1
