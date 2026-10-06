"""Comprehensive test suite for TrackSeerr Phase 4:

Media Issues API, Router Mounting, Quota Integration, and Governance.
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    has_permission,
    require_permission,
)
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    IssueStatus,
    IssueType,
    MediaIssue,
    MusicRequest,
    NotificationEvent,
    RequestStatus,
    UserPermission,
)
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        user_request_quota=3,
        auto_approve_requests=False,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin, manager, and regular users with distinct permissions into test DB."""
    # Admin has is_admin=True and permissions=35 (ADMIN | REQUEST | REPORT_ISSUE)
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)

    # Manager: regular user with MANAGE_REQUESTS added (34 | 16 = 50)
    manager = test_db.upsert_user("user-manager", "manager_user", "manager@plex.tv", is_admin=False)
    manager = test_db.update_user_governance(
        "user-manager",
        permissions=int(UserPermission.DEFAULT | UserPermission.MANAGE_REQUESTS),
    )

    # Alice: standard user with DEFAULT permissions (34: REQUEST | REPORT_ISSUE)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)

    # Bob: standard user with DEFAULT permissions
    bob = test_db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)

    # Charlie: restricted user with no permissions (0)
    charlie = test_db.upsert_user("user-charlie", "charlie", "charlie@plex.tv", is_admin=False)
    charlie = test_db.update_user_governance("user-charlie", permissions=0)

    # Dave: auto-approve user (REQUEST | REPORT_ISSUE | AUTO_APPROVE = 38)
    dave = test_db.upsert_user("user-dave", "dave", "dave@plex.tv", is_admin=False)
    dave = test_db.update_user_governance(
        "user-dave",
        permissions=int(UserPermission.DEFAULT | UserPermission.AUTO_APPROVE),
    )

    # Eve: auto-approve-album user (REQUEST | REPORT_ISSUE | AUTO_APPROVE_ALBUM = 42)
    eve = test_db.upsert_user("user-eve", "eve", "eve@plex.tv", is_admin=False)
    eve = test_db.update_user_governance(
        "user-eve",
        permissions=int(UserPermission.DEFAULT | UserPermission.AUTO_APPROVE_ALBUM),
    )

    return {
        "admin": admin,
        "manager": manager,
        "alice": alice,
        "bob": bob,
        "charlie": charlie,
        "dave": dave,
        "eve": eve,
    }


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer header for a given user."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=bool(user.get("is_admin")),
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# 1. Database Migration v13 & Storage Tests
# =============================================================================


