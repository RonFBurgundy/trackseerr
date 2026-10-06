"""Library health: diff the music folder against what the media server actually indexes, by file path.

``run_check`` walks the music root, lists the server's files (mapped to local paths), and records two findings kinds:
``server_unindexed`` (on disk, not on the server; each classified into one cause) and ``server_stale`` (on the server,
gone from disk). ``weak_match`` findings are written by the importer (``record_weak_match``). See
``docs/library-server-reconciliation.md`` for the design contract.
"""

import fnmatch
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import mutagen

from plex_playlist_sync import path_mapping
from plex_playlist_sync.import_security import check_magic
from plex_playlist_sync.job_tracker import track_job
from plex_playlist_sync.library import AUDIO_EXTENSIONS, inspect_audio_file
from plex_playlist_sync.media_servers.base import MediaServerError, MediaServerUnsupported
from plex_playlist_sync.recycle_bin import EXCLUDED_DIRNAMES, is_system_dirname, is_system_filename
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)

KIND_UNINDEXED = "server_unindexed"
KIND_STALE = "server_stale"
KIND_WEAK = "weak_match"
KIND_ORPHAN_TORRENT = "orphan_torrent"
KIND_CLEANUP_FAILED = "cleanup_failed"

CAUSE_FOLDER = "folder_not_in_server"
CAUSE_NOT_SCANNED = "not_scanned_yet"
CAUSE_UNSUPPORTED = "unsupported_format"
CAUSE_CORRUPT = "corrupt"
CAUSE_MISSING_TAGS = "missing_tags"
CAUSE_PERMISSIONS = "unreadable_permissions"
CAUSE_IGNORED = "ignored_by_rule"
CAUSE_UNKNOWN = "unknown"
CAUSE_STALE = "stale_on_server"
CAUSE_WEAK = "weak_tag_match"
CAUSE_ORPHAN_TORRENT = "orphan_torrent"
CAUSE_CLEANUP_FAILED = "cleanup_failed"

# Container extensions (no dot) each server type cannot play or index. Plex skips these; Jellyfin and Subsonic servers
# index anything their ffmpeg/transcoder can read, so their sets are empty. These extensions are added to the disk walk
# so the rule can actually fire for files the importer itself never produces.
UNSUPPORTED_EXTENSIONS: dict[str, frozenset[str]] = {
    "plex": frozenset({"ape", "wv", "dsf", "dff", "tak", "mpc"}),
    "jellyfin": frozenset(),
    "subsonic": frozenset(),
}

SUGGESTIONS: dict[str, str] = {
    CAUSE_FOLDER: "This folder is not part of the server's library. Add it to the server library, or fix the path mapping.",
    CAUSE_NOT_SCANNED: "The server has not scanned these files yet. Refresh the server library.",
    CAUSE_UNSUPPORTED: "The server does not support this format. Convert the files, or ignore them.",
    CAUSE_CORRUPT: "These files look corrupt or truncated. Re-download them (search the track again from Wanted).",
    CAUSE_MISSING_TAGS: "These files have no artist, album or title tag. Retag them with Manual Import or Rename.",
    CAUSE_PERMISSIONS: "The server probably cannot read these files. Fix the file permissions.",
    CAUSE_IGNORED: "A .plexignore rule excludes these files. Remove the rule if they should be indexed.",
    CAUSE_UNKNOWN: "No known cause. Check the tags and path, then rescan the server library.",
    CAUSE_STALE: "The server lists files that no longer exist on disk. Empty the server's trash or rescan its library.",
    CAUSE_WEAK: "Imported on a weak tag match. Check it and use Re-match if it landed on the wrong track.",
    CAUSE_ORPHAN_TORRENT: "This torrent is in TrackSeerr's category but TrackSeerr never grabbed or imported it. It is never removed automatically: remove it yourself, with or without its files.",
    CAUSE_CLEANUP_FAILED: "TrackSeerr could not remove this finished torrent after 3 attempts. Check the download client, then retry.",
}

SUGGEST_SAMPLE = 500
# With no known last scan, files modified this recently are assumed not to have been scanned yet.
RECENT_GRACE = timedelta(hours=24)
_SCAN_BATCH = 500

_run_lock = threading.Lock()


class LibraryHealthBusy(RuntimeError):
    """A check is already running."""


