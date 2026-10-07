"""Regression: the first authenticated calls right after Plex sign-in must succeed with either credential.

The SPA stores the verify token and immediately fires its data requests. Those requests must work with the
Bearer token alone (localStorage) and with the session cookie alone (cookie-only clients), and an anonymous
request made before login must be a clean 401 (the SPA has to treat that as stale once login completes).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database


@pytest.fixture
def stack(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    yield TestClient(app)
    db.close()


def _verify(client):
    with (
        patch("plex_playlist_sync.api.routes.auth.check_plex_pin", return_value="plex-tok"),
        patch("plex_playlist_sync.api.routes.auth.verify_server_access", return_value=(True, True)),
        patch(
            "plex_playlist_sync.api.routes.auth.get_plex_user",
            return_value={"id": "u1", "username": "owner", "email": "o@x.tv"},
        ),
    ):
        return client.post("/api/auth/plex/verify", json={"pin_id": 1, "target_machine_id": "m"})


def test_anonymous_call_before_login_is_401(stack):
    assert stack.get("/api/auth/me").status_code == 401


def test_first_call_after_verify_works_with_bearer_only(stack):
    resp = _verify(stack)
    assert resp.status_code == 200
    token = resp.json()["token"]
    stack.cookies.clear()
    me = stack.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["id"] == "u1"


def test_first_call_after_verify_works_with_cookie_only(stack):
    assert _verify(stack).status_code == 200
    assert "session_token" in stack.cookies
    assert stack.get("/api/auth/me").status_code == 200


def test_repeated_verify_polls_keep_earlier_token_valid(stack):
    """The SPA polls verify on a timer; an overlapping second poll must not revoke the first token."""
    first = _verify(stack).json()["token"]
    second = _verify(stack).json()["token"]
    stack.cookies.clear()
    for tok in (first, second):
        assert stack.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
