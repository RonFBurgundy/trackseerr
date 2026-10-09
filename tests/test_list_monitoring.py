"""apply_list_item levels (native + Lidarr), playlist monitor modes, import list sync and the acquisition fix."""

from tests.audio_fixtures import write_flac
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.backlog_worker import WantedBacklogWorker
from plex_playlist_sync.clients.import_lists import ImportListError, ImportListItem
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.import_list_worker import (
    ImportListBusy,
    ImportListWorker,
    claim_sync,
    release_sync,
    sync_import_list,
)
from plex_playlist_sync.library_monitoring import LIST_MONITOR_MODES
from plex_playlist_sync.list_monitoring import (
    ListItem,
    apply_list_item,
    apply_playlist_missing,
    effective_level,
)
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryArtist,
)
from plex_playlist_sync.request_submission import RequestRejected
from plex_playlist_sync.storage import Database
from tests.lidarr_fake import FastClock

ART = "aaaaaaaa-0000-0000-0000-00000000000a"
RG = "bbbbbbbb-0000-0000-0000-00000000000b"
RG2 = "cccccccc-0000-0000-0000-00000000000c"


@pytest.fixture
def db():
    database = Database(":memory:")
    database.upsert_user("admin-1", "admin", "a@example.com", is_admin=True)
    database.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Nothing here may reach the network or sleep for real."""
    # Patch the module's own ``time`` name: ``list_monitoring.time.sleep`` is the process-global ``time.sleep``, and
    # a no-op there turns every background thread's sleep loop (in any module) into a hot loop for the whole test.
    monkeypatch.setattr("plex_playlist_sync.list_monitoring.time", FastClock())
    monkeypatch.setattr("plex_playlist_sync.mediacover.mediacover_service.ensure_artwork", lambda *a, **k: Path("/tmp/c.jpg"))


def make_enricher() -> MagicMock:
    enr = MagicMock(spec=MbidEnricherClient)
    enr.lookup_artist_mbid.return_value = ART
    enr.lookup_album_mbids.return_value = {"mb_release_group_id": RG, "mb_artist_id": ART}
    enr.lookup_track_mbids.return_value = {
        "musicbrainz_artistid": ART,
        "musicbrainz_releasegroupid": RG,
        "musicbrainz_albumid": "rel",
        "musicbrainz_trackid": "rec",
    }
    enr.get_artist_details.return_value = {"id": ART}
    discography = [
        {"id": RG, "title": "OK Computer", "album_type": "album", "year": 1997},
        {"id": RG2, "title": "The Bends", "album_type": "album", "year": 1995},
        {"id": "dddddddd-0000-0000-0000-00000000000d", "title": "Creep", "album_type": "single", "year": 1992},
    ]
    enr.get_artist_discography.return_value = discography
    enr.get_artist_discography_result.return_value = (discography, True)
    enr.source_available.return_value = True
    enr.stats.return_value = {"network_requests": 0, "cache_hits": 0}
    enr.get_release_group_tracks.return_value = [
        {"track_number": 1, "disc_number": 1, "title": "Airbag", "duration_seconds": 284.0, "mb_recording_id": "r1"},
        {"track_number": 2, "disc_number": 1, "title": "Paranoid Android", "duration_seconds": 383.0, "mb_recording_id": "r2"},
    ]
    return enr


def album_item(**kw: Any) -> ListItem:
    base: dict[str, Any] = dict(kind="album", artist_name="Radiohead", album_title="OK Computer", mbid=RG, artist_mbid=ART)
    base.update(kw)
    return ListItem(**base)


def artist_item(**kw: Any) -> ListItem:
    base: dict[str, Any] = dict(kind="artist", artist_name="Radiohead", mbid=ART, artist_mbid=ART)
    base.update(kw)
    return ListItem(**base)


def track_item(**kw: Any) -> ListItem:
    base: dict[str, Any] = dict(kind="track", artist_name="Radiohead", album_title="OK Computer", track_title="Airbag")
    base.update(kw)
    return ListItem(**base)


def albums_of(db: Database, artist_id: str) -> dict[str, bool]:
    return {a["title"]: bool(a["monitored"]) for a in db.list_library_albums(artist_id=artist_id)}


# ------------------------------------------------------------------ level widening


@pytest.mark.parametrize(
    "kind,mode,expected",
    [
        ("track", "track", "track"),
        ("track", "album", "album"),
        ("track", "artist", "artist"),
        ("album", "track", "album"),
        ("album", "album", "album"),
        ("album", "artist", "artist"),
        ("artist", "track", "artist"),
        ("artist", "album", "artist"),
        ("artist", "artist", "artist"),
        ("track", "none", None),
        ("album", "none", None),
        ("artist", "none", None),
    ],
)
def test_effective_level_table(kind, mode, expected):
    assert effective_level(kind, mode) == expected


def test_effective_level_rejects_unknown():
    with pytest.raises(ValueError):
        effective_level("track", "everything")
    with pytest.raises(ValueError):
        effective_level("playlist", "track")
    assert LIST_MONITOR_MODES == ("track", "album", "artist", "none")


def test_none_mode_records_only(db):
    enr = make_enricher()
    res = apply_list_item(db, None, album_item(), "none", enricher=enr)
    assert (res.status, res.applied_level) == ("skipped", None)
    enr.lookup_album_mbids.assert_not_called()
    assert db.list_library_artists() == []


def test_invalid_mode_and_option_raise(db):
    with pytest.raises(ValueError):
        apply_list_item(db, None, album_item(), "bogus")
    with pytest.raises(ValueError):
        apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="bogus")


# ------------------------------------------------------------------ native: album level


def test_native_album_new_artist_monitors_only_that_album_and_tracks(db):
    enr = make_enricher()
    res = apply_list_item(db, None, album_item(), "album", enricher=enr)
    assert (res.status, res.applied_level, res.mbid) == ("applied", "album", RG)

    artist = db.get_library_artist_by_mbid(ART)
    assert artist["monitored"] is True and artist["monitor_option"] == "none"
    assert albums_of(db, artist["id"]) == {"OK Computer": True}
    tracks = db.list_library_tracks(album_id=db.get_library_album_by_release_group_id(RG)["id"])
    assert sorted(t["title"] for t in tracks) == ["Airbag", "Paranoid Android"]
    assert all(t["monitored"] for t in tracks)

    # A later artist refresh (option "none") adds the rest of the discography unmonitored and keeps this album.
    from plex_playlist_sync.artist_refresh import refresh_single_artist

    assert refresh_single_artist(artist["id"], db, enricher=enr)["success"] is True
    assert albums_of(db, artist["id"]) == {"OK Computer": True, "The Bends": False, "Creep": False}


def test_native_album_widened_from_track_item_in_track_mode_via_mbid_lookup(db):
    enr = make_enricher()
    res = apply_list_item(db, None, track_item(), "album", enricher=enr)
    assert (res.status, res.applied_level) == ("applied", "album")
    enr.lookup_track_mbids.assert_called_once()
    assert albums_of(db, db.get_library_artist_by_mbid(ART)["id"]) == {"OK Computer": True}


def test_native_album_item_in_track_mode_is_widened_to_album(db):
    res = apply_list_item(db, None, album_item(), "track", enricher=make_enricher())
    assert (res.status, res.applied_level) == ("applied", "album")


def test_native_album_existing_artist_is_left_alone_and_never_downgraded(db):
    db.upsert_library_artist(
        LibraryArtist(id="art-x", name="Radiohead", mbid=ART, monitored=True, monitor_option="existing")
    )
    db.upsert_library_album({"id": "alb-bends", "artist_id": "art-x", "title": "The Bends", "mb_release_group_id": RG2, "monitored": False})
    db.upsert_library_album({"id": "alb-own", "artist_id": "art-x", "title": "Pablo Honey", "mb_release_group_id": "rg-p", "monitored": True})
    res = apply_list_item(db, None, album_item(), "album", enricher=make_enricher())
    assert res.status == "applied"
    artist = db.get_library_artist("art-x")
    assert (artist["monitored"], artist["monitor_option"]) == (True, "existing")
    assert albums_of(db, "art-x") == {"The Bends": False, "Pablo Honey": True, "OK Computer": True}


def test_native_album_existing_unmonitored_album_gets_monitored_with_all_tracks(db):
    db.upsert_library_artist(LibraryArtist(id="art-x", name="Radiohead", mbid=ART, monitored=True, monitor_option="existing"))
    db.upsert_library_album({"id": "alb-1", "artist_id": "art-x", "title": "OK Computer", "mb_release_group_id": RG, "monitored": False})
    db.upsert_library_track({"id": "t-1", "album_id": "alb-1", "artist_id": "art-x", "title": "Airbag", "track_number": 1, "monitored": False})
    apply_list_item(db, None, album_item(), "album", enricher=make_enricher())
    assert db.get_library_album("alb-1")["monitored"] is True
    assert db.get_library_track("t-1")["monitored"] is True
    # Re-applying never flips anything back and adds no duplicates.
    apply_list_item(db, None, album_item(), "album", enricher=make_enricher())
    assert len(db.list_library_tracks(album_id="alb-1")) == 2


def test_native_album_on_unmonitored_existing_artist_monitors_artist_only(db):
    db.upsert_library_artist(LibraryArtist(id="art-x", name="Radiohead", mbid=ART, monitored=False, monitor_option="existing"))
    db.upsert_library_album({"id": "alb-sib", "artist_id": "art-x", "title": "Pablo Honey", "mb_release_group_id": "rg-p", "monitored": False})
    db.upsert_library_track({"id": "t-sib", "album_id": "alb-sib", "artist_id": "art-x", "title": "Creep", "track_number": 1, "monitored": False})
    res = apply_list_item(db, None, album_item(), "album", enricher=make_enricher())
    assert res.status == "applied" and not res.error
    artist = db.get_library_artist("art-x")
    assert (artist["monitored"], artist["monitor_option"]) == (True, "existing")
    assert albums_of(db, "art-x") == {"Pablo Honey": False, "OK Computer": True}  # sibling untouched
    assert db.get_library_track("t-sib")["monitored"] is False
    assert any(
        r["album"] == "OK Computer" for r in db.list_wanted("missing", 1, 50, "artist", "asc")[0]
    )


def test_album_unresolved_when_musicbrainz_has_no_match(db):
    enr = make_enricher()
    enr.lookup_album_mbids.return_value = None
    res = apply_list_item(db, None, album_item(mbid=None, artist_mbid=None), "album", enricher=enr)
    assert res.status == "unresolved" and "release group" in res.error
    assert db.list_library_artists() == []


# ------------------------------------------------------------------ native: artist level


def test_native_artist_uses_artist_monitor_option_and_refresh_rules(db):
    enr = make_enricher()
    res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="albums", enricher=enr)
    assert (res.status, res.applied_level, res.mbid) == ("applied", "artist", ART)
    artist = db.get_library_artist_by_mbid(ART)
    assert (artist["monitored"], artist["monitor_option"]) == (True, "albums")
    assert albums_of(db, artist["id"]) == {"OK Computer": True, "The Bends": True, "Creep": False}


def test_native_artist_falls_back_to_add_monitor_option(db):
    db.update_media_management_settings({"add_monitor_option": "singles_eps"})
    apply_list_item(db, None, artist_item(), "artist", enricher=make_enricher())
    artist = db.get_library_artist_by_mbid(ART)
    assert artist["monitor_option"] == "singles_eps"
    assert albums_of(db, artist["id"]) == {"OK Computer": False, "The Bends": False, "Creep": True}


def test_native_artist_resolves_by_name_when_mbid_missing(db):
    enr = make_enricher()
    res = apply_list_item(db, None, artist_item(mbid=None, artist_mbid=None), "artist", artist_monitor_option="none", enricher=enr)
    enr.lookup_artist_mbid.assert_called_once_with("Radiohead")
    assert res.status == "applied" and db.get_library_artist_by_mbid(ART) is not None


def test_native_artist_unresolved(db):
    enr = make_enricher()
    enr.lookup_artist_mbid.return_value = None
    res = apply_list_item(db, None, artist_item(mbid=None, artist_mbid=None), "artist", enricher=enr)
    assert res.status == "unresolved"


def test_native_artist_existing_is_untouched(db):
    db.upsert_library_artist(LibraryArtist(id="art-x", name="Radiohead", monitored=False, monitor_option="none"))
    res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="all", enricher=make_enricher())
    assert res.status == "applied"
    artist = db.get_library_artist("art-x")
    assert (artist["monitored"], artist["monitor_option"], artist["mbid"]) == (False, "none", ART)
    assert db.list_library_albums(artist_id="art-x") == []  # no refresh was run for an existing artist


def test_artist_widening_from_track_item(db):
    res = apply_list_item(db, None, track_item(), "artist", artist_monitor_option="none", enricher=make_enricher())
    assert (res.status, res.applied_level) == ("applied", "artist")


# ------------------------------------------------------------------ track level


def test_track_mode_goes_through_submit_track_request_as_system_request(db):
    alice = db.get_user("user-alice")
    with patch("plex_playlist_sync.list_monitoring.submit_track_request") as submit:
        res = apply_list_item(db, "cfg", track_item(mbid="rec-1"), "track", quality_profile_id="qp-1", requested_by=alice)
    assert (res.status, res.applied_level) == ("applied", "track")
    args, kwargs = submit.call_args
    assert args[0] is db and args[1] == "cfg"
    user, title, artist, album = args[2:6]
    assert (title, artist, album) == ("Airbag", "Radiohead", "OK Computer")
    assert user["id"] == "user-alice" and user["is_admin"] is True and user["forwarded"] is False
    assert kwargs["quality_profile_id"] == "qp-1" and kwargs["source"] == "list" and kwargs["foreign_id"] == "rec-1"
    assert alice["is_admin"] is False  # the caller's row is not mutated


def test_track_mode_creates_approved_request_attributed_to_user_and_ignores_quota(db):
    db.update_account_settings({"default_quota_tracks": 0})
    alice = db.get_user("user-alice")
    res = apply_list_item(db, None, track_item(), "track", requested_by=alice)
    assert res.status == "applied"
    reqs = db.list_requests(user_id="user-alice")
    assert len(reqs) == 1
    assert (reqs[0]["title"], reqs[0]["artist"], reqs[0]["status"]) == ("Airbag", "Radiohead", "processing")


def test_track_mode_without_user_fails_and_missing_title_unresolved(db):
    assert apply_list_item(db, None, track_item(), "track", requested_by=None).status == "failed"
    assert apply_list_item(db, None, track_item(track_title=""), "track", requested_by=db.get_user("admin-1")).status == "unresolved"


def test_track_mode_rejected_request_is_failed_not_raised(db):
    with patch("plex_playlist_sync.list_monitoring.submit_track_request", side_effect=RequestRejected("quota", 400, "Request quota reached")):
        res = apply_list_item(db, None, track_item(), "track", requested_by=db.get_user("admin-1"))
    assert (res.status, res.error) == ("failed", "Request quota reached")


# ------------------------------------------------------------------ lidarr mode


@pytest.fixture
def lidarr_mode(db):
    db.update_media_management_settings({"library_mode": "lidarr"})


def lidarr_client(*, artist_id: int = 0, albums: Optional[list[dict[str, Any]]] = None) -> MagicMock:
    client = MagicMock(spec=LidarrClient)
    client.lookup_artist.return_value = [{"foreignArtistId": ART, "artistName": "Radiohead", "id": artist_id}]
    client.add_artist_with_defaults.return_value = {"id": 77}
    client.fetch_artist_albums.return_value = albums if albums is not None else []
    client.fetch_album.return_value = {"monitored": True}  # the verify-after-monitor read
    return client


def test_lidarr_album_adds_artist_unmonitored_then_monitors_and_searches_album(db, lidarr_mode):
    client = lidarr_client(albums=[
        {"id": 5, "title": "The Bends", "foreignAlbumId": RG2, "monitored": False},
        {"id": 6, "title": "OK Computer", "foreignAlbumId": RG, "monitored": False},
    ])
    res = apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=client)
    assert (res.status, res.applied_level) == ("applied", "album")
    client.lookup_artist.assert_called_once_with(f"lidarr:{ART}")
    client.add_artist_with_defaults.assert_called_once()
    assert client.add_artist_with_defaults.call_args.kwargs["whole_artist"] is False  # unmonitored add
    client.set_albums_monitored.assert_called_once_with([6], True)
    client.ensure_artist_monitored.assert_called_once_with(77)  # Lidarr ignores monitored albums of an unmonitored artist
    client.run_command.assert_called_once_with("AlbumSearch", albumIds=[6])
    assert db.list_library_artists() == []  # nothing native in lidarr mode


def test_lidarr_album_respects_auto_search_off_and_skips_already_monitored(db, lidarr_mode):
    db.update_lidarr_settings({"auto_search": False})
    client = lidarr_client(artist_id=9, albums=[{"id": 6, "title": "OK Computer", "foreignAlbumId": RG, "monitored": False}])
    apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=client)
    client.add_artist_with_defaults.assert_not_called()  # artist already in Lidarr
    client.set_albums_monitored.assert_called_once_with([6], True)
    client.run_command.assert_not_called()

    monitored = lidarr_client(artist_id=9, albums=[{"id": 6, "title": "OK Computer", "foreignAlbumId": RG, "monitored": True}])
    apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=monitored)
    monitored.set_albums_monitored.assert_not_called()


def test_lidarr_album_matches_by_title_when_foreign_id_differs(db, lidarr_mode):
    client = lidarr_client(artist_id=9, albums=[{"id": 8, "title": "ok  computer", "foreignAlbumId": "other", "monitored": False}])
    assert apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=client).status == "applied"
    client.set_albums_monitored.assert_called_once_with([8], True)


def test_lidarr_album_not_loaded_yet_stays_pending_and_missing_album_unresolved(db, lidarr_mode):
    client = lidarr_client()  # no albums ever appear
    res = apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=client)
    assert res.status == "pending" and "not loaded" in res.error
    client.set_albums_monitored.assert_not_called()

    other = lidarr_client(artist_id=9, albums=[{"id": 1, "title": "Other", "foreignAlbumId": "x", "monitored": False}])
    assert apply_list_item(db, None, album_item(), "album", enricher=make_enricher(), lidarr_client=other).status == "unresolved"


@pytest.mark.parametrize("option", ["all", "existing", "future", "none", "albums", "singles_eps"])
@pytest.mark.parametrize("auto_search", [True, False])
def test_lidarr_artist_ignores_list_monitor_option_and_uses_root_folder_defaults(db, lidarr_mode, option, auto_search):
    """In Lidarr mode the list's artist_monitor_option is ignored: the add carries Lidarr's own defaults."""
    db.update_lidarr_settings({"auto_search": auto_search})
    client = lidarr_client()
    with patch("plex_playlist_sync.list_monitoring.lidarr_library.apply_monitor_preset") as preset:
        res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option=option, enricher=make_enricher(), lidarr_client=client)
    assert (res.status, res.applied_level) == ("applied", "artist")
    client.add_artist_with_defaults.assert_called_once()
    assert client.add_artist_with_defaults.call_args.kwargs == {"whole_artist": True, "search": auto_search}
    preset.assert_not_called()
    client.run_command.assert_not_called()  # the search rides on the add (searchForMissingAlbums)


def test_lidarr_artist_existing_is_untouched_and_errors_are_recorded(db, lidarr_mode):
    existing = lidarr_client(artist_id=3)
    assert apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="all", enricher=make_enricher(), lidarr_client=existing).status == "applied"
    existing.add_artist_with_defaults.assert_not_called()
    existing.run_command.assert_not_called()

    broken = lidarr_client()
    broken.add_artist_with_defaults.side_effect = LidarrApiError("Lidarr returned HTTP 500 for artist")
    res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="all", enricher=make_enricher(), lidarr_client=broken)
    assert res.status == "failed" and "HTTP 500" in res.error

    nothing = lidarr_client()
    nothing.lookup_artist.return_value = []
    assert apply_list_item(db, None, artist_item(), "artist", enricher=make_enricher(), lidarr_client=nothing).status == "unresolved"


def test_lidarr_unconfigured_is_pending(db, lidarr_mode):
    with patch("plex_playlist_sync.list_monitoring.build_lidarr_client", return_value=None):
        res = apply_list_item(db, None, artist_item(), "artist", enricher=make_enricher())
    assert res.status == "pending" and "not configured" in res.error


# ------------------------------------------------------------------ playlists


def _playlist(db: Database, mode: str, missing: Optional[list[dict[str, str]]] = None) -> None:
    db.upsert_playlist("pl-1", "Mix", creator_id="admin-1")
    db.set_playlist_monitor_mode("pl-1", mode)
    db.record_sync_result("pl-1", "success", missing or [{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}])


def test_playlist_monitor_mode_column_default_and_validation(db):
    db.upsert_playlist("pl-1", "Mix")
    assert db.get_playlist("pl-1")["monitor_mode"] == "track"
    assert db.list_playlists()[0]["monitor_mode"] == "track"
    assert db.list_playlists(user_id="u", enabled_only=True) == []
    assert db.set_playlist_monitor_mode("pl-1", "album") is True
    assert db.get_playlist("pl-1")["monitor_mode"] == "album"
    with pytest.raises(ValueError):
        db.set_playlist_monitor_mode("pl-1", "everything")


def test_playlist_album_mode_monitors_album_stamps_track_once_and_survives_resync(db):
    _playlist(db, "album")
    enr = make_enricher()
    counts = apply_playlist_missing(db, None, "pl-1", enricher=enr)
    assert counts["applied"] == 1
    artist = db.get_library_artist_by_mbid(ART)
    assert albums_of(db, artist["id"]) == {"OK Computer": True}
    assert db.get_missing_tracks("pl-1")[0]["list_applied_at"]

    # The next sync re-records the same missing track: the applied marker survives and nothing is re-applied.
    db.record_sync_result("pl-1", "success", [{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}])
    assert db.get_missing_tracks("pl-1")[0]["list_applied_at"]
    enr2 = make_enricher()
    assert apply_playlist_missing(db, None, "pl-1", enricher=enr2)["applied"] == 0
    enr2.lookup_track_mbids.assert_not_called()


def test_playlist_artist_mode_applies_artist_level_with_creator_attribution(db):
    _playlist(db, "artist")
    with patch("plex_playlist_sync.list_monitoring.apply_list_item") as apply:
        apply.return_value = MagicMock(status="applied")
        apply_playlist_missing(db, None, "pl-1")
    assert apply.call_args.args[3] == "artist"
    assert apply.call_args.kwargs["requested_by"]["id"] == "admin-1"
    assert apply.call_args.args[2].kind == "track"


@pytest.mark.parametrize("mode", ["track", "none"])
def test_playlist_track_and_none_modes_apply_nothing(db, mode):
    _playlist(db, mode)
    enr = make_enricher()
    assert apply_playlist_missing(db, None, "pl-1", enricher=enr) == {"applied": 0, "unresolved": 0, "pending": 0, "failed": 0}
    enr.lookup_track_mbids.assert_not_called()
    assert db.get_missing_tracks("pl-1")[0]["list_applied_at"] is None


def test_playlist_unresolved_track_is_not_stamped_and_retried(db):
    _playlist(db, "album")
    enr = make_enricher()
    enr.lookup_track_mbids.return_value = None
    enr.lookup_album_mbids.return_value = None
    assert apply_playlist_missing(db, None, "pl-1", enricher=enr)["unresolved"] == 1
    assert db.get_missing_tracks("pl-1")[0]["list_applied_at"] is None
    assert apply_playlist_missing(db, None, "pl-1", enricher=make_enricher())["applied"] == 1


def _backlog_calls(db: Database) -> list[Any]:
    worker = WantedBacklogWorker()
    worker.pace_delay = 0.0
    with patch("plex_playlist_sync.backlog_worker.acquisition_coordinator.search_and_grab", return_value={"success": False}) as grab:
        worker.poll_once(db=db)
    return [c.kwargs for c in grab.call_args_list]


def test_backlog_searches_missing_track_in_track_mode(db):
    _playlist(db, "track")
    assert [c["title"] for c in _backlog_calls(db)] == ["Airbag"]


def test_backlog_skips_missing_tracks_of_none_mode_playlist(db):
    _playlist(db, "none")
    assert _backlog_calls(db) == []


def test_backlog_skips_applied_tracks_of_album_mode_but_searches_unapplied(db):
    _playlist(db, "album", [
        {"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"},
        {"title": "Lucky", "artist": "Radiohead", "album": "OK Computer"},
    ])
    airbag = next(t for t in db.get_missing_tracks("pl-1") if t["title"] == "Airbag")
    db.mark_missing_tracks_list_applied([airbag["id"]])
    assert [c["title"] for c in _backlog_calls(db)] == ["Lucky"]  # unresolved one still falls back to a lone search


def test_backlog_end_to_end_album_mode_searches_the_monitored_catalog_track_not_a_lone_track(db):
    _playlist(db, "album")
    apply_playlist_missing(db, None, "pl-1", enricher=make_enricher())
    calls = _backlog_calls(db)
    assert calls and all(c.get("track_id") for c in calls)  # catalog tracks only, never the playlist's lone track
    assert sorted(c["title"] for c in calls) == ["Airbag", "Paranoid Android"]


# ------------------------------------------------------------------ import list sync


def _make_list(db: Database, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = dict(
        name="Loved", provider="lastfm", config={"username": "u", "api_key": "k", "source": "loved_tracks"},
        monitor_mode="album", artist_monitor_option=None, quality_profile_id=None, sync_interval_minutes=60,
    )
    data.update(kw)
    return db.create_import_list(data)


def _fetched(*items: ImportListItem):
    return patch("plex_playlist_sync.import_list_worker.fetch_items", return_value=list(items))


def test_sync_applies_new_items_once_and_never_reapplies(db):
    lst = _make_list(db)
    it = ImportListItem(kind="album", external_key=RG, artist_name="Radiohead", album_title="OK Computer", mbid=RG, artist_mbid=ART)
    enr = make_enricher()
    with _fetched(it):
        summary = sync_import_list(db, lst["id"], None, enricher=enr)
    assert summary["status"] == "ok" and summary["new_items"] == 1 and summary["applied"] == 1
    row = db.list_import_list_items(lst["id"])[0][0]
    assert (row["status"], row["applied_level"], row["mbid"]) == ("applied", "album", RG)
    refreshed = db.get_import_list(lst["id"])
    assert refreshed["last_status"] == "ok" and refreshed["last_synced_at"] and refreshed["last_error"] is None

    # The user unmonitors the album; the next sync sees the same item but leaves their choice alone.
    album = db.get_library_album_by_release_group_id(RG)
    db.conn.execute("UPDATE library_albums SET monitored = 0 WHERE id = ?", (album["id"],))
    db.conn.commit()
    enr2 = make_enricher()
    with _fetched(it):
        summary = sync_import_list(db, lst["id"], None, enricher=enr2)
    assert summary["new_items"] == 0 and "applied" not in summary
    assert db.get_library_album(album["id"])["monitored"] is False
    enr2.lookup_album_mbids.assert_not_called()
    assert db.import_list_item_counts(lst["id"])["applied"] == 1


def test_sync_records_per_item_failures_without_aborting(db):
    lst = _make_list(db, monitor_mode="artist")
    good = ImportListItem(kind="artist", external_key=ART, artist_name="Radiohead", mbid=ART, artist_mbid=ART)
    unresolvable = ImportListItem(kind="artist", external_key="nobody", artist_name="Nobody")
    crashing = ImportListItem(kind="artist", external_key="boom", artist_name="Boom")
    enr = make_enricher()

    def lookup(name: str):
        if name == "Nobody":
            return None
        if name == "Boom":
            raise KeyError("unexpected")
        return ART

    enr.lookup_artist_mbid.side_effect = lookup
    with _fetched(crashing, unresolvable, good):
        summary = sync_import_list(db, lst["id"], None, enricher=enr)
    assert summary["status"] == "ok"
    counts = db.import_list_item_counts(lst["id"])
    assert (counts["applied"], counts["unresolved"], counts["failed"]) == (1, 1, 1)
    failed = db.list_import_list_items(lst["id"], status="failed")[0][0]
    assert failed["error"] == "KeyError"


def test_sync_none_mode_records_items_as_skipped(db):
    lst = _make_list(db, monitor_mode="none")
    with _fetched(ImportListItem(kind="track", external_key="a|b", artist_name="a", track_title="b")):
        sync_import_list(db, lst["id"], None, enricher=make_enricher())
    assert db.import_list_item_counts(lst["id"])["skipped"] == 1
    assert db.list_requests() == []


def test_sync_track_mode_creates_requests_for_track_items(db):
    lst = _make_list(db, monitor_mode="track")
    with _fetched(ImportListItem(kind="track", external_key="a|b", artist_name="Band", track_title="Song")):
        sync_import_list(db, lst["id"], None, enricher=make_enricher())
    reqs = db.list_requests()
    assert [(r["title"], r["artist"], r["user_id"]) for r in reqs] == [("Song", "Band", "admin-1")]


def test_sync_fetch_error_marks_list_error_and_keeps_items(db):
    lst = _make_list(db)
    with patch("plex_playlist_sync.import_list_worker.fetch_items", side_effect=ImportListError("provider returned HTTP 500")):
        summary = sync_import_list(db, lst["id"], None)
    assert summary == {"status": "error", "error": "provider returned HTTP 500"}
    after = db.get_import_list(lst["id"])
    assert (after["last_status"], after["last_error"]) == ("error", "provider returned HTTP 500")


def test_pending_item_is_retried_on_next_sync(db):
    db.update_media_management_settings({"library_mode": "lidarr"})
    lst = _make_list(db, monitor_mode="artist")
    it = ImportListItem(kind="artist", external_key=ART, artist_name="Radiohead", mbid=ART, artist_mbid=ART)
    with _fetched(it), patch("plex_playlist_sync.list_monitoring.build_lidarr_client", return_value=None):
        sync_import_list(db, lst["id"], None, enricher=make_enricher())
    assert db.import_list_item_counts(lst["id"])["pending"] == 1
    client = lidarr_client()
    with _fetched(it):
        sync_import_list(db, lst["id"], None, enricher=make_enricher(), lidarr_client=client)
    assert db.import_list_item_counts(lst["id"])["applied"] == 1


def test_concurrent_sync_is_refused(db):
    lst = _make_list(db)
    assert claim_sync(lst["id"])
    try:
        with pytest.raises(ImportListBusy):
            sync_import_list(db, lst["id"])
    finally:
        release_sync(lst["id"])


def test_worker_syncs_only_due_enabled_lists(db):
    due = _make_list(db, name="Due")
    fresh = _make_list(db, name="Fresh")
    off = _make_list(db, name="Off", enabled=False)
    db.set_import_list_sync_result(fresh["id"], "ok")
    assert [l["id"] for l in db.list_due_import_lists()] == [due["id"]]
    db.conn.execute("UPDATE import_lists SET last_synced_at = datetime('now', '-2 hours') WHERE id = ?", (fresh["id"],))
    db.conn.commit()
    assert {l["id"] for l in db.list_due_import_lists()} == {due["id"], fresh["id"]}
    assert off["id"] not in {l["id"] for l in db.list_due_import_lists()}

    worker = ImportListWorker()
    with patch("plex_playlist_sync.import_list_worker.sync_import_list", return_value={"status": "ok"}) as sync:
        assert worker.run_due(db, None) == 2
    assert {c.args[1] for c in sync.call_args_list} == {due["id"], fresh["id"]}
    assert worker.lists_synced == 2


def test_worker_survives_a_crashing_list_and_records_it(db):
    a, b = _make_list(db, name="A"), _make_list(db, name="B")
    worker = ImportListWorker()
    calls: list[str] = []

    def fake(_db, list_id, *args, **kw):
        calls.append(list_id)
        if list_id == a["id"]:
            raise RuntimeError("boom")
        return {"status": "ok"}

    with patch("plex_playlist_sync.import_list_worker.sync_import_list", side_effect=fake):
        worker.run_due(db, None)
    assert sorted(calls) == sorted([a["id"], b["id"]])
    assert db.get_import_list(a["id"])["last_status"] == "error"
    assert worker.errors == 1


# ------------------------------------------------------------------ acquisition-created artists


def _import_one(db: Database, tmp_path: Path, artist: str = "Pink Floyd") -> None:
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    downloads.mkdir()
    music.mkdir()
    db.update_media_management_settings(
        {"root_folder_path": str(music), "library_mode": "native", "staging_folder_path": str(downloads)}
    )
    db.create_download_client(DownloadClientConfig(id="c1", name="Qbit", driver_type=DownloadDriverType.QBITTORRENT, host_url="http://q:8080"))
    folder = downloads / "dl"
    folder.mkdir()
    audio = folder / "04_Time.flac"
    write_flac(audio)
    db.create_active_download(
        ActiveDownload(
            id="dl-1", client_id="c1", title="Pink Floyd - Time [FLAC]", artist=artist, item_type="track",
            status=DownloadStatus.COMPLETED.value, download_hash="h1", size_bytes=audio.stat().st_size,
        )
    )
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 26, "source_path": str(audio)}
    meta = {
        "artist": artist, "title": "Time", "album": "The Dark Side of the Moon", "year": 1973, "codec": "FLAC",
        "bitrate": 1411, "sample_rate": 96000, "bits_per_sample": 24, "duration": 425.0, "track_number": 4,
        "disc_number": 1, "total_discs": 1, "total_tracks": 10, "file_path": str(audio),
    }
    worker = AcquisitionWorker()
    worker.staging_dir = str(downloads)
    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), patch(
        "plex_playlist_sync.acquisition_worker.inspect_audio_file", return_value=meta
    ):
        assert worker.poll_once(db=db, staging_dir=str(downloads))["imported"] == 1


def test_acquisition_created_artist_gets_scan_monitor_option(db, tmp_path):
    db.update_media_management_settings({"scan_monitor_option": "future"})
    _import_one(db, tmp_path)
    artist = db.get_library_artist_by_name("Pink Floyd")
    assert (artist["monitored"], artist["monitor_option"]) == (True, "future")


def test_acquisition_created_artist_defaults_to_existing_not_all(db, tmp_path):
    _import_one(db, tmp_path)
    assert db.get_library_artist_by_name("Pink Floyd")["monitor_option"] == "existing"


def test_acquisition_leaves_existing_artist_untouched(db, tmp_path):
    db.update_media_management_settings({"scan_monitor_option": "none"})
    db.upsert_library_artist(LibraryArtist(id="pf", name="Pink Floyd", monitored=True, monitor_option="all"))
    _import_one(db, tmp_path)
    artist = db.get_library_artist("pf")
    assert (artist["monitored"], artist["monitor_option"]) == (True, "all")


# ------------------------------------------------------------------ review fixes


def test_playlist_of_non_admin_creator_treats_album_and_artist_as_track(db):
    """Defense in depth: a creator who is (or became) a non-admin gets no album/artist apply."""
    for mode in ("album", "artist"):
        db.upsert_playlist("pl-x", "Mix", creator_id="user-alice")
        db.set_playlist_monitor_mode("pl-x", mode)
        db.record_sync_result("pl-x", "success", [{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}])
        enr = make_enricher()
        with patch("plex_playlist_sync.list_monitoring.apply_list_item") as apply:
            counts = apply_playlist_missing(db, None, "pl-x", enricher=enr)
        assert counts == {"applied": 0, "unresolved": 0, "pending": 0, "failed": 0}
        apply.assert_not_called()
        assert db.get_missing_tracks("pl-x")[0]["list_applied_at"] is None
    assert db.list_library_artists() == []


def test_playlist_of_demoted_admin_stops_applying(db):
    db.upsert_user("admin-2", "boss", "b@example.com", is_admin=True)
    db.upsert_playlist("pl-x", "Mix", creator_id="admin-2")
    db.set_playlist_monitor_mode("pl-x", "album")
    db.record_sync_result("pl-x", "success", [{"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"}])
    db.upsert_user("admin-2", "boss", "b@example.com", is_admin=False)
    assert db.get_user("admin-2")["is_admin"] is False
    assert apply_playlist_missing(db, None, "pl-x", enricher=make_enricher())["applied"] == 0
    assert db.list_library_artists() == []


def test_playlist_stamps_each_track_immediately_and_survives_unexpected_error(db):
    _playlist(db, "album", [
        {"title": "One", "artist": "Radiohead", "album": "OK Computer"},
        {"title": "Two", "artist": "Radiohead", "album": "OK Computer"},
        {"title": "Three", "artist": "Radiohead", "album": "OK Computer"},
    ])
    seen: list[str] = []

    def fake_apply(db_, cfg, item, mode, **kw):
        seen.append(item.track_title)
        if item.track_title == "Two":
            raise KeyError("boom")
        return MagicMock(status="applied")

    with patch("plex_playlist_sync.list_monitoring.apply_list_item", side_effect=fake_apply):
        counts = apply_playlist_missing(db, None, "pl-1")
    assert seen == ["One", "Two", "Three"]
    assert counts["applied"] == 2 and counts["failed"] == 1
    stamped = {t["title"]: bool(t["list_applied_at"]) for t in db.get_missing_tracks("pl-1")}
    assert stamped == {"One": True, "Two": False, "Three": True}


def test_list_actor_picks_oldest_enabled_admin_not_alphabetical(db):
    db.upsert_user("adm-a", "aaa", "a@x.com", is_admin=True)   # alphabetically first, but created last
    db.upsert_user("adm-z", "zzz", "z@x.com", is_admin=True)   # created earliest
    db.upsert_user("adm-off", "bbb", "o@x.com", is_admin=True)  # oldest of all, but disabled
    for uid, ts in (("admin-1", "2024-03-01 00:00:00"), ("adm-a", "2024-04-01 00:00:00"),
                    ("adm-z", "2024-02-01 00:00:00"), ("adm-off", "2024-01-01 00:00:00")):
        db.conn.execute("UPDATE users SET created_at = ? WHERE id = ?", (ts, uid))
    db.conn.execute("UPDATE users SET disabled = 1 WHERE id = 'adm-off'")
    db.conn.commit()
    from plex_playlist_sync.list_monitoring import list_actor

    assert list_actor(db)["id"] == "adm-z"
    # Equal created_at falls back to id.
    db.conn.execute("UPDATE users SET created_at = '2024-02-01 00:00:00' WHERE id IN ('admin-1', 'adm-a')")
    db.conn.commit()
    assert list_actor(db)["id"] == "adm-a"


def test_native_artist_retry_finishes_refresh_for_artist_this_item_added(db):
    item = artist_item()
    enr = make_enricher()
    added: list[bool] = []
    with patch("plex_playlist_sync.artist_refresh.refresh_single_artist", return_value={"success": False, "message": "mb down"}):
        res = apply_list_item(db, None, item, "artist", enricher=enr, on_artist_added=lambda: added.append(True))
    assert res.status == "pending" and added == [True]
    assert db.get_library_artist_by_mbid(ART) is not None  # artist exists, refresh did not complete

    # Retry without the persisted flag would return early; with it the refresh is done.
    with patch("plex_playlist_sync.artist_refresh.refresh_single_artist", return_value={"success": True}) as refresh:
        res = apply_list_item(db, None, item, "artist", enricher=enr, artist_added=True)
    assert res.status == "applied"
    refresh.assert_called_once()


def test_native_pre_existing_artist_is_never_refreshed_even_on_retry_without_flag(db):
    db.upsert_library_artist(LibraryArtist(id="a1", name="Radiohead", clean_name="radiohead", path="/music/R", monitored=True, mbid=ART), preserve_monitoring=True)
    with patch("plex_playlist_sync.artist_refresh.refresh_single_artist") as refresh:
        res = apply_list_item(db, None, artist_item(), "artist", enricher=make_enricher())
    assert res.status == "applied"
    refresh.assert_not_called()


def test_lidarr_artist_retry_after_item_added_artist_finds_it_and_changes_nothing(db, lidarr_mode):
    client = lidarr_client()
    flagged: list[bool] = []
    res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="albums",
                          enricher=make_enricher(), lidarr_client=client, on_artist_added=lambda: flagged.append(True))
    assert res.status == "applied" and flagged == [True]

    # Retry: Lidarr now reports the artist as existing (id 77): nothing is added or edited again.
    retry_client = lidarr_client(artist_id=77)
    res = apply_list_item(db, None, artist_item(), "artist", enricher=make_enricher(), lidarr_client=retry_client, artist_added=True)
    assert res.status == "applied"
    retry_client.add_artist_with_defaults.assert_not_called()
    retry_client.run_command.assert_not_called()


def test_lidarr_pre_existing_artist_untouched(db, lidarr_mode):
    client = lidarr_client(artist_id=9)
    with patch("plex_playlist_sync.list_monitoring.lidarr_library.apply_monitor_preset") as preset:
        res = apply_list_item(db, None, artist_item(), "artist", artist_monitor_option="albums",
                              enricher=make_enricher(), lidarr_client=client)
    assert res.status == "applied"
    preset.assert_not_called()
    client.add_artist_with_defaults.assert_not_called()
    client.set_artist_monitored.assert_not_called()
    client.bulk_edit_artists.assert_not_called()
    client.run_command.assert_not_called()
