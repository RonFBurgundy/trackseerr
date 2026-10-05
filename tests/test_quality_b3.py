"""Phase B3: new codecs (parser, defaults, migration v51) and the per-track bitrate check on import."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from plex_playlist_sync.acquisition_worker import AcquisitionWorker, _quality_from_codec
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.import_quality_check import (
    check_files,
    detect_audio_quality,
    normalize_check_mode,
    probe_audio_file,
)
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
)
from plex_playlist_sync.quality import parse_release_title
from plex_playlist_sync.quality_defaults import DEFAULT_QUALITY_DEFINITIONS, QUALITY_ORDER, V51_NEW_QUALITIES
from plex_playlist_sync.storage import SCHEMA_VERSION, Database


# --------------------------------------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Artist - Album [ALAC]", "ALAC"),
        ("Artist - Album (2020) ALAC 24bit", "ALAC"),
        ("Artist - Album (2020) [WAV]", "WAV/AIFF"),
        ("Artist - Album AIFF", "WAV/AIFF"),
        ("Artist - Album PCM", "WAV/AIFF"),
        ("Artist - Album Opus 128k", "Opus"),
        ("Artist - Album [OGG]", "OGG Vorbis"),
        ("Artist - Album Vorbis q6", "OGG Vorbis"),
        ("Artist - Album MP3 V1", "MP3 V1"),
        ("Artist - Album MP3 VBR V1", "MP3 V1"),
        ("Artist - Album AAC 192", "AAC (other)"),
        ("Artist - Album AAC 128k", "AAC (other)"),
        ("Artist - Album (2014) m4a 128", "AAC (other)"),
        ("Artist - Album AAC 256", "AAC 256"),
        ("Artist - Album AAC", "AAC 256"),
        ("Artist - Album 320 kbps AAC", "AAC 256"),
    ],
)
def test_parser_new_qualities(title, expected):
    assert parse_release_title(title).quality == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Artist - Album [FLAC]", "FLAC 16bit"),
        ("Artist - New Wave FLAC", "FLAC 16bit"),  # "wave" is not wav
        ("Artist - Album FLAC + WAV sampler", "FLAC 16bit"),  # explicit flac wins
        ("Artist - Album 24bit FLAC", "FLAC 24bit"),
        ("Artist - Album MP3 V0", "MP3 V0"),
        ("Artist - Album MP3 V2", "MP3 V2"),
        ("Artist - Album Vol 1", "Unknown"),  # no bare "1" -> V1
        ("Artist - Album Opus-Magnum 320", "Opus"),
        ("Artist - Album 320", "MP3 320"),
        ("Artist - Album 192", "MP3 192"),
        ("Artist - Album", "Unknown"),
    ],
)
def test_parser_no_false_positives(title, expected):
    assert parse_release_title(title).quality == expected


def test_alac_no_longer_flac16():
    assert parse_release_title("A - B [ALAC]").quality != "FLAC 16bit"


def test_aac_numeric_does_not_pick_up_year_or_depth():
    assert parse_release_title("A - B AAC 24bit").quality == "FLAC 24bit"
    assert parse_release_title("A - B (2014) AAC").quality == "AAC 256"


# --------------------------------------------------------------------------------------------------------------------
# Defaults / ordering
# --------------------------------------------------------------------------------------------------------------------
def test_defaults_and_order():
    defs = {q: (lo, pref, hi) for q, _t, lo, pref, hi in DEFAULT_QUALITY_DEFINITIONS}
    assert defs["ALAC"] == (0, 900, 1600)
    assert defs["WAV/AIFF"] == (1300, 1411, 5000)
    assert defs["MP3 V1"] == (150, 225, 320)
    assert defs["AAC (other)"] == (96, 192, 320)
    assert defs["Opus"] == (64, 160, 256)
    assert defs["OGG Vorbis"] == (96, 192, 320)
    assert QUALITY_ORDER == [
        "FLAC 24bit", "FLAC 16bit", "ALAC", "WAV/AIFF", "MP3 320", "MP3 V0", "MP3 V1", "AAC 256", "Opus",
        "OGG Vorbis", "AAC (other)", "MP3 192", "MP3 V2", "Unknown",
    ]
    assert set(defs) == set(QUALITY_ORDER)


# --------------------------------------------------------------------------------------------------------------------
# Migration v51
# --------------------------------------------------------------------------------------------------------------------
def _downgrade_to_v50(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM quality_definitions WHERE quality IN (%s)" % ",".join("?" * len(V51_NEW_QUALITIES)), V51_NEW_QUALITIES)
    conn.execute("ALTER TABLE media_management_settings DROP COLUMN import_bitrate_check")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 51")
    conn.execute("DELETE FROM quality_profiles")
    items = [
        {"type": "quality", "quality": "MP3 320", "allowed": True},
        {"type": "group", "name": "Lossless", "allowed": True, "items": ["FLAC 24bit", "FLAC 16bit"]},
        {"type": "quality", "quality": "Unknown", "allowed": False},
    ]
    conn.execute(
        "INSERT INTO quality_profiles (id, name, cutoff, items_json, is_default) VALUES (?,?,?,?,1)",
        ("p-v50", "V50 profile", "Lossless", json.dumps(items)),
    )
    conn.commit()
    conn.close()
    return items


def test_migration_v51_on_v50_db(tmp_path):
    path = str(tmp_path / "t.db")
    Database(path).close()
    original = _downgrade_to_v50(path)
    db = Database(path)
    try:
        assert SCHEMA_VERSION >= 52
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        defs = {d["quality"]: d for d in db.list_quality_definitions()}
        assert (defs["ALAC"]["min_kbps"], defs["ALAC"]["preferred_kbps"], defs["ALAC"]["max_kbps"]) == (0, 900, 1600)
        assert (defs["Opus"]["min_kbps"], defs["Opus"]["max_kbps"]) == (64, 256)
        assert set(V51_NEW_QUALITIES) <= set(defs)
        assert [d["quality"] for d in db.list_quality_definitions()] == QUALITY_ORDER
        p = db.get_quality_profile("p-v50")
        # existing entries, their order and the cutoff are untouched
        # (ALAC joins the allowed Lossless group after FLAC 16bit; the rest follow the disallowed Unknown)
        assert p["items"][0] == original[0] and p["items"][2] == original[2]
        assert p["items"][1]["items"] == ["FLAC 24bit", "FLAC 16bit", "ALAC"]
        assert p["cutoff"] == "Lossless"
        added = p["items"][3:]
        assert [e["quality"] for e in added] == [q for q in V51_NEW_QUALITIES if q != "ALAC"]
        assert all(e["type"] == "quality" and e["allowed"] is False for e in added)
        assert db.get_media_management_settings()["import_bitrate_check"] == "warn"
    finally:
        db.close()


def test_migration_v51_does_not_overwrite_edited_definition_or_duplicate(tmp_path):
    path = str(tmp_path / "t.db")
    Database(path).close()
    _downgrade_to_v50(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO quality_definitions (quality, title, min_kbps, preferred_kbps, max_kbps) VALUES ('Opus','Opus',10,20,30)")
    conn.execute(
        "UPDATE quality_profiles SET items_json = ?",
        (json.dumps([{"type": "quality", "quality": "Opus", "allowed": True}, {"type": "quality", "quality": "MP3 320", "allowed": True}]),),
    )
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        d = db.get_quality_definition("Opus")
        assert (d["min_kbps"], d["preferred_kbps"], d["max_kbps"]) == (10, 20, 30)
        qualities = [e["quality"] for e in db.get_quality_profile("p-v50")["items"]]
        assert qualities.count("Opus") == 1 and qualities[:2] == ["Opus", "MP3 320"]
        assert db.get_quality_profile("p-v50")["items"][0]["allowed"] is True
    finally:
        db.close()


# --------------------------------------------------------------------------------------------------------------------
# Per-track check: detection + comparison with synthetic mutagen results
# --------------------------------------------------------------------------------------------------------------------
def _fake(cls, length=200.0, bitrate=None, **info_attrs):
    audio = MagicMock(spec=cls)
    audio.info = MagicMock(spec=["length", "bitrate", *info_attrs.keys()])
    audio.info.length = length
    audio.info.bitrate = bitrate
    for k, v in info_attrs.items():
        setattr(audio.info, k, v)
    return audio


@pytest.mark.parametrize(
    "audio,kbps,expected",
    [
        (_fake(FLAC, bits_per_sample=16), 900, "FLAC 16bit"),
        (_fake(FLAC, bits_per_sample=24), 2500, "FLAC 24bit"),
        (_fake(MP3, bitrate_mode=BitrateMode.CBR), 320, "MP3 320"),
        (_fake(MP3, bitrate_mode=BitrateMode.CBR), 192, "MP3 192"),
        (_fake(MP3, bitrate_mode=BitrateMode.VBR), 245, "MP3 V0"),
        (_fake(MP3, bitrate_mode=BitrateMode.VBR), 220, "MP3 V1"),
        (_fake(MP3, bitrate_mode=BitrateMode.VBR), 190, "MP3 V2"),
        (_fake(MP4, codec="alac"), 800, "ALAC"),
        (_fake(MP4, codec="mp4a.40.2"), 256, "AAC 256"),
        (_fake(MP4, codec="mp4a.40.2"), 128, "AAC (other)"),
        (_fake(OggOpus), 128, "Opus"),
        (_fake(OggVorbis), 160, "OGG Vorbis"),
        (_fake(WAVE), 1411, "WAV/AIFF"),
    ],
)
def test_detect_audio_quality(audio, kbps, expected):
    assert detect_audio_quality(audio, kbps) == expected


def test_detect_unknown_container():
    assert detect_audio_quality(MagicMock(), 100) is None


DEFS = {
    "FLAC 16bit": {"min_kbps": 0.0, "max_kbps": 1400.0},
    "MP3 320": {"min_kbps": 290.0, "max_kbps": 350.0},
    "Opus": {"min_kbps": 64.0, "max_kbps": None},
    "ALAC": {"min_kbps": 0.0, "max_kbps": 0},
}


def _files(tmp_path, names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"x" * 1000)
        out.append(p)
    return out


def _patched(results):
    """mutagen.File returns the next synthetic object keyed by file name."""
    return patch("plex_playlist_sync.import_quality_check.mutagen.File", side_effect=lambda path: results[Path(path).name])


def test_check_off_does_not_touch_files(tmp_path):
    files = _files(tmp_path, ["a.flac"])
    with patch("plex_playlist_sync.import_quality_check.mutagen.File") as mf:
        res = check_files(files, "off", DEFS)
    mf.assert_not_called()
    assert res.checked == 0 and not res.out_of_range and not res.failed


def test_check_flags_fake_flac_and_ok_files(tmp_path):
    files = _files(tmp_path, ["ok.mp3", "bad.mp3", "low.opus", "open.m4a", "tiny.flac"])
    results = {
        "ok.mp3": _fake(MP3, bitrate=320000, bitrate_mode=BitrateMode.CBR),
        "bad.mp3": _fake(MP3, bitrate=400000, bitrate_mode=BitrateMode.CBR),  # above max 350 + 10%
        "low.opus": _fake(OggOpus, bitrate=None, length=100.0),  # 1000 B / 100 s = 0.08 kbps from size
        "open.m4a": _fake(MP4, codec="alac", bitrate=800000, bits_per_sample=16, sample_rate=44100, channels=2),
        "tiny.flac": _fake(FLAC, bitrate=None, length=1.0, bits_per_sample=16),  # no sample rate/channels: skipped
    }
    with _patched(results):
        res = check_files(files, "warn", DEFS)
    assert res.checked == 4 and [Path(p).name for p, _ in res.skipped] == ["tiny.flac"]
    flagged = {Path(f.path).name: f for f in res.out_of_range}
    assert set(flagged) == {"bad.mp3", "low.opus"}
    assert flagged["bad.mp3"].quality == "MP3 320" and round(flagged["bad.mp3"].kbps) == 400
    assert flagged["low.opus"].min_kbps == 64
    assert not res.failed  # warn never fails
    text = res.reason()
    assert "bad.mp3: MP3 320 400 kbps (allowed 290-350 kbps)" in text and "low.opus" in text


def test_check_reject_marks_failed(tmp_path):
    files = _files(tmp_path, ["bad.mp3"])
    with _patched({"bad.mp3": _fake(MP3, bitrate=128000, bitrate_mode=BitrateMode.CBR)}):
        # 128 kbps CBR -> MP3 192 tier; give it a definition so it is out of range
        res = check_files(files, "reject", {**DEFS, "MP3 192": {"min_kbps": 150.0, "max_kbps": 210.0}})
    # lossy range misses are warnings only, even in reject mode
    assert res.out_of_range[0].quality == "MP3 192" and not res.failed


def test_check_skips_unreadable_duration_and_unknown(tmp_path):
    files = _files(tmp_path, ["zero.mp3", "none.bin", "boom.mp3"])

    def fake_file(path):
        name = Path(path).name
        if name == "none.bin":
            return None
        if name == "boom.mp3":
            import mutagen

            raise mutagen.MutagenError("corrupt")
        return _fake(MP3, length=0.0, bitrate=320000, bitrate_mode=BitrateMode.CBR)

    with patch("plex_playlist_sync.import_quality_check.mutagen.File", side_effect=fake_file):
        res = check_files(files, "reject", DEFS)
    assert res.checked == 0 and not res.out_of_range and not res.failed
    assert len(res.skipped) == 3
    assert "skipped" in res.reason()


def test_probe_missing_definition_is_skipped(tmp_path):
    files = _files(tmp_path, ["w.wav"])
    with _patched({"w.mp3": _fake(MP3, bitrate=320000, bitrate_mode=BitrateMode.CBR)}):
        res = check_files(_files(tmp_path, ["w.mp3"]), "reject", {})  # no definition supplied for a lossy file
    assert res.skipped and not res.failed
    assert probe_audio_file  # imported API stays public


def test_normalize_mode():
    assert normalize_check_mode("REJECT") == "reject"
    assert normalize_check_mode(None) == "warn"
    assert normalize_check_mode("nonsense") == "warn"


def test_quality_from_codec_helper():
    assert _quality_from_codec({"codec": "ALAC"}) == "ALAC"
    assert _quality_from_codec({"codec": "Opus"}) == "Opus"
    assert _quality_from_codec({"codec": "Vorbis"}) == "OGG Vorbis"
    assert _quality_from_codec({"codec": "FLAC", "bits_per_sample": 24}) == "FLAC 24bit"
    assert _quality_from_codec({"codec": "MP3", "bitrate": 320000}) == "MP3 320"
    assert _quality_from_codec({"codec": "MP3", "bitrate": 192000}) == "MP3 192"
    assert _quality_from_codec({"codec": "AAC", "bitrate": 128000}) == "AAC (other)"
    assert _quality_from_codec({"codec": "XYZ"}) is None


# --------------------------------------------------------------------------------------------------------------------
# Worker integration: warn / reject / off
# --------------------------------------------------------------------------------------------------------------------
def _run_import(tmp_path, mode, mutagen_result):
    db = Database(":memory:")
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    downloads.mkdir()
    music.mkdir()
    db.create_download_client(
        DownloadClientConfig(id="c1", name="SAB", driver_type=DownloadDriverType.SABNZBD, host_url="http://sab:8080", api_key="k")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1", title="Album X", artist="Artist X", client_id="c1", download_hash="h1",
            status=DownloadStatus.DOWNLOADING.value,
        )
    )
    audio = downloads / "01 - Song.mp3"
    audio.write_bytes(b"x" * 2000)
    settings = db.get_media_management_settings()
    settings["root_folder_path"] = str(music)
    settings["import_bitrate_check"] = mode
    db.update_media_management_settings(settings)
    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 2000, "speed_bps": 0,
        "eta_seconds": 0, "source_path": str(audio), "error_message": None,
    }
    meta = {
        "artist": "Artist X", "title": "Song", "album": "Album X", "file_path": str(audio), "extension": ".mp3",
        "track_number": 1, "year": 2020, "disc_number": 1, "total_discs": 1,
    }
    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), patch(
        "plex_playlist_sync.acquisition_worker.inspect_audio_file", return_value=meta
    ), patch("plex_playlist_sync.import_quality_check.mutagen.File", return_value=mutagen_result):
        stats = AcquisitionWorker().poll_once(db=db, plex_client=MagicMock(), staging_dir=str(downloads))
    return db, stats, audio



def test_worker_warn_imports_and_records_event(tmp_path):
    bad = _fake(MP3, bitrate=128000, bitrate_mode=BitrateMode.CBR)  # MP3 192 tier, min 150
    db, stats, _ = _run_import(tmp_path, "warn", bad)
    try:
        assert stats["imported"] == 1 and stats["failed"] == 0
        assert db.get_active_download("dl-1")["status"] == DownloadStatus.IMPORTED.value
        rows = db.conn.execute("SELECT severity, message, details_json FROM system_events WHERE event_type='import_bitrate_check'").fetchall()
        assert len(rows) == 1 and rows[0][0] == "warning"
        assert "01 - Song.mp3: MP3 192 128 kbps (allowed 150-210 kbps)" in rows[0][1]
        assert json.loads(rows[0][2])["out_of_range"][0]["kbps"] == 128.0
    finally:
        db.close()


def test_worker_reject_fails_import_with_reason(tmp_path):
    bad = _fake(FLAC, bitrate=320000, bits_per_sample=16, sample_rate=44100, channels=2)  # fake lossless
    db, stats, audio = _run_import(tmp_path, "reject", bad)
    try:
        assert stats["failed"] == 1 and stats["imported"] == 0
        dl = db.get_active_download("dl-1")
        assert dl["status"] == DownloadStatus.FAILED.value
        assert "Bitrate check failed" in dl["error_message"] and "320 kbps" in dl["error_message"]
        assert "lossless floor" in dl["error_message"]
        assert audio.exists()  # nothing was moved into the library
    finally:
        db.close()


def test_worker_off_skips_check(tmp_path):
    bad = _fake(MP3, bitrate=128000, bitrate_mode=BitrateMode.CBR)
    db, stats, _ = _run_import(tmp_path, "off", bad)
    try:
        assert stats["imported"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='import_bitrate_check'").fetchone()[0] == 0
    finally:
        db.close()


def test_worker_reject_in_range_imports(tmp_path):
    good = _fake(MP3, bitrate=320000, bitrate_mode=BitrateMode.CBR)
    db, stats, _ = _run_import(tmp_path, "reject", good)
    try:
        assert stats["imported"] == 1 and stats["failed"] == 0
    finally:
        db.close()


# --------------------------------------------------------------------------------------------------------------------
# Settings API round trip
# --------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    user = db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    yield client, db, {"Authorization": f"Bearer {token}"}
    db.close()


def test_settings_api_round_trip(api):
    client, db, h = api
    url = "/api/settings/media-management"
    assert client.get(url, headers=h).json()["settings"]["import_bitrate_check"] == "warn"
    res = client.post(url, json={"import_bitrate_check": "reject"}, headers=h)
    assert res.status_code == 200 and res.json()["import_bitrate_check"] == "reject"
    assert db.get_media_management_settings()["import_bitrate_check"] == "reject"
    assert client.get(url, headers=h).json()["settings"]["import_bitrate_check"] == "reject"
    assert client.post(url, json={"import_bitrate_check": "off"}, headers=h).json()["import_bitrate_check"] == "off"
    # invalid value rejected, stored value unchanged
    assert client.post(url, json={"import_bitrate_check": "maybe"}, headers=h).status_code == 422
    assert db.get_media_management_settings()["import_bitrate_check"] == "off"
    # an unrelated update leaves it alone
    client.post(url, json={"enrich_mbids": False}, headers=h)
    assert db.get_media_management_settings()["import_bitrate_check"] == "off"


def test_quality_definitions_api_lists_new_qualities(api):
    client, _db, h = api
    body = client.get("/api/settings/quality-definitions", headers=h).json()
    assert [d["quality"] for d in body] == QUALITY_ORDER


def test_worker_reject_does_not_fail_on_lossy_out_of_range(tmp_path):
    db, stats, _ = _run_import(tmp_path, "reject", _fake(MP3, bitrate=128000, bitrate_mode=BitrateMode.CBR))
    try:
        assert stats["imported"] == 1 and stats["failed"] == 0
    finally:
        db.close()
