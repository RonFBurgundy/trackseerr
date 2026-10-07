"""Task manager core: the registry of background tasks, persisted run history and editable schedules.

* ``TASKS`` is the one table describing every background job (kind, default interval, editable presets).
* ``record_task_run`` is the one context manager every execution path (scheduled loop iteration, manual run, startup,
  event trigger) wraps itself in. It writes a ``task_runs`` row on start and finishes it as success, failed or
  cancelled. Recording problems never break the work being recorded.
* ``effective_interval_seconds`` resolves a task's interval: DB override > config/env value > code default.
* ``wait_for_next_cycle`` is the sleep every interval loop uses. It re-reads the effective interval each second, so
  an edited schedule takes effect without a restart.

This module imports nothing heavy so any worker can import it without a cycle.
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from plex_playlist_sync.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

KIND_INTERVAL = "interval"  # fixed period between runs
KIND_CONTINUOUS = "continuous"  # event/queue-driven loop with a short poll; only ticks that did work are recorded
KIND_MANUAL = "manual"  # runs on demand only

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_STARTUP = "startup"
TRIGGER_EVENT = "event"

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

RETENTION_DAYS = 7
PRUNE_EVERY_SECONDS = 24 * 3600
MESSAGE_LIMIT = 500
OVERRIDE_KEY_PREFIX = "task_schedule_override:"

_MIN = 60
_HOUR = 3600
_DAY = 86400
# The only intervals an admin may pick from: 5m, 15m, 30m, 1h, 2h, 6h, 12h, 24h, 7d.
ALL_PRESETS: tuple[int, ...] = (5 * _MIN, 15 * _MIN, 30 * _MIN, _HOUR, 2 * _HOUR, 6 * _HOUR, 12 * _HOUR, _DAY, 7 * _DAY)


def _presets(minimum: int, maximum: Optional[int] = None) -> tuple[int, ...]:
    return tuple(p for p in ALL_PRESETS if p >= minimum and (maximum is None or p <= maximum))


@dataclass(frozen=True)
class TaskSpec:
    id: str
    name: str
    description: str
    kind: str
    default_interval_seconds: Optional[int] = None  # code default; config/env may override it (see _CONFIG_INTERVALS)
    presets: tuple[int, ...] = ()  # empty = schedule not editable

    @property
    def editable(self) -> bool:
        return self.kind == KIND_INTERVAL and bool(self.presets)


# Every background job. Keep in step with WORKER_THREAD_TASKS below (a test enforces completeness).
TASKS: dict[str, TaskSpec] = {
    spec.id: spec
    for spec in (
        # Disk scan is started by an admin (or the setup flow); there is no scheduled scan.
        TaskSpec(
            "filesystem_scan",
            "Media Library Disk Scanner",
            "Scans local audio storage, extracts Mutagen tags, and indexes media into the library catalog.",
            KIND_MANUAL,
        ),
        # Calls Spotify/Deezer/Plex: not more often than every 15m to stay clear of rate limits. Config: WAIT_SECONDS.
        TaskSpec(
            "playlist_sync",
            "Plex Playlist Sync",
            "Synchronizes enabled Spotify and Deezer playlists with Plex media server users.",
            KIND_INTERVAL,
            _DAY,
            _presets(15 * _MIN),
        ),
        # Searches indexers per wanted item: >=15m so indexer API limits are respected.
        # Config: BACKLOG_SEARCH_INTERVAL_MINUTES.
        TaskSpec(
            "wanted_backlog_sweep",
            "Monitored Missing & Upgrade Search Sweep",
            "Sweeps unfulfilled requests and missing library tracks against indexers for new releases or quality upgrades.",
            KIND_INTERVAL,
            _HOUR,
            _presets(15 * _MIN),
        ),
        # RSS feeds are cheap but indexers ban aggressive polling: >=15m; beyond 6h it stops being "recent releases".
        # Config: RSS_SYNC_INTERVAL_MINUTES.
        TaskSpec(
            "indexer_rss_sync",
            "Torznab / Newznab RSS Sync",
            "Monitors indexer recent releases for incoming tracks and albums matching pending requests.",
            KIND_INTERVAL,
            15 * _MIN,
            _presets(15 * _MIN, 6 * _HOUR),
        ),
        # Paced pushes into Lidarr (MusicBrainz rate limits): >=15m. Settings: Lidarr auto-trickle interval.
        TaskSpec(
            "lidarr_auto_trickle",
            "Lidarr Trickle Worker",
            "Trickles missing tracks into Lidarr with paced delays to prevent MusicBrainz rate limits.",
            KIND_INTERVAL,
            30 * _MIN,
            _presets(15 * _MIN),
        ),
        # Polls active download clients every few seconds; only ticks that found downloads are recorded.
        TaskSpec(
            "download_queue_monitor",
            "Acquisition Worker",
            "Monitors active download clients, processes finished downloads, tags audio files, and moves them to the library.",
            KIND_CONTINUOUS,
        ),
        # Hits MusicBrainz/Deezer for every stale artist: >=6h (the worker also skips artists refreshed recently).
        TaskSpec(
            "artist_metadata_refresh",
            "Artist Metadata & Discography Refresh",
            "Refreshes artist metadata, canonical discographies, full tracklists, and artwork cache from BrainzMash / MusicBrainz and Deezer.",
            KIND_INTERVAL,
            _DAY,
            _presets(6 * _HOUR),
        ),
        # Local file stats only and missing-only, so it may run often (>=1h); also triggered by scans and Lidarr imports.
        TaskSpec(
            "art_thumbnail_backfill",
            "Artwork Thumbnail Backfill",
            "Generates missing 250/500px thumbnails and version tokens for artwork already on disk. Skips finished items, so it is safe to re-run.",
            KIND_INTERVAL,
            _DAY,
            _presets(_HOUR),
        ),
        # Talks to download clients and deletes files: >=1h is plenty frequent.
        TaskSpec(
            "seed_cleanup",
            "Seed Cleanup",
            "Removes finished torrents per the 'When seeding is done' setting and flags torrents TrackSeerr no longer tracks for review.",
            KIND_INTERVAL,
            _DAY,
            _presets(_HOUR),
        ),
        # Walks the whole music folder and the media server index: heavy, so daily at most.
        TaskSpec(
            "library_health",
            "Library Health Check",
            "Compares the music folder with what the media server indexes and records what is missing, stale or weakly matched.",
            KIND_INTERVAL,
            7 * _DAY,
            _presets(_DAY),
        ),
        # Directory pruning by age; the retention days setting decides what is deleted, this only sets how often to look.
        TaskSpec(
            "recycle_bin_cleanup",
            "Recycle Bin cleanup",
            "Deletes recycle bin folders older than the configured number of days.",
            KIND_INTERVAL,
            _DAY,
            _presets(6 * _HOUR),
        ),
        # Polls every minute, but each stuck request backs off on its own schedule; only sweeps that re-sent
        # something are recorded.
        TaskSpec(
            "lidarr_request_retry",
            "Retry Stuck Lidarr Requests",
            "Re-sends approved requests Lidarr could not finish yet (releases still loading, rate limits, failed monitoring) on a backoff schedule. Each stuck request backs off on its own schedule (2m, 5m, 15m, 1h, 6h, then daily); Lidarr is contacted only for requests that are due.",
            KIND_CONTINUOUS,
        ),
        # Polls every minute for delay-profile releases whose wait has passed; only ticks that released something
        # are recorded.
        TaskSpec(
            "pending_releases",
            "Delay-Profile Pending Releases",
            "Grabs releases held back by a delay profile once their wait is over, after re-checking they are still wanted.",
            KIND_CONTINUOUS,
        ),
        # Wakes every 5 minutes; each import list has its own schedule, so only ticks that synced a list are recorded.
        TaskSpec(
            "import_list_sync",
            "Import List Sync",
            "Syncs the import lists (Spotify, Last.fm, ListenBrainz and others) that are due.",
            KIND_CONTINUOUS,
        ),
        # Regenerates tailored mixes that are due. Generation reads listening history only: 30m to daily.
        TaskSpec(
            "mix_generation",
            "Tailored Mix Generation",
            "Regenerates the tailored mixes that are due from listening history and syncs them to the media server.",
            KIND_INTERVAL,
            _HOUR,
            _presets(30 * _MIN, _DAY),
        ),
        # Ticks every 30s; the history poll and forward retries gate themselves, so only ticks that did work are
        # recorded.
        TaskSpec(
            "scrobble_sync",
            "Scrobble Sync",
            "Polls the media server play history and forwards scrobbles to Last.fm and ListenBrainz, retrying failures.",
            KIND_CONTINUOUS,
        ),
    )
}

# Interval tasks whose period comes from config/env or settings when no override is set (returns seconds or None).
_CONFIG_INTERVALS: dict[str, Callable[[Any, Any], Optional[int]]] = {}


def _positive(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _sync_interval(db: Any, config: Any) -> Optional[int]:
    return _positive(getattr(config, "wait_seconds", None))


def _backlog_interval(db: Any, config: Any) -> Optional[int]:
    minutes = _positive(getattr(config, "backlog_search_interval_minutes", None))
    return minutes * 60 if minutes else None


def _rss_interval(db: Any, config: Any) -> Optional[int]:
    minutes = _positive(getattr(config, "rss_sync_interval_minutes", None))
    return minutes * 60 if minutes else None


def _trickle_interval(db: Any, config: Any) -> Optional[int]:
    minutes: Optional[int] = None
    try:
        settings = db.get_lidarr_settings()
        if isinstance(settings, dict):
            minutes = _positive(settings.get("auto_trickle_interval_minutes"))
    except sqlite3.Error as exc:
        logger.warning("Could not read the Lidarr trickle interval setting: %s", safe_exc(exc))
    if minutes is None:
        minutes = _positive(getattr(config, "lidarr_auto_trickle_interval_minutes", None))
    return minutes * 60 if minutes else None


_CONFIG_INTERVALS.update(
    {
        "playlist_sync": _sync_interval,
        "wanted_backlog_sweep": _backlog_interval,
        "indexer_rss_sync": _rss_interval,
        "lidarr_auto_trickle": _trickle_interval,
    }
)


# Every thread the package starts, mapped to the registry task it runs. tests/test_task_manager.py fails when a new
# constant-named thread appears that is in neither this table nor NON_TASK_THREADS.
WORKER_THREAD_TASKS: dict[str, str] = {
    "ScheduledSyncWorker": "playlist_sync",
    "ScheduledLidarrTrickleWorker": "lidarr_auto_trickle",
    "LidarrTrickleWorkerThread": "lidarr_auto_trickle",
    "WantedBacklogWorkerThread": "wanted_backlog_sweep",
    "WantedManualSearchThread": "wanted_backlog_sweep",
    "RSSSyncWorkerThread": "indexer_rss_sync",
    "ArtistRefreshWorkerThread": "artist_metadata_refresh",
    "AutoArtistHydrationThread": "artist_metadata_refresh",
    "AcquisitionWorkerThread": "download_queue_monitor",
    "PendingReleaseWorkerThread": "pending_releases",
    "ImportListWorkerThread": "import_list_sync",
    "MixWorkerThread": "mix_generation",
    "ScrobbleWorkerThread": "scrobble_sync",
    "LibraryScannerThread": "filesystem_scan",
    "LibraryHealthWorkerThread": "library_health",
    "LibraryHealthCheck": "library_health",
    "SeedCleanupWorkerThread": "seed_cleanup",
    "SeedCleanup": "seed_cleanup",
    "RecycleBinWorkerThread": "recycle_bin_cleanup",
    "RecycleBinCleanup": "recycle_bin_cleanup",
    "ArtStartupBackfill": "art_thumbnail_backfill",
    "ArtBackfillScheduler": "art_thumbnail_backfill",
    "ArtBackfillEvent": "art_thumbnail_backfill",
    "ManualScanTask": "filesystem_scan",
    "ManualSyncTask": "playlist_sync",
    "ManualBacklogTask": "wanted_backlog_sweep",
    "ManualRSSTask": "indexer_rss_sync",
    "ManualLidarrTask": "lidarr_auto_trickle",
    "ManualRequestRetryTask": "lidarr_request_retry",
    "ManualAcquisitionTask": "download_queue_monitor",
    "ManualArtistRefreshTask": "artist_metadata_refresh",
    "ArtBackfillTask": "art_thumbnail_backfill",
}

NON_TASK_THREADS: dict[str, str] = {
    "GatewayLinkWorker": "heartbeat of the gateway process to the core; runs in the gateway, which serves no task API "
    "and keeps no run history; the core reports gateway health separately",
    "media-server-probe": "one-shot connectivity probe with a timeout, started per status request",
    "lidarr-list-refresh": "request-driven cache refresh of the Lidarr list snapshot, not a scheduled job",
    "LidarrMigrationThread": "one-off user-started migration with its own status endpoint",
    "BootInit": "one-shot post-bind startup sequence (starts the real workers), not a recurring job",
    "BootListenLog": "one-shot boot log line once the server socket is bound",
    "media-server-user-discovery": "one-shot user import when a media server connects, not a recurring job",
}


# ---------------------------------------------------------------------------------------------------------------
# Run recording
# ---------------------------------------------------------------------------------------------------------------


class TaskCancelled(Exception):
    """Raise inside ``record_task_run`` to finish the run as cancelled."""


class RunHandle:
    """Lets the body of a ``record_task_run`` block attach a message or mark the run cancelled / failed / a no-op."""

    def __init__(self) -> None:
        self.message: Optional[str] = None
        self.cancelled = False
        self.failed: Optional[str] = None  # set when the run ended badly without raising
        self.discard = False  # the run turned out to be a no-op: remove the row

    def apply_result(self, result: Any) -> None:
        """Takes the outcome from a worker's result dict (``status == already_running`` discards the run)."""
        if not isinstance(result, dict):
            return
        if result.get("status") == "already_running":
            self.discard = True
            return
        from plex_playlist_sync.job_tracker import summarize_result

        self.message = summarize_result(result)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    return redact_text(str(message))[:MESSAGE_LIMIT]


