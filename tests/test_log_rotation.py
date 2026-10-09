"""Time-based log rotation, retention/size pruning, live settings, file listing/download and log redaction."""

import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes.system.logs import log_ring_buffer
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.log_rotation import (
    CURRENT_LOG_NAME,
    TRACE_LEVEL,
    LogSettings,
    TimedLogFileHandler,
    apply_log_settings,
    list_log_files,
    load_log_settings,
    resolve_log_file,
)
from trackseerr.storage import Database

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
T0 = time.mktime((2026, 1, 10, 8, 0, 0, 0, 0, -1))
HOUR = 3600


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_handler(tmp_path: Path, clock: FakeClock, **kwargs) -> TimedLogFileHandler:
    handler = TimedLogFileHandler(tmp_path / CURRENT_LOG_NAME, clock=clock, **kwargs)
    handler.setFormatter(logging.Formatter(FMT, datefmt=DATEFMT))
    return handler


def emit(handler: TimedLogFileHandler, clock: FakeClock, message: str, level: int = logging.INFO) -> None:
    record = logging.makeLogRecord(
        {"msg": message, "levelno": level, "levelname": logging.getLevelName(level), "name": "t", "created": clock()}
    )
    handler.handle(record)


def stamp(epoch: float) -> str:
    return time.strftime("%Y%m%d-%H%M", time.localtime(epoch))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- rotation


def test_rotates_after_interval_and_loses_no_lines(tmp_path):
    clock = FakeClock()
    handler = make_handler(tmp_path, clock, rotation_hours=12)
    emit(handler, clock, "line one")
    clock.now = T0 + 12 * HOUR - 60
    emit(handler, clock, "line two")
    assert not list(tmp_path.glob("trackseerr.2*.txt"))  # not yet due

    clock.now = T0 + 12 * HOUR
    emit(handler, clock, "line three")
    handler.close()

    rotated = tmp_path / f"trackseerr.{stamp(T0)}.txt"
    assert rotated.exists()
    assert "line one" in read(rotated) and "line two" in read(rotated)
    assert "line three" not in read(rotated)
    assert read(tmp_path / CURRENT_LOG_NAME).count("line three") == 1
    total = sum(read(p).count("line ") for p in tmp_path.glob("trackseerr*.txt"))
    assert total == 3


def test_interval_is_configurable_live(tmp_path):
    clock = FakeClock()
    handler = make_handler(tmp_path, clock, rotation_hours=24)
    emit(handler, clock, "a")
    handler.configure(rotation_hours=6, retention_days=7, max_total_mb=100)
    clock.now = T0 + 6 * HOUR
    emit(handler, clock, "b")
    handler.close()
    assert (tmp_path / f"trackseerr.{stamp(T0)}.txt").exists()


def test_restart_continues_the_interval_of_the_existing_file(tmp_path):
    clock = FakeClock()
    first = make_handler(tmp_path, clock, rotation_hours=12)
    emit(first, clock, "before restart")
    first.close()

    clock.now = T0 + 8 * HOUR  # process restarted 8h into the file's life
    second = make_handler(tmp_path, clock, rotation_hours=12)
    emit(second, clock, "after restart")
    assert not list(tmp_path.glob("trackseerr.2*.txt"))  # a restart must not reset the 12h window

    clock.now = T0 + 12 * HOUR
    emit(second, clock, "after boundary")
    second.close()
    rotated = tmp_path / f"trackseerr.{stamp(T0)}.txt"
    assert "before restart" in read(rotated) and "after restart" in read(rotated)
    assert "after boundary" in read(tmp_path / CURRENT_LOG_NAME)


def test_rotated_name_collision_does_not_overwrite(tmp_path):
    clock = FakeClock()
    existing = tmp_path / f"trackseerr.{stamp(T0)}.txt"
    existing.write_text("keep me\n", encoding="utf-8")
    handler = make_handler(tmp_path, clock, rotation_hours=6)
    emit(handler, clock, "x")
    clock.now = T0 + 6 * HOUR
    emit(handler, clock, "y")
    handler.close()
    assert read(existing) == "keep me\n"
    assert len(list(tmp_path.glob("trackseerr.2*.txt"))) == 2


# --------------------------------------------------------------------------- pruning


