"""Unified artist profile: identity linking, album matching, endpoint, annotate hints, migration."""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from plex_playlist_sync import artist_links
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_discovery_client, get_lidarr_client
from plex_playlist_sync.api.routes.discovery import annotate_item_statuses
from plex_playlist_sync.artist_links import (
    LidarrLibraryIndex,
    extract_mbid,
    normalize_artist_name,
    resolve_from_discovery,
    resolve_from_library,
)
from plex_playlist_sync.artist_profile import match_discography, normalize_album_title, ownership_status
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import SCHEMA_VERSION, Database

MBID = "11111111-2222-3333-4444-555555555555"
OTHER_MBID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _artist(db: Database, art_id: str, name: str, mbid: Optional[str] = None, foreign: Optional[str] = None) -> None:
    db.upsert_library_artist({"id": art_id, "name": name, "mbid": mbid, "foreign_artist_id": foreign})


def _album(db: Database, alb_id: str, art_id: str, title: str, year: Optional[int] = None, total: int = 0) -> None:
    db.upsert_library_album(
        {"id": alb_id, "artist_id": art_id, "title": title, "year": year, "total_tracks": total,
         "release_date": f"{year}-01-01" if year else None}
    )


def _track(db: Database, track_id: str, alb_id: str, art_id: str, number: int, with_file: bool) -> None:
    db.upsert_library_track({"id": track_id, "album_id": alb_id, "artist_id": art_id, "title": f"T{track_id}",
                             "track_number": number})
    if with_file:
        db.upsert_library_file({"id": f"f-{track_id}", "track_id": track_id, "file_path": f"/m/{track_id}.flac",
                                "relative_path": f"{track_id}.flac", "codec": "flac"})


def _discovery(details: Optional[dict[str, Any]] = None, search: Any = None, top: Any = None) -> MagicMock:
    disc = MagicMock(spec=DiscoveryClient)
    disc.get_artist_details.return_value = details
    disc.search_artists.return_value = search if search is not None else []
    disc.get_artist_top_tracks_detailed.return_value = top or []
    return disc


def _enricher(mbid: Optional[str]) -> MagicMock:
    enr = MagicMock(spec=MbidEnricherClient)
    enr.lookup_artist_mbid_by_url.return_value = mbid
    return enr


def _details(name: str = "Radiohead", **groups: list) -> dict[str, Any]:
    return {"id": "deezer:artist:399", "name": name, "image_url": "http://img/a.jpg",
            "albums": groups.get("albums", []), "singles_eps": groups.get("singles_eps", []),
            "compilations": groups.get("compilations", [])}


def _dalb(alb_id: str, title: str, date: str = "2000-10-02", tracks: Optional[int] = None) -> dict[str, Any]:
    return {"id": f"deezer:album:{alb_id}", "item_type": "album", "title": title, "artist": "Radiohead",
            "release_date": date, "track_count": tracks, "cover_url": None}


# ----------------------------------------------------------------------------------------------- normalisation


def test_artist_name_normalisation_matches_library_clean_name():
    assert normalize_artist_name("  AC/DC ") == "acdc"
    assert normalize_artist_name("Beyoncé") == "beyoncé"
    assert normalize_artist_name("Daft_Punk") == "daft punk"
    assert normalize_artist_name(None) == ""


def test_extract_mbid_handles_prefixed_and_bare():
    assert extract_mbid(f"musicbrainz:artist:{MBID.upper()}") == MBID
    assert extract_mbid(None, MBID) == MBID
    assert extract_mbid("deezer:1", "") is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("OK Computer (Deluxe Edition)", "ok computer"),
        ("OK Computer [Remastered 2011]", "ok computer"),
        ("Kid A - Single", "kid a"),
        ("Pablo Honey - EP", "pablo honey"),
        ("Thriller (Deluxe) [Remastered]", "thriller"),
        ("Rock & Roll!", "rock and roll"),
        ("Songs (Of Innocence)", "songs of innocence"),  # non-edition brackets are kept (punctuation folded only)
        ("Hail to the Thief - Special Edition", "hail to the thief"),
    ],
)
def test_album_title_normalisation(title, expected):
    assert normalize_album_title(title) == expected


# ----------------------------------------------------------------------------------------------- linking


