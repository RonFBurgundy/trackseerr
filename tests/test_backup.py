"""Tests for database backup and restore: snapshotting, manifest packaging, retention pruning, validation, staged boot restore, routes, and background task integration."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import __version__
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backup import (
    BACKUP_FILENAME_RE,
    DB_MEMBER_NAME,
    MANIFEST_MEMBER_NAME,
    PENDING_RESTORE_DB_NAME,
    PENDING_RESTORE_MARKER_NAME,
    BackupValidationError,
    InvalidBackupNameError,
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
    validate_backup,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import SCHEMA_VERSION, Database
from plex_playlist_sync.task_manager import TASKS


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

    # 5. Corrupt database (integrity check failure)
    corrupt_zip = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt_zip, "w") as zf:
        zf.writestr(MANIFEST_MEMBER_NAME, json.dumps({"schema_version": SCHEMA_VERSION}))
        zf.writestr(DB_MEMBER_NAME, b"not a valid sqlite database header at all")
    with pytest.raises(BackupValidationError, match="integrity check failed"):
        validate_backup(corrupt_zip)


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
    with patch("plex_playlist_sync.backup._create_pre_restore_backup", side_effect=OSError("Disk write failed")):
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
    from plex_playlist_sync.api.dependencies import require_core_tier
    from plex_playlist_sync.api.routes.backups import router as backup_router

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