_prune_lock = threading.Lock()
_last_prune_monotonic: Optional[float] = None


def maybe_prune(db: Any, force: bool = False) -> int:
    """Deletes runs older than RETENTION_DAYS, at most once per PRUNE_EVERY_SECONDS per process. Returns rows removed."""
    global _last_prune_monotonic
    with _prune_lock:
        now = time.monotonic()
        if not force and _last_prune_monotonic is not None and now - _last_prune_monotonic < PRUNE_EVERY_SECONDS:
            return 0
        _last_prune_monotonic = now
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        removed = int(db.prune_task_runs(cutoff))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Could not prune task run history: %s", safe_exc(exc))
        return 0
    if removed:
        logger.info("Pruned %d task run(s) older than %d days", removed, RETENTION_DAYS)
    return removed


def startup_housekeeping(db: Any) -> int:
    """Marks runs a previous process left ``running`` as failed, and prunes old history. Returns rows closed."""
    closed = 0
    try:
        closed = int(db.fail_interrupted_task_runs(_now_iso(), "interrupted by restart"))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Could not close interrupted task runs: %s", safe_exc(exc))
    if closed:
        logger.info("Marked %d interrupted task run(s) as failed", closed)
    maybe_prune(db, force=True)
    return closed


def _begin(db: Any, task_id: str, trigger: str) -> Optional[int]:
    maybe_prune(db)
    try:
        return int(db.start_task_run(task_id, trigger, _now_iso()))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Could not record the start of task '%s': %s", task_id, safe_exc(exc))
        return None


