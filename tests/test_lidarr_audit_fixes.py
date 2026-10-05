"""Regression tests for the Lidarr follow-settings audit: schema v38, retry scheduling, title normalisation,
partial-load settling, acquisition-layer outcomes, removed env vars."""

from plex_playlist_sync.storage import SCHEMA_VERSION
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync import list_monitoring
from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator
from plex_playlist_sync.clients.acquisition.base import AcquisitionRetryableError, AcquisitionUnavailableError
from plex_playlist_sync.clients.acquisition.lidarr_adapter import LidarrAdapter
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.lidarr_queue import LidarrTrickleWorker
from plex_playlist_sync.lidarr_release import norm_title, titles_match
from plex_playlist_sync.models import AcquisitionSearchResult, MusicRequest, RequestStatus
from plex_playlist_sync.storage import Database, lidarr_item_due, lidarr_retry_delay
from tests.lidarr_fake import FakeLidarr, FastClock

HTTPX = "plex_playlist_sync.clients.lidarr.httpx.Client"
SONG = "Bohemian Rhapsody"


def client_for() -> LidarrClient:
    return LidarrClient("http://lidarr.test:8686", "key-abcdef123456", root_folder="/music")


def album(album_id, title, kind="Album", released="2000-01-01", monitored=False):
    return {"id": album_id, "title": title, "albumType": kind, "releaseDate": released, "monitored": monitored}


def track(track_id, album_id, title):
    return {"id": track_id, "albumId": album_id, "title": title}


# ------------------------------------------------------------------------------------------ 1. schema v38


def _downgrade_to_v37(path: str) -> None:
    """Rebuilds the state of an install that ran migration v28 before it knew the status columns."""
    raw = sqlite3.connect(path)
    for table, columns in (
        ("music_requests", ("status_reason", "status_message")),
        ("missing_tracks", ("attempts", "next_attempt_at")),
    ):
        for column in columns:
            raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    raw.execute("DELETE FROM schema_migrations WHERE version >= 38")
    raw.commit()
    raw.close()


class TestMigrationV38:
    def test_v37_database_gains_the_columns_and_requests_work(self, tmp_path):
        path = str(tmp_path / "sync.sqlite")
        db = Database(path)
        db.upsert_user("u1", "alice", "a@x.com", is_admin=False)
        db.create_request(
            MusicRequest(id="r1", user_id="u1", item_type="track", title="T", artist="A", status=RequestStatus.PROCESSING)
        )
        db.close()
        _downgrade_to_v37(path)
        raw = sqlite3.connect(path)
        assert "status_reason" not in {r[1] for r in raw.execute("PRAGMA table_info(music_requests)")}
        assert raw.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 37
        raw.close()

        db = Database(path)  # runs v38 over the v37 schema
        try:
            assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
            assert db.get_request("r1")["status_reason"] is None
            assert [r["id"] for r in db.list_requests()] == ["r1"]
            assert db.set_request_outcome("r1", "not_in_metadata_profile", "nope")
            row = db.get_request("r1")
            assert (row["status_reason"], row["status_message"]) == ("not_in_metadata_profile", "nope")
            assert db.list_requests(user_id="u1")[0]["status_message"] == "nope"
            db.update_request_status("r1", "approved")
            assert db.get_request("r1")["status_reason"] is None  # a status change clears the stale outcome
        finally:
            db.close()

    def test_v38_is_idempotent(self, tmp_path):
        path = str(tmp_path / "sync.sqlite")
        db = Database(path)
        db.conn.execute("DELETE FROM schema_migrations WHERE version = 38")
        db.conn.commit()
        db.close()
        db = Database(path)  # columns already exist: must not raise
        try:
            assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        finally:
            db.close()


# ------------------------------------------------------------------------------------------ 2. retry policy


