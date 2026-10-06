"""Background sync of import lists: fetch the remote list, record its items, apply the new ones to the library.

``ImportListWorker`` wakes every five minutes and syncs each enabled list whose interval has elapsed.
``sync_import_list`` does one list; per-item failures are recorded on the item and never abort the list, and an item
that was already applied is never applied again (so unmonitoring it later is respected).

Retry policy (each sync applies every item that is due):

* ``pending`` (a transient problem such as Lidarr not ready): retried on every sync.
* ``failed``: retried with exponential backoff after the 1st, 2nd, 3rd failure (1 hour, 6 hours, 24 hours), then
  every 7 days; after the 6th failed attempt the item stays ``failed`` and is no longer retried.
* ``unresolved`` (no MusicBrainz match): retried weekly, since metadata may appear later.
* ``applied`` and ``skipped``: never retried. Changing a list's monitor mode resets its ``skipped`` items to
  ``pending``.

An item that added its artist but did not finish (album load, monitor preset, refresh) remembers that
(``artist_added_by_item``), so its retry completes the remaining step for that artist and never touches one that
pre-existed. The item becomes ``applied`` only after the whole effect is in place.
"""

import logging
import threading
import uuid
from typing import Any, Optional

from plex_playlist_sync.clients.import_lists import ImportListError, fetch_items
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.item_history import TRIGGER_IMPORT_LIST, GrabTrigger
from plex_playlist_sync.job_tracker import tracked
from plex_playlist_sync.list_monitoring import STATUS_FAILED, apply_list_item, list_actor
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300
INITIAL_DELAY_SECONDS = 30

_syncing_lock = threading.Lock()
_syncing: dict[str, str] = {}  # list id -> token of the claim that holds it


class ImportListBusy(Exception):
    """The list is already being synced."""


def is_syncing(list_id: str) -> bool:
    with _syncing_lock:
        return str(list_id) in _syncing


def claim_sync(list_id: str) -> Optional[str]:
    """Marks the list as syncing and returns the claim token; None if it already was.

    Pair every token with ``release_sync(list_id, token)``: a release only takes effect while the token still
    holds the claim, so a late or repeated release can never free a newer claim.
    """
    with _syncing_lock:
        if str(list_id) in _syncing:
            return None
        token = uuid.uuid4().hex
        _syncing[str(list_id)] = token
        return token


def release_sync(list_id: str, token: Optional[str] = None) -> bool:
    """Releases the claim when ``token`` matches the one holding it (any claim when ``token`` is None, for tests
    and operator tooling). Returns whether a claim was released."""
    with _syncing_lock:
        held = _syncing.get(str(list_id))
        if held is None or (token is not None and held != token):
            return False
        del _syncing[str(list_id)]
        return True


def _mirror_url(db: Database) -> Optional[str]:
    return db.get_media_management_settings().get("mb_mirror_url") or None


@tracked("import_list_sync", "Import List Sync")
def _run_sync(
    db: Database,
    list_id: str,
    config: Any,
    enricher: Optional[MbidEnricherClient],
    lidarr_client: Optional[LidarrClient],
    stop_event: Optional[threading.Event],
) -> dict[str, Any]:
    lst = db.get_import_list(list_id)
    if lst is None:
        return {"status": "missing", "list_id": list_id}

    try:
        fetched = fetch_items(lst["provider"], lst["config"], mb_base_url=_mirror_url(db))
    except ImportListError as exc:
        logger.warning("Import list %s (%s) fetch failed: %s", lst["name"], lst["provider"], exc)
        db.set_import_list_sync_result(list_id, "error", str(exc))
        return {"status": "error", "error": str(exc)}

    new_items = db.upsert_import_list_items(list_id, [item.to_row() for item in fetched])
    pending = db.list_pending_import_items(list_id)
    actor = list_actor(db)
    if enricher is None:
        enricher = MbidEnricherClient(base_url=_mirror_url(db), timeout=10.0)

    outcome: dict[str, int] = {}
    for row in pending:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            result = apply_list_item(
                db,
                config,
                row,
                lst["monitor_mode"],
                artist_monitor_option=lst.get("artist_monitor_option"),
                quality_profile_id=lst.get("quality_profile_id"),
                requested_by=actor,
                enricher=enricher,
                lidarr_client=lidarr_client,
                artist_added=bool(row.get("artist_added_by_item")),
                trigger=GrabTrigger(
                    TRIGGER_IMPORT_LIST, ref=list_id, label=lst.get("name"),
                    actor_user_id=str(actor["id"]) if actor else None,
                ),
                on_artist_added=lambda item_id=row["id"]: db.mark_import_item_artist_added(item_id),
            )
            status, level, error, mbid = result.status, result.applied_level, result.error, result.mbid
        except Exception as exc:  # one bad item must not abort the list; the cause is logged and kept on the item
            logger.warning("Import list %s: item %s failed: %s", lst["name"], row["id"], safe_exc(exc))
            logger.debug("Import list item traceback", exc_info=True)
            status, level, error, mbid = STATUS_FAILED, None, safe_exc(exc), None
        db.update_import_list_item(row["id"], status, level, error, mbid)
        outcome[status] = outcome.get(status, 0) + 1

    db.set_import_list_sync_result(list_id, "ok", None)
    return {"status": "ok", "fetched": len(fetched), "new_items": new_items, **outcome}


