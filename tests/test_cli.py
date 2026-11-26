import os
from unittest.mock import MagicMock, patch

from trackseerr.cli import main
from trackseerr.gateway_link import HANDSHAKE_OK, HandshakeResult


_ENV_PLEX_ROW = {
    "id": "env-plex",
    "kind": "plex",
    "name": "Plex",
    "source": "env",
    "url": "",
    "username": "",
    "password": "",
    "api_key": "",
    "music_section": "",
    "verify_ssl": True,
    "enabled": True,
    "is_login": True,
    "use_refresh": True,
    "use_playlists": True,
    "use_users": True,
    "use_scrobbles": True,
}


def test_cli_partial_plex_without_token_fails():
    """No PLEX_* is fine; a partial PLEX_URL without PLEX_TOKEN is a startup error."""
    with patch.dict(os.environ, {"PLEX_URL": "http://localhost:32400"}, clear=True):
        code = main()
        assert code == 1


@patch("trackseerr.clients.plex.PlexClient")
@patch("trackseerr.cli.SyncCoordinator")
def test_cli_run_once_success(mock_coord_class, mock_plex_class):
    mock_coord = MagicMock()
    mock_coord_class.return_value = mock_coord

    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "RUN_ONCE": "1",
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 0
        mock_coord.run_sync_cycle.assert_called_once()


@patch("trackseerr.clients.plex.PlexClient")
@patch("trackseerr.cli.SyncCoordinator")
def test_cli_headless_mode(mock_coord_class, mock_plex_class):
    mock_coord = MagicMock()
    mock_coord_class.return_value = mock_coord

    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "HEADLESS": "1",
        "SECONDS_TO_WAIT": "1",
    }
    # Simulate a shutdown request after first cycle
    with patch.dict(os.environ, env, clear=True):
        with patch("trackseerr.cli._shutdown_requested", True):
            code = main()
            assert code == 0


@patch("trackseerr.cli.uvicorn.Server")
@patch("trackseerr.cli.Database")
@patch("trackseerr.clients.plex.PlexClient")
def test_cli_web_mode_default(mock_plex_class, mock_db_class, mock_server_class, tmp_path):
    mock_plex = MagicMock()
    mock_plex.get_home_users.return_value = [
        {"id": "user-1", "name": "Ron", "email": "ron@test.local", "admin": True}
    ]
    mock_plex_class.return_value = mock_plex

    mock_db = MagicMock()
    mock_db.is_tombstoned.return_value = False  # discovery skips admin-deleted (tombstoned) users
    mock_db.get_user.return_value = None
    mock_db.list_media_server_rows.return_value = [_ENV_PLEX_ROW]
    mock_db_class.return_value = mock_db

    mock_server = MagicMock()
    mock_server_class.return_value = mock_server

    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "PORT": "5250",
        "DATA_DIR": str(tmp_path),
        "SECONDS_TO_WAIT": "0",  # Disable background thread for unit test
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 0
        mock_plex.get_home_users.assert_called_once()
        mock_db.import_media_server_user.assert_called_once_with(
            "user-1", "Ron", "ron@test.local", auth_type="plex", grant_admin=True
        )
        mock_server.run.assert_called_once()


@patch("trackseerr.cli.uvicorn.Server")
@patch("trackseerr.cli.Database")
@patch("trackseerr.clients.plex.PlexClient")
def test_cli_web_mode_custom_port(mock_plex_class, mock_db_class, mock_server_class, tmp_path):
    mock_plex = MagicMock()
    mock_plex.get_home_users.return_value = []
    mock_plex_class.return_value = mock_plex
    mock_db_class.return_value.list_media_server_rows.return_value = [_ENV_PLEX_ROW]

    mock_server = MagicMock()
    mock_server_class.return_value = mock_server

    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "PORT": "8080",
        "HOST": "127.0.0.1",
        "DATA_DIR": str(tmp_path),
        "SECONDS_TO_WAIT": "0",
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 0
        mock_server.run.assert_called_once()


