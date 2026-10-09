"""Recycle bin and quarantine locations, replaced-file disposal and the scheduled cleanup.

Replaced library files (an upgrade or an issue replacement) are *renamed* into
``<recycle>/<YYYY-MM-DD>/<path relative to the library root>``; nothing is ever copied and deleted, so a library file
that is a hardlink of a seeding torrent keeps the torrent's inode untouched. The only way to delete a replaced file
instead is the explicit ``recycle_bin_permanent_delete`` setting. Files outside the library root, or under any download
client root, are never touched.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from trackseerr.import_security import QUARANTINE_DIRNAME as LEGACY_QUARANTINE_DIRNAME
from trackseerr.item_history import TRIGGER_RECYCLE_CLEANUP, download_trigger_kwargs, emit
from trackseerr.redaction import safe_exc
from trackseerr.task_manager import (
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    record_task_run,
    wait_for_next_cycle,
)
from trackseerr.system_paths import (  # noqa: F401  (re-exported for library walkers)
    SYSTEM_DIRNAMES,
    SYSTEM_FILENAMES,
    is_system_dirname,
    is_system_filename,
    is_system_folder_name,
)

logger = logging.getLogger(__name__)

RECYCLE_DIRNAME = ".trackseerr-recycle"
QUARANTINE_DIRNAME = ".trackseerr-quarantine"
DEFAULT_RECYCLE_CLEANUP_DAYS = 30
EXCLUDED_DIRNAMES = frozenset({LEGACY_QUARANTINE_DIRNAME, RECYCLE_DIRNAME, QUARANTINE_DIRNAME})
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WORKER_INTERVAL_SECONDS = 24 * 3600.0
WORKER_INITIAL_DELAY_SECONDS = 15 * 60.0


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolved(raw: str | Path) -> Path:
    return Path(raw).resolve()


def _library_root(mm: dict[str, Any]) -> Optional[Path]:
    raw = str(mm.get("root_folder_path") or "").strip()
    return _resolved(raw) if raw else None


def effective_recycle_path(mm: dict[str, Any]) -> Optional[Path]:
    """The configured recycle path, else ``<library root>/.trackseerr-recycle``; None when neither is known."""
    raw = str(mm.get("recycle_bin_path") or "").strip()
    if raw:
        return _resolved(raw)
    root = _library_root(mm)
    return root / RECYCLE_DIRNAME if root else None


def effective_quarantine_path(mm: dict[str, Any]) -> Optional[Path]:
    """The configured quarantine path, else ``<library root>/.trackseerr-quarantine``; None when neither is known."""
    raw = str(mm.get("quarantine_folder_path") or "").strip()
    if raw:
        return _resolved(raw)
    root = _library_root(mm)
    return root / QUARANTINE_DIRNAME if root else None


def library_excluded_paths(mm: dict[str, Any]) -> list[Path]:
    """The effective recycle and quarantine folders: library walks skip these wherever they are."""
    return [p for p in (effective_recycle_path(mm), effective_quarantine_path(mm)) if p is not None]


def is_excluded_entry(entry: Path, root: Path, excluded: Iterable[Path]) -> bool:
    """True when ``entry`` (under ``root``) sits in a legacy/default quarantine or recycle folder, or an excluded path."""
    try:
        parts = entry.relative_to(root).parts
    except ValueError:
        parts = ()
    if any(p in EXCLUDED_DIRNAMES for p in parts):
        return True
    if any(is_system_dirname(p) for p in parts[:-1]) or (parts and is_system_filename(parts[-1])):
        return True
    return any(_under(entry, ex) for ex in excluded)


def prune_excluded_dirs(walk_root: str, dirs: list[str], excluded: Iterable[Path]) -> list[str]:
    """For ``os.walk``: the subset of ``dirs`` that is not a recycle/quarantine folder."""
    excluded = list(excluded)
    return [
        d for d in dirs
        if d not in EXCLUDED_DIRNAMES and not is_system_dirname(d) and not any(_under(Path(walk_root, d).resolve(), ex) for ex in excluded)
    ]


@dataclass
class DisposeResult:
    """Outcome for one replaced file. ``status`` is recycled, deleted, kept (left in place) or missing."""

    status: str
    old_path: Path
    dest: Optional[Path] = None
    reason: str = ""


def _recycle_destination(recycle_root: Path, rel: Path, today: date) -> Path:
    base = recycle_root / today.isoformat() / rel
    dest = base
    i = 0
    while os.path.lexists(dest):
        i += 1
        dest = base.with_name(f"{base.name}.{i}")
    return dest


def dispose_replaced_file(
    old_path: Path | str,
    *,
    library_root: Path | str,
    recycle_root: Optional[Path | str],
    client_roots: Iterable[Path],
    permanent_delete: bool = False,
    today: Optional[date] = None,
) -> DisposeResult:
    """Recycles (rename) or, when explicitly enabled, deletes a superseded library file. Never raises ``OSError``.

    Refuses (status ``kept``) when the file is outside the library root, under any download client root, inside the
    recycle bin, or when the move fails (e.g. EXDEV across filesystems): it is then left exactly where it was.
    """
    old_p = Path(os.path.abspath(old_path))
    lib = _resolved(library_root)
    roots = [Path(r) for r in client_roots]
    if not os.path.lexists(old_p):
        return DisposeResult("missing", old_p)
    try:
        resolved = old_p.resolve()
    except (OSError, RuntimeError) as exc:
        return DisposeResult("kept", old_p, reason=f"could not resolve path ({type(exc).__name__})")
    if any(_under(old_p, r) or _under(resolved, r) for r in roots):
        logger.warning("Replaced file %s is inside a download folder: left in place", old_p)
        return DisposeResult("kept", old_p, reason="inside a download folder: left in place")
    if not (_under(old_p, lib) and _under(resolved, lib)):
        logger.warning("Replaced file %s is outside the library root: left in place", old_p)
        return DisposeResult("kept", old_p, reason="outside the library: left in place")
    rec = _resolved(recycle_root) if recycle_root else None
    if rec is not None and _under(resolved, rec):
        return DisposeResult("kept", old_p, reason="already in the recycle bin")
    if old_p.is_symlink():
        # A symlink inside the library whose target is also inside it: only the link would move; leave it alone.
        return DisposeResult("kept", old_p, reason="is a symbolic link: left in place")

    if permanent_delete:
        try:
            os.unlink(old_p)  # drops this name only; a hardlinked torrent file keeps its inode
        except OSError as exc:
            logger.warning("Could not delete replaced file %s: %s: %s", old_p, type(exc).__name__, exc)
            return DisposeResult("kept", old_p, reason="could not delete")
        logger.info("Permanently deleted replaced file %s (recycle bin permanent delete is on)", old_p)
        return DisposeResult("deleted", old_p)

    if rec is None:
        return DisposeResult("kept", old_p, reason="no recycle bin location is configured")
    if any(_under(rec, r) for r in roots):
        logger.error("Recycle bin %s is inside a download folder; not moving %s", rec, old_p)
        return DisposeResult("kept", old_p, reason="recycle bin is inside a download folder")
    dest = _recycle_destination(rec, old_p.relative_to(lib), today or datetime.now(timezone.utc).date())
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old_p, dest)  # rename only: never copy+delete (EXDEV keeps the file in place)
    except OSError as exc:
        logger.warning("Could not recycle %s to %s: %s: %s", old_p, dest, type(exc).__name__, exc)
        return DisposeResult("kept", old_p, reason="could not move")
    logger.info("Recycled replaced file %s -> %s", old_p, dest)
    return DisposeResult("recycled", old_p, dest=dest)


def restore_recycled(result: DisposeResult) -> bool:
    """Puts a just-recycled file back (used when the replacement could not be placed). True on success."""
    if result.status != "recycled" or result.dest is None:
        return False
    try:
        os.rename(result.dest, result.old_path)
    except OSError as exc:
        logger.error("Could not restore %s from the recycle bin (%s): %s", result.old_path, result.dest, exc)
        return False
    return True


# ------------------------------------------------------------------------------- replacement (shared by importers)
def dispose_for_settings(
    mm: dict[str, Any], library_root: Path, old_p: Path, client_roots: Iterable[Path]
) -> DisposeResult:
    """``dispose_replaced_file`` driven by the media-management settings (recycle path, permanent-delete toggle)."""
    return dispose_replaced_file(
        old_p,
        library_root=library_root,
        recycle_root=effective_recycle_path(mm),
        client_roots=client_roots,
        permanent_delete=bool(mm.get("recycle_bin_permanent_delete")),
    )


def recycle_in_place_target(
    db: Any,
    mm: dict[str, Any],
    library_root: Path,
    desired: Path,
    track_id: Optional[str],
    client_roots: Iterable[Path],
) -> Optional[tuple[DisposeResult, dict[str, Any]]]:
    """When a new file would land on the path the track's current file already has, recycles that file first (rename)
    so the replacement takes the clean name instead of ``Name (1).ext`` and the old bytes are never overwritten.
    Returns ``(result, old row)``, or None when this does not apply (or the old file could not be moved, in which case
    the caller falls back to a collision-suffixed name). Skipped for permanent delete: the old file is deleted only
    after the replacement is in place."""
    if not track_id or mm.get("recycle_bin_permanent_delete") or not os.path.lexists(desired):
        return None
    try:
        row = db.get_library_file_by_path(str(desired))
    except sqlite3.Error as exc:
        logger.warning("Could not look up library file for %s: %s", desired, safe_exc(exc))
        return None
    if not row or str(row.get("track_id") or "") != str(track_id):
        return None
    result = dispose_for_settings(mm, library_root, desired, client_roots)
    return (result, row) if result.status == "recycled" else None


def log_recycled(
    db: Any,
    result: DisposeResult,
    new_path: Path,
    old_row: dict[str, Any],
    new_quality: str,
    *,
    title: str,
    download_id: Any,
    issue_id: Optional[str],
    retired: list[str],
    source: str = "AcquisitionWorker",
    log_prefix: str = "Upgrade import",
) -> None:
    """Logs and records the ``file_recycled`` history event for a replaced file; appends to ``retired`` for the
    issue's admin comment."""
    action = "deleted" if result.status == "deleted" else "recycled"
    where = f"{result.old_path} -> {result.dest}" if result.dest else f"{result.old_path} (deleted)"
    retired.append(where)
    logger.info("%s: %s old file %s (%s -> %s)", log_prefix, action, result.old_path, old_row.get("quality_name"), new_quality)
    try:
        db.record_event(
            "file_recycled",
            f"Replaced file {action} for '{title}': {old_row.get('quality_name')} -> {new_quality}",
            source=source,
            severity="info",
            details={
                "download_id": download_id,
                "issue_id": issue_id,
                "old_path": str(result.old_path),
                "recycled_to": str(result.dest) if result.dest else None,
                "new_path": str(new_path),
                "old_quality": old_row.get("quality_name"),
                "new_quality": new_quality,
                "permanent_delete": result.status == "deleted",
            },
        )
    except sqlite3.Error as ev_err:
        logger.warning("Failed to record file_recycled event: %s", safe_exc(ev_err))
    emit(
        db, "file_replaced", track_id=old_row.get("track_id"), download_id=str(download_id) if download_id else None,
        message=f"Replaced {old_row.get('quality_name')} file ({action})",
        details={
            "old_path": str(result.old_path), "disposition": action,
            "recycled_to": str(result.dest) if result.dest else None, "new_path": str(new_path),
            "old_quality": old_row.get("quality_name"), "new_quality": new_quality, "issue_id": issue_id,
        },
        **download_trigger_kwargs(db, str(download_id) if download_id else None),
    )


