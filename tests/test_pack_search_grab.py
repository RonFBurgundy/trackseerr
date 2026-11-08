"""Tests for pack search and grab (partial torrent download)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.acquisition_import import ImportPipelineMixin, _ImportJob, _PollContext
from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.item_history import GrabTrigger
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    EvaluationResult,
    QualityProfile,
    QualityProfileItem,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def test_search_returns_pack_file_candidate(db: Database) -> None:
    idx = db.create_indexer(
        {
            "id": "idx-1",
            "name": "Prowlarr",
            "indexer_type": "torznab",
            "host_url": "http://prowlarr:9696",
            "api_key": "secret",
            "enabled": True,
            "pack_scan": "auto",
        }
    )
    pr = db.upsert_pack_release(
        indexer_id=idx["id"],
        guid="pack-guid-1",
        title="VA - 2000s Hits Compilation",
        download_url="http://prowlarr/download/pack1.torrent",
        info_hash="aabbccddeeff00112233445566778899aabbccdd",
        seeders=25,
        file_count=50,
        scan_status="scanned",
    )
    db.record_pack_files(
        pr["id"],
        [
            {
                "file_index": 0,
                "path": "01 - Britney Spears - Toxic.flac",
                "size_bytes": 25000000,
            },
            {
                "file_index": 1,
                "path": "02 - Daft Punk - One More Time.flac",
                "size_bytes": 35000000,
            },
        ],
    )

    coord = AcquisitionCoordinator()
    with patch("trackseerr.acquisition_coordinator._search_indexers", return_value=[]), \
         patch("trackseerr.acquisition_coordinator._search_slskd", return_value=[]):
        results = coord.search_all_indexers(artist="Daft Punk", title="One More Time", db=db)

    assert len(results) == 1
    cand = results[0]
    assert "(in pack: VA - 2000s Hits Compilation)" in cand.title
    assert cand.artist == "Daft Punk"
    assert cand.item_type == "track"
    assert cand.protocol == "torrent"
    assert cand.size_bytes == 35000000
    assert cand.seeders == 25
    assert cand.extra["file_indices"] == [1]
    assert cand.extra["file_paths"] == ["02 - Daft Punk - One More Time.flac"]
    assert cand.extra["pack_id"] == pr["id"]
    assert cand.extra["indexer_name"] == "Prowlarr"

    # search_log row created with "<indexer name> (pack index)"
    logs = db.conn.execute(
        "SELECT * FROM search_log WHERE source_name = ?",
        ("Prowlarr (pack index)",),
    ).fetchall()
    assert len(logs) == 1
    assert logs[0]["result_count"] == 1


def test_pack_candidates_respect_profile_and_exclusions(db: Database) -> None:
    idx = db.create_indexer(
        {
            "id": "idx-1",
            "name": "Prowlarr",
            "indexer_type": "torznab",
            "host_url": "http://prowlarr:9696",
            "api_key": "secret",
            "enabled": True,
            "pack_scan": "auto",
        }
    )
    pr = db.upsert_pack_release(
        indexer_id=idx["id"],
        guid="pack-guid-ex",
        title="VA - Millennium Hits [MP3 320]",
        info_hash="1122334455667788990011223344556677889900",
        seeders=15,
        scan_status="scanned",
    )
    db.record_pack_files(
        pr["id"],
        [
            {
                "file_index": 0,
                "path": "01 - Radiohead - Creep [320].mp3",
                "size_bytes": 10000000,
            }
        ],
    )

    # 1. Quality profile evaluation
    coord = AcquisitionCoordinator()
    with patch("trackseerr.acquisition_coordinator._search_indexers", return_value=[]), \
         patch("trackseerr.acquisition_coordinator._search_slskd", return_value=[]):
        cands = coord.search_all_indexers(artist="Radiohead", title="Creep", db=db)
    assert len(cands) == 1

    # FLAC-only profile rejects MP3 candidate
    flac_profile = QualityProfile(
        id="prof-flac",
        name="FLAC Only",
        cutoff=1,
        items=[QualityProfileItem(quality="FLAC 16bit", allowed=True)],
    )
    ranked = coord.evaluate_and_rank(cands, profile=flac_profile, db=db)
    assert len(ranked) == 0

    # 2. Exclusion check refuses search_and_grab
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")
    res = coord.search_and_grab(
        artist="Radiohead",
        title="Creep",
        db=db,
        trigger=GrabTrigger(kind="manual"),
    )
    assert res.get("success") is False
    assert "excluded" in str(res.get("message", "")).lower()


def test_paused_or_off_indexer_not_searched(db: Database) -> None:
    # Indexer 1: pack_scan is 'off'
    idx_off = db.create_indexer(
        {
            "id": "idx-off",
            "name": "OffIndexer",
            "indexer_type": "torznab",
            "host_url": "http://off:9696",
            "api_key": "secret",
            "enabled": True,
            "pack_scan": "off",
        }
    )
    pr1 = db.upsert_pack_release(
        indexer_id=idx_off["id"],
        guid="pack-off-1",
        title="Off Pack",
        info_hash="aabbcc11",
        scan_status="scanned",
    )
    db.record_pack_files(
        pr1["id"],
        [{"file_index": 0, "path": "01 - Artist - Song.flac", "size_bytes": 20000000}],
    )

    # Indexer 2: paused
    idx_paused = db.create_indexer(
        {
            "id": "idx-paused",
            "name": "PausedIndexer",
            "indexer_type": "torznab",
            "host_url": "http://paused:9696",
            "api_key": "secret",
            "enabled": True,
            "pack_scan": "auto",
        }
    )
    pr2 = db.upsert_pack_release(
        indexer_id=idx_paused["id"],
        guid="pack-paused-1",
        title="Paused Pack",
        info_hash="aabbcc22",
        scan_status="scanned",
    )
    db.record_pack_files(
        pr2["id"],
        [{"file_index": 0, "path": "01 - Artist - Song.flac", "size_bytes": 20000000}],
    )

    coord = AcquisitionCoordinator()
    from datetime import datetime, timedelta, timezone
    with patch("trackseerr.acquisition_coordinator._search_indexers", return_value=[]), \
         patch("trackseerr.acquisition_coordinator._search_slskd", return_value=[]), \
         patch("trackseerr.indexer_status.indexer_status.paused_until", side_effect=lambda idx_id: datetime.now(timezone.utc) + timedelta(minutes=10) if idx_id == "idx-paused" else None):
        cands = coord.search_all_indexers(artist="Artist", title="Song", db=db)

    assert len(cands) == 0
    logs = db.conn.execute("SELECT * FROM search_log WHERE source_name LIKE '%pack index%'").fetchall()
    assert len(logs) == 0


def test_grab_pack_file_sets_priorities_and_resumes() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hash123:2",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hash123&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [2],
            "file_paths": ["folder/03 - Song.flac"],
            "info_hash": "hash123",
        },
    )

    mock_add_resp = MagicMock(status_code=200, text="Ok.")
    mock_files_resp = MagicMock(
        status_code=200,
        json=lambda: [
            {"index": 0, "name": "folder/01 - Other.flac"},
            {"index": 1, "name": "folder/02 - Other.flac"},
            {"index": 2, "name": "folder/03 - Song.flac"},
        ],
    )
    mock_prio_resp = MagicMock(status_code=200, text="Ok.")
    mock_start_resp = MagicMock(status_code=200, text="Ok.")

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", return_value=mock_files_resp):
        mock_post.side_effect = [mock_add_resp, mock_prio_resp, mock_prio_resp, mock_start_resp]
        result_hash = driver.download(cand)
        assert result_hash == "hash123"

        calls = mock_post.call_args_list
        assert len(calls) == 4

        # 1. Add paused & stopped
        add_url, add_kwargs = calls[0][0][0], calls[0][1]
        assert "torrents/add" in add_url
        assert add_kwargs["data"]["paused"] == "true"
        assert add_kwargs["data"]["stopped"] == "true"

        # 2. filePrio for other files (0 and 1) -> priority 0
        prio1_url, prio1_kwargs = calls[1][0][0], calls[1][1]
        assert "torrents/filePrio" in prio1_url
        assert prio1_kwargs["data"]["id"] == "0|1"
        assert prio1_kwargs["data"]["priority"] == 0

        # 3. filePrio for wanted file (2) -> priority 1
        prio2_url, prio2_kwargs = calls[2][0][0], calls[2][1]
        assert "torrents/filePrio" in prio2_url
        assert prio2_kwargs["data"]["id"] == "2"
        assert prio2_kwargs["data"]["priority"] == 1

        # 4. Start
        start_url, _ = calls[3][0][0], calls[3][1]
        assert "torrents/start" in start_url


def test_grab_waits_for_magnet_metadata() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashmeta:0",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashmeta&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [0],
            "file_paths": ["01 - Song.flac"],
            "info_hash": "hashmeta",
        },
    )

    empty_resp = MagicMock(status_code=200, json=lambda: [])
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [{"index": 0, "name": "01 - Song.flac"}],
    )

    with patch("httpx.Client.post", return_value=MagicMock(status_code=200, text="Ok.")), \
         patch("httpx.Client.get", side_effect=[empty_resp, empty_resp, files_resp]) as mock_get, \
         patch("time.sleep") as mock_sleep:
        h = driver.download(cand)
        assert h == "hashmeta"
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2


def test_metadata_timeout_removes_torrent() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashtimeout:0",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashtimeout&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [0],
            "file_paths": ["01 - Song.flac"],
            "info_hash": "hashtimeout",
        },
    )

    empty_resp = MagicMock(status_code=200, json=lambda: [])

    with patch("httpx.Client.post") as mock_post, \
         patch("httpx.Client.get", return_value=empty_resp), \
         patch("time.sleep"):
        mock_post.return_value = MagicMock(status_code=200, text="Ok.")

        with pytest.raises(RuntimeError) as exc_info:
            driver.download(cand)
        assert "qBittorrent did not receive the torrent's file list in time" in str(exc_info.value)

        # Assert delete was called with deleteFiles=false
        del_calls = [c for c in mock_post.call_args_list if "torrents/delete" in c[0][0]]
        assert len(del_calls) == 1
        assert del_calls[0][1]["data"]["deleteFiles"] == "false"
        assert del_calls[0][1]["data"]["hashes"] == "hashtimeout"


def test_index_drift_aborts() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashdrift:1",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashdrift&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [1],
            "file_paths": ["02 - Expected Song.flac"],
            "info_hash": "hashdrift",
        },
    )

    drift_resp = MagicMock(
        status_code=200,
        json=lambda: [
            {"index": 0, "name": "01 - First.flac"},
            {"index": 1, "name": "02 - Unexpected Drifting Title.flac"},
        ],
    )

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", return_value=drift_resp):
        mock_post.return_value = MagicMock(status_code=200, text="Ok.")
        with pytest.raises(RuntimeError) as exc_info:
            driver.download(cand)
        assert "Index drift" in str(exc_info.value)

        del_calls = [c for c in mock_post.call_args_list if "torrents/delete" in c[0][0]]
        assert len(del_calls) == 1
        assert del_calls[0][1]["data"]["deleteFiles"] == "false"


def test_non_qbit_client_skips_pack_candidate(db: Database) -> None:
    # Configure Transmission client (non-qbittorrent)
    db.create_download_client(
        DownloadClientConfig(
            id="c-trans",
            name="Transmission",
            driver_type=DownloadDriverType.TRANSMISSION,
            host_url="http://transmission:9091",
        )
    )

    coord = AcquisitionCoordinator()
    pack_cand = AcquisitionSearchResult(
        download_id="packhash:0",
        title="Daft Punk - One More Time [FLAC] (in pack: Best)",
        artist="Daft Punk",
        protocol="torrent",
        extra={"file_indices": [0], "file_paths": ["01 - One More Time.flac"]},
    )

    # 1. grab_candidate skips candidate directly
    res = coord.grab_candidate(
        db,
        pack_cand,
        parsed_quality="FLAC 16bit",
        score=1000,
        artist="Daft Punk",
        album=None,
        item_type="track",
        request_id=None,
        track_id=None,
        album_id=None,
        trigger=GrabTrigger(kind="manual"),
    )
    assert res["success"] is False
    assert "Only qBittorrent can download part of a torrent" in res["reason"]

    # 2. search_and_grab falls back to normal non-pack candidate if available
    normal_cand = AcquisitionSearchResult(
        download_id="singlehash",
        title="Daft Punk - One More Time [FLAC]",
        artist="Daft Punk",
        protocol="torrent",
    )
    eval_pack = EvaluationResult(is_acceptable=True, score=2000, rejection_reasons=[], parsed_quality="FLAC 16bit", meets_cutoff=True)
    eval_norm = EvaluationResult(is_acceptable=True, score=1500, rejection_reasons=[], parsed_quality="FLAC 16bit", meets_cutoff=True)

    with patch.object(coord, "search_with_fallback", return_value=[pack_cand, normal_cand]), \
         patch.object(coord, "evaluate_and_rank", return_value=[(pack_cand, eval_pack), (normal_cand, eval_norm)]), \
         patch.object(coord, "grab_candidate", return_value={"success": True, "download_id": "dl-ok"}) as mock_grab:
        grab_res = coord.search_and_grab(
            artist="Daft Punk",
            title="One More Time",
            db=db,
            trigger=GrabTrigger(kind="manual"),
        )
        assert grab_res["success"] is True
        # Verified that pack_cand was skipped and normal_cand was grabbed
        assert mock_grab.call_args[0][1] == normal_cand


def test_partial_progress_uses_selected_files() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    # Torrent total size: 40 GB, progress 1%
    info_resp = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "hash": "hashpartial",
                "name": "Giant 40GB Pack",
                "state": "downloading",
                "progress": 0.01,
                "total_size": 40000000000,
                "dlspeed": 500000,
                "eta": 10000,
                "content_path": "/downloads/Giant 40GB Pack",
            }
        ],
    )

    # File 0: 39.997 GB (priority 0, not selected, 0% done)
    # File 1: 3 MB (priority 1, selected, 100% done)
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [
            {"index": 0, "name": "Other.flac", "size": 39997000000, "priority": 0, "progress": 0.0},
            {"index": 1, "name": "Song.flac", "size": 3000000, "priority": 1, "progress": 1.0},
        ],
    )

    with patch("httpx.Client.get", side_effect=[info_resp, files_resp]):
        status = driver.get_status("hashpartial")
        assert status["progress"] == 100.0
        assert status["status"] == DownloadStatus.COMPLETED.value
        assert status["size_bytes"] == 3000000


def test_import_uses_only_selected_files(tmp_path: Path, db: Database) -> None:
    staging = tmp_path / "staging"
    music = tmp_path / "music"
    staging.mkdir()
    music.mkdir()

    pack_dir = staging / "BigPack"
    pack_dir.mkdir()
    wanted_file = pack_dir / "wanted_song.flac"
    wanted_file.write_bytes(b"fLaC" + b"\x00" * 100)
    neighbour_file = pack_dir / "neighbour_partial.flac"
    neighbour_file.write_bytes(b"fLaC" + b"\x00" * 50)

    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit",
            name="qBit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbit:8080",
        )
    )

    dl = db.create_active_download(
        ActiveDownload(
            id="dl-pack-1",
            client_id="c-qbit",
            title="Artist - Song [FLAC] (in pack: BigPack)",
            artist="Artist",
            item_type="track",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(pack_dir),
            selected_files=[{"file_index": 1, "path": "BigPack/wanted_song.flac"}],
        )
    )

    mixin = ImportPipelineMixin()
    mixin.allowed_roots = MagicMock()
    mixin.allowed_roots.roots = [staging]
    mixin.allowed_roots.check.return_value = (True, "")

    job = _ImportJob(
        item=dl,
        download_id=dl["id"],
        client_id="c-qbit",
        client_config={"name": "qBit", "driver_type": "qbittorrent"},
        driver=MagicMock(),
        driver_type="qbittorrent",
        import_mode="auto",
        status_dict={"source_path": str(pack_dir)},
        target_lookup=dl["id"],
        db=db,
    )
    ctx = _PollContext(
        media_settings={"staging_folder_path": str(staging)},
        write_tags=False,
        embed_art=False,
        save_cover=False,
        plex_client=None,
        lidarr_mode=False,
    )

    ok = mixin._locate_audio_files(db, ctx, job, {"completed": 0, "failed": 0, "imported": 0})
    assert ok is True
    # Verify ONLY wanted_song.flac was located, neighbour_partial.flac is completely ignored
    assert len(job.audio_files) == 1
    assert job.audio_files[0].name == "wanted_song.flac"


def test_selected_file_missing_warns(tmp_path: Path, db: Database) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    pack_dir = staging / "BigPack"
    pack_dir.mkdir()

    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit",
            name="qBit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbit:8080",
        )
    )

    dl = db.create_active_download(
        ActiveDownload(
            id="dl-missing-1",
            client_id="c-qbit",
            title="Artist - Song [FLAC] (in pack: BigPack)",
            artist="Artist",
            item_type="track",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(pack_dir),
            selected_files=[{"file_index": 1, "path": "BigPack/ghost_song.flac"}],
        )
    )

    mixin = ImportPipelineMixin()
    mixin.allowed_roots = MagicMock()
    mixin.allowed_roots.roots = [staging]
    mixin.allowed_roots.check.return_value = (True, "")

    job = _ImportJob(
        item=dl,
        download_id=dl["id"],
        client_id="c-qbit",
        client_config={"name": "qBit", "driver_type": "qbittorrent"},
        driver=MagicMock(),
        driver_type="qbittorrent",
        import_mode="auto",
        status_dict={"source_path": str(pack_dir)},
        target_lookup=dl["id"],
        db=db,
    )
    ctx = _PollContext(
        media_settings={"staging_folder_path": str(staging)},
        write_tags=False,
        embed_art=False,
        save_cover=False,
        plex_client=None,
        lidarr_mode=False,
    )

    ok = mixin._locate_audio_files(db, ctx, job, {"completed": 0, "failed": 0, "imported": 0})
    assert ok is False

    updated_dl = db.get_active_download("dl-missing-1")
    assert updated_dl is not None
    assert updated_dl["status"] == DownloadStatus.WARNING.value
    assert "Selected file not found: ghost_song.flac" in updated_dl["error_message"]


def test_qbit4_resume_fallback() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashqbit4:0",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashqbit4&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [0],
            "file_paths": ["01 - Song.flac"],
            "info_hash": "hashqbit4",
        },
    )

    add_resp = MagicMock(status_code=200, text="Ok.")
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [{"index": 0, "name": "01 - Song.flac"}],
    )
    prio_resp = MagicMock(status_code=200, text="Ok.")
    # qBittorrent 4: torrents/start returns 404
    start_resp = MagicMock(status_code=404, text="Not Found")
    resume_resp = MagicMock(status_code=200, text="Ok.")

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", return_value=files_resp):
        mock_post.side_effect = [add_resp, prio_resp, start_resp, resume_resp]
        res_h = driver.download(cand)
        assert res_h == "hashqbit4"

        urls = [c[0][0] for c in mock_post.call_args_list]
        assert any("torrents/start" in u for u in urls)
        assert any("torrents/resume" in u for u in urls)


def test_cleanup_partial_torrent_targets_torrent_only() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    del_resp = MagicMock(status_code=200, text="Ok.")
    with patch("httpx.Client.post", return_value=del_resp) as mock_post:
        ok = driver.cleanup_completed("hashpartial123", delete_files=True)
        assert ok is True
        assert mock_post.called
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert "torrents/delete" in url
        assert kwargs["data"]["hashes"] == "hashpartial123"
        assert kwargs["data"]["deleteFiles"] == "true"
