"""Regression tests for multi-server user refresh, rename safety, headless settings, and SyncCoordinator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trackseerr import cli
from trackseerr.api.dependencies import get_config, get_db, get_media_client, require_admin, require_core_tier, require_media_server, track_admin_actor
from trackseerr.api.routes.library.tagging import rename_safety
from trackseerr.api.routes.users import router as users_router
from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServerError
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.models import Playlist, SyncResult, Track
from trackseerr.storage import Database
from trackseerr.sync import SyncCoordinator


def _make_record(server_id: str, kind: str, name: str, roles: frozenset[str] = frozenset(registry.ROLES)) -> ServerRecord:
    return ServerRecord(
        id=server_id,
        kind=kind,
        name=name,
        source="settings",
        url=f"http://{server_id}.local",
        roles=roles,
    )


def test_rename_safety_merges_warnings_and_survives_error(monkeypatch: pytest.MonkeyPatch) -> None:
    rec_plex = _make_record("s-plex", "plex", "Plex")
    rec_jf = _make_record("s-jf", "jellyfin", "Jellyfin")
    rec_broken = _make_record("s-sub", "subsonic", "Navidrome")

    plex_adapter = MagicMock()
    plex_adapter.rename_warnings.return_value = ["Empty trash automatically is enabled"]
    jf_adapter = MagicMock()
    jf_adapter.rename_warnings.return_value = ["Real-time monitoring disabled"]
    broken_adapter = MagicMock()
    broken_adapter.rename_warnings.side_effect = MediaServerError("timeout")

    monkeypatch.setattr(registry, "is_installed", lambda: True)
    monkeypatch.setattr(
        registry,
        "media_servers",
        lambda role=None, config=None: [(rec_plex, plex_adapter), (rec_jf, jf_adapter), (rec_broken, broken_adapter)],
    )

    res = rename_safety(plex_client=None, _admin={"id": "admin", "is_admin": True})
    assert res["warnings"] == [
        "Plex: Empty trash automatically is enabled",
        "Jellyfin: Real-time monitoring disabled",
    ]


def test_refresh_users_imports_multiple_servers_and_survives_one_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(str(tmp_path / "test.sqlite"))
    try:
        app = FastAPI()
        app.include_router(users_router, prefix="/api/users")
        app.dependency_overrides[require_media_server] = lambda: None
        app.dependency_overrides[require_admin] = lambda: {"id": "admin", "is_admin": True}
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_media_client] = lambda: None

        rec_plex = _make_record("s-plex", "plex", "Plex")
        rec_jf = _make_record("s-jf", "jellyfin", "Jellyfin")

        plex_adapter = MagicMock()
        plex_adapter.kind = "plex"
        plex_adapter.capabilities = SimpleNamespace(users=True)
        plex_adapter.list_users.return_value = [
            SimpleNamespace(id="p1", username="alice", name="alice", extra={}, is_admin=False),
        ]

        jf_adapter = MagicMock()
        jf_adapter.kind = "jellyfin"
        jf_adapter.capabilities = SimpleNamespace(users=True)
        jf_adapter.list_users.side_effect = RuntimeError("jellyfin down")

        monkeypatch.setattr(registry, "is_installed", lambda: True)
        monkeypatch.setattr(
            registry,
            "media_servers",
            lambda role=None, config=None: [(rec_plex, plex_adapter), (rec_jf, jf_adapter)],
        )

        client = TestClient(app)
        res = client.post("/api/users/refresh")
        assert res.status_code == 200
        usernames = {u["username"] for u in res.json()}
        assert "alice" in usernames

        # When every server fails, 502 is returned with generic message (not "Plex").
        plex_adapter.list_users.side_effect = RuntimeError("plex down")
        res_fail = client.post("/api/users/refresh")
        assert res_fail.status_code == 502
        assert "Failed to query media server users:" in res_fail.json()["detail"]
    finally:
        db.close()


def test_apply_saved_media_server_settings_loads_saved_row_with_env_plex(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "sync_db.sqlite"
    db = Database(str(db_path))
    db.close()

    config = Config(
        plex_url="http://plex.local:32400",
        plex_token="env-token",
        data_dir=str(tmp_path),
    )

    loaded_records: list[ServerRecord] = []
    monkeypatch.setattr(registry, "reload", lambda cfg, database: loaded_records.extend(registry.env_records(cfg)))

    with patch("trackseerr.cli.media_server_settings.load_into_process") as mock_load:
        cli._apply_saved_media_server_settings(config)
        mock_load.assert_called_once()
    assert len(loaded_records) == 1
    assert loaded_records[0].kind == "plex"


def test_sync_coordinator_pushes_to_all_playlist_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(plex_url="", plex_token="")
    pl = Playlist(id="pl-1", name="Test Mix", tracks=[Track(title="Song", artist="Artist", album="Album")])

    rec_plex = _make_record("s-plex", "plex", "Plex")
    rec_sub = _make_record("s-sub", "subsonic", "Navidrome")

    plex_res = SyncResult("Test Mix", 1, 1, 0, True)
    sub_res = SyncResult("Test Mix", 1, 1, 0, True)

    plex_adapter = MagicMock()
    plex_adapter.sync_playlist.return_value = [plex_res]
    sub_adapter = MagicMock()

    monkeypatch.setattr(registry, "is_installed", lambda: True)
    monkeypatch.setattr(
        registry,
        "media_servers",
        lambda role=None, config=None: [(rec_plex, plex_adapter), (rec_sub, sub_adapter)],
    )

    with patch("trackseerr.sync.sync_subsonic_for_users", return_value=[sub_res]) as mock_sub_sync:
        coord = SyncCoordinator(config=config, plex_client=None)
        result = coord._sync_one(pl)

    assert result == plex_res
    plex_adapter.sync_playlist.assert_called_once()
    mock_sub_sync.assert_called_once()
