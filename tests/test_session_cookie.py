"""Session cookie persistence: Max-Age on login, restore from Bearer on /me, logout path."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trackseerr import local_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

PW = "correct horse battery staple"


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
        internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
        application_url="https://music.example.com",
    )


@pytest.fixture
def client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


@pytest.fixture
def token(db: Database, config: Config) -> str:
    user = db.get_user("1001")
    key = get_or_create_secret_key(data_dir=config.data_dir)
    tok = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=key)
    db.create_session(tok, user["id"], {"auth": "test"})
    return tok


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def _session_cookies(res: Any) -> list[str]:
    return [h for h in res.headers.get_list("set-cookie") if h.startswith("session_token=")]


def test_login_cookie_has_max_age(client: TestClient, db: Database) -> None:
    user, raw = db.create_local_user("bob", "bob@x.tv", None, "admin-1")
    assert db.consume_token_set_password(raw, local_auth.hash_password(PW)) == user["id"]
    res = client.post("/api/auth/local/login", json={"username": "bob", "password": PW})
    assert res.status_code == 200
    cookies = _session_cookies(res)
    assert len(cookies) == 1
    assert "Max-Age=604800" in cookies[0]
    assert "HttpOnly" in cookies[0]


def test_me_restores_missing_cookie_from_bearer(client: TestClient, token: str) -> None:
    res = client.get("/api/auth/me", headers=_bearer(token))
    assert res.status_code == 200
    cookies = _session_cookies(res)
    assert len(cookies) == 1
    assert cookies[0].startswith(f"session_token={token}")
    assert "Max-Age=604800" in cookies[0]


def test_me_does_not_reset_matching_cookie(client: TestClient, token: str) -> None:
    client.cookies.set("session_token", token)
    res = client.get("/api/auth/me", headers=_bearer(token))
    assert res.status_code == 200
    assert _session_cookies(res) == []


def test_me_cookie_only_request_no_set_cookie(client: TestClient, token: str) -> None:
    client.cookies.set("session_token", token)
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert _session_cookies(res) == []


def test_me_invalid_bearer_401_no_cookie(client: TestClient) -> None:
    res = client.get("/api/auth/me", headers=_bearer("bogus"))
    assert res.status_code == 401
    assert _session_cookies(res) == []


def test_logout_clears_cookie_path_root(client: TestClient, token: str) -> None:
    res = client.post("/api/auth/logout", headers=_bearer(token))
    assert res.status_code == 200
    cookies = _session_cookies(res)
    assert len(cookies) == 1
    assert "Path=/" in cookies[0]
    assert "Max-Age=0" in cookies[0]
