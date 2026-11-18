"""Tests for suggesting and applying MusicBrainz matches for albums that are still unlinked."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.test_compilation_filing import BOB_MBID, NOW_44, _group, _seed_album
from tests.test_library_api import _auth_headers, app_and_client, seeded_users, test_config, test_db  # noqa: F401
from tests.test_recording_search import _resp
from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.api.dependencies import get_mbid_enricher
from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.match_suggestions import apply_match, suggest_matches
from trackseerr.storage import Database

BOB = "Bob Marley vs. Funkstar De Luxe"


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_match_suggestions.db"))
    clear_negative_cache()
    yield database
    database.close()


def _rt(title: str, num: int, rec: str | None, seconds: float | None = None) -> dict[str, Any]:
    return {
        "track_number": num,
        "disc_number": 1,
        "title": title,
        "duration_seconds": seconds,
        "mb_recording_id": rec,
        "artist_credit": [],
    }


HITS = _group("rg-hits", "Hits", date="2001-01-01")
HITS_TRACKS = [_rt("Could I Be In Love", 1, "rec-bob"), _rt("Jamming", 2, "rec-jam")]
NOW_TRACKS = [_rt("Could I Be In Love", 1, "rec-bob"), _rt("Other Song", 2, "rec-other"), _rt("Third", 3, "rec-third")]


def _enricher(
    groups_by_rec: dict[str, list[dict[str, Any]]] | None = None,
    rg_tracks: dict[str, list[dict[str, Any]]] | None = None,
    rg_search: dict[str, list[dict[str, Any]] | None] | None = None,
    rec_search: list[dict[str, Any]] | None = None,
    releases_by_rg: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.side_effect = lambda rec: (groups_by_rec or {}).get(rec, [])
    enricher.get_release_group_tracks.side_effect = lambda rg, force=False: (rg_tracks or {}).get(rg, [])
    enricher.search_release_groups.side_effect = lambda artist, title, limit=10: (rg_search or {}).get(artist, [])
    enricher.search_recordings.side_effect = lambda artist, title, limit=10: rec_search if rec_search is not None else []
    if releases_by_rg is not None:
        enricher.get_release_group_releases.side_effect = lambda rg, force=False: releases_by_rg.get(rg, [])
    else:
        enricher.get_release_group_releases.return_value = []
    return enricher


def _two_track_album(db: Database, tmp_path: Path, rec_ids: bool = True) -> None:
    _seed_album(
        db, tmp_path, "alb-un", "Hits",
        [
            {"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob" if rec_ids else None},
            {"num": 2, "title": "Jamming", "rec_id": "rec-jam" if rec_ids else None},
        ],
    )


# --- suggest ---------------------------------------------------------------------------------------------------


def test_suggest_ranks_full_track_match_first(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path)
    partial = _group("rg-partial", "Best Of", date="2005-01-01")
    enricher = _enricher(
        {"rec-bob": [partial, HITS], "rec-jam": [HITS]},
        {"rg-hits": HITS_TRACKS, "rg-partial": [_rt("Could I Be In Love", 1, "rec-bob")]},
    )

    result = suggest_matches(db, enricher, "alb-un")

    assert result["unavailable"] is False
    assert result["album"]["track_count"] == 2 and result["album"]["artist_name"] == BOB
    ids = [c["id"] for c in result["candidates"]]
    assert ids == ["rg-hits", "rg-partial"]
    top = result["candidates"][0]
    assert top["mapped"] == 2 and top["target_artist"] == BOB
    assert top["confidence"] > result["candidates"][1]["confidence"]
    assert "All 2 files found on this release" in top["reasons"]
    assert "Album title matches" in top["reasons"]


def test_suggest_includes_various_artists_compilation(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-un", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    enricher = _enricher(
        {"rec-bob": [NOW_44]}, {"rg-now44": NOW_TRACKS}, rg_search={"Various Artists": [NOW_44]}
    )

    result = suggest_matches(db, enricher, "alb-un")

    assert [c["id"] for c in result["candidates"]] == ["rg-now44"]
    cand = result["candidates"][0]
    assert cand["target_artist"] == "Various Artists" and cand["is_various"]
    assert cand["track_count"] == 3 and cand["mapped"] == 1
    assert "Released as a compilation by Various Artists" in cand["reasons"]


def test_suggest_title_match_ignores_edition_words(db: Database, tmp_path: Path):
    _seed_album(
        db,
        tmp_path,
        "alb-un",
        "Life After Death (2014 Remastered Edition)",
        [
            {"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"},
            {"num": 2, "title": "Jamming", "rec_id": "rec-jam"},
        ],
    )
    db._conn.execute("UPDATE library_tracks SET duration_seconds = 200 WHERE album_id = ?", ("alb-un",))
    db._conn.commit()
    plain = _group("rg-life", "Life After Death", date="1997-03-25")
    release_tracks = [_rt("Could I Be In Love", 1, "rec-bob", seconds=200), _rt("Jamming", 2, "rec-jam", seconds=200)]
    enricher = _enricher({"rec-bob": [plain], "rec-jam": [plain]}, {"rg-life": release_tracks})

    result = suggest_matches(db, enricher, "alb-un")

    top = result["candidates"][0]
    assert top["id"] == "rg-life"
    assert top["confidence"] >= 90
    assert "Album title matches" in top["reasons"]


def test_suggest_maps_bonus_tracks_from_other_release(db: Database, tmp_path: Path):
    _seed_album(
        db,
        tmp_path,
        "alb-un",
        "Hits (Deluxe)",
        [
            {"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"},
            {"num": 2, "title": "Jamming", "rec_id": "rec-jam"},
            {"num": 3, "title": "Bonus Song", "rec_id": "rec-bonus"},
        ],
    )
    db._conn.execute("UPDATE library_tracks SET duration_seconds = 200 WHERE album_id = ?", ("alb-un",))
    db._conn.commit()
    releases = [
        {
            "id": "rel-std",
            "title": "Hits",
            "track_count": 2,
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {"position": 1, "number": "1", "title": "Could I Be In Love", "recording_id": "rec-bob", "length": 200_000},
                        {"position": 2, "number": "2", "title": "Jamming", "recording_id": "rec-jam", "length": 200_000},
                    ],
                }
            ],
        },
        {
            "id": "rel-deluxe",
            "title": "Hits (Deluxe)",
            "track_count": 3,
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {"position": 1, "number": "1", "title": "Could I Be In Love", "recording_id": "rec-bob", "length": 200_000},
                        {"position": 2, "number": "2", "title": "Jamming", "recording_id": "rec-jam", "length": 200_000},
                        {"position": 3, "number": "3", "title": "Bonus Song", "recording_id": "rec-bonus", "length": 200_000},
                    ],
                }
            ],
        },
    ]
    enricher = _enricher(
        {"rec-bob": [HITS], "rec-jam": [HITS]},
        {"rg-hits": HITS_TRACKS},
        releases_by_rg={"rg-hits": releases},
    )

    result = suggest_matches(db, enricher, "alb-un")

    top = result["candidates"][0]
    assert top["id"] == "rg-hits"
    assert top["mapped"] == 3
    assert top["track_count"] == 3
    assert top["release_id"] == "rel-deluxe"
    assert top["confidence"] >= 90


def test_suggest_skips_release_lookup_when_all_mapped(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path)
    enricher = _enricher({"rec-bob": [HITS], "rec-jam": [HITS]}, {"rg-hits": HITS_TRACKS})

    result = suggest_matches(db, enricher, "alb-un")

    assert result["candidates"][0]["mapped"] == 2
    assert result["candidates"][0].get("release_id") is None
    enricher.get_release_group_releases.assert_not_called()


def test_suggest_own_release_beats_various_compilation(db: Database, tmp_path: Path):
    _seed_album(
        db,
        tmp_path,
        "alb-un",
        "Pet Sounds (UDSACD)",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
    )
    va_comp = _group("rg-va", "Alle 40 goed: Evergreens", secondary=["Compilation"], date="1995-01-01", various=True)
    own_album = _group("rg-own", "Pet Sounds", date="1999-01-01")
    one_track = [_rt("Could I Be In Love", 1, "rec-bob")]
    enricher = _enricher(
        {"rec-bob": [va_comp, own_album]},
        {"rg-va": one_track, "rg-own": one_track},
    )

    result = suggest_matches(db, enricher, "alb-un")

    assert [c["id"] for c in result["candidates"]] == ["rg-own", "rg-va"]
    va_cand = result["candidates"][1]
    assert "Found only on a Various Artists compilation" in va_cand["reasons"]
    # Own album also has +30 title match, VA loses both title match (30) and compilation penalty (20)
    assert result["candidates"][0]["confidence"] - va_cand["confidence"] >= 20


def test_suggest_tie_prefers_album_over_artist_compilation(db: Database, tmp_path: Path):
    _seed_album(
        db,
        tmp_path,
        "alb-un",
        "Something Else",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
    )
    artist_comp = _group("rg-comp", "Greatest Hits", secondary=["Compilation"], date="1995-01-01", various=False)
    plain_album = _group("rg-album", "Original LP", date="2005-01-01", various=False)
    one_track = [_rt("Could I Be In Love", 1, "rec-bob")]
    enricher = _enricher(
        {"rec-bob": [artist_comp, plain_album]},
        {"rg-comp": one_track, "rg-album": one_track},
    )

    result = suggest_matches(db, enricher, "alb-un")

    assert [c["id"] for c in result["candidates"]] == ["rg-album", "rg-comp"]
    assert result["candidates"][0]["confidence"] == result["candidates"][1]["confidence"]


def test_suggest_maps_by_recording_then_title(db: Database, tmp_path: Path):
    _seed_album(
        db, tmp_path, "alb-un", "Hits",
        [
            {"num": 1, "title": "Jamming (live)", "rec_id": "rec-jam"},
            {"num": 2, "title": "Could I Be In Love", "rec_id": None},
        ],
    )
    # the release has the recording under a different title, and a title-only match for the untagged track
    tracks = [_rt("Could I Be In Love", 1, "rec-new"), _rt("Jamming", 2, "rec-jam")]
    enricher = _enricher({"rec-jam": [HITS]}, {"rg-hits": tracks})

    cand = suggest_matches(db, enricher, "alb-un")["candidates"][0]

    by_track = {r["track_id"]: r for r in cand["track_map"]}
    assert by_track["alb-un-1"]["recording_id"] == "rec-jam" and by_track["alb-un-1"]["release_track_number"] == 2
    assert by_track["alb-un-2"]["recording_id"] == "rec-new" and by_track["alb-un-2"]["release_track_title"] == (
        "Could I Be In Love"
    )
    assert cand["mapped"] == 2


def test_suggest_uses_recording_search_for_untagged_tracks(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path, rec_ids=False)
    hit = {
        "id": "rec-bob", "title": "Could I Be In Love", "score": 75, "length_ms": None,
        "artist_credit": BOB, "artist_names": [BOB], "artist_ids": [BOB_MBID],
        "release_groups": [HITS],
    }
    enricher = _enricher(rg_tracks={"rg-hits": HITS_TRACKS}, rec_search=[hit])

    result = suggest_matches(db, enricher, "alb-un")

    assert [c["id"] for c in result["candidates"]] == ["rg-hits"]
    assert result["candidates"][0]["mapped"] == 2
    enricher.search_recordings.assert_called()


def test_suggest_unavailable_when_musicbrainz_down(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path)
    enricher = _enricher({"rec-bob": [HITS]}, rg_search={BOB: None})

    result = suggest_matches(db, enricher, "alb-un")

    assert result["candidates"] == [] and result["unavailable"] is True
    assert db.get_library_album("alb-un")["mb_release_group_id"] is None


def test_search_release_groups_parses_and_caches():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    body = {
        "release-groups": [
            {
                "id": "rg-1", "title": "Kaya", "score": 100, "primary-type": "Album", "first-release-date": "1978-03-23",
                "artist-credit": [{"name": "Bob Marley", "artist": {"id": "a1", "name": "Bob Marley"}}],
            },
            {
                "id": "rg-2", "title": "Reggae Hits", "score": 80, "primary-type": "Album",
                "secondary-types": ["Compilation"],
                "artist-credit": [{"name": "Various Artists", "artist": {"id": VARIOUS_ARTISTS_MBID}}],
            },
            {"title": "No id"},
        ]
    }
    enricher._request = MagicMock(return_value=_resp(200, body))

    first = enricher.search_release_groups("Bob Marley", 'Ka"ya')
    second = enricher.search_release_groups("Bob Marley", 'Ka"ya')

    assert enricher._request.call_count == 1 and first == second and first is not None
    assert [g["id"] for g in first] == ["rg-1", "rg-2"]
    assert first[0]["artist_credit"] == "Bob Marley" and first[0]["is_various"] is False
    assert first[0]["first_release_date"] == "1978-03-23"
    assert first[1]["is_various"] is True and first[1]["secondary_types"] == ["Compilation"]
    params = enricher._request.call_args[1]["params"]
    assert params["query"] == 'releasegroup:"Ka ya" AND artist:"Bob Marley"'

    enricher._request = MagicMock(return_value=_resp(503, {}))
    assert enricher.search_release_groups(None, "Other Title") is None
    assert enricher._request.call_args[1]["params"]["query"] == 'releasegroup:"Other Title"'


# --- apply -----------------------------------------------------------------------------------------------------


def test_apply_links_in_place_and_matches_release(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path, rec_ids=False)
    enricher = _enricher(rg_tracks={"rg-hits": HITS_TRACKS})
    enricher.get_release_group_releases.return_value = [
        {"id": "rel-1", "title": "Hits", "track_count": 2, "tracks": [
            {"recording_id": "rec-bob", "disc_number": 1, "track_number": 1, "title": "Could I Be In Love"},
            {"recording_id": "rec-jam", "disc_number": 1, "track_number": 2, "title": "Jamming"},
        ]},
    ]

    result = apply_match(db, enricher, "alb-un", HITS, {"alb-un-1": "rec-bob", "alb-un-2": "rec-jam"})

    assert result == {"album_id": "alb-un", "merged": False, "conflict": False}
    album = db.get_library_album("alb-un")
    assert album["mb_release_group_id"] == "rg-hits"
    assert db.get_library_track("alb-un-1")["mb_recording_id"] == "rec-bob"
    assert db.get_library_track("alb-un-2")["mb_recording_id"] == "rec-jam"
    enricher.get_release_group_releases.assert_called_with("rg-hits")


def test_apply_sets_release_id_from_candidate(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path, rec_ids=False)
    enricher = _enricher(rg_tracks={"rg-hits": HITS_TRACKS})
    candidate = {**HITS, "release_id": "rel-deluxe"}

    result = apply_match(db, enricher, "alb-un", candidate, {"alb-un-1": "rec-bob", "alb-un-2": "rec-jam"})

    assert result == {"album_id": "alb-un", "merged": False, "conflict": False}
    album = db.get_library_album("alb-un")
    assert album is not None
    assert album["mb_release_group_id"] == "rg-hits"
    assert album["mb_release_id"] == "rel-deluxe"


def test_apply_compilation_moves_to_various_artists(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-un", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": None}])
    enricher = _enricher(rg_tracks={"rg-now44": NOW_TRACKS})

    result = apply_match(db, enricher, "alb-un", NOW_44, {"alb-un-1": "rec-bob"})

    assert result["merged"] is True and result["conflict"] is False
    assert db.get_library_album("alb-un") is None
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    assert va is not None
    assert result["album_id"] == db.album_holding_release_group(str(va["id"]), "rg-now44", "")
    tracks = {t["mb_recording_id"]: t for t in db.list_library_tracks(album_id=result["album_id"], limit=50)}
    assert tracks["rec-bob"]["has_file"]


def test_apply_merges_into_existing_holder(db: Database, tmp_path: Path):
    _two_track_album(db, tmp_path, rec_ids=False)
    _seed_album(db, tmp_path, "alb-home", "Hits",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob", "file": False},
                 {"num": 2, "title": "Jamming", "rec_id": "rec-jam", "file": False}], rg_id="rg-hits")
    enricher = _enricher(rg_tracks={"rg-hits": HITS_TRACKS})

    result = apply_match(db, enricher, "alb-un", HITS, {"alb-un-1": "rec-bob", "alb-un-2": "rec-jam"})

    assert result == {"album_id": "alb-home", "merged": True, "conflict": False}
    assert db.get_library_album("alb-un") is None
    assert all(t["has_file"] for t in db.list_library_tracks(album_id="alb-home", limit=50))


def test_apply_conflict_reported(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-un", "Hits", [{"num": 1, "title": "Could I Be In Love", "rec_id": None}])
    _seed_album(db, tmp_path, "alb-home", "Hits", [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
                rg_id="rg-hits")
    enricher = _enricher(rg_tracks={"rg-hits": HITS_TRACKS})

    result = apply_match(db, enricher, "alb-un", HITS, {"alb-un-1": "rec-bob"})

    assert result == {"album_id": "alb-un", "merged": False, "conflict": True}
    assert db.get_library_album("alb-un") is not None
    assert any(c["source_album_id"] == "alb-un" and c["holder_album_id"] == "alb-home"
               for c in db.list_merge_conflicts())


# --- routes ----------------------------------------------------------------------------------------------------


def _route_setup(app, test_db, tmp_path: Path) -> MagicMock:
    _seed_album(test_db, tmp_path, "alb-un", "Hits", [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    enricher = _enricher({"rec-bob": [HITS]}, {"rg-hits": HITS_TRACKS})
    app.dependency_overrides[get_mbid_enricher] = lambda: enricher
    return enricher


def test_suggestion_routes_admin_only(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):  # noqa: F811
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice_h = _auth_headers(seeded_users["alice"], test_db, test_config)
    _route_setup(app, test_db, tmp_path)
    url = "/api/library/musicbrainz/unlinked/alb-un/suggestions"

    assert client.get(url, headers=alice_h).status_code == 403
    res = client.get(url, headers=admin_h)
    assert res.status_code == 200
    body = res.json()
    assert body["candidates"][0]["id"] == "rg-hits" and body["unavailable"] is False
    assert body["candidates"][0]["track_map"][0]["recording_id"] == "rec-bob"
    payload = {"release_group": {"id": "rg-hits", "title": "Hits"}, "track_recordings": {}}
    assert client.post(url.replace("suggestions", "apply"), json=payload, headers=alice_h).status_code == 403
    assert client.get("/api/library/musicbrainz/unlinked/nope/suggestions", headers=admin_h).status_code == 404


def test_apply_unknown_album_404(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):  # noqa: F811
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _route_setup(app, test_db, tmp_path)
    payload = {"release_group": {"id": "rg-hits", "title": "Hits"}, "track_recordings": {}}

    res = client.post("/api/library/musicbrainz/unlinked/missing/apply", json=payload, headers=admin_h)

    assert res.status_code == 404


def test_apply_already_linked_409(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):  # noqa: F811
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _route_setup(app, test_db, tmp_path)
    test_db.set_album_mb_release_group_id("alb-un", "rg-existing")
    payload = {"release_group": {"id": "rg-hits", "title": "Hits"}, "track_recordings": {}}

    res = client.post("/api/library/musicbrainz/unlinked/alb-un/apply", json=payload, headers=admin_h)

    assert res.status_code == 409


def test_apply_route_links_album(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):  # noqa: F811
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _route_setup(app, test_db, tmp_path)
    payload = {
        "release_group": {"id": "rg-hits", "title": "Hits", "primary_type": "Album", "is_various": False},
        "track_recordings": {"alb-un-1": "rec-bob"},
    }

    res = client.post("/api/library/musicbrainz/unlinked/alb-un/apply", json=payload, headers=admin_h)

    assert res.status_code == 200
    assert res.json() == {"album_id": "alb-un", "merged": False, "conflict": False}
    assert test_db.get_library_album("alb-un")["mb_release_group_id"] == "rg-hits"
