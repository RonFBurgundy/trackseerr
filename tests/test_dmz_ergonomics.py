"""DMZ ergonomics: startup guardrails, handshake/heartbeat, gateway status, init-dmz, role flips."""

from trackseerr.storage import SCHEMA_VERSION
import io
import json
import logging
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import trackseerr
from trackseerr import gateway_link, init_dmz, internal_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.cli import main, run
from trackseerr.clients.core_client import CoreClient
from trackseerr.config import Config
from trackseerr.gateway_link import (
    HANDSHAKE_OK,
    HANDSHAKE_UNREACHABLE,
    GatewayLinkWorker,
    ProtocolMismatch,
    compute_gateway_status,
    perform_handshake,
    record_heartbeat,
)
from trackseerr.models import MusicRequest
from trackseerr.role_change import current_notice, dismiss_notice, record_boot_role
from trackseerr.role_guard import (
    CODE_APP_URL_MISSING,
    CODE_APP_URL_SELF,
    CODE_BIND_ALL,
    CODE_CORE_URL,
    CODE_DB_PRESENT,
    CODE_FORBIDDEN_ENV,
    CODE_SECRET_WEAK,
    GATEWAY_FORBIDDEN_ENV,
    RealFs,
    check_role_environment,
)
from trackseerr.storage import Database

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _clean_state():
    internal_auth._nonce_cache.clear()
    gateway_link.clear_memory()
    yield
    internal_auth._nonce_cache.clear()
    gateway_link.clear_memory()


# --------------------------------------------------------------------------- guardrail pure function


class FakeFs:
    def __init__(self, owners: Optional[dict[str, str]] = None) -> None:
        self.owners = owners or {}

    def db_owner(self, directory: str) -> Optional[str]:
        return self.owners.get(directory)

    def db_owner_file(self, path: str) -> Optional[str]:
        return self.owners.get(path)


def _gw_env(**extra: str) -> dict[str, str]:
    env = {
        "ROLE": "gateway",
        "INTERNAL_CORE_SECRET": SECRET,
        "TRACKSEERR_CORE_URL": "http://trackseerr-core:5251",
        "APPLICATION_URL": "https://requests.example.com",
    }
    env.update(extra)
    return env


def _codes(problems) -> set[str]:
    return {p.code for p in problems}


def test_gateway_clean_environment_has_no_problems():
    assert check_role_environment("gateway", _gw_env(), FakeFs()) == []


def test_all_in_one_has_no_new_checks():
    env = {"PLEX_TOKEN": "x", "LASTFM_API_KEY": "y"}
    assert check_role_environment("all-in-one", env, FakeFs({"/config": ""})) == []


@pytest.mark.parametrize("name", GATEWAY_FORBIDDEN_ENV)
def test_gateway_refuses_each_forbidden_secret(name):
    problems = check_role_environment("gateway", _gw_env(**{name: "super-secret-value"}), FakeFs())
    assert _codes(problems) == {CODE_FORBIDDEN_ENV}
    assert all(p.fatal for p in problems)
    assert name in problems[0].message
    assert "super-secret-value" not in problems[0].message


def test_gateway_forbidden_env_empty_value_is_ignored():
    assert check_role_environment("gateway", _gw_env(PLEX_TOKEN="  "), FakeFs()) == []


def test_gateway_refuses_download_client_credentials():
    env = _gw_env(QBITTORRENT_PASSWORD="hunter2", SABNZBD_API_KEY="k")
    problems = check_role_environment("gateway", env, FakeFs())
    assert _codes(problems) == {CODE_FORBIDDEN_ENV}
    assert "QBITTORRENT_PASSWORD" in problems[0].message and "SABNZBD_API_KEY" in problems[0].message
    assert "hunter2" not in problems[0].message


@pytest.mark.parametrize("owner", ["", "all-in-one", "core"])
def test_gateway_refuses_a_foreign_database(owner):
    problems = check_role_environment("gateway", _gw_env(), FakeFs({"/config": owner}))
    assert _codes(problems) == {CODE_DB_PRESENT}


def test_gateway_accepts_its_own_database_and_checks_configured_dirs():
    assert check_role_environment("gateway", _gw_env(), FakeFs({"/config": "gateway", "/data": "gateway"})) == []
    problems = check_role_environment("gateway", _gw_env(CONFIG_DIR="/srv/cfg"), FakeFs({"/srv/cfg": "core"}))
    assert _codes(problems) == {CODE_DB_PRESENT}


@pytest.mark.parametrize("secret", [None, "", "short"])
def test_gateway_and_core_refuse_missing_or_weak_secret(secret):
    for role in ("gateway", "core"):
        env = _gw_env()
        env.pop("INTERNAL_CORE_SECRET")
        if secret is not None:
            env["INTERNAL_CORE_SECRET"] = secret
        problems = check_role_environment(role, env, FakeFs())
        assert CODE_SECRET_WEAK in _codes(problems)
        assert any(p.fatal for p in problems if p.code == CODE_SECRET_WEAK)


@pytest.mark.parametrize("url", [None, "", "core:5251", "ftp://core:5251", "http://"])
def test_gateway_refuses_bad_core_url(url):
    env = _gw_env()
    env.pop("TRACKSEERR_CORE_URL")
    if url is not None:
        env["TRACKSEERR_CORE_URL"] = url
    assert _codes(check_role_environment("gateway", env, FakeFs())) == {CODE_CORE_URL}


def test_gateway_accepts_https_core_url():
    assert check_role_environment("gateway", _gw_env(TRACKSEERR_CORE_URL="https://core.lan"), FakeFs()) == []


def test_gateway_refuses_missing_application_url():
    env = _gw_env()
    env.pop("APPLICATION_URL")
    assert _codes(check_role_environment("gateway", env, FakeFs())) == {CODE_APP_URL_MISSING}


def test_gateway_reports_every_problem_at_once():
    env = {"PLEX_TOKEN": "x"}
    assert _codes(check_role_environment("gateway", env, FakeFs({"/data": ""}))) == {
        CODE_FORBIDDEN_ENV,
        CODE_DB_PRESENT,
        CODE_SECRET_WEAK,
        CODE_CORE_URL,
        CODE_APP_URL_MISSING,
    }


def _core_env(**extra: str) -> dict[str, str]:
    env = {"ROLE": "core", "INTERNAL_CORE_SECRET": SECRET, "PORT": "5251", "CORE_LAN_BIND": "192.168.1.10"}
    env.update(extra)
    return env


