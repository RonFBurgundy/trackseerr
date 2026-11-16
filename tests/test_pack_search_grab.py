"""Tests for pack search and grab (partial torrent download)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.acquisition_import import ImportPipelineMixin, _ImportJob, _PollContext
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.bencode import info_hash
from trackseerr.clients.acquisition.base import AcquisitionUnavailableError
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

    def mock_get(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return mock_files_resp

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", side_effect=mock_get):
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
    file_responses = [empty_resp, empty_resp, files_resp]

    def mock_get(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return file_responses.pop(0)

    with patch("httpx.Client.post", return_value=MagicMock(status_code=200, text="Ok.")), \
         patch("httpx.Client.get", side_effect=mock_get) as mock_get_patch, \
         patch("time.sleep") as mock_sleep:
        h = driver.download(cand)
        assert h == "hashmeta"
        assert mock_get_patch.call_count == 4
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


def test_index_drift_is_unavailable() -> None:
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

    def mock_get(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return drift_resp

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", side_effect=mock_get):
        mock_post.return_value = MagicMock(status_code=200, text="Ok.")
        with pytest.raises(AcquisitionUnavailableError) as exc_info:
            driver.download(cand)
        assert exc_info.value.reason == "release_unavailable"
        assert "Index drift" in str(exc_info.value)

        del_calls = [c for c in mock_post.call_args_list if "torrents/delete" in c[0][0]]
        assert len(del_calls) == 1
        assert del_calls[0][1]["data"]["deleteFiles"] == "false"


def test_second_song_from_existing_pack_torrent() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashpack:7",
        title="Artist - Seventh Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashpack&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [7],
            "file_paths": ["folder/07 - Seventh Song.flac"],
            "info_hash": "hashpack",
        },
    )

    info_resp = MagicMock(
        status_code=200,
        json=lambda: [{"hash": "hashpack", "category": "trackseerr"}],
    )
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [
            {"index": 3, "name": "folder/03 - Third Song.flac", "priority": 1},
            {"index": 7, "name": "folder/07 - Seventh Song.flac", "priority": 0},
        ],
    )

    def mock_get(url, *args, **kwargs):
        if "torrents/info" in url:
            return info_resp
        if "torrents/files" in url:
            return files_resp
        return MagicMock(status_code=404)

    mock_post_resp = MagicMock(status_code=200, text="Ok.")
    with patch("httpx.Client.get", side_effect=mock_get), patch(
        "httpx.Client.post", return_value=mock_post_resp
    ) as mock_post:
        res_h = driver.download(cand)
        assert res_h == "hashpack"

        urls = [c[0][0] for c in mock_post.call_args_list]
        assert not any("torrents/add" in u for u in urls)
        assert any("torrents/filePrio" in u for u in urls)
        assert any("torrents/start" in u or "torrents/resume" in u for u in urls)

        prio_calls = [c for c in mock_post.call_args_list if "torrents/filePrio" in c[0][0]]
        assert len(prio_calls) == 1
        assert prio_calls[0][1]["data"]["id"] == "7"
        assert prio_calls[0][1]["data"]["priority"] == 1
        assert not any(c[1]["data"]["priority"] == 0 for c in prio_calls)


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
    assert "Only qBittorrent or rTorrent can download part of a torrent" in res["reason"]

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


def test_pack_import_names_song_without_bpm_and_with_album_fallback(
    tmp_path: Path, db: Database
) -> None:
    staging = tmp_path / "staging"
    music = tmp_path / "music"
    staging.mkdir()
    music.mkdir()

    pack_dir = staging / "BigPack"
    pack_dir.mkdir()
    wanted_file = pack_dir / "Rafael - Gotta Be Cool 124.mp3"
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    wanted_file.write_bytes(frame * 2)

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
            id="dl-pack-rafael",
            client_id="c-qbit",
            title="Rafael - Gotta Be Cool 124 [MP3] (in pack: BigPack)",
            artist="Rafael",
            item_type="track",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(pack_dir),
            selected_files=[{"file_index": 1, "path": "BigPack/Rafael - Gotta Be Cool 124.mp3"}],
        )
    )

    worker = AcquisitionWorker()
    worker.allowed_roots = MagicMock()
    worker.allowed_roots.roots = [staging]
    worker.allowed_roots.check.return_value = (True, "")

    job = _ImportJob(
        item=dl,
        download_id=dl["id"],
        client_id="c-qbit",
        client_config={"name": "qBit", "driver_type": "qbittorrent"},
        driver=MagicMock(),
        driver_type="qbittorrent",
        import_mode="copy",
        status_dict={"source_path": str(pack_dir)},
        target_lookup=dl["id"],
        db=db,
    )
    ctx = _PollContext(
        media_settings={"staging_folder_path": str(staging), "root_folder_path": str(music)},
        write_tags=False,
        embed_art=False,
        save_cover=False,
        plex_client=None,
        lidarr_mode=False,
    )

    ok = worker._locate_audio_files(db, ctx, job, {"completed": 0, "failed": 0, "imported": 0})
    assert ok is True
    assert len(job.audio_files) == 1
    worker._prepare_placement(db, ctx, job)
    worker._place_one_file(db, ctx, job, job.audio_files[0])
    assert job.last_metadata["title"] == "Gotta Be Cool"
    assert job.last_metadata["album"] == "Unknown Album"
    placed = list(music.rglob("*.mp3"))
    assert len(placed) == 1
    assert "Gotta Be Cool.mp3" in placed[0].name
    assert "Unknown Album" in str(placed[0].parent)


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

    def mock_get(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return files_resp

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", side_effect=mock_get):
        mock_post.side_effect = [add_resp, prio_resp, start_resp, resume_resp]
        res_h = driver.download(cand)
        assert res_h == "hashqbit4"

        urls = [c[0][0] for c in mock_post.call_args_list]
        assert any("torrents/start" in u for u in urls)
        assert any("torrents/resume" in u for u in urls)


def test_partial_grab_with_202_add() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    cand = AcquisitionSearchResult(
        download_id="hashqbit5:0",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="magnet:?xt=urn:btih:hashqbit5&dn=Pack",
        protocol="torrent",
        extra={
            "file_indices": [0],
            "file_paths": ["01 - Song.flac"],
            "info_hash": "hashqbit5",
        },
    )

    add_resp = MagicMock(
        status_code=202,
        text='{"added_torrent_ids":[],"failure_count":0,"pending_count":1,"success_count":0}',
        json=lambda: {
            "added_torrent_ids": [],
            "failure_count": 0,
            "pending_count": 1,
            "success_count": 0,
        },
    )
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [{"index": 0, "name": "01 - Song.flac"}],
    )
    prio_resp = MagicMock(status_code=200, text="Ok.")
    resume_resp = MagicMock(status_code=200, text="Ok.")

    def mock_get_202(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return files_resp

    with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get", side_effect=mock_get_202):
        mock_post.side_effect = [add_resp, prio_resp, resume_resp]
        res_h = driver.download(cand)
        assert res_h == "hashqbit5"

        urls = [c[0][0] for c in mock_post.call_args_list]
        assert any("torrents/filePrio" in u for u in urls)
        assert any("torrents/start" in u or "torrents/resume" in u for u in urls)


def test_partial_grab_uploads_torrent_file() -> None:
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    driver._cookie = "SID=valid"

    torrent_bytes = (
        b"d4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:"
        + b"0" * 20
        + b"ee"
    )
    expected_hash = info_hash(torrent_bytes)

    cand = AcquisitionSearchResult(
        download_id="original_id:0",
        title="Artist - Song [FLAC] (in pack: Compilation)",
        artist="Artist",
        download_url="http://indexer.local/download?apikey=secret123",
        protocol="torrent",
        extra={
            "file_indices": [0],
            "file_paths": ["01 - Song.flac"],
        },
    )

    add_resp = MagicMock(status_code=200, text="Ok.")
    files_resp = MagicMock(
        status_code=200,
        json=lambda: [{"index": 0, "name": "01 - Song.flac"}],
    )
    prio_resp = MagicMock(status_code=200, text="Ok.")
    resume_resp = MagicMock(status_code=200, text="Ok.")

    def mock_get_upload(url, *args, **kwargs):
        if "torrents/info" in url:
            return MagicMock(status_code=200, json=lambda: [])
        return files_resp

    with patch(
        "trackseerr.clients.acquisition.torznab.fetch_torrent_file",
        return_value=torrent_bytes,
    ) as mock_fetch, patch(
        "httpx.Client.post"
    ) as mock_post, patch(
        "httpx.Client.get", side_effect=mock_get_upload
    ):
        mock_post.side_effect = [add_resp, prio_resp, resume_resp]
        res_h = driver.download(cand)
        assert res_h == expected_hash

        mock_fetch.assert_called_once_with(
            "http://indexer.local/download?apikey=secret123", driver.timeout
        )

        calls = mock_post.call_args_list
        assert len(calls) >= 3

        # First call is torrents/add: multipart and stopped
        add_url, add_kwargs = calls[0][0][0], calls[0][1]
        assert "torrents/add" in add_url
        files = add_kwargs.get("files")
        data = add_kwargs.get("data")
        assert files is not None and "torrents" in files
        assert files["torrents"] == ("release.torrent", torrent_bytes, "application/x-bittorrent")
        assert "urls" not in data
        assert data.get("paused") == "true"
        assert data.get("stopped") == "true"

        # File priorities and resume
        urls = [c[0][0] for c in calls]
        assert any("torrents/filePrio" in u for u in urls)
        assert any("torrents/start" in u or "torrents/resume" in u for u in urls)


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


def test_list_unfinished_downloads_by_hash_excludes_finished_and_self(db: Database) -> None:
    db.create_download_client(
        DownloadClientConfig(
            id="c-test-store",
            name="TestClient",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://localhost:8080",
        )
    )
    db.create_active_download({
        "id": "dl-1",
        "client_id": "c-test-store",
        "title": "Song 1",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "downloading",
        "selected_files": [{"file_index": 0, "path": "s1.flac"}],
    })
    db.create_active_download({
        "id": "dl-2",
        "client_id": "c-test-store",
        "title": "Song 2",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "imported",
        "selected_files": [{"file_index": 1, "path": "s2.flac"}],
    })
    db.create_active_download({
        "id": "dl-3",
        "client_id": "c-test-store",
        "title": "Song 3",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "failed",
        "selected_files": [{"file_index": 2, "path": "s3.flac"}],
    })
    db.create_active_download({
        "id": "dl-4",
        "client_id": "c-test-store",
        "title": "Song 4",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "queued",
        "selected_files": [{"file_index": 3, "path": "s4.flac"}],
    })
    db.create_active_download({
        "id": "dl-other",
        "client_id": "c-test-store",
        "title": "Other",
        "artist": "Artist",
        "download_hash": "otherhash999",
        "status": "downloading",
        "selected_files": [{"file_index": 0, "path": "other.flac"}],
    })

    results = db.list_unfinished_downloads_by_hash("PACKHASH123")
    ids = [r["id"] for r in results]
    assert set(ids) == {"dl-1", "dl-4"}
    assert all(isinstance(r["selected_files"], list) for r in results)

    results_excluded = db.list_unfinished_downloads_by_hash("packhash123", exclude_id="dl-1")
    assert [r["id"] for r in results_excluded] == ["dl-4"]


def test_seed_cleanup_keeps_shared_torrent_while_other_song_downloads(db: Database) -> None:
    from trackseerr.seed_safety import evaluate_seed_cleanup

    db.create_download_client(
        DownloadClientConfig(
            id="c-test-safety",
            name="TestClient",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://localhost:8080",
        )
    )
    db.create_active_download({
        "id": "dl-seed",
        "client_id": "c-test-safety",
        "title": "Song 1",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "completed",
    })
    db.create_active_download({
        "id": "dl-active",
        "client_id": "c-test-safety",
        "title": "Song 2",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "downloading",
    })

    driver = MagicMock()
    driver.cleanup_completed.return_value = True

    outcome = evaluate_seed_cleanup(
        driver=driver,
        target_lookup="packhash123",
        media_settings={"seed_complete_action": "remove"},
        import_mode="copy",
        status_dict=None,
        download=db.get_active_download("dl-seed"),
        db=db,
    )

    driver.cleanup_completed.assert_not_called()
    assert outcome.status == DownloadStatus.COMPLETED.value
    assert outcome.removed is False


def test_seed_cleanup_removes_when_no_other_song_pending(db: Database) -> None:
    from trackseerr.seed_safety import evaluate_seed_cleanup

    db.create_download_client(
        DownloadClientConfig(
            id="c-test-safety2",
            name="TestClient",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://localhost:8080",
        )
    )
    db.create_active_download({
        "id": "dl-seed",
        "client_id": "c-test-safety2",
        "title": "Song 1",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "completed",
    })
    db.create_active_download({
        "id": "dl-finished",
        "client_id": "c-test-safety2",
        "title": "Song 2",
        "artist": "Artist",
        "download_hash": "packhash123",
        "status": "imported",
    })

    driver = MagicMock()
    driver.cleanup_completed.return_value = True

    outcome = evaluate_seed_cleanup(
        driver=driver,
        target_lookup="packhash123",
        media_settings={"seed_complete_action": "remove"},
        import_mode="copy",
        status_dict=None,
        download=db.get_active_download("dl-seed"),
        db=db,
    )

    driver.cleanup_completed.assert_called_once_with("packhash123", delete_files=False)
    assert outcome.status == DownloadStatus.IMPORTED.value
    assert outcome.removed is True


def _bencode_test(val: object) -> bytes:
    if isinstance(val, int):
        return f"i{val}e".encode("ascii")
    if isinstance(val, bytes):
        return f"{len(val)}:".encode("ascii") + val
    if isinstance(val, str):
        raw = val.encode("utf-8")
        return f"{len(raw)}:".encode("ascii") + raw
    if isinstance(val, list):
        return b"l" + b"".join(_bencode_test(item) for item in val) + b"e"
    if isinstance(val, dict):
        items = sorted(
            ((k.encode("utf-8") if isinstance(k, str) else k), v)
            for k, v in val.items()
        )
        return b"d" + b"".join(_bencode_test(k) + _bencode_test(v) for k, v in items) + b"e"
    raise TypeError(f"Unsupported bencode type: {type(val)}")


def _make_album_torrent_bytes() -> bytes:
    return _bencode_test(
        {
            "info": {
                "files": [
                    {"length": 15_000_000, "path": ["01 - Intro.flac"]},
                    {"length": 42_000_000, "path": ["02 - Liquid Hook (Zentura Remix).flac"]},
                    {"length": 400_000, "path": ["cover.jpg"]},
                ],
                "name": "Ace Ventura - Album [FLAC]",
                "piece length": 16384,
                "pieces": b"0" * 20,
            }
        }
    )


def test_track_request_album_torrent_selects_file(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trackseerr import track_file_selection
    from trackseerr.models import MusicRequest, RequestStatus

    db.upsert_user("user-1", "tester")
    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit-album",
            name="qBit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbittorrent:8080",
        )
    )
    db.create_request(
        MusicRequest(
            id="req-track-1",
            user_id="user-1",
            item_type="track",
            title="Liquid Hook (Zentura Remix)",
            artist="Ace Ventura",
            status=RequestStatus.APPROVED,
        )
    )

    album_bytes = _make_album_torrent_bytes()
    monkeypatch.setattr(track_file_selection, "fetch_torrent_bytes", lambda _res, timeout=15.0: album_bytes)

    cand = AcquisitionSearchResult(
        download_id="album-guid-1",
        title="Ace Ventura - Album [FLAC]",
        artist="Ace Ventura",
        download_url="http://indexer.local/dl/album.torrent",
        protocol="torrent",
        size_bytes=57_400_000,
    )

    coord = AcquisitionCoordinator()
    dispatched: list[AcquisitionSearchResult] = []

    def fake_dispatch(_db: Database, candidate: AcquisitionSearchResult):
        dispatched.append(candidate)
        return {"id": "c-qbit-album", "name": "qBit", "driver_type": "qbittorrent"}, "hash-album-1"

    with patch.object(coord, "dispatch_with_failover", side_effect=fake_dispatch), \
         patch("trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=MagicMock()):
        res = coord.grab_candidate(
            db,
            cand,
            parsed_quality="FLAC 16bit",
            score=1000,
            artist="Ace Ventura",
            album="Album",
            item_type="track",
            request_id="req-track-1",
            track_id=None,
            album_id=None,
            trigger=GrabTrigger(kind="manual"),
        )

    assert res["success"] is True
    assert len(dispatched) == 1
    assert dispatched[0].extra["file_indices"] == [1]
    assert dispatched[0].extra["file_paths"] == [
        "Ace Ventura - Album [FLAC]/02 - Liquid Hook (Zentura Remix).flac"
    ]
    assert dispatched[0].extra["torrent_bytes_size"] == 42_000_000


def test_track_request_without_match_downloads_whole_and_notes_request(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trackseerr import track_file_selection
    from trackseerr.models import MusicRequest, RequestStatus

    db.upsert_user("user-1", "tester")
    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit-whole",
            name="qBit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbittorrent:8080",
        )
    )
    db.create_request(
        MusicRequest(
            id="req-track-whole",
            user_id="user-1",
            item_type="track",
            title="Unlisted Bonus Track",
            artist="Ace Ventura",
            status=RequestStatus.APPROVED,
        )
    )

    album_bytes = _make_album_torrent_bytes()
    monkeypatch.setattr(track_file_selection, "fetch_torrent_bytes", lambda _res, timeout=15.0: album_bytes)

    cand = AcquisitionSearchResult(
        download_id="album-guid-whole",
        title="Ace Ventura - Album [FLAC]",
        artist="Ace Ventura",
        download_url="http://indexer.local/dl/album.torrent",
        protocol="torrent",
        size_bytes=57_400_000,
    )

    coord = AcquisitionCoordinator()
    dispatched: list[AcquisitionSearchResult] = []

    def fake_dispatch(_db: Database, candidate: AcquisitionSearchResult):
        dispatched.append(candidate)
        return {"id": "c-qbit-whole", "name": "qBit", "driver_type": "qbittorrent"}, "hash-album-whole"

    with patch.object(coord, "dispatch_with_failover", side_effect=fake_dispatch), \
         patch("trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=MagicMock()):
        res = coord.grab_candidate(
            db,
            cand,
            parsed_quality="FLAC 16bit",
            score=1000,
            artist="Ace Ventura",
            album="Album",
            item_type="track",
            request_id="req-track-whole",
            track_id=None,
            album_id=None,
            trigger=GrabTrigger(kind="manual"),
        )

    assert res["success"] is True
    assert len(dispatched) == 1
    assert not (dispatched[0].extra or {}).get("file_indices")
    req = db.get_request("req-track-whole")
    assert req is not None
    assert req["status_reason"] == "whole_release"
    assert req["status_message"] == (
        "Only available inside a full release: the whole release is downloading and only the requested track is imported."
    )


def test_album_request_is_never_split(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trackseerr import track_file_selection
    from trackseerr.models import MusicRequest, RequestStatus

    db.upsert_user("user-1", "tester")
    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit-alb-req",
            name="qBit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbittorrent:8080",
        )
    )
    db.create_request(
        MusicRequest(
            id="req-album-1",
            user_id="user-1",
            item_type="album",
            title="Intro",
            artist="Ace Ventura",
            status=RequestStatus.APPROVED,
        )
    )

    fetch_called = False

    def fail_if_called(_res, timeout=15.0):
        nonlocal fetch_called
        fetch_called = True
        return _make_album_torrent_bytes()

    monkeypatch.setattr(track_file_selection, "fetch_torrent_bytes", fail_if_called)

    cand = AcquisitionSearchResult(
        download_id="album-guid-full",
        title="Ace Ventura - Intro [FLAC]",
        artist="Ace Ventura",
        download_url="http://indexer.local/dl/album.torrent",
        protocol="torrent",
        size_bytes=57_400_000,
    )

    coord = AcquisitionCoordinator()
    dispatched: list[AcquisitionSearchResult] = []

    def fake_dispatch(_db: Database, candidate: AcquisitionSearchResult):
        dispatched.append(candidate)
        return {"id": "c-qbit-alb-req", "name": "qBit", "driver_type": "qbittorrent"}, "hash-album-full"

    with patch.object(coord, "dispatch_with_failover", side_effect=fake_dispatch), \
         patch("trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=MagicMock()):
        res = coord.grab_candidate(
            db,
            cand,
            parsed_quality="FLAC 16bit",
            score=1000,
            artist="Ace Ventura",
            album="Intro",
            item_type="album",
            request_id="req-album-1",
            track_id=None,
            album_id=None,
            trigger=GrabTrigger(kind="manual"),
        )

    assert res["success"] is True
    assert fetch_called is False
    assert len(dispatched) == 1
    assert not (dispatched[0].extra or {}).get("file_indices")


def test_dispatch_partial_candidate_to_rtorrent(db: Database) -> None:
    db.create_download_client(
        DownloadClientConfig(
            id="c-rtorrent",
            name="rTorrent",
            driver_type=DownloadDriverType.RTORRENT,
            host_url="http://rtorrent:8080/RPC2",
        )
    )
    coord = AcquisitionCoordinator()
    cand = AcquisitionSearchResult(
        download_id="packhash:0",
        title="Daft Punk - One More Time [FLAC] (in pack: Best)",
        artist="Daft Punk",
        protocol="torrent",
        extra={"file_indices": [0], "file_paths": ["01 - One More Time.flac"]},
    )
    mock_driver = MagicMock()
    mock_driver.download.return_value = "hash-rtorrent"

    client, dl_hash = coord.dispatch_with_failover(db, cand, driver_factory=lambda _c: mock_driver)
    assert client["id"] == "c-rtorrent"
    assert dl_hash == "hash-rtorrent"
    mock_driver.download.assert_called_once_with(cand)


def test_dispatch_partial_candidate_skips_transmission(db: Database) -> None:
    db.create_download_client(
        DownloadClientConfig(
            id="c-trans",
            name="Transmission",
            driver_type=DownloadDriverType.TRANSMISSION,
            host_url="http://transmission:9091",
            priority=1,
        )
    )
    db.create_download_client(
        DownloadClientConfig(
            id="c-rtorrent",
            name="rTorrent",
            driver_type=DownloadDriverType.RTORRENT,
            host_url="http://rtorrent:8080/RPC2",
            priority=2,
        )
    )
    coord = AcquisitionCoordinator()
    cand = AcquisitionSearchResult(
        download_id="packhash:0",
        title="Daft Punk - One More Time [FLAC] (in pack: Best)",
        artist="Daft Punk",
        protocol="torrent",
        extra={"file_indices": [0], "file_paths": ["01 - One More Time.flac"]},
    )
    mock_driver = MagicMock()
    mock_driver.download.return_value = "hash-rtorrent"

    client, dl_hash = coord.dispatch_with_failover(db, cand, driver_factory=lambda _c: mock_driver)
    assert client["id"] == "c-rtorrent"
    assert dl_hash == "hash-rtorrent"
    mock_driver.download.assert_called_once_with(cand)


def test_dispatch_partial_candidate_no_capable_client(db: Database) -> None:
    db.create_download_client(
        DownloadClientConfig(
            id="c-trans",
            name="Transmission",
            driver_type=DownloadDriverType.TRANSMISSION,
            host_url="http://transmission:9091",
        )
    )
    coord = AcquisitionCoordinator()
    cand = AcquisitionSearchResult(
        download_id="packhash:0",
        title="Daft Punk - One More Time [FLAC] (in pack: Best)",
        artist="Daft Punk",
        protocol="torrent",
        extra={"file_indices": [0], "file_paths": ["01 - One More Time.flac"]},
    )
    with pytest.raises(AcquisitionUnavailableError) as exc_info:
        coord.dispatch_with_failover(db, cand)
    assert "Only qBittorrent or rTorrent can download part of a torrent" in str(exc_info.value)


def test_select_grab_candidate_keeps_pack_for_rtorrent(db: Database) -> None:
    db.create_download_client(
        DownloadClientConfig(
            id="c-rtorrent",
            name="rTorrent",
            driver_type=DownloadDriverType.RTORRENT,
            host_url="http://rtorrent:8080/RPC2",
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
    eval_pack = EvaluationResult(
        is_acceptable=True, score=2000, rejection_reasons=[], parsed_quality="FLAC 16bit", meets_cutoff=True
    )
    cand, res, skip_reason = coord._select_grab_candidate([(pack_cand, eval_pack)], db)
    assert cand is pack_cand
    assert res is eval_pack
    assert skip_reason is None