def test_mbid_match_beats_name_match(db):
    _artist(db, "a-name", "Radiohead")  # same name, no MBID
    _artist(db, "a-mbid", "Radiohead UK", foreign=f"musicbrainz:artist:{MBID}")
    link = resolve_from_discovery(db, _discovery(_details()), "deezer:artist:399", enricher=_enricher(MBID))
    assert (link.library_artist_id, link.confidence, link.mbid) == ("a-mbid", "mbid", MBID)


def test_name_fallback_when_no_mbid(db):
    _artist(db, "a1", "Radio-Head")
    link = resolve_from_discovery(db, _discovery(_details("Radiohead")), "deezer:artist:399", enricher=_enricher(None))
    assert (link.library_artist_id, link.confidence) == ("a1", "name")


def test_name_match_rejected_when_mbids_conflict(db):
    _artist(db, "a1", "Radiohead", mbid=OTHER_MBID)
    link = resolve_from_discovery(db, _discovery(_details()), "deezer:artist:399", enricher=_enricher(MBID))
    assert link.library_artist_id is None and link.confidence == "none" and link.mbid == MBID


def test_no_match(db):
    link = resolve_from_discovery(db, _discovery(_details()), "deezer:artist:399", enricher=_enricher(None))
    assert link.library_artist_id is None and link.confidence == "none" and link.name == "Radiohead"


def test_itunes_id_skips_musicbrainz_and_uses_name(db):
    _artist(db, "a1", "Radiohead")
    enr = _enricher(MBID)
    link = resolve_from_discovery(db, _discovery(_details()), "itunes:artist:5", enricher=enr)
    assert link.confidence == "name"
    enr.lookup_artist_mbid_by_url.assert_not_called()


def test_musicbrainz_called_with_deezer_url(db):
    enr = _enricher(None)
    resolve_from_discovery(db, _discovery(_details()), "deezer:artist:399", enricher=enr)
    enr.lookup_artist_mbid_by_url.assert_called_once_with("https://www.deezer.com/artist/399")


def test_positive_cache_is_reused_while_fresh_then_refreshed(db):
    _artist(db, "a1", "Radiohead")
    disc = _discovery(_details())
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert disc.get_artist_details.call_count == 1

    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute("UPDATE artist_links SET updated_at = ?", (old,))
    db.conn.commit()
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert disc.get_artist_details.call_count == 2


def test_negative_cache_expires_after_one_day_but_positive_survives_six(db):
    disc = _discovery(_details())
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))  # negative
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert disc.get_artist_details.call_count == 1

    two_days = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute("UPDATE artist_links SET updated_at = ?", (two_days,))
    db.conn.commit()
    _artist(db, "a1", "Radiohead")  # now exists: the stale negative must be re-resolved
    link = resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert link.library_artist_id == "a1"

    six_days = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute("UPDATE artist_links SET updated_at = ?", (six_days,))
    db.conn.commit()
    resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert disc.get_artist_details.call_count == 2  # the 6-day-old positive is still fresh: no third fetch


def test_discovery_failure_is_not_cached(db):
    disc = _discovery(None)
    link = resolve_from_discovery(db, disc, "deezer:artist:399", enricher=_enricher(None))
    assert link.confidence == "none" and link.name == ""
    assert db.get_artist_link(discovery_id="deezer:artist:399") is None


def test_library_to_discovery_exact_name_most_fans_wins(db):
    _artist(db, "a1", "Radiohead")
    search = [
        {"id": "deezer:artist:1", "name": "Radiohead Tribute", "nb_fan": 9_000_000},
        {"id": "deezer:artist:2", "name": "Radiohead", "nb_fan": 10},
        {"id": "deezer:artist:399", "name": "RADIOHEAD", "nb_fan": 500},
    ]
    link = resolve_from_library(db, _discovery(search=search), "a1", enricher=_enricher(None))
    assert (link.discovery_id, link.confidence) == ("deezer:artist:399", "name")
    assert db.get_discovery_ids_for_library_artists(["a1"]) == {"a1": "deezer:artist:399"}


def test_library_to_discovery_upgrades_to_mbid_when_musicbrainz_confirms(db):
    _artist(db, "a1", "Radiohead", foreign=f"musicbrainz:artist:{MBID}")
    search = [{"id": "deezer:artist:399", "name": "Radiohead", "nb_fan": 5}]
    link = resolve_from_library(db, _discovery(search=search), "a1", enricher=_enricher(MBID))
    assert (link.confidence, link.mbid) == ("mbid", MBID)


