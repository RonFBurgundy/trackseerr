"""Native metadata profiles: optional, off by default, shape only AUTOMATIC monitoring, never hide releases."""

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from trackseerr.api.dependencies import get_discovery_client, get_mbid_enricher
from trackseerr.artist_refresh import refresh_single_artist
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import (
    NATIVE_MONITOR_OPTIONS,
    album_in_metadata_profile,
    album_monitored_for_option,
)
from trackseerr.mediacover import mediacover_service
from trackseerr.storage import SCHEMA_VERSION, Database

from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

STUDIO_ALBUMS = {"primary_types": ["album"], "secondary_types": ["studio"]}
ADDED = "2020-01-01"


def _profile_id(db: Database, name: str) -> int:
    return next(p["id"] for p in db.list_metadata_profiles() if p["name"] == name)


def _artist(db: Database, aid: str = "ar", option: str = "all", profile: Optional[int] = None) -> None:
    db.upsert_library_artist(
        {"id": aid, "name": aid, "monitored": True, "monitor_option": option, "mbid": "mb-" + aid,
         "metadata_profile_id": profile, "created_at": ADDED}
    )


def _album(db: Database, album_id: str, album_type: str = "album", secondary: Optional[list[str]] = None,
           monitored: bool = True, aid: str = "ar", file: bool = False, year: int = 2000) -> None:
    db.upsert_library_album(
        {"id": album_id, "artist_id": aid, "title": album_id, "album_type": album_type, "year": year,
         "monitored": monitored, "secondary_types": secondary, "mb_release_group_id": "rg-" + album_id}
    )
    db.upsert_library_track({"id": album_id + "-t1", "album_id": album_id, "artist_id": aid, "title": "t",
                             "track_number": 1, "monitored": monitored})
    if file:
        db.upsert_library_file(
            {"id": "f-" + album_id, "track_id": album_id + "-t1", "file_path": f"/m/{album_id}.flac",
             "relative_path": f"{album_id}.flac", "codec": "FLAC", "quality_name": "FLAC", "size_bytes": 1}
        )


def _a(db: Database, album_id: str) -> bool:
    return bool(db.get_library_album(album_id)["monitored"])


# ------------------------------------------------------------------ migration + presets

def test_migration_seeds_presets_and_columns(test_db: Database):
    assert SCHEMA_VERSION >= 45
    profiles = {p["name"]: p for p in test_db.list_metadata_profiles()}
    assert set(profiles) >= {"Studio Albums", "Studio Albums, EPs & Singles", "Everything"}
    assert profiles["Studio Albums"]["primary_types"] == ["album"]
    assert profiles["Studio Albums"]["secondary_types"] == ["studio"]
    assert profiles["Studio Albums, EPs & Singles"]["primary_types"] == ["album", "ep", "single"]
    assert "live" in profiles["Everything"]["secondary_types"] and "broadcast" in profiles["Everything"]["primary_types"]
    assert all(p["artist_count"] == 0 for p in profiles.values())
    cols = {r[1] for r in test_db.conn.execute("PRAGMA table_info(library_albums)")}
    assert "secondary_types" in cols
    assert "metadata_profile_id" in {r[1] for r in test_db.conn.execute("PRAGMA table_info(library_artists)")}
    mm = test_db.get_media_management_settings()
    assert mm["add_metadata_profile_id"] is None  # off by default


# ------------------------------------------------------------------ semantics: option x profile

def _helper(option: str, album_type: str, secondary: Optional[list[str]], has_files: bool = False,
            profile: Optional[dict[str, Any]] = STUDIO_ALBUMS) -> bool:
    return album_monitored_for_option(
        option, artist_monitored=True, album_type=album_type, has_files=has_files, release_date="2021-05-05",
        year=2021, artist_added_at=ADDED, profile=profile, secondary_types=secondary,
    )


@pytest.mark.parametrize("option", ["all", "albums", "future"])
def test_out_of_profile_not_auto_monitored(option):
    assert _helper(option, "album", []) is True
    assert _helper(option, "album", ["live"]) is False
    assert _helper(option, "album", ["compilation"]) is False


def test_singles_eps_option_with_profile():
    ses = {"primary_types": ["ep", "single"], "secondary_types": ["studio"]}
    assert _helper("singles_eps", "single", [], profile=ses) is True
    assert _helper("singles_eps", "single", [], profile=STUDIO_ALBUMS) is False  # single not in profile
    assert _helper("singles_eps", "ep", ["remix"], profile=ses) is False


