"""Boot/logging hardening: idempotent setup_logging, post-ready failure isolation, scheduler gating,
lazy Plex resolution, gateway step privacy, startup-503 security headers, and gateway login stamping."""

import io
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import cli, internal_auth
from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.boot import boot_state
from plex_playlist_sync.cli import RedactLogFilter, setup_logging
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _reset_boot_state():
    boot_state.mark_ready()
    yield
    boot_state.mark_ready()


def _config(tmp_path: Path, role: str = "all-in-one", **extra: Any) -> Config:
    return Config(
        application_url="https://music.example.com",
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET if role != "all-in-one" else None,
        trackseerr_core_url="http://core.internal:5251",
        **extra,
    )


# --------------------------------------------------------------------------- 1. logging


def test_setup_logging_is_idempotent_and_every_root_handler_is_redacting(tmp_path):
    root = logging.getLogger()
    cfg = _config(tmp_path)
    foreign = logging.StreamHandler(io.StringIO())  # a handler added by some third party before setup
    root.addHandler(foreign)
    setup_logging("INFO", cfg)
    setup_logging("DEBUG", cfg)
    setup_logging("INFO", cfg)
    stdout_handlers = [h for h in root.handlers if getattr(h, "name", None) == cli._STDOUT_HANDLER_NAME]
    assert len(stdout_handlers) == 1
    assert len([h for h in root.handlers if type(h).__name__ == "TimedLogFileHandler"]) == 1
    assert len([h for h in root.handlers if type(h).__name__ == "LogRingBuffer"]) == 1
    for handler in root.handlers:
        assert sum(isinstance(f, RedactLogFilter) for f in handler.filters) == 1, handler


def test_setup_logging_repoints_stale_stdout_handler(tmp_path):
    root = logging.getLogger()
    setup_logging("INFO", _config(tmp_path))
    handler = next(h for h in root.handlers if getattr(h, "name", None) == cli._STDOUT_HANDLER_NAME)
    dead = io.StringIO()
    handler.setStream(dead)
    dead.close()
    setup_logging("INFO", _config(tmp_path))
    assert handler.stream is sys.stdout
    assert len([h for h in root.handlers if getattr(h, "name", None) == cli._STDOUT_HANDLER_NAME]) == 1


# --------------------------------------------------------------------------- 2. lockout vs gateway login stamp


def _lock_cols(db: Database, uid: str) -> dict[str, Any]:
    row = db.conn.execute(
        "SELECT failed_logins, locked_until, last_login_at FROM users WHERE id = ?", (uid,)
    ).fetchone()
    return {"failed_logins": row[0], "locked_until": row[1], "last_login_at": row[2]}


def _post_status(client: TestClient, payload: dict[str, Any]):
    url = "/api/internal/auth/session-status"
    body = json.dumps(payload).encode()
    headers = internal_auth.sign_assertion(SECRET, "POST", url, "", "", body)
    headers["Content-Type"] = "application/json"
    return client.post(url, content=body, headers=headers)


def _core_client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def test_session_status_record_login_never_unlocks_a_local_user(tmp_path):
    db = Database(":memory:")
    try:
        db.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
        user, _raw = db.create_local_user("bob", "bob@x.tv", None, "admin-1")
        uid = user["id"]
        assert uid.startswith("local-")
        locked_until_us = int((time.time() + 3600) * 1_000_000)
        for _ in range(5):
            db.record_failed_login(uid, locked_until_us)
        before = _lock_cols(db, uid)
        assert before["failed_logins"] == 5 and before["locked_until"]
        internal_auth._nonce_cache.clear()
        client = _core_client(db, _config(tmp_path, "core"))
        res = _post_status(client, {"user_id": uid, "session_issued_at": 1, "record_login": True, "username": "bob"})
        assert res.json() == {"valid": True}
        after = _lock_cols(db, uid)
        assert after["failed_logins"] == 5
        assert after["locked_until"] == before["locked_until"]
        assert not after["last_login_at"]
    finally:
        db.close()


def test_stamp_last_login_leaves_lockout_columns_alone():
    db = Database(":memory:")
    try:
        db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
        db.record_failed_login("1001", int((time.time() + 600) * 1_000_000))
        db.stamp_last_login("1001")
        row = _lock_cols(db, "1001")
        assert row["last_login_at"] and row["failed_logins"] == 1 and row["locked_until"]
    finally:
        db.close()


# --------------------------------------------------------------------------- 3. background init isolation


def _run_init(tmp_path, *, patches: dict[str, Any], plex: Any = None):
    config = _config(tmp_path)
    clients = cli._Clients(plex=plex)
    server = MagicMock()
    server.should_exit = False
    failure = {"code": 0}
    boot_state.begin("starting")
    targets = {
        "plex_playlist_sync.cli._start_lidarr_trickle": {},
        "plex_playlist_sync.cli._start_local_workers": {},
        "plex_playlist_sync.cli._connect_clients": {"return_value": True},
        "plex_playlist_sync.cli._discover_plex_users": {},
        "plex_playlist_sync.acquisition_worker.acquisition_worker.start": {},
    }
    for name, kwargs in patches.items():
        targets[name] = kwargs
    managers = [patch(name, **kwargs) for name, kwargs in targets.items()]
    mocks = [m.start() for m in managers]
    try:
        cli._background_init(
            role="all-in-one", db=Database(":memory:"), config=config, clients=clients, server=server,
            failure=failure,
        )
    finally:
        for m in managers:
            m.stop()
    return server, failure, dict(zip(targets, mocks))