def test_core_clean_environment_has_no_problems():
    assert check_role_environment("core", _core_env(APPLICATION_URL="https://requests.example.com"), FakeFs()) == []


def test_core_warns_when_application_url_is_its_own_address():
    for url in ("http://localhost:5251", "http://127.0.0.1:5251/", "http://192.168.1.10:5251"):
        problems = check_role_environment("core", _core_env(APPLICATION_URL=url, HOST="192.168.1.10"), FakeFs())
        assert _codes(problems) == {CODE_APP_URL_SELF}, url
        assert not problems[0].fatal


def test_core_does_not_warn_for_other_port_or_host():
    env = _core_env(APPLICATION_URL="http://localhost:9999", HOST="192.168.1.10")
    assert check_role_environment("core", env, FakeFs()) == []


def test_core_warns_on_bind_all_without_lan_bind_hint():
    env = _core_env()
    env.pop("CORE_LAN_BIND")
    problems = check_role_environment("core", env, FakeFs())
    assert _codes(problems) == {CODE_BIND_ALL} and not problems[0].fatal
    env["HOST"] = "127.0.0.1"
    assert check_role_environment("core", env, FakeFs()) == []


def test_realfs_reads_last_role_read_only(tmp_path):
    fs = RealFs()
    assert fs.db_owner(str(tmp_path)) is None
    db = Database(str(tmp_path / "sync_db.sqlite"))
    assert fs.db_owner(str(tmp_path)) == "gateway"  # fresh DB, no core-only content: not core-like
    db.upsert_playlist("pl-1", "Road Trip", service="spotify")
    assert fs.db_owner(str(tmp_path)) == "core"  # content says core
    db.set_last_role("gateway")
    assert fs.db_owner(str(tmp_path)) == "gateway"  # explicit gateway marker wins
    db.close()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    sqlite3.connect(legacy / "sync_db.sqlite").close()  # zero-byte file
    assert fs.db_owner(str(legacy)) == "gateway"


# --------------------------------------------------------------------------- refusal through create_app / cli


def _base_gateway_config(tmp_path: Path) -> Config:
    return Config(
        plex_url="",
        plex_token="",
        data_dir=str(tmp_path),
        role="gateway",
        internal_core_secret=SECRET,
        trackseerr_core_url="http://core.internal:5251",
        application_url="https://requests.example.com",
    )


def test_create_app_refuses_gateway_with_forbidden_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTFM_API_SECRET", "leaky-secret-value")
    with pytest.raises(RuntimeError) as exc:
        create_app(db=Database(":memory:"), config=_base_gateway_config(tmp_path))
    assert "LASTFM_API_SECRET" in str(exc.value)
    assert "leaky-secret-value" not in str(exc.value)


def _gateway_env(monkeypatch, tmp_path: Path, **extra: str) -> None:
    for key in list(os.environ):
        if key in GATEWAY_FORBIDDEN_ENV:
            monkeypatch.delenv(key)
    base = {
        "ROLE": "gateway",
        "INTERNAL_CORE_SECRET": SECRET,
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "APPLICATION_URL": "https://requests.example.com",
        "CONFIG_DIR": str(tmp_path / "cfg"),
        "DATA_DIR": str(tmp_path / "data"),
    }
    base.update(extra)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_create_app_without_config_refuses_missing_urls(tmp_path, monkeypatch):
    _gateway_env(monkeypatch, tmp_path)
    monkeypatch.delenv("APPLICATION_URL")
    with pytest.raises(RuntimeError, match="APPLICATION_URL"):
        create_app()
    monkeypatch.setenv("APPLICATION_URL", "https://requests.example.com")
    monkeypatch.setenv("TRACKSEERR_CORE_URL", "not-a-url")
    with pytest.raises(RuntimeError, match="TRACKSEERR_CORE_URL"):
        create_app()


