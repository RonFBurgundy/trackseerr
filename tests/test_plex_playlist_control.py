"""Tests for Plex Playlist Control: registry storage, overwrite protection, REST API, mixes and sync refresh."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from plexapi.exceptions import BadRequest, NotFound

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.api.routes.sync import sync_state
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.plex import (
    PlaylistProtectedError,
    PlexClient,
    classify_playlist_owner,
)
from trackseerr.config import Config
from trackseerr.models import Playlist, Track
from trackseerr.storage import Database


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def make_track(key, title, artist="Artist", album="Album", item_id=None, ttype="track"):
    return SimpleNamespace(
        ratingKey=key,
        title=title,
        grandparentTitle=artist,
        parentTitle=album,
        duration=1000,
        playlistItemID=item_id if item_id is not None else key * 10,
        type=ttype,
    )


def make_playlist(key, title, items=None, smart=False, ptype="audio"):
    pl = MagicMock()
    pl.ratingKey = key
    pl.title = title
    pl.smart = smart
    pl.playlistType = ptype
    pl.leafCount = len(items or [])
    pl.duration = 5000
    pl.updatedAt = None
    pl.summary = ""
    pl.items.return_value = list(items or [])
    return pl


class FakeServer:
    def __init__(self, name="srv", playlists=None, tracks=None, sections=None):
        self.name = name
        self.pls = list(playlists or [])
        self.tracks = {t.ratingKey: t for t in (tracks or [])}
        self.library = MagicMock()
        self.library.sections.return_value = sections or []
        self.created = []

    def playlists(self, playlistType=None):
        return list(self.pls)

    def playlist(self, title):
        for p in self.pls:
            if p.title == title:
                return p
        raise NotFound("no such playlist")

    def fetchItem(self, key):
        if isinstance(key, str) and key.startswith("/playlists/"):
            rk = int(key.rsplit("/", 1)[1])
            for p in self.pls:
                if p.ratingKey == rk:
                    return p
            raise NotFound("playlist missing")
        if key in self.tracks:
            return self.tracks[key]
        raise NotFound("item missing")

    def createPlaylist(self, title, items=None):
        pl = make_playlist(900 + len(self.created), title, items=items)
        self.created.append(pl)
        self.pls.append(pl)
        return pl


def make_client(admin_server, users=None, admin_name="admin"):
    """Build a PlexClient whose server is a fake. ``users`` maps username -> FakeServer for switchUser."""
    users = users or {}
    with patch("trackseerr.clients.plex.PlexServer"):
        client = PlexClient("http://plex", "tok")
    account = SimpleNamespace(
        username=admin_name,
        id=1,
        email="",
        thumb="",
        users=lambda: [SimpleNamespace(username=u, id=i + 2, email="", thumb="") for i, u in enumerate(users)],
    )
    admin_server.myPlexAccount = lambda: account

    def switch(username):
        if username not in users:
            raise NotFound("not a home user")
        return users[username]

    admin_server.switchUser = switch
    client.server = admin_server
    return client


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="http://plex", plex_token="tok", data_dir=str(tmp_path))


@pytest.fixture
def users(db):
    return {
        "admin": db.upsert_user("u-admin", "admin", "a@x.io", is_admin=True),
        "alice": db.upsert_user("u-alice", "alice", "al@x.io", is_admin=False),
        "bob": db.upsert_user("u-bob", "bob", "b@x.io", is_admin=False),
    }


def headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def build_app(db, config, client):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_plex_client] = lambda: client
    return TestClient(app)


@pytest.fixture
def env(db, config, users):
    """Admin server with one regular playlist, one smart playlist and library tracks; alice has her own server."""
    t1, t2, t3 = make_track(1, "One"), make_track(2, "Two"), make_track(3, "Three")
    regular = make_playlist(10, "Road Trip", items=[t1, t2])
    smart = make_playlist(11, "Plexamp Smart", items=[t3], smart=True)
    video = make_playlist(12, "Movies", ptype="video")
    admin_srv = FakeServer("admin", playlists=[regular, smart, video], tracks=[t1, t2, t3])
    alice_srv = FakeServer("alice", playlists=[make_playlist(20, "Mine", items=[t1])], tracks=[t1, t2, t3])
    client = make_client(admin_srv, users={"alice": alice_srv})
    tc = build_app(db, config, client)
    return SimpleNamespace(
        tc=tc, client=client, admin=admin_srv, alice=alice_srv, regular=regular, smart=smart, tracks=(t1, t2, t3)
    )


# ---------------------------------------------------------------------------
# Migration + storage
# ---------------------------------------------------------------------------


def test_migration_v24_creates_tables(db):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "plex_playlist_registry" in names
    assert "plex_mix_snapshots" in names
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 24


def test_registry_upsert_preserves_owner_and_ignored(db):
    db.upsert_plex_registry("Alice", "5", "A", "regular", "user", ignored=True)
    db.upsert_plex_registry("alice", "5", "A2", "regular", "trackseerr", update_owner=False)
    row = db.get_plex_registry_row("ALICE", "5")
    assert row["owner"] == "user" and row["ignored"] is True and row["title"] == "A2"
    assert db.prune_plex_registry("alice", []) == 1
    assert db.get_plex_registry_row("alice", "5") is None


# ---------------------------------------------------------------------------
# Ownership classification
# ---------------------------------------------------------------------------


def _legacy_row(db, pid, name, user_id, synced=True, created="2024-01-01 00:00:00"):
    db.upsert_playlist(pid, name=name, service="spotify")
    db.set_playlist_targets(pid, [user_id])
    db.conn.execute(
        "UPDATE playlists SET created_at = ?, last_synced_at = ? WHERE id = ?",
        (created, "2024-02-01 00:00:00" if synced else None, pid),
    )
    db.conn.commit()


def test_classification_rules(db, users):
    assert classify_playlist_owner(db, "alice", "Anything", smart=True) == "plexamp"
    assert classify_playlist_owner(db, "alice", "Hand Made", smart=False) == "user"
    # Legacy adoption: a TrackSeerr playlist with this name targeting alice
    _legacy_row(db, "sp1", "Legacy Mix", "u-alice")
    assert classify_playlist_owner(db, "Alice", "Legacy Mix", smart=False) == "trackseerr"
    # Same name but bob is not a target
    assert classify_playlist_owner(db, "bob", "Legacy Mix", smart=False) == "user"
    # Smart wins over a legacy name match
    assert classify_playlist_owner(db, "alice", "Legacy Mix", smart=True) == "plexamp"


def test_classification_requires_synced_and_not_older_than_row(db, users):
    _legacy_row(db, "never", "Never", "u-alice", synced=False)
    assert classify_playlist_owner(db, "alice", "Never", smart=False) == "user"
    _legacy_row(db, "old", "Old", "u-alice", created="2024-01-01 00:00:00")
    assert classify_playlist_owner(db, "alice", "Old", False, added_at=datetime(2023, 1, 1)) == "user"
    assert classify_playlist_owner(db, "alice", "Old", False, added_at=datetime(2024, 6, 1)) == "trackseerr"


def test_inventory_classifies_and_filters_audio(env, db, config, users):
    _legacy_row(db, "sp1", "Road Trip", "u-admin")
    resp = env.tc.get("/api/plex-playlists", headers=headers(users["admin"], db, config))
    assert resp.status_code == 200
    body = {p["title"]: p for p in resp.json()}
    assert set(body) == {"Road Trip", "Plexamp Smart"}  # video playlist filtered
    assert body["Road Trip"]["owner"] == "trackseerr" and body["Road Trip"]["kind"] == "regular"
    assert body["Plexamp Smart"]["owner"] == "plexamp" and body["Plexamp Smart"]["kind"] == "smart"
    assert body["Road Trip"]["track_count"] == 2
    assert body["Road Trip"]["thumb_url"] is None
    assert body["Road Trip"]["plex_user"] == "admin"


def test_inventory_prunes_vanished_and_ignored_filter(env, db, config, users):
    h = headers(users["admin"], db, config)
    db.upsert_plex_registry("admin", "999", "Gone", "regular", "user")
    env.tc.get("/api/plex-playlists", headers=h)
    assert db.get_plex_registry_row("admin", "999") is None
    resp = env.tc.put("/api/plex-playlists/10/flags", json={"ignored": True}, headers=h)
    assert resp.status_code == 200 and resp.json()["ignored"] is True
    assert [p["rating_key"] for p in env.tc.get("/api/plex-playlists", headers=h).json()] == ["11"]
    shown = env.tc.get("/api/plex-playlists?include_ignored=true", headers=h).json()
    assert {p["rating_key"] for p in shown} == {"10", "11"}


# ---------------------------------------------------------------------------
# Sync overwrite protection
# ---------------------------------------------------------------------------


def _sync(client, db, name, usernames):
    track = make_track(77, "New Song")
    client.match_playlist_tracks = MagicMock(return_value=([track], []))
    pl = Playlist(id="p", name=name, tracks=[Track("New Song", "A", "B")])
    return client.sync_playlist_to_users(pl, usernames, db=db), track


def test_sync_user_owned_playlist_untouched(db):
    mine = make_playlist(30, "Favorites")
    srv = FakeServer("admin", playlists=[mine])
    client = make_client(srv)
    db.upsert_plex_registry("admin", "30", "Favorites", "regular", "user")
    results, _ = _sync(client, db, "Favorites", ["admin"])
    assert results[0].success is False
    assert results[0].error == "Protected: 'Favorites' is owned by user in admin's profile"
    mine.removeItems.assert_not_called()
    mine.addItems.assert_not_called()


def test_sync_smart_playlist_untouched_even_without_db(db):
    smart = make_playlist(31, "Smartie", smart=True)
    client = make_client(FakeServer("admin", playlists=[smart]))
    results, _ = _sync(client, None, "Smartie", ["admin"])
    assert results[0].success is False and "plexamp" in results[0].error
    smart.removeItems.assert_not_called()
    smart.addItems.assert_not_called()


def test_sync_claims_legacy_playlist(db, users):
    _legacy_row(db, "sp-legacy", "Legacy", "u-admin")
    legacy = make_playlist(32, "Legacy")
    legacy.addedAt = datetime.now() + timedelta(days=1)
    client = make_client(FakeServer("admin", playlists=[legacy]))
    results, track = _sync(client, db, "Legacy", ["admin"])
    assert results[0].success is True
    legacy.addItems.assert_called_once_with([track])
    assert db.get_plex_registry_row("admin", "32")["owner"] == "trackseerr"


def test_sync_does_not_overwrite_handmade_same_name_on_never_synced_row(db, users):
    _legacy_row(db, "sp-new", "Handmade", "u-admin", synced=False)
    mine = make_playlist(40, "Handmade")
    client = make_client(FakeServer("admin", playlists=[mine]))
    results, _ = _sync(client, db, "Handmade", ["admin"])
    assert results[0].success is False and results[0].error.startswith("Protected:")
    mine.removeItems.assert_not_called()
    mine.addItems.assert_not_called()
    assert db.get_plex_registry_row("admin", "40")["owner"] == "user"


def test_sync_does_not_claim_playlist_older_than_row(db, users):
    _legacy_row(db, "sp-old", "Older", "u-admin", created="2024-01-01 00:00:00")
    older = make_playlist(41, "Older")
    older.addedAt = datetime(2020, 1, 1)
    client = make_client(FakeServer("admin", playlists=[older]))
    results, _ = _sync(client, db, "Older", ["admin"])
    assert results[0].success is False and results[0].error.startswith("Protected:")
    older.removeItems.assert_not_called()
    older.addItems.assert_not_called()


def test_sync_refreshes_registry_once_per_user_before_writing(db):
    client = make_client(FakeServer("admin"), users={"alice": FakeServer("alice")})
    with patch.object(client, "refresh_playlist_registry", wraps=client.refresh_playlist_registry) as spy:
        _sync(client, db, "Fresh", ["admin", "alice"])
    assert [c.args[1] for c in spy.call_args_list] == ["admin", "alice"]


def test_update_or_create_with_db_requires_username(db):
    client = make_client(FakeServer("admin"))
    with pytest.raises(ValueError):
        client.update_or_create_playlist("X", [], db=db)


def test_get_admin_username_failure_logged_as_warning(caplog):
    srv = FakeServer("admin")
    client = make_client(srv)
    srv.myPlexAccount = MagicMock(side_effect=BadRequest("boom-401"))
    with caplog.at_level("WARNING"):
        assert client._get_admin_username() == ""
    assert any(r.levelname == "WARNING" and "boom-401" in r.getMessage() for r in caplog.records)


def test_sync_creates_new_and_registers(db):
    srv = FakeServer("admin")
    client = make_client(srv)
    results, _ = _sync(client, db, "Fresh", ["admin"])
    assert results[0].success is True and len(srv.created) == 1
    row = db.get_plex_registry_row("admin", str(srv.created[0].ratingKey))
    assert row["owner"] == "trackseerr"


def test_sync_switches_user_for_non_admin(db):
    alice_srv = FakeServer("alice")
    client = make_client(FakeServer("admin"), users={"alice": alice_srv})
    results, _ = _sync(client, db, "Fresh", ["alice"])
    assert results[0].success is True and len(alice_srv.created) == 1


def test_update_or_create_without_db_keeps_legacy_behaviour():
    existing = make_playlist(33, "Plain")
    client = make_client(FakeServer("admin", playlists=[existing]))
    client.update_or_create_playlist("Plain", [make_track(1, "x")], add_description=False, add_poster=False)
    existing.removeItems.assert_called_once()
    existing.addItems.assert_called_once()


def test_update_or_create_raises_protected_for_plexamp_row(db):
    pl = make_playlist(34, "X")
    client = make_client(FakeServer("admin", playlists=[pl]))
    db.upsert_plex_registry("admin", "34", "X", "regular", "plexamp")
    with pytest.raises(PlaylistProtectedError):
        client.update_or_create_playlist("X", [], db=db, username="admin")


# ---------------------------------------------------------------------------
# Permissions and status codes
# ---------------------------------------------------------------------------


def test_non_admin_targeting_other_user_is_404(env, db, config, users):
    h = headers(users["alice"], db, config)
    assert env.tc.get("/api/plex-playlists?user=admin", headers=h).status_code == 404
    assert env.tc.get("/api/plex-playlists/10/items?user=bob", headers=h).status_code == 404
    assert env.tc.delete("/api/plex-playlists/10?user=admin", headers=h).status_code == 404
    r = env.tc.post("/api/plex-playlists/20/copy", json={"target_users": ["admin"]}, headers=h)
    assert r.status_code == 403


def test_non_admin_default_user_is_self(env, db, config, users):
    h = headers(users["alice"], db, config)
    resp = env.tc.get("/api/plex-playlists", headers=h)
    assert resp.status_code == 200
    assert [p["title"] for p in resp.json()] == ["Mine"]
    assert resp.json()[0]["plex_user"] == "alice"


def test_users_endpoint(env, db, config, users):
    admin_list = env.tc.get("/api/plex-playlists/users", headers=headers(users["admin"], db, config)).json()
    assert {u["username"] for u in admin_list} == {"admin", "alice"}
    assert next(u for u in admin_list if u["username"] == "admin")["is_self"] is True
    assert next(u for u in admin_list if u["username"] == "admin")["is_admin_account"] is True
    alice_list = env.tc.get("/api/plex-playlists/users", headers=headers(users["alice"], db, config)).json()
    assert alice_list == [{"username": "alice", "is_admin_account": False, "is_self": True}]


def test_admin_unknown_user_404_and_switch_failure_502(env, db, config, users):
    h = headers(users["admin"], db, config)
    assert env.tc.get("/api/plex-playlists?user=nobody", headers=h).status_code == 404
    # bob is a TrackSeerr non-admin whose switchUser fails (not in Plex Home)
    resp = env.tc.get("/api/plex-playlists", headers=headers(users["bob"], db, config))
    assert resp.status_code == 502
    assert "not a home user" in resp.json()["detail"]


def test_admin_can_manage_another_user(env, db, config, users):
    resp = env.tc.get("/api/plex-playlists?user=alice", headers=headers(users["admin"], db, config))
    assert resp.status_code == 200 and resp.json()[0]["title"] == "Mine"


def test_503_without_plex(db, config, users):
    tc = build_app(db, config, None)
    h = headers(users["admin"], db, config)
    assert tc.get("/api/plex-playlists", headers=h).status_code == 503
    assert tc.get("/api/plex-playlists/users", headers=h).status_code == 503
    assert tc.get("/api/plex-playlists/mixes", headers=h).status_code == 503
    assert tc.get("/api/plex-playlists/mixes/snapshots", headers=h).status_code == 503


def test_requires_auth(env):
    assert env.tc.get("/api/plex-playlists").status_code == 401


def test_smart_playlist_track_edits_409(env, db, config, users):
    h = headers(users["admin"], db, config)
    base = "/api/plex-playlists/11"
    assert env.tc.post(f"{base}/items", json={"track_rating_keys": ["1"]}, headers=h).status_code == 409
    assert env.tc.delete(f"{base}/items/30", headers=h).status_code == 409
    assert env.tc.post(f"{base}/items/30/move", json={"after_playlist_item_id": None}, headers=h).status_code == 409
    assert env.tc.put(f"{base}/flags", json={"owner": "user"}, headers=h).status_code == 409
    env.smart.addItems.assert_not_called()
    env.smart.removeItems.assert_not_called()
    env.smart.moveItem.assert_not_called()
    # Rename, ignore flag and delete are still allowed on smart playlists
    assert env.tc.patch(f"{base}", json={"title": "Renamed Smart"}, headers=h).status_code == 200
    assert env.tc.put(f"{base}/flags", json={"ignored": True}, headers=h).status_code == 200


def test_missing_playlist_404(env, db, config, users):
    h = headers(users["admin"], db, config)
    assert env.tc.get("/api/plex-playlists/404/items", headers=h).status_code == 404
    assert env.tc.get("/api/plex-playlists/12/items", headers=h).status_code == 404
    assert env.tc.get("/api/plex-playlists/abc/items", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# Rename / delete / add / remove / move
# ---------------------------------------------------------------------------


def test_items_listing(env, db, config, users):
    resp = env.tc.get("/api/plex-playlists/10/items", headers=headers(users["admin"], db, config))
    assert resp.status_code == 200
    assert resp.json() == [
        {"playlist_item_id": 10, "rating_key": "1", "title": "One", "artist": "Artist", "album": "Album", "duration_ms": 1000},
        {"playlist_item_id": 20, "rating_key": "2", "title": "Two", "artist": "Artist", "album": "Album", "duration_ms": 1000},
    ]


def test_rename_updates_plex_and_registry(env, db, config, users):
    h = headers(users["admin"], db, config)
    env.regular.edit.side_effect = lambda title: setattr(env.regular, "title", title)
    resp = env.tc.patch("/api/plex-playlists/10", json={"title": "  New Name "}, headers=h)
    assert resp.status_code == 200
    env.regular.edit.assert_called_once_with(title="New Name")
    assert resp.json()["title"] == "New Name"
    assert db.get_plex_registry_row("admin", "10")["title"] == "New Name"
    assert env.tc.patch("/api/plex-playlists/10", json={"title": "   "}, headers=h).status_code == 422


def test_delete_removes_registry(env, db, config, users):
    h = headers(users["admin"], db, config)
    env.tc.get("/api/plex-playlists", headers=h)
    assert db.get_plex_registry_row("admin", "10") is not None
    resp = env.tc.delete("/api/plex-playlists/10", headers=h)
    assert resp.status_code == 204
    env.regular.delete.assert_called_once()
    assert db.get_plex_registry_row("admin", "10") is None


def test_add_items(env, db, config, users):
    h = headers(users["admin"], db, config)
    resp = env.tc.post("/api/plex-playlists/10/items", json={"track_rating_keys": ["3"]}, headers=h)
    assert resp.status_code == 200
    env.regular.addItems.assert_called_once_with([env.tracks[2]])
    assert env.tc.post("/api/plex-playlists/10/items", json={"track_rating_keys": ["999"]}, headers=h).status_code == 404
    assert env.tc.post("/api/plex-playlists/10/items", json={"track_rating_keys": ["x"]}, headers=h).status_code == 422


def test_remove_item(env, db, config, users):
    h = headers(users["admin"], db, config)
    assert env.tc.delete("/api/plex-playlists/10/items/20", headers=h).status_code == 204
    env.regular.removeItems.assert_called_once_with([env.tracks[1]])
    assert env.tc.delete("/api/plex-playlists/10/items/999", headers=h).status_code == 404


def test_move_item(env, db, config, users):
    h = headers(users["admin"], db, config)
    resp = env.tc.post("/api/plex-playlists/10/items/20/move", json={"after_playlist_item_id": None}, headers=h)
    assert resp.status_code == 200
    env.regular.moveItem.assert_called_once_with(env.tracks[1])
    env.regular.moveItem.reset_mock()
    resp = env.tc.post("/api/plex-playlists/10/items/10/move", json={"after_playlist_item_id": 20}, headers=h)
    assert resp.status_code == 200
    env.regular.moveItem.assert_called_once_with(env.tracks[0], after=env.tracks[1])
    assert env.tc.post("/api/plex-playlists/10/items/10/move", json={"after_playlist_item_id": 555}, headers=h).status_code == 404


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def test_copy_to_other_user_and_collision(env, db, config, users):
    h = headers(users["admin"], db, config)
    # alice already has a hand-made "Mine"; copying a playlist titled "Mine" must fail for her only
    env.regular.title = "Mine"
    resp = env.tc.post("/api/plex-playlists/10/copy", json={"target_users": ["alice", "admin"]}, headers=h)
    assert resp.status_code == 200
    by_user = {r["username"]: r for r in resp.json()}
    assert by_user["alice"]["success"] is False
    assert by_user["alice"]["error"] == "Protected: 'Mine' is owned by user in alice's profile"
    assert env.alice.pls[0].addItems.call_count == 0
    # copying into the admin profile collides with the source itself (a user-owned playlist)
    assert by_user["admin"]["success"] is False
    assert by_user["admin"]["error"] == "Protected: 'Mine' is owned by user in admin's profile"


def test_copy_creates_trackseerr_registered_copy(env, db, config, users):
    h = headers(users["admin"], db, config)
    resp = env.tc.post(
        "/api/plex-playlists/10/copy", json={"target_users": ["alice"], "title": "Road Trip Copy"}, headers=h
    )
    assert resp.status_code == 200
    result = resp.json()[0]
    assert result["success"] is True and result["rating_key"]
    assert len(env.alice.created) == 1 and env.alice.created[0].title == "Road Trip Copy"
    assert db.get_plex_registry_row("alice", result["rating_key"])["owner"] == "trackseerr"


def test_copy_smart_playlist_as_static_snapshot(env, db, config, users):
    h = headers(users["admin"], db, config)
    resp = env.tc.post("/api/plex-playlists/11/copy", json={"target_users": ["alice"]}, headers=h)
    assert resp.json()[0]["success"] is True
    assert env.alice.created[0].title == "Plexamp Smart"


def test_copy_unknown_target_reports_error(env, db, config, users):
    resp = env.tc.post(
        "/api/plex-playlists/10/copy", json={"target_users": ["ghost"]}, headers=headers(users["admin"], db, config)
    )
    assert resp.status_code == 200
    assert resp.json()[0]["success"] is False
    assert resp.json()[0]["error"] == "Unknown Plex user 'ghost'"


def test_non_admin_can_copy_into_own_profile(env, db, config, users):
    h = headers(users["alice"], db, config)
    resp = env.tc.post("/api/plex-playlists/20/copy", json={"target_users": ["alice"], "title": "Mine 2"}, headers=h)
    assert resp.status_code == 200 and resp.json()[0]["success"] is True


# ---------------------------------------------------------------------------
# Adopt + sync refresh
# ---------------------------------------------------------------------------


def test_adopt_creates_plex_playlist_row(env, db, config, users):
    h = headers(users["admin"], db, config)
    resp = env.tc.post("/api/plex-playlists/10/adopt", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "plex_admin_10" and body["service"] == "plex" and body["name"] == "Road Trip"
    assert body["creator_id"] == "u-admin" and body["targets"] == []
    assert json.loads(body["tracks_json"]) == [
        {"title": "One", "artist": "Artist", "album": "Album"},
        {"title": "Two", "artist": "Artist", "album": "Album"},
    ]
    row = db.get_plex_registry_row("admin", "10")
    assert row["trackseerr_playlist_id"] == "plex_admin_10"
    assert row["owner"] == "user"  # owner unchanged: the source is a source, not a target


def test_adopt_cannot_overwrite_row_owned_by_someone_else(env, db, config, users):
    """An admin adopts alice's playlist; alice re-adopting must 404 and leave the row untouched."""
    admin_h = headers(users["admin"], db, config)
    assert env.tc.post("/api/plex-playlists/20/adopt?user=alice", headers=admin_h).status_code == 201
    before = db.get_playlist("plex_alice_20")
    assert before["creator_id"] == "u-admin"

    env.alice.pls[0].title = "Hijacked"
    env.alice.pls[0].items.return_value = [make_track(9, "Evil")]
    resp = env.tc.post("/api/plex-playlists/20/adopt", headers=headers(users["alice"], db, config))
    assert resp.status_code == 404

    after = db.get_playlist("plex_alice_20")
    assert after["name"] == before["name"] == "Mine"
    assert after["tracks_json"] == before["tracks_json"]
    assert after["creator_id"] == "u-admin"


