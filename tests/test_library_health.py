"""Library health: disk-vs-server diff, cause classification, dismissals, mapping, scheduling, routes, migration."""
import os
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library_health as lh
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_active_media_server, get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.media_servers.base import MediaServerError, MediaServerUnsupported, ServerCapabilities, ServerFileRef
from trackseerr.storage import SCHEMA_VERSION, Database

SERVER_ROOT = "/data/music"
FULL_TAGS = {"artist": "A", "album": "B", "title": "C"}


class FakeServer:
    kind = "plex"
    paths_relative = False

    def __init__(self, refs: list[ServerFileRef], *, file_paths: bool = True, roots: Optional[list[str]] = None,
                 last_scan: Optional[datetime] = None, error: Optional[Exception] = None, kind: str = "plex"):
        self.refs, self.roots, self.last_scan, self.error, self.kind = refs, roots or [], last_scan, error, kind
        self._caps = ServerCapabilities(file_paths=file_paths)

    @property
    def capabilities(self) -> ServerCapabilities:
        return self._caps

    def iter_library_files(self) -> Iterator[ServerFileRef]:
        if self.error:
            raise self.error
        yield from self.refs

    def last_scan_at(self) -> Optional[datetime]:
        return self.last_scan

    def library_roots(self) -> list[str]:
        return self.roots


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "health.db"))
    yield d
    d.close()


@pytest.fixture
def music(tmp_path: Path) -> Path:
    m = tmp_path / "music"
    m.mkdir()
    return m


@pytest.fixture(autouse=True)
def tags_ok(monkeypatch):
    """Default: files are healthy (magic ok, full tags); individual tests override."""
    monkeypatch.setattr(lh, "check_magic", lambda p: None)
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: dict(FULL_TAGS))


