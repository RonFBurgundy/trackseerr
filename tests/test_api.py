"""Comprehensive tests for FastAPI REST Service (Phase 3)."""

from datetime import datetime, timezone
import io
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_deezer_client,
    get_plex_client,
    get_spotify_client,
)
from plex_playlist_sync.auth import (
    PlexAuthError,
    create_session_token,
    get_or_create_secret_key,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import Playlist, Track
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def secret_key(tmp_path):
    """Provides a consistent 32-byte secret key."""
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        spotify_client_id="sp-client-id",
        spotify_client_secret="sp-client-secret",
    )


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    bob = test_db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice, "bob": bob}


def create_auth_headers_or_cookies(test_db, user: dict, secret_key: bytes, use_bearer: bool = False):
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    test_db.create_session(session_id=token, user_id=user["id"])
    if use_bearer:
        return {"headers": {"Authorization": f"Bearer {token}"}, "token": token}
    return {"cookies": {"session_token": token}, "token": token}


# =============================================================================
# Security Headers & CORS Tests
# =============================================================================


class TestSecurityHeadersAndCORS:
    def test_security_headers_present(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "tier": "all-in-one"}
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-XSS-Protection"] == "1; mode=block"

    def test_cors_headers(self, app_and_client):
        _, client = app_and_client
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert resp.headers.get("access-control-allow-credentials") == "true"


# =============================================================================
# Authentication Endpoints Tests
# =============================================================================