def test_sync_refreshes_adopted_playlist_and_skips_source(env, db, config, users):
    h = headers(users["admin"], db, config)
    env.tc.post("/api/plex-playlists/10/adopt", headers=h)
    db.set_playlist_targets("plex_admin_10", ["u-admin", "u-alice"])
    # Source gains a track after adoption
    env.regular.items.return_value = [*env.regular.items.return_value, env.tracks[2]]
    env.client.match_playlist_tracks = MagicMock(return_value=([env.tracks[0]], []))
    # Even if the user flips protection off, the source itself must still be skipped by rating key
    env.tc.put("/api/plex-playlists/10/flags", json={"owner": "trackseerr"}, headers=h)
    env.regular.removeItems.reset_mock()
    env.regular.addItems.reset_mock()

    sync_state.is_syncing = False
    out = sync_state.execute_sync(db, config, env.client, None, None)
    assert out["status"] == "success"

    refreshed = json.loads(db.get_playlist("plex_admin_10")["tracks_json"])
    assert [t["title"] for t in refreshed] == ["One", "Two", "Three"]
    env.regular.removeItems.assert_not_called()
    env.regular.addItems.assert_not_called()
    # Alice (not the source owner) received a fresh copy
    assert [p.title for p in env.alice.created] == ["Road Trip"]


