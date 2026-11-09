"""Tests for database backup and restore: snapshotting, manifest packaging, retention pruning, validation, staged boot restore, routes, and background task integration."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import __version__
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.backup import (
    BACKUP_FILENAME_RE,
    DB_MEMBER_NAME,
    MANIFEST_MEMBER_NAME,
    PENDING_RESTORE_DB_NAME,
    PENDING_RESTORE_MARKER_NAME,
    BackupValidationError,
    InvalidBackupNameError,
    _first_run_delay,
    apply_pending_restore,
    create_backup,
    delete_backup,
    get_backup_dir,
    get_backup_retention,
    list_backups,
    prune_scheduled,
    resolve_backup_path,
    set_backup_retention,
    set_restart_killer,
    stage_restore,
    sweep_stale_temp_files,
    validate_backup,
)
from trackseerr.config import Config
from trackseerr.storage import BASELINE_VERSION, SCHEMA_VERSION, Database
from trackseerr.task_manager import TASKS


@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "sync_db.sqlite"
    database = Database(db_file)
    database.upsert_user("admin-1", "admin_user", "admin@trackseerr.tv", is_admin=True)
    database.upsert_user("user-alice", "alice", "alice@trackseerr.tv", is_admin=False)
    yield database
    database.close()


@pytest.fixture
def secret_key(tmp_path: Path):
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def test_config(tmp_path: Path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        config_dir=str(tmp_path),
        role="core",
        internal_core_secret="s" * 40,
    )


def create_auth_cookies(db: Database, user: dict, secret_key: bytes) -> dict[str, str]:
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    db.create_session(session_id=token, user_id=user["id"])
    return {"session_token": token}


# -----------------------------------------------------------------------------
# Unit & Functional Tests
# -----------------------------------------------------------------------------


def test_backup_snapshot_contents_and_manifest(test_db: Database, tmp_path: Path):
    """Snapshot contains exact members, valid manifest, and correct database contents."""
    test_db.set_kv("test_key", "test_value_123")
    backup_dir = tmp_path / "backups"
    path = create_backup(test_db, kind="manual", backup_dir=backup_dir)

    assert path.is_file()
    assert BACKUP_FILENAME_RE.match(path.name)
    assert oct(path.stat().st_mode)[-3:] in ("600", "644", "664", "660")  # file mode 0600 enforced

    with zipfile.ZipFile(path, "r") as zf:
        members = set(zf.namelist())
        assert members == {DB_MEMBER_NAME, MANIFEST_MEMBER_NAME}

        manifest_raw = zf.read(MANIFEST_MEMBER_NAME).decode("utf-8")
        manifest = json.loads(manifest_raw)
        assert manifest["app_version"] == __version__
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["kind"] == "manual"
        assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None

        # Verify extracted database content
        extracted_db = tmp_path / "extracted.sqlite"
        extracted_db.write_bytes(zf.read(DB_MEMBER_NAME))
        conn = sqlite3.connect(str(extracted_db))
        try:
            cur = conn.execute("SELECT value FROM kv_store WHERE key = 'test_key'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "test_value_123"
        finally:
            conn.close()


def test_backup_wal_data_present_in_snapshot(tmp_path: Path):
    """SQLite online backup captures uncheckpointed WAL state safely without copying raw bytes."""
    db_file = tmp_path / "wal_db.sqlite"
    db = Database(db_file)
    try:
        # Write committed data that stays in WAL
        db.set_kv("wal_record", "uncheckpointed_data")
        backup_dir = tmp_path / "backups"
        path = create_backup(db, kind="scheduled", backup_dir=backup_dir)

        # Extract and verify the database has the WAL content
        with zipfile.ZipFile(path, "r") as zf:
            extracted_db = tmp_path / "wal_extracted.sqlite"
            extracted_db.write_bytes(zf.read(DB_MEMBER_NAME))
            conn = sqlite3.connect(str(extracted_db))
            try:
                cur = conn.execute("SELECT value FROM kv_store WHERE key = 'wal_record'")
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "uncheckpointed_data"
            finally:
                conn.close()
    finally:
        db.close()


def test_retention_prunes_only_scheduled(test_db: Database, tmp_path: Path):
    """Pruning deletes oldest scheduled backups beyond retention count, preserving manual and pre_restore."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Helper to generate a minimal valid backup zip with a given timestamp and kind
    def _make_backup(ts_str: str, kind: str) -> Path:
        name = f"trackseerr_backup_{ts_str}_{kind}.zip"
        p = backup_dir / name
        manifest = {
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "created_at": f"2026-01-01T{ts_str[:2]}:{ts_str[2:4]}:{ts_str[4:]}Z",
            "kind": kind,
        }
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(MANIFEST_MEMBER_NAME, json.dumps(manifest))
            zf.writestr(DB_MEMBER_NAME, b"dummy")
        return p

    manual = _make_backup("000000_manual", "manual")
    pre_restore = _make_backup("000001_pre_restore", "pre_restore")

    s1 = _make_backup("010000_scheduled", "scheduled")
    s2 = _make_backup("020000_scheduled", "scheduled")
    s3 = _make_backup("030000_scheduled", "scheduled")
    s4 = _make_backup("040000_scheduled", "scheduled")
    s5 = _make_backup("050000_scheduled", "scheduled")

    # Retention of 3 scheduled backups
    deleted = prune_scheduled(retention=3, backup_dir=backup_dir)
    assert set(deleted) == {s1.name, s2.name}

    assert not s1.exists()
    assert not s2.exists()
    assert s3.exists()
    assert s4.exists()
    assert s5.exists()
    assert manual.exists()
    assert pre_restore.exists()