def test_library_to_discovery_no_match_is_negative_cached(db):
    _artist(db, "a1", "Radiohead")
    disc = _discovery(search=[{"id": "deezer:artist:1", "name": "Other", "nb_fan": 1}])
    assert resolve_from_library(db, disc, "a1", enricher=_enricher(None)).confidence == "none"
    resolve_from_library(db, disc, "a1", enricher=_enricher(None))
    assert disc.search_artists.call_count == 1
    assert db.get_discovery_ids_for_library_artists(["a1"]) == {}  # negatives never surface as discovery ids


def test_library_to_discovery_search_failure_not_cached(db):
    _artist(db, "a1", "Radiohead")
    disc = _discovery()
    disc.search_artists.return_value = None
    link = resolve_from_library(db, disc, "a1", enricher=_enricher(None))
    assert link.discovery_id is None
    assert db.get_artist_link(library_artist_id="a1") is None


def test_lidarr_index_matches_records():
    idx = LidarrLibraryIndex([{"id": "7", "name": "Radiohead", "clean_name": "radiohead", "mbid": MBID}])
    assert idx.find_by_mbid(MBID)["id"] == "7"
    assert idx.find_by_clean_name("radiohead")["id"] == "7"
    assert idx.get("7")["name"] == "Radiohead" and idx.get("8") is None


@pytest.mark.real_mbid_enricher
def test_musicbrainz_url_lookup_parses_relations_and_handles_failure():
    client = MbidEnricherClient(base_url="https://musicbrainz.org", min_interval=0)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"relations": [{"type": "free streaming", "artist": {"id": MBID, "name": "Radiohead"}}]}
    client._session = MagicMock()
    client._session.get.return_value = resp
    url = "https://www.deezer.com/artist/399"
    assert MbidEnricherClient.lookup_artist_mbid_by_url(client, url) == MBID
    assert client._session.get.call_args.kwargs["params"]["resource"] == url

    client._session.get.return_value = MagicMock(status_code=404)
    assert MbidEnricherClient.lookup_artist_mbid_by_url(client, "https://www.deezer.com/artist/1") is None
    client._session.get.side_effect = requests.ConnectionError("down")
    assert MbidEnricherClient.lookup_artist_mbid_by_url(client, "https://www.deezer.com/artist/2") is None


# ----------------------------------------------------------------------------------------------- album matching


def test_ownership_status():
    assert ownership_status(0, 10) == "missing"
    assert ownership_status(3, 10) == "partial"
    assert ownership_status(10, 10) == "in_library"
    assert ownership_status(4, 0) == "in_library"


def _lib(alb_id: str, title: str, year: Optional[int], total: int, have: int) -> dict[str, Any]:
    return {"id": alb_id, "title": title, "year": year, "release_date": f"{year}-01-01" if year else None,
            "cover_url": None, "track_count": total, "track_file_count": have, "monitored": True}


def test_match_deluxe_and_remaster_titles():
    groups = {"albums": [_dalb("1", "OK Computer (Deluxe Edition)"), _dalb("2", "Kid A [Remastered 2011]", "2000-10-02")]}
    out, only = match_discography(groups, [_lib("L1", "OK Computer", 1997, 12, 12), _lib("L2", "Kid A", 2000, 10, 4)])
    assert [(i["library_album_id"], i["status"]) for i in out["albums"]] == [("L1", "in_library"), ("L2", "partial")]
    assert out["albums"][1]["have_tracks"] == 4 and out["albums"][1]["total_tracks"] == 10
    assert only == []


def test_match_prefers_same_year_among_duplicates():
    groups = {"albums": [_dalb("1", "Greatest Hits", "2005-01-01")]}
    lib = [_lib("old", "Greatest Hits", 1995, 10, 10), _lib("new", "Greatest Hits", 2005, 10, 2)]
    out, only = match_discography(groups, lib)
    assert out["albums"][0]["library_album_id"] == "new" and out["albums"][0]["status"] == "partial"
    assert [o["library_album_id"] for o in only] == ["old"]


