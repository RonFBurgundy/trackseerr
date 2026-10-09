"""Background worker that regenerates due tailored mixes (checked hourly)."""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests
from plexapi.exceptions import PlexApiException

from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.mb_metadata_store import get_shared_discovery_client
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database
from plex_playlist_sync.task_manager import TRIGGER_SCHEDULED, TRIGGER_STARTUP, record_task_run, wait_for_next_cycle
from plex_playlist_sync.tailored_mixes import InsufficientHistoryError, generate_and_sync

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600
MIX_INTERVALS = {
    "discover_weekly": timedelta(days=7),
    "daily_blend": timedelta(days=1),
    "artist_radio": timedelta(days=1),
}


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def is_due(config_row: dict[str, Any], now: datetime) -> bool:
    """A mix is due at ``last_generated_at + interval``; never-generated mixes are due immediately."""
    if not config_row.get("enabled"):
        return False
    interval = MIX_INTERVALS.get(config_row.get("mix_type", ""))
    if interval is None:
        return False
    last = config_row.get("last_generated_at")
    if not last:
        return True
    try:
        last_dt = _as_utc(datetime.fromisoformat(str(last)))
    except ValueError:
        logger.warning("MixWorker: unparseable last_generated_at %r for mix %s", last, config_row.get("id"))
        return True
    return _as_utc(now) >= last_dt + interval


class MixWorker:
    """Regenerates enabled tailored mixes whose interval has elapsed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._iteration_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._plex: Optional[PlexClient] = None
        self._discovery: Optional[DiscoveryClient] = None
        self.last_run_at: Optional[str] = None
        self.generated = 0
        self.errors = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._is_running,
                "last_run_at": self.last_run_at,
                "generated": self.generated,
                "errors": self.errors,
            }

    def run_iteration(
        self,
        db: Database,
        config: Config,
        plex_client: Any,
        discovery: Any,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Generate every due mix (or all configured mixes when force=True). One mix failing never stops the others."""
        now = _as_utc(now or datetime.now(timezone.utc))
        result = {"due": 0, "generated": 0, "errors": 0}
        for row in db.list_mix_configs():
            if not force and not is_due(row, now):
                continue
            result["due"] += 1
            try:
                generate_and_sync(db, plex_client, discovery, row, config)
                result["generated"] += 1
            except InsufficientHistoryError as exc:
                logger.info("MixWorker: mix %s skipped: %s", row["id"], exc)
            except (PlexApiException, requests.RequestException, ValueError, RuntimeError) as exc:
                logger.error("MixWorker: mix %s failed (%s)", row["id"], type(exc).__name__)
                result["errors"] += 1
        with self._lock:
            self.last_run_at = now.isoformat(timespec="seconds")
            self.generated += result["generated"]
            self.errors += result["errors"]
        return result

    def run_now(self, db: Database, config: Config) -> dict[str, int]:
        """Manually run tailored mix generation now for every configured mix."""
        plex = self._get_plex(config)
        if self._discovery is None:
            self._discovery = get_shared_discovery_client(db)
        with self._iteration_lock:
            return self.run_iteration(db, config, plex, self._discovery, force=True)

    def _get_plex(self, config: Config) -> Optional[PlexClient]:
        if self._plex is not None:
            return self._plex
        if not config.plex_enabled:
            return None
        try:
            self._plex = PlexClient(
                config.plex_url,
                config.plex_token,
                verify_ssl=config.plex_verify_ssl,
                music_section=config.plex_music_section,
            )
        except (PlexApiException, requests.RequestException) as exc:
            logger.warning("MixWorker: could not connect to Plex (%s)", type(exc).__name__)
            self._plex = None
        return self._plex

    def start(
        self,
        db: Database,
        config: Config,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
        plex_factory: Optional[Callable[[Config], Optional[Any]]] = None,
        discovery: Optional[Any] = None,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        with self._lock:
            if self._is_running:
                logger.warning("MixWorker: Already running")
                return False
            self._stop_event.clear()
            self._is_running = True
        self._discovery = discovery or get_shared_discovery_client(db)
        cycle_interval = interval_fn or (lambda: float(interval_seconds))

        def _loop() -> None:
            logger.info("MixWorker: Loop started (interval: %ds)", cycle_interval())
            trigger = TRIGGER_STARTUP
            while not self._stop_event.is_set():
                try:
                    plex = plex_factory(config) if plex_factory else self._get_plex(config)
                    with self._iteration_lock:
                        with record_task_run(db, "mix_generation", trigger) as run:
                            outcome = self.run_iteration(db, config, plex, self._discovery)
                            if isinstance(outcome, dict):
                                run.message = f"due={outcome.get('due', 0)}, generated={outcome.get('generated', 0)}, errors={outcome.get('errors', 0)}"
                except Exception as exc:
                    logger.error("MixWorker: Error in iteration (%s)", type(exc).__name__)  # no traceback/message: may embed Plex token URLs
                    with self._lock:
                        self.errors += 1
                trigger = TRIGGER_SCHEDULED
                if wait_for_next_cycle(self._stop_event, cycle_interval):
                    break
            with self._lock:
                self._is_running = False
            logger.info("MixWorker: Loop terminated cleanly")

        self._thread = threading.Thread(target=_loop, daemon=True, name="MixWorkerThread")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = None
        with self._lock:
            if self._thread is not None and self._thread != threading.current_thread():
                thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


mix_worker = MixWorker()

__all__ = ["MixWorker", "mix_worker", "is_due", "MIX_INTERVALS"]
