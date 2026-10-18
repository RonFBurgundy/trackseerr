"""Tests for bulk activity operations (queue bulk remove/retry and blocklist bulk remove)."""

from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import activity_service
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import ActiveDownload, DownloadClientConfig
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def users(test_db):
    return {
        "admin": test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False),
    }


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_h(test_db, test_config, users):
    return _headers(users["admin"], test_db, test_config)


@pytest.fixture
def api(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


def _client_row(db, cid="cli-qbit", driver="qbittorrent"):
    db.create_download_client(
        DownloadClientConfig(id=cid, name=f"{driver}-{cid}", driver_type=driver, host_url="http://c:1", enabled=True)
    )


def _dl(db, did, artist="Artist", title="Release", status="downloading", progress=0.0, size=1000, created=None, **extra):
    if db.get_download_client("cli-qbit") is None:
        _client_row(db)
    db.create_active_download(
        ActiveDownload(
            id=did,
            client_id="cli-qbit",
            title=title,
            artist=artist,
            status=status,
            progress=progress,
            size_bytes=size,
            **extra,
        )
    )
    if created:
        db.conn.execute("UPDATE active_downloads SET created_at = ? WHERE id = ?", (created, did))
        db.conn.commit()


def test_bulk_remove_with_blocklist(api, test_db, admin_h):
    _dl(test_db, "dl-1", artist="Artist 1", title="Artist 1 - Album 1 [FLAC]", download_hash="HASH001")
    _dl(test_db, "dl-2", artist="Artist 2", title="Artist 2 - Album 2 [FLAC]", download_hash="HASH002")
    test_db.set_download_release_meta("dl-1", indexer="Prowlarr", quality="FLAC 16bit", protocol="torrent")
    test_db.set_download_release_meta("dl-2", indexer="Prowlarr", quality="FLAC 16bit", protocol="torrent")

    driver = MagicMock()
    with patch.object(activity_service, "get_acquisition_driver", return_value=driver):
        res = api.post(
            "/api/activity/queue/bulk",
            json={
                "ids": ["dl-1", "dl-2"],
                "action": "remove",
                "remove_from_client": True,
                "blocklist": True,
            },
            headers=admin_h,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert len(body["results"]) == 2
    assert all(r["success"] is True for r in body["results"])

    driver.cancel.assert_any_call("HASH001")
    driver.cancel.assert_any_call("HASH002")
    assert driver.cancel.call_count == 2

    assert test_db.get_active_download("dl-1") is None
    assert test_db.get_active_download("dl-2") is None

    bl = test_db.list_blocklist()
    assert len(bl) == 2
    bl_hashes = {r["info_hash"] for r in bl}
    assert bl_hashes == {"hash001", "hash002"}


def test_bulk_remove_partial_missing_id(api, test_db, admin_h):
    _dl(test_db, "dl-real", artist="Real", title="Real - Album [FLAC]", download_hash="HASHREAL")
    test_db.set_download_release_meta("dl-real", indexer="Prowlarr", quality="FLAC 16bit", protocol="torrent")

    driver = MagicMock()
    with patch.object(activity_service, "get_acquisition_driver", return_value=driver):
        res = api.post(
            "/api/activity/queue/bulk",
            json={
                "ids": ["dl-real", "dl-missing"],
                "action": "remove",
                "remove_from_client": True,
                "blocklist": False,
            },
            headers=admin_h,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert len(body["results"]) == 2

    res_real = next(r for r in body["results"] if r["id"] == "dl-real")
    res_missing = next(r for r in body["results"] if r["id"] == "dl-missing")

    assert res_real["success"] is True
    assert res_missing["success"] is False
    assert res_missing["message"] == "Queue item not found"

    assert test_db.get_active_download("dl-real") is None


def test_bulk_retry_calls_retry_per_id(api, test_db, admin_h):
    _dl(test_db, "dl-r1")
    _dl(test_db, "dl-r2")

    with patch(
        "trackseerr.activity_service.native_retry_queue_item",
        return_value={"success": True, "message": "Search queued"},
    ) as mock_retry:
        res = api.post(
            "/api/activity/queue/bulk",
            json={
                "ids": ["dl-r1", "dl-r2"],
                "action": "retry",
                "blocklist": True,
            },
            headers=admin_h,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert len(body["results"]) == 2

    assert mock_retry.call_count == 2
    mock_retry.assert_any_call(ANY, "dl-r1", ANY, True)
    mock_retry.assert_any_call(ANY, "dl-r2", ANY, True)


def test_bulk_blocklist_remove(api, test_db, admin_h):
    it1 = test_db.add_to_blocklist(
        source_title="Artist 1 rel",
        artist="Artist 1",
        album="Album 1",
        indexer="Idx",
        protocol="torrent",
        reason="bad",
    )
    it2 = test_db.add_to_blocklist(
        source_title="Artist 2 rel",
        artist="Artist 2",
        album="Album 2",
        indexer="Idx",
        protocol="torrent",
        reason="bad",
    )

    res = api.post(
        "/api/activity/blocklist/bulk-remove",
        json={"ids": [it1["id"], it2["id"], "bl-unknown"]},
        headers=admin_h,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    assert len(body["results"]) == 3

    r1 = next(r for r in body["results"] if r["id"] == it1["id"])
    r2 = next(r for r in body["results"] if r["id"] == it2["id"])
    runknown = next(r for r in body["results"] if r["id"] == "bl-unknown")

    assert r1["success"] is True and r1["message"] == "Removed from the blocklist"
    assert r2["success"] is True and r2["message"] == "Removed from the blocklist"
    assert runknown["success"] is False and runknown["message"] == "Blocklist item not found"

    assert test_db.get_blocklist_item(it1["id"]) is None
    assert test_db.get_blocklist_item(it2["id"]) is None


def test_bulk_requires_admin(api, test_db, test_config, users):
    alice_h = _headers(users["alice"], test_db, test_config)

    # Queue bulk non-admin -> 403
    res_q_alice = api.post(
        "/api/activity/queue/bulk",
        json={"ids": ["dl-1"], "action": "remove"},
        headers=alice_h,
    )
    assert res_q_alice.status_code == 403

    # Queue bulk unauthenticated -> 401/403
    res_q_anon = api.post(
        "/api/activity/queue/bulk",
        json={"ids": ["dl-1"], "action": "remove"},
    )
    assert res_q_anon.status_code in (401, 403)

    # Blocklist bulk non-admin -> 403
    res_bl_alice = api.post(
        "/api/activity/blocklist/bulk-remove",
        json={"ids": ["bl-1"]},
        headers=alice_h,
    )
    assert res_bl_alice.status_code == 403

    # Blocklist bulk unauthenticated -> 401/403
    res_bl_anon = api.post(
        "/api/activity/blocklist/bulk-remove",
        json={"ids": ["bl-1"]},
    )
    assert res_bl_anon.status_code in (401, 403)


def test_bulk_rejects_empty_ids(api, admin_h):
    # Queue bulk empty ids -> 422
    res_q = api.post(
        "/api/activity/queue/bulk",
        json={"ids": [], "action": "remove"},
        headers=admin_h,
    )
    assert res_q.status_code == 422

    # Blocklist bulk empty ids -> 422
    res_bl = api.post(
        "/api/activity/blocklist/bulk-remove",
        json={"ids": []},
        headers=admin_h,
    )
    assert res_bl.status_code == 422


def test_single_routes_unchanged(api, test_db, admin_h):
    # Test single delete queue item
    _dl(test_db, "dl-single", artist="Single", title="Single - Album [FLAC]", download_hash="SINGLEHASH")
    test_db.set_download_release_meta("dl-single", indexer="Prowlarr", quality="FLAC 16bit", protocol="torrent")

    driver = MagicMock()
    with patch.object(activity_service, "get_acquisition_driver", return_value=driver):
        res = api.delete("/api/activity/queue/dl-single?remove_from_client=true&blocklist=true", headers=admin_h)
    assert res.status_code == 200
    assert res.json()["success"] is True
    driver.cancel.assert_called_once_with("SINGLEHASH")
    assert test_db.get_active_download("dl-single") is None
    assert len(test_db.list_blocklist()) == 1

    # Test single delete blocklist item
    bl_item = test_db.list_blocklist()[0]
    res_bl = api.delete(f"/api/activity/blocklist/{bl_item['id']}", headers=admin_h)
    assert res_bl.status_code == 200
    assert res_bl.json()["success"] is True
    assert test_db.get_blocklist_item(bl_item["id"]) is None