def touch(path: Path, age_days: float = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


def ref(music: Path, path: Path) -> ServerFileRef:
    return ServerFileRef("id", SERVER_ROOT + "/" + str(path.relative_to(music)).replace(os.sep, "/"), "t", "a", "b", "flac")


def save_mapping(db: Database, music: Path, kind: str = "plex") -> None:
    db.set_media_server_path_mapping(
        {"server_prefix": SERVER_ROOT, "local_prefix": str(music), "auto": False, "server_kind": kind}
    )


def check(db: Database, server: Any, music: Path) -> dict[str, Any]:
    return lh.run_check(db, server, music_root=music)


def findings(db: Database) -> dict[str, dict[str, Any]]:
    return {f["path"]: f for f in db.list_library_health_findings()}


def base_library(db: Database, music: Path) -> tuple[Path, list[ServerFileRef]]:
    """music/Art/Alb1/{1,2}.flac indexed; returns (indexed file 1, refs)."""
    f1, f2 = touch(music / "Art/Alb1/1.flac"), touch(music / "Art/Alb1/2.flac")
    save_mapping(db, music)
    return f1, [ref(music, f1), ref(music, f2)]


# ------------------------------------------------------------------ cause rules


def test_folder_not_in_server_groups_subtree_under_topmost_dir(db, music):
    _, refs = base_library(db, music)
    touch(music / "Art/Alb2/CD1/1.flac")
    touch(music / "Art/Alb2/CD2/2.flac")
    run = check(db, FakeServer(refs), music)
    assert run["error"] is None and run["unindexed"] == 2
    got = findings(db)
    assert {f["cause"] for f in got.values()} == {lh.CAUSE_FOLDER}
    assert {f["group_key"] for f in got.values()} == {str(music / "Art/Alb2")}
    groups = lh.group_findings(list(got.values()))
    assert len(groups) == 1 and groups[0]["count"] == 2 and groups[0]["suggestion"]


def test_folder_rule_zero_overlap_wrong_mapping(db, music):
    f = touch(music / "Art/Alb/1.flac")
    save_mapping(db, music)
    other = [ServerFileRef("x", "/elsewhere/Art/Alb/1.flac")]
    check(db, FakeServer(other), music)
    got = findings(db)
    assert got[str(f)]["cause"] == lh.CAUSE_FOLDER
    assert not any(x["kind"] == lh.KIND_STALE for x in got.values()), "no stale noise on a zero-overlap mapping"


def test_folder_rule_outside_server_library_roots(db, music):
    f1, refs = base_library(db, music)
    extra = touch(music / "Art/Alb1/3.flac")  # indexed dir, but outside the server's configured library root
    check(db, FakeServer(refs, roots=[SERVER_ROOT + "/Other"]), music)
    assert findings(db)[str(extra)]["cause"] == lh.CAUSE_FOLDER


def test_not_scanned_yet_uses_mtime_vs_last_scan(db, music):
    _, refs = base_library(db, music)
    fresh = touch(music / "Art/Alb3/new.flac", age_days=0)
    old = touch(music / "Art/Alb1/old.flac", age_days=30)
    scan = datetime.now(timezone.utc) - timedelta(days=1)
    check(db, FakeServer(refs, last_scan=scan), music)
    got = findings(db)
    assert got[str(fresh)]["cause"] == lh.CAUSE_NOT_SCANNED
    assert got[str(old)]["cause"] == lh.CAUSE_UNKNOWN


def test_new_album_dir_after_last_scan_is_not_scanned_yet_not_folder(db, music):
    _, refs = base_library(db, music)
    new = touch(music / "Art/NewAlb/1.flac", age_days=0)
    scan = datetime.now(timezone.utc) - timedelta(hours=1)
    check(db, FakeServer(refs, last_scan=scan), music)
    assert findings(db)[str(new)]["cause"] == lh.CAUSE_NOT_SCANNED


def test_new_album_dir_older_than_last_scan_is_folder_not_in_server(db, music):
    _, refs = base_library(db, music)
    old = touch(music / "Art/NewAlb/1.flac", age_days=5)
    scan = datetime.now(timezone.utc) - timedelta(days=1)
    check(db, FakeServer(refs, last_scan=scan), music)
    assert findings(db)[str(old)]["cause"] == lh.CAUSE_FOLDER


def test_dir_newest_file_mtime_marks_old_sibling_not_scanned(db, music):
    _, refs = base_library(db, music)
    touch(music / "Art/NewAlb/new.flac", age_days=0)
    old = touch(music / "Art/NewAlb/old.flac", age_days=5)
    scan = datetime.now(timezone.utc) - timedelta(days=1)
    check(db, FakeServer(refs, last_scan=scan), music)
    assert findings(db)[str(old)]["cause"] == lh.CAUSE_NOT_SCANNED


def test_no_last_scan_recent_file_is_not_scanned_yet(db, music):
    _, refs = base_library(db, music)
    recent = touch(music / "Art/NewAlb/1.flac", age_days=0)
    stale = touch(music / "Art/OtherAlb/1.flac", age_days=5)
    check(db, FakeServer(refs, last_scan=None), music)
    got = findings(db)
    assert got[str(recent)]["cause"] == lh.CAUSE_NOT_SCANNED
    assert got[str(stale)]["cause"] == lh.CAUSE_FOLDER


def test_unsupported_format_is_per_server(db, music):
    _, refs = base_library(db, music)
    ape = touch(music / "Art/Alb1/3.ape")
    check(db, FakeServer(refs, kind="plex"), music)
    assert findings(db)[str(ape)]["cause"] == lh.CAUSE_UNSUPPORTED
    save_mapping(db, music, kind="jellyfin")
    check(db, FakeServer(refs, kind="jellyfin"), music)
    assert str(ape) not in findings(db), "jellyfin indexes .ape, and .ape is outside the importer's audio extensions"


def test_corrupt_by_magic_and_by_parse_failure(db, music, monkeypatch):
    _, refs = base_library(db, music)
    bad_magic = touch(music / "Art/Alb1/magic.flac")
    bad_parse = touch(music / "Art/Alb1/parse.flac")
    monkeypatch.setattr(lh, "check_magic", lambda p: "content does not match .flac signature" if "magic" in str(p) else None)

    def inspect(p):
        if "parse" in str(p):
            raise ValueError("Unsupported audio file format or corrupted file")
        return dict(FULL_TAGS)

    monkeypatch.setattr(lh, "inspect_audio_file", inspect)
    check(db, FakeServer(refs), music)
    got = findings(db)
    assert got[str(bad_magic)]["cause"] == got[str(bad_parse)]["cause"] == lh.CAUSE_CORRUPT
    assert "signature" in got[str(bad_magic)]["detail"]["problem"]


@pytest.mark.parametrize("missing", ["artist", "album", "title"])
def test_missing_core_tags(db, music, monkeypatch, missing):
    _, refs = base_library(db, music)
    f = touch(music / "Art/Alb1/untagged.flac")
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: {**FULL_TAGS, missing: None})
    check(db, FakeServer(refs), music)
    assert findings(db)[str(f)]["cause"] == lh.CAUSE_MISSING_TAGS