def recycle_replaced_files(
    db: Any,
    old_rows: list[dict[str, Any]],
    new_path: Path,
    library_root: Path,
    mm: dict[str, Any],
    client_roots: Iterable[Path],
    new_quality: str,
    retired: list[str],
    kept: list[str],
    *,
    title: str,
    download_id: Any,
    issue_id: Optional[str],
    source: str = "AcquisitionWorker",
    log_prefix: str = "Upgrade import",
) -> None:
    """Recycles a track's previous library files after an import replaced them.

    Rename into ``<recycle>/<date>/<path under the library>`` only (never copy+delete), or delete when the explicit
    permanent-delete setting is on. Files outside the library root or under any download client root are never
    moved. A file that cannot be moved stays on disk and is reported in ``kept``. Stale rows are removed either way
    so the track points at the new file only.
    """
    roots = list(client_roots)
    for row in old_rows:
        old_str = str(row.get("file_path") or "")
        if not old_str or old_str == str(new_path):
            continue
        old_p = Path(os.path.abspath(old_str))
        if old_p == new_path:
            continue
        result = dispose_for_settings(mm, library_root, old_p, roots)
        if result.status in ("recycled", "deleted"):
            log_recycled(db, result, new_path, row, new_quality, title=title, download_id=download_id,
                         issue_id=issue_id, retired=retired, source=source, log_prefix=log_prefix)
        elif result.status == "kept":
            kept.append(f"{old_p}: {result.reason}")
            emit(
                db, "file_replaced", track_id=row.get("track_id"),
                download_id=str(download_id) if download_id else None,
                message=f"Replaced file kept in place: {result.reason}",
                details={"old_path": str(old_p), "disposition": "kept", "new_path": str(new_path),
                         "old_quality": row.get("quality_name"), "new_quality": new_quality, "issue_id": issue_id},
                **download_trigger_kwargs(db, str(download_id) if download_id else None),
            )
            logger.warning("Replacement of %s: old file kept (%s)", old_p, result.reason)
        try:
            db.delete_library_file(str(row["id"]))
        except sqlite3.Error as del_err:
            logger.warning("Could not remove stale library file row %s: %s", row.get("id"), safe_exc(del_err))


