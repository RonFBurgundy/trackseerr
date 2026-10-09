"""Admin user management (/api/admin/users*, /api/admin/settings/accounts): guards, secrecy, tombstones."""

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth, local_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes import admin_users
from trackseerr.api.tier_middleware import (
    GATEWAY_FORWARD_ALLOWLIST,
    GATEWAY_FORWARD_SERVICE_ALLOWLIST,
    GATEWAY_LOCAL_ALLOWLIST,
    _allowed,
)
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import MusicRequest, RequestStatus, UserPermission
from trackseerr.storage import Database

SECRET = "s" * 40
PW = "correct horse battery staple"
APP_URL = "https://music.example.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    internal_auth._nonce_cache.clear()
    monkeypatch.delenv("APPLICATION_URL", raising=False)
    monkeypatch.delenv("APP_URL", raising=False)
    yield
    internal_auth._nonce_cache.clear()


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    d.upsert_user("1002", "bob", "bo@x.tv", is_admin=False)
    yield d
    d.close()


def _config(tmp_path: Path, role: str = "core", application_url: Optional[str] = APP_URL) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET if role != "all-in-one" else None,
        trackseerr_core_url="http://core.internal:5251",
        application_url=application_url,
    )


def _client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def _session(db: Database, cfg: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(db, tmp_path):
    cfg = _config(tmp_path)
    return {
        "client": _client(db, cfg),
        "db": db,
        "cfg": cfg,
        "admin": _session(db, cfg, "admin-1"),
        "alice": _session(db, cfg, "1001"),
    }


def _mk_local(db: Database, username: str = "carol", perms: Optional[int] = None) -> dict[str, Any]:
    user, raw = db.create_local_user(username, f"{username}@x.tv", perms, "admin-1")
    assert db.consume_token_set_password(raw, local_auth.hash_password(PW)) == user["id"]
    return db.get_user(user["id"])


def _enroll_mfa(db: Database, user_id: str) -> tuple[str, list[str]]:
    secret = local_auth.generate_totp_secret()
    codes = local_auth.generate_recovery_codes()
    db.enable_totp(user_id, secret, 1, [local_auth.hash_recovery_code(c) for c in codes])
    return secret, codes


def _delete(client: TestClient, url: str, headers: dict[str, str], body: Optional[dict[str, Any]] = None):
    return client.request("DELETE", url, json=body, headers=headers)


USER_KEYS = {
    "id", "username", "email", "auth_type", "is_admin", "permissions", "disabled", "mfa_enabled",
    "last_login_at", "created_at", "quotas", "usage",
}


# --------------------------------------------------------------------------- access control

ADMIN_ROUTES = [
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users", {"username": "newbie"}),
    ("PATCH", "/api/admin/users/1001", {"permissions": 2}),
    ("POST", "/api/admin/users/1001/disable", None),
    ("POST", "/api/admin/users/1001/enable", None),
    ("POST", "/api/admin/users/1001/reset-password", None),
    ("POST", "/api/admin/users/1001/reset-mfa", None),
    ("POST", "/api/admin/users/1001/revoke-sessions", None),
    ("DELETE", "/api/admin/users/1001", {"confirm_username": "alice"}),
    ("POST", "/api/admin/users/1001/restore", None),
    ("GET", "/api/admin/settings/accounts", None),
    ("PUT", "/api/admin/settings/accounts", {"default_quota_tracks": 5}),
    ("GET", "/api/admin/permissions", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_unauthenticated_is_401(env, method, path, body):
    assert env["client"].request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_non_admin_is_403_and_changes_nothing(env, method, path, body):
    res = env["client"].request(method, path, json=body, headers=env["alice"])
    assert res.status_code == 403
    assert env["db"].get_user("1001") is not None
    assert not env["db"].is_tombstoned("1001")
    assert env["db"].get_user_by_username("newbie") is None


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_forwarded_admin_is_403(env, method, path, body):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = internal_auth.sign_assertion(SECRET, method, path, "admin-1", "root", raw)
    if raw:
        headers["Content-Type"] = "application/json"
    res = env["client"].request(method, path, content=raw or None, headers=headers)
    assert res.status_code == 403
    assert env["db"].get_user("1001") is not None


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_gateway_returns_404_even_for_admin_sessions(db, tmp_path, method, path, body):
    cfg = _config(tmp_path, "gateway")
    client = _client(db, cfg)
    headers = _session(db, cfg, "admin-1")
    assert client.request(method, path, json=body, headers=headers).status_code == 404
    assert client.request(method, path, json=body).status_code == 404


def test_admin_paths_are_on_no_gateway_allowlist():
    tables = (GATEWAY_LOCAL_ALLOWLIST, GATEWAY_FORWARD_ALLOWLIST, GATEWAY_FORWARD_SERVICE_ALLOWLIST)
    for method, path, _ in ADMIN_ROUTES:
        for table in tables:
            assert not _allowed(table, method, path), (method, path)


def test_api_key_cannot_manage_users(env):
    key = env["db"].get_api_key()
    res = env["client"].get("/api/admin/users", headers={"X-Api-Key": key})
    assert res.status_code == 403
    res = env["client"].post("/api/admin/users", json={"username": "viakey"}, headers={"X-Api-Key": key})
    assert res.status_code == 403
    assert env["db"].get_user_by_username("viakey") is None


def test_all_in_one_tier_serves_admin_routes(db, tmp_path):
    cfg = _config(tmp_path, "all-in-one")
    client = _client(db, cfg)
    res = client.get("/api/admin/users", headers=_session(db, cfg, "admin-1"))
    assert res.status_code == 200


# --------------------------------------------------------------------------- list and shape


def test_list_shape_is_exact_and_includes_quotas_and_usage(env):
    db = env["db"]
    db.update_user_admin_fields("1001", {"quota_albums": 3, "quota_window_days": 14})
    db.create_request(MusicRequest(id="r1", user_id="1001", item_type="album", title="A", artist="X",
                                   status=RequestStatus.PENDING))
    res = env["client"].get("/api/admin/users", headers=env["admin"])
    assert res.status_code == 200
    rows = {u["username"]: u for u in res.json()}
    assert set(rows) == {"root", "alice", "bob"}
    alice = rows["alice"]
    assert set(alice) == USER_KEYS
    assert alice["auth_type"] == "plex" and alice["is_admin"] is False and alice["disabled"] is False
    assert alice["mfa_enabled"] is False
    assert alice["quotas"]["overrides"] == {"tracks": None, "albums": 3, "discographies": None, "window_days": 14}
    assert alice["quotas"]["effective"] == {"tracks": 25, "albums": 3, "discographies": 1, "window_days": 14}
    assert alice["usage"] == {"tracks": 0, "albums": 1, "discographies": 0}
    assert rows["root"]["is_admin"] is True


def test_list_marks_mfa_and_local_users(env):
    db = env["db"]
    user = _mk_local(db)
    _enroll_mfa(db, user["id"])
    rows = {u["username"]: u for u in env["client"].get("/api/admin/users", headers=env["admin"]).json()}
    assert rows["carol"]["auth_type"] == "local" and rows["carol"]["mfa_enabled"] is True


def test_no_secrets_in_any_response(env):
    db = env["db"]
    user = _mk_local(db)
    secret, codes = _enroll_mfa(db, user["id"])
    cred = db.get_local_credentials(user["id"])
    forbidden = [cred["password_hash"], "scrypt$", secret, "password_hash", "totp_secret", "totp_last_counter",
                 "recovery", "token_hash", *codes]
    client, admin = env["client"], env["admin"]
    texts = [
        client.get("/api/admin/users", headers=admin).text,
        client.patch(f"/api/admin/users/{user['id']}", json={"quota_tracks": 4}, headers=admin).text,
        client.post(f"/api/admin/users/{user['id']}/disable", headers=admin).text,
        client.post(f"/api/admin/users/{user['id']}/enable", headers=admin).text,
        client.post(f"/api/admin/users/{user['id']}/reset-mfa", headers=admin).text,
        client.post(f"/api/admin/users/{user['id']}/revoke-sessions", headers=admin).text,
        client.get("/api/admin/settings/accounts", headers=admin).text,
    ]
    created = client.post("/api/admin/users", json={"username": "dave"}, headers=admin)
    texts.append(json.dumps(created.json()["user"]))
    for text in texts:
        for needle in forbidden:
            assert needle not in text, needle


# --------------------------------------------------------------------------- create


def test_create_returns_user_and_one_time_invite_url(env):
    db = env["db"]
    res = env["client"].post(
        "/api/admin/users",
        json={"username": "Newbie", "email": "n@x.tv", "permissions": 2 | 4,
              "quotas": {"tracks": 5, "window_days": 30}},
        headers=env["admin"],
    )
    assert res.status_code == 201
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert set(body) == {"user", "invite_url"}
    user = body["user"]
    assert set(user) == USER_KEYS
    assert user["username"] == "newbie" and user["auth_type"] == "local" and user["permissions"] == 6
    assert user["quotas"]["overrides"] == {"tracks": 5, "albums": None, "discographies": None, "window_days": 30}
    assert body["invite_url"].startswith(APP_URL + "/invite/")
    raw = body["invite_url"].rsplit("/", 1)[1]
    info = db.get_valid_token(raw)
    assert info["username"] == "newbie" and info["purpose"] == "invite"
    # the raw token is stored nowhere and appears in no later response
    dump = json.dumps([tuple(r) for r in db.conn.execute("SELECT * FROM user_invites")])
    assert raw not in dump
    assert raw not in env["client"].get("/api/admin/users", headers=env["admin"]).text


def test_create_accepts_flat_quota_fields_and_admin_grant_on_core(env):
    res = env["client"].post(
        "/api/admin/users",
        json={"username": "second-admin", "permissions": 1 | 2, "quota_albums": 0},
        headers=env["admin"],
    )
    assert res.status_code == 201
    user = res.json()["user"]
    assert user["is_admin"] is True and user["quotas"]["overrides"]["albums"] == 0


def test_create_without_application_url_is_503_and_creates_nothing(db, tmp_path):
    cfg = _config(tmp_path, application_url=None)
    client = _client(db, cfg)
    res = client.post("/api/admin/users", json={"username": "ghost"}, headers=_session(db, cfg, "admin-1"))
    assert res.status_code == 503
    assert db.get_user_by_username("ghost") is None
    assert db.conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0] == 0


def test_create_uses_application_url_from_general_settings(db, tmp_path):
    cfg = _config(tmp_path, application_url=None)
    client = _client(db, cfg)
    db.update_general_settings({"application_url": "https://settings.example.org/"})
    res = client.post("/api/admin/users", json={"username": "fromdb"}, headers=_session(db, cfg, "admin-1"))
    assert res.status_code == 201
    assert res.json()["invite_url"].startswith("https://settings.example.org/invite/")


@pytest.mark.parametrize(
    "body,code",
    [
        ({"username": "ab"}, 422),
        ({"username": "Bad Name!"}, 422),
        ({"username": "root"}, 409),  # clashes with a Plex username, case-insensitively
        ({"username": "ROOT"}, 409),
        ({"username": "okname", "permissions": 128}, 422),  # unknown bit
        ({"username": "okname", "permissions": -1}, 422),
        ({"username": "okname", "permissions": "6"}, 422),
        ({"username": "okname", "permissions": True}, 422),
        ({"username": "okname", "email": "no-at-sign"}, 422),
        ({"username": "okname", "quota_tracks": -1}, 422),
        ({"username": "okname", "quota_window_days": 0}, 422),
        ({"username": "okname", "quotas": {"bogus": 1}}, 422),
        ({}, 422),
    ],
)
def test_create_validation(env, body, code):
    res = env["client"].post("/api/admin/users", json=body, headers=env["admin"])
    assert res.status_code == code
    assert env["db"].get_user_by_username("okname") is None


# --------------------------------------------------------------------------- patch


def test_patch_permissions_email_and_quotas_and_reset_to_default(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    res = client.patch(
        "/api/admin/users/1001",
        json={"permissions": 2 | 4 | 8 | 64, "email": "new@x.tv", "quota_tracks": 7, "quotas": {"albums": 3}},
        headers=admin,
    )
    assert res.status_code == 200
    user = res.json()
    assert set(user) == USER_KEYS
    assert user["permissions"] == 78 and user["email"] == "new@x.tv"
    assert user["quotas"]["overrides"]["tracks"] == 7 and user["quotas"]["overrides"]["albums"] == 3
    assert db.get_user("1001")["permissions"] == 78
    reset = client.patch("/api/admin/users/1001", json={"quota_tracks": None, "quotas": {"albums": None}}, headers=admin)
    assert reset.json()["quotas"]["overrides"] == {"tracks": None, "albums": None, "discographies": None,
                                                    "window_days": None}
    assert reset.json()["quotas"]["effective"]["tracks"] == 25


def test_patch_can_grant_and_revoke_admin_for_another_user_on_core(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    granted = client.patch("/api/admin/users/1001", json={"permissions": 1 | 2}, headers=admin)
    assert granted.status_code == 200 and granted.json()["is_admin"] is True
    assert db.get_user("1001")["is_admin"] is True
    revoked = client.patch("/api/admin/users/1001", json={"permissions": 2}, headers=admin)
    assert revoked.status_code == 200 and revoked.json()["is_admin"] is False


@pytest.mark.parametrize("perms", [256, 511, 1 << 20, -2])
def test_patch_unknown_permission_bits_are_422(env, perms):
    res = env["client"].patch("/api/admin/users/1001", json={"permissions": perms}, headers=env["admin"])
    assert res.status_code == 422
    assert env["db"].get_user("1001")["permissions"] == 34


def test_patch_rejects_null_permissions_bad_email_empty_body_unknown_user(env):
    client, admin = env["client"], env["admin"]
    assert client.patch("/api/admin/users/1001", json={"permissions": None}, headers=admin).status_code == 422
    assert client.patch("/api/admin/users/1001", json={"email": "bad"}, headers=admin).status_code == 422
    assert client.patch("/api/admin/users/1001", json={}, headers=admin).status_code == 422
    assert client.patch("/api/admin/users/nope", json={"permissions": 2}, headers=admin).status_code == 404


def test_patch_self_cannot_change_own_admin_bit(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    res = client.patch("/api/admin/users/admin-1", json={"permissions": 2}, headers=admin)
    assert res.status_code == 400
    assert db.get_user("admin-1")["is_admin"] is True
    # other self-edits stay allowed, and keeping the admin bit is not a change
    assert client.patch("/api/admin/users/admin-1", json={"quota_tracks": 3}, headers=admin).status_code == 200
    assert client.patch("/api/admin/users/admin-1", json={"permissions": 3}, headers=admin).status_code == 200


def test_patch_last_admin_cannot_be_demoted(env, db):
    # An actor that is not itself a stored admin (e.g. a stale session) must not demote the only admin.
    env["client"].app.dependency_overrides[admin_users.admin_core_user] = lambda: {"id": "ghost", "is_admin": True}
    res = env["client"].patch("/api/admin/users/admin-1", json={"permissions": 2})
    assert res.status_code == 409
    assert db.get_user("admin-1")["is_admin"] is True


def test_patch_admin_can_demote_another_admin_when_one_remains(env, db):
    db.upsert_user("admin-2", "root2", None, is_admin=True)
    res = env["client"].patch("/api/admin/users/admin-2", json={"permissions": 2}, headers=env["admin"])
    assert res.status_code == 200 and db.get_user("admin-2")["is_admin"] is False


# --------------------------------------------------------------------------- disable / enable / revoke


def test_disable_revokes_sessions_and_blocks_access_then_enable(env):
    client, admin, db, cfg = env["client"], env["admin"], env["db"], env["cfg"]
    assert client.get("/api/account", headers=env["alice"]).status_code == 200
    res = client.post("/api/admin/users/1001/disable", headers=admin)
    assert res.status_code == 200 and res.json()["disabled"] is True
    assert db.get_auth_state("1001")["sessions_revoked_at_us"] > 0
    assert client.get("/api/account", headers=env["alice"]).status_code == 401
    assert client.get("/api/account", headers=_session(db, cfg, "1001")).status_code == 401
    enabled = client.post("/api/admin/users/1001/enable", headers=admin)
    assert enabled.status_code == 200 and enabled.json()["disabled"] is False
    assert db.get_user("1001")["disabled"] is False


def test_disable_self_is_400(env):
    res = env["client"].post("/api/admin/users/admin-1/disable", headers=env["admin"])
    assert res.status_code == 400
    assert env["db"].get_user("admin-1")["disabled"] is False


def test_disable_last_admin_is_409(env, db):
    env["client"].app.dependency_overrides[admin_users.admin_core_user] = lambda: {"id": "ghost", "is_admin": True}
    res = env["client"].post("/api/admin/users/admin-1/disable")
    assert res.status_code == 409
    assert db.get_user("admin-1")["disabled"] is False


def test_disabled_admin_does_not_count_as_a_remaining_admin(env, db):
    db.upsert_user("admin-2", "root2", None, is_admin=True)
    db.set_disabled("admin-2", True)
    env["client"].app.dependency_overrides[admin_users.admin_core_user] = lambda: {"id": "ghost", "is_admin": True}
    assert env["client"].post("/api/admin/users/admin-1/disable").status_code == 409


def test_unknown_user_is_404_on_every_target_route(env):
    client, admin = env["client"], env["admin"]
    for path in ("disable", "enable", "reset-password", "reset-mfa", "revoke-sessions"):
        assert client.post(f"/api/admin/users/nope/{path}", headers=admin).status_code == 404, path
    assert _delete(client, "/api/admin/users/nope", admin, {"confirm_username": "x"}).status_code == 404


def test_revoke_sessions_sets_marker_and_kills_existing_sessions(env):
    client, db = env["client"], env["db"]
    assert db.get_auth_state("1001")["sessions_revoked_at_us"] == 0
    res = client.post("/api/admin/users/1001/revoke-sessions", headers=env["admin"])
    assert res.status_code == 200
    assert db.get_auth_state("1001")["sessions_revoked_at_us"] > 0
    assert client.get("/api/account", headers=env["alice"]).status_code == 401


# --------------------------------------------------------------------------- reset password / MFA


def test_reset_password_returns_single_use_link_and_revokes_sessions(env):
    client, db, cfg = env["client"], env["db"], env["cfg"]
    user = _mk_local(db)
    session = _session(db, cfg, user["id"])
    assert client.get("/api/account", headers=session).status_code == 200
    res = client.post(f"/api/admin/users/{user['id']}/reset-password", headers=env["admin"])
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    assert set(res.json()) == {"reset_url"}
    url = res.json()["reset_url"]
    assert url.startswith(APP_URL + "/invite/")
    raw = url.rsplit("/", 1)[1]
    assert db.get_valid_token(raw)["purpose"] == "reset"
    assert db.get_auth_state(user["id"])["sessions_revoked_at_us"] > 0
    assert client.get("/api/account", headers=session).status_code == 401
    # a second reset voids the first link
    again = client.post(f"/api/admin/users/{user['id']}/reset-password", headers=env["admin"]).json()["reset_url"]
    assert db.get_valid_token(raw) is None
    assert db.get_valid_token(again.rsplit("/", 1)[1]) is not None
    # the old password still works until the link is used; redeeming sets a new one
    assert db.consume_token_set_password(again.rsplit("/", 1)[1], local_auth.hash_password("another long passphrase!"))


def test_reset_password_is_local_only(env):
    res = env["client"].post("/api/admin/users/1001/reset-password", headers=env["admin"])
    assert res.status_code == 400
    assert env["db"].conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0] == 0


def test_reset_password_503_without_application_url(db, tmp_path):
    cfg = _config(tmp_path, application_url=None)
    client = _client(db, cfg)
    user = _mk_local(db)
    session = _session(db, cfg, user["id"])
    res = client.post(f"/api/admin/users/{user['id']}/reset-password", headers=_session(db, cfg, "admin-1"))
    assert res.status_code == 503
    assert client.get("/api/account", headers=session).status_code == 200  # nothing was revoked or issued
    assert db.conn.execute("SELECT COUNT(*) FROM user_invites WHERE purpose = 'reset'").fetchone()[0] == 0


def test_reset_mfa_clears_secret_and_codes_and_revokes_sessions(env):
    client, db, cfg = env["client"], env["db"], env["cfg"]
    user = _mk_local(db)
    _enroll_mfa(db, user["id"])
    session = _session(db, cfg, user["id"])
    assert db.count_recovery_codes(user["id"]) == 10
    res = client.post(f"/api/admin/users/{user['id']}/reset-mfa", headers=env["admin"])
    assert res.status_code == 200 and res.json()["mfa_enabled"] is False
    assert db.get_auth_state(user["id"])["mfa_enabled"] is False
    assert db.count_recovery_codes(user["id"]) == 0
    assert db.get_local_credentials(user["id"])["totp_secret"] is None
    assert db.get_auth_state(user["id"])["sessions_revoked_at_us"] > 0
    assert client.get("/api/account", headers=session).status_code == 401


def test_reset_mfa_is_local_only(env):
    assert env["client"].post("/api/admin/users/1001/reset-mfa", headers=env["admin"]).status_code == 400


# --------------------------------------------------------------------------- delete / tombstone / restore


def test_delete_requires_exact_confirmation(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    assert client.request("DELETE", "/api/admin/users/1001", headers=admin).status_code == 422
    for wrong in ("", "Alice", "alice ", "bob", "1001"):
        res = _delete(client, "/api/admin/users/1001", admin, {"confirm_username": wrong})
        assert res.status_code == 400, wrong
    assert db.get_user("1001") is not None and not db.is_tombstoned("1001")


def test_delete_self_and_last_admin_are_refused(env, db):
    client, admin = env["client"], env["admin"]
    res = _delete(client, "/api/admin/users/admin-1", admin, {"confirm_username": "root"})
    assert res.status_code == 400
    client.app.dependency_overrides[admin_users.admin_core_user] = lambda: {"id": "ghost", "is_admin": True}
    res = _delete(client, "/api/admin/users/admin-1", {}, {"confirm_username": "root"})
    assert res.status_code == 409
    assert db.get_user("admin-1") is not None and not db.is_tombstoned("admin-1")


def test_delete_writes_tombstone_cascades_data_and_blocks_return(env):
    client, admin, db, cfg = env["client"], env["admin"], env["db"], env["cfg"]
    db.create_request(MusicRequest(id="req-a", user_id="1001", item_type="track", title="T", artist="A",
                                   status=RequestStatus.PENDING))
    db.upsert_playlist("pl-1", name="Alice list", service="spotify", creator_id="1001")
    db.upsert_playlist("pl-2", name="Shared list", service="spotify", creator_id="1001")
    db.set_playlist_targets("pl-2", ["1001", "1002"])
    alice_session = env["alice"]
    res = _delete(client, "/api/admin/users/1001", admin, {"confirm_username": "alice"})
    assert res.status_code == 200 and res.json() == {"status": "deleted", "id": "1001"}
    assert db.get_user("1001") is None
    assert db.is_tombstoned("1001")
    assert db.conn.execute("SELECT deleted_by FROM user_tombstones WHERE user_id = '1001'").fetchone()[0] == "admin-1"
    assert db.get_request("req-a") is None
    assert db.conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = '1001'").fetchone()[0] == 0
    assert db.conn.execute("SELECT 1 FROM playlists WHERE id = 'pl-1'").fetchone() is None
    shared = db.conn.execute("SELECT creator_id FROM playlists WHERE id = 'pl-2'").fetchone()
    assert shared is not None and shared[0] is None  # another user is still targeted: orphaned, not deleted
    assert client.get("/api/account", headers=alice_session).status_code == 401
    # ensure_user (the forwarded path) refuses to recreate the id
    with pytest.raises(PermissionError):
        db.ensure_user("1001", "alice")
    headers = internal_auth.sign_assertion(SECRET, "GET", "/api/account", "1001", "alice", b"")
    assert client.get("/api/account", headers=headers).status_code == 401
    assert db.get_user("1001") is None


def test_deleted_local_user_cannot_sign_in(env):
    client, db = env["client"], env["db"]
    user = _mk_local(db)
    ok = client.post("/api/auth/local/login", json={"username": "carol", "password": PW})
    assert ok.status_code == 200
    res = _delete(client, f"/api/admin/users/{user['id']}", env["admin"], {"confirm_username": "carol"})
    assert res.status_code == 200
    again = client.post("/api/auth/local/login", json={"username": "carol", "password": PW})
    assert again.status_code == 401
    assert db.is_tombstoned(user["id"])
    with pytest.raises(PermissionError):
        db.ensure_user(user["id"], "carol")
    assert db.conn.execute("SELECT COUNT(*) FROM user_invites WHERE user_id = ?", (user["id"],)).fetchone()[0] == 0


def test_refresh_does_not_resurrect_deleted_plex_users(env, db):
    client, admin = env["client"], env["admin"]
    assert _delete(client, "/api/admin/users/1002", admin, {"confirm_username": "bob"}).status_code == 200
    # The refresh route skips tombstoned ids instead of re-creating them.
    fake_plex = type("P", (), {"get_home_users": lambda self: [{"id": 1002, "username": "bob"},
                                                                {"id": 1001, "username": "alice"}]})()
    from trackseerr.api.dependencies import get_plex_client

    client.app.dependency_overrides[get_plex_client] = lambda: fake_plex
    res = client.post("/api/users/refresh", headers=admin)
    assert res.status_code == 200
    assert db.get_user("1002") is None
    assert db.get_user("1001") is not None


def test_restore_plex_user_lifts_tombstone(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    _delete(client, "/api/admin/users/1001", admin, {"confirm_username": "alice"})
    assert db.is_tombstoned("1001")
    res = client.post("/api/admin/users/1001/restore", headers=admin)
    assert res.status_code == 200 and res.json() == {"status": "restored", "id": "1001"}
    assert not db.is_tombstoned("1001")
    user = db.ensure_user("1001", "alice")  # the forwarded path recreates a fresh default user
    assert user["permissions"] == 34 and user["is_admin"] is False
    assert client.post("/api/admin/users/1001/restore", headers=admin).status_code == 404  # no longer removed


def test_restore_refuses_local_users_and_unknown_ids(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    user = _mk_local(db)
    _delete(client, f"/api/admin/users/{user['id']}", admin, {"confirm_username": "carol"})
    res = client.post(f"/api/admin/users/{user['id']}/restore", headers=admin)
    assert res.status_code == 400
    assert db.is_tombstoned(user["id"])
    assert client.post("/api/admin/users/9999/restore", headers=admin).status_code == 404


# --------------------------------------------------------------------------- settings and labels


def test_account_settings_get_put_roundtrip(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    assert client.get("/api/admin/settings/accounts", headers=admin).json() == {
        "require_mfa_local": False, "default_quota_tracks": 25, "default_quota_albums": 10,
        "default_quota_discographies": 1, "default_quota_window_days": 7,
    }
    res = client.put(
        "/api/admin/settings/accounts",
        json={"require_mfa_local": True, "default_quota_tracks": 40, "default_quota_window_days": 30},
        headers=admin,
    )
    assert res.status_code == 200
    assert res.json() == {
        "require_mfa_local": True, "default_quota_tracks": 40, "default_quota_albums": 10,
        "default_quota_discographies": 1, "default_quota_window_days": 30,
    }
    assert db.get_account_settings()["default_quota_tracks"] == 40
    # the new default applies to users without an override
    users = {u["username"]: u for u in client.get("/api/admin/users", headers=admin).json()}
    assert users["alice"]["quotas"]["effective"]["tracks"] == 40
    assert users["alice"]["quotas"]["effective"]["window_days"] == 30


@pytest.mark.parametrize(
    "body",
    [
        {"default_quota_tracks": -1},
        {"default_quota_albums": 10**9},
        {"default_quota_window_days": 0},
        {"default_quota_window_days": 4000},
        {"require_mfa_local": "yes"},
        {"default_quota_tracks": "5"},
        {"default_quota_tracks": True},
        {},
    ],
)
def test_account_settings_validation(env, body):
    res = env["client"].put("/api/admin/settings/accounts", json=body, headers=env["admin"])
    assert res.status_code == 422
    assert env["db"].get_account_settings()["default_quota_tracks"] == 25


def test_permission_labels(env):
    res = env["client"].get("/api/admin/permissions", headers=env["admin"])
    assert res.status_code == 200
    by_bit = {row["bit"]: row for row in res.json()}
    assert set(by_bit) == {1, 2, 4, 8, 16, 32, 64, 128}
    assert by_bit[int(UserPermission.AUTO_REQUEST_PLAYLISTS)]["label"] == "Auto-request playlist tracks"
    assert by_bit[int(UserPermission.AUTO_APPROVE_DISCOGRAPHY)]["label"]


# --------------------------------------------------------------------------- legacy routes still work, admin-only


def test_legacy_put_is_admin_only_and_delegates_to_guards(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    assert client.put("/api/users/1001", json={"permissions": 2}, headers=env["alice"]).status_code == 403
    assert client.put("/api/users/admin-1", json={"is_admin": False}, headers=admin).status_code == 400
    assert client.put("/api/users/admin-1", json={"permissions": 2}, headers=admin).status_code == 400
    assert client.put("/api/users/1001", json={"permissions": 1 << 9}, headers=admin).status_code == 422
    assert db.get_user("admin-1")["is_admin"] is True
    ok = client.put("/api/users/1001", json={"is_admin": True}, headers=admin)
    assert ok.status_code == 200 and ok.json()["is_admin"] is True
    assert db.get_user("1001")["permissions"] & 1 == 1  # bit kept in step with the flag


def test_legacy_put_quota_maps_to_per_type_overrides(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    res = client.put("/api/users/1001", json={"request_limit_quota": 6, "request_limit_days": 21}, headers=admin)
    assert res.status_code == 200
    assert res.json()["request_limit_quota"] == 6 and res.json()["request_limit_days"] == 21
    assert db.get_user_quota_overrides("1001") == {
        "quota_tracks": 6, "quota_albums": 6, "quota_discographies": None, "quota_window_days": 21,
    }
    cleared = client.put("/api/users/1001", json={"request_limit_quota": None}, headers=admin)
    assert cleared.status_code == 200 and cleared.json()["request_limit_quota"] is None
    assert db.get_user_quota_overrides("1001")["quota_tracks"] is None


def test_legacy_put_unknown_user_404_and_api_key_cannot_change_permissions(env):
    client, admin, db = env["client"], env["admin"], env["db"]
    assert client.put("/api/users/nope", json={"permissions": 2}, headers=admin).status_code == 404
    key = {"X-Api-Key": db.get_api_key()}
    assert client.put("/api/users/1001", json={"permissions": 2 | 4}, headers=key).status_code == 403
    assert db.get_user("1001")["permissions"] == 34


def test_legacy_list_and_refresh_remain_admin_only(env):
    client = env["client"]
    assert client.get("/api/users", headers=env["alice"]).status_code == 403
    assert client.post("/api/users/refresh", headers=env["alice"]).status_code == 403
    assert client.get("/api/users", headers=env["admin"]).status_code == 200


def test_refresh_does_not_demote_admins_granted_in_the_ui(env, db):
    from trackseerr.api.dependencies import get_plex_client

    db.update_user_admin_fields("1001", {"permissions": 3})
    fake_plex = type("P", (), {"get_home_users": lambda self: [{"id": 1001, "username": "alice", "is_admin": False}]})()
    env["client"].app.dependency_overrides[get_plex_client] = lambda: fake_plex
    assert env["client"].post("/api/users/refresh", headers=env["admin"]).status_code == 200
    assert db.get_user("1001")["is_admin"] is True