def test_unreadable_permissions(db, music):
    _, refs = base_library(db, music)
    f = touch(music / "Art/Alb1/locked.flac")
    f.chmod(0o600)
    ok = touch(music / "Art/Alb1/open.flac")
    ok.chmod(0o644)
    check(db, FakeServer(refs), music)
    got = findings(db)
    assert got[str(f)]["cause"] == lh.CAUSE_PERMISSIONS
    assert got[str(ok)]["cause"] == lh.CAUSE_UNKNOWN


def test_plexignore_applies_to_plex_only(db, music):
    _, refs = base_library(db, music)
    (music / ".plexignore").write_text("# comment\n\nskip_*\n")
    f = touch(music / "Art/Alb1/skip_me.flac")
    check(db, FakeServer(refs, kind="plex"), music)
    assert findings(db)[str(f)]["cause"] == lh.CAUSE_IGNORED
    save_mapping(db, music, kind="jellyfin")
    check(db, FakeServer(refs, kind="jellyfin"), music)
    assert findings(db)[str(f)]["cause"] == lh.CAUSE_UNKNOWN


def test_first_matching_rule_wins(db, music, monkeypatch):
    _, refs = base_library(db, music)
    monkeypatch.setattr(lh, "check_magic", lambda p: "bad")
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: {})
    # new + unsupported + corrupt + untagged + unreadable: not_scanned_yet comes first
    a = touch(music / "Art/Alb8/a.ape", age_days=0)
    a.chmod(0o600)
    # old + unsupported + corrupt + untagged + unreadable: unsupported next
    b = touch(music / "Art/Alb1/b.wv")
    b.chmod(0o600)
    # old + supported + corrupt + untagged + unreadable: corrupt beats missing tags and permissions
    c = touch(music / "Art/Alb1/c.flac")
    c.chmod(0o600)
    # whole unindexed folder that is also corrupt: folder first
    d = touch(music / "Art/Alb9/d.flac", age_days=30)
    scan = datetime.now(timezone.utc) - timedelta(days=1)
    check(db, FakeServer(refs, last_scan=scan), music)
    got = findings(db)
    assert got[str(a)]["cause"] == lh.CAUSE_NOT_SCANNED
    assert got[str(b)]["cause"] == lh.CAUSE_UNSUPPORTED
    assert got[str(c)]["cause"] == lh.CAUSE_CORRUPT
    assert got[str(d)]["cause"] == lh.CAUSE_FOLDER


def test_missing_tags_beats_permissions(db, music, monkeypatch):
    _, refs = base_library(db, music)
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: {"artist": "A", "album": None, "title": "T"})
    f = touch(music / "Art/Alb1/x.flac")
    f.chmod(0o600)
    check(db, FakeServer(refs), music)
    assert findings(db)[str(f)]["cause"] == lh.CAUSE_MISSING_TAGS


