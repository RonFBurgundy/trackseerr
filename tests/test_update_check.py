"""Tests for the software update checker: version comparison, GitHub release polling,

database persistence & migration v70, system API endpoints, and task registration.
"""

from datetime import datetime, timezone
import logging
import os
import sqlite3
from unittest.mock import MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

import trackseerr
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, require_core_tier
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import SCHEMA_VERSION, Database
from trackseerr.task_manager import TASKS, WORKER_THREAD_TASKS
from trackseerr.update_check import (
    _fallback_parse_tuple,
    fetch_latest_release,
    is_newer_version,
    is_update_check_enabled,
    run_update_check,
    get_update_status,
)


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance with full migrations."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def secret_key(tmp_path):
    """Provides a consistent secret key."""
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        update_check=True,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and non-admin users in DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


def create_auth_cookies(test_db, user: dict, secret_key: bytes):
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    test_db.create_session(session_id=token, user_id=user["id"])
    return {"session_token": token}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a TestClient with injected DB and Config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


# =============================================================================
# 1. Version Comparison Tests
# =============================================================================


class TestVersionComparison:
    def test_newer_standard_versions(self):
        assert is_newer_version("1.1.0", "1.0.0") is True
        assert is_newer_version("2.0.0", "1.9.9") is True
        assert is_newer_version("1.0.1", "1.0.0") is True
        assert is_newer_version("1.10.0", "1.9.0") is True

    def test_same_version(self):
        assert is_newer_version("1.0.0", "1.0.0") is False
        assert is_newer_version("2.1.3", "2.1.3") is False

    def test_older_version(self):
        assert is_newer_version("0.9.0", "1.0.0") is False
        assert is_newer_version("1.0.0", "1.1.0") is False
        assert is_newer_version("1.0.0", "2.0.0") is False

    def test_v_prefix_handling(self):
        assert is_newer_version("v1.1.0", "1.0.0") is True
        assert is_newer_version("1.1.0", "v1.0.0") is True
        assert is_newer_version("v1.1.0", "v1.0.0") is True
        assert is_newer_version("v1.0.0", "1.0.0") is False
        assert is_newer_version("1.0.0", "v1.0.0") is False

    def test_prerelease_comparisons(self):
        # Final release is newer than release candidate of same version
        assert is_newer_version("1.1.0", "1.1.0rc1") is True
        # Release candidate is newer than older final release
        assert is_newer_version("1.1.0rc1", "1.0.0") is True
        # Higher rc number is newer
        assert is_newer_version("1.1.0rc2", "1.1.0rc1") is True
        # RC is NOT newer than final of same version
        assert is_newer_version("1.1.0rc1", "1.1.0") is False

    def test_dev_and_local_builds(self):
        # Final release is newer than dev build of same version
        assert is_newer_version("1.0.0", "1.0.0.dev1") is True
        # Dev build of future version is newer than current release
        assert is_newer_version("1.1.0.dev1", "1.0.0") is True
        # Current final is NOT older than dev of same version
        assert is_newer_version("1.0.0.dev1", "1.0.0") is False
        # Version with local tag
        assert is_newer_version("1.0.1", "1.0.0+abc1234") is True

    def test_empty_or_invalid_versions(self):
        assert is_newer_version("", "1.0.0") is False
        assert is_newer_version("1.0.0", "") is False
        assert is_newer_version("", "") is False
        assert is_newer_version("invalid", "1.0.0") is False

    def test_fallback_parse_tuple_direct(self):
        # Verify heuristic fallback parsing tiers
        dev_t = _fallback_parse_tuple("1.0.0.dev2")
        alpha_t = _fallback_parse_tuple("1.0.0a1")
        beta_t = _fallback_parse_tuple("1.0.0b1")
        rc_t = _fallback_parse_tuple("1.0.0rc1")
        final_t = _fallback_parse_tuple("1.0.0")

        assert dev_t[1] == 0  # dev tier
        assert alpha_t[1] == 1  # alpha tier
        assert beta_t[1] == 2  # beta tier
        assert rc_t[1] == 3  # rc tier
        assert final_t[1] == 4  # final tier

        assert dev_t < alpha_t < beta_t < rc_t < final_t

    def test_invalid_pep440_falls_back_to_heuristic(self, caplog):
        """Invalid PEP 440 version strings fall back to heuristic parser and log at DEBUG."""
        with caplog.at_level(logging.DEBUG, logger="trackseerr.update_check"):
            assert is_newer_version("1.2.0-hotfix", "1.1.0") is True
            assert is_newer_version("1.1.0", "1.2.0-hotfix") is False

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "packaging.version failed to parse" in r.getMessage()
        ]
        assert len(debug_records) >= 1
        rec = debug_records[0]
        assert "1.2.0-hotfix" in rec.getMessage()
        assert "1.1.0" in rec.getMessage()

    def test_unparseable_pair_returns_false_and_logs(self, caplog):
        """When fallback parser raises ValueError or TypeError, returns False and logs WARNING."""
        with caplog.at_level(logging.WARNING, logger="trackseerr.update_check"):
            with patch(
                "trackseerr.update_check._fallback_parse_tuple",
                side_effect=ValueError("bad digits"),
            ):
                assert is_newer_version("invalid-a", "invalid-b") is False

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Fallback version parsing failed" in r.getMessage()
        ]
        assert len(warning_records) == 1
        rec = warning_records[0]
        assert "invalid-a" in rec.getMessage()
        assert "invalid-b" in rec.getMessage()
        assert "bad digits" in rec.getMessage()

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="trackseerr.update_check"):
            with patch(
                "trackseerr.update_check._fallback_parse_tuple",
                side_effect=TypeError("incomparable"),
            ):
                assert is_newer_version("bad-1", "bad-2") is False

        type_err_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Fallback version parsing failed" in r.getMessage()
        ]
        assert len(type_err_records) == 1
        assert "bad-1" in type_err_records[0].getMessage()
        assert "bad-2" in type_err_records[0].getMessage()
        assert "incomparable" in type_err_records[0].getMessage()

    def test_fallback_when_packaging_unavailable(self):
        """When packaging module is not available (pkg_version is None), fallback parser handles comparisons."""
        with patch("trackseerr.update_check.pkg_version", None):
            assert is_newer_version("1.2.0", "1.1.0") is True
            assert is_newer_version("1.0.0", "1.1.0") is False
            assert is_newer_version("1.1.0", "1.1.0") is False


