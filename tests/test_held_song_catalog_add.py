"""Tests for adding missing wanted songs from MusicBrainz instead of holding."""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import trackseerr.acquisition_worker
import trackseerr.mb_metadata_store
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage import Database


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "held_song_catalog_add.db"))
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _clear_mb_add_tried():
    trackseerr.acquisition_worker._MB_ADD_TRIED.clear()
    yield
    trackseerr.acquisition_worker._MB_ADD_TRIED.clear()


class _FakeEnricher:
    def __init__(
        self,
        search_result: list[dict[str, Any]] | None = None,
        release_groups: dict[str, list[dict[str, Any]]] | None = None,
        search_results_by_title: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._search_result = search_result
        self._release_groups = release_groups or {}
        self._search_results_by_title = search_results_by_title
        self.search_calls: list[tuple[str, str]] = []

    def search_recordings(self, artist: str, title: str) -> list[dict[str, Any]] | None:
        self.search_calls.append((artist, title))
        if self._search_results_by_title is not None:
            return self._search_results_by_title.get(title, [])
        return self._search_result

    def get_recording_release_groups(self, recording_id: str) -> list[dict[str, Any]]:
        return self._release_groups.get(recording_id, [])


def _seed_funkadelic(
    db: Database,
    tmp_path: Path,
    *,
    download_status: str = DownloadStatus.DOWNLOADING.value,
    held_file_path: str | None = None,
    extra_track_with_r1: bool = False,
    title: str = "Whole Lot of BS - 1972 Version",
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

    art = db.upsert_library_artist(
        LibraryArtist(id="art-func", name="Funkadelic", path=str(music / "Funkadelic"))
    )
    alb = db.upsert_library_album(
        LibraryAlbum(
            id="alb-mb",
            artist_id="art-func",
            title="Maggot Brain",
            year=1971,
            mb_release_group_id="RG1",
        )
    )
    for num in range(1, 9):
        db.upsert_library_track(
            LibraryTrack(
                id=f"trk-{num}",
                album_id="alb-mb",
                artist_id="art-func",
                title=f"Album Track {num}",
                duration_seconds=200.0,
                track_number=num,
                disc_number=1,
                monitored=True,
            )
        )
    extra_trk = None
    if extra_track_with_r1:
        extra_trk = db.upsert_library_track(
            LibraryTrack(
                id="trk-r1",
                album_id="alb-mb",
                artist_id="art-func",
                title="Whole Lot of BS (1972 version)",
                duration_seconds=241.0,
                track_number=9,
                disc_number=1,
                monitored=True,
                mb_recording_id="R1",
            )
        )

    db.create_request(
        MusicRequest(
            id="req-1",
            user_id="u1",
            item_type="track",
            title=title,
            artist="Funkadelic",
            album="Maggot Brain",
            status=RequestStatus.APPROVED,
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
            request_id="req-1",
            title=title,
            artist="Funkadelic",
            item_type="track",
            status=download_status,
            download_hash="h1",
            album_id="alb-mb",
        )
    )
    if held_file_path:
        db.set_download_unmatched_files("dl-1", [held_file_path])
        db.update_download_status(
            "dl-1",
            status=DownloadStatus.WARNING.value,
            error_message="1 file(s) couldn't be matched — manual import required",
        )
    return music, staging, art, alb, extra_trk


def _run_worker(
    db: Database,
    dl: Path,
    staging: Path,
    metas: dict[str, dict[str, Any]],
    *,
    poll_method: str = "poll_once",
):
    driver = MagicMock()
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
            "artist": "Funkadelic",
            "album": "Maggot Brain",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    worker = AcquisitionWorker()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        stats = getattr(worker, poll_method)(db=db, staging_dir=str(staging))
    return stats, driver


def _r1_candidate(length_ms: int = 241000) -> dict[str, Any]:
    return {
        "id": "R1",
        "title": "Whole Lot of BS (1972 version)",
        "artist_credit": "Funkadelic",
        "score": 100,
        "length_ms": length_ms,
        "release_groups": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}],
    }