def is_running() -> bool:
    return _run_lock.locked()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _walk_audio(root: Path, extensions: frozenset[str]) -> Iterator[str]:
    """Every audio file under ``root`` (os.scandir recursion; symlinked directories are not followed)."""
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_system_dirname(entry.name) and entry.name not in EXCLUDED_DIRNAMES:
                                stack.append(entry.path)
                        elif is_system_filename(entry.name):
                            continue
                        elif entry.is_file() and os.path.splitext(entry.name)[1].lower() in extensions:
                            yield entry.path
                    except OSError as exc:
                        logger.warning("library health: cannot stat %s: %s", entry.path, type(exc).__name__)
        except OSError as exc:
            logger.warning("library health: cannot read directory %s: %s", current, type(exc).__name__)


def disk_inventory(music_root: Path, server_kind: str) -> dict[str, str]:
    """normalize_key(path) -> path for every audio file under the music root (orphans included)."""
    exts = {e.lower() for e in AUDIO_EXTENSIONS}
    exts |= {"." + e for e in UNSUPPORTED_EXTENSIONS.get(server_kind, frozenset())}
    return {path_mapping.normalize_key(p): p for p in _walk_audio(music_root, frozenset(exts))}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    music_root: Path
    server_kind: str
    last_scan: Optional[datetime]
    indexed_dirs: set[str]
    mapped_roots: list[str]
    zero_overlap: bool
    dir_mtimes: dict[str, float] = field(default_factory=dict)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _topmost_unindexed_dir(directory: str, ctx: _Context) -> tuple[Optional[str], str]:
    """(topmost directory whose whole subtree is unindexed, its parent), or (None, root) for root-level files."""
    root = str(ctx.music_root)
    top: Optional[str] = None
    cur = directory
    while cur != root and _under(cur, root):
        if cur in ctx.indexed_dirs:
            break
        top = cur
        cur = os.path.dirname(cur)
    return top, cur


def _folder_rule(directory: str, ctx: _Context) -> Optional[str]:
    """Group key (the topmost unindexed directory) when the folder rule fires, else None."""
    top, _ = _topmost_unindexed_dir(directory, ctx)
    anchor = top or directory
    if ctx.zero_overlap:
        return anchor
    if ctx.mapped_roots:
        nk = path_mapping.normalize_key(directory)
        if not any(_under(nk, path_mapping.normalize_key(r)) for r in ctx.mapped_roots):
            return anchor
    # ``top`` exists only when the walk stopped at an indexed ancestor or at the root, i.e. a wholly unindexed
    # subtree sits beside indexed content (the zero-overlap case returned above).
    return top


def _plexignored(path: str, music_root: Path) -> bool:
    root = str(music_root)
    directory = os.path.dirname(path)
    while _under(directory, root):
        ignore = os.path.join(directory, ".plexignore")
        if os.path.isfile(ignore):
            rel = os.path.relpath(path, directory)
            try:
                lines = Path(ignore).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                logger.warning("library health: cannot read %s: %s", ignore, type(exc).__name__)
                lines = []
            for line in lines:
                pat = line.strip()
                if not pat or pat.startswith("#"):
                    continue
                pat = pat.rstrip("/")
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(path), pat) or any(
                    fnmatch.fnmatch(part, pat) for part in rel.split(os.sep)[:-1]
                ):
                    return True
        if directory == root:
            break
        directory = os.path.dirname(directory)
    return False


