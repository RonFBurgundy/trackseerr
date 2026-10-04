"""The "Prefer singles for song requests" Lidarr setting: selection order, persistence, migration, the defaults
endpoint's ``singles_enabled`` and the end-to-end effect on which release gets monitored."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import library_manager
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.lidarr import LidarrClient, invalidate_add_defaults
from plex_playlist_sync.config import Config
from plex_playlist_sync.lidarr_release import select_release_for_song
from plex_playlist_sync.storage import Database
from tests.lidarr_fake import FakeLidarr, metadata_profile

API_KEY = "lidarr-secret-key-abcdef123456"
HTTPX = "plex_playlist_sync.clients.lidarr.httpx.Client"
SONG = "Bohemian Rhapsody"


def album(album_id, title, kind="Album", released="2000-01-01"):
    return {"id": album_id, "title": title, "albumType": kind, "releaseDate": released, "monitored": False}


@pytest.fixture(autouse=True)
def _fresh_cache():
    invalidate_add_defaults()
    yield
    invalidate_add_defaults()


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def admin_client(test_db, tmp_path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    app = create_app(db=test_db, config=config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: config
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id="admin-1", username="admin_user", is_admin=True, secret_key=secret)
    test_db.create_session(token, "admin-1", {"auth": "test"})
    return TestClient(app), {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------------------------------ selection order


SINGLE = album(10, SONG, "Single", "1975-10-31")
OPERA = album(2, "A Night at the Opera", "Album", "1975-11-21")
HITS = album(1, "Greatest Hits", "Album", "1981-10-26")
EP = album(3, "Live EP", "EP", "1970-01-01")
OTHER = album(4, "Broadcast", "Broadcast", "1960-01-01")


@pytest.mark.parametrize(
    "candidates, hint, with_singles, without_singles",
    [
        ([HITS, OPERA, SINGLE], "", 10, 2),  # earliest album beats the single when singles are off
        ([HITS, OPERA, SINGLE], "Greatest Hits", 10, 1),  # a hinted album beats the single when singles are off
        ([HITS, OPERA, SINGLE], "Not A Candidate", 10, 2),  # an unmatched hint is ignored
        ([HITS, EP, SINGLE], "", 10, 1),  # album beats EP either way once the single is out of the way
        ([EP, SINGLE], "", 10, 3),  # EP beats the single when singles are off
        ([SINGLE, OTHER], "", 10, 10),  # a single beats "other" kinds in both modes
        ([HITS, EP, OTHER], "", 1, 1),  # no single: identical
        ([SINGLE], "", 10, 10),  # only the single: both modes pick it
        ([album(11, "Different Title", "Single", "1970-01-01"), HITS], "", 1, 1),  # a non-matching single never leads
    ],
)
def test_selection_order(candidates, hint, with_singles, without_singles):
    assert select_release_for_song(candidates, SONG, hint)["id"] == with_singles
    assert select_release_for_song(candidates, SONG, hint, prefer_singles=True)["id"] == with_singles
    assert select_release_for_song(candidates, SONG, hint, prefer_singles=False)["id"] == without_singles


def test_selection_with_no_candidates_is_none_in_both_modes():
    assert select_release_for_song([], SONG, prefer_singles=False) is None
    assert select_release_for_song([], SONG, prefer_singles=True) is None


# ------------------------------------------------------------------------ persistence and migration


def test_setting_defaults_on_and_round_trips(test_db):
    assert test_db.get_lidarr_settings()["prefer_singles"] is True
    assert test_db.update_lidarr_settings({"prefer_singles": False})["prefer_singles"] is False
    assert test_db.get_lidarr_settings()["prefer_singles"] is False
    assert test_db.update_lidarr_settings({"prefer_singles": None})["prefer_singles"] is False  # None never overwrites
    assert test_db.update_lidarr_settings({"prefer_singles": True})["prefer_singles"] is True


def test_setting_round_trips_through_the_api(admin_client):
    client, headers = admin_client
    assert client.get("/api/settings/lidarr", headers=headers).json()["prefer_singles"] is True
    resp = client.put("/api/settings/lidarr", json={"prefer_singles": False}, headers=headers)
    assert resp.status_code == 200 and resp.json()["prefer_singles"] is False
    assert client.get("/api/settings/lidarr", headers=headers).json()["prefer_singles"] is False


def _columns(db):
    return [r[1] for r in db.conn.execute("PRAGMA table_info(lidarr_settings)").fetchall()]


def test_migration_is_idempotent_and_adds_the_column_to_an_old_table(test_db):
    assert _columns(test_db).count("prefer_singles") == 1
    cur = test_db.conn.cursor()
    test_db._migration_v38(cur)  # re-run with the column present: no error, no duplicate
    assert _columns(test_db).count("prefer_singles") == 1

    test_db.conn.execute("ALTER TABLE lidarr_settings DROP COLUMN prefer_singles")
    assert "prefer_singles" not in _columns(test_db)
    test_db._migration_v38(test_db.conn.cursor())
    assert _columns(test_db).count("prefer_singles") == 1
    assert test_db.get_lidarr_settings()["prefer_singles"] is True  # the existing singleton row gets the default


# --------------------------------------------------------------------- defaults endpoint: singles_enabled


class TestSinglesEnabled:
    def get(self, admin_client, test_db, fake):
        client, headers = admin_client
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY, "root_folder": "/music"})
        with patch(HTTPX, fake):
            resp = client.get("/api/settings/lidarr/defaults", headers=headers)
        assert resp.status_code == 200
        return resp.json()

    def test_true_when_the_resolved_profile_allows_singles(self, admin_client, test_db):
        assert self.get(admin_client, test_db, FakeLidarr())["singles_enabled"] is True

    def test_false_when_the_resolved_profile_excludes_singles(self, admin_client, test_db):
        fake = FakeLidarr()
        fake.metadata_profiles = [metadata_profile(1, "Standard"), metadata_profile(6, "Albums only", singles=False)]
        assert self.get(admin_client, test_db, fake)["singles_enabled"] is False

    def test_reads_the_profile_the_root_folder_resolves_to(self, admin_client, test_db):
        fake = FakeLidarr()  # root folder resolves to profile 6; profile 1 excludes singles but must not matter
        fake.metadata_profiles = [metadata_profile(1, "Albums only", singles=False), metadata_profile(6, "Everything")]
        assert self.get(admin_client, test_db, fake)["singles_enabled"] is True
        assert fake.paths("GET").count("metadataprofile/6") == 1

    @pytest.mark.parametrize(
        "primary",
        [None, "nope", [], [{"albumType": {"name": "Album"}, "allowed": True}], [{"albumType": "Single"}], ["x"]],
    )
    def test_unexpected_shape_falls_back_to_true_with_a_warning(self, admin_client, test_db, caplog, primary):
        fake = FakeLidarr()
        fake.metadata_profiles = [{"id": 1, "name": "Standard"}, {"id": 6, "name": "Everything", "primaryAlbumTypes": primary}]
        with caplog.at_level("WARNING"):
            assert self.get(admin_client, test_db, fake)["singles_enabled"] is True
        assert "unexpected shape" in caplog.text

    def test_is_cached_until_the_settings_are_saved(self, admin_client, test_db):
        client, headers = admin_client
        fake = FakeLidarr()
        assert self.get(admin_client, test_db, fake)["singles_enabled"] is True
        fake.metadata_profiles[1] = metadata_profile(6, "Everything", singles=False)
        with patch(HTTPX, fake):
            assert client.get("/api/settings/lidarr/defaults", headers=headers).json()["singles_enabled"] is True
            client.put("/api/settings/lidarr", json={"root_folder": "/music"}, headers=headers)
            assert client.get("/api/settings/lidarr/defaults", headers=headers).json()["singles_enabled"] is False
        assert fake.paths("GET").count("metadataprofile/6") == 2


# --------------------------------------------------------------------------------------------- end to end


def _song_fake():
    fake = FakeLidarr()
    fake.lookup = [{"id": 5, "artistName": "Queen"}]
    fake.albums = [OPERA | {"monitored": False}, SINGLE | {"monitored": False}]
    fake.tracks = [
        {"id": 1, "albumId": 2, "title": SONG},
        {"id": 2, "albumId": 10, "title": SONG},
    ]
    return fake


def _monitored(fake):
    return sorted(a["id"] for a in fake.albums if a["monitored"])


@pytest.mark.parametrize("prefer, expected", [(True, [10]), (False, [2])])
def test_client_monitors_single_or_album_per_the_setting(prefer, expected):
    fake = _song_fake()
    client = LidarrClient("http://lidarr.test:8686", API_KEY, auto_search=False, prefer_singles=prefer)
    with patch(HTTPX, fake):
        res = client.add_artist_and_albums(
            "Queen", wants=[{"album": "", "title": SONG, "item_type": "track"}]
        )
    assert res["status"] == "success" and _monitored(fake) == expected


@pytest.mark.parametrize("stored, expected", [(True, [10]), (False, [2])])
def test_stored_setting_reaches_the_client_built_by_the_library_manager(test_db, stored, expected):
    test_db.update_lidarr_settings(
        {"url": "http://lidarr.test:8686", "api_key": API_KEY, "auto_search": False, "prefer_singles": stored}
    )
    client = library_manager.build_lidarr_client(test_db, None)
    assert client is not None and client.prefer_singles is stored
    fake = _song_fake()
    with patch(HTTPX, fake):
        client.add_artist_and_albums("Queen", wants=[{"album": "", "title": SONG, "item_type": "track"}])
    assert _monitored(fake) == expected
