"""Admins sign in on Core, not on the Requests gateway: the gateway refuses them at sign-in with a pointer to Core."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import DETAIL_ADMIN_USE_CORE, get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.core_client import CoreClient
from trackseerr.config import Config
from trackseerr.storage import Database

SECRET = "s" * 40
DETAIL = "Admin accounts sign in on TrackSeerr Core, not the Requests site"
AUTH = "trackseerr.api.routes.auth"


@pytest.fixture(autouse=True)
def _fresh():
    internal_auth._nonce_cache.clear()
    yield
    internal_auth._nonce_cache.clear()


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


def _config(tmp_path: Path, role: str) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET,
        trackseerr_core_url="http://core.internal:5251",
    )


def _client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def _status(client: TestClient, user_id: str) -> Any:
    body = json.dumps({"user_id": user_id, "session_issued_at": 10**18}).encode()
    url = "/api/internal/auth/session-status"
    headers = internal_auth.sign_assertion(SECRET, "POST", url, "", "", body)
    headers["Content-Type"] = "application/json"
    return client.post(url, content=body, headers=headers)


def _plex_verify(client: TestClient, *, is_owner: bool, user_id: str = "1001", verdict: Any = None) -> Any:
    verdict = verdict or {"valid": True}
    with (
        patch(f"{AUTH}.check_plex_pin", return_value="plex-token"),
        patch(f"{AUTH}._resolve_machine_id", return_value="machine"),
        patch(f"{AUTH}.verify_server_access", return_value=(True, is_owner)),
        patch(f"{AUTH}.get_plex_user", return_value={"id": user_id, "username": "alice", "email": None}),
        patch(f"{AUTH}.core_session_status", return_value=verdict),
    ):
        return client.post("/api/auth/plex/verify", json={"pin_id": 1})


def _local_reply(is_admin: bool) -> tuple[int, dict[str, Any]]:
    return 200, {
        "user": {"id": "local-" + "a" * 24, "username": "bob", "is_admin": is_admin},
        "mfa_required": False,
        "mfa_enrollment_required": False,
        "session_floor_us": 0,
    }


def _local_login(client: TestClient) -> Any:
    return client.post("/api/auth/local/login", json={"username": "bob", "password": "correct horse battery staple"})


# --------------------------------------------------------------------------- core: session-status / verify


def test_session_status_reports_admin(db, tmp_path):
    client = _client(db, _config(tmp_path, "core"))
    assert _status(client, "admin-1").json() == {"valid": False, "reason": "admin"}
    assert _status(client, "1001").json() == {"valid": True}


def test_session_status_admin_permission_bit(db, tmp_path):
    from trackseerr.models import UserPermission

    db.conn.execute("UPDATE users SET permissions = ? WHERE id = '1001'", (int(UserPermission.ADMIN),))
    db.conn.commit()
    client = _client(db, _config(tmp_path, "core"))
    assert _status(client, "1001").json() == {"valid": False, "reason": "admin"}


def test_session_status_disabled_wins_over_admin(db, tmp_path):
    client = _client(db, _config(tmp_path, "core"))
    db.set_disabled("admin-1", True)
    assert _status(client, "admin-1").json() == {"valid": False, "reason": "disabled"}


# --------------------------------------------------------------------------- gateway: Plex sign-in


def test_gateway_plex_owner_refused_without_session(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    res = _plex_verify(client, is_owner=True)
    assert res.status_code == 403 and res.json()["detail"] == DETAIL
    assert DETAIL == DETAIL_ADMIN_USE_CORE
    assert "set-cookie" not in res.headers


def test_gateway_plex_core_admin_refused_without_session(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    res = _plex_verify(client, is_owner=False, user_id="777", verdict={"valid": False, "reason": "admin"})
    assert res.status_code == 403 and res.json()["detail"] == DETAIL
    assert "set-cookie" not in res.headers
    assert db.get_user("777") is None


def test_gateway_plex_non_admin_unchanged(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    res = _plex_verify(client, is_owner=False)
    assert res.status_code == 200
    assert "session_token=" in res.headers["set-cookie"]
    assert res.json()["user"]["is_admin"] is False


def test_core_plex_owner_still_signs_in(db, tmp_path):
    client = _client(db, _config(tmp_path, "core"))
    res = _plex_verify(client, is_owner=True)
    assert res.status_code == 200 and res.json()["user"]["is_admin"] is True


# --------------------------------------------------------------------------- gateway: local sign-in


def test_gateway_local_admin_refused_without_session_or_mirror(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    with patch.object(CoreClient, "local_verify", return_value=_local_reply(True)):
        res = _local_login(client)
    assert res.status_code == 403 and res.json()["detail"] == DETAIL
    assert "set-cookie" not in res.headers
    assert db.get_user("local-" + "a" * 24) is None


def test_gateway_local_non_admin_unchanged(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    with patch.object(CoreClient, "local_verify", return_value=_local_reply(False)):
        res = _local_login(client)
    assert res.status_code == 200 and "session_token=" in res.headers["set-cookie"]


def test_core_local_verify_returns_is_admin(db, tmp_path):
    from trackseerr import local_auth

    user, raw = db.create_local_user("carol", "c@x.tv", None, "admin-1")
    db.consume_token_set_password(raw, local_auth.hash_password("correct horse battery staple"))
    db.conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
    db.conn.commit()
    client = _client(db, _config(tmp_path, "core"))
    body = json.dumps({"username": "carol", "password": "correct horse battery staple", "client_ip": "192.0.2.1"}).encode()
    url = "/api/internal/auth/local/verify"
    headers = internal_auth.sign_assertion(SECRET, "POST", url, "", "", body)
    headers["Content-Type"] = "application/json"
    res = client.post(url, content=body, headers=headers)
    assert res.status_code == 200 and res.json()["user"]["is_admin"] is True


def test_core_local_admin_login_unchanged(db, tmp_path):
    from trackseerr import local_auth

    user, raw = db.create_local_user("dave", "d@x.tv", None, "admin-1")
    db.consume_token_set_password(raw, local_auth.hash_password("correct horse battery staple"))
    db.conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
    db.conn.commit()
    client = _client(db, _config(tmp_path, "core"))
    res = client.post("/api/auth/local/login", json={"username": "dave", "password": "correct horse battery staple"})
    assert res.status_code == 200


# --------------------------------------------------------------------------- existing admin gateway session


def test_existing_admin_gateway_session_gets_401_with_detail(db, tmp_path):
    cfg = _config(tmp_path, "core")
    client = _client(db, cfg)
    h = internal_auth.sign_assertion(SECRET, "GET", "/api/users/me", "admin-1", "root", b"")
    res = client.get("/api/users/me", headers=h)
    assert res.status_code == 401 and res.json()["detail"] == DETAIL
