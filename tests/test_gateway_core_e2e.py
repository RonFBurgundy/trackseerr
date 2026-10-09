"""End-to-end gateway -> core: the real signed CoreClient call path against a real core app.

The autouse ``CoreClient.session_status`` mock in conftest.py is disabled for this module via the
``real_core_client`` marker. The gateway's outbound ``httpx.Client`` is rerouted through an
``httpx.MockTransport`` whose handler replays each request (real signing headers and body untouched)
into the core app's ``TestClient``, so signing, body hashing, nonce and verification all run for real.
"""

from pathlib import Path
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth, local_auth
from trackseerr.api import dependencies
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.clients import core_client
from trackseerr.config import Config
from trackseerr.storage import Database

pytestmark = pytest.mark.real_core_client

SECRET = "e2e-shared-secret-" + "x" * 30
PW = "correct horse battery staple"
CORE_URL = "http://core.internal:5251"


def _config(tmp_path: Path, role: str) -> Config:
    d = tmp_path / role
    d.mkdir()
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(d),
        role=role,
        internal_core_secret=SECRET,
        trackseerr_core_url=CORE_URL,
        user_request_quota=10,
    )


def _app_client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.core_db = Database(":memory:")
        self.core_db.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
        user, raw = self.core_db.create_local_user("bob", "bob@x.tv", None, "admin-1")
        assert self.core_db.consume_token_set_password(raw, local_auth.hash_password(PW)) == user["id"]
        self.bob_id = user["id"]
        self.gw_db = Database(":memory:")
        self.core = _app_client(self.core_db, _config(tmp_path, "core"))
        self.gateway = _app_client(self.gw_db, _config(tmp_path, "gateway"))
        self.core_calls: list[tuple[str, str]] = []
        self.real_client = httpx.Client

    def login(self, password: str = PW):
        return self.gateway.post("/api/auth/local/login", json={"username": "bob", "password": password})


@pytest.fixture
def env(tmp_path, monkeypatch):
    internal_auth._nonce_cache.clear()
    e = Env(tmp_path)
    real_client = httpx.Client
    e.real_client = real_client

    def handler(request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode()
        e.core_calls.append((request.method, target))
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        res = e.core.request(request.method, target, headers=headers, content=request.content)
        return httpx.Response(res.status_code, headers={"content-type": res.headers.get("content-type", "")}, content=res.content)

    def routed_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(core_client.httpx, "Client", routed_client)
    yield e
    e.core_db.close()
    e.gw_db.close()
    internal_auth._nonce_cache.clear()


def _advance_time(monkeypatch, seconds: float) -> None:
    base = dependencies._monotonic()
    monkeypatch.setattr(dependencies, "_monotonic", lambda: base + seconds)


def test_valid_gateway_session_hits_real_session_status(env):
    assert env.login().status_code == 200
    res = env.gateway.get("/api/users/me")
    assert res.status_code == 200
    assert res.json()["username"] == "bob"
    assert ("POST", "/api/internal/auth/session-status") in env.core_calls


def test_disabled_on_core_invalidates_gateway_session_after_cache_expiry(env, monkeypatch):
    assert env.login().status_code == 200
    assert env.gateway.get("/api/users/me").status_code == 200
    assert env.core_db.set_disabled(env.bob_id, True)
    # still inside the 60 s verdict cache: unchanged
    assert env.gateway.get("/api/users/me").status_code == 200
    _advance_time(monkeypatch, dependencies.SESSION_STATUS_TTL_SECONDS + 1)
    assert env.gateway.get("/api/users/me").status_code == 401
    token = env.gateway.cookies.get("session_token")
    assert token and env.gw_db.get_session(token) is None


def test_local_login_through_gateway_and_generic_failure(env):
    ok = env.login()
    assert ok.status_code == 200
    assert ("POST", "/api/internal/auth/local/verify") in env.core_calls
    assert "session_token" in env.gateway.cookies
    env.gateway.cookies.clear()
    bad = env.login("wrong password entirely")
    unknown = env.gateway.post("/api/auth/local/login", json={"username": "nobody", "password": "wrong password entirely"})
    assert bad.status_code == 401 and unknown.status_code == 401
    assert bad.json() == unknown.json()
    assert "session_token" not in env.gateway.cookies


def test_forwarded_account_route_end_to_end(env):
    assert env.login().status_code == 200
    res = env.gateway.get("/api/account")
    assert res.status_code == 200, res.text
    assert ("GET", "/api/account") in env.core_calls


def test_core_unreachable_gives_503(env, monkeypatch):
    assert env.login().status_code == 200
    real_client = env.real_client

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        core_client.httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(down)})
    )
    dependencies.clear_session_status_cache()
    res = env.gateway.get("/api/users/me")
    assert res.status_code == 503
