"""Backfill of per-track artist credits: MusicBrainz match, file-tag fallback, resumability."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.storage import Database
from trackseerr.track_credits_backfill import run_credits_backfill

from tests.test_library_api import test_db  # noqa: F401

_CREDIT = [{"name": "Bee Gees", "joinphrase": "", "artist": {"id": "mb-bg", "name": "Bee Gees"}}]


def _seed_album(db: Database, album_id: str, rg: str | None, tracks: list[dict], tmp: Path) -> list[str]:
    if db.get_library_artist("va") is None:
        db.upsert_library_artist(
            LibraryArtist(id="va", name="Various Artists", mbid="mbid-va", monitored=True, monitor_option="all")
        )
    db.upsert_library_album(
        LibraryAlbum(id=album_id, artist_id="va", title=f"Album {album_id}", mb_release_group_id=rg)
    )
    ids = []
    for spec in tracks:
        tid = f"{album_id}-{spec['n']}"
        db.upsert_library_track(
            LibraryTrack(
                id=tid, album_id=album_id, artist_id="va", title=spec["title"], track_number=spec["n"],
                disc_number=1, mb_recording_id=spec.get("rec"),
            )
        )
        if spec.get("file", True):
            path = tmp / f"{tid}.flac"
            path.write_bytes(b"x")
            db.upsert_library_file(
                LibraryFile(id=f"f-{tid}", track_id=tid, file_path=str(path), relative_path=path.name, codec="FLAC")
            )
        ids.append(tid)
    return ids


def _enricher(mb_tracks: list[dict]) -> MagicMock:
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_tracks.return_value = mb_tracks
    return enricher


def _mb(n: int, title: str, rec: str | None) -> dict:
    return {"track_number": n, "disc_number": 1, "title": title, "mb_recording_id": rec, "artist_credit": _CREDIT}


def test_backfill_uses_musicbrainz_credits_matched_by_recording_id(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    (tid,) = _seed_album(test_db, "a1", "rg1", [{"n": 1, "title": "Renamed Locally", "rec": "rec-1"}], tmp_path)
    counts = run_credits_backfill(test_db, _enricher([_mb(9, "Stayin' Alive", "rec-1")]))
    credits = test_db.list_track_artists(tid)
    assert [(c["artist_name"], c["source"]) for c in credits] == [("Bee Gees", "musicbrainz")]
    assert counts["tracks_musicbrainz"] == 1 and counts["albums"] == 1 and counts["errors"] == 0


def test_backfill_matches_by_position_and_title_not_position_alone(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    ids = _seed_album(
        test_db, "a1", "rg1",
        [{"n": 1, "title": "Stayin' Alive"}, {"n": 2, "title": "Totally Different"}],
        tmp_path,
    )
    mb = [_mb(1, "Stayin' Alive", None), _mb(2, "Night Fever", None)]
    with patch(
        "trackseerr.track_credits_backfill.inspect_audio_file", return_value={"artist": "Tag Artist"}
    ):
        counts = run_credits_backfill(test_db, _enricher(mb))
    assert test_db.list_track_artists(ids[0])[0]["source"] == "musicbrainz"
    fallback = test_db.list_track_artists(ids[1])
    assert [(c["artist_name"], c["source"]) for c in fallback] == [("Tag Artist", "tags")]
    assert counts["tracks_musicbrainz"] == 1 and counts["tracks_tags"] == 1


def test_backfill_falls_back_to_file_tags(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    ids = _seed_album(test_db, "a1", None, [{"n": 1, "title": "One"}, {"n": 2, "title": "Two"}], tmp_path)
    tags = {ids[0]: {"artist": "Bee Gees", "musicbrainz_artistid": "mb-bg"}, ids[1]: {"artist": ""}}

    def fake(path: str) -> dict:
        return tags[Path(path).stem]

    with patch("trackseerr.track_credits_backfill.inspect_audio_file", side_effect=fake):
        counts = run_credits_backfill(test_db, _enricher([]))
    credit = test_db.list_track_artists(ids[0])[0]
    assert (credit["artist_name"], credit["artist_mbid"], credit["role"], credit["source"]) == (
        "Bee Gees", "mb-bg", "primary", "tags",
    )
    assert test_db.list_track_artists(ids[1]) == []
    assert counts["tracks_tags"] == 1 and counts["tracks_skipped"] == 1


def test_backfill_skips_tracks_with_credits_and_albums_without_files(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    (done,) = _seed_album(test_db, "a1", "rg1", [{"n": 1, "title": "Done"}], tmp_path)
    test_db.replace_track_artists(
        done, [{"artist_name": "Kept", "artist_mbid": None, "role": "primary", "join_phrase": "", "source": "musicbrainz"}]
    )
    (nofile,) = _seed_album(test_db, "a2", "rg2", [{"n": 1, "title": "Nofile", "file": False}], tmp_path)
    enricher = _enricher([_mb(1, "Done", None)])
    with patch("trackseerr.track_credits_backfill.inspect_audio_file") as inspect:
        counts = run_credits_backfill(test_db, enricher)
    assert counts["albums"] == 0
    enricher.get_release_group_tracks.assert_not_called()
    inspect.assert_not_called()
    assert test_db.list_track_artists(done)[0]["artist_name"] == "Kept"
    assert test_db.list_track_artists(nofile) == []


def test_backfill_continues_after_album_error(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    _seed_album(test_db, "a1", "rg-bad", [{"n": 1, "title": "Bad"}], tmp_path)
    (good,) = _seed_album(test_db, "a2", "rg-ok", [{"n": 1, "title": "Good"}], tmp_path)

    def fake(rg: str) -> list[dict]:
        if rg == "rg-bad":
            raise RuntimeError("boom")
        return [_mb(1, "Good", None)]

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_tracks.side_effect = fake
    counts = run_credits_backfill(test_db, enricher)
    assert counts["errors"] == 1 and counts["albums"] == 1
    assert test_db.list_track_artists(good)[0]["source"] == "musicbrainz"


def test_backfill_stops_when_asked(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    _seed_album(test_db, "a1", "rg1", [{"n": 1, "title": "One"}], tmp_path)
    _seed_album(test_db, "a2", "rg2", [{"n": 1, "title": "Two"}], tmp_path)
    enricher = _enricher([_mb(1, "One", None)])
    counts = run_credits_backfill(test_db, enricher, should_stop=lambda: True)
    assert counts["albums"] == 0
    enricher.get_release_group_tracks.assert_not_called()


def test_list_albums_needing_credits_keyset_paging(test_db: Database, tmp_path: Path) -> None:  # noqa: F811
    for i in range(5):
        _seed_album(test_db, f"a{i}", None, [{"n": 1, "title": f"T{i}"}], tmp_path)
    seen: list[str] = []
    after = None
    while True:
        page = test_db.list_albums_needing_credits(after, 2)
        if not page:
            break
        seen.extend(a["id"] for a in page)
        after = page[-1]["id"]
    assert seen == ["a0", "a1", "a2", "a3", "a4"]
    test_db.replace_track_artists(
        "a1-1", [{"artist_name": "X", "artist_mbid": None, "role": "primary", "join_phrase": "", "source": "tags"}]
    )
    assert [a["id"] for a in test_db.list_albums_needing_credits("a0", 10)] == ["a2", "a3", "a4"]
    tracks = test_db.list_album_tracks_with_files("a1")
    assert tracks[0]["has_credits"] is True and tracks[0]["file_path"].endswith("a1-1.flac")