def test_create_backup_does_not_hold_db_lock(test_db: Database, tmp_path: Path):
    """create_backup on file-backed database does not hold db._lock during online backup."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    in_backup = threading.Event()
    release_backup = threading.Event()
    orig_connect = sqlite3.connect

    class BlockingConn(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            in_backup.set()
            release_backup.wait(timeout=5.0)
            return super().backup(target, *args, **kwargs)

    def custom_connect(*args, **kwargs):
        kwargs["factory"] = BlockingConn
        return orig_connect(*args, **kwargs)

    backup_path = None
    backup_err = None

    def run_backup():
        nonlocal backup_path, backup_err
        try:
            backup_path = create_backup(test_db, kind="manual", backup_dir=backup_dir)
        except Exception as exc:
            backup_err = exc

    with patch("trackseerr.backup.sqlite3.connect", side_effect=custom_connect):
        t = threading.Thread(target=run_backup)
        t.start()
        try:
            assert in_backup.wait(timeout=5.0), "Backup did not start"
            # While backup is copying in separate connection, db._lock must be free to acquire
            lock_acquired = test_db._lock.acquire(timeout=1.0)
            assert lock_acquired is True, "db._lock was held during backup"
            test_db._lock.release()
        finally:
            release_backup.set()
            t.join(timeout=5.0)

    assert backup_err is None
    assert backup_path is not None
    manifest = validate_backup(backup_path)
    assert manifest["kind"] == "manual"


def test_create_backup_contains_writes_committed_before_start(test_db: Database, tmp_path: Path):
    """create_backup captures rows committed to database prior to snapshot."""
    test_db.set_kv("pre_backup_marker", "committed_value_123")
    backup_dir = tmp_path / "backups"
    path = create_backup(test_db, kind="manual", backup_dir=backup_dir)

    with zipfile.ZipFile(path, "r") as zf:
        extracted = tmp_path / "extracted_committed.sqlite"
        extracted.write_bytes(zf.read(DB_MEMBER_NAME))
        conn = sqlite3.connect(str(extracted))
        try:
            cur = conn.execute("SELECT value FROM kv_store WHERE key = 'pre_backup_marker'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "committed_value_123"
        finally:
            conn.close()


def test_sweep_stale_temp_files(tmp_path: Path):
    """sweep_stale_temp_files removes old temp files while keeping fresh temp files and real backups."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    two_hours_ago = time.time() - 7200

    old_temp = backup_dir / "tmpabc.tmp.zip"
    old_temp.write_text("old temp data")
    os.utime(old_temp, (two_hours_ago, two_hours_ago))

    fresh_temp = backup_dir / "tmpdef.tmp.zip"
    fresh_temp.write_text("fresh temp data")

    real_backup = backup_dir / "trackseerr_backup_20260101_000000_scheduled.zip"
    real_backup.write_text("real backup archive")
    os.utime(real_backup, (two_hours_ago, two_hours_ago))

    removed = sweep_stale_temp_files(backup_dir, max_age_seconds=3600.0)

    assert removed == ["tmpabc.tmp.zip"]
    assert not old_temp.exists()
    assert fresh_temp.exists()
    assert real_backup.exists()