def test_create_app_without_config_refuses_core_database_but_allows_own(tmp_path, monkeypatch):
    _gateway_env(monkeypatch, tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    db = Database(str(cfg / "sync_db.sqlite"))
    db.upsert_playlist("pl-1", "Road Trip", service="spotify")  # core-only content
    with pytest.raises(RuntimeError, match="core-like TrackSeerr database"):
        create_app()
    db.set_last_role("gateway")
    create_app()  # its own DB is fine
    db.close()


def test_create_app_refuses_weak_core_secret(tmp_path):
    cfg = _base_gateway_config(tmp_path)
    cfg.role = "core"
    cfg.internal_core_secret = "short"
    with pytest.raises(RuntimeError):
        create_app(db=Database(":memory:"), config=cfg)


def test_cli_gateway_refuses_with_one_stderr_line_per_problem(tmp_path, capsys):
    env = {"ROLE": "gateway", "PLEX_TOKEN": "tok-should-not-print", "DATA_DIR": str(tmp_path), "CONFIG_DIR": str(tmp_path)}
    with patch.dict(os.environ, {**env, "INTERNAL_CORE_SECRET": SECRET}, clear=True):
        assert main() == 1
    err = capsys.readouterr().err
    assert "PLEX_TOKEN" in err and "tok-should-not-print" not in err
    assert "TRACKSEERR_CORE_URL" in err and "APPLICATION_URL" in err
    assert 'README "Two-tier deployment"' in err
    assert err.count("ERROR: ROLE=gateway:") == 3


def test_cli_core_warns_but_starts_checks(tmp_path, caplog):
    env = {
        "ROLE": "core",
        "INTERNAL_CORE_SECRET": SECRET,
        "MEDIA_SERVER": "plex",
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
    }
    with patch.dict(os.environ, env, clear=True), caplog.at_level(logging.WARNING):
        assert main() == 1  # explicit MEDIA_SERVER=plex without PLEX_URL/TOKEN refuses, after the new warning
    assert any("CORE_LAN_BIND" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- version / protocol


def test_protocol_version_and_package_version_agree():
    assert internal_auth.PROTOCOL_VERSION == 1
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8"))
    assert pyproject["project"]["version"] == trackseerr.__version__


# --------------------------------------------------------------------------- hello / heartbeat


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


def _config(tmp_path: Path, role: str = "core") -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET,
        trackseerr_core_url="http://core.internal:5251",
        application_url="https://requests.example.com",
    )


def _client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _signed(method: str, target: str, user_id: str = "", user_name: str = "", body: bytes = b"", secret: str = SECRET):
    headers = internal_auth.sign_assertion(secret, method, target, user_id, user_name, body)
    if body:
        headers["Content-Type"] = "application/json"
    return headers


def _admin_headers(db: Database, cfg: Config) -> dict[str, str]:
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(user_id="admin-1", username="root", is_admin=True, secret_key=key)
    db.create_session(token, "admin-1", {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _user_headers(db: Database, cfg: Config) -> dict[str, str]:
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(user_id="1001", username="alice", is_admin=False, secret_key=key)
    db.create_session(token, "1001", {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def core(db, tmp_path):
    cfg = _config(tmp_path, "core")
    return _client(db, cfg), cfg


def test_hello_service_principal_gets_handshake(core, db):
    client, _ = core
    res = client.get("/api/internal/hello", headers=_signed("GET", "/api/internal/hello"))
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "protocol": 1,
        "version": trackseerr.__version__,
        "role": "core",
        "instance_id": db.get_instance_id(),
    }
    assert len(body["instance_id"]) >= 16


def test_instance_id_is_stable_across_calls_and_reopen(tmp_path):
    path = str(tmp_path / "sync_db.sqlite")
    d = Database(path)
    first = d.get_instance_id()
    assert d.get_instance_id() == first
    d.close()
    d2 = Database(path)
    assert d2.get_instance_id() == first
    d2.close()


def _hb_body(**over: Any) -> bytes:
    payload = {"version": "1.0.0", "protocol": 1, "gateway_id": "gw1", "started_at": time.time(), "active_sessions": 3}
    payload.update(over)
    return json.dumps(payload).encode()


def test_heartbeat_service_principal_is_accepted_and_stored(core, db):
    client, _ = core
    body = _hb_body()
    res = client.post("/api/internal/gateway-heartbeat", content=body, headers=_signed("POST", "/api/internal/gateway-heartbeat", body=body))
    assert res.status_code == 200 and res.json()["ok"] is True
    assert json.loads(db.get_kv("gateway_status"))["active_sessions"] == 3


def test_heartbeat_rejects_invalid_payload(core):
    client, _ = core
    body = _hb_body(active_sessions=-1)
    res = client.post("/api/internal/gateway-heartbeat", content=body, headers=_signed("POST", "/api/internal/gateway-heartbeat", body=body))
    assert res.status_code == 422


@pytest.mark.parametrize("path,method", [("/api/internal/hello", "GET"), ("/api/internal/gateway-heartbeat", "POST")])
def test_internal_endpoints_are_404_for_everyone_but_the_service_principal(core, db, path, method):
    client, cfg = core
    body = _hb_body() if method == "POST" else b""
    kwargs: dict[str, Any] = {"content": body} if body else {}
    ctype = {"Content-Type": "application/json"} if body else {}
    # unauthenticated
    assert client.request(method, path, **kwargs, headers=ctype).status_code == 404
    # admin session
    assert client.request(method, path, **kwargs, headers={**ctype, **_admin_headers(db, cfg)}).status_code == 404
    # signed as an ordinary user
    user_hdr = _signed(method, path, "1001", "alice", body)
    assert client.request(method, path, **kwargs, headers=user_hdr).status_code == 404
    # signed with the wrong secret
    bad = _signed(method, path, "", "", body, secret="x" * 40)
    assert client.request(method, path, **kwargs, headers=bad).status_code == 404
    # a heartbeat from a rejected caller must not have been recorded
    assert db.get_kv("gateway_status") is None


def test_internal_endpoints_are_404_on_the_gateway(db, tmp_path):
    cfg = _config(tmp_path, "gateway")
    client = _client(db, cfg)
    assert client.get("/api/internal/hello", headers=_signed("GET", "/api/internal/hello")).status_code in (400, 404)


# --------------------------------------------------------------------------- handshake


class FakeClient:
    """Stands in for CoreClient: ``script`` items are (status, body) tuples or exceptions."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.heartbeats: list[dict[str, Any]] = []
        self.heartbeat_status = 200

    def hello(self):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def heartbeat(self, payload):
        self.heartbeats.append(payload)
        return self.heartbeat_status


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _ok(version: str = trackseerr.__version__, protocol: int = 1):
    return 200, {"protocol": protocol, "version": version, "role": "core", "instance_id": "abc"}


def test_handshake_success_first_try():
    clock = Clock()
    client = FakeClient([_ok()])
    result = perform_handshake(client, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.state == HANDSHAKE_OK and clock.sleeps == []


def test_handshake_retries_with_backoff_until_core_boots():
    clock = Clock()
    boom = httpx.ConnectError("refused")
    client = FakeClient([boom, boom, (503, {}), _ok()])
    result = perform_handshake(client, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.state == HANDSHAKE_OK
    assert client.calls == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_handshake_gives_up_after_the_deadline_and_fails_closed(caplog):
    clock = Clock()
    client = FakeClient([httpx.ConnectError("refused")])
    with caplog.at_level(logging.ERROR):
        result = perform_handshake(client, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.state == HANDSHAKE_UNREACHABLE
    assert clock.now >= 60.0 and clock.now <= 60.0 + 1e-9
    assert max(clock.sleeps) <= 10.0
    assert any("did not complete the handshake" in r.getMessage() for r in caplog.records)


def test_handshake_404_is_retried_not_a_protocol_mismatch(caplog):
    clock = Clock()
    client = FakeClient([(404, {}), _ok()])
    assert perform_handshake(client, sleep=clock.sleep, monotonic=clock.monotonic).state == HANDSHAKE_OK


def test_handshake_protocol_mismatch_raises_with_exact_message():
    client = FakeClient([_ok(protocol=2)])
    with pytest.raises(ProtocolMismatch) as exc:
        perform_handshake(client, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert str(exc.value) == (
        "Gateway protocol 1 != core protocol 2 — run the same TrackSeerr version on both containers"
    )


def test_handshake_version_mismatch_only_warns(caplog):
    client = FakeClient([_ok(version="9.9.9")])
    with caplog.at_level(logging.WARNING):
        result = perform_handshake(client, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert result.state == HANDSHAKE_OK and result.core_version == "9.9.9"
    assert any("9.9.9" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def _gateway_cli_env(tmp_path: Path) -> dict[str, str]:
    return {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": SECRET,
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
        "PORT": "5250",
    }


@patch("trackseerr.gateway_link.GatewayLinkWorker.start")
@patch("trackseerr.cli.uvicorn.Server")
def test_cli_gateway_refuses_to_start_on_protocol_mismatch(mock_server, mock_worker, tmp_path, capsys):
    with patch.dict(os.environ, _gateway_cli_env(tmp_path), clear=True), patch.object(
        CoreClient, "hello", return_value=_ok(protocol=2)
    ):
        assert main() == 1
    assert "Gateway protocol 1 != core protocol 2" in capsys.readouterr().err
    mock_server.return_value.run.assert_not_called()
    mock_worker.assert_not_called()


@patch("trackseerr.gateway_link.GatewayLinkWorker.start")
@patch("trackseerr.cli.uvicorn.Server")
def test_cli_gateway_starts_on_version_mismatch_and_starts_heartbeat(mock_server, mock_worker, tmp_path, caplog):
    with patch.dict(os.environ, _gateway_cli_env(tmp_path), clear=True), patch.object(
        CoreClient, "hello", return_value=_ok(version="9.9.9")
    ), caplog.at_level(logging.WARNING):
        assert main() == 0
    mock_server.return_value.run.assert_called_once()
    mock_worker.assert_called_once()
    assert mock_worker.call_args.kwargs["handshaken"] is True
    assert any("9.9.9" in r.getMessage() for r in caplog.records)


@patch("trackseerr.gateway_link.GatewayLinkWorker.stop")
@patch("trackseerr.gateway_link.GatewayLinkWorker.start")
@patch("trackseerr.cli.uvicorn.Server")
def test_cli_gateway_starts_failing_closed_when_core_is_unreachable(mock_server, mock_worker, mock_stop, tmp_path):
    clock = Clock()
    real = perform_handshake

    def fast(client, **kw):
        return real(client, sleep=clock.sleep, monotonic=clock.monotonic, **{k: v for k, v in kw.items() if k == "should_stop"})

    with patch.dict(os.environ, _gateway_cli_env(tmp_path), clear=True), patch.object(
        CoreClient, "hello", side_effect=httpx.ConnectError("refused")
    ), patch("trackseerr.gateway_link.perform_handshake", fast):
        assert main() == 0
    mock_server.return_value.run.assert_called_once()
    assert mock_worker.call_args.kwargs["handshaken"] is False
    mock_stop.assert_called_once()  # stopped on shutdown like the other workers


def test_gateway_database_records_its_own_role_so_a_restart_is_allowed(tmp_path):
    with patch.dict(os.environ, _gateway_cli_env(tmp_path), clear=True), patch.object(
        CoreClient, "hello", return_value=_ok()
    ), patch("trackseerr.gateway_link.GatewayLinkWorker.start"), patch(
        "trackseerr.cli.uvicorn.Server"
    ):
        assert main() == 0
        assert main() == 0  # second boot: DB in CONFIG_DIR is the gateway's own
    assert RealFs().db_owner(str(tmp_path)) == "gateway"


def test_worker_retries_handshake_then_heartbeats():
    worker = GatewayLinkWorker()
    client = FakeClient([httpx.ConnectError("down"), _ok()])
    counter = lambda: 7  # noqa: E731
    assert worker.tick(client, counter) == -1.0 and client.heartbeats == []
    assert worker.tick(client, counter) == 0.0
    assert worker.handshaken is True
    hb = client.heartbeats[0]
    assert hb["active_sessions"] == 7 and hb["protocol"] == 1 and hb["gateway_id"] == worker.gateway_id
    assert set(hb) == {"version", "protocol", "gateway_id", "started_at", "active_sessions"}


def test_worker_with_protocol_mismatch_after_start_does_not_heartbeat(caplog):
    worker = GatewayLinkWorker()
    client = FakeClient([_ok(protocol=3)])
    with caplog.at_level(logging.ERROR):
        assert worker.tick(client, lambda: 0) == -1.0
    assert client.heartbeats == [] and any("protocol 3" in r.getMessage() for r in caplog.records)


def test_worker_thread_starts_and_stops_cleanly():
    worker = GatewayLinkWorker()
    client = FakeClient([_ok()])
    worker.start(client, lambda: 1, handshaken=True, interval=0.01, retry_interval=0.01)
    deadline = time.time() + 3
    while not client.heartbeats and time.time() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert client.heartbeats
    assert worker._thread is None


def test_core_client_hello_and_heartbeat_sign_as_service_principal():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"protocol": 1})

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    with patch("trackseerr.clients.core_client.httpx.Client", lambda **kw: real_client(transport=transport, **kw)):
        cc = CoreClient("http://core:5251", SECRET)
        assert cc.hello() == (200, {"protocol": 1})
        assert cc.heartbeat({"version": "1"}) == 200
    assert [r.url.path for r in captured] == ["/api/internal/hello", "/api/internal/gateway-heartbeat"]
    assert all(r.headers[internal_auth.HEADER_USER_ID] == "" for r in captured)
    assert all(SECRET not in str(r.headers) for r in captured)


# --------------------------------------------------------------------------- gateway-status


def _status(client, db, cfg, **kw):
    return client.get("/api/admin/gateway-status", headers=_admin_headers(db, cfg))


def test_status_never_seen(core, db):
    client, cfg = core
    res = _status(client, db, cfg)
    assert res.status_code == 200
    assert res.json() == {
        "configured": True,
        "public_url": "https://requests.example.com",
        "last_seen_at": None,
        "version": None,
        "protocol": None,
        "version_match": None,
        "active_sessions": None,
        "state": "never_seen",
    }


def test_status_online_after_a_signed_heartbeat(core, db):
    client, cfg = core
    body = _hb_body(active_sessions=4)
    client.post("/api/internal/gateway-heartbeat", content=body, headers=_signed("POST", "/api/internal/gateway-heartbeat", body=body))
    data = _status(client, db, cfg).json()
    assert data["state"] == "online"
    assert data["active_sessions"] == 4
    assert data["version"] == "1.0.0" and data["protocol"] == 1
    assert data["version_match"] is True
    assert data["last_seen_at"].endswith("+00:00")


def test_status_version_mismatch_is_reported(core, db):
    client, cfg = core
    record_heartbeat(db, {"version": "0.0.1", "protocol": 1, "gateway_id": "g", "started_at": 0, "active_sessions": 0})
    assert _status(client, db, cfg).json()["version_match"] is False


def test_status_goes_stale_after_three_minutes(core, db):
    client, cfg = core
    payload = {"version": "1.0.0", "protocol": 1, "gateway_id": "g", "started_at": 0, "active_sessions": 1}
    record_heartbeat(db, payload, now=time.time() - 170)
    assert _status(client, db, cfg).json()["state"] == "online"
    record_heartbeat(db, payload, now=time.time() - 200)
    assert _status(client, db, cfg).json()["state"] == "stale"


def test_status_survives_a_restart_through_the_kv_row(core, db):
    client, cfg = core
    record_heartbeat(db, {"version": "1.0.0", "protocol": 1, "gateway_id": "g", "started_at": 0, "active_sessions": 2})
    gateway_link.clear_memory()  # simulates a process restart: memory gone, row remains
    data = _status(client, db, cfg).json()
    assert data["state"] == "online" and data["active_sessions"] == 2


def test_status_not_used_on_all_in_one(db, tmp_path):
    cfg = _config(tmp_path, "all-in-one")
    client = _client(db, cfg)
    data = _status(client, db, cfg).json()
    assert data["state"] == "not_used" and data["configured"] is False
    assert data["last_seen_at"] is None and data["version_match"] is None


def test_status_public_url_null_when_unset(db, tmp_path, monkeypatch):
    monkeypatch.delenv("APPLICATION_URL", raising=False)
    monkeypatch.delenv("APP_URL", raising=False)
    cfg = _config(tmp_path, "core")
    cfg.application_url = None
    client = _client(db, cfg)
    data = _status(client, db, cfg).json()
    assert data["public_url"] is None and data["configured"] is False


def test_status_requires_an_admin(core, db):
    client, cfg = core
    assert client.get("/api/admin/gateway-status").status_code == 401
    assert client.get("/api/admin/gateway-status", headers=_user_headers(db, cfg)).status_code == 403
    forwarded = _signed("GET", "/api/admin/gateway-status", "1001", "alice")
    assert client.get("/api/admin/gateway-status", headers=forwarded).status_code == 403


def test_status_is_404_on_the_gateway_even_for_admin_sessions(db, tmp_path):
    cfg = _config(tmp_path, "gateway")
    client = _client(db, cfg)
    for path in ("/api/admin/gateway-status", "/api/admin/role-change-notice"):
        assert client.get(path, headers=_admin_headers(db, cfg)).status_code == 404
        assert client.get(path).status_code == 404
    assert client.post("/api/admin/role-change-notice/dismiss", headers=_admin_headers(db, cfg)).status_code == 404


def test_compute_status_is_pure_over_injected_clock(db):
    record_heartbeat(db, {"version": "1.0.0", "protocol": 1, "gateway_id": "g", "started_at": 0, "active_sessions": 0}, now=1000.0)
    assert compute_gateway_status(db, role="core", public_url="", now=1100.0)["state"] == "online"
    assert compute_gateway_status(db, role="core", public_url="", now=1181.0)["state"] == "stale"


# --------------------------------------------------------------------------- health tier and manifest


@pytest.mark.parametrize("role,tier", [("gateway", "gateway"), ("core", "core"), ("all-in-one", "all-in-one")])
def test_health_exposes_only_tier(db, tmp_path, role, tier):
    client = _client(db, _config(tmp_path, role))
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "tier": tier}


def test_manifest_is_role_aware_and_reachable_on_the_gateway(db, tmp_path):
    gw = _client(db, _config(tmp_path, "gateway")).get("/manifest.json")
    assert gw.status_code == 200
    body = gw.json()
    assert body["name"] == "TrackSeerr Requests" and body["short_name"] == "Requests"
    assert body["start_url"] == "/" and body["icons"]  # everything else from the static manifest
    for role in ("core", "all-in-one"):
        other = _client(db, _config(tmp_path, role)).get("/manifest.json").json()
        assert other["name"] == "TrackSeerr" and other["short_name"] == "TrackSeerr"
        assert other["icons"] == body["icons"]


# --------------------------------------------------------------------------- init-dmz


def _run_init(args: list[str], **kw: Any):
    out, err = io.StringIO(), io.StringIO()
    code = init_dmz.main(args, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_dmz_writes_files_with_secure_env(tmp_path):
    code, out, _ = _run_init(["--out", str(tmp_path / "o"), "--public-url", "https://r.example.org/", "--network", "dmz-net"])
    assert code == 0
    compose = tmp_path / "o" / "docker-compose.dmz.yml"
    env = tmp_path / "o" / ".env"
    assert compose.is_file() and env.is_file()
    assert _mode(env) == 0o600
    env_vars = dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line and not line.startswith("#"))
    secret = env_vars["INTERNAL_CORE_SECRET"].strip("'")
    assert len(secret) == 64 and int(secret, 16) >= 0
    assert env_vars["APPLICATION_URL"] == "'https://r.example.org'"
    assert env_vars["TRACKSEERR_INTERNAL_NETWORK"] == "'dmz-net'"
    # the compose file holds no secret value and keeps the roles apart
    text = compose.read_text()
    assert secret not in text and "ROLE=gateway" in text and 'ROLE: "core"' in text
    # secret shown exactly once on stdout
    assert out.count(secret) == 1
    assert "--network=proxynet --network=dmz-net" in out
    assert "--network=trackseerr-core-lan --network=dmz-net" in out
    assert out.count("--network=proxynet --network=dmz-net") == 2
    assert "docker network create --internal dmz-net" in out
    assert "http://trackseerr-core:5251" in out


def test_init_dmz_compose_is_valid_yaml_and_keeps_gateway_free_of_volumes(tmp_path):
    yaml = pytest.importorskip("yaml")
    assert _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])[0] == 0
    data = yaml.safe_load((tmp_path / "docker-compose.dmz.yml").read_text())
    gw = data["services"]["trackseerr-requests"]
    assert "volumes" not in gw and not any("PLEX" in e or "LASTFM" in e for e in gw["environment"])
    assert data["networks"]["internal"]["internal"] is True


def test_init_dmz_never_overwrites_existing_files(tmp_path):
    (tmp_path / ".env").write_text("KEEP=me\n")
    (tmp_path / "docker-compose.dmz.yml").write_text("keep: me\n")
    code, out, _ = _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])
    assert code == 0
    assert (tmp_path / ".env").read_text() == "KEEP=me\n"
    assert (tmp_path / "docker-compose.dmz.yml").read_text() == "keep: me\n"
    assert (tmp_path / ".env.new").is_file() and (tmp_path / "docker-compose.dmz.yml.new").is_file()
    assert _mode(tmp_path / ".env.new") == 0o600
    assert "NOT overwritten" in out
    # again: the .new files are preserved too
    first_new = (tmp_path / ".env.new").read_text()
    _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])
    assert (tmp_path / ".env.new").read_text() == first_new
    assert (tmp_path / ".env.new.1").is_file() and _mode(tmp_path / ".env.new.1") == 0o600


def test_init_dmz_does_not_follow_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (tmp_path / ".env").symlink_to(victim)
    assert _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])[0] == 0
    assert victim.read_text() == "precious"
    assert (tmp_path / ".env.new").is_file() and not (tmp_path / ".env.new").is_symlink()


def test_init_dmz_from_existing_env_file_carries_settings_by_reference(tmp_path):
    src = tmp_path / "existing.env"
    src.write_text(
        "# comment\nPLEX_URL=http://plex:32400\nPLEX_TOKEN=tok-AAA-111\nexport LASTFM_API_SECRET='lfm-secret-$x'\n"
        "SECONDS_TO_WAIT=600\nHOST=0.0.0.0\nINTERNAL_CORE_SECRET=old-should-not-carry\nUNRELATED=zzz\n"
    )
    out_dir = tmp_path / "o"
    code, out, err = _run_init(["--from-existing", "--env-file", str(src), "--out", str(out_dir), "--public-url", "https://r.example.org"])
    assert code == 0
    compose = (out_dir / "docker-compose.dmz.yml").read_text()
    env = (out_dir / ".env").read_text()
    # secrets by reference in compose, values only in .env
    assert 'PLEX_TOKEN: "${PLEX_TOKEN}"' in compose and "tok-AAA-111" not in compose
    assert 'LASTFM_API_SECRET: "${LASTFM_API_SECRET}"' in compose and "lfm-secret" not in compose
    assert "PLEX_TOKEN='tok-AAA-111'" in env and "LASTFM_API_SECRET='lfm-secret-$x'" in env
    # non-secret settings carried verbatim; unrelated / role / old secret are not
    assert 'PLEX_URL: "http://plex:32400"' in compose and 'SECONDS_TO_WAIT: "600"' in compose
    assert "UNRELATED" not in compose and "UNRELATED" not in env
    assert "old-should-not-carry" not in compose + env + out + err
    # nothing secret on stdout or stderr
    for leaked in ("tok-AAA-111", "lfm-secret"):
        assert leaked not in out and leaked not in err
    assert _mode(out_dir / ".env") == 0o600


def test_init_dmz_from_existing_reads_proc_environ(tmp_path):
    fake = tmp_path / "environ"
    fake.write_bytes(b"PLEX_TOKEN=proc-token-777\0PLEX_URL=http://plex:32400\0PATH=/usr/bin\0")
    code, out, err = _run_init(
        ["--from-existing", "--out", str(tmp_path / "o"), "--public-url", "https://r.example.org"],
        proc_environ_path=str(fake),
    )
    assert code == 0
    assert "proc-token-777" not in out + err + (tmp_path / "o" / "docker-compose.dmz.yml").read_text()
    assert "PLEX_TOKEN='proc-token-777'" in (tmp_path / "o" / ".env").read_text()
    assert "PATH" not in (tmp_path / "o" / "docker-compose.dmz.yml").read_text()


def test_init_dmz_from_existing_unreadable_source_is_an_error(tmp_path):
    code, _, err = _run_init(["--from-existing", "--out", str(tmp_path), "--public-url", "https://r.example.org"], proc_environ_path=str(tmp_path / "nope"))
    assert code == 2 and "could not read" in err
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    "args",
    [["--core-lan-bind", "0.0.0.0"], ["--network", "bad name!"], ["--public-url", "ftp://x"]],
)
def test_init_dmz_rejects_unsafe_arguments(tmp_path, args):
    code, _, err = _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org", *args])
    assert code == 2 and err
    assert list(tmp_path.iterdir()) == []


def test_init_dmz_without_public_url_warns_on_stderr(tmp_path):
    code, _, err = _run_init(["--out", str(tmp_path)])
    assert code == 0 and "placeholder" in err


def test_init_dmz_secret_is_fresh_each_run(tmp_path):
    a = _run_init(["--out", str(tmp_path / "a"), "--public-url", "https://r.example.org"])[1]
    b = _run_init(["--out", str(tmp_path / "b"), "--public-url", "https://r.example.org"])[1]
    assert a != b


def test_run_dispatches_init_dmz_and_defaults_to_the_server(tmp_path):
    with patch("trackseerr.cli.main", return_value=7) as server:
        assert run([]) == 7
        assert run(["something-else"]) == 7  # unknown args keep today's behaviour
        server.assert_called()
    with patch("trackseerr.init_dmz.main", return_value=3) as sub:
        assert run(["init-dmz", "--out", str(tmp_path)]) == 3
        sub.assert_called_once_with(["--out", str(tmp_path)])


def test_main_module_entrypoint_calls_run():
    src = (Path(trackseerr.__file__).parent / "__main__.py").read_text()
    assert "run(sys.argv[1:])" in src


# --------------------------------------------------------------------------- role flip


def _populate(db: Database) -> dict[str, Any]:
    db.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    db.upsert_playlist("pl-1", "Road Trip", service="spotify")
    db.create_request(MusicRequest(id="req-1", user_id="1001", item_type="album", title="OK Computer", artist="Radiohead"))
    db.update_general_settings({"application_url": "https://music.example.com"})
    return {"api_key": db.get_api_key()}


def _snapshot(db: Database) -> dict[str, Any]:
    return {
        "users": sorted(r[0] for r in db.conn.execute("SELECT id FROM users")),
        "playlist": db.get_playlist("pl-1")["name"],
        "request": db.conn.execute("SELECT title FROM music_requests WHERE id = 'req-1'").fetchone()[0],
        "app_url": db.get_general_settings()["application_url"],
        "api_key": db.get_api_key(),
        "instance_id": db.get_instance_id(),
    }


def test_all_in_one_to_core_keeps_every_row_and_shows_checklist_once(tmp_path, caplog):
    path = str(tmp_path / "sync_db.sqlite")
    db = Database(path)
    _populate(db)
    assert record_boot_role(db, "all-in-one") is None  # first ever boot: nothing to announce
    before = _snapshot(db)
    db.close()

    db = Database(path)  # container recreated with ROLE=core
    with caplog.at_level(logging.WARNING):
        notice = record_boot_role(db, "core")
    assert notice and notice["from_role"] == "all-in-one" and notice["to_role"] == "core"
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    for fragment in ("APPLICATION_URL", "proxy or tunnel", "Plex webhook URL is unchanged", "sign in once"):
        assert fragment in text
    assert _snapshot(db) == before
    assert db.get_last_role() == "core"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert record_boot_role(db, "core") is None  # second boot as core: silent
    assert not caplog.records
    assert current_notice(db)["active"] is True  # banner stays until dismissed
    db.close()


def test_notice_endpoints_show_then_dismiss(tmp_path):
    db = Database(str(tmp_path / "sync_db.sqlite"))
    _populate(db)
    record_boot_role(db, "all-in-one")
    record_boot_role(db, "core")
    cfg = _config(tmp_path, "core")
    client = _client(db, cfg)
    headers = _admin_headers(db, cfg)

    shown = client.get("/api/admin/role-change-notice", headers=headers).json()
    assert shown["active"] is True and shown["from_role"] == "all-in-one" and shown["to_role"] == "core"
    assert shown["changed_at"] and len(shown["checklist"]) == 4
    assert set(shown) == {"active", "from_role", "to_role", "changed_at", "checklist"}

    assert client.get("/api/admin/role-change-notice").status_code == 401
    assert client.post("/api/admin/role-change-notice/dismiss", headers=_user_headers(db, cfg)).status_code == 403
    assert client.get("/api/admin/role-change-notice", headers=_user_headers(db, cfg)).status_code == 403

    dismissed = client.post("/api/admin/role-change-notice/dismiss", headers=headers).json()
    assert dismissed["active"] is False and dismissed["checklist"] == []
    assert client.get("/api/admin/role-change-notice", headers=headers).json()["active"] is False
    # dismissing twice is harmless; a restart does not bring the banner back
    assert client.post("/api/admin/role-change-notice/dismiss", headers=headers).status_code == 200
    assert record_boot_role(db, "core") is None
    assert current_notice(db)["active"] is False
    db.close()


def test_core_back_to_all_in_one_keeps_data_and_all_admin_routes_work(tmp_path, caplog):
    path = str(tmp_path / "sync_db.sqlite")
    db = Database(path)
    _populate(db)
    record_boot_role(db, "all-in-one")
    before = _snapshot(db)
    record_boot_role(db, "core")
    dismiss_notice(db)
    db.close()

    for role in ("core", "all-in-one"):
        db = Database(path)
        if role == "all-in-one":
            with caplog.at_level(logging.WARNING):
                notice = record_boot_role(db, "all-in-one")
            assert notice and notice["from_role"] == "core" and notice["to_role"] == "all-in-one"
            assert any("ROLE=all-in-one" in r.getMessage() for r in caplog.records)
            assert current_notice(db)["active"] is True and len(current_notice(db)["checklist"]) == 4
        cfg = _config(tmp_path, role)
        client = _client(db, cfg)
        headers = _admin_headers(db, cfg)
        assert client.get("/api/admin/users", headers=headers).status_code == 200
        assert client.get("/api/playlists", headers=headers).status_code == 200
        assert client.get("/api/admin/gateway-status", headers=headers).status_code == 200
        assert client.get("/api/admin/role-change-notice", headers=headers).status_code == 200
        assert _snapshot(db) == before
        db.close()


def test_flip_back_and_forth_creates_a_fresh_notice_each_time(tmp_path):
    db = Database(str(tmp_path / "sync_db.sqlite"))
    _populate(db)
    record_boot_role(db, "all-in-one")
    assert record_boot_role(db, "core")["to_role"] == "core"
    assert record_boot_role(db, "all-in-one")["to_role"] == "all-in-one"
    assert current_notice(db)["to_role"] == "all-in-one"
    dismiss_notice(db)
    assert record_boot_role(db, "core") is not None and current_notice(db)["active"] is True
    db.close()


def test_fresh_install_and_gateway_never_show_a_notice(tmp_path):
    db = Database(str(tmp_path / "fresh.sqlite"))
    assert record_boot_role(db, "core") is None  # empty DB: nothing to migrate
    assert db.get_last_role() == "core" and current_notice(db)["active"] is False
    db.close()
    gw = Database(str(tmp_path / "gw.sqlite"))
    _populate(gw)
    assert record_boot_role(gw, "gateway") is None
    assert gw.get_last_role() == "gateway" and current_notice(gw)["active"] is False
    gw.close()


def test_empty_last_role_is_recorded_silently_never_inferred_as_a_flip(tmp_path):
    """A pre-v30 database has no last_role; no flip is inferred, whatever it contains."""
    for role in ("core", "gateway", "all-in-one"):
        db = Database(str(tmp_path / f"{role}.sqlite"))
        _populate(db)
        assert db.get_last_role() == ""
        assert record_boot_role(db, role) is None
        assert db.get_last_role() == role and current_notice(db)["active"] is False
        db.close()


def test_migrations_are_idempotent_across_role_flips(tmp_path):
    path = str(tmp_path / "sync_db.sqlite")
    for role in ("all-in-one", "core", "all-in-one", "core", "gateway"):
        db = Database(path)
        record_boot_role(db, role)
        versions = [r[0] for r in db.conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [SCHEMA_VERSION]
        db.close()
    db = Database(path)
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(general_settings)")]
    assert cols.count("last_role") == 1 and cols.count("instance_id") == 1
    db.close()


# --------------------------------------------------------------------------- audit fixes: role guard by content


def _make_db(path: Path) -> Database:
    return Database(str(path))


def _uvicorn_path_gateway_env(monkeypatch, tmp_path: Path) -> None:
    for k, v in _gateway_cli_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_PATH", raising=False)


def test_uvicorn_path_gateway_restarts_fine_twice(tmp_path, monkeypatch):
    """get_db() lazily creates the gateway DB; it must not brick the next restart."""
    _uvicorn_path_gateway_env(monkeypatch, tmp_path)
    from trackseerr.api import dependencies

    dependencies._db_instances.clear()
    try:
        create_app()
        db = get_db()
        assert db.get_last_role() == "gateway"
        db.set_last_role("")  # even if the marker were absent, an empty DB is not core-like
        db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
        create_app()
        create_app()
    finally:
        dependencies._db_instances.clear()


def test_create_app_records_gateway_role_on_a_supplied_db(tmp_path):
    cfg = _base_gateway_config(tmp_path)
    db = Database(":memory:")
    create_app(db=db, config=cfg)
    assert db.get_last_role() == "gateway"


def test_pre_v30_gateway_db_with_only_users_and_sessions_is_accepted(tmp_path, monkeypatch):
    path = tmp_path / "sync_db.sqlite"
    db = _make_db(path)
    db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    db.conn.execute("UPDATE general_settings SET last_role = ''")
    db.conn.commit()
    db.close()
    assert RealFs().db_owner(str(tmp_path)) == "gateway"
    _uvicorn_path_gateway_env(monkeypatch, tmp_path)
    create_app()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO library_artists (id, name, clean_name) VALUES ('a1', 'Radiohead', 'radiohead')",
        "UPDATE lidarr_settings SET api_key = 'k' WHERE id = 1",
        "UPDATE users SET password_hash = 'x'",
        "UPDATE general_settings SET lastfm_api_key = 'k' WHERE id = 1",
    ],
)
def test_gateway_refuses_core_like_database_content(tmp_path, monkeypatch, sql):
    path = tmp_path / "sync_db.sqlite"
    db = _make_db(path)
    db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    db.conn.execute(sql)
    db.conn.commit()
    db.close()
    assert RealFs().db_owner(str(tmp_path)) == "core"
    _uvicorn_path_gateway_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="Remove the /config volume from the Requests container"):
        create_app()


@pytest.mark.parametrize("table", ["download_clients", "indexers"])
def test_gateway_refuses_download_clients_and_indexers_rows(tmp_path, table):
    db = _make_db(tmp_path / "sync_db.sqlite")
    required = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})") if r[3] and r[4] is None and not r[5]]
    db.conn.execute(
        f"INSERT INTO {table} ({', '.join(required)}) VALUES ({', '.join('?' for _ in required)})",
        ["x"] * len(required),
    )
    db.conn.commit()
    db.close()
    assert RealFs().db_owner(str(tmp_path)) == "core"


def test_gateway_refuses_core_like_database_path_env(tmp_path, monkeypatch):
    core = tmp_path / "elsewhere" / "core.sqlite"
    core.parent.mkdir()
    db = _make_db(core)
    db.upsert_playlist("pl-1", "Road Trip", service="spotify")
    db.close()
    empty_cfg = tmp_path / "cfg"
    empty_cfg.mkdir()
    env = _gw_env(CONFIG_DIR=str(empty_cfg), DATABASE_PATH=str(core))
    problems = check_role_environment("gateway", env, RealFs())
    assert _codes(problems) == {CODE_DB_PRESENT}
    assert "Remove the /config volume from the Requests container" in problems[0].message


def test_gateway_corrupt_and_empty_database_files(tmp_path, caplog):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "sync_db.sqlite").write_bytes(b"")
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "sync_db.sqlite").write_bytes(b"this is definitely not sqlite" * 10)
    with caplog.at_level(logging.WARNING):
        assert RealFs().db_owner(str(empty)) == "gateway"
        assert RealFs().db_owner(str(corrupt)) == "core"
    assert any("Empty database file" in r.message for r in caplog.records)
    # SQLite header but truncated/garbled body: unreadable, not provably core-like
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "sync_db.sqlite").write_bytes(b"SQLite format 3\x00" + b"\xff" * 200)
    assert RealFs().db_owner(str(broken)) == "gateway"


# --------------------------------------------------------------------------- audit fixes: init-dmz


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "0:0:0:0:0:0:0:0", "::ffff:0.0.0.0", "999.1.1.1", "localhost", "1.2.3"])
def test_init_dmz_rejects_bad_core_lan_bind(tmp_path, bind):
    code, _, err = _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org", "--core-lan-bind", bind])
    assert code == 2 and err
    assert list(tmp_path.iterdir()) == []


def test_init_dmz_accepts_ipv4_and_brackets_ipv6(tmp_path):
    assert _run_init(["--out", str(tmp_path / "a"), "--public-url", "https://r.example.org", "--core-lan-bind", "192.168.1.10"])[0] == 0
    code, _, _ = _run_init(["--out", str(tmp_path / "b"), "--public-url", "https://r.example.org", "--core-lan-bind", "fd00::5"])
    assert code == 0
    env_text = (tmp_path / "b" / ".env").read_text()
    assert "CORE_LAN_BIND='[fd00::5]'" in env_text
    assert init_dmz.bind_for_compose("fd00::5") == "[fd00::5]"
    assert init_dmz.bind_for_compose("10.0.0.1") == "10.0.0.1"


@pytest.mark.parametrize(
    "url",
    ["https://", "http:///path", "ftp://x.example", "https://a b.example", "https://a.example/\tx", "https://a.example/\nx", "//x.example", "https://a.example:99999"],
)
def test_init_dmz_rejects_bad_public_url(tmp_path, url):
    code, _, err = _run_init(["--out", str(tmp_path), "--public-url", url])
    assert code == 2 and err
    assert list(tmp_path.iterdir()) == []


def test_init_dmz_writes_no_compose_when_env_write_fails(tmp_path):
    real = init_dmz.write_new_file

    def fail_env(directory, name, content, mode):
        if name == init_dmz.ENV_NAME:
            raise OSError(13, "denied")
        return real(directory, name, content, mode)

    with patch.object(init_dmz, "write_new_file", side_effect=fail_env):
        code, _, err = _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])
    assert code == 1 and "could not write" in err
    assert list(tmp_path.iterdir()) == []


def test_init_dmz_next_step_references_files_actually_written(tmp_path):
    (tmp_path / ".env").write_text("OLD=1\n")
    code, out, _ = _run_init(["--out", str(tmp_path), "--public-url", "https://r.example.org"])
    assert code == 0
    assert "--env-file .env.new up -d" in out and "--env-file .env up" not in out
    (tmp_path / "docker-compose.dmz.yml").write_text("x")  # already existed from the first run
    assert (tmp_path / "docker-compose.dmz.yml").exists()
