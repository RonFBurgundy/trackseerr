"""Plex tokens embedded in plexapi/requests exception text must never reach results, responses or logs."""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from plex_playlist_sync.api.routes import auth as auth_routes
from plex_playlist_sync.api.routes import plex_playlists as plex_playlists_routes
from plex_playlist_sync.api.routes import users as users_routes
from plex_playlist_sync.api.routes.sync import SyncState
from plex_playlist_sync.auth import PlexAuthError, check_plex_pin
from plex_playlist_sync.cli import RedactLogFilter, redact_sensitive_query
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.models import Playlist, Track
from plex_playlist_sync.redaction import redact_text, safe_exc
from plex_playlist_sync import tailored_mixes

SECRET = "SECRETPLEX"
LEAKY_URL = f"http://plex:32400/library?X-Plex-Token={SECRET}"


@pytest.fixture
def redacted_caplog(caplog):
    """caplog at DEBUG with the same root redaction filter cli.install_log_redaction puts on real handlers."""
    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(RedactLogFilter())
    return caplog


def _assert_clean(caplog) -> None:
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()


# --------------------------------------------------------------------------- safe_exc unit tests


@pytest.mark.parametrize(
    "message",
    [
        f"http://plex:32400/x?X-Plex-Token={SECRET}",
        f"http://plex:32400/x?x-plex-token={SECRET}&a=1",
        f"http://plex:32400/x?X-PLEX-TOKEN={SECRET}",
        f"http://plex:32400/x?a=1&token={SECRET}",
        f"http://lidarr/x?apikey={SECRET}",
        f"http://x/y?api_key={SECRET}#frag",
        f"http://x/y?sk={SECRET}&api_sig={SECRET}",
        f"headers {{'X-Plex-Token': '{SECRET}'}}",
        f"X-Plex-Token={SECRET}",
    ],
)
def test_safe_exc_redacts_query_values_for_plexapi_types(message):
    for exc_type in (NotFound, BadRequest, Unauthorized):
        text = safe_exc(exc_type(message))
        assert SECRET not in text
        assert text.startswith(exc_type.__name__)
        assert "REDACTED" in text


def test_safe_exc_strips_url_userinfo():
    text = safe_exc(BadRequest(f"GET https://admin:{SECRET}@plex.example:32400/library failed"))
    assert SECRET not in text
    assert "plex.example:32400" in text
    assert SECRET not in redact_text(f"see http://user:{SECRET}@host/")


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(LEAKY_URL),
        requests.exceptions.ConnectionError(LEAKY_URL),
        ValueError(f"bad {LEAKY_URL}"),
        KeyError(SECRET),
    ],
)
def test_safe_exc_unknown_types_give_type_name_only(exc):
    assert safe_exc(exc) == type(exc).__name__


def test_safe_exc_safe_types_opt_in_and_empty_message():
    assert safe_exc(ValueError(f"x {LEAKY_URL}"), safe_types=(ValueError,)) == (
        "ValueError: x http://plex:32400/library?X-Plex-Token=REDACTED"
    )
    assert safe_exc(NotFound("")) == "NotFound"


def test_cli_reuses_shared_redaction_regex():
    from plex_playlist_sync import redaction

    assert redact_sensitive_query is redaction.redact_sensitive_query
    assert SECRET not in redact_sensitive_query(f"/p?X-Plex-Token={SECRET}")
    assert "invite/[REDACTED]" in redact_sensitive_query("/api/auth/invite/abc123")


# --------------------------------------------------------------------------- clients/plex.py


def _client(server: MagicMock) -> PlexClient:
    with patch("plex_playlist_sync.clients.plex.PlexServer", return_value=server):
        return PlexClient("http://localhost:32400", "tok")


class _Track:
    title = "Track 1"

    def artist(self):
        return MagicMock(title="Artist 1")

    def album(self):
        return MagicMock(title="Album 1")


def _playlist() -> Playlist:
    return Playlist(id="p1", name="PL", tracks=[Track("Track 1", "Artist 1", "Album 1")])


