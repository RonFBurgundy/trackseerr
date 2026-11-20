"""Regression tests for Plex server token safety and PlexAPI client identity."""

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import plexapi
import pytest
import requests

from trackseerr.clients.plex import PlexClient, PlexCloudCallsBlocked, _UNCONFIRMED_TOKEN_WARNING
from trackseerr.media_servers.plex import PlexMediaServer


def _make_client(token: str = "tok-123", query_el=None) -> tuple[PlexClient, MagicMock]:
    mock_server = MagicMock()
    mock_server.friendlyName = "MyPlexServer"
    mock_server.version = "1.40.0"
    if query_el is not None:
        mock_server.query.return_value = query_el
    with patch("trackseerr.clients.plex.PlexServer", return_value=mock_server):
        client = PlexClient("http://localhost:32400", token)
    return client, mock_server


def test_admin_username_comes_from_local_server():
    el = ET.Element("MyPlex", {"username": "local_admin", "authToken": "server-tok"})
    client, mock_server = _make_client(token="server-tok", query_el=el)

    assert client._get_admin_username() == "local_admin"
    mock_server.query.assert_called_once_with("/myplex/account")
    mock_server.myPlexAccount.assert_not_called()


def test_server_token_detected():
    el_same = ET.Element("MyPlex", {"username": "admin", "authToken": "server-tok"})
    client_same, _ = _make_client(token="server-tok", query_el=el_same)
    assert client_same.token_kind == "server"
    assert client_same.uses_server_token is True
    assert client_same.cloud_calls_allowed is False

    el_diff = ET.Element("MyPlex", {"username": "admin", "authToken": "other-tok"})
    client_diff, _ = _make_client(token="user-tok", query_el=el_diff)
    assert client_diff.token_kind == "account"
    assert client_diff.uses_server_token is False
    assert client_diff.cloud_calls_allowed is True

    el_missing = ET.Element("MyPlex", {"username": "admin"})
    client_missing, _ = _make_client(token="user-tok", query_el=el_missing)
    assert client_missing.token_kind == "unknown"
    assert client_missing.uses_server_token is False
    assert client_missing.cloud_calls_allowed is False


def test_home_users_with_server_token_never_calls_plex_tv(caplog):
    el = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    client, mock_server = _make_client(token="server-tok", query_el=el)
    mock_server.systemAccounts.return_value = [
        SimpleNamespace(id=0, name="System"),
        SimpleNamespace(id=1, name="admin_user"),
        SimpleNamespace(id=2, name="alice"),
        SimpleNamespace(id=3, name=""),
        SimpleNamespace(id=4, name="bob"),
    ]

    with caplog.at_level("WARNING"):
        first = client.get_home_users()
        second = client.get_home_users()

    assert first == second
    assert [u["username"] for u in first] == ["admin_user", "alice", "bob"]
    assert first[0]["id"] == "admin"
    assert first[0]["is_admin"] is True
    assert first[1]["is_admin"] is False
    assert first[2]["is_admin"] is False
    mock_server.myPlexAccount.assert_not_called()

    token_warnings = [r for r in caplog.records if "own token" in r.getMessage()]
    assert len(token_warnings) == 1


def test_home_users_with_own_token_uses_account():
    el = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    client, mock_server = _make_client(token="separate-user-tok", query_el=el)
    managed = SimpleNamespace(username="alice", id="u-2", email="", thumb="")
    mock_account = SimpleNamespace(
        username="admin_user",
        id="u-1",
        email="admin@example.com",
        thumb="",
        users=lambda: [managed],
    )
    mock_server.myPlexAccount.return_value = mock_account

    users = client.get_home_users()

    mock_server.myPlexAccount.assert_called_once()
    assert [u["username"] for u in users] == ["admin_user", "alice"]


