"""Per-item audit trail: ``item_events`` storage, grab trigger provenance, redaction and the history API."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import delay_gate, pending_worker
from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator, acquisition_coordinator
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backlog_worker import RSSSyncWorker, WantedBacklogWorker
from plex_playlist_sync.config import Config
from plex_playlist_sync.item_history import (
    ITEM_EVENTS,
    TRIGGER_KINDS,
    GrabTrigger,
    emit,
    provenance,
    redact_details,
    request_trigger,
)
from plex_playlist_sync.models import AcquisitionSearchResult, MusicRequest, RequestStatus
from plex_playlist_sync.storage import SCHEMA_VERSION, Database

HQ = "profile-high-quality"
HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "history.db"))
    yield d
    d.close()


@pytest.fixture
def library(db: Database) -> dict[str, str]:
    db.upsert_library_artist({"id": "art-1", "name": "Nirvana", "clean_name": "nirvana", "monitored": True})
    db.upsert_library_album(
        {"id": "alb-1", "artist_id": "art-1", "title": "Nevermind", "clean_title": "nevermind", "monitored": True}
    )
    db.upsert_library_track(
        {
            "id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Lithium", "clean_title": "lithium",
            "track_number": 3, "disc_number": 1, "monitored": True,
        }
    )
    db.upsert_library_track(
        {
            "id": "trk-2", "album_id": "alb-1", "artist_id": "art-1", "title": "Polly", "clean_title": "polly",
            "track_number": 4, "disc_number": 1, "monitored": True,
        }
    )
    return {"artist": "art-1", "album": "alb-1", "track": "trk-1", "track2": "trk-2"}


@pytest.fixture
def users(db: Database) -> dict[str, dict[str, Any]]:
    return {
        "admin": db.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True),
        "alice": db.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False),
        "bob": db.upsert_user("user-bob", "bob", "bob@example.com", is_admin=False),
    }


@pytest.fixture
def clients(db: Database) -> None:
    db.create_download_client(
        {"id": "c-qbit", "name": "qBit", "driver_type": "qbittorrent", "host_url": "http://q:8080", "enabled": True}
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def headers(user: dict[str, Any], db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def cand(title: str = "Nirvana - Nevermind [FLAC]", did: str = "id-1", extra: dict[str, Any] | None = None):
    return AcquisitionSearchResult(
        download_id=did, title=title, artist="Nirvana", album="Nevermind", size_bytes=300_000_000,
        magnet_url=f"magnet:?xt=urn:btih:{HASH}", source="torznab", protocol="torrent", seeders=10,
        extra=extra or {"indexer_name": "Private Tracker"},
    )


def grab(db: Database, trigger: GrabTrigger, candidates=None, **kw):
    coord = AcquisitionCoordinator()
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch.object(coord, "search_all_indexers", return_value=candidates if candidates is not None else [cand()]), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver
    ):
        return coord.search_and_grab(
            artist="Nirvana", title="Lithium", album="Nevermind", db=db, quality_profile_id=HQ,
            track_id=kw.pop("track_id", "trk-1"), album_id=kw.pop("album_id", "alb-1"), trigger=trigger, **kw
        )


def history_row(db: Database, event: str = "grabbed") -> dict[str, Any]:
    row = db.conn.execute(
        'SELECT event, "trigger", trigger_ref, trigger_label, indexer, protocol FROM download_history '
        "WHERE event = ? ORDER BY rowid DESC LIMIT 1",
        (event,),
    ).fetchone()
    assert row is not None, f"no {event} history row"
    return dict(row)


def events_of(db: Database, entity: str, entity_id: str, event: str) -> list[dict[str, Any]]:
    return [e for e in db.list_item_events(entity, entity_id, limit=500) if e["event"] == event]


# ------------------------------------------------------------------------------------------------------ storage
def test_migration_creates_append_only_table_without_foreign_keys(db: Database):
    assert SCHEMA_VERSION == 63
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 63
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(item_events)").fetchall()}
    assert {
        "id", "event", "artist_id", "album_id", "track_id", "artist_name", "album_title", "track_title", "trigger",
        "trigger_ref", "trigger_label", "actor_user_id", "request_id", "download_id", "message", "details_json",
        "created_at",
    } <= cols
    assert db.conn.execute("PRAGMA foreign_key_list(item_events)").fetchall() == []
    idx = {r[1] for r in db.conn.execute("PRAGMA index_list(item_events)").fetchall()}
    assert {"idx_item_events_track", "idx_item_events_album", "idx_item_events_artist"} <= idx
    for table in ("download_history", "music_requests"):
        have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"trigger", "trigger_ref", "trigger_label"} <= have


def test_record_and_list_round_trip_resolves_parents_and_names(db: Database, library):
    event_id = db.record_item_event(
        "grabbed", track_id="trk-1", trigger="request", trigger_ref="req-1", trigger_label="alice",
        actor_user_id="user-alice", request_id="req-1", download_id="dl-1", message="Grabbed it",
        details={"indexer": "X", "quality": "FLAC"},
    )
    (ev,) = events_of(db, "track", "trk-1", "grabbed")
    assert ev["id"] == event_id
    assert (ev["album_id"], ev["artist_id"]) == ("alb-1", "art-1")
    assert (ev["track_title"], ev["album_title"], ev["artist_name"]) == ("Lithium", "Nevermind", "Nirvana")
    assert (ev["trigger"], ev["trigger_ref"], ev["trigger_label"], ev["actor_user_id"]) == (
        "request", "req-1", "alice", "user-alice",
    )
    assert ev["details"] == {"indexer": "X", "quality": "FLAC"} and ev["message"] == "Grabbed it"
    assert ev["created_at"]


def test_explicit_kwargs_win_over_resolution(db: Database, library):
    db.record_item_event("monitored", track_id="trk-1", track_title="Override", artist_name="Someone Else")
    ev = db.list_item_events("track", "trk-1")[0]
    assert ev["track_title"] == "Override" and ev["artist_name"] == "Someone Else"
    assert ev["album_title"] == "Nevermind"


def test_album_only_event_resolves_artist(db: Database, library):
    db.record_item_event("monitored", album_id="alb-1")
    ev = db.list_item_events("album", "alb-1")[0]
    assert ev["artist_id"] == "art-1" and ev["artist_name"] == "Nirvana" and ev["track_id"] is None


def test_scope_rollup_track_event_appears_in_album_and_artist(db: Database, library):
    db.record_item_event("imported", track_id="trk-1", message="track one")
    db.record_item_event("imported", track_id="trk-2", message="track two")
    db.record_item_event("monitored", album_id="alb-1", message="album only")
    track = [e["message"] for e in events_of(db, "track", "trk-1", "imported")]
    album = {e["message"] for e in db.list_item_events("album", "alb-1")}
    artist = {e["message"] for e in db.list_item_events("artist", "art-1")}
    assert track == ["track one"]
    assert {"track one", "track two", "album only"} <= album
    assert {"track one", "track two", "album only"} <= artist
    assert "track two" not in {e["message"] for e in db.list_item_events("track", "trk-1")}


def test_history_survives_deleting_the_library_rows(db: Database, library):
    db.record_item_event("imported", track_id="trk-1", message="kept")
    assert db.delete_library_track("trk-1")
    assert db.get_library_track("trk-1") is None
    assert "kept" in [e["message"] for e in db.list_item_events("track", "trk-1")]
    removed = events_of(db, "track", "trk-1", "removed_from_library")
    assert removed and removed[0]["track_title"] == "Lithium"  # names were denormalised before the row went
    db.record_item_event("imported", album_id="alb-1", track_id="trk-2", message="album-level")
    assert db.delete_library_artist("art-1")  # cascades albums and tracks
    assert db.get_library_album("alb-1") is None
    msgs = {e["message"] for e in db.list_item_events("artist", "art-1")}
    assert {"kept", "album-level"} <= msgs
    assert {e["artist_name"] for e in db.list_item_events("artist", "art-1")} == {"Nirvana"}


def test_keyset_paging_newest_first(db: Database, library):
    ids = [db.record_item_event("searched", track_id="trk-1", message=f"m{i}") for i in range(7)]
    page1 = events_of(db, "track", "trk-1", "searched")[:0] or db.list_item_events("track", "trk-1", limit=3)
    assert [e["id"] for e in page1] == sorted(
        [e["id"] for e in db.list_item_events("track", "trk-1", limit=500)], reverse=True
    )[:3]
    page2 = db.list_item_events("track", "trk-1", limit=3, before_id=page1[-1]["id"])
    assert max(e["id"] for e in page2) < min(e["id"] for e in page1)
    seen = {e["id"] for e in page1} | {e["id"] for e in page2}
    rest = db.list_item_events("track", "trk-1", limit=500, before_id=page2[-1]["id"])
    assert seen | {e["id"] for e in rest} >= set(ids)
    assert not seen & {e["id"] for e in rest}


def test_unknown_event_is_rejected(db: Database, library):
    with pytest.raises(ValueError):
        db.record_item_event("exploded", track_id="trk-1")
    assert "exploded" not in ITEM_EVENTS
    assert db.conn.execute("SELECT COUNT(*) FROM item_events WHERE event = 'exploded'").fetchone()[0] == 0


def test_recording_failure_is_logged_and_swallowed(caplog):
    broken = MagicMock()
    broken.record_item_event.side_effect = sqlite3.OperationalError("disk I/O error")
    with caplog.at_level(logging.ERROR, logger="plex_playlist_sync.item_history"):
        assert emit(broken, "imported", track_id="t") is None  # does not raise
    assert any("Could not record item event" in r.message and r.exc_info for r in caplog.records)


def test_storage_hook_failure_does_not_break_the_library_write(db: Database, caplog):
    with patch.object(Database, "record_item_event", side_effect=sqlite3.OperationalError("locked")):
        with caplog.at_level(logging.ERROR):
            art = db.upsert_library_artist({"id": "art-x", "name": "X", "clean_name": "x", "monitored": True})
    assert art["id"] == "art-x" and db.get_library_artist("art-x") is not None
    assert any("Could not record item event" in r.message for r in caplog.records)


def test_library_lifecycle_events_with_ambient_provenance(db: Database):
    with provenance(GrabTrigger("scan", label="Library scan")):
        db.upsert_library_artist({"id": "a1", "name": "Pixies", "clean_name": "pixies", "monitored": True})
        db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Doolittle", "clean_title": "doolittle"})
    added = events_of(db, "artist", "a1", "added_to_library")
    assert {e["trigger"] for e in added} == {"scan"} and len(added) == 2  # the artist's and its album's
    db.upsert_library_artist({"id": "a1", "name": "Pixies", "clean_name": "pixies", "monitored": True})
    assert len(events_of(db, "artist", "a1", "added_to_library")) == 2  # an update is not an addition
    db.upsert_library_track(
        {"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Debaser", "clean_title": "debaser"}
    )
    with provenance(GrabTrigger("user", label="admin_user", actor_user_id="admin-1")):
        assert db.set_track_monitored("t1", False)
        assert db.set_track_monitored("t1", False)  # no change, no event
        assert db.set_track_monitored("t1", True)
        db.set_album_monitored("b1", False)
        db.bulk_set_tracks_monitored(["t1"], True)
    kinds = [e["event"] for e in reversed(db.list_item_events("track", "t1", limit=50)) if "monitored" in e["event"]]
    # unmonitor, (duplicate suppressed), monitor, then the album-level change (album event only), bulk monitor
    assert kinds == ["unmonitored", "monitored", "monitored"]
    assert [e["event"] for e in db.list_item_events("album", "b1", limit=50) if e["event"] == "unmonitored"]
    unmon = events_of(db, "track", "t1", "unmonitored")[0]
    assert unmon["trigger"] == "user" and unmon["actor_user_id"] == "admin-1"
    db.delete_library_album("b1")
    assert events_of(db, "album", "b1", "removed_from_library")


# ------------------------------------------------------------------------------------------- trigger threading
@pytest.mark.parametrize(
    "trigger",
    [
        GrabTrigger("request", ref="req-1", label="alice", actor_user_id="user-alice"),
        GrabTrigger("request_approved", ref="req-1", label="alice", actor_user_id="admin-1"),
        GrabTrigger("wanted", label="Wanted search"),
        GrabTrigger("rss", ref="Private Tracker", label="Private Tracker"),
        GrabTrigger("playlist", ref="pl-1", label="Road Trip", actor_user_id="user-alice"),
        GrabTrigger("mix", ref="mix-1", label="Weekly Mix", actor_user_id="user-alice"),
        GrabTrigger("import_list", ref="lst-1", label="ListenBrainz"),
        GrabTrigger("manual", actor_user_id="admin-1"),
        GrabTrigger("issue", ref="issue-1", actor_user_id="admin-1"),
        GrabTrigger("upgrade", label="Quality upgrade"),
        GrabTrigger("retry", actor_user_id="admin-1"),
    ],
    ids=lambda t: t.kind,
)
def test_every_trigger_kind_reaches_download_history_and_the_grabbed_item_event(db, library, clients, trigger):
    assert trigger.kind in TRIGGER_KINDS
    res = grab(db, trigger)
    assert res["success"] is True
    row = history_row(db)
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == (trigger.kind, trigger.ref, trigger.label)
    (ev,) = events_of(db, "track", "trk-1", "grabbed")
    assert (ev["trigger"], ev["trigger_ref"], ev["trigger_label"], ev["actor_user_id"]) == (
        trigger.kind, trigger.ref, trigger.label, trigger.actor_user_id,
    )
    assert ev["download_id"] == res["download_id"]
    assert ev["details"]["indexer"] == "Private Tracker" and ev["details"]["protocol"] == "torrent"
    assert ev["details"]["client"] == "qBit" and ev["details"]["release"] == "Nirvana - Nevermind [FLAC]"
    assert ev["details"]["quality"]
    assert events_of(db, "album", "alb-1", "grabbed") and events_of(db, "artist", "art-1", "grabbed")


def test_trigger_is_required_so_a_missed_caller_fails_loudly(db):
    with pytest.raises(TypeError):
        AcquisitionCoordinator().search_and_grab(artist="A", title="T", db=db)  # type: ignore[call-arg]


def test_upgrade_grab_emits_upgraded_history_row_with_trigger(db, library, clients):
    res = grab(db, GrabTrigger("upgrade"), min_score=0)
    assert res["success"] is True
    assert history_row(db, "upgraded")["trigger"] == "upgrade"


def test_searched_event_records_the_reason_when_nothing_is_found(db, library, clients):
    res = grab(db, GrabTrigger("wanted", label="Wanted search"), candidates=[])
    assert res["success"] is False
    (ev,) = events_of(db, "track", "trk-1", "searched")
    assert ev["trigger"] == "wanted" and "No acceptable releases" in ev["details"]["reason"]
    grab(db, GrabTrigger("wanted", label="Wanted search"), candidates=[])
    assert len(events_of(db, "track", "trk-1", "searched")) == 1  # identical consecutive sweeps do not flood


def test_pending_delay_gate_keeps_the_trigger_until_release(db, library, clients, monkeypatch):
    clock = {"now": T0}
    monkeypatch.setattr(delay_gate, "utcnow", lambda: clock["now"])
    d = db.list_delay_profiles()[-1]
    db.update_delay_profile(
        d["id"],
        {"name": d["name"], "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": 60, "soulseek": 0},
         "bypass_if_highest_quality": False, "bypass_if_above_score": None, "tags": []},
    )
    trig = GrabTrigger("playlist", ref="pl-9", label="Road Trip", actor_user_id="user-alice")
    res = grab(db, trig)
    assert res.get("delayed") is True
    (pending,) = db.list_pending_releases()
    assert pending["payload"]["trigger"] == {
        "kind": "playlist", "ref": "pl-9", "label": "Road Trip", "actor_user_id": "user-alice",
    }
    clock["now"] = T0 + timedelta(minutes=61)
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        out = pending_worker.release_due(db, clock["now"])
    assert out["released"] == 1
    row = history_row(db)
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == ("playlist", "pl-9", "Road Trip")
    (ev,) = events_of(db, "track", "trk-1", "grabbed")
    assert ev["trigger"] == "playlist" and ev["actor_user_id"] == "user-alice"


def test_backlog_worker_threads_wanted_and_upgrade_triggers(db, users):
    db.create_request(
        MusicRequest(
            id="req-w", user_id="user-alice", item_type="album", title="Discovery", artist="Daft Punk",
            album="Discovery", status=RequestStatus.PENDING,
        )
    )
    worker = WantedBacklogWorker()
    worker.pace_delay = 0.01
    mock_grab = MagicMock(return_value={"success": True, "download_id": "dl-1"})
    with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
        worker.poll_once(db)
    assert mock_grab.call_count >= 1
    for call in mock_grab.call_args_list:
        assert isinstance(call.kwargs["trigger"], GrabTrigger) and call.kwargs["trigger"].kind == "wanted"


def test_manual_wanted_search_threads_actor_and_issue_replacement_kind(db, library):
    from plex_playlist_sync.backlog_worker import ReplacementSpec, _search_trigger

    assert _search_trigger(None, None, "admin-1") == GrabTrigger("wanted", label="Wanted search", actor_user_id="admin-1")
    assert _search_trigger(None, 0).kind == "upgrade"
    issue = _search_trigger(ReplacementSpec(issue_id="issue-7"), None, "admin-1")
    assert (issue.kind, issue.ref, issue.actor_user_id) == ("issue", "issue-7", "admin-1")

    worker = WantedBacklogWorker()
    worker.pace_delay = 0
    target = {
        "track_id": "trk-1", "album_id": "alb-1", "artist": "Nirvana", "title": "Lithium", "album": "Nevermind",
        "quality_profile_id": None, "current_quality": None, "cutoff_met": None,
    }
    mock_grab = MagicMock(return_value={"success": True})
    with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
        assert worker.search_wanted_tracks(db, [target], replacement=ReplacementSpec("issue-7"), actor_user_id="admin-1") == 1
        worker.last_search_thread.join(timeout=10)
    trig = mock_grab.call_args.kwargs["trigger"]
    assert (trig.kind, trig.ref, trig.actor_user_id) == ("issue", "issue-7", "admin-1")
    assert mock_grab.call_args.kwargs["replacement_issue_id"] == "issue-7"


def test_rss_worker_grab_carries_the_indexer_as_trigger(db, library, users, clients):
    db.create_indexer(
        {"id": "idx-1", "name": "Private Tracker", "indexer_type": "torznab", "host_url": "http://127.0.0.1:9696",
         "enabled": True}
    )
    db.create_request(
        MusicRequest(
            id="req-rss", user_id="user-bob", item_type="album", title="Nevermind", artist="Nirvana",
            album="Nevermind", status=RequestStatus.PENDING,
        )
    )
    match = AcquisitionSearchResult(
        download_id="dl-n", title="Nirvana - Nevermind (1991) [FLAC 16bit]", artist="Nirvana", album="Nevermind",
        size_bytes=420000000, protocol="torrent", download_url="magnet:?xt=urn:btih:abc123",
        extra={"indexer_name": "Private Tracker"},
    )
    idx = MagicMock()
    idx.fetch_recent.return_value = [match]
    cl = MagicMock()
    cl.download.return_value = "abc123"
    with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=idx), patch(
        "plex_playlist_sync.backlog_worker.get_acquisition_driver", return_value=cl
    ):
        stats = RSSSyncWorker().poll_once(db)
    assert stats["grabs_triggered"] == 1
    row = history_row(db)
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == ("rss", "Private Tracker", "Private Tracker")


def test_request_submission_threads_request_trigger_with_username_label(db, users, clients):
    from plex_playlist_sync.request_submission import submit_track_request

    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=True)
    mock_grab = MagicMock(return_value={"success": True})
    with patch("plex_playlist_sync.request_submission.native_is_configured", return_value=True), patch.object(
        acquisition_coordinator, "search_and_grab", mock_grab
    ):
        sub = submit_track_request(db, cfg, users["alice"] | {"permissions": 0xFFFF}, "Lithium", "Nirvana", "Nevermind")
    trig = mock_grab.call_args.kwargs["trigger"]
    assert (trig.kind, trig.label, trig.ref, trig.actor_user_id) == (
        "request", "alice", sub.request["id"], "user-alice",
    )


def test_system_sourced_requests_keep_their_trigger_through_the_request(db, users, clients):
    from plex_playlist_sync.request_submission import submit_track_request

    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=True)
    src = GrabTrigger("import_list", ref="lst-1", label="ListenBrainz Weekly", actor_user_id="admin-1")
    mock_grab = MagicMock(return_value={"success": True})
    admin = users["admin"] | {"permissions": 0xFFFF}
    with patch("plex_playlist_sync.request_submission.native_is_configured", return_value=True), patch.object(
        acquisition_coordinator, "search_and_grab", mock_grab
    ):
        sub = submit_track_request(db, cfg, admin, "Lithium", "Nirvana", "Nevermind", trigger=src)
    row = db.get_request(sub.request["id"])
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == ("import_list", "lst-1", "ListenBrainz Weekly")
    trig = mock_grab.call_args.kwargs["trigger"]
    assert (trig.kind, trig.ref, trig.label) == ("import_list", "lst-1", "ListenBrainz Weekly")


def test_mix_acquisition_request_carries_the_mix_trigger(db, users):
    from plex_playlist_sync import tailored_mixes
    from plex_playlist_sync.models import UserPermission  # noqa: F401  (documents that permissions come from the row)

    user = users["alice"] | {"permissions": 0xFFFF}
    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=True)
    track = MagicMock(title="Lithium", artist="Nirvana", album="Nevermind")
    mix = db.create_mix_config("user-alice", "discover_weekly", "Weekly Mix")
    with patch("plex_playlist_sync.request_submission.native_is_configured", return_value=False):
        sub = tailored_mixes._queue_acquisition(db, cfg, mix, user, track)
    assert sub is not None
    row = db.get_request(sub.request["id"])
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == ("mix", mix["id"], "Weekly Mix")
    trig = request_trigger(db, row)
    assert (trig.kind, trig.ref, trig.label, trig.actor_user_id) == ("mix", mix["id"], "Weekly Mix", "user-alice")


def test_import_list_and_playlist_tracks_become_requests_with_their_trigger(db, users):
    from plex_playlist_sync import list_monitoring

    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=True)
    item = list_monitoring.ListItem(kind="track", artist_name="Nirvana", album_title="Nevermind", track_title="Lithium")
    trig = GrabTrigger("import_list", ref="lst-1", label="ListenBrainz", actor_user_id="admin-1")
    with patch("plex_playlist_sync.request_submission.native_is_configured", return_value=False):
        result = list_monitoring.apply_list_item(
            db, cfg, item, "track", requested_by=users["admin"] | {"permissions": 0xFFFF}, trigger=trig
        )
    assert result.status == list_monitoring.STATUS_APPLIED
    (req,) = db.list_requests()
    row = db.get_request(req["id"])
    assert (row["trigger"], row["trigger_ref"], row["trigger_label"]) == ("import_list", "lst-1", "ListenBrainz")


def test_route_callers_assign_request_approved_retry_manual_and_playlist(db, users, clients, client, config):
    admin_h = headers(users["admin"], db, config)
    db.upsert_playlist("pl-1", "Road Trip", "spotify")
    db.record_sync_result(
        playlist_id="pl-1", status="success",
        missing_tracks=[{"title": "Lithium", "artist": "Nirvana", "album": "Nevermind", "url": "http://x"}],
    )
    missing_id = db.get_missing_tracks()[0]["id"]
    req = db.create_request(
        MusicRequest(
            id="req-r", user_id="user-alice", item_type="track", title="Lithium", artist="Nirvana", album="Nevermind",
            status=RequestStatus.PENDING,
        )
    )
    mock_grab = MagicMock(return_value={"success": False, "message": "none"})
    with patch("plex_playlist_sync.api.routes.requests.native_is_configured", return_value=True), patch.object(
        acquisition_coordinator, "search_and_grab", mock_grab
    ):
        assert client.post(f"/api/requests/{req['id']}/approve", headers=admin_h).status_code == 200
        approve = mock_grab.call_args.kwargs["trigger"]
        assert client.post(f"/api/requests/{req['id']}/retry", headers=admin_h).status_code == 200
        retry = mock_grab.call_args.kwargs["trigger"]
        assert client.post(f"/api/missing/{missing_id}/grab", headers=admin_h).status_code == 200
        playlist = mock_grab.call_args.kwargs["trigger"]
    assert (approve.kind, approve.label, approve.ref, approve.actor_user_id) == (
        "request_approved", "alice", "req-r", "admin-1",
    )
    assert (retry.kind, retry.label, retry.actor_user_id) == ("retry", "alice", "admin-1")
    assert (playlist.kind, playlist.ref, playlist.label, playlist.actor_user_id) == (
        "playlist", "pl-1", "Road Trip", "admin-1",
    )
    kinds = [e["event"] for e in db.conn.execute("SELECT event FROM item_events").fetchall()]
    assert "request_approved" in kinds


def test_queue_retry_uses_retry_trigger_with_admin_actor(db, users, library, clients, client, config):
    admin_h = headers(users["admin"], db, config)
    from plex_playlist_sync.models import ActiveDownload, DownloadStatus

    db.create_active_download(
        ActiveDownload(
            id="dl-stuck", title="Nirvana - Nevermind", artist="Nirvana", client_id="c-qbit", download_hash="h1",
            status=DownloadStatus.DOWNLOADING.value, track_id="trk-1", album_id="alb-1",
        )
    )
    mock_grab = MagicMock(return_value={"success": False, "message": "nothing"})
    with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
        client.post("/api/activity/queue/dl-stuck/retry", headers=admin_h)
    trig = mock_grab.call_args.kwargs["trigger"]
    assert (trig.kind, trig.actor_user_id) == ("retry", "admin-1")


def test_interactive_grab_route_records_manual_trigger_with_admin_actor(db, users, library, clients, client, config):
    admin_h = headers(users["admin"], db, config)
    driver = MagicMock()
    driver.download.return_value = HASH
    payload = {
        "release": {
            "id": "rel-1", "title": "Nirvana - Nevermind [FLAC]", "size_bytes": 1000, "protocol": "torrent",
            "indexer_name": "Private Tracker", "parsed_quality": "FLAC 16bit", "is_acceptable": True, "score": 900,
            "meets_cutoff": True, "magnet_url": f"magnet:?xt=urn:btih:{HASH}",
        },
        "artist": "Nirvana", "title": "Lithium", "album": "Nevermind", "track_id": "trk-1", "album_id": "alb-1",
    }
    with patch("plex_playlist_sync.api.routes.acquisition.get_acquisition_driver", return_value=driver):
        resp = client.post("/api/acquisition/grab", json=payload, headers=admin_h)
    assert resp.status_code == 200, resp.text
    row = history_row(db)
    assert row["trigger"] == "manual"
    (ev,) = events_of(db, "track", "trk-1", "grabbed")
    assert ev["trigger"] == "manual" and ev["actor_user_id"] == "admin-1"


# ------------------------------------------------------------------------------------------------- redaction unit
def test_redact_details_drops_indexer_client_hash_and_paths():
    raw = {
        "indexer": "Private Tracker", "client": "qBit", "protocol": "torrent", "info_hash": "abc", "release_guid": "g",
        "quality": "FLAC", "release": "Nirvana - Nevermind",
        "path": "/music/x.flac", "old_path": "/music/old.flac", "note": "failed at /downloads/a/b.flac today",
        "nested": {"recycled_to": "/rec/x", "keep": 1, "other": "C:\\Music\\a.flac"},
        "files": [{"file": "/a/b.mp3"}],
    }
    out = redact_details(raw)
    assert out["quality"] == "FLAC" and "release" not in out
    for gone in ("indexer", "client", "protocol", "info_hash", "release_guid", "path", "old_path", "files"):
        assert gone not in out
    assert "note" not in out  # a string with a path separator is dropped whole, not scrubbed
    assert out["nested"] == {"keep": 1}


# --------------------------------------------------------------------------------------------------------- API
def _seed_history(db: Database) -> None:
    db.record_item_event(
        "requested", track_id="trk-1", trigger="request", trigger_ref="req-1", trigger_label="alice",
        actor_user_id="user-alice", request_id="req-1", message="Requested",
    )
    db.record_item_event(
        "grabbed", track_id="trk-1", trigger="request", trigger_ref="req-1", trigger_label="alice",
        actor_user_id="user-alice", message="Grabbed",
        details={"indexer": "Private Tracker", "client": "qBit", "protocol": "torrent", "info_hash": "abc",
                 "release_guid": "g-1", "quality": "FLAC", "release": "Nirvana - Nevermind [FLAC]"},
    )
    db.record_item_event(
        "imported", track_id="trk-1", trigger="request", trigger_label="alice", actor_user_id="user-alice",
        message="Imported 03 - Lithium.flac",
        details={"path": "/music/Nirvana/Nevermind/03 - Lithium.flac", "quality": "FLAC", "codec": "FLAC"},
    )
    db.record_item_event(
        "grabbed", track_id="trk-2", trigger="rss", trigger_ref="Private Tracker", trigger_label="Private Tracker",
        message="Grabbed", details={"indexer": "Private Tracker"},
    )
    db.record_item_event(
        "grabbed", track_id="trk-2", trigger="request", trigger_label="bob", actor_user_id="user-bob",
        message="Grabbed for bob",
    )
    db.record_item_event(
        "grabbed", track_id="trk-2", trigger="playlist", trigger_ref="pl-b", trigger_label="Bob's private playlist",
        actor_user_id="user-bob", message="Grabbed",
    )


def test_history_api_admin_sees_everything(db, users, library, client, config):
    _seed_history(db)
    r = client.get("/api/library/track/trk-1/history", headers=headers(users["admin"], db, config))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entity"] == "track" and body["entity_id"] == "trk-1" and body["next_before"] is None
    assert [e["event"] for e in body["events"]][:3] == ["imported", "grabbed", "requested"]
    grabbed = next(e for e in body["events"] if e["event"] == "grabbed")
    assert grabbed["details"]["indexer"] == "Private Tracker" and grabbed["details"]["info_hash"] == "abc"
    assert grabbed["actor_display"] == "alice" and grabbed["trigger"] == "request"
    imported = next(e for e in body["events"] if e["event"] == "imported")
    assert imported["details"]["path"].startswith("/music/")
    assert set(grabbed) == {
        "id", "event", "created_at", "track_id", "album_id", "artist_id", "track_title", "album_title", "artist_name",
        "trigger", "trigger_label", "actor_display", "message", "details",
    }
    assert grabbed["created_at"].endswith("Z") and "T" in grabbed["created_at"]
    assert set(body["origin"]) == {"trigger", "trigger_label", "actor_display", "created_at"}
    assert body["origin"]["trigger"] == "request" and body["origin"]["actor_display"] == "alice"


def test_history_api_non_admin_gets_redacted_details_and_another_user(db, users, library, client, config):
    _seed_history(db)
    alice = client.get("/api/library/track/trk-1/history", headers=headers(users["alice"], db, config)).json()
    for ev in alice["events"]:
        blob = str(ev["details"]) + ev["message"]
        for secret in ("Private Tracker", "qBit", "torrent", "abc", "g-1", "/music/"):
            assert secret not in blob
    grabbed = next(e for e in alice["events"] if e["event"] == "grabbed")
    assert grabbed["details"] == {"quality": "FLAC"}  # the release title is hidden from non-admins
    assert grabbed["actor_display"] == "alice" and grabbed["trigger_label"] == "alice"  # her own request
    imported = next(e for e in alice["events"] if e["event"] == "imported")
    assert imported["details"] == {"quality": "FLAC", "codec": "FLAC"}

    bob_view = client.get("/api/library/track/trk-1/history", headers=headers(users["bob"], db, config)).json()
    for ev in bob_view["events"]:
        assert ev["actor_display"] == "another user" and ev["trigger_label"] == "another user"
        assert "alice" not in str(ev)
    assert bob_view["origin"]["actor_display"] == "another user" and "alice" not in str(bob_view["origin"])

    track2 = client.get("/api/library/track/trk-2/history", headers=headers(users["alice"], db, config)).json()
    by_trigger = {e["trigger"]: e for e in track2["events"]}
    rss = by_trigger["rss"]
    assert rss["trigger_label"] == "RSS sync" and rss["actor_display"] == "RSS sync"  # the indexer name never leaks
    assert "Private Tracker" not in str(rss)
    assert by_trigger["playlist"]["trigger_label"] is None  # another user's playlist name is dropped
    assert by_trigger["playlist"]["actor_display"] == "another user"
    assert "Bob" not in str(track2["events"])


def test_history_api_album_and_artist_roll_up_and_page(db, users, library, client, config):
    _seed_history(db)
    admin_h = headers(users["admin"], db, config)
    album = client.get("/api/library/album/alb-1/history?limit=500", headers=admin_h).json()
    assert {e["track_id"] for e in album["events"] if e["event"] == "grabbed"} == {"trk-1", "trk-2"}
    artist_events = client.get("/api/library/artist/art-1/history?limit=500", headers=admin_h).json()["events"]
    assert len(artist_events) >= len(album["events"])
    seen: list[int] = []
    before = None
    while True:
        url = "/api/library/artist/art-1/history?limit=4" + (f"&before={before}" if before else "")
        page = client.get(url, headers=admin_h).json()
        assert len(page["events"]) <= 4
        seen += [e["id"] for e in page["events"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == sorted(seen, reverse=True) and len(seen) == len(set(seen)) == len(artist_events)
    assert client.get("/api/library/artist/art-1/history?limit=0", headers=admin_h).status_code == 422


def test_history_api_404_unknown_entity_but_deleted_item_with_history_is_served(db, users, library, client, config):
    admin_h = headers(users["admin"], db, config)
    assert client.get("/api/library/track/nope/history", headers=admin_h).status_code == 404
    assert client.get("/api/library/album/nope/history", headers=admin_h).status_code == 404
    assert client.get("/api/library/bogus/trk-1/history", headers=admin_h).status_code == 422
    assert client.get("/api/library/track/trk-1/history").status_code in (401, 403)
    db.record_item_event("imported", track_id="trk-1", message="before deletion")
    db.delete_library_track("trk-1")
    r = client.get("/api/library/track/trk-1/history", headers=admin_h)
    assert r.status_code == 200 and "before deletion" in [e["message"] for e in r.json()["events"]]


def test_history_api_origin_is_the_earliest_origin_event_in_scope(db, users, library, client, config):
    db.record_item_event("monitored", track_id="trk-2", message="not an origin")
    db.record_item_event("grabbed", track_id="trk-2", trigger="rss", trigger_label="Tracker", message="first")
    db.record_item_event("requested", track_id="trk-1", trigger="request", trigger_label="alice", actor_user_id="user-alice")
    body = client.get("/api/library/album/alb-1/history", headers=headers(users["admin"], db, config)).json()
    # added_to_library from the fixtures' upserts is the earliest origin event of the album scope
    assert body["origin"]["created_at"]
    earliest = db.earliest_item_event("album", "alb-1")
    assert earliest is not None and earliest["event"] in ("added_to_library", "requested", "grabbed")
    assert earliest["id"] == min(
        e["id"] for e in db.list_item_events("album", "alb-1", limit=500)
        if e["event"] in ("added_to_library", "requested", "grabbed")
    )
