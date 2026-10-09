"""Strict-mode backstop for the typed auth, account, users, admin_users, internal and deployment responses.

``ApiModel`` forbids undeclared keys under test, so each request fails if a route returns a key its model lacks. Where a
route serves admins and non-admins both are exercised, and secret material is asserted absent from every body.
"""

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth, local_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_media_client, require_media_server
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

SECRET = "s" * 40
PW = "correct horse battery staple"
APP_URL = "https://music.example.com"
SECRET_KEYS = ("password_hash", "totp_secret", "token_hash", "recovery_hash", "internal_core_secret", "plex_token")


@pytest.fixture(autouse=True)
def _nonces():
    internal_auth._nonce_cache.clear()
    yield
    internal_auth._nonce_cache.clear()


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "t.db"))
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="core",
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251", application_url=APP_URL,
    )


@pytest.fixture
def client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _headers(db: Database, config: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=key)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db, config):
    return _headers(db, config, "admin-1")


@pytest.fixture
def alice(db, config):
    return _headers(db, config, "1001")


def _ok(resp, status: int = 200) -> Any:
    assert resp.status_code == status, resp.text
    body = resp.json()
    text = json.dumps(body)
    for key in SECRET_KEYS:
        assert key not in text, f"{key} leaked in {resp.request.url}"
    return body


def _local_user(db: Database, username: str = "bob") -> dict[str, Any]:
    user, raw = db.create_local_user(username, f"{username}@x.tv", None, "admin-1")
    db.consume_token_set_password(raw, local_auth.hash_password(PW))
    return db.get_user(user["id"])


# ----------------------------------------------------------------------------------------------------------- auth


def test_plex_pin_and_verify(client, db):
    pin = {"id": 99, "code": "ABCD", "auth_url": "https://app.plex.tv/auth#?code=ABCD"}
    with patch("trackseerr.api.routes.auth.create_plex_pin", return_value=pin):
        assert _ok(client.post("/api/auth/plex/pin", json={})) == pin
    with (
        patch("trackseerr.api.routes.auth.check_plex_pin", return_value="plex-tok"),
        patch("trackseerr.api.routes.auth.verify_server_access", return_value=(True, False)),
        patch("trackseerr.api.routes.auth.get_plex_user", return_value={"id": "2002", "username": "carol", "email": "c@x.tv"}),
    ):
        body = _ok(client.post("/api/auth/plex/verify", json={"pin_id": 99, "target_machine_id": "m1"}))
    assert body["token"] and body["user"]["username"] == "carol" and body["user"]["is_admin"] is False
    assert "plex-tok" not in json.dumps(body)


def test_me_logout_admin_and_user(client, admin, alice):
    me = _ok(client.get("/api/auth/me", headers=admin))
    assert me["user"]["is_admin"] is True and me["tier"] == "core"
    assert _ok(client.get("/api/auth/me", headers=alice))["user"]["username"] == "alice"
    assert _ok(client.post("/api/auth/logout", headers=alice)) == {"status": "success", "message": "Successfully logged out"}


def test_local_invite_and_login_flow(client, db, admin):
    created = _ok(client.post("/api/admin/users", json={"username": "dave", "email": "d@x.tv"}, headers=admin), 201)
    token = created["invite_url"].rsplit("/", 1)[1]
    info = _ok(client.get(f"/api/auth/invite/{token}"))
    assert info["username"] == "dave" and info["purpose"]
    assert _ok(client.post(f"/api/auth/invite/{token}", json={"password": PW})) == {"status": "success", "username": "dave"}
    login = _ok(client.post("/api/auth/local/login", json={"username": "dave", "password": PW}))
    assert login["user"]["username"] == "dave" and login["mfa_enrollment_required"] is False


# --------------------------------------------------------------------------------------------------------- account


