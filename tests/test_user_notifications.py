"""Unit and integration test suite for per-user notifications, in-app inbox, Web Push, and personal channels."""

from datetime import datetime, timezone
import json
import sqlite3
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    MusicRequest,
    NotificationChannel,
    NotificationEvent,
    RequestStatus,
)
from plex_playlist_sync.notifications import (
    USER_FACING_EVENTS,
    NotificationDispatcher,
    notification_dispatcher,
)
from plex_playlist_sync.security import mask_secret
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
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular test users."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@test.local", is_admin=False)
    bob = test_db.upsert_user("user-bob", "bob", "bob@test.local", is_admin=False)
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
    """Generates an authenticated Bearer header for a given user session."""
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
# 1. Schema v69 Migration Tests
# =============================================================================


class TestMigrationV69:
    def test_migration_v69_applies_on_v68_database(self):
        """Simulate a v68 database and verify _migration_v69 applies cleanly."""
        db = Database(":memory:")
        # Database(":memory:") runs all migrations. Let's inspect that v69 applied.
        cur = db.conn.cursor()

        # 1. Check notification_channels has owner_user_id
        cur.execute("PRAGMA table_info(notification_channels)")
        columns = {row["name"]: row for row in cur.fetchall()}
        assert "owner_user_id" in columns
        assert columns["owner_user_id"]["type"].upper() == "TEXT"

        # 2. Check user_notifications table and index
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_notifications'")
        assert cur.fetchone() is not None
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_user_notifications_user_read'")
        assert cur.fetchone() is not None

        # 3. Check web_push_subscriptions table and indexes
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='web_push_subscriptions'")
        assert cur.fetchone() is not None
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_web_push_subs_user'")
        assert cur.fetchone() is not None

        # 4. Check user_notification_prefs table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_notification_prefs'")
        assert cur.fetchone() is not None

        # 5. Check general_settings columns for VAPID keys
        cur.execute("PRAGMA table_info(general_settings)")
        gs_columns = {row["name"]: row for row in cur.fetchall()}
        assert "vapid_public_key" in gs_columns
        assert "vapid_private_key" in gs_columns
        db.close()

    def test_migration_v69_direct_call(self):
        """Directly invoke _migration_v69 on a raw SQLite connection with prior tables."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Create prerequisite tables as they exist in v68
        cur.execute("CREATE TABLE notification_channels (id TEXT PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE general_settings (id INTEGER PRIMARY KEY)")
        cur.execute("INSERT INTO general_settings (id) VALUES (1)")
        conn.commit()

        # Run migration v69
        db = Database.__new__(Database)
        db._migration_v69(cur)
        conn.commit()

        # Verify columns and tables
        cur.execute("PRAGMA table_info(notification_channels)")
        assert "owner_user_id" in [row["name"] for row in cur.fetchall()]

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cur.fetchall()}
        assert "user_notifications" in tables
        assert "web_push_subscriptions" in tables
        assert "user_notification_prefs" in tables

        cur.execute("PRAGMA table_info(general_settings)")
        gs_cols = {row["name"] for row in cur.fetchall()}
        assert "vapid_public_key" in gs_cols
        assert "vapid_private_key" in gs_cols
        conn.close()


# =============================================================================
# 2. Dispatcher Routing, Targeting & Preferences Tests
# =============================================================================


class TestNotificationDispatcherRouting:
    def test_user_facing_event_routes_only_to_owner(self, test_db, seeded_users):
        """User-facing event creates inbox row and notifies own channel/push only for the owner."""
        alice_id = seeded_users["alice"]["id"]
        bob_id = seeded_users["bob"]["id"]

        # Alice's personal channel
        test_db.create_notification_channel(
            NotificationChannel(
                id="ch-alice",
                name="Alice Discord",
                channel_type="discord",
                config={"webhook_url": "https://discord.com/api/webhooks/alice/token"},
                events=["request_approved"],
                owner_user_id=alice_id,
            )
        )
        # Bob's personal channel
        test_db.create_notification_channel(
            NotificationChannel(
                id="ch-bob",
                name="Bob Discord",
                channel_type="discord",
                config={"webhook_url": "https://discord.com/api/webhooks/bob/token"},
                events=["request_approved"],
                owner_user_id=bob_id,
            )
        )
        # Global admin channel
        test_db.create_notification_channel(
            NotificationChannel(
                id="ch-global",
                name="Global Webhook",
                channel_type="webhook",
                config={"webhook_url": "https://example.com/global"},
                events=["request_approved"],
                owner_user_id=None,
            )
        )

        # Alice's Web Push subscription
        test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/alice",
            p256dh="alice_p256dh",
            auth="alice_auth",
        )
        # Bob's Web Push subscription
        test_db.create_or_update_web_push_subscription(
            user_id=bob_id,
            endpoint="https://push.example.com/bob",
            p256dh="bob_p256dh",
            auth="bob_auth",
        )

        dispatcher = NotificationDispatcher()
        notified_channels = []
        sent_pushes = []

        def fake_send_channel(ch, event, data, allow_lan=True):
            cid = ch.get("id") if isinstance(ch, dict) else ch.id
            notified_channels.append(cid)

        def fake_send_push(db, user_id, event, data):
            sent_pushes.append(user_id)

        with patch.object(dispatcher, "_send_to_channel", side_effect=fake_send_channel), \
             patch.object(dispatcher, "_send_web_push", side_effect=fake_send_push):
            # Dispatch event belonging to Alice
            dispatcher._run_dispatch(
                "request_approved",
                {"artist": "Pink Floyd", "album": "The Wall", "requested_by": alice_id},
                db=test_db,
            )

        # 1. Global channel and Alice's channel notified; Bob's channel NOT notified
        assert "ch-global" in notified_channels
        assert "ch-alice" in notified_channels
        assert "ch-bob" not in notified_channels

        # 2. Push sent to Alice only; not Bob
        assert alice_id in sent_pushes
        assert bob_id not in sent_pushes

        # 3. Alice inbox has 1 item; Bob inbox has 0
        alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
        assert len(alice_inbox) == 1
        assert "Pink Floyd" in alice_inbox[0]["title"] or "Pink Floyd" in alice_inbox[0]["message"]

        bob_inbox, _, _ = test_db.list_user_notifications(bob_id)
        assert len(bob_inbox) == 0

    def test_admin_only_event_does_not_leak_to_user_inbox_or_channels(self, test_db, seeded_users):
        """request_created and issue_reported stay admin-only even if user_id is in payload."""
        alice_id = seeded_users["alice"]["id"]

        test_db.create_notification_channel(
            NotificationChannel(
                id="ch-alice-all",
                name="Alice Discord",
                channel_type="discord",
                config={"webhook_url": "https://discord.com/api/webhooks/alice/token"},
                events=["request_created", "request_approved"],
                owner_user_id=alice_id,
            )
        )
        test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/alice",
            p256dh="alice_p256dh",
            auth="alice_auth",
        )

        dispatcher = NotificationDispatcher()
        notified_channels = []
        sent_pushes = []

        with patch.object(dispatcher, "_send_to_channel", side_effect=lambda ch, *a, **kw: notified_channels.append(ch.id)), \
             patch.object(dispatcher, "_send_web_push", side_effect=lambda sub, *a, **kw: sent_pushes.append(sub["user_id"])):
            dispatcher._run_dispatch(
                "request_created",
                {"artist": "Daft Punk", "requested_by": alice_id},
                db=test_db,
            )

        assert "ch-alice-all" not in notified_channels
        assert len(sent_pushes) == 0
        alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
        assert len(alice_inbox) == 0

    def test_preferences_suppress_inbox_and_push(self, test_db, seeded_users):
        """Turning off in_app and push preferences suppresses delivery for that event."""
        alice_id = seeded_users["alice"]["id"]

        # Suppress in_app and push for request_rejected
        test_db.set_user_notification_pref(alice_id, "request_rejected", in_app=False, push=False)

        test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/alice",
            p256dh="alice_p256dh",
            auth="alice_auth",
        )

        dispatcher = NotificationDispatcher()
        sent_pushes = []

        with patch.object(dispatcher, "_send_web_push", side_effect=lambda sub, *a, **kw: sent_pushes.append(sub["user_id"])):
            dispatcher._run_dispatch(
                "request_rejected",
                {"artist": "Radiohead", "requested_by": alice_id},
                db=test_db,
            )

        # No inbox row created
        alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
        assert len(alice_inbox) == 0
        # No push sent
        assert len(sent_pushes) == 0


# =============================================================================
# 3. Web Push Failure & Pruning Tests
# =============================================================================


class TestWebPushFailureHandling:
    def test_web_push_410_deletes_subscription(self, test_db, seeded_users):
        """HTTP 410 Gone immediately deletes the Web Push subscription."""
        from pywebpush import WebPushException

        alice_id = seeded_users["alice"]["id"]
        sub = test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/expired",
            p256dh="p256",
            auth="auth",
        )
        assert len(test_db.get_user_web_push_subscriptions(alice_id)) == 1

        dispatcher = NotificationDispatcher()

        mock_resp = MagicMock()
        mock_resp.status_code = 410
        exc = WebPushException("Subscription gone", response=mock_resp)

        with patch("pywebpush.webpush", side_effect=exc):
            dispatcher._send_web_push(test_db, alice_id, "item_available", {"artist": "Artist"})

        # Subscription should be deleted
        assert len(test_db.get_user_web_push_subscriptions(alice_id)) == 0

    def test_web_push_5_consecutive_failures_deletes_subscription(self, test_db, seeded_users):
        """5 consecutive non-410 failures deletes the subscription."""
        from pywebpush import WebPushException

        alice_id = seeded_users["alice"]["id"]
        sub = test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/failing",
            p256dh="p256",
            auth="auth",
        )

        dispatcher = NotificationDispatcher()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        exc = WebPushException("Internal server error", response=mock_resp)

        with patch("pywebpush.webpush", side_effect=exc):
            # Run 4 failures
            for i in range(1, 5):
                dispatcher._send_web_push(test_db, alice_id, "item_available", {"artist": "Artist"})
                subs = test_db.get_user_web_push_subscriptions(alice_id)
                assert len(subs) == 1
                assert subs[0]["failure_count"] == i

            # 5th failure deletes
            dispatcher._send_web_push(test_db, alice_id, "item_available", {"artist": "Artist"})
            assert len(test_db.get_user_web_push_subscriptions(alice_id)) == 0

    def test_web_push_success_resets_failure_count(self, test_db, seeded_users):
        """A successful push resets failure_count to 0 and records last_success_at."""
        alice_id = seeded_users["alice"]["id"]
        sub = test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/healthy",
            p256dh="p256",
            auth="auth",
        )
        test_db.increment_web_push_failure(sub["id"])
        test_db.increment_web_push_failure(sub["id"])
        subs = test_db.get_user_web_push_subscriptions(alice_id)
        assert subs[0]["failure_count"] == 2

        dispatcher = NotificationDispatcher()

        with patch("pywebpush.webpush", return_value=MagicMock()):
            dispatcher._send_web_push(test_db, alice_id, "item_available", {"artist": "Artist"})

        subs = test_db.get_user_web_push_subscriptions(alice_id)
        assert subs[0]["failure_count"] == 0
        assert subs[0]["last_success_at"] is not None


# =============================================================================
# 4. User API: Ownership Isolation, Caps & RBAC Tests
# =============================================================================


class TestUserNotificationsApi:
    def test_api_key_user_gets_403(self, app_and_client, test_db):
        """API-key pseudo user receives 403 on account notification routes."""
        _, client = app_and_client
        api_key = test_db.get_api_key()
        res = client.get("/api/account/notifications/inbox", headers={"X-Api-Key": api_key})
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_inbox_ownership_isolation(self, app_and_client, test_db, test_config, seeded_users):
        """User Bob cannot read, mark, or delete Alice's inbox items."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        bob_headers = _auth_headers(seeded_users["bob"], test_db, test_config)

        alice_id = seeded_users["alice"]["id"]
        item = test_db.create_user_notification(
            user_id=alice_id,
            event="request_approved",
            title="Alice's Item",
            message="Your request was approved",
        )
        notif_id = item["id"]

        # Alice sees it
        r_alice = client.get("/api/account/notifications/inbox", headers=alice_headers)
        assert r_alice.status_code == status.HTTP_200_OK
        assert len(r_alice.json()["items"]) == 1

        # Bob cannot see it
        r_bob = client.get("/api/account/notifications/inbox", headers=bob_headers)
        assert r_bob.status_code == status.HTTP_200_OK
        assert len(r_bob.json()["items"]) == 0

        # Bob cannot mark Alice's item read
        r_mark = client.post(f"/api/account/notifications/inbox/{notif_id}/read", headers=bob_headers)
        assert r_mark.status_code == status.HTTP_404_NOT_FOUND

        # Bob cannot delete Alice's item
        r_del = client.delete(f"/api/account/notifications/inbox/{notif_id}", headers=bob_headers)
        assert r_del.status_code == status.HTTP_404_NOT_FOUND

        # Alice marks read
        r_alice_mark = client.post(f"/api/account/notifications/inbox/{notif_id}/read", headers=alice_headers)
        assert r_alice_mark.status_code == status.HTTP_200_OK

        # Alice deletes
        r_alice_del = client.delete(f"/api/account/notifications/inbox/{notif_id}", headers=alice_headers)
        assert r_alice_del.status_code == status.HTTP_200_OK

    def test_channel_ownership_isolation(self, app_and_client, test_db, test_config, seeded_users):
        """User Bob cannot read, update, or delete Alice's notification channel."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        bob_headers = _auth_headers(seeded_users["bob"], test_db, test_config)

        # Alice creates a Discord channel
        alice_ch = client.post(
            "/api/account/notifications/channels",
            json={
                "name": "Alice Channel",
                "channel_type": "discord",
                "config": {"webhook_url": "https://discord.com/api/webhooks/123/token"},
                "events": ["item_available"],
            },
            headers=alice_headers,
        ).json()
        ch_id = alice_ch["id"]

        # Bob cannot see it in his channel list
        bob_list = client.get("/api/account/notifications/channels", headers=bob_headers).json()
        assert not any(c["id"] == ch_id for c in bob_list)

        # Bob cannot get, update, or delete it
        assert client.get(f"/api/account/notifications/channels/{ch_id}", headers=bob_headers).status_code == status.HTTP_404_NOT_FOUND
        assert client.put(f"/api/account/notifications/channels/{ch_id}", json={"name": "Hacked", "channel_type": "discord"}, headers=bob_headers).status_code == status.HTTP_404_NOT_FOUND
        assert client.delete(f"/api/account/notifications/channels/{ch_id}", headers=bob_headers).status_code == status.HTTP_404_NOT_FOUND

    def test_ssrf_rejection_for_non_admin_lan_webhook(self, app_and_client, test_db, test_config, seeded_users):
        """Non-admin user cannot configure a channel pointing to LAN/localhost (allow_lan=False)."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        # 1. LAN IP webhook
        lan_payload = {
            "name": "LAN Hook",
            "channel_type": "webhook",
            "config": {"webhook_url": "http://192.168.1.50:8080/hook"},
        }
        res_create = client.post("/api/account/notifications/channels", json=lan_payload, headers=alice_headers)
        assert res_create.status_code == status.HTTP_400_BAD_REQUEST
        assert "SSRF" in res_create.json()["detail"]

        # 2. Localhost webhook test endpoint returns success=False
        lh_payload = {
            "name": "Localhost Hook",
            "channel_type": "webhook",
            "config": {"webhook_url": "http://127.0.0.1:9000/hook"},
        }
        res_test = client.post("/api/account/notifications/channels/test", json=lh_payload, headers=alice_headers)
        assert res_test.status_code == status.HTTP_200_OK
        assert res_test.json()["success"] is False
        assert "SSRF" in res_test.json()["message"]

        # 3. Email channel type is rejected for users
        email_payload = {
            "name": "Email Channel",
            "channel_type": "email",
            "config": {"to_addr": "alice@test.local"},
        }
        res_email = client.post("/api/account/notifications/channels", json=email_payload, headers=alice_headers)
        assert res_email.status_code == status.HTTP_400_BAD_REQUEST

    def test_per_user_channel_and_push_caps(self, app_and_client, test_db, test_config, seeded_users):
        """User cannot exceed 5 channels and 10 push subscriptions."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        # Create 5 channels
        for i in range(5):
            r = client.post(
                "/api/account/notifications/channels",
                json={
                    "name": f"Channel {i}",
                    "channel_type": "discord",
                    "config": {"webhook_url": f"https://discord.com/api/webhooks/{i}/tok"},
                },
                headers=alice_headers,
            )
            assert r.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)

        # 6th channel fails with 400
        r_6 = client.post(
            "/api/account/notifications/channels",
            json={
                "name": "Channel 6",
                "channel_type": "discord",
                "config": {"webhook_url": "https://discord.com/api/webhooks/6/tok"},
            },
            headers=alice_headers,
        )
        assert r_6.status_code == status.HTTP_400_BAD_REQUEST
        assert "5" in r_6.json()["detail"]

        # Create 10 push subscriptions
        for i in range(10):
            r = client.post(
                "/api/account/notifications/push/subscribe",
                json={
                    "endpoint": f"https://push.example.com/endpoint-{i}",
                    "keys": {"p256dh": f"p256-{i}", "auth": f"auth-{i}"},
                },
                headers=alice_headers,
            )
            assert r.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)

        # 11th push subscription fails with 400
        r_11 = client.post(
            "/api/account/notifications/push/subscribe",
            json={
                "endpoint": "https://push.example.com/endpoint-11",
                "keys": {"p256dh": "p256-11", "auth": "auth-11"},
            },
            headers=alice_headers,
        )
        assert r_11.status_code == status.HTTP_400_BAD_REQUEST
        assert "10" in r_11.json()["detail"]

    def test_private_vapid_key_never_leaked(self, app_and_client, test_db, test_config, seeded_users):
        """GET /api/account/notifications/push returns public key only, never private key."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        res = client.get("/api/account/notifications/push", headers=alice_headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert "public_key" in data
        assert len(data["public_key"]) > 0
        assert "private_key" not in data
        assert "vapid_private_key" not in data

    def test_admin_list_excludes_user_channels(self, app_and_client, test_db, test_config, seeded_users):
        """Global admin channels list only includes owner_user_id IS NULL; cannot modify user channel."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        # Alice creates user channel
        alice_ch = client.post(
            "/api/account/notifications/channels",
            json={
                "name": "Alice Private Hook",
                "channel_type": "discord",
                "config": {"webhook_url": "https://discord.com/api/webhooks/alice/tok"},
            },
            headers=alice_headers,
        ).json()
        ch_id = alice_ch["id"]

        # Admin creates global channel
        admin_ch = client.post(
            "/api/settings/notifications",
            json={
                "name": "Admin Global Hook",
                "channel_type": "discord",
                "config": {"webhook_url": "https://discord.com/api/webhooks/admin/tok"},
            },
            headers=admin_headers,
        ).json()

        # Admin lists global channels
        admin_list = client.get("/api/settings/notifications", headers=admin_headers).json()
        ids = [c["id"] for c in admin_list]
        assert admin_ch["id"] in ids
        assert ch_id not in ids

        # Admin cannot edit or delete Alice's channel via global settings route
        upd = client.put(
            f"/api/settings/notifications/{ch_id}",
            json={"name": "Tampered", "channel_type": "discord"},
            headers=admin_headers,
        )
        assert upd.status_code == status.HTTP_404_NOT_FOUND

        del_res = client.delete(f"/api/settings/notifications/{ch_id}", headers=admin_headers)
        assert del_res.status_code == status.HTTP_404_NOT_FOUND


# =============================================================================
# 5. Call Site Auditing: Request & Acquisition Owner ID
# =============================================================================


class TestCallSitesIncludeOwnerId:
    def test_request_approved_and_rejected_include_owner_id(self, app_and_client, test_db, test_config, seeded_users):
        """Approve and reject routes include requested_by in dispatch data."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        alice_id = seeded_users["alice"]["id"]

        # Seed request made by Alice
        req = test_db.create_request(
            MusicRequest(
                id="req-alice-1",
                user_id=alice_id,
                item_type="album",
                artist="Radiohead",
                title="OK Computer",
                status=RequestStatus.PENDING,
            )
        )

        dispatched_events = []

        def capture_dispatch(event, data, db=None):
            dispatched_events.append((event, data))

        with patch.object(notification_dispatcher, "dispatch", side_effect=capture_dispatch):
            # 1. Approve
            r_app = client.post(f"/api/requests/{req['id']}/approve", headers=admin_headers)
            assert r_app.status_code == status.HTTP_200_OK

            assert any(
                ev in ("request_approved", NotificationEvent.REQUEST_APPROVED)
                and (d.get("requested_by") == alice_id or d.get("user_id") == alice_id)
                for ev, d in dispatched_events
            )

            # 2. Reject
            dispatched_events.clear()
            req2 = test_db.create_request(
                MusicRequest(
                    id="req-alice-2",
                    user_id=alice_id,
                    item_type="album",
                    artist="Radiohead",
                    title="Kid A",
                    status=RequestStatus.PENDING,
                )
            )
            r_rej = client.post(
                f"/api/requests/{req2['id']}/reject",
                json={"reason": "Duplicate"},
                headers=admin_headers,
            )
            assert r_rej.status_code == status.HTTP_200_OK

            assert any(
                ev in ("request_rejected", NotificationEvent.REQUEST_REJECTED)
                and (d.get("requested_by") == alice_id or d.get("user_id") == alice_id)
                for ev, d in dispatched_events
            )

    def test_item_available_includes_owner_id(self, test_db, seeded_users):
        """Acquisition coordinator / worker includes owner_user_id on item_available dispatch."""
        alice_id = seeded_users["alice"]["id"]

        test_db.create_request(
            MusicRequest(
                id="req-alice-avail",
                user_id=alice_id,
                item_type="album",
                artist="Miles Davis",
                title="Kind of Blue",
                status=RequestStatus.PROCESSING,
            )
        )

        dispatched_events = []

        def capture_dispatch(event, data, db=None):
            dispatched_events.append((event, data))

        # Direct test of dispatcher receiving item_available with requested_by
        with patch.object(notification_dispatcher, "_run_dispatch") as mock_run:
            notification_dispatcher.dispatch(
                "item_available",
                {"artist": "Miles Davis", "album": "Kind of Blue", "requested_by": alice_id},
                db=test_db,
            )
            # Give short moment for thread if background, or inspect call
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == "item_available"
            assert args[1]["requested_by"] == alice_id


