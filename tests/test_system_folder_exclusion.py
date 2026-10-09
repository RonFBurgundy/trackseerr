"""OS/NAS system and trash folders are never library, import or metadata-lookup content."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.api.routes.library.manual_import import _walk_audio_files
from trackseerr.artist_refresh import refresh_single_artist
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_health import _walk_audio
from trackseerr.library_scanner import LibraryScanner
from trackseerr.recycle_bin import is_system_dirname, is_system_filename, is_system_folder_name
from trackseerr.storage import Database

JUNK = [
    "$RECYCLE.BIN/S-1-5-21-111-222-333-1001/$I123ABC.mp3",
    "$recycle.bin/S-1-5-21-111-222-333-1001/$RABC123.flac",
    ".Trash-1000/x.flac",
    ".Trashes/501/y.flac",
    "@eaDir/song.flac/SYNOAUDIO.flac",
    "#recycle/gone.flac",
    "System Volume Information/z.flac",
    "Some Artist/.stfolder/a.flac",
    "Some Artist/Album/._song.flac",
    "Some Artist/Album/.DS_Store",
    "RECYCLER/q.mp3",
    "lost+found/q.mp3",
]
REAL = "Recycle Bin/Album/01.flac"


def _build(root: Path) -> Path:
    for rel in [*JUNK, REAL]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root / REAL


@pytest.mark.parametrize("name", [
    "$RECYCLE.BIN", "$recycle.bin", "RECYCLER", "RECYCLED", "System Volume Information", "lost+found", ".Trash",
    ".Trash-1000", ".Trashes", "#recycle", "#snapshot", "@eaDir", "@Recycle", "@Recently-Snapshot", ".recycle",
    ".AppleDouble", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", ".stfolder",
    ".stversions", ".sync", ".git", ".trackseerr-recycle", ".trackseerr-quarantine", ".Trash-5",
])
def test_system_dirnames(name):
    assert is_system_dirname(name)


def test_real_names_are_not_system():
    assert not is_system_dirname("Recycle Bin")
    assert not is_system_dirname("Pink Floyd")
    assert not is_system_folder_name("Recycle Bin")
    assert is_system_folder_name("$RECYCLE.BIN")
    assert is_system_folder_name("S-1-5-21-1-2-3")
    assert is_system_folder_name("$I123ABC") and is_system_folder_name("$rabc123")
    assert is_system_filename("._x.flac") and is_system_filename("Thumbs.db") and is_system_filename("desktop.ini")
    assert not is_system_filename("01.flac")


DOTTED = [".38 Special/Album/01.flac", "...And You Will Know Us by the Trail of Dead/X/01.flac", "$uicideboy$/Album/01.flac"]


@pytest.mark.parametrize("name", [".38 Special", "...And You Will Know Us by the Trail of Dead", ".Flow",
                                  "...Baby One More Time", "$uicideboy$", "$NOT", "$ki Mask the Slump God", "._dir"])
def test_real_dotted_and_dollar_names_are_not_system(name):
    assert not is_system_dirname(name)
    assert not is_system_folder_name(name)


def test_scanner_indexes_dotted_and_dollar_artists(tmp_path: Path):
    music = tmp_path / "music"
    for rel in DOTTED:
        p = music / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    db = Database(tmp_path / "t.db")

    def fake_inspect(path):
        p = Path(path)
        return {
            "title": "One", "artist": p.parts[-3], "album": p.parts[-2], "track_number": 1, "disc_number": 1,
            "year": 2000, "total_tracks": 1, "duration": 1.0, "codec": "FLAC", "bitrate": 1, "sample_rate": 44100,
            "bits_per_sample": 16, "quality_full": "FLAC", "file_path": str(p.resolve()),
        }

    try:
        with patch("trackseerr.library_scanner.inspect_audio_file", side_effect=fake_inspect), patch(
            "trackseerr.artist_refresh_worker.artist_refresh_worker.refresh_once"
        ):
            status = LibraryScanner().scan(db, root_folder=str(music))
        assert status["files_indexed"] == 3
        names = {a["name"] for a in db.list_library_artists(limit=100)}
        assert names == {".38 Special", "...And You Will Know Us by the Trail of Dead", "$uicideboy$"}
    finally:
        db.close()


def test_walks_index_dotted_and_dollar_artists(tmp_path: Path):
    for rel in DOTTED:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert len(list(_walk_audio_files(tmp_path))) == 3
    assert len(list(_walk_audio(tmp_path, frozenset({".flac"})))) == 3


def test_lookups_happen_for_dotted_and_dollar_artists():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"artists": [], "data": []}
    enricher = MbidEnricherClient()
    with patch.object(MbidEnricherClient, "_request", return_value=resp) as req:
        enricher.lookup_artist_mbid("$uicideboy$")
        enricher.lookup_artist_mbid(".38 Special")
    assert req.call_count == 2
    discovery = DiscoveryClient()
    with patch.object(discovery, "session") as sess:
        sess.get.return_value = resp
        discovery.search_artist("$uicideboy$")
        discovery.search_artist(".38 Special")
    assert sess.get.call_count == 2


def test_scanner_indexes_only_the_real_band(tmp_path: Path):
    music = tmp_path / "music"
    real = _build(music)
    db = Database(tmp_path / "t.db")
    seen: list[str] = []

    def fake_inspect(path):
        p = Path(path)
        seen.append(str(p))
        return {
            "title": "One", "artist": p.parts[-3], "album": p.parts[-2], "track_number": 1, "disc_number": 1,
            "year": 2000, "total_tracks": 1, "duration": 1.0, "codec": "FLAC", "bitrate": 1, "sample_rate": 44100,
            "bits_per_sample": 16, "quality_full": "FLAC", "file_path": str(p.resolve()),
        }

    try:
        with patch("trackseerr.library_scanner.inspect_audio_file", side_effect=fake_inspect), patch(
            "trackseerr.artist_refresh_worker.artist_refresh_worker.refresh_once"
        ):
            status = LibraryScanner().scan(db, root_folder=str(music))
        assert status["total_files_found"] == 1
        assert status["files_indexed"] == 1
        assert seen == [str(real.resolve())]
        names = {a["name"] for a in db.list_library_artists(limit=100)}
        assert names == {"Recycle Bin"}
    finally:
        db.close()


def test_health_and_import_walks_skip_junk(tmp_path: Path):
    _build(tmp_path)
    exts = frozenset({".flac", ".mp3"})
    assert [Path(p).name for p in _walk_audio(tmp_path, exts)] == ["01.flac"]
    assert [p.name for p in _walk_audio_files(tmp_path)] == ["01.flac"]


def test_no_lookup_for_system_folder_names():
    enricher = MbidEnricherClient()
    discovery = DiscoveryClient()
    with patch.object(MbidEnricherClient, "_request") as req, patch(
        "trackseerr.clients.discovery.requests.get"
    ) as rget:
        assert enricher.lookup_artist_mbid("$RECYCLE.BIN") is None
        assert enricher.lookup_album_mbids("$RECYCLE.BIN", "S-1-5-21-1") is None
        assert discovery.search_artist("$RECYCLE.BIN") is None
        assert discovery.search_artist(".Trash-1000") is None
    req.assert_not_called()
    rget.assert_not_called()


def test_refresh_skips_system_artist(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    try:
        art = db.upsert_library_artist({"name": "$RECYCLE.BIN"})
        enricher, discovery = MagicMock(), MagicMock()
        res = refresh_single_artist(artist_id=art["id"], db=db, discovery_client=discovery, enricher=enricher)
        assert res["success"] is False
        enricher.lookup_artist_mbid.assert_not_called()
        discovery.search_artist.assert_not_called()
        enricher.get_artist_details.assert_not_called()
    finally:
        db.close()