def test_account_profile_password_and_mfa(client, db, config):
    user = _local_user(db)
    h = _headers(db, config, user["id"])
    acct = _ok(client.get("/api/account", headers=h))
    assert acct["auth_type"] == "local" and acct["quotas"]["used"]["tracks"] == 0 and "auto_approve" in acct
    setup = _ok(client.post("/api/account/mfa/setup", json={"password": PW}, headers=h))
    code = local_auth.totp_code(setup["secret"])
    confirmed = _ok(client.post("/api/account/mfa/confirm", json={"code": code}, headers=h))
    assert len(confirmed["recovery_codes"]) >= 1
    assert _ok(client.get("/api/account", headers=h))["mfa_enabled"] is True
    # a fresh TOTP counter is required for each re-auth: step forward one period
    import time
    nxt = local_auth.totp_code(setup["secret"], for_time=time.time() + 30)
    with patch("trackseerr.local_auth.time.time", return_value=time.time() + 30):
        regen = _ok(client.post("/api/account/mfa/recovery-codes", json={"password": PW, "code": nxt}, headers=h))
    assert regen["recovery_codes"]
    nxt2 = local_auth.totp_code(setup["secret"], for_time=time.time() + 60)
    with patch("trackseerr.local_auth.time.time", return_value=time.time() + 60):
        assert _ok(client.post("/api/account/mfa/disable", json={"password": PW, "code": nxt2}, headers=h)) == {"status": "success"}
    changed = _ok(client.post("/api/account/password", json={"current_password": PW, "new_password": PW + " again"}, headers=h))
    assert changed == {"status": "success"}


# ----------------------------------------------------------------------------------------------------------- users


def test_users_me_list_update_refresh(client, db, admin, alice):
    mine = _ok(client.get("/api/users/me", headers=alice))
    assert mine["is_admin"] is False and mine["quotas"]["used"]["albums"] == 0 and mine["tier"] == "core"
    adm = _ok(client.get("/api/users/me", headers=admin))
    assert adm["is_admin"] is True and adm["quotas"]["albums"] is None
    users = _ok(client.get("/api/users", headers=admin))
    assert {u["username"] for u in users} == {"root", "alice"}
    assert client.get("/api/users", headers=alice).status_code == 403
    updated = _ok(client.put("/api/users/1001", json={"request_limit_quota": 5}, headers=admin))
    assert updated["request_limit_quota"] == 5
    assert _ok(client.put("/api/users/1001", json={}, headers=admin))["id"] == "1001"

    class FakeServer:
        kind = "plex"

        def list_users(self):
            return []

    app = client.app
    app.dependency_overrides[get_media_client] = lambda: object()
    app.dependency_overrides[require_media_server] = lambda: None
    try:
        with patch("trackseerr.api.routes.users.as_media_server", return_value=FakeServer()), patch(
            "trackseerr.api.routes.users.import_server_users", return_value=(0, 0)
        ):
            assert len(_ok(client.post("/api/users/refresh", headers=admin))) == 2
    finally:
        app.dependency_overrides.pop(get_media_client, None)
        app.dependency_overrides.pop(require_media_server, None)


# ----------------------------------------------------------------------------------------------------- admin users