def test_library_only_group_and_unmatched_items_untouched():
    groups = {"albums": [_dalb("1", "Kid A")], "singles_eps": [_dalb("2", "Creep - Single")]}
    out, only = match_discography(groups, [_lib("L9", "Bootleg Sessions", 2003, 5, 5)])
    assert "status" not in out["albums"][0] and "library_album_id" not in out["singles_eps"][0]
    assert only[0]["title"] == "Bootleg Sessions" and only[0]["status"] == "in_library"
    assert only[0]["library_album_id"] == "L9" and only[0]["id"] == "library:album:L9"


# ----------------------------------------------------------------------------------------------- endpoint


def _headers(db: Database, config: Config, user_id: str, is_admin: bool) -> dict[str, str]:
    user = db.upsert_user(user_id, user_id, f"{user_id}@x.tv", is_admin=is_admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username=user_id, is_admin=is_admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(db, config):
    """(app, client, admin headers); ``user_headers`` is a fixture for the non-admin case."""
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_lidarr_client] = lambda: None
    return app, TestClient(app), _headers(db, config, "root", True)


@pytest.fixture
def user_headers(db, config, api):
    return _headers(db, config, "alice", False)


def _seed_radiohead(db: Database) -> None:
    _artist(db, "a1", "Radiohead", foreign=f"musicbrainz:artist:{MBID}")
    _album(db, "L1", "a1", "OK Computer", 1997, total=4)
    for n in range(1, 5):
        _track(db, f"ok{n}", "L1", "a1", n, with_file=n <= 2)
    _album(db, "L2", "a1", "Bootleg", 2001, total=2)
    for n in range(1, 3):
        _track(db, f"bt{n}", "L2", "a1", n, with_file=True)


def test_requires_exactly_one_identifier(api):
    app, client, headers = api
    app.dependency_overrides[get_discovery_client] = lambda: _discovery()
    assert client.get("/api/discovery/artist-profile", headers=headers).status_code == 400
    both = "/api/discovery/artist-profile?discovery_id=deezer:artist:1&library_artist_id=a1"
    assert client.get(both, headers=headers).status_code == 400


def test_requires_login(api):
    _app, client, _headers = api
    assert client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:1").status_code in (401, 403)


def test_unresolvable_ids_404(api):
    app, client, headers = api
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(None)
    assert client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:1", headers=headers).status_code == 404
    assert client.get("/api/discovery/artist-profile?library_artist_id=nope", headers=headers).status_code == 404


def test_profile_from_discovery_merges_library(api, db, monkeypatch):
    app, client, headers = api
    _seed_radiohead(db)
    monkeypatch.setattr(artist_links, "get_enricher", lambda: _enricher(MBID))
    details = _details(albums=[_dalb("1", "OK Computer (Deluxe Edition)", "1997-05-21"), _dalb("2", "Kid A")])
    top = [{"id": "deezer:track:5", "title": "Creep", "artist": "Radiohead", "album": "Pablo Honey",
            "duration": 238, "preview_url": "http://p/5.mp3"}]
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(details, top=top)

    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=headers).json()

    assert body["artist"] == {"name": "Radiohead", "image_url": "http://img/a.jpg", "discovery_id": "deezer:artist:399",
                              "library_artist_id": "a1", "mbid": MBID, "link_confidence": "mbid"}
    assert body["library"]["artist_id"] == "a1" and body["library"]["album_count"] == 2
    assert body["library"]["track_file_count"] == 4 and body["library"]["monitored"] is True
    ok, kid = body["discography"]["albums"]
    assert (ok["status"], ok["have_tracks"], ok["total_tracks"], ok["library_album_id"]) == ("partial", 2, 4, "L1")
    assert kid["status"] == "none" and "library_album_id" not in kid
    assert [a["title"] for a in body["discography"]["library_only"]] == ["Bootleg"]
    assert body["discography"]["singles_eps"] == [] and body["discography"]["compilations"] == []
    assert body["top_tracks"][0]["title"] == "Creep" and body["top_tracks"][0]["preview_url"] == "http://p/5.mp3"
    assert set(body["top_tracks"][0]) >= {"id", "title", "album", "duration", "preview_url", "status"}


