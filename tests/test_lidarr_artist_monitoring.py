"""A request monitors its release; Lidarr then ignores it unless the ARTIST is monitored too."""

from unittest.mock import patch

from trackseerr.clients.lidarr import LidarrClient
from tests.lidarr_fake import FakeLidarr
from tests.test_lidarr_add_options_race import SONG, WANT, _album, add, client_for, new_artist_fake

HTTPX = "trackseerr.clients.lidarr.httpx.Client"


def test_new_request_artist_added_with_monitor_new_items_none_then_monitored():
    fake = new_artist_fake()
    res = add(fake, attempts=6)
    assert res["status"] == "success" and res["matched_album_ids"] == [2]
    post = fake.requests("POST", "artist")[0]
    assert post["monitorNewItems"] == "none"
    assert post["addOptions"]["monitor"] == "none"
    assert fake.artist_monitored is True
    assert [a["monitored"] for a in fake.albums] == [False, True]  # other album untouched
    # The artist PUT happens after the album monitor and before the search.
    order = [(m, p.split("?")[0]) for m, p, _ in fake.calls if m in ("PUT", "POST") and p != "artist"]
    assert order.index(("PUT", "album/monitor")) < order.index(("PUT", "artist/99")) < order.index(("POST", "command"))


def test_whole_artist_add_keeps_root_folder_defaults():
    fake = FakeLidarr()
    with patch(HTTPX, fake):
        client_for().add_artist_with_defaults({"artistName": "Queen"}, whole_artist=True)
    post = fake.requests("POST", "artist")[0]
    assert post["monitorNewItems"] == "new" and post["addOptions"]["monitor"] == "future"


def test_existing_unmonitored_artist_becomes_monitored_other_albums_untouched():
    fake = FakeLidarr()
    fake.existing_artist_id = 5
    fake.artist_monitored = False
    fake.lookup = [{"id": 5, "artistName": "Queen"}]
    fake.albums = [_album(1, "Greatest Hits"), _album(2, "Opera")]
    fake.tracks = [{"id": 2, "albumId": 2, "title": SONG}]
    with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep"):
        res = client_for().add_artist_and_albums("Queen", wants=WANT, auto_search=True)
    assert res["status"] == "success" and res["added"] is False
    assert fake.artist_monitored is True
    put = fake.requests("PUT", "artist/5")[0]
    assert put["monitorNewItems"] == "all" and put["qualityProfileId"] == 4  # full resource preserved
    assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
    assert [a["monitored"] for a in fake.albums] == [False, True]
    assert fake.requests("POST", "artist") == []


def test_already_monitored_artist_is_not_put_again():
    fake = FakeLidarr()
    fake.existing_artist_id = 5
    fake.artist_monitored = True
    fake.lookup = [{"id": 5, "artistName": "Queen"}]
    fake.albums = [_album(2, "Opera")]
    fake.tracks = [{"id": 2, "albumId": 2, "title": SONG}]
    with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep"):
        client_for().add_artist_and_albums("Queen", wants=WANT)
    assert fake.requests("PUT", "artist/") == []


def test_artist_not_touched_when_no_album_was_monitored():
    fake = FakeLidarr()
    fake.existing_artist_id = 5
    fake.lookup = [{"id": 5, "artistName": "Queen"}]
    fake.albums = [_album(1, "Greatest Hits")]
    fake.tracks = [{"id": 1, "albumId": 1, "title": "Other Song"}]
    with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep"):
        res = client_for().add_artist_and_albums("Queen", wants=WANT)
    assert res["status"] != "success"
    assert fake.requests("PUT", "artist/") == [] and fake.artist_monitored is False


def test_artist_monitor_failure_does_not_fail_the_request():
    fake = FakeLidarr()
    fake.existing_artist_id = 5
    fake.lookup = [{"id": 5, "artistName": "Queen"}]
    fake.albums = [_album(2, "Opera")]
    fake.tracks = [{"id": 2, "albumId": 2, "title": SONG}]
    fake.fail[("PUT", "artist/5")] = 500
    with patch(HTTPX, fake), patch("trackseerr.clients.lidarr.time.sleep"):
        res = client_for().add_artist_and_albums("Queen", wants=WANT)
    assert res["status"] == "success" and res["matched_album_ids"] == [2]


def test_ensure_artist_monitored_reports_whether_it_changed_anything():
    fake = FakeLidarr()
    fake.existing_artist_id = 5
    with patch(HTTPX, fake):
        c: LidarrClient = client_for()
        assert c.ensure_artist_monitored(5) is True
        assert c.ensure_artist_monitored(5) is False
