"""Import list REST API: CRUD, secret masking, test endpoint, sync trigger, items paging, validation, authz;
plus the playlist monitor_mode field."""

from typing import Any
from unittest.mock import patch

import pytest

from plex_playlist_sync.api.dependencies import get_config
from plex_playlist_sync.clients.import_lists import ImportListError, ImportListItem
from plex_playlist_sync.config import Config
from plex_playlist_sync.import_list_worker import claim_sync, release_sync

# Reuse the authenticated TestClient fixtures of the library API tests.
from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

LASTFM = {"username": "ron", "api_key": "SECRET-KEY", "source": "loved_tracks", "limit": 50}


def body(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = dict(
        name="My loved", provider="lastfm", config=dict(LASTFM), enabled=True, monitor_mode="track",
        artist_monitor_option=None, quality_profile_id=None, sync_interval_minutes=1440,
    )
    data.update(kw)
    return data


@pytest.fixture
def admin(test_db, test_config, seeded_users):
    return _auth_headers(seeded_users["admin"], test_db, test_config)


def test_providers_metadata(app_and_client, admin):
    _, client = app_and_client
    res = client.get("/api/import-lists/providers", headers=admin)
    assert res.status_code == 200
    data = {p["provider"]: p for p in res.json()}
    assert set(data) == {"lastfm", "listenbrainz", "musicbrainz_collection"}
    lastfm = data["lastfm"]
    assert lastfm["label"] == "Last.fm" and lastfm["sources"] == ["loved_tracks", "top_artists", "top_albums", "top_tracks"]
    fields = {f["key"]: f for f in lastfm["fields"]}
    assert fields["api_key"] == {"key": "api_key", "label": "API key (blank uses LASTFM_API_KEY)", "type": "secret", "required": False}
    assert fields["username"]["required"] is True
    assert fields["source"]["type"] == "select" and "top_albums" in fields["source"]["options"]
    assert fields["limit"]["type"] == "number"


def test_create_get_list_shapes_and_secret_masking(app_and_client, admin, test_db):
    _, client = app_and_client
    res = client.post("/api/import-lists", json=body(), headers=admin)
    assert res.status_code == 201
    created = res.json()
    assert set(created) == {
        "id", "name", "provider", "config", "enabled", "monitor_mode", "artist_monitor_option", "quality_profile_id",
        "sync_interval_minutes", "last_synced_at", "last_status", "last_error", "item_counts", "created_at", "updated_at",
        "tags",
    }
    assert created["config"]["api_key"] == "********" and created["config"]["username"] == "ron"
    assert created["item_counts"] == {"applied": 0, "pending": 0, "unresolved": 0, "failed": 0, "skipped": 0}
    assert created["last_status"] is None
    assert test_db.get_import_list(created["id"])["config"]["api_key"] == "SECRET-KEY"  # stored for real

    assert client.get(f"/api/import-lists/{created['id']}", headers=admin).json() == created
    listing = client.get("/api/import-lists", headers=admin).json()
    assert [l["id"] for l in listing] == [created["id"]]
    assert "SECRET-KEY" not in client.get("/api/import-lists", headers=admin).text


def test_blank_secret_is_not_masked(app_and_client, admin):
    _, client = app_and_client
    cfg = {**LASTFM, "api_key": ""}
    created = client.post("/api/import-lists", json=body(config=cfg), headers=admin).json()
    assert created["config"]["api_key"] == ""


def test_put_with_placeholder_keeps_stored_secret_and_new_value_replaces(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    res = client.put(
        f"/api/import-lists/{lid}",
        json=body(name="Renamed", monitor_mode="album", artist_monitor_option="albums", sync_interval_minutes=120,
                  config={**LASTFM, "api_key": "********", "limit": 10}),
        headers=admin,
    )
    assert res.status_code == 200
    out = res.json()
    assert (out["name"], out["monitor_mode"], out["artist_monitor_option"], out["sync_interval_minutes"]) == ("Renamed", "album", "albums", 120)
    assert out["config"]["api_key"] == "********" and out["config"]["limit"] == 10
    stored = test_db.get_import_list(lid)
    assert stored["config"]["api_key"] == "SECRET-KEY"

    client.put(f"/api/import-lists/{lid}", json=body(config={**LASTFM, "api_key": "NEW-KEY"}), headers=admin)
    assert test_db.get_import_list(lid)["config"]["api_key"] == "NEW-KEY"


def test_put_provider_change_does_not_carry_old_secret(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    res = client.put(
        f"/api/import-lists/{lid}",
        json=body(provider="listenbrainz", config={"source": "top_artists", "username": "u", "token": "********"}),
        headers=admin,
    )
    assert res.status_code == 200
    assert "token" not in test_db.get_import_list(lid)["config"]


def test_get_put_delete_unknown_list_404(app_and_client, admin):
    _, client = app_and_client
    assert client.get("/api/import-lists/nope", headers=admin).status_code == 404
    assert client.put("/api/import-lists/nope", json=body(), headers=admin).status_code == 404
    assert client.delete("/api/import-lists/nope", headers=admin).status_code == 404
    assert client.post("/api/import-lists/nope/sync", headers=admin).status_code == 404
    assert client.get("/api/import-lists/nope/items", headers=admin).status_code == 404


def test_delete_cascades_items(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    test_db.upsert_import_list_items(lid, [ImportListItem(kind="artist", external_key="a", artist_name="A").to_row()])
    assert client.delete(f"/api/import-lists/{lid}", headers=admin).json() == {"status": "deleted", "id": lid}
    assert test_db.conn.execute("SELECT COUNT(*) FROM import_list_items").fetchone()[0] == 0
    assert client.get("/api/import-lists", headers=admin).json() == []


@pytest.mark.parametrize(
    "patch_body",
    [
        {"monitor_mode": "everything"},
        {"provider": "spotify"},
        {"sync_interval_minutes": 59},
        {"sync_interval_minutes": 0},
        {"artist_monitor_option": "bogus"},
        {"name": "   "},
        {"config": {"source": "loved_tracks"}},  # missing required username
    ],
)
def test_validation_errors_are_422(app_and_client, admin, test_db, patch_body):
    _, client = app_and_client
    assert client.post("/api/import-lists", json=body(**patch_body), headers=admin).status_code == 422
    assert test_db.list_import_lists() == []
    lid = test_db.create_import_list(body())["id"]
    assert client.put(f"/api/import-lists/{lid}", json=body(**patch_body), headers=admin).status_code == 422
    assert client.post("/api/import-lists/test", json=body(**patch_body), headers=admin).status_code == 422


def test_boundary_interval_and_option_null_accepted(app_and_client, admin):
    _, client = app_and_client
    res = client.post("/api/import-lists", json=body(sync_interval_minutes=60, artist_monitor_option=""), headers=admin)
    assert res.status_code == 201 and res.json()["artist_monitor_option"] is None


def test_test_endpoint_reads_only_and_limits_sample(app_and_client, admin, test_db):
    _, client = app_and_client
    items = [ImportListItem(kind="track", external_key=f"k{i}", artist_name="A", track_title=f"T{i}") for i in range(25)]
    with patch("plex_playlist_sync.api.routes.import_lists.fetch_items", return_value=items) as fetch:
        res = client.post("/api/import-lists/test", json=body(), headers=admin)
    assert res.status_code == 200
    out = res.json()
    assert out["ok"] is True and out["item_count"] == 25 and out["error"] is None and len(out["sample"]) == 10
    assert out["sample"][0] == {
        "id": 0, "kind": "track", "mbid": None, "artist_name": "A", "album_title": "", "track_title": "T0",
        "status": "pending", "applied_level": None, "error": None, "first_seen_at": None, "last_seen_at": None,
    }
    assert fetch.call_args.args[0] == "lastfm" and fetch.call_args.args[1]["api_key"] == "SECRET-KEY"
    assert test_db.list_import_lists() == []
    assert test_db.conn.execute("SELECT COUNT(*) FROM import_list_items").fetchone()[0] == 0
    assert test_db.list_library_artists() == [] and test_db.list_requests() == []


def test_test_endpoint_reports_provider_error_and_uses_stored_secret(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    with patch("plex_playlist_sync.api.routes.import_lists.fetch_items", side_effect=ImportListError("provider returned HTTP 401")) as fetch:
        res = client.post(f"/api/import-lists/test?list_id={lid}", json=body(config={**LASTFM, "api_key": "********"}), headers=admin)
    assert res.json() == {"ok": False, "item_count": 0, "sample": [], "error": "provider returned HTTP 401"}
    assert fetch.call_args.args[1]["api_key"] == "SECRET-KEY"


def test_sync_endpoint_queues_runs_in_background_and_409_when_busy(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    with patch("plex_playlist_sync.api.routes.import_lists.sync_import_list") as sync:
        res = client.post(f"/api/import-lists/{lid}/sync", headers=admin)
    assert res.status_code == 200 and res.json() == {"queued": True}
    assert sync.call_args.args[1] == lid and isinstance(sync.call_args.kwargs["claim_token"], str)

    assert claim_sync(lid)
    try:
        assert client.post(f"/api/import-lists/{lid}/sync", headers=admin).status_code == 409
    finally:
        release_sync(lid)


def test_sync_endpoint_end_to_end_records_items(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(monitor_mode="none"), headers=admin).json()["id"]
    items = [ImportListItem(kind="track", external_key="a|b", artist_name="A", track_title="B")]
    with patch("plex_playlist_sync.import_list_worker.fetch_items", return_value=items):
        assert client.post(f"/api/import-lists/{lid}/sync", headers=admin).json() == {"queued": True}
    got = client.get(f"/api/import-lists/{lid}", headers=admin).json()
    assert got["last_status"] == "ok" and got["item_counts"]["skipped"] == 1


def test_sync_endpoint_background_failure_is_recorded_not_raised(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    with patch("plex_playlist_sync.api.routes.import_lists.sync_import_list", side_effect=RuntimeError("boom")):
        assert client.post(f"/api/import-lists/{lid}/sync", headers=admin).status_code == 200
    assert test_db.get_import_list(lid)["last_status"] == "error"


def test_items_paging_and_status_filter(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    test_db.upsert_import_list_items(
        lid, [ImportListItem(kind="artist", external_key=f"a{i}", artist_name=f"Artist {i}").to_row() for i in range(7)]
    )
    pending = test_db.list_pending_import_items(lid)
    test_db.update_import_list_item(pending[0]["id"], "applied", "artist", None, "mbid-1")
    test_db.update_import_list_item(pending[1]["id"], "failed", None, "boom")

    page = client.get(f"/api/import-lists/{lid}/items?limit=3&offset=0", headers=admin).json()
    assert page["total"] == 7 and len(page["items"]) == 3
    assert set(page["items"][0]) == {
        "id", "kind", "mbid", "artist_name", "album_title", "track_title", "status", "applied_level", "error",
        "first_seen_at", "last_seen_at",
    }
    page2 = client.get(f"/api/import-lists/{lid}/items?limit=3&offset=6", headers=admin).json()
    assert page2["total"] == 7 and len(page2["items"]) == 1
    applied = client.get(f"/api/import-lists/{lid}/items?status=applied", headers=admin).json()
    assert applied["total"] == 1 and applied["items"][0]["mbid"] == "mbid-1" and applied["items"][0]["applied_level"] == "artist"
    assert client.get(f"/api/import-lists/{lid}/items?status=failed", headers=admin).json()["items"][0]["error"] == "boom"
    assert client.get(f"/api/import-lists/{lid}/items?status=bogus", headers=admin).status_code == 422
    assert client.get(f"/api/import-lists/{lid}/items?limit=0", headers=admin).status_code == 422
    assert client.get(f"/api/import-lists/{lid}", headers=admin).json()["item_counts"] == {
        "applied": 1, "pending": 5, "unresolved": 0, "failed": 1, "skipped": 0,
    }


def test_authz_requires_admin_and_core_tier(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    lid = test_db.create_import_list(body())["id"]
    calls = [
        ("get", "/api/import-lists", None),
        ("post", "/api/import-lists", body()),
        ("get", "/api/import-lists/providers", None),
        ("post", "/api/import-lists/test", body()),
        ("get", f"/api/import-lists/{lid}", None),
        ("put", f"/api/import-lists/{lid}", body()),
        ("delete", f"/api/import-lists/{lid}", None),
        ("post", f"/api/import-lists/{lid}/sync", None),
        ("get", f"/api/import-lists/{lid}/items", None),
    ]
    for method, url, payload in calls:
        kw = {"json": payload} if payload is not None else {}
        assert getattr(client, method)(url, **kw).status_code == 401, (method, url)
        assert getattr(client, method)(url, headers=alice, **kw).status_code == 403, (method, url)

    gateway = Config(plex_url="http://x", plex_token="t", data_dir=test_config.data_dir, role="gateway")
    app.dependency_overrides[get_config] = lambda: gateway
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    # A gateway-role app hides Core-only routes entirely (deny-by-default 404); require_core_tier is the backstop.
    assert client.get("/api/import-lists", headers=admin).status_code in (403, 404)


# ------------------------------------------------------------------ playlist monitor_mode


def test_playlist_monitor_mode_in_get_and_update(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.upsert_playlist("pl-1", "Mix", creator_id="admin-1")
    listing = client.get("/api/playlists", headers=admin).json()
    assert listing[0]["monitor_mode"] == "track"

    res = client.put("/api/playlists/pl-1/enabled", json={"monitor_mode": "album"}, headers=admin)
    assert res.status_code == 200 and res.json() == {"id": "pl-1", "enabled": True, "monitor_mode": "album"}
    assert client.get("/api/playlists", headers=admin).json()[0]["monitor_mode"] == "album"

    res = client.put("/api/playlists/pl-1/enabled", json={"enabled": False, "monitor_mode": "none"}, headers=admin)
    assert res.json() == {"id": "pl-1", "enabled": False, "monitor_mode": "none"}
    # Updating only `enabled` keeps the mode.
    res = client.put("/api/playlists/pl-1/enabled", json={"enabled": True}, headers=admin)
    assert res.json() == {"id": "pl-1", "enabled": True, "monitor_mode": "none"}

    res = client.put("/api/playlists/pl-1/monitor-mode", json={"monitor_mode": "artist"}, headers=admin)
    assert res.json() == {"id": "pl-1", "monitor_mode": "artist"}
    assert test_db.get_playlist("pl-1")["monitor_mode"] == "artist"


def test_playlist_monitor_mode_validation_and_ownership(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    test_db.upsert_playlist("pl-1", "Mix", creator_id="admin-1")
    assert client.put("/api/playlists/pl-1/enabled", json={"monitor_mode": "bogus"}, headers=admin).status_code == 422
    assert client.put("/api/playlists/pl-1/enabled", json={}, headers=admin).status_code == 422
    assert client.put("/api/playlists/pl-1/monitor-mode", json={"monitor_mode": "bogus"}, headers=admin).status_code == 422
    assert client.put("/api/playlists/pl-1/monitor-mode", json={"monitor_mode": "album"}, headers=alice).status_code == 404
    assert client.put("/api/playlists/nope/monitor-mode", json={"monitor_mode": "album"}, headers=admin).status_code == 404
    assert test_db.get_playlist("pl-1")["monitor_mode"] == "track"


# ------------------------------------------------------------------ review fixes


def test_non_admin_cannot_set_album_or_artist_monitor_mode(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    test_db.upsert_playlist("pl-a", "Mine", creator_id="user-alice")
    for mode in ("album", "artist"):
        assert client.put("/api/playlists/pl-a/monitor-mode", json={"monitor_mode": mode}, headers=alice).status_code == 403
        assert client.put("/api/playlists/pl-a/enabled", json={"monitor_mode": mode}, headers=alice).status_code == 403
        assert client.put("/api/playlists/pl-a/enabled", json={"enabled": False, "monitor_mode": mode}, headers=alice).status_code == 403
    pl = test_db.get_playlist("pl-a")
    assert pl["monitor_mode"] == "track" and pl["enabled"]  # nothing was written, not even `enabled`
    # Track mode needs the auto-request permission; list-only (none) never does.
    assert client.put("/api/playlists/pl-a/monitor-mode", json={"monitor_mode": "none"}, headers=alice).status_code == 200
    assert client.put("/api/playlists/pl-a/enabled", json={"monitor_mode": "none"}, headers=alice).status_code == 200
    assert client.put("/api/playlists/pl-a/monitor-mode", json={"monitor_mode": "track"}, headers=alice).status_code == 403
    test_db.update_user_admin_fields("user-alice", {"permissions": 34 | 128})
    for mode in ("none", "track"):
        assert client.put("/api/playlists/pl-a/monitor-mode", json={"monitor_mode": mode}, headers=alice).status_code == 200
        assert client.put("/api/playlists/pl-a/enabled", json={"monitor_mode": mode}, headers=alice).status_code == 200
    assert client.put("/api/playlists/pl-a/enabled", json={"enabled": False}, headers=alice).status_code == 200


def test_admin_can_set_album_and_artist_monitor_mode(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.upsert_playlist("pl-a", "Mine", creator_id="user-alice")
    assert client.put("/api/playlists/pl-a/monitor-mode", json={"monitor_mode": "artist"}, headers=admin).status_code == 200
    assert client.put("/api/playlists/pl-a/enabled", json={"monitor_mode": "album"}, headers=admin).status_code == 200


def test_direct_import_does_not_run_monitor_apply_on_the_request_thread(app_and_client, test_db, test_config, seeded_users):
    import threading
    from plex_playlist_sync.api.dependencies import get_plex_client
    from unittest.mock import MagicMock

    app, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    plex = MagicMock()
    plex.sync_playlist_to_users.return_value = [MagicMock(success=True)]
    plex.match_playlist_tracks.return_value = ([], [{"title": "T", "artist": "A", "album": ""}])
    app.dependency_overrides[get_plex_client] = lambda: plex
    started, release = threading.Event(), threading.Event()
    thread_names: list[str] = []

    def slow_apply(db, config, playlist_id):
        thread_names.append(threading.current_thread().name)
        started.set()
        release.wait(timeout=10)

    try:
        with patch("plex_playlist_sync.api.routes.playlists.apply_playlist_missing_safely", side_effect=slow_apply):
            res = client.post(
                "/api/playlists/import",
                json={"name": "Imp", "service": "custom", "tracks": [{"title": "T", "artist": "A"}], "targets": ["admin-1"]},
                headers=admin,
            )
            # The route has already answered while the apply is still blocked.
            assert res.status_code == 201, res.text
            assert started.wait(timeout=5)
            assert not release.is_set()
            assert thread_names[0] != threading.current_thread().name
    finally:
        release.set()
        app.dependency_overrides.pop(get_plex_client, None)


def test_test_endpoint_merges_stored_secrets_only_for_same_provider(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(), headers=admin).json()["id"]
    with patch("plex_playlist_sync.api.routes.import_lists.fetch_items", return_value=[]) as fetch:
        client.post(f"/api/import-lists/test?list_id={lid}", json=body(config={**LASTFM, "api_key": "********"}), headers=admin)
        assert fetch.call_args.args[1]["api_key"] == "SECRET-KEY"
        # Same placeholder aimed at a different provider must not pick up Last.fm's stored key.
        client.post(
            f"/api/import-lists/test?list_id={lid}",
            json=body(provider="listenbrainz", config={"source": "top_artists", "username": "u", "token": "********"}),
            headers=admin,
        )
        assert "token" not in fetch.call_args.args[1]
        assert "SECRET-KEY" not in fetch.call_args.args[1].values()


def test_put_monitor_mode_change_resets_skipped_items(app_and_client, admin, test_db):
    _, client = app_and_client
    lid = client.post("/api/import-lists", json=body(monitor_mode="none"), headers=admin).json()["id"]
    test_db.upsert_import_list_items(lid, [ImportListItem(kind="artist", external_key="a", artist_name="A").to_row()])
    item_id = test_db.list_pending_import_items(lid)[0]["id"]
    test_db.update_import_list_item(item_id, "skipped")
    res = client.put(f"/api/import-lists/{lid}", json=body(monitor_mode="album"), headers=admin)
    assert res.status_code == 200
    assert res.json()["item_counts"]["pending"] == 1 and res.json()["item_counts"]["skipped"] == 0