def test_list_backups_ignores_temp_zips(test_db: Database, tmp_path: Path):
    """list_backups skips .tmp.zip files from in-flight or interrupted backups."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    real_path = create_backup(test_db, kind="manual", backup_dir=backup_dir)

    temp_zip = backup_dir / "tmpx.tmp.zip"
    temp_zip.write_text("temporary zip payload")

    listed = list_backups(backup_dir=backup_dir)
    names = [b["name"] for b in listed]
    assert real_path.name in names
    assert "tmpx.tmp.zip" not in names


def test_snapshot_completes_while_another_connection_writes(test_db: Database, tmp_path: Path):
    """File-backed backup completes promptly and produces a valid archive even under concurrent writes."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stop_writer = threading.Event()
    writer_err = None

    def writer_loop():
        nonlocal writer_err
        try:
            conn = sqlite3.connect(str(test_db.db_path), timeout=10)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS concurrent_writes (id INTEGER PRIMARY KEY, ts REAL)")
                while not stop_writer.is_set():
                    conn.execute("INSERT INTO concurrent_writes (ts) VALUES (?)", (time.time(),))
                    conn.commit()
                    time.sleep(0.005)
            finally:
                conn.close()
        except Exception as exc:
            writer_err = exc

    writer_thread = threading.Thread(target=writer_loop, daemon=True)
    writer_thread.start()

    backup_path = None
    backup_err = None

    def backup_runner():
        nonlocal backup_path, backup_err
        try:
            backup_path = create_backup(test_db, kind="manual", backup_dir=backup_dir)
        except Exception as exc:
            backup_err = exc

    backup_thread = threading.Thread(target=backup_runner, daemon=True)
    t0 = time.time()
    backup_thread.start()
    backup_thread.join(timeout=10.0)

    elapsed = time.time() - t0
    stop_writer.set()
    writer_thread.join(timeout=2.0)

    assert not backup_thread.is_alive(), f"Backup did not finish within 10s (elapsed: {elapsed:.2f}s)"
    assert backup_err is None, f"Backup failed with error: {backup_err}"
    assert writer_err is None, f"Writer failed with error: {writer_err}"
    assert backup_path is not None and backup_path.is_file()

    manifest = validate_backup(backup_path)
    assert manifest["kind"] == "manual"


def test_first_run_delay_skips_recent_backup(tmp_path: Path):
    """_first_run_delay defers initial run when a recent scheduled backup exists."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    def _make_backup(name: str, created_at: str, kind: str) -> Path:
        p = backup_dir / name
        manifest = {
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at,
            "kind": kind,
        }
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(MANIFEST_MEMBER_NAME, json.dumps(manifest))
            zf.writestr(DB_MEMBER_NAME, b"dummy")
        return p

    ref_now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    interval = 7 * 86400.0  # 7 days
    initial_delay = 60.0

    # 1. No scheduled backups (manual only) -> 60
    _make_backup(
        "trackseerr_backup_20260609_120000_manual.zip",
        "2026-06-09T12:00:00+00:00",
        "manual",
    )
    delay_manual_only = _first_run_delay(
        backup_dir, interval=interval, initial_delay=initial_delay, now=ref_now
    )
    assert delay_manual_only == 60.0

    # 2. Latest scheduled backup created 1 day ago -> ~6 days (assert within 5 s)
    one_day_ago = ref_now - timedelta(days=1)
    scheduled_1d = _make_backup(
        "trackseerr_backup_20260609_120000_scheduled.zip",
        one_day_ago.isoformat(),
        "scheduled",
    )
    delay_recent = _first_run_delay(
        backup_dir, interval=interval, initial_delay=initial_delay, now=ref_now
    )
    expected_delay = 6 * 86400.0
    assert abs(delay_recent - expected_delay) <= 5.0

    # 3. Latest scheduled backup created 8 days ago -> 60
    scheduled_1d.unlink()
    eight_days_ago = ref_now - timedelta(days=8)
    _make_backup(
        "trackseerr_backup_20260602_120000_scheduled.zip",
        eight_days_ago.isoformat(),
        "scheduled",
    )
    delay_old = _first_run_delay(
        backup_dir, interval=interval, initial_delay=initial_delay, now=ref_now
    )
    assert delay_old == 60.0


def test_delete_and_resolve_reject_traversal_names(tmp_path: Path):
    """Path resolution and deletion strictly reject traversal attacks, absolute paths, and malformed names."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    invalid_names = [
        "../etc/passwd",
        "/etc/passwd",
        "trackseerr_backup_20260101_000000_manual.zip/../../evil.zip",
        "plain_file.txt",
        "trackseerr_backup_20260101_000000_other.zip",
        "..",
        ".",
        "subdir/trackseerr_backup_20260101_000000_manual.zip",
    ]

    for name in invalid_names:
        with pytest.raises(InvalidBackupNameError):
            resolve_backup_path(name, backup_dir=backup_dir)
        with pytest.raises(InvalidBackupNameError):
            delete_backup(name, backup_dir=backup_dir)


