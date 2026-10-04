"""Library-manager interlock: exactly one of TrackSeerr (``native``) or Lidarr (``lidarr``) manages the library.

The mode lives in ``media_management_settings.library_mode``. Everything that grabs, imports or requests music
asks this module which side is active, so two managers never work one library at the same time.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.lidarr_queue import lidarr_worker
from plex_playlist_sync.models import DownloadStatus
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

MODE_NATIVE = "native"
MODE_LIDARR = "lidarr"
LIBRARY_MODES = (MODE_NATIVE, MODE_LIDARR)

# Native acquisition rows that still need the worker (terminal states are ``failed`` and ``imported``).
_IN_FLIGHT_STATUSES = [
    DownloadStatus.QUEUED.value,
    DownloadStatus.DOWNLOADING.value,
    DownloadStatus.IMPORTING.value,
    DownloadStatus.COMPLETED.value,
]


class ModeChanged(Exception):
    """The library manager is not the mode a unit of work expected; skip or re-route it (admin actions: 409)."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Library manager is {actual}, not {expected}")
        self.expected = expected
        self.actual = actual


class SwitchRefused(Exception):
    """A mode switch was refused because work is in flight; ``reason`` is safe to show to the admin."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Guard state. ``_guard_lock`` is only ever held for a counter update plus one mode read/write, never while work
# runs, so holders cannot deadlock against ``switch_mode``. Rule: never call ``switch_mode`` while holding a guard
# (it would see its own work and refuse).
_guard_lock = threading.Lock()
_in_flight: dict[str, int] = {MODE_NATIVE: 0, MODE_LIDARR: 0}


def acquire_work(db: Database, expected_mode: str) -> None:
    """Registers one unit of ``expected_mode`` work, or raises ``ModeChanged`` if another mode is active.

    Every call that succeeds must be paired with ``release_work``; prefer the ``work_guard`` context manager.
    """
    with _guard_lock:
        actual = get_library_mode(db)
        if actual != expected_mode:
            raise ModeChanged(expected_mode, actual)
        _in_flight[expected_mode] += 1


def release_work(expected_mode: str) -> None:
    with _guard_lock:
        _in_flight[expected_mode] = max(0, _in_flight[expected_mode] - 1)


@contextmanager
def work_guard(db: Database, expected_mode: str) -> Iterator[None]:
    """Holds the mode steady while a unit of work runs; ``ModeChanged`` on entry if the mode is not as expected."""
    acquire_work(db, expected_mode)
    try:
        yield
    finally:
        release_work(expected_mode)


def in_flight_count(mode: Optional[str] = None) -> int:
    with _guard_lock:
        return sum(_in_flight.values()) if mode is None else _in_flight.get(mode, 0)


def run_for_mode(db: Database, *, native: Any, lidarr: Any, attempts: int = 3) -> Any:
    """Runs ``native()`` or ``lidarr()`` for whichever mode is active, under that mode's guard.

    If the mode flips between reading it and taking the guard, re-routes to the new mode (up to ``attempts``), so a
    request is never silently dropped by a concurrent switch. Raises ``ModeChanged`` only if it keeps flipping.
    """
    last: Optional[ModeChanged] = None
    for _ in range(max(1, attempts)):
        mode = get_library_mode(db)
        try:
            with work_guard(db, mode):
                return lidarr() if mode == MODE_LIDARR else native()
        except ModeChanged as exc:
            last = exc
    assert last is not None
    raise last


def run_guarded(db: Database, fn: Any) -> Any:
    """Runs ``fn()`` under the guard of whichever mode is active, for work that branches on the mode itself.

    The mode cannot change while ``fn`` runs; ``ModeChanged`` only escapes if it flips on every attempt.
    """
    return run_for_mode(db, native=fn, lidarr=fn)


def get_library_mode(db: Database) -> str:
    """Returns the active mode; anything unreadable or unknown falls back to ``native`` (the schema default)."""
    try:
        mode = str(db.get_media_management_settings().get("library_mode") or MODE_NATIVE).strip().lower()
    except Exception as exc:  # sqlite3.Error and odd test doubles alike; the safe default is native
        logger.warning("Could not read library_mode; assuming native: %s", type(exc).__name__)
        return MODE_NATIVE
    return mode if mode in LIBRARY_MODES else MODE_NATIVE


def lidarr_is_configured(db: Database, config: Optional[Config] = None) -> bool:
    """True when a Lidarr URL and API key are available from the DB settings or the environment."""
    settings = db.get_lidarr_settings()
    if settings.get("url") and settings.get("api_key"):
        return True
    return bool(config is not None and config.has_lidarr)


def native_is_configured(db: Database) -> bool:
    """True with at least one enabled non-Lidarr download client and one enabled indexer (or slskd)."""
    clients = db.list_download_clients()
    has_client = any(c.get("enabled") and c.get("driver_type") != "lidarr" for c in clients)
    has_source = any(i.get("enabled") for i in db.list_indexers()) or any(
        c.get("driver_type") == "slskd" and c.get("enabled") for c in clients
    )
    return bool(has_client and has_source)


def _blocking_reason(db: Database, mode: str, busy: int) -> Optional[str]:
    if busy:
        noun = "operation" if busy == 1 else "operations"
        return f"{busy} {mode} {noun} in progress; wait for them to finish and try again."
    active = [
        d for d in db.list_active_downloads(statuses=_IN_FLIGHT_STATUSES) if d.get("client_driver_type") != "lidarr"
    ]
    if active:
        noun = "download" if len(active) == 1 else "downloads"
        return f"{len(active)} native {noun} still in progress; wait for them to finish or remove them first."
    if lidarr_worker.is_running():
        return "A Lidarr trickle is running; wait for it to finish or cancel it first."
    return None


def blocking_reason(db: Database) -> Optional[str]:
    """Why the mode cannot be switched right now, or None when nothing is in flight."""
    return _blocking_reason(db, get_library_mode(db), in_flight_count())


def switch_mode(db: Database, new_mode: str, source: str, user: Optional[str] = None) -> str:
    """Atomically switches the library manager; returns the previous mode.

    Under the guard lock (so no work unit can start or be mid-registration), refuses with ``SwitchRefused`` if any
    work is in flight or ``blocking_reason`` says so; otherwise writes the mode and a ``library_manager_changed``
    event. Raises ``sqlite3.Error`` if the write fails. A no-op switch to the current mode returns without writing.
    """
    if new_mode not in LIBRARY_MODES:
        raise ValueError(f"Unknown library mode: {new_mode!r}")
    with _guard_lock:
        current = get_library_mode(db)
        if current == new_mode:
            return current
        reason = _blocking_reason(db, current, sum(_in_flight.values()))
        if reason is not None:
            raise SwitchRefused(reason)
        db.update_media_management_settings({"library_mode": new_mode})
    try:
        db.record_event(
            "library_manager_changed",
            f"Library manager switched from {current} to {new_mode}",
            source=source,
            severity="info",
            details={"from": current, "to": new_mode, "user": user, "source": source},
        )
    except sqlite3.Error as exc:
        logger.warning("Failed to record library_manager_changed event: %s", safe_exc(exc))
    logger.info("Library manager switched from %s to %s (%s)", current, new_mode, source)
    return current


def get_status(db: Database, config: Optional[Config] = None) -> dict[str, Any]:
    reason = blocking_reason(db)
    return {
        "mode": get_library_mode(db),
        "lidarr_configured": lidarr_is_configured(db, config),
        "native_configured": native_is_configured(db),
        "can_switch": reason is None,
        "blocking_reason": reason,
    }


def build_lidarr_client(db: Database, config: Optional[Config]) -> Optional[LidarrClient]:
    """Builds a LidarrClient from the DB settings, falling back to (and seeding from) the environment config."""
    lidarr_settings = db.get_lidarr_settings()
    url = lidarr_settings.get("url")
    api_key = lidarr_settings.get("api_key")
    auto_search = lidarr_settings.get("auto_search", True)
    root_folder = lidarr_settings.get("root_folder")
    quality_profile_id = lidarr_settings.get("quality_profile_id")
    metadata_profile_id = lidarr_settings.get("metadata_profile_id")

    if not (url and api_key):
        if config is not None and config.has_lidarr:
            url = config.lidarr_url
            api_key = config.lidarr_api_key
            auto_search = config.lidarr_auto_search
            root_folder = config.lidarr_root_folder
            quality_profile_id = config.lidarr_quality_profile_id
            metadata_profile_id = config.lidarr_metadata_profile_id
            try:  # seed the DB so subsequent requests use it
                db.update_lidarr_settings(
                    {
                        "url": url,
                        "api_key": api_key,
                        "auto_search": auto_search,
                        "root_folder": root_folder,
                        "quality_profile_id": quality_profile_id,
                        "metadata_profile_id": metadata_profile_id,
                        "trickle_rate_seconds": config.lidarr_trickle_rate_seconds,
                        "trickle_batch_size": config.lidarr_trickle_batch_size,
                        "auto_trickle": config.lidarr_auto_trickle,
                        "auto_trickle_interval_minutes": config.lidarr_auto_trickle_interval_minutes,
                    }
                )
            except (sqlite3.Error, ValueError, TypeError) as exc:
                logger.warning("Failed to seed Lidarr settings to DB: %s", safe_exc(exc))
        else:
            return None

    try:
        return LidarrClient(
            base_url=str(url),
            api_key=str(api_key),
            verify_ssl=config.plex_verify_ssl if config is not None else True,
            auto_search=bool(auto_search),
            root_folder=root_folder,
            quality_profile_id=quality_profile_id,
            metadata_profile_id=metadata_profile_id,
            monitor_option=lidarr_settings.get("monitor_option"),
            tag_ids=lidarr_settings.get("tag_ids") or [],
        )
    except (AttributeError, ValueError, TypeError) as exc:
        logger.error("Failed to initialize LidarrClient: %s", safe_exc(exc))
        return None


def dispatch_to_lidarr(db: Database, client: Any, items: list[dict[str, Any]], config: Optional[Config] = None) -> bool:
    """Hands approved requests to the Lidarr trickle worker using the stored ``lidarr_settings``.

    Returns True when the worker accepted them. Refuses (False) outside lidarr mode, so no caller can reach Lidarr
    from native mode.
    """
    if get_library_mode(db) != MODE_LIDARR:
        logger.debug("Not dispatching %d request(s) to Lidarr: library_mode is native", len(items))
        return False
    if client is None:
        client = build_lidarr_client(db, config)
    if client is None:
        logger.warning("Lidarr mode is active but Lidarr is not configured; %d request(s) stay in processing", len(items))
        return False
    settings = db.get_lidarr_settings()
    delay = float(settings.get("trickle_rate_seconds") or (config.lidarr_trickle_rate_seconds if config else 3.0))
    result = lidarr_worker.start_trickle(
        items=items,
        client=client,
        db=db,
        delay_seconds=delay,
        auto_search=bool(settings.get("auto_search", True)),
        queue_if_running=True,
    )
    status = result.get("status")
    if status in ("started", "queued"):
        logger.info("Enqueued %d request(s) to Lidarr (%s)", len(items), status)
        return True
    logger.warning(
        "Lidarr trickle did not accept %d request(s) (status=%s: %s); they stay in processing and can be retried",
        len(items),
        status,
        result.get("message"),
    )
    return False


LIBRARY_MODE_MIGRATION_KEY = "library_mode_migrated_v33"


def migrate_library_mode(db: Database, config: Optional[Config] = None) -> Optional[str]:
    """One-time upgrade step: keep Lidarr-only installs on Lidarr now that routing is strictly mode-driven.

    Runs at startup (after config load) rather than as a pure SQL schema migration because Lidarr can be configured
    purely through ``LIDARR_URL`` / ``LIDARR_API_KEY``, which the database cannot see. Guarded by a kv marker so it
    runs exactly once; an admin's later choice is never overridden. Returns the mode it set (or kept), or None when it
    had already run.

    Lidarr only -> ``lidarr``; native only, neither, or both -> stays ``native`` (both also logs a WARNING and records
    a system event asking the admin to choose in Settings -> General).
    """
    try:
        if db.get_kv(LIBRARY_MODE_MIGRATION_KEY):
            return None
        lidarr = lidarr_is_configured(db, config) or any(
            c.get("enabled") and c.get("driver_type") == "lidarr" for c in db.list_download_clients()
        )
        native = native_is_configured(db)
        result = MODE_NATIVE
        if lidarr and not native:
            db.update_media_management_settings({"library_mode": MODE_LIDARR})
            result = MODE_LIDARR
            logger.info("Library manager migration: Lidarr-only install detected; library_mode set to 'lidarr'")
            db.record_event(
                "library_mode_migrated",
                "Existing Lidarr-only setup detected: Lidarr is now the library manager. "
                "Change this in Settings -> General.",
                source="library_manager",
                details={"mode": MODE_LIDARR},
            )
        elif lidarr and native:
            logger.warning(
                "Both Lidarr and native download clients/indexers are configured; library_mode stays 'native'. "
                "Choose the library manager in Settings -> General."
            )
            db.record_event(
                "library_mode_choice_needed",
                "Both Lidarr and native downloading are configured. TrackSeerr is managing the library; "
                "choose Lidarr or TrackSeerr in Settings -> General.",
                source="library_manager",
                severity="warning",
                details={"mode": MODE_NATIVE},
            )
        db.set_kv(LIBRARY_MODE_MIGRATION_KEY, result)
        return result
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logger.error("Library manager migration failed; will retry next boot: %s", safe_exc(exc))
        return None
