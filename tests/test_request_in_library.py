"""Requests for items the native library already holds are refused; availability finds compilation and deep-catalog tracks."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes.discovery import annotate_item_statuses
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_availability import get_item_availability
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.request_submission import submit_batch_requests
from trackseerr.storage import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), auto_approve_requests=False)


@pytest.fixture
def client(db, cfg):
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def _headers(db: Database, cfg: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _artist(db: Database, aid: str, name: str) -> None:
    db.upsert_library_artist(LibraryArtist(id=aid, name=name, monitored=True, monitor_option="none"))


def _album(db: Database, alb: str, aid: str, title: str) -> None:
    db.upsert_library_album(LibraryAlbum(id=alb, artist_id=aid, title=title, clean_title=title.lower(), monitored=True))


def _track(db: Database, tid: str, alb: str, aid: str, title: str, n: int = 1, file: bool = True) -> None:
    db.upsert_library_track(LibraryTrack(id=tid, album_id=alb, artist_id=aid, title=title, track_number=n))
    if file:
        db.upsert_library_file(
            LibraryFile(id=f"f-{tid}", track_id=tid, file_path=f"/m/{tid}.mp3", relative_path=f"{tid}.mp3", codec="mp3")
        )


def _post(client, headers, title="Stayin Alive", artist="Bee Gees"):
    return client.post("/api/requests", json={"item_type": "track", "title": title, "artist": artist}, headers=headers)


def test_availability_finds_track_beyond_500_catalog_rows(db):
    _artist(db, "ar1", "Big Catalog")
    _album(db, "alb1", "ar1", "Everything")
    for i in range(600):
        _track(db, f"t{i}", "alb1", "ar1", f"Song {i}", n=i + 1, file=(i == 589))
    res = get_item_availability(db, artist_name="Big Catalog", track_title="Song 589")
    assert res["status"] == "available"
    assert res["in_library"] is True


def test_availability_prefers_same_title_row_with_file(db):
    _artist(db, "ar1", "Band")
    _album(db, "a1", "ar1", "First")
    _album(db, "a2", "ar1", "Deluxe")
    _track(db, "t1", "a1", "ar1", "Hit", file=False)
    _track(db, "t2", "a2", "ar1", "Hit", file=True)
    assert get_item_availability(db, artist_name="Band", track_title="Hit")["status"] == "available"
    # also when the album hint points at the empty edition
    res = get_item_availability(db, artist_name="Band", album_title="First", track_title="Hit")
    assert res["status"] == "available"


def _compilation(db: Database) -> None:
    _artist(db, "va", "Various Artists")
    _album(db, "comp", "va", "Disco Hits")
    _track(db, "ct1", "comp", "va", "Stayin Alive")
    db.replace_track_artists("ct1", [{"artist_name": "Bee Gees", "role": "primary", "source": "tags"}])


def test_availability_matches_compilation_track_by_credit(db):
    _compilation(db)
    res = get_item_availability(db, artist_name="Bee Gees", track_title="Stayin Alive")
    assert res["in_library"] is True
    assert res["status"] == "available"


def test_request_rejected_when_track_in_library(db, cfg, client):
    _compilation(db)
    for who in ("1001", "admin-1"):
        res = _post(client, _headers(db, cfg, who))
        assert res.status_code == 409
        assert res.json()["detail"] == "Already in your library"
    assert db.list_requests() == []


def test_request_allowed_when_track_missing_file(db, cfg, client):
    _artist(db, "ar1", "Bee Gees")
    _album(db, "a1", "ar1", "Spirits")
    _track(db, "t1", "a1", "ar1", "Stayin Alive", file=False)
    assert _post(client, _headers(db, cfg, "1001")).status_code == 201


def test_request_allowed_in_lidarr_mode(db, cfg, client):
    _compilation(db)
    db.update_media_management_settings({"library_mode": "lidarr"})
    assert _post(client, _headers(db, cfg, "1001")).status_code == 201


def test_batch_skips_in_library_items_without_quota(db, cfg):
    _compilation(db)
    db.update_account_settings({"default_quota_tracks": 1})
    alice = db.get_user("1001")
    items: list[dict[str, Any]] = [
        {"item_type": "track", "title": "Stayin Alive", "artist": "Bee Gees"},
        {"item_type": "track", "title": "Jive Talkin", "artist": "Bee Gees"},
    ]
    created = submit_batch_requests(db, cfg, alice, items)
    assert [s.request["title"] for s in created] == ["Jive Talkin"]


def test_discovery_marks_compilation_track_in_library(db, cfg):
    _compilation(db)
    out = annotate_item_statuses(
        [{"id": "x1", "type": "track", "title": "Stayin Alive", "artist": "Bee Gees"}], db=db, config=cfg
    )
    assert out[0]["status"] == "available"
