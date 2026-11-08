"""Pack scanner: finds torrent packs, reads their file lists, and indexes audio files.

Backfill Depth:
    The number of pages fetched per enabled indexer during periodic pack backfill
    is governed by the kv_store key `pack_backfill_pages` (integer, default 5, clamped 1-50).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from trackseerr.bencode import BencodeError, decode as bencode_decode, info_hash as bencode_info_hash, torrent_files
from trackseerr.clients.acquisition import get_indexer_driver
from trackseerr.clients.acquisition.torznab import IndexerError
from trackseerr.indexer_status import indexer_status
from trackseerr.job_tracker import track_job
from trackseerr.pack_index import AUDIO_EXTS, looks_like_pack
from trackseerr.redaction import safe_exc
from trackseerr.task_manager import (
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    parse_iso,
    record_task_run,
    wait_for_next_cycle,
)

logger = logging.getLogger(__name__)

LAST_RUN_KV = "pack_index_last_run"
LAST_BACKFILL_KV = "pack_index_last_backfill"
BACKFILL_INTERVAL_HOURS = 24
WORKER_INITIAL_DELAY_SECONDS = 300.0
WORKER_INTERVAL_SECONDS = 6 * 3600.0

_last_run: Optional[dict[str, Any]] = None
_run_lock = threading.Lock()
_state_lock = threading.Lock()
_progress: tuple[int, int] = (0, 0)
_progress_message: str | None = None
_progress_lock = threading.Lock()


class PackScannerBusy(RuntimeError):
    """Raised when another pack scan sweep is already running."""


def _set_progress(
    current: int,
    total: int,
    message: str | None = None,
    handle: Any | None = None,
) -> None:
    global _progress, _progress_message
    with _progress_lock:
        _progress = (current, total)
        if message is not None:
            _progress_message = message
    if message is not None and handle is not None:
        handle.update_message(message)


def _reset_progress() -> None:
    global _progress, _progress_message
    with _progress_lock:
        _progress = (0, 0)
        _progress_message = None


def get_progress() -> tuple[int, int]:
    """Returns the current module-level (current, total) scanning progress."""
    with _progress_lock:
        return _progress


def _iso(now: datetime) -> str:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).isoformat()


def _extract_release_fields(r: Any) -> tuple[Optional[str], Optional[str], dict[str, Any]]:
    """Extracts (title, guid, fields) from AcquisitionSearchResult or dict."""
    is_dict = isinstance(r, dict)
    title = str(r.get("title") or "") if is_dict else str(getattr(r, "title", "") or "")
    if not title:
        return None, None, {}

    extra = (r.get("extra") if is_dict else getattr(r, "extra", None)) or {}
    if not isinstance(extra, dict):
        extra = {}

    info_hash_cand = extra.get("infohash") or extra.get("info_hash")
    download_id = r.get("guid") or r.get("download_id") if is_dict else getattr(r, "download_id", None)
    download_url = r.get("download_url") if is_dict else getattr(r, "download_url", None)
    magnet_url = r.get("magnet_url") if is_dict else getattr(r, "magnet_url", None)
    size_bytes = r.get("size_bytes") if is_dict else getattr(r, "size_bytes", None)
    seeders = r.get("seeders") if is_dict else getattr(r, "seeders", None)
    published_at = extra.get("published_at") or (
        r.get("published_at") or r.get("published") if is_dict else getattr(r, "published_at", None)
    )

    guid = info_hash_cand or download_id or download_url
    if not guid:
        return None, None, {}

    fields = {
        "download_url": str(download_url) if download_url else None,
        "magnet_url": str(magnet_url) if magnet_url else None,
        "info_hash": str(info_hash_cand) if info_hash_cand else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "seeders": int(seeders) if seeders is not None else None,
        "published_at": str(published_at) if published_at else None,
    }
    return title, str(guid), fields


def _is_torrent_release(r: Any, idx_cfg: dict[str, Any]) -> bool:
    """Checks whether candidate release is a torrent."""
    is_dict = isinstance(r, dict)
    protocol = r.get("protocol") if is_dict else getattr(r, "protocol", None)
    source = r.get("source") if is_dict else getattr(r, "source", None)
    magnet = r.get("magnet_url") if is_dict else getattr(r, "magnet_url", None)

    if protocol == "usenet" or source == "newznab":
        return False

    indexer_type = str(idx_cfg.get("indexer_type") or "").lower()
    return bool(
        protocol == "torrent"
        or source in ("torznab", "torrent")
        or magnet
        or indexer_type in ("torznab", "torrent")
    )


def collect_from_releases(
    db: Any,
    idx_cfg: dict[str, Any],
    releases: list[Any],
) -> int:
    """Collects pack releases from recent search/RSS candidates into database."""
    mode = str(idx_cfg.get("pack_scan") or "off").lower()
    if mode == "off":
        return 0

    idx_id = str(idx_cfg.get("id"))
    collected = 0

    for r in releases:
        if not _is_torrent_release(r, idx_cfg):
            continue

        title, guid, fields = _extract_release_fields(r)
        if not title or not guid:
            continue

        if mode == "auto" and not looks_like_pack(title):
            continue

        db.upsert_pack_release(indexer_id=idx_id, guid=guid, title=title, **fields)
        collected += 1

    return collected


def _process_one_pending_pack(
    db: Any,
    pack: dict[str, Any],
    driver: Any,
    counts: dict[str, int],
) -> None:
    """Fetches and parses a single pending torrent pack release."""
    pack_id = str(pack["id"])
    idx_id = str(pack["indexer_id"])
    download_url = pack.get("download_url")

    res = driver.fetch_torrent(download_url)
    indexer_status.record_success(idx_id)

    if isinstance(res, str):
        db.mark_pack_skipped(pack_id, "Magnet only: file list not available")
        counts["skipped"] += 1
        counts["skipped_magnet"] = counts.get("skipped_magnet", 0) + 1
        return

    try:
        meta = bencode_decode(res)
        h = bencode_info_hash(res)
        files = torrent_files(meta)
    except BencodeError as exc:
        db.mark_pack_failed(pack_id, f"Bencode error: {safe_exc(exc)}")
        counts["failed"] += 1
        return

    audio_count = sum(
        1 for f in files
        if Path(str(f.get("path", "")).replace("\\", "/")).suffix.lower() in AUDIO_EXTS
    )
    if audio_count < 2:
        db.mark_pack_skipped(pack_id, f"Fewer than 2 audio files ({audio_count})")
        counts["skipped"] += 1
        return

    db.upsert_pack_release(idx_id, pack["guid"], pack["title"], info_hash=h)
    db.record_pack_files(pack_id, files)
    counts["scanned"] += 1
    counts["songs"] = counts.get("songs", 0) + audio_count


def scan_pending(db: Any, limit: int = 20, handle: Any | None = None) -> dict[str, int]:
    """Scans pending pack releases, fetching and parsing their .torrent files."""
    counts = {"scanned": 0, "skipped": 0, "failed": 0, "errors": 0, "songs": 0, "skipped_magnet": 0}
    pending = db.list_pending_pack_releases(limit=limit)
    if not pending:
        return counts

    total = len(pending)
    _set_progress(0, total, handle=handle)

    all_indexers = {str(idx.get("id")): idx for idx in db.list_indexers()}
    indexer_drivers: dict[str, Any] = {}
    skipped_indexers: set[str] = set()
    last_request_times: dict[str, float] = {}

    try:
        for i, pack in enumerate(pending, 1):
            pack_title = str(pack.get("title") or "")
            scan_msg = f"Reading pack {i} of {total}: {pack_title[:60]}"
            _set_progress(i, total, message=scan_msg, handle=handle)

            pack_id = str(pack["id"])
            idx_id = str(pack["indexer_id"])

            if idx_id in skipped_indexers:
                continue

            idx_cfg = all_indexers.get(idx_id)
            if not idx_cfg or str(idx_cfg.get("pack_scan") or "off").lower() == "off":
                db.mark_pack_skipped(pack_id, "Indexer pack_scan is off")
                counts["skipped"] += 1
                continue

            if not idx_cfg.get("enabled", True):
                continue

            if indexer_status.paused_until(idx_id) is not None:
                skipped_indexers.add(idx_id)
                continue

            download_url = pack.get("download_url")
            if not download_url or download_url.startswith("magnet:"):
                db.mark_pack_skipped(pack_id, "Magnet only: file list not available")
                counts["skipped"] += 1
                counts["skipped_magnet"] = counts.get("skipped_magnet", 0) + 1
                continue

            if idx_id not in indexer_drivers:
                try:
                    indexer_drivers[idx_id] = get_indexer_driver(idx_cfg)
                except Exception as exc:  # noqa: BLE001 - driver factory error
                    logger.warning("Could not instantiate driver for indexer %s: %s", idx_id, safe_exc(exc))
                    skipped_indexers.add(idx_id)
                    counts["errors"] += 1
                    continue

            driver = indexer_drivers[idx_id]
            if not hasattr(driver, "fetch_torrent"):
                db.mark_pack_skipped(pack_id, "Driver does not support fetch_torrent")
                counts["skipped"] += 1
                continue

            # Pace 1 request / 2 s per indexer
            elapsed = time.monotonic() - last_request_times.get(idx_id, 0.0)
            if elapsed < 2.0:
                time.sleep(2.0 - elapsed)
            last_request_times[idx_id] = time.monotonic()

            try:
                _process_one_pending_pack(db, pack, driver, counts)
            except IndexerError as exc:
                indexer_status.record_failure(idx_id, exc)
                skipped_indexers.add(idx_id)
                counts["errors"] += 1
                logger.warning("Indexer error fetching torrent from %s: %s", idx_cfg.get("name") or idx_id, safe_exc(exc))
    finally:
        _reset_progress()

    return counts


PACK_SEARCH_TERMS_DEFAULT = ("Dj Pools Music Pack", "pack", "pool", "megapack", "chart", "VA")


def get_pack_search_terms(db: Any) -> list[str]:
    """Read pack_search_terms from kv_store as JSON list of non-empty strings, else default."""
    if hasattr(db, "get_kv"):
        try:
            raw = db.get_kv("pack_search_terms")
            if raw is not None:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    terms = [str(t).strip() for t in parsed if isinstance(t, str) and t.strip()]
                    if terms:
                        return terms
        except (ValueError, TypeError, sqlite3.Error) as exc:
            logger.warning("Could not read pack_search_terms, falling back to default: %s", safe_exc(exc))
    return list(PACK_SEARCH_TERMS_DEFAULT)


def get_pack_search_pages(db: Any) -> int:
    """Read pack_search_pages from kv_store, default 3, clamped 1-20."""
    if hasattr(db, "get_kv"):
        try:
            raw = db.get_kv("pack_search_pages")
            if raw is not None:
                val = int(raw)
                return max(1, min(20, val))
        except (ValueError, TypeError, sqlite3.Error):
            pass
    return 3


def get_pack_backfill_pages(db: Any) -> int:
    """Read pack_backfill_pages from kv_store, default 5, clamped 1-50."""
    if hasattr(db, "get_kv"):
        try:
            raw = db.get_kv("pack_backfill_pages")
            if raw is not None:
                val = int(raw)
                return max(1, min(50, val))
        except (ValueError, TypeError, sqlite3.Error):
            pass
    return 5


def _backfill_search_terms(
    db: Any,
    idx_cfg: dict[str, Any],
    driver: Any,
    counts: dict[str, int],
    handle: Any | None,
) -> None:
    """Queries configured pack search terms for an indexer, collecting results."""
    idx_id = str(idx_cfg.get("id"))
    if indexer_status.paused_until(idx_id) is not None:
        return

    idx_name = str(idx_cfg.get("name") or idx_id)
    terms = get_pack_search_terms(db)
    search_pages = get_pack_search_pages(db)

    for term in terms:
        if indexer_status.paused_until(idx_id) is not None:
            break
        for page in range(search_pages):
            if indexer_status.paused_until(idx_id) is not None:
                break
            msg = f'Searching {idx_name} for "{term}", page {page + 1} of {search_pages}'
            _set_progress(0, 0, message=msg, handle=handle)
            offset = page * 100
            try:
                releases = driver.fetch_page(offset=offset, limit=100, query=term)
                indexer_status.record_success(idx_id)
            except IndexerError as exc:
                indexer_status.record_failure(idx_id, exc)
                counts["errors"] += 1
                return

            if not releases:
                break

            counts["collected"] += collect_from_releases(db, idx_cfg, releases)


def backfill(db: Any, pages: int | None = None, handle: Any | None = None) -> dict[str, int]:
    """Paginates enabled pack-scanning indexers, collects releases, and scans pending."""
    if pages is None:
        effective_pages = get_pack_backfill_pages(db)
    else:
        effective_pages = max(1, min(50, int(pages)))
    counts = {
        "collected": 0,
        "scanned": 0,
        "skipped": 0,
        "failed": 0,
        "errors": 0,
        "pruned": 0,
        "songs": 0,
        "skipped_magnet": 0,
    }
    enabled_indexers = db.list_indexers(enabled_only=True)

    for idx_cfg in enabled_indexers:
        if str(idx_cfg.get("pack_scan") or "off").lower() == "off":
            continue
        if idx_cfg.get("indexer_type", "torznab") not in ("torznab", "torrent"):
            continue

        idx_id = str(idx_cfg.get("id"))
        if indexer_status.paused_until(idx_id) is not None:
            continue

        try:
            driver = get_indexer_driver(idx_cfg)
        except Exception as exc:  # noqa: BLE001 - driver init
            logger.warning("Could not instantiate driver for indexer %s: %s", idx_id, safe_exc(exc))
            counts["errors"] += 1
            continue

        if not hasattr(driver, "fetch_page"):
            continue

        for page in range(effective_pages):
            if indexer_status.paused_until(idx_id) is not None:
                break
            idx_name = str(idx_cfg.get("name") or idx_id)
            bf_msg = f"Looking through {idx_name} releases, page {page + 1} of {effective_pages}"
            _set_progress(0, 0, message=bf_msg, handle=handle)
            offset = page * 100
            try:
                releases = driver.fetch_page(offset=offset, limit=100)
                indexer_status.record_success(idx_id)
            except IndexerError as exc:
                indexer_status.record_failure(idx_id, exc)
                counts["errors"] += 1
                break

            if not releases:
                break

            counts["collected"] += collect_from_releases(db, idx_cfg, releases)

        _backfill_search_terms(db, idx_cfg, driver, counts, handle)

    scan_res = scan_pending(db, limit=200, handle=handle)
    for k in ("scanned", "skipped", "failed", "errors", "songs", "skipped_magnet"):
        counts[k] = counts.get(k, 0) + scan_res.get(k, 0)

    try:
        counts["pruned"] = db.prune_pack_index()
    except sqlite3.Error as exc:
        logger.warning("Failed to prune pack index: %s", safe_exc(exc))

    return counts


def is_running() -> bool:
    """True when a pack scan sweep is currently in progress."""
    return _run_lock.locked()


def get_status(db: Any = None) -> dict[str, Any]:
    """Returns {running, last_run} for tasks and health APIs."""
    with _state_lock:
        last = dict(_last_run) if _last_run else None
    if last is None and db is not None:
        try:
            raw = db.get_kv(LAST_RUN_KV)
            data = json.loads(raw) if raw else None
            last = data if isinstance(data, dict) else None
        except (ValueError, TypeError, sqlite3.Error) as exc:
            logger.warning("Ignoring corrupt %s: %s", LAST_RUN_KV, type(exc).__name__)
    return {"running": is_running(), "last_run": last}


def _format_sweep_summary(stats: dict[str, int], db: Any) -> str:
    """Formats plain English summary for the sweep."""
    indexers = db.list_indexers(enabled_only=True) if hasattr(db, "list_indexers") else []
    has_pack_indexer = any(
        str(idx.get("pack_scan") or "off").lower() in ("auto", "all")
        for idx in indexers
    )

    scanned = int(stats.get("scanned") or 0)
    skipped = int(stats.get("skipped") or 0)
    failed = int(stats.get("failed") or 0)
    total_active = scanned + skipped + failed

    if not has_pack_indexer and total_active == 0:
        return "No indexer has Index packs turned on."

    idx_stats = db.pack_index_stats() if hasattr(db, "pack_index_stats") else {}
    n_holds = int(idx_stats.get("packs") or 0)
    m_songs = int(idx_stats.get("files") or 0)

    if total_active == 0:
        pack_word = "pack" if n_holds == 1 else "packs"
        song_word = "song" if m_songs == 1 else "songs"
        return f"No new packs to read. Index holds {n_holds:,} {pack_word} ({m_songs:,} {song_word})."

    pack_word = "pack" if scanned == 1 else "packs"
    songs = int(stats.get("songs") or 0)
    song_word = "song" if songs == 1 else "songs"
    parts = [f"{songs:,} {song_word} indexed"]

    if skipped > 0:
        skipped_magnet = int(stats.get("skipped_magnet") or 0)
        if skipped_magnet > 0 and skipped_magnet == skipped:
            parts.append(f"{skipped:,} skipped (magnet only)")
        elif skipped_magnet > 0:
            parts.append(f"{skipped:,} skipped ({skipped_magnet:,} magnet only)")
        else:
            parts.append(f"{skipped:,} skipped")

    if failed > 0:
        parts.append(f"{failed:,} failed")

    holds_word = "pack" if n_holds == 1 else "packs"
    return f"Read {scanned:,} {pack_word}: {', '.join(parts)}. Index holds {n_holds:,} {holds_word}."


def run_sweep(
    db: Any,
    *,
    now: Optional[datetime] = None,
    trigger: str = TRIGGER_MANUAL,
) -> dict[str, Any]:
    """Runs one full or incremental pack index sweep."""
    global _last_run
    if not _run_lock.acquire(blocking=False):
        raise PackScannerBusy("A pack scan sweep is already running")

    when = now or datetime.now(timezone.utc)
    started = _iso(when)
    error: Optional[str] = None
    stats: dict[str, int] = {
        "collected": 0,
        "scanned": 0,
        "skipped": 0,
        "failed": 0,
        "errors": 0,
        "pruned": 0,
        "songs": 0,
        "skipped_magnet": 0,
    }

    try:
        with record_task_run(db, "pack_index", trigger) as run, track_job("pack_index", "Pack Index") as handle:
            need_backfill = True
            if trigger != TRIGGER_MANUAL:
                try:
                    raw_bf = db.get_kv(LAST_BACKFILL_KV)
                    if raw_bf:
                        dt = parse_iso(raw_bf)
                        if dt and (when - dt) < timedelta(hours=BACKFILL_INTERVAL_HOURS):
                            need_backfill = False
                except sqlite3.Error as exc:
                    logger.warning("Could not read %s: %s", LAST_BACKFILL_KV, safe_exc(exc))

            if need_backfill:
                bf_stats = backfill(db, handle=handle)
                for k, v in bf_stats.items():
                    stats[k] = stats.get(k, 0) + v
                try:
                    db.set_kv(LAST_BACKFILL_KV, _iso(when))
                except sqlite3.Error as exc:
                    logger.warning("Could not persist %s: %s", LAST_BACKFILL_KV, safe_exc(exc))
            else:
                sp_stats = scan_pending(db, limit=200, handle=handle)
                for k, v in sp_stats.items():
                    stats[k] = stats.get(k, 0) + v

            msg = _format_sweep_summary(stats, db)
            run.message = handle.message = msg
            logger.info("Pack index: %s", msg)

        return stats
    except Exception as exc:
        error = safe_exc(exc)
        raise
    finally:
        _reset_progress()
        record = {
            "started_at": started,
            "finished_at": _iso(datetime.now(timezone.utc)),
            "stats": stats,
            "error": error,
        }
        with _state_lock:
            _last_run = record
        try:
            db.set_kv(LAST_RUN_KV, json.dumps(record))
        except sqlite3.Error as exc:
            logger.warning("Could not persist %s: %s", LAST_RUN_KV, safe_exc(exc))
        _run_lock.release()


def start_sweep_async(db: Any) -> bool:
    """Runs a sweep on a background thread. Returns False if already running."""
    if _run_lock.locked():
        return False

    def _target() -> None:
        try:
            run_sweep(db, trigger=TRIGGER_MANUAL)
        except PackScannerBusy:
            return
        except Exception as exc:  # noqa: BLE001 - background thread error logging
            logger.error("Pack scanner background sweep failed: %s", safe_exc(exc))
            logger.debug("Pack scanner traceback", exc_info=True)

    thread = threading.Thread(target=_target, daemon=True, name="ManualPackIndexTask")
    thread.start()
    return True


class PackIndexWorker:
    """Daemon thread: runs one pack scan cycle per interval (default 6h). Stop-event based."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def start(
        self,
        db: Any,
        config: Any = None,
        interval_seconds: float = WORKER_INTERVAL_SECONDS,
        initial_delay: float = WORKER_INITIAL_DELAY_SECONDS,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        with self._lock:
            if self._is_running:
                return False
            self._stop_event.clear()
            self._is_running = True
        cycle_interval = interval_fn or (lambda: float(interval_seconds))

        def _loop() -> None:
            if not self._stop_event.wait(initial_delay):
                if hasattr(db, "reparse_pack_files"):
                    try:
                        db.reparse_pack_files()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Failed to reparse pack files: %s", safe_exc(exc))
                while not self._stop_event.is_set():
                    self.run_once(db)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="PackIndexWorkerThread")
        self._thread.start()
        return True

    def run_once(self, db: Any) -> bool:
        try:
            run_sweep(db, trigger=TRIGGER_SCHEDULED)
            return True
        except PackScannerBusy:
            return False
        except Exception as exc:  # noqa: BLE001 - schedule loop must survive any one failure
            logger.error("PackIndexWorker: sweep failed: %s", safe_exc(exc))
            logger.debug("PackIndexWorker traceback", exc_info=True)
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


pack_index_worker = PackIndexWorker()

__all__ = [
    "PackIndexWorker",
    "PackScannerBusy",
    "backfill",
    "collect_from_releases",
    "get_pack_backfill_pages",
    "get_progress",
    "get_status",
    "is_running",
    "pack_index_worker",
    "run_sweep",
    "scan_pending",
    "start_sweep_async",
]
