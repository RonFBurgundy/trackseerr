"""System health checks and notification runner.

Probes indexers, download clients, media servers, and disk storage;
tracks state transitions (ok, warning, error) and notifies on state changes.
Also monitors stalled active downloads and held manual imports.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx

from trackseerr.clients.acquisition import get_acquisition_driver, get_indexer_driver
from trackseerr.config import MEDIA_SERVER_NONE
from trackseerr.download_roots import allowed_roots_for_all_clients
from trackseerr.media_servers import get_media_server
from trackseerr.models import DownloadStatus, NotificationEvent
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)


def _parse_utc_timestamp(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    cleaned = ts_str.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def observe(
    db: Any,
    key: str,
    kind: str,
    label: str,
    ok: bool,
    message: str,
    threshold_minutes: int,
    now: datetime,
    settings: dict[str, Any] | None = None,
) -> str:
    """State machine transition for a single health check probe. Returns the new state."""
    if settings is None:
        settings = db.get_health_settings()

    existing = db.get_health_check(key)
    prev_state = existing["state"] if existing else None
    prev_notified_state = existing["notified_state"] if existing else None
    prev_notified_at_str = existing["notified_at"] if existing else None
    renotify_hours = int(settings.get("renotify_hours", 24)) if settings else 24
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    if ok:
        new_state = "ok"
        new_message = None
        new_failing_since = None
        if prev_state == "error":
            notification_dispatcher.dispatch(
                NotificationEvent.HEALTH_RECOVERED,
                {
                    "label": label,
                    "kind": kind,
                    "message": f"{label} is working again.",
                    "since": "",
                },
                db=db,
            )
            new_notified_state = "ok"
            new_notified_at = now_utc.isoformat()
        else:
            new_notified_state = prev_notified_state
            new_notified_at = prev_notified_at_str

        db.upsert_health_check(
            key=key,
            kind=kind,
            label=label,
            state=new_state,
            message=new_message,
            failing_since=new_failing_since,
            last_checked_at=now_utc.isoformat(),
            notified_state=new_notified_state,
            notified_at=new_notified_at,
        )
        return new_state

    # Failing probe
    if existing and prev_state in ("warning", "error") and existing.get("failing_since"):
        failing_since_str = str(existing["failing_since"])
        failing_since_dt = _parse_utc_timestamp(failing_since_str) or now_utc
    else:
        failing_since_dt = now_utc
        failing_since_str = now_utc.isoformat()

    elapsed_minutes = (now_utc - failing_since_dt).total_seconds() / 60.0
    if threshold_minutes <= 0 or elapsed_minutes >= threshold_minutes:
        new_state = "error"
    else:
        new_state = "warning"

    new_notified_state = prev_notified_state
    new_notified_at = prev_notified_at_str

    if new_state == "error":
        should_notify = False
        if prev_state != "error":
            should_notify = True
        elif prev_notified_at_str and renotify_hours > 0:
            prev_notified_dt = _parse_utc_timestamp(prev_notified_at_str)
            if prev_notified_dt:
                hours_since = (now_utc - prev_notified_dt).total_seconds() / 3600.0
                if hours_since >= renotify_hours:
                    should_notify = True
            else:
                should_notify = True
        elif not prev_notified_at_str:
            should_notify = True

        if should_notify:
            notification_dispatcher.dispatch(
                NotificationEvent.HEALTH_ISSUE,
                {
                    "label": label,
                    "message": redact_text(message),
                    "since": failing_since_str,
                    "kind": kind,
                },
                db=db,
            )
            new_notified_state = "error"
            new_notified_at = now_utc.isoformat()

    db.upsert_health_check(
        key=key,
        kind=kind,
        label=label,
        state=new_state,
        message=redact_text(message),
        failing_since=failing_since_str,
        last_checked_at=now_utc.isoformat(),
        notified_state=new_notified_state,
        notified_at=new_notified_at,
    )
    return new_state


def _check_indexers(
    db: Any, now: datetime, settings: dict[str, Any]
) -> tuple[int, int, int]:
    checked = 0
    errors = 0
    warnings = 0
    seen_keys: list[str] = []
    threshold_minutes = int(settings.get("indexer_fail_minutes", 15))

    try:
        indexers = db.list_indexers(enabled_only=True)
    except sqlite3.Error as exc:
        logger.warning("Failed to list indexers for health check: %s", safe_exc(exc))
        indexers = []

    for cfg in indexers:
        indexer_id = str(cfg.get("id") or "")
        name = cfg.get("name") or indexer_id
        key = f"indexer:{indexer_id}"
        seen_keys.append(key)
        label = f"Indexer {name}"

        try:
            driver = get_indexer_driver(cfg)
            ok, msg = driver.test_connection()
            if not ok and not msg:
                msg = "Connection test failed"
        except (httpx.HTTPError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
            logger.warning("Indexer probe exception for %s: %s", key, safe_exc(exc))
            ok = False
            msg = redact_text(str(exc)) or type(exc).__name__

        state = observe(
            db,
            key=key,
            kind="indexer",
            label=label,
            ok=ok,
            message=msg,
            threshold_minutes=threshold_minutes,
            now=now,
            settings=settings,
        )
        checked += 1
        if state == "error":
            errors += 1
        elif state == "warning":
            warnings += 1

    try:
        db.delete_health_checks_except("indexer", seen_keys)
    except sqlite3.Error as exc:
        logger.warning("Failed to prune old indexer health checks: %s", safe_exc(exc))

    return checked, errors, warnings


def _check_download_clients(
    db: Any, now: datetime, settings: dict[str, Any]
) -> tuple[int, int, int]:
    checked = 0
    errors = 0
    warnings = 0
    seen_keys: list[str] = []
    threshold_minutes = int(settings.get("client_fail_minutes", 5))

    try:
        clients = db.list_download_clients(enabled_only=True)
    except sqlite3.Error as exc:
        logger.warning("Failed to list download clients for health check: %s", safe_exc(exc))
        clients = []

    for cfg in clients:
        client_id = str(cfg.get("id") or "")
        name = cfg.get("name") or client_id
        key = f"client:{client_id}"
        seen_keys.append(key)
        label = f"Download client {name}"

        try:
            driver = get_acquisition_driver(cfg)
            ok, msg = driver.test_connection()
            if not ok and not msg:
                msg = "Connection test failed"
        except (httpx.HTTPError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
            logger.warning("Download client probe exception for %s: %s", key, safe_exc(exc))
            ok = False
            msg = redact_text(str(exc)) or type(exc).__name__

        state = observe(
            db,
            key=key,
            kind="download_client",
            label=label,
            ok=ok,
            message=msg,
            threshold_minutes=threshold_minutes,
            now=now,
            settings=settings,
        )
        checked += 1
        if state == "error":
            errors += 1
        elif state == "warning":
            warnings += 1

    try:
        db.delete_health_checks_except("download_client", seen_keys)
    except sqlite3.Error as exc:
        logger.warning("Failed to prune old download client health checks: %s", safe_exc(exc))

    return checked, errors, warnings


def _check_media_server(
    db: Any, config: Any, now: datetime, settings: dict[str, Any]
) -> tuple[int, int, int]:
    server_type = getattr(config, "media_server_type", None) or MEDIA_SERVER_NONE
    if server_type == MEDIA_SERVER_NONE:
        try:
            db.delete_health_checks_except("media_server", [])
        except sqlite3.Error as exc:
            logger.warning("Failed to delete media_server health checks: %s", safe_exc(exc))
        return 0, 0, 0

    threshold_minutes = int(settings.get("media_server_fail_minutes", 10))
    key = "media_server"
    label = "Media server"

    try:
        server = get_media_server(config)
        if server is None:
            ok = False
            msg = "cannot be reached"
        else:
            conn_test = server.test_connection()
            ok = bool(conn_test.ok)
            msg = conn_test.message or ("Connected" if ok else "cannot be reached")
    except (httpx.HTTPError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        logger.warning("Media server probe exception: %s", safe_exc(exc))
        ok = False
        msg = redact_text(str(exc)) or type(exc).__name__

    state = observe(
        db,
        key=key,
        kind="media_server",
        label=label,
        ok=ok,
        message=msg,
        threshold_minutes=threshold_minutes,
        now=now,
        settings=settings,
    )
    try:
        db.delete_health_checks_except("media_server", [key])
    except sqlite3.Error as exc:
        logger.warning("Failed to prune media_server health checks: %s", safe_exc(exc))

    checked = 1
    errors = 1 if state == "error" else 0
    warnings = 1 if state == "warning" else 0
    return checked, errors, warnings


def _check_disks(
    db: Any, now: datetime, settings: dict[str, Any]
) -> tuple[int, int, int]:
    checked = 0
    errors = 0
    warnings = 0
    seen_keys: list[str] = []
    disk_free_min_gb = int(settings.get("disk_free_min_gb", 20))
    min_free_bytes = disk_free_min_gb * (1024 ** 3)

    candidate_paths: list[str] = []
    try:
        mm = db.get_media_management_settings()
        root_path = mm.get("root_folder_path")
        if root_path:
            candidate_paths.append(str(root_path))
    except sqlite3.Error as exc:
        logger.warning("Failed to read media management settings: %s", safe_exc(exc))

    try:
        for r in allowed_roots_for_all_clients(db).usable_roots():
            candidate_paths.append(str(r))
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Failed to get allowed roots: %s", safe_exc(exc))

    seen_paths: set[str] = set()
    unique_paths: list[str] = []
    for p in candidate_paths:
        p_str = p.strip()
        if p_str and p_str not in seen_paths:
            seen_paths.add(p_str)
            unique_paths.append(p_str)

    for path in unique_paths:
        key = f"disk:{path}"
        seen_keys.append(key)
        label = f"Disk {path}"

        try:
            usage = shutil.disk_usage(path)
            free_bytes = usage.free
            free_gb = free_bytes / (1024 ** 3)
            if free_bytes < min_free_bytes:
                ok = False
                msg = f"Only {free_gb:.1f} GB free on {path}"
            else:
                ok = True
                msg = f"{free_gb:.1f} GB free"
        except FileNotFoundError:
            ok = False
            msg = "folder not found"
        except (httpx.HTTPError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
            if not os.path.exists(path):
                ok = False
                msg = "folder not found"
            else:
                logger.warning("Disk check exception for %s: %s", path, safe_exc(exc))
                ok = False
                msg = redact_text(str(exc)) or type(exc).__name__

        state = observe(
            db,
            key=key,
            kind="disk",
            label=label,
            ok=ok,
            message=msg,
            threshold_minutes=0,
            now=now,
            settings=settings,
        )
        checked += 1
        if state == "error":
            errors += 1
        elif state == "warning":
            warnings += 1

    try:
        db.delete_health_checks_except("disk", seen_keys)
    except sqlite3.Error as exc:
        logger.warning("Failed to prune old disk health checks: %s", safe_exc(exc))

    return checked, errors, warnings


def _check_stalled_downloads(
    db: Any, now: datetime, settings: dict[str, Any]
) -> int:
    alerts = 0
    stall_minutes = int(settings.get("download_stall_minutes", 60))
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    try:
        downloading_status = DownloadStatus.DOWNLOADING.value
        downloads = db.list_active_downloads(statuses=[downloading_status])
    except sqlite3.Error as exc:
        logger.warning("Failed to list active downloads for stall check: %s", safe_exc(exc))
        return 0

    for dl in downloads:
        dl_id = str(dl.get("id") or "")
        if not dl_id:
            continue
        ts_str = dl.get("progress_updated_at")
        dt = _parse_utc_timestamp(ts_str)
        if dt is None:
            continue
        elapsed_seconds = (now_utc - dt).total_seconds()
        elapsed_minutes = int(elapsed_seconds / 60)
        if elapsed_minutes >= stall_minutes and not db.has_health_item_alert(
            NotificationEvent.DOWNLOAD_STALLED.value, dl_id
        ):
            artist = dl.get("artist") or "Unknown Artist"
            title = dl.get("title") or "Unknown Title"
            client = dl.get("client_name") or "client"
            notification_dispatcher.dispatch(
                NotificationEvent.DOWNLOAD_STALLED,
                {
                    "artist": artist,
                    "title": title,
                    "client": client,
                    "minutes": elapsed_minutes,
                },
                db=db,
            )
            db.record_health_item_alert(
                NotificationEvent.DOWNLOAD_STALLED.value, dl_id, now_utc.isoformat()
            )
            alerts += 1

    return alerts


def _check_held_imports(
    db: Any, now: datetime, settings: dict[str, Any]
) -> int:
    alerts = 0
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    try:
        downloads = db.list_active_downloads()
    except sqlite3.Error as exc:
        logger.warning("Failed to list active downloads for held import check: %s", safe_exc(exc))
        return 0

    for dl in downloads:
        dl_id = str(dl.get("id") or "")
        if not dl_id:
            continue
        unmatched = dl.get("unmatched_files")
        if not (isinstance(unmatched, list) and len(unmatched) > 0):
            continue

        if not db.has_health_item_alert(NotificationEvent.IMPORT_NEEDS_ATTENTION.value, dl_id):
            artist = dl.get("artist") or "Unknown Artist"
            title = dl.get("title") or "Unknown Title"
            count = len(unmatched)
            file_word = "file" if count == 1 else "files"
            count_msg = f"{count} unmatched {file_word} held."
            reason = str(dl.get("error_message") or count_msg)
            notification_dispatcher.dispatch(
                NotificationEvent.IMPORT_NEEDS_ATTENTION,
                {
                    "artist": artist,
                    "title": title,
                    "reason": reason,
                },
                db=db,
            )
            db.record_health_item_alert(
                NotificationEvent.IMPORT_NEEDS_ATTENTION.value, dl_id, now_utc.isoformat()
            )
            alerts += 1

    return alerts


def _prune_alerts(db: Any) -> None:
    try:
        all_active = db.list_active_downloads()
        active_ids = [str(d["id"]) for d in all_active if d.get("id")]
        db.prune_health_item_alerts(active_ids)
    except sqlite3.Error as exc:
        logger.warning("Failed to prune health item alerts: %s", safe_exc(exc))


def run_health_checks(
    db: Any, config: Any, now: datetime | None = None
) -> dict[str, int]:
    """Runs all health probes and item alerts. Returns counts dict."""
    if now is None:
        now = datetime.now(timezone.utc)
    settings = db.get_health_settings()

    checked = 0
    errors = 0
    warnings = 0
    alerts = 0

    c, e, w = _check_indexers(db, now, settings)
    checked += c
    errors += e
    warnings += w

    c, e, w = _check_download_clients(db, now, settings)
    checked += c
    errors += e
    warnings += w

    c, e, w = _check_media_server(db, config, now, settings)
    checked += c
    errors += e
    warnings += w

    c, e, w = _check_disks(db, now, settings)
    checked += c
    errors += e
    warnings += w

    alerts += _check_stalled_downloads(db, now, settings)
    alerts += _check_held_imports(db, now, settings)
    _prune_alerts(db)

    return {
        "checked": checked,
        "errors": errors,
        "warnings": warnings,
        "alerts": alerts,
    }
