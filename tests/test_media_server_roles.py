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


def test_history_poll_runs_when_role_on(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    rec = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=True)
    plex_client = MagicMock()
    adapter = MagicMock()
    adapter.plex_extras.return_value = plex_client
    monkeypatch.setattr(registry, "build_adapter", lambda _rec: adapter)
    registry.install([rec])

    worker = ScrobbleWorker()
    worker.poll_history_once = MagicMock(return_value=[{"id": 1}])

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


def _fake_plex_client(entries=None, accounts=None, admin_username="admin", machine_id="machine-1", error=None):
    from types import SimpleNamespace

    server = MagicMock()
    server.systemAccounts.return_value = [SimpleNamespace(id=k, name=v) for k, v in (accounts or {}).items()]
    if error is not None:
        server.history.side_effect = error
    else:
        server.history.return_value = entries or []
    client = MagicMock()
    client.server = server
    client._get_admin_username.return_value = admin_username
    client.machine_identifier = machine_id
    return client


def test_history_polled_for_each_plex_with_switch(monkeypatch, tmp_path):
    from types import SimpleNamespace

    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    db.upsert_user("u-alice", "alice", is_admin=False)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    rec1 = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=True)
    rec2 = _make_record("plex-two", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=False)
    rec3 = _make_record("plex-off", kind="plex", roles=frozenset({registry.ROLE_USERS}), is_login=False)

    e1 = SimpleNamespace(
        type="track", viewedAt=now, originalTitle="Artist 1", grandparentTitle="Artist 1",
        title="Song 1", parentTitle="Album 1", ratingKey="11", duration=180000, accountID=10,
    )
    e2 = SimpleNamespace(
        type="track", viewedAt=now, originalTitle="Artist 2", grandparentTitle="Artist 2",
        title="Song 2", parentTitle="Album 2", ratingKey="22", duration=180000, accountID=20,
    )

    c1 = _fake_plex_client([e1], {10: "alice"})
    c2 = _fake_plex_client([e2], {20: "alice"})
    c3 = _fake_plex_client([], {})
    adapters = {}
    for rid, client in (("plex-login", c1), ("plex-two", c2), ("plex-off", c3)):
        a = MagicMock()
        a.plex_extras.return_value = client
        adapters[rid] = a

    monkeypatch.setattr(registry, "build_adapter", lambda rec: adapters[rec.id])
    registry.install([rec1, rec2, rec3])

    worker = ScrobbleWorker()
    result = worker.run_iteration(db, config=config, now=now, force=True)

    assert result["ingested"] == 2
    c1.server.history.assert_called_once()
    c2.server.history.assert_called_once()
    c3.server.history.assert_not_called()
    assert db.get_scrobble_state("plex_history_last_poll") == now.isoformat(timespec="seconds")
    assert db.get_scrobble_state("plex_history:plex-two") == now.isoformat(timespec="seconds")
    assert db.get_scrobble_state("plex_history:plex-off") is None
    db.close()


def test_plex_poll_failure_isolated(monkeypatch, tmp_path):
    from plexapi.exceptions import BadRequest
    from types import SimpleNamespace

    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    db.upsert_user("u-alice", "alice", is_admin=False)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    rec1 = _make_record("plex-fail", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=True)
    rec2 = _make_record("plex-ok", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=False)

    e2 = SimpleNamespace(
        type="track", viewedAt=now, originalTitle="Artist 2", grandparentTitle="Artist 2",
        title="Song 2", parentTitle="Album 2", ratingKey="22", duration=180000, accountID=20,
    )
    c1 = _fake_plex_client(error=BadRequest("boom"))
    c2 = _fake_plex_client([e2], {20: "alice"})
    adapters = {}
    for rid, client in (("plex-fail", c1), ("plex-ok", c2)):
        a = MagicMock()
        a.plex_extras.return_value = client
        adapters[rid] = a

    monkeypatch.setattr(registry, "build_adapter", lambda rec: adapters[rec.id])
    registry.install([rec1, rec2])

    worker = ScrobbleWorker()
    result = worker.run_iteration(db, config=config, now=now, force=True)

    assert result["ingested"] == 1
    assert worker.errors == 1
    c2.server.history.assert_called_once()
    db.close()


