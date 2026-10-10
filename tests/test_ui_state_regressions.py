"""Regressions: quality-profile upgrade flag persistence, Plex last-login stamping, request status tabs."""

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth
from trackseerr.api import dependencies
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.core_client import CoreClient
from trackseerr.config import Config
from trackseerr.models import MusicRequest, RequestStatus
from trackseerr.storage import Database

SECRET = "s" * 40


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


def _cfg(tmp_path, role: str = "") -> Config:
    kwargs: dict[str, Any] = {}
    if role:
        kwargs = {
            "application_url": "https://music.example.com",
            "role": role,
            "internal_core_secret": SECRET,
            "trackseerr_core_url": "http://core.internal:5251",
        }
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), **kwargs)


def _client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def _admin_headers(db: Database, cfg: Config) -> dict[str, str]:
    user = db.get_user("admin-1")
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=True,
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(session_id=token, user_id=user["id"])
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------- 1. upgrade_allowed


def test_upgrade_allowed_round_trips_through_api(db, tmp_path):
    cfg = _cfg(tmp_path)
    client = _client(db, cfg)
    headers = _admin_headers(db, cfg)
    created = client.post(
        "/api/settings/quality-profiles",
        json={"name": "Lossless", "cutoff": "FLAC", "upgrade_allowed": False},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert created.json()["upgrade_allowed"] is False
    pid = created.json()["id"]
    listed = {p["id"]: p for p in client.get("/api/settings/quality-profiles", headers=headers).json()}
    assert listed[pid]["upgrade_allowed"] is False

    body = {**listed[pid], "upgrade_allowed": True}
    assert client.post("/api/settings/quality-profiles", json=body, headers=headers).json()["upgrade_allowed"] is True
    assert db.get_quality_profile(pid)["upgrade_allowed"] is True



def test_cutoff_unmet_requests_skip_profiles_that_forbid_upgrades(db):
    default = db.get_default_quality_profile()
    locked = db.upsert_quality_profile(
        {"id": "locked", "name": "Locked", "cutoff": "FLAC", "items": [], "upgrade_allowed": False}
    )
    for rid, profile_id in (("r-default", None), ("r-locked", locked["id"])):
        db.create_request(
            MusicRequest(id=rid, user_id="1001", item_type="album", title=rid, artist="A", status=RequestStatus.AVAILABLE)
        )
        db.conn.execute(
            "UPDATE music_requests SET quality_profile_id = ?, cutoff_met = 0 WHERE id = ?", (profile_id, rid)
        )
    db.conn.commit()
    assert default["upgrade_allowed"] is True
    assert [r["id"] for r in db.get_cutoff_unmet_requests()] == ["r-default"]
    # A NULL profile follows the default profile's flag.
    db.upsert_quality_profile({**default, "upgrade_allowed": False})
    assert db.get_cutoff_unmet_requests() == []


# ----------------------------------------------------------------------- 2. Plex last login


def _last_login(db: Database, user_id: str):
    row = db.conn.execute("SELECT last_login_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0]



def _plex_login(client: TestClient, user_id: str = "5005"):
    with patch("trackseerr.api.routes.auth.check_plex_pin", return_value="tok"), patch(
        "trackseerr.api.routes.auth.verify_server_access", return_value=(True, False)
    ), patch(
        "trackseerr.api.routes.auth.get_plex_user",
        return_value={"id": user_id, "username": "plexuser", "email": "p@x.tv"},
    ):
        return client.post("/api/auth/plex/verify", json={"pin_id": 1, "target_machine_id": "m1"})


def test_plex_sign_in_records_last_login(db, tmp_path, monkeypatch):
    monkeypatch.setenv("PLEX_MACHINE_IDENTIFIER", "m1")
    client = _client(db, _cfg(tmp_path))
    assert _plex_login(client).status_code == 200
    first = _last_login(db, "5005")
    assert first
    # A later authenticated request must not touch it; only a new sign-in does.
    client.get("/api/auth/me")
    assert _last_login(db, "5005") == first


def test_refused_plex_sign_in_does_not_record_login(db, tmp_path, monkeypatch):
    monkeypatch.setenv("PLEX_MACHINE_IDENTIFIER", "m1")
    db.upsert_user("5005", "plexuser", "p@x.tv", is_admin=False)
    db.set_disabled("5005", True)
    client = _client(db, _cfg(tmp_path))
    assert _plex_login(client).status_code == 403
    assert not _last_login(db, "5005")


def test_plex_sign_in_never_trusts_client_machine_id(db, tmp_path, monkeypatch):
    monkeypatch.delenv("PLEX_MACHINE_IDENTIFIER", raising=False)
    client = _client(db, _cfg(tmp_path))
    client.app.dependency_overrides[get_plex_client] = lambda: None
    with patch("trackseerr.api.routes.auth.check_plex_pin", return_value="tok"), patch(
        "trackseerr.api.routes.auth.verify_server_access", return_value=(True, False)
    ) as access, patch(
        "trackseerr.api.routes.auth.get_plex_user",
        return_value={"id": "5005", "username": "plexuser", "email": "p@x.tv"},
    ):
        resp = client.post("/api/auth/plex/verify", json={"pin_id": 1, "target_machine_id": "m1"})
    assert resp.status_code != 200
    access.assert_not_called()


def test_gateway_plex_sign_in_asks_core_to_record_login(db, tmp_path, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake(self, user_id, issued, **kwargs):
        calls.append({"user_id": user_id, **kwargs})
        return 200, {"valid": True}

    monkeypatch.setattr(CoreClient, "session_status", fake)
    dependencies.clear_session_status_cache()
    assert _plex_login(_client(db, _cfg(tmp_path, "gateway"))).status_code == 200
    assert calls[-1] == {"user_id": "5005", "record_login": True, "username": "plexuser"}


def _plex_login_no_target(client: TestClient, recorder: list[tuple[str, str]]):
    def fake_access(token, machine_id):
        recorder.append((token, machine_id))
        return True, False

    with patch("trackseerr.api.routes.auth.check_plex_pin", return_value="tok"), patch(
        "trackseerr.api.routes.auth.verify_server_access", side_effect=fake_access
    ), patch(
        "trackseerr.api.routes.auth.get_plex_user",
        return_value={"id": "5005", "username": "plexuser", "email": "p@x.tv"},
    ):
        return client.post("/api/auth/plex/verify", json={"pin_id": 1})


def test_gateway_plex_sign_in_uses_core_machine_id(db, tmp_path, monkeypatch):
    monkeypatch.delenv("PLEX_MACHINE_IDENTIFIER", raising=False)
    monkeypatch.setattr(CoreClient, "plex_identity", lambda self: (200, {"machine_identifier": "core-mid"}))
    seen: list[tuple[str, str]] = []
    res = _plex_login_no_target(_client(db, _cfg(tmp_path, "gateway")), seen)
    assert res.status_code == 200, res.text
    assert seen == [("tok", "core-mid")]


def test_gateway_plex_sign_in_core_has_no_plex(db, tmp_path, monkeypatch):
    monkeypatch.delenv("PLEX_MACHINE_IDENTIFIER", raising=False)
    monkeypatch.setattr(CoreClient, "plex_identity", lambda self: (200, {"machine_identifier": None}))
    seen: list[tuple[str, str]] = []
    res = _plex_login_no_target(_client(db, _cfg(tmp_path, "gateway")), seen)
    assert res.status_code == 409  # MediaServerUnavailable
    assert seen == []


def test_gateway_plex_sign_in_core_unreachable(db, tmp_path, monkeypatch):
    monkeypatch.delenv("PLEX_MACHINE_IDENTIFIER", raising=False)

    def boom(self):
        raise httpx.ConnectError("x")

    monkeypatch.setattr(CoreClient, "plex_identity", boom)
    res = _plex_login_no_target(_client(db, _cfg(tmp_path, "gateway")), [])
    assert res.status_code == 503


def test_gateway_machine_id_is_cached(db, tmp_path, monkeypatch):
    monkeypatch.delenv("PLEX_MACHINE_IDENTIFIER", raising=False)
    calls: list[int] = []

    def fake(self):
        calls.append(1)
        return 200, {"machine_identifier": "core-mid"}

    monkeypatch.setattr(CoreClient, "plex_identity", fake)
    client = _client(db, _cfg(tmp_path, "gateway"))
    assert _plex_login_no_target(client, []).status_code == 200
    assert _plex_login_no_target(client, []).status_code == 200
    assert len(calls) == 1


def test_core_plex_identity_endpoint(db, tmp_path, monkeypatch):
    monkeypatch.setenv("PLEX_MACHINE_IDENTIFIER", "env-mid")
    client = _client(db, _cfg(tmp_path, "core"))
    url = "/api/internal/plex/identity"
    internal_auth._nonce_cache.clear()
    headers = internal_auth.sign_assertion(SECRET, "GET", url, "", "", b"")
    res = client.get(url, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"machine_identifier": "env-mid"}
    assert client.get(url).status_code == 404


def test_core_session_status_records_login_only_when_asked_and_valid(db, tmp_path):
    client = _client(db, _cfg(tmp_path, "core"))
    url = "/api/internal/auth/session-status"

    def post(payload: dict[str, Any]):
        body = json.dumps(payload).encode()
        headers = internal_auth.sign_assertion(SECRET, "POST", url, "", "", body)
        headers["Content-Type"] = "application/json"
        return client.post(url, content=body, headers=headers)

    internal_auth._nonce_cache.clear()
    assert post({"user_id": "1001", "session_issued_at": 1}).json() == {"valid": True}
    assert not _last_login(db, "1001")  # a plain status poll is not a sign-in
    assert post({"user_id": "7007", "session_issued_at": 1, "record_login": True, "username": "newbie"}).json() == {
        "valid": True
    }
    assert _last_login(db, "7007")  # unknown-on-core user is created and stamped
    db.set_disabled("1001", True)
    assert post({"user_id": "1001", "session_issued_at": 1, "record_login": True}).json()["valid"] is False
    assert not _last_login(db, "1001")


# ----------------------------------------------------------------------- 3. request status tabs


@pytest.mark.parametrize(
    "tab,expected",
    [
        ("pending", {"r-pending"}),
        ("approved", {"r-processing"}),
        ("fulfilled", {"r-available"}),
        ("rejected", {"r-rejected"}),
        ("processing", {"r-processing"}),
        ("available", {"r-available"}),
    ],
)
def test_requests_status_tabs_match_stored_statuses(db, tmp_path, tab, expected):
    for status in (RequestStatus.PENDING, RequestStatus.PROCESSING, RequestStatus.AVAILABLE, RequestStatus.REJECTED):
        db.create_request(
            MusicRequest(id=f"r-{status.value}", user_id="1001", item_type="album", title="T", artist="A", status=status)
        )
    cfg = _cfg(tmp_path)
    client = _client(db, cfg)
    headers = _admin_headers(db, cfg)
    res = client.get(f"/api/requests?status={tab}", headers=headers)
    assert {r["id"] for r in res.json()["requests"]} == expected
    assert len(client.get("/api/requests", headers=headers).json()["requests"]) == 4