def test_grouping_by_cause_and_parent_directory(db, music, monkeypatch):
    _, refs = base_library(db, music)
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: {**FULL_TAGS, "title": None})
    for n in range(3):
        touch(music / f"Art/Alb1/u{n}.flac")
    touch(music / "Art/Alb1/sub/v.flac")
    check(db, FakeServer(refs), music)
    groups = {(g["group_key"], g["cause"]): g["count"] for g in lh.group_findings(db.list_library_health_findings())}
    assert groups[(str(music / "Art/Alb1"), lh.CAUSE_MISSING_TAGS)] == 3
    assert groups[(str(music / "Art/Alb1/sub"), lh.CAUSE_FOLDER)] == 1


# ------------------------------------------------------------------ stale, refresh, dismissals


def test_stale_server_entries_and_rows_not_seen_are_deleted(db, music):
    f1, refs = base_library(db, music)
    ghost = ServerFileRef("g", SERVER_ROOT + "/Art/Alb1/gone.flac", "Gone", "A", "B", "flac")
    run = check(db, FakeServer(refs + [ghost]), music)
    assert run["stale"] == 1 and run["server_files"] == 3 and run["disk_files"] == 2
    stale = [f for f in db.list_library_health_findings() if f["kind"] == lh.KIND_STALE]
    assert stale[0]["path"] == str(music / "Art/Alb1/gone.flac") and stale[0]["detail"]["server_path"].endswith("gone.flac")
    first_seen = stale[0]["first_seen"]
    # second run: entry removed from the server -> finding is deleted; unindexed fixed -> deleted
    extra = touch(music / "Art/Alb1/3.flac")
    check(db, FakeServer(refs), music)
    assert [f["path"] for f in db.list_library_health_findings()] == [str(extra)]
    check(db, FakeServer(refs + [ref(music, extra)]), music)
    assert db.list_library_health_findings() == []
    assert first_seen


def test_refresh_keeps_id_and_first_seen(db, music):
    _, refs = base_library(db, music)
    f = touch(music / "Art/Alb1/3.flac")
    check(db, FakeServer(refs), music)
    before = findings(db)[str(f)]
    time.sleep(0.01)
    check(db, FakeServer(refs), music)
    after = findings(db)[str(f)]
    assert after["id"] == before["id"] and after["first_seen"] == before["first_seen"] and after["last_seen"] > before["last_seen"]


def test_dismissals_by_file_and_folder_survive_rerun(db, music):
    _, refs = base_library(db, music)
    one = touch(music / "Art/Alb1/one.flac")
    keep = touch(music / "Art/Alb1/keep.flac")
    touch(music / "Art/Alb2/a.flac")
    touch(music / "Art/Alb2/deep/b.flac")
    check(db, FakeServer(refs), music)
    assert len(db.list_library_health_findings()) == 4
    db.add_library_health_dismissal("file", str(one))
    db.add_library_health_dismissal("folder", str(music / "Art/Alb2"))
    check(db, FakeServer(refs), music)
    assert list(findings(db)) == [str(keep)]
    # a similarly-named sibling folder is not covered by the folder prefix
    touch(music / "Art/Alb22/c.flac")
    check(db, FakeServer(refs), music)
    assert str(music / "Art/Alb22/c.flac") in findings(db)


# ------------------------------------------------------------------ mapping


def test_mapping_is_auto_suggested_and_persisted_with_flag(db, music):
    files = [touch(music / f"Art/Alb/{n}.flac") for n in range(4)]
    refs = [ref(music, f) for f in files]
    assert db.get_media_server_path_mapping() is None
    run = check(db, FakeServer(refs), music)
    assert run["error"] is None and run["unindexed"] == 0
    m = db.get_media_server_path_mapping()
    assert m and m["auto"] is True and m["server_prefix"].startswith(SERVER_ROOT)
    assert str(music) in m["local_prefix"] and lh.mapping_view(db)["auto"] is True