def _old_file(tmp_path: Path, name: str, age_days: float, clock: FakeClock, size: int = 10, sparse_mb: int = 0) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    if sparse_mb:
        os.truncate(path, sparse_mb * 1024 * 1024)
    mtime = clock() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def test_retention_prunes_old_rotated_files_only(tmp_path):
    clock = FakeClock()
    old = _old_file(tmp_path, "trackseerr.20260101-0000.txt", 9, clock)
    fresh = _old_file(tmp_path, "trackseerr.20260109-0000.txt", 1, clock)
    legacy = _old_file(tmp_path, "trackseerr.log.1", 90, clock)
    handler = make_handler(tmp_path, clock, retention_days=7)
    handler.close()
    assert not old.exists()
    assert fresh.exists()
    assert legacy.exists()  # old-format backups are never touched


def test_total_size_cap_deletes_oldest_first(tmp_path):
    clock = FakeClock()
    oldest = _old_file(tmp_path, "trackseerr.20260105-0000.txt", 4, clock, sparse_mb=30)
    middle = _old_file(tmp_path, "trackseerr.20260106-0000.txt", 3, clock, sparse_mb=30)
    newest = _old_file(tmp_path, "trackseerr.20260107-0000.txt", 2, clock, sparse_mb=30)
    handler = make_handler(tmp_path, clock, max_total_mb=50)
    handler.close()
    assert not oldest.exists() and not middle.exists()
    assert newest.exists()


def test_size_cap_applies_when_limit_lowered_live(tmp_path):
    clock = FakeClock()
    a = _old_file(tmp_path, "trackseerr.20260105-0000.txt", 2, clock, sparse_mb=40)
    b = _old_file(tmp_path, "trackseerr.20260106-0000.txt", 1, clock, sparse_mb=40)
    handler = make_handler(tmp_path, clock, max_total_mb=250)
    assert a.exists() and b.exists()
    handler.configure(rotation_hours=12, retention_days=7, max_total_mb=50)
    handler.close()
    assert not a.exists() and b.exists()


# --------------------------------------------------------------------------- listing / resolve


def test_listing_newest_first_with_current_and_legacy(tmp_path):
    clock = FakeClock()
    _old_file(tmp_path, "trackseerr.20260108-0000.txt", 2, clock)
    _old_file(tmp_path, "trackseerr.20260109-0000.txt", 1, clock)
    _old_file(tmp_path, "trackseerr.log", 5, clock)
    (tmp_path / "unrelated.txt").write_text("nope")
    handler = make_handler(tmp_path, clock)
    emit(handler, clock, "now")
    handler.close()

    files = list_log_files(tmp_path)
    names = [f.name for f in files]
    assert names[0] == CURRENT_LOG_NAME
    assert names.index("trackseerr.20260109-0000.txt") < names.index("trackseerr.20260108-0000.txt")
    assert "trackseerr.log" in names and "unrelated.txt" not in names
    current = files[0]
    assert current.end_at is None and current.start_at is not None and current.size_bytes > 0
    rotated = next(f for f in files if f.name == "trackseerr.20260109-0000.txt")
    assert rotated.start_at == "2026-01-09T00:00:00" and rotated.end_at is not None


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "/etc/passwd", "..%2Fsecret", "trackseerr.txt/../x", "trackseerr.20260101-0000.txt.bak", "sync_db.sqlite", ""],
)
def test_resolve_rejects_non_listed_names(tmp_path, name):
    (tmp_path / CURRENT_LOG_NAME).write_text("x")
    assert resolve_log_file(tmp_path, name) is None


def test_resolve_rejects_symlink_pointing_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trackseerr.20260101-0000.txt").symlink_to(secret)
    assert resolve_log_file(logs, "trackseerr.20260101-0000.txt") is None


def test_resolve_accepts_listed_file(tmp_path):
    (tmp_path / "trackseerr.20260101-0000.txt").write_text("hi")
    assert resolve_log_file(tmp_path, "trackseerr.20260101-0000.txt") == (tmp_path / "trackseerr.20260101-0000.txt").resolve()


# --------------------------------------------------------------------------- redaction


SECRET_LINES = [
    ("GET http://x/api?apikey=SEKRET1&a=1", "SEKRET1"),
    ("GET http://x/api?token=SEKRET2", "SEKRET2"),
    ("GET http://x/api?api_key=SEKRET3", "SEKRET3"),
    ("headers {'X-Api-Key': 'SEKRET4'}", "SEKRET4"),
    ("Authorization: Bearer SEKRET5abcdef", "SEKRET5abcdef"),
    ("headers {'Authorization': 'Basic SEKRET6'}", "SEKRET6"),
    ("login password=SEKRET7 ok", "SEKRET7"),
    ('payload {"password": "SEKRET8", "user": "bob"}', "SEKRET8"),
    ("https://plex/library?X-Plex-Token=SEKRET9", "SEKRET9"),
    ("X-Plex-Token: SEKRET10", "SEKRET10"),
    ("sent bearer SEKRET11abcdefgh to upstream", "SEKRET11abcdefgh"),
    ("client_secret=SEKRET12&x=1", "SEKRET12"),
    ("tmdb_api_key: SEKRET13", "SEKRET13"),
    ("X-Api-Key: SEKRET14", "SEKRET14"),
]


