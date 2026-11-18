"""Tests for admin 'Sign in with Plex' owner account token storage, plex.tv usage, and routes."""

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import PlexAuthError, create_session_token, get_or_create_secret_key
from trackseerr.clients.plex import PlexAccountNotOwner, PlexClient
from trackseerr.config import Config, get_plex_account_token, set_plex_account_token
from trackseerr.media_servers import settings as media_server_settings
from trackseerr.role_guard import core_like_reasons
from trackseerr.storage import Database


def _make_client(
    token: str = "local-tok",
    query_el=None,
    machine_id: str = "machine-123",
    account_token: str | None = None,
) -> tuple[PlexClient, MagicMock]:
    mock_server = MagicMock()
    mock_server.friendlyName = "MyPlexServer"
    mock_server.version = "1.40.0"
    mock_server.machineIdentifier = machine_id
    if query_el is not None:
        mock_server.query.return_value = query_el
    with patch("trackseerr.clients.plex.PlexServer", return_value=mock_server):
        client = PlexClient("http://localhost:32400", token, account_token=account_token)
    return client, mock_server


def _auth_headers(db: Database, cfg: Config, user: dict) -> dict[str, str]:
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="http://localhost:32400", plex_token="local-tok", data_dir=str(tmp_path))
    plex_client, mock_server = _make_client(token="local-tok", machine_id="machine-123")
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: plex_client
    admin = db.upsert_user("a1", "admin", "a@x", is_admin=True)
    alice = db.upsert_user("u1", "alice", "u@x", is_admin=False)
    yield SimpleNamespace(
        tc=TestClient(app),
        db=db,
        cfg=cfg,
        plex=plex_client,
        mock_server=mock_server,
        admin=_auth_headers(db, cfg, admin),
        alice=_auth_headers(db, cfg, alice),
        app=app,
    )


def test_account_token_allows_cloud_calls_even_when_kind_unknown() -> None:
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, _ = _make_client(token="unconfirmed-tok", query_el=el)
    assert client.token_kind == "unknown"
    assert client.cloud_calls_allowed is False
    assert client.token_warning() is not None

    set_plex_account_token("acct-tok-123")
    assert client.cloud_calls_allowed is True
    assert client.token_warning() is None

    # When PLEX_TOKEN is the server's own token, still warn while allowing cloud calls via account token
    el_server = ET.Element("MyPlex", {"username": "admin_user", "authToken": "server-tok"})
    server_client, _ = _make_client(token="server-tok", query_el=el_server, account_token="acct-tok-123")
    assert server_client.token_kind == "server"
    assert server_client.cloud_calls_allowed is True
    warning = server_client.token_warning()
    assert warning is not None and "own token" in warning


def test_home_users_use_account_token_not_server_token() -> None:
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, mock_server = _make_client(token="unconfirmed-tok", query_el=el, account_token="acct-tok-123")
    managed = SimpleNamespace(username="alice", id="u-2", email="", thumb="")
    mock_account = SimpleNamespace(
        username="admin_user",
        id="u-1",
        email="admin@example.com",
        thumb="",
        users=lambda: [managed],
    )

    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=mock_account) as account_cls:
        users = client.get_home_users()

    account_cls.assert_called_with(token="acct-tok-123", session=client._session, timeout=client.timeout)
    mock_server.myPlexAccount.assert_not_called()
    assert [u["username"] for u in users] == ["admin_user", "alice"]


def test_get_user_server_uses_account_user_token() -> None:
    el = ET.Element("MyPlex", {"username": "admin_user"})
    client, mock_server = _make_client(
        token="unconfirmed-tok",
        query_el=el,
        machine_id="machine-xyz",
        account_token="acct-tok-123",
    )
    mock_user = MagicMock()
    mock_user.get_token.return_value = "alice-server-tok"
    mock_account = MagicMock()
    mock_account.user.return_value = mock_user
    user_server_instance = MagicMock()

    with (
        patch("trackseerr.clients.plex.MyPlexAccount", return_value=mock_account) as account_cls,
        patch("trackseerr.clients.plex.PlexServer", return_value=user_server_instance) as server_cls,
    ):
        resolved = client.get_user_server("alice")

    account_cls.assert_called_with(token="acct-tok-123", session=client._session, timeout=client.timeout)
    mock_account.user.assert_called_once_with("alice")
    mock_user.get_token.assert_called_once_with("machine-xyz")
    server_cls.assert_called_once_with(
        "http://localhost:32400",
        token="alice-server-tok",
        session=client._session,
        timeout=client.timeout,
    )
    mock_server.switchUser.assert_not_called()
    assert resolved is user_server_instance