def test_sync_adopted_falls_back_to_snapshot_when_source_gone(env, db, config, users):
    h = headers(users["admin"], db, config)
    env.tc.post("/api/plex-playlists/10/adopt", headers=h)
    db.set_playlist_targets("plex_admin_10", ["u-alice"])
    before = db.get_playlist("plex_admin_10")["tracks_json"]
    env.admin.pls.remove(env.regular)
    env.client.match_playlist_tracks = MagicMock(return_value=([env.tracks[0]], []))
    sync_state.is_syncing = False
    assert sync_state.execute_sync(db, config, env.client, None, None)["status"] == "success"
    assert db.get_playlist("plex_admin_10")["tracks_json"] == before
    assert len(env.alice.created) == 1


# ---------------------------------------------------------------------------
# Mixes
# ---------------------------------------------------------------------------


def make_mix_server(extra_playlists=None):
    mix_tracks = [make_track(1, "One"), make_track(2, "Two")]
    mix_item = SimpleNamespace(
        key="/playlists/mix1/items", title="Daily Mix 1", leafCount=2, items=lambda: mix_tracks
    )
    other_item = SimpleNamespace(key="/library/x", title="Recent Album", leafCount=None, items=lambda: [])
    gone_hub_item = SimpleNamespace(
        key="/playlists/mix2/items",
        title="Fresh Mix",
        leafCount=None,
        items=None,
    )
    mixes_hub = SimpleNamespace(hubIdentifier="music.mixes", title="Your Mixes", items=[mix_item, gone_hub_item])
    plain_hub = SimpleNamespace(hubIdentifier="music.recent", title="Recently Added", items=[other_item])
    section = MagicMock()
    section.type = "artist"
    section.hubs.return_value = [plain_hub, mixes_hub]
    movie_section = MagicMock()
    movie_section.type = "movie"
    srv = FakeServer("admin", playlists=extra_playlists, sections=[movie_section, section])
    srv.fetchItems = MagicMock(
        return_value=[make_track(5, "Fetched"), make_track(6, "Not a track", ttype="album")]
    )
    return srv, mix_tracks