@pytest.mark.parametrize("line,secret", SECRET_LINES)
def test_secrets_masked_in_file_and_ring_buffer(tmp_path, line, secret):
    clock = FakeClock()
    handler = make_handler(tmp_path, clock)
    emit(handler, clock, line)
    handler.close()
    on_disk = read(tmp_path / CURRENT_LOG_NAME)
    assert secret not in on_disk and "REDACTED" in on_disk

    log_ring_buffer.clear()
    log_ring_buffer.handle(logging.makeLogRecord({"msg": line, "levelno": 20, "levelname": "INFO", "name": "t"}))
    buffered = log_ring_buffer.get_logs(limit=5)[-1]["message"]
    assert secret not in buffered and "REDACTED" in buffered
    log_ring_buffer.clear()


def test_redaction_masks_exception_text_and_keeps_ordinary_text(tmp_path):
    clock = FakeClock()
    handler = make_handler(tmp_path, clock)
    try:
        raise RuntimeError("boom password=TRACEPW")
    except RuntimeError:
        record = logging.makeLogRecord(
            {"msg": "failed: max_tokens=500 count=3", "levelno": 40, "levelname": "ERROR", "name": "t",
             "created": clock(), "exc_info": sys.exc_info()}
        )
    handler.handle(record)
    handler.close()
    text = read(tmp_path / CURRENT_LOG_NAME)
    assert "TRACEPW" not in text
    assert "max_tokens=500 count=3" in text


# --------------------------------------------------------------------------- API


@pytest.fixture
def root_state():
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    levels = {h: h.level for h in handlers}
    for h in handlers:  # a file handler left by another test would shadow the one under test
        if isinstance(h, TimedLogFileHandler):
            root.removeHandler(h)
    yield root
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    for h, lvl in levels.items():
        h.setLevel(lvl)
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)


@pytest.fixture
def api(tmp_path, root_state):
    db = Database(":memory:")
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    for h in list(root_state.handlers):  # create_app() runs setup_logging(), which attaches its own file handler
        if isinstance(h, TimedLogFileHandler):
            root_state.removeHandler(h)
            h.close()
    secret = get_or_create_secret_key(data_dir=str(tmp_path))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    def cookies(user):
        token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret)
        db.create_session(session_id=token, user_id=user["id"])
        return {"session_token": token}

    admin = cookies(db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True))
    alice = cookies(db.upsert_user("user-alice", "alice", "al@x.tv", is_admin=False))
    with patch("trackseerr.api.routes.system.logs.get_log_file_path", return_value=log_dir / CURRENT_LOG_NAME):
        yield TestClient(app), admin, alice, log_dir, db
    db.close()


def test_settings_defaults_and_non_admin_forbidden(api):
    client, admin, alice, _, _ = api
    assert client.get("/api/system/logs/settings", cookies=alice).status_code == 403
    assert client.put("/api/system/logs/settings", json={"log_level": "DEBUG"}, cookies=alice).status_code == 403
    assert client.get("/api/system/logs/files", cookies=alice).status_code == 403
    assert client.get("/api/system/logs/files/trackseerr.txt", cookies=alice).status_code == 403
    body = client.get("/api/system/logs/settings", cookies=admin).json()
    assert body["log_rotation_hours"] == 12
    assert body["log_retention_days"] == 7
    assert body["log_max_total_mb"] == 100
    assert body["rotation_hours_options"] == [6, 8, 12, 24]
    assert body["level_options"] == ["INFO", "DEBUG", "TRACE"]


def test_env_log_level_is_the_default_until_saved(api, monkeypatch):
    client, admin, _, _, _ = api
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert client.get("/api/system/logs/settings", cookies=admin).json()["log_level"] == "DEBUG"


@pytest.mark.parametrize(
    "payload",
    [
        {"log_rotation_hours": 5},
        {"log_rotation_hours": 0},
        {"log_retention_days": 0},
        {"log_retention_days": 31},
        {"log_max_total_mb": 75},
        {"log_level": "WARNING"},
        {"log_level": "nonsense"},
    ],
)
def test_invalid_settings_rejected_with_400(api, payload):
    client, admin, _, _, db = api
    assert client.put("/api/system/logs/settings", json=payload, cookies=admin).status_code == 400
    assert db.get_kv("log_level") is None and db.get_kv("log_rotation_hours") is None