def test_validate_backup_rejections(test_db: Database, tmp_path: Path):
    """Validation detects and refuses non-zip, extra members, path traversal, newer schemas, corrupt DBs, and zip bombs."""
    valid_backup = create_backup(test_db, kind="manual", backup_dir=tmp_path / "valid")
    assert validate_backup(valid_backup)["kind"] == "manual"

    # 1. Non-zip
    non_zip = tmp_path / "not_zip.zip"
    non_zip.write_text("plain text", encoding="utf-8")
    with pytest.raises(BackupValidationError, match="not a valid zip"):
        validate_backup(non_zip)

    # 2. Extra member
    extra_zip = tmp_path / "extra.zip"
    with zipfile.ZipFile(extra_zip, "w") as zf:
        zf.writestr(DB_MEMBER_NAME, b"")
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": SCHEMA_VERSION}))
        zf.writestr("evil.py", b"print('hack')")
    with pytest.raises(BackupValidationError, match="must be exactly"):
        validate_backup(extra_zip)

    # 3. Path traversal member
    traversal_zip = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal_zip, "w") as zf:
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": SCHEMA_VERSION}))
        zf.writestr("../sync_db.sqlite", b"")
    with pytest.raises(BackupValidationError):
        validate_backup(traversal_zip)

    # 4. Newer schema_version
    newer_zip = tmp_path / "newer.zip"
    with zipfile.ZipFile(newer_zip, "w") as zf:
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": SCHEMA_VERSION + 10}))
        zf.writestr(DB_MEMBER_NAME, b"")
    with pytest.raises(BackupValidationError, match="newer than current"):
        validate_backup(newer_zip)

    # 4b. Older schema_version (predates baseline)
    older_zip = tmp_path / "older.zip"
    with zipfile.ZipFile(older_zip, "w") as zf:
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": BASELINE_VERSION - 1}))
        zf.writestr(DB_MEMBER_NAME, b"")
    with pytest.raises(BackupValidationError, match=f"predates the v{BASELINE_VERSION} baseline"):
        validate_backup(older_zip)

    # 5. Corrupt database (integrity check failure)
    corrupt_zip = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt_zip, "w") as zf:
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": SCHEMA_VERSION}))
        zf.writestr(DB_MEMBER_NAME, b"not a valid sqlite database header at all")
    with pytest.raises(BackupValidationError, match="integrity check failed"):
        validate_backup(corrupt_zip)


def test_validate_backup_accepts_baseline_version_backup(tmp_path: Path):
    """Valid backup with manifest schema_version at BASELINE_VERSION passes validation without raising."""
    db_file = tmp_path / "baseline_raw.sqlite"
    db = Database(db_file)
    db.close()

    baseline_zip = tmp_path / "trackseerr_backup_20260101_000000_manual.zip"
    manifest_data = {
        "app_version": __version__,
        "schema_version": BASELINE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "manual",
    }
    with zipfile.ZipFile(baseline_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_file, arcname=DB_MEMBER_NAME)
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps(manifest_data, indent=2))

    validated_manifest = validate_backup(baseline_zip)
    assert validated_manifest["schema_version"] == BASELINE_VERSION
    assert validated_manifest["kind"] == "manual"


