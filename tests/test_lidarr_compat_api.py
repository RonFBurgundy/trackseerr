"""Tests for Lidarr-compatible API endpoints (/api/v1/indexer, /api/v1/system/status)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import __version__
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.api.routes.lidarr_compat import LIDARR_COMPAT_VERSION
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="token",
        data_dir=str(tmp_path),
        role="all-in-one",
    )


@pytest.fixture
def api_key(db: Database) -> str:
    return db.get_api_key()


@pytest.fixture
def client(db: Database, config: Config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def test_system_status(client: TestClient, api_key: str):
    """GET /api/v1/system/status returns Lidarr-compatible status with version >= minimum."""
    resp = client.get("/api/v1/system/status", headers={"X-Api-Key": api_key})
    assert resp.status_code == 200
    data = resp.json()

    assert data["appName"] == "TrackSeerr"
    assert data["instanceName"] == "TrackSeerr"
    assert data["version"] == LIDARR_COMPAT_VERSION
    assert data["trackseerrVersion"] == __version__
    assert data["status"] == "ok"
    assert data["isProduction"] is True
    assert data["isAdmin"] is True

    # Check version is >= Prowlarr's minimum (1.0.2.0)
    version_parts = [int(p) for p in data["version"].split(".")[:4]]
    assert version_parts >= [1, 0, 2, 0]

    # Verify X-Application-Version header
    assert resp.headers.get("X-Application-Version") == LIDARR_COMPAT_VERSION


def test_indexer_schema(client: TestClient, api_key: str):
    """GET /api/v1/indexer/schema returns Torznab and Newznab schema templates."""
    resp = client.get("/api/v1/indexer/schema", headers={"X-Api-Key": api_key})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    by_impl = {item["implementation"]: item for item in data}
    assert "Torznab" in by_impl
    assert "Newznab" in by_impl

    # Torznab schema checks
    torznab = by_impl["Torznab"]
    assert torznab["protocol"] == "torrent"
    assert torznab["configContract"] == "TorznabSettings"
    assert torznab["priority"] == 25
    field_names = [f["name"] for f in torznab["fields"]]
    assert "baseUrl" in field_names
    assert "apiPath" in field_names
    assert "apiKey" in field_names
    assert "categories" in field_names
    assert "earlyReleaseLimit" in field_names
    assert "additionalParameters" in field_names
    assert "minimumSeeders" in field_names
    assert "seedCriteria.seedRatio" in field_names
    assert "seedCriteria.seedTime" in field_names
    assert "seedCriteria.discographySeedTime" in field_names
    assert "rejectBlocklistedTorrentHashesWhileGrabbing" in field_names

    # Default categories is [3000, 3010, 3030, 3040]
    cat_field = next(f for f in torznab["fields"] if f["name"] == "categories")
    assert cat_field["value"] == [3000, 3010, 3030, 3040]

    # Default apiPath is "/api"
    api_path_field = next(f for f in torznab["fields"] if f["name"] == "apiPath")
    assert api_path_field["value"] == "/api"

    # Newznab schema checks
    newznab = by_impl["Newznab"]
    assert newznab["protocol"] == "usenet"
    assert newznab["configContract"] == "NewznabSettings"


def test_prowlarr_crud_round_trip(client: TestClient, db: Database, api_key: str):
    """Full Prowlarr-shaped round trip: create, list, get, update, delete."""
    indexer_payload: dict[str, Any] = {
        "id": 0,
        "name": "Redacted (Prowlarr)",
        "enableRss": True,
        "enableAutomaticSearch": True,
        "enableInteractiveSearch": True,
        "priority": 25,
        "implementation": "Torznab",
        "implementationName": "Torznab",
        "configContract": "TorznabSettings",
        "protocol": "torrent",
        "downloadClientId": 0,
        "tags": [],
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/1/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": "prowlarr-api-key-xyz"},
            {"name": "categories", "value": [3000, 3010, 3030]},
            {"name": "minimumSeeders", "value": 5},
            {"name": "seedCriteria.seedRatio", "value": 2.5},
            {"name": "seedCriteria.seedTime", "value": 2880},
            {"name": "seedCriteria.discographySeedTime", "value": 4320},
            {"name": "rejectBlocklistedTorrentHashesWhileGrabbing", "value": False},
        ],
    }

    # 1. POST /api/v1/indexer
    create_resp = client.post(
        "/api/v1/indexer?forceSave=true",
        json=indexer_payload,
        headers={"X-Api-Key": api_key},
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assert isinstance(created["id"], int)
    assert created["id"] > 0
    rowid = created["id"]
    assert created["name"] == "Redacted (Prowlarr)"
    assert created["enableRss"] is True
    assert created["enableAutomaticSearch"] is True
    assert created["enableInteractiveSearch"] is True
    assert created["priority"] == 25
    assert created["implementation"] == "Torznab"
    assert created["protocol"] == "torrent"

    # Check direct DB row
    row = db.get_indexer_by_rowid(rowid)
    assert row is not None
    assert row["name"] == "Redacted (Prowlarr)"
    assert row["host_url"] == "http://192.168.1.5:9696/1/"  # Trailing slash preserved
    assert row["api_key"] == "prowlarr-api-key-xyz"
    assert row["categories"] == "3000,3010,3030"
    assert row["enabled"] is True
    assert row["priority"] == 25
    assert row["minimum_seeders"] == 5
    assert row["seed_ratio"] == 2.5
    assert row["seed_time_minutes"] == 2880
    assert row["discography_seed_time_minutes"] == 4320

    # 2. GET /api/v1/indexer returns identical fields and integer id
    list_resp = client.get("/api/v1/indexer", headers={"X-Api-Key": api_key})
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == rowid
    assert item["name"] == "Redacted (Prowlarr)"
    fields = {f["name"]: f.get("value") for f in item["fields"]}
    assert fields["baseUrl"] == "http://192.168.1.5:9696/1/"
    assert fields["apiPath"] == "/api"
    assert fields["apiKey"] == "prowlarr-api-key-xyz"  # Unmasked!
    assert fields["categories"] == [3000, 3010, 3030]
    assert fields["minimumSeeders"] == 5
    assert fields["seedCriteria.seedRatio"] == 2.5
    assert fields["seedCriteria.seedTime"] == 2880
    assert fields["seedCriteria.discographySeedTime"] == 4320

    # 3. GET /api/v1/indexer/{id}
    get_resp = client.get(f"/api/v1/indexer/{rowid}", headers={"X-Api-Key": api_key})
    assert get_resp.status_code == 200
    single = get_resp.json()
    assert single["id"] == rowid
    assert single["name"] == "Redacted (Prowlarr)"

    # 4. PUT /api/v1/indexer/{id}?forceSave=true
    updated_payload = dict(indexer_payload)
    updated_payload["id"] = rowid
    updated_payload["name"] = "Redacted Updated (Prowlarr)"
    updated_payload["priority"] = 15
    updated_payload["fields"] = [
        {"name": "baseUrl", "value": "http://192.168.1.5:9696/1/"},
        {"name": "apiPath", "value": "/api"},
        {"name": "apiKey", "value": "prowlarr-new-key"},
        {"name": "categories", "value": [3000, 3040]},
        {"name": "minimumSeeders", "value": 10},
        {"name": "seedCriteria.seedRatio", "value": 3.0},
        {"name": "seedCriteria.seedTime", "value": 1440},
        {"name": "seedCriteria.discographySeedTime", "value": 2880},
    ]

    put_resp = client.put(
        f"/api/v1/indexer/{rowid}?forceSave=true",
        json=updated_payload,
        headers={"X-Api-Key": api_key},
    )
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["id"] == rowid
    assert updated["name"] == "Redacted Updated (Prowlarr)"
    assert updated["priority"] == 15

    # Verify DB has updated values
    db_updated = db.get_indexer_by_rowid(rowid)
    assert db_updated is not None
    assert db_updated["name"] == "Redacted Updated (Prowlarr)"
    assert db_updated["api_key"] == "prowlarr-new-key"
    assert db_updated["categories"] == "3000,3040"
    assert db_updated["priority"] == 15
    assert db_updated["minimum_seeders"] == 10
    assert db_updated["seed_ratio"] == 3.0
    assert db_updated["seed_time_minutes"] == 1440
    assert db_updated["discography_seed_time_minutes"] == 2880

    # 5. DELETE /api/v1/indexer/{id}
    del_resp = client.delete(f"/api/v1/indexer/{rowid}", headers={"X-Api-Key": api_key})
    assert del_resp.status_code == 200

    # Subsequent GET returns 404
    get_after_del = client.get(f"/api/v1/indexer/{rowid}", headers={"X-Api-Key": api_key})
    assert get_after_del.status_code == 404
    assert db.get_indexer_by_rowid(rowid) is None


def test_newznab_indexer_round_trip(client: TestClient, db: Database, api_key: str):
    """Newznab indexer creates with usenet protocol and Newznab implementation."""
    payload = {
        "id": 0,
        "name": "NZBGeek (Prowlarr)",
        "implementation": "Newznab",
        "implementationName": "Newznab",
        "configContract": "NewznabSettings",
        "protocol": "usenet",
        "enableRss": True,
        "fields": [
            {"name": "baseUrl", "value": "https://api.nzbgeek.info/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": "geek-key"},
            {"name": "categories", "value": [3000, 3010]},
        ],
    }
    resp = client.post("/api/v1/indexer", json=payload, headers={"X-Api-Key": api_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["protocol"] == "usenet"
    assert data["implementation"] == "Newznab"
    assert data["configContract"] == "NewznabSettings"

    row = db.get_indexer_by_rowid(data["id"])
    assert row is not None
    assert row["indexer_type"] == "newznab"


def test_test_endpoint_paths(client: TestClient, api_key: str):
    """POST /api/v1/indexer/test 200 on success, 400 with Lidarr validation error array on failure."""
    payload = {
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.10:9696/2/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": "test-key"},
            {"name": "categories", "value": [3000]},
        ],
    }

    # 1. Success path
    with patch("plex_playlist_sync.clients.acquisition.torznab.TorznabDriver.test_connection") as mock_test:
        mock_test.return_value = (True, "Torznab Indexer Online")
        resp = client.post("/api/v1/indexer/test?forceTest=true", json=payload, headers={"X-Api-Key": api_key})
        assert resp.status_code == 200
        assert resp.headers.get("X-Application-Version") == LIDARR_COMPAT_VERSION

    # 2. Failure path (returns 400 with Lidarr validation failure array)
    with patch("plex_playlist_sync.clients.acquisition.torznab.TorznabDriver.test_connection") as mock_test:
        mock_test.return_value = (False, "HTTP 401 Unauthorized")
        resp = client.post("/api/v1/indexer/test?forceTest=true", json=payload, headers={"X-Api-Key": api_key})
        assert resp.status_code == 400
        assert resp.headers.get("X-Application-Version") == LIDARR_COMPAT_VERSION
        errors = resp.json()
        assert isinstance(errors, list)
        assert len(errors) == 1
        assert "HTTP 401" in errors[0]["errorMessage"]
        assert errors[0]["severity"] == "error"

    # 3. Exception path
    with patch("plex_playlist_sync.clients.acquisition.torznab.TorznabDriver.test_connection") as mock_test:
        mock_test.side_effect = RuntimeError("Network timeout connecting to indexer")
        resp = client.post("/api/v1/indexer/test?forceTest=true", json=payload, headers={"X-Api-Key": api_key})
        assert resp.status_code == 400
        errors = resp.json()
        assert isinstance(errors, list)
        assert len(errors) == 1
        assert "Network timeout" in errors[0]["errorMessage"]


def test_api_path_validation_error(client: TestClient, api_key: str):
    """apiPath != /api -> 400 array on POST, PUT, and test endpoints."""
    bad_payload = {
        "name": "Invalid ApiPath",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.10:9696/api"},
            {"name": "apiPath", "value": "/wrong_path"},
            {"name": "apiKey", "value": "k"},
            {"name": "categories", "value": [3000]},
        ],
    }

    # POST /indexer
    r_post = client.post("/api/v1/indexer", json=bad_payload, headers={"X-Api-Key": api_key})
    assert r_post.status_code == 400
    errors = r_post.json()
    assert isinstance(errors, list)
    assert errors[0]["propertyName"] == "apiPath"
    assert "/api" in errors[0]["errorMessage"]

    # POST /indexer/test
    r_test = client.post("/api/v1/indexer/test", json=bad_payload, headers={"X-Api-Key": api_key})
    assert r_test.status_code == 400
    errors = r_test.json()
    assert isinstance(errors, list)
    assert errors[0]["propertyName"] == "apiPath"

    # PUT /indexer/{id}
    r_put = client.put("/api/v1/indexer/1", json=bad_payload, headers={"X-Api-Key": api_key})
    assert r_put.status_code in (400, 404)  # 404 if not found, 400 if validation runs first


def test_ssrf_blocked_host_rejected(client: TestClient, api_key: str):
    """SSRF-prohibited host URLs (e.g. cloud metadata) are rejected with 400 array."""
    ssrf_payload = {
        "name": "SSRF Target",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://169.254.169.254/latest/meta-data"},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": "k"},
            {"name": "categories", "value": [3000]},
        ],
    }

    r_post = client.post("/api/v1/indexer", json=ssrf_payload, headers={"X-Api-Key": api_key})
    assert r_post.status_code == 400
    errors = r_post.json()
    assert isinstance(errors, list)
    assert errors[0]["propertyName"] == "baseUrl"
    assert "SSRF" in errors[0]["errorMessage"]

    r_test = client.post("/api/v1/indexer/test", json=ssrf_payload, headers={"X-Api-Key": api_key})
    assert r_test.status_code == 400
    errors = r_test.json()
    assert isinstance(errors, list)
    assert errors[0]["propertyName"] == "baseUrl"


def test_auth_missing_or_invalid_api_key(client: TestClient, api_key: str):
    """Missing or invalid API key results in 401 Unauthorized."""
    # Missing API key
    r_none = client.get("/api/v1/system/status")
    assert r_none.status_code == 401

    # Invalid API key header
    r_bad = client.get("/api/v1/system/status", headers={"X-Api-Key": "invalid-key-xyz"})
    assert r_bad.status_code == 401

    # Valid API key via apikey query parameter
    r_query1 = client.get(f"/api/v1/system/status?apikey={api_key}")
    assert r_query1.status_code == 200

    # Valid API key via api_key query parameter
    r_query2 = client.get(f"/api/v1/system/status?api_key={api_key}")
    assert r_query2.status_code == 200


def test_auth_non_admin_session_forbidden(client: TestClient, db: Database, config: Config):
    """Non-admin user sessions are rejected with 403 Forbidden."""
    user = db.upsert_user("u1", "regular_user", "u1@example.com", is_admin=False)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="regular_user", is_admin=False, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})

    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/v1/system/status", headers=headers)
    assert resp.status_code == 403
    assert "Administrator access required" in resp.json()["detail"]


def test_gateway_tier_denies_routes(db: Database, tmp_path: Path):
    """Gateway tier denies /api/v1/* routes via GatewayGuardMiddleware and require_core_tier."""
    gateway_config = Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="token",
        data_dir=str(tmp_path),
        role="gateway",
        internal_core_secret="a" * 40,
    )
    app = create_app(db=db, config=gateway_config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: gateway_config
    gw_client = TestClient(app)

    # Denied by gateway guard (404) or core tier check (403)
    r_indexer = gw_client.get("/api/v1/indexer")
    assert r_indexer.status_code in (403, 404)

    r_status = gw_client.get("/api/v1/system/status")
    assert r_status.status_code in (403, 404)


def test_unknown_id_404(client: TestClient, api_key: str):
    """Unknown indexer ID returns 404 for GET, PUT, and DELETE."""
    headers = {"X-Api-Key": api_key}
    assert client.get("/api/v1/indexer/99999", headers=headers).status_code == 404
    assert client.delete("/api/v1/indexer/99999", headers=headers).status_code == 404

    payload = {
        "name": "Unknown",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/"},
            {"name": "apiPath", "value": "/api"},
        ],
    }
    assert client.put("/api/v1/indexer/99999", json=payload, headers=headers).status_code == 404


def test_extra_unknown_fields_ignored(client: TestClient, db: Database, api_key: str):
    """Unknown extra fields at root or in fields list are ignored without error."""
    payload = {
        "id": 0,
        "name": "Indexer With Extras",
        "unknownProperty": "ignoreMe",
        "anotherRandomField": 999,
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": "key"},
            {"name": "categories", "value": [3000]},
            {"name": "unknownFieldInside", "value": "something"},
        ],
    }
    resp = client.post("/api/v1/indexer", json=payload, headers={"X-Api-Key": api_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Indexer With Extras"
    assert db.get_indexer_by_rowid(data["id"]) is not None


def test_validation_errors(client: TestClient, api_key: str):
    """Validates name length, invalid categories, and negative seed values."""
    headers = {"X-Api-Key": api_key}

    # Empty name
    p_empty_name = {
        "name": "",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/"},
            {"name": "apiPath", "value": "/api"},
        ],
    }
    r = client.post("/api/v1/indexer", json=p_empty_name, headers=headers)
    assert r.status_code == 400
    assert r.json()[0]["propertyName"] == "name"

    # Name too long (>120 chars)
    p_long_name = dict(p_empty_name)
    p_long_name["name"] = "x" * 121
    r = client.post("/api/v1/indexer", json=p_long_name, headers=headers)
    assert r.status_code == 400
    assert r.json()[0]["propertyName"] == "name"

    # Invalid non-integer categories
    p_bad_cats = {
        "name": "Bad Cats",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "categories", "value": ["not", "ints"]},
        ],
    }
    r = client.post("/api/v1/indexer", json=p_bad_cats, headers=headers)
    assert r.status_code == 400
    assert r.json()[0]["propertyName"] == "categories"

    # Negative seed ratio
    p_neg_ratio = {
        "name": "Neg Ratio",
        "implementation": "Torznab",
        "fields": [
            {"name": "baseUrl", "value": "http://192.168.1.5:9696/"},
            {"name": "apiPath", "value": "/api"},
            {"name": "seedCriteria.seedRatio", "value": -1.5},
        ],
    }
    r = client.post("/api/v1/indexer", json=p_neg_ratio, headers=headers)
    assert r.status_code == 400
    assert r.json()[0]["propertyName"] == "seedCriteria.seedRatio"