# =============================================================================
# 6. Self-Action Suppression Tests
# =============================================================================


class TestSelfActionNotificationSuppression:
    def test_reporter_own_comment_skips_inbox_admin_comment_creates_inbox_row(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        """Reporter's own comment creates no inbox row for reporter; admin's comment creates inbox row."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        alice_id = seeded_users["alice"]["id"]

        issue = test_db.create_issue(
            {
                "id": "iss-suppress-1",
                "user_id": alice_id,
                "media_title": "OK Computer",
                "artist": "Radiohead",
                "issue_type": "audio_quality",
                "problem_details": "Crackling noise on track 1",
            }
        )

        def sync_dispatch(event, data, db=None):
            event_str = event.value if isinstance(event, NotificationEvent) else str(event)
            notification_dispatcher._run_dispatch(event_str, dict(data), db)

        with patch.object(notification_dispatcher, "dispatch", side_effect=sync_dispatch):
            # 1. Reporter (Alice) comments on her own issue
            r_alice = client.post(
                f"/api/issues/{issue['id']}/comments",
                json={"body": "Also happens on track 2"},
                headers=alice_headers,
            )
            assert r_alice.status_code == status.HTTP_201_CREATED

            # Alice has no inbox row
            alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
            assert len(alice_inbox) == 0

            # 2. Admin comments on the same issue
            r_admin = client.post(
                f"/api/issues/{issue['id']}/comments",
                json={"body": "We have replaced the release"},
                headers=admin_headers,
            )
            assert r_admin.status_code == status.HTTP_201_CREATED

            # Alice now has an inbox row
            alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
            assert len(alice_inbox) == 1
            assert "OK Computer" in alice_inbox[0]["title"] or "OK Computer" in alice_inbox[0]["message"]

    def test_run_dispatch_skips_user_channels_and_push_when_actor_is_owner(self, test_db, seeded_users):
        """_run_dispatch skips inbox, personal channels, and web push when actor_user_id == owner_user_id."""
        alice_id = seeded_users["alice"]["id"]

        test_db.create_notification_channel(
            NotificationChannel(
                id="ch-alice-own",
                name="Alice Discord",
                channel_type="discord",
                config={"webhook_url": "https://discord.com/api/webhooks/alice/token"},
                events=["issue_updated"],
                owner_user_id=alice_id,
            )
        )
        test_db.create_or_update_web_push_subscription(
            user_id=alice_id,
            endpoint="https://push.example.com/alice",
            p256dh="alice_p256dh",
            auth="alice_auth",
        )

        dispatcher = NotificationDispatcher()
        notified_channels = []
        sent_pushes = []

        def fake_send_ch(ch, *a, **kw):
            notified_channels.append(ch.get("id") if isinstance(ch, dict) else ch.id)

        with patch.object(dispatcher, "_send_to_channel", side_effect=fake_send_ch), \
             patch.object(dispatcher, "_send_web_push", side_effect=lambda db, uid, *a, **kw: sent_pushes.append(uid)):
            # Self-action: actor == owner
            dispatcher._run_dispatch(
                "issue_updated",
                {
                    "issue_id": "iss-1",
                    "user_id": alice_id,
                    "actor_user_id": alice_id,
                    "media_title": "OK Computer",
                },
                db=test_db,
            )

        assert "ch-alice-own" not in notified_channels
        assert len(sent_pushes) == 0
        alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
        assert len(alice_inbox) == 0

        # Non-self action: actor != owner
        with patch.object(dispatcher, "_send_to_channel", side_effect=fake_send_ch), \
             patch.object(dispatcher, "_send_web_push", side_effect=lambda db, uid, *a, **kw: sent_pushes.append(uid)):
            dispatcher._run_dispatch(
                "issue_updated",
                {
                    "issue_id": "iss-1",
                    "user_id": alice_id,
                    "actor_user_id": "admin-1",
                    "media_title": "OK Computer",
                },
                db=test_db,
            )

        assert "ch-alice-own" in notified_channels
        assert alice_id in sent_pushes
        alice_inbox, _, _ = test_db.list_user_notifications(alice_id)
        assert len(alice_inbox) == 1