@pytest.mark.parametrize(
    "failing",
    [
        "plex_playlist_sync.cli._connect_clients",
        "plex_playlist_sync.cli._discover_plex_users",
        "plex_playlist_sync.acquisition_worker.acquisition_worker.start",
    ],
)
def test_post_ready_failure_degrades_but_keeps_serving(tmp_path, caplog, failing):
    caplog.set_level(logging.ERROR)
    server, failure, _ = _run_init(
        tmp_path, patches={failing: {"side_effect": RuntimeError("boom")}}, plex=MagicMock()
    )
    assert server.should_exit is False
    assert failure["code"] == 0
    assert boot_state.ready
    assert any(r.levelno == logging.ERROR and "RuntimeError" in r.getMessage() for r in caplog.records)


def test_pre_ready_failure_still_exits(tmp_path):
    server, failure, _ = _run_init(
        tmp_path, patches={"plex_playlist_sync.cli._start_local_workers": {"side_effect": RuntimeError("x")}}
    )
    assert server.should_exit is True and failure["code"] == 1
    assert not boot_state.ready


def test_scheduler_event_is_set_even_when_connect_fails(tmp_path):
    seen: list[threading.Event] = []

    def fake_scheduler(db, config, clients, clients_ready=None):
        seen.append(clients_ready)

    _run_init(
        tmp_path,
        patches={
            "plex_playlist_sync.cli._start_sync_scheduler": {"side_effect": fake_scheduler},
            "plex_playlist_sync.cli._connect_clients": {"side_effect": RuntimeError("down")},
        },
    )
    # wait_seconds default is > 0 so the scheduler was started with the gating event, now set
    assert seen and isinstance(seen[0], threading.Event) and seen[0].is_set()


# --------------------------------------------------------------------------- 4. scheduler waits for clients


def test_first_sync_waits_for_clients_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_shutdown_requested", False)
    cfg = _config(tmp_path, wait_seconds=1)
    ready = threading.Event()
    calls: list[Any] = []
    with patch("plex_playlist_sync.cli.sync_state.execute_sync", side_effect=lambda **kw: calls.append(kw)):
        cli._start_sync_scheduler(Database(":memory:"), cfg, cli._Clients(), ready)
        time.sleep(2.6)  # well past the 1s interval
        assert calls == [], "sync ran before clients were connected"
        ready.set()
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        monkeypatch.setattr(cli, "_shutdown_requested", True)
    assert calls, "sync never ran after clients connected"


# --------------------------------------------------------------------------- 5. lazy Plex client


def test_plex_provider_retries_connect_and_rate_limits(tmp_path):
    cfg = _config(tmp_path)
    clients = cli._Clients()
    sentinel = object()
    with patch("plex_playlist_sync.cli.PlexClient", side_effect=[ConnectionError("down"), sentinel]) as ctor:
        provider = cli._make_plex_provider(cfg, clients, retry_interval=0.0)
        assert provider() is None
        assert provider() is sentinel
        assert provider() is sentinel
        assert ctor.call_count == 2
    with patch("plex_playlist_sync.cli.PlexClient") as ctor:
        slow = cli._make_plex_provider(cfg, cli._Clients(), retry_interval=3600.0)
        assert slow() is None and slow() is None
        ctor.assert_not_called()  # bounded: no hot reconnect loop


def test_acquisition_worker_uses_plex_client_that_appears_later():
    worker = AcquisitionWorker()
    sentinel = object()
    state = {"plex": None}
    seen: list[Any] = []
    got_real = threading.Event()

    def fake_poll(db, plex_client=None, staging_dir=None):
        seen.append(plex_client)
        if plex_client is not None:
            got_real.set()
        return {}

    worker.poll_once = fake_poll  # type: ignore[method-assign]
    worker.start(db=MagicMock(), plex_client_provider=lambda: state["plex"], poll_interval=0.05)
    try:
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen and seen[0] is None
        state["plex"] = sentinel
        assert got_real.wait(timeout=3)
        assert seen[-1] is sentinel
    finally:
        worker.stop()


# --------------------------------------------------------------------------- 6/7. gateway privacy + headers


def _gateway_app(tmp_path):
    return create_app(db=Database(":memory:"), config=_config(tmp_path, "gateway"))


def test_gateway_health_and_503_publish_generic_step(tmp_path):
    client = TestClient(_gateway_app(tmp_path))
    boot_state.begin("waiting for core (protocol handshake)")
    health = client.get("/api/health").json()
    assert health["status"] == "starting" and health["step"] == "starting"
    res = client.get("/api/requests")
    assert res.status_code == 503
    assert res.json()["step"] == "starting"
    assert "core" not in res.text.lower().replace("trackseerr", "")
    assert "waiting" not in client.get("/api/health/ready").text


def test_core_keeps_detailed_step(tmp_path):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path, "core")))
    boot_state.begin("connecting to Plex")
    assert client.get("/api/health").json()["step"] == "connecting to Plex"


@pytest.mark.parametrize("role", ["gateway", "core", "all-in-one"])
def test_startup_503_carries_security_headers(tmp_path, role):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path, role)))
    boot_state.begin("starting")
    ok = client.get("/api/health")
    res = client.get("/api/requests")
    assert res.status_code == 503
    for header in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy", "Referrer-Policy"):
        assert header in res.headers, header
        assert res.headers[header] == ok.headers[header]
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Retry-After"]