def test_verify_owner_rejects_non_owner() -> None:
    client, _ = _make_client(machine_id="machine-123")

    not_owned = SimpleNamespace(
        username="guest",
        resources=lambda: [SimpleNamespace(clientIdentifier="machine-123", owned=False)],
    )
    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=not_owned):
        with pytest.raises(PlexAccountNotOwner, match="doesn't own"):
            client.verify_account_owns_server("guest-tok")

    missing_server = SimpleNamespace(
        username="stranger",
        resources=lambda: [SimpleNamespace(clientIdentifier="other-machine", owned=True)],
    )
    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=missing_server):
        with pytest.raises(PlexAccountNotOwner, match="doesn't own"):
            client.verify_account_owns_server("stranger-tok")

    owner = SimpleNamespace(
        username="owner_name",
        resources=lambda: [SimpleNamespace(clientIdentifier="machine-123", owned=True)],
    )
    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=owner):
        assert client.verify_account_owns_server("owner-tok") == "owner_name"


def test_pin_requires_plex_and_admin(env) -> None:
    assert env.tc.post("/api/settings/media-server/plex-account/pin", headers=env.alice).status_code == 403

    env.cfg.media_server = "none"
    env.cfg.plex_url = ""
    env.cfg.plex_token = ""
    res = env.tc.post("/api/settings/media-server/plex-account/pin", headers=env.admin)
    assert res.status_code == 409

    env.cfg.media_server = "plex"
    env.cfg.plex_url = "http://localhost:32400"
    env.cfg.plex_token = "local-tok"
    with patch(
        "trackseerr.api.routes.settings.create_plex_pin",
        return_value={"id": 42, "code": "ABCD", "auth_url": "https://app.plex.tv/auth#?code=ABCD"},
    ):
        ok = env.tc.post("/api/settings/media-server/plex-account/pin", headers=env.admin)
    assert ok.status_code == 200
    assert ok.json() == {"id": 42, "code": "ABCD", "auth_url": "https://app.plex.tv/auth#?code=ABCD"}


def test_check_pin_stores_token_and_username(env) -> None:
    owner = SimpleNamespace(
        username="plex_owner",
        resources=lambda: [SimpleNamespace(clientIdentifier="machine-123", owned=True)],
    )
    with (
        patch("trackseerr.api.routes.settings.check_plex_pin", return_value="secret-acct-tok"),
        patch("trackseerr.clients.plex.MyPlexAccount", return_value=owner),
    ):
        res = env.tc.post("/api/settings/media-server/plex-account/check", json={"pin_id": 42}, headers=env.admin)

    assert res.status_code == 200
    assert res.json() == {"connected": True, "pending": False, "username": "plex_owner"}
    assert "secret-acct-tok" not in res.text
    assert env.db.get_plex_account() == {"token": "secret-acct-tok", "username": "plex_owner"}
    assert get_plex_account_token() == "secret-acct-tok"

    # Also verify startup loader restores it
    set_plex_account_token("")
    media_server_settings.load_into_process(env.db)
    assert get_plex_account_token() == "secret-acct-tok"


def test_check_pin_pending(env) -> None:
    with patch("trackseerr.api.routes.settings.check_plex_pin", return_value=None):
        res = env.tc.post("/api/settings/media-server/plex-account/check", json={"pin_id": 42}, headers=env.admin)

    assert res.status_code == 200
    assert res.json() == {"connected": False, "pending": True}
    assert env.db.get_plex_account() == {"token": "", "username": ""}


def test_check_pin_non_owner_403_not_stored(env) -> None:
    non_owner = SimpleNamespace(
        username="not_owner",
        resources=lambda: [SimpleNamespace(clientIdentifier="machine-123", owned=False)],
    )
    with (
        patch("trackseerr.api.routes.settings.check_plex_pin", return_value="non-owner-tok"),
        patch("trackseerr.clients.plex.MyPlexAccount", return_value=non_owner),
    ):
        res = env.tc.post("/api/settings/media-server/plex-account/check", json={"pin_id": 42}, headers=env.admin)

    assert res.status_code == 403
    assert "doesn't own" in res.json()["detail"]
    assert env.db.get_plex_account() == {"token": "", "username": ""}
    assert get_plex_account_token() == ""


def test_check_pin_auth_error_returns_502(env) -> None:
    with patch("trackseerr.api.routes.settings.check_plex_pin", side_effect=PlexAuthError("plex.tv down")):
        res = env.tc.post("/api/settings/media-server/plex-account/check", json={"pin_id": 42}, headers=env.admin)
    assert res.status_code == 502


def test_disconnect_clears_token(env) -> None:
    env.db.set_plex_account("secret-acct-tok", "plex_owner")
    set_plex_account_token("secret-acct-tok")

    res = env.tc.delete("/api/settings/media-server/plex-account", headers=env.admin)
    assert res.status_code == 200
    assert res.json() == {"connected": False}
    assert env.db.get_plex_account() == {"token": "", "username": ""}
    assert get_plex_account_token() == ""


def test_get_media_server_reports_connected_without_token(env) -> None:
    res_empty = env.tc.get("/api/settings/media-server", headers=env.admin)
    assert res_empty.status_code == 200
    assert res_empty.json()["plex_account_connected"] is False
    assert res_empty.json()["plex_account_username"] == ""

    env.db.set_plex_account("super-secret-plex-token", "plex_owner")
    res_conn = env.tc.get("/api/settings/media-server", headers=env.admin)
    assert res_conn.status_code == 200
    body = res_conn.json()
    assert body["plex_account_connected"] is True
    assert body["plex_account_username"] == "plex_owner"
    assert "super-secret-plex-token" not in res_conn.text


