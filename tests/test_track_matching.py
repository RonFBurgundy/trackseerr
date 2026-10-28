"""Tests for track matching rules."""
from pathlib import Path
from unittest.mock import MagicMock

from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryTrack
from trackseerr.storage import Database
from trackseerr.track_matching import (
    find_fileless_equivalent,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
    titles_match,
)


def test_album_version_matches_plain_title() -> None:
    assert titles_match("Won't Get Fooled Again (Original Album Version)", "Won’t Get Fooled Again")
    assert not titles_match("Beat Goes On (Extended Mix)", "Beat Goes On")
    assert not titles_match("Mountain Sound (The Cabin Sessions)", "Mountain Sound")


def test_number_match_rejected_when_title_differs() -> None:
    candidates = [{"id": "t11", "title": "Butterflies", "track_number": 11, "disc_number": 1}]
    meta = {"title": "Grand", "track_number": 11, "disc_number": 1}
    track, _ = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is None or track["title"] != "Butterflies"


def test_instrumental_file_not_matched_to_vocal_by_number() -> None:
    candidates = [
        {"id": "t3", "title": "Legend Has It", "track_number": 3, "disc_number": 1},
        {"id": "t13", "title": "Legend Has It (instrumental)", "track_number": 13, "disc_number": 1},
    ]
    meta = {
        "title": "Legend Has It",
        "track_number": 3,
        "disc_number": 1,
        "file_path": "/dl/03 Legend Has It (Instrumental).flac",
    }
    track, _ = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None and track["id"] == "t13"


def test_number_match_still_strong_when_titles_agree() -> None:
    candidates = [{"id": "t1", "title": "Song", "track_number": 1, "disc_number": 1}]
    meta = {"title": "Song", "track_number": 1, "disc_number": 1}
    track, strength = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None and track["id"] == "t1"
    assert strength == "strong"


def _fileless_db(*tracks: dict) -> MagicMock:
    db = MagicMock()
    db.list_fileless_tracks_for_artist.return_value = list(tracks)
    return db


def test_fileless_equivalent_isrc_match_ignores_duration() -> None:
    track = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 230.0, "isrc": "GBASQ7800024"}
    db = _fileless_db(track)
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", 200.0, isrc=" gbasq7800024 ") == track
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", None, isrc="GBASQ7800024") == track


def test_fileless_equivalent_isrc_mismatch_falls_back_to_duration() -> None:
    track = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 230.0, "isrc": "GBASQ7800024"}
    db = _fileless_db(track)
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", 200.0, isrc="USXXX0000001") is None


def test_fileless_equivalent_two_candidates_still_none() -> None:
    t1 = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 204.0}
    t2 = {"id": "t2", "title": "This Year's Girl", "duration_seconds": 202.0}
    assert find_fileless_equivalent(_fileless_db(t1, t2), ["a1"], "This Year's Girl", 203.0) is None


def test_expected_tracks_include_same_named_sibling_album(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    artist = db.upsert_library_artist(LibraryArtist(id="art-beyonce", name="Beyoncé"))
    alb_plat = db.upsert_library_album(
        LibraryAlbum(id="alb-plat", artist_id=artist["id"], title="BEYONCÉ [Platinum Edition]", album_type="album")
    )
    alb_studio = db.upsert_library_album(
        LibraryAlbum(id="alb-studio", artist_id=artist["id"], title="BEYONCÉ", album_type="album")
    )
    t_plat = db.upsert_library_track(
        LibraryTrack(
            id="trk-plat-1",
            album_id=alb_plat["id"],
            artist_id=artist["id"],
            title="Standing on the Sun Remix",
            track_number=1,
            disc_number=1,
        )
    )
    db.upsert_library_file({"track_id": t_plat["id"], "file_path": "/music/Beyonce/Standing.flac", "cutoff_met": True})
    db.upsert_library_track(
        LibraryTrack(
            id="trk-studio-1",
            album_id=alb_studio["id"],
            artist_id=artist["id"],
            title="Haunted",
            track_number=1,
            disc_number=1,
        )
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-studio-2",
            album_id=alb_studio["id"],
            artist_id=artist["id"],
            title="Partition",
            track_number=2,
            disc_number=1,
        )
    )

    item = {"item_type": "track", "artist": "Beyoncé", "title": "Haunted"}
    req = {"artist": "Beyoncé", "album": "BEYONCÉ [Platinum Edition]", "title": "Haunted"}

    target, expected = resolve_download_expected_tracks(db, item, req)
    assert target is not None
    assert target["id"] == alb_plat["id"]
    assert target["title"] == "BEYONCÉ [Platinum Edition]"
    expected_ids = [t["id"] for t in expected]
    assert expected_ids == ["trk-plat-1", "trk-studio-1", "trk-studio-2"]
    db.close()