class TestDatabaseMigrationV13:
    def test_migration_v13_applied_and_columns_exist(self, test_db):
        """Verifies schema migration v13 added the governance columns and media_issues table."""
        # 1. Check schema_migrations version
        cur = test_db.conn.cursor()
        cur.execute("SELECT MAX(version) FROM schema_migrations")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 13

        # 2. Check users table columns
        cur.execute("PRAGMA table_info(users)")
        user_cols = {r["name"]: r["type"] for r in cur.fetchall()}
        assert "permissions" in user_cols
        assert "request_limit_quota" in user_cols
        assert "request_limit_days" in user_cols

        # 3. Check media_issues table columns
        cur.execute("PRAGMA table_info(media_issues)")
        issue_cols = {r["name"]: r["type"] for r in cur.fetchall()}
        expected_cols = [
            "id",
            "request_id",
            "media_title",
            "artist",
            "issue_type",
            "problem_details",
            "status",
            "user_id",
            "created_at",
            "updated_at",
        ]
        for col in expected_cols:
            assert col in issue_cols, f"Column {col} missing in media_issues table"

        # 4. Check indices
        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indices = [r["name"] for r in cur.fetchall()]
        assert "idx_media_issues_status" in indices
        assert "idx_media_issues_user" in indices

    def test_migration_v13_updates_admin_flag_in_permissions(self, test_db):
        """Admin users have ADMIN bit (1) set in permissions."""
        admin = test_db.upsert_user("adm-test", "adm_test", is_admin=True)
        assert admin["permissions"] & int(UserPermission.ADMIN)
        assert admin["is_admin"] == 1

        reg = test_db.upsert_user("reg-test", "reg_test", is_admin=False)
        assert not (reg["permissions"] & int(UserPermission.ADMIN))
        assert reg["permissions"] & int(UserPermission.REQUEST)
        assert reg["permissions"] & int(UserPermission.REPORT_ISSUE)

    def test_media_issues_storage_crud(self, test_db, seeded_users):
        """Tests Database class CRUD operations for media issues."""
        user = seeded_users["alice"]

        # Create
        issue = MediaIssue(
            id="issue-test-1",
            user_id=user["id"],
            media_title="Abbey Road",
            artist="The Beatles",
            issue_type=IssueType.AUDIO_QUALITY,
            problem_details="Crackles on track 2",
            status=IssueStatus.OPEN,
        )
        created = test_db.create_issue(issue)
        assert created["id"] == "issue-test-1"
        assert created["media_title"] == "Abbey Road"
        assert created["artist"] == "The Beatles"
        assert created["issue_type"] == "audio_quality"
        assert created["status"] == "open"
        assert created["username"] == "alice"

        # Read
        fetched = test_db.get_issue("issue-test-1")
        assert fetched is not None
        assert fetched["id"] == "issue-test-1"
        assert fetched["problem_details"] == "Crackles on track 2"
        assert fetched["username"] == "alice"

        # List
        all_issues = test_db.list_issues()
        assert len(all_issues) == 1

        alice_issues = test_db.list_issues(user_id=user["id"])
        assert len(alice_issues) == 1

        bob_issues = test_db.list_issues(user_id="user-bob")
        assert len(bob_issues) == 0

        open_issues = test_db.list_issues(status="open")
        assert len(open_issues) == 1

        closed_issues = test_db.list_issues(status="closed")
        assert len(closed_issues) == 0

        # Update
        updated = test_db.update_issue(
            "issue-test-1",
            {"status": "in_progress", "problem_details": "Investigating FLAC source"},
        )
        assert updated["status"] == "in_progress"
        assert updated["problem_details"] == "Investigating FLAC source"

        # Delete
        assert test_db.delete_issue("issue-test-1") is True
        assert test_db.get_issue("issue-test-1") is None
        assert test_db.delete_issue("issue-test-1") is False

    def test_media_issues_cascades_and_foreign_keys(self, test_db, seeded_users):
        """Verifies ON DELETE CASCADE for user and ON DELETE SET NULL for request."""
        alice = seeded_users["alice"]

        req = MusicRequest(
            id="req-cascade-1",
            user_id=alice["id"],
            item_type="album",
            title="Let It Be",
            artist="The Beatles",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        issue = MediaIssue(
            id="issue-cascade-1",
            user_id=alice["id"],
            request_id="req-cascade-1",
            media_title="Let It Be",
            artist="The Beatles",
            issue_type="missing_tracks",
            problem_details="Track 4 missing",
        )
        test_db.create_issue(issue)

        # Deleting request sets request_id to NULL
        test_db.delete_request("req-cascade-1")
        refreshed = test_db.get_issue("issue-cascade-1")
        assert refreshed is not None
        assert refreshed["request_id"] is None

        # Deleting user cascades and deletes media issue
        test_db.conn.execute("DELETE FROM users WHERE id = ?", (alice["id"],))
        test_db.conn.commit()
        assert test_db.get_issue("issue-cascade-1") is None


# =============================================================================
# 2. Permissions IntFlag & Dependencies Tests
# =============================================================================


class TestUserPermissionsAndDependencies:
    def test_user_permission_intflag_values(self):
        """Verifies exact bitmask values for UserPermission."""
        assert UserPermission.ADMIN == 1
        assert UserPermission.REQUEST == 2
        assert UserPermission.AUTO_APPROVE == 4
        assert UserPermission.AUTO_APPROVE_ALBUM == 8
        assert UserPermission.MANAGE_REQUESTS == 16
        assert UserPermission.REPORT_ISSUE == 32
        assert UserPermission.DEFAULT == 34  # REQUEST | REPORT_ISSUE

        # Bitwise assertions
        assert (UserPermission.REQUEST | UserPermission.REPORT_ISSUE) == UserPermission.DEFAULT
        assert UserPermission.ADMIN | UserPermission.DEFAULT == 35

    def test_has_permission_logic(self):
        """Tests has_permission helper function with various bitmasks."""
        # Admin user (is_admin=True) has all permissions
        admin_user = {"is_admin": True, "permissions": 1}
        assert has_permission(admin_user, UserPermission.REQUEST) is True
        assert has_permission(admin_user, UserPermission.AUTO_APPROVE) is True
        assert has_permission(admin_user, UserPermission.AUTO_APPROVE_ALBUM) is True
        assert has_permission(admin_user, UserPermission.MANAGE_REQUESTS) is True
        assert has_permission(admin_user, UserPermission.REPORT_ISSUE) is True

        # Admin user (permissions & ADMIN) has all permissions even if is_admin is False
        perm_admin = {"is_admin": False, "permissions": int(UserPermission.ADMIN)}
        assert has_permission(perm_admin, UserPermission.REQUEST) is True

        # Default user (permissions=34)
        default_user = {"is_admin": False, "permissions": int(UserPermission.DEFAULT)}
        assert has_permission(default_user, UserPermission.REQUEST) is True
        assert has_permission(default_user, UserPermission.REPORT_ISSUE) is True
        assert has_permission(default_user, UserPermission.AUTO_APPROVE) is False
        assert has_permission(default_user, UserPermission.AUTO_APPROVE_ALBUM) is False
        assert has_permission(default_user, UserPermission.MANAGE_REQUESTS) is False

        # Restricted user (permissions=0)
        restricted = {"is_admin": False, "permissions": 0}
        assert has_permission(restricted, UserPermission.REQUEST) is False
        assert has_permission(restricted, UserPermission.REPORT_ISSUE) is False

        # None fallback
        fallback = {"is_admin": False, "permissions": None}
        assert has_permission(fallback, UserPermission.REQUEST) is True
        assert has_permission(fallback, UserPermission.REPORT_ISSUE) is True
        assert has_permission(fallback, UserPermission.AUTO_APPROVE) is False

    def test_require_permission_dependency(self):
        """Tests require_permission FastAPI dependency."""
        dep_request = require_permission(UserPermission.REQUEST)
        user_ok = {"is_admin": False, "permissions": int(UserPermission.REQUEST)}
        assert dep_request(current_user=user_ok) == user_ok

        user_forbidden = {"is_admin": False, "permissions": 0}
        with pytest.raises(HTTPException) as exc_info:
            dep_request(current_user=user_forbidden)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Permission denied: requires REQUEST"

        dep_report = require_permission(UserPermission.REPORT_ISSUE)
        with pytest.raises(HTTPException) as exc_info2:
            dep_report(current_user=user_forbidden)
        assert exc_info2.value.status_code == 403
        assert exc_info2.value.detail == "Permission denied: requires REPORT_ISSUE"


# =============================================================================
# 3. Rolling Quota & Request Permissions Tests
# =============================================================================


class TestRollingQuotaAndRequestPermissions:
    def test_rolling_active_request_count_db(self, test_db, seeded_users):
        """Tests rolling window count vs lifetime active count in Database."""
        alice = seeded_users["alice"]
        uid = alice["id"]

        # Insert active request created now
        test_db.conn.execute(
            """
            INSERT INTO music_requests (id, user_id, item_type, title, artist, status, created_at)
            VALUES (?, ?, 'album', 'Recent Active', 'Artist 1', 'pending', CURRENT_TIMESTAMP)
            """,
            ("req-recent", uid),
        )

        # Insert active request created 10 days ago
        test_db.conn.execute(
            """
            INSERT INTO music_requests (id, user_id, item_type, title, artist, status, created_at)
            VALUES (?, ?, 'album', 'Old Active', 'Artist 2', 'processing', datetime('now', '-10 days'))
            """,
            ("req-old", uid),
        )

        # Insert completed/inactive request created now
        test_db.conn.execute(
            """
            INSERT INTO music_requests (id, user_id, item_type, title, artist, status, created_at)
            VALUES (?, ?, 'album', 'Recent Available', 'Artist 3', 'available', CURRENT_TIMESTAMP)
            """,
            ("req-avail", uid),
        )

        # Insert rejected request created 2 days ago
        test_db.conn.execute(
            """
            INSERT INTO music_requests (id, user_id, item_type, title, artist, status, created_at)
            VALUES (?, ?, 'album', 'Recent Rejected', 'Artist 4', 'rejected', datetime('now', '-2 days'))
            """,
            ("req-rej", uid),
        )
        test_db.conn.commit()

        # 7-day rolling window counts only the recent pending request
        assert test_db.get_user_active_request_count(uid, days=7) == 1

        # 14-day rolling window counts recent and old active requests
        assert test_db.get_user_active_request_count(uid, days=14) == 2

        # Lifetime window (days=None or days=0) counts all active requests
        assert test_db.get_user_active_request_count(uid, days=None) == 2
        assert test_db.get_user_active_request_count(uid, days=0) == 2

    def test_create_request_permission_enforcement(self, app_and_client, test_db, test_config, seeded_users):
        """User without REQUEST permission receives 403 Forbidden."""
        _, client = app_and_client
        charlie = seeded_users["charlie"]
        headers = _auth_headers(charlie, test_db, test_config)

        resp = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "No Access", "artist": "Artist"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "Permission denied: requires REQUEST" in resp.json()["detail"]

        resp_batch = client.post(
            "/api/requests/batch",
            json={"requests": [{"item_type": "album", "title": "No Access", "artist": "Artist"}]},
            headers=headers,
        )
        assert resp_batch.status_code == 403
        assert "Permission denied: requires REQUEST" in resp_batch.json()["detail"]

    def test_create_request_rolling_quota_enforcement(self, app_and_client, test_db, test_config, seeded_users):
        """Tests that user-specific request_limit_quota and request_limit_days are respected."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        # Set quota to 2 within 7 days
        test_db.update_user_governance("user-alice", request_limit_quota=2, request_limit_days=7)
        alice_updated = test_db.get_user("user-alice")
        headers = _auth_headers(alice_updated, test_db, test_config)

        # 1. First request
        r1 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 1", "artist": "Artist 1"},
            headers=headers,
        )
        assert r1.status_code == 201

        # 2. Second request
        r2 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 2", "artist": "Artist 2"},
            headers=headers,
        )
        assert r2.status_code == 201

        # 3. Third request fails quota
        r3 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 3", "artist": "Artist 3"},
            headers=headers,
        )
        assert r3.status_code == 400
        assert "quota reached" in r3.json()["detail"].lower()

    def test_batch_requests_rolling_quota_enforcement(self, app_and_client, test_db, test_config, seeded_users):
        """Batch request checks remaining quota against rolling active count."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        test_db.update_user_governance("user-alice", request_limit_quota=3, request_limit_days=7)
        alice_updated = test_db.get_user("user-alice")
        headers = _auth_headers(alice_updated, test_db, test_config)

        # Create 2 initial requests
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Existing 1", "artist": "Artist A"},
            headers=headers,
        )
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Existing 2", "artist": "Artist B"},
            headers=headers,
        )

        # Batch of 2 should fail because only 1 remains of 3
        resp_batch = client.post(
            "/api/requests/batch",
            json={
                "requests": [
                    {"item_type": "album", "title": "Batch 1", "artist": "Artist C"},
                    {"item_type": "album", "title": "Batch 2", "artist": "Artist D"},
                ]
            },
            headers=headers,
        )
        assert resp_batch.status_code == 400
        assert "quota reached" in resp_batch.json()["detail"].lower()

        # Batch of 1 succeeds
        resp_batch_ok = client.post(
            "/api/requests/batch",
            json={
                "requests": [
                    {"item_type": "album", "title": "Batch 1", "artist": "Artist C"},
                ]
            },
            headers=headers,
        )
        assert resp_batch_ok.status_code == 201
        assert resp_batch_ok.json()["count"] == 1

    def test_auto_approve_permissions_workflow(self, app_and_client, test_db, test_config, seeded_users):
        """Tests that AUTO_APPROVE and AUTO_APPROVE_ALBUM permissions transition immediately to processing."""
        _, client = app_and_client
        dave = seeded_users["dave"]  # Has AUTO_APPROVE
        dave_headers = _auth_headers(dave, test_db, test_config)

        resp = client.post(
            "/api/requests",
            json={"item_type": "track", "title": "Track Dave", "artist": "Artist Dave"},
            headers=dave_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "processing"

        eve = seeded_users["eve"]  # Has AUTO_APPROVE_ALBUM
        eve_headers = _auth_headers(eve, test_db, test_config)

        # Eve requesting an album -> auto approved (processing)
        resp_album = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Eve Album", "artist": "Artist Eve"},
            headers=eve_headers,
        )
        assert resp_album.status_code == 201
        assert resp_album.json()["status"] == "processing"

        # Eve requesting a single track -> pending (not auto approved)
        resp_track = client.post(
            "/api/requests",
            json={"item_type": "track", "title": "Eve Track", "artist": "Artist Eve"},
            headers=eve_headers,
        )
        assert resp_track.status_code == 201
        assert resp_track.json()["status"] == "pending"