def test_saving_media_server_page_keeps_plex_account(env, monkeypatch) -> None:
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env.cfg.media_server = ""
    env.cfg.plex_url = ""
    env.cfg.plex_token = ""

    env.db.set_plex_account("super-secret-plex-token", "plex_owner")

    res_jf = env.tc.put(
        "/api/settings/media-server",
        json={"type": "jellyfin", "url": "http://jf.example:8096", "username": "bob", "password": "", "api_key": "jf-key"},
        headers=env.admin,
    )
    assert res_jf.status_code == 200
    assert env.db.get_plex_account() == {"token": "super-secret-plex-token", "username": "plex_owner"}

    res_none = env.tc.put("/api/settings/media-server", json={"type": "none"}, headers=env.admin)
    assert res_none.status_code == 200
    assert env.db.get_plex_account() == {"token": "super-secret-plex-token", "username": "plex_owner"}


def test_role_guard_flags_plex_account_token() -> None:
    db = Database(":memory:")
    try:
        assert "a Plex account token" not in core_like_reasons(db.conn)
        db.set_plex_account("secret-tok", "plex_owner")
        assert "a Plex account token" in core_like_reasons(db.conn)
    finally:
        db.close()


def test_owner_recognised_when_local_username_differs() -> None:
    el = ET.Element("MyPlex", {"username": "ron@example.com"})
    client, _ = _make_client(token="local-tok", query_el=el, account_token="acct-tok-123")
    mock_account = MagicMock()
    mock_account.username = "RonTube"
    mock_account.email = "ron@example.com"
    mock_account.title = "Ron"

    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=mock_account):
        assert client.is_admin_username("RonTube") is True
        assert client.is_admin_username("rontube") is True
        assert client._get_admin_username() == "RonTube"
        server = client.get_user_server("RonTube")

    assert server is client.server
    mock_account.user.assert_not_called()


def test_owner_recognised_by_email_or_title() -> None:
    el = ET.Element("MyPlex", {"username": "ron_local", "email": "ron@example.com", "title": "Ron Title"})
    client, _ = _make_client(token="local-tok", query_el=el)
    assert client.is_admin_username("ron@example.com") is True
    assert client.is_admin_username("Ron Title") is True
    assert client.is_admin_username("ron_local") is True


def test_home_user_still_switches() -> None:
    el = ET.Element("MyPlex", {"username": "ron@example.com"})
    client, _ = _make_client(token="local-tok", query_el=el, machine_id="machine-xyz", account_token="acct-tok-123")
    mock_user = MagicMock()
    mock_user.get_token.return_value = "alice-server-tok"
    mock_account = MagicMock()
    mock_account.username = "RonTube"
    mock_account.email = "ron@example.com"
    mock_account.title = "Ron"
    mock_account.user.return_value = mock_user
    user_server = MagicMock()

    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=mock_account), patch(
        "trackseerr.clients.plex.PlexServer", return_value=user_server
    ) as server_cls:
        resolved = client.get_user_server("alice")

    assert resolved is user_server
    mock_account.user.assert_called_once_with("alice")
    server_cls.assert_called_once_with("http://localhost:32400", token="alice-server-tok", session=client._session, timeout=client.timeout)


def test_cloud_name_lookup_error_falls_back_to_local() -> None:
    import requests

    el = ET.Element("MyPlex", {"username": "ron_local", "email": "ron@example.com"})
    client, _ = _make_client(token="local-tok", query_el=el, account_token="acct-tok-123")

    with patch("trackseerr.clients.plex.MyPlexAccount", side_effect=requests.RequestException("network down")):
        assert client.is_admin_username("ron_local") is True
        assert client.is_admin_username("ron@example.com") is True
        assert client.is_admin_username("someone_else") is False
        assert client._get_admin_username() == "ron_local"


def test_sync_playlist_to_owner_uses_admin_server() -> None:
    from trackseerr.models import Playlist, Track

    el = ET.Element("MyPlex", {"username": "ron@example.com"})
    client, mock_server = _make_client(token="local-tok", query_el=el, account_token="acct-tok-123")
    mock_account = MagicMock()
    mock_account.username = "RonTube"
    mock_account.email = "ron@example.com"
    mock_account.title = "Ron"

    track = MagicMock()
    track.ratingKey = "101"
    mock_server.playlists.return_value = []
    mock_playlist = MagicMock()
    mock_playlist.ratingKey = "pl-1"
    mock_server.createPlaylist.return_value = mock_playlist

    playlist = Playlist(id="p1", name="My Mix", tracks=[Track("Song", "Artist", "Album")])
    with patch("trackseerr.clients.plex.MyPlexAccount", return_value=mock_account), patch.object(
        client, "match_playlist_tracks", return_value=([track], [])
    ):
        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["RonTube"],
        )

    assert len(results) == 1
    assert results[0].success is True
    mock_account.user.assert_not_called()