def _newest_mtime(directory: str, own: float, ctx: _Context) -> float:
    """Newest mtime of ``own`` and the direct files of its directory (cached per directory).

    An album imported by copy/hardlink can keep an old mtime on some tracks while its siblings are new.
    """
    cached = ctx.dir_mtimes.get(directory)
    if cached is None:
        cached = 0.0
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            cached = max(cached, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            pass
        ctx.dir_mtimes[directory] = cached
    return max(cached, own)


def _recently_changed(directory: str, st: os.stat_result, ctx: _Context) -> bool:
    newest = _newest_mtime(directory, st.st_mtime, ctx)
    if ctx.last_scan is not None:
        return newest > ctx.last_scan.timestamp()
    return newest > (_now() - RECENT_GRACE).timestamp()


def classify_unindexed(path: str, ctx: _Context) -> tuple[str, str, dict[str, Any]]:
    """(cause, group_key, detail) for one unindexed file. First matching rule wins, in the documented order.

    "Not scanned yet" is evaluated first: a freshly imported album folder is unindexed *because* the server has not
    scanned it, and would otherwise read as a folder outside the server's library.
    """
    directory = os.path.dirname(path)
    detail: dict[str, Any] = {}

    try:
        st = os.stat(path)
    except OSError as exc:
        detail["error"] = type(exc).__name__
        return CAUSE_UNKNOWN, directory, detail

    if _recently_changed(directory, st, ctx):
        return CAUSE_NOT_SCANNED, directory, detail

    group = _folder_rule(directory, ctx)
    if group is not None:
        return CAUSE_FOLDER, group, detail

    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in UNSUPPORTED_EXTENSIONS.get(ctx.server_kind, frozenset()):
        detail["extension"] = ext
        return CAUSE_UNSUPPORTED, directory, detail

    problem = check_magic(path)
    tags: dict[str, Any] = {}
    if problem is None:
        try:
            tags = inspect_audio_file(path)
        except (ValueError, OSError, mutagen.MutagenError) as exc:
            problem = f"unparseable: {type(exc).__name__}"
    if problem is not None:
        detail["problem"] = problem
        return CAUSE_CORRUPT, directory, detail

    detail.update({k: tags.get(k) for k in ("artist", "album", "title")})
    if not (tags.get("artist") and tags.get("album") and tags.get("title")):
        return CAUSE_MISSING_TAGS, directory, detail

    if not (st.st_mode & (stat.S_IROTH | stat.S_IRGRP)):
        detail["mode"] = oct(stat.S_IMODE(st.st_mode))
        return CAUSE_PERMISSIONS, directory, detail

    if ctx.server_kind == "plex" and _plexignored(path, ctx.music_root):
        return CAUSE_IGNORED, directory, detail

    return CAUSE_UNKNOWN, directory, detail


# ---------------------------------------------------------------------------
# Dismissals
# ---------------------------------------------------------------------------


class _Dismissed:
    def __init__(self, dismissals: list[dict[str, Any]]) -> None:
        self.files = {path_mapping.normalize_key(d["path"]) for d in dismissals if d["scope"] == "file"}
        self.folders = [path_mapping.normalize_key(d["path"]) for d in dismissals if d["scope"] == "folder"]

    def covers(self, path: str) -> bool:
        key = path_mapping.normalize_key(path)
        return key in self.files or any(_under(key, f) for f in self.folders)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _finish(db: Any, run: dict[str, Any], error: Optional[str] = None) -> dict[str, Any]:
    run["error"] = error
    run["finished_at"] = _now().isoformat()
    return db.record_library_health_run(run)


def _resolve_mapping(
    db: Any, server: Any, server_kind: str, disk_paths: list[str], first_refs: list[Any]
) -> tuple[Optional[tuple[str, str]], bool]:
    """(mapping or None, auto). Uses the saved mapping; otherwise suggests one from a sample and persists it."""
    saved = db.get_media_server_path_mapping()
    if saved and saved.get("server_kind") in ("", server_kind) and (saved["server_prefix"] or saved["local_prefix"]):
        return (saved["server_prefix"], saved["local_prefix"]), bool(saved.get("auto"))
    if server.paths_relative or not first_refs:
        return None, False
    suggestion = path_mapping.suggest_mapping([r.path for r in first_refs[:SUGGEST_SAMPLE]], disk_paths)
    if suggestion is None:
        return None, False
    db.set_media_server_path_mapping(
        {"server_prefix": suggestion[0], "local_prefix": suggestion[1], "auto": True, "server_kind": server_kind}
    )
    return (suggestion[0], suggestion[1]), True


def _execute(db: Any, server: Optional[Any], music_root: Path, now: datetime) -> dict[str, Any]:
    started = now.isoformat()
    run: dict[str, Any] = {
        "started_at": started,
        "server_kind": getattr(server, "kind", None),
        "disk_files": 0,
        "server_files": 0,
        "unindexed": 0,
        "stale": 0,
    }
    if server is None:
        return _finish(db, run, "No media server is configured.")
    server_kind = str(getattr(server, "kind", "") or "")
    if not server.capabilities.file_paths:
        return _finish(db, run, f"{server_kind or 'This media server'} does not expose library file paths.")
    if not music_root.is_dir():
        return _finish(db, run, f"The music folder {music_root} does not exist.")

    try:
        disk = disk_inventory(music_root, server_kind)
        run["disk_files"] = len(disk)
        refs = iter(server.iter_library_files())
        head: list[Any] = []
        for ref in refs:  # buffer a sample to suggest a mapping from
            head.append(ref)
            if len(head) >= SUGGEST_SAMPLE:
                break
        mapping, auto = _resolve_mapping(db, server, server_kind, list(disk.values()), head)
        relative = bool(server.paths_relative)

        def local_path(server_path: str) -> str:
            return path_mapping.apply_mapping(server_path, mapping, relative=relative, music_root=str(music_root))

        server_map: dict[str, Any] = {}
        for ref in _chain(head, refs):
            server_map[path_mapping.normalize_key(local_path(ref.path))] = ref
        last_scan = server.last_scan_at()
        roots = [local_path(r) for r in server.library_roots()] if not relative else []
    except MediaServerUnsupported as exc:
        return _finish(db, run, f"Library file paths are not available: {safe_exc(exc)}")
    except MediaServerError as exc:
        logger.warning("library health: media server error: %s", safe_exc(exc))
        return _finish(db, run, f"Could not read the media server library: {safe_exc(exc)}")
    except (OSError, ValueError, KeyError) as exc:
        logger.error("library health: check failed: %s", safe_exc(exc))
        logger.debug("library health traceback", exc_info=True)
        return _finish(db, run, f"Check failed: {safe_exc(exc)}")

    run["server_files"] = len(server_map)
    indexed_keys = set(disk) & set(server_map)
    unindexed = [disk[k] for k in sorted(set(disk) - indexed_keys)]
    zero_overlap = not indexed_keys
    stale_keys = [] if zero_overlap and disk else sorted(set(server_map) - indexed_keys)

    root = str(music_root)
    indexed_dirs: set[str] = set()
    for key in indexed_keys:
        cur = os.path.dirname(disk[key])
        while cur != root and _under(cur, root) and cur not in indexed_dirs:
            indexed_dirs.add(cur)
            cur = os.path.dirname(cur)

    ctx = _Context(music_root, server_kind, last_scan, indexed_dirs, roots, zero_overlap)
    dismissed = _Dismissed(db.list_library_health_dismissals())

    rows: list[dict[str, Any]] = []
    for path in unindexed:
        if dismissed.covers(path):
            continue
        cause, group_key, detail = classify_unindexed(path, ctx)
        rows.append(
            {"kind": KIND_UNINDEXED, "server_kind": server_kind, "cause": cause, "group_key": group_key, "path": path, "detail": detail}
        )
    for key in stale_keys:
        ref = server_map[key]
        shown = local_path(ref.path)
        if dismissed.covers(shown):
            continue
        rows.append(
            {
                "kind": KIND_STALE,
                "server_kind": server_kind,
                "cause": CAUSE_STALE,
                "group_key": os.path.dirname(shown),
                "path": shown,
                "detail": {"server_path": ref.path, "title": ref.title, "artist": ref.artist, "album": ref.album},
            }
        )

    seen_at = started
    for i in range(0, len(rows), _SCAN_BATCH):
        db.upsert_library_health_findings(rows[i : i + _SCAN_BATCH], seen_at)
    db.delete_library_health_findings_not_seen([KIND_UNINDEXED, KIND_STALE], seen_at)
    run["unindexed"] = sum(1 for r in rows if r["kind"] == KIND_UNINDEXED)
    run["stale"] = sum(1 for r in rows if r["kind"] == KIND_STALE)
    run["mapping_auto"] = auto
    return _finish(db, run)


def _chain(head: list[Any], rest: Iterator[Any]) -> Iterator[Any]:
    yield from head
    yield from rest


def run_check(db: Any, server: Optional[Any], *, music_root: Path | str, now: Optional[datetime] = None) -> dict[str, Any]:
    """One full check, recorded in the job tracker. Raises LibraryHealthBusy when another check is running."""
    if not _run_lock.acquire(blocking=False):
        raise LibraryHealthBusy("A library health check is already running")
    try:
        return _run_tracked(db, server, Path(music_root), now or _now())
    finally:
        _run_lock.release()


def _run_tracked(db: Any, server: Optional[Any], music_root: Path, now: datetime) -> dict[str, Any]:
    with track_job("library_health", "Library health check") as handle:
        run = _execute(db, server, music_root, now)
        if run.get("error"):
            handle.failed = str(run["error"])[:200]
        else:
            handle.message = f"unindexed={run['unindexed']}, stale={run['stale']}"
        return run


def start_check_async(db: Any, server: Optional[Any], *, music_root: Path | str) -> bool:
    """Starts a check on a background thread. False (nothing started) when one is already running."""
    if not _run_lock.acquire(blocking=False):
        return False

    def _target() -> None:
        try:
            _run_tracked(db, server, Path(music_root), _now())
        except Exception as exc:  # noqa: BLE001 - a thread must not die silently; the cause is logged
            logger.error("library health check crashed: %s", safe_exc(exc))
            logger.debug("library health traceback", exc_info=True)
        finally:
            _run_lock.release()

    threading.Thread(target=_target, daemon=True, name="LibraryHealthCheck").start()
    return True


# ---------------------------------------------------------------------------
# Weak import matches
# ---------------------------------------------------------------------------


def record_weak_match(
    db: Any, path: str, *, track_id: str, title: str, source_name: str, strength: str
) -> bool:
    """Records a weak-match finding for an imported file. Never raises: a failure is logged and False returned."""
    try:
        db.upsert_library_health_findings(
            [
                {
                    "kind": KIND_WEAK,
                    "server_kind": None,
                    "cause": CAUSE_WEAK,
                    "group_key": os.path.dirname(path),
                    "path": path,
                    "detail": {"track_id": track_id, "title": title, "source_name": source_name, "strength": strength},
                }
            ],
            _now().isoformat(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - recording is advisory; the import must never fail because of it
        logger.warning("Could not record weak-match finding for %s: %s", path, safe_exc(exc))
        return False


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def group_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for f in findings:
        key = (f["kind"], f["cause"], f["group_key"])
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "group_key": f["group_key"],
                "kind": f["kind"],
                "cause": f["cause"],
                "count": 0,
                "sample_path": f["path"],
                "suggestion": SUGGESTIONS.get(f["cause"], ""),
            }
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: (-g["count"], g["group_key"]))


def weekly_due(db: Any, now: Optional[datetime] = None) -> bool:
    """True when the weekly schedule is on and the last run finished at least a week ago (or never ran)."""
    if not db.get_library_health_weekly():
        return False
    last = db.get_last_library_health_run()
    if not last or not last.get("finished_at"):
        return True
    try:
        finished = datetime.fromisoformat(str(last["finished_at"]))
    except ValueError:
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return ((now or _now()) - finished).days >= 7


def mapping_view(db: Any) -> Optional[dict[str, Any]]:
    m = db.get_media_server_path_mapping()
    if not m:
        return None
    return {"server_prefix": m["server_prefix"], "local_prefix": m["local_prefix"], "auto": m["auto"]}


def run_view(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return dict(row) if row else None


def finding_view(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f["id"],
        "kind": f["kind"],
        "cause": f["cause"],
        "group_key": f["group_key"],
        "path": f["path"],
        "detail": f.get("detail"),
        "first_seen": f["first_seen"],
        "last_seen": f["last_seen"],
    }


# ---------------------------------------------------------------------------
# Weekly schedule
# ---------------------------------------------------------------------------

WORKER_WAKE_SECONDS = 6 * 3600
WORKER_INITIAL_DELAY_SECONDS = 300.0


class LibraryHealthWorker:
    """Daemon thread that starts a check when the weekly schedule is due and a media server is configured."""

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
        config: Any,
        interval_seconds: float = WORKER_WAKE_SECONDS,
        initial_delay: float = WORKER_INITIAL_DELAY_SECONDS,
    ) -> bool:
        with self._lock:
            if self._is_running:
                return False
            self._stop_event.clear()
            self._is_running = True

        def _loop() -> None:
            if not self._stop_event.wait(initial_delay):
                while not self._stop_event.is_set():
                    self.run_if_due(db, config)
                    self._stop_event.wait(interval_seconds)
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="LibraryHealthWorkerThread")
        self._thread.start()
        return True

    def run_if_due(self, db: Any, config: Any) -> bool:
        """Runs one check when due. Returns True when a check was started and finished."""
        try:
            if not weekly_due(db):
                return False
            from plex_playlist_sync.media_servers import get_media_server

            server = get_media_server(config)
            if server is None or not server.capabilities.file_paths:
                return False
            music_root = Path(db.get_media_management_settings().get("root_folder_path") or "/music")
            run_check(db, server, music_root=music_root)
            return True
        except LibraryHealthBusy:
            return False
        except Exception as exc:  # noqa: BLE001 - the schedule loop must survive any one failure; the cause is logged
            logger.error("LibraryHealthWorker: scheduled check failed: %s", safe_exc(exc))
            logger.debug("LibraryHealthWorker traceback", exc_info=True)
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


library_health_worker = LibraryHealthWorker()

__all__ = [
    "LibraryHealthBusy",
    "LibraryHealthWorker",
    "library_health_worker",
    "KIND_CLEANUP_FAILED",
    "KIND_ORPHAN_TORRENT",
    "SUGGESTIONS",
    "UNSUPPORTED_EXTENSIONS",
    "classify_unindexed",
    "group_findings",
    "is_running",
    "record_weak_match",
    "run_check",
    "start_check_async",
    "weekly_due",
]