def _server() -> MagicMock:
    server = MagicMock()
    server.myPlexAccount.return_value = MagicMock(username="admin_user")
    server.search.return_value = [_Track()]
    return server


@pytest.mark.parametrize("exc_factory", [lambda: RuntimeError(LEAKY_URL), lambda: BadRequest(LEAKY_URL)])
def test_sync_playlist_to_users_error_has_no_token(redacted_caplog, exc_factory):
    server = _server()
    server.switchUser.side_effect = exc_factory()
    results = _client(server).sync_playlist_to_users(playlist=_playlist(), target_usernames=["alice"])
    assert len(results) == 1 and not results[0].success
    assert results[0].error.startswith("User alice: ")
    assert SECRET not in results[0].error
    _assert_clean(redacted_caplog)


def test_sync_playlist_to_users_admin_path_connection_error(redacted_caplog):
    server = _server()
    server.playlist.side_effect = requests.exceptions.ConnectionError(LEAKY_URL)
    results = _client(server).sync_playlist_to_users(playlist=_playlist(), target_usernames=["admin_user"])
    assert not results[0].success
    assert results[0].error.startswith("User admin_user: ")
    assert SECRET not in results[0].error
    _assert_clean(redacted_caplog)


def test_update_or_create_playlist_via_sync_playlist_result_has_no_token(redacted_caplog):
    server = _server()
    server.playlist.side_effect = RuntimeError(LEAKY_URL)
    result = _client(server).sync_playlist(_playlist())
    assert not result.success
    assert result.error == "RuntimeError"
    _assert_clean(redacted_caplog)


def test_update_or_create_playlist_plexapi_error_keeps_redacted_message(redacted_caplog):
    server = _server()
    server.playlist.side_effect = BadRequest(LEAKY_URL)
    result = _client(server).sync_playlist(_playlist())
    assert result.error == "BadRequest: http://plex:32400/library?X-Plex-Token=REDACTED"
    _assert_clean(redacted_caplog)


def test_test_connection_failure_message_has_no_token(redacted_caplog):
    server = MagicMock()
    client = _client(server)
    type(server).friendlyName = property(lambda self: (_ for _ in ()).throw(requests.exceptions.ConnectionError(LEAKY_URL)))
    ok, message = client.test_connection()
    assert ok is False
    assert SECRET not in message
    _assert_clean(redacted_caplog)


def test_connect_failure_logs_no_token(redacted_caplog):
    with patch("plex_playlist_sync.clients.plex.PlexServer", side_effect=requests.exceptions.ConnectionError(LEAKY_URL)):
        with pytest.raises(requests.exceptions.ConnectionError):
            PlexClient("http://localhost:32400", "tok")
    _assert_clean(redacted_caplog)


# --------------------------------------------------------------------------- api/routes/sync.py


def test_execute_sync_error_result_and_logs_have_no_token(redacted_caplog):
    db = MagicMock()
    db.list_playlists.side_effect = requests.exceptions.ConnectionError(LEAKY_URL)
    state = SyncState()
    try:
        result = state.execute_sync(db, MagicMock(), None, None, None)
    finally:
        logging.getLogger("plex_playlist_sync").removeHandler(state.log_handler)
    assert result == {"status": "error", "error": "ConnectionError"}
    assert state.last_run_stats["success_count"] == 0
    _assert_clean(redacted_caplog)


def test_execute_sync_with_failing_plex_playlist_sync_logs_no_token(redacted_caplog):
    db = MagicMock()
    db.list_playlists.return_value = [
        {"id": "p1", "name": "PL", "service": "spotify", "enabled": 1, "service_id": "x", "tracks_json": "[]"}
    ]
    plex = MagicMock()
    plex.refresh_auto_mix_snapshots.side_effect = NotFound(LEAKY_URL)
    state = SyncState()
    try:
        state.execute_sync(db, MagicMock(), plex, None, None)
    finally:
        logging.getLogger("plex_playlist_sync").removeHandler(state.log_handler)
    _assert_clean(redacted_caplog)
    assert "NotFound" in redacted_caplog.text