def test_mixes_listing_filters_hubs(db, config, users):
    srv, _ = make_mix_server()
    client = make_client(srv)
    tc = build_app(db, config, client)
    resp = tc.get("/api/plex-playlists/mixes", headers=headers(users["admin"], db, config))
    assert resp.status_code == 200
    mixes = resp.json()
    assert [m["mix_key"] for m in mixes] == ["/playlists/mix1/items", "/playlists/mix2/items"]
    assert mixes[0] == {
        "mix_key": "/playlists/mix1/items",
        "title": "Daily Mix 1",
        "hub_title": "Your Mixes",
        "track_count": 2,
        "thumb_url": None,
        "snapshot_id": None,
    }
    assert mixes[1]["track_count"] is None


def test_mixes_return_empty_on_plex_failure(db):
    srv = FakeServer("admin")
    srv.library.sections.side_effect = BadRequest("boom")
    client = make_client(srv)
    assert client.get_user_mixes("admin") == []


def test_mix_tracks_fall_back_to_fetch_items(db):
    srv, _ = make_mix_server()
    client = make_client(srv)
    tracks = client.get_mix_tracks("admin", "/playlists/mix2/items")
    assert [t.title for t in tracks] == ["Fetched"]


def test_snapshot_create_list_update_delete(db, config, users):
    srv, mix_tracks = make_mix_server()
    client = make_client(srv)
    tc = build_app(db, config, client)
    h = headers(users["admin"], db, config)
    resp = tc.post(
        "/api/plex-playlists/mixes/snapshot",
        json={"mix_key": "/playlists/mix1/items", "auto_refresh": True},
        headers=h,
    )
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["playlist_title"] == "Daily Mix 1 (Saved)" and snap["auto_refresh"] is True
    assert snap["plex_user"] == "admin" and snap["rating_key"]
    assert [p.title for p in srv.created] == ["Daily Mix 1 (Saved)"]
    assert db.get_plex_registry_row("admin", snap["rating_key"])["owner"] == "trackseerr"

    assert tc.get("/api/plex-playlists/mixes/snapshots", headers=h).json() == [snap]
    mixes = tc.get("/api/plex-playlists/mixes", headers=h).json()
    assert mixes[0]["snapshot_id"] == snap["id"]

    upd = tc.put(f"/api/plex-playlists/mixes/snapshots/{snap['id']}", json={"auto_refresh": False}, headers=h)
    assert upd.status_code == 200 and upd.json()["auto_refresh"] is False
    assert tc.delete(f"/api/plex-playlists/mixes/snapshots/{snap['id']}", headers=h).status_code == 204
    assert db.get_mix_snapshot(snap["id"]) is None
    assert len(srv.pls) == 1  # Plex playlist kept


