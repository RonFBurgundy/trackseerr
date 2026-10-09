"""Comprehensive tests for Music Requests lifecycle, quotas, and admin workflows."""

import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_lidarr_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import MusicRequest, RequestStatus, UserPermission
from trackseerr.storage import Database


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
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    bob = test_db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice, "bob": bob}


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
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# 1. Storage Tier Tests
# =============================================================================


class TestMusicRequestsStorage:
    def test_create_and_get_request(self, test_db, seeded_users):
        user = seeded_users["alice"]
        req = MusicRequest(
            id="req-1",
            user_id=user["id"],
            item_type="album",
            title="Abbey Road",
            artist="The Beatles",
            album="Abbey Road",
            cover_url="http://example.com/cover.jpg",
            preview_url="http://example.com/preview.mp3",
            status=RequestStatus.PENDING,
            release_date="1969-09-26",
            foreign_id="itunes:album:123",
        )
        created = test_db.create_request(req)
        assert created["id"] == "req-1"
        assert created["title"] == "Abbey Road"
        assert created["artist"] == "The Beatles"
        assert created["status"] == "pending"
        assert created["username"] == "alice"

        fetched = test_db.get_request("req-1")
        assert fetched is not None
        assert fetched["id"] == "req-1"
        assert fetched["username"] == "alice"

    def test_list_requests_filtering(self, test_db, seeded_users):
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]

        req1 = MusicRequest(
            id="req-1",
            user_id=alice["id"],
            item_type="album",
            title="Album A",
            artist="Artist 1",
            status=RequestStatus.PENDING,
        )
        req2 = MusicRequest(
            id="req-2",
            user_id=alice["id"],
            item_type="track",
            title="Track B",
            artist="Artist 2",
            status=RequestStatus.PROCESSING,
        )
        req3 = MusicRequest(
            id="req-3",
            user_id=bob["id"],
            item_type="album",
            title="Album C",
            artist="Artist 3",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req1)
        test_db.create_request(req2)
        test_db.create_request(req3)

        # All requests
        all_reqs = test_db.list_requests()
        assert len(all_reqs) == 3

        # Filter by user
        alice_reqs = test_db.list_requests(user_id=alice["id"])
        assert len(alice_reqs) == 2

        # Filter by status
        pending_reqs = test_db.list_requests(status="pending")
        assert len(pending_reqs) == 2

        # Filter by user and status
        alice_pending = test_db.list_requests(user_id=alice["id"], status="pending")
        assert len(alice_pending) == 1
        assert alice_pending[0]["id"] == "req-1"

    def test_update_request_status(self, test_db, seeded_users):
        alice = seeded_users["alice"]
        req = MusicRequest(
            id="req-update",
            user_id=alice["id"],
            item_type="album",
            title="Test",
            artist="Artist",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        ok = test_db.update_request_status("req-update", RequestStatus.PROCESSING)
        assert ok is True
        fetched = test_db.get_request("req-update")
        assert fetched["status"] == "processing"

        ok = test_db.update_request_status("req-update", "available")
        assert ok is True
        fetched = test_db.get_request("req-update")
        assert fetched["status"] == "available"

    def test_delete_request(self, test_db, seeded_users):
        alice = seeded_users["alice"]
        req = MusicRequest(
            id="req-del",
            user_id=alice["id"],
            item_type="album",
            title="Test Del",
            artist="Artist",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)
        assert test_db.get_request("req-del") is not None

        deleted = test_db.delete_request("req-del")
        assert deleted is True
        assert test_db.get_request("req-del") is None

    def test_find_matching_processing_requests(self, test_db, seeded_users):
        alice = seeded_users["alice"]
        req_album = MusicRequest(
            id="req-match-1",
            user_id=alice["id"],
            item_type="album",
            title="Rumours",
            artist="Fleetwood Mac",
            album="Rumours",
            status=RequestStatus.PROCESSING,
        )
        req_track = MusicRequest(
            id="req-match-2",
            user_id=alice["id"],
            item_type="track",
            title="Dreams",
            artist="Fleetwood Mac",
            album="Rumours",
            status=RequestStatus.PENDING,
        )
        req_done = MusicRequest(
            id="req-match-3",
            user_id=alice["id"],
            item_type="track",
            title="Go Your Own Way",
            artist="Fleetwood Mac",
            album="Rumours",
            status=RequestStatus.AVAILABLE,
        )
        test_db.create_request(req_album)
        test_db.create_request(req_track)
        test_db.create_request(req_done)

        # Match by artist and album (case-insensitive)
        matches = test_db.find_matching_processing_requests(artist="fleetwood mac", album="rumours")
        match_ids = [m["id"] for m in matches]
        assert "req-match-1" in match_ids
        assert "req-match-2" in match_ids
        assert "req-match-3" not in match_ids  # already available

        # Match by track title
        track_matches = test_db.find_matching_processing_requests(
            artist="Fleetwood Mac", title="Dreams"
        )
        assert len(track_matches) >= 1
        assert track_matches[0]["id"] == "req-match-2"

    def test_user_foreign_key_cascade_deletes_requests(self, test_db, seeded_users):
        alice = seeded_users["alice"]
        req = MusicRequest(
            id="req-cascade",
            user_id=alice["id"],
            item_type="album",
            title="Cascade Album",
            artist="Artist",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)
        assert test_db.get_request("req-cascade") is not None

        # Delete user
        test_db.delete_user(alice["id"])
        assert test_db.get_request("req-cascade") is None


# =============================================================================
# 2. REST API & Workflow Tests
# =============================================================================


class TestMusicRequestsAPI:
    def test_create_request_as_regular_user_pending(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        payload = {
            "item_type": "album",
            "title": "OK Computer",
            "artist": "Radiohead",
            "album": "OK Computer",
            "cover_url": "http://example.com/okc.jpg",
            "release_date": "1997-05-21",
            "foreign_id": "itunes:album:radiohead-okc",
            "preview_url": "http://example.com/okc-preview.mp3",
        }
        resp = client.post("/api/requests", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "OK Computer"
        assert data["artist"] == "Radiohead"
        assert data["status"] == "pending"
        assert data["user_id"] == alice["id"]
        assert data["username"] == "alice"

    def test_create_request_as_admin_auto_approves(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        payload = {
            "item_type": "album",
            "title": "Kid A",
            "artist": "Radiohead",
        }
        resp = client.post("/api/requests", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "processing"

    def test_create_request_with_auto_approve_config(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        test_config.auto_approve_requests = True
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        payload = {
            "item_type": "track",
            "title": "Karma Police",
            "artist": "Radiohead",
        }
        resp = client.post("/api/requests", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "processing"

    def test_user_isolation(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Alice creates a request
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Alice Album", "artist": "Artist A"},
            headers=alice_headers,
        )

        # Bob creates a request
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Bob Album", "artist": "Artist B"},
            headers=bob_headers,
        )

        # Alice should only see her request
        alice_resp = client.get("/api/requests", headers=alice_headers)
        assert alice_resp.status_code == 200
        alice_data = alice_resp.json()
        assert alice_data["count"] == 1
        assert alice_data["requests"][0]["title"] == "Alice Album"

        # Bob should only see his request
        bob_resp = client.get("/api/requests", headers=bob_headers)
        assert bob_resp.status_code == 200
        bob_data = bob_resp.json()
        assert bob_data["count"] == 1
        assert bob_data["requests"][0]["title"] == "Bob Album"

        # Admin should see both requests
        admin_resp = client.get("/api/requests", headers=admin_headers)
        assert admin_resp.status_code == 200
        admin_data = admin_resp.json()
        assert admin_data["count"] == 2

    def test_quota_enforcement(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        test_db.update_account_settings({"default_quota_albums": 2})
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # 1st request - ok
        resp1 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 1", "artist": "Artist 1"},
            headers=headers,
        )
        assert resp1.status_code == 201

        # 2nd request - ok
        resp2 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 2", "artist": "Artist 2"},
            headers=headers,
        )
        assert resp2.status_code == 201

        # 3rd request - quota exceeded (quota=2)
        resp3 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Album 3", "artist": "Artist 3"},
            headers=headers,
        )
        assert resp3.status_code == 400
        assert "quota reached" in resp3.json()["detail"].lower()

    def test_duplicate_request_rejected(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # First request
        resp1 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Duplicate Album", "artist": "Duplicate Artist"},
            headers=headers,
        )
        assert resp1.status_code == 201

        # Duplicate request
        resp2 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Duplicate Album", "artist": "Duplicate Artist"},
            headers=headers,
        )
        assert resp2.status_code == 409
        assert "already submitted" in resp2.json()["detail"].lower()

    def test_admin_approve_and_reject_workflows(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        admin = seeded_users["admin"]
        alice_headers = _auth_headers(alice, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Alice creates a pending request
        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Workflow Album", "artist": "Artist"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]

        # Alice cannot approve
        non_admin_approve = client.post(f"/api/requests/{req_id}/approve", headers=alice_headers)
        assert non_admin_approve.status_code == 403

        # Alice cannot reject
        non_admin_reject = client.post(f"/api/requests/{req_id}/reject", headers=alice_headers)
        assert non_admin_reject.status_code == 403

        # Admin approves
        admin_approve = client.post(f"/api/requests/{req_id}/approve", headers=admin_headers)
        assert admin_approve.status_code == 200
        assert admin_approve.json()["status"] == "processing"

        # Admin rejects
        admin_reject = client.post(f"/api/requests/{req_id}/reject", headers=admin_headers)
        assert admin_reject.status_code == 200
        assert admin_reject.json()["status"] == "rejected"

    def test_delete_request_permissions(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Alice creates a pending request
        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Alice Pending", "artist": "Artist"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]

        # Bob cannot delete Alice's request (404: existence is not revealed)
        bob_del = client.delete(f"/api/requests/{req_id}", headers=bob_headers)
        assert bob_del.status_code == 404

        # Alice can delete her pending request
        alice_del = client.delete(f"/api/requests/{req_id}", headers=alice_headers)
        assert alice_del.status_code == 200
        assert alice_del.json()["status"] == "deleted"

    def test_cannot_delete_processing_request_as_standard_user(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        admin = seeded_users["admin"]
        alice_headers = _auth_headers(alice, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Create request and approve it
        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "To Process", "artist": "Artist"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]
        client.post(f"/api/requests/{req_id}/approve", headers=admin_headers)

        # Alice cannot delete once processing
        alice_del = client.delete(f"/api/requests/{req_id}", headers=alice_headers)
        assert alice_del.status_code == 400

        # Admin can delete processing request
        admin_del = client.delete(f"/api/requests/{req_id}", headers=admin_headers)
        assert admin_del.status_code == 200
        assert admin_del.json()["status"] == "deleted"

    def test_webhook_auto_fulfillment_on_lidarr_event(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        admin = seeded_users["admin"]
        alice = seeded_users["alice"]
        alice_headers = _auth_headers(alice, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Alice requests an album
        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "In Rainbows", "artist": "Radiohead"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]

        # Lidarr download webhook ping received
        lidarr_payload = {
            "eventType": "Download",
            "artist": {"name": "Radiohead"},
            "albums": [{"title": "In Rainbows"}],
        }

        webhook_resp = client.post(
            "/api/sync/webhook",
            json=lidarr_payload,
            headers=admin_headers,
        )
        assert webhook_resp.status_code == 200
        assert webhook_resp.json().get("fulfilled_requests") == 1

        # Verify request is now AVAILABLE
        req_row = test_db.get_request(req_id)
        assert req_row["status"] == "available"

    def test_manager_can_list_all_requests(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        bob = seeded_users["bob"]
        admin = seeded_users["admin"]

        test_db.upsert_user("user-mgr", "mgr", "mgr@plex.tv", is_admin=False)
        test_db.update_user_governance("user-mgr", permissions=int(UserPermission.REQUEST | UserPermission.MANAGE_REQUESTS))
        manager = test_db.get_user("user-mgr")

        alice_headers = _auth_headers(alice, test_db, test_config)
        bob_headers = _auth_headers(bob, test_db, test_config)
        mgr_headers = _auth_headers(manager, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        # Alice creates a request
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Alice Track", "artist": "Artist A"},
            headers=alice_headers,
        )
        # Bob creates a request
        client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Bob Track", "artist": "Artist B"},
            headers=bob_headers,
        )

        # Alice sees only her own (1)
        res_alice = client.get("/api/requests", headers=alice_headers)
        assert res_alice.status_code == 200
        assert res_alice.json()["count"] == 1

        # Manager sees all requests (2)
        res_mgr = client.get("/api/requests", headers=mgr_headers)
        assert res_mgr.status_code == 200
        assert res_mgr.json()["count"] == 2

        # Admin sees all requests (2)
        res_admin = client.get("/api/requests", headers=admin_headers)
        assert res_admin.status_code == 200
        assert res_admin.json()["count"] == 2

    def test_manager_can_approve_and_reject_and_sets_actor(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        test_db.upsert_user("user-mgr2", "mgr2", "mgr2@plex.tv", is_admin=False)
        test_db.update_user_governance("user-mgr2", permissions=int(UserPermission.REQUEST | UserPermission.MANAGE_REQUESTS))
        manager = test_db.get_user("user-mgr2")

        alice_headers = _auth_headers(alice, test_db, test_config)
        mgr_headers = _auth_headers(manager, test_db, test_config)

        # Alice creates request 1
        res1 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "To Approve", "artist": "Artist 1"},
            headers=alice_headers,
        )
        req1_id = res1.json()["id"]

        # Manager approves
        appr_res = client.post(f"/api/requests/{req1_id}/approve", headers=mgr_headers)
        assert appr_res.status_code == 200
        assert appr_res.json()["status"] == "processing"

        # Check item history / event actor
        events = test_db.conn.execute(
            "SELECT event, actor_user_id FROM item_events WHERE request_id = ? ORDER BY id",
            (req1_id,),
        ).fetchall()
        approve_event = next((e for e in events if e["event"] == "request_approved"), None)
        assert approve_event is not None
        assert approve_event["actor_user_id"] == "user-mgr2"

        # Alice creates request 2
        res2 = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "To Reject", "artist": "Artist 2"},
            headers=alice_headers,
        )
        req2_id = res2.json()["id"]

        # Manager rejects
        rej_res = client.post(f"/api/requests/{req2_id}/reject", headers=mgr_headers)
        assert rej_res.status_code == 200
        assert rej_res.json()["status"] == "rejected"

        events2 = test_db.conn.execute(
            "SELECT event, actor_user_id FROM item_events WHERE request_id = ? ORDER BY id",
            (req2_id,),
        ).fetchall()
        reject_event = next((e for e in events2 if e["event"] == "request_declined"), None)
        assert reject_event is not None
        assert reject_event["actor_user_id"] == "user-mgr2"

    def test_manager_cannot_retry_or_delete_others(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        admin = seeded_users["admin"]
        test_db.upsert_user("user-mgr3", "mgr3", "mgr3@plex.tv", is_admin=False)
        test_db.update_user_governance("user-mgr3", permissions=int(UserPermission.REQUEST | UserPermission.MANAGE_REQUESTS))
        manager = test_db.get_user("user-mgr3")

        alice_headers = _auth_headers(alice, test_db, test_config)
        mgr_headers = _auth_headers(manager, test_db, test_config)
        admin_headers = _auth_headers(admin, test_db, test_config)

        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Retry Album", "artist": "Artist"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]

        # Manager cannot delete Alice's request (404 to avoid leaking existence)
        del_mgr = client.delete(f"/api/requests/{req_id}", headers=mgr_headers)
        assert del_mgr.status_code == 404

        # Manager cannot retry
        retry_mgr = client.post(f"/api/requests/{req_id}/retry", headers=mgr_headers)
        assert retry_mgr.status_code == 403

        # Admin CAN retry (admin only)
        retry_admin = client.post(f"/api/requests/{req_id}/retry", headers=admin_headers)
        assert retry_admin.status_code == 200

        # Admin CAN delete
        del_admin = client.delete(f"/api/requests/{req_id}", headers=admin_headers)
        assert del_admin.status_code == 200

    def test_forwarded_principal_with_manage_requests_bit_is_403(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        alice_headers = _auth_headers(alice, test_db, test_config)

        # Alice creates a pending request
        res = client.post(
            "/api/requests",
            json={"item_type": "album", "title": "Signed Album", "artist": "Artist"},
            headers=alice_headers,
        )
        req_id = res.json()["id"]

        secret = "s" * 40
        test_config.role = "core"
        test_config.internal_core_secret = secret

        # Create a user in DB who has MANAGE_REQUESTS permission
        test_db.upsert_user("99901", "fwd_manager", "fwd@example.com", is_admin=False)
        test_db.update_user_governance("99901", permissions=int(UserPermission.REQUEST | UserPermission.MANAGE_REQUESTS))

        # Sign request as forwarded from gateway
        signed_headers = internal_auth.sign_assertion(
            secret,
            "POST",
            f"/api/requests/{req_id}/approve",
            user_id="99901",
            user_name="fwd_manager",
        )

        # Gateway-signed assertion must be refused on approve (403)
        res_approve = client.post(f"/api/requests/{req_id}/approve", headers=signed_headers)
        assert res_approve.status_code == 403

        # And reject must also be refused (403)
        signed_reject_headers = internal_auth.sign_assertion(
            secret,
            "POST",
            f"/api/requests/{req_id}/reject",
            user_id="99901",
            user_name="fwd_manager",
        )
        res_reject = client.post(f"/api/requests/{req_id}/reject", headers=signed_reject_headers)
        assert res_reject.status_code == 403

        # And forwarded user cannot list other users' requests (scoped to own)
        signed_list_headers = internal_auth.sign_assertion(
            secret,
            "GET",
            "/api/requests",
            user_id="99901",
            user_name="fwd_manager",
        )
        res_list = client.get("/api/requests", headers=signed_list_headers)
        assert res_list.status_code == 200
        # 99901 has no requests of their own, so count is 0
        assert res_list.json()["count"] == 0
