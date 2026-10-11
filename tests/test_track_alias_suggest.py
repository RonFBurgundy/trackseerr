"""POST /api/playlists/{id}/tracks/suggest: users suggest the right song for a missing playlist track."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.test_two_tier_security import SECRET, _signed
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_deezer_client, get_spotify_client
from trackseerr.api.routes.sync import sync_state
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import Playlist, Track, UserPermission
from trackseerr.storage import Database

PL_ID = "37i9dQZF1DXcBWIGoYBM5M"
SRC = {"title": "One More Tyme", "artist": "Daft Punk"}
BODY = {
    "source_title": SRC["title"],
    "source_artist": SRC["artist"],
    "discovery_id": "dz-123",
    "target_title": "One More Time",
    "target_artist": "Daft Punk",
    "target_album": "Discovery",
    "note": "typo in the playlist",
}


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    d.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    d.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    d.upsert_user("user-carol", "carol", "carol@plex.tv", is_admin=False)
    d.upsert_playlist(
        playlist_id=PL_ID, name="Mix", service="spotify",
        tracks_json=json.dumps([{"title": SRC["title"], "artist": SRC["artist"], "album": ""}]),
        creator_id="user-alice",
    )
    d.set_playlist_targets(PL_ID, ["user-bob"])
    d.record_sync_result(PL_ID, status="success", missing_tracks=[Track(SRC["title"], SRC["artist"], "")])
    yield d
    d.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        plex_url="", plex_token="", data_dir=str(tmp_path), spotify_client_id="id", spotify_client_secret="s"
    )


@pytest.fixture
def secret_key(tmp_path):
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_spotify_client] = lambda: None
    app.dependency_overrides[get_deezer_client] = lambda: None
    return TestClient(app)


def _cookies(db, uid, username, is_admin, secret_key):
    token = create_session_token(user_id=uid, username=username, is_admin=is_admin, secret_key=secret_key)
    db.create_session(session_id=token, user_id=uid)
    return {"session_token": token}


def _suggest(client, cookies, body=None, playlist_id=PL_ID):
    return client.post(f"/api/playlists/{playlist_id}/tracks/suggest", json=body or BODY, cookies=cookies)


def test_creator_suggests_pending_alias_with_issue(client, db, secret_key):
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    res = _suggest(client, alice)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "pending" and data["issue_id"]
    issue = db.get_issue(data["issue_id"])
    assert issue["issue_type"] == "playlist_match"
    assert issue["discovery_id"] == "dz-123" and issue["item_type"] == "track"
    assert issue["media_title"] == SRC["title"] and issue["artist"] == SRC["artist"]
    assert "Suggested match in playlist 'Mix'" in issue["problem_details"]
    assert "Daft Punk – One More Time (Discovery)" in issue["problem_details"]
    assert "Note: typo in the playlist" in issue["problem_details"]
    alias = db.get_track_alias(data["alias_id"])
    assert alias["status"] == "pending" and alias["issue_id"] == data["issue_id"]
    assert alias["suggested_by"] == "user-alice" and alias["target_title"] == "One More Time"
    tracks = client.get(f"/api/playlists/{PL_ID}/tracks", cookies=alice).json()["tracks"]
    assert tracks[0]["suggestion"] == "pending"


def test_target_user_may_suggest(client, db, secret_key):
    assert _suggest(client, _cookies(db, "user-bob", "bob", False, secret_key)).status_code == 201


def test_unrelated_user_gets_404(client, db, secret_key):
    assert _suggest(client, _cookies(db, "user-carol", "carol", False, secret_key)).status_code == 404
    assert _suggest(client, _cookies(db, "user-alice", "alice", False, secret_key), playlist_id="nope").status_code == 404


def test_user_without_report_issue_permission_gets_403(client, db, secret_key):
    with db._lock:
        db.conn.execute(
            "UPDATE users SET permissions = ? WHERE id = 'user-alice'", (int(UserPermission.REQUEST),)
        )
        db.conn.commit()
    assert _suggest(client, _cookies(db, "user-alice", "alice", False, secret_key)).status_code == 403


def test_unauthenticated_gets_401(client):
    assert _suggest(client, None).status_code == 401


def test_non_missing_track_gets_404(client, db, secret_key):
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    res = _suggest(client, alice, {**BODY, "source_title": "Something Else"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Track is not missing from this playlist"


def test_source_key_matches_normalised(client, db, secret_key):
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    res = _suggest(client, alice, {**BODY, "source_title": "  one more  TYME ", "source_artist": "daft punk"})
    assert res.status_code == 201


def test_approved_alias_conflicts(client, db, secret_key):
    db.upsert_track_alias(SRC["title"], SRC["artist"], "alias", "approved", target_title="X", target_artist="Y")
    assert _suggest(client, _cookies(db, "user-alice", "alice", False, secret_key)).status_code == 409


def test_duplicate_open_issue_conflicts_with_existing_id(client, db, secret_key):
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    first = _suggest(client, alice).json()
    res = _suggest(client, alice)
    assert res.status_code == 409
    assert res.json()["existing_issue_id"] == first["issue_id"]
    assert len(db.list_track_aliases()) == 1


def test_newer_suggestion_from_other_user_replaces_pending(client, db, secret_key):
    first = _suggest(client, _cookies(db, "user-alice", "alice", False, secret_key)).json()
    second = _suggest(
        client, _cookies(db, "user-bob", "bob", False, secret_key), {**BODY, "target_title": "One More Time (Radio)"}
    ).json()
    assert second["alias_id"] == first["alias_id"]
    alias = db.get_track_alias(second["alias_id"])
    assert alias["target_title"] == "One More Time (Radio)" and alias["issue_id"] == second["issue_id"]


def test_validation_rejects_blank_and_control_chars(client, db, secret_key):
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    assert _suggest(client, alice, {**BODY, "target_title": "   "}).status_code == 422
    assert _suggest(client, alice, {**BODY, "note": "bad\x00note"}).status_code == 422
    assert _suggest(client, alice, {**BODY, "discovery_id": ""}).status_code == 422


def test_admin_suggestion_is_approved_without_issue(client, db, secret_key):
    res = _suggest(client, _cookies(db, "admin-1", "admin_user", True, secret_key))
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "approved" and data.get("issue_id") is None
    alias = db.get_track_alias(data["alias_id"])
    assert alias["status"] == "approved" and alias["decided_by"] == "admin-1" and alias["issue_id"] is None
    assert db.list_issues() == []


def _seed_library(db):
    db.upsert_library_artist({"id": "a1", "name": "Daft Punk"})
    db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Discovery"})
    db.upsert_library_track(
        {"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "One More Time", "duration_seconds": 320.0, "track_number": 1}
    )
    db.upsert_library_file(
        {"id": "f-t1", "track_id": "t1", "file_path": "/music/Daft Punk/Discovery/01.mp3",
         "relative_path": "Daft Punk/Discovery/01.mp3", "codec": "mp3", "quality_name": "MP3-320"}
    )


def test_admin_approval_resolves_issue_and_next_sync_matches(client, db, config, secret_key):
    _seed_library(db)
    suggested = _suggest(client, _cookies(db, "user-alice", "alice", False, secret_key)).json()
    admin = _cookies(db, "admin-1", "admin_user", True, secret_key)
    res = client.post(f"/api/missing/aliases/{suggested['alias_id']}/approve", cookies=admin)
    assert res.status_code == 200
    assert db.get_issue(suggested["issue_id"])["status"] == "resolved"

    spotify = MagicMock()
    spotify.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Mix", description="", poster="")
    spotify.get_playlist_tracks.return_value = [Track(SRC["title"], SRC["artist"], "")]
    result = sync_state.execute_sync(db=db, config=config, plex_client=None, spotify_client=spotify, deezer_client=None)
    assert result["status"] == "success"
    assert db.get_missing_tracks(PL_ID) == []


def test_forwarded_principal_can_suggest_and_is_never_admin(db, tmp_path):
    cfg = Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="core",
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251",
    )
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    c = TestClient(app)
    db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    db.set_playlist_targets(PL_ID, ["1001"])
    path = f"/api/playlists/{PL_ID}/tracks/suggest"
    raw = json.dumps(BODY).encode()
    res = c.post(path, content=raw, headers=_signed("POST", path, body=raw))
    assert res.status_code == 201
    assert res.json()["status"] == "pending" and res.json()["issue_id"]
    assert db.get_issue(res.json()["issue_id"])["user_id"] == "1001"