def test_snapshot_unknown_mix_404_and_collision_409(db, config, users):
    srv, _ = make_mix_server(extra_playlists=[make_playlist(50, "Daily Mix 1 (Saved)")])
    client = make_client(srv)
    tc = build_app(db, config, client)
    h = headers(users["admin"], db, config)
    assert tc.post("/api/plex-playlists/mixes/snapshot", json={"mix_key": "nope", "auto_refresh": False}, headers=h).status_code == 404
    resp = tc.post(
        "/api/plex-playlists/mixes/snapshot", json={"mix_key": "/playlists/mix1/items", "auto_refresh": False}, headers=h
    )
    assert resp.status_code == 409
    assert "Protected" in resp.json()["detail"]


def test_snapshot_ownership_enforced(db, config, users):
    srv, _ = make_mix_server()
    client = make_client(srv)
    tc = build_app(db, config, client)
    snap = db.upsert_mix_snapshot("admin", "/playlists/mix1/items", "Daily Mix 1", "Saved", None, True)
    resp = tc.put(
        f"/api/plex-playlists/mixes/snapshots/{snap['id']}",
        json={"auto_refresh": False},
        headers=headers(users["alice"], db, config),
    )
    assert resp.status_code == 404
    assert tc.delete("/api/plex-playlists/mixes/snapshots/nope", headers=headers(users["admin"], db, config)).status_code == 404