def test_request_status_shows_for_unmatched_album(api, db, monkeypatch):
    from plex_playlist_sync.models import MusicRequest, RequestStatus

    app, client, headers = api
    monkeypatch.setattr(artist_links, "get_enricher", lambda: _enricher(None))
    db.upsert_user("u1", "u1", "u1@x.tv", is_admin=False)
    db.create_request(MusicRequest(id="r1", user_id="u1", artist="Radiohead", title="Kid A", item_type="album",
                                   status=RequestStatus.PENDING, foreign_id="deezer:album:2"))
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(_details(albums=[_dalb("2", "Kid A")]))
    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=headers).json()
    assert body["artist"]["library_artist_id"] is None and body["library"] is None
    assert body["discography"]["albums"][0]["status"] == "requested"
    assert body["discography"]["albums"][0]["request_id"] == "r1"
    assert body["discography"]["library_only"] == []


def test_library_only_artist_without_discovery_match(api, db):
    app, client, headers = api
    _seed_radiohead(db)
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(search=[])
    body = client.get("/api/discovery/artist-profile?library_artist_id=a1", headers=headers).json()
    assert body["artist"]["discovery_id"] is None and body["artist"]["link_confidence"] == "none"
    assert body["top_tracks"] == []
    d = body["discography"]
    assert d["albums"] == d["singles_eps"] == d["compilations"] == []
    assert {a["title"] for a in d["library_only"]} == {"OK Computer", "Bootleg"}


def test_library_artist_with_discovery_match_gets_discography(api, db, monkeypatch):
    app, client, headers = api
    _seed_radiohead(db)
    monkeypatch.setattr(artist_links, "get_enricher", lambda: _enricher(MBID))
    search = [{"id": "deezer:artist:399", "name": "Radiohead", "nb_fan": 1}]
    disc = _discovery(_details(albums=[_dalb("2", "Kid A")]), search=search)
    app.dependency_overrides[get_discovery_client] = lambda: disc
    body = client.get("/api/discovery/artist-profile?library_artist_id=a1", headers=headers).json()
    assert body["artist"]["discovery_id"] == "deezer:artist:399" and body["artist"]["link_confidence"] == "mbid"
    assert [a["title"] for a in body["discography"]["albums"]] == ["Kid A"]
    assert len(body["discography"]["library_only"]) == 2


def test_deezer_failure_still_returns_library_section(api, db):
    app, client, headers = api
    _seed_radiohead(db)
    disc = _discovery()
    disc.search_artists.return_value = None  # lookup failed
    disc.get_artist_details.side_effect = requests.ConnectionError("down")
    disc.get_artist_top_tracks_detailed.side_effect = requests.ConnectionError("down")
    app.dependency_overrides[get_discovery_client] = lambda: disc
    resp = client.get("/api/discovery/artist-profile?library_artist_id=a1", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["artist"]["link_confidence"] == "none" and body["library"]["artist_id"] == "a1"
    assert body["top_tracks"] == []
    assert len(body["discography"]["library_only"]) == 2


def test_discovery_id_with_failed_details_and_cached_library_link_keeps_library(api, db):
    app, client, headers = api
    _seed_radiohead(db)
    db.save_artist_link("a1", "deezer:artist:399", MBID, "mbid", name="Radiohead")
    disc = _discovery()
    disc.get_artist_details.side_effect = requests.ConnectionError("down")
    app.dependency_overrides[get_discovery_client] = lambda: disc
    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=headers).json()
    assert body["library"]["artist_id"] == "a1"
    assert body["discography"]["albums"] == []
    assert len(body["discography"]["library_only"]) == 2


# ----------------------------------------------------------------------------------------------- annotate hints


def test_annotate_adds_library_artist_id_in_one_batched_query(db, config):
    _artist(db, "a1", "Radiohead")
    items = [
        {"id": "deezer:track:1", "item_type": "track", "title": "Creep", "artist": "Radiohead",
         "artist_discovery_id": "deezer:artist:399"},
        {"id": "deezer:track:2", "item_type": "track", "title": "Other", "artist": "radio-head"},
        {"id": "deezer:track:3", "item_type": "track", "title": "X", "artist": "Nobody"},
    ]
    out = annotate_item_statuses(items, db=db, config=config, user={"id": "root", "is_admin": True})
    assert [i.get("library_artist_id") for i in out] == ["a1", "a1", None]
    assert out[0]["artist_discovery_id"] == "deezer:artist:399"  # pass-through of the client's hint


