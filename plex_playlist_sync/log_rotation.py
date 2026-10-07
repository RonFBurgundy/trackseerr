"""Time-based log rotation, retention/size pruning, live-tunable log settings and log file listing.

The active file is ``trackseerr.txt``. When it has been open for the rotation interval it is renamed to
``trackseerr.<YYYYMMDD-HHMM>.txt`` (the local time its first entry was written) and a fresh file starts. Rotated files
are pruned by age (retention days) and by a hard cap on the total size of the log directory's Trackseerr files
(oldest first), so TRACE logging can never fill the disk. Old-format ``trackseerr.log[.N]`` files are never touched or
counted, but are listed so they can still be downloaded.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .redaction import RedactLogFilter, safe_exc

logger = logging.getLogger(__name__)

TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")

CURRENT_LOG_NAME = "trackseerr.txt"
LEGACY_LOG_NAME = "trackseerr.log"

ROTATION_HOURS_PRESETS: tuple[int, ...] = (6, 8, 12, 24)
RETENTION_DAYS_RANGE: tuple[int, int] = (1, 30)
MAX_TOTAL_MB_PRESETS: tuple[int, ...] = (50, 100, 250, 500)
LEVEL_CHOICES: tuple[str, ...] = ("INFO", "DEBUG", "TRACE")

DEFAULT_ROTATION_HOURS = 12
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_TOTAL_MB = 100

_ROTATED_RE = re.compile(r"^trackseerr\.(\d{8}-\d{4})\.txt$")
_LISTABLE_RE = re.compile(r"^(?:trackseerr(?:\.\d{8}-\d{4})?\.txt|trackseerr\.log(?:\.\d+)?)$")
_STAMP_FORMAT = "%Y%m%d-%H%M"
_LINE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

KV_ROTATION_HOURS = "log_rotation_hours"
KV_RETENTION_DAYS = "log_retention_days"
KV_MAX_TOTAL_MB = "log_max_total_mb"
KV_LEVEL = "log_level"


def numeric_level(name: str) -> int:
    """Numeric value of a level name (TRACE included); unknown names fall back to INFO."""
    value = logging.getLevelName(str(name).strip().upper())
    return value if isinstance(value, int) else logging.INFO


def _first_line_time(path: Path) -> Optional[float]:
    """Epoch seconds of the timestamp on the first line of ``path`` (the handler's own format), if parseable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.readline(40)
    except OSError:
        return None
    try:
        return time.mktime(time.strptime(head[:19], _LINE_TS_FORMAT))
    except (ValueError, OverflowError):
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S")


class TimedLogFileHandler(logging.FileHandler):
    """File handler that rotates by elapsed time, survives restarts and prunes by age and total size.

    ``clock`` is injectable so tests never sleep. Rotation and pruning happen under the handler lock, before the
    record that triggered them is written, so no line is lost or split across files.
    """

    follows_log_level = True

    def __init__(
        self,
        log_path: str | os.PathLike[str],
        *,
        rotation_hours: int = DEFAULT_ROTATION_HOURS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self.rotation_hours = int(rotation_hours)
        self.retention_days = int(retention_days)
        self.max_total_mb = int(max_total_mb)
        self._start: Optional[float] = None
        super().__init__(str(log_path), mode="a", encoding="utf-8", delay=True)
        self.addFilter(RedactLogFilter())
        self._adopt_existing_file()
        self.prune()

    # -- configuration ---------------------------------------------------------------------------------------------
    def configure(self, *, rotation_hours: int, retention_days: int, max_total_mb: int) -> None:
        """Applies new limits live; the next emit (or this call, for pruning) honours them."""
        self.acquire()
        try:
            self.rotation_hours = int(rotation_hours)
            self.retention_days = int(retention_days)
            self.max_total_mb = int(max_total_mb)
            self.prune()
        finally:
            self.release()

    # -- state -----------------------------------------------------------------------------------------------------
    @property
    def log_dir(self) -> Path:
        return Path(self.baseFilename).parent

    def _adopt_existing_file(self) -> None:
        """Continue the interval of a file left by a previous process instead of restarting the clock."""
        path = Path(self.baseFilename)
        try:
            st = path.stat()
        except OSError:
            self._start = None
            return
        if st.st_size == 0:
            self._start = None
            return
        self._start = _first_line_time(path) or st.st_mtime

    def _due(self, now: float) -> bool:
        return self._start is not None and now - self._start >= self.rotation_hours * 3600

    def _rotate(self) -> None:
        if self.stream is not None:
            try:
                self.stream.flush()
            finally:
                self.stream.close()
                self.stream = None  # type: ignore[assignment]
        start = self._start if self._start is not None else self._clock()
        target = self._unique_target(start)
        try:
            os.replace(self.baseFilename, target)
        except OSError as exc:
            # Keep writing to the current file rather than lose lines; try again on the next record.
            logging.getLogger(__name__).warning("Could not rotate %s: %s", self.baseFilename, safe_exc(exc))
            return
        self._start = None
        self.prune()

    def _unique_target(self, start: float) -> str:
        base = Path(self.baseFilename).parent
        stamp = start
        for _ in range(1440):
            candidate = base / f"trackseerr.{time.strftime(_STAMP_FORMAT, time.localtime(stamp))}.txt"
            if not candidate.exists():
                return str(candidate)
            stamp += 60
        return str(base / f"trackseerr.{time.strftime(_STAMP_FORMAT, time.localtime(start))}.txt.dup")

    # -- pruning ---------------------------------------------------------------------------------------------------
    def prune(self) -> list[str]:
        """Deletes rotated files past retention, then oldest-first until under the size cap. Returns removed names."""
        removed: list[str] = []
        now = self._clock()
        rotated: list[tuple[float, int, Path]] = []
        current_size = 0
        try:
            entries = list(os.scandir(self.log_dir))
        except OSError as exc:
            logging.getLogger(__name__).warning("Could not scan the log directory: %s", safe_exc(exc))
            return removed
        for entry in entries:
            try:
                if entry.name == CURRENT_LOG_NAME:
                    current_size = entry.stat().st_size
                elif _ROTATED_RE.match(entry.name) and entry.is_file():
                    st = entry.stat()
                    rotated.append((st.st_mtime, st.st_size, Path(entry.path)))
            except OSError:
                continue
        rotated.sort(key=lambda item: item[0])  # oldest first
        cutoff = now - self.retention_days * 86400
        kept: list[tuple[float, int, Path]] = []
        for item in rotated:
            if item[0] < cutoff and self._remove(item[2]):
                removed.append(item[2].name)
            else:
                kept.append(item)
        cap = self.max_total_mb * 1024 * 1024
        total = current_size + sum(item[1] for item in kept)
        for item in kept:
            if total <= cap:
                break
            if self._remove(item[2]):
                removed.append(item[2].name)
                total -= item[1]
        return removed

    @staticmethod
    def _remove(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError as exc:
            logging.getLogger(__name__).warning("Could not delete old log %s: %s", path.name, safe_exc(exc))
            return False

    # -- logging.Handler -------------------------------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = self._clock()
            if self._due(now):
                self._rotate()
            if self._start is None:
                self._start = now
            super().emit(record)
            # The size cap must hold even without a rotation (TRACE can outgrow it inside one interval).
            if self.stream is not None and self.max_total_mb > 0:
                self._enforce_current_cap()
        except (OSError, ValueError):
            self.handleError(record)

    def _enforce_current_cap(self) -> None:
        try:
            size = self.stream.tell()
        except (OSError, ValueError):
            return
        if size >= self.max_total_mb * 1024 * 1024 // 2:
            # The live file alone has used half the budget: rotate early so pruning can reclaim it.
            self.stream.flush()
            self._rotate()


# -- settings -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogSettings:
    log_rotation_hours: int
    log_retention_days: int
    log_max_total_mb: int
    log_level: str


class LogSettingsError(ValueError):
    """An out-of-range or unsupported log setting."""


def default_log_level() -> str:
    return (os.environ.get("LOG_LEVEL") or "INFO").strip().upper() or "INFO"


def validate_log_settings(
    *,
    log_rotation_hours: Optional[int] = None,
    log_retention_days: Optional[int] = None,
    log_max_total_mb: Optional[int] = None,
    log_level: Optional[str] = None,
) -> dict[str, Any]:
    """Returns only the provided fields, normalised. Raises :class:`LogSettingsError` on an invalid value."""
    out: dict[str, Any] = {}
    if log_rotation_hours is not None:
        if isinstance(log_rotation_hours, bool) or log_rotation_hours not in ROTATION_HOURS_PRESETS:
            raise LogSettingsError(f"log_rotation_hours must be one of {list(ROTATION_HOURS_PRESETS)}")
        out["log_rotation_hours"] = int(log_rotation_hours)
    if log_retention_days is not None:
        lo, hi = RETENTION_DAYS_RANGE
        if isinstance(log_retention_days, bool) or not lo <= log_retention_days <= hi:
            raise LogSettingsError(f"log_retention_days must be between {lo} and {hi}")
        out["log_retention_days"] = int(log_retention_days)
    if log_max_total_mb is not None:
        if isinstance(log_max_total_mb, bool) or log_max_total_mb not in MAX_TOTAL_MB_PRESETS:
            raise LogSettingsError(f"log_max_total_mb must be one of {list(MAX_TOTAL_MB_PRESETS)}")
        out["log_max_total_mb"] = int(log_max_total_mb)
    if log_level is not None:
        level = str(log_level).strip().upper()
        if level not in LEVEL_CHOICES:
            raise LogSettingsError(f"log_level must be one of {list(LEVEL_CHOICES)}")
        out["log_level"] = level
    return out


def _kv_int(db: Any, key: str, default: int) -> int:
    raw = db.get_kv(key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def load_log_settings(db: Any) -> LogSettings:
    """Saved values from ``kv_store``; the env ``LOG_LEVEL`` (and code defaults) apply where nothing is saved."""
    level = db.get_kv(KV_LEVEL)
    return LogSettings(
        log_rotation_hours=_kv_int(db, KV_ROTATION_HOURS, DEFAULT_ROTATION_HOURS),
        log_retention_days=_kv_int(db, KV_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
        log_max_total_mb=_kv_int(db, KV_MAX_TOTAL_MB, DEFAULT_MAX_TOTAL_MB),
        log_level=(level.strip().upper() if level else default_log_level()),
    )


def save_log_settings(db: Any, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        db.set_kv(key, str(value))


def find_file_handler(root_logger: Optional[logging.Logger] = None) -> Optional[TimedLogFileHandler]:
    for handler in (root_logger or logging.getLogger()).handlers:
        if isinstance(handler, TimedLogFileHandler):
            return handler
    return None


def apply_log_level(level_name: str, root_logger: Optional[logging.Logger] = None) -> int:
    """Sets the root logger and every level-following handler (stdout, ring buffer, file) at once."""
    root = root_logger or logging.getLogger()
    level = numeric_level(level_name)
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "follows_log_level", False):
            handler.setLevel(level)
    return level


def apply_log_settings(settings: LogSettings, root_logger: Optional[logging.Logger] = None) -> None:
    """Applies level and rotation limits to the live logging configuration (no restart needed)."""
    root = root_logger or logging.getLogger()
    apply_log_level(settings.log_level, root)
    handler = find_file_handler(root)
    if handler is not None:
        handler.configure(
            rotation_hours=settings.log_rotation_hours,
            retention_days=settings.log_retention_days,
            max_total_mb=settings.log_max_total_mb,
        )


def apply_saved_log_settings(db: Any, root_logger: Optional[logging.Logger] = None) -> Optional[LogSettings]:
    """Boot hook: reads the saved settings and applies them. A database error leaves the env defaults in force."""
    try:
        settings = load_log_settings(db)
    except sqlite3.Error as exc:
        logger.warning("Could not read the saved log settings: %s", safe_exc(exc))
        return None
    apply_log_settings(settings, root_logger)
    return settings


# -- file listing -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogFileInfo:
    name: str
    size_bytes: int
    start_at: Optional[str]
    end_at: Optional[str]
    path: Path


def is_listable_log_name(name: str) -> bool:
    return bool(_LISTABLE_RE.match(name))


def list_log_files(log_dir: Path) -> list[LogFileInfo]:
    """Current, rotated and legacy log files in ``log_dir``, newest first. ``end_at`` is None for the current file."""
    files: list[tuple[float, LogFileInfo]] = []
    try:
        entries = list(os.scandir(log_dir))
    except OSError as exc:
        logger.warning("Could not list the log directory: %s", safe_exc(exc))
        return []
    for entry in entries:
        if not is_listable_log_name(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        path = Path(entry.path)
        rotated = _ROTATED_RE.match(entry.name)
        if entry.name == CURRENT_LOG_NAME:
            start = _first_line_time(path) or st.st_mtime
            info = LogFileInfo(entry.name, st.st_size, _iso(start), None, path)
            sort_key = float("inf")
        elif rotated:
            start_dt = datetime.strptime(rotated.group(1), _STAMP_FORMAT)
            info = LogFileInfo(entry.name, st.st_size, start_dt.strftime("%Y-%m-%dT%H:%M:%S"), _iso(st.st_mtime), path)
            sort_key = st.st_mtime
        else:  # legacy size-rotated backup
            info = LogFileInfo(entry.name, st.st_size, None, _iso(st.st_mtime), path)
            sort_key = st.st_mtime
        files.append((sort_key, info))
    files.sort(key=lambda item: item[0], reverse=True)
    return [info for _, info in files]


def resolve_log_file(log_dir: Path, name: str) -> Optional[Path]:
    """The real path of a listed log file, or None. The name must match the strict pattern, appear in the directory
    listing and resolve to a regular file directly inside the (resolved) log directory."""
    if not is_listable_log_name(name):
        return None
    try:
        base = log_dir.resolve(strict=True)
    except OSError:
        return None
    for info in list_log_files(log_dir):
        if info.name != name:
            continue
        try:
            resolved = info.path.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != base or not resolved.is_file():
            return None
        return resolved
    return None
