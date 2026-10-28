"""Tests for system health store and check runner."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from trackseerr.config import Config
from trackseerr.models import DownloadStatus, NotificationEvent
from trackseerr.storage import Database
from trackseerr.system_health import observe, run_health_checks


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
        plex_url="",
        plex_token="",
        media_server="none",
        data_dir=str(tmp_path),
    )


def test_failure_below_threshold_is_warning_no_notify(test_db):
    now0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        state = observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Connection timeout",
            threshold_minutes=15,
            now=now0,
        )
        assert state == "warning"
        mock_dispatch.assert_not_called()
        row = test_db.get_health_check("indexer:1")
        assert row is not None
        assert row["state"] == "warning"
        assert row["message"] == "Connection timeout"
        assert row["failing_since"] == now0.isoformat()


def test_failure_past_threshold_is_error_notifies_once(test_db):
    now0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    now1 = now0 + timedelta(minutes=16)
    now2 = now0 + timedelta(minutes=25)

    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        # Run 1: first failure -> warning, no notify
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Connection timeout",
            threshold_minutes=15,
            now=now0,
        )
        mock_dispatch.assert_not_called()

        # Run 2: past threshold -> transitions to error -> dispatch once
        state1 = observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Connection timeout",
            threshold_minutes=15,
            now=now1,
        )
        assert state1 == "error"
        assert mock_dispatch.call_count == 1
        args, kwargs = mock_dispatch.call_args
        assert args[0] == NotificationEvent.HEALTH_ISSUE
        assert args[1]["label"] == "Indexer Torznab"
        assert args[1]["since"] == now0.isoformat()

        # Run 3: still failing, still in error, renotify_hours (24) not elapsed -> no second notify
        state2 = observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Connection timeout",
            threshold_minutes=15,
            now=now2,
        )
        assert state2 == "error"
        assert mock_dispatch.call_count == 1


def test_renotify_after_renotify_hours(test_db):
    now0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    now1 = now0 + timedelta(minutes=16)
    now2 = now1 + timedelta(hours=24, minutes=1)

    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Timeout",
            threshold_minutes=15,
            now=now0,
        )
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Timeout",
            threshold_minutes=15,
            now=now1,
        )
        assert mock_dispatch.call_count == 1

        # After renotify_hours (24h), another notification should fire
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Timeout",
            threshold_minutes=15,
            now=now2,
        )
        assert mock_dispatch.call_count == 2


def test_recovery_notifies_only_after_error(test_db):
    now0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    now1 = now0 + timedelta(minutes=5)

    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        # Warning -> ok sends nothing
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Temporary hiccup",
            threshold_minutes=15,
            now=now0,
        )
        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=True,
            message="",
            threshold_minutes=15,
            now=now1,
        )
        mock_dispatch.assert_not_called()

        # Error -> ok sends health_recovered
        now2 = now1 + timedelta(minutes=10)
        now3 = now2 + timedelta(minutes=5)

        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=False,
            message="Hard fail",
            threshold_minutes=0,
            now=now2,
        )
        assert mock_dispatch.call_count == 1
        assert mock_dispatch.call_args[0][0] == NotificationEvent.HEALTH_ISSUE

        observe(
            test_db,
            key="indexer:1",
            kind="indexer",
            label="Indexer Torznab",
            ok=True,
            message="",
            threshold_minutes=0,
            now=now3,
        )
        assert mock_dispatch.call_count == 2
        assert mock_dispatch.call_args[0][0] == NotificationEvent.HEALTH_RECOVERED
        assert mock_dispatch.call_args[0][1]["label"] == "Indexer Torznab"


def test_zero_threshold_errors_immediately(test_db):
    now0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        state = observe(
            test_db,
            key="disk:/data",
            kind="disk",
            label="Disk /data",
            ok=False,
            message="Low disk space",
            threshold_minutes=0,
            now=now0,
        )
        assert state == "error"
        assert mock_dispatch.call_count == 1
        assert mock_dispatch.call_args[0][0] == NotificationEvent.HEALTH_ISSUE


def test_indexer_probe_exception_counts_as_failure(test_db, test_config):
    test_db.create_indexer(
        {
            "id": "idx-broken",
            "name": "Broken Torznab",
            "host_url": "http://broken.example.com",
            "indexer_type": "torznab",
            "api_key": "dummy",
            "enabled": True,
        }
    )

    with patch("trackseerr.system_health.get_indexer_driver") as mock_get_driver:
        mock_driver = MagicMock()
        mock_driver.test_connection.side_effect = httpx.ConnectError("Connection refused")
        mock_get_driver.return_value = mock_driver

        res = run_health_checks(test_db, test_config)
        assert res["checked"] >= 1
        check = test_db.get_health_check("indexer:idx-broken")
        assert check is not None
        assert check["state"] in ("warning", "error")
        assert "ConnectError" in check["message"] or "Connection refused" in check["message"]


def test_deleted_indexer_row_removed(test_db, test_config):
    test_db.create_indexer(
        {
            "id": "idx-keep",
            "name": "Keep Indexer",
            "host_url": "http://keep.example.com",
            "indexer_type": "torznab",
            "api_key": "k1",
            "enabled": True,
        }
    )
    test_db.create_indexer(
        {
            "id": "idx-del",
            "name": "Delete Indexer",
            "host_url": "http://del.example.com",
            "indexer_type": "torznab",
            "api_key": "k2",
            "enabled": True,
        }
    )

    with patch("trackseerr.system_health.get_indexer_driver") as mock_get_driver:
        mock_driver = MagicMock()
        mock_driver.test_connection.return_value = (True, "OK")
        mock_get_driver.return_value = mock_driver

        run_health_checks(test_db, test_config)
        assert test_db.get_health_check("indexer:idx-keep") is not None
        assert test_db.get_health_check("indexer:idx-del") is not None

        test_db.delete_indexer("idx-del")

        run_health_checks(test_db, test_config)
        assert test_db.get_health_check("indexer:idx-keep") is not None
        assert test_db.get_health_check("indexer:idx-del") is None


def test_disk_low_space_errors(test_db, test_config):
    usage_mock = MagicMock(
        total=100 * (1024**3),
        used=95 * (1024**3),
        free=5 * (1024**3),
    )
    with patch("shutil.disk_usage", return_value=usage_mock):
        res = run_health_checks(test_db, test_config)
        assert res["errors"] >= 1
        disk_checks = [c for c in test_db.list_health_checks() if c["kind"] == "disk"]
        assert len(disk_checks) >= 1
        assert disk_checks[0]["state"] == "error"
        assert "Only 5.0 GB free" in disk_checks[0]["message"]


def test_media_server_none_skipped(test_db, test_config):
    test_config.media_server = "none"
    with patch("trackseerr.system_health.get_media_server") as mock_get_ms:
        res = run_health_checks(test_db, test_config)
        mock_get_ms.assert_not_called()
        ms_checks = [c for c in test_db.list_health_checks() if c["kind"] == "media_server"]
        assert len(ms_checks) == 0


def test_stalled_download_alerts_once(test_db, test_config):
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(minutes=75)).strftime("%Y-%m-%d %H:%M:%S")

    test_db.create_download_client(
        {
            "id": "c1",
            "name": "qBittorrent",
            "driver_type": "qbittorrent",
            "host_url": "http://127.0.0.1:8080",
            "enabled": False,
        }
    )

    test_db.create_active_download(
        {
            "id": "dl-stall",
            "client_id": "c1",
            "title": "Harder Better Faster",
            "artist": "Daft Punk",
            "status": DownloadStatus.DOWNLOADING.value,
        }
    )
    with test_db._lock:
        test_db.conn.execute(
            "UPDATE active_downloads SET progress_updated_at = ? WHERE id = 'dl-stall'",
            (old_time,),
        )
        test_db.conn.commit()

    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        res1 = run_health_checks(test_db, test_config, now=now)
        assert res1["alerts"] == 1
        assert mock_dispatch.call_count == 1
        args, _ = mock_dispatch.call_args
        assert args[0] == NotificationEvent.DOWNLOAD_STALLED
        assert args[1]["artist"] == "Daft Punk"
        assert args[1]["minutes"] >= 75
        assert test_db.has_health_item_alert(NotificationEvent.DOWNLOAD_STALLED.value, "dl-stall") is True

        # Second run: does not dispatch again
        res2 = run_health_checks(test_db, test_config, now=now)
        assert res2["alerts"] == 0
        assert mock_dispatch.call_count == 1


def test_held_import_alerts_once_and_prunes_when_gone(test_db, test_config):
    test_db.create_download_client(
        {
            "id": "c1",
            "name": "qBittorrent",
            "driver_type": "qbittorrent",
            "host_url": "http://127.0.0.1:8080",
            "enabled": False,
        }
    )

    test_db.create_active_download(
        {
            "id": "dl-held",
            "client_id": "c1",
            "title": "Around the World",
            "artist": "Daft Punk",
            "status": "completed",
        }
    )
    with test_db._lock:
        test_db.conn.execute(
            "UPDATE active_downloads SET unmatched_files = ? WHERE id = 'dl-held'",
            ('["/data/music/unmatched1.mp3", "/data/music/unmatched2.mp3"]',),
        )
        test_db.conn.commit()

    usage_ok = MagicMock(total=100 * (1024**3), used=10 * (1024**3), free=90 * (1024**3))
    with patch("shutil.disk_usage", return_value=usage_ok), patch(
        "trackseerr.system_health.notification_dispatcher.dispatch"
    ) as mock_dispatch:
        res1 = run_health_checks(test_db, test_config)
        assert res1["alerts"] == 1
        assert mock_dispatch.call_count == 1
        args, _ = mock_dispatch.call_args
        assert args[0] == NotificationEvent.IMPORT_NEEDS_ATTENTION
        assert args[1]["artist"] == "Daft Punk"
        assert "2 unmatched files held" in args[1]["reason"]
        assert test_db.has_health_item_alert(NotificationEvent.IMPORT_NEEDS_ATTENTION.value, "dl-held") is True

        # Second run: download still exists, alert not repeated
        res2 = run_health_checks(test_db, test_config)
        assert res2["alerts"] == 0
        assert mock_dispatch.call_count == 1

        # Delete download and run again: alert row pruned
        test_db.delete_active_download("dl-held")
        run_health_checks(test_db, test_config)
        assert test_db.has_health_item_alert(NotificationEvent.IMPORT_NEEDS_ATTENTION.value, "dl-held") is False


def test_health_settings_defaults_and_merge(test_db):
    defaults = test_db.get_health_settings()
    assert defaults["indexer_fail_minutes"] == 15
    assert defaults["client_fail_minutes"] == 5
    assert defaults["media_server_fail_minutes"] == 10
    assert defaults["disk_free_min_gb"] == 20
    assert defaults["download_stall_minutes"] == 60
    assert defaults["renotify_hours"] == 24

    updated = test_db.set_health_settings(
        {
            "indexer_fail_minutes": -10,
            "disk_free_min_gb": 40,
            "arbitrary_unknown_key": "some_value",
        }
    )
    assert updated["indexer_fail_minutes"] == 0
    assert updated["disk_free_min_gb"] == 40
    assert "arbitrary_unknown_key" not in updated
    assert updated["client_fail_minutes"] == 5
    assert updated["media_server_fail_minutes"] == 10
    assert updated["download_stall_minutes"] == 60
    assert updated["renotify_hours"] == 24

    current = test_db.get_health_settings()
    assert current == updated
