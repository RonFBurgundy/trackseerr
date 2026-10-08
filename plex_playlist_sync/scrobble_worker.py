"""Background worker for Plex history polling and scrobble forward retries."""

import logging
import threading
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Callable, Optional

import requests
from plexapi.exceptions import PlexApiException

from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.scrobbling import PLEX_ADMIN_USERNAME_KEY, forward_listen, resolve_plex_user
from plex_playlist_sync.storage import Database
from plex_playlist_sync.task_manager import TRIGGER_SCHEDULED, record_finished_run

logger = logging.getLogger(__name__)

HISTORY_CURSOR_KEY = "plex_history_last_poll"
RETRY_INTERVAL_SECONDS = 300
MAX_FORWARD_ATTEMPTS = 5
MAX_FORWARD_AGE_DAYS = 14
TICK_SECONDS = 30


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class ScrobbleWorker:
    """Polls Plex history (fallback when webhooks are unavailable) and retries pending forwards."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._iteration_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._plex: Optional[PlexClient] = None
        self._last_retry_at: Optional[datetime] = None
        self.last_poll_at: Optional[str] = None
        self.listens_ingested = 0
        self.forwards_attempted = 0
        self.errors = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._is_running,
                "last_poll_at": self.last_poll_at,
                "listens_ingested": self.listens_ingested,
                "forwards_attempted": self.forwards_attempted,
                "errors": self.errors,
            }

    # -- single-iteration pieces -------------------------------------------------

    def poll_history_once(
        self, db: Database, plex_client: Any, now: Optional[datetime] = None
    ) -> list[int]:
        """Ingest music plays from Plex history since the persisted cursor. Returns inserted listen ids."""
        now = _as_utc(now or datetime.now(timezone.utc))
        minutes = db.get_plex_history_poll_minutes()
        cursor_raw = db.get_scrobble_state(HISTORY_CURSOR_KEY)
        if cursor_raw:
            cursor = _as_utc(datetime.fromisoformat(cursor_raw))
        else:
            # First run: no backfill of the whole server history, only the last poll window.
            cursor = now - timedelta(minutes=max(minutes, 1))

        server = plex_client.server
        try:
            admin_username = plex_client._get_admin_username() or None
            if admin_username:
                db.set_scrobble_state(PLEX_ADMIN_USERNAME_KEY, admin_username)
            else:
                admin_username = db.get_scrobble_state(PLEX_ADMIN_USERNAME_KEY)
            accounts = {str(a.id): str(a.name) for a in server.systemAccounts()}
            entries = list(server.history(mindate=cursor))
        except (PlexApiException, requests.RequestException) as exc:
            # plexapi exception text embeds the request URL (X-Plex-Token): log the type only.
            logger.warning("ScrobbleWorker: Plex history poll failed: %s", type(exc).__name__)
            raise

        inserted: list[int] = []
        for entry in entries:
            if getattr(entry, "type", None) != "track":
                continue
            viewed_at = getattr(entry, "viewedAt", None)
            artist = getattr(entry, "originalTitle", None) or getattr(entry, "grandparentTitle", None)
            title = getattr(entry, "title", None)
            if not viewed_at or not artist or not title:
                continue
            account_id = getattr(entry, "accountID", None)
            user = resolve_plex_user(
                db, account_id, accounts.get(str(account_id)), admin_username=admin_username
            )
            if user is None:
                continue
            listen_id = db.insert_listen(
                user["id"],
                artist=str(artist),
                title=str(title),
                album=getattr(entry, "parentTitle", None),
                rating_key=str(getattr(entry, "ratingKey", "") or "") or None,
                duration_ms=getattr(entry, "duration", None),
                played_at=_as_utc(viewed_at),
                source="plex_history",
            )
            if listen_id is not None:
                inserted.append(listen_id)

        db.set_scrobble_state(HISTORY_CURSOR_KEY, now.isoformat(timespec="seconds"))
        with self._lock:
            self.last_poll_at = now.isoformat(timespec="seconds")
            self.listens_ingested += len(inserted)
        return inserted

    def retry_forwards_once(self, db: Database, config: Optional[Config] = None) -> int:
        """Forward every pending/failed listen (<14 days old, <5 attempts). Returns listens processed."""
        pending = db.list_pending_forwards(
            max_age_days=MAX_FORWARD_AGE_DAYS, limit=200, max_attempts=MAX_FORWARD_ATTEMPTS
        )
        for listen in pending:
            forward_listen(db, listen["id"], config)
        with self._lock:
            self.forwards_attempted += len(pending)
        return len(pending)

    def run_iteration(
        self,
        db: Database,
        config: Optional[Config] = None,
        plex_client: Any = None,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> dict[str, int]:
        """One scheduler tick: history poll (when due and enabled) then the retry pass (when due)."""
        now = _as_utc(now or datetime.now(timezone.utc))
        result = {"ingested": 0, "retried": 0}

        minutes = db.get_plex_history_poll_minutes()
        if minutes > 0 and plex_client is not None:
            cursor_raw = db.get_scrobble_state(HISTORY_CURSOR_KEY)
            due = force or not cursor_raw or (
                now - _as_utc(datetime.fromisoformat(cursor_raw)) >= timedelta(minutes=minutes)
            )
            if due:
                try:
                    result["ingested"] = len(self.poll_history_once(db, plex_client, now=now))
                except (PlexApiException, requests.RequestException):
                    with self._lock:
                        self.errors += 1

        if force or self._last_retry_at is None or (
            now - self._last_retry_at >= timedelta(seconds=RETRY_INTERVAL_SECONDS)
        ):
            self._last_retry_at = now
            result["retried"] = self.retry_forwards_once(db, config)
        return result

    def run_now(self, db: Database, config: Config) -> dict[str, int]:
        """Run an iteration immediately, bypassing the history poll gate and retry intervals."""
        plex = self._get_plex(config)
        with self._iteration_lock:
            return self.run_iteration(db, config, plex, force=True)

    # -- lifecycle ----------------------------------------------------------------

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
            logger.warning("ScrobbleWorker: could not connect to Plex: %s", type(exc).__name__)
            self._plex = None
        return self._plex

    def start(
        self,
        db: Database,
        config: Config,
        tick_seconds: int = TICK_SECONDS,
        plex_factory: Optional[Callable[[Config], Optional[Any]]] = None,
    ) -> bool:
        with self._lock:
            if self._is_running:
                logger.warning("ScrobbleWorker: Already running")
                return False
            self._stop_event.clear()
            self._is_running = True

        def _loop() -> None:
            logger.info("ScrobbleWorker: Loop started (tick: %ds)", tick_seconds)
            while not self._stop_event.is_set():
                try:
                    plex = plex_factory(config) if plex_factory else self._get_plex(config)
                    tick_started = time.monotonic()
                    with self._iteration_lock:
                        outcome = self.run_iteration(db, config, plex)
                    if isinstance(outcome, dict) and (outcome.get("ingested") or outcome.get("retried")):
                        record_finished_run(  # idle ticks (nothing polled or retried) are not history
                            db,
                            "scrobble_sync",
                            TRIGGER_SCHEDULED,
                            tick_started,
                            f"ingested={outcome.get('ingested', 0)}, retried={outcome.get('retried', 0)}",
                        )
                except Exception as exc:
                    # No traceback/message: plexapi and requests errors can embed token-bearing URLs.
                    logger.error("ScrobbleWorker: Error in iteration: %s", type(exc).__name__)
                    with self._lock:
                        self.errors += 1
                self._stop_event.wait(tick_seconds)
            with self._lock:
                self._is_running = False
            logger.info("ScrobbleWorker: Loop terminated cleanly")

        self._thread = threading.Thread(target=_loop, daemon=True, name="ScrobbleWorkerThread")
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


scrobble_worker = ScrobbleWorker()

__all__ = ["ScrobbleWorker", "scrobble_worker"]