def test_plexapi_identity_set(monkeypatch):
    import plexapi.myplex
    import plexapi.server

    monkeypatch.setattr("trackseerr.clients.plex._PLEXAPI_IDENTITY_SET", False)
    el = ET.Element("MyPlex", {"username": "admin"})
    _make_client(token="tok", query_el=el)

    assert plexapi.BASE_HEADERS["X-Plex-Product"] == "TrackSeerr"
    assert plexapi.BASE_HEADERS["X-Plex-Device-Name"] == "TrackSeerr"
    assert plexapi.server.BASE_HEADERS is plexapi.BASE_HEADERS
    assert plexapi.server.BASE_HEADERS["X-Plex-Product"] == "TrackSeerr"
    assert plexapi.server.BASE_HEADERS["X-Plex-Device-Name"] == "TrackSeerr"
    assert plexapi.myplex.BASE_HEADERS["X-Plex-Product"] == "TrackSeerr"
    assert plexapi.myplex.BASE_HEADERS["X-Plex-Device-Name"] == "TrackSeerr"


def test_test_connection_reports_server_token_warning():
    el = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    client, _ = _make_client(token="server-tok", query_el=el)
    adapter = PlexMediaServer(client)

    result = adapter.test_connection()

    assert result.ok is True
    assert "own token" in result.message


def test_local_account_ignores_non_string_attributes():
    client, mock_server = _make_client(token="tok")

    assert client._local_account() == {"username": "", "auth_token": ""}
    assert client.token_kind == "unknown"
    assert client._get_admin_username() == ""
    mock_server.myPlexAccount.assert_not_called()


def test_missing_auth_token_never_calls_plex_tv():
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, mock_server = _make_client(token="tok-123", query_el=el)
    mock_server.systemAccounts.return_value = [
        SimpleNamespace(id=1, name="admin_user"),
        SimpleNamespace(id=2, name="alice"),
    ]

    users = client.get_home_users()
    assert [u["username"] for u in users] == ["admin_user", "alice"]
    assert client._get_admin_username() == "admin_user"
    mock_server.myPlexAccount.assert_not_called()


def test_local_account_query_failure_is_unknown():
    client, mock_server = _make_client(token="tok-123")
    mock_server.query.side_effect = requests.RequestException("boom")

    assert client.token_kind == "unknown"
    assert client.cloud_calls_allowed is False
    assert client._get_admin_username() == ""
    mock_server.myPlexAccount.assert_not_called()


def test_unconfirmed_token_status_warning():
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, _ = _make_client(token="tok-123", query_el=el)
    adapter = PlexMediaServer(client)

    result = adapter.test_connection()

    assert result.ok is True
    assert _UNCONFIRMED_TOKEN_WARNING in result.message


def test_confirmed_env_allows_cloud_calls(monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN_CONFIRMED", "1")
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, mock_server = _make_client(token="tok-123", query_el=el)
    managed = SimpleNamespace(username="alice", id="u-2", email="", thumb="")
    mock_server.myPlexAccount.return_value = SimpleNamespace(
        username="admin_user",
        id="u-1",
        email="admin@example.com",
        thumb="",
        users=lambda: [managed],
    )

    assert client.token_kind == "account"
    assert client.cloud_calls_allowed is True
    users = client.get_home_users()
    mock_server.myPlexAccount.assert_called_once()
    assert [u["username"] for u in users] == ["admin_user", "alice"]


def test_confirmed_env_does_not_override_server_match(monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN_CONFIRMED", "1")
    el = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    client, mock_server = _make_client(token="server-tok", query_el=el)
    mock_server.systemAccounts.return_value = [SimpleNamespace(id=1, name="admin_user")]

    assert client.token_kind == "server"
    assert client.cloud_calls_allowed is False
    client.get_home_users()
    mock_server.myPlexAccount.assert_not_called()


def test_different_auth_token_is_account():
    el = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    client, _ = _make_client(token="user-tok", query_el=el)

    assert client.token_kind == "account"
    assert client.token_warning() is None


def test_get_user_server_blocked_when_unknown():
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, mock_server = _make_client(token="tok-123", query_el=el)

    assert client.get_user_server("admin_user") is mock_server
    with pytest.raises(PlexCloudCallsBlocked):
        client.get_user_server("alice")
    mock_server.switchUser.assert_not_called()