class TestRetryPolicy:
    @pytest.mark.parametrize(
        "status,attempts,expected",
        [
            ("unavailable", 1, timedelta(days=7)),
            ("unavailable", 9, timedelta(days=7)),
            ("not_found", 1, timedelta(days=7)),
            ("error", 1, timedelta(hours=1)),
            ("error", 2, timedelta(hours=6)),
            ("rate_limited", 3, timedelta(hours=24)),
            ("rate_limited", 4, timedelta(days=7)),
            ("error", 50, timedelta(days=7)),
            ("monitored", 1, None),
            ("unmonitored", 1, None),
        ],
    )
    def test_delays(self, status, attempts, expected):
        assert lidarr_retry_delay(status, attempts) == expected

    def test_due_filter(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert lidarr_item_due({"lidarr_status": "unmonitored", "next_attempt_at": None}, now)
        assert not lidarr_item_due({"lidarr_status": "monitored"}, now)
        assert not lidarr_item_due({"lidarr_status": "unavailable", "next_attempt_at": "2026-01-02 00:00:00"}, now)
        assert lidarr_item_due({"lidarr_status": "unavailable", "next_attempt_at": "2026-01-01 11:59:59"}, now)

    @pytest.fixture
    def db(self):
        d = Database(":memory:")
        d.upsert_playlist("pl", "P", service="spotify")
        d.record_sync_result("pl", status="partial", missing_tracks=[{"title": "T", "artist": "A", "album": ""}])
        yield d
        d.close()

    def test_status_updates_schedule_attempts_and_a_success_clears_them(self, db):
        tid = db.get_missing_tracks()[0]["id"]
        now = datetime.now(timezone.utc)
        for expected_hours in (1, 6, 24, 168):
            db.update_missing_tracks_lidarr_status_bulk([tid], "error")
            row = db.get_missing_track(tid)
            due = datetime.strptime(row["next_attempt_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            assert abs((due - now).total_seconds() / 3600 - expected_hours) < 0.1
            assert not lidarr_item_due(row)
        assert db.get_missing_track(tid)["attempts"] == 4
        db.update_missing_track_lidarr_status(tid, "unavailable")
        assert db.get_missing_track(tid)["attempts"] == 5
        db.update_missing_track_lidarr_status(tid, "monitored")
        row = db.get_missing_track(tid)
        assert (row["attempts"], row["next_attempt_at"]) == (0, None)

    def test_sync_keeps_the_schedule(self, db):
        tid = db.get_missing_tracks()[0]["id"]
        db.update_missing_track_lidarr_status(tid, "unavailable")
        db.record_sync_result("pl", status="partial", missing_tracks=[{"title": "T", "artist": "A", "album": ""}])
        row = db.get_missing_tracks()[0]
        assert row["lidarr_status"] == "unavailable" and row["attempts"] == 1 and row["next_attempt_at"]

    def run_worker(self, db, fake, items):
        worker = LidarrTrickleWorker()
        worker._delay_seconds = 0.0
        worker._auto_search = True
        with patch(HTTPX, fake), patch("plex_playlist_sync.lidarr_queue.time", FastClock()):
            worker._process_groups(LidarrTrickleWorker._group_by_artist(items), client_for(), db)

    def test_not_in_metadata_profile_is_unavailable_and_not_re_enqueued_until_due(self, db):
        row = db.get_missing_tracks()[0]
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "A"}]
        fake.albums = [album(2, "Other")]
        fake.tracks = [track(1, 2, "Something Else")]
        self.run_worker(db, fake, [{"id": row["id"], "artist": "A", "album": "", "title": "T", "item_type": "track"}])
        after = db.get_missing_track(row["id"])
        assert after["lidarr_status"] == "unavailable"
        assert not [t for t in db.get_missing_tracks() if lidarr_item_due(t)]  # the trickle enqueue picks nothing
        later = datetime.now(timezone.utc) + timedelta(days=8)
        assert [t for t in db.get_missing_tracks() if lidarr_item_due(t, later)]  # re-checked after a week

    def test_rate_limited_item_gets_the_rate_limited_status_and_backoff(self, db):
        row = db.get_missing_tracks()[0]
        fake = FakeLidarr()
        fake.fail[("GET", "artist/lookup")] = 429
        self.run_worker(db, fake, [{"id": row["id"], "artist": "A", "album": "", "title": "T", "item_type": "track"}])
        after = db.get_missing_track(row["id"])
        assert after["lidarr_status"] == "rate_limited" and after["attempts"] >= 1 and not lidarr_item_due(after)


# ------------------------------------------------------------------------------------------ 3. normalisation


class TestTitleNormalisation:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("Song - Remastered 2011", "Song"),
            ("Song - 2011 Remaster", "Song"),
            ("Song (Remastered 2009)", "Song"),
            ("Song [Remastered]", "Song"),
            ("Song (feat. Someone)", "Song"),
            ("Song ft. Someone", "Song"),
            ("Song (with Someone Else)", "Song"),
            ("Song (Featuring A, B)", "Song"),
            ("Beyoncé", "Beyonce"),
            ("Rock & Roll", "rock and roll"),
            ("Don't Stop!", "dont stop"),
            ("Song (Radio Edit)", "Song"),
            ("Song - Single Version", "Song"),
            ("Song (Album Version)", "Song"),
            ("Song (Mono)", "Song"),
            ("Song (Stereo)", "Song"),
            ("Song (Explicit)", "Song"),
            ("Song [Clean]", "Song"),
            ("Song (Deluxe Edition)", "Song"),
            ("Song (Bonus Track)", "Song"),
            ("Song (2011 Remaster) [Explicit]", "Song"),
            ("  SONG   ", "song"),
        ],
    )
    def test_equivalent(self, left, right):
        assert titles_match(left, right), (norm_title(left), norm_title(right))

    @pytest.mark.parametrize(
        "left,right",
        [
            ("Song (Live)", "Song"),
            ("Song - Live at Wembley", "Song"),
            ("Song (Instrumental)", "Song"),
            ("Song (Remix)", "Song"),
            ("Song (Acoustic)", "Song"),
            ("Song (Demo)", "Song"),
            ("Song (Extended Edit)", "Song"),
            ("Song (Spanish Version)", "Song"),
            ("Love", "Love Songs"),
            ("Live", "Alive"),
            ("Dancing With Myself", "Dancing"),
            ("Stay With Me", "Stay"),
            ("", ""),
            ("Song A", "Song B"),
        ],
    )
    def test_different(self, left, right):
        assert not titles_match(left, right), (norm_title(left), norm_title(right))


