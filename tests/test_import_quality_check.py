"""Import quality check redesign: ~zero false positives; ERROR vs WARN classes; art subtraction; exception safety."""

import base64
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE

from trackseerr.import_quality_check import check_files, probe_audio_file
from trackseerr.quality_defaults import DEFAULT_QUALITY_DEFINITIONS

DEFS = {q: {"min_kbps": lo, "max_kbps": hi} for q, _t, lo, _p, hi in DEFAULT_QUALITY_DEFINITIONS}


def _fake(cls, length=200.0, bitrate=None, **attrs):
    audio = MagicMock(spec=cls)
    audio.info = MagicMock(spec=["length", "bitrate", *attrs.keys()])
    audio.info.length = length
    audio.info.bitrate = bitrate
    for k, v in attrs.items():
        setattr(audio.info, k, v)
    audio.tags = None
    audio.pictures = []
    return audio


def _run(tmp_path, audio, mode="reject", name="t.bin", size=1000, defs=None):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    with patch("trackseerr.import_quality_check.mutagen.File", return_value=audio):
        return check_files([p], mode, DEFS if defs is None else defs)


def _flac(kbps, bits=16, sr=44100, ch=2, **kw):
    return _fake(FLAC, bitrate=int(kbps * 1000), bits_per_sample=bits, sample_rate=sr, channels=ch, **kw)


@pytest.mark.parametrize(
    "audio",
    [
        _fake(MP3, bitrate=224000, bitrate_mode=BitrateMode.CBR),
        _fake(MP3, bitrate=256000, bitrate_mode=BitrateMode.CBR),
        _fake(MP3, bitrate=290000, bitrate_mode=BitrateMode.ABR),
        _fake(MP3, bitrate=215000, bitrate_mode=BitrateMode.ABR),
        _fake(MP4, bitrate=320000, codec="mp4a.40.2"),  # AAC 320
        _flac(1800, sr=96000),  # 16/96 FLAC
        _fake(MP4, bitrate=2500000, codec="alac", bits_per_sample=24, sample_rate=96000, channels=2),
        _fake(WAVE, bitrate=705600, bits_per_sample=16, sample_rate=44100, channels=1),  # mono WAV
        _fake(WAVE, bitrate=1411200, bits_per_sample=16, sample_rate=44100, channels=2),
        _fake(MP3, bitrate=120000, bitrate_mode=BitrateMode.VBR),  # sparse V2
        _flac(500),  # quiet classical
        _flac(300, length=10.0),  # short, near-silent intro track: warn at most
    ],
)
def test_no_false_positive_rejects(tmp_path, audio):
    res = _run(tmp_path, audio)
    assert not res.failed and not res.errors
    assert res.checked == 1


@pytest.mark.parametrize(
    "audio",
    [
        _fake(MP3, bitrate=224000, bitrate_mode=BitrateMode.CBR),
        _fake(MP4, bitrate=320000, codec="mp4a.40.2"),
        _fake(MP3, bitrate=120000, bitrate_mode=BitrateMode.VBR),
    ],
)
def test_lossy_common_encodes_not_even_warned(tmp_path, audio):
    assert not _run(tmp_path, audio, mode="warn").out_of_range


def test_lossy_out_of_range_only_warns_even_in_reject(tmp_path):
    res = _run(tmp_path, _fake(MP3, bitrate=64000, bitrate_mode=BitrateMode.CBR))
    assert len(res.out_of_range) == 1 and res.out_of_range[0].severity == "warning"
    assert not res.failed
    res = _run(tmp_path, _fake(MP3, bitrate=500000, bitrate_mode=BitrateMode.CBR))
    assert res.out_of_range and not res.failed


def test_aac_256_ceiling_only_above_25_percent(tmp_path):
    assert not _run(tmp_path, _fake(MP4, bitrate=345000, codec="mp4a.40.2"), mode="warn").out_of_range
    assert _run(tmp_path, _fake(MP4, bitrate=360000, codec="mp4a.40.2"), mode="warn").out_of_range


