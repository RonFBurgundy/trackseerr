"""Release year and track count on native artist release cards: ingest, refresh, backfill, detail payload."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_playlist_sync.api.routes.library import (
    IngestArtistRequest,
    get_artist,
    ingest_artist,
    refresh_single_artist,
)
from plex_playlist_sync.album_track_hydration import hydrate_album_tracks
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.models import LibraryAlbum, LibraryArtist
from plex_playlist_sync.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


def _resp(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def test_deezer_artist_details_carry_release_date_and_nb_tracks():
    client = DiscoveryClient()
    client.session = MagicMock()

    def fake_get(url, **_kw):
        if url.endswith("/artist/7"):
            return _resp({"id": 7, "name": "A", "nb_album": 2})
        return _resp(
            {
                "data": [
                    {"id": 1, "title": "Full", "release_date": "2020-05-01", "record_type": "album", "nb_tracks": 12},
                    {"id": 2, "title": "NoCount", "release_date": "2021-01-01", "record_type": "ep"},
                ]
            }
        )

    client.session.get.side_effect = fake_get
    d = client._get_deezer_artist_details("7")
    assert d["albums"][0]["release_date"] == "2020-05-01"
    assert d["albums"][0]["track_count"] == 12
    assert d["singles_eps"][0]["track_count"] is None  # unknown, never 0


def test_itunes_artist_details_carry_track_count():
    client = DiscoveryClient()
    client.session = MagicMock()
    client.session.get.return_value = _resp(
        {
            "results": [
                {"wrapperType": "artist", "artistName": "A", "artistId": 9},
                {
                    "wrapperType": "collection",
                    "collectionId": 5,
                    "collectionName": "Big",
                    "artistName": "A",
                    "releaseDate": "2019-03-03T08:00:00Z",
                    "trackCount": 14,
                    "collectionType": "Album",
                },
            ]
        }
    )
    d = client._get_itunes_artist_details("9")
    assert d["albums"][0]["track_count"] == 14
    assert d["albums"][0]["release_date"].startswith("2019")


def test_ingest_persists_year_and_total_tracks_and_detail_payload_reports_them(db: Database):
    discovery = MagicMock(spec=DiscoveryClient)
    discovery.get_artist_details.return_value = {
        "albums": [
            {"id": "deezer:album:1", "title": "Full", "release_date": "2020-05-01", "record_type": "album", "track_count": 12},
            {"id": "deezer:album:2", "title": "Bare", "release_date": "2018-01-01", "record_type": "album"},
        ],
        "singles_eps": [],
        "compilations": [],
    }
    res = ingest_artist(
        IngestArtistRequest(foreign_artist_id="deezer:artist:7", artist_name="A", monitor_option="none", monitored=False),
        db=db,
        discovery_client=discovery,
        _admin={},
        lidarr_client=None,
        enricher=MagicMock(spec=MbidEnricherClient),
    )
    albums = {a["title"]: a for a in get_artist(res["id"], db=db, client=None, _admin={})["albums"]}
    assert albums["Full"]["release_date"] == "2020-05-01" and albums["Full"]["year"] == 2020
    assert albums["Full"]["track_count"] == 12  # provider count before any track rows exist
    assert albums["Full"]["track_file_count"] == 0
    assert albums["Bare"]["track_count"] == 0  # unknown count, no rows


def test_ingest_stores_count_from_album_details(db: Database):
    discovery = MagicMock(spec=DiscoveryClient)
    discovery.get_artist_details.return_value = {
        "albums": [{"id": "deezer:album:1", "title": "Mon", "release_date": "2020-05-01", "record_type": "album"}],
        "singles_eps": [],
        "compilations": [],
    }
    discovery.get_album_details.return_value = {
        "track_count": 3,
        "tracks": [{"id": f"t{i}", "title": f"T{i}", "track_number": i} for i in (1, 2, 3)],
    }
    res = ingest_artist(
        IngestArtistRequest(foreign_artist_id="deezer:artist:7", artist_name="B", monitor_option="all"),
        db=db,
        discovery_client=discovery,
        _admin={},
        lidarr_client=None,
        enricher=MagicMock(spec=MbidEnricherClient),
    )
    alb = get_artist(res["id"], db=db, client=None, _admin={})["albums"][0]
    assert alb["total_tracks"] == 3 and alb["track_count"] == 3


def test_refresh_musicbrainz_backfills_release_date_and_track_count(db: Database):
    """Rows created before the fix have a year but no release_date and no count; refresh fills both."""
    db.upsert_library_artist(LibraryArtist(id="ar", name="12th Planet", mbid="mb-ar", monitored=True, monitor_option="none"))
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Monomyth", clean_title="monomyth", mb_release_group_id="rg1",
                     year=2014, monitored=False)
    )
    before = get_artist("ar", db=db, client=None, _admin={})["albums"][0]
    assert not before["release_date"] and before["track_count"] == 0

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mb-ar"}
    enricher.get_artist_discography.return_value = [
        {"id": "rg1", "title": "Monomyth", "album_type": "album", "first_release_date": "2014-06-24", "year": 2014,
         "track_count": 12},
        {"id": "rg2", "title": "New One", "album_type": "single", "first_release_date": "2016-01-02", "year": 2016,
         "track_count": 2},
    ]
    refresh_single_artist("ar", db, discovery_client=MagicMock(spec=DiscoveryClient), enricher=enricher)

    albums = {a["title"]: a for a in get_artist("ar", db=db, client=None, _admin={})["albums"]}
    assert albums["Monomyth"]["release_date"] == "2014-06-24"
    assert albums["Monomyth"]["track_count"] == 12
    assert albums["New One"]["release_date"] == "2016-01-02" and albums["New One"]["track_count"] == 2


def test_refresh_deezer_path_backfills_total_tracks(db: Database):
    db.upsert_library_artist(
        LibraryArtist(id="ar", name="Z", foreign_artist_id="deezer:artist:7", monitored=True, monitor_option="none")
    )
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Full", clean_title="full", foreign_album_id="deezer:album:1",
                     monitored=False)
    )
    discovery = MagicMock(spec=DiscoveryClient)
    discovery.get_artist_details.return_value = {
        "albums": [{"id": "deezer:album:1", "title": "Full", "release_date": "2020-05-01", "record_type": "album",
                    "track_count": 9}],
        "singles_eps": [],
        "compilations": [],
    }
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.lookup_artist_mbid.return_value = None
    refresh_single_artist("ar", db, discovery_client=discovery, enricher=enricher)
    alb = get_artist("ar", db=db, client=None, _admin={})["albums"][0]
    assert alb["total_tracks"] == 9 and alb["track_count"] == 9 and alb["release_date"] == "2020-05-01"


def test_hydration_sets_total_tracks_and_detail_falls_back_to_rows(db: Database):
    db.upsert_library_artist(LibraryArtist(id="ar", name="H", monitored=True, monitor_option="none"))
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Open Me", clean_title="open me", mb_release_group_id="rg9",
                     monitored=False)
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_tracks.return_value = [
        {"track_number": i, "disc_number": 1, "title": f"T{i}", "duration_seconds": 1.0} for i in range(1, 6)
    ]
    assert hydrate_album_tracks(db, enricher, "al") == 5
    alb = get_artist("ar", db=db, client=None, _admin={})["albums"][0]
    assert alb["total_tracks"] == 5 and alb["track_count"] == 5

    # No provider count stored: the detail payload falls back to stored rows.
    with db._lock:
        db.conn.execute("UPDATE library_albums SET total_tracks = NULL WHERE id = 'al'")
        db.conn.commit()
    assert get_artist("ar", db=db, client=None, _admin={})["albums"][0]["track_count"] == 5


def test_total_tracks_only_grows_unless_authoritative(db: Database):
    from plex_playlist_sync.api.routes.library import _store_total_tracks

    db.upsert_library_artist(LibraryArtist(id="ar", name="G", monitored=True, monitor_option="none"))
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Deluxe", clean_title="deluxe", total_tracks=20, monitored=False)
    )
    _store_total_tracks(db, "al", 12)  # a shorter standard edition must not clobber the deluxe count
    assert db.get_library_album("al")["total_tracks"] == 20
    _store_total_tracks(db, "al", "junk")
    _store_total_tracks(db, "al", 0)
    assert db.get_library_album("al")["total_tracks"] == 20
    _store_total_tracks(db, "al", 24)
    assert db.get_library_album("al")["total_tracks"] == 24
    _store_total_tracks(db, "al", 14, authoritative=True)  # a full MusicBrainz release may replace it
    assert db.get_library_album("al")["total_tracks"] == 14


def test_hydration_does_not_shrink_an_existing_total_tracks(db: Database):
    db.upsert_library_artist(LibraryArtist(id="ar", name="H", monitored=True, monitor_option="none"))
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Open Me", clean_title="open me", mb_release_group_id="rg9",
                     total_tracks=18, monitored=False)
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_tracks.return_value = [
        {"track_number": i, "disc_number": 1, "title": f"T{i}", "duration_seconds": 1.0} for i in range(1, 6)
    ]
    assert hydrate_album_tracks(db, enricher, "al") == 5
    assert db.get_library_album("al")["total_tracks"] == 18


@pytest.mark.parametrize("bad", ["many", "12 tracks", [], {}, float("inf")])
def test_deezer_artist_albums_survive_non_numeric_nb_tracks(bad):
    client = DiscoveryClient()
    client.session = MagicMock()

    def fake_get(url, **_kw):
        if url.endswith("/artist/7"):
            return _resp({"id": 7, "name": "A", "nb_album": 2})
        return _resp(
            {
                "data": [
                    {"id": 1, "title": "Bad", "release_date": "2020-05-01", "record_type": "album", "nb_tracks": bad},
                    {"id": 2, "title": "Str", "release_date": "2021-01-01", "record_type": "album", "nb_tracks": "9"},
                ]
            }
        )

    client.session.get.side_effect = fake_get
    d = client._get_deezer_artist_details("7")
    assert d is not None
    counts = {a["title"]: a["track_count"] for a in d["albums"]}
    assert counts == {"Bad": None, "Str": 9}
