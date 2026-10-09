"""Database backup and restore: online snapshots, manifest packaging, retention pruning, validation, and staged boot restore."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from trackseerr import __version__
from trackseerr.redaction import safe_exc
from trackseerr.storage import SCHEMA_VERSION, Database
from trackseerr.task_manager import (
    TRIGGER_SCHEDULED,
    TaskCancelled,
    record_task_run,
    wait_for_next_cycle,
)

logger = logging.getLogger(__name__)

BACKUP_FILENAME_RE = re.compile(
    r"^trackseerr_backup_(\d{8}_\d{6})_(manual|scheduled|pre_restore)\.zip$"
)

DB_MEMBER_NAME = "sync_db.sqlite"
MANIFEST_MEMBER_NAME = "manifest.json"
PENDING_RESTORE_DB_NAME = ".pending_restore.sqlite"
PENDING_RESTORE_MARKER_NAME = ".pending_restore.marker"

KV_BACKUP_RETENTION = "backup_retention"
DEFAULT_BACKUP_RETENTION = 7

MAX_BACKUP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_BACKUP_RATIO = 200
BACKUP_RATIO_MIN_COMPRESSED = 1024 * 1024  # 1 MiB

DIR_MODE = 0o700
FILE_MODE = 0o600

_restart_killer: Optional[Callable[[], None]] = None
_backup_cancel_event = threading.Event()


class BackupError(Exception):
    """Base exception for backup and restore errors."""


class BackupValidationError(BackupError):
    """Raised when a backup archive fails security, format, schema, or integrity validation."""


class InvalidBackupNameError(BackupError):
    """Raised when a backup filename is invalid or contains traversal sequences."""


def cancel_backup() -> None:
    """Sets the cancellation event to interrupt an ongoing backup operation."""
    _backup_cancel_event.set()


def set_restart_killer(killer: Optional[Callable[[], None]]) -> None:
    """Injects a custom process termination hook for restore restart testing."""
    global _restart_killer
    _restart_killer = killer


def trigger_restart(delay_seconds: float = 0.5, killer: Optional[Callable[[], None]] = None) -> None:
    """Triggers an asynchronous delayed restart by sending SIGTERM to the process."""
    target_killer = killer or _restart_killer

    def _do_restart() -> None:
        time.sleep(delay_seconds)
        if target_killer is not None:
            target_killer()
        else:
            logger.info("Sending SIGTERM to process %d for restart after restore", os.getpid())
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError as exc:
                logger.error("Failed to send SIGTERM: %s", safe_exc(exc))

    thread = threading.Thread(target=_do_restart, daemon=True, name="BackupRestartThread")
    thread.start()


def ensure_backup_dir(backup_dir: Path) -> Path:
    """Ensures the directory exists with 0700 permissions."""
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(backup_dir, DIR_MODE)
    except OSError as exc:
        logger.debug("Could not chmod backup directory %s: %s", backup_dir, safe_exc(exc))
    return backup_dir


def get_backup_dir(base_dir: Optional[Union[str, Path]] = None) -> Path:
    """Resolves and ensures the backup directory (<config_dir>/backups) with 0700 permissions."""
    if base_dir is not None:
        cfg_dir = Path(base_dir)
    elif os.getenv("CONFIG_DIR"):
        cfg_dir = Path(os.getenv("CONFIG_DIR", ""))
    elif os.path.isdir("/config"):
        cfg_dir = Path("/config")
    else:
        cfg_dir = Path(os.getenv("DATA_DIR", "/data"))

    backup_dir = cfg_dir / "backups"
    return ensure_backup_dir(backup_dir)


def get_backup_retention(db: Database) -> int:
    """Retrieves the configured retention count for scheduled backups (default: 7)."""
    try:
        raw = db.get_kv(KV_BACKUP_RETENTION)
        if raw is not None:
            val = int(raw)
            if val > 0:
                return val
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logger.warning("Could not read backup retention setting: %s", safe_exc(exc))
    return DEFAULT_BACKUP_RETENTION


def set_backup_retention(db: Database, retention: int) -> None:
    """Persists the retention count for scheduled backups in settings storage."""
    if retention <= 0:
        raise ValueError("Backup retention count must be positive")
    db.set_kv(KV_BACKUP_RETENTION, str(retention))


def resolve_backup_path(name: str, backup_dir: Optional[Path] = None) -> Path:
    """Validates that ``name`` is a plain filename matching the backup pattern and resolves it."""
    if not isinstance(name, str):
        raise InvalidBackupNameError("Backup name must be a string")
    if "/" in name or "\\" in name or ".." in name:
        raise InvalidBackupNameError(f"Path traversal detected in backup name: {name}")
    if name != Path(name).name:
        raise InvalidBackupNameError(f"Invalid backup name: {name}")
    if not BACKUP_FILENAME_RE.match(name):
        raise InvalidBackupNameError(f"Backup filename does not match expected pattern: {name}")

    b_dir = backup_dir or get_backup_dir()
    resolved = (b_dir / name).resolve()
    if not resolved.is_relative_to(b_dir.resolve()):
        raise InvalidBackupNameError(f"Path traversal detected in backup name: {name}")
    return resolved


def delete_backup(name: str, backup_dir: Optional[Path] = None) -> None:
    """Deletes a backup archive by name. Rejects non-plain filenames and traversal attempts."""
    path = resolve_backup_path(name, backup_dir=backup_dir)
    if not path.is_file():
        raise FileNotFoundError(f"Backup file not found: {name}")
    path.unlink()
    logger.info("Deleted backup %s", name)


def create_backup(
    db: Database,
    kind: str = "manual",
    backup_dir: Optional[Path] = None,
) -> Path:
    """Snapshots the live SQLite database using the sqlite3 online backup API into a zip archive."""
    if kind not in ("manual", "scheduled", "pre_restore"):
        raise ValueError(f"Invalid backup kind: {kind}")

    if _backup_cancel_event.is_set():
        _backup_cancel_event.clear()
        raise TaskCancelled("Backup cancelled by admin")

    if backup_dir is not None:
        target_dir = ensure_backup_dir(Path(backup_dir))
    elif isinstance(db.db_path, Path):
        target_dir = get_backup_dir(base_dir=db.db_path.parent)
    else:
        target_dir = get_backup_dir()

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    filename = f"trackseerr_backup_{timestamp_str}_{kind}.zip"
    target_path = target_dir / filename

    # Avoid collision in rapid execution loops
    counter = 1
    while target_path.exists():
        time.sleep(1.0)
        now = datetime.now(timezone.utc)
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        filename = f"trackseerr_backup_{timestamp_str}_{kind}.zip"
        target_path = target_dir / filename
        counter += 1
        if counter > 5:
            break

    manifest_data = {
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "created_at": now.isoformat(),
        "kind": kind,
    }

    temp_db_fd, temp_db_path_str = tempfile.mkstemp(suffix=".sqlite", dir=str(target_dir))
    os.close(temp_db_fd)
    temp_db_path = Path(temp_db_path_str)

    temp_zip_fd, temp_zip_path_str = tempfile.mkstemp(suffix=".tmp.zip", dir=str(target_dir))
    os.close(temp_zip_fd)
    temp_zip_path = Path(temp_zip_path_str)

    try:
        # 1. Snapshot database using SQLite online backup API (WAL-safe)
        dst_conn = sqlite3.connect(str(temp_db_path))
        try:
            with db._lock:
                src_conn = db._ensure_connection()
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()

        if _backup_cancel_event.is_set():
            _backup_cancel_event.clear()
            raise TaskCancelled("Backup cancelled by admin")

        # 2. Package database and manifest into temporary zip archive
        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(temp_db_path, arcname=DB_MEMBER_NAME)
            zf.writestr(MANIFEST_MEMBER_NAME, json.dumps(manifest_data, indent=2))

        try:
            os.chmod(temp_zip_path, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod temp backup %s: %s", temp_zip_path, safe_exc(exc))

        # 3. Atomically rename to final target path
        os.replace(temp_zip_path, target_path)
        try:
            os.chmod(target_path, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod backup file %s: %s", target_path, safe_exc(exc))

        logger.info("Successfully created %s backup: %s (%d bytes)", kind, target_path.name, target_path.stat().st_size)
        return target_path
    finally:
        if temp_db_path.exists():
            try:
                temp_db_path.unlink()
            except OSError as exc:
                logger.debug("Failed to remove temp db %s: %s", temp_db_path, safe_exc(exc))
        if temp_zip_path.exists():
            try:
                temp_zip_path.unlink()
            except OSError as exc:
                logger.debug("Failed to remove temp zip %s: %s", temp_zip_path, safe_exc(exc))


def list_backups(backup_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Lists all database backups in the backup directory, parsing manifests and tolerating unreadable files."""
    b_dir = backup_dir or get_backup_dir()
    if not b_dir.exists():
        return []

    items: list[dict[str, Any]] = []
    for entry in b_dir.iterdir():
        if not entry.is_file() or not entry.name.endswith(".zip"):
            continue
        # Check against base naming or accept any zip in backup directory
        is_backup_name = bool(BACKUP_FILENAME_RE.match(entry.name))
        try:
            file_size = entry.stat().st_size
        except OSError as exc:
            logger.warning("Could not stat backup file %s: %s", entry.name, safe_exc(exc))
            continue

        created_at: Optional[str] = None
        kind: Optional[str] = None
        app_version: Optional[str] = None
        schema_version: Optional[int] = None
        error: Optional[str] = None

        match = BACKUP_FILENAME_RE.match(entry.name)
        fallback_kind = match.group(2) if match else None

        try:
            if not zipfile.is_zipfile(entry):
                error = "Not a valid zip archive"
            else:
                with zipfile.ZipFile(entry, "r") as zf:
                    namelist = zf.namelist()
                    if MANIFEST_MEMBER_NAME not in namelist:
                        error = "Missing manifest.json in archive"
                    else:
                        manifest_bytes = zf.read(MANIFEST_MEMBER_NAME)
                        manifest_data = json.loads(manifest_bytes.decode("utf-8"))
                        if isinstance(manifest_data, dict):
                            app_version = str(manifest_data.get("app_version")) if manifest_data.get("app_version") is not None else None
                            schema_ver_val = manifest_data.get("schema_version")
                            schema_version = int(schema_ver_val) if isinstance(schema_ver_val, (int, float)) else None
                            created_at = str(manifest_data.get("created_at")) if manifest_data.get("created_at") is not None else None
                            kind = str(manifest_data.get("kind")) if manifest_data.get("kind") is not None else fallback_kind
                        else:
                            error = "Invalid manifest.json structure"
        except Exception as exc:
            error = f"Unreadable archive: {safe_exc(exc)}"

        if kind is None:
            kind = fallback_kind or "manual"
        if created_at is None:
            try:
                mtime = entry.stat().st_mtime
                created_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                created_at = datetime.now(timezone.utc).isoformat()

        items.append({
            "name": entry.name,
            "size": file_size,
            "created_at": created_at,
            "kind": kind,
            "app_version": app_version,
            "schema_version": schema_version,
            "error": error,
        })

    # Sort descending by created_at / name
    items.sort(key=lambda x: str(x.get("created_at") or x["name"]), reverse=True)
    return items