class TestAuthEndpoints:
    @patch("plex_playlist_sync.api.routes.auth.create_plex_pin")
    def test_generate_pin_success(self, mock_create_pin, app_and_client):
        mock_create_pin.return_value = {
            "id": 12345,
            "code": "CODE12",
            "auth_url": "https://app.plex.tv/auth#?clientID=trackseerr&code=CODE12",
        }
        _, client = app_and_client
        resp = client.post("/api/auth/plex/pin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 12345
        assert data["code"] == "CODE12"
        assert "auth_url" in data

    @patch("plex_playlist_sync.api.routes.auth.create_plex_pin")
    def test_generate_pin_with_forward_url(self, mock_create_pin, app_and_client):
        mock_create_pin.return_value = {
            "id": 12345,
            "code": "CODE12",
            "auth_url": "https://app.plex.tv/auth#?clientID=trackseerr&code=CODE12&forwardUrl=https%3A%2F%2Ftrackseerr.local%2F",
        }
        _, client = app_and_client
        # Same origin as the request (TestClient host is "testserver"); cross-origin is covered in
        # tests/test_forward_url_origin.py.
        resp = client.post("/api/auth/plex/pin", json={"forward_url": "http://testserver/"})
        assert resp.status_code == 200
        mock_create_pin.assert_called_once_with(forward_url="http://testserver/")

    @patch("plex_playlist_sync.api.routes.auth.create_plex_pin")
    def test_generate_pin_uses_application_url_as_fallback_forward_url(self, mock_create_pin, app_and_client, test_db):
        test_db.update_general_settings({"application_url": "https://trackseerr.mydomain.com"})
        mock_create_pin.return_value = {
            "id": 12345,
            "code": "CODE12",
            "auth_url": "https://app.plex.tv/auth#?clientID=trackseerr&code=CODE12&forwardUrl=https%3A%2F%2Ftrackseerr.mydomain.com",
        }
        _, client = app_and_client
        resp = client.post("/api/auth/plex/pin")
        assert resp.status_code == 200
        mock_create_pin.assert_called_once_with(forward_url="https://trackseerr.mydomain.com")



    @patch("plex_playlist_sync.api.routes.auth.create_plex_pin")
    def test_generate_pin_failure_upstream(self, mock_create_pin, app_and_client):
        mock_create_pin.side_effect = PlexAuthError("Plex server down")
        _, client = app_and_client
        resp = client.post("/api/auth/plex/pin")
        assert resp.status_code == 502
        assert "Plex server down" in resp.json()["detail"]

    @patch("plex_playlist_sync.api.routes.auth.check_plex_pin")
    def test_verify_pin_unclaimed_or_expired(self, mock_check_pin, app_and_client):
        mock_check_pin.return_value = None
        _, client = app_and_client
        resp = client.post("/api/auth/plex/verify", json={"pin_id": 999})
        assert resp.status_code == 400
        assert "not yet authorized" in resp.json()["detail"]

    @patch("plex_playlist_sync.api.routes.auth.verify_server_access")
    @patch("plex_playlist_sync.api.routes.auth.check_plex_pin")
    def test_verify_pin_outsider_403_rejection(
        self, mock_check_pin, mock_verify_access, app_and_client
    ):
        mock_check_pin.return_value = "plex-token-xyz"
        mock_verify_access.return_value = (False, False)  # has_access = False

        _, client = app_and_client
        resp = client.post(
            "/api/auth/plex/verify",
            json={"pin_id": 12345, "target_machine_id": "target-server-123"},
        )
        assert resp.status_code == 403
        assert "Forbidden" in resp.json()["detail"]

    @patch("plex_playlist_sync.api.routes.auth.get_plex_user")
    @patch("plex_playlist_sync.api.routes.auth.verify_server_access")
    @patch("plex_playlist_sync.api.routes.auth.check_plex_pin")
    def test_verify_pin_success_admin_and_cookie_set(
        self, mock_check_pin, mock_verify_access, mock_get_user, app_and_client, test_db
    ):
        mock_check_pin.return_value = "plex-token-admin"
        mock_verify_access.return_value = (True, True)  # has_access=True, is_owner=True
        mock_get_user.return_value = {
            "id": "plex-admin-1",
            "username": "PlexAdmin",
            "email": "admin@plex.tv",
            "thumb": "https://plex.tv/admin.jpg",
        }

        _, client = app_and_client
        resp = client.post(
            "/api/auth/plex/verify",
            json={"pin_id": 12345, "target_machine_id": "target-server-123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["id"] == "plex-admin-1"
        assert data["user"]["is_admin"] is True
        assert "token" in data

        # Check HttpOnly cookie
        assert "session_token" in resp.cookies
        cookie_header = resp.headers.get("set-cookie", "").lower()
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header

        # Verify user and session saved in DB
        db_user = test_db.get_user("plex-admin-1")
        assert db_user is not None
        assert db_user["is_admin"] is True
        db_session = test_db.get_session(data["token"])
        assert db_session is not None

    @patch("plex_playlist_sync.api.routes.auth.get_plex_user")
    @patch("plex_playlist_sync.api.routes.auth.verify_server_access")
    @patch("plex_playlist_sync.api.routes.auth.check_plex_pin")
    def test_verify_pin_success_regular_user(
        self, mock_check_pin, mock_verify_access, mock_get_user, app_and_client, test_db
    ):
        mock_check_pin.return_value = "plex-token-friend"
        mock_verify_access.return_value = (True, False)  # has_access=True, is_owner=False
        mock_get_user.return_value = {
            "id": "plex-friend-2",
            "username": "FriendUser",
            "email": "friend@plex.tv",
            "thumb": "",
        }

        _, client = app_and_client
        resp = client.post(
            "/api/auth/plex/verify",
            json={"pin_id": 54321, "target_machine_id": "target-server-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["is_admin"] is False

    def test_get_me_unauthenticated_returns_401(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_get_me_with_cookie_success(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp = client.get("/api/auth/me", cookies=auth["cookies"])
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "alice"
        assert resp.json()["user"]["is_admin"] is False

    def test_get_me_with_bearer_header_success(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key, use_bearer=True)
        resp = client.get("/api/auth/me", headers=auth["headers"])
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "admin_user"
        assert resp.json()["user"]["is_admin"] is True

    def test_logout_clears_db_and_cookie(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        token = auth["token"]

        # User is authenticated
        resp = client.get("/api/auth/me", cookies=auth["cookies"])
        assert resp.status_code == 200

        # Logout
        resp_logout = client.post("/api/auth/logout", cookies=auth["cookies"])
        assert resp_logout.status_code == 200
        assert resp_logout.json()["status"] == "success"

        # Session in DB deleted
        assert test_db.get_session(token) is None

        # Subsequent call with old token fails
        resp_after = client.get("/api/auth/me", cookies=auth["cookies"])
        assert resp_after.status_code == 401


# =============================================================================
# Users Endpoints Tests
# =============================================================================


class TestUsersEndpoints:
    def test_list_users_admin_boundary(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.get("/api/users", cookies=auth["cookies"])
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) == 3
        usernames = [u["username"] for u in users]
        assert "admin_user" in usernames
        assert "alice" in usernames
        assert "bob" in usernames

    def test_list_users_regular_user_forbidden(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp = client.get("/api/users", cookies=auth["cookies"])
        assert resp.status_code == 403

    def test_refresh_users_non_admin_forbidden(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp = client.post("/api/users/refresh", cookies=auth["cookies"])
        assert resp.status_code == 403

    def test_refresh_users_admin_success(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        app, client = app_and_client
        mock_plex = MagicMock()
        mock_plex.get_home_users.return_value = [
            {"id": "admin-1", "username": "admin_user", "email": "admin@plex.tv", "is_admin": True},
            {"id": "new-user-3", "username": "charlie", "email": "charlie@plex.tv", "is_admin": False},
        ]
        app.dependency_overrides[get_plex_client] = lambda: mock_plex

        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post("/api/users/refresh", cookies=auth["cookies"])
        assert resp.status_code == 200
        users = resp.json()
        usernames = [u["username"] for u in users]
        assert "charlie" in usernames
        assert test_db.get_user("new-user-3") is not None


# =============================================================================
# Playlists Endpoints & SSRF Validation Tests
# =============================================================================


class TestPlaylistsEndpoints:
    def test_create_playlist_ssrf_and_malicious_urls_rejected(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)

        malicious_inputs = [
            "http://169.254.169.254/latest/meta-data",
            "http://localhost:32400/admin",
            "http://127.0.0.1:8080/evil",
            "https://attacker.com/open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://open.spotify.com@attacker.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "file:///etc/passwd",
            "ftp://deezer.com/playlist/12345",
            "javascript:alert(1)",
            "invalid_random_string",
            "123",  # Deezer IDs must be >= 5 digits
            "37i9dQZF1DXcBWIGoYBM5",  # Spotify IDs must be 22 chars
        ]

        for bad_input in malicious_inputs:
            resp = client.post(
                "/api/playlists",
                json={"url_or_id": bad_input},
                cookies=auth["cookies"],
            )
            assert resp.status_code == 400, f"Expected 400 for {bad_input}, got {resp.status_code}"
            assert "SSRF validation failed" in resp.json()["detail"]

    def test_create_spotify_playlist_success(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        app, client = app_and_client
        mock_spotify = MagicMock()
        mock_spotify.get_playlist_by_id.return_value = Playlist(
            id="37i9dQZF1DXcBWIGoYBM5M",
            name="Today's Top Hits <script>alert(1)</script>",
            description="The biggest hits right now.",
            poster="https://img.spotify.com/poster.jpg",
        )
        app.dependency_overrides[get_spotify_client] = lambda: mock_spotify

        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post(
            "/api/playlists",
            json={
                "url_or_id": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
                "targets": ["admin-1", "user-alice"],
            },
            cookies=auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "37i9dQZF1DXcBWIGoYBM5M"
        assert data["service"] == "spotify"
        # Script tag stripped by sanitize_text
        assert "<script>" not in data["name"]
        assert "Today's Top Hits alert(1)" in data["name"]
        assert data["targets"] == ["admin-1", "user-alice"]

    def test_create_deezer_playlist_success(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        app, client = app_and_client
        mock_deezer = MagicMock()
        mock_deezer.get_playlist_by_id.return_value = Playlist(
            id="1313621735",
            name="Chill Vibes",
            description="Relaxing beats",
            poster="https://e-cdns-images.dzcdn.net/images/cover/chill.jpg",
        )
        app.dependency_overrides[get_deezer_client] = lambda: mock_deezer

        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post(
            "/api/playlists",
            json={"url_or_id": "https://www.deezer.com/us/playlist/1313621735"},
            cookies=auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "1313621735"
        assert data["service"] == "deezer"
        assert data["name"] == "Chill Vibes"

    def test_create_playlist_regular_user_targets_self_only(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp = client.post(
            "/api/playlists",
            json={
                "url_or_id": "37i9dQZF1DXcBWIGoYBM5M",
                "targets": ["user-bob", "admin-1"],  # Attempt to target others
            },
            cookies=auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        # Alice is creator, target should strictly default/restrict to Alice
        assert data["targets"] == ["user-alice"]

    def test_list_playlists_permission_boundaries(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        test_db.upsert_playlist("p1", "Playlist 1")
        test_db.upsert_playlist("p2", "Playlist 2")
        test_db.set_playlist_targets("p1", ["user-alice"])
        test_db.set_playlist_targets("p2", ["user-bob"])

        _, client = app_and_client

        # Admin sees all playlists
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp_admin = client.get("/api/playlists", cookies=admin_auth["cookies"])
        assert resp_admin.status_code == 200
        assert len(resp_admin.json()) == 2

        # Alice only sees p1
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp_alice = client.get("/api/playlists", cookies=alice_auth["cookies"])
        assert resp_alice.status_code == 200
        alice_ids = [p["id"] for p in resp_alice.json()]
        assert alice_ids == ["p1"]

    def test_update_playlist_targets_admin(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        test_db.upsert_playlist("p1", "Playlist 1")
        test_db.set_playlist_targets("p1", ["user-alice"])

        _, client = app_and_client
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.put(
            "/api/playlists/p1/targets",
            json={"user_ids": ["user-alice", "user-bob", "admin-1"]},
            cookies=admin_auth["cookies"],
        )
        assert resp.status_code == 200
        assert set(resp.json()["targets"]) == {"user-alice", "user-bob", "admin-1"}

    def test_update_playlist_targets_regular_user_toggles_self_only(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        test_db.upsert_playlist("p1", "Playlist 1", creator_id="user-alice")
        test_db.set_playlist_targets("p1", ["admin-1", "user-bob"])

        _, client = app_and_client
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)

        # Alice (creator) may target only herself
        resp = client.put(
            "/api/playlists/p1/targets",
            json={"user_ids": ["user-alice"]},
            cookies=alice_auth["cookies"],
        )
        assert resp.status_code == 200
        assert resp.json()["targets"] == ["user-alice"]

        # Targeting anyone else is forbidden and changes nothing
        resp_other = client.put(
            "/api/playlists/p1/targets",
            json={"user_ids": ["user-alice", "user-bob"]},
            cookies=alice_auth["cookies"],
        )
        assert resp_other.status_code == 403
        assert test_db.get_playlist_targets("p1") == ["user-alice"]

        # Alice clears her targets
        resp_out = client.put(
            "/api/playlists/p1/targets",
            json={"user_ids": []},
            cookies=alice_auth["cookies"],
        )
        assert resp_out.status_code == 200
        assert resp_out.json()["targets"] == []

    def test_update_playlist_targets_regular_user_cannot_access_private_playlist(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        # Bob creates a private playlist
        test_db.upsert_playlist("p_bob", "Bob Private Playlist", creator_id="user-bob")
        test_db.set_playlist_targets("p_bob", ["user-bob"])

        _, client = app_and_client
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)

        # Alice attempts to add herself to Bob's private playlist (IDOR attempt): 404, not 403
        resp = client.put(
            "/api/playlists/p_bob/targets",
            json={"user_ids": ["user-alice"]},
            cookies=alice_auth["cookies"],
        )
        assert resp.status_code == 404
        assert test_db.get_playlist_targets("p_bob") == ["user-bob"]

    def test_delete_playlist_boundaries(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        # Alice creates p1
        test_db.upsert_playlist("p1", "Alice Playlist", creator_id="user-alice")

        _, client = app_and_client
        bob_auth = create_auth_headers_or_cookies(test_db, seeded_users["bob"], secret_key)
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)

        # Bob cannot delete p1
        resp_bob = client.delete("/api/playlists/p1", cookies=bob_auth["cookies"])
        assert resp_bob.status_code == 404
        assert test_db.get_playlist("p1") is not None

        # Alice (creator) can delete p1
        resp_alice = client.delete("/api/playlists/p1", cookies=alice_auth["cookies"])
        assert resp_alice.status_code == 200
        assert resp_alice.json()["status"] == "deleted"
        assert test_db.get_playlist("p1") is None

        # Admin creates p2 and deletes p2
        test_db.upsert_playlist("p2", "Admin Playlist", creator_id="admin-1")
        resp_admin = client.delete("/api/playlists/p2", cookies=admin_auth["cookies"])
        assert resp_admin.status_code == 200
        assert test_db.get_playlist("p2") is None

    def test_import_playlist_tracks_regular_user_targets_self_only(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)

        resp = client.post(
            "/api/playlists/import",
            json={
                "name": "My Offline Playlist",
                "service": "spotify",
                "tracks": [
                    {"title": "Track One", "artist": "Artist One"},
                    {"title": "Track Two", "artist": "Artist Two", "album": "Album Two"},
                ],
                "targets": ["user-bob", "admin-1"],  # Alice attempts targeting others
            },
            cookies=alice_auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Offline Playlist"
        assert data["track_count"] == 2
        # Target must be strictly restricted to Alice
        assert data["targets"] == ["user-alice"]
        assert test_db.get_playlist(data["id"]) is not None

    def test_import_playlist_tracks_admin_targets(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)

        resp = client.post(
            "/api/playlists/import",
            json={
                "name": "Shared Family Mix",
                "service": "spotify",
                "tracks": [
                    {"title": "Family Track", "artist": "Artist"},
                ],
                "targets": ["user-alice", "user-bob"],
            },
            cookies=admin_auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["targets"] == ["user-alice", "user-bob"]
        assert test_db.get_playlist_targets(data["id"]) == ["user-alice", "user-bob"]

    def test_import_playlist_sanitization_and_validation(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)

        # XSS sanitization
        resp = client.post(
            "/api/playlists/import",
            json={
                "name": "Clean Mix <script>alert(1)</script>",
                "tracks": [
                    {"title": "Track <b style='color:red'>Bold</b>", "artist": "Artist"},
                ],
            },
            cookies=admin_auth["cookies"],
        )
        assert resp.status_code == 201
        assert "<script>" not in resp.json()["name"]
        assert "Clean Mix alert(1)" in resp.json()["name"]

        # Empty tracks validation
        resp_empty = client.post(
            "/api/playlists/import",
            json={"name": "Empty Mix", "tracks": []},
            cookies=admin_auth["cookies"],
        )
        assert resp_empty.status_code == 422


# =============================================================================
# Sync Endpoints Tests
# =============================================================================


class TestSyncEndpoints:
    def test_sync_status(self, app_and_client, seeded_users, test_db, secret_key):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.get("/api/sync/status", cookies=auth["cookies"])
        assert resp.status_code == 200
        data = resp.json()
        assert "is_syncing" in data
        assert "last_run_stats" in data

    def test_trigger_sync_background(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post("/api/sync", cookies=auth["cookies"])
        assert resp.status_code == 200
        assert resp.json()["status"] in ("started", "already_running")

    def test_sync_stream_sse_endpoint(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.get("/api/sync/stream?limit=1", cookies=auth["cookies"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "live sync log stream" in resp.text


# =============================================================================
# Missing Tracks & CSV Generation Tests
# =============================================================================


class TestMissingEndpoints:
    @pytest.fixture(autouse=True)
    def seed_missing(self, test_db, seeded_users):
        test_db.upsert_playlist("p1", "Hits 2026")
        test_db.set_playlist_targets("p1", ["user-alice"])
        test_db.record_sync_result(
            "p1",
            status="partial",
            missing_tracks=[
                {"title": "=1+1 Formula Injection", "artist": "Attacker", "album": "Bad", "url": "https://bad.com"},
                {"title": "Normal Track", "artist": "Good Artist", "album": "Album", "url": "https://good.com"},
            ],
        )

        test_db.upsert_playlist("p2", "Bob Hits")
        test_db.set_playlist_targets("p2", ["user-bob"])
        test_db.record_sync_result(
            "p2",
            status="partial",
            missing_tracks=[
                {"title": "Bob Missing", "artist": "Artist 2", "album": "Album 2", "url": ""},
            ],
        )

    def test_get_missing_tracks_permission_boundaries(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client

        # Admin sees all missing tracks (3 total)
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp_admin = client.get("/api/missing", cookies=admin_auth["cookies"])
        assert resp_admin.status_code == 200
        assert len(resp_admin.json()) == 3

        # Missing tracks are admin-only: a regular user is refused outright
        alice_auth = create_auth_headers_or_cookies(test_db, seeded_users["alice"], secret_key)
        resp_alice = client.get("/api/missing", cookies=alice_auth["cookies"])
        assert resp_alice.status_code == 403
        resp_alice_p2 = client.get("/api/missing?playlist_id=p2", cookies=alice_auth["cookies"])
        assert resp_alice_p2.status_code == 403

    def test_missing_tracks_csv_generation_and_formula_injection_defense(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        _, client = app_and_client
        admin_auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.get("/api/missing/csv?playlist_id=p1", cookies=admin_auth["cookies"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert 'attachment; filename="missing_tracks_p1.csv"' in resp.headers["content-disposition"]

        csv_content = resp.text
        lines = [line.strip() for line in csv_content.strip().split("\n")]
        assert lines[0] == "Playlist ID,Title,Artist,Album,URL,Created At"

        # Check CSV formula injection neutralized with leading single quote: '=1+1 -> ''=1+1
        assert "'=1+1 Formula Injection" in csv_content
        assert "Normal Track" in csv_content


# =============================================================================
# Edge Cases & Branch Coverage Tests
# =============================================================================


class TestEdgeCasesAndBranchCoverage:
    def test_verify_pin_no_machine_id_configured(self, app_and_client):
        app, client = app_and_client
        app.dependency_overrides[get_plex_client] = lambda: None

        with patch("plex_playlist_sync.api.routes.auth.check_plex_pin") as mock_check, \
             patch.dict("os.environ", {}, clear=True):
            mock_check.return_value = "some-token"
            resp = client.post("/api/auth/plex/verify", json={"pin_id": 123})
            assert resp.status_code == 500
            assert "machine identifier is not configured" in resp.json()["detail"]

    def test_verify_pin_get_user_plex_auth_error(self, app_and_client):
        with patch("plex_playlist_sync.api.routes.auth.check_plex_pin") as mock_check, \
             patch("plex_playlist_sync.api.routes.auth.verify_server_access") as mock_access, \
             patch("plex_playlist_sync.api.routes.auth.get_plex_user") as mock_get_user:
            mock_check.return_value = "some-token"
            mock_access.return_value = (True, True)
            mock_get_user.side_effect = PlexAuthError("Plex user endpoint failed")

            _, client = app_and_client
            resp = client.post(
                "/api/auth/plex/verify",
                json={"pin_id": 123, "target_machine_id": "machine-1"},
            )
            assert resp.status_code == 502
            assert "Failed to retrieve user details from Plex" in resp.json()["detail"]

    def test_verify_pin_unexpected_error(self, app_and_client):
        with patch("plex_playlist_sync.api.routes.auth.check_plex_pin") as mock_check:
            mock_check.side_effect = RuntimeError("Crash")
            _, client = app_and_client
            resp = client.post("/api/auth/plex/verify", json={"pin_id": 123})
            assert resp.status_code == 500

    def test_refresh_users_plex_client_error(self, app_and_client, seeded_users, test_db, secret_key):
        app, client = app_and_client
        mock_plex = MagicMock()
        mock_plex.get_home_users.side_effect = RuntimeError("Plex server unreachable")
        app.dependency_overrides[get_plex_client] = lambda: mock_plex

        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post("/api/users/refresh", cookies=auth["cookies"])
        assert resp.status_code == 502
        assert "Failed to query Plex users" in resp.json()["detail"]

    def test_create_playlist_metadata_fetch_error_handled_gracefully(
        self, app_and_client, seeded_users, test_db, secret_key
    ):
        app, client = app_and_client
        mock_spotify = MagicMock()
        mock_spotify.get_playlist_by_id.side_effect = Exception("Spotify API rate limit")
        app.dependency_overrides[get_spotify_client] = lambda: mock_spotify

        auth = create_auth_headers_or_cookies(test_db, seeded_users["admin"], secret_key)
        resp = client.post(
            "/api/playlists",
            json={"url_or_id": "37i9dQZF1DXcBWIGoYBM5M"},
            cookies=auth["cookies"],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "37i9dQZF1DXcBWIGoYBM5M"
        assert "Spotify Playlist 37i9dQZF1DXcBWIGoYBM5M" in data["name"]

    def test_sync_state_execute_sync_full_flow(
        self, test_db, test_config
    ):
        from plex_playlist_sync.api.routes.sync import sync_state
        from plex_playlist_sync.models import SyncResult

        # Seed playlist and users
        user = test_db.upsert_user("u1", "alice", is_admin=False)
        test_db.upsert_playlist("p1", "My Sync Playlist", service="spotify")
        test_db.set_playlist_targets("p1", ["u1"])

        mock_sp = MagicMock()
        mock_sp.get_playlist_tracks.return_value = [
            Track("Song 1", "Artist 1", "Album 1"),
            Track("Song 2", "Artist 2", "Album 2"),
        ]
        mock_plex = MagicMock()
        mock_plex.sync_playlist_to_users.return_value = [
            SyncResult("My Sync Playlist", 2, 2, 0, True)
        ]
        mock_plex.match_playlist_tracks.return_value = ([object()], [])

        res = sync_state.execute_sync(
            db=test_db,
            config=test_config,
            plex_client=mock_plex,
            spotify_client=mock_sp,
            deezer_client=None,
        )
        assert res["status"] == "success"
        assert res["stats"]["success_count"] == 1
        assert sync_state.last_run_stats["success_count"] == 1

        # Test already running branch
        sync_state.is_syncing = True
        try:
            res_busy = sync_state.execute_sync(test_db, test_config, None, None, None)
            assert res_busy["status"] == "already_running"
        finally:
            sync_state.is_syncing = False

    def test_dependencies_client_fallbacks(self, monkeypatch, tmp_path):
        from plex_playlist_sync.api.dependencies import (
            get_current_user,
            get_db,
            get_deezer_client,
            get_plex_client,
            get_spotify_client,
        )
        from fastapi import HTTPException
        from starlette.requests import Request

        # Test get_db with :memory:
        monkeypatch.setenv("DATABASE_PATH", ":memory:")
        db_mem = get_db()
        assert db_mem.db_path == ":memory:"

        # Test get_plex_client exception handling
        cfg_bad_plex = Config(plex_url="invalid://url", plex_token="bad", data_dir=str(tmp_path))
        with patch("plex_playlist_sync.api.dependencies.PlexClient", side_effect=Exception("Plex init fail")):
            assert get_plex_client(cfg_bad_plex) is None

        # Test get_spotify_client exception handling
        cfg_bad_sp = Config(
            plex_url="http://localhost:32400",
            plex_token="token",
            spotify_client_id="id",
            spotify_client_secret="secret",
            data_dir=str(tmp_path),
        )
        # Test get_spotify_client falls back to SpotifyWebScraper if SpotifyClient fails
        with patch("plex_playlist_sync.api.dependencies.SpotifyClient", side_effect=Exception("Spotify init fail")):
            fallback_client = get_spotify_client(cfg_bad_sp)
            assert fallback_client is not None
            assert hasattr(fallback_client, "get_playlist_by_id")

        # Test get_spotify_client returns None if both fail
        with patch("plex_playlist_sync.api.dependencies.SpotifyClient", side_effect=Exception("Spotify init fail")), \
             patch("plex_playlist_sync.api.dependencies.SpotifyWebScraper", side_effect=Exception("Scraper fail")):
            assert get_spotify_client(cfg_bad_sp) is None

        # Test get_deezer_client exception handling
        with patch("plex_playlist_sync.api.dependencies.DeezerClient", side_effect=Exception("Deezer init fail")):
            assert get_deezer_client() is None