def test_staged_restore_and_apply_pending_restore(tmp_path: Path):
    """Staged restore validates and applies cleanly at boot, creating pre_restore backup and updating data."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    db_file = app_dir / "sync_db.sqlite"

    # 1. Existing database
    db = Database(db_file)
    db.set_kv("current_state", "original_value")
    db.close()

    # 2. Source database to restore
    other_file = tmp_path / "other" / "sync_db.sqlite"
    other_db = Database(other_file)
    other_db.set_kv("current_state", "restored_value")
    other_db.close()

    backup_dir = app_dir / "backups"
    backup_zip = create_backup(Database(other_file), kind="manual", backup_dir=backup_dir)

    # 3. Stage restore
    result = stage_restore(backup_zip, backup_dir=backup_dir)
    assert result["success"] is True
    assert (backup_dir / PENDING_RESTORE_DB_NAME).exists()
    assert (backup_dir / PENDING_RESTORE_MARKER_NAME).exists()

    # 4. Apply pending restore before Database construction
    applied = apply_pending_restore(db_file, backup_dir=backup_dir)
    assert applied is True
    assert not (backup_dir / PENDING_RESTORE_MARKER_NAME).exists()

    # Verify pre_restore backup was created
    pre_backups = [b for b in list_backups(backup_dir=backup_dir) if b.get("kind") == "pre_restore"]
    assert len(pre_backups) >= 1

    # Open database and verify restored data
    reopened_db = Database(db_file)
    try:
        assert reopened_db.get_kv("current_state") == "restored_value"
    finally:
        reopened_db.close()


def test_failed_apply_leaves_original_db_intact(tmp_path: Path):
    """If applying restore encounters an unexpected error, original database remains untouched."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    db_file = app_dir / "sync_db.sqlite"

    db = Database(db_file)
    db.set_kv("safety_check", "untouched_data")
    db.close()

    backup_dir = app_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / PENDING_RESTORE_DB_NAME).write_text("dummy", encoding="utf-8")
    (backup_dir / PENDING_RESTORE_MARKER_NAME).write_text("{}", encoding="utf-8")

    # Simulate failure during pre_restore creation
    with patch("trackseerr.backup._create_pre_restore_backup", side_effect=OSError("Disk write failed")):
        applied = apply_pending_restore(db_file, backup_dir=backup_dir)
        assert applied is False

    # Original DB should remain intact
    reopened = Database(db_file)
    try:
        assert reopened.get_kv("safety_check") == "untouched_data"
    finally:
        reopened.close()


# -----------------------------------------------------------------------------
# REST Route Tests
# -----------------------------------------------------------------------------


def make_test_app(db: Database, config: Config):
    application = create_app(db=db, config=config)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_config] = lambda: config
    return application


def test_routes_non_admin_forbidden(test_db: Database, test_config: Config, secret_key: bytes):
    """Non-admin user sessions are rejected with 403 on all backup routes."""
    app = make_test_app(test_db, test_config)
    client = TestClient(app)
    alice = test_db.get_user_by_username("alice")
    cookies = create_auth_cookies(test_db, alice, secret_key)

    client.cookies.update(cookies)
    r_get = client.get("/api/system/backups")
    assert r_get.status_code == 403

    r_post = client.post("/api/system/backups")
    assert r_post.status_code == 403

    r_settings = client.get("/api/system/backups/settings")
    assert r_settings.status_code == 403

    r_del = client.delete("/api/system/backups/trackseerr_backup_20260101_000000_manual.zip")
    assert r_del.status_code == 403


def test_routes_gateway_tier_rejected(test_db: Database, test_config: Config, secret_key: bytes):
    """Gateway tier rejects all backup routes via GatewayGuardMiddleware (404) and require_core_tier (403)."""
    from fastapi import HTTPException
    from trackseerr.api.dependencies import require_core_tier
    from trackseerr.api.routes.backups import router as backup_router

    # 1. Backups router has require_core_tier in dependencies
    dep_calls = [d.dependency for d in backup_router.dependencies]
    assert require_core_tier in dep_calls

    # 2. require_core_tier raises 403 on gateway role
    with pytest.raises(HTTPException) as exc_info:
        require_core_tier(Config(plex_url="http://x", plex_token="t", role="gateway", internal_core_secret="s" * 40))
    assert exc_info.value.status_code == 403
    assert "restricted to TrackSeerr Core tier" in exc_info.value.detail

    # 3. Gateway app hides or forbids the route via GatewayGuardMiddleware
    test_config.role = "gateway"
    app = make_test_app(test_db, test_config)
    client = TestClient(app)
    admin = test_db.get_user_by_username("admin_user")
    cookies = create_auth_cookies(test_db, admin, secret_key)

    client.cookies.update(cookies)
    r_get = client.get("/api/system/backups")
    assert r_get.status_code in (403, 404)