def sync_import_list(
    db: Database,
    list_id: str,
    config: Any = None,
    *,
    enricher: Optional[MbidEnricherClient] = None,
    lidarr_client: Optional[LidarrClient] = None,
    stop_event: Optional[threading.Event] = None,
    claim_token: Optional[str] = None,
) -> dict[str, Any]:
    """Syncs one list and returns a summary (``status`` ``ok``/``error``/``missing`` plus counts per item status).

    Raises ImportListBusy when the list is already syncing, unless the caller already took the claim
    (``claim_token`` is the token ``claim_sync`` returned). The claim is released, by token, when the sync ends.
    """
    token = claim_token or claim_sync(list_id)
    if token is None:
        raise ImportListBusy(list_id)
    try:
        return _run_sync(db, list_id, config, enricher, lidarr_client, stop_event)
    finally:
        release_sync(list_id, token)


class ImportListWorker:
    """Daemon thread that syncs due import lists."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self.lists_synced = 0
        self.errors = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def start(
        self,
        db: Database,
        config: Any = None,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
        initial_delay: float = INITIAL_DELAY_SECONDS,
    ) -> bool:
        with self._lock:
            if self._is_running:
                logger.warning("ImportListWorker: already running")
                return False
            self._stop_event.clear()
            self._is_running = True

        def _loop() -> None:
            logger.info("ImportListWorker: loop started (interval: %ds)", interval_seconds)
            if self._stop_event.wait(initial_delay):
                with self._lock:
                    self._is_running = False
                return
            while not self._stop_event.is_set():
                self.run_due(db, config)
                self._stop_event.wait(interval_seconds)
            with self._lock:
                self._is_running = False
            logger.info("ImportListWorker: loop terminated cleanly")

        self._thread = threading.Thread(target=_loop, daemon=True, name="ImportListWorkerThread")
        self._thread.start()
        return True

    def run_due(self, db: Database, config: Any = None) -> int:
        """Syncs every enabled list that is due; returns how many were attempted."""
        try:
            due = db.list_due_import_lists()
        except Exception as exc:  # the worker must survive a transient database problem; the cause is logged
            logger.error("ImportListWorker: could not list due import lists: %s", safe_exc(exc))
            with self._lock:
                self.errors += 1
            return 0
        attempted = 0
        for lst in due:
            if self._stop_event.is_set():
                break
            token = claim_sync(lst["id"])
            if token is None:
                continue  # a manual sync is already running it
            attempted += 1
            try:
                summary = sync_import_list(db, lst["id"], config, stop_event=self._stop_event, claim_token=token)
                with self._lock:
                    self.lists_synced += 1
                    if summary.get("status") == "error":
                        self.errors += 1
            except Exception as exc:  # keep the loop alive for the other lists; the cause is logged
                logger.error("ImportListWorker: sync of %s crashed: %s", lst["id"], safe_exc(exc))
                logger.debug("Import list sync traceback", exc_info=True)
                with self._lock:
                    self.errors += 1
                try:
                    db.set_import_list_sync_result(lst["id"], "error", safe_exc(exc))
                except Exception as db_exc:  # the original failure is already logged above
                    logger.error("ImportListWorker: could not record failure for %s: %s", lst["id"], safe_exc(db_exc))
        return attempted

    def stop(self) -> None:
        self._stop_event.set()
        thread = None
        with self._lock:
            if self._thread is not None and self._thread is not threading.current_thread():
                thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


import_list_worker = ImportListWorker()

__all__ = [
    "ImportListBusy",
    "ImportListWorker",
    "claim_sync",
    "import_list_worker",
    "is_syncing",
    "release_sync",
    "sync_import_list",
]