def test_saved_mapping_is_used_not_re_suggested(db, music):
    f = touch(music / "Art/Alb/1.flac")
    db.set_media_server_path_mapping({"server_prefix": "/srv/lib", "local_prefix": str(music), "auto": False, "server_kind": "plex"})
    with patch.object(lh.path_mapping, "suggest_mapping") as sug:
        check(db, FakeServer([ServerFileRef("1", "/srv/lib/Art/Alb/1.flac")]), music)
    sug.assert_not_called()
    assert db.list_library_health_findings() == [] and f.exists()
    assert db.get_media_server_path_mapping()["auto"] is False


def test_relative_server_paths_join_onto_music_root(db, music):
    f = touch(music / "Art/Alb/1.flac")
    server = FakeServer([ServerFileRef("1", "Art/Alb/1.flac")], kind="subsonic")
    server.paths_relative = True
    run = check(db, server, music)
    assert run["unindexed"] == 0 and run["stale"] == 0 and f.exists()


# ------------------------------------------------------------------ error handling


def test_unsupported_server_records_error_and_leaves_findings(db, music):
    _, refs = base_library(db, music)
    f = touch(music / "Art/Alb1/3.flac")
    check(db, FakeServer(refs), music)
    before = findings(db)
    run = check(db, FakeServer([], error=MediaServerUnsupported("user key has no Path")), music)
    assert "not available" in run["error"]
    assert findings(db) == before
    assert db.get_last_library_health_run()["error"] == run["error"]
    assert f.exists()


def test_unreachable_server_records_error(db, music):
    base_library(db, music)
    run = check(db, FakeServer([], error=MediaServerError("boom")), music)
    assert "Could not read the media server library" in run["error"]


def test_no_server_and_no_file_path_capability_are_clear_errors_not_exceptions(db, music):
    assert "No media server" in check(db, None, music)["error"]
    assert "does not expose" in check(db, FakeServer([], file_paths=False), music)["error"]
    assert "does not exist" in check(db, FakeServer([]), music / "nope")["error"]


# ------------------------------------------------------------------ concurrency, job tracker, schedule


def test_concurrent_check_is_rejected(db, music):
    with lh._run_lock:
        assert lh.is_running()
        with pytest.raises(lh.LibraryHealthBusy):
            check(db, FakeServer([]), music)
        assert lh.start_check_async(db, FakeServer([]), music_root=music) is False
    assert not lh.is_running()


def test_check_appears_in_job_tracker(db, music):
    from trackseerr.job_tracker import job_tracker

    job_tracker.clear()
    base_library(db, music)
    check(db, FakeServer([]), music)
    jobs = [j for j in job_tracker.snapshot()["recent"] if j["task_id"] == "library_health"]
    assert len(jobs) == 1 and jobs[0]["state"] == "completed"
    check(db, None, music)  # an error run is a failed job
    jobs = [j for j in job_tracker.snapshot()["recent"] if j["task_id"] == "library_health"]
    assert [j["state"] for j in jobs].count("failed") == 1


def test_start_check_async_runs_and_releases_lock(db, music):
    _, refs = base_library(db, music)
    assert lh.start_check_async(db, FakeServer(refs), music_root=music) is True
    deadline = time.time() + 5
    while lh.is_running() and time.time() < deadline:
        time.sleep(0.02)
    assert not lh.is_running()
    assert db.get_last_library_health_run()["error"] is None


def test_weekly_due_logic(db):
    now = datetime.now(timezone.utc)
    assert lh.weekly_due(db, now) is True  # never ran, default on
    db.record_library_health_run({"started_at": now.isoformat(), "finished_at": (now - timedelta(days=3)).isoformat()})
    assert lh.weekly_due(db, now) is False
    db.record_library_health_run({"started_at": now.isoformat(), "finished_at": (now - timedelta(days=8)).isoformat()})
    assert lh.weekly_due(db, now) is True
    db.set_library_health_weekly(False)
    assert lh.weekly_due(db, now) is False