# ------------------------------------------------------------------------------------------ 5. partial load


class TestSettling:
    def fake_for_new_artist(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 0, "artistName": "Queen"}]
        fake.albums = [album(1, "Greatest Hits", released="1981-01-01"), album(2, "Opera", released="1975-01-01")]
        fake.tracks = [track(1, 1, "Another One Bites the Dust"), track(2, 2, SONG)]
        return fake

    def add(self, fake, attempts=6):
        with patch(HTTPX, fake), patch("plex_playlist_sync.clients.lidarr.time.sleep") as sleep:
            res = client_for().add_artist_and_albums(
                "Queen", wants=[{"album": "", "title": SONG, "item_type": "track"}], album_wait_attempts=attempts
            )
        return res, sleep

    def test_refresh_command_shape_is_polled_for_the_new_artist(self):
        fake = self.fake_for_new_artist()
        fake.refresh_polls = 2
        fake.album_snapshots = [[album(1, "Greatest Hits", released="1981-01-01")], fake.albums]
        res, _ = self.add(fake)
        assert res["status"] == "success" and res["matched_album_ids"] == [2]
        assert fake.requests("GET", "command")  # GET /api/v1/command was polled
        assert fake.commands[0]["name"] == "RefreshArtist" and fake.commands[0]["status"] == "completed"

    def test_first_non_empty_list_is_not_treated_as_complete(self):
        fake = self.fake_for_new_artist()
        fake.refresh_polls = 3
        # Only the first album has loaded on the first polls; the song's album arrives later.
        fake.album_snapshots = [[fake.albums[0]], [fake.albums[0]], fake.albums]
        res, _ = self.add(fake)
        assert res["status"] == "success" and res["matched_album_ids"] == [2]

    def test_unsettled_with_missing_song_is_albums_pending_never_not_in_profile(self):
        fake = self.fake_for_new_artist()
        fake.refresh_polls = 50  # still refreshing after every poll
        fake.album_snapshots = [[fake.albums[0]]]
        res, _ = self.add(fake, attempts=3)
        assert res["status"] == "albums_pending"
        assert res["outcomes"][0]["status"] == "albums_pending"
        assert not fake.requests("PUT", "album/monitor")

    def test_single_attempt_from_a_request_thread_never_sleeps(self):
        fake = self.fake_for_new_artist()
        fake.refresh_polls = 5
        fake.album_snapshots = [[fake.albums[0]]]
        res, sleep = self.add(fake, attempts=1)
        assert res["status"] == "albums_pending"
        sleep.assert_not_called()

    def test_fallback_to_stable_counts_when_commands_are_not_listed(self):
        fake = self.fake_for_new_artist()
        fake.command_list_supported = False
        fake.album_snapshots = [[fake.albums[0]], fake.albums, fake.albums]
        res, _ = self.add(fake)
        assert res["status"] == "success" and res["matched_album_ids"] == [2]

    def test_fallback_without_stability_stays_pending(self):
        fake = self.fake_for_new_artist()
        fake.command_list_supported = False
        growing = [[album(i, f"A{i}")] + [album(100 + j, f"B{j}") for j in range(i)] for i in range(1, 6)]
        fake.album_snapshots = growing
        fake.tracks = []
        res, _ = self.add(fake, attempts=3)
        assert res["status"] == "albums_pending"

    def test_settled_artist_with_no_such_song_is_not_in_metadata_profile(self):
        fake = self.fake_for_new_artist()
        fake.tracks = [track(1, 1, "Another One Bites the Dust")]
        res, _ = self.add(fake)
        assert res["status"] == "not_in_metadata_profile"

    def test_refresh_state_parsing(self):
        fake = FakeLidarr()
        fake.refresh_polls = 9  # keeps started commands started
        fake.commands = [
            {"id": 1, "name": "RefreshArtist", "status": "completed", "body": {"artistIds": [7]}},
            {"id": 2, "name": "RefreshArtist", "status": "started", "body": {"artistIds": [8]}},
            {"id": 3, "name": "RescanFolders", "status": "started", "body": {}},
        ]
        with patch(HTTPX, fake):
            c = client_for()
            assert c.artist_refresh_state(7) == "done"
            assert c.artist_refresh_state(8) == "running"
            assert c.artist_refresh_state(9) == "unknown"