def prune_scheduled(
    retention: int = DEFAULT_BACKUP_RETENTION,
    backup_dir: Optional[Path] = None,
) -> list[str]:
    """Prunes oldest scheduled backups beyond retention limit. Never prunes manual or pre_restore backups."""
    if retention <= 0:
        retention = DEFAULT_BACKUP_RETENTION

    b_dir = backup_dir or get_backup_dir()
    all_backups = list_backups(backup_dir=b_dir)

    # Filter only valid scheduled backups
    scheduled = [
        b for b in all_backups
        if b.get("kind") == "scheduled" and b.get("error") is None
    ]

    # Sort oldest first
    scheduled.sort(key=lambda x: str(x.get("created_at") or x["name"]))

    deleted_names: list[str] = []
    if len(scheduled) > retention:
        to_prune = scheduled[: len(scheduled) - retention]
        for item in to_prune:
            path = b_dir / item["name"]
            try:
                if path.is_file():
                    path.unlink()
                    deleted_names.append(item["name"])
                    logger.info("Pruned old scheduled backup: %s", item["name"])
            except OSError as exc:
                logger.warning("Failed to prune backup %s: %s", item["name"], safe_exc(exc))

    return deleted_names


def validate_backup(path: Union[str, Path]) -> dict[str, Any]:  # noqa: C901
    """Validates an archive for zip structure, member names, security bounds, schema version, and database integrity."""
    archive_path = Path(path).resolve()
    if not archive_path.is_file():
        raise BackupValidationError(f"Backup file not found: {archive_path}")

    if not zipfile.is_zipfile(archive_path):
        raise BackupValidationError("Backup file is not a valid zip archive")

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.namelist()
            expected_members = {DB_MEMBER_NAME, MANIFEST_MEMBER_NAME}
            if set(members) != expected_members:
                raise BackupValidationError(
                    f"Archive member names must be exactly {expected_members}, found: {set(members)}"
                )

            total_uncompressed = 0
            for info in zf.infolist():
                # Symlink check
                if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                    raise BackupValidationError(f"Archive contains a forbidden symlink member: {info.filename}")

                # Path traversal check
                norm_name = info.filename.replace("\\", "/")
                if "/" in norm_name or norm_name in ("..", "."):
                    raise BackupValidationError(f"Path traversal detected in archive member: {info.filename}")

                # Zip bomb limit checks
                total_uncompressed += info.file_size
                if (
                    info.compress_size > BACKUP_RATIO_MIN_COMPRESSED
                    and info.file_size > info.compress_size * MAX_BACKUP_RATIO
                ):
                    raise BackupValidationError(
                        f"Archive member {info.filename} compression ratio exceeds {MAX_BACKUP_RATIO}:1 limit"
                    )

            if total_uncompressed > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise BackupValidationError(
                    f"Total uncompressed size {total_uncompressed} bytes exceeds limit of {MAX_BACKUP_UNCOMPRESSED_BYTES} bytes"
                )

            # Read and validate manifest
            try:
                manifest_bytes = zf.read(MANIFEST_MEMBER_NAME)
                manifest_data = json.loads(manifest_bytes.decode("utf-8"))
            except Exception as exc:
                raise BackupValidationError(f"Could not parse manifest.json: {safe_exc(exc)}")

            if not isinstance(manifest_data, dict):
                raise BackupValidationError("Manifest content is not a JSON object")

            schema_version = manifest_data.get("schema_version")
            if not isinstance(schema_version, int):
                raise BackupValidationError("Manifest missing valid integer schema_version")

            if schema_version > SCHEMA_VERSION:
                raise BackupValidationError(
                    f"Backup schema version ({schema_version}) is newer than current application schema version ({SCHEMA_VERSION})"
                )

            if schema_version < SCHEMA_VERSION:
                raise BackupValidationError(
                    f"Backup schema version ({schema_version}) predates the v{SCHEMA_VERSION} baseline and cannot be restored by this build"
                )

            # Extract SQLite database to temporary location and test PRAGMA integrity_check == ok
            with tempfile.TemporaryDirectory() as temp_dir:
                extracted_db = Path(temp_dir) / DB_MEMBER_NAME
                with zf.open(DB_MEMBER_NAME) as src_fh, open(extracted_db, "wb") as dst_fh:
                    shutil.copyfileobj(src_fh, dst_fh)

                try:
                    conn = sqlite3.connect(str(extracted_db))
                    try:
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA integrity_check")
                        result = cursor.fetchall()
                        if not result or result[0][0] != "ok":
                            raise BackupValidationError(f"Database integrity check failed: {result}")
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    raise BackupValidationError(f"Database integrity check failed: {safe_exc(exc)}")

            return manifest_data
    except zipfile.BadZipFile as exc:
        raise BackupValidationError(f"Corrupt zip archive: {safe_exc(exc)}")