def test_broadcast_log_handler_redacts_tracebacks():
    from plex_playlist_sync.api.routes.sync import BroadcastLogHandler

    handler = BroadcastLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    sent: list[str] = []
    handler._safe_put = lambda q, msg: sent.append(msg)  # type: ignore[method-assign]
    loop = MagicMock()
    loop.is_running.return_value = True
    loop.call_soon_threadsafe.side_effect = lambda fn, q, msg: fn(q, msg)
    handler.listeners.append((loop, MagicMock()))
    try:
        raise RuntimeError(LEAKY_URL)
    except RuntimeError:
        import sys

        record = logging.LogRecord("plex_playlist_sync", logging.DEBUG, __file__, 1, "boom", None, sys.exc_info())
    handler.emit(record)
    assert sent and SECRET not in sent[0]


# --------------------------------------------------------------------------- api/routes/plex_playlists.py


def test_plex_errors_context_manager_http_detail_has_no_token():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as info:
        with plex_playlists_routes._plex_errors():
            raise requests.exceptions.ConnectionError(LEAKY_URL)
    assert info.value.status_code == 502
    assert SECRET not in info.value.detail

    with pytest.raises(HTTPException) as info:
        with plex_playlists_routes._plex_errors():
            raise NotFound(LEAKY_URL)
    assert info.value.status_code == 404 and SECRET not in info.value.detail


def test_server_for_http_detail_and_log_have_no_token(redacted_caplog):
    from fastapi import HTTPException

    plex = MagicMock()
    plex.get_user_server.side_effect = Unauthorized(LEAKY_URL)
    with pytest.raises(HTTPException) as info:
        plex_playlists_routes._server_for(plex, "bob")
    assert SECRET not in info.value.detail
    _assert_clean(redacted_caplog)


# --------------------------------------------------------------------------- tailored_mixes.py


def test_tailored_mixes_sync_error_uses_shared_helper():
    assert not hasattr(tailored_mixes, "_redact_tokens")
    assert tailored_mixes.safe_exc is safe_exc
    assert tailored_mixes.redact_text is redact_text


# --------------------------------------------------------------------------- library_scanner.py


def test_library_scanner_status_error_has_no_token(redacted_caplog):
    scanner = LibraryScanner()
    scanner.scan = MagicMock(side_effect=requests.exceptions.ConnectionError(LEAKY_URL))  # type: ignore[method-assign]
    scanner._run_background_scan(MagicMock(), None, False, None)
    assert scanner._status["status"] == "failed"
    assert SECRET not in str(scanner._status)
    assert scanner._status["error"] == "ConnectionError"
    _assert_clean(redacted_caplog)


# --------------------------------------------------------------------------- auth.py / api/routes/users.py / auth routes


def test_plex_pin_check_error_has_no_token():
    err = requests.exceptions.ConnectionError(LEAKY_URL)
    with patch("plex_playlist_sync.auth.requests.get", side_effect=err):
        with pytest.raises(PlexAuthError) as info:
            check_plex_pin(1)
    assert SECRET not in str(info.value)


def test_auth_route_pin_error_detail_has_no_token(redacted_caplog):
    from fastapi import HTTPException

    with patch.object(auth_routes, "create_plex_pin", side_effect=PlexAuthError(f"Failed: {LEAKY_URL}")):
        with pytest.raises(HTTPException) as info:
            auth_routes.generate_pin(req=None, forward_url=None, db=None)
    assert info.value.status_code == 502
    assert SECRET not in info.value.detail
    _assert_clean(redacted_caplog)


def test_users_refresh_discovery_error_detail_has_no_token(redacted_caplog):
    from fastapi import HTTPException

    plex = MagicMock()
    plex.get_home_users.side_effect = requests.exceptions.ConnectionError(LEAKY_URL)
    with pytest.raises(HTTPException) as info:
        users_routes.refresh_users(_admin={}, db=MagicMock(), plex_client=plex)
    assert info.value.status_code == 502
    assert SECRET not in info.value.detail
    _assert_clean(redacted_caplog)