def _end(db: Any, run_id: Optional[int], task_id: str, status: str, message: Optional[str], t0: float) -> None:
    if run_id is None:
        return
    try:
        db.finish_task_run(run_id, status, _now_iso(), _clip(message), int((time.monotonic() - t0) * 1000))
    except sqlite3.Error as exc:
        logger.warning("Could not record the end of task '%s': %s", task_id, safe_exc(exc))


@contextmanager
def record_task_run(db: Any, task_id: str, trigger: str) -> Iterator[RunHandle]:
    """Records one execution of ``task_id``. An exception finishes the run as failed (cause logged) and propagates,
    so the caller's own error handling is unchanged. ``TaskCancelled`` (or ``handle.cancelled``) finishes it as
    cancelled; ``handle.discard`` removes the row."""
    handle = RunHandle()
    t0 = time.monotonic()
    run_id = _begin(db, task_id, trigger)
    try:
        yield handle
    except TaskCancelled:
        _end(db, run_id, task_id, STATUS_CANCELLED, handle.message or "cancelled", t0)
    except Exception as exc:
        logger.error("Task '%s' (%s) failed: %s", task_id, trigger, safe_exc(exc))
        _end(db, run_id, task_id, STATUS_FAILED, safe_exc(exc), t0)
        raise
    except BaseException:
        # Interpreter shutdown or thread teardown: the run did not complete, and the exception must keep propagating.
        _end(db, run_id, task_id, STATUS_CANCELLED, "interrupted", t0)
        raise
    else:
        if handle.discard:
            if run_id is not None:
                try:
                    db.delete_task_run(run_id)
                except sqlite3.Error as exc:
                    logger.warning("Could not discard the no-op run of '%s': %s", task_id, safe_exc(exc))
        elif handle.failed is not None:
            _end(db, run_id, task_id, STATUS_FAILED, handle.failed, t0)
        elif handle.cancelled:
            _end(db, run_id, task_id, STATUS_CANCELLED, handle.message or "cancelled", t0)
        else:
            _end(db, run_id, task_id, STATUS_SUCCESS, handle.message, t0)