# =============================================================================
# 2. HTTP Polling & GitHub Release Fetching Tests
# =============================================================================


class TestFetchLatestRelease:
    def test_fetch_200_ok(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tag_name": "v1.5.0",
            "html_url": "https://github.com/RonFBurgundy/trackseerr/releases/tag/v1.5.0",
            "published_at": "2026-10-01T12:00:00Z",
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")

            assert error is None
            assert data is not None
            assert data["tag_name"] == "v1.5.0"
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args[1]
            assert "Accept" in call_kwargs["headers"]
            assert call_kwargs["headers"]["Accept"] == "application/vnd.github+json"
            assert call_kwargs["headers"]["User-Agent"] == "TrackSeerr/1.0.0"

    def test_fetch_404_no_releases(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")
            assert data is None
            assert error is None  # 404 is clean: no releases yet

    def test_fetch_403_rate_limit(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")
            assert data is None
            assert error is not None
            assert "rate limit exceeded" in error
            assert "403" in error

    def test_fetch_429_rate_limit(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")
            assert data is None
            assert error is not None
            assert "rate limit exceeded" in error
            assert "429" in error

    def test_fetch_timeout_exception(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.TimeoutException("Read timed out")
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")
            assert data is None
            assert error is not None
            assert "timed out" in error.lower()

    def test_fetch_network_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client

            data, error = fetch_latest_release("1.0.0")
            assert data is None
            assert error is not None
            assert "network error" in error.lower()


# =============================================================================
# 3. Update Check Execution & Persistence Tests
# =============================================================================


class TestRunUpdateCheck:
    def test_run_success_persists_result(self, test_db, test_config):
        mock_data = {
            "tag_name": "v9.9.0",
            "html_url": "https://github.com/RonFBurgundy/trackseerr/releases/tag/v9.9.0",
            "published_at": "2026-10-05T00:00:00Z",
        }

        with patch("trackseerr.update_check.fetch_latest_release", return_value=(mock_data, None)):
            res = run_update_check(test_db, test_config)

            assert res["latest_version"] == "9.9.0"
            assert res["release_url"] == "https://github.com/RonFBurgundy/trackseerr/releases/tag/v9.9.0"
            assert res["published_at"] == "2026-10-05T00:00:00Z"
            assert res["update_available"] is True
            assert res["enabled"] is True
            assert res["error"] is None
            assert res["checked_at"] is not None

            # Verify persisted in database
            state = test_db.get_update_check_state()
            assert state["latest_version"] == "9.9.0"
            assert state["release_url"] == "https://github.com/RonFBurgundy/trackseerr/releases/tag/v9.9.0"
            assert state["error"] is None

    def test_run_404_no_releases_persists_clean_state(self, test_db, test_config):
        with patch("trackseerr.update_check.fetch_latest_release", return_value=(None, None)):
            res = run_update_check(test_db, test_config)

            assert res["latest_version"] is None
            assert res["update_available"] is False
            assert res["enabled"] is True
            assert res["error"] is None
            assert res["checked_at"] is not None

            state = test_db.get_update_check_state()
            assert state["latest_version"] is None
            assert state["error"] is None

    def test_run_404_clears_previous_release(self, test_db, test_config):
        test_db.save_update_check_result(
            latest_version="1.5.0",
            release_url="https://github.com/RonFBurgundy/trackseerr/releases/tag/v1.5.0",
            published_at="2026-09-01T00:00:00Z",
            checked_at="2026-09-01T00:00:00Z",
            error=None,
        )
        with patch("trackseerr.update_check.fetch_latest_release", return_value=(None, None)):
            res = run_update_check(test_db, test_config)
            assert res["latest_version"] is None
            assert res["update_available"] is False
            state = test_db.get_update_check_state()
            assert state["latest_version"] is None
            assert state["release_url"] is None
            assert state["published_at"] is None

    def test_run_403_rate_limit_keeps_last_good_result(self, test_db, test_config):
        # 1. Seed DB with last good result
        test_db.save_update_check_result(
            latest_version="1.5.0",
            release_url="https://github.com/RonFBurgundy/trackseerr/releases/tag/v1.5.0",
            published_at="2026-09-01T00:00:00Z",
            checked_at="2026-09-01T00:00:00Z",
            error=None,
        )

        # 2. Simulate 403 rate limit error
        err_msg = "GitHub API rate limit exceeded (HTTP 403)"
        with patch("trackseerr.update_check.fetch_latest_release", return_value=(None, err_msg)):
            res = run_update_check(test_db, test_config)

            # Last good result must be preserved
            assert res["latest_version"] == "1.5.0"
            assert res["release_url"] == "https://github.com/RonFBurgundy/trackseerr/releases/tag/v1.5.0"
            assert res["published_at"] == "2026-09-01T00:00:00Z"
            # Error must be stamped
            assert res["error"] == err_msg
            assert res["checked_at"] is not None

            # Verify in DB
            state = test_db.get_update_check_state()
            assert state["latest_version"] == "1.5.0"
            assert state["release_url"] == "https://github.com/RonFBurgundy/trackseerr/releases/tag/v1.5.0"
            assert state["error"] == err_msg

    def test_disabled_via_database_setting_skips_http_call(self, test_db, test_config):
        test_db.set_update_check_enabled(False)
        assert is_update_check_enabled(test_db, test_config) is False

        with patch("httpx.Client") as mock_client:
            res = run_update_check(test_db, test_config)
            mock_client.assert_not_called()
            assert res["enabled"] is False

    def test_disabled_via_env_config_skips_http_call(self, test_db):
        config = Config(plex_url="http://x", plex_token="t", update_check=False)
        test_db.set_update_check_enabled(True)
        assert is_update_check_enabled(test_db, config) is False

        with patch("httpx.Client") as mock_client:
            res = run_update_check(test_db, config)
            mock_client.assert_not_called()
            assert res["enabled"] is False


# =============================================================================
# 4. API Endpoints & RBAC Tests
# =============================================================================


class TestUpdateApiEndpoints:
    def test_unauthenticated_forbidden(self, app_and_client):
        _, client = app_and_client
        assert client.get("/api/system/update").status_code == 401
        assert client.put("/api/system/update", json={"enabled": False}).status_code == 401

    def test_non_admin_forbidden(self, app_and_client, seeded_users, secret_key, test_db):
        _, client = app_and_client
        alice_cookies = create_auth_cookies(test_db, seeded_users["alice"], secret_key)

        resp_get = client.get("/api/system/update", cookies=alice_cookies)
        assert resp_get.status_code == 403
        assert "Administrator access required" in resp_get.json()["detail"]

        resp_put = client.put("/api/system/update", json={"enabled": False}, cookies=alice_cookies)
        assert resp_put.status_code == 403
        assert "Administrator access required" in resp_put.json()["detail"]

    def test_admin_get_and_put_update(self, app_and_client, seeded_users, secret_key, test_db):
        _, client = app_and_client
        admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

        # GET: Initial state
        resp = client.get("/api/system/update", cookies=admin_cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert "current_version" in body
        assert "current_commit" in body
        assert "latest_version" in body
        assert "release_url" in body
        assert "published_at" in body
        assert "checked_at" in body
        assert "update_available" in body
        assert "enabled" in body
        assert "error" in body
        assert body["enabled"] is True

        # PUT: Toggle enabled off
        resp_put = client.put("/api/system/update", json={"enabled": False}, cookies=admin_cookies)
        assert resp_put.status_code == 200
        assert resp_put.json()["enabled"] is False

        # GET: Confirm setting persists
        resp_get2 = client.get("/api/system/update", cookies=admin_cookies)
        assert resp_get2.status_code == 200
        assert resp_get2.json()["enabled"] is False

        # PUT: Toggle enabled back on
        resp_put2 = client.put("/api/system/update", json={"enabled": True}, cookies=admin_cookies)
        assert resp_put2.status_code == 200
        assert resp_put2.json()["enabled"] is True

    def test_core_tier_required(self):
        # Gateway tier must raise 403 from require_core_tier
        gateway_cfg = Config(plex_url="http://x", plex_token="t", role="gateway")
        with pytest.raises(Exception) as exc_info:
            require_core_tier(gateway_cfg)
        assert exc_info.value.status_code == 403

    def test_not_on_gateway_allowlists(self):
        """Endpoints under /api/system/update must never be present on any gateway allowlist."""
        from trackseerr.api import tier_middleware

        for table in (
            tier_middleware.GATEWAY_LOCAL_ALLOWLIST,
            tier_middleware.GATEWAY_FORWARD_ALLOWLIST,
            tier_middleware.GATEWAY_FORWARD_SERVICE_ALLOWLIST,
        ):
            assert not tier_middleware._allowed(table, "GET", "/api/system/update")
            assert not tier_middleware._allowed(table, "PUT", "/api/system/update")


# =============================================================================
# 5. Schema v70 Migration Tests
# =============================================================================


class TestMigrationV70:

    def test_database_reaches_schema_head(self, test_db):
        """Full Database initialization reaches SCHEMA_VERSION (>= 70)."""
        assert SCHEMA_VERSION >= 70
        cur = test_db.conn.cursor()
        cur.execute("SELECT MAX(version) FROM schema_migrations")
        max_ver = cur.fetchone()[0]
        assert max_ver == SCHEMA_VERSION

        state = test_db.get_update_check_state()
        assert state["enabled"] is True
        assert state["latest_version"] is None
        assert state["error"] is None


# =============================================================================
# 6. Task Registration Tests
# =============================================================================


class TestTaskRegistration:
    def test_update_check_task_registered(self):
        assert "update_check" in TASKS
        spec = TASKS["update_check"]
        assert spec.id == "update_check"
        assert spec.name == "Software Update Check"
        assert spec.default_interval_seconds == 12 * 3600  # 12 hours
        assert spec.kind == "interval"
        assert 6 * 3600 in spec.presets
        assert spec.editable is True

    def test_worker_thread_tasks_mapping(self):
        assert "UpdateCheckWorkerThread" in WORKER_THREAD_TASKS
        assert WORKER_THREAD_TASKS["UpdateCheckWorkerThread"] == "update_check"
        assert "ManualUpdateCheckTask" in WORKER_THREAD_TASKS
        assert WORKER_THREAD_TASKS["ManualUpdateCheckTask"] == "update_check"

    def test_manual_run_via_tasks_api(self, app_and_client, seeded_users, secret_key, test_db):
        _, client = app_and_client
        admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
        with patch("trackseerr.update_check.run_update_check") as mock_run:
            mock_run.return_value = {
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "update_available": True,
                "enabled": True,
                "error": None,
            }
            resp = client.post("/api/system/tasks/update_check/run", cookies=admin_cookies)
            assert resp.status_code == 200
            assert resp.json()["success"] is True