def test_existing_files_always_win_and_none_unchanged():
    assert _helper("existing", "album", ["live"], has_files=True) is True
    assert _helper("existing", "album", ["live"], has_files=False) is False
    assert _helper("none", "album", [], has_files=True) is False


@pytest.mark.parametrize("option", NATIVE_MONITOR_OPTIONS)
@pytest.mark.parametrize("album_type,secondary", [("album", None), ("single", []), ("live", ["live"]), ("album", ["live"])])
def test_no_profile_is_exactly_old_behaviour(option, album_type, secondary):
    with_none = _helper(option, album_type, secondary, profile=None)
    old = album_monitored_for_option(
        option, artist_monitored=True, album_type=album_type, has_files=False, release_date="2021-05-05",
        year=2021, artist_added_at=ADDED,
    )
    assert with_none is old


def test_null_secondary_is_studio_and_matching_rules():
    assert album_in_metadata_profile(STUDIO_ALBUMS, "album", None) is True
    assert album_in_metadata_profile(STUDIO_ALBUMS, "album", []) is True
    no_studio = {"primary_types": ["album"], "secondary_types": ["live"]}
    assert album_in_metadata_profile(no_studio, "album", None) is False
    assert album_in_metadata_profile(no_studio, "album", ["live"]) is True
    # every secondary type must be allowed
    assert album_in_metadata_profile(no_studio, "album", ["live", "remix"]) is False
    # album_type fallback when secondary unknown
    assert album_in_metadata_profile(STUDIO_ALBUMS, "live", None) is False
    assert album_in_metadata_profile(STUDIO_ALBUMS, "compilation", None) is False


@pytest.mark.parametrize("option", NATIVE_MONITOR_OPTIONS)
def test_sql_twin_agrees_with_python(test_db: Database, option):
    pid = _profile_id(test_db, "Studio Albums")
    _artist(test_db, option=option, profile=pid)
    cases = {
        "studio": ("album", None), "studio_empty": ("album", []), "live": ("live", ["live"]),
        "albumlive": ("album", ["live"]), "single": ("single", []), "comp": ("compilation", None),
        "owned_live": ("album", ["live"]),
    }
    for aid, (t, sec) in cases.items():
        _album(test_db, aid, album_type=t, secondary=sec, file=aid == "owned_live")
    test_db.bulk_edit_library_artists(["ar"], monitor_option=option, apply_monitor_to_albums=True)
    profile = test_db.get_metadata_profile(pid)
    for aid, (t, sec) in cases.items():
        expected = album_monitored_for_option(
            option, artist_monitored=option != "none" and True, album_type=t, has_files=aid == "owned_live",
            release_date=None, year=2000, artist_added_at=ADDED, profile=profile, secondary_types=sec,
        )
        assert _a(test_db, aid) is expected, (option, aid)


# ------------------------------------------------------------------ recompute on profile change

def test_profile_change_recomputes_and_clearing_restores(test_db: Database):
    _artist(test_db, option="all")
    _album(test_db, "studio")
    _album(test_db, "live", album_type="live", secondary=["live"])
    pid = _profile_id(test_db, "Studio Albums")
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=pid, apply_monitor_to_albums=True)
    assert test_db.get_library_artist("ar")["metadata_profile_id"] == pid
    assert (_a(test_db, "studio"), _a(test_db, "live")) == (True, False)
    assert test_db.get_library_track("live-t1")["monitored"] is False
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=None, apply_monitor_to_albums=True)
    assert test_db.get_library_artist("ar")["metadata_profile_id"] is None
    assert _a(test_db, "live") is True


def test_profile_change_without_apply_leaves_albums_alone(test_db: Database):
    _artist(test_db, option="all")
    _album(test_db, "live", album_type="live", secondary=["live"])
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=_profile_id(test_db, "Studio Albums"))
    assert _a(test_db, "live") is True


def test_existing_option_files_win_over_profile_on_recompute(test_db: Database):
    _artist(test_db, option="existing", profile=_profile_id(test_db, "Studio Albums"))
    _album(test_db, "owned_live", album_type="live", secondary=["live"], file=True)
    _album(test_db, "unowned_live", album_type="live", secondary=["live"])
    test_db.bulk_edit_library_artists(["ar"], apply_monitor_to_albums=True)
    assert (_a(test_db, "owned_live"), _a(test_db, "unowned_live")) == (True, False)
    assert test_db.get_library_track("owned_live-t1")["monitored"] is True