@pytest.mark.parametrize("user", [None, {"id": "u", "is_admin": False}, {"id": "u", "is_admin": True, "forwarded": True}])
def test_annotate_withholds_library_artist_id_from_non_admins(db, config, user):
    _artist(db, "a1", "Radiohead")
    items = [{"id": "deezer:track:1", "item_type": "track", "title": "Creep", "artist": "Radiohead",
              "artist_discovery_id": "deezer:artist:399"}]
    out = annotate_item_statuses(items, db=db, config=config, user=user)
    assert "library_artist_id" not in out[0] and out[0]["artist_discovery_id"] == "deezer:artist:399"


def test_discovery_client_emits_artist_discovery_id():
    client = DiscoveryClient()
    client.session = MagicMock()

    def resp(payload):
        r = MagicMock(status_code=200)
        r.json.return_value = payload
        return r

    client.session.get.side_effect = [
        resp({"data": [{"id": 1, "title": "Creep", "artist": {"id": 399, "name": "Radiohead"}, "album": {"title": "PH"}}]}),
        resp({"data": [{"id": 2, "title": "Kid A", "artist": {"id": 399, "name": "Radiohead"}}]}),
    ]
    items = client.get_trending(limit=10)
    assert {i["artist_discovery_id"] for i in items} == {"deezer:artist:399"}


def test_discovery_item_omits_hint_when_unknown():
    from plex_playlist_sync.models import DiscoveryItem

    assert "artist_discovery_id" not in DiscoveryItem(id="x", item_type="album", title="t", artist="a").to_dict()


# ----------------------------------------------------------------------------------------------- library responses


def test_library_artist_list_and_detail_carry_cached_discovery_id(db, config):
    from plex_playlist_sync.api.dependencies import require_admin

    _seed_radiohead(db)
    _artist(db, "a2", "Unlinked")
    db.save_artist_link("a1", "deezer:artist:399", MBID, "mbid", name="Radiohead")
    db.save_artist_link("a2", None, None, "none")
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[require_admin] = lambda: {"id": "adm", "is_admin": True}
    app.dependency_overrides[get_lidarr_client] = lambda: None
    client = TestClient(app)

    listed = {a["id"]: a for a in client.get("/api/library/artists").json()}
    assert listed["a1"]["discovery_id"] == "deezer:artist:399" and listed["a2"]["discovery_id"] is None
    paged = {a["id"]: a for a in client.get("/api/library/artists/paged").json()["records"]}
    assert paged["a1"]["discovery_id"] == "deezer:artist:399"
    assert client.get("/api/library/artists/a1").json()["discovery_id"] == "deezer:artist:399"


# ----------------------------------------------------------------------------------------------- migration


def test_migration_v60_creates_artist_links(db):
    assert SCHEMA_VERSION >= 60
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(artist_links)")}
    assert {"library_artist_id", "discovery_id", "mbid", "confidence", "updated_at"} <= cols


def test_migration_v60_applies_on_existing_v59_database(tmp_path):
    path = str(tmp_path / "t.db")
    first = Database(path)
    first.conn.execute("DROP TABLE artist_links")
    first.conn.execute("DELETE FROM schema_migrations WHERE version >= 60")
    first.conn.commit()
    first.close()

    reopened = Database(path)
    try:
        assert reopened.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        reopened.save_artist_link("a1", "deezer:artist:1", None, "name")
        reopened.save_artist_link("a1", "deezer:artist:2", None, "name")  # same library id replaces the row
        assert reopened.get_artist_link(library_artist_id="a1")["discovery_id"] == "deezer:artist:2"
        with pytest.raises(sqlite3.IntegrityError):
            reopened.conn.execute("INSERT INTO artist_links (library_artist_id, confidence) VALUES ('a1', 'name')")
    finally:
        reopened.close()


def test_migration_v60_is_idempotent(db):
    db._migration_v60(db.conn.cursor())


# ----------------------------------------------------------------------------------------------- role shaping

_FORBIDDEN_FOR_REQUESTERS = {
    "library_artist_id", "library_album_id", "mbid", "link_confidence", "quality", "monitored", "path",
    "file_path", "track_file_count", "album_count", "artist_id",
}


