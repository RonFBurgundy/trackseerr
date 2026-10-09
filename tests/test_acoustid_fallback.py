"""AcoustID fingerprint fallback for weak tag matches on download import (all AcoustID calls are mocked)."""
import logging
import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library as library_mod
from trackseerr.acquisition_worker import (
    AcquisitionWorker,
)
from trackseerr.track_matching import (
    reconcile_audio_file_to_track,
    reconcile_audio_file_to_track_scored,
)
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
)
from trackseerr.storage import SCHEMA_VERSION, Database

REC_1 = "11111111-1111-1111-1111-111111111111"
REC_2 = "22222222-2222-2222-2222-222222222222"


def _create_minimal_flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def test_db(tmp_path: Path):
    db = Database(str(tmp_path / "acoustid.db"))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _admin_headers(db: Database, config: Config) -> dict[str, str]:
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(test_db: Database, test_config: Config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


# ------------------------------------------------------------------ scored reconcile

CANDS = [
    {"id": "a", "disc_number": 1, "track_number": 1, "title": "Intro", "duration_seconds": 60.0},
    {"id": "b", "disc_number": 1, "track_number": 2, "title": "Around the World", "duration_seconds": 200.0},
    {"id": "c", "disc_number": 1, "track_number": 3, "title": "Harder Better", "duration_seconds": 224.0},
]


def test_scored_unique_number_match_is_strong():
    t, s = reconcile_audio_file_to_track_scored({"track_number": 2, "disc_number": 1, "title": "zzz"}, CANDS)
    assert (t["id"], s) == ("b", "strong")


def test_scored_number_ambiguity_resolved_by_exact_title_is_strong():
    cands = [
        {"id": "x", "disc_number": 1, "track_number": 1, "title": "One"},
        {"id": "y", "disc_number": 1, "track_number": 1, "title": "Two"},
    ]
    t, s = reconcile_audio_file_to_track_scored({"track_number": 1, "disc_number": 1, "title": "Two"}, cands)
    assert (t["id"], s) == ("y", "strong")


def test_scored_number_ambiguity_duration_and_first_pick_are_weak():
    cands = [
        {"id": "x", "disc_number": 1, "track_number": 1, "title": "One", "duration_seconds": 100.0},
        {"id": "y", "disc_number": 1, "track_number": 1, "title": "Two", "duration_seconds": 200.0},
    ]
    t, s = reconcile_audio_file_to_track_scored(
        {"track_number": 1, "disc_number": 1, "title": "??", "duration": 201.0}, cands
    )
    assert (t["id"], s) == ("y", "weak")
    t, s = reconcile_audio_file_to_track_scored({"track_number": 1, "disc_number": 1, "title": "??"}, cands)
    assert (t["id"], s) == ("x", "weak")


def test_scored_unique_exact_title_is_strong():
    t, s = reconcile_audio_file_to_track_scored({"track_number": None, "title": "Harder Better"}, CANDS)
    assert (t["id"], s) == ("c", "strong")


def test_scored_duplicate_titles_are_weak():
    cands = [
        {"id": "p", "disc_number": 1, "track_number": 1, "title": "Aero", "duration_seconds": 212.0},
        {"id": "q", "disc_number": 1, "track_number": 2, "title": "Aero", "duration_seconds": 380.0},
    ]
    t, s = reconcile_audio_file_to_track_scored({"track_number": None, "title": "Aero", "duration": 214.0}, cands)
    assert (t["id"], s) == ("p", "weak")
    t, s = reconcile_audio_file_to_track_scored({"track_number": None, "title": "Aero"}, cands)
    assert (t["id"], s) == ("p", "weak")


def test_scored_fuzzy_title_is_weak():
    t, s = reconcile_audio_file_to_track_scored({"track_number": None, "title": "Harder Bettter"}, CANDS)
    assert t is not None and t["id"] == "c" and s == "weak"


def test_scored_no_match_and_empty_candidates():
    assert reconcile_audio_file_to_track_scored({"track_number": None, "title": "Nothing Alike Xyz"}, CANDS) == (None, "none")
    assert reconcile_audio_file_to_track_scored({"title": "Intro"}, []) == (None, "none")


def test_wrapper_returns_same_track_as_scored():
    meta = {"track_number": None, "title": "Harder Bettter"}
    assert reconcile_audio_file_to_track(meta, CANDS) is reconcile_audio_file_to_track_scored(meta, CANDS)[0]


# ------------------------------------------------------------------ rate limiter


def test_rate_limiter_enforces_spacing(monkeypatch):
    clock = [1000.0]
    sleeps: list[float] = []

    def fake_sleep(d: float) -> None:
        sleeps.append(d)
        clock[0] += d

    monkeypatch.setattr(library_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(library_mod, "ACOUSTID_MIN_INTERVAL_SECONDS", 0.34)
    monkeypatch.setattr(library_mod, "_acoustid_last_call", 0.0)

    library_mod._acoustid_rate_limit()  # first call: long since last call, no sleep
    assert sleeps == []
    clock[0] += 0.10
    library_mod._acoustid_rate_limit()
    assert sleeps == [pytest.approx(0.24)]
    clock[0] += 1.0  # past the interval: no further wait
    library_mod._acoustid_rate_limit()
    assert len(sleeps) == 1


def test_fingerprint_audio_file_calls_rate_limiter_before_match(tmp_path, monkeypatch):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    order: list[str] = []
    monkeypatch.setattr(library_mod, "_acoustid_rate_limit", lambda: order.append("limit"))
    fake = MagicMock()
    fake.match.side_effect = lambda *a, **k: (order.append("match"), [(0.93, REC_1, "Song", "Artist")])[1]
    with patch.dict("sys.modules", {"acoustid": fake}):
        res = library_mod.fingerprint_audio_file(audio, api_key="k")
    assert order == ["limit", "match"]
    assert res == {"score": 0.93, "recording_id": REC_1, "title": "Song", "artist": "Artist"}


# ------------------------------------------------------------------ settings / migration


def test_schema_version_is_54_and_setting_defaults_off(test_db: Database):
    assert SCHEMA_VERSION >= 54
    assert test_db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in test_db.conn.execute("PRAGMA table_info(media_management_settings)").fetchall()]
    assert "fingerprint_on_weak_match" in cols
    assert test_db.get_media_management_settings()["fingerprint_on_weak_match"] is False


def test_setting_round_trips_through_db(test_db: Database):
    out = test_db.update_media_management_settings({"fingerprint_on_weak_match": True, "acoustid_api_key": "abcd1234"})
    assert out["fingerprint_on_weak_match"] is True and out["acoustid_api_key"] == "abcd1234"
    assert test_db.update_media_management_settings({"fingerprint_on_weak_match": False})["fingerprint_on_weak_match"] is False


def test_settings_route_masks_key_and_keeps_it_when_masked_value_is_saved(client, test_db, test_config):
    headers = _admin_headers(test_db, test_config)
    resp = client.post(
        "/api/settings/media-management",
        json={"acoustid_api_key": "SECRETKEY1234", "fingerprint_on_weak_match": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["fingerprint_on_weak_match"] is True
    assert resp.json()["acoustid_api_key"] == "•" * 9 + "1234"

    got = client.get("/api/settings/media-management", headers=headers).json()["settings"]
    assert got["acoustid_api_key"] == "•" * 9 + "1234"
    assert got["fingerprint_on_weak_match"] is True

    # Saving the masked value back must not overwrite the real key.
    client.post("/api/settings/media-management", json={"acoustid_api_key": got["acoustid_api_key"]}, headers=headers)
    assert test_db.get_media_management_settings()["acoustid_api_key"] == "SECRETKEY1234"

    # An empty string clears it.
    client.post("/api/settings/media-management", json={"acoustid_api_key": ""}, headers=headers)
    assert test_db.get_media_management_settings()["acoustid_api_key"] is None


# ------------------------------------------------------------------ worker fallback


def _seed_album(db: Database, music_dir: Path, staging_dir: Path, *, fp_setting: bool, api_key: str | None):
    db.update_media_management_settings(
        {
            "root_folder_path": str(music_dir),
            "staging_folder_path": str(staging_dir),
            "library_mode": "native",
            "enrich_mbids": False,
            "fingerprint_on_weak_match": fp_setting,
            "acoustid_api_key": api_key,
        }
    )
    artist = db.upsert_library_artist(LibraryArtist(id="art-1", name="Daft Punk", path=str(music_dir / "Daft Punk")))
    album = db.upsert_library_album(
        LibraryAlbum(id="alb-1", artist_id=artist["id"], title="Discovery", year=2001, path=str(music_dir / "DP"))
    )
    t1 = db.upsert_library_track(
        LibraryTrack(id="trk-1", album_id=album["id"], artist_id=artist["id"], title="One More Time",
                     track_number=1, disc_number=1, mb_recording_id=REC_1)
    )
    t2 = db.upsert_library_track(
        LibraryTrack(id="trk-2", album_id=album["id"], artist_id=artist["id"], title="Aerodynamic",
                     track_number=2, disc_number=1, mb_recording_id=REC_2)
    )
    dl = staging_dir / "Daft.Punk.Discovery.FLAC"
    audio = dl / "mystery.flac"
    _create_minimal_flac(audio)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(id="dl-1", client_id="c1", title="Daft.Punk.Discovery.FLAC", artist="Daft Punk",
                       item_type="album", status=DownloadStatus.DOWNLOADING.value, download_hash="h",
                       album_id=album["id"])
    )
    return dl, t1, t2


def _run_import(db: Database, dl: Path, staging_dir: Path, meta: dict[str, Any], fp_result: dict[str, Any] | None):
    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1, "speed_bps": 0,
        "eta_seconds": 0, "source_path": str(dl), "error_message": None,
    }
    full_meta = {"artist": "Daft Punk", "album": "Discovery", "disc_number": 1, "codec": "FLAC",
                 "bits_per_sample": 16, "bitrate": 900, "sample_rate": 44100, "extension": ".flac", **meta}
    fp_mock = MagicMock(return_value=fp_result)
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", return_value=full_meta), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", return_value=full_meta), \
         patch("trackseerr.track_matching.fingerprint_audio_file", fp_mock):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging_dir))
    return fp_mock