def test_bulk_edit_rejects_unknown_profile(test_db: Database):
    _artist(test_db)
    with pytest.raises(ValueError):
        test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=9999)


# ------------------------------------------------------------------ ingest / refresh

def _mb_refresh(db: Database, discography: list[dict[str, Any]], aid: str = "ar") -> None:
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mb-" + aid}
    enricher.get_artist_discography.return_value = discography
    enricher.get_artist_discography_result.return_value = (discography, True)
    enricher.get_release_group_tracks.return_value = []
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        assert refresh_single_artist(artist_id=aid, db=db, enricher=enricher, discovery_client=MagicMock())["success"]


def test_refresh_persists_secondary_types_and_applies_profile_to_new_albums(test_db: Database):
    _artist(test_db, option="all", profile=_profile_id(test_db, "Studio Albums"))
    _mb_refresh(test_db, [
        {"id": "rg-s", "title": "Studio LP", "album_type": "album", "secondary_types": [], "year": 2001},
        {"id": "rg-l", "title": "Live LP", "album_type": "live", "secondary_types": ["live"], "year": 2002},
        {"id": "rg-r", "title": "Remix LP", "album_type": "compilation", "secondary_types": ["compilation", "remix"], "year": 2003},
    ])
    by = {a["title"]: a for a in test_db.list_library_albums(artist_id="ar")}
    assert by["Studio LP"]["secondary_types"] == [] and by["Studio LP"]["monitored"] is True
    assert by["Live LP"]["secondary_types"] == ["live"] and by["Live LP"]["monitored"] is False
    assert by["Remix LP"]["secondary_types"] == ["compilation", "remix"]
    # the out-of-profile albums are still in the catalog
    assert len(by) == 3


def test_refresh_backfills_unknown_secondary_types_without_touching_monitoring(test_db: Database):
    _artist(test_db, option="all", profile=_profile_id(test_db, "Studio Albums"))
    _album(test_db, "old", album_type="live", secondary=None, monitored=True)  # manual monitor, secondary unknown
    assert test_db.get_library_album("old")["secondary_types"] is None
    _mb_refresh(test_db, [{"id": "rg-old", "title": "old", "album_type": "live", "secondary_types": ["live"], "year": 2000}])
    row = test_db.get_library_album("old")
    assert row["secondary_types"] == ["live"]
    assert row["monitored"] is True  # a manual monitor of an out-of-profile album survives refresh


def test_refresh_without_profile_unchanged(test_db: Database):
    _artist(test_db, option="all")
    _mb_refresh(test_db, [{"id": "rg-l", "title": "Live LP", "album_type": "live", "secondary_types": ["live"], "year": 2002}])
    assert [a["monitored"] for a in test_db.list_library_albums(artist_id="ar")] == [True]


def test_upsert_without_secondary_types_keeps_existing(test_db: Database):
    _artist(test_db)
    _album(test_db, "x", secondary=["live"])
    test_db.upsert_library_album({"id": "x", "artist_id": "ar", "title": "x"})
    assert test_db.get_library_album("x")["secondary_types"] == ["live"]


def test_manual_album_monitor_of_out_of_profile_album_works(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db, option="all", profile=_profile_id(test_db, "Studio Albums"))
    _album(test_db, "live", album_type="live", secondary=["live"], monitored=False)
    r = client.put("/api/library/albums/live/monitored", json={"monitored": True}, headers=h)
    assert r.status_code == 200, r.text
    assert _a(test_db, "live") is True


# ------------------------------------------------------------------ API

