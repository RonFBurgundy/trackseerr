"""``POST /api/library/{entity}/{id}/reject-current``: blocklist the release behind the files and search again."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library_manager as lm
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.backlog_worker import ReplacementSpec, backlog_worker
from tests.test_governance_and_issues import (  # noqa: F401  (fixtures)
    _auth_headers,
    seeded_users,
    test_config,
    test_db,
)


@pytest.fixture
def client(test_db, test_config, seeded_users):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


@pytest.fixture
def hdr(test_db, test_config, seeded_users):
    return {name: _auth_headers(user, test_db, test_config) for name, user in seeded_users.items()}


def _add_file(db, track_id: str) -> None:
    db.upsert_library_file(
        {
            "id": f"f-{track_id}", "track_id": track_id, "file_path": f"/music/{track_id}.flac",
            "relative_path": f"{track_id}.flac", "codec": "flac", "quality_name": "FLAC", "size_bytes": 1000,
        }
    )


@pytest.fixture
def library(test_db):
    test_db.upsert_library_artist({"id": "ar-1", "name": "The Beatles"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "ar-1", "title": "Abbey Road"})
    for n in (1, 2):
        test_db.upsert_library_track(
            {"id": f"trk-{n}", "album_id": "alb-1", "artist_id": "ar-1", "title": f"Song {n}", "track_number": n}
        )
        _add_file(test_db, f"trk-{n}")
    return "alb-1"


def _record_import(db) -> None:
    db.record_download_event(
        "imported", album_id="alb-1", release_title="Bad.Release.FLAC", info_hash="ABCDEF", indexer="ix",
        protocol="torrent", artist="The Beatles", album="Abbey Road",
    )


def _reject(client, headers, entity, entity_id):
    return client.post(f"/api/library/{entity}/{entity_id}/reject-current", headers=headers)


def test_reject_album_blocklists_source_and_queues_replacement(client, hdr, library, test_db):
    _record_import(test_db)
    with patch.object(backlog_worker, "queue_wanted_search", return_value={"queued": 2}) as queue:
        res = _reject(client, hdr["admin"], "album", "alb-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["blocklisted"] is True and body["release"] == "Bad.Release.FLAC" and body["queued"] == 2
    spec = queue.call_args.kwargs["replacement"]
    assert isinstance(spec, ReplacementSpec) and spec.issue_id is None and spec.require_better is False
    assert sorted(t["track_id"] for t in queue.call_args.args[1]) == ["trk-1", "trk-2"]
    entry = test_db.find_blocklist_entry(release_title="Bad.Release.FLAC")
    assert entry["reason"] == "Rejected from the library"
    # The current files are untouched.
    assert test_db.list_library_files_for_track("trk-1")


def test_reject_track_scopes_to_one_track(client, hdr, library, test_db):
    _record_import(test_db)
    with patch.object(backlog_worker, "queue_wanted_search", return_value={"queued": 1}) as queue:
        res = _reject(client, hdr["admin"], "track", "trk-2")
    assert res.status_code == 200, res.text
    assert [t["track_id"] for t in queue.call_args.args[1]] == ["trk-2"]
    assert queue.call_args.kwargs["replacement"].issue_id is None


def test_reject_track_blocklists_the_tracks_own_release(client, hdr, library, test_db):
    test_db.record_download_event(
        "imported", track_id="trk-2", release_title="Release.B", info_hash="bbbb", artist="The Beatles"
    )
    test_db.record_download_event(
        "imported", album_id="alb-1", release_title="Release.A", info_hash="aaaa", artist="The Beatles"
    )
    with patch.object(backlog_worker, "queue_wanted_search", return_value={"queued": 1}):
        res = _reject(client, hdr["admin"], "track", "trk-2")
    assert res.status_code == 200, res.text
    assert res.json()["release"] == "Release.B"
    assert test_db.find_blocklist_entry(release_title="Release.B") is not None
    assert test_db.find_blocklist_entry(release_title="Release.A") is None


def test_reject_without_source_still_searches(client, hdr, library, test_db):
    with patch.object(backlog_worker, "queue_wanted_search", return_value={"queued": 2}) as queue:
        res = _reject(client, hdr["admin"], "album", "alb-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["blocklisted"] is False and body["release"] is None and body["queued"] == 2
    assert "could not be identified" in body["message"]
    assert queue.called
    assert test_db.list_blocklist() == []


def test_reject_no_files_409(client, hdr, test_db):
    test_db.upsert_library_artist({"id": "ar-1", "name": "A"})
    test_db.upsert_library_album({"id": "alb-9", "artist_id": "ar-1", "title": "Empty"})
    test_db.upsert_library_track(
        {"id": "trk-9", "album_id": "alb-9", "artist_id": "ar-1", "title": "S", "track_number": 1}
    )
    with patch.object(backlog_worker, "queue_wanted_search") as queue:
        assert _reject(client, hdr["admin"], "album", "alb-9").status_code == 409
        assert _reject(client, hdr["admin"], "track", "trk-9").status_code == 409
        assert _reject(client, hdr["admin"], "album", "missing").status_code == 404
    queue.assert_not_called()


def test_reject_lidarr_mode_409(client, hdr, library, test_db):
    lm.switch_mode(test_db, lm.MODE_LIDARR, source="test")
    with patch.object(backlog_worker, "queue_wanted_search") as queue:
        res = _reject(client, hdr["admin"], "album", "alb-1")
    assert res.status_code == 409 and "Lidarr" in res.json()["detail"]
    queue.assert_not_called()


def test_reject_requires_admin(client, hdr, library):
    assert _reject(client, hdr["alice"], "album", "alb-1").status_code == 403
    assert client.post("/api/library/album/alb-1/reject-current").status_code == 401