def record_finished_run(
    db: Any,
    task_id: str,
    trigger: str,
    started_monotonic: float,
    message: Optional[str] = None,
    status: str = STATUS_SUCCESS,
) -> None:
    """Records an already-finished run. Polling loops use it so idle ticks write nothing: they time the tick, and
    call this only when it did work."""
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    finished = datetime.now(timezone.utc)
    started = finished - timedelta(milliseconds=duration_ms)
    try:
        run_id = int(db.start_task_run(task_id, trigger, started.isoformat()))
        db.finish_task_run(run_id, status, finished.isoformat(), _clip(message), duration_ms)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Could not record a run of task '%s': %s", task_id, safe_exc(exc))


# ---------------------------------------------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------------------------------------------


def override_key(task_id: str) -> str:
    return OVERRIDE_KEY_PREFIX + task_id


def get_interval_override(db: Any, task_id: str) -> Optional[int]:
    try:
        raw = db.get_kv(override_key(task_id))
    except sqlite3.Error as exc:
        logger.warning("Could not read the schedule override of '%s': %s", task_id, safe_exc(exc))
        return None
    if not isinstance(raw, str):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def set_interval_override(db: Any, task_id: str, seconds: Optional[int]) -> None:
    """Stores a preset interval for the task; ``None`` clears the override (back to config/default)."""
    if seconds is None:
        db.delete_kv(override_key(task_id))
    else:
        db.set_kv(override_key(task_id), str(int(seconds)))