# ------------------------------------------------------------------------------------------ 7 + 10. adapter


class TestAdapterOutcomes:
    def adapter(self):
        return LidarrAdapter("http://lidarr.test:8686", "key-abcdef123456", root_folder="/music")

    def result(self):
        return AcquisitionSearchResult(
            download_id="x", title=SONG, artist="Queen", album=None, item_type="track", source="lidarr",
            extra={"title": SONG},
        )

    def run(self, fake):
        with patch(HTTPX, fake), patch("plex_playlist_sync.clients.lidarr.time.sleep") as sleep, patch(
            "plex_playlist_sync.clients.acquisition.lidarr_adapter.is_safe_service_url", return_value=True
        ):
            try:
                return self.adapter().download(self.result()), sleep
            finally:
                self.last_sleep = sleep

    def test_rate_limited_raises_the_retryable_error(self):
        fake = FakeLidarr()
        fake.fail[("GET", "artist/lookup")] = 429
        with pytest.raises(AcquisitionRetryableError) as exc:
            self.run(fake)
        assert exc.value.reason == "rate_limited" and exc.value.retry_after == 7

    def test_albums_pending_is_retryable_and_never_sleeps(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 0, "artistName": "Queen"}]
        fake.refresh_polls = 9
        fake.empty_album_polls = 9
        with pytest.raises(AcquisitionRetryableError) as exc:
            self.run(fake)
        assert exc.value.reason == "albums_pending"
        self.last_sleep.assert_not_called()

    def test_not_in_metadata_profile_raises_unavailable(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Opera")]
        fake.tracks = [track(1, 2, "Other")]
        with pytest.raises(AcquisitionUnavailableError):
            self.run(fake)

    def test_success_returns_the_download_id(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums = [album(2, "Opera")]
        fake.tracks = [track(1, 2, SONG)]
        download_id, _ = self.run(fake)
        assert download_id == f"lidarr::Queen::{SONG}"


class TestCoordinatorRecordsOutcome:
    @pytest.mark.parametrize(
        "error,reason,retryable",
        [
            (AcquisitionUnavailableError("Not available with your Lidarr metadata profile"), "not_in_metadata_profile", False),
            (AcquisitionRetryableError("Rate limited", reason="rate_limited"), "rate_limited", True),
        ],
    )
    def test_requester_sees_the_message(self, error, reason, retryable):
        db = Database(":memory:")
        try:
            db.upsert_user("u1", "alice", "a@x.com", is_admin=False)
            db.create_request(
                MusicRequest(id="r1", user_id="u1", item_type="track", title=SONG, artist="Queen", status=RequestStatus.PROCESSING)
            )
            coordinator = AcquisitionCoordinator()
            candidate = AcquisitionSearchResult(
                download_id="x", title=SONG, artist="Queen", album=None, item_type="track", source="lidarr", protocol="lidarr"
            )
            driver = MagicMock()
            driver.download.side_effect = error
            profile = {"id": "p", "name": "p"}
            with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch.object(
                coordinator, "evaluate_and_rank", return_value=[(candidate, MagicMock(score=10))]
            ), patch.object(coordinator, "find_client_for_protocol", return_value={"id": 1, "name": "lidarr"}), patch(
                "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver
            ), patch.object(db, "get_default_quality_profile", return_value=profile), patch(
                "plex_playlist_sync.acquisition_coordinator._to_quality_profile", return_value=MagicMock()
            ):
                res = coordinator._search_and_grab("Queen", SONG, None, "track", "r1", db, None, None, None, None)
            assert res["success"] is False and res["retryable"] is retryable and res["reason"] == reason
            row = db.get_request("r1")
            assert row["status_reason"] == reason and row["status_message"] == str(error)
            assert row["status"] == "processing"
        finally:
            db.close()


# ------------------------------------------------------------------------------------------ 8 + 9. removals


def test_removed_profile_env_vars_are_ignored(monkeypatch):
    monkeypatch.setenv("LIDARR_QUALITY_PROFILE_ID", "3")
    monkeypatch.setenv("LIDARR_METADATA_PROFILE_ID", "4")
    monkeypatch.setenv("PLEX_URL", "http://plex.test:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    cfg = Config.from_env()
    assert not hasattr(cfg, "lidarr_quality_profile_id") and not hasattr(cfg, "lidarr_metadata_profile_id")


def test_lidarr_artist_apply_has_no_resume_parameter():
    assert "resume" not in inspect.signature(list_monitoring._apply_artist_lidarr).parameters
