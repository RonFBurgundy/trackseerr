"""On-demand track hydration for native albums.

Add/refresh only fetch tracklists for monitored albums, so most of a discography has no track rows. When the UI opens
such an album, or a user monitors it, the tracks are fetched from MusicBrainz here (one bounded call through the
shared, rate-limited ``MbidEnricherClient``) and stored. The call is idempotent: an album that already has tracks is
never refetched, and rows go through ``Database.upsert_library_track`` which reuses existing rows by title.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import hydrated_track_monitored
from trackseerr.models import LibraryTrack
from trackseerr.storage import Database, clean_library_name

logger = logging.getLogger(__name__)

# Upper bound on how long a UI request waits for MusicBrainz.
HYDRATION_TIMEOUT_SECONDS = 12.0
# How long an album whose fetch failed or came back empty is skipped before it is tried again.
NEGATIVE_CACHE_SECONDS = 600.0
_NEGATIVE_CACHE_MAX = 4096

# Per-album single-flight: concurrent hydrations (and refresh writes, via ``album_hydration_lock``) of one album
# serialise on one lock, so the "has tracks?" check and the inserts are atomic against each other.
_locks_guard = threading.Lock()
_album_locks: dict[str, threading.Lock] = {}
# album_id -> time.monotonic() of the last failed or empty attempt (in memory only; a restart retries everything).
_negative: dict[str, float] = {}


def _lock_for(album_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _album_locks.get(album_id)
        if lock is None:
            lock = _album_locks[album_id] = threading.Lock()
        return lock


@contextmanager
def album_hydration_lock(album_id: str) -> Iterator[None]:
    """Holds the album's hydration lock; refresh code that writes an album's tracks takes it to avoid duplicates."""
    lock = _lock_for(str(album_id))
    with lock:
        yield


def _recently_failed(album_id: str) -> bool:
    with _locks_guard:
        at = _negative.get(album_id)
        if at is None:
            return False
        if time.monotonic() - at >= NEGATIVE_CACHE_SECONDS:
            del _negative[album_id]
            return False
        return True


def _mark_failed(album_id: str) -> None:
    with _locks_guard:
        if len(_negative) >= _NEGATIVE_CACHE_MAX:
            now = time.monotonic()
            for key in [k for k, v in _negative.items() if now - v >= NEGATIVE_CACHE_SECONDS]:
                del _negative[key]
            if len(_negative) >= _NEGATIVE_CACHE_MAX:
                _negative.pop(next(iter(_negative)))
        _negative[album_id] = time.monotonic()


def clear_negative_cache() -> None:
    """Forgets every failed attempt (tests)."""
    with _locks_guard:
        _negative.clear()


def _fetch_tracks(
    enricher: MbidEnricherClient, rg_id: str, timeout: float
) -> list[dict[str, Any]]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="album-hydrate")
    future = pool.submit(enricher.get_release_group_tracks, rg_id)
    try:
        return future.result(timeout=timeout) or []
    except FutureTimeout:
        logger.warning("Album track hydration timed out after %.0fs for release group %s", timeout, rg_id)
        return []
    except Exception as exc:  # the enricher swallows most errors; this guards anything it does not
        logger.warning("Album track hydration failed for release group %s: %s", rg_id, exc)
        return []
    finally:
        pool.shutdown(wait=False)


def hydrate_album_tracks(
    db: Database,
    enricher: MbidEnricherClient,
    album_id: str,
    *,
    timeout: float = HYDRATION_TIMEOUT_SECONDS,
    deadline: Optional[float] = None,
) -> int:
    """Fetches and stores the tracks of an album that has none yet; returns how many were created.

    Returns 0 without any network call when the album is unknown, already has tracks, has no MusicBrainz release
    group, or its last attempt failed/came back empty less than ``NEGATIVE_CACHE_SECONDS`` ago. New tracks are
    unmonitored unless the album is monitored and the artist's option keeps tracks following their album (anything
    but ``existing``); a track with a file is always created monitored under ``existing``.

    Concurrent calls for one album are single-flight: the others wait on the per-album lock, then find the tracks
    already stored. The check and the inserts run under that lock. ``deadline`` is an absolute ``time.monotonic()``
    instant shared by a batch: the lock wait and the fetch are bounded by it, and an album it cuts off is skipped
    (not negative-cached) so it hydrates on first open.

    Side effect: this WRITES. ``GET /tracks/paged`` calls it for page 1 of an album, so a read request may create
    track rows.
    """
    album_id = str(album_id)
    if _recently_failed(album_id):
        return 0

    def remaining() -> float:
        return timeout if deadline is None else min(timeout, deadline - time.monotonic())

    if remaining() <= 0:
        return 0
    lock = _lock_for(album_id)
    if not lock.acquire(timeout=max(0.0, remaining())):
        return 0  # another request is still fetching this album; it will have rows (or not) when it finishes
    try:
        if _recently_failed(album_id):
            return 0
        album = db.get_library_album(album_id)
        if album is None:
            return 0
        if db.list_library_tracks(album_id=album_id, limit=1):
            return 0
        rg_id = str(album.get("mb_release_group_id") or "").strip()
        if not rg_id:
            return 0
        budget = remaining()
        if budget <= 0:
            return 0

        artist_id = str(album["artist_id"])
        artist: Optional[dict[str, Any]] = db.get_library_artist(artist_id)
        option = str((artist or {}).get("monitor_option") or "")
        follow_album = bool(album.get("monitored")) and bool((artist or {}).get("monitored", True))

        fetched = _fetch_tracks(enricher, rg_id, budget)
        if not fetched:
            if budget >= timeout:  # a fetch cut short by a batch deadline is not evidence the album is bad
                _mark_failed(album_id)
            return 0

        # max(existing, new): the release group's first release may be a shorter edition than one already recorded.
        db.set_library_album_total_tracks(album_id, len(fetched))
        created = 0
        for trk in fetched:
            title = trk.get("title") or "Unknown Track"
            number = int(trk.get("track_number") or 1)
            if db.get_library_track_by_title(album_id, title, track_number=number):
                continue
            db.upsert_library_track(
                LibraryTrack(
                    id=str(uuid.uuid4()),
                    album_id=album_id,
                    artist_id=artist_id,
                    title=title,
                    clean_title=clean_library_name(title),
                    track_number=number,
                    disc_number=int(trk.get("disc_number") or 1),
                    duration_seconds=trk.get("duration_seconds"),
                    monitored=follow_album and hydrated_track_monitored(option, has_file=False),
                    mb_recording_id=trk.get("mb_recording_id"),
                )
            )
            created += 1
        return created
    finally:
        lock.release()