# --------------------------------------------------------------------------------------------------- cleanup
@dataclass
class CleanupResult:
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"removed": list(self.removed), "errors": list(self.errors), "skipped_reason": self.skipped_reason}


def _bin_unsafe_reason(recycle_root: Path, library_root: Optional[Path], client_roots: Iterable[Path]) -> str:
    """Why the bin must not be pruned ('' when safe): it must be a real folder disjoint from the library and clients."""
    try:
        rec = recycle_root.resolve()
    except (OSError, RuntimeError) as exc:
        return f"could not resolve the recycle bin ({type(exc).__name__})"
    if rec == Path(rec.anchor) or len(rec.parts) < 2:
        return "recycle bin resolves to a filesystem root"
    if library_root is not None and (rec == library_root or _under(library_root, rec)):
        return "recycle bin is or contains the library root"
    for r in client_roots:
        r = Path(r)
        if _under(rec, r) or _under(r, rec):
            return "recycle bin overlaps a download folder"
    return ""


def _remove_entry(entry: Path) -> None:
    """Removes a file/symlink (the link itself) or a directory tree (never following symlinks)."""
    if entry.is_symlink() or not entry.is_dir():
        entry.unlink()
    else:
        shutil.rmtree(entry)


def cleanup_recycle_bin(
    recycle_root: Optional[Path | str],
    days: int,
    *,
    library_root: Optional[Path | str] = None,
    client_roots: Iterable[Path] = (),
    now: Optional[date] = None,
    empty_all: bool = False,
) -> CleanupResult:
    """Deletes dated folders older than ``days`` days (``days`` <= 0 does nothing) or, with ``empty_all``, everything.

    Only direct children of the recycle path are considered; symlinks are unlinked, never followed.
    """
    result = CleanupResult()
    if not empty_all and days <= 0:
        result.skipped_reason = "automatic cleanup is disabled (0 days)"
        return result
    if not recycle_root:
        result.skipped_reason = "no recycle bin location is configured"
        return result
    rec = Path(recycle_root)
    if not rec.is_dir():
        result.skipped_reason = "recycle bin does not exist yet"
        return result
    lib = _resolved(library_root) if library_root else None
    unsafe = _bin_unsafe_reason(rec, lib, client_roots)
    if unsafe:
        logger.error("Recycle bin cleanup refused: %s (%s)", unsafe, rec)
        result.skipped_reason = unsafe
        return result
    rec = rec.resolve()
    cutoff = (now or datetime.now(timezone.utc).date()) - timedelta(days=max(days, 0))
    try:
        entries = sorted(rec.iterdir())
    except OSError as exc:
        result.errors.append(f"could not list {rec}: {exc}")
        return result
    for entry in entries:
        if not empty_all:
            if entry.is_symlink() or not entry.is_dir() or not _DATE_DIR.match(entry.name):
                continue
            try:
                folder_date = date.fromisoformat(entry.name)
            except ValueError:
                continue
            if folder_date >= cutoff:
                continue
        try:
            _remove_entry(entry)
            result.removed.append(str(entry))
        except OSError as exc:
            logger.warning("Recycle bin cleanup could not remove %s: %s: %s", entry, type(exc).__name__, exc)
            result.errors.append(f"{entry.name}: {exc}")
    return result