# =============================================================================
# 4. User Governance API (/api/users) Tests
# =============================================================================


class TestUserGovernanceAPI:
    def test_put_user_governance_admin_success(self, app_and_client, test_db, test_config, seeded_users):
        """Admin can update permissions and quotas on any user."""
        _, client = app_and_client
        admin = seeded_users["admin"]
        alice = seeded_users["alice"]
        admin_headers = _auth_headers(admin, test_db, test_config)

        payload = {
            "permissions": int(UserPermission.DEFAULT | UserPermission.AUTO_APPROVE),
            "request_limit_quota": 15,
            "request_limit_days": 30,
            "is_admin": False,
        }
        resp = client.put(f"/api/users/{alice['id']}", json=payload, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["permissions"] == int(UserPermission.DEFAULT | UserPermission.AUTO_APPROVE)
        assert data["request_limit_quota"] == 15
        assert data["request_limit_days"] == 30
        assert data["is_admin"] == 0

        # Verify DB persisted
        user_db = test_db.get_user(alice["id"])
        assert user_db["request_limit_quota"] == 15
        assert user_db["request_limit_days"] == 30

    def test_put_user_governance_non_admin_forbidden(self, app_and_client, test_db, test_config, seeded_users):
        """Non-admin user receives 403 Forbidden attempting to update governance."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        alice_headers = _auth_headers(alice, test_db, test_config)

        resp = client.put(
            f"/api/users/{bob['id']}",
            json={"request_limit_quota": 999},
            headers=alice_headers,
        )
        assert resp.status_code == 403

    def test_put_user_governance_clear_quota(self, app_and_client, test_db, test_config, seeded_users):
        """Setting request_limit_quota=None clears custom quota to NULL."""
        _, client = app_and_client
        admin = seeded_users["admin"]
        alice = seeded_users["alice"]
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Set custom quota
        client.put(f"/api/users/{alice['id']}", json={"request_limit_quota": 10}, headers=admin_headers)
        assert test_db.get_user(alice["id"])["request_limit_quota"] == 10

        # Clear custom quota
        resp = client.put(f"/api/users/{alice['id']}", json={"request_limit_quota": None}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["request_limit_quota"] is None
        assert test_db.get_user(alice["id"])["request_limit_quota"] is None

    def test_put_user_governance_not_found(self, app_and_client, test_db, test_config, seeded_users):
        """Updating non-existent user returns 404."""
        _, client = app_and_client
        admin = seeded_users["admin"]
        admin_headers = _auth_headers(admin, test_db, test_config)

        resp = client.put("/api/users/non-existent-user", json={"permissions": 1}, headers=admin_headers)
        assert resp.status_code == 404

    def test_get_user_me_telemetry(self, app_and_client, test_db, test_config, seeded_users):
        """GET /api/users/me returns governance and rolling quota telemetry."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        test_db.update_user_governance("user-alice", request_limit_quota=5, request_limit_days=14)
        alice_updated = test_db.get_user("user-alice")
        headers = _auth_headers(alice_updated, test_db, test_config)

        # Before requests: active=0, remaining=5
        resp1 = client.get("/api/users/me", headers=headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["id"] == "user-alice"
        assert data1["quota_limit"] == 5
        assert data1["rolling_days"] == 14
        assert data1["active_requests"] == 0
        assert data1["remaining_quota"] == 5

        # Create 1 request
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Telemetry Album", "artist": "Artist"},
            headers=headers,
        )

        # After request: active=1, remaining=4
        resp2 = client.get("/api/users/me", headers=headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["active_requests"] == 1
        assert data2["remaining_quota"] == 4


# =============================================================================
# 5. Media Issues API (/api/issues) Endpoints & Notification Tests
# =============================================================================


class TestMediaIssuesAPI:
    @patch("plex_playlist_sync.api.routes.issues.notification_dispatcher.dispatch")
    def test_create_issue_success_and_notification(
        self, mock_dispatch, app_and_client, test_db, test_config, seeded_users
    ):
        """Creates an issue, emits notification event, and returns 201 Created."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        payload = {
            "media_title": "The Dark Side of the Moon",
            "artist": "Pink Floyd",
            "issue_type": "audio_quality",
            "problem_details": "Distortion on Time guitar solo",
            "request_id": None,
        }
        resp = client.post("/api/issues", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"].startswith("issue-")
        assert data["media_title"] == "The Dark Side of the Moon"
        assert data["artist"] == "Pink Floyd"
        assert data["issue_type"] == "audio_quality"
        assert data["problem_details"] == "Distortion on Time guitar solo"
        assert data["status"] == "open"
        assert data["user_id"] == alice["id"]
        assert data["username"] == "alice"
        assert data["created_at"] is not None

        # Verify notification dispatched
        mock_dispatch.assert_called_once()
        args, kwargs = mock_dispatch.call_args
        assert args[0] == NotificationEvent.ISSUE_REPORTED
        assert args[1]["id"] == data["id"]
        assert args[1]["artist"] == "Pink Floyd"
        assert args[1]["title"] == "The Dark Side of the Moon"

    def test_create_issue_forbidden_without_permission(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        """User without REPORT_ISSUE permission receives 403 Forbidden."""
        _, client = app_and_client
        charlie = seeded_users["charlie"]  # 0 permissions
        headers = _auth_headers(charlie, test_db, test_config)

        payload = {
            "media_title": "Album",
            "artist": "Artist",
            "issue_type": "other",
            "problem_details": "Cannot report",
        }
        resp = client.post("/api/issues", json=payload, headers=headers)
        assert resp.status_code == 403
        assert "Permission denied: requires REPORT_ISSUE" in resp.json()["detail"]

    def test_list_issues_isolation_and_filters(self, app_and_client, test_db, test_config, seeded_users):
        """Lists issues with owner isolation for standard users and full view for admin/managers."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]
        manager = seeded_users["manager"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)
        manager_headers = _auth_headers(manager, test_db, test_config)

        # Alice creates 2 issues (1 open, 1 resolved)
        i1 = client.post(
            "/api/issues",
            json={"media_title": "A1", "artist": "Art1", "issue_type": "other", "problem_details": "det1"},
            headers=alice_headers,
        ).json()
        i2 = client.post(
            "/api/issues",
            json={"media_title": "A2", "artist": "Art2", "issue_type": "other", "problem_details": "det2"},
            headers=alice_headers,
        ).json()
        test_db.update_issue(i2["id"], {"status": "resolved"})

        # Bob creates 1 open issue
        client.post(
            "/api/issues",
            json={"media_title": "B1", "artist": "Art3", "issue_type": "other", "problem_details": "det3"},
            headers=bob_headers,
        )

        # 1. Alice sees only her 2 issues
        resp_alice = client.get("/api/issues", headers=alice_headers)
        assert resp_alice.status_code == 200
        alice_data = resp_alice.json()
        assert len(alice_data) == 2
        assert all(item["user_id"] == alice["id"] for item in alice_data)

        # Alice filters by status=open
        resp_alice_open = client.get("/api/issues?status=open", headers=alice_headers)
        assert resp_alice_open.status_code == 200
        assert len(resp_alice_open.json()) == 1
        assert resp_alice_open.json()[0]["id"] == i1["id"]

        # 2. Bob sees only his 1 issue
        resp_bob = client.get("/api/issues", headers=bob_headers)
        assert resp_bob.status_code == 200
        assert len(resp_bob.json()) == 1
        assert resp_bob.json()[0]["user_id"] == bob["id"]

        # 3. Admin sees all 3 issues
        resp_admin = client.get("/api/issues", headers=admin_headers)
        assert resp_admin.status_code == 200
        assert len(resp_admin.json()) == 3

        # 4. A non-admin holding MANAGE_REQUESTS is still a regular user: sees only their own (none)
        resp_manager = client.get("/api/issues", headers=manager_headers)
        assert resp_manager.status_code == 200
        assert resp_manager.json() == []

    def test_get_issue_by_id_boundaries(self, app_and_client, test_db, test_config, seeded_users):
        """Owner and admin can retrieve an issue; every other non-admin gets 404."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]
        manager = seeded_users["manager"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)
        manager_headers = _auth_headers(manager, test_db, test_config)

        created = client.post(
            "/api/issues",
            json={"media_title": "Target Issue", "artist": "Artist", "issue_type": "other", "problem_details": "detail"},
            headers=alice_headers,
        ).json()
        issue_id = created["id"]

        # Owner gets issue
        assert client.get(f"/api/issues/{issue_id}", headers=alice_headers).status_code == 200

        # Admin gets issue
        assert client.get(f"/api/issues/{issue_id}", headers=admin_headers).status_code == 200

        # MANAGE_REQUESTS does not grant visibility of other users' issues: 404
        assert client.get(f"/api/issues/{issue_id}", headers=manager_headers).status_code == 404

        # Bob (other user) gets 404, not 403
        resp_bob = client.get(f"/api/issues/{issue_id}", headers=bob_headers)
        assert resp_bob.status_code == 404

        # Non-existent issue returns 404
        assert client.get("/api/issues/issue-nonexistent", headers=admin_headers).status_code == 404

    def test_update_issue_workflows(self, app_and_client, test_db, test_config, seeded_users):
        """Only admins can update issues; owners, managers and third parties are refused."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]
        manager = seeded_users["manager"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)
        manager_headers = _auth_headers(manager, test_db, test_config)

        created = client.post(
            "/api/issues",
            json={"media_title": "Update Target", "artist": "Artist", "issue_type": "other", "problem_details": "v1"},
            headers=alice_headers,
        ).json()
        issue_id = created["id"]

        # 1-3. Owner, manager (MANAGE_REQUESTS) and third party are all forbidden
        for headers in (alice_headers, manager_headers, bob_headers):
            denied = client.put(
                f"/api/issues/{issue_id}",
                json={"status": "in_progress", "problem_details": "tampered"},
                headers=headers,
            )
            assert denied.status_code == 403
        unchanged = test_db.get_issue(issue_id)
        assert unchanged["status"] == "open"
        assert unchanged["problem_details"] == "v1"

        # 4. Admin updates status and details -> 200
        up_admin = client.put(
            f"/api/issues/{issue_id}",
            json={"status": "resolved", "problem_details": "fixed"},
            headers=admin_headers,
        )
        assert up_admin.status_code == 200
        assert up_admin.json()["status"] == "resolved"
        assert up_admin.json()["problem_details"] == "fixed"

        # 5. Non-existent returns 404
        assert client.put("/api/issues/issue-unknown", json={"status": "wont_fix"}, headers=admin_headers).status_code == 404

    def test_delete_issue_boundaries(self, app_and_client, test_db, test_config, seeded_users):
        """Only admins can delete issues; the owner and third parties receive 403."""
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        iss1 = client.post(
            "/api/issues",
            json={"media_title": "Del 1", "artist": "Art", "issue_type": "other", "problem_details": "d1"},
            headers=alice_headers,
        ).json()["id"]

        # Bob and the owner are both refused
        assert client.delete(f"/api/issues/{iss1}", headers=bob_headers).status_code == 403
        assert client.delete(f"/api/issues/{iss1}", headers=alice_headers).status_code == 403
        assert test_db.get_issue(iss1) is not None

        # Admin deletes Alice's issue -> 200
        del_admin = client.delete(f"/api/issues/{iss1}", headers=admin_headers)
        assert del_admin.status_code == 200
        assert del_admin.json()["status"] == "deleted"
        assert test_db.get_issue(iss1) is None

        # Non-existent delete returns 404
        assert client.delete("/api/issues/issue-none", headers=admin_headers).status_code == 404
