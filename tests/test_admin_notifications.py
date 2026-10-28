"""Tests for admin notification events, admin tray delivery, preferences, and push notifications."""

from unittest.mock import patch
import pytest

from trackseerr.models import NotificationEvent
from trackseerr.notifications import NotificationDispatcher
from trackseerr.request_submission import RequestRejected, _enforce_quota
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


def test_admin_gets_tray_for_health_issue(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    user = test_db.upsert_user("user-1", "regular_user", "user@test.local", is_admin=False)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.HEALTH_ISSUE.value,
            {"label": "Prowlarr", "message": "Connection refused", "since": "recently"},
            db=test_db,
        )

    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 1
    assert admin_notes[0]["event"] == NotificationEvent.HEALTH_ISSUE.value
    assert admin_notes[0]["title"] == "Prowlarr: problem"
    assert admin_notes[0]["link"] == "#/settings/system/status"

    user_notes, _, _ = test_db.list_user_notifications(user["id"])
    assert len(user_notes) == 0


def test_non_admin_gets_no_admin_event(test_db):
    user = test_db.upsert_user("user-1", "regular_user", "user@test.local", is_admin=False)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.TASK_FAILED.value,
            {"task_id": "indexer_rss_sync", "label": "RSS Sync", "message": "Failed"},
            db=test_db,
        )

    user_notes, _, _ = test_db.list_user_notifications(user["id"])
    assert len(user_notes) == 0


def test_admin_default_off_event_not_delivered(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    user = test_db.upsert_user("user-1", "regular_user", "user@test.local", is_admin=False)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.DOWNLOAD_STARTED.value,
            {"artist": "Daft Punk", "title": "One More Time", "user_id": user["id"]},
            db=test_db,
        )

    # Regular user (owner) gets it
    user_notes, _, _ = test_db.list_user_notifications(user["id"])
    assert len(user_notes) == 1

    # Admin has no pref set; download_started is in ADMIN_DEFAULT_OFF, so admin tray gets 0
    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 0


def test_admin_pref_off_suppresses(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    test_db.set_user_notification_pref(admin["id"], NotificationEvent.HEALTH_ISSUE.value, in_app=False, push=False)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.HEALTH_ISSUE.value,
            {"label": "Prowlarr", "message": "Down"},
            db=test_db,
        )

    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 0


def test_admin_pref_on_enables_default_off_event(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    user = test_db.upsert_user("user-1", "regular_user", "user@test.local", is_admin=False)
    test_db.set_user_notification_pref(admin["id"], NotificationEvent.DOWNLOAD_STARTED.value, in_app=True, push=False)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.DOWNLOAD_STARTED.value,
            {"artist": "Daft Punk", "title": "One More Time", "user_id": user["id"]},
            db=test_db,
        )

    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 1
    assert admin_notes[0]["event"] == NotificationEvent.DOWNLOAD_STARTED.value
    assert admin_notes[0]["link"] == "#/activity/queue"


def test_owner_admin_not_duplicated(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    # Admin is the owner of the request, approved by someone else
    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.REQUEST_APPROVED.value,
            {
                "artist": "Daft Punk",
                "title": "Discovery",
                "user_id": admin["id"],
                "actor_user_id": "other-user",
            },
            db=test_db,
        )

    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 1
    assert admin_notes[0]["event"] == NotificationEvent.REQUEST_APPROVED.value


def test_disabled_admin_skipped(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)
    test_db.conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (admin["id"],))
    test_db.conn.commit()

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push"):
        dispatcher._run_dispatch(
            NotificationEvent.HEALTH_ISSUE.value,
            {"label": "Prowlarr", "message": "Down"},
            db=test_db,
        )

    admin_notes, _, _ = test_db.list_user_notifications(admin["id"])
    assert len(admin_notes) == 0


def test_push_follows_pref(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@test.local", is_admin=True)

    dispatcher = NotificationDispatcher()
    with patch.object(dispatcher, "_send_web_push") as mock_push:
        # Default pref for health_issue has push=False
        dispatcher._run_dispatch(
            NotificationEvent.HEALTH_ISSUE.value,
            {"label": "Prowlarr", "message": "Down"},
            db=test_db,
        )
        mock_push.assert_not_called()

        # Enable push for admin
        test_db.set_user_notification_pref(admin["id"], NotificationEvent.HEALTH_ISSUE.value, in_app=True, push=True)
        dispatcher._run_dispatch(
            NotificationEvent.HEALTH_ISSUE.value,
            {"label": "Prowlarr", "message": "Down"},
            db=test_db,
        )
        assert mock_push.call_count == 1
        args, kwargs = mock_push.call_args
        assert args[1] == admin["id"]
        assert args[2] == NotificationEvent.HEALTH_ISSUE.value


def test_quota_reached_notifies_once_per_day(test_db):
    user = test_db.upsert_user("user-1", "regular_user", "user@test.local", is_admin=False)
    with patch.object(
        Database,
        "count_user_requests_by_type",
        return_value={"track": 25, "album": 0, "discography": 0},
    ):
        with patch("trackseerr.notifications.notification_dispatcher.dispatch") as mock_dispatch:
            with pytest.raises(RequestRejected) as exc_info:
                _enforce_quota(test_db, user, {"track": 1})
            assert "Request quota reached for tracks" in str(exc_info.value)
            assert mock_dispatch.call_count == 1
            args, kwargs = mock_dispatch.call_args
            assert args[0] == NotificationEvent.REQUEST_QUOTA_REACHED
            assert args[1]["username"] == "regular_user"

            # Second attempt on same day raises RequestRejected but does not dispatch again
            mock_dispatch.reset_mock()
            with pytest.raises(RequestRejected):
                _enforce_quota(test_db, user, {"track": 1})
            mock_dispatch.assert_not_called()