def stage_restore(
    name_or_uploaded_path: Union[str, Path],
    backup_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Validates the specified backup and stages it for application at the next startup."""
    b_dir = ensure_backup_dir(Path(backup_dir)) if backup_dir is not None else get_backup_dir()

    candidate_path = Path(name_or_uploaded_path)
    if candidate_path.is_file():
        archive_path = candidate_path.resolve()
    else:
        archive_path = resolve_backup_path(str(name_or_uploaded_path), backup_dir=b_dir)

    manifest = validate_backup(archive_path)

    pending_db = b_dir / PENDING_RESTORE_DB_NAME
    pending_marker = b_dir / PENDING_RESTORE_MARKER_NAME

    temp_fd, temp_path_str = tempfile.mkstemp(suffix=".tmp.sqlite", dir=str(b_dir))
    os.close(temp_fd)
    temp_path = Path(temp_path_str)

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            with zf.open(DB_MEMBER_NAME) as src_fh, open(temp_path, "wb") as dst_fh:
                shutil.copyfileobj(src_fh, dst_fh)

        try:
            os.chmod(temp_path, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod temp pending db %s: %s", temp_path, safe_exc(exc))

        os.replace(temp_path, pending_db)
        try:
            os.chmod(pending_db, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod pending db %s: %s", pending_db, safe_exc(exc))

        marker_data = {
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "source": archive_path.name,
            "manifest": manifest,
        }

        marker_temp_fd, marker_temp_str = tempfile.mkstemp(suffix=".tmp.marker", dir=str(b_dir))
        os.close(marker_temp_fd)
        marker_temp_path = Path(marker_temp_str)
        try:
            marker_temp_path.write_text(json.dumps(marker_data, indent=2), encoding="utf-8")
            try:
                os.chmod(marker_temp_path, FILE_MODE)
            except OSError as exc:
                logger.debug("Could not chmod marker temp %s: %s", marker_temp_path, safe_exc(exc))
            os.replace(marker_temp_path, pending_marker)
            try:
                os.chmod(pending_marker, FILE_MODE)
            except OSError as exc:
                logger.debug("Could not chmod pending marker %s: %s", pending_marker, safe_exc(exc))
        finally:
            if marker_temp_path.exists():
                try:
                    marker_temp_path.unlink()
                except OSError as exc:
                    logger.debug("Could not unlink marker temp: %s", safe_exc(exc))

        logger.info("Successfully staged restore from %s", archive_path.name)
        return {
            "success": True,
            "source": archive_path.name,
            "manifest": manifest,
        }
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as exc:
                logger.debug("Could not unlink temp pending db: %s", safe_exc(exc))


def _create_pre_restore_backup(target_db: Path, backup_dir: Path) -> Path:
    """Creates a snapshot zip archive of the live target_db before it is replaced."""
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    pre_restore_name = f"trackseerr_backup_{timestamp_str}_pre_restore.zip"
    pre_restore_path = backup_dir / pre_restore_name

    # Read current schema version from DB if possible
    schema_ver = SCHEMA_VERSION
    try:
        conn = sqlite3.connect(str(target_db))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cur = conn.cursor()
            cur.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
            row = cur.fetchone()
            if row and isinstance(row[0], int):
                schema_ver = row[0]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not checkpoint or read schema version from %s: %s", target_db, safe_exc(exc))

    manifest_data = {
        "app_version": __version__,
        "schema_version": schema_ver,
        "created_at": now.isoformat(),
        "kind": "pre_restore",
    }

    temp_db_fd, temp_db_path_str = tempfile.mkstemp(suffix=".sqlite", dir=str(backup_dir))
    os.close(temp_db_fd)
    temp_db_path = Path(temp_db_path_str)

    temp_zip_fd, temp_zip_path_str = tempfile.mkstemp(suffix=".tmp.zip", dir=str(backup_dir))
    os.close(temp_zip_fd)
    temp_zip_path = Path(temp_zip_path_str)

    try:
        src_conn = sqlite3.connect(str(target_db))
        dst_conn = sqlite3.connect(str(temp_db_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(temp_db_path, arcname=DB_MEMBER_NAME)
            zf.writestr(MANIFEST_MEMBER_NAME, json.dumps(manifest_data, indent=2))

        try:
            os.chmod(temp_zip_path, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod temp pre-restore zip %s: %s", temp_zip_path, safe_exc(exc))

        os.replace(temp_zip_path, pre_restore_path)
        try:
            os.chmod(pre_restore_path, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod pre-restore zip %s: %s", pre_restore_path, safe_exc(exc))

        logger.info("Created pre-restore backup: %s", pre_restore_path.name)
        return pre_restore_path
    finally:
        if temp_db_path.exists():
            try:
                temp_db_path.unlink()
            except OSError as exc:
                logger.debug("Could not unlink temp db: %s", safe_exc(exc))
        if temp_zip_path.exists():
            try:
                temp_zip_path.unlink()
            except OSError as exc:
                logger.debug("Could not unlink temp zip: %s", safe_exc(exc))


def apply_pending_restore(
    db_path: Union[str, Path],
    backup_dir: Optional[Path] = None,
) -> bool:
    """Applies a pending restore before any Database connection is constructed at startup."""
    target_db = Path(db_path).resolve()
    b_dir = backup_dir or get_backup_dir(base_dir=target_db.parent)

    pending_db = b_dir / PENDING_RESTORE_DB_NAME
    pending_marker = b_dir / PENDING_RESTORE_MARKER_NAME

    if not pending_marker.exists() or not pending_db.exists():
        return False

    logger.info("Pending restore detected at %s. Beginning restore application...", pending_marker)

    try:
        # 1. If target database exists, capture a pre_restore backup first
        if target_db.exists():
            try:
                _create_pre_restore_backup(target_db, b_dir)
            except Exception as exc:
                logger.critical(
                    "Failed to create pre_restore backup before applying restore: %s. Aborting restore to protect existing database.",
                    safe_exc(exc),
                    exc_info=True,
                )
                return False

        # 2. Clean up any existing -wal or -shm files next to the target DB
        wal_file = target_db.with_name(target_db.name + "-wal")
        shm_file = target_db.with_name(target_db.name + "-shm")
        if wal_file.exists():
            try:
                wal_file.unlink()
            except OSError as exc:
                logger.warning("Could not remove stale WAL file %s: %s", wal_file, safe_exc(exc))
        if shm_file.exists():
            try:
                shm_file.unlink()
            except OSError as exc:
                logger.warning("Could not remove stale SHM file %s: %s", shm_file, safe_exc(exc))

        # 3. Move pending database into place
        if not target_db.parent.exists():
            target_db.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(pending_db), str(target_db))
        try:
            os.chmod(target_db, FILE_MODE)
        except OSError as exc:
            logger.debug("Could not chmod restored database %s: %s", target_db, safe_exc(exc))

        # 4. Remove pending marker
        pending_marker.unlink(missing_ok=True)

        logger.info(
            "Pending restore successfully applied to %s. Migrations will run on next database open.",
            target_db,
        )
        return True
    except Exception as exc:
        logger.critical(
            "Fatal error applying pending database restore to %s: %s",
            target_db,
            safe_exc(exc),
            exc_info=True,
        )
        return False


class BackupWorker:
    """Scheduled backup worker thread running in the background."""

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
        interval_seconds: float = 7 * 86400.0,
        initial_delay: float = 60.0,
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

        self._thread = threading.Thread(target=_loop, daemon=True, name="BackupWorkerThread")
        self._thread.start()
        return True

    def run_once(self, db: Any) -> bool:
        try:
            with record_task_run(db, "backup", TRIGGER_SCHEDULED) as run:
                retention = get_backup_retention(db)
                backup_path = create_backup(db, kind="scheduled")
                pruned = prune_scheduled(retention=retention, backup_dir=backup_path.parent)
                run.message = f"created={backup_path.name}, pruned={len(pruned)}"
            return True
        except Exception as exc:
            logger.error("BackupWorker: scheduled backup failed: %s", safe_exc(exc))
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


backup_worker = BackupWorker()