WEAK_META = {"title": "Garbled Tag Title", "track_number": None, "duration": 200.0}


def test_worker_fingerprint_recording_id_beats_missing_tag_match(tmp_path, test_db):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(); staging.mkdir()
    dl, t1, t2 = _seed_album(test_db, music, staging, fp_setting=True, api_key="key")
    fp = _run_import(test_db, dl, staging, WEAK_META,
                     {"score": 0.95, "recording_id": REC_2, "title": "Aerodynamic", "artist": "Daft Punk"})
    assert fp.call_count == 1
    assert test_db.get_library_file_for_track(t2["id"]) is not None
    assert test_db.get_library_file_for_track(t1["id"]) is None


def test_worker_does_not_fingerprint_on_strong_match(tmp_path, test_db):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(); staging.mkdir()
    dl, t1, t2 = _seed_album(test_db, music, staging, fp_setting=True, api_key="key")
    fp = _run_import(test_db, dl, staging, {"title": "Aerodynamic", "track_number": 2},
                     {"score": 0.99, "recording_id": REC_1, "title": "One More Time", "artist": "x"})
    fp.assert_not_called()
    assert test_db.get_library_file_for_track(t2["id"]) is not None


@pytest.mark.parametrize("fp_setting,api_key", [(False, "key"), (True, None), (True, "")])
def test_worker_does_not_fingerprint_when_disabled_or_keyless(tmp_path, test_db, fp_setting, api_key):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(); staging.mkdir()
    dl, t1, t2 = _seed_album(test_db, music, staging, fp_setting=fp_setting, api_key=api_key)
    fp = _run_import(test_db, dl, staging, WEAK_META,
                     {"score": 0.99, "recording_id": REC_2, "title": "Aerodynamic", "artist": "x"})
    fp.assert_not_called()
    assert test_db.get_library_file_for_track(t2["id"]) is None


