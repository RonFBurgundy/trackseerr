"""Comprehensive unit and integration test suite for Outbound Notification & Webhooks Engine."""

from tests.audio_fixtures import write_flac
from datetime import datetime, timezone
import json
from pathlib import Path
import smtplib
import threading
import time
from unittest.mock import ANY, MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import httpx
import pytest

from trackseerr.item_history import GrabTrigger
from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.backlog_worker import RSSSyncWorker
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
    NotificationChannel,
    NotificationChannelType,
    NotificationEvent,
    QualityProfile,
    QualityProfileItem,
    RequestStatus,
)
from trackseerr.notifications import (
    NotificationDispatcher,
    format_notification,
    notification_dispatcher,
)
from trackseerr.security import mask_secret
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
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


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
# 1. Storage & Migration v12 Tests
# =============================================================================


class TestNotificationStorage:
    def test_migration_v12_creates_table_and_indexes(self, test_db):
        cur = test_db.conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notification_channels'")
        row = cur.fetchone()
        assert row is not None
        assert row["name"] == "notification_channels"

        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_notification_channels_enabled'")
        assert cur.fetchone() is not None

    def test_create_and_get_notification_channel(self, test_db):
        channel = NotificationChannel(
            id="chan-discord-1",
            name="Homelab Discord",
            channel_type=NotificationChannelType.DISCORD,
            enabled=True,
            config={"webhook_url": "https://discord.com/api/webhooks/123/tokenABC"},
            events=[NotificationEvent.REQUEST_CREATED.value, NotificationEvent.ITEM_AVAILABLE.value],
        )
        saved = test_db.create_notification_channel(channel)
        assert saved["id"] == "chan-discord-1"
        assert saved["name"] == "Homelab Discord"
        assert saved["channel_type"] == "discord"
        assert saved["enabled"] is True
        assert saved["config"]["webhook_url"] == "https://discord.com/api/webhooks/123/tokenABC"
        assert saved["events"] == ["request_created", "item_available"]

        # Fetch by ID
        fetched = test_db.get_notification_channel("chan-discord-1")
        assert fetched is not None
        assert fetched["name"] == "Homelab Discord"

        # Fetch non-existent
        assert test_db.get_notification_channel("non-existent") is None

    def test_create_channel_from_dict(self, test_db):
        data = {
            "id": "chan-telegram-1",
            "name": "Admin Telegram",
            "channel_type": "telegram",
            "enabled": True,
            "config": {"bot_token": "12345:token", "chat_id": "-100123"},
            "events": ["download_failed"],
        }
        saved = test_db.create_notification_channel(data)
        assert saved["id"] == "chan-telegram-1"
        assert saved["channel_type"] == "telegram"
        assert saved["config"]["chat_id"] == "-100123"

    def test_list_notification_channels_with_filter(self, test_db):
        test_db.create_notification_channel(
            NotificationChannel(id="c1", name="Active Channel", channel_type="discord", enabled=True)
        )
        test_db.create_notification_channel(
            NotificationChannel(id="c2", name="Disabled Channel", channel_type="telegram", enabled=False)
        )

        all_channels = test_db.list_notification_channels(enabled_only=False)
        assert len(all_channels) == 2

        active_channels = test_db.list_notification_channels(enabled_only=True)
        assert len(active_channels) == 1
        assert active_channels[0]["id"] == "c1"

    def test_update_notification_channel(self, test_db):
        test_db.create_notification_channel(
            NotificationChannel(id="c-upd", name="Original Name", channel_type="discord", enabled=True)
        )

        updated = test_db.update_notification_channel(
            "c-upd",
            {
                "name": "Updated Name",
                "enabled": False,
                "config": {"webhook_url": "https://discord.com/api/webhooks/999/tokenXYZ"},
            },
        )
        assert updated["name"] == "Updated Name"
        assert updated["enabled"] is False
        assert updated["config"]["webhook_url"] == "https://discord.com/api/webhooks/999/tokenXYZ"

        with pytest.raises(KeyError):
            test_db.update_notification_channel("non-existent", {"name": "Test"})

    def test_delete_notification_channel(self, test_db):
        test_db.create_notification_channel(
            NotificationChannel(id="c-del", name="To Delete", channel_type="webhook")
        )
        assert test_db.delete_notification_channel("c-del") is True
        assert test_db.get_notification_channel("c-del") is None
        assert test_db.delete_notification_channel("c-del") is False

    def test_notification_channel_model_to_dict_masking(self):
        channel = NotificationChannel(
            id="c-mask",
            name="Discord Channel",
            channel_type="discord",
            config={"webhook_url": "https://discord.com/api/webhooks/12345/supersecrettoken"},
        )
        unmasked = channel.to_dict(mask_secrets=False)
        assert unmasked["config"]["webhook_url"] == "https://discord.com/api/webhooks/12345/supersecrettoken"

        masked = channel.to_dict(mask_secrets=True)
        assert "supersecrettoken" not in masked["config"]["webhook_url"]
        assert "•••" in masked["config"]["webhook_url"]