def test_admin_users_full_lifecycle(client, db, admin):
    perms = _ok(client.get("/api/admin/permissions", headers=admin))
    assert perms[0]["name"] == "ADMIN"
    listed = _ok(client.get("/api/admin/users", headers=admin))
    assert {u["username"] for u in listed} == {"root", "alice"} and listed[0]["quotas"]["effective"]["window_days"]
    created = _ok(client.post("/api/admin/users", json={"username": "erin", "quotas": {"tracks": 3}}, headers=admin), 201)
    uid = created["user"]["id"]
    assert created["user"]["quotas"]["overrides"]["tracks"] == 3 and created["invite_url"].startswith(APP_URL)
    assert _ok(client.patch(f"/api/admin/users/{uid}", json={"email": "e@x.tv"}, headers=admin))["email"] == "e@x.tv"
    # reset-password / reset-mfa require a local account
    assert _ok(client.post(f"/api/admin/users/{uid}/reset-password", headers=admin))["reset_url"].startswith(APP_URL)
    assert _ok(client.post(f"/api/admin/users/{uid}/reset-mfa", headers=admin))["id"] == uid
    assert _ok(client.post(f"/api/admin/users/{uid}/disable", headers=admin))["disabled"] is True
    assert _ok(client.post(f"/api/admin/users/{uid}/enable", headers=admin))["disabled"] is False
    assert _ok(client.post(f"/api/admin/users/{uid}/revoke-sessions", headers=admin)) == {"status": "success", "id": uid}
    assert _ok(client.request("DELETE", f"/api/admin/users/{uid}", json={"confirm_username": "erin"}, headers=admin)) == {"status": "deleted", "id": uid}
    # restore a removed Plex user
    assert _ok(client.request("DELETE", "/api/admin/users/1001", json={"confirm_username": "alice"}, headers=admin))["status"] == "deleted"
    assert _ok(client.post("/api/admin/users/1001/restore", headers=admin)) == {"status": "restored", "id": "1001"}
    settings = _ok(client.get("/api/admin/settings/accounts", headers=admin))
    assert set(settings) == {"require_mfa_local", "default_quota_tracks", "default_quota_albums", "default_quota_discographies", "default_quota_window_days"}
    assert _ok(client.put("/api/admin/settings/accounts", json={"default_quota_tracks": 7}, headers=admin))["default_quota_tracks"] == 7


def test_admin_users_refused_for_non_admin(client, alice):
    assert client.get("/api/admin/users", headers=alice).status_code == 403


# ---------------------------------------------------------------------------------------------- internal + deployment


def _signed(method: str, target: str, body: Optional[dict] = None) -> tuple[dict[str, str], bytes]:
    raw = json.dumps(body).encode() if body is not None else b""
    headers = internal_auth.sign_assertion(SECRET, method, target, "", "", raw)
    if raw:
        headers["Content-Type"] = "application/json"
    return headers, raw


def test_internal_endpoints(client, db):
    _local_user(db, "frank")
    h, raw = _signed("GET", "/api/internal/hello")
    hello = _ok(client.get("/api/internal/hello", headers=h))
    assert set(hello) == {"protocol", "version", "role", "instance_id"}
    h, raw = _signed("POST", "/api/internal/auth/local/verify", {"username": "frank", "password": PW, "client_ip": "10.0.0.5"})
    verified = _ok(client.post("/api/internal/auth/local/verify", content=raw, headers=h))
    assert verified["user"]["username"] == "frank" and verified["mfa_required"] is False and "session_floor_us" in verified
    h, raw = _signed("POST", "/api/internal/auth/session-status", {"user_id": "1001", "session_issued_at": 1})
    assert _ok(client.post("/api/internal/auth/session-status", content=raw, headers=h)) == {"valid": True}
    db.set_disabled("1001", True)
    h, raw = _signed("POST", "/api/internal/auth/session-status", {"user_id": "1001", "session_issued_at": 1})
    assert _ok(client.post("/api/internal/auth/session-status", content=raw, headers=h)) == {"valid": False, "reason": "disabled"}
    body = {"version": "1.0.0", "protocol": 1, "gateway_id": "gw-1", "started_at": 1.0, "active_sessions": 2}
    h, raw = _signed("POST", "/api/internal/gateway-heartbeat", body)
    assert _ok(client.post("/api/internal/gateway-heartbeat", content=raw, headers=h))["ok"] is True


def test_deployment_endpoints(client, db, admin):
    status = _ok(client.get("/api/admin/gateway-status", headers=admin))
    assert status["state"] == "never_seen" and status["public_url"] == APP_URL
    quiet = _ok(client.get("/api/admin/role-change-notice", headers=admin))
    assert quiet["active"] is False and quiet["checklist"] == []
    db.set_role_change_notice({"from_role": "all-in-one", "to_role": "core", "changed_at": "2026-01-01T00:00:00+00:00", "dismissed": False})
    live = _ok(client.get("/api/admin/role-change-notice", headers=admin))
    assert live["active"] is True and live["to_role"] == "core" and live["checklist"]
    assert _ok(client.post("/api/admin/role-change-notice/dismiss", headers=admin))["active"] is False