def test_worker_low_score_keeps_tag_result(tmp_path, test_db):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(); staging.mkdir()
    dl, t1, t2 = _seed_album(test_db, music, staging, fp_setting=True, api_key="key")
    fp = _run_import(test_db, dl, staging, WEAK_META,
                     {"score": 0.79, "recording_id": REC_2, "title": "Aerodynamic", "artist": "x"})
    assert fp.call_count == 1
    assert test_db.get_library_file_for_track(t2["id"]) is None
    assert test_db.get_library_file_for_track(t1["id"]) is None


def test_worker_fingerprint_failure_holds_unmatched_file_for_manual_import(tmp_path, test_db):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(); staging.mkdir()
    dl, _, _ = _seed_album(test_db, music, staging, fp_setting=True, api_key="key")
    _run_import(test_db, dl, staging, WEAK_META, None)
    assert not any(music.rglob("*.flac")), "an unmatched file is held, never placed under its tag names"
    assert (dl / "mystery.flac").exists()
    assert test_db.get_active_download("dl-1")["status"] == "warning"


def _fallback(settings, tracks, tag_track, strength, fp_result, caplog=None):
    from trackseerr.track_matching import _fingerprint_fallback_match

    with patch("trackseerr.track_matching.fingerprint_audio_file", return_value=fp_result) as m:
        return _fingerprint_fallback_match(Path("f.flac"), settings, tracks, tag_track, strength), m


