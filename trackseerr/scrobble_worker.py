"""Background worker for Plex history polling and scrobble forward retries."""

import logging
import threading
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Callable, Optional

import requests
from plexapi.exceptions import PlexApiException

from trackseerr.clients.plex import PlexClient
from trackseerr.config import Config
from trackseerr.media_servers.base import MediaServerError
from trackseerr.scrobbling import PLEX_ADMIN_USERNAME_KEY, forward_listen, resolve_plex_user
from trackseerr.storage import Database
from trackseerr.task_manager import TRIGGER_SCHEDULED, record_finished_run

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
        self,
        db: Database,
        plex_client: Any,
        now: Optional[datetime] = None,
        cursor_key: str = HISTORY_CURSOR_KEY,
        owner_is_admin: bool = True,
    ) -> list[int]:
        """Ingest music plays from Plex history since the persisted cursor. Returns inserted listen ids."""
        now = _as_utc(now or datetime.now(timezone.utc))
        minutes = db.get_plex_history_poll_minutes()
        cursor_raw = db.get_scrobble_state(cursor_key)
        if cursor_raw:
            cursor = _as_utc(datetime.fromisoformat(cursor_raw))
        else:
            # First run: no backfill of the whole server history, only the last poll window.
            cursor = now - timedelta(minutes=max(minutes, 1))

        server = plex_client.server
        try:
            if owner_is_admin:
                admin_username = plex_client._get_admin_username() or None
                if admin_username:
                    db.set_scrobble_state(PLEX_ADMIN_USERNAME_KEY, admin_username)
                else:
                    admin_username = db.get_scrobble_state(PLEX_ADMIN_USERNAME_KEY)
            else:
                admin_username = None
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
                db,
                account_id,
                accounts.get(str(account_id)),
                admin_username=admin_username,
                owner_is_admin=owner_is_admin,
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

        db.set_scrobble_state(cursor_key, now.isoformat(timespec="seconds"))
        with self._lock:
            self.last_poll_at = now.isoformat(timespec="seconds")
            self.listens_ingested += len(inserted)
        return inserted

    def poll_jellyfin_once(
        self, db: Database, rec: Any, adapter: Any, now: Optional[datetime] = None
    ) -> list[int]:
        """Ingest audio plays from a Jellyfin server since its persisted cursor. Returns inserted listen ids."""
        now = _as_utc(now or datetime.now(timezone.utc))
        minutes = db.get_plex_history_poll_minutes()
        cursor_key = f"jellyfin_history:{rec.id}"
        cursor_raw = db.get_scrobble_state(cursor_key)
        if cursor_raw:
            cursor = _as_utc(datetime.fromisoformat(cursor_raw))
        else:
            # First run: only the last poll window, like Plex.
            cursor = now - timedelta(minutes=max(minutes, 1))

        try:
            server_users = adapter.list_users()
            users_by_name = {(u.get("username") or "").lower(): u for u in db.list_users() if u.get("username")}
            inserted: list[int] = []
            for server_user in server_users:
                user = db.get_user(server_user.id) or users_by_name.get(server_user.name.lower())
                if user is None:
                    continue
                for play in adapter.recent_plays(server_user.id, since=cursor):
                    listen_id = db.insert_listen(
                        user["id"],
                        artist=str(play["artist"]),
                        title=str(play["title"]),
                        album=play.get("album"),
                        rating_key=f"jellyfin:{play['item_id']}",
                        duration_ms=play.get("duration_ms"),
                        played_at=_as_utc(play["played_at"]),
                        source="jellyfin_history",
                    )
                    if listen_id is not None:
                        inserted.append(listen_id)
        except MediaServerError as exc:
            logger.warning("ScrobbleWorker: Jellyfin history poll failed for %s: %s", rec.name, exc.safe_detail)
            with self._lock:
                self.errors += 1
            return []

        db.set_scrobble_state(cursor_key, now.isoformat(timespec="seconds"))
        with self._lock:
            self.last_poll_at = now.isoformat(timespec="seconds")
            self.listens_ingested += len(inserted)
        return inserted

    def _poll_plex_servers(
        self,
        db: Database,
        config: Optional[Config],
        minutes: int,
        now: datetime,
        force: bool,
    ) -> int:
        if minutes <= 0:
            return 0
        from trackseerr.media_servers import registry

        ingested = 0
        for rec, adapter in registry.media_servers(registry.ROLE_SCROBBLES, config):
            if rec.kind != "plex":
                continue
            cursor_key = HISTORY_CURSOR_KEY if rec.is_login else f"plex_history:{rec.id}"
            cursor_raw = db.get_scrobble_state(cursor_key)
            due = force or not cursor_raw or (
                now - _as_utc(datetime.fromisoformat(cursor_raw)) >= timedelta(minutes=minutes)
            )
            if not due:
                continue
            try:
                ingested += len(
                    self.poll_history_once(
                        db,
                        adapter.plex_extras(),
                        now=now,
                        cursor_key=cursor_key,
                        owner_is_admin=rec.is_login,
                    )
                )
            except (PlexApiException, requests.RequestException):
                with self._lock:
                    self.errors += 1
        return ingested

    def _poll_jellyfin_servers(
        self,
        db: Database,
        config: Optional[Config],
        minutes: int,
        now: datetime,
        force: bool,
    ) -> int:
        if minutes <= 0:
            return 0
        from trackseerr.media_servers import registry

        ingested = 0
        for rec, adapter in registry.media_servers(registry.ROLE_SCROBBLES, config):
            if rec.kind != "jellyfin":
                continue
            cursor_raw = db.get_scrobble_state(f"jellyfin_history:{rec.id}")
            due = force or not cursor_raw or (
                now - _as_utc(datetime.fromisoformat(cursor_raw)) >= timedelta(minutes=minutes)
            )
            if due:
                ingested += len(self.poll_jellyfin_once(db, rec, adapter, now=now))
        return ingested

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

        from trackseerr.media_servers import registry

        minutes = db.get_plex_history_poll_minutes()
        if registry.is_installed():
            result["ingested"] += self._poll_plex_servers(db, config, minutes, now, force)
            result["ingested"] += self._poll_jellyfin_servers(db, config, minutes, now, force)
        elif minutes > 0 and plex_client is not None:
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
        from trackseerr.media_servers import registry

        return registry.plex_login_client(config)

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