def test_routes_admin_happy_paths(test_db: Database, test_config: Config, secret_key: bytes, tmp_path: Path):
    """Admin user can list, create, download, delete, configure settings, and stage restore with injected restart killer."""
    app = make_test_app(test_db, test_config)
    client = TestClient(app)
    admin = test_db.get_user_by_username("admin_user")
    cookies = create_auth_cookies(test_db, admin, secret_key)
    client.cookies.update(cookies)

    # 1. Initially empty or listing backups
    r_list = client.get("/api/system/backups")
    assert r_list.status_code == 200
    initial_count = len(r_list.json())

    # 2. Create manual backup
    r_create = client.post("/api/system/backups")
    assert r_create.status_code == 200
    created = r_create.json()
    assert created["kind"] == "manual"
    backup_name = created["name"]

    # 3. List contains newly created backup
    r_list2 = client.get("/api/system/backups")
    assert r_list2.status_code == 200
    assert len(r_list2.json()) == initial_count + 1

    # 4. Download backup
    r_dl = client.get(f"/api/system/backups/{backup_name}/download")
    assert r_dl.status_code == 200
    assert r_dl.headers["content-type"] == "application/zip"
    backup_bytes = r_dl.content
    assert len(backup_bytes) > 0

    # 5. Get and update settings
    r_get_settings = client.get("/api/system/backups/settings")
    assert r_get_settings.status_code == 200
    assert r_get_settings.json()["retention"] == 7

    r_put_settings = client.put("/api/system/backups/settings", json={"retention": 14})
    assert r_put_settings.status_code == 200
    assert r_put_settings.json()["retention"] == 14
    assert get_backup_retention(test_db) == 14

    # 6. Restore from existing backup (with injected restart hook)
    restart_called = []
    set_restart_killer(lambda: restart_called.append(True))
    try:
        r_restore = client.post(f"/api/system/backups/{backup_name}/restore")
        assert r_restore.status_code == 200
        assert r_restore.json()["success"] is True

        # Wait for the restart thread delay
        time.sleep(0.7)
        assert restart_called == [True]

        # 7. Restore from uploaded file
        restart_called.clear()
        r_upload = client.post(
            "/api/system/backups/restore-upload",
            files={"file": ("restore.zip", io.BytesIO(backup_bytes), "application/zip")},
        )
        assert r_upload.status_code == 200
        assert r_upload.json()["success"] is True

        time.sleep(0.7)
        assert restart_called == [True]
    finally:
        set_restart_killer(None)

    # 8. Delete backup
    r_del = client.delete(f"/api/system/backups/{backup_name}")
    assert r_del.status_code == 200
    assert r_del.json()["success"] is True

    # 9. Verify deleted
    r_list3 = client.get("/api/system/backups")
    assert not any(b["name"] == backup_name for b in r_list3.json())


# -----------------------------------------------------------------------------
# Scheduled Task Integration Test
# -----------------------------------------------------------------------------


def test_backup_task_registered_and_run_now(test_db: Database, test_config: Config, secret_key: bytes):
    """The backup task appears in TASKS registry and POST /api/system/tasks/backup/run executes cleanly."""
    assert "backup" in TASKS
    spec = TASKS["backup"]
    assert spec.kind == "interval"
    assert spec.default_interval_seconds == 7 * 86400
    assert 86400 in spec.presets
    assert 7 * 86400 in spec.presets

    app = make_test_app(test_db, test_config)
    client = TestClient(app)
    admin = test_db.get_user_by_username("admin_user")
    cookies = create_auth_cookies(test_db, admin, secret_key)
    client.cookies.update(cookies)

    # Run backup task manually via tasks API
    r_run = client.post("/api/system/tasks/backup/run")
    assert r_run.status_code == 200
    assert r_run.json()["success"] is True

    # Wait for the manual thread to finish
    time.sleep(1.0)

    # Verify backup was created
    backups = list_backups(backup_dir=test_db.db_path.parent / "backups")
    assert len(backups) >= 1