# =============================================================================
# 2. Notification Dispatcher Channel Senders Tests
# =============================================================================


class TestNotificationDispatcherSenders:
    def test_format_notification_titles_and_messages(self):
        data = {"artist": "Daft Punk", "title": "Discovery", "username": "ron"}

        t1, m1 = format_notification("request_created", data)
        assert "Music Requested: Daft Punk - Discovery" in t1
        assert "ron requested" in m1

        t2, m2 = format_notification("request_approved", data)
        assert "Request Approved: Daft Punk - Discovery" in t2

        t3, m3 = format_notification("request_rejected", data)
        assert "Request Rejected: Daft Punk - Discovery" in t3

        t4, m4 = format_notification("download_started", {"artist": "Daft Punk", "title": "One More Time", "client": "slskd"})
        assert "Download Started: Daft Punk - One More Time" in t4
        assert "slskd" in m4

        t5, m5 = format_notification("item_available", data)
        assert "Music Available: Daft Punk - Discovery is ready in Plex!" in t5

        t6, m6 = format_notification("download_failed", {"artist": "Daft Punk", "title": "Discovery", "error_message": "Timeout"})
        assert "Download Failed: Daft Punk - Discovery" in t6
        assert "Timeout" in m6

    @patch("httpx.Client.post")
    def test_send_discord_payload_structure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        webhook_url = "https://discord.com/api/webhooks/12345/token"
        dispatcher._send_discord(
            webhook_url=webhook_url,
            title="Music Available: Artist - Title is ready in Plex!",
            message="Imported successfully.",
            data={"artist": "Artist", "title": "Title", "album": "Album", "cover_url": "https://i.scdn.co/art.jpg"},
            event="item_available",
        )

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == webhook_url
        payload = kwargs["json"]
        assert payload["username"] == "TrackSeerr"
        embed = payload["embeds"][0]
        assert embed["color"] == 0x2ECC71  # Green for available
        assert embed["thumbnail"]["url"] == "https://i.scdn.co/art.jpg"
        field_names = [f["name"] for f in embed["fields"]]
        assert "Artist" in field_names
        assert "Title" in field_names
        assert "Album" in field_names

    def test_send_discord_rejects_ssrf(self):
        dispatcher = NotificationDispatcher()
        with pytest.raises(ValueError, match="Prohibited Discord webhook URL"):
            dispatcher._send_discord(
                webhook_url="http://169.254.169.254/latest/meta-data",
                title="Test",
                message="Test",
                data={},
            )

    @patch("httpx.Client.post")
    def test_send_telegram_payload(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_telegram(
            bot_token="bot12345:token",
            chat_id="-100987654321",
            title="Music Requested: <Band> & 'Co'",
            message="User requested <Band>",
            data={},
        )

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "https://api.telegram.org/botbot12345:token/sendMessage" == args[0]
        payload = kwargs["json"]
        assert payload["chat_id"] == "-100987654321"
        assert payload["parse_mode"] == "HTML"
        # Verify HTML escaping
        assert "&lt;Band&gt;" in payload["text"]
        assert "&amp;" in payload["text"]

    @patch("httpx.Client.post")
    def test_send_pushover_payload(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_pushover(
            user_key="user_key_123",
            app_token="app_token_456",
            title="Download Started",
            message="Grabbed album",
            data={},
        )

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.pushover.net/1/messages.json"
        data = kwargs["data"]
        assert data["user"] == "user_key_123"
        assert data["token"] == "app_token_456"
        assert data["title"] == "Download Started"
        assert data["message"] == "Grabbed album"

    @patch("httpx.Client.post")
    def test_send_webhook_payload(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        webhook_url = "https://example.com/api/webhook"
        dispatcher._send_webhook(
            webhook_url=webhook_url,
            event="item_available",
            data={"artist": "Radiohead", "title": "Kid A"},
            secret_header="secret-auth-key-xyz",
        )

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == webhook_url
        headers = kwargs["headers"]
        assert headers["X-TrackSeerr-Secret"] == "secret-auth-key-xyz"
        payload = kwargs["json"]
        assert payload["event"] == "item_available"
        assert payload["data"] == {"artist": "Radiohead", "title": "Kid A"}
        assert "timestamp" in payload

    @patch("smtplib.SMTP")
    def test_send_email_smtp_tls(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        dispatcher = NotificationDispatcher()
        dispatcher._send_email(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="user@gmail.com",
            password="app-password",
            use_tls=True,
            use_ssl=False,
            from_addr="trackseerr@gmail.com",
            to_addr="admin@homelab.local",
            title="Request Approved",
            message="Request for album has been approved.",
        )

        mock_smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=10.0)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@gmail.com", "app-password")
        mock_server.send_message.assert_called_once()
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "[TrackSeerr] Request Approved"
        assert sent_msg["From"] == "trackseerr@gmail.com"
        assert sent_msg["To"] == "admin@homelab.local"

    @patch("smtplib.SMTP_SSL")
    def test_send_email_smtp_ssl(self, mock_smtp_ssl_cls):
        mock_server = MagicMock()
        mock_smtp_ssl_cls.return_value.__enter__.return_value = mock_server

        dispatcher = NotificationDispatcher()
        dispatcher._send_email(
            smtp_host="mail.example.com",
            smtp_port=465,
            username="bot@example.com",
            password="pass",
            use_tls=False,
            use_ssl=True,
            from_addr="bot@example.com",
            to_addr="user@example.com",
            title="Music Available",
            message="Your music is ready.",
        )

        mock_smtp_ssl_cls.assert_called_once_with("mail.example.com", 465, context=ANY, timeout=10.0)
        mock_server.login.assert_called_once_with("bot@example.com", "pass")
        mock_server.send_message.assert_called_once()


class TestNotificationApplicationUrlLinkbacks:
    """Tests that application_url is correctly incorporated into outbound notifications."""

    @patch("httpx.Client.post")
    def test_discord_includes_application_url(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_discord(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            title="Music Requested",
            message="User requested track",
            data={"artist": "Artist", "title": "Track", "application_url": "https://music.mydomain.com"},
        )
        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["url"] == "https://music.mydomain.com"
        field_values = [f["value"] for f in embed["fields"]]
        assert any("https://music.mydomain.com" in v for v in field_values)

    @patch("httpx.Client.post")
    def test_telegram_includes_application_url(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_telegram(
            bot_token="token",
            chat_id="12345",
            title="Music Requested",
            message="User requested track",
            data={"application_url": "https://music.mydomain.com"},
        )
        payload = mock_post.call_args[1]["json"]
        assert '<a href="https://music.mydomain.com">Open in TrackSeerr</a>' in payload["text"]

    @patch("httpx.Client.post")
    def test_pushover_includes_application_url(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_pushover(
            user_key="user",
            app_token="app",
            title="Title",
            message="Message",
            data={"application_url": "https://music.mydomain.com"},
        )
        payload = mock_post.call_args[1]["data"]
        assert payload["url"] == "https://music.mydomain.com"
        assert payload["url_title"] == "Open in TrackSeerr"

    @patch("httpx.Client.post")
    def test_webhook_includes_application_url(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dispatcher = NotificationDispatcher()
        dispatcher._send_webhook(
            webhook_url="https://example.com/hook",
            event="item_available",
            data={"artist": "Artist", "application_url": "https://music.mydomain.com"},
        )
        payload = mock_post.call_args[1]["json"]
        assert payload["application_url"] == "https://music.mydomain.com"

    @patch("smtplib.SMTP")
    def test_email_includes_application_url(self, mock_smtp):
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        dispatcher = NotificationDispatcher()
        dispatcher._send_email(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user",
            password="pwd",
            use_tls=True,
            use_ssl=False,
            from_addr="ts@example.com",
            to_addr="u@example.com",
            title="Title",
            message="Body message",
            data={"application_url": "https://music.mydomain.com"},
        )
        sent_msg = mock_server.send_message.call_args[0][0]
        content = sent_msg.get_content()
        assert "Open in TrackSeerr: https://music.mydomain.com" in content

    def test_dispatch_populates_application_url_from_db(self, test_db):
        test_db.update_general_settings({"application_url": "https://trackseerr.mydomain.com"})
        test_db.create_notification_channel(
            NotificationChannel(
                id="c-url-test",
                name="URL Test",
                channel_type="webhook",
                config={"webhook_url": "https://example.com/hook"},
                events=["item_available"],
            )
        )
        dispatcher = NotificationDispatcher()
        captured_data = {}

        def capture_send(ch, event, data):
            captured_data.update(data)

        with patch.object(dispatcher, "_send_to_channel", side_effect=capture_send):
            dispatcher._run_dispatch("item_available", {"artist": "Artist"}, db=test_db)
            assert captured_data.get("application_url") == "https://trackseerr.mydomain.com"


# =============================================================================
# 3. Notification Dispatcher Lifecycle & Isolation Tests
# =============================================================================


class TestNotificationDispatcherLifecycle:
    def test_test_channel_success(self):
        dispatcher = NotificationDispatcher()
        with patch.object(dispatcher, "_send_discord") as mock_send:
            ok, msg = dispatcher.test_channel("discord", {"webhook_url": "https://discord.com/api/webhooks/1/tok"})
            assert ok is True
            assert msg == "Notification sent successfully"
            mock_send.assert_called_once()

    def test_test_channel_failure(self):
        dispatcher = NotificationDispatcher()
        with patch.object(dispatcher, "_send_discord", side_effect=httpx.HTTPError("404 Not Found")):
            ok, msg = dispatcher.test_channel("discord", {"webhook_url": "https://discord.com/api/webhooks/1/tok"})
            assert ok is False
            assert "404 Not Found" in msg

    def test_dispatch_asynchronous_background_thread(self, test_db):
        dispatcher = NotificationDispatcher()
        test_db.create_notification_channel(
            NotificationChannel(
                id="c-async",
                name="Async Channel",
                channel_type="webhook",
                config={"webhook_url": "https://example.com/hook"},
                events=["item_available"],
            )
        )

        called_event = threading.Event()

        def slow_send(*args, **kwargs):
            time.sleep(0.05)
            called_event.set()

        with patch.object(dispatcher, "_send_webhook", side_effect=slow_send):
            start = time.perf_counter()
            dispatcher.dispatch("item_available", {"artist": "Test", "title": "Track"}, db=test_db)
            duration = time.perf_counter() - start
            # Must return immediately without waiting for slow_send
            assert duration < 0.04
            assert called_event.wait(timeout=2.0)

    def test_dispatch_exception_isolation(self, test_db):
        """Failure in channel 1 must not prevent channel 2 from receiving notifications."""
        dispatcher = NotificationDispatcher()
        test_db.create_notification_channel(
            NotificationChannel(
                id="c-fail",
                name="Failing Channel",
                channel_type="webhook",
                config={"webhook_url": "https://fail.com/hook"},
                events=["request_created"],
            )
        )
        test_db.create_notification_channel(
            NotificationChannel(
                id="c-ok",
                name="Succeeding Channel",
                channel_type="telegram",
                config={"bot_token": "token", "chat_id": "123"},
                events=["request_created"],
            )
        )

        telegram_called = threading.Event()

        with patch.object(dispatcher, "_send_webhook", side_effect=Exception("Network down")), patch.object(
            dispatcher, "_send_telegram", side_effect=lambda *args, **kwargs: telegram_called.set()
        ):
            dispatcher.dispatch("request_created", {"artist": "Artist", "title": "Song"}, db=test_db)
            assert telegram_called.wait(timeout=2.0)

    def test_dispatch_event_filtering(self, test_db):
        """Channel only receives events it subscribed to."""
        dispatcher = NotificationDispatcher()
        test_db.create_notification_channel(
            NotificationChannel(
                id="c-filtered",
                name="Filtered Channel",
                channel_type="webhook",
                config={"webhook_url": "https://example.com/hook"},
                events=["download_failed"],  # Only subscribed to failure
            )
        )

        with patch.object(dispatcher, "_send_webhook") as mock_send:
            # Trigger request_created
            dispatcher._run_dispatch("request_created", {"artist": "Artist", "title": "Song"}, db=test_db)
            mock_send.assert_not_called()

            # Trigger download_failed
            dispatcher._run_dispatch("download_failed", {"artist": "Artist", "title": "Song"}, db=test_db)
            mock_send.assert_called_once()


# =============================================================================
# 4. Notification Settings API Endpoint Tests
# =============================================================================


class TestNotificationAPI:
    def test_notifications_api_rbac_unauthenticated(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/settings/notifications")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_notifications_api_rbac_non_admin_forbidden(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        resp = client.get("/api/settings/notifications", headers=headers)
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_admin_create_and_list_channels_with_masked_secrets(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # 1. Create Discord Channel
        create_payload = {
            "name": "Admin Discord",
            "channel_type": "discord",
            "enabled": True,
            "config": {"webhook_url": "https://discord.com/api/webhooks/12345/mysecrettoken123"},
            "events": ["request_created", "item_available"],
        }
        res = client.post("/api/settings/notifications", json=create_payload, headers=headers)
        assert res.status_code == status.HTTP_201_CREATED
        created = res.json()
        assert created["name"] == "Admin Discord"
        assert created["channel_type"] == "discord"
        assert "mysecrettoken123" not in created["config"]["webhook_url"]
        assert "•••" in created["config"]["webhook_url"]
        channel_id = created["id"]

        # 2. List channels
        list_res = client.get("/api/settings/notifications", headers=headers)
        assert list_res.status_code == status.HTTP_200_OK
        channels = list_res.json()
        assert len(channels) == 1
        assert channels[0]["id"] == channel_id
        assert "mysecrettoken123" not in channels[0]["config"]["webhook_url"]

    def test_admin_create_channel_validation_errors(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # Invalid channel type
        bad_type = {
            "name": "Bad Channel",
            "channel_type": "sms",
            "config": {},
        }
        r1 = client.post("/api/settings/notifications", json=bad_type, headers=headers)
        assert r1.status_code == status.HTTP_400_BAD_REQUEST

        # SSRF prohibited webhook URL
        ssrf_payload = {
            "name": "SSRF Channel",
            "channel_type": "discord",
            "config": {"webhook_url": "http://169.254.169.254/secret"},
        }
        r2 = client.post("/api/settings/notifications", json=ssrf_payload, headers=headers)
        assert r2.status_code == status.HTTP_400_BAD_REQUEST

        # Invalid notification event
        bad_event = {
            "name": "Bad Event Channel",
            "channel_type": "webhook",
            "config": {"webhook_url": "https://example.com/hook"},
            "events": ["invalid_event_type"],
        }
        r3 = client.post("/api/settings/notifications", json=bad_event, headers=headers)
        assert r3.status_code == status.HTTP_400_BAD_REQUEST

    def test_admin_update_channel_preserves_masked_secrets(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # 1. Create channel
        ch = test_db.create_notification_channel(
            NotificationChannel(
                id="chan-upd-test",
                name="Original Name",
                channel_type="telegram",
                config={"bot_token": "12345:secretbottoken", "chat_id": "-1001"},
            )
        )

        # 2. Update with masked bot_token (e.g. from frontend edit form)
        update_payload = {
            "name": "Renamed Channel",
            "channel_type": "telegram",
            "enabled": True,
            "config": {"bot_token": mask_secret("12345:secretbottoken"), "chat_id": "-1002"},
            "events": ["item_available"],
        }
        res = client.put(f"/api/settings/notifications/{ch['id']}", json=update_payload, headers=headers)
        assert res.status_code == status.HTTP_200_OK
        updated_api = res.json()
        assert updated_api["name"] == "Renamed Channel"

        # 3. Verify in database that secret was preserved
        in_db = test_db.get_notification_channel(ch["id"])
        assert in_db["config"]["bot_token"] == "12345:secretbottoken"
        assert in_db["config"]["chat_id"] == "-1002"

    def test_admin_delete_channel(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        test_db.create_notification_channel(
            NotificationChannel(id="chan-to-del", name="Delete Me", channel_type="webhook")
        )

        del_res = client.delete("/api/settings/notifications/chan-to-del", headers=headers)
        assert del_res.status_code == status.HTTP_200_OK
        assert del_res.json()["status"] == "deleted"

        del_404 = client.delete("/api/settings/notifications/chan-to-del", headers=headers)
        assert del_404.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_live_test_channel_endpoint(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        with patch.object(notification_dispatcher, "test_channel", return_value=(True, "Notification sent successfully")):
            test_payload = {
                "channel_type": "discord",
                "config": {"webhook_url": "https://discord.com/api/webhooks/123/token"},
            }
            res = client.post("/api/settings/notifications/test", json=test_payload, headers=headers)
            assert res.status_code == status.HTTP_200_OK
            assert res.json() == {"success": True, "message": "Notification sent successfully"}


# =============================================================================
# 5. Event Trigger Wiring Tests
# =============================================================================


class TestNotificationEventTriggers:
    def test_create_and_approve_request_triggers_notifications(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        dispatched_events = []

        def track_dispatch(event, data, db=None):
            dispatched_events.append(event.value if hasattr(event, "value") else str(event))

        with patch.object(notification_dispatcher, "dispatch", side_effect=track_dispatch):
            # 1. Create request as admin (auto-approves)
            create_payload = {
                "item_type": "album",
                "artist": "Pink Floyd",
                "title": "The Dark Side of the Moon",
            }
            res = client.post("/api/requests", json=create_payload, headers=headers)
            assert res.status_code == status.HTTP_201_CREATED
            # Should have emitted REQUEST_CREATED and REQUEST_APPROVED (since auto-approved)
            assert "request_created" in dispatched_events
            assert "request_approved" in dispatched_events

    def test_approve_and_reject_request_triggers_notifications(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        req = test_db.create_request(
            MusicRequest(
                id="req-test-app",
                user_id=seeded_users["alice"]["id"],
                item_type="album",
                artist="Led Zeppelin",
                title="IV",
                status=RequestStatus.PENDING,
            )
        )

        dispatched_events = []

        def track_dispatch(event, data, db=None):
            dispatched_events.append(event.value if hasattr(event, "value") else str(event))

        with patch.object(notification_dispatcher, "dispatch", side_effect=track_dispatch):
            # Approve
            client.post(f"/api/requests/{req['id']}/approve", headers=headers)
            assert "request_approved" in dispatched_events

            # Reject
            client.post(f"/api/requests/{req['id']}/reject", headers=headers)
            assert "request_rejected" in dispatched_events

    def test_coordinator_grab_triggers_download_started(self, test_db):
        coordinator = AcquisitionCoordinator()

        # Seed profile and client
        profile = test_db.get_default_quality_profile()
        test_db.create_download_client(
            DownloadClientConfig(
                id="client-test-1",
                name="Test Torrent Client",
                driver_type="qbittorrent",
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        mock_candidate = AcquisitionSearchResult(
            download_id="cand-1",
            title="Beatles - Abbey Road [FLAC]",
            artist="Beatles",
            album="Abbey Road",
            protocol="torrent",
            size_bytes=300000000,
        )

        test_db.upsert_user("user-test-1", "testuser", "test@example.com")
        test_db.create_request(
            MusicRequest(
                id="req-beatles",
                user_id="user-test-1",
                artist="Beatles",
                title="Abbey Road",
                item_type="album",
            )
        )

        dispatched = []

        def track_dispatch(event, data, db=None):
            dispatched.append((event, data))

        with patch.object(coordinator, "search_all_indexers", return_value=[mock_candidate]), patch(
            "trackseerr.acquisition_coordinator.get_acquisition_driver"
        ) as mock_get_driver, patch.object(notification_dispatcher, "dispatch", side_effect=track_dispatch):
            mock_driver = MagicMock()
            mock_driver.download.return_value = "hash123456"
            mock_get_driver.return_value = mock_driver

            res = coordinator.search_and_grab(
                artist="Beatles",
                title="Abbey Road",
                item_type="album",
                request_id="req-beatles",
                db=test_db,
                trigger=GrabTrigger("request"),
            )
            assert res["success"] is True
            assert len(dispatched) == 1
            ev, data = dispatched[0]
            assert ev == NotificationEvent.DOWNLOAD_STARTED
            assert data["artist"] == "Beatles"
            assert data["release"] == "Beatles - Abbey Road [FLAC]"

    def test_acquisition_worker_import_triggers_item_available(self, test_db, tmp_path):
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        music_root = tmp_path / "music"
        music_root.mkdir(parents=True, exist_ok=True)

        test_db.update_media_management_settings(
            {"staging_folder_path": str(staging_dir), "root_folder_path": str(music_root)}
        )

        worker = AcquisitionWorker()
        worker.staging_dir = str(staging_dir)

        # Create audio file in staging
        sample_audio = staging_dir / "track.flac"
        write_flac(sample_audio)

        test_db.create_download_client(
            DownloadClientConfig(
                id="client-import",
                name="Import Client",
                driver_type="qbittorrent",
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )
        dl = test_db.create_active_download(
            ActiveDownload(
                id="dl-import-test",
                client_id="client-import",
                title="Song",
                artist="Artist",
                status=DownloadStatus.COMPLETED.value,
            )
        )

        dispatched = []

        def track_dispatch(event, data, db=None):
            dispatched.append((event, data))

        target_file = music_root / "Artist" / "Album" / "01 - Song.flac"

        with patch("trackseerr.acquisition_worker.get_acquisition_driver") as mock_drv, patch(
            "trackseerr.acquisition_import.inspect_audio_file",
            return_value={"artist": "Artist", "title": "Song", "album": "Album", "track_number": 1},
        ), patch(
            "trackseerr.acquisition_catalog.inspect_audio_file",
            return_value={"artist": "Artist", "title": "Song", "album": "Album", "track_number": 1},
        ), patch("trackseerr.acquisition_import.place_audio_file", return_value=target_file), patch(
            "trackseerr.acquisition_import.resolve_collision", return_value=str(target_file)
        ), patch.object(
            notification_dispatcher, "dispatch", side_effect=track_dispatch
        ):
            driver = MagicMock()
            driver.get_status.return_value = {"status": "completed", "source_path": str(sample_audio)}
            mock_drv.return_value = driver

            stats = worker.poll_once(db=test_db, staging_dir=str(staging_dir))
            assert stats["imported"] >= 1
            available_events = [ev for ev, data in dispatched if ev == NotificationEvent.ITEM_AVAILABLE]
            assert len(available_events) == 1

    def test_acquisition_worker_failure_triggers_download_failed(self, test_db):
        worker = AcquisitionWorker()
        test_db.create_download_client(
            DownloadClientConfig(
                id="client-fail",
                name="Failing Client",
                driver_type="qbittorrent",
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )
        test_db.create_active_download(
            ActiveDownload(
                id="dl-fail-test",
                client_id="client-fail",
                title="Failed Song",
                artist="Artist",
                status=DownloadStatus.DOWNLOADING.value,
            )
        )

        dispatched = []

        def track_dispatch(event, data, db=None):
            dispatched.append((event, data))

        with patch("trackseerr.acquisition_worker.get_acquisition_driver") as mock_drv, patch.object(
            notification_dispatcher, "dispatch", side_effect=track_dispatch
        ):
            driver = MagicMock()
            driver.get_status.return_value = {"status": "failed", "error_message": "Peer connection timeout"}
            mock_drv.return_value = driver

            stats = worker.poll_once(db=test_db)
            assert stats["failed"] >= 1
            failed_events = [data for ev, data in dispatched if ev == NotificationEvent.DOWNLOAD_FAILED]
            assert len(failed_events) == 1
            assert failed_events[0]["error_message"] == "Peer connection timeout"

    def test_rss_sync_grab_triggers_download_started(self, test_db):
        worker = RSSSyncWorker()
        test_db.create_indexer(
            IndexerConfig(
                id="idx-1",
                name="Test Indexer",
                indexer_type="torznab",
                host_url="http://127.0.0.1:9117",
                enabled=True,
            )
        )
        test_db.create_download_client(
            DownloadClientConfig(
                id="c-rss",
                name="Torrent Client",
                driver_type="qbittorrent",
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )
        test_db.upsert_user("u1", "testuser", "test@example.com")
        req = test_db.create_request(
            MusicRequest(
                id="req-wanted-rss",
                user_id="u1",
                item_type="album",
                artist="Daft Punk",
                title="Homework",
                status=RequestStatus.PENDING,
            )
        )

        rss_candidate = AcquisitionSearchResult(
            download_id="rss-1",
            title="Daft Punk - Homework [FLAC]",
            artist="Daft Punk",
            album="Homework",
            protocol="torrent",
            size_bytes=400000000,
        )

        dispatched = []

        def track_dispatch(event, data, db=None):
            dispatched.append((event, data))

        with patch("trackseerr.backlog_worker.get_indexer_driver") as mock_idx_drv, patch(
            "trackseerr.backlog_worker.get_acquisition_driver"
        ) as mock_acq_drv, patch.object(notification_dispatcher, "dispatch", side_effect=track_dispatch):
            idx_driver = MagicMock()
            idx_driver.fetch_recent.return_value = [rss_candidate]
            mock_idx_drv.return_value = idx_driver

            acq_driver = MagicMock()
            acq_driver.download.return_value = "hash-rss-dl"
            mock_acq_drv.return_value = acq_driver

            res = worker.poll_once(db=test_db)
            assert res["grabs_triggered"] == 1
            assert any(ev == NotificationEvent.DOWNLOAD_STARTED for ev, data in dispatched)


class TestWebhookUrlMasking:
    RAW = "https://ntfy.example.com/topic-xyz?auth=tok_SECRET99"

    def _create(self, client, headers, key="webhook_url"):
        res = client.post(
            "/api/settings/notifications",
            json={"name": "Hook", "channel_type": "webhook", "config": {key: self.RAW}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_201_CREATED, res.text
        return res.json()

    def test_mask_helper_covers_url_and_webhook_url(self):
        from trackseerr.security import mask_channel_config

        for key in ("url", "webhook_url"):
            out = mask_channel_config("webhook", {key: self.RAW})
            assert out[key].startswith("https://ntfy.example.com")
            assert "tok_SECRET99" not in out[key] and "topic-xyz" not in out[key]
            assert "•••" in out[key]
        out = mask_channel_config("webhook", {"url": "https://user:pw@h.example/x"})
        assert "pw" not in out["url"] and "user" not in out["url"]

    def test_list_get_never_return_raw_url(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        created = self._create(client, headers)
        assert "tok_SECRET99" not in created["config"]["webhook_url"]
        listed = client.get("/api/settings/notifications", headers=headers)
        assert "tok_SECRET99" not in listed.text
        assert "topic-xyz" not in listed.text

    def test_update_with_masked_placeholder_keeps_original(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        created = self._create(client, headers)
        masked = created["config"]["webhook_url"]
        res = client.put(
            f"/api/settings/notifications/{created['id']}",
            json={"name": "Hook2", "channel_type": "webhook", "config": {"webhook_url": masked}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert "tok_SECRET99" not in res.text
        assert test_db.get_notification_channel(created["id"])["config"]["webhook_url"] == self.RAW

    def test_update_with_new_url_replaces(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        created = self._create(client, headers)
        new = "https://hooks.example.com/new/tok_NEW12345"
        res = client.put(
            f"/api/settings/notifications/{created['id']}",
            json={"name": "Hook", "channel_type": "webhook", "config": {"webhook_url": new}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert "tok_NEW12345" not in res.text
        assert test_db.get_notification_channel(created["id"])["config"]["webhook_url"] == new

    def test_update_edited_host_with_masked_path_is_400(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        created = self._create(client, headers)
        masked = created["config"]["webhook_url"]
        edited = masked.replace("ntfy.example.com", "evil.example.com")
        assert edited != masked and "•••" in edited
        res = client.put(
            f"/api/settings/notifications/{created['id']}",
            json={"name": "Hook", "channel_type": "webhook", "config": {"webhook_url": edited}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.text
        assert "placeholder" in res.json()["detail"]
        assert test_db.get_notification_channel(created["id"])["config"]["webhook_url"] == self.RAW

    def test_discord_mask_reveals_no_token_and_round_trips(self, app_and_client, test_db, test_config, seeded_users):
        from trackseerr.security import mask_channel_config

        raw = "https://discord.com/api/webhooks/12345/supersecrettoken"
        masked = mask_channel_config("discord", {"webhook_url": raw})["webhook_url"]
        assert masked.startswith("https://discord.com") and "oken" not in masked and "12345" not in masked
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        created = client.post(
            "/api/settings/notifications",
            json={"name": "D", "channel_type": "discord", "config": {"webhook_url": raw}},
            headers=headers,
        ).json()
        res = client.put(
            f"/api/settings/notifications/{created['id']}",
            json={"name": "D2", "channel_type": "discord", "config": {"webhook_url": created["config"]["webhook_url"]}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert test_db.get_notification_channel(created["id"])["config"]["webhook_url"] == raw

    def test_masked_url_leaks_no_trailing_characters(self):
        from trackseerr.security import mask_channel_config

        out = mask_channel_config("webhook", {"url": "https://ntfy.example.com/topic/abcd1234"})["url"]
        assert out == "https://ntfy.example.com" + "•" * len("/topic/abcd1234")
        v6 = mask_channel_config("webhook", {"url": "http://[::1]:8080/hook/tok"})["url"]
        assert v6.startswith("http://[::1]:8080") and "tok" not in v6

    def test_short_path_mask_round_trips(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        raw = "https://h.example.com/x"
        res = client.post(
            "/api/settings/notifications",
            json={"name": "S", "channel_type": "webhook", "config": {"webhook_url": raw}},
            headers=headers,
        )
        created = res.json()
        masked = created["config"]["webhook_url"]
        assert masked == "https://h.example.com••"
        res = client.put(
            f"/api/settings/notifications/{created['id']}",
            json={"name": "S2", "channel_type": "webhook", "config": {"webhook_url": masked}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert test_db.get_notification_channel(created["id"])["config"]["webhook_url"] == raw

    def test_test_endpoint_does_not_send_stored_secrets_to_new_url(
        self, app_and_client, test_db, test_config, seeded_users, monkeypatch
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        ch = test_db.create_notification_channel(
            NotificationChannel(
                id="chan-exfil",
                name="W",
                channel_type="webhook",
                config={"webhook_url": "https://good.example.com/hook", "secret_header": "TOPSECRET"},
            )
        )
        seen: list[dict] = []
        monkeypatch.setattr(
            notification_dispatcher, "test_channel", lambda channel_type, config: (seen.append(dict(config)) or (True, "ok"))
        )
        res = client.post(
            "/api/settings/notifications/test",
            json={"channel_type": "webhook", "channel_id": ch["id"], "config": {"webhook_url": "https://evil.example.com/x"}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert seen[-1] == {"webhook_url": "https://evil.example.com/x"}
        # Unchanged destination (omitted URL) still merges the stored secret.
        res = client.post(
            "/api/settings/notifications/test",
            json={"channel_type": "webhook", "channel_id": ch["id"], "config": {}},
            headers=headers,
        )
        assert res.status_code == status.HTTP_200_OK, res.text
        assert seen[-1]["secret_header"] == "TOPSECRET"