def test_expected_tracks_sibling_used_when_exact_missing(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    artist = db.upsert_library_artist(LibraryArtist(id="art-beyonce", name="Beyoncé"))
    alb_studio = db.upsert_library_album(
        LibraryAlbum(id="alb-studio", artist_id=artist["id"], title="BEYONCÉ", album_type="album")
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-studio-1",
            album_id=alb_studio["id"],
            artist_id=artist["id"],
            title="Haunted",
            track_number=1,
            disc_number=1,
        )
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-studio-2",
            album_id=alb_studio["id"],
            artist_id=artist["id"],
            title="Partition",
            track_number=2,
            disc_number=1,
        )
    )

    item = {"item_type": "track", "artist": "Beyoncé", "title": "Haunted"}
    req = {"artist": "Beyoncé", "album": "BEYONCÉ [Platinum Edition]", "title": "Haunted"}

    target, expected = resolve_download_expected_tracks(db, item, req)
    assert target is not None
    assert target["id"] == alb_studio["id"]
    assert target["title"] == "BEYONCÉ"
    expected_ids = [t["id"] for t in expected]
    assert expected_ids == ["trk-studio-1", "trk-studio-2"]
    db.close()


def test_album_download_ignores_siblings(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    artist = db.upsert_library_artist(LibraryArtist(id="art-beyonce", name="Beyoncé"))
    alb_plat = db.upsert_library_album(
        LibraryAlbum(id="alb-plat", artist_id=artist["id"], title="BEYONCÉ [Platinum Edition]", album_type="album")
    )
    alb_studio = db.upsert_library_album(
        LibraryAlbum(id="alb-studio", artist_id=artist["id"], title="BEYONCÉ", album_type="album")
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-plat-1",
            album_id=alb_plat["id"],
            artist_id=artist["id"],
            title="Standing on the Sun Remix",
            track_number=1,
            disc_number=1,
        )
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-studio-1",
            album_id=alb_studio["id"],
            artist_id=artist["id"],
            title="Haunted",
            track_number=1,
            disc_number=1,
        )
    )

    item = {"item_type": "album", "album_id": alb_plat["id"], "artist": "Beyoncé", "title": "BEYONCÉ [Platinum Edition]"}
    req = None

    target, expected = resolve_download_expected_tracks(db, item, req)
    assert target is not None
    assert target["id"] == alb_plat["id"]
    assert [t["id"] for t in expected] == ["trk-plat-1"]
    db.close()


def test_title_match_rejects_far_duration() -> None:
    candidates = [{"id": "t1", "title": "Partition", "duration_seconds": 229.0}]
    meta = {"title": "Partition", "duration_seconds": 138.0}
    track, strength = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is None
    assert strength == "none"


def test_title_match_accepts_close_duration() -> None:
    candidates = [{"id": "t1", "title": "Partition", "duration_seconds": 229.0}]
    meta = {"title": "Partition", "duration_seconds": 225.0}
    track, strength = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None
    assert track["id"] == "t1"
    assert strength == "strong"


def test_number_match_unaffected_by_duration() -> None:
    candidates = [{"id": "t1", "title": "Partition", "track_number": 3, "disc_number": 1, "duration_seconds": 229.0}]
    meta = {"title": "Partition", "track_number": 3, "disc_number": 1, "duration_seconds": 138.0}
    track, strength = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None
    assert track["id"] == "t1"
    assert strength == "strong"

