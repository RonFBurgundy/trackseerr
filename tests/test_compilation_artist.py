"""Unit tests for compilation album-artist resolution and MusicBrainz identity lookup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.compilation_artist import (
    compilation_display_artist,
    series_from_title,
    soundtrack_name,
    work_out_compilation_artist,
)
from trackseerr.mb_metadata_store import MbMetadataStore
from trackseerr.models import LibraryAlbum, LibraryArtist
from trackseerr.storage import Database


def _resp(status_code: int, payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Now That's What I Call Music! 44", "Now That's What I Call Music!"),
        ("UKF Drum & Bass 2013", "UKF Drum & Bass"),
        ("Glitch & Funk, Volume 2", "Glitch & Funk"),
        ("we are friends, vol. 9", "we are friends"),
        ("Hospitality (Vol. 1)", "Hospitality"),
        ("FabricLive #3", "FabricLive"),
        ("Black Label XL", None),
        ("Michael", None),
        ("AB 1", None),
        ("", None),
    ],
)
def test_series_from_title(title: str, expected: str | None) -> None:
    assert series_from_title(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Rocky IV: Original Motion Picture Soundtrack", "Rocky IV"),
        ("The Music of Grand Theft Auto V, Volume 1: Original Music", "Grand Theft Auto V"),
        ("Saturday Night Fever: The Original Movie Sound Track", "Saturday Night Fever"),
        ("Drive (Original Soundtrack)", "Drive"),
        ("Akira OST", "Akira"),
        ("Pulp Fiction: Music from the Motion Picture", "Pulp Fiction"),
        ("Original Soundtrack", ""),
    ],
)
def test_soundtrack_name(title: str, expected: str) -> None:
    assert soundtrack_name(title) == expected


def test_work_out_compilation_artist_precedence_and_no_rg_id() -> None:
    enricher = MagicMock()

    # 1. MB series beats soundtrack, title series, and label (and strips trailing " series")
    enricher.get_release_group_identity.return_value = {
        "series": ["UKF Drum & Bass series"],
        "labels": ["UKF Music"],
    }
    assert (
        work_out_compilation_artist(
            enricher,
            {
                "title": "Rocky IV: Original Motion Picture Soundtrack 2013",
                "secondary_types": ["Soundtrack", "Compilation"],
                "mb_release_group_id": "rg-1",
            },
        )
        == "UKF Drum & Bass"
    )

    # 2. Soundtrack beats title series and label (handles JSON string secondary_types)
    enricher.get_release_group_identity.return_value = {
        "series": [],
        "labels": ["Capitol Records"],
    }
    assert (
        work_out_compilation_artist(
            enricher,
            {
                "title": "The Music of Grand Theft Auto V, Volume 1: Original Music",
                "secondary_types": '["Compilation", "Soundtrack"]',
                "mb_release_group_id": "rg-2",
            },
        )
        == "Grand Theft Auto V"
    )

    # 3. Title series beats label
    enricher.get_release_group_identity.return_value = {
        "series": [],
        "labels": ["OWSLA"],
    }
    assert (
        work_out_compilation_artist(
            enricher,
            {
                "title": "we are friends, vol. 9",
                "secondary_types": ["Compilation"],
                "mb_release_group_id": "rg-3",
            },
        )
        == "we are friends"
    )

    # 4. Label used when nothing else matches
    enricher.get_release_group_identity.return_value = {
        "series": [],
        "labels": ["Technique Recordings"],
    }
    assert (
        work_out_compilation_artist(
            enricher,
            {
                "title": "Black Label XL",
                "secondary_types": ["Compilation"],
                "mb_release_group_id": "rg-4",
            },
        )
        == "Technique Recordings"
    )

    # 5. "" when nothing matches and no mb_release_group_id skips enricher
    enricher.get_release_group_identity.reset_mock()
    assert (
        work_out_compilation_artist(
            enricher,
            {
                "title": "Black Label XL",
                "secondary_types": ["Compilation"],
                "mb_release_group_id": None,
            },
        )
        == ""
    )
    enricher.get_release_group_identity.assert_not_called()


def test_compilation_display_artist_guards_and_db_caching(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "comp.db"))
    try:
        db.upsert_library_artist(
            LibraryArtist(id="va-1", name="Various Artists", mbid=VARIOUS_ARTISTS_MBID, monitored=True)
        )
        db.upsert_library_artist(
            LibraryArtist(id="art-2", name="Daft Punk", mbid="056e4f3e-d505-4dad-8ec1-d04f521cbb56", monitored=True)
        )
        db.upsert_library_album(
            LibraryAlbum(
                id="alb-1",
                artist_id="va-1",
                title="UKF Drum & Bass 2013",
                mb_release_group_id="rg-ukf",
            )
        )
        db.upsert_library_album(
            LibraryAlbum(
                id="alb-empty",
                artist_id="va-1",
                title="Untitled Compilation",
                mb_release_group_id=None,
            )
        )

        va_artist = db.get_library_artist("va-1")
        other_artist = db.get_library_artist("art-2")
        assert va_artist is not None and other_artist is not None

        enricher = MagicMock()
        enricher.get_release_group_identity.return_value = {"series": [], "labels": []}

        # Setting "various" -> None, enricher not called
        alb = db.get_library_album("alb-1")
        assert alb is not None
        assert (
            compilation_display_artist(db, enricher, alb, va_artist, {"compilation_album_artist": "various"})
            is None
        )
        enricher.get_release_group_identity.assert_not_called()

        # Non-VA artist -> None even when setting is "series_label"
        assert (
            compilation_display_artist(
                db, enricher, alb, other_artist, {"compilation_album_artist": "series_label"}
            )
            is None
        )
        enricher.get_release_group_identity.assert_not_called()

        # Setting "series_label" on VA -> works out once, persists, second call skips enricher
        first = compilation_display_artist(
            db, enricher, alb, va_artist, {"compilation_album_artist": "series_label"}
        )
        assert first == "UKF Drum & Bass"
        assert enricher.get_release_group_identity.call_count == 1

        reloaded = db.get_library_album("alb-1")
        assert reloaded is not None
        assert reloaded["compilation_artist"] == "UKF Drum & Bass"
        second = compilation_display_artist(
            db, enricher, reloaded, va_artist, {"compilation_album_artist": "series_label"}
        )
        assert second == "UKF Drum & Bass"
        assert enricher.get_release_group_identity.call_count == 1

        # Empty result stores "" and returns None without recomputing on subsequent calls
        alb_empty = db.get_library_album("alb-empty")
        assert alb_empty is not None
        assert (
            compilation_display_artist(
                db, enricher, alb_empty, va_artist, {"compilation_album_artist": "series_label"}
            )
            is None
        )
        reloaded_empty = db.get_library_album("alb-empty")
        assert reloaded_empty is not None
        assert reloaded_empty["compilation_artist"] == ""
        assert (
            compilation_display_artist(
                db, enricher, reloaded_empty, va_artist, {"compilation_album_artist": "series_label"}
            )
            is None
        )
    finally:
        db.close()


def test_get_release_group_identity_parsing_filtering_and_caching(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "mb_cache.db"))
    try:
        store = MbMetadataStore(db)
        enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)

        series_payload = {
            "id": "rg-1",
            "relations": [
                {"target-type": "url", "url": {"resource": "https://example.com"}},
                {"target-type": "series", "series": {"id": "s1", "name": "UKF Drum & Bass"}},
                {"target-type": "series", "series": {"id": "s1", "name": "UKF Drum & Bass"}},
                {"target-type": "series", "series": {"id": "s2", "name": "UKF Compilations"}},
            ],
        }
        releases_payload = {
            "releases": [
                {
                    "id": "r1",
                    "label-info": [
                        {"label": {"name": "[no label]"}},
                        {"label": {"name": "Not On Label"}},
                        {"label": {"name": "No Label Records"}},
                        {"label": {"name": "OWSLA"}},
                        {"label": {"name": "Technique Recordings"}},
                    ],
                },
                {
                    "id": "r2",
                    "label-info": [
                        {"label": {"name": "Technique Recordings"}},
                        {"label": {"name": "Beta Label"}},
                    ],
                },
            ]
        }

        enricher._request = MagicMock(
            side_effect=[_resp(200, series_payload), _resp(200, releases_payload)]
        )

        identity = enricher.get_release_group_identity("rg-1")
        assert identity == {
            "series": ["UKF Drum & Bass", "UKF Compilations"],
            "labels": ["Technique Recordings", "Beta Label", "OWSLA"],
        }
        assert enricher._request.call_count == 2

        # L1 cache hit
        assert enricher.get_release_group_identity("rg-1") == identity
        assert enricher._request.call_count == 2

        # L2 cache hit in a fresh client sharing the persistent store
        enricher2 = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)
        enricher2._request = MagicMock()
        assert enricher2.get_release_group_identity("rg-1") == identity
        enricher2._request.assert_not_called()

        # Transient failure is not persisted to L2 store
        enricher2._request = MagicMock(side_effect=requests.ConnectionError("boom"))
        assert enricher2.get_release_group_identity("rg-transient") == {"series": [], "labels": []}
        assert store.get("rg_identity:v1:rg-transient") == (False, None)
    finally:
        db.close()