def test_put_persists_and_applies_rotation_limits_live(api, root_state):
    client, admin, _, log_dir, db = api
    handler = TimedLogFileHandler(log_dir / CURRENT_LOG_NAME)
    root_state.addHandler(handler)
    resp = client.put(
        "/api/system/logs/settings",
        json={"log_rotation_hours": 6, "log_retention_days": 3, "log_max_total_mb": 250},
        cookies=admin,
    )
    assert resp.status_code == 200, resp.text
    assert (handler.rotation_hours, handler.retention_days, handler.max_total_mb) == (6, 3, 250)
    assert load_log_settings(db).log_rotation_hours == 6
    assert client.get("/api/system/logs/settings", cookies=admin).json()["log_max_total_mb"] == 250


def test_level_change_updates_root_ring_buffer_and_file_handler_including_trace(api, root_state):
    client, admin, _, log_dir, _ = api
    handler = TimedLogFileHandler(log_dir / CURRENT_LOG_NAME)
    handler.setFormatter(logging.Formatter(FMT, datefmt=DATEFMT))
    root_state.addHandler(handler)

    assert client.put("/api/system/logs/settings", json={"log_level": "TRACE"}, cookies=admin).status_code == 200
    assert root_state.level == TRACE_LEVEL
    assert log_ring_buffer.level == TRACE_LEVEL and handler.level == TRACE_LEVEL
    assert logging.getLevelName(TRACE_LEVEL) == "TRACE"

    log_ring_buffer.clear()
    logging.getLogger("trace.test").log(TRACE_LEVEL, "very detailed trace line")
    handler.flush()
    assert any(e["level"] == "TRACE" and "very detailed" in e["message"] for e in log_ring_buffer.get_logs(limit=50))
    assert "very detailed trace line" in read(log_dir / CURRENT_LOG_NAME)

    assert client.put("/api/system/logs/settings", json={"log_level": "INFO"}, cookies=admin).status_code == 200
    assert root_state.level == logging.INFO and handler.level == logging.INFO and log_ring_buffer.level == logging.INFO
    log_ring_buffer.clear()
    logging.getLogger("trace.test").log(TRACE_LEVEL, "should be dropped")
    assert not log_ring_buffer.get_logs(limit=50)


def test_apply_log_settings_without_file_handler_is_safe(root_state):
    apply_log_settings(LogSettings(12, 7, 100, "DEBUG"))
    assert root_state.level == logging.DEBUG


def test_files_listing_and_download(api):
    client, admin, _, log_dir, _ = api
    (log_dir / CURRENT_LOG_NAME).write_text("current content\n", encoding="utf-8")
    (log_dir / "trackseerr.20260101-0000.txt").write_text("old content\n", encoding="utf-8")
    (log_dir / "trackseerr.log.1").write_text("legacy content\n", encoding="utf-8")

    listing = client.get("/api/system/logs/files", cookies=admin).json()
    assert listing[0]["name"] == CURRENT_LOG_NAME and listing[0]["end_at"] is None
    assert {f["name"] for f in listing} == {CURRENT_LOG_NAME, "trackseerr.20260101-0000.txt", "trackseerr.log.1"}
    assert all({"name", "size_bytes", "start_at", "end_at"} <= set(f) for f in listing)

    resp = client.get("/api/system/logs/files/trackseerr.20260101-0000.txt", cookies=admin)
    assert resp.status_code == 200
    assert resp.text == "old content\n"
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" in resp.headers["content-disposition"]
    assert client.get("/api/system/logs/files/trackseerr.log.1", cookies=admin).text == "legacy content\n"

    legacy_route = client.get("/api/system/logs/download", cookies=admin)
    assert legacy_route.status_code == 200 and legacy_route.text == "current content\n"


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/logs/files/..%2F..%2Fetc%2Fpasswd",
        "/api/system/logs/files/%2Fetc%2Fpasswd",
        "/api/system/logs/files/trackseerr.99999999-9999.txt",
        "/api/system/logs/files/secret.txt",
    ],
)
def test_download_rejects_traversal_and_unlisted_names(api, path):
    client, admin, _, log_dir, _ = api
    (log_dir.parent / "secret.txt").write_text("secret")
    (log_dir / "secret.txt").write_text("secret")
    resp = client.get(path, cookies=admin)
    assert resp.status_code in (400, 404, 405)
    assert "secret" not in resp.text
