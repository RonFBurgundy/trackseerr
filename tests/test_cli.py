import os
from unittest.mock import MagicMock, patch

from plex_playlist_sync.cli import main


def test_cli_missing_plex_vars():
    with patch.dict(os.environ, {}, clear=True):
        code = main()
        assert code == 1


@patch("plex_playlist_sync.cli.PlexClient")
@patch("plex_playlist_sync.cli.SyncCoordinator")
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


@patch("plex_playlist_sync.cli.PlexClient")
@patch("plex_playlist_sync.cli.SyncCoordinator")
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
        with patch("plex_playlist_sync.cli._shutdown_requested", True):
            code = main()
            assert code == 0


@patch("plex_playlist_sync.cli.uvicorn.Server")
@patch("plex_playlist_sync.cli.Database")
@patch("plex_playlist_sync.cli.PlexClient")
def test_cli_web_mode_default(mock_plex_class, mock_db_class, mock_server_class, tmp_path):
    mock_plex = MagicMock()
    mock_plex.get_home_users.return_value = [
        {"id": "user-1", "name": "Ron", "email": "ron@test.local", "admin": True}
    ]
    mock_plex_class.return_value = mock_plex

    mock_db = MagicMock()
    mock_db.is_tombstoned.return_value = False  # discovery skips admin-deleted (tombstoned) users
    mock_db.get_user.return_value = None
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
        mock_db.upsert_user.assert_called_once_with(
            user_id="user-1", username="Ron", email="ron@test.local", is_admin=True
        )
        mock_server.run.assert_called_once()


@patch("plex_playlist_sync.cli.uvicorn.Server")
@patch("plex_playlist_sync.cli.Database")
@patch("plex_playlist_sync.cli.PlexClient")
def test_cli_web_mode_custom_port(mock_plex_class, mock_db_class, mock_server_class, tmp_path):
    mock_plex = MagicMock()
    mock_plex.get_home_users.return_value = []
    mock_plex_class.return_value = mock_plex

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


@patch("plex_playlist_sync.cli.Database")
@patch("plex_playlist_sync.cli.PlexClient")
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


@patch("plex_playlist_sync.cli.uvicorn.Server")
@patch("plex_playlist_sync.cli.SyncCoordinator")
@patch("plex_playlist_sync.cli.PlexClient")
def test_cli_gateway_role_starts_without_plex_vars(
    mock_plex_class, mock_coord_class, mock_server_class, tmp_path
):
    mock_server = MagicMock()
    mock_server_class.return_value = mock_server

    env = {
        "ROLE": "gateway",
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


@patch("plex_playlist_sync.cli.uvicorn.Server")
@patch("plex_playlist_sync.cli.Database")
@patch("plex_playlist_sync.cli.SyncCoordinator")
@patch("plex_playlist_sync.cli.PlexClient")
def test_cli_gateway_role_fallback_to_ephemeral_db(
    mock_plex_class, mock_coord_class, mock_db_class, mock_server_class
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


def test_cli_core_role_requires_plex_vars():
    env = {"ROLE": "core"}
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 1


def test_cli_all_in_one_role_requires_plex_vars():
    env = {"ROLE": "all-in-one"}
    with patch.dict(os.environ, env, clear=True):
        code = main()
        assert code == 1


