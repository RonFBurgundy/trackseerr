"""Tests for AcoustID fingerprint contribution, verification, and API endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

import acoustid
from trackseerr.acoustid_contrib import (
    contribute_fingerprints,
    refresh_pending_submissions,
    run_acoustid_contribution,
)
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library import FingerprintUnavailable
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    db_file = tmp_path / "acoustid_test.db"
    database = Database(str(db_file))
    yield database
    database.close()


def _seed_track_and_file(
    db: Database,
    idx: int,
    mbid: str | None = "mbid-rec-1",
    duration: float = 180.0,
    bitrate: int | None = 900,
    codec: str = "FLAC",
) -> tuple[str, str]:
    db.upsert_library_artist({
        "id": f"art-{idx:05d}",
        "name": f"Artist {idx}",
        "monitored": True,
    })
    db.upsert_library_album({
        "id": f"alb-{idx:05d}",
        "artist_id": f"art-{idx:05d}",
        "title": f"Album {idx}",
        "year": 2020,
        "monitored": True,
    })
    db.upsert_library_track({
        "id": f"trk-{idx:05d}",
        "album_id": f"alb-{idx:05d}",
        "artist_id": f"art-{idx:05d}",
        "title": f"Track {idx}",
        "track_number": 1,
        "duration_seconds": duration,
        "mb_recording_id": mbid,
        "monitored": True,
    })
    db.upsert_library_file({
        "id": f"file-{idx:05d}",
        "track_id": f"trk-{idx:05d}",
        "file_path": f"/music/file_{idx:05d}.flac",
        "relative_path": f"file_{idx:05d}.flac",
        "codec": codec,
        "bitrate": bitrate,
    })
    return f"trk-{idx:05d}", f"file-{idx:05d}"


def test_missing_user_key_does_nothing(db: Database) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-key",
        "acoustid_user_key": None,
    })
    _seed_track_and_file(db, 1)

    res = contribute_fingerprints(db)
    assert res == {"missing_keys": 1}

    run_res = run_acoustid_contribution(db)
    assert run_res == {"missing_keys": 1}

    # Missing API key
    db.update_media_management_settings({
        "acoustid_api_key": None,
        "acoustid_user_key": "user-key",
    })
    assert contribute_fingerprints(db) == {"missing_keys": 1}
    assert refresh_pending_submissions(db) == {"missing_keys": 1}
    assert run_acoustid_contribution(db) == {"missing_keys": 1}


def test_submits_unlinked_fingerprint_with_mbid(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(
        db, 1, mbid="mbid-target-1", duration=200.0, bitrate=900, codec="FLAC"
    )

    fake_fp = "AQAAZEmiZUmiRUqU"
    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 200.2,
            "fingerprint": fake_fp,
            "matches": [],
        },
    )

    submitted_data: list[Any] = []

    def fake_submit(
        apikey: str, userkey: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        assert apikey == "api-1"
        assert userkey == "user-1"
        submitted_data.extend(data)
        return {
            "status": "ok",
            "submissions": [{"index": 0, "id": 5555, "status": "pending"}],
        }

    monkeypatch.setattr(acoustid, "submit", fake_submit)

    res = contribute_fingerprints(db)
    assert res["submitted"] == 1
    assert len(submitted_data) == 1
    assert submitted_data[0]["fingerprint"] == fake_fp
    assert submitted_data[0]["duration"] == 200
    assert submitted_data[0]["mbid"] == "mbid-target-1"
    assert submitted_data[0]["fileformat"] == "FLAC"
    assert submitted_data[0]["bitrate"] == 900

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "pending"
    assert row["submission_id"] == 5555
    expected_hash = hashlib.sha1(fake_fp.encode("utf-8")).hexdigest()
    assert row["fingerprint_hash"] == expected_hash
    assert row["submitted_at"] is not None


def test_already_linked_skipped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-linked-1")

    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": "AQAA...",
            "matches": [{"recording_id": "mbid-linked-1", "score": 0.95}],
        },
    )

    mock_submit = MagicMock()
    monkeypatch.setattr(acoustid, "submit", mock_submit)

    res = contribute_fingerprints(db)
    assert res["already_linked"] == 1
    assert mock_submit.call_count == 0

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "skipped"
    assert row["reason"] == "already_linked"


def test_conflicting_match_skipped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-our-track")

    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": "AQAA...",
            "matches": [{"recording_id": "mbid-other-track", "score": 0.93}],
        },
    )

    mock_submit = MagicMock()
    monkeypatch.setattr(acoustid, "submit", mock_submit)

    res = contribute_fingerprints(db)
    assert res["conflicting_match"] == 1
    assert mock_submit.call_count == 0

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "skipped"
    assert row["reason"] == "conflicting_match"


def test_duration_mismatch_skipped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-dur-1", duration=180.0)

    # 195s vs 180s = diff 15s > 7s
    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 195.0,
            "fingerprint": "AQAA...",
            "matches": [],
        },
    )

    mock_submit = MagicMock()
    monkeypatch.setattr(acoustid, "submit", mock_submit)

    res = contribute_fingerprints(db)
    assert res["duration_mismatch"] == 1
    assert mock_submit.call_count == 0

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "skipped"
    assert row["reason"] == "duration_mismatch"


def test_fingerprint_error_skipped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-err-1")

    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "error",
            "error": "file missing",
            "cached": False,
        },
    )

    mock_submit = MagicMock()
    monkeypatch.setattr(acoustid, "submit", mock_submit)

    res = contribute_fingerprints(db)
    assert res["fingerprint_error"] == 1
    assert mock_submit.call_count == 0

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "skipped"
    assert row["reason"] == "fingerprint_error"


def test_batches_of_50(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    file_ids = []
    for i in range(120):
        _, fid = _seed_track_and_file(db, i, mbid=f"mbid-{i}", duration=180.0)
        file_ids.append(fid)

    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": f"FP-{cand['id']}",
            "matches": [],
        },
    )

    submit_calls: list[list[dict[str, Any]]] = []

    def fake_submit(
        apikey: str, userkey: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        submit_calls.append(list(data))
        batch_num = len(submit_calls)
        sub_list = [
            {"index": i, "id": batch_num * 1000 + i, "status": "pending"}
            for i in range(len(data))
        ]
        return {"status": "ok", "submissions": sub_list}

    monkeypatch.setattr(acoustid, "submit", fake_submit)

    res = contribute_fingerprints(db, limit=200)
    assert res["submitted"] == 120
    assert len(submit_calls) == 3
    assert len(submit_calls[0]) == 50
    assert len(submit_calls[1]) == 50
    assert len(submit_calls[2]) == 20

    # Ensure every file has a submission row
    counts = db.count_acoustid_submissions()
    assert counts == {"pending": 120}

    # Verify indexes mapped correctly
    first_sub = db.get_acoustid_submission(file_ids[0])
    assert first_sub is not None
    assert first_sub["submission_id"] == 1000

    last_sub = db.get_acoustid_submission(file_ids[119])
    assert last_sub is not None
    assert last_sub["submission_id"] == 3019


def test_submit_error_marks_batch_error_and_retries_after_7_days(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-retry-1")

    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": "FP-1",
            "matches": [],
        },
    )

    def fail_submit(
        apikey: str, userkey: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise acoustid.WebServiceError("server is unavailable")

    monkeypatch.setattr(acoustid, "submit", fail_submit)

    res = contribute_fingerprints(db)
    assert res["errors"] == 1

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "error"
    assert "server is unavailable" in str(row["reason"])

    # Right now, candidate iteration must not return this file
    candidates = list(db.iter_acoustid_candidates())
    assert len(candidates) == 0

    # Simulate 8 days passing by updating checked_at
    past_iso = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    db.upsert_acoustid_submission({**row, "checked_at": past_iso})

    # Now it should be returned as a candidate again
    candidates_retried = list(db.iter_acoustid_candidates())
    assert len(candidates_retried) == 1
    assert candidates_retried[0]["id"] == fid


def test_refresh_pending_marks_imported(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid = _seed_track_and_file(db, 1, mbid="mbid-pending-1")

    db.upsert_acoustid_submission({
        "file_id": fid,
        "recording_mbid": "mbid-pending-1",
        "fingerprint_hash": "hash-pending",
        "submission_id": 777,
        "status": "pending",
        "reason": None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": None,
    })

    def fake_get_status(apikey: str, sub_id: int) -> dict[str, Any]:
        assert apikey == "api-1"
        assert sub_id == 777
        return {
            "status": "ok",
            "submissions": [
                {
                    "id": 777,
                    "status": "imported",
                    "result": {"id": "acoustid-guid"},
                }
            ],
        }

    monkeypatch.setattr(acoustid, "get_submission_status", fake_get_status)

    res = refresh_pending_submissions(db)
    assert res["imported"] == 1

    row = db.get_acoustid_submission(fid)
    assert row is not None
    assert row["status"] == "imported"
    assert row["checked_at"] is not None


def test_should_stop_honored(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    for i in range(10):
        _seed_track_and_file(db, i, mbid=f"mbid-{i}")

    verified_count = 0

    def fake_verify(
        database: Any, cand: dict[str, Any], key: str
    ) -> dict[str, Any]:
        nonlocal verified_count
        verified_count += 1
        return {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": "FP",
            "matches": [],
        }

    monkeypatch.setattr("trackseerr.acoustid_contrib.verify_file", fake_verify)
    monkeypatch.setattr(
        acoustid,
        "submit",
        lambda apikey, userkey, data: {
            "status": "ok",
            "submissions": [{"index": 0, "id": 1, "status": "pending"}],
        },
    )

    call_count = 0

    def stop_after_two() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2

    res = contribute_fingerprints(db, should_stop=stop_after_two)
    assert verified_count == 2
    assert res["submitted"] == 2


def test_fpcalc_unavailable_stops_run(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    for i in range(5):
        _seed_track_and_file(db, i, mbid=f"mbid-{i}")

    def fail_verify(
        database: Any, cand: dict[str, Any], key: str
    ) -> dict[str, Any]:
        raise FingerprintUnavailable("chromaprint fpcalc binary not found")

    monkeypatch.setattr("trackseerr.acoustid_contrib.verify_file", fail_verify)

    res = contribute_fingerprints(db)
    assert res.get("fpcalc_unavailable") == 1
    assert res.get("submitted", 0) == 0


def test_run_acoustid_contribution_merges_counts(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.update_media_management_settings({
        "acoustid_api_key": "api-1",
        "acoustid_user_key": "user-1",
    })
    _, fid1 = _seed_track_and_file(db, 1, mbid="mbid-pending")
    _, fid2 = _seed_track_and_file(db, 2, mbid="mbid-new")

    db.upsert_acoustid_submission({
        "file_id": fid1,
        "recording_mbid": "mbid-pending",
        "fingerprint_hash": "hash-1",
        "submission_id": 123,
        "status": "pending",
        "reason": None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": None,
    })

    monkeypatch.setattr(
        acoustid,
        "get_submission_status",
        lambda key, sub_id: {
            "status": "ok",
            "submissions": [{"id": sub_id, "status": "imported"}],
        },
    )
    monkeypatch.setattr(
        "trackseerr.acoustid_contrib.verify_file",
        lambda database, cand, key: {
            "status": "ok",
            "duration": 180.0,
            "fingerprint": "FP-2",
            "matches": [],
        },
    )
    monkeypatch.setattr(
        acoustid,
        "submit",
        lambda apikey, userkey, data: {
            "status": "ok",
            "submissions": [{"index": 0, "id": 456, "status": "pending"}],
        },
    )

    merged = run_acoustid_contribution(db)
    assert merged["imported"] == 1
    assert merged["submitted"] == 1
    assert merged["errors"] == 0


def test_contributions_endpoint_counts_admin_only(tmp_path: Path) -> None:
    test_db = Database(str(tmp_path / "endpoint_test.db"))
    sec_key = get_or_create_secret_key(data_dir=str(tmp_path))
    test_db.upsert_user("admin-1", "admin", "admin@example.com", is_admin=True)
    test_db.upsert_user("alice-1", "alice", "alice@example.com", is_admin=False)
    admin_token = create_session_token(
        "admin-1", "admin", is_admin=True, secret_key=sec_key
    )
    alice_token = create_session_token(
        "alice-1", "alice", is_admin=False, secret_key=sec_key
    )
    test_db.create_session(admin_token, "admin-1")
    test_db.create_session(alice_token, "alice-1")

    cfg = Config(
        plex_url="http://plex.invalid:32400",
        plex_token="t",
        data_dir=str(tmp_path),
    )
    app = create_app(db=test_db, config=cfg)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)

    test_db.update_media_management_settings({
        "acoustid_user_key": "secret-user-key"
    })
    _, fid1 = _seed_track_and_file(test_db, 1)
    _, fid2 = _seed_track_and_file(test_db, 2)

    test_db.upsert_acoustid_submission({
        "file_id": fid1,
        "recording_mbid": "mbid-1",
        "fingerprint_hash": "h1",
        "submission_id": 10,
        "status": "pending",
        "reason": None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": None,
    })
    test_db.upsert_acoustid_submission({
        "file_id": fid2,
        "recording_mbid": "mbid-2",
        "fingerprint_hash": "h2",
        "submission_id": 11,
        "status": "imported",
        "reason": None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": None,
    })

    # Admin request succeeds
    res_admin = client.get(
        "/api/library/acoustid/contributions",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin.status_code == 200
    data = res_admin.json()
    assert data["has_user_key"] is True
    assert data["counts"] == {"pending": 1, "imported": 1}

    # Non-admin request gets 403 Forbidden
    res_alice = client.get(
        "/api/library/acoustid/contributions",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert res_alice.status_code == 403
    test_db.close()