def test_sync_refreshes_auto_refresh_snapshots(db, config, users):
    existing = make_playlist(60, "Daily Mix 1 (Saved)", items=[make_track(9, "Old")])
    srv, mix_tracks = make_mix_server(extra_playlists=[existing])
    client = make_client(srv)
    db.upsert_plex_registry("admin", "60", "Daily Mix 1 (Saved)", "regular", "trackseerr")
    auto = db.upsert_mix_snapshot("admin", "/playlists/mix1/items", "Daily Mix 1", "Daily Mix 1 (Saved)", "60", True)
    manual = db.upsert_mix_snapshot("admin", "/playlists/mix2/items", "Fresh Mix", "Fresh Mix (Saved)", None, False)
    db.conn.execute("UPDATE plex_mix_snapshots SET last_refreshed_at = NULL")
    db.conn.commit()

    sync_state.is_syncing = False
    assert sync_state.execute_sync(db, config, client, None, None)["status"] == "success"
    existing.removeItems.assert_called_once()
    existing.addItems.assert_called_once_with(mix_tracks)
    assert db.get_mix_snapshot(auto["id"])["last_refreshed_at"] is not None
    assert db.get_mix_snapshot(manual["id"])["last_refreshed_at"] is None


def test_sync_snapshot_missing_mix_leaves_playlist_alone(db, config, users):
    existing = make_playlist(61, "Gone Mix (Saved)", items=[make_track(9, "Old")])
    srv, _ = make_mix_server(extra_playlists=[existing])
    client = make_client(srv)
    db.upsert_plex_registry("admin", "61", "Gone Mix (Saved)", "regular", "trackseerr")
    db.upsert_mix_snapshot("admin", "/playlists/vanished/items", "Gone Mix", "Gone Mix (Saved)", "61", True)
    sync_state.is_syncing = False
    assert sync_state.execute_sync(db, config, client, None, None)["status"] == "success"
    existing.removeItems.assert_not_called()
    existing.addItems.assert_not_called()


