"""Tests for inspecting and adopting Subsonic and Jellyfin playlists via /api/media-servers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.media_servers import MediaServerUnsupported, registry
from trackseerr.media_servers.jellyfin import JellyfinMediaServer
from trackseerr.media_servers.subsonic import SubsonicMediaServer
from trackseerr.storage import Database
from tests.jellyfin_fake import (
    API_KEY as JF_API_KEY,
    FakeEntry,
    FakePlaylist as JfFakePlaylist,
    FakeUser as JfFakeUser,
    JellyfinState,
    FakeJellyfin,
    default_state as jf_default_state,
)
from tests.subsonic_fake import (
    FakePlaylist as SubFakePlaylist,
    FakeSubsonic,
    default_state as sub_default_state,
)

SONGS = [
    ("Song One", "Artist A", "Album 1"),
    ("Song Two", "Artist B", "Album 2"),
    ("Song Three", "Artist C", "Album 3"),
]


def _headers(db: Database, cfg: Config, user: dict) -> dict[str, str]:
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _install_record_with_adapter(db: Database, cfg: Config, row_spec: dict, adapter) -> str:
    row = db.insert_media_server_row(row_spec)
    records = registry.load_records(cfg, db)
    registry.install(records)
    rec = next(r for r in records if r.id == row["id"])
    with registry._adapters_lock:
        registry._adapters[rec.id] = (registry._adapter_key(rec), adapter)
    return row["id"]


@pytest.fixture
def env(tmp_path):
    registry._reset_for_tests()
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: None
    admin = db.upsert_user("a1", "admin", "a@x", is_admin=True)
    alice = db.upsert_user("u1", "alice", "u@x", is_admin=False)

    sub_fake = FakeSubsonic(sub_default_state(SONGS, password="pw"))
    sub_fake.state.playlists["sub-1"] = SubFakePlaylist("sub-1", "Admin Rock", "admin", ["s1", "s2"])
    sub_fake.state.playlists["sub-2"] = SubFakePlaylist("sub-2", "Kid Public", "kid", ["s3"])
    sub_adapter = SubsonicMediaServer("http://navidrome:4533", "admin", "pw", transport=sub_fake.transport())
    sub_id = _install_record_with_adapter(
        db,
        cfg,
        {"kind": "subsonic", "name": "Navidrome", "url": "http://navidrome:4533", "username": "admin", "password": "pw"},
        sub_adapter,
    )

    jf_state: JellyfinState = jf_default_state(SONGS)
    jf_state.users.append(JfFakeUser("u-bob", "bob", False))
    jf_state.playlists["jf-admin-priv"] = JfFakePlaylist(
        "jf-admin-priv", "Admin Private", "u-admin", public=False, entries=[FakeEntry("e1", "s1")]
    )
    jf_state.playlists["jf-kid-priv"] = JfFakePlaylist(
        "jf-kid-priv", "Kid Mix", "u-kid", public=False, entries=[FakeEntry("e2", "s2"), FakeEntry("e3", "s3")]
    )
    jf_state.playlists["jf-shared"] = JfFakePlaylist(
        "jf-shared", "Shared Vibes", "u-bob", public=True, entries=[FakeEntry("e4", "s1")]
    )
    jf_fake = FakeJellyfin(jf_state)
    jf_adapter = JellyfinMediaServer("http://jellyfin:8096", JF_API_KEY, transport=jf_fake.transport())
    jf_id = _install_record_with_adapter(
        db,
        cfg,
        {"kind": "jellyfin", "name": "Jellyfin", "url": "http://jellyfin:8096", "api_key": JF_API_KEY},
        jf_adapter,
    )

    plex_row = db.insert_media_server_row(
        {"kind": "plex", "name": "Plex", "url": "http://plex:32400", "api_key": "plex-tok"}
    )
    registry.install(registry.load_records(cfg, db))

    yield SimpleNamespace(
        tc=TestClient(app),
        db=db,
        cfg=cfg,
        admin=_headers(db, cfg, admin),
        alice=_headers(db, cfg, alice),
        admin_user=admin,
        sub_id=sub_id,
        sub_fake=sub_fake,
        jf_id=jf_id,
        jf_fake=jf_fake,
        plex_id=plex_row["id"],
    )
    registry._reset_for_tests()
    db.close()


def test_list_subsonic_playlists_includes_own_and_public(env) -> None:
    resp = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists", headers=env.admin)
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": "sub-1", "title": "Admin Rock", "owner": "admin", "track_count": 2, "adopted_playlist_id": None},
        {"id": "sub-2", "title": "Kid Public", "owner": "kid", "track_count": 1, "adopted_playlist_id": None},
    ]


def test_list_jellyfin_playlists_for_named_user(env) -> None:
    resp = env.tc.get(f"/api/media-servers/{env.jf_id}/playlists?user=kid", headers=env.admin)
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": "jf-kid-priv", "title": "Kid Mix", "owner": "", "track_count": 2, "adopted_playlist_id": None},
        {"id": "jf-shared", "title": "Shared Vibes", "owner": "", "track_count": 1, "adopted_playlist_id": None},
    ]

    missing_user = env.tc.get(f"/api/media-servers/{env.jf_id}/playlists?user=nobody", headers=env.admin)
    assert missing_user.status_code == 404


def test_get_playlist_items(env) -> None:
    sub_items = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists/sub-1/items", headers=env.admin)
    assert sub_items.status_code == 200
    assert sub_items.json() == [
        {"title": "Song One", "artist": "Artist A", "album": "Album 1"},
        {"title": "Song Two", "artist": "Artist B", "album": "Album 2"},
    ]

    jf_items = env.tc.get(f"/api/media-servers/{env.jf_id}/playlists/jf-kid-priv/items?user=kid", headers=env.admin)
    assert jf_items.status_code == 200
    assert jf_items.json() == [
        {"title": "Song Two", "artist": "Artist B", "album": "Album 2"},
        {"title": "Song Three", "artist": "Artist C", "album": "Album 3"},
    ]

    missing_item = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists/nope/items", headers=env.admin)
    assert missing_item.status_code == 404


def test_adopt_creates_playlist_source_row_without_excluding_server(env) -> None:
    expected_id = f"subsonic_{env.sub_id[:8]}_sub-2"
    # Pre-seed an existing exclude to verify adopt preserves existing excludes without adding the source server
    env.db.upsert_playlist(expected_id, name="Old", service="subsonic")
    env.db.set_playlist_server_excludes(expected_id, [env.jf_id])

    resp = env.tc.post(f"/api/media-servers/{env.sub_id}/playlists/sub-2/adopt", headers=env.admin)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == expected_id
    assert body["name"] == "Kid Public"
    assert body["service"] == "subsonic"
    assert body["targets"] == []
    assert json.loads(body["tracks_json"]) == [{"title": "Song Three", "artist": "Artist C", "album": "Album 3"}]

    source = env.db.get_server_playlist_source(expected_id)
    assert source == {
        "playlist_id": expected_id,
        "server_id": env.sub_id,
        "server_playlist_id": "sub-2",
        "server_user": "kid",
    }
    assert env.db.get_playlist_server_excludes(expected_id) == {env.jf_id}

    listed = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists", headers=env.admin).json()
    kid_row = next(r for r in listed if r["id"] == "sub-2")
    assert kid_row["adopted_playlist_id"] == expected_id


def test_adopting_twice_keeps_one_playlist(env) -> None:
    first = env.tc.post(f"/api/media-servers/{env.jf_id}/playlists/jf-kid-priv/adopt?user=kid", headers=env.admin)
    assert first.status_code == 201
    expected_id = f"jellyfin_{env.jf_id[:8]}_jf-kid-priv"

    env.jf_fake.state.playlists["jf-kid-priv"].entries.append(FakeEntry("e99", "s1"))
    second = env.tc.post(f"/api/media-servers/{env.jf_id}/playlists/jf-kid-priv/adopt?user=kid", headers=env.admin)
    assert second.status_code == 201

    playlists = [p for p in env.db.list_playlists() if p["id"] == expected_id]
    assert len(playlists) == 1
    assert len(json.loads(playlists[0]["tracks_json"])) == 3
    assert env.db.get_server_playlist_source(expected_id)["server_user"] == "kid"
    assert env.jf_id not in env.db.get_playlist_server_excludes(expected_id)


def test_non_admin_returns_403(env) -> None:
    assert env.tc.get(f"/api/media-servers/{env.sub_id}/playlists", headers=env.alice).status_code == 403
    assert env.tc.get(f"/api/media-servers/{env.sub_id}/playlists/sub-1/items", headers=env.alice).status_code == 403
    assert env.tc.post(f"/api/media-servers/{env.sub_id}/playlists/sub-1/adopt", headers=env.alice).status_code == 403


def test_unknown_server_returns_404(env) -> None:
    assert env.tc.get("/api/media-servers/does-not-exist/playlists", headers=env.admin).status_code == 404
    assert env.tc.get("/api/media-servers/does-not-exist/playlists/1/items", headers=env.admin).status_code == 404
    assert env.tc.post("/api/media-servers/does-not-exist/playlists/1/adopt", headers=env.admin).status_code == 404


def test_media_server_errors_mapped_cleanly(env) -> None:
    env.sub_fake.state.transport_failure = lambda: httpx.ConnectError("http://navidrome:4533?t=SECRET")
    resp = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists", headers=env.admin)
    assert resp.status_code == 502
    assert "SECRET" not in resp.json()["detail"]
    env.sub_fake.state.transport_failure = None

    with registry._adapters_lock:
        _, adapter = registry._adapters[env.sub_id]

    def _unsupported(user=None):
        raise MediaServerUnsupported("Not supported")

    adapter.list_source_playlists = _unsupported  # type: ignore[method-assign]
    resp_unsup = env.tc.get(f"/api/media-servers/{env.sub_id}/playlists", headers=env.admin)
    assert resp_unsup.status_code == 409


def test_login_plex_still_409(env) -> None:
    env.db.conn.execute("UPDATE media_servers SET is_login = 1 WHERE id = ?", (env.plex_id,))
    env.db.conn.commit()
    registry.install(registry.load_records(env.cfg, env.db))
    assert env.tc.get(f"/api/media-servers/{env.plex_id}/playlists", headers=env.admin).status_code == 409
    assert env.tc.get(f"/api/media-servers/{env.plex_id}/playlists/1/items", headers=env.admin).status_code == 409
    assert env.tc.post(f"/api/media-servers/{env.plex_id}/playlists/1/adopt", headers=env.admin).status_code == 409


def test_second_plex_lists_and_adopts(env) -> None:
    from trackseerr.media_servers.plex import PlexMediaServer

    audio_pl = SimpleNamespace(
        ratingKey="42",
        title="Second Plex Mix",
        playlistType="audio",
        leafCount=2,
        items=lambda: [
            SimpleNamespace(title="Track A", grandparentTitle="Artist X", parentTitle="Album Y"),
            SimpleNamespace(title="Track B", grandparentTitle="Artist Z", parentTitle="Album W"),
        ],
    )
    video_pl = SimpleNamespace(ratingKey="99", title="Movies", playlistType="video", leafCount=1, items=lambda: [])
    fake_server = SimpleNamespace(
        playlists=lambda: [audio_pl, video_pl],
        fetchItem=lambda key: audio_pl if int(key) == 42 else None,
    )
    adapter = PlexMediaServer(SimpleNamespace(server=fake_server, music_section=None))
    second_id = _install_record_with_adapter(
        env.db,
        env.cfg,
        {"kind": "plex", "name": "Second Plex", "url": "http://plex-2:32400", "token": "tok2", "is_login": False},
        adapter,
    )

    listed = env.tc.get(f"/api/media-servers/{second_id}/playlists", headers=env.admin)
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "id": "42",
            "title": "Second Plex Mix",
            "owner": "",
            "track_count": 2,
            "adopted_playlist_id": None,
        }
    ]

    items = env.tc.get(f"/api/media-servers/{second_id}/playlists/42/items", headers=env.admin)
    assert items.status_code == 200
    assert items.json() == [
        {"title": "Track A", "artist": "Artist X", "album": "Album Y"},
        {"title": "Track B", "artist": "Artist Z", "album": "Album W"},
    ]

    adopted = env.tc.post(f"/api/media-servers/{second_id}/playlists/42/adopt", headers=env.admin)
    assert adopted.status_code == 201
    expected_id = f"plex_{second_id[:8]}_42"
    assert adopted.json()["id"] == expected_id
