"""Track aliases: store, migration v73, sync application, admin routes."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_deezer_client, get_spotify_client
from trackseerr.api.routes.sync import sync_state
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import Playlist, Track
from trackseerr.storage import SCHEMA_VERSION, Database
from trackseerr.track_alias_store import track_alias_key
from trackseerr.track_aliases import apply_track_aliases, restore_missing

PL_ID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.fixture
def db():
    database = Database(":memory:")
    database.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    database.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="", plex_token="", data_dir=str(tmp_path), spotify_client_id="id", spotify_client_secret="s")


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


def _alias(db, title="One More Tyme", artist="Daft Punk", kind="alias", status="approved", **kw):
    if kind == "alias":
        kw.setdefault("target_title", "One More Time")
        kw.setdefault("target_artist", "Daft Punk")
        kw.setdefault("target_album", "Discovery")
    return db.upsert_track_alias(title, artist, kind, status, **kw)


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


# ------------------------------------------------------------------------------------------------ store


def test_key_normalisation():
    assert track_alias_key("  Daft   PUNK ", "One  More\tTime") == track_alias_key("daft punk", "one more time")
    assert track_alias_key("a", "b") != track_alias_key("b", "a")


def test_schema_version_and_table(db):
    assert SCHEMA_VERSION >= 73
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    assert {r[1] for r in db.conn.execute("PRAGMA table_info(track_aliases)")} >= {"source_key", "kind", "status"}


def test_upsert_replaces_by_key_and_resets_decision_when_pending(db):
    first = _alias(db, status="approved", decided_by="admin-1")
    assert first["decided_at"] is not None and first["decided_by"] == "admin-1"
    again = _alias(db, title="ONE MORE  TYME", status="pending", suggested_by="user-alice", target_title="Other")
    assert again["id"] == first["id"]
    assert again["status"] == "pending" and again["target_title"] == "Other"
    assert again["decided_at"] is None and again["decided_by"] is None
    assert len(db.list_track_aliases()) == 1


def test_crud_and_status_filters(db):
    a = _alias(db)
    b = _alias(db, title="Song B", kind="ignore", status="pending")
    assert {x["id"] for x in db.list_track_aliases()} == {a["id"], b["id"]}
    assert [x["id"] for x in db.list_track_aliases("pending")] == [b["id"]]
    assert set(db.approved_aliases_by_key()) == {a["source_key"]}
    decided = db.set_track_alias_status(b["id"], "approved", "admin-1")
    assert decided["status"] == "approved" and decided["decided_at"] and decided["decided_by"] == "admin-1"
    assert db.set_track_alias_status(9999, "approved", "admin-1") is None
    assert db.get_track_alias(a["id"])["source_title"] == "One More Tyme"
    assert db.delete_track_alias(a["id"]) is True
    assert db.delete_track_alias(a["id"]) is False
    with pytest.raises(ValueError):
        db.upsert_track_alias("x", "y", "bogus", "approved")


def test_user_delete_keeps_alias_and_nulls_actor(db):
    a = _alias(db, status="pending", suggested_by="user-alice")
    db.delete_user("user-alice")
    assert db.get_track_alias(a["id"])["suggested_by"] is None


def test_migration_v73_applies_on_v72_database(tmp_path):
    path = str(tmp_path / "m.db")
    d = Database(path)
    d.conn.execute("DROP TABLE track_aliases")
    d.conn.execute("UPDATE schema_migrations SET version = 72 WHERE version = 74")
    d.conn.commit()
    d.close()
    d2 = Database(path)
    try:
        assert d2.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 74
        assert _alias(d2)["id"] == 1
    finally:
        d2.close()


# ------------------------------------------------------------------------------------------------ pure logic


def test_apply_and_restore(db):
    _alias(db)
    _alias(db, title="Skip Me", artist="Nobody", kind="ignore")
    _alias(db, title="Pending One", artist="X", kind="alias", status="pending", target_title="T", target_artist="A")
    _alias(db, title="Rejected One", artist="Y", kind="alias", status="rejected", target_title="T", target_artist="A")
    src = [
        Track("one more tyme", "daft punk", "Old", url="u", duration_seconds=1.5),
        Track("Skip Me", "Nobody", ""),
        Track("Pending One", "X", ""),
        Track("Rejected One", "Y", ""),
        Track("Plain", "Z", ""),
    ]
    eff, originals, ignored = apply_track_aliases(db, src)
    assert (eff[0].title, eff[0].artist, eff[0].album, eff[0].url, eff[0].duration_seconds) == (
        "One More Time", "Daft Punk", "Discovery", "u", 1.5)
    assert originals[id(eff[0])] is src[0]
    assert eff[1] is src[1] and id(eff[1]) in ignored
    assert eff[2:] == src[2:]
    restored = restore_missing(list(eff), originals, ignored)
    assert restored == [src[0], src[2], src[3], src[4]]
    assert restored[0] is src[0]


def test_apply_without_aliases_is_identity(db):
    src = [Track("a", "b", "c")]
    assert apply_track_aliases(db, src) == (src, {}, set())


# ------------------------------------------------------------------------------------------------ sync


def _sync(db, config, tracks):
    spotify = MagicMock()
    spotify.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Mix", description="", poster="")
    spotify.get_playlist_tracks.return_value = tracks
    db.upsert_playlist(
        playlist_id=PL_ID, name="Mix", service="spotify", tracks_json=json.dumps([]), creator_id="admin-1"
    )
    return sync_state.execute_sync(db=db, config=config, plex_client=None, spotify_client=spotify, deezer_client=None)


def _missing_titles(db):
    return sorted(m["title"] for m in db.get_missing_tracks(PL_ID))


def test_sync_without_alias_reports_missing(db, config):
    _seed_library(db)
    assert _sync(db, config, [Track("One More Tyme", "Daft Punk", "")])["status"] == "success"
    assert _missing_titles(db) == ["One More Tyme"]


def test_sync_approved_alias_matches_and_pending_rejected_do_not(db, config):
    _seed_library(db)
    tracks = [Track("One More Tyme", "Daft Punk", "")]
    _alias(db, status="pending")
    _sync(db, config, tracks)
    assert _missing_titles(db) == ["One More Tyme"]
    db.set_track_alias_status(1, "rejected", "admin-1")
    _sync(db, config, tracks)
    assert _missing_titles(db) == ["One More Tyme"]
    db.set_track_alias_status(1, "approved", "admin-1")
    _sync(db, config, tracks)
    assert _missing_titles(db) == []


def test_sync_ignored_track_never_missing_and_missing_keeps_original(db, config):
    _seed_library(db)
    _alias(db, title="Weird Song", artist="Nobody", kind="ignore")
    _alias(db, title="Bad Alias", artist="Foo", target_title="Still Not There", target_artist="Foo")
    _sync(db, config, [Track("Weird Song", "Nobody", ""), Track("Bad Alias", "Foo", "")])
    # the unmatched alias target stays missing under the SOURCE spelling, so playlist rows keep matching
    assert _missing_titles(db) == ["Bad Alias"]


# ------------------------------------------------------------------------------------------------ routes


def test_alias_routes_require_admin(client, db, secret_key):
    a = _alias(db)
    alice = _cookies(db, "user-alice", "alice", False, secret_key)
    body = {"source_title": "x", "source_artist": "y", "kind": "ignore"}
    assert client.get("/api/missing/aliases", cookies=alice).status_code == 403
    assert client.post("/api/missing/aliases", json=body, cookies=alice).status_code == 403
    assert client.post(f"/api/missing/aliases/{a['id']}/approve", cookies=alice).status_code == 403
    assert client.post(f"/api/missing/aliases/{a['id']}/reject", cookies=alice).status_code == 403
    assert client.delete(f"/api/missing/aliases/{a['id']}", cookies=alice).status_code == 403
    assert client.get("/api/missing/aliases").status_code == 401


def test_admin_create_list_delete(client, db, secret_key):
    ck = _cookies(db, "admin-1", "admin_user", True, secret_key)
    r = client.post("/api/missing/aliases", json={"source_title": "S", "source_artist": "A", "kind": "ignore"}, cookies=ck)
    assert r.status_code == 201
    assert r.json()["status"] == "approved" and r.json()["kind"] == "ignore" and r.json()["decided_by"] == "admin-1"
    bad = client.post("/api/missing/aliases", json={"source_title": "S2", "source_artist": "A", "kind": "alias",
                                                    "target_title": "T"}, cookies=ck)
    assert bad.status_code == 422
    ok = client.post("/api/missing/aliases", json={"source_title": "S2", "source_artist": "A", "kind": "alias",
                                                   "target_title": "T", "target_artist": "TA"}, cookies=ck)
    assert ok.status_code == 201 and ok.json()["target_artist"] == "TA"
    assert len(client.get("/api/missing/aliases", cookies=ck).json()) == 2
    assert len(client.get("/api/missing/aliases?status=pending", cookies=ck).json()) == 0
    assert client.get("/api/missing/aliases?status=bogus", cookies=ck).status_code == 422
    assert client.delete(f"/api/missing/aliases/{r.json()['id']}", cookies=ck).json()["status"] == "deleted"
    assert client.delete(f"/api/missing/aliases/{r.json()['id']}", cookies=ck).status_code == 404


def _issue(db, issue_id="iss-1"):
    db.create_issue({"id": issue_id, "user_id": "user-alice", "media_title": "T", "artist": "A",
                     "issue_type": "other", "problem_details": "wrong match"})


def test_approve_resolves_and_reject_wont_fix_linked_issue(client, db, secret_key):
    ck = _cookies(db, "admin-1", "admin_user", True, secret_key)
    _issue(db, "iss-1")
    _issue(db, "iss-2")
    a = _alias(db, status="pending", issue_id="iss-1", suggested_by="user-alice")
    b = _alias(db, title="Other", artist="Q", status="pending", issue_id="iss-2", suggested_by="user-alice")
    r = client.post(f"/api/missing/aliases/{a['id']}/approve", cookies=ck)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert db.get_issue("iss-1")["status"] == "resolved"
    r = client.post(f"/api/missing/aliases/{b['id']}/reject", cookies=ck)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert db.get_issue("iss-2")["status"] == "wont_fix"
    assert client.post("/api/missing/aliases/9999/approve", cookies=ck).status_code == 404


def test_approve_tolerates_missing_or_already_closed_issue(client, db, secret_key):
    ck = _cookies(db, "admin-1", "admin_user", True, secret_key)
    a = _alias(db, status="pending", issue_id="gone")
    assert client.post(f"/api/missing/aliases/{a['id']}/approve", cookies=ck).status_code == 200
    _issue(db, "iss-3")
    db.set_issue_status("iss-3", "wont_fix", "admin-1", True)
    b = _alias(db, title="Q", artist="Q", status="pending", issue_id="iss-3")
    assert client.post(f"/api/missing/aliases/{b['id']}/approve", cookies=ck).status_code == 200
    assert db.get_issue("iss-3")["status"] == "wont_fix"