def test_crud_and_artist_counts_and_delete_nulls(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    r = client.get("/api/library/metadata-profiles", headers=h)
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["profiles"]]
    assert "Studio Albums" in names and "studio" in r.json()["secondary_types"]

    body = {"name": "Mine", "primary_types": ["Album", "ep"], "secondary_types": ["studio", "live"]}
    r = client.post("/api/library/metadata-profiles", json=body, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["primary_types"] == ["album", "ep"]
    assert client.post("/api/library/metadata-profiles", json=body, headers=h).status_code == 400  # duplicate name
    bad = client.post("/api/library/metadata-profiles", json={**body, "name": "B", "primary_types": ["nope"]}, headers=h)
    assert bad.status_code == 400
    empty = client.post("/api/library/metadata-profiles", json={**body, "name": "E", "secondary_types": []}, headers=h)
    assert empty.status_code == 400

    r = client.put(f"/api/library/metadata-profiles/{pid}", json={**body, "name": "Renamed"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert client.put("/api/library/metadata-profiles/9999", json=body, headers=h).status_code == 404

    _artist(test_db, "a1", profile=pid)
    _artist(test_db, "a2", profile=pid)
    test_db.update_media_management_settings({"add_metadata_profile_id": pid})
    counts = {p["id"]: p["artist_count"] for p in client.get("/api/library/metadata-profiles", headers=h).json()["profiles"]}
    assert counts[pid] == 2

    r = client.delete(f"/api/library/metadata-profiles/{pid}", headers=h)
    assert r.status_code == 200 and r.json()["artists_cleared"] == 2
    assert test_db.get_library_artist("a1")["metadata_profile_id"] is None
    assert test_db.get_media_management_settings()["add_metadata_profile_id"] is None
    assert client.delete(f"/api/library/metadata-profiles/{pid}", headers=h).status_code == 404


def test_single_and_bulk_edit_metadata_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_id(test_db, "Studio Albums")
    for aid in ("a1", "a2"):
        _artist(test_db, aid)
        _album(test_db, aid + "-s", aid=aid)
        _album(test_db, aid + "-l", album_type="live", secondary=["live"], aid=aid)

    r = client.put("/api/library/artists/a1/monitored",
                   json={"monitored": True, "metadata_profile_id": pid, "apply_monitor_to_albums": True}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist("a1")["metadata_profile_id"] == pid
    assert (_a(test_db, "a1-s"), _a(test_db, "a1-l")) == (True, False)
    assert client.put("/api/library/artists/a1/monitored",
                      json={"monitored": True, "metadata_profile_id": 9999}, headers=h).status_code == 400

    r = client.post("/api/library/artists/bulk-edit",
                    json={"artist_ids": ["a2"], "metadata_profile_id": pid, "apply_monitor_to_albums": True}, headers=h)
    assert r.status_code == 200, r.text
    assert (_a(test_db, "a2-s"), _a(test_db, "a2-l")) == (True, False)

    # explicit null clears; omitted leaves alone
    r = client.post("/api/library/artists/bulk-edit", json={"all": True, "monitored": True}, headers=h)
    assert r.status_code == 200
    assert test_db.get_library_artist("a2")["metadata_profile_id"] == pid
    r = client.post("/api/library/artists/bulk-edit", json={"all": True, "metadata_profile_id": None}, headers=h)
    assert r.status_code == 200
    assert test_db.get_library_artist("a2")["metadata_profile_id"] is None


def test_preview_counts(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db)
    _album(test_db, "s")
    _album(test_db, "l", album_type="live", secondary=["live"])
    _album(test_db, "e", album_type="ep")
    pid = _profile_id(test_db, "Studio Albums")
    r = client.get(f"/api/library/artists/ar/metadata-profile-preview?profile_id={pid}", headers=h)
    assert r.status_code == 200
    assert {k: r.json()[k] for k in ("matching", "total")} == {"matching": 1, "total": 3}
    assert r.json()["would_change"]["albums_to_unmonitor"] == 2  # option 'all', all three currently monitored
    pid2 = _profile_id(test_db, "Everything")
    r2 = client.get(f"/api/library/artists/ar/metadata-profile-preview?profile_id={pid2}", headers=h).json()
    assert (r2["matching"], r2["total"]) == (3, 3)
    assert r2["would_change"] == {"albums_to_monitor": 0, "albums_to_unmonitor": 0,
                                  "tracks_to_monitor": 0, "tracks_to_unmonitor": 0}
    assert client.get("/api/library/artists/nope/metadata-profile-preview?profile_id=1", headers=h).status_code == 404


def test_detail_payload_in_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db)
    _album(test_db, "s")
    _album(test_db, "l", album_type="live", secondary=["live"])
    d = client.get("/api/library/artists/ar", headers=h).json()
    assert d["metadata_profile_id"] is None
    assert all(a["in_profile"] is True for a in d["albums"])  # no profile: everything in profile
    pid = _profile_id(test_db, "Studio Albums")
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=pid)
    d = client.get("/api/library/artists/ar", headers=h).json()
    assert d["metadata_profile_id"] == pid
    assert {a["id"]: a["in_profile"] for a in d["albums"]} == {"s": True, "l": False}
    assert len(d["albums"]) == 2  # never hidden


def test_ingest_defaults_to_add_metadata_profile(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    discovery = MagicMock()
    discovery.get_artist_details.return_value = {
        "albums": [{"id": "d:1", "title": "LP", "year": 2000}],
        "compilations": [{"id": "d:2", "title": "Hits", "year": 2001}],
    }
    app.dependency_overrides[get_discovery_client] = lambda: discovery
    no_mb = MagicMock(spec=MbidEnricherClient)
    no_mb.lookup_artist_mbid.return_value = None
    app.dependency_overrides[get_mbid_enricher] = lambda: no_mb
    test_db.update_media_management_settings({"add_monitor_option": "all"})
    r = client.post("/api/library/artists/ingest", json={"foreign_artist_id": "deezer:artist:1", "artist_name": "NoProf"}, headers=h)
    assert r.status_code == 200, r.text
    art = test_db.get_library_artist_by_name("NoProf")
    assert art["metadata_profile_id"] is None
    assert [a["monitored"] for a in test_db.list_library_albums(artist_id=art["id"])] == [True, True]

    pid = _profile_id(test_db, "Studio Albums")
    test_db.update_media_management_settings({"add_metadata_profile_id": pid})
    r = client.post("/api/library/artists/ingest", json={"foreign_artist_id": "deezer:artist:2", "artist_name": "Dflt"}, headers=h)
    art = test_db.get_library_artist_by_name("Dflt")
    assert art["metadata_profile_id"] == pid
    assert {a["title"]: a["monitored"] for a in test_db.list_library_albums(artist_id=art["id"])} == {"LP": True, "Hits": False}

    # explicit null overrides the default
    client.post("/api/library/artists/ingest",
                json={"foreign_artist_id": "deezer:artist:3", "artist_name": "Over", "metadata_profile_id": None}, headers=h)
    assert test_db.get_library_artist_by_name("Over")["metadata_profile_id"] is None


def test_settings_roundtrip_add_metadata_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_id(test_db, "Everything")
    assert client.post("/api/settings/media-management", json={"add_metadata_profile_id": pid}, headers=h).status_code == 200
    assert client.get("/api/settings/media-management", headers=h).json()["settings"]["add_metadata_profile_id"] == pid
    assert client.post("/api/settings/media-management", json={"add_metadata_profile_id": None}, headers=h).status_code == 200
    assert test_db.get_media_management_settings()["add_metadata_profile_id"] is None
    assert client.post("/api/settings/media-management", json={"add_metadata_profile_id": 9999}, headers=h).status_code == 400


def test_native_only_guard_409_in_lidarr_mode(app_and_client, test_db, test_config, seeded_users):
    from trackseerr.api.dependencies import get_lidarr_client

    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr' WHERE id = 1")
    test_db.conn.commit()
    app.dependency_overrides[get_lidarr_client] = lambda: MagicMock()
    body = {"name": "X", "primary_types": ["album"], "secondary_types": ["studio"]}
    assert client.get("/api/library/metadata-profiles", headers=h).status_code == 409
    assert client.post("/api/library/metadata-profiles", json=body, headers=h).status_code == 409
    assert client.put("/api/library/metadata-profiles/1", json=body, headers=h).status_code == 409
    assert client.delete("/api/library/metadata-profiles/1", headers=h).status_code == 409
    assert client.get("/api/library/artists/1/metadata-profile-preview?profile_id=1", headers=h).status_code == 409
    assert client.post("/api/library/artists/bulk-edit", json={"all": True, "metadata_profile_id": 1}, headers=h).status_code == 409
    assert client.put("/api/library/artists/1/monitored", json={"monitored": True, "metadata_profile_id": 1}, headers=h).status_code == 409


# --- profile effectiveness on add (MusicBrainz secondary types) and the refresh release date --------------------

MB_RGS = [
    {"id": "rg-lp", "title": "Studio LP", "primary-type": "Album", "secondary-types": [], "first-release-date": "2000-03-01"},
    {"id": "rg-live", "title": "Live At Y", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2002-05-05"},
    {"id": "rg-hits", "title": "Greatest Hits", "primary-type": "Album", "secondary-types": ["Compilation"], "first-release-date": "2001-01-01"},
]


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._p, self.status_code, self.headers = payload, status_code, {}

    def json(self) -> dict[str, Any]:
        return self._p


def _mb_http(rgs: Optional[list[dict[str, Any]]] = None, down: bool = False) -> MbidEnricherClient:
    """A real MbidEnricherClient whose HTTP session is mocked (no network, no rate limiting)."""
    enr = MbidEnricherClient(base_url="http://mb.test", min_interval=0)

    def fake_get(url: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> _Resp:
        if down:
            raise requests.ConnectionError("mb down")
        if url.endswith("/ws/2/artist"):
            return _Resp({"artists": [{"id": "mb-artist-1"}]})
        if url.endswith("/ws/2/release-group"):
            return _Resp({"release-groups": rgs if rgs is not None else MB_RGS})
        return _Resp({}, 404)

    enr._session = MagicMock()
    enr._session.get.side_effect = fake_get
    return enr


def _discovery() -> MagicMock:
    d = MagicMock()
    d.get_artist_details.return_value = {
        "albums": [
            {"id": "d:1", "title": "Studio LP", "year": 2000},
            {"id": "d:2", "title": "Live At Y", "year": 2002},  # Deezer labels live albums as plain albums
        ],
        "compilations": [{"id": "d:3", "title": "Greatest Hits", "year": 2001}],
    }
    d.get_album_details.return_value = None
    return d


def _ingest(app, client, db, config, users, enricher, option: str = "all", name: str = "Prof") -> dict[str, Any]:
    h = _auth_headers(users["admin"], db, config)
    app.dependency_overrides[get_discovery_client] = _discovery
    app.dependency_overrides[get_mbid_enricher] = lambda: enricher
    r = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": f"deezer:artist:{name}", "artist_name": name, "monitor_option": option,
              "metadata_profile_id": _profile_id(db, "Studio Albums")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return db.get_library_artist_by_name(name)


def _by_title(db: Database, artist_id: str) -> dict[str, dict[str, Any]]:
    return {a["title"]: a for a in db.list_library_albums(artist_id=artist_id)}


def _refresh_real(db: Database, artist_id: str, enricher: MbidEnricherClient) -> None:
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        assert refresh_single_artist(artist_id=artist_id, db=db, enricher=enricher, discovery_client=_discovery())["success"]


def _run_jobs(background_jobs) -> int:
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        return background_jobs.run()


def test_ingest_with_profile_always_defers_then_background_job_applies_mb_types(
    app_and_client, test_db, test_config, seeded_users, background_jobs
):
    app, client = app_and_client
    enricher = _mb_http()
    art = _ingest(app, client, test_db, test_config, seeded_users, enricher)
    # the request returned without touching MusicBrainz: flag set, job queued, types still unknown
    assert enricher._session.get.call_count == 0
    assert test_db.get_library_artist(art["id"])["pending_profile_recompute"]
    assert len(background_jobs.pending) == 1
    assert _by_title(test_db, art["id"])["Live At Y"]["monitored"] is True

    assert _run_jobs(background_jobs) == 1
    art = test_db.get_library_artist(art["id"])
    albums = _by_title(test_db, art["id"])
    assert albums["Studio LP"]["monitored"] is True
    assert albums["Live At Y"]["monitored"] is False and albums["Live At Y"]["secondary_types"] == ["live"]
    assert albums["Greatest Hits"]["monitored"] is False
    assert art["mbid"] == "mb-artist-1" and not art["pending_profile_recompute"]


def test_ingest_with_mb_down_defers_then_first_refresh_recomputes_once(
    app_and_client, test_db, test_config, seeded_users, background_jobs
):
    app, client = app_and_client
    art = _ingest(app, client, test_db, test_config, seeded_users, _mb_http(down=True))
    assert test_db.get_library_artist(art["id"])["pending_profile_recompute"]
    _run_jobs(background_jobs)  # MusicBrainz is down: the job logs and leaves the flag for the next refresh
    assert test_db.get_library_artist(art["id"])["pending_profile_recompute"]
    pre = _by_title(test_db, art["id"])
    assert pre["Live At Y"]["monitored"] is True  # secondary types unknown yet: counts as studio

    _refresh_real(test_db, art["id"], _mb_http())
    albums = _by_title(test_db, art["id"])
    assert albums["Studio LP"]["monitored"] is True
    assert albums["Live At Y"]["monitored"] is False
    assert albums["Greatest Hits"]["monitored"] is False
    assert not test_db.get_library_artist(art["id"])["pending_profile_recompute"]

    # once only: a later manual monitor survives further refreshes
    test_db.set_album_monitored(albums["Live At Y"]["id"], True)
    _refresh_real(test_db, art["id"], _mb_http())
    assert _by_title(test_db, art["id"])["Live At Y"]["monitored"] is True


def test_manual_monitor_before_deferred_recompute_survives(
    app_and_client, test_db, test_config, seeded_users, background_jobs
):
    app, client = app_and_client
    art = _ingest(app, client, test_db, test_config, seeded_users, _mb_http())
    live = _by_title(test_db, art["id"])["Live At Y"]
    test_db.set_album_monitored(live["id"], True)  # user touches the artist's albums before the job runs
    assert not test_db.get_library_artist(art["id"])["pending_profile_recompute"]
    _run_jobs(background_jobs)  # the queued job now persists types but must not recompute over the user's choice
    albums = _by_title(test_db, art["id"])
    assert albums["Live At Y"]["monitored"] is True and albums["Live At Y"]["secondary_types"] == ["live"]
    art = test_db.get_library_artist(art["id"])
    assert not art["pending_profile_recompute"]
    # and the old scenario: MusicBrainz down at ingest, later refresh
    art = _ingest(app, client, test_db, test_config, seeded_users, _mb_http(down=True), name="Prof2")
    live = _by_title(test_db, art["id"])["Live At Y"]
    test_db.set_album_monitored(live["id"], True)
    _refresh_real(test_db, art["id"], _mb_http())
    albums = _by_title(test_db, art["id"])
    assert albums["Live At Y"]["monitored"] is True and albums["Live At Y"]["secondary_types"] == ["live"]


def test_ingest_existing_or_none_option_sets_no_deferred_flag(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    art = _ingest(app, client, test_db, test_config, seeded_users, _mb_http(down=True), option="none")
    assert not test_db.get_library_artist(art["id"])["pending_profile_recompute"]


def test_refresh_persists_first_release_date_from_enricher(test_db: Database):
    _artist(test_db, option="none")
    _album(test_db, "Studio LP", year=None)  # existing album without a date
    _refresh_real(test_db, "ar", _mb_http())
    row = test_db.get_library_album("Studio LP")
    assert row["release_date"] == "2000-03-01" and row["year"] == 2000
    live = next(a for a in test_db.list_library_albums(artist_id="ar") if a["title"] == "Live At Y")
    assert live["release_date"] == "2002-05-05"


def test_future_option_works_off_refresh_release_date(test_db: Database):
    _artist(test_db, option="future")  # artist added 2020-01-01
    rgs = [
        {"id": "rg-old", "title": "Old", "primary-type": "Album", "secondary-types": [], "first-release-date": "2010-06-01"},
        {"id": "rg-new", "title": "New", "primary-type": "Album", "secondary-types": [], "first-release-date": "2030-06-01"},
    ]
    _refresh_real(test_db, "ar", _mb_http(rgs))
    by = _by_title(test_db, "ar")
    assert by["New"]["release_date"] == "2030-06-01" and by["New"]["monitored"] is True
    assert by["Old"]["monitored"] is False


# ------------------------------------------------------------------ v48 rename migration + deprecated aliases



def test_deprecated_release_profile_request_aliases(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_id(test_db, "Studio Albums")
    _artist(test_db, "a1")
    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1"], "release_profile_id": pid}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist("a1")["metadata_profile_id"] == pid
    r = client.put("/api/library/artists/a1/monitored", json={"monitored": True, "release_profile_id": None}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist("a1")["metadata_profile_id"] is None  # explicit null via the alias clears
    r = client.post("/api/settings/media-management", json={"add_release_profile_id": pid}, headers=h)
    assert r.status_code == 200, r.text
    assert "release_profile_id" not in r.text
    assert test_db.get_media_management_settings()["add_metadata_profile_id"] == pid
    # The new name wins when both are sent.
    r = client.post("/api/library/artists/bulk-edit",
                    json={"artist_ids": ["a1"], "release_profile_id": None, "metadata_profile_id": pid}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist("a1")["metadata_profile_id"] == pid