def test_fallback_title_path_overrides_weak_pick_when_unique():
    tracks = [
        {"id": "a", "title": "One More Time", "mb_recording_id": None},
        {"id": "b", "title": "Aerodynamic", "mb_recording_id": None},
    ]
    settings = {"fingerprint_on_weak_match": True, "acoustid_api_key": "k"}
    got, _ = _fallback(settings, tracks, tracks[0], "weak",
                       {"score": 0.9, "recording_id": "unknown", "title": "Aerodynamic!", "artist": None})
    assert got["id"] == "b"


def test_fallback_title_path_ambiguous_keeps_tag(caplog):
    tracks = [{"id": "a", "title": "Same"}, {"id": "b", "title": "Same"}]
    settings = {"fingerprint_on_weak_match": True, "acoustid_api_key": "k"}
    with caplog.at_level(logging.INFO):
        got, _ = _fallback(settings, tracks, tracks[0], "weak",
                           {"score": 0.9, "recording_id": "r", "title": "Same", "artist": None})
    assert got is tracks[0]
    assert "tag-weak-kept" in caplog.text


# ------------------------------------------------------------------ manual route


def test_manual_fingerprint_route_returns_library_track(client, test_db, test_config, tmp_path):
    headers = _admin_headers(test_db, test_config)
    test_db.update_media_management_settings({"acoustid_api_key": "key", "root_folder_path": str(tmp_path)})
    test_db.upsert_library_artist({"id": "art-9", "name": "Radiohead", "monitored": True})
    test_db.upsert_library_album({"id": "alb-9", "artist_id": "art-9", "title": "OK Computer", "monitored": True})
    test_db.upsert_library_track({"id": "trk-9", "album_id": "alb-9", "artist_id": "art-9", "title": "Karma Police",
                                  "track_number": 6, "mb_recording_id": REC_1})
    audio = tmp_path / "x.flac"
    _create_minimal_flac(audio)
    fp = {"score": 0.97, "recording_id": REC_1, "title": "Karma Police", "artist": "Radiohead"}
    with patch("trackseerr.api.routes.library.manual_import.fingerprint_audio_file", return_value=fp):
        resp = client.post("/api/library/manual-import/fingerprint", json={"file_path": str(audio)}, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "fingerprint": fp,
        "library_track": {"id": "trk-9", "title": "Karma Police", "album_id": "alb-9", "artist": "Radiohead"},
    }

    fp2 = {**fp, "recording_id": "33333333-3333-3333-3333-333333333333"}
    with patch("trackseerr.api.routes.library.manual_import.fingerprint_audio_file", return_value=fp2):
        resp = client.post("/api/library/manual-import/fingerprint", json={"file_path": str(audio)}, headers=headers)
    assert resp.json()["library_track"] is None