def test_non_login_plex_owner_not_mapped_to_admin(monkeypatch, tmp_path):
    from types import SimpleNamespace

    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    db.upsert_user("u-admin", "trackseerr_admin", is_admin=True)
    db.upsert_user("u-bob", "bob", is_admin=False)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("plex-other", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=False)

    owner_play = SimpleNamespace(
        type="track", viewedAt=now, originalTitle="Artist 1", grandparentTitle="Artist 1",
        title="Owner Song", parentTitle="Album 1", ratingKey="11", duration=180000, accountID=1,
    )
    bob_play = SimpleNamespace(
        type="track", viewedAt=now, originalTitle="Artist 2", grandparentTitle="Artist 2",
        title="Bob Song", parentTitle="Album 2", ratingKey="22", duration=180000, accountID=99,
    )
    client = _fake_plex_client([owner_play, bob_play], {1: "other_owner", 99: "bob"}, admin_username="other_owner")
    adapter = MagicMock()
    adapter.plex_extras.return_value = client

    monkeypatch.setattr(registry, "build_adapter", lambda _rec: adapter)
    registry.install([rec])

    worker = ScrobbleWorker()
    result = worker.run_iteration(db, config=config, now=now, force=True)

    assert result["ingested"] == 1
    assert db.list_listens("u-admin") == []
    assert len(db.list_listens("u-bob")) == 1
    client._get_admin_username.assert_not_called()
    db.close()


def test_webhook_accepted_when_any_plex_has_switch(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.upsert_user("u-alice", "alice", is_admin=False)
    config = Config(plex_url="http://localhost:32400", plex_token="tok")
    secret = db.get_plex_webhook_secret()

    rec_login = _make_record("plex-login", kind="plex", roles=frozenset({registry.ROLE_USERS}), is_login=True)
    rec_second = _make_record("plex-two", kind="plex", roles=frozenset({registry.ROLE_SCROBBLES}), is_login=False)
    registry.install([rec_login, rec_second])

    login_adapter = MagicMock()
    login_adapter.plex_extras.return_value = _fake_plex_client(machine_id="uuid-login")
    second_adapter = MagicMock()
    second_adapter.plex_extras.return_value = _fake_plex_client(machine_id="uuid-second")
    cached = {"plex-login": login_adapter, "plex-two": second_adapter}
    monkeypatch.setattr(registry, "cached_adapter", lambda rec: cached.get(rec.id))

    app = FastAPI()
    app.include_router(scrobbles_routes.router, prefix="/api/scrobbles")
    app.dependency_overrides[scrobbles_routes.get_db] = lambda: db
    app.dependency_overrides[scrobbles_routes.get_config] = lambda: config
    client = TestClient(app)

    # Accepted when second Plex has ROLE_SCROBBLES on (and matches its uuid)
    payload_ok = {
        "event": "media.scrobble",
        "Server": {"uuid": "uuid-second"},
        "Account": {"id": 99, "title": "alice"},
        "Metadata": {"type": "track", "grandparentTitle": "Artist", "title": "Track"},
    }
    resp = client.post(
        f"/api/scrobbles/plex?token={secret}",
        content=json.dumps(payload_ok),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    # Ignored when Server.uuid matches a cached Plex adapter whose ROLE_SCROBBLES is off
    payload_off = {
        "event": "media.scrobble",
        "Server": {"uuid": "uuid-login"},
        "Account": {"id": 99, "title": "alice"},
        "Metadata": {"type": "track", "grandparentTitle": "Artist", "title": "Track 2"},
    }
    resp_off = client.post(
        f"/api/scrobbles/plex?token={secret}",
        content=json.dumps(payload_off),
        headers={"content-type": "application/json"},
    )
    assert resp_off.status_code == 200
    assert resp_off.json() == {"status": "ignored"}
    db.close()
