"""Replay the live 20-row held queue as a regression test."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import struct
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.backlog_worker import WantedBacklogWorker, _SweepCollect
from trackseerr.models import (
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    RequestStatus,
)
from trackseerr.storage import Database

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "replay_queue_2026_10_18.json.gz"


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


def _setup_replay_db(tmp_path: Path) -> tuple[Database, Path, Path, dict[str, Any]]:
    db = Database(str(tmp_path / "replay.db"))
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.upsert_user("u1", "user", "u@example.com", is_admin=True)
    db.update_media_management_settings(
        {
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
            "library_mode": "native",
            "enrich_mbids": False,
            "write_audio_tags": False,
        }
    )
    db.create_download_client(
        DownloadClientConfig(
            id="client-1",
            name="slskd",
            driver_type=DownloadDriverType.SLSKD.value,
            host_url="http://localhost:5030",
            enabled=True,
        )
    )

    with gzip.open(FIXTURE_PATH, "rt") as f:
        data = json.load(f)

    # Rewrite downloads before inserting
    held_tags: dict[str, dict[str, Any]] = data["held_file_tags"]
    for dl in data["active_downloads"]:
        dl_id = dl["id"]
        files = json.loads(dl["unmatched_files"])
        new_files = []
        for fpath in files:
            bname = Path(fpath).name
            held_file = tmp_path / "held" / dl_id / bname
            _flac(held_file)
            new_files.append(str(held_file))
        dl["unmatched_files"] = json.dumps(new_files)
        dl["source_path"] = str(tmp_path / "held" / dl_id)

    order = [
        ("library_artists", "library_artists"),
        ("library_albums", "library_albums"),
        ("library_tracks", "library_tracks"),
        ("library_files", "library_files"),
        ("music_requests", "music_requests"),
        ("active_downloads", "active_downloads"),
    ]
    for key, table in order:
        rows = data[key]
        if not rows:
            continue
        cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for row in rows:
            matched_keys = [k for k in row.keys() if k in cols]
            placeholders = ", ".join(["?"] * len(matched_keys))
            col_names = ", ".join(matched_keys)
            vals = [row[k] for k in matched_keys]
            db.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                vals,
            )
    db.conn.commit()
    return db, music, staging, held_tags


def _run_replay_poll(db: Database, staging: Path, held_tags: dict[str, dict[str, Any]]) -> dict[str, int]:
    def mock_inspect(path: Any) -> dict[str, Any]:
        p = Path(path)
        dl_id = p.parent.name
        tags = held_tags.get(dl_id, {})
        dur = float(tags.get("duration", 0.0))
        trk = int(tags.get("track", 1))
        return {
            "title": tags.get("title", ""),
            "artist": tags.get("artist", ""),
            "album": tags.get("album", ""),
            "duration": dur,
            "duration_seconds": dur,
            "track_number": trk,
            "disc_number": 1,
            "codec": "FLAC",
            "extension": ".flac",
            "bits_per_sample": 16,
            "sample_rate": 44100,
            "bitrate": 900,
        }

    driver = MagicMock()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=mock_inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=mock_inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=mock_inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        worker = AcquisitionWorker()
        stats = worker.poll_once(db=db, staging_dir=str(staging))
    return stats


@pytest.fixture(scope="module")
def replay_env(tmp_path_factory: pytest.TempPathFactory):
    td = tmp_path_factory.mktemp("replay_module")
    db, music, staging, held_tags = _setup_replay_db(td)
    _run_replay_poll(db, staging, held_tags)
    yield db, staging, held_tags
    db.close()


ROW_OUTCOME_CASES = [
    # Elvis "The Beat"/"You Belong to Me"/"Hand in Hand", owned under "Elvis Costello"
    pytest.param("dl-0d06471309b5", "imported", "Already in library — duplicate discarded", "available", False, id="dl-0d06471309b5"),
    pytest.param("dl-59b18b9ab644", "imported", "Already in library — duplicate discarded", "available", False, id="dl-59b18b9ab644"),
    pytest.param("dl-126f7af24aeb", "imported", "Already in library — duplicate discarded", "available", False, id="dl-126f7af24aeb"),
    # Who "Remix" request, file is original album version you own
    pytest.param("dl-29c8bfaf93e6", "imported", "Discarded a file that is a different song you already own", "processing", True, id="dl-29c8bfaf93e6"),
    # CSN request, file tagged "David Crosby", song owned under David Crosby
    pytest.param("dl-1b0c5cd4fae5", "imported", "Discarded a file that is a different song you already own", "processing", True, id="dl-1b0c5cd4fae5"),
    pytest.param("dl-f51d7579d749", "imported", "Discarded a file that is a different song you already own", "processing", True, id="dl-f51d7579d749"),
    # Held rows
    pytest.param("dl-674b29983719", "warning", None, None, False, id="dl-674b29983719"),
    pytest.param("dl-bcadbf3bac32", "warning", None, None, False, id="dl-bcadbf3bac32"),
    pytest.param("dl-3f9b4ccf4c93", "warning", None, None, False, id="dl-3f9b4ccf4c93"),
    pytest.param("dl-97c62b10410e", "warning", None, None, False, id="dl-97c62b10410e"),
    pytest.param("dl-21f9580296de", "warning", None, None, False, id="dl-21f9580296de"),
    pytest.param("dl-3cbec6352972", "warning", None, None, False, id="dl-3cbec6352972"),
    pytest.param("dl-35f8ba8b135e", "warning", None, None, False, id="dl-35f8ba8b135e"),
    pytest.param("dl-9c6c319e9aad", "warning", None, None, False, id="dl-9c6c319e9aad"),
    pytest.param("dl-25122ba0517e", "warning", None, None, False, id="dl-25122ba0517e"),
    pytest.param("dl-25a242b23975", "warning", None, None, False, id="dl-25a242b23975"),
    pytest.param("dl-33ba1dfaacf6", "warning", None, None, False, id="dl-33ba1dfaacf6"),
    pytest.param("dl-c7cc25ff02eb", "warning", None, None, False, id="dl-c7cc25ff02eb"),
    # Chelsea rows: expected held, but duplicate-cleared via primary_artist('Elvis Costello & The Attractions')
    pytest.param(
        "dl-9e38d608823f",
        "warning",
        None,
        None,
        False,
        marks=pytest.mark.xfail(
            strict=True,
            reason="trackseerr/track_matching.py:387: (I Don't Want To Go To) Chelsea is owned in library under Elvis Costello; candidate_artist_ids matches related artist, auto-clearing duplicate",
        ),
        id="dl-9e38d608823f",
    ),
    pytest.param(
        "dl-362609c24533",
        "warning",
        None,
        None,
        False,
        marks=pytest.mark.xfail(
            strict=True,
            reason="trackseerr/track_matching.py:387: (I Don't Want To Go To) Chelsea is owned in library under Elvis Costello; candidate_artist_ids matches related artist, auto-clearing duplicate",
        ),
        id="dl-362609c24533",
    ),
]


@pytest.mark.parametrize(
    ("download_id", "expected_status", "expected_msg", "expected_req_status", "expect_blocklist"),
    ROW_OUTCOME_CASES,
)
def test_replay_row_outcome(
    replay_env: tuple[Database, Path, dict[str, Any]],
    download_id: str,
    expected_status: str,
    expected_msg: str | None,
    expected_req_status: str | None,
    expect_blocklist: bool,
) -> None:
    db, _, _ = replay_env
    dl = db.get_active_download(download_id)
    assert dl is not None, f"Download {download_id} not found"
    assert dl["status"] == expected_status

    if expected_msg is not None:
        assert dl.get("error_message") == expected_msg

    if expected_status == DownloadStatus.WARNING.value:
        unmatched = dl.get("unmatched_files") or []
        assert len(unmatched) > 0, f"Expected held files in {download_id}"

    if dl.get("request_id") and expected_req_status is not None:
        req = db.get_request(str(dl["request_id"]))
        assert req is not None
        assert req["status"] == expected_req_status

    bl_row = db.conn.execute(
        "SELECT COUNT(*) FROM download_blocklist WHERE release_guid = ?",
        (download_id,),
    ).fetchone()[0]
    if expect_blocklist:
        assert bl_row > 0, f"Expected blocklist row for {download_id}"
    else:
        assert bl_row == 0, f"Unexpected blocklist row for {download_id}"


def test_replay_second_poll_is_stable(tmp_path: Path) -> None:
    db, _, staging, held_tags = _setup_replay_db(tmp_path)
    _run_replay_poll(db, staging, held_tags)

    all_dls_1 = db.list_active_downloads(statuses=["imported", "warning", "failed", "completed"])
    statuses_1 = {d["id"]: (d["status"], d.get("error_message")) for d in all_dls_1}
    bl_count_1 = db.conn.execute("SELECT COUNT(*) FROM download_blocklist").fetchone()[0]

    _run_replay_poll(db, staging, held_tags)

    all_dls_2 = db.list_active_downloads(statuses=["imported", "warning", "failed", "completed"])
    statuses_2 = {d["id"]: (d["status"], d.get("error_message")) for d in all_dls_2}
    bl_count_2 = db.conn.execute("SELECT COUNT(*) FROM download_blocklist").fetchone()[0]

    assert statuses_1 == statuses_2, "Second poll changed download statuses"
    assert bl_count_1 == bl_count_2, "Second poll added new blocklist rows"


def test_replay_backlog_skips_owned_requests(replay_env: tuple[Database, Path, dict[str, Any]]) -> None:
    db, _, _ = replay_env
    bw = WantedBacklogWorker()
    ctx = _SweepCollect(db=db)
    unfulfilled = bw._collect_unfulfilled_requests(ctx)
    bw._append_request_items(ctx, unfulfilled)

    search_req_ids = {item[4] for item in ctx.items_to_search}

    # Assert no search item is produced for Elvis or Prince requests
    elvis_req_ids = {"req-679fb5d90441", "req-72700d6eb1c2", "req-a49b92e2cf63"}
    prince_req_ids = {"req-f560ab87ab2e", "req-634cea40c563"}
    for eid in elvis_req_ids:
        assert eid not in search_req_ids, f"Elvis request {eid} unexpectedly queued for search"
    for pid in prince_req_ids:
        assert pid not in search_req_ids, f"Prince request {pid} unexpectedly queued for search"

    # Prince requests are now available (owned by title)
    req_p1 = db.get_request("req-f560ab87ab2e")
    req_p2 = db.get_request("req-634cea40c563")
    assert req_p1 and req_p1["status"] == RequestStatus.AVAILABLE.value
    assert req_p2 and req_p2["status"] == RequestStatus.AVAILABLE.value

    # T. Rex / James Brown requests still produce search items
    assert "req-b5c48dbc50cb" in search_req_ids, "T. Rex request not queued for search"
    assert any(
        jid in search_req_ids for jid in ("req-9647c5259dfd", "req-592578bd5c52")
    ), "James Brown requests not queued for search"

    # Chelsea requests: handoff expects Chelsea to produce search items, but requests were satisfied
    # under related artist Elvis Costello and marked available.
    chelsea_req_ids = {"req-24d1a053e177", "req-8636fe8e50e8"}
    chelsea_searched = any(cid in search_req_ids for cid in chelsea_req_ids)
    if not chelsea_searched:
        pytest.xfail(
            "trackseerr/track_matching.py:387: (I Don't Want To Go To) Chelsea is owned in library under Elvis Costello; "
            "Chelsea requests were marked available and do not produce search items"
        )
    assert chelsea_searched