def default_interval_seconds(db: Any, config: Any, task_id: str) -> Optional[int]:
    """What a reset returns to: the config/env/settings value when set, else the code default."""
    spec = TASKS.get(task_id)
    if spec is None or spec.kind != KIND_INTERVAL:
        return None
    resolver = _CONFIG_INTERVALS.get(task_id)
    configured = resolver(db, config) if resolver is not None and config is not None else None
    return configured or spec.default_interval_seconds


def effective_interval_seconds(db: Any, config: Any, task_id: str) -> Optional[int]:
    """DB override > config/env value > code default. None for tasks without an interval."""
    spec = TASKS.get(task_id)
    if spec is None or spec.kind != KIND_INTERVAL:
        return None
    if spec.editable:
        override = get_interval_override(db, task_id)
        if override is not None:
            return override
    return default_interval_seconds(db, config, task_id)


def interval_fn(db: Any, config: Any, task_id: str) -> Callable[[], float]:
    """A callable returning the task's current effective interval. Falls back to the last good value on errors."""
    last: list[float] = [float(default_interval_seconds(db, config, task_id) or 60)]

    def current() -> float:
        try:
            value = effective_interval_seconds(db, config, task_id)
        except sqlite3.Error as exc:
            logger.warning("Could not resolve the interval of '%s': %s", task_id, safe_exc(exc))
            return last[0]
        if value:
            last[0] = float(value)
        return last[0]

    return current


def wait_for_next_cycle(stop_event: threading.Event, interval: Callable[[], float], step: float = 1.0) -> bool:
    """Sleeps until ``interval()`` seconds have passed, re-reading the interval every ``step`` seconds so a changed
    schedule takes effect at once (a shorter interval ends the sleep early, a longer one extends it).
    Returns True when ``stop_event`` was set."""
    t0 = time.monotonic()
    while True:
        remaining = float(interval()) - (time.monotonic() - t0)
        if remaining <= 0:
            return stop_event.is_set()
        if stop_event.wait(min(step, remaining)):
            return True


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def seconds_until_due(db: Any, task_id: str, interval_seconds: float) -> float:
    """Seconds until the next run: last finished run's end + interval. 0 when it never ran."""
    try:
        last = db.get_last_finished_task_run(task_id)
    except sqlite3.Error as exc:
        logger.warning("Could not read the last run of '%s': %s", task_id, safe_exc(exc))
        return 0.0
    finished = parse_iso(last.get("finished_at")) if last else None
    if finished is None:
        return 0.0
    return max(0.0, (finished + timedelta(seconds=interval_seconds) - datetime.now(timezone.utc)).total_seconds())


def next_run_at(spec: TaskSpec, interval_seconds: Optional[int], last_finished_at: Optional[str]) -> Optional[str]:
    """Last run end + interval for interval tasks that have run; None otherwise."""
    if spec.kind != KIND_INTERVAL or not interval_seconds:
        return None
    finished = parse_iso(last_finished_at)
    if finished is None:
        return None
    return (finished + timedelta(seconds=interval_seconds)).isoformat()


def format_interval(seconds: Optional[int]) -> str:
    if not seconds:
        return "Manual / On Demand"
    if seconds % _DAY == 0:
        return f"Every {seconds // _DAY}d" if seconds != _DAY else "Every 24h"
    if seconds % _HOUR == 0:
        return f"Every {seconds // _HOUR}h"
    if seconds % _MIN == 0:
        return f"Every {seconds // _MIN}m"
    return f"Every {seconds}s"
