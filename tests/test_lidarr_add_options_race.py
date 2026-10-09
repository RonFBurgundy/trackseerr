"""Lidarr applies a new artist's ``addOptions.monitor: "none"`` only after RefreshArtist reads ``completed``.

Until then every album lists as ``monitored`` (observed on Lidarr 3.1.0.4875, see docs/INTEGRATION_TESTS.md), so a
client that settles in that window and trusts the flag skips the PUT and ends with nothing monitored.
"""

from unittest.mock import patch

from trackseerr.clients.lidarr import LidarrClient
from tests.lidarr_fake import FakeLidarr, metadata_profile

HTTPX = "trackseerr.clients.lidarr.httpx.Client"
SONG = "Bohemian Rhapsody"
WANT = [{"album": "", "title": SONG, "item_type": "track"}]


def client_for() -> LidarrClient:
    return LidarrClient("http://lidarr.test:8686", "key-abcdef123456", root_folder="/music")


def _album(album_id, title, monitored=False):
    return {"id": album_id, "title": title, "albumType": "Album", "releaseDate": "1975-01-01", "monitored": monitored}


def new_artist_fake() -> FakeLidarr:
    fake = FakeLidarr()
    fake.lookup = [{"id": 0, "artistName": "Queen"}]
    fake.albums = [_album(1, "Greatest Hits"), _album(2, "Opera")]
    fake.tracks = [
        {"id": 1, "albumId": 1, "title": "Another One Bites the Dust"},
        {"id": 2, "albumId": 2, "title": SONG},
    ]
    fake.model_add_window = True
    fake.refresh_polls = 1  # the first command poll already reads "completed"
    return fake


def add(fake: FakeLidarr, attempts: int):
    with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep"):
        return client_for().add_artist_and_albums("Queen", wants=WANT, album_wait_attempts=attempts)


class TestAddOptionsWindow:
    def test_polling_inside_the_window_is_not_settled(self):
        fake = new_artist_fake()
        res = add(fake, attempts=1)  # refresh done, addOptions still set
        assert res["status"] == "albums_pending"
        assert fake.add_window_open and not fake.requests("PUT", "album/monitor")

    def test_waits_out_the_window_then_monitors_exactly_the_chosen_album(self):
        fake = new_artist_fake()
        res = add(fake, attempts=6)
        assert res["status"] == "success" and res["matched_album_ids"] == [2]
        assert not fake.add_window_open and fake.add_options is None
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
        assert [a["monitored"] for a in fake.albums] == [False, True]

    def test_put_is_sent_even_when_the_chosen_album_lists_as_monitored(self):
        fake = new_artist_fake()
        fake.model_add_window = False
        fake.albums[1]["monitored"] = True  # the stale "monitored" Lidarr lists before applying monitor: none
        res = add(fake, attempts=3)
        assert res["status"] == "success"
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]

    def test_single_attempt_settles_once_a_later_poll_sees_addoptions_cleared(self):
        fake = new_artist_fake()
        assert add(fake, attempts=1)["status"] == "albums_pending"
        with patch(HTTPX, fake):
            _, settled = client_for().wait_for_artist_albums(fake.NEW_ARTIST_ID, 1, 0)
        assert settled and fake.add_options is None  # the album list read just before may still be stale: always PUT


class TestExistingArtist:
    def test_already_monitored_album_is_not_put_again(self):
        fake = new_artist_fake()
        fake.model_add_window = False
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.albums[1]["monitored"] = True
        res = add(fake, attempts=1)
        assert res["status"] == "success" and res["added"] is False
        assert not fake.requests("PUT", "album/monitor")


class TestZeroTypesProfile:
    def test_profile_allowing_no_primary_type_is_not_in_metadata_profile(self):
        fake = new_artist_fake()
        none_profile = metadata_profile(6, "None")
        for item in none_profile["primaryAlbumTypes"]:
            item["allowed"] = False
        fake.metadata_profiles = [metadata_profile(1, "Standard"), none_profile]
        fake.albums = []
        with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep") as sleep:
            res = client_for().add_artist_and_albums("Queen", wants=WANT, album_wait_attempts=6)
        assert res["status"] == "not_in_metadata_profile"
        assert res["added"] is True
        sleep.assert_not_called()
        assert not fake.requests("GET", "album?artistId") and not fake.requests("PUT", "album/monitor")
