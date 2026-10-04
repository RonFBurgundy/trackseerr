"""Boot visibility: readiness state machine, 503 while starting, health shape, [boot] log lines, and
proof that slow external probes never delay readiness."""

import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import cli
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.boot import BootState, boot_state
from plex_playlist_sync.cli import main, setup_logging
from plex_playlist_sync.config import Config
from plex_playlist_sync.gateway_link import HANDSHAKE_OK, HANDSHAKE_UNREACHABLE, HandshakeResult, ProtocolMismatch
from plex_playlist_sync.storage import Database

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _reset_boot_state():
    boot_state.mark_ready()
    yield
    boot_state.mark_ready()


def _config(tmp_path: Path, role: str = "all-in-one") -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET if role != "all-in-one" else None,
        trackseerr_core_url="http://core.internal:5251",
    )


# --------------------------------------------------------------------------- state machine


def test_default_state_is_ready():
    state = BootState()
    assert state.ready is True
    assert state.snapshot() == {"status": "ok"}


def test_starting_then_ready_transitions():
    state = BootState()
    state.begin("opening database")
    assert state.ready is False
    assert state.snapshot() == {"status": "starting", "step": "opening database"}
    state.set_step("starting workers")
    assert state.snapshot() == {"status": "starting", "step": "starting workers"}
    state.mark_ready()
    assert state.ready is True
    assert state.snapshot() == {"status": "ok"}


def test_step_timer_logs_start_and_elapsed(caplog):
    state = BootState()
    state.begin("x")
    with caplog.at_level(logging.INFO, logger="plex_playlist_sync.boot"):
        with state.step_timer("config load"):
            assert state.step == "config load"
    messages = [r.getMessage() for r in caplog.records]
    assert "[boot] step: config load" in messages
    assert any(m.startswith("[boot] step: config load done in ") and m.endswith("s") for m in messages)


def test_step_timer_logs_failure_and_reraises(caplog):
    state = BootState()
    with caplog.at_level(logging.INFO, logger="plex_playlist_sync.boot"):
        with pytest.raises(RuntimeError):
            with state.step_timer("boom"):
                raise RuntimeError("nope")
    assert any("[boot] step: boom failed after" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- HTTP behavior


@pytest.mark.parametrize("role", ["all-in-one", "core", "gateway"])
def test_health_reports_starting_then_ok(tmp_path, role):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path, role)))
    boot_state.begin("waiting for core")
    res = client.get("/api/health")
    assert res.status_code == 200
    # The internet-facing gateway never publishes internal step names.
    step = "starting" if role == "gateway" else "waiting for core"
    assert res.json() == {"status": "starting", "step": step, "tier": role}
    boot_state.mark_ready()
    assert client.get("/api/health").json() == {"status": "ok", "tier": role}


def test_health_ready_probe_is_503_while_starting(tmp_path):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path)))
    boot_state.begin("initializing")
    res = client.get("/api/health/ready")
    assert res.status_code == 503
    assert res.headers["Retry-After"] == "5"
    assert res.json()["status"] == "starting"
    boot_state.mark_ready()
    assert client.get("/api/health/ready").status_code == 200


@pytest.mark.parametrize("role", ["all-in-one", "gateway"])
def test_health_ready_reachable_on_every_tier(tmp_path, role):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path, role)))
    assert client.get("/api/health/ready").status_code == 200


@pytest.mark.parametrize("role", ["all-in-one", "gateway"])
def test_api_routes_return_503_with_retry_after_while_starting(tmp_path, role):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path, role)))
    boot_state.begin("starting background workers")
    for method, path in (("get", "/api/playlists"), ("get", "/api/auth/me"), ("post", "/api/requests")):
        res = getattr(client, method)(path)
        assert res.status_code == 503, path
        assert res.headers["Retry-After"] == "5"
        body = res.json()
        assert body["status"] == "starting"
        assert body["step"] == ("starting" if role == "gateway" else "starting background workers")
    boot_state.mark_ready()
    assert client.get("/api/playlists").status_code != 503


def test_spa_shell_and_assets_load_while_starting(tmp_path):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path)))
    boot_state.begin("initializing")
    assert client.get("/").status_code == 200  # the loading screen is part of the SPA bundle


def test_health_payload_has_no_sensitive_fields(tmp_path):
    client = TestClient(create_app(db=Database(":memory:"), config=_config(tmp_path)))
    boot_state.begin("connecting to Plex")
    assert set(client.get("/api/health").json()) <= {"status", "step", "tier"}


