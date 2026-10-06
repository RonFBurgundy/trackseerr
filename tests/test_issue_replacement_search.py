"""Replacement search used by the issue fix actions: targets tracks that already have files."""

from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.backlog_worker import ReplacementSpec, backlog_worker
from plex_playlist_sync.models import AcquisitionSearchResult, DownloadClientConfig, IndexerConfig
from plex_playlist_sync.storage import Database

SAME_QUALITY_RELEASE = "Beta - Low Quality [FLAC]"


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_pacing():
    old = backlog_worker.pace_delay
    backlog_worker.pace_delay = 0
    yield
    backlog_worker.pace_delay = old


def _library(db, *, cutoff_met):
    db.upsert_library_artist({"id": "ar-1", "name": "Beta", "clean_name": "beta", "monitored": True})
    db.upsert_library_album(
        {"id": "al-1", "artist_id": "ar-1", "title": "Second", "clean_title": "second", "year": 2010, "monitored": True}
    )
    db.upsert_library_track(
        {"id": "t-1", "album_id": "al-1", "artist_id": "ar-1", "title": "Low Quality", "clean_title": "low quality",
         "track_number": 1, "disc_number": 1, "monitored": True}
    )
    db.upsert_library_file(
        {"id": "f-1", "track_id": "t-1", "file_path": "/m/f-1.flac", "relative_path": "f-1.flac", "codec": "FLAC",
         "quality_name": "FLAC 16bit", "size_bytes": 10, "cutoff_met": cutoff_met}
    )


def _targets(db):
    return db.list_wanted_search_targets(track_ids=["t-1"])


def _run(db, replacement=None):
    res = backlog_worker.queue_wanted_search(db, _targets(db), replacement=replacement)
    if backlog_worker.last_search_thread is not None:
        backlog_worker.last_search_thread.join(timeout=5)
    return res


class TestBacklogReplacementKwargs:
    def _search(self, db, replacement):
        with patch.object(
            acquisition_coordinator, "search_and_grab", return_value={"success": False, "message": "none"}
        ) as search:
            res = _run(db, replacement)
        return res, search

    def test_same_quality_replacement_has_no_floor_and_bypasses_delay(self, test_db):
        _library(test_db, cutoff_met=True)
        res, search = self._search(test_db, ReplacementSpec("iss-1"))
        assert res == {"queued": 1}
        kw = search.call_args.kwargs
        assert kw["min_score"] is None and kw["bypass_delay"] is True and kw["replacement_issue_id"] == "iss-1"

    def test_normal_mode_on_cutoff_met_file_has_no_issue_tag(self, test_db):
        _library(test_db, cutoff_met=True)
        _, search = self._search(test_db, None)
        kw = search.call_args.kwargs
        assert kw["bypass_delay"] is False and kw["replacement_issue_id"] is None

    def test_require_better_keeps_an_upgrade_floor_even_when_cutoff_met(self, test_db):
        _library(test_db, cutoff_met=True)
        _, search = self._search(test_db, ReplacementSpec("iss-1", require_better=True))
        assert search.call_args.kwargs["min_score"] is not None

    def test_recent_search_guard_bypassed_only_in_replacement_mode(self, test_db):
        _library(test_db, cutoff_met=True)
        test_db.mark_tracks_searched(["t-1"])
        normal, _ = self._search(test_db, None)
        assert normal["queued"] == 0 and "10 minutes" in normal["message"]
        replaced, search = self._search(test_db, ReplacementSpec("iss-1"))
        assert replaced == {"queued": 1} and search.called


class TestReplacementEndToEnd:
    """Real coordinator ranking with mocked indexer and download client driver."""

    def _setup(self, db):
        _library(db, cutoff_met=False)
        db.create_indexer(
            IndexerConfig(id="idx-1", name="Prowlarr", indexer_type="torznab", host_url="http://p:9696", enabled=True)
        )
        db.create_download_client(
            DownloadClientConfig(
                id="client-qbit", name="qBittorrent", driver_type="qbittorrent", host_url="http://q:8080", enabled=True
            )
        )

    def _candidate(self, title=SAME_QUALITY_RELEASE):
        return AcquisitionSearchResult(
            download_id="dl-hash-1", title=title, artist="Beta", item_type="track", size_bytes=35 * 1024 * 1024,
            magnet_url="magnet:?xt=urn:btih:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            source="torznab", protocol="torrent", seeders=5,
        )

    def _grab(self, db, replacement, candidate=None):
        driver = MagicMock()
        driver.download.return_value = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        with patch.object(
            acquisition_coordinator, "search_all_indexers", return_value=[candidate or self._candidate()]
        ), patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
            _run(db, replacement)
        return driver

    def test_same_quality_rejected_in_normal_mode(self, test_db):
        self._setup(test_db)
        driver = self._grab(test_db, None)
        driver.download.assert_not_called()

    def test_same_quality_accepted_in_replacement_mode_and_issue_recorded(self, test_db):
        self._setup(test_db)
        driver = self._grab(test_db, ReplacementSpec("iss-1"))
        driver.download.assert_called_once()
        dl = test_db.list_active_downloads(statuses=["queued"])[0]
        assert test_db.get_download_replacement_issue(dl["id"]) == "iss-1"

    def test_audio_quality_still_requires_a_better_release(self, test_db):
        self._setup(test_db)
        driver = self._grab(test_db, ReplacementSpec("iss-1", require_better=True))
        driver.download.assert_not_called()

    def test_blocklisted_release_is_not_regrabbed(self, test_db):
        self._setup(test_db)
        test_db.add_to_blocklist(source_title=SAME_QUALITY_RELEASE, reason="wrong release")
        driver = self._grab(test_db, ReplacementSpec("iss-1"))
        driver.download.assert_not_called()

    def test_plain_grab_has_no_issue_link(self, test_db):
        self._setup(test_db)
        self._grab(test_db, ReplacementSpec("iss-1"))
        dl = test_db.list_active_downloads(statuses=["queued"])[0]
        assert test_db.get_download_replacement_issue("someone-else") is None
        assert test_db.get_download_replacement_issue(dl["id"]) == "iss-1"
