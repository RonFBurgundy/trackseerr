"""Tests for finding owned tracks under related/primary artists."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.backlog_worker import WantedBacklogWorker
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    MusicRequest,
    RequestStatus,
)
from trackseerr.request_submission import _library_has
from trackseerr.storage import Database
from trackseerr.track_matching import find_owned_for_item


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "owned_related_artist.db"))
    yield d
    d.close()


def _seed_costello(
    db: Database,
    tmp_path: Path,
    *,
    with_download: bool = False,
    download_status: str = DownloadStatus.DOWNLOADING.value,
    held_file_path: str | None = None,
    track_duration: float | None = 158.0,
):
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

    # Artist 1: "Elvis Costello" with FILED "Hand in Hand" on "This Year's Model" (158s, cutoff met)
    art_ec = db.upsert_library_artist(
        LibraryArtist(id="art-ec", name="Elvis Costello", clean_name="elvis costello", path=str(music / "Elvis Costello"))
    )
    alb_tym = db.upsert_library_album(
        LibraryAlbum(id="alb-tym", artist_id="art-ec", title="This Year's Model", clean_title="this years model", year=1978)
    )
    trk_hih = db.upsert_library_track(
        LibraryTrack(
            id="trk-hih",
            album_id="alb-tym",
            artist_id="art-ec",
            title="Hand in Hand",
            clean_title="hand in hand",
            duration_seconds=track_duration,
            track_number=7,
            disc_number=1,
            monitored=True,
        )
    )
    db.upsert_library_file(
        LibraryFile(
            id="fil-hih",
            track_id="trk-hih",
            file_path=str(music / "Elvis Costello" / "This Year's Model" / "07 Hand in Hand.flac"),
            relative_path="Elvis Costello/This Year's Model/07 Hand in Hand.flac",
            codec="flac",
            cutoff_met=True,
        )
    )

    # Artist 2: "Elvis Costello & The Attractions" with target album "Armed Forces" and no copy of that song
    art_ecta = db.upsert_library_artist(
        LibraryArtist(
            id="art-ecta",
            name="Elvis Costello & The Attractions",
            clean_name="elvis costello the attractions",
            path=str(music / "Elvis Costello & The Attractions"),
        )
    )
    alb_af = db.upsert_library_album(
        LibraryAlbum(id="alb-af", artist_id="art-ecta", title="Armed Forces", clean_title="armed forces", year=1979)
    )
    trk_awh = db.upsert_library_track(
        LibraryTrack(
            id="trk-awh",
            album_id="alb-af",
            artist_id="art-ecta",
            title="Accidents Will Happen",
            clean_title="accidents will happen",
            duration_seconds=180.0,
            track_number=1,
            disc_number=1,
            monitored=False,
        )
    )

    if with_download:
        db.create_download_client(
            DownloadClientConfig(
                id="c1",
                name="C",
                driver_type=DownloadDriverType.SLSKD,
                host_url="http://slskd:5030",
            )
        )
        db.create_active_download(
            ActiveDownload(
                id="dl-1",
                client_id="c1",
                title="Elvis Costello & The Attractions - Armed Forces",
                artist="Elvis Costello & The Attractions",
                item_type="album",
                status=download_status,
                download_hash="h1",
                album_id="alb-af",
            )
        )
        if held_file_path:
            db.set_download_unmatched_files("dl-1", [held_file_path])
            db.update_download_status(
                "dl-1",
                status=DownloadStatus.WARNING.value,
                error_message="1 file(s) couldn't be matched — manual import required",
            )

    return music, staging, art_ec, alb_tym, trk_hih, art_ecta, alb_af, trk_awh


def _run_worker(
    db: Database,
    dl: Path,
    staging: Path,
    metas: dict[str, dict[str, Any]],
    driver: MagicMock | None = None,
):
    driver = driver or MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl),
        "error_message": None,
    }

    def inspect(path):
        base = {
            "artist": "Elvis Costello & The Attractions",
            "album": "Armed Forces",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        stats = AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))
    return stats, driver


def test_find_owned_for_item_checks_primary_artist(tmp_path: Path, db: Database):
    """item artist 'Elvis Costello & The Attractions', title 'Hand In Hand - 2021 Remaster' -> returns the filed track."""
    _seed_costello(db, tmp_path)
    res = find_owned_for_item(
        db,
        {"artist": "Elvis Costello & The Attractions"},
        "Hand In Hand - 2021 Remaster",
        duration_seconds=158.0,
    )
    assert res is not None
    assert res["id"] == "trk-hih"
    assert res["cutoff_met"] is True


def test_find_owned_for_item_respects_multiple_editions(tmp_path: Path, db: Database):
    """Same check returns None when allow_multiple_editions is True."""
    _seed_costello(db, tmp_path)
    db.update_media_management_settings({"allow_multiple_editions": True})
    res = find_owned_for_item(
        db,
        {"artist": "Elvis Costello & The Attractions"},
        "Hand In Hand - 2021 Remaster",
        duration_seconds=158.0,
    )
    assert res is None


def test_find_owned_for_item_unrelated_artist_none(tmp_path: Path, db: Database):
    """item artist 'Costello Tribute Band' -> None."""
    _seed_costello(db, tmp_path)
    res = find_owned_for_item(
        db,
        {"artist": "Costello Tribute Band"},
        "Hand In Hand - 2021 Remaster",
        duration_seconds=158.0,
    )
    assert res is None


def test_regrabbed_owned_song_discarded_and_request_available(tmp_path: Path, db: Database):
    """Download for a processing request is discarded as duplicate and request marked AVAILABLE."""
    music, staging, _, _, _, _, _, _ = _seed_costello(db, tmp_path)
    db.create_request(
        MusicRequest(
            id="req-1",
            user_id="u1",
            item_type="track",
            title="Hand In Hand - 2021 Remaster",
            artist="Elvis Costello & The Attractions",
            status=RequestStatus.PROCESSING,
        )
    )
    db.create_download_client(
        DownloadClientConfig(
            id="c1",
            name="C",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Elvis Costello & The Attractions - Armed Forces",
            artist="Elvis Costello & The Attractions",
            item_type="album",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="h1",
            album_id="alb-af",
            request_id="req-1",
        )
    )
    dl = staging / "Armed Forces"
    dl.mkdir(parents=True, exist_ok=True)
    f = dl / "07 - Hand in Hand [FLAC 24bit].flac"
    _flac(f)

    metas = {
        "07 - Hand in Hand [FLAC 24bit].flac": {
            "artist": "Elvis Costello & The Attractions",
            "title": "Hand in Hand",
            "duration": 158.0,
        }
    }
    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert dl_row["unmatched_files"] == []

    req_row = db.get_request("req-1")
    assert req_row["status"] == RequestStatus.AVAILABLE.value


def test_parked_row_for_owned_related_song_cleared(tmp_path: Path, db: Database):
    """An already-parked WARNING row with duplicate held file on disk is cleared on poll."""
    music, staging, _, _, _, _, _, _ = _seed_costello(db, tmp_path)
    db.create_request(
        MusicRequest(
            id="req-1",
            user_id="u1",
            item_type="track",
            title="Hand In Hand - 2021 Remaster",
            artist="Elvis Costello & The Attractions",
            status=RequestStatus.PROCESSING,
        )
    )
    dl = staging / "Armed Forces"
    dl.mkdir(parents=True, exist_ok=True)
    f = dl / "07 - Hand in Hand [FLAC 24bit].flac"
    _flac(f)

    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Armed Forces",
            artist="Elvis Costello & The Attractions",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-af",
            request_id="req-1",
        )
    )
    db.set_download_unmatched_files("dl-1", [str(f)])
    db.update_download_status(
        "dl-1",
        status=DownloadStatus.WARNING.value,
        error_message="1 file(s) couldn't be matched — manual import required",
    )

    metas = {
        "07 - Hand in Hand [FLAC 24bit].flac": {
            "artist": "Elvis Costello & The Attractions",
            "title": "Hand in Hand",
            "duration": 158.0,
        }
    }
    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert dl_row["unmatched_files"] == []
    assert not f.exists()

    req_row = db.get_request("req-1")
    assert req_row["status"] == RequestStatus.AVAILABLE.value


def test_library_has_sees_related_artist(tmp_path: Path, db: Database):
    """_library_has sees track owned under primary artist."""
    _seed_costello(db, tmp_path)
    res = _library_has(
        db,
        item_type="track",
        artist="Elvis Costello & The Attractions",
        title="Hand In Hand - 2021 Remaster",
        album=None,
        foreign_id=None,
    )
    assert res == "Already in your library"


def test_backlog_skips_and_closes_request_owned_under_related_artist(tmp_path: Path, db: Database):
    """Open processing track request for song owned under related artist is skipped and marked AVAILABLE."""
    _seed_costello(db, tmp_path)
    db.create_indexer(
        {
            "id": "idx-1",
            "name": "Prowlarr",
            "indexer_type": "torznab",
            "host_url": "http://127.0.0.1:9696/1",
            "api_key": "key",
            "enabled": True,
        }
    )
    db.create_download_client(
        {
            "id": "c1",
            "name": "qBit",
            "driver_type": "qbittorrent",
            "host_url": "http://127.0.0.1:8080",
            "enabled": True,
        }
    )
    db.create_request(
        MusicRequest(
            id="req-open",
            user_id="u1",
            item_type="track",
            title="Hand In Hand - 2021 Remaster",
            artist="Elvis Costello & The Attractions",
            status=RequestStatus.PROCESSING,
        )
    )
    worker = WantedBacklogWorker()
    mock_grab = MagicMock(return_value={"success": True, "download_id": "dl-auto-1"})
    with patch("trackseerr.backlog_worker.acquisition_coordinator.search_and_grab", mock_grab):
        stats = worker.poll_once(db)
        assert mock_grab.call_count == 0
        req_row = db.get_request("req-open")
        assert req_row["status"] == RequestStatus.AVAILABLE.value


def test_wrong_song_by_related_artist_still_held(tmp_path: Path, db: Database):
    """File title 'Radio Radio' has no match and is still held as WARNING."""
    music, staging, _, _, _, _, _, _ = _seed_costello(db, tmp_path, with_download=True)
    dl = staging / "Armed Forces"
    dl.mkdir(parents=True, exist_ok=True)
    f = dl / "Radio Radio.flac"
    _flac(f)

    metas = {
        "Radio Radio.flac": {
            "artist": "Elvis Costello & The Attractions",
            "title": "Radio Radio",
            "duration": 185.0,
        }
    }
    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert str(f) in dl_row["unmatched_files"]
