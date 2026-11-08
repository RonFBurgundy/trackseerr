"""Comprehensive tests for pack_scanner logic, workers, backfill, and RSS hooks."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from trackseerr.clients.acquisition.torznab import IndexerRateLimited, TorznabDriver
from trackseerr.indexer_status import indexer_status
from trackseerr.models import AcquisitionSearchResult
from trackseerr.pack_scanner import (
    LAST_BACKFILL_KV,
    LAST_RUN_KV,
    PackIndexWorker,
    PackScannerBusy,
    _format_sweep_summary,
    backfill,
    collect_from_releases,
    get_progress,
    get_status,
    is_running,
    pack_index_worker,
    run_sweep,
    scan_pending,
    start_sweep_async,
)
from trackseerr.storage import Database
import trackseerr.task_manager as task_manager


@pytest.fixture(autouse=True)
def _reset_indexer_backoff():
    indexer_status.reset()
    yield
    indexer_status.reset()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "pack_scanner_test.db"))
    yield database
    database.close()


def make_test_torrent_bytes(name: str = "Test Pack", files: list[tuple[str, int]] = None) -> bytes:
    if files is None:
        files = [
            ("01 - Artist One - Song A.mp3", 5000000),
            ("02 - Artist One - Song B.flac", 20000000),
            ("03 - Artist Two - Song C.m4a", 8000000),
        ]
    files_bencode = []
    for path, length in files:
        path_parts = path.split("/")
        parts_bencode = b"".join(f"{len(p)}:{p}".encode("utf-8") for p in path_parts)
        files_bencode.append(
            b"d"
            + b"6:lengthi" + str(length).encode("ascii") + b"e"
            + b"4:pathl" + parts_bencode + b"e"
            + b"e"
        )
    info_bencode = (
        b"d"
        + b"5:filesl" + b"".join(files_bencode) + b"e"
        + b"4:name" + f"{len(name)}:{name}".encode("utf-8")
        + b"12:piece lengthi16384e"
        + b"6:pieces20:12345678901234567890"
        + b"e"
    )
    return (
        b"d"
        + b"8:announce16:http://tracker/a"
        + b"4:info" + info_bencode
        + b"e"
    )


def test_collect_from_releases_modes(db: Database) -> None:
    db.create_indexer({
        "id": "idx-torznab",
        "name": "Prowlarr",
        "indexer_type": "torznab",
        "host_url": "http://prowlarr:9696",
        "pack_scan": "auto",
    })

    pack_release = AcquisitionSearchResult(
        download_id="guid-pack-1",
        title="VA - Top 100 Dance Hits 2024",
        artist="Various Artists",
        album="Top 100",
        download_url="http://prowlarr/dl/1.torrent",
        size_bytes=500000000,
        seeders=25,
        source="torznab",
        protocol="torrent",
        extra={"infohash": "hash123", "published_at": "2024-05-01 12:00:00"},
    )
    single_release = AcquisitionSearchResult(
        download_id="guid-single-1",
        title="David Guetta - Titanium - CD - FLAC",
        artist="David Guetta",
        album="Titanium",
        download_url="http://prowlarr/dl/2.torrent",
        size_bytes=30000000,
        seeders=10,
        source="torznab",
        protocol="torrent",
        extra={"infohash": "hash456"},
    )
    usenet_release = AcquisitionSearchResult(
        download_id="guid-nzb-1",
        title="VA - Top 100 Dance Hits 2024",
        artist="Various Artists",
        download_url="http://newznab/dl/1.nzb",
        source="newznab",
        protocol="usenet",
    )

    # 1. pack_scan == 'auto': only pack_release collected, single and usenet ignored
    idx_cfg_auto = {"id": "idx-torznab", "pack_scan": "auto", "indexer_type": "torznab"}
    c_auto = collect_from_releases(db, idx_cfg_auto, [pack_release, single_release, usenet_release])
    assert c_auto == 1

    row = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'hash123'").fetchone()
    assert row is not None
    assert row["title"] == "VA - Top 100 Dance Hits 2024"
    assert row["info_hash"] == "hash123"
    assert row["scan_status"] == "pending"

    # 2. pack_scan == 'all': both torrents collected
    idx_cfg_all = {"id": "idx-torznab", "pack_scan": "all", "indexer_type": "torznab"}
    c_all = collect_from_releases(db, idx_cfg_all, [pack_release, single_release, usenet_release])
    assert c_all == 2
    row_single = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'hash456'").fetchone()
    assert row_single is not None

    # 3. pack_scan == 'off': returns 0
    idx_cfg_off = {"id": "idx-torznab", "pack_scan": "off", "indexer_type": "torznab"}
    c_off = collect_from_releases(db, idx_cfg_off, [pack_release, single_release])
    assert c_off == 0


def test_scan_pending_multi_file_torrent(db: Database) -> None:
    db.create_indexer({
        "id": "idx-scan",
        "name": "Indexer1",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-scan",
        guid="guid-pack-multi",
        title="VA - Mega Dance Pack 2024",
        download_url="http://indexer.local/dl/multi.torrent",
        scan_status="pending",
    )

    torrent_bytes = make_test_torrent_bytes()

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.return_value = torrent_bytes

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert counts["scanned"] == 1
        assert counts["failed"] == 0
        assert counts["skipped"] == 0

    row = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'guid-pack-multi'").fetchone()
    assert row["scan_status"] == "scanned"
    assert row["file_count"] == 3
    assert row["info_hash"] is not None

    files = db.conn.execute("SELECT * FROM pack_files WHERE pack_id = ?", (row["id"],)).fetchall()
    assert len(files) == 3
    artists = [f["parsed_artist"] for f in files]
    assert "Artist One" in artists
    assert "Artist Two" in artists

    # Search finding
    matches = db.search_pack_files("Artist One", "Song A")
    assert len(matches) == 1
    assert matches[0].pack_title == "VA - Mega Dance Pack 2024"


def test_scan_pending_fewer_than_2_audio_files(db: Database) -> None:
    db.create_indexer({
        "id": "idx-few",
        "name": "IndexerFew",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-few",
        guid="guid-few",
        title="Artist - Single Track Pack",
        download_url="http://indexer.local/dl/single.torrent",
        scan_status="pending",
    )

    torrent_bytes = make_test_torrent_bytes(
        files=[("01 - Song.mp3", 5000000), ("info.nfo", 1000)]
    )

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.return_value = torrent_bytes

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert counts["skipped"] == 1
        assert counts["scanned"] == 0

    row = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'guid-few'").fetchone()
    assert row["scan_status"] == "skipped"
    assert "Fewer than 2 audio files" in row["scan_error"]


def test_scan_pending_magnet_redirect(db: Database) -> None:
    db.create_indexer({
        "id": "idx-mag",
        "name": "IndexerMag",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-mag",
        guid="guid-mag",
        title="VA - Magnet Only Pack",
        download_url="http://indexer.local/dl/mag.torrent",
        scan_status="pending",
    )

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.return_value = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert counts["skipped"] == 1

    row = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'guid-mag'").fetchone()
    assert row["scan_status"] == "skipped"
    assert "Magnet only" in row["scan_error"]


def test_scan_pending_bencode_error(db: Database) -> None:
    db.create_indexer({
        "id": "idx-err",
        "name": "IndexerErr",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-err",
        guid="guid-err",
        title="VA - Corrupt Pack",
        download_url="http://indexer.local/dl/corrupt.torrent",
        scan_status="pending",
    )

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.return_value = b"NOT_BENCODE_DATA"

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert counts["failed"] == 1

    row = db.conn.execute("SELECT * FROM pack_releases WHERE guid = 'guid-err'").fetchone()
    assert row["scan_status"] == "failed"
    assert "Bencode error" in row["scan_error"]


def test_scan_pending_indexer_rate_limited(db: Database) -> None:
    db.create_indexer({
        "id": "idx-ratelimit",
        "name": "IndexerRL",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-ratelimit",
        guid="guid-rl1",
        title="VA - Pack RL 1",
        download_url="http://indexer.local/dl/1.torrent",
        scan_status="pending",
    )
    db.upsert_pack_release(
        indexer_id="idx-ratelimit",
        guid="guid-rl2",
        title="VA - Pack RL 2",
        download_url="http://indexer.local/dl/2.torrent",
        scan_status="pending",
    )

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.side_effect = IndexerRateLimited("HTTP 429", code=429, retry_after=120.0)

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert counts["errors"] == 1
        # Called only once because the indexer was paused and skipped for subsequent items
        assert mock_driver.fetch_torrent.call_count == 1
        assert indexer_status.paused_until("idx-ratelimit") is not None


def test_scan_pending_paused_indexer_skipped(db: Database) -> None:
    db.create_indexer({
        "id": "idx-paused",
        "name": "IndexerPaused",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    db.upsert_pack_release(
        indexer_id="idx-paused",
        guid="guid-p1",
        title="VA - Pack Paused",
        download_url="http://indexer.local/dl/p.torrent",
        scan_status="pending",
    )

    indexer_status.record_failure("idx-paused", IndexerRateLimited("429", retry_after=300))
    assert indexer_status.paused_until("idx-paused") is not None

    mock_driver = MagicMock(spec=TorznabDriver)

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        counts = scan_pending(db, limit=10)
        assert mock_driver.fetch_torrent.call_count == 0
        assert counts["scanned"] == 0


def test_backfill_pages_and_prune(db: Database) -> None:
    db.create_indexer({
        "id": "idx-bf",
        "name": "IndexerBF",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })

    def fake_fetch_page(offset: int = 0, limit: int = 100):
        if offset >= 200:
            return []
        page_num = offset // 100
        return [
            AcquisitionSearchResult(
                download_id=f"bf-{page_num}-{i}",
                title=f"VA - Backfill Hits Vol {page_num} Track {i}",
                artist="VA",
                album="Backfill",
                download_url=f"http://indexer.local/dl/{page_num}_{i}.torrent",
                size_bytes=100000000,
                seeders=10,
                source="torznab",
                protocol="torrent",
            )
            for i in range(2)
        ]

    torrent_bytes = make_test_torrent_bytes()

    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_page.side_effect = fake_fetch_page
    mock_driver.fetch_torrent.return_value = torrent_bytes

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        with patch.object(db, "prune_pack_index", wraps=db.prune_pack_index) as mock_prune:
            res = backfill(db, pages=3)
            assert res["collected"] == 4  # 2 pages * 2 releases
            assert res["scanned"] == 4
            assert mock_prune.called


def test_rss_run_calls_collect_and_survives_error(db: Database) -> None:
    from trackseerr.backlog_worker import RSSSyncWorker

    db.create_indexer({
        "id": "idx-rss",
        "name": "IndexerRSS",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
        "enabled": 1,
    })
    db.conn.execute("INSERT INTO users (id, username) VALUES ('u-1', 'testuser')")
    db.conn.execute(
        "INSERT INTO music_requests (id, user_id, item_type, title, artist, status) "
        "VALUES ('req-1', 'u-1', 'track', 'Song', 'Artist', 'pending')"
    )
    db.conn.commit()

    worker = RSSSyncWorker()
    releases = [
        AcquisitionSearchResult(
            download_id="rss-1",
            title="VA - Top 40 Dance Hits 2024",
            artist="Various Artists",
            download_url="http://indexer.local/dl/rss1.torrent",
            source="torznab",
            protocol="torrent",
        )
    ]

    with patch("trackseerr.backlog_worker._gate_search", return_value=None):
        with patch.object(worker, "_fetch_recent_releases", return_value=releases):
            with patch("trackseerr.pack_scanner.collect_from_releases", side_effect=sqlite3.OperationalError("disk locked")) as mock_collect:
                # Does not raise and returns cleanly
                result = worker.poll_once(db)
                assert mock_collect.called
                assert result is not None


def test_task_manager_pack_index_registration():
    # 1. Registered in TASKS
    spec = task_manager.TASKS.get("pack_index")
    assert spec is not None
    assert spec.name == "Pack Index"
    assert spec.kind == task_manager.KIND_INTERVAL
    assert spec.default_interval_seconds == 6 * 3600
    assert spec.group == task_manager.GROUP_ACQUISITION

    # 2. Worker thread mapping
    assert task_manager.WORKER_THREAD_TASKS["PackIndexWorkerThread"] == "pack_index"
    assert task_manager.WORKER_THREAD_TASKS["ManualPackIndexTask"] == "pack_index"

    # 3. Pausable
    assert "pack_index" in task_manager.PAUSABLE_TASKS

    # 4. Manual runnable
    from trackseerr.api.routes.system.tasks import _MANUAL_RUNNABLE
    assert "pack_index" in _MANUAL_RUNNABLE


def test_run_sweep_24h_cadence(db: Database) -> None:
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    with patch("trackseerr.pack_scanner.backfill", return_value={"collected": 10, "scanned": 10, "skipped": 0, "failed": 0, "errors": 0, "pruned": 0}) as mock_bf:
        with patch("trackseerr.pack_scanner.scan_pending", return_value={"scanned": 5, "skipped": 0, "failed": 0, "errors": 0}) as mock_sp:
            # First run: backfill should run
            stats1 = run_sweep(db, now=now)
            assert mock_bf.call_count == 1
            assert mock_sp.call_count == 0
            assert db.get_kv(LAST_BACKFILL_KV) == now.isoformat()

            # Immediate second run (5 minutes later): backfill should NOT run
            run_sweep(db, now=now + timedelta(minutes=5))
            assert mock_bf.call_count == 1
            assert mock_sp.call_count == 1

            # Run after 25 hours: backfill should run again
            run_sweep(db, now=now + timedelta(hours=25))
            assert mock_bf.call_count == 2


def test_pack_scanner_busy_guard(db: Database):
    from trackseerr.pack_scanner import _run_lock
    assert is_running() is False
    _run_lock.acquire()
    try:
        assert is_running() is True
        with pytest.raises(PackScannerBusy):
            run_sweep(db)
        assert start_sweep_async(db) is False
    finally:
        _run_lock.release()
    assert is_running() is False


def test_pack_scanner_worker_lifecycle(db: Database):
    worker = PackIndexWorker()
    assert worker.is_running() is False

    with patch.object(worker, "run_once", return_value=True):
        started = worker.start(db, interval_seconds=1.0, initial_delay=0.01)
        assert started is True
        assert worker.is_running() is True
        worker.stop()
        assert worker.is_running() is False


def test_pack_index_progress_reported_while_scanning(db: Database) -> None:
    from trackseerr.api.routes.system.tasks import _task_progress

    db.create_indexer({
        "id": "idx-prog",
        "name": "ProgressTracker",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })
    db.upsert_pack_release(
        indexer_id="idx-prog",
        guid="guid-pack-p1",
        title="Pack One Title",
        download_url="http://indexer.local/dl/p1.torrent",
        scan_status="pending",
    )
    db.upsert_pack_release(
        indexer_id="idx-prog",
        guid="guid-pack-p2",
        title="Pack Two Title",
        download_url="http://indexer.local/dl/p2.torrent",
        scan_status="pending",
    )

    torrent_bytes = make_test_torrent_bytes("Pack")
    observed_progress: list[tuple[tuple[int, int], Any]] = []

    mock_driver = MagicMock(spec=TorznabDriver)

    def fake_fetch(url: str):
        prog_pair = get_progress()
        task_prog = _task_progress("pack_index", True)
        observed_progress.append((prog_pair, task_prog))
        return torrent_bytes

    mock_driver.fetch_torrent.side_effect = fake_fetch

    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        with patch("trackseerr.pack_scanner.time.sleep", return_value=None):
            db.set_kv(LAST_BACKFILL_KV, datetime.now(timezone.utc).isoformat())
            run_sweep(db)

    assert len(observed_progress) == 2
    assert observed_progress[0][0] == (1, 2)
    assert observed_progress[0][1] is not None
    assert observed_progress[0][1].current == 1
    assert observed_progress[0][1].total == 2
    assert "Reading pack 1 of 2: Pack One Title" in (observed_progress[0][1].message or "")

    assert observed_progress[1][0] == (2, 2)
    assert observed_progress[1][1] is not None
    assert observed_progress[1][1].current == 2
    assert observed_progress[1][1].total == 2
    assert "Reading pack 2 of 2: Pack Two Title" in (observed_progress[1][1].message or "")

    assert get_progress() == (0, 0)
    assert _task_progress("pack_index", False) is None


def test_pack_index_final_message_no_indexer_pack_scan_on(db: Database) -> None:
    run_sweep(db)
    finished = db.latest_finished_task_runs()
    assert "pack_index" in finished
    assert finished["pack_index"]["message"] == "No indexer has Index packs turned on."

    db.create_indexer({
        "id": "idx-off",
        "name": "IndexerOff",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "off",
    })
    run_sweep(db)
    finished = db.latest_finished_task_runs()
    assert finished["pack_index"]["message"] == "No indexer has Index packs turned on."


def test_pack_index_final_message_nothing_pending(db: Database) -> None:
    db.create_indexer({
        "id": "idx-empty",
        "name": "IndexerEmpty",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })
    db.set_kv(LAST_BACKFILL_KV, datetime.now(timezone.utc).isoformat())

    run_sweep(db)
    finished = db.latest_finished_task_runs()
    assert finished["pack_index"]["message"] == "No new packs to read. Index holds 0 packs (0 songs)."


def test_pack_index_final_message_packs_read(db: Database) -> None:
    db.create_indexer({
        "id": "idx-read",
        "name": "IndexerRead",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "auto",
    })
    db.upsert_pack_release(
        indexer_id="idx-read",
        guid="guid-single-read",
        title="VA - Greatest Hits",
        download_url="http://indexer.local/dl/read.torrent",
        scan_status="pending",
    )
    mock_driver = MagicMock(spec=TorznabDriver)
    mock_driver.fetch_torrent.return_value = make_test_torrent_bytes()

    db.set_kv(LAST_BACKFILL_KV, datetime.now(timezone.utc).isoformat())
    with patch("trackseerr.pack_scanner.get_indexer_driver", return_value=mock_driver):
        with patch("trackseerr.pack_scanner.time.sleep", return_value=None):
            run_sweep(db)

    finished = db.latest_finished_task_runs()
    assert finished["pack_index"]["message"] == "Read 1 pack: 3 songs indexed. Index holds 1 pack."


def test_format_sweep_summary_three_cases_and_example_wording(db: Database) -> None:
    stats = {
        "scanned": 140,
        "songs": 52310,
        "skipped": 6,
        "skipped_magnet": 6,
        "failed": 1,
    }
    with patch.object(db, "pack_index_stats", return_value={"packs": 1204, "files": 52310}):
        msg = _format_sweep_summary(stats, db)
        assert msg == "Read 140 packs: 52,310 songs indexed, 6 skipped (magnet only), 1 failed. Index holds 1,204 packs."


def test_pack_index_logs_one_info_line(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    db.create_indexer({
        "id": "idx-info",
        "name": "IndexerInfo",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "pack_scan": "off",
    })
    with caplog.at_level(logging.INFO):
        run_sweep(db)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO and "Pack index:" in r.getMessage()]
    assert len(info_records) == 1
    assert "No indexer has Index packs turned on." in info_records[0].getMessage()
    assert "http://" not in info_records[0].getMessage()
    assert "https://" not in info_records[0].getMessage()

