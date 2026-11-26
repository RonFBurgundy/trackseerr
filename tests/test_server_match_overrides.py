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
from trackseerr.config import Config
from trackseerr.media_servers import SubsonicMediaServer, registry
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.models import Track
from trackseerr.storage import Database
from tests.subsonic_fake import FakeSubsonic, default_state


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
    assert db.get_missing_tracks() == []

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