# ---------------------------------------------------------------------------
# Copy accounting, per-target failures, input validation
# ---------------------------------------------------------------------------


def test_copy_counts_omitted_tracks(env, db, config, users):
    t1, t2, t3 = env.tracks
    env.regular.items.return_value = [t1, t2, t3]
    del env.alice.tracks[2]  # alice cannot see track 2 -> NotFound
    resp = env.tc.post(
        "/api/plex-playlists/10/copy",
        json={"target_users": ["alice"], "title": "Trip"},
        headers=headers(users["admin"], db, config),
    )
    r = resp.json()[0]
    assert resp.status_code == 200
    assert r["success"] is True and r["copied_tracks"] == 2 and r["omitted_tracks"] == 1


def test_copy_non_numeric_rating_key_is_omitted_not_500(env, db, config, users):
    t1 = env.tracks[0]
    bad = make_track("abc", "Bad")
    env.regular.items.return_value = [t1, bad]
    resp = env.tc.post(
        "/api/plex-playlists/10/copy",
        json={"target_users": ["alice"], "title": "Trip"},
        headers=headers(users["admin"], db, config),
    )
    assert resp.status_code == 200
    r = resp.json()[0]
    assert r["success"] is True and r["copied_tracks"] == 1 and r["omitted_tracks"] == 1


