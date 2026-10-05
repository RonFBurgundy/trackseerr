"""Native artwork pipeline: keep derivatives and version tokens ready *before* the browser asks.

Mirrors how Lidarr treats MediaCover: when art lands on disk (a remote cover finished downloading, the scanner found
folder art, an artist was added or refreshed) the 250/500 derivatives are generated right away in the background and
an ``art_version`` token (file mtime + size) is stored on the artist/album row. API payloads then carry
``?v=<token>`` so the response can be cached as immutable, and a request never waits on a resize or a remote fetch.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional

from plex_playlist_sync import art_thumbs
from plex_playlist_sync.library import find_folder_art
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

ARTIST_FOLDER_ART = ("artist.jpg", "artist.png", "folder.jpg")
_CATEGORY = {"artist": "artist_poster", "album": "album_cover"}
# Pre-caching one artist walks its covers one at a time on a single worker: bounded, and it never drops work the way
# the request-time pool does when 256 downloads are already queued.
_PRECACHE_DELAY_SECONDS = 0.05
# At most this many artists wait for the pre-cache worker (one per artist, deduped); beyond it work is dropped with a
# log line and heals on the next artist open / refresh / backfill.
_PRECACHE_MAX_PENDING = 256
_precache_lock = threading.Lock()
_precache_executor: Optional[ThreadPoolExecutor] = None
_precache_pending: set[str] = set()
# Set by ``shutdown``; each executor gets its own event so a restart (tests) is not poisoned by an old stop.
_precache_stop = threading.Event()


def thumb_dir() -> Path:
    return mediacover_service.base_dir / "mediacover" / "thumbs"


def _is_remote(url: Any) -> bool:
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def cached_art_path(kind: str, item_id: str) -> Path:
    if kind == "artist":
        return mediacover_service.get_artist_poster_path(item_id)
    return mediacover_service.get_album_cover_path(item_id)


def local_folder_art(kind: str, path: Optional[str]) -> Optional[Path]:
    """The folder art file the serving route would pick for an artist/album ``path`` (None when there is none)."""
    if not path:
        return None
    p = Path(path)
    try:
        if kind == "artist":
            if p.is_dir():
                for name in ARTIST_FOLDER_ART:
                    if (p / name).is_file():
                        return p / name
            return None
        if p.is_dir():
            return find_folder_art(p)
        return p if p.is_file() else None
    except OSError as exc:
        logger.debug("Folder art lookup failed for %s: %s", path, exc)
        return None


def _row_url(kind: str, row: dict[str, Any]) -> Optional[str]:
    return row.get("image_url" if kind == "artist" else "cover_url") or row.get("url")


def _validated_folder_art(kind: str, path: Optional[str], db: Optional[Database]) -> Optional[Path]:
    """Folder art under a media-root-validated ``path`` (None when unapproved, missing, or without art)."""
    if not path:
        return None
    # Lazy: the routes module imports this one at import time.
    from fastapi import HTTPException

    from plex_playlist_sync.api.routes.library import validate_media_path

    try:
        validated = validate_media_path(path, db=db)
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        logger.debug("Folder art path '%s' rejected: %s", path, exc)
        return None
    return local_folder_art(kind, str(validated))


def resolve_served_art(
    kind: str, row: dict[str, Any], settings: Optional[dict[str, Any]], db: Optional[Database] = None
) -> Optional[Path]:
    """The one local file the artwork route serves for an artist/album ``row``, or None (remote redirect/placeholder).

    The single source of truth for "which file": the routes serve it, and the scanner, ``cache_remote_art`` and the
    backfill version exactly it, so a stored ``art_version`` can never describe a different file than the one served.
    Artists prefer folder art, then the mediacover cache. Albums follow ``prefer_local_artwork`` (default on) between
    folder art and the cache. Folder art goes through ``validate_media_path``. No network, no writes.
    """
    item_id = str(row["id"])
    local = _validated_folder_art(kind, row.get("path"), db)
    cached: Optional[Path] = cached_art_path(kind, item_id)
    if art_thumbs.art_version(cached) is None:
        cached = None
    if kind == "artist" or bool((settings or {}).get("prefer_local_artwork", True)):
        return local or cached
    return cached or local


def _publish(db: Database, kind: str, item_id: str) -> Optional[str]:
    """Generates the served file's derivatives, then stores its version (never before the thumbnails exist)."""
    row = db.get_library_artist(item_id) if kind == "artist" else db.get_library_album(item_id)
    if row is None:
        return None
    src = resolve_served_art(kind, row, db.get_media_management_settings(), db)
    if src is None:
        return None
    art_thumbs.pregenerate(src, thumb_dir())
    version = art_thumbs.art_version(src)
    if version is None or not art_thumbs.has_all_thumbs(src, thumb_dir()):
        return None
    if version != (row.get("art_version") or None):
        db.set_library_art_version(kind, item_id, version)
    return version


