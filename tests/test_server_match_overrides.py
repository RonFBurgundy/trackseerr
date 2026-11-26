"""Per-server match overrides: store, registry, adapter isolation, and /api/missing routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.plex import PlexClient
from trackseerr.config import Config
from trackseerr.media_servers import JellyfinMediaServer, SubsonicMediaServer, registry
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.models import Track
from trackseerr.storage import SCHEMA_VERSION, Database
from tests.jellyfin_fake import API_KEY as JELLYFIN_KEY
from tests.jellyfin_fake import FakeAudio, FakeJellyfin, JellyfinState
from tests.jellyfin_fake import FakeUser as JfUser
from tests.subsonic_fake import FakeSong, FakeSubsonic, SubsonicState, default_state
from tests.subsonic_fake import FakeUser as SubUser


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "overrides.db"))
    yield database
    database.close()


def _admin_client(db: Database, tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    user = db.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_store_round_trip_precedence_and_delete(db: Database) -> None:
    srv1 = db.insert_media_server_row({"kind": "plex", "name": "Home Plex", "url": "http://plex:32400"})
    srv2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})

    legacy = db.add_match_override("Song A", "Artist 1", "plex-legacy", "Song A", "Artist 1")
    assert legacy["server_id"] is None

    s1_row = db.add_server_match_override(srv1["id"], "Song A", "Artist 1", "plex-1", "Song A (Plex)", "Artist 1")
    assert s1_row["server_id"] == srv1["id"]
    assert s1_row["plex_rating_key"] == "plex-1"

    # Upsert updates existing server row
    s1_updated = db.add_server_match_override(srv1["id"], "Song A", "Artist 1", "plex-2", "Song A (v2)", "Artist 1")
    assert s1_updated["id"] == s1_row["id"]
    assert s1_updated["plex_rating_key"] == "plex-2"

    # Server row wins over legacy (case-insensitive)
    hit_s1 = db.get_match_override("song a", "ARTIST 1", server_id=srv1["id"], use_legacy=True)
    assert hit_s1 is not None and hit_s1["plex_rating_key"] == "plex-2" and hit_s1["server_id"] == srv1["id"]

    # Another server falls back to legacy only when use_legacy=True
    hit_s2_legacy = db.get_match_override("Song A", "Artist 1", server_id=srv2["id"], use_legacy=True)
    assert hit_s2_legacy is not None and hit_s2_legacy["plex_rating_key"] == "plex-legacy" and hit_s2_legacy["server_id"] is None
    assert db.get_match_override("Song A", "Artist 1", server_id=srv2["id"], use_legacy=False) is None

    # list_match_overrides includes both sets with server_id
    listed = db.list_match_overrides()
    assert len(listed) == 2
    assert {item["server_id"] for item in listed} == {srv1["id"], None}

    # Deleting server override with server_id leaves legacy intact even when autoincrement ids match
    assert db.delete_match_override(s1_updated["id"], server_id=srv2["id"]) is False
    assert db.delete_match_override(s1_updated["id"], server_id=srv1["id"]) is True
    assert len(db.list_match_overrides()) == 1
    assert db.list_match_overrides()[0]["plex_rating_key"] == "plex-legacy"

    # Deleting legacy override without server_id
    assert db.delete_match_override(legacy["id"]) is True
    assert db.list_match_overrides() == []


def test_cascade_on_server_delete(db: Database) -> None:
    srv = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"})
    db.add_match_override("Legacy Track", "Artist", "legacy-1", "Legacy Track", "Artist")
    db.add_server_match_override(srv["id"], "Server Track", "Artist", "jf-1", "Server Track", "Artist")

    assert db.delete_media_server_row(srv["id"]) is True
    listed = db.list_match_overrides()
    assert len(listed) == 1
    assert listed[0]["plex_rating_key"] == "legacy-1"


def test_registry_sets_server_id_and_legacy_overrides() -> None:
    plex_rec = ServerRecord(
        id="srv-plex",
        kind="plex",
        name="Plex",
        source="env",
        url="http://plex:32400",
        api_key="tok",
        is_login=True,
    )
    nav_rec = ServerRecord(
        id="srv-nav",
        kind="subsonic",
        name="Navidrome",
        source="env",
        url="http://nav:4533",
        username="u",
        password="p",
        is_login=False,
    )
    registry.install([plex_rec, nav_rec])

    with patch("trackseerr.clients.plex.PlexServer", return_value=MagicMock()):
        plex_adapter = registry.build_adapter(plex_rec)
    nav_adapter = registry.build_adapter(nav_rec)

    assert plex_adapter is not None
    assert plex_adapter.server_id == "srv-plex"
    assert plex_adapter.legacy_overrides is True
    plex_client = plex_adapter.plex_extras()
    assert plex_client.server_id == "srv-plex"
    assert plex_client.legacy_overrides is True

    assert nav_adapter is not None
    assert nav_adapter.server_id == "srv-nav"
    assert nav_adapter.legacy_overrides is False


def test_navidrome_secondary_ignores_legacy_plex_pin(db: Database) -> None:
    plex_srv = db.insert_media_server_row({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True})
    nav_srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})

    # Legacy Plex override points to a Plex ratingKey that does not exist on Navidrome
    db.add_match_override("Song A", "Artist 1", "999999", "Song A", "Artist 1")

    fake = FakeSubsonic(default_state([("Song A", "Artist 1", "Album X"), ("Other Song", "Someone", "Album Y")]))
    nav = SubsonicMediaServer("http://nav:4533", "admin", "pw", transport=fake.transport())
    nav.server_id = nav_srv["id"]
    nav.legacy_overrides = False

    ref = nav.match_track(Track("Song A", "Artist 1", "Album X"), db=db)
    assert ref is not None and ref.id == "s1"
    # Confirm Navidrome never asked getSong for the legacy Plex ratingKey
    assert not [c for c in fake.state.calls if c[0] == "getSong"]

    # When a server-scoped override exists for Navidrome, it IS used
    db.add_server_match_override(nav_srv["id"], "Unfindable", "Nobody", "s2", "Other Song", "Someone")
    pinned = nav.match_track(Track("Unfindable", "Nobody", ""), db=db)
    assert pinned is not None and pinned.id == "s2"
    assert plex_srv["id"] != nav_srv["id"]


def test_api_search_match_list_and_delete_per_server(db: Database, tmp_path: Path) -> None:
    client, headers = _admin_client(db, tmp_path)
    srv1 = db.insert_media_server_row({"kind": "plex", "name": "Home Plex", "url": "http://plex:32400", "is_login": True})
    srv2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
    db.upsert_playlist("pl-1", name="Mix", service="spotify", creator_id="admin-1")
    db.record_sync_result(
        "pl-1",
        "partial",
        missing_tracks=[{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}],
    )

    rec1 = ServerRecord(id=srv1["id"], kind="plex", name="Home Plex", source="env", url="http://plex:32400", api_key="t", is_login=True)
    rec2 = ServerRecord(id=srv2["id"], kind="subsonic", name="Navidrome", source="env", url="http://nav:4533", username="u", password="p")
    registry.install([rec1, rec2])

    nav_hits = [{"id": "nav-42", "rating_key": "nav-42", "title": "Airbag (Nav)", "artist": "Radiohead", "album": "OK"}]
    nav_adapter = SimpleNamespace(search_tracks=lambda q, limit=15: nav_hits)

    def fake_server_by_id(sid: str, config=None):
        if sid == srv2["id"]:
            return (rec2, nav_adapter)
        return None

    with patch("trackseerr.api.routes.missing.registry.server_by_id", side_effect=fake_server_by_id):
        res = client.get(f"/api/missing/search?query=airbag&server_id={srv2['id']}", headers=headers)
        assert res.status_code == 200
        assert res.json() == nav_hits

        missing_res = client.get("/api/missing/search?query=airbag&server_id=nonexistent", headers=headers)
        assert missing_res.status_code == 404

    # Save legacy override + server-scoped override
    legacy_resp = client.post(
        "/api/missing/match",
        json={
            "source_title": "Legacy Song",
            "source_artist": "Artist",
            "plex_rating_key": "plex-1",
            "plex_title": "Legacy Song",
            "plex_artist": "Artist",
        },
        headers=headers,
    )
    assert legacy_resp.status_code == 201
    assert legacy_resp.json()["override"]["server_id"] is None

    srv_resp = client.post(
        "/api/missing/match",
        json={
            "source_title": "Airbag",
            "source_artist": "Radiohead",
            "plex_rating_key": "nav-42",
            "plex_title": "Airbag (Nav)",
            "plex_artist": "Radiohead",
            "server_id": srv2["id"],
        },
        headers=headers,
    )
    assert srv_resp.status_code == 201
    srv_override = srv_resp.json()["override"]
    assert srv_override["server_id"] == srv2["id"]
    assert srv_override["server_name"] == "Navidrome"
    assert len(db.get_missing_tracks()) == 1

    # List includes both with server_id and server_name
    listed = client.get("/api/missing/matches", headers=headers).json()
    assert len(listed) == 2
    by_title = {item["source_title"]: item for item in listed}
    assert by_title["Legacy Song"]["server_id"] is None and by_title["Legacy Song"]["server_name"] is None
    assert by_title["Airbag"]["server_id"] == srv2["id"] and by_title["Airbag"]["server_name"] == "Navidrome"

    # Delete server override with ?server_id= leaves legacy override intact
    del_srv = client.delete(f"/api/missing/match/{srv_override['id']}?server_id={srv2['id']}", headers=headers)
    assert del_srv.status_code == 200
    remaining = client.get("/api/missing/matches", headers=headers).json()
    assert len(remaining) == 1 and remaining[0]["source_title"] == "Legacy Song"


def test_secondary_server_pin_keeps_missing_track(db: Database, tmp_path: Path) -> None:
    client, headers = _admin_client(db, tmp_path)
    srv1 = db.insert_media_server_row({"kind": "plex", "name": "Home Plex", "url": "http://plex:32400", "is_login": True})
    srv2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
    db.upsert_playlist("pl-1", name="Mix", service="spotify", creator_id="admin-1")
    db.record_sync_result(
        "pl-1",
        "partial",
        missing_tracks=[{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}],
    )

    rec1 = ServerRecord(id=srv1["id"], kind="plex", name="Home Plex", source="env", url="http://plex:32400", api_key="t", is_login=True)
    rec2 = ServerRecord(id=srv2["id"], kind="subsonic", name="Navidrome", source="env", url="http://nav:4533", username="u", password="p")
    registry.install([rec1, rec2])

    resp = client.post(
        "/api/missing/match",
        json={
            "source_title": "Airbag",
            "source_artist": "Radiohead",
            "plex_rating_key": "nav-42",
            "plex_title": "Airbag (Nav)",
            "plex_artist": "Radiohead",
            "server_id": srv2["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    missing = db.get_missing_tracks()
    assert len(missing) == 1
    assert missing[0]["title"] == "Airbag"


def test_primary_pin_clears_missing_track(db: Database, tmp_path: Path) -> None:
    client, headers = _admin_client(db, tmp_path)
    srv1 = db.insert_media_server_row({"kind": "plex", "name": "Home Plex", "url": "http://plex:32400", "is_login": True})
    srv2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
    db.upsert_playlist("pl-1", name="Mix", service="spotify", creator_id="admin-1")
    db.record_sync_result(
        "pl-1",
        "partial",
        missing_tracks=[{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}],
    )

    rec1 = ServerRecord(id=srv1["id"], kind="plex", name="Home Plex", source="env", url="http://plex:32400", api_key="t", is_login=True)
    rec2 = ServerRecord(id=srv2["id"], kind="subsonic", name="Navidrome", source="env", url="http://nav:4533", username="u", password="p")
    registry.install([rec1, rec2])

    resp = client.post(
        "/api/missing/match",
        json={
            "source_title": "Airbag",
            "source_artist": "Radiohead",
            "plex_rating_key": "plex-42",
            "plex_title": "Airbag",
            "plex_artist": "Radiohead",
            "server_id": srv1["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert db.get_missing_tracks() == []


def _seed_library_track(
    db: Database,
    *,
    track_id: str = "trk-pin-1",
    title: str = "Karma Police",
    artist: str = "Radiohead",
    album: str = "OK Computer",
    file_path: str | None = "/data/music/Radiohead/OK Computer/06 Karma Police.flac",
) -> None:
    db.upsert_library_artist({"id": f"art-{track_id}", "name": artist})
    db.upsert_library_album({"id": f"alb-{track_id}", "artist_id": f"art-{track_id}", "title": album})
    db.upsert_library_track(
        {
            "id": track_id,
            "album_id": f"alb-{track_id}",
            "artist_id": f"art-{track_id}",
            "title": title,
            "track_number": 6,
            "disc_number": 1,
            "duration_seconds": 264.0,
            "has_file": 1 if file_path else 0,
        }
    )
    if file_path:
        db.upsert_library_file(
            {
                "id": f"file-{track_id}",
                "track_id": track_id,
                "file_path": file_path,
                "relative_path": Path(file_path).name,
                "codec": "flac",
                "quality_name": "FLAC",
                "size_bytes": 123456,
            }
        )


def test_library_pin_store_migration_and_cascade(db: Database) -> None:
    assert SCHEMA_VERSION >= 115
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION

    # Idempotent migration when dropped and re-applied
    cur = db.conn.cursor()
    cur.execute("DROP TABLE library_match_pins")
    db.conn.commit()
    db._migration_v115(cur)
    db._migration_v115(cur)
    db.conn.commit()

    _seed_library_track(db, track_id="trk-1")
    _seed_library_track(db, track_id="trk-nofile", title="No File", artist="Ghost", album="Empty", file_path=None)

    pin = db.set_library_pin("Playlist Title", "Playlist Artist", "trk-1", created_by=None)
    assert pin["track_id"] == "trk-1"
    assert pin["kind"] == "library"
    assert pin["server_id"] is None

    # Case-insensitive lookup builds LibraryHit with file_path
    hit = db.get_library_pin("playlist title", "PLAYLIST ARTIST")
    assert hit is not None
    assert hit.track_id == "trk-1"
    assert hit.title == "Karma Police"
    assert hit.artist == "Radiohead"
    assert hit.album == "OK Computer"
    assert hit.file_path == "/data/music/Radiohead/OK Computer/06 Karma Police.flac"
    assert hit.via == "library_pin"

    # Upsert to a track without a file yields None from get_library_pin
    db.set_library_pin("Playlist Title", "Playlist Artist", "trk-nofile")
    assert db.get_library_pin("Playlist Title", "Playlist Artist") is None

    listed = db.list_library_pins()
    assert len(listed) == 1 and listed[0]["id"] == pin["id"]

    # Cascade when library_tracks row is deleted
    with db._lock:
        db.conn.execute("DELETE FROM library_tracks WHERE id = ?", ("trk-nofile",))
        db.conn.commit()
    assert db.list_library_pins() == []


def test_api_library_pin_search_save_list_delete_and_clears_missing(db: Database, tmp_path: Path) -> None:
    client, headers = _admin_client(db, tmp_path)
    _seed_library_track(db, track_id="trk-kp")
    db.upsert_playlist("pl-1", name="Mix", service="spotify", creator_id="admin-1")
    db.record_sync_result(
        "pl-1",
        "partial",
        missing_tracks=[{"title": "Karma (Weird Mix)", "artist": "Radiohead feat. X", "album": "Mix"}],
    )

    # source=library works even without any configured media server
    search_resp = client.get("/api/missing/search?query=karma&source=library", headers=headers)
    assert search_resp.status_code == 200
    assert search_resp.json() == [
        {
            "track_id": "trk-kp",
            "title": "Karma Police",
            "artist": "Radiohead",
            "album": "OK Computer",
        }
    ]

    # Validation checks
    bad_both = client.post(
        "/api/missing/match",
        json={
            "source_title": "Karma (Weird Mix)",
            "source_artist": "Radiohead feat. X",
            "track_id": "trk-kp",
            "server_id": "srv-1",
        },
        headers=headers,
    )
    assert bad_both.status_code == 422

    bad_missing_track = client.post(
        "/api/missing/match",
        json={
            "source_title": "Karma (Weird Mix)",
            "source_artist": "Radiohead feat. X",
            "track_id": "does-not-exist",
        },
        headers=headers,
    )
    assert bad_missing_track.status_code == 404

    # Save library pin clears missing row
    pin_resp = client.post(
        "/api/missing/match",
        json={
            "source_title": "Karma (Weird Mix)",
            "source_artist": "Radiohead feat. X",
            "track_id": "trk-kp",
        },
        headers=headers,
    )
    assert pin_resp.status_code == 201
    pin_override = pin_resp.json()["override"]
    assert pin_override["kind"] == "library"
    assert pin_override["track_id"] == "trk-kp"
    assert pin_override["server_id"] is None
    assert db.get_missing_tracks() == []

    # Add a legacy override with id=1 as well to verify ?kind=library isolation on DELETE
    legacy = db.add_match_override("Legacy Track", "Legacy Artist", "plex-9", "Legacy Track", "Legacy Artist")
    assert legacy["id"] == pin_override["id"] == 1

    listed = client.get("/api/missing/matches", headers=headers).json()
    assert len(listed) == 2
    by_kind = {item["kind"]: item for item in listed}
    assert by_kind["library"]["track_id"] == "trk-kp"
    assert by_kind["legacy"]["plex_rating_key"] == "plex-9"

    del_pin = client.delete(f"/api/missing/match/{pin_override['id']}?kind=library", headers=headers)
    assert del_pin.status_code == 200
    remaining = client.get("/api/missing/matches", headers=headers).json()
    assert len(remaining) == 1 and remaining[0]["kind"] == "legacy"


def test_library_pin_applies_across_all_servers_and_per_server_wins(db: Database) -> None:
    _seed_library_track(
        db,
        track_id="trk-kp",
        title="Karma Police",
        artist="Radiohead",
        album="OK Computer",
        file_path="/data/music/Radiohead/OK Computer/06 Karma Police.flac",
    )
    db.set_library_pin("Weird Playlist Name", "Different Artist", "trk-kp")
    target = Track("Weird Playlist Name", "Different Artist", "Whatever")

    plex_srv = db.insert_media_server_row({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True})
    sub_srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
    jf_srv = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"})

    # 1. Subsonic
    sub_fake = FakeSubsonic(
        SubsonicState(
            songs=[
                FakeSong(id="s1", title="Karma Police", artist="Radiohead", album="OK Computer", path="/srv/Radiohead/OK Computer/06 Karma Police.flac"),
                FakeSong(id="s2", title="Server Only", artist="Radiohead", album="Alt", path="/srv/Radiohead/Alt/99.flac"),
            ],
            users={"admin": SubUser("admin", admin=True, password="pw")},
        )
    )
    sub = SubsonicMediaServer("http://nav:4533", "admin", "pw", transport=sub_fake.transport(), sleep=lambda _: None)
    sub.server_id = sub_srv["id"]
    sub.legacy_overrides = False
    try:
        sub_hit = sub.match_track(target, db=db)
        assert sub_hit is not None and sub_hit.id == "s1"

        db.add_server_match_override(sub_srv["id"], "Weird Playlist Name", "Different Artist", "s2", "Server Only", "Radiohead")
        sub_override = sub.match_track(target, db=db)
        assert sub_override is not None and sub_override.id == "s2"
    finally:
        sub.close()

    # 2. Jellyfin
    jf_fake = FakeJellyfin(
        JellyfinState(
            songs=[
                FakeAudio(id="j1", title="Karma Police", artist="Radiohead", album="OK Computer", path=r"D:\Audio\Radiohead\OK Computer\06 Karma Police.flac"),
                FakeAudio(id="j2", title="Server Only", artist="Radiohead", album="Alt", path=r"D:\Audio\Radiohead\Alt\99.flac"),
            ],
            users=[JfUser("u-admin", "admin", admin=True)],
        )
    )
    jf = JellyfinMediaServer("http://jf:8096", JELLYFIN_KEY, "admin", transport=jf_fake.transport(), sleep=lambda _: None)
    jf.server_id = jf_srv["id"]
    jf.legacy_overrides = False
    try:
        jf_hit = jf.match_track(target, db=db)
        assert jf_hit is not None and jf_hit.id == "j1"

        db.add_server_match_override(jf_srv["id"], "Weird Playlist Name", "Different Artist", "j2", "Server Only", "Radiohead")
        jf_override = jf.match_track(target, db=db)
        assert jf_override is not None and jf_override.id == "j2"
    finally:
        jf.close()

    # 3. Plex
    plex_pinned_item = SimpleNamespace(
        ratingKey="101",
        title="Karma Police",
        grandparentTitle="Radiohead",
        parentTitle="OK Computer",
        duration=264000,
        media=[SimpleNamespace(parts=[SimpleNamespace(file="/plex/Radiohead/OK Computer/06 Karma Police.flac")])],
    )
    plex_server_override_item = SimpleNamespace(
        ratingKey="202",
        title="Server Only",
        grandparentTitle="Radiohead",
        parentTitle="Alt",
        duration=200000,
        media=[],
    )
    admin_server = MagicMock()
    admin_server.myPlexAccount.return_value = SimpleNamespace(username="admin", id=1, email="", thumb="", users=lambda: [])
    admin_server.search.side_effect = lambda q, **kwargs: [plex_pinned_item] if q == "Karma Police" else []
    admin_server.fetchItem.side_effect = lambda key: plex_server_override_item if int(key) == 202 else None
    with patch("trackseerr.clients.plex.PlexServer"):
        plex = PlexClient("http://plex:32400", "tok")
    plex.server = admin_server
    plex.server_id = plex_srv["id"]
    plex.legacy_overrides = True

    plex_hit = plex.match_track(target, db=db)
    assert plex_hit is plex_pinned_item

    db.add_server_match_override(plex_srv["id"], "Weird Playlist Name", "Different Artist", "202", "Server Only", "Radiohead")
    plex_override = plex.match_track(target, db=db)
    assert plex_override is plex_server_override_item