# --------------------------------------------------------------------------- logging


def test_setup_logging_adds_stdout_handler_even_when_root_already_has_handlers(capsys):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        for h in list(root.handlers):
            root.removeHandler(h)
        # Reproduces the original bug: something attached a handler before setup (basicConfig then no-ops).
        root.addHandler(logging.NullHandler())
        setup_logging("INFO")
        logging.getLogger("plex_playlist_sync.boot").info("[boot] hello visible")
        assert "[boot] hello visible" in capsys.readouterr().out
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_boot_logs_redact_secrets(capsys):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        for h in list(root.handlers):
            root.removeHandler(h)
        setup_logging("INFO")
        logging.getLogger("plex_playlist_sync.boot").info("[boot] url http://x/api?X-Plex-Token=SUPERSECRET123")
        assert "SUPERSECRET123" not in capsys.readouterr().out
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def _web_env(tmp_path, **extra):
    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "PORT": "5250",
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
        "SECONDS_TO_WAIT": "0",
    }
    env.update(extra)
    return env


def _stub_workers():
    """Patches every background worker start so main() can run in-process."""
    return [
        patch("plex_playlist_sync.acquisition_worker.acquisition_worker.start"),
        patch("plex_playlist_sync.backlog_worker.backlog_worker.start"),
        patch("plex_playlist_sync.backlog_worker.rss_worker.start"),
        patch("plex_playlist_sync.artist_refresh_worker.artist_refresh_worker.start"),
        patch("plex_playlist_sync.scrobble_worker.scrobble_worker.start"),
        patch("plex_playlist_sync.mix_worker.mix_worker.start"),
        patch("plex_playlist_sync.cli._start_lidarr_trickle"),
    ]


def _run_main(env, *extra_patches):
    patches = _stub_workers() + list(extra_patches)
    for p in patches:
        p.start()
    try:
        with patch.dict(os.environ, env, clear=True):
            return main()
    finally:
        for p in reversed(patches):
            p.stop()