def test_worker_runs_only_with_a_file_path_server(db, music):
    db.update_media_management_settings({"root_folder_path": str(music)})
    worker = lh.LibraryHealthWorker()
    with patch("trackseerr.media_servers.get_media_server", return_value=None):
        assert worker.run_if_due(db, MagicMock()) is False
    with patch("trackseerr.media_servers.get_media_server", return_value=FakeServer([], file_paths=False)):
        assert worker.run_if_due(db, MagicMock()) is False
    assert db.get_last_library_health_run() is None
    with patch("trackseerr.media_servers.get_media_server", return_value=FakeServer([])):
        assert worker.run_if_due(db, MagicMock()) is True
        assert worker.run_if_due(db, MagicMock()) is False  # ran just now: not due again
    assert db.get_last_library_health_run() is not None


def test_worker_start_stop_waits_on_event(db):
    worker = lh.LibraryHealthWorker()
    assert worker.start(db, MagicMock(), interval_seconds=3600, initial_delay=3600) is True
    assert worker.start(db, MagicMock()) is False
    t0 = time.time()
    worker.stop()
    assert time.time() - t0 < 4 and not worker.is_running()


# ------------------------------------------------------------------ weak matches


def test_record_weak_match_and_failure_never_raises(db):
    assert lh.record_weak_match(db, "/music/a.flac", track_id="t1", title="T", source_name="Rel", strength="weak")
    f = db.list_library_health_findings()[0]
    assert f["kind"] == "weak_match" and f["cause"] == "weak_tag_match" and f["path"] == "/music/a.flac"
    assert f["detail"] == {"track_id": "t1", "title": "T", "source_name": "Rel", "strength": "weak"}
    with patch.object(Database, "upsert_library_health_findings", side_effect=sqlite3.OperationalError("locked")):
        assert lh.record_weak_match(db, "/music/b.flac", track_id="t", title="T", source_name="R", strength="weak") is False
    # a refresh run never deletes weak matches
    assert db.delete_library_health_findings_not_seen([lh.KIND_UNINDEXED, lh.KIND_STALE], "zzz") == 0
    assert db.count_library_health_findings() == 1


# ------------------------------------------------------------------ migration


def test_migration_v57(db):
    assert SCHEMA_VERSION >= 57
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 57
    for table in ("library_health_findings", "library_health_runs", "library_health_dismissals"):
        assert db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(media_server_settings)")}
    assert "path_mapping_json" in cols
    assert db.get_library_health_weekly() is True
    db._migration_v57(db.conn.cursor())  # idempotent
    # settings saves keep the mapping
    db.set_media_server_path_mapping({"server_prefix": "/a", "local_prefix": "/b", "auto": False, "server_kind": ""})
    db.save_media_server_settings({"type": "jellyfin", "url": "http://j"})
    assert db.get_media_server_path_mapping()["server_prefix"] == "/a"
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO library_health_findings (id, kind, cause, group_key, path, first_seen, last_seen) "
            "VALUES ('x', 'bogus', 'c', 'g', 'p', 'n', 'n')"
        )


# ------------------------------------------------------------------ routes


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db: Database, config: Config, *, admin: bool) -> dict[str, str]:
    user = db.upsert_user("u-1" if not admin else "a-1", "user" if not admin else "admin_user", "u@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="x", is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def server_box() -> dict[str, Any]:
    return {"server": None}


@pytest.fixture
def client(db, config, server_box):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_active_media_server] = lambda: server_box["server"]
    return TestClient(app)


def test_routes_require_admin(client, db, config):
    for method, url in (("get", ""), ("get", "/count"), ("post", "/check"), ("delete", "/mapping")):
        assert getattr(client, method)("/api/library-health" + url).status_code in (401, 403)
        resp = getattr(client, method)("/api/library-health" + url, headers=_headers(db, config, admin=False))
        assert resp.status_code == 403, (method, url)


