"""Import hardening: magic bytes, must-parse-as-audio, quarantine, chmod, archive bomb limits."""

import io
import os
import stat
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr import library
from trackseerr.acquisition_worker import AcquisitionWorker, place_audio_file
from trackseerr.import_security import check_magic, quarantine_files, verify_files
from trackseerr.library import ArchiveLimitError, extract_archive
from trackseerr.models import ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus
from trackseerr.storage import Database

PAD = b"\x00" * 32


def _id3(size: int = 0) -> bytes:
    sz = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + sz + b"\x00" * size


GOOD = {
    "a.flac": b"fLaC" + PAD,
    "a.mp3": b"ID3\x03\x00\x00\x00\x00\x00\x00" + PAD,
    "b.mp3": b"\xff\xfb\x90\x00" + PAD,
    "a.m4a": b"\x00\x00\x00\x20ftypM4A " + PAD,
    "a.alac": b"\x00\x00\x00\x20ftypM4A " + PAD,
    "a.aac": b"\xff\xf1\x50\x80" + PAD,
    "b.m4a": b"\xff\xf1\x50\x80" + PAD,
    "a.ogg": b"OggS\x00" + PAD,
    "a.opus": b"OggS\x00" + PAD,
    "a.wav": b"RIFF\x24\x00\x00\x00WAVEfmt " + PAD,
    "a.aiff": b"FORM\x00\x00\x00\x24AIFFCOMM" + PAD,
    "b.aiff": b"FORM\x00\x00\x00\x24AIFCCOMM" + PAD,
    "a.aif": b"FORM\x00\x00\x00\x24AIFFCOMM" + PAD,
}


@pytest.mark.parametrize("name", sorted(GOOD), ids=sorted(GOOD))
def test_magic_positive(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(GOOD[name])
    assert check_magic(f) is None


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("x.txt", date_time=(1980, 1, 1, 0, 0, 0))
        z.writestr(info, "hi")
    return buf.getvalue()


@pytest.mark.parametrize(
    "name,content",
    [
        ("evil.flac", b"MZ\x90\x00" + PAD),  # exe renamed
        ("err.mp3", b"<!DOCTYPE html><html>404</html>"),  # HTML error page
        ("arc.m4a", _zip_bytes()),  # zip renamed
        ("e.flac", b""),
        ("e.mp3", b""),
        ("e.wav", b""),
        ("x.ogg", b"RIFF\x00\x00\x00\x00WAVE" + PAD),
        ("x.wav", b"RIFF\x00\x00\x00\x00AVI " + PAD),  # RIFF but not WAVE
        ("x.aiff", b"FORM\x00\x00\x00\x00XXXX" + PAD),
        ("x.opus", b"fLaC" + PAD),
        ("x.flac", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"notflac" + PAD),  # ID3 not followed by fLaC
        ("x.m4a", b"\x00\x00\x00\x20moov" + PAD),
    ],
    ids=["evil.flac", "err.mp3", "arc.m4a", "e.flac", "e.mp3", "e.wav", "x.ogg", "x.wav", "x.aiff", "x.opus", "x.flac", "x.m4a"],
)
def test_magic_negative(tmp_path, name, content):
    f = tmp_path / name
    f.write_bytes(content)
    assert check_magic(f) is not None


def test_empty_file_reason(tmp_path):
    f = tmp_path / "e.flac"
    f.write_bytes(b"")
    assert check_magic(f) == "empty file"


@pytest.mark.parametrize("tag_size", [0, 50, 4000])
def test_id3_before_flac_accepted(tmp_path, tag_size):
    f = tmp_path / "t.flac"
    f.write_bytes(_id3(tag_size) + b"fLaC" + PAD)
    assert check_magic(f) is None


def test_id3_with_footer_before_flac(tmp_path):
    f = tmp_path / "t.flac"
    hdr = bytearray(_id3(20))
    hdr[5] |= 0x10
    f.write_bytes(bytes(hdr) + b"\x00" * 10 + b"fLaC" + PAD)
    assert check_magic(f) is None


def test_magic_micro_benchmark(tmp_path):
    files = []
    for i in range(1000):
        f = tmp_path / f"{i}.flac"
        f.write_bytes(b"fLaC" + PAD)
        files.append(f)
    start = time.perf_counter()
    assert all(check_magic(f) is None for f in files)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"magic check of 1000 files took {elapsed * 1000:.0f} ms"


# ---------------------------------------------------------------- parse requirement
def test_verify_files_unparseable_is_failure(tmp_path):
    f = tmp_path / "a.flac"
    f.write_bytes(b"fLaC" + PAD)  # right magic, but mutagen cannot parse a bare signature
    probes: dict = {}
    res = verify_files([f], probes)
    assert res.failed and "not parseable" in res.reason()
    assert str(f) in probes  # the probe is kept for reuse


def test_verify_files_probe_parse_once_and_magic_failure_not_parsed(tmp_path):
    good = tmp_path / "g.mp3"
    good.write_bytes(b"\xff\xfb\x90\x00" + PAD)
    bad = tmp_path / "b.mp3"
    bad.write_bytes(b"<html>")
    fake_audio = MagicMock()
    fake_audio.info.length = 10.0
    fake_audio.info.bitrate = 320000
    with patch("trackseerr.import_quality_check.mutagen.File", return_value=fake_audio) as mf:
        probes: dict = {}
        res = verify_files([good, bad], probes)
        assert mf.call_count == 1  # only the magic-clean file is parsed
        # check_files reuses the shared probe: no second parse
        from trackseerr.import_quality_check import check_files

        check_files([good], "warn", {}, probes=probes)
        assert mf.call_count == 1
    assert [Path(p).name for p, _ in res.failures] == ["b.mp3"]


# ---------------------------------------------------------------- worker integration
def _run(tmp_path, mode, files: dict[str, bytes], mutagen_result, driver_type=DownloadDriverType.SABNZBD,
         extra_settings=None):
    db = Database(":memory:")
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    rel = downloads / "Album"
    rel.mkdir(parents=True)
    music.mkdir()
    db.create_download_client(
        DownloadClientConfig(id="c1", name="SAB", driver_type=driver_type, host_url="http://sab:8080", api_key="k")
    )
    db.create_active_download(
        ActiveDownload(id="dl-1", title="Album X", artist="Artist X", client_id="c1", download_hash="h1",
                       status=DownloadStatus.DOWNLOADING.value)
    )
    paths = []
    for name, content in files.items():
        p = rel / name
        p.write_bytes(content)
        paths.append(p)
    settings = db.get_media_management_settings()
    settings["root_folder_path"] = str(music)
    settings["import_bitrate_check"] = mode
    settings.update(extra_settings or {})
    db.update_media_management_settings(settings)
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1,
                                      "speed_bps": 0, "eta_seconds": 0, "source_path": str(rel), "error_message": None}
    meta = {"artist": "Artist X", "title": "Song", "album": "Album X", "file_path": str(paths[0]), "extension": ".mp3",
            "track_number": 1, "year": 2020, "disc_number": 1, "total_discs": 1}
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), patch(
        "trackseerr.acquisition_worker.inspect_audio_file", return_value=meta
    ), patch("trackseerr.import_quality_check.mutagen.File", return_value=mutagen_result):
        stats = AcquisitionWorker().poll_once(db=db, plex_client=MagicMock(), staging_dir=str(downloads))
    return db, stats, paths, downloads, music


