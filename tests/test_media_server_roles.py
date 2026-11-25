"""Tests for honouring media server ``ROLE_USERS`` and ``ROLE_SCROBBLES``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trackseerr import cli
from trackseerr.api.routes import scrobbles as scrobbles_routes
from trackseerr.config import Config
from trackseerr.storage import Database
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServer
from trackseerr.scrobble_worker import ScrobbleWorker


@pytest.fixture(autouse=True)
def _reset_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def _make_record(
    server_id: str,
    *,
    kind: str = "jellyfin",
    roles: frozenset[str] = frozenset(registry.ROLES),
    is_login: bool = False,
) -> registry.ServerRecord:
    return registry.ServerRecord(
        id=server_id,
        kind=kind,
        name=server_id,
        source="db",
        url="http://localhost:8096",
        api_key="secret",
        roles=roles,
        is_login=is_login,
    )


def _fake_adapter(kind: str = "jellyfin", users: list | None = None, error: Exception | None = None):
    adapter = MagicMock(spec=MediaServer)
    adapter.kind = kind
    adapter.capabilities = MagicMock(users=True)
    if error is not None:
        adapter.list_users.side_effect = error
    else:
        adapter.list_users.return_value = users or []
    return adapter


def test_user_import_runs_for_each_users_server(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    rec1 = _make_record("srv-1", roles=frozenset({registry.ROLE_USERS}))
    rec2 = _make_record("srv-2", roles=frozenset({registry.ROLE_USERS}))
    rec3 = _make_record("srv-3", roles=frozenset({registry.ROLE_REFRESH}))  # no ROLE_USERS

    adapters = {
        "srv-1": _fake_adapter("jellyfin", []),
        "srv-2": _fake_adapter("jellyfin", []),
        "srv-3": _fake_adapter("jellyfin", []),
    }
    monkeypatch.setattr(registry, "build_adapter", lambda rec: adapters[rec.id])
    registry.install([rec1, rec2, rec3])

    cli._discover_all_server_users(db, config)

    adapters["srv-1"].list_users.assert_called_once()
    adapters["srv-2"].list_users.assert_called_once()
    adapters["srv-3"].list_users.assert_not_called()
    db.close()


def test_one_server_failure_does_not_stop_user_import(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    rec1 = _make_record("srv-fail", roles=frozenset({registry.ROLE_USERS}))
    rec2 = _make_record("srv-ok", roles=frozenset({registry.ROLE_USERS}))

    adapters = {
        "srv-fail": _fake_adapter("jellyfin", error=RuntimeError("boom")),
        "srv-ok": _fake_adapter("jellyfin", []),
    }
    monkeypatch.setattr(registry, "build_adapter", lambda rec: adapters[rec.id])
    registry.install([rec1, rec2])

    cli._discover_all_server_users(db, config)

    adapters["srv-fail"].list_users.assert_called_once()
    adapters["srv-ok"].list_users.assert_called_once()
    db.close()


def test_history_poll_skipped_when_role_off(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    rec = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_USERS}), is_login=True)
    registry.install([rec])

    worker = ScrobbleWorker()
    worker.poll_history_once = MagicMock(return_value=[])
    plex_client = MagicMock()

    result = worker.run_iteration(db, config=config, plex_client=plex_client, now=datetime.now(timezone.utc), force=True)

    worker.poll_history_once.assert_not_called()
    assert result["ingested"] == 0
    db.close()


def test_history_poll_runs_when_role_on(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    rec = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=True)
    registry.install([rec])

    worker = ScrobbleWorker()
    worker.poll_history_once = MagicMock(return_value=[{"id": 1}])
    plex_client = MagicMock()

    result = worker.run_iteration(db, config=config, plex_client=plex_client, now=datetime.now(timezone.utc), force=True)

    worker.poll_history_once.assert_called_once()
    assert result["ingested"] == 1
    db.close()


def test_webhook_ignored_when_role_off(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    config = Config(plex_url="http://localhost:32400", plex_token="tok")
    secret = db.get_plex_webhook_secret()

    rec = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_USERS}), is_login=True)
    registry.install([rec])

    app = FastAPI()
    app.include_router(scrobbles_routes.router, prefix="/api/scrobbles")
    app.dependency_overrides[scrobbles_routes.get_db] = lambda: db
    app.dependency_overrides[scrobbles_routes.get_config] = lambda: config

    client = TestClient(app)
    payload = {
        "event": "media.scrobble",
        "Metadata": {"type": "track", "grandparentTitle": "Artist", "title": "Track"},
    }
    resp = client.post(
        f"/api/scrobbles/plex?token={secret}",
        content=json.dumps(payload),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    db.close()