def test_get_shape_and_count(client, db, config, music, server_box):
    h = _headers(db, config, admin=True)
    empty = client.get("/api/library-health", headers=h).json()
    assert empty == {"findings": [], "groups": [], "last_run": None, "running": False, "mapping": None,
                     "server": None, "count": 0, "weekly": True}
    _, refs = base_library(db, music)
    touch(music / "Art/Alb1/3.flac")
    touch(music / "Art/Alb1/4.flac")
    server_box["server"] = FakeServer(refs)
    check(db, server_box["server"], music)
    body = client.get("/api/library-health", headers=h).json()
    assert body["count"] == 2 and len(body["findings"]) == 2
    assert set(body["findings"][0]) == {"id", "kind", "cause", "group_key", "path", "detail", "first_seen", "last_seen"}
    g = body["groups"][0]
    assert set(g) == {"group_key", "kind", "cause", "count", "sample_path", "suggestion"} and g["count"] == 2
    assert body["server"] == {"kind": "plex", "file_paths": True}
    assert body["mapping"] == {"server_prefix": SERVER_ROOT, "local_prefix": str(music), "auto": False}
    assert set(body["last_run"]) >= {"started_at", "finished_at", "server_kind", "disk_files", "server_files", "unindexed", "stale", "error"}
    assert client.get("/api/library-health/count", headers=h).json() == {"count": 2}


def test_check_route_starts_then_409_while_running(client, db, config, music, server_box):
    h = _headers(db, config, admin=True)
    db.update_media_management_settings({"root_folder_path": str(music)})
    _, refs = base_library(db, music)
    server_box["server"] = FakeServer(refs)
    gate = threading.Event()
    real = lh._execute

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    with patch.object(lh, "_execute", side_effect=slow):
        r = client.post("/api/library-health/check", headers=h)
        assert r.status_code == 202 and r.json() == {"started": True}
        assert client.get("/api/library-health", headers=h).json()["running"] is True
        assert client.post("/api/library-health/check", headers=h).status_code == 409
        gate.set()
        deadline = time.time() + 5
        while lh.is_running() and time.time() < deadline:
            time.sleep(0.02)
    assert db.get_last_library_health_run() is not None


def test_dismiss_route_removes_findings_and_persists(client, db, config, music, server_box):
    h = _headers(db, config, admin=True)
    _, refs = base_library(db, music)
    a, b = touch(music / "Art/Alb1/a.flac"), touch(music / "Art/Alb2/b.flac")
    server_box["server"] = FakeServer(refs)
    check(db, server_box["server"], music)
    assert client.post("/api/library-health/dismiss", json={"path": str(a), "scope": "file"}, headers=h).json()["removed"] == 1
    assert client.post("/api/library-health/dismiss", json={"path": str(music / "Art/Alb2"), "scope": "folder"}, headers=h).json()["removed"] == 1
    assert client.get("/api/library-health/count", headers=h).json() == {"count": 0}
    check(db, server_box["server"], music)
    assert db.list_library_health_findings() == [] and b.exists()
    assert client.post("/api/library-health/dismiss", json={"path": "/x", "scope": "bogus"}, headers=h).status_code == 422


def test_mapping_routes_save_clear_and_clear_auto_flag(client, db, config):
    h = _headers(db, config, admin=True)
    db.set_media_server_path_mapping({"server_prefix": "/a", "local_prefix": "/b", "auto": True, "server_kind": "plex"})
    r = client.put("/api/library-health/mapping", json={"server_prefix": "/data", "local_prefix": "/music"}, headers=h)
    assert r.json() == {"mapping": {"server_prefix": "/data", "local_prefix": "/music", "auto": False}}
    assert client.get("/api/library-health", headers=h).json()["mapping"]["auto"] is False
    assert client.delete("/api/library-health/mapping", headers=h).json() == {"mapping": None}
    assert db.get_media_server_path_mapping() is None
    assert client.put("/api/library-health/weekly", json={"enabled": False}, headers=h).json() == {"weekly": False}