def register_served_art(db: Database, kind: str, row: dict[str, Any], settings: Optional[dict[str, Any]]) -> Optional[str]:
    """Versions the file the route serves for ``row`` and makes sure its 250/500 derivatives exist.

    Thumbnails already there: the version is stored (only when it changed) and nothing is scheduled. Otherwise
    generation is queued and the version is published by its completion callback, so a payload never advertises a
    version whose derivatives are not ready; if the backlog drops the item (logged by ``art_thumbs``) the version stays
    unpublished and the next request or the backfill heals it. Returns the published version, else None.
    """
    src = resolve_served_art(kind, row, settings, db)
    if src is None:
        return None
    version = art_thumbs.art_version(src)
    if version is None:
        return None
    if art_thumbs.has_all_thumbs(src, thumb_dir()):
        if version != (row.get("art_version") or None):
            db.set_library_art_version(kind, str(row["id"]), version)
        return version
    item_id = str(row["id"])
    art_thumbs.schedule_pregenerate(src, thumb_dir(), on_done=lambda: _publish(db, kind, item_id))
    return None


def _on_cached(db: Database, kind: str, item_id: str) -> Callable[[Path], None]:
    def _cb(_path: Path) -> None:
        # Runs on the mediacover worker, which is already bounded: generate inline, then publish the version of
        # whichever file is actually served (the download may not be it when folder art is preferred).
        _publish(db, kind, item_id)

    return _cb


def cache_remote_art(db: Database, kind: str, row: dict[str, Any], settings: Optional[dict[str, Any]]) -> Optional[Path]:
    """Ingest-time cache of a remote cover: returns the cached file if present, else queues the download (derivatives
    and the served file's version follow when it lands). Never schedules thumbnail work for art that is already done."""
    item_id = str(row["id"])
    path = mediacover_service.ensure_artwork(
        _CATEGORY[kind], item_id, _row_url(kind, row), on_cached=_on_cached(db, kind, item_id)
    )
    if path is not None:
        register_served_art(db, kind, row, settings)
    return path


def serve_art(db: Database, kind: str, row: dict[str, Any], settings: Optional[dict[str, Any]]) -> Optional[Path]:
    """What the artwork route serves for ``row`` (see ``resolve_served_art``), kicking off a remote download when the
    file that would be preferred is not cached yet, and keeping the stored version in step with the served file."""
    settings = settings or {}
    served = resolve_served_art(kind, row, settings, db)
    cache_missing = art_thumbs.art_version(cached_art_path(kind, str(row["id"]))) is None
    prefers_cache = kind == "album" and not bool(settings.get("prefer_local_artwork", True))
    if cache_missing and _is_remote(_row_url(kind, row)) and (served is None or prefers_cache):
        cache_remote_art(db, kind, row, settings)
        served = resolve_served_art(kind, row, settings, db)
    if served is not None:
        register_served_art(db, kind, row, settings)
    return served


def _precache_artist(db: Database, artist_id: str, delay: float, stop: Optional[threading.Event] = None) -> int:
    """Downloads the artist image and every album cover that is remote and not yet local; returns how many fetched."""
    fetched = 0
    jobs: list[tuple[str, str, str]] = []
    artist = db.get_library_artist(artist_id)
    if artist is None:
        return 0
    if _is_remote(artist.get("image_url")):
        jobs.append(("artist", artist_id, artist["image_url"]))
    for alb in db.list_library_albums(artist_id=artist_id, limit=1000):
        if _is_remote(alb.get("cover_url")):
            jobs.append(("album", str(alb["id"]), alb["cover_url"]))
    for kind, item_id, url in jobs:
        if stop is not None and stop.is_set():
            break
        target = cached_art_path(kind, item_id)
        try:
            already = target.is_file() and target.stat().st_size > 0
        except OSError:
            already = False
        try:
            if already:
                _publish(db, kind, item_id)
                continue
            if mediacover_service.cache_image(target, url):
                _publish(db, kind, item_id)
                fetched += 1
        except Exception as exc:
            logger.warning("Art pre-cache of %s %s failed: %s", kind, item_id, exc)
        if delay:
            time.sleep(delay)
    return fetched


