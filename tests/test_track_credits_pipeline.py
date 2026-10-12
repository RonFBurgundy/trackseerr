"""MusicBrainz artist-credit fetch and persistence into library_track_artists."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from trackseerr.artist_refresh import refresh_single_artist
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.mediacover import mediacover_service
from trackseerr.models import LibraryArtist
from trackseerr.storage import Database

from tests.test_library_api import test_db  # noqa: F401

_CREDIT = [{"name": "Bee Gees", "joinphrase": "", "artist": {"id": "mb-bg", "name": "Bee Gees"}}]


def test_get_release_group_tracks_carries_artist_credit() -> None:
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    payload = {
        "releases": [
            {
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {"position": 1, "title": "Stayin' Alive", "artist-credit": _CREDIT, "recording": {"id": "r1"}},
                            {
                                "position": 2,
                                "title": "Rec Credit",
                                "recording": {"id": "r2", "artist-credit": _CREDIT},
                            },
                            {"position": 3, "title": "None", "recording": {"id": "r3"}},
                        ],
                    }
                ]
            }
        ]
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    with patch.object(enricher, "_do_http_call", return_value=resp) as http, patch.object(
        enricher, "_set_cached"
    ), patch.object(enricher, "_remember") as remember:
        tracks = enricher.get_release_group_tracks("RG-ABC")
    assert tracks[0]["artist_credit"] == _CREDIT
    assert tracks[1]["artist_credit"] == _CREDIT
    assert tracks[2]["artist_credit"] == []
    assert "artist-credits" in str(http.call_args)
    assert remember.call_args.args[0] == "tracks:rg:v2:rg-abc"


def test_refresh_writes_credits_for_compilation_track(test_db: Database) -> None:  # noqa: F811
    test_db.upsert_library_artist(
        LibraryArtist(id="va", name="Various Artists", mbid="mbid-va", monitored=True, monitor_option="all")
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mbid-va"}
    disco = [{"id": "rg-snf", "title": "Saturday Night Fever", "album_type": "album", "year": 1977, "cover_url": None}]
    enricher.get_artist_discography.return_value = disco
    enricher.get_artist_discography_result.return_value = (disco, True)
    enricher.source_available.return_value = True
    enricher.stats.return_value = {"network_requests": 0, "cache_hits": 0}
    enricher.get_release_group_tracks.return_value = [
        {
            "track_number": 1,
            "disc_number": 1,
            "title": "Stayin' Alive",
            "duration_seconds": 245.0,
            "mb_recording_id": "rec-sa",
            "artist_credit": _CREDIT,
        },
        {"track_number": 2, "disc_number": 1, "title": "No Credit", "duration_seconds": 100.0, "mb_recording_id": "rec-nc"},
    ]
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/cover.jpg")):
        assert refresh_single_artist(artist_id="va", db=test_db, enricher=enricher)["success"] is True
    album = test_db.list_library_albums(artist_id="va")[0]
    credits = test_db.list_track_artists_for_album(album["id"])
    by_title = {t["title"]: t["id"] for t in test_db.list_library_tracks(album_id=album["id"])}
    assert [(c["artist_name"], c["role"]) for c in credits[by_title["Stayin' Alive"]]] == [("Bee Gees", "primary")]
    assert by_title["No Credit"] not in credits


def test_hydrate_album_tracks_writes_credits(test_db: Database) -> None:  # noqa: F811
    from trackseerr.album_track_hydration import clear_negative_cache, hydrate_album_tracks

    clear_negative_cache()
    test_db.upsert_library_artist({"id": "va2", "name": "Various Artists", "mbid": "mb-va2"})
    test_db.upsert_library_album(
        {"id": "al2", "artist_id": "va2", "title": "Comp", "mb_release_group_id": "rg-comp"}
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_tracks.return_value = [
        {
            "track_number": 1,
            "disc_number": 1,
            "title": "Closer (Adriatique Remix)",
            "duration_seconds": 200.0,
            "mb_recording_id": "r-c",
            "artist_credit": _CREDIT,
        }
    ]
    assert hydrate_album_tracks(test_db, enricher, "al2") == 1
    credits = next(iter(test_db.list_track_artists_for_album("al2").values()))
    assert [(c["artist_name"], c["role"]) for c in credits] == [("Bee Gees", "primary"), ("Adriatique", "remixer")]