def test_bonus_track_added_and_imported(tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch):
    music, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    new_trk = db.get_library_track_by_mb_recording_id("R1", album_id="alb-mb")
    assert new_trk is not None
    assert new_trk["track_number"] == 9
    assert db.get_library_file_for_track(str(new_trk["id"])) is not None
    assert len(fake.search_calls) == 1


def test_file_title_not_the_wanted_song_still_holds(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Other Song"
    f = dl / "01_Totally Different Song.flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Totally Different Song",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 1,
        }
    }
    fake = _FakeEnricher(search_result=[_r1_candidate(241000)])
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert fake.search_calls == []
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8


def test_no_confident_recording_still_holds(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(270000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert db.get_library_track_by_mb_recording_id("R1") is None
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8


def test_musicbrainz_down_still_holds(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(search_result=None)
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8


def test_existing_recording_track_reused(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, extra_trk = _seed_funkadelic(db, tmp_path, extra_track_with_r1=True)
    assert extra_trk is not None
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 9
    assert db.get_library_file_for_track(str(extra_trk["id"])) is not None


def test_parked_download_retried_after_add(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    dl = tmp_path / "staging" / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    _, staging, _, _, _ = _seed_funkadelic(
        db,
        tmp_path,
        download_status=DownloadStatus.WARNING.value,
        held_file_path=str(f),
    )
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    # Poll 1: _classify_held_path adds track via MB and requeues the parked download.
    _run_worker(db, dl, staging, metas, poll_method="_poll_once")
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.COMPLETED.value
    assert "Retried import" in str(dl_row.get("error_message"))

    # Poll 2: imports onto the newly created track.
    _run_worker(db, dl, staging, metas, poll_method="_poll_once")
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    new_trk = db.get_library_track_by_mb_recording_id("R1", album_id="alb-mb")
    assert new_trk is not None
    assert db.get_library_file_for_track(str(new_trk["id"])) is not None

    # Second parked file that stays unmatched: second poll must not call search_recordings again.
    db.create_request(
        MusicRequest(
            id="req-2",
            user_id="u1",
            item_type="track",
            title="Hit It and Quit It",
            artist="Funkadelic",
            album="Maggot Brain",
            status=RequestStatus.APPROVED,
        )
    )
    f2 = dl / "10_Hit It and Quit It.flac"
    _flac(f2)
    metas2 = {
        f2.name: {
            "title": "Hit It and Quit It",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 10,
        }
    }
    db.create_active_download(
        ActiveDownload(
            id="dl-2",
            client_id="c1",
            request_id="req-2",
            title="Hit It and Quit It",
            artist="Funkadelic",
            item_type="track",
            status=DownloadStatus.WARNING.value,
            download_hash="h2",
            album_id="alb-mb",
        )
    )
    db.set_download_unmatched_files("dl-2", [str(f2)])
    db.update_download_status(
        "dl-2",
        status=DownloadStatus.WARNING.value,
        error_message="1 file(s) couldn't be matched — manual import required",
    )
    unmatched_fake = _FakeEnricher(
        search_result=[
            {
                "id": "R2",
                "title": "Hit It and Quit It",
                "artist_credit": "Funkadelic",
                "score": 100,
                "length_ms": 270000,
                "release_groups": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}],
            }
        ]
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: unmatched_fake)

    _run_worker(db, dl, staging, metas2, poll_method="_poll_once")
    assert len(unmatched_fake.search_calls) == 1
    assert db.get_active_download("dl-2")["status"] == DownloadStatus.WARNING.value

    _run_worker(db, dl, staging, metas2, poll_method="_poll_once")
    assert len(unmatched_fake.search_calls) == 1
    assert db.get_active_download("dl-2")["status"] == DownloadStatus.WARNING.value


def test_compilation_holder_not_used(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    db.upsert_library_album(
        LibraryAlbum(
            id="alb-mb",
            artist_id="art-func",
            title="Greatest Hits",
            album_type="compilation",
            secondary_types=["Compilation"],
            mb_release_group_id="RG-comp",
        )
    )
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    rg_comp = {
        "id": "RG-comp",
        "title": "Greatest Hits",
        "primary_type": "Album",
        "secondary_types": ["Compilation"],
    }
    rg_album = {
        "id": "RG-album",
        "title": "Maggot Brain",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "1971-07-12",
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [rg_comp, rg_album]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    def _seed_home_album(d: Database, _enr: Any, artist_id: str, group: dict[str, Any]) -> str:
        d.upsert_library_album(
            LibraryAlbum(
                id="alb-home",
                artist_id=artist_id,
                title=str(group["title"]),
                mb_release_group_id=str(group["id"]),
                monitored=False,
            )
        )
        return "alb-home"

    with patch(
        "trackseerr.compilation_filing.ensure_release_group_album",
        side_effect=_seed_home_album,
    ) as mock_ensure:
        _run_worker(db, dl, staging, metas)

    mock_ensure.assert_called_once()
    assert mock_ensure.call_args[0][3]["id"] == "RG-album"
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert db.get_library_track_by_mb_recording_id("R1", album_id="alb-home") is not None
    assert db.get_library_track_by_mb_recording_id("R1", album_id="alb-mb") is None


def test_requested_album_title_wins(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    db.upsert_library_album(
        LibraryAlbum(
            id="alb-single",
            artist_id="art-func",
            title="Whole Lot of BS",
            album_type="single",
            mb_release_group_id="RG-single",
        )
    )
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "album": "Maggot Brain",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={
            "R1": [
                {"id": "RG-single", "title": "Whole Lot of BS", "primary_type": "Single", "secondary_types": []},
                {"id": "RG1", "title": "Maggot Brain", "primary_type": "Album", "secondary_types": []},
            ]
        },
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    with patch("trackseerr.compilation_filing.ensure_release_group_album") as mock_ensure:
        _run_worker(db, dl, staging, metas)

    mock_ensure.assert_not_called()
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert db.get_library_track_by_mb_recording_id("R1", album_id="alb-mb") is not None
    assert db.get_library_track_by_mb_recording_id("R1", album_id="alb-single") is None


def test_home_holder_reused(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "album": "",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album", "secondary_types": []}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    with patch("trackseerr.compilation_filing.ensure_release_group_album") as mock_ensure:
        _run_worker(db, dl, staging, metas)

    mock_ensure.assert_not_called()
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert db.get_library_track_by_mb_recording_id("R1", album_id="alb-mb") is not None


def test_no_home_group_still_holds(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={
            "R1": [
                {"id": "RG-va", "title": "Now Funk", "primary_type": "Album", "is_various": True},
                {"id": "RG-comp", "title": "Best Of", "primary_type": "Album", "secondary_types": ["Compilation"]},
            ]
        },
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert db.get_library_track_by_mb_recording_id("R1") is None


def test_retried_held_slskd_download_imports_without_transfer(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    dl = tmp_path / "staging" / "Funkadelic - Whole Lot of BS"
    f = dl / "09_Whole Lot of BS (1972 Version).flac"
    _flac(f)
    _, staging, _, _, _ = _seed_funkadelic(
        db,
        tmp_path,
        download_status=DownloadStatus.WARNING.value,
        held_file_path=str(f),
    )
    db.conn.execute(
        "UPDATE active_downloads SET created_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00Z", "dl-1"),
    )
    db.conn.commit()
    metas = {
        f.name: {
            "title": "Whole Lot of BS (1972 Version)",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_result=[_r1_candidate(241000)],
        release_groups={"R1": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    # Poll 1: adds track and requeues the parked download.
    _run_worker(db, dl, staging, metas, poll_method="_poll_once")
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.COMPLETED.value
    assert "Retried import" in str(dl_row.get("error_message"))

    # Poll 2: slskd driver reports transfer missing, but retried held import skips get_status and imports.
    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.DOWNLOADING.value,
        "progress": 0.0,
        "missing": True,
    }

    def inspect(path):
        return {
            "artist": "Funkadelic",
            "album": "Maggot Brain",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
            **metas.get(Path(path).name, {}),
        }

    worker = AcquisitionWorker()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        worker._poll_once(db=db, staging_dir=str(staging))

    driver.get_status.assert_not_called()
    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value


def test_base_title_fallback_adds_song(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    wanted = "I Miss My Baby - 1972 Version"
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path, title=wanted)
    dl = staging / "Funkadelic - I Miss My Baby"
    f = dl / "09_I Miss My Baby - 1972 Version.flac"
    _flac(f)
    metas = {
        f.name: {
            "title": wanted,
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_results_by_title={
            wanted: [],
            "I Miss My Baby": [
                {"id": "R-240", "title": "I Miss My Baby", "artist_credit": "Funkadelic", "score": 100, "length_ms": 240000},
                {"id": "R-302", "title": "I Miss My Baby", "artist_credit": "Funkadelic", "score": 100, "length_ms": 302000},
            ],
        },
        release_groups={"R-240": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    new_trk = db.get_library_track_by_mb_recording_id("R-240", album_id="alb-mb")
    assert new_trk is not None
    assert new_trk["title"] == wanted
    assert db.get_library_file_for_track(str(new_trk["id"])) is not None
    assert fake.search_calls == [("Funkadelic", wanted), ("Funkadelic", "I Miss My Baby")]


def test_base_title_fallback_needs_duration(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    wanted = "I Miss My Baby - 1972 Version"
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path, title=wanted)
    dl = staging / "Funkadelic - I Miss My Baby"
    f = dl / "09_I Miss My Baby - 1972 Version.flac"
    _flac(f)
    metas = {
        f.name: {
            "title": wanted,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_results_by_title={
            wanted: [],
            "I Miss My Baby": [
                {"id": "R-240", "title": "I Miss My Baby", "artist_credit": "Funkadelic", "score": 100, "length_ms": 240000},
            ],
        },
        release_groups={"R-240": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert db.get_library_track_by_mb_recording_id("R-240") is None
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8


def test_base_title_fallback_ambiguous(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    wanted = "I Miss My Baby - 1972 Version"
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path, title=wanted)
    dl = staging / "Funkadelic - I Miss My Baby"
    f = dl / "09_I Miss My Baby - 1972 Version.flac"
    _flac(f)
    metas = {
        f.name: {
            "title": wanted,
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_results_by_title={
            wanted: [],
            "I Miss My Baby": [
                {"id": "R-240", "title": "I Miss My Baby", "artist_credit": "Funkadelic", "score": 100, "length_ms": 240000},
                {"id": "R-2405", "title": "I Miss My Baby", "artist_credit": "Funkadelic", "score": 100, "length_ms": 240500},
            ],
        },
        release_groups={
            "R-240": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}],
            "R-2405": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}],
        },
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8


def test_skip_reason_logged(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path)
    dl = staging / "Funkadelic - Other Song"
    f = dl / "01_Totally Different Song.flac"
    _flac(f)
    metas = {
        f.name: {
            "title": "Totally Different Song",
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 1,
        }
    }
    fake = _FakeEnricher(search_result=[_r1_candidate(241000)])
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    with caplog.at_level(logging.INFO, logger="trackseerr.held_song_catalog"):
        _run_worker(db, dl, staging, metas)

    assert "MusicBrainz add skipped" in caplog.text
    assert "file title is not the wanted song" in caplog.text


def test_base_title_fallback_skips_live(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
):
    wanted = "Rock Star (Live)"
    _, staging, _, _, _ = _seed_funkadelic(db, tmp_path, title=wanted)
    dl = staging / "Funkadelic - Rock Star (Live)"
    f = dl / "09_Rock Star (Live).flac"
    _flac(f)
    metas = {
        f.name: {
            "title": wanted,
            "duration": 240.0,
            "duration_seconds": 240.0,
            "track_number": 9,
        }
    }
    fake = _FakeEnricher(
        search_results_by_title={
            wanted: [],
            "Rock Star": [
                {"id": "R-240", "title": "Rock Star", "artist_credit": "Funkadelic", "score": 100, "length_ms": 240000},
            ],
        },
        release_groups={"R-240": [{"id": "RG1", "title": "Maggot Brain", "primary_type": "Album"}]},
    )
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: fake)

    _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert len(db.list_library_tracks(album_id="alb-mb", limit=100)) == 8
    assert fake.search_calls == [("Funkadelic", wanted)]
