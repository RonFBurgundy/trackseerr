"""Lidarr mode follows Lidarr: root-folder defaults are the only source of profiles, tags and monitoring, an artist
already in Lidarr is never modified, and a song/album request monitors exactly the release it needs."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.acquisition.lidarr_adapter import LidarrAdapter
from plex_playlist_sync.clients import lidarr as lidarr_mod
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient, invalidate_add_defaults
from plex_playlist_sync.config import Config
from plex_playlist_sync.lidarr_queue import LidarrTrickleWorker
from plex_playlist_sync.lidarr_release import (
    albums_containing_song,
    match_named_album,
    norm_title,
    select_release_for_song,
    titles_match,
)
from plex_playlist_sync.models import AcquisitionSearchResult, MusicRequest, RequestStatus
from plex_playlist_sync.storage import Database
from tests.lidarr_fake import FakeLidarr, FastClock

API_KEY = "lidarr-secret-key-abcdef123456"
HTTPX = "plex_playlist_sync.clients.lidarr.httpx.Client"


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def admin_client(test_db, test_config):
    test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    secret = get_or_create_secret_key(data_dir=test_config.data_dir)
    token = create_session_token(user_id="admin-1", username="admin_user", is_admin=True, secret_key=secret)
    test_db.create_session(token, "admin-1", {"auth": "test"})
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def client_for(root_folder="/music") -> LidarrClient:
    return LidarrClient("http://lidarr.test:8686", API_KEY, root_folder=root_folder)


def album(album_id, title, kind="Album", released="2000-01-01", monitored=False):
    return {"id": album_id, "title": title, "albumType": kind, "releaseDate": released, "monitored": monitored}


def track(track_id, album_id, title):
    return {"id": track_id, "albumId": album_id, "title": title}


# ------------------------------------------------------------------------------- root-folder defaults


class TestRootFolderDefaults:
    def test_uses_the_root_folder_matching_the_setting(self):
        fake = FakeLidarr()
        fake.root_folders = [
            {"path": "/first", "defaultQualityProfileId": 1, "defaultMetadataProfileId": 1},
            {**fake.root_folders[0], "path": "/music/"},
        ]
        with patch(HTTPX, fake):
            d = client_for("/music").get_root_folder_defaults()
        assert (d.root_folder_path, d.quality_profile_id, d.metadata_profile_id) == ("/music/", 4, 6)
        assert (d.monitor, d.new_item_monitor, d.tag_ids, d.source) == ("future", "new", [7], "rootfolder")

    @pytest.mark.parametrize("setting", [None, "/gone"])
    def test_first_root_folder_when_setting_is_absent_or_unknown(self, setting):
        fake = FakeLidarr()
        fake.root_folders = [
            {**fake.root_folders[0], "path": "/first", "defaultQualityProfileId": 2},
            {**fake.root_folders[0], "path": "/second", "defaultQualityProfileId": 3},
        ]
        with patch(HTTPX, fake):
            d = client_for(setting).get_root_folder_defaults()
        assert (d.root_folder_path, d.quality_profile_id) == ("/first", 2)

    def test_missing_default_fields_fall_back_to_first_profiles(self):
        fake = FakeLidarr()
        fake.root_folders = [{"path": "/music", "freeSpace": 5}]  # an older Lidarr: no default* fields
        with patch(HTTPX, fake):
            d = client_for().get_root_folder_defaults()
        assert (d.quality_profile_id, d.metadata_profile_id) == (1, 1)
        assert (d.monitor, d.new_item_monitor, d.tag_ids, d.source) == ("all", "all", [], "fallback")

    def test_no_root_folders_at_all_still_resolves_with_fallback(self):
        fake = FakeLidarr()
        fake.root_folders = []
        with patch(HTTPX, fake):
            d = client_for("/music").get_root_folder_defaults()
        assert d.root_folder_path == "/music" and d.source == "fallback"

    def test_fallback_without_any_profile_is_an_error(self):
        fake = FakeLidarr()
        fake.root_folders = [{"path": "/music"}]
        fake.quality_profiles = []
        with patch(HTTPX, fake), pytest.raises(LidarrApiError):
            client_for().get_root_folder_defaults()

    def test_cached_for_five_minutes_then_refetched_and_invalidation_drops_it(self):
        fake = FakeLidarr()
        clock = [1000.0]
        with patch(HTTPX, fake), patch.object(lidarr_mod, "_monotonic", lambda: clock[0]):
            client_for().get_root_folder_defaults()
            client_for().get_root_folder_defaults()
            assert len(fake.requests("GET", "rootfolder")) == 1
            clock[0] += 299
            client_for().get_root_folder_defaults()
            assert len(fake.requests("GET", "rootfolder")) == 1
            clock[0] += 2
            client_for().get_root_folder_defaults()
            assert len(fake.requests("GET", "rootfolder")) == 2
            invalidate_add_defaults()
            client_for().get_root_folder_defaults()
            assert len(fake.requests("GET", "rootfolder")) == 3

    def test_cached_tag_list_is_not_shared_between_callers(self):
        fake = FakeLidarr()
        with patch(HTTPX, fake):
            first = client_for().get_root_folder_defaults()
            first.tag_ids.append(999)
            assert client_for().get_root_folder_defaults().tag_ids == [7]


# ------------------------------------------------------------------------------- adding artists


class TestArtistAdds:
    def test_whole_artist_add_carries_every_root_folder_default(self):
        fake = FakeLidarr()
        with patch(HTTPX, fake):
            created = client_for().add_artist_with_defaults(
                {"artistName": "Queen", "foreignArtistId": "mb-queen", "id": 0}, whole_artist=True, search=True
            )
        assert created["id"] == FakeLidarr.NEW_ARTIST_ID
        sent = fake.requests("POST", "artist")[0]
        assert sent["rootFolderPath"] == "/music"
        assert (sent["qualityProfileId"], sent["metadataProfileId"], sent["tags"]) == (4, 6, [7])
        assert sent["monitored"] is True and sent["monitorNewItems"] == "new"
        assert sent["addOptions"] == {"monitor": "future", "searchForMissingAlbums": True}

    def test_whole_artist_add_without_auto_search_does_not_search(self):
        fake = FakeLidarr()
        with patch(HTTPX, fake):
            client_for().add_artist_with_defaults({"artistName": "Queen", "id": 0}, whole_artist=True, search=False)
        assert fake.requests("POST", "artist")[0]["addOptions"]["searchForMissingAlbums"] is False

    def test_the_removed_overrides_are_no_longer_constructor_arguments(self):
        for name in ("quality_profile_id", "metadata_profile_id", "monitor_option", "tag_ids"):
            with pytest.raises(TypeError):
                LidarrClient("http://lidarr.test", API_KEY, **{name: 1})

    def test_new_artist_song_request_adds_unmonitored_then_monitors_exactly_one_album(self):
        fake = FakeLidarr()
        fake.albums = [
            album(1, "Greatest Hits", "Album", "1981-10-26"),
            album(2, "A Night at the Opera", "Album", "1975-11-21"),
            album(3, "Jazz", "Album", "1978-11-10"),
        ]
        fake.tracks = [track(10, 1, "Bohemian Rhapsody"), track(11, 2, "Bohemian Rhapsody"), track(12, 3, "Fat Bottomed Girls")]
        fake.empty_album_polls = 2
        with patch(HTTPX, fake), patch.object(lidarr_mod.time, "sleep") as sleep:
            res = client_for().add_artist_and_albums(
                "Queen", auto_search=True, wants=[{"album": "", "title": "Bohemian Rhapsody", "item_type": "track"}]
            )
        assert res["status"] == "success" and res["added"] is True and res["matched_album_ids"] == [2]
        sent = fake.requests("POST", "artist")[0]
        assert sent["addOptions"] == {"monitor": "none", "searchForMissingAlbums": False}
        assert (sent["qualityProfileId"], sent["metadataProfileId"], sent["tags"], sent["monitorNewItems"]) == (4, 6, [7], "new")
        assert sent["rootFolderPath"] == "/music" and sent["monitored"] is True
        assert sleep.call_count == 2  # waited for the albums to load
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
        assert fake.requests("POST", "command") == [{"name": "AlbumSearch", "albumIds": [2]}]
        assert [a["monitored"] for a in fake.albums] == [False, True, False]  # exactly one
        assert "PUT" not in {m for m, p, _ in fake.calls if p.startswith("artist")}

    def test_search_is_skipped_when_auto_search_is_off(self):
        fake = FakeLidarr()
        fake.albums = [album(2, "A Night at the Opera")]
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["A Night at the Opera"], auto_search=False)
        assert res["status"] == "success" and res["searched"] is False
        assert fake.requests("POST", "command") == []

    def test_artist_already_in_lidarr_is_never_modified(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen", "monitored": False, "qualityProfileId": 1, "tags": [2]}]
        fake.albums = [album(2, "A Night at the Opera"), album(3, "Jazz")]
        fake.tracks = [track(11, 2, "Bohemian Rhapsody")]
        with patch(HTTPX, fake):
            res = client_for().search_and_add_track("Queen", title="Bohemian Rhapsody", auto_search=True)
        assert res["status"] == "already_monitored"
        assert fake.requests("POST", "artist") == [] and fake.requests("PUT", "artist") == []
        assert not [p for m, p, _ in fake.calls if m in ("POST", "PUT", "DELETE") and p.startswith("artist")]
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
        assert fake.requests("GET", "rootfolder") == []  # nothing to add, so no defaults were even needed

    def test_already_monitored_album_is_not_put_again_but_is_still_searched(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Jazz", monitored=True)]
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["Jazz"], auto_search=True)
        assert res["status"] == "success"
        assert fake.requests("PUT", "album/monitor") == []
        assert fake.requests("POST", "command") == [{"name": "AlbumSearch", "albumIds": [2]}]

    def test_album_request_monitors_that_album_and_searches_it(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "A Night at the Opera"), album(3, "Jazz")]
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums(
                "Queen", auto_search=True, wants=[{"album": "jazz", "title": "", "item_type": "album"}]
            )
        assert res["matched_album_ids"] == [3]
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [3], "monitored": True}]

    def test_track_lookup_falls_back_to_per_album_when_artist_level_list_is_empty(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "A Night at the Opera"), album(3, "Jazz")]
        fake.tracks = [track(12, 3, "Fat Bottomed Girls")]
        fake.track_by_artist_supported = False
        with patch(HTTPX, fake):
            res = client_for().search_and_add_track("Queen", title="Fat Bottomed Girls", auto_search=False)
        assert res["status"] == "already_monitored"
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [3], "monitored": True}]

    def test_rate_limit_on_lookup_is_reported_with_retry_after(self):
        fake = FakeLidarr()
        fake.fail[("GET", "artist/lookup")] = 429
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["Jazz"])
        assert res["status"] == "rate_limited" and res["retry_after"] == 7

    def test_5xx_on_artist_add_is_a_retryable_rate_limit_not_a_crash(self):
        fake = FakeLidarr()
        fake.fail[("POST", "artist")] = 503
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["Jazz"])
        assert res["status"] == "rate_limited"

    def test_artist_not_found(self):
        fake = FakeLidarr()
        fake.lookup = []
        with patch(HTTPX, fake):
            assert client_for().add_artist_and_albums("Nobody", ["x"])["status"] == "not_found"

    def test_albums_never_loading_for_a_new_artist_is_pending_not_failed_profile(self):
        fake = FakeLidarr()
        fake.empty_album_polls = 99
        with patch(HTTPX, fake), patch.object(lidarr_mod.time, "sleep"):
            res = client_for().add_artist_and_albums("Queen", wants=[{"album": "", "title": "X", "item_type": "track"}])
        assert res["status"] == "albums_pending" and fake.requests("PUT", "album/monitor") == []


# ------------------------------------------------------------------------------- release selection


class TestReleaseSelection:
    SONG = "Bohemian Rhapsody"

    def pick(self, albums, tracks):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = albums
        fake.tracks = tracks
        with patch(HTTPX, fake):
            res = client_for().search_and_add_track("Queen", title=self.SONG, auto_search=False)
        return res, fake

    def monitored_ids(self, fake):
        return [a["id"] for a in fake.albums if a["monitored"]]

    def test_single_with_matching_title_is_preferred_over_albums(self):
        albums = [
            album(1, "Greatest Hits", "Album", "1981-10-26"),
            album(2, "A Night at the Opera", "Album", "1975-11-21"),
            album(3, "Bohemian Rhapsody", "Single", "1975-10-31"),
            album(4, "Other Single", "Single", "1970-01-01"),
        ]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG), track(3, 3, self.SONG), track(4, 4, self.SONG)]
        res, fake = self.pick(albums, tracks)
        assert res["status"] == "already_monitored" and self.monitored_ids(fake) == [3]

    def test_earliest_album_when_no_single_matches(self):
        albums = [
            album(1, "Greatest Hits", "Album", "1981-10-26"),
            album(2, "A Night at the Opera", "Album", "1975-11-21"),
            album(3, "Queen Live", "EP", "1970-01-01"),
        ]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG), track(3, 3, self.SONG)]
        _, fake = self.pick(albums, tracks)
        assert self.monitored_ids(fake) == [2]

    def test_ep_when_no_album_carries_the_song(self):
        albums = [album(1, "Live EP", "EP", "1990-01-01"), album(2, "Compilation", "Other", "1980-01-01")]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG)]
        _, fake = self.pick(albums, tracks)
        assert self.monitored_ids(fake) == [1]

    def test_any_other_candidate_as_last_resort(self):
        albums = [album(1, "Broadcast", "Broadcast", "1990-01-01"), album(2, "Compilation", "Other", "1980-01-01")]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG)]
        _, fake = self.pick(albums, tracks)
        assert self.monitored_ids(fake) == [2]

    def test_a_single_whose_title_differs_does_not_beat_the_album(self):
        albums = [album(1, "Another One Bites The Dust", "Single", "1970-01-01"), album(2, "Opera", "Album", "1975-11-21")]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG)]
        _, fake = self.pick(albums, tracks)
        assert self.monitored_ids(fake) == [2]

    def run_named(self, albums, tracks, album_name):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = albums
        fake.tracks = tracks
        with patch(HTTPX, fake):
            res = client_for().search_and_add_track("Queen", album_name=album_name, title=self.SONG, auto_search=False)
        return res, fake

    def test_a_matching_single_beats_the_named_album(self):
        albums = [album(2, "A Night at the Opera"), album(3, self.SONG, "Single", "1975-10-31")]
        _, fake = self.run_named(albums, [track(2, 2, self.SONG), track(3, 3, self.SONG)], "A Night at the Opera")
        assert self.monitored_ids(fake) == [3]

    def test_the_named_album_is_a_tie_breaker_among_albums_holding_the_song(self):
        albums = [
            album(1, "Greatest Hits", "Album", "1981-10-26"),
            album(2, "A Night at the Opera", "Album", "1975-11-21"),
            album(3, "Jazz", "Album", "1978-11-10"),
        ]
        tracks = [track(1, 1, self.SONG), track(2, 2, self.SONG), track(3, 3, self.SONG)]
        _, fake = self.run_named(albums, tracks, "Greatest Hits")
        assert self.monitored_ids(fake) == [1]  # not the earliest album, because the request named this one

    def test_a_named_album_without_the_song_is_never_monitored(self):
        albums = [album(1, "Jazz", "Album", "1978-11-10"), album(2, "A Night at the Opera", "Album", "1975-11-21")]
        tracks = [track(1, 1, "Fat Bottomed Girls"), track(2, 2, self.SONG)]
        res, fake = self.run_named(albums, tracks, "Jazz")
        assert res["status"] == "already_monitored"
        assert self.monitored_ids(fake) == [2]  # falls back to the album that does hold the song

    def test_a_named_album_without_the_song_and_no_other_holder_is_not_in_profile(self):
        albums = [album(1, "Jazz", "Album", "1978-11-10")]
        res, fake = self.run_named(albums, [track(1, 1, "Fat Bottomed Girls")], "Jazz")
        assert res["status"] == "not_in_metadata_profile"
        assert self.monitored_ids(fake) == []

    def test_album_wants_match_the_exact_normalised_title_only(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(1, "Jazz Deluxe Live", "Album"), album(2, "Jazz (Deluxe Edition)", "Album", "2001-01-01")]
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["Jazz"], auto_search=False)
        assert res["status"] == "success" and self.monitored_ids(fake) == [2]
        fake2 = FakeLidarr()
        fake2.lookup = [{"id": 5, "artistName": "Queen"}]
        fake2.albums = [album(1, "Jazz Live", "Album")]
        with patch(HTTPX, fake2):
            res2 = client_for().add_artist_and_albums("Queen", ["Jazz"], auto_search=False)
        assert res2["status"] == "not_in_metadata_profile" and self.monitored_ids(fake2) == []

    def test_title_matching_ignores_case_punctuation_and_remaster_suffix(self):
        albums = [album(1, "Opera", "Album")]
        tracks = [track(1, 1, "Bohemian Rhapsody (Remastered 2011)")]
        res, fake = self.pick(albums, tracks)
        assert res["status"] == "already_monitored" and self.monitored_ids(fake) == [1]

    def test_helpers(self):
        assert norm_title("  Don't  Stop! ") == "dont stop"
        assert not titles_match("Song (Live)", "song") and not titles_match("Song A", "Song B")
        assert match_named_album([album(1, "Jazz"), album(2, "Jazz Deluxe Live")], "jazz")["id"] == 1
        assert match_named_album([album(2, "Jazz Live")], "Jazz") is None  # no containment fallback
        assert match_named_album([album(1, "Jazz")], "Opera") is None
        assert select_release_for_song([], "x") is None
        found = albums_containing_song([track(1, 1, "A"), track(2, 9, "A")], [album(1, "One"), album(2, "Two")], "a")
        assert [a["id"] for a in found] == [1]


# ------------------------------------------------------------------------------- feedback loop


class TestFeedbackLoop:
    def test_song_not_in_any_lidarr_album_is_not_in_metadata_profile_and_nothing_is_monitored(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "A Night at the Opera")]
        fake.tracks = [track(1, 2, "Love of My Life")]
        with patch(HTTPX, fake):
            res = client_for().search_and_add_track("Queen", title="Some Deep Cut", auto_search=True)
        assert res["status"] == "not_in_metadata_profile"
        assert res["outcomes"][0]["message"] == "Not available with your Lidarr metadata profile"
        assert fake.requests("PUT", "album/monitor") == [] and fake.requests("POST", "command") == []
        assert not [p for m, p, _ in fake.calls if m in ("POST", "PUT") and p.startswith("artist")]
        assert not any(a["monitored"] for a in fake.albums)  # and the metadata profile is never touched
        assert not [p for m, p, _ in fake.calls if "metadataprofile" in p and m != "GET"]

    def test_unmonitored_after_the_put_is_recorded_as_failure_and_not_searched_as_success(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Jazz")]
        fake.monitor_sticks = False
        with patch(HTTPX, fake):
            res = client_for().add_artist_and_albums("Queen", ["Jazz"], auto_search=True)
        assert res["status"] == "monitor_failed"
        assert res["outcomes"][0]["album_id"] == 2
        assert fake.requests("GET", "album/2")  # the album was re-read to verify

    def test_the_album_is_reread_after_monitoring(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Jazz")]
        with patch(HTTPX, fake):
            client_for().add_artist_and_albums("Queen", ["Jazz"], auto_search=False)
        order = [(m, p) for m, p, _ in fake.calls if p.startswith("album/")]
        assert order == [("PUT", "album/monitor"), ("GET", "album/2")]

    def make_request(self, db, req_id="req-1", item_type="track", title="Some Deep Cut", album=None):
        db.upsert_user("user-a", "alice", "a@x.com", is_admin=False)
        db.create_request(
            MusicRequest(
                id=req_id, user_id="user-a", item_type=item_type, title=title, artist="Queen", album=album,
                status=RequestStatus.PROCESSING,
            )
        )

    def run_worker(self, db, fake, items):
        worker = LidarrTrickleWorker()
        worker._delay_seconds = 0.0
        worker._auto_search = True
        groups = LidarrTrickleWorker._group_by_artist(items)
        with patch(HTTPX, fake), patch("plex_playlist_sync.lidarr_queue.time", FastClock()):
            worker._process_groups(groups, client_for(), db)
        return worker

    def test_worker_records_not_in_metadata_profile_on_the_request(self, test_db):
        self.make_request(test_db)
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "A Night at the Opera")]
        fake.tracks = [track(1, 2, "Love of My Life")]
        item = {"id": "req-1", "artist": "Queen", "album": "", "title": "Some Deep Cut", "item_type": "track", "is_request": True}
        self.run_worker(test_db, fake, [item])
        req = test_db.get_request("req-1")
        assert req["status"] == "processing"
        assert req["status_reason"] == "not_in_metadata_profile"
        assert req["status_message"] == "Not available with your Lidarr metadata profile"
        assert fake.requests("PUT", "album/monitor") == []
        assert test_db.list_requests()[0]["status_message"] == "Not available with your Lidarr metadata profile"

    def test_worker_records_a_monitor_failure_and_success_clears_the_message(self, test_db):
        self.make_request(test_db, title="Jazz", item_type="album", album="Jazz")
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Jazz")]
        fake.monitor_sticks = False
        item = {"id": "req-1", "artist": "Queen", "album": "Jazz", "title": "Jazz", "item_type": "album", "is_request": True}
        self.run_worker(test_db, fake, [item])
        assert test_db.get_request("req-1")["status_reason"] == "monitor_failed"

        fake.monitor_sticks = True
        self.run_worker(test_db, fake, [item])
        req = test_db.get_request("req-1")
        assert (req["status"], req["status_reason"], req["status_message"]) == ("processing", None, None)

    def test_worker_handles_each_item_of_one_artist_separately(self, test_db):
        self.make_request(test_db, "req-1", title="Jazz", item_type="album", album="Jazz")
        self.make_request(test_db, "req-2", title="Some Deep Cut")
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Jazz")]
        fake.tracks = [track(1, 2, "Fat Bottomed Girls")]
        items = [
            {"id": "req-1", "artist": "Queen", "album": "Jazz", "title": "Jazz", "item_type": "album", "is_request": True},
            {"id": "req-2", "artist": "Queen", "album": "", "title": "Some Deep Cut", "item_type": "track", "is_request": True},
        ]
        worker = self.run_worker(test_db, fake, items)
        assert test_db.get_request("req-1")["status_reason"] is None
        assert test_db.get_request("req-2")["status_reason"] == "not_in_metadata_profile"
        assert (worker._successful_items, worker._failed_items) == (1, 1)
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]

    def test_status_change_clears_a_stale_outcome(self, test_db):
        self.make_request(test_db)
        test_db.set_request_outcome("req-1", "not_in_metadata_profile", "msg")
        assert test_db.get_request("req-1")["status_message"] == "msg"
        test_db.update_request_status("req-1", RequestStatus.PROCESSING)
        assert test_db.get_request("req-1")["status_message"] is None


# ------------------------------------------------------------------------------- adapter and call sites


class TestAdapterAndDispatch:
    def adapter_run(self, result, fake):
        adapter = LidarrAdapter("http://lidarr.test:8686", API_KEY, auto_search=True, root_folder="/music")
        with patch(HTTPX, fake), patch.object(lidarr_mod.time, "sleep"):
            return adapter.download(result)

    def test_track_without_album_no_longer_adds_the_artist_with_all(self):
        fake = FakeLidarr()
        fake.albums = [album(2, "A Night at the Opera", released="1975-11-21"), album(3, "Jazz", released="1978-11-10")]
        fake.tracks = [track(1, 2, "Bohemian Rhapsody")]
        result = AcquisitionSearchResult(
            download_id="x", title="Bohemian Rhapsody", artist="Queen", album=None, item_type="track",
            extra={"artist": "Queen", "album": None, "title": "Bohemian Rhapsody"},
        )
        assert self.adapter_run(result, fake) == "lidarr::Queen::Bohemian Rhapsody"
        sent = fake.requests("POST", "artist")[0]
        assert sent["addOptions"] == {"monitor": "none", "searchForMissingAlbums": False}
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
        assert not [c for c in fake.calls if c[0] == "POST" and c[1].startswith("command") and c[2]["name"] == "ArtistSearch"]

    def test_album_request_monitors_just_that_album(self):
        fake = FakeLidarr()
        fake.albums = [album(2, "A Night at the Opera"), album(3, "Jazz")]
        result = AcquisitionSearchResult(download_id="x", title="Jazz", artist="Queen", album="Jazz", item_type="album")
        self.adapter_run(result, fake)
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [3], "monitored": True}]

    def test_request_dispatch_items_carry_the_song_title_and_type(self, test_db):
        from plex_playlist_sync.request_submission import run_submission_followups, RequestSubmission

        test_db.upsert_user("user-a", "alice", "a@x.com", is_admin=False)
        created = test_db.create_request(
            MusicRequest(id="r1", user_id="user-a", item_type="track", title="Airbag", artist="Radiohead", album=None,
                         status=RequestStatus.PROCESSING)
        )
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        with patch("plex_playlist_sync.request_submission.dispatch_to_lidarr") as dispatch:
            run_submission_followups(
                test_db, {"id": "user-a", "username": "alice", "is_admin": False},
                RequestSubmission(request=created, status=RequestStatus.PROCESSING), source="test", config=None,
            )
        items = dispatch.call_args.args[2]
        assert items == [{"id": "r1", "artist": "Radiohead", "album": "", "title": "Airbag", "item_type": "track", "is_request": True}]


# ------------------------------------------------------------------------------- settings API


class TestDefaultsEndpoint:
    def configure(self, db):
        db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY, "root_folder": "/music"})

    def test_shape(self, admin_client, test_db):
        client, headers = admin_client
        self.configure(test_db)
        fake = FakeLidarr()
        fake.root_folders.append({"path": "/other"})
        with patch(HTTPX, fake):
            resp = client.get("/api/settings/lidarr/defaults", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {
            "root_folder": "/music",
            "quality_profile": {"id": 4, "name": "Lossless"},
            "metadata_profile": {"id": 6, "name": "Everything"},
            "monitor": "future",
            "new_item_monitor": "new",
            "tags": [{"id": 7, "label": "family"}],
            "source": "rootfolder",
            "root_folders": ["/music", "/other"],
            "singles_enabled": True,
        }

    def test_fallback_source_is_reported(self, admin_client, test_db):
        client, headers = admin_client
        self.configure(test_db)
        fake = FakeLidarr()
        fake.root_folders = [{"path": "/music"}]
        with patch(HTTPX, fake):
            body = client.get("/api/settings/lidarr/defaults", headers=headers).json()
        assert body["source"] == "fallback" and body["monitor"] == "all" and body["tags"] == []

    def test_502_when_lidarr_is_unreachable(self, admin_client, test_db):
        client, headers = admin_client
        self.configure(test_db)
        fake = FakeLidarr()
        fake.fail[("GET", "rootfolder")] = 500
        with patch(HTTPX, fake):
            resp = client.get("/api/settings/lidarr/defaults", headers=headers)
        assert resp.status_code == 502 and API_KEY not in resp.text

    def test_unconfigured_is_422_and_non_admin_is_refused(self, admin_client, test_db):
        client, headers = admin_client
        assert client.get("/api/settings/lidarr/defaults", headers=headers).status_code == 422
        assert client.get("/api/settings/lidarr/defaults").status_code in (401, 403)

    def test_saving_lidarr_settings_invalidates_the_cached_defaults(self, admin_client, test_db):
        client, headers = admin_client
        self.configure(test_db)
        fake = FakeLidarr()
        with patch(HTTPX, fake):
            assert client.get("/api/settings/lidarr/defaults", headers=headers).json()["quality_profile"]["id"] == 4
            fake.root_folders[0]["defaultQualityProfileId"] = 1
            # still cached: Lidarr's change is not visible until the settings are saved (or five minutes pass)
            assert client.get("/api/settings/lidarr/defaults", headers=headers).json()["quality_profile"]["id"] == 4
            client.put("/api/settings/lidarr", json={"root_folder": "/music"}, headers=headers)
            body = client.get("/api/settings/lidarr/defaults", headers=headers).json()
        assert body["quality_profile"]["id"] == 1


class TestIngestInLidarrMode:
    def test_ingest_new_artist_adds_with_all_defaults_and_searches(self, admin_client, test_db):
        client, headers = admin_client
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY, "root_folder": "/music"})
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        fake = FakeLidarr()
        with patch(HTTPX, fake):
            resp = client.post(
                "/api/library/artists/ingest",
                json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Queen", "monitor_option": "none",
                      "quality_profile_id": "ignored"},
                headers=headers,
            )
        assert resp.status_code == 200 and resp.json()["already_existed"] is False
        sent = fake.requests("POST", "artist")[0]
        assert sent["addOptions"] == {"monitor": "future", "searchForMissingAlbums": True}
        assert (sent["qualityProfileId"], sent["metadataProfileId"], sent["tags"]) == (4, 6, [7])

    def test_ingest_existing_artist_is_left_alone(self, admin_client, test_db):
        client, headers = admin_client
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        with patch(HTTPX, fake):
            resp = client.post(
                "/api/library/artists/ingest",
                json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Queen"},
                headers=headers,
            )
        assert resp.status_code == 200 and resp.json()["already_existed"] is True
        assert not [c for c in fake.calls if c[0] in ("POST", "PUT")]