def _client_roots(db: Any, mm: dict[str, Any]) -> list[Path]:
    from trackseerr.download_roots import allowed_roots_for_all_clients

    try:
        return list(allowed_roots_for_all_clients(db, mm).roots)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Could not read download client roots for the recycle bin: %s", safe_exc(exc))
        return []


def run_cleanup(db: Any, *, empty_all: bool = False) -> CleanupResult:
    """Prunes the bin per the current settings (or empties it); records a system event when something was removed."""
    mm = db.get_media_management_settings()
    days = int(mm.get("recycle_bin_cleanup_days") or 0)
    result = cleanup_recycle_bin(
        effective_recycle_path(mm), days,
        library_root=_library_root(mm), client_roots=_client_roots(db, mm), empty_all=empty_all,
    )
    _record_run(db, result, empty_all)
    _record_purged_files(db, list(result.removed), empty_all)
    return result


def _record_purged_files(db: Any, removed_paths: list[str], emptied: bool) -> None:
    """``file_deleted`` item events for every replaced file whose recycled copy was just purged from the bin.

    One history lookup and one bulk write for all purged paths, however many the cleanup removed.
    """
    if not removed_paths:
        return
    try:
        events = db.find_recycled_item_events(removed_paths)
    except sqlite3.Error:
        logger.exception("Could not look up recycled files under %d purged path(s)", len(removed_paths))
        return
    if not events:
        return
    label = "Recycle bin emptied" if emptied else "Recycle bin cleanup"
    batch = [
        {
            "event": "file_deleted", "track_id": ev.get("track_id"), "album_id": ev.get("album_id"),
            "artist_id": ev.get("artist_id"), "artist_name": ev.get("artist_name"),
            "album_title": ev.get("album_title"), "track_title": ev.get("track_title"),
            "trigger": TRIGGER_RECYCLE_CLEANUP, "trigger_label": label,
            "message": "Recycled file purged from the recycle bin",
            "details": {
                "path": (ev.get("details") or {}).get("recycled_to"),
                "original_path": (ev.get("details") or {}).get("old_path"),
            },
        }
        for ev in events
    ]
    try:
        db.record_item_events_bulk(batch)
    except sqlite3.Error:
        logger.exception("Could not record %d purged recycle-bin file event(s)", len(batch))