def test_main_emits_boot_banner_and_timed_steps(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        with patch("plex_playlist_sync.cli.uvicorn.Server") as server_cls, patch(
            "plex_playlist_sync.cli.PlexClient"
        ) as plex_cls:
            plex_cls.return_value.get_home_users.return_value = []
            server_cls.return_value = MagicMock()
            code = _run_main(_web_env(tmp_path))
    assert code == 0
    messages = [r.getMessage() for r in caplog.records]
    joined = "\n".join(messages)
    assert any(m.startswith("Initializing TrackSeerr v") and "role=all-in-one" in m for m in messages)
    assert "[boot] step: config load" in joined
    assert "[boot] step: database open + migrations done in" in joined
    assert "[boot] migrations: applied" in joined
    assert "[boot] step: binding web server on" in joined
    assert "[boot] step: starting background workers done in" in joined
    assert "[boot] step: connecting to Plex done in" in joined
    assert "[boot] ready after" in joined
    assert "[boot] complete after" in joined
    assert "token" not in joined.lower().replace("plex_token", "")  # no credential values echoed


def test_main_logs_schema_up_to_date_on_second_boot(tmp_path, caplog):
    Database(str(tmp_path / "sync_db.sqlite")).close()
    with caplog.at_level(logging.INFO):
        with patch("plex_playlist_sync.cli.uvicorn.Server"), patch("plex_playlist_sync.cli.PlexClient"):
            _run_main(_web_env(tmp_path))
    assert any("[boot] migrations: schema up to date" in r.getMessage() for r in caplog.records)


def test_slow_plex_probe_does_not_delay_readiness(tmp_path):
    """The server must be able to serve (ready) while Plex is still hanging on its connect timeout."""
    release_plex = threading.Event()
    ready_at: dict[str, float] = {}

    def slow_plex(*args, **kwargs):
        ready_at["plex_started"] = time.monotonic()
        release_plex.wait(timeout=10)
        raise ConnectionError("plex unreachable")

    def run_server():
        # Real uvicorn.Server.run is replaced by a stand-in that waits for readiness, like a live server would.
        deadline = time.monotonic() + 5
        while not boot_state.ready and time.monotonic() < deadline:
            time.sleep(0.01)
        ready_at["ready"] = time.monotonic()
        release_plex.set()

    with patch("plex_playlist_sync.cli.uvicorn.Server") as server_cls, patch(
        "plex_playlist_sync.cli.PlexClient", side_effect=slow_plex
    ):
        server_cls.return_value.run.side_effect = run_server
        t0 = time.monotonic()
        code = _run_main(_web_env(tmp_path))
    assert code == 0
    assert "ready" in ready_at, "readiness never flipped while Plex was still blocking"
    # Readiness happened before the (blocked) Plex probe was released.
    assert ready_at["ready"] - t0 < 5
    assert release_plex.is_set()


def test_background_init_sets_ready_before_plex_returns(tmp_path):
    config = _config(tmp_path)
    clients = cli._Clients()
    server = MagicMock()
    server.should_exit = False
    gate = threading.Event()
    seen_ready_during_probe: list[bool] = []

    def slow_connect(cfg, cl, *, fatal_plex):
        seen_ready_during_probe.append(boot_state.ready)
        gate.wait(timeout=5)
        return True

    boot_state.begin("starting")
    failure = {"code": 0}
    with patch("plex_playlist_sync.cli._connect_clients", side_effect=slow_connect), patch(
        "plex_playlist_sync.cli._start_lidarr_trickle"
    ), patch("plex_playlist_sync.cli._start_local_workers"), patch(
        "plex_playlist_sync.acquisition_worker.acquisition_worker.start"
    ):
        thread = threading.Thread(
            target=cli._background_init,
            kwargs=dict(
                role="all-in-one", db=Database(":memory:"), config=config, clients=clients, server=server,
                failure=failure,
            ),
        )
        thread.start()
        deadline = time.monotonic() + 3
        while not seen_ready_during_probe and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen_ready_during_probe == [True]
        assert boot_state.ready
        gate.set()
        thread.join(timeout=5)
    assert failure["code"] == 0


# --------------------------------------------------------------------------- gateway


def test_gateway_stays_starting_until_handshake_resolves(tmp_path):
    release = threading.Event()
    states: list[bool] = []

    def slow_handshake(client, **kwargs):
        if kwargs.get("deadline_seconds") == 0:  # pre-bind single probe: core not reachable yet
            return HandshakeResult(HANDSHAKE_UNREACHABLE)
        states.append(boot_state.ready)
        release.wait(timeout=5)
        return HandshakeResult(HANDSHAKE_OK, "1.0.0")

    def run_server():
        deadline = time.monotonic() + 3
        while not states and time.monotonic() < deadline:
            time.sleep(0.01)
        states.append(boot_state.ready)  # still waiting on the handshake
        release.set()

    env = {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": "g" * 40,
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
    }
    with patch("plex_playlist_sync.gateway_link.perform_handshake", side_effect=slow_handshake), patch(
        "plex_playlist_sync.gateway_link.GatewayLinkWorker.start"
    ), patch("plex_playlist_sync.cli.uvicorn.Server") as server_cls:
        server_cls.return_value.run.side_effect = run_server
        with patch.dict(os.environ, env, clear=True):
            code = main()
    assert code == 0
    assert states[0] is False and states[1] is False
    assert boot_state.ready  # flipped once the handshake resolved


def test_gateway_protocol_mismatch_refuses_before_binding(tmp_path):
    env = {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": "g" * 40,
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
    }
    with patch(
        "plex_playlist_sync.gateway_link.perform_handshake", side_effect=ProtocolMismatch("protocol differs")
    ), patch("plex_playlist_sync.gateway_link.GatewayLinkWorker.start") as link_start, patch(
        "plex_playlist_sync.cli.uvicorn.Server"
    ) as server_cls:
        with patch.dict(os.environ, env, clear=True):
            code = main()
    assert code == 1
    server_cls.return_value.run.assert_not_called()
    link_start.assert_not_called()


def test_gateway_protocol_mismatch_after_bind_stops_server_with_exit_1(tmp_path):
    """Core unreachable at the pre-bind probe, then answers with a different protocol: server told to exit."""
    def handshake(client, **kwargs):
        if kwargs.get("deadline_seconds") == 0:
            return HandshakeResult(HANDSHAKE_UNREACHABLE)
        raise ProtocolMismatch("protocol differs")

    env = {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": "g" * 40,
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
    }
    with patch("plex_playlist_sync.gateway_link.perform_handshake", side_effect=handshake), patch(
        "plex_playlist_sync.gateway_link.GatewayLinkWorker.start"
    ) as link_start, patch("plex_playlist_sync.cli.uvicorn.Server") as server_cls:
        with patch.dict(os.environ, env, clear=True):
            code = main()
    assert code == 1
    assert server_cls.return_value.should_exit is True
    link_start.assert_not_called()