def _audio(length=100.0, bitrate=320000):
    a = MagicMock()
    a.info.length = length
    a.info.bitrate = bitrate
    return a


@pytest.mark.parametrize("mode", ["off", "warn", "reject"])
def test_unparseable_quarantined_even_with_check_off(tmp_path, mode):
    db, stats, paths, downloads, music = _run(
        tmp_path, mode, {"01.mp3": b"\xff\xfb\x90\x00" + PAD}, mutagen_result=None
    )
    assert stats["failed"] == 1 and stats["imported"] == 0
    dl = db.get_active_download("dl-1")
    assert dl["status"] == DownloadStatus.FAILED.value
    assert dl["error_message"].startswith("Security check failed: 01.mp3: ")
    assert not paths[0].exists()  # usenet source: nothing seeds, so it is moved out of the download folder
    qdir = music / ".trackseerr-quarantine" / "dl-1"
    assert qdir.is_dir() and list(qdir.iterdir())
    assert not (downloads / "_quarantine").exists()  # the legacy location is never written
    assert not any(p for p in music.rglob("*.mp3") if ".trackseerr-quarantine" not in p.parts)


def test_whole_release_rejected_and_blocklisted_and_event(tmp_path):
    db, stats, paths, downloads, music = _run(
        tmp_path,
        "off",
        {"01.mp3": b"\xff\xfb\x90\x00" + PAD, "02.mp3": b"<html>nope</html>", "03.mp3": b"\xff\xfb\x90\x00" + PAD},
        mutagen_result=_audio(),
    )
    assert stats["failed"] == 1 and stats["imported"] == 0
    assert not any(p for p in music.rglob("*") if ".trackseerr-quarantine" not in p.parts)  # nothing imported
    qdir = music / ".trackseerr-quarantine" / "dl-1"
    assert [p.name.split("_", 1)[1] for p in qdir.iterdir()] == ["02.mp3"]
    assert paths[0].exists() and paths[2].exists()  # innocent files left in staging
    rows = db.conn.execute("SELECT severity, message, details_json FROM system_events WHERE event_type='import_security'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "error" and "02.mp3" in rows[0][1] and "02.mp3" in rows[0][2]
    bl = db.conn.execute("SELECT reason FROM download_blocklist").fetchall()
    assert len(bl) == 1 and bl[0][0].startswith("Security check failed")


def test_clean_release_imports_with_check_off(tmp_path):
    db, stats, paths, downloads, music = _run(
        tmp_path, "off", {"01.mp3": b"\xff\xfb\x90\x00" + PAD}, mutagen_result=_audio()
    )
    assert stats["imported"] == 1 and stats["failed"] == 0
    placed = [p for p in music.rglob("*.mp3")]
    assert placed and stat.S_IMODE(placed[0].stat().st_mode) == 0o644


def test_quarantine_files_helper_sanitises_id(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    moved = quarantine_files([f], tmp_path / "q", "../../evil")
    assert moved and Path(tmp_path / "q") in moved[0].parents
    assert moved[0].parent.parent == (tmp_path / "q").resolve()
    assert not f.exists()


def test_quarantine_files_copy_leaves_source(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    moved = quarantine_files([f], tmp_path / "q", "dl", copy=True)
    assert f.exists() and moved[0].read_bytes() == b"x"


def test_torrent_reject_never_removes_files_from_the_torrent_root(tmp_path):
    db, stats, paths, downloads, music = _run(
        tmp_path, "off", {"01.mp3": b"\xff\xfb\x90\x00" + PAD, "02.mp3": b"<html>nope</html>"},
        mutagen_result=_audio(), driver_type=DownloadDriverType.QBITTORRENT,
    )
    assert stats["failed"] == 1 and stats["imported"] == 0
    assert all(p.exists() for p in paths)  # still where the torrent client seeds them
    qdir = music / ".trackseerr-quarantine" / "dl-1"
    copies = list(qdir.iterdir())
    assert [c.name.split("_", 1)[1] for c in copies] == ["02.mp3"]
    assert copies[0].read_bytes() == b"<html>nope</html>"
    ev = db.conn.execute("SELECT details_json FROM system_events WHERE event_type='import_security'").fetchone()
    assert '"sources_kept_for_seeding": true' in ev[0]
    assert db.get_active_download("dl-1")["status"] == DownloadStatus.FAILED.value


def test_custom_quarantine_path_is_used(tmp_path):
    custom = tmp_path / "elsewhere" / "q"
    db, stats, paths, downloads, music = _run(
        tmp_path, "off", {"01.mp3": b"<html>nope</html>"}, mutagen_result=_audio(),
        extra_settings={"quarantine_folder_path": str(custom)},
    )
    assert stats["failed"] == 1
    assert [p.name for p in (custom / "dl-1").iterdir()] == ["000_01.mp3"]
    assert not (music / ".trackseerr-quarantine").exists()


# ---------------------------------------------------------------- chmod
@pytest.mark.parametrize("mode", ["hardlink", "move"])
def test_place_clears_exec_bits(tmp_path, mode):
    src = tmp_path / "s.flac"
    src.write_bytes(b"fLaC")
    os.chmod(src, 0o755)
    dst = place_audio_file(src, tmp_path / "lib" / "d.flac", mode=mode)
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_place_copy_fallback_clears_exec_bits(tmp_path):
    src = tmp_path / "s.flac"
    src.write_bytes(b"fLaC")
    os.chmod(src, 0o755)
    with patch("trackseerr.acquisition_worker.os.link", side_effect=OSError("EXDEV")):
        dst = place_audio_file(src, tmp_path / "lib" / "d.flac", mode="hardlink")
    assert dst.stat().st_ino != src.stat().st_ino  # a real copy
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_hardlink_shares_inode(tmp_path):
    src = tmp_path / "s.flac"
    src.write_bytes(b"fLaC")
    dst = place_audio_file(src, tmp_path / "lib" / "d.flac", mode="hardlink")
    assert dst.stat().st_ino == src.stat().st_ino


# ---------------------------------------------------------------- archive limits
def _zip(tmp_path, members, name="a.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in members:
            z.writestr(n, data)
    return p


def _out(tmp_path):
    d = tmp_path / "out"
    d.mkdir(exist_ok=True)
    return d


def test_zip_within_limits_ok(tmp_path):
    z = _zip(tmp_path, [("a.flac", b"fLaC" + PAD)])
    assert len(extract_archive(z, _out(tmp_path))) == 1


def test_zip_member_count_limit(tmp_path):
    z = _zip(tmp_path, [(f"{i}.txt", b"x") for i in range(library.MAX_ARCHIVE_MEMBERS + 1)])
    with pytest.raises(ArchiveLimitError, match="members"):
        extract_archive(z, _out(tmp_path))
    assert not list(_out(tmp_path).iterdir())


def test_zip_total_size_limit(tmp_path):
    z = _zip(tmp_path, [("a.flac", b"fLaC" + PAD)])
    with patch.object(library, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 10):
        with pytest.raises(ArchiveLimitError, match="uncompressed size"):
            extract_archive(z, _out(tmp_path))


def test_zip_ratio_limit(tmp_path):
    # Real ratio, scaled: patch the compressed-size floor down rather than build a >1 MB compressed member.
    z = _zip(tmp_path, [("big.bin", b"\x00" * 5_000_000)])
    with patch.object(library, "ARCHIVE_RATIO_MIN_COMPRESSED", 100):
        with pytest.raises(ArchiveLimitError, match="ratio"):
            extract_archive(z, _out(tmp_path))


def test_zip_ratio_ignored_below_min_compressed(tmp_path):
    z = _zip(tmp_path, [("big.bin", b"\x00" * 5_000_000)])  # compresses to a few KB: under the 1 MB floor
    extract_archive(z, _out(tmp_path))


def test_zip_symlink_rejected(tmp_path):
    p = tmp_path / "s.zip"
    with zipfile.ZipFile(p, "w") as z:
        info = zipfile.ZipInfo("link.flac")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveLimitError, match="symlink"):
        extract_archive(p, _out(tmp_path))
    assert not list(_out(tmp_path).iterdir())


def test_tar_member_count_limit(tmp_path):
    import tarfile

    p = tmp_path / "a.tar"
    with tarfile.open(p, "w") as tf:
        for i in range(4):
            ti = tarfile.TarInfo(f"{i}.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    with patch.object(library, "MAX_ARCHIVE_MEMBERS", 3):
        with pytest.raises(ArchiveLimitError):
            extract_archive(p, _out(tmp_path))


def test_tar_total_size_limit(tmp_path):
    import tarfile

    p = tmp_path / "a.tar"
    with tarfile.open(p, "w") as tf:
        ti = tarfile.TarInfo("a.flac")
        ti.size = 100
        tf.addfile(ti, io.BytesIO(b"x" * 100))
    with patch.object(library, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 50):
        with pytest.raises(ArchiveLimitError, match="uncompressed size"):
            extract_archive(p, _out(tmp_path))
    assert not list(_out(tmp_path).iterdir())


@pytest.mark.parametrize("escape", [True, False])
def test_tar_symlink_member_not_extracted_as_escaping_link(tmp_path, escape):
    """Absolute/escaping symlinks are refused by filter="data" (extraction raises ValueError and nothing lands);
    a link that stays inside the target is allowed by the data filter and is not an escape."""
    import tarfile

    p = tmp_path / "l.tar"
    with tarfile.open(p, "w") as tf:
        ti = tarfile.TarInfo("link.flac")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd" if escape else "real.flac"
        tf.addfile(ti)
    out = _out(tmp_path)
    if escape:
        with pytest.raises(ValueError, match="Unsafe tar"):
            extract_archive(p, out)
        assert not (out / "link.flac").exists() and not (out / "link.flac").is_symlink()
    else:
        extract_archive(p, out)
        link = out / "link.flac"
        assert link.is_symlink() and os.readlink(link) == "real.flac"  # stays inside out/: no escape possible


def test_worker_archive_limit_records_event_and_fails(tmp_path):
    db = Database(":memory:")
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    downloads.mkdir()
    music.mkdir()
    db.create_download_client(
        DownloadClientConfig(id="c1", name="SAB", driver_type=DownloadDriverType.SABNZBD, host_url="http://sab:8080", api_key="k")
    )
    db.create_active_download(
        ActiveDownload(id="dl-1", title="Album X", artist="Artist X", client_id="c1", download_hash="h1",
                       status=DownloadStatus.DOWNLOADING.value)
    )
    arc = _zip(downloads, [("a.flac", b"fLaC" + PAD)], name="rel.zip")
    settings = db.get_media_management_settings()
    settings["root_folder_path"] = str(music)
    db.update_media_management_settings(settings)
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1,
                                      "speed_bps": 0, "eta_seconds": 0, "source_path": str(arc), "error_message": None}
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), patch.object(
        library, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1
    ):
        stats = AcquisitionWorker().poll_once(db=db, plex_client=MagicMock(), staging_dir=str(downloads))
    assert stats["failed"] == 1
    assert db.get_active_download("dl-1")["error_message"].startswith("Archive rejected")
    n = db.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='import_security' AND severity='error'").fetchone()[0]
    assert n == 1