@patch("trackseerr.cli.Database")
@patch("trackseerr.clients.plex.PlexClient")
def test_cli_web_mode_database_permission_error(mock_plex_class, mock_db_class, tmp_path):
    mock_db_class.side_effect = PermissionError("Permission denied: /data/sync_db.sqlite")

    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "token",
        "DATA_DIR": str(tmp_path),
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 1


@patch("trackseerr.gateway_link.GatewayLinkWorker.start")
@patch(
    "trackseerr.gateway_link.perform_handshake",
    return_value=HandshakeResult(HANDSHAKE_OK, "1.0.0"),
)
@patch("trackseerr.cli.uvicorn.Server")
@patch("trackseerr.cli.SyncCoordinator")
@patch("trackseerr.clients.plex.PlexClient")
def test_cli_gateway_role_starts_without_plex_vars(
    mock_plex_class, mock_coord_class, mock_server_class, _hs, _worker, tmp_path
):
    mock_server = MagicMock()
    mock_server_class.return_value = mock_server

    env = {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": "g" * 40,
        "PORT": "5250",
        "DATA_DIR": str(tmp_path),
        "SECONDS_TO_WAIT": "0",
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 0
        mock_plex_class.assert_not_called()
        mock_coord_class.assert_not_called()
        mock_server.run.assert_called_once()


@patch("trackseerr.gateway_link.GatewayLinkWorker.start")
@patch(
    "trackseerr.gateway_link.perform_handshake",
    return_value=HandshakeResult(HANDSHAKE_OK, "1.0.0"),
)
@patch("trackseerr.cli.uvicorn.Server")
@patch("trackseerr.cli.Database")
@patch("trackseerr.cli.SyncCoordinator")
@patch("trackseerr.clients.plex.PlexClient")
def test_cli_gateway_role_fallback_to_ephemeral_db(
    mock_plex_class, mock_coord_class, mock_db_class, mock_server_class, _hs, _worker
):
    mock_server = MagicMock()
    mock_server_class.return_value = mock_server

    db_paths_called = []

    def mock_db_init(path):
        db_paths_called.append(path)
        if "/data" in str(path):
            raise PermissionError("Permission denied: /data/sync_db.sqlite")
        return MagicMock()

    mock_db_class.side_effect = mock_db_init

    env = {
        "ROLE": "gateway",
        "APPLICATION_URL": "https://requests.example.com",
        "TRACKSEERR_CORE_URL": "http://core.internal:5251",
        "INTERNAL_CORE_SECRET": "g" * 40,
        "PORT": "5250",
        "DATA_DIR": "/data",
        "SECONDS_TO_WAIT": "0",
    }
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 0
        mock_plex_class.assert_not_called()
        mock_coord_class.assert_not_called()
        mock_server.run.assert_called_once()
        assert len(db_paths_called) == 2
        assert "/data/sync_db.sqlite" in db_paths_called[0]
        assert db_paths_called[1] == "/tmp/trackseerr_gateway.sqlite"
        assert os.environ.get("DATABASE_PATH") == "/tmp/trackseerr_gateway.sqlite"


def test_cli_core_role_requires_internal_secret():
    env = {"ROLE": "core"}
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 1


def test_cli_all_in_one_role_with_partial_plex_requires_both_vars():
    env = {"ROLE": "all-in-one", "PLEX_URL": "http://localhost:32400"}
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 1


def test_db_base_dir_uses_state_dir(tmp_path, monkeypatch):
    from trackseerr.cli import _db_base_dir
    from trackseerr.config import Config

    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    monkeypatch.setenv("CONFIG_DIR", cfg.config_dir)
    assert _db_base_dir(cfg) == cfg.config_dir

    monkeypatch.delenv("CONFIG_DIR", raising=False)
    monkeypatch.setattr("os.path.isdir", lambda p: False if p == "/config" else os.path.isdir(p))
    assert _db_base_dir(cfg) == cfg.data_dir