_status_lock = threading.Lock()
_running = False
_last_run: dict[str, Any] = {}


def _record_run(db: Any, result: CleanupResult, empty_all: bool) -> None:
    with _status_lock:
        _last_run.update(
            finished_at=datetime.now(timezone.utc).isoformat(),
            removed=len(result.removed), errors=len(result.errors), emptied=empty_all,
        )
    if not result.removed and not result.errors:
        return
    try:
        db.record_event(
            "recycle_bin_emptied" if empty_all else "recycle_bin_cleanup",
            f"Recycle bin {'emptied' if empty_all else 'cleanup'}: removed {len(result.removed)} item(s)",
            source="RecycleBin",
            severity="warning" if result.errors else "info",
            details=result.to_dict(),
        )
    except sqlite3.Error as exc:
        logger.warning("Could not record the recycle bin event: %s", safe_exc(exc))


def get_status() -> dict[str, Any]:
    with _status_lock:
        return {"running": _running, "last_run": dict(_last_run) or None}


def _run_recorded(db: Any, trigger: str) -> Any:
    """``run_cleanup`` wrapped in the task run history (failures propagate to the caller's own handling)."""
    with record_task_run(db, "recycle_bin_cleanup", trigger) as run:
        result = run_cleanup(db)
        removed = getattr(result, "removed", None)
        if isinstance(removed, (list, tuple)):
            run.message = f"removed={len(removed)}"
        return result