def _keys(node: Any) -> set:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _requester_setup(api, db, monkeypatch):
    app, client, _admin = api
    _seed_radiohead(db)
    monkeypatch.setattr(artist_links, "get_enricher", lambda: _enricher(MBID))
    details = _details(albums=[_dalb("1", "OK Computer", "1997-05-21"), _dalb("2", "Kid A")])
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(details)
    return client


def test_non_admin_profile_has_no_library_management_data(api, db, user_headers, monkeypatch):
    client = _requester_setup(api, db, monkeypatch)
    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=user_headers).json()

    assert set(body) == {"artist", "library", "top_tracks", "discography"}
    assert set(body["artist"]) == {"name", "image_url", "discovery_id"}
    assert body["library"] is None
    assert not (_keys(body) & _FORBIDDEN_FOR_REQUESTERS)
    ok, kid = body["discography"]["albums"]
    assert ok["status"] == "partial" and ok["have_tracks"] == 2 and ok["total_tracks"] == 4
    assert kid["status"] == "none"
    assert body["discography"]["library_only"] == [{"title": "Bootleg", "year": 2001, "status": "in_library"}]
    for it in (ok, kid):
        assert set(it) <= set(__import__("plex_playlist_sync.artist_profile", fromlist=["x"])._REQUESTER_ITEM_KEYS)


def test_non_admin_cannot_address_by_library_artist_id(api, db, user_headers):
    app, client, _admin = api
    _seed_radiohead(db)
    app.dependency_overrides[get_discovery_client] = lambda: _discovery()
    assert client.get("/api/discovery/artist-profile?library_artist_id=a1", headers=user_headers).status_code == 403


def test_admin_profile_keeps_full_shape(api, db, monkeypatch):
    client = _requester_setup(api, db, monkeypatch)
    headers = api[2]
    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=headers).json()
    assert set(body["artist"]) == {"name", "image_url", "discovery_id", "library_artist_id", "mbid", "link_confidence"}
    assert set(body["library"]) == {"artist_id", "monitored", "album_count", "track_count", "track_file_count"}
    assert body["discography"]["albums"][0]["library_album_id"] == "L1"


def test_forwarded_admin_flag_is_never_trusted(api, db, monkeypatch):
    from plex_playlist_sync.api.dependencies import require_user

    app, client, _admin = api
    client = _requester_setup(api, db, monkeypatch)
    app.dependency_overrides[require_user] = lambda: {"id": "u", "is_admin": True, "forwarded": True}
    body = client.get("/api/discovery/artist-profile?discovery_id=deezer:artist:399").json()
    assert body["library"] is None and "library_artist_id" not in body["artist"]


def test_gateway_resolves_availability_on_core_and_has_no_local_library(db, tmp_path, monkeypatch):
    from unittest.mock import patch

    from plex_playlist_sync.clients.core_client import CoreClient

    cfg = Config(plex_url="http://p", plex_token="t", data_dir=str(tmp_path), role="gateway",
                 trackseerr_core_url="http://core:5251", internal_core_secret="x" * 40)
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_lidarr_client] = lambda: None
    monkeypatch.setattr(artist_links, "get_enricher", lambda: _enricher(None))
    _seed_radiohead(db)  # must be ignored: a gateway has no library
    app.dependency_overrides[get_discovery_client] = lambda: _discovery(_details(albums=[_dalb("2", "Kid A")]))
    headers = _headers(db, cfg, "alice", False)
    with patch.object(CoreClient, "get_availability",
                      return_value={"in_library": True, "status": "available", "quality": "FLAC"}) as avail:
        body = TestClient(app).get("/api/discovery/artist-profile?discovery_id=deezer:artist:399", headers=headers).json()
    assert avail.called and avail.call_args.kwargs["user_info"]["id"] == "alice"
    assert body["discography"]["albums"][0]["status"] == "available"
    assert body["discography"]["library_only"] == [] and body["library"] is None
    assert not (_keys(body) & _FORBIDDEN_FOR_REQUESTERS)


def test_artist_profile_route_is_on_the_gateway_local_allowlist():
    from plex_playlist_sync.api import tier_middleware as tm

    assert tm._allowed(tm.GATEWAY_LOCAL_ALLOWLIST, "GET", "/api/discovery/artist-profile")
    assert not tm._allowed(tm.GATEWAY_LOCAL_ALLOWLIST, "POST", "/api/discovery/artist-profile")