def test_copy_one_target_bad_request_does_not_block_next(env, db, config, users):
    bob_srv = FakeServer("bob", tracks=list(env.tracks))
    env.client.server.switchUser = lambda u: {"alice": env.alice, "bob": bob_srv}[u]
    env.client.server.myPlexAccount = lambda: SimpleNamespace(
        username="admin",
        users=lambda: [SimpleNamespace(username=u, id=i + 2, email="", thumb="") for i, u in enumerate(["alice", "bob"])],
    )
    env.alice.createPlaylist = MagicMock(side_effect=BadRequest("nope-400"))
    resp = env.tc.post(
        "/api/plex-playlists/10/copy",
        json={"target_users": ["alice", "bob"], "title": "Fresh Copy"},
        headers=headers(users["admin"], db, config),
    )
    assert resp.status_code == 200
    by_user = {r["username"]: r for r in resp.json()}
    assert by_user["alice"]["success"] is False and "nope-400" in by_user["alice"]["error"]
    assert by_user["bob"]["success"] is True and by_user["bob"]["copied_tracks"] == 2


def test_add_items_rejects_non_track(env, db, config, users):
    album = make_track(50, "An Album", ttype="album")
    env.admin.tracks[50] = album
    resp = env.tc.post(
        "/api/plex-playlists/10/items", json={"track_rating_keys": ["50"]}, headers=headers(users["admin"], db, config)
    )
    assert resp.status_code == 400 and "50" in resp.json()["detail"]
    env.regular.addItems.assert_not_called()


def test_empty_caller_username_is_400(env, db, config, users):
    nameless = db.upsert_user("u-x", "", "x@x.io", is_admin=True)
    h = headers(nameless, db, config)
    resp = env.tc.get("/api/plex-playlists", headers=h)
    assert resp.status_code == 400 and "no Plex username" in resp.json()["detail"]