def start_cleanup_async(db: Any) -> bool:
    """Runs one cleanup in a background thread. False when one is already running."""
    global _running
    with _status_lock:
        if _running:
            return False
        _running = True

    def _go() -> None:
        global _running
        try:
            _run_recorded(db, TRIGGER_MANUAL)
        except (sqlite3.Error, OSError) as exc:
            logger.error("Recycle bin cleanup failed: %s", safe_exc(exc))
        finally:
            with _status_lock:
                _running = False

    threading.Thread(target=_go, daemon=True, name="RecycleBinCleanup").start()
    return True


class RecycleBinWorker:
    """Daemon thread: one cleanup shortly after start, then one per day."""

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
                while not self._stop_event.is_set():
                    self.run_once(db)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="RecycleBinWorkerThread")
        self._thread.start()
        return True

    def run_once(self, db: Any) -> bool:
        try:
            _run_recorded(db, TRIGGER_SCHEDULED)
            return True
        except (sqlite3.Error, OSError) as exc:
            logger.error("RecycleBinWorker: cleanup failed: %s", safe_exc(exc))
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


recycle_bin_worker = RecycleBinWorker()


# ------------------------------------------------------------------------------------- settings validation
def _nearest_existing(path: Path) -> Optional[Path]:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def validate_storage_paths(
    mm: dict[str, Any], client_roots: Iterable[Path]
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the recycle and quarantine paths of the merged settings ``mm``.

    Errors (block the save): not absolute, equal to the library root, inside a download client root, nested in each
    other. Warning: a different filesystem from the library root, where moves fall back to keeping files.
    """
    errors: list[str] = []
    warnings: list[str] = []
    lib = _library_root(mm)
    roots = [Path(r) for r in client_roots]
    configured: dict[str, Path] = {}
    for key, label in (("recycle_bin_path", "Recycle bin"), ("quarantine_folder_path", "Quarantine folder")):
        raw = str(mm.get(key) or "").strip()
        if not raw:
            continue
        if not os.path.isabs(raw):
            errors.append(f"{label} path must be absolute")
            continue
        p = _resolved(raw)
        configured[key] = p
        if lib is not None and p == lib:
            errors.append(f"{label} path must not be the library root")
        if any(_under(p, r) for r in roots):
            errors.append(f"{label} path must not be inside a download client folder")
        if lib is not None and not errors:
            near, lib_near = _nearest_existing(p), _nearest_existing(lib)
            if near is not None and lib_near is not None:
                try:
                    if os.stat(near).st_dev != os.stat(lib_near).st_dev:
                        warnings.append(
                            f"{label} is on a different filesystem from the library; files can't be moved there "
                            "and will be left in place"
                        )
                except OSError as exc:
                    logger.warning("Could not compare filesystems for %s: %s", p, exc)
    eff_r, eff_q = effective_recycle_path(mm), effective_quarantine_path(mm)
    if eff_r is not None and eff_q is not None and (configured.get("recycle_bin_path") or configured.get("quarantine_folder_path")):
        if _under(eff_r, eff_q) or _under(eff_q, eff_r):
            errors.append("The recycle bin and quarantine folders must not be inside each other")
    return errors, warnings