def test_upconverted_flac_320_is_error(tmp_path):
    res = _run(tmp_path, _flac(320))
    assert res.failed and res.errors[0].quality == "FLAC 16bit"
    assert "lossless floor" in res.reason()


def test_truncated_tiny_flac_is_error(tmp_path):
    res = _run(tmp_path, _flac(40, length=180.0))
    assert res.failed


def test_above_pcm_ceiling_is_error(tmp_path):
    res = _run(tmp_path, _flac(1600))  # 16/44.1 stereo ceiling 1411 x 1.05 = 1481
    assert res.failed and "PCM ceiling" in res.errors[0].detail
    assert not _run(tmp_path, _flac(1480)).failed


def test_user_min_above_zero_used_as_floor(tmp_path):
    defs = {**DEFS, "FLAC 16bit": {"min_kbps": 700.0, "max_kbps": 1400.0}}
    assert _run(tmp_path, _flac(600), defs=defs).failed
    assert not _run(tmp_path, _flac(800), defs=defs).failed


def test_zero_duration_error_only_with_readable_siblings(tmp_path):
    good, bad = tmp_path / "a.flac", tmp_path / "b.flac"
    good.write_bytes(b"x"), bad.write_bytes(b"x")
    results = {"a.flac": _flac(900), "b.flac": _flac(900, length=0.0)}
    with patch("trackseerr.import_quality_check.mutagen.File", side_effect=lambda p: results[Path(p).name]):
        res = check_files([good, bad], "reject", DEFS)
    assert res.failed and res.errors[0].path == str(bad)
    with patch("trackseerr.import_quality_check.mutagen.File", side_effect=lambda p: results[Path(p).name]):
        alone = check_files([bad], "reject", DEFS)
    assert not alone.failed and alone.skipped


def test_embedded_art_subtracted_from_derived_bitrate(tmp_path):
    # Opus: 100 s, 1.5 MB file of which 1 MB is cover art -> 500 kB audio = 40 kbps (not 120 kbps)
    audio = _fake(OggOpus, length=100.0, bitrate=None, sample_rate=48000, channels=2)
    audio.tags = {"metadata_block_picture": [base64.b64encode(b"a" * 1_000_000).decode()]}
    p = tmp_path / "x.opus"
    p.write_bytes(b"x" * 1_500_000)
    with patch("trackseerr.import_quality_check.mutagen.File", return_value=audio):
        probed = probe_audio_file(p)
    assert round(probed.kbps) == 40


def test_embedded_art_flac_pictures_subtracted(tmp_path):
    audio = _fake(FLAC, length=10.0, bitrate=None, bits_per_sample=16, sample_rate=44100, channels=2)
    audio.pictures = [MagicMock(data=b"a" * 500_000)]
    p = tmp_path / "x.flac"
    p.write_bytes(b"x" * 1_000_000)
    with patch("trackseerr.import_quality_check.mutagen.File", return_value=audio):
        probed = probe_audio_file(p)
    assert round(probed.kbps) == 400  # 500 kB * 8 / 10 s


@pytest.mark.parametrize("exc", [struct.error("bad"), ValueError("bad"), IndexError("x"), EOFError(), RuntimeError("x")])
def test_probe_swallows_every_exception_class(tmp_path, exc, caplog):
    p = tmp_path / "bad.mp3"
    p.write_bytes(b"x")
    with patch("trackseerr.import_quality_check.mutagen.File", side_effect=exc):
        probed = probe_audio_file(p)
        res = check_files([p], "reject", DEFS)
    assert probed.skipped_reason and type(exc).__name__ in probed.skipped_reason
    assert res.skipped and not res.failed
    assert type(exc).__name__ in caplog.text


def test_check_files_survives_post_parse_failure(tmp_path):
    audio = _fake(MP3, bitrate=320000, bitrate_mode=BitrateMode.CBR)
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    with patch("trackseerr.import_quality_check.mutagen.File", return_value=audio), patch(
        "trackseerr.import_quality_check._check_lossy", side_effect=KeyError("boom")
    ):
        res = check_files([p], "reject", DEFS)
    assert res.skipped and "KeyError" in res.skipped[0][1] and not res.failed