def schedule_precache(db: Database, artist_id: str, delay: float = _PRECACHE_DELAY_SECONDS) -> Optional[Future]:
    """Queues a throttled background download of an artist's image and album covers (artist add / refresh).

    Deduped per artist and capped at ``_PRECACHE_MAX_PENDING`` waiting artists (a drop is logged at info).
    """
    global _precache_executor, _precache_stop
    with _precache_lock:
        if artist_id in _precache_pending:
            return None
        if len(_precache_pending) >= _PRECACHE_MAX_PENDING:
            logger.info("Art pre-cache backlog full (%d artists); dropping artist %s", _PRECACHE_MAX_PENDING, artist_id)
            return None
        _precache_pending.add(artist_id)
        if _precache_executor is None:
            _precache_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="art-precache")
            _precache_stop = threading.Event()
        executor, stop = _precache_executor, _precache_stop

    def _job() -> int:
        try:
            if stop.is_set():
                return 0
            return _precache_artist(db, artist_id, delay, stop)
        finally:
            with _precache_lock:
                _precache_pending.discard(artist_id)

    try:
        return executor.submit(_job)
    except RuntimeError as exc:
        with _precache_lock:
            _precache_pending.discard(artist_id)
        logger.warning("Art pre-cache pool unavailable: %s", exc)
        return None


def shutdown() -> None:
    """Stops the background pre-cache and thumbnail pools without waiting: queued work is cancelled, running work
    ends at its next item. Called from the server shutdown hook so interpreter exit never drains a long queue."""
    global _precache_executor
    with _precache_lock:
        executor, _precache_executor = _precache_executor, None
        _precache_stop.set()
        _precache_pending.clear()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    art_thumbs.shutdown()


def backfill(
    db: Database,
    delay: float = 0.02,
    batch: int = 200,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, int]:
    """One-off pass over art that already exists locally (folder art and the mediacover cache): generates missing
    derivatives and records missing/changed version tokens.

    Idempotent and resumable: finished items cost two ``stat`` calls and no sleep, so re-running (or restarting after
    an interruption) just skips ahead. Rows are walked by id keyset, so memory stays flat on huge libraries. ``delay``
    throttles only items that needed work.
    """
    stats = {"scanned": 0, "generated": 0, "versioned": 0}
    settings = db.get_media_management_settings()
    for kind in ("artist", "album"):
        after = ""
        while True:
            rows = db.list_library_art_rows(kind, after, batch)
            if not rows:
                break
            for row in rows:
                if should_stop is not None and should_stop():
                    return stats
                after = str(row["id"])
                stats["scanned"] += 1
                src = resolve_served_art(kind, row, settings, db)
                if src is None:
                    continue
                did_work = False
                if not art_thumbs.has_all_thumbs(src, thumb_dir()):
                    art_thumbs.pregenerate(src, thumb_dir())
                    stats["generated"] += 1
                    did_work = True
                version = art_thumbs.art_version(src)
                if version and version != (row.get("art_version") or None):
                    db.set_library_art_version(kind, after, version)
                    stats["versioned"] += 1
                    did_work = True
                if did_work and delay:
                    time.sleep(delay)
    return stats


# Bump when derivatives or version tokens change shape so existing libraries are backfilled once more.
ART_PIPELINE_VERSION = 1
BACKFILL_MARKER_KEY = "art_backfill_done_version"
_startup_stop = threading.Event()
_startup_thread: Optional[threading.Thread] = None


def backfill_needed(db: Database) -> bool:
    return db.get_kv(BACKFILL_MARKER_KEY) != str(ART_PIPELINE_VERSION)


def start_startup_backfill(db: Database) -> Optional[threading.Thread]:
    """Runs the art backfill once in the background after an upgrade; never blocks the caller.

    The marker is written only when a pass finishes uncancelled, so an interrupted run (crash, shutdown) is simply
    started again on the next boot; ``backfill`` is idempotent and skips finished items cheaply. Returns the thread,
    or None when the marker says it is already done or a run is in flight.
    """
    global _startup_thread
    if not backfill_needed(db):
        return None
    if _startup_thread is not None and _startup_thread.is_alive():
        return None
    _startup_stop.clear()

    def _run() -> None:
        try:
            from plex_playlist_sync.job_tracker import track_job

            with track_job("art_thumbnail_backfill", "Artwork Thumbnail Backfill") as job:
                stats = backfill(db, should_stop=_startup_stop.is_set)
                if _startup_stop.is_set():
                    job.message = "interrupted; will resume on next start"
                    return
                db.set_kv(BACKFILL_MARKER_KEY, str(ART_PIPELINE_VERSION))
                job.message = f"scanned {stats['scanned']}, generated {stats['generated']}, versioned {stats['versioned']}"
        except Exception as exc:
            from plex_playlist_sync.redaction import safe_exc

            logger.error("Startup art backfill failed; will retry on next start: %s", safe_exc(exc))
            logger.debug("Startup art backfill traceback", exc_info=True)

    _startup_thread = threading.Thread(target=_run, daemon=True, name="ArtStartupBackfill")
    _startup_thread.start()
    return _startup_thread


def stop_startup_backfill() -> None:
    _startup_stop.set()


def wait_idle(timeout: float = 5.0) -> bool:
    """Blocks until pre-cache and thumbnail queues are empty (tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _precache_lock:
            idle = not _precache_pending
        if idle and art_thumbs.wait_idle(0.05):
            return True
        time.sleep(0.01)
    return False
