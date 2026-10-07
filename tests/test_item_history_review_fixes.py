"""Regression tests for the item-history review findings: non-admin redaction, event transactions, bulk writes."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync import recycle_bin
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, require_admin
from plex_playlist_sync.item_history import (
    TRIGGER_KINDS,
    TRIGGER_RSS,
    GrabTrigger,
    HistoryPresenter,
    current_provenance,
    provenance,
    public_message,
    redact_details,
)
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.storage import Database
from tests.test_item_history import client, config, db, headers, library, users  # noqa: F401  (fixtures)

LABEL = "SECRET-LABEL-7731"


# ------------------------------------------------------------------------------------------ 1. trigger labels
def _event(kind: str, actor: str | None, event: str = "grabbed") -> dict[str, Any]:
    return {
        "id": 1, "event": event, "created_at": "2026-01-01 00:00:00", "track_id": "trk-1", "album_id": "alb-1",
        "artist_id": "art-1", "track_title": "Lithium", "album_title": "Nevermind", "artist_name": "Nirvana",
        "trigger": kind, "trigger_ref": LABEL, "trigger_label": LABEL, "actor_user_id": actor,
        "message": f"Grabbed for {LABEL}", "details": {"indexer": LABEL, "indexerName": LABEL, "quality": "FLAC"},
    }


@pytest.mark.parametrize("actor", [None, "user-bob"])
@pytest.mark.parametrize("kind", sorted(TRIGGER_KINDS))
def test_no_trigger_label_reaches_a_non_admin_for_any_trigger_kind(db, users, kind, actor):  # noqa: F811
    presenter = HistoryPresenter(db, users["alice"], is_admin=False)
    event = _event(kind, actor)
    shown = presenter.present(event)
    origin = presenter.present_origin(event)
    for field in ("actor_display", "trigger_label", "message", "details"):
        assert LABEL not in str(shown[field]), (kind, actor, field)
    assert LABEL not in str(origin)
    assert shown["actor_display"] and origin["actor_display"] == shown["actor_display"]


def test_rss_sync_shows_a_generic_actor_not_the_indexer(db, users):  # noqa: F811
    shown = HistoryPresenter(db, users["alice"], is_admin=False).present(_event(TRIGGER_RSS, None))
    assert shown["actor_display"] == "RSS sync" and shown["trigger_label"] == "RSS sync"
    admin = HistoryPresenter(db, users["admin"], is_admin=True).present(_event(TRIGGER_RSS, None))
    assert admin["actor_display"] == LABEL  # admins still see the indexer


def test_viewer_still_sees_their_own_playlist_and_requests(db, users):  # noqa: F811
    presenter = HistoryPresenter(db, users["alice"], is_admin=False)
    own = presenter.present(_event("playlist", "user-alice"))
    assert own["trigger_label"] == LABEL and own["actor_display"] == "alice"
    other = presenter.present(_event("playlist", "user-bob"))
    assert other["trigger_label"] is None and other["actor_display"] == "another user"
    retry = presenter.present({**_event("retry", None), "trigger_label": "bob"})
    assert retry["trigger_label"] == "another user" and "bob" not in str(retry)


# --------------------------------------------------------------------------- 2. path / filename redaction
@pytest.mark.parametrize(
    "event,message,details",
    [
        ("imported", "Imported 03 - Lithium.flac", {"path": "/music/N/03 - Lithium.flac", "quality": "FLAC"}),
        ("file_deleted", "Deleted my secret song.flac from disk", {"path": "/music/my secret song.flac"}),
        ("renamed", "old name.flac -> new name.flac", {"from": "/m/old name.flac", "to": "/m/new name.flac"}),
        ("moved", "Moved to rel/dir/x y.flac", {"relPath": "rel/dir/x y.flac"}),
        ("file_missing", "Scanner found the file gone from disk", {"path": "C:\\Music\\a b.flac"}),
    ],
)
def test_non_admin_message_is_server_generated_and_path_free(db, users, event, message, details):  # noqa: F811
    ev = {**_event("system", None, event), "message": message, "details": details, "trigger_label": None}
    shown = HistoryPresenter(db, users["alice"], is_admin=False).present(ev)
    assert shown["message"] == public_message(event, details)
    assert shown["message"] != message
    blob = str(shown)
    for leak in (".flac", "secret", "rel/dir", "Music", "/m/", "new name"):
        assert leak not in blob, leak
    assert HistoryPresenter(db, users["admin"], is_admin=True).present(ev)["message"] == message


def test_generated_messages_use_only_safe_fields():
    assert public_message("imported", {"quality": "FLAC 16/44", "codec": "FLAC"}) == "Imported (FLAC 16/44)"
    assert public_message("imported", {"quality": "/etc/passwd"}) == "Imported"
    assert public_message("file_deleted", {"path": "/x/y.flac"}) == "File deleted from disk"
    assert public_message("renamed", {"from": "a", "to": "b"}) == "File renamed"
    assert public_message("upgraded", {"from_quality": "MP3 320", "to_quality": "FLAC"}) == "Upgraded (MP3 320 -> FLAC)"
    assert public_message("download_failed", {"failure_count": 3}) == "Download failed (attempt 3)"


def test_redact_details_handles_spaces_relative_paths_nesting_and_camel_case():
    raw = {
        "note": "see my music/Some Album/track one.flac now",
        "windows": "C:\\My Music\\a b.flac",
        "relPath": "x", "indexerName": "Tracker", "releaseTitle": "Some Release", "release": "r", "downloadUrl": "u",
        "InfoHash": "h", "Recycled_To": "/bin/x",
        "quality": "FLAC 16/44", "codec": "FLAC", "failure_count": 2, "ok": "fine",
        "nested": {"deep": [{"filePath": "p", "keep": "yes", "list": ["a/b.flac", "plain"]}], "folder": "f"},
    }
    assert redact_details(raw) == {
        "quality": "FLAC 16/44", "codec": "FLAC", "failure_count": 2, "ok": "fine",
        "nested": {"deep": [{"keep": "yes", "list": ["plain"]}]},
    }


# ------------------------------------------------------------------------ 3. event transactions are scoped
class _FailingConn:
    """Delegates to a real connection but raises on any statement containing ``needle``."""

    def __init__(self, real: sqlite3.Connection, needle: str) -> None:
        self._real, self._needle = real, needle

    def execute(self, sql: str, *args: Any):
        if self._needle in sql:
            raise sqlite3.OperationalError("simulated failure")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _event_rows(db: Database, event: str) -> int:  # noqa: F811
    return db.conn.execute("SELECT COUNT(*) FROM item_events WHERE event = ?", (event,)).fetchone()[0]


def test_failed_event_write_is_rolled_back_and_cannot_ride_a_later_commit(db, library):  # noqa: F811
    real = db.conn
    db.record_item_event("requested", request_id="req-9", artist_name="Nirvana", message="first")
    db._conn = _FailingConn(real, "UPDATE item_events")  # the adopt step fails after the insert succeeded
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.record_item_event("grabbed", track_id="trk-1", request_id="req-9", message="orphan")
    finally:
        db._conn = real
    assert not real.in_transaction
    db.set_track_monitored("trk-2", False)  # an unrelated commit
    assert _event_rows(db, "grabbed") == 0


def test_event_inside_a_callers_transaction_does_not_commit_it(db, library):  # noqa: F811
    db.conn.execute("UPDATE library_tracks SET title = 'Pending' WHERE id = 'trk-1'")  # caller txn left open
    assert db.conn.in_transaction
    db.record_item_event("grabbed", track_id="trk-1", message="inside")
    assert db.conn.in_transaction  # still the caller's to commit
    db.conn.rollback()
    assert db.conn.execute("SELECT title FROM library_tracks WHERE id = 'trk-1'").fetchone()[0] == "Lithium"
    assert _event_rows(db, "grabbed") == 0


def test_failed_event_inside_a_callers_transaction_leaves_the_callers_work_intact(db, library):  # noqa: F811
    real = db.conn
    real.execute("UPDATE library_tracks SET title = 'Pending' WHERE id = 'trk-1'")
    db._conn = _FailingConn(real, "UPDATE item_events")
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.record_item_event("requested", track_id="trk-1", request_id="req-1", message="boom")
    finally:
        db._conn = real
    assert real.in_transaction
    real.commit()
    assert real.execute("SELECT title FROM library_tracks WHERE id = 'trk-1'").fetchone()[0] == "Pending"
    assert _event_rows(db, "requested") == 0


# ------------------------------------------------------------------ 4. blocklist guarded id resolution
def test_add_to_blocklist_survives_a_failing_library_lookup(db, caplog):  # noqa: F811
    with patch.object(db, "find_library_ids_by_name", side_effect=sqlite3.OperationalError("locked")):
        item = db.add_to_blocklist("Some - Release [FLAC]", artist="Nirvana", album="Nevermind", info_hash="abc")
    assert item["source_title"] == "Some - Release [FLAC]"
    assert db.is_blocklisted(release_title="Some - Release [FLAC]")
    assert "Could not record the blocklisted item event" in caplog.text


# ------------------------------------------------------------------------------------- 5. bulk + recycled
class _CommitCounter:
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real, self.commits = real, 0

    def commit(self) -> None:
        self.commits += 1
        self._real.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _add_albums(db: Database, count: int) -> list[str]:  # noqa: F811
    ids = []
    for i in range(count):
        aid = f"bulk-alb-{i}"
        db.upsert_library_album(
            {"id": aid, "artist_id": "art-1", "title": f"Album {i}", "clean_title": f"album {i}", "monitored": True}
        )
        ids.append(aid)
    return ids


def test_bulk_album_toggle_writes_all_events_with_one_event_commit(db, library):  # noqa: F811
    ids = _add_albums(db, 500)
    counter = _CommitCounter(db.conn)
    db._conn = counter
    try:
        with provenance(GrabTrigger("manual", label="admin_user", actor_user_id="admin-1")):
            assert db.bulk_set_albums_monitored(ids, False, cascade_tracks=False) == 500
    finally:
        db._conn = counter._real
    assert counter.commits == 2  # the toggle itself plus ONE for all 500 events
    rows = db.conn.execute(
        "SELECT album_id, artist_id, album_title, artist_name, \"trigger\" FROM item_events WHERE event = 'unmonitored'"
    ).fetchall()
    assert len(rows) == 500 and {r["album_id"] for r in rows} == set(ids)
    assert all(r["artist_id"] == "art-1" and r["artist_name"] == "Nirvana" and r["trigger"] == "manual" for r in rows)
    assert {r["album_title"] for r in rows} >= {"Album 0", "Album 499"}


def test_bulk_track_toggle_records_one_event_per_changed_track(db, library):  # noqa: F811
    counter = _CommitCounter(db.conn)
    db._conn = counter
    try:
        assert db.bulk_set_tracks_monitored(["trk-1", "trk-2"], False) == 2
        assert db.bulk_set_tracks_monitored(["trk-1", "trk-2"], False) == 2  # no change: no events
    finally:
        db._conn = counter._real
    assert counter.commits == 3  # first call: update + one events commit; the no-change call commits only its update
    rows = db.list_item_events("album", "alb-1", limit=50)
    assert sorted(r["track_id"] for r in rows if r["event"] == "unmonitored") == ["trk-1", "trk-2"]
    assert {r["track_title"] for r in rows if r["event"] == "unmonitored"} == {"Lithium", "Polly"}


def test_record_item_events_bulk_validates_and_rolls_back_atomically(db, library):  # noqa: F811
    before = db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0]
    with pytest.raises(ValueError):
        db.record_item_events_bulk([{"event": "monitored", "track_id": "trk-1"}, {"event": "nope"}])
    assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == before
    with patch.object(db, "_insert_item_event", side_effect=sqlite3.OperationalError("boom")):
        with pytest.raises(sqlite3.OperationalError):
            db.record_item_events_bulk(
                [{"event": "monitored", "track_id": "trk-1"}, {"event": "requested", "request_id": "r1"}]
            )
    assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == before
    assert db.record_item_events_bulk([]) == 0
    assert db.record_item_events_bulk(
        [{"event": "monitored", "track_id": "trk-1", "message": "m"}, {"event": "requested", "request_id": "r2",
                                                                      "artist_name": "X"}]
    ) == 2
    assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == before + 2


def test_recycled_lookup_takes_many_prefixes_in_one_pass(db):  # noqa: F811
    for i in range(300):
        db.record_item_event(
            "file_replaced", track_id=f"t{i}", artist_name="A",
            details={"recycled_to": f"/bin/dir{i}/old {i}.flac", "old_path": f"/lib/{i}.flac"},
        )
    db.record_item_event("file_replaced", track_id="tx", details={"recycled_to": None})
    statements: list[str] = []
    db.conn.set_trace_callback(statements.append)
    try:
        found = db.find_recycled_item_events(
            ["/bin/dir1", "/bin/dir2/", "/bin/dir30/old 30.flac", "/bin/dir999", "/bin/dir7/old 7.flac"]
        )
    finally:
        db.conn.set_trace_callback(None)
    assert sorted(e["track_id"] for e in found) == ["t1", "t2", "t30", "t7"]  # dir1 must not match dir10..dir19
    assert len([s for s in statements if "json_extract" in s]) == 1
    assert db.find_recycled_item_events([]) == []
    assert [e["track_id"] for e in db.find_recycled_item_events("/bin/dir5")] == ["t5"]
    assert "_recycled_to" not in found[0]


def test_recycle_purge_records_all_paths_with_one_lookup_and_bulk_write(db):  # noqa: F811
    for i in range(50):
        db.record_item_event(
            "file_replaced", track_id=f"t{i}", details={"recycled_to": f"/bin/d{i}/x.flac", "old_path": f"/lib/{i}"},
        )
    recycle_bin._record_purged_files(db, [f"/bin/d{i}" for i in range(0, 50, 2)], emptied=False)
    rows = db.conn.execute(
        "SELECT track_id, \"trigger\", trigger_label FROM item_events WHERE event = 'file_deleted'"
    ).fetchall()
    assert len(rows) == 25 and {r["trigger"] for r in rows} == {"recycle_cleanup"}
    assert {r["trigger_label"] for r in rows} == {"Recycle bin cleanup"}


# ------------------------------------------------------------------------ 6. API-key principal is no actor
@pytest.mark.parametrize(
    "method,url,service_fn",
    [
        ("post", "/api/activity/queue/dl-1/retry", "native_retry_queue_item"),
        ("post", "/api/activity/history/h-1/failed", "native_mark_history_failed"),
    ],
)
def test_api_key_principal_is_not_persisted_as_actor(db, config, method, url, service_fn):  # noqa: F811
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[require_admin] = lambda: {"id": "api_key_user", "username": "api", "is_admin": True}
    from fastapi.testclient import TestClient

    with patch(f"plex_playlist_sync.activity_service.{service_fn}", return_value={"success": True, "message": "ok"}) as fn:
        assert getattr(TestClient(app), method)(url).status_code == 200
    assert fn.call_args.args[2] is None

    app.dependency_overrides[require_admin] = lambda: {"id": "admin-1", "username": "admin_user", "is_admin": True}
    with patch(f"plex_playlist_sync.activity_service.{service_fn}", return_value={"success": True, "message": "ok"}) as fn:
        assert getattr(TestClient(app), method)(url).status_code == 200
    assert fn.call_args.args[2] == "admin-1"


# ------------------------------------------------------------------------------ 7. scanner provenance
def _scan_meta(path: Path) -> dict[str, Any]:
    return {
        "title": "Come As You Are", "artist": "Nirvana", "album": "Bleach", "track_number": 1, "disc_number": 1,
        "year": 1989, "total_tracks": 1, "duration": 200.0, "codec": "FLAC", "bitrate": 900000, "sample_rate": 44100,
        "bits_per_sample": 16, "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(path.resolve()),
    }


def test_scan_added_album_event_carries_the_scan_trigger(db, tmp_path):  # noqa: F811
    song = tmp_path / "music" / "Nirvana" / "Bleach" / "01 - Come As You Are.flac"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=lambda p: _scan_meta(song)):
        status = LibraryScanner().scan(db, root_folder=str(tmp_path / "music"))
    assert status["albums_created"] == 1
    album = db.get_library_album_by_title(db.get_library_artist_by_name("Nirvana")["id"], "Bleach")
    (ev,) = [e for e in db.list_item_events("album", album["id"]) if e["event"] == "added_to_library"
             and e["album_id"] == album["id"] and e["track_id"] is None]
    assert ev["trigger"] == "scan" and ev["trigger_label"] == "Library scan"


def test_background_scan_thread_and_hydration_thread_keep_the_scan_provenance(db, tmp_path):  # noqa: F811
    song = tmp_path / "music" / "Nirvana" / "Bleach" / "01 - Come As You Are.flac"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    seen: dict[str, Any] = {}
    done = threading.Event()

    def refresh(**_: Any) -> None:
        trig = current_provenance()
        seen["kind"] = trig.kind if trig else None
        done.set()

    scanner = LibraryScanner()
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=lambda p: _scan_meta(song)), patch(
        "plex_playlist_sync.artist_refresh_worker.artist_refresh_worker.refresh_once", side_effect=refresh
    ):
        assert scanner.start_scan(db, root_folder=str(tmp_path / "music"))
        scanner._thread.join(timeout=30)
        assert done.wait(10)
    assert seen["kind"] == "scan"
    album = db.get_library_album_by_title(db.get_library_artist_by_name("Nirvana")["id"], "Bleach")
    kinds = {e["trigger"] for e in db.list_item_events("album", album["id"]) if e["event"] == "added_to_library"}
    assert kinds == {"scan"}
