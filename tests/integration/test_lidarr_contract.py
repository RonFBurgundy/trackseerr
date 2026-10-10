"""Contract tests: Trackseerr's Lidarr client against a REAL Lidarr (see docs/INTEGRATION_TESTS.md).

``tests/lidarr_fake.py`` was written from Lidarr's source; these tests confirm the real API matches what the client
(and the fake) assume. No download clients or indexers exist in the target Lidarr, so nothing is ever downloaded.
"""

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.config import Config
from trackseerr.lidarr_release import albums_containing_song, select_release_for_song
from trackseerr.storage import Database
from tests.integration.conftest import (
    ARTIST_MBID,
    ARTIST_NAME,
    MONITOR_OPTION,
    NEW_ITEM_OPTION,
    PROFILE_NO_SINGLES,
    PROFILE_WITH_SINGLES,
    QUALITY_NAME,
    ROOT_MAIN,
    ROOT_NO_SINGLES,
    SONG_ON_SINGLE_AND_ALBUM,
    SONG_ONLY_ON_A_SINGLE,
    TAG_LABEL,
    make_client,
)

pytestmark = pytest.mark.integration

SETTLE_ATTEMPTS = 120
SETTLE_SECONDS = 0.5


def song_want(title: str, album: str = "") -> list[dict[str, str]]:
    return [{"album": album, "title": title, "item_type": "track"}]


def artist_writes(calls):
    """POST/PUT/DELETE calls aimed at the artist resource (``artist``, ``artist/<id>``, ``artist/editor``)."""
    return [c for c in calls if c[0] in ("POST", "PUT", "DELETE") and c[1].split("?")[0].startswith("artist")]


# ------------------------------------------------------------------------------------ 1. root-folder defaults


def test_root_folder_defaults_match_what_was_configured(client, lidarr_admin, lidarr_setup):
    raw = next(r for r in lidarr_admin.get("rootfolder") if r["path"] == ROOT_MAIN)
    # The field names the client reads, confirmed against the real resource.
    for field in (
        "defaultQualityProfileId",
        "defaultMetadataProfileId",
        "defaultMonitorOption",
        "defaultNewItemMonitorOption",
        "defaultTags",
    ):
        assert field in raw, f"rootfolder resource has no {field}: {sorted(raw)}"

    defaults = client.get_root_folder_defaults()
    assert defaults.source == "rootfolder"
    assert defaults.root_folder_path == ROOT_MAIN
    assert defaults.quality_profile_id == lidarr_setup["quality_id"]
    assert defaults.metadata_profile_id == lidarr_setup["with_singles_id"]
    assert defaults.monitor == MONITOR_OPTION
    assert defaults.new_item_monitor == NEW_ITEM_OPTION
    assert defaults.tag_ids == [lidarr_setup["tag_id"]]


def test_root_folder_defaults_follow_the_requested_folder(lidarr_target, lidarr_setup):
    other = make_client(*lidarr_target, root_folder=ROOT_NO_SINGLES).get_root_folder_defaults()
    assert other.source == "rootfolder" and other.root_folder_path == ROOT_NO_SINGLES
    assert other.metadata_profile_id == lidarr_setup["no_singles_id"]
    assert (other.monitor, other.new_item_monitor, other.tag_ids) == ("none", "none", [])


# ------------------------------------------------------------------------- 2. metadata profile singles flag


def test_metadata_profile_singles_flag(client, lidarr_setup):
    assert client.metadata_profile_allows_singles(lidarr_setup["with_singles_id"]) is True
    assert client.metadata_profile_allows_singles(lidarr_setup["no_singles_id"]) is False


# ------------------------------------------------------------------------------------- 3. whole-artist add


def test_whole_artist_add_carries_the_root_folder_defaults(client, lidarr_admin, lidarr_setup, metadata_network, clean_artist):
    created = client.add_artist_with_defaults(metadata_network, whole_artist=True, search=False)
    artist = lidarr_admin.get(f"artist/{created['id']}")
    assert artist["foreignArtistId"] == ARTIST_MBID
    assert artist["qualityProfileId"] == lidarr_setup["quality_id"]
    assert artist["metadataProfileId"] == lidarr_setup["with_singles_id"]
    assert artist["tags"] == [lidarr_setup["tag_id"]]
    assert artist["rootFolderPath"].rstrip("/") == ROOT_MAIN
    assert artist["monitorNewItems"] == NEW_ITEM_OPTION
    assert artist["monitored"] is True
    # addOptions.monitor is the root folder's option; the response echoes it.
    assert created["addOptions"]["monitor"] == MONITOR_OPTION
    assert created["addOptions"]["searchForMissingAlbums"] is False

    albums, settled = client.wait_for_artist_albums(created["id"], SETTLE_ATTEMPTS, SETTLE_SECONDS)
    assert settled and albums
    lidarr_admin.wait_add_options_consumed(created["id"])
    # "future" monitors only releases dated after the add; this catalogue is entirely in the past.
    assert all(a["releaseDate"][:10] <= date.today().isoformat() for a in albums)
    assert lidarr_admin.monitored_album_ids(created["id"]) == []


# ------------------------------------------------------------------ 4. song request for a NEW artist


def test_song_request_for_new_artist_monitors_exactly_one_album(
    lidarr_target, lidarr_admin, lidarr_setup, metadata_network, clean_artist, recorded_calls
):
    client = make_client(*lidarr_target, auto_search=True)
    states: list[str] = []
    real_state = client.artist_refresh_state

    def observe(artist_id: int) -> str:
        state = real_state(artist_id)
        states.append(state)
        return state

    client.artist_refresh_state = observe  # type: ignore[method-assign]

    result = client.add_artist_and_albums(
        ARTIST_NAME,
        wants=song_want(SONG_ON_SINGLE_AND_ALBUM),
        album_wait_attempts=SETTLE_ATTEMPTS,
        album_wait_seconds=0.25,
    )

    assert result["status"] == "success", result
    assert result["added"] is True and result["searched"] is True
    outcome = result["outcomes"][0]
    assert outcome["status"] == "monitored"

    # RefreshArtist was really observed running, then done, in that order, and the wait stopped at the first "done".
    assert "running" in states, f"never saw the refresh running: {states}"
    assert states[-1] == "done", states
    assert states.index("running") < states.index("done")
    assert "unknown" not in states[states.index("running") :]

    # Lidarr's own state: the artist was added with monitor none and exactly the chosen album is monitored.
    artist_id = result["artist_id"]
    posts = [c for c in recorded_calls if c[0] == "POST" and c[1] == "artist"]
    assert len(posts) == 1 and posts[0][2]["addOptions"]["monitor"] == "none"
    assert posts[0][2]["addOptions"]["searchForMissingAlbums"] is False
    monitored = lidarr_admin.monitored_album_ids(artist_id)
    assert monitored == [outcome["album_id"]] == sorted(result["matched_album_ids"])
    chosen = lidarr_admin.get(f"album/{outcome['album_id']}")
    assert chosen["albumType"] == "Single" and chosen["title"] == SONG_ON_SINGLE_AND_ALBUM
    assert len(lidarr_admin.albums(artist_id)) > 1  # the other releases exist, unmonitored

    # The AlbumSearch command shape the client sends is accepted and listed back by Lidarr.
    searches = [c for c in lidarr_admin.get("command") if c["name"] == "AlbumSearch"]
    assert searches and searches[-1]["body"]["albumIds"] == [outcome["album_id"]]
    assert searches[-1]["status"].islower()


# ----------------------------------------------------------------------------------- 5. release selection


@pytest.mark.parametrize(
    "prefer_singles, expected_type, expected_title",
    [(True, "Single", SONG_ON_SINGLE_AND_ALBUM), (False, "Album", "In the Aeroplane Over the Sea")],
)
def test_release_selection_uses_the_real_album_and_track_lists(
    lidarr_target, lidarr_admin, settled_artist, recorded_calls, prefer_singles, expected_type, expected_title
):
    client = make_client(*lidarr_target, prefer_singles=prefer_singles)
    artist_id = settled_artist["id"]

    albums = client.fetch_artist_albums(artist_id)
    tracks = client.fetch_artist_tracks(artist_id, albums)
    # Which track query worked is recorded here: /track?artistId= answered, so no per-album fallback was needed.
    track_calls = [c[1] for c in recorded_calls if c[1].startswith("track")]
    assert track_calls == [f"track?artistId={artist_id}"], track_calls
    assert tracks and all(t["albumId"] in {a["id"] for a in albums} for t in tracks)

    candidates = albums_containing_song(tracks, albums, SONG_ON_SINGLE_AND_ALBUM)
    assert sorted(a["albumType"] for a in candidates) == ["Album", "Single"]
    picked = select_release_for_song(candidates, SONG_ON_SINGLE_AND_ALBUM, prefer_singles=prefer_singles)
    assert (picked["albumType"], picked["title"]) == (expected_type, expected_title)

    result = client.add_artist_and_albums(ARTIST_NAME, wants=song_want(SONG_ON_SINGLE_AND_ALBUM))
    assert result["status"] == "success" and result["added"] is False
    assert result["matched_album_ids"] == [picked["id"]]
    assert lidarr_admin.monitored_album_ids(artist_id) == [picked["id"]]


# ---------------------------------------------------------------------------------- 6. existing artist


def test_request_against_existing_artist_only_sets_it_monitored(
    lidarr_target, lidarr_admin, settled_artist, recorded_calls
):
    artist_id = settled_artist["id"]
    # An unmonitored existing artist is set monitored (Lidarr does not search an unmonitored artist's albums), nothing else.
    current = lidarr_admin.get(f"artist/{artist_id}")
    lidarr_admin.put(f"artist/{artist_id}", {**current, "monitored": False})
    before = lidarr_admin.get(f"artist/{artist_id}")
    assert before["monitored"] is False
    recorded_calls.clear()

    client = make_client(*lidarr_target)
    result = client.add_artist_and_albums(ARTIST_NAME, wants=song_want(SONG_ON_SINGLE_AND_ALBUM))

    assert result["status"] == "success" and result["added"] is False
    writes = artist_writes(recorded_calls)
    assert len(writes) == 1, writes
    assert writes[0][:2] == ("PUT", f"artist/{artist_id}")
    assert writes[0][2]["monitored"] is True
    assert sorted(c[1] for c in recorded_calls if c[0] == "PUT") == sorted([f"artist/{artist_id}", "album/monitor"])
    after = lidarr_admin.get(f"artist/{artist_id}")
    assert after["monitored"] is True
    derived = {"lastAlbum", "nextAlbum", "statistics", "monitored"}  # recomputed by Lidarr when an album's monitoring changes
    assert {k: v for k, v in after.items() if k not in derived} == {k: v for k, v in before.items() if k not in derived}
    assert lidarr_admin.monitored_album_ids(artist_id) == sorted(result["matched_album_ids"])


# ------------------------------------------------------------------------- 7. not_in_metadata_profile


def test_song_only_on_a_single_is_not_in_a_no_singles_profile(
    lidarr_target, lidarr_admin, lidarr_setup, metadata_network, clean_artist
):
    client = make_client(*lidarr_target, root_folder=ROOT_NO_SINGLES)
    result = client.add_artist_and_albums(
        ARTIST_NAME,
        wants=song_want(SONG_ONLY_ON_A_SINGLE),
        album_wait_attempts=SETTLE_ATTEMPTS,
        album_wait_seconds=0.5,
    )
    assert result["status"] == "not_in_metadata_profile", result
    assert result["added"] is True
    assert [o["status"] for o in result["outcomes"]] == ["not_in_metadata_profile"]
    assert result["matched_album_ids"] == []

    artist = lidarr_admin.test_artist()
    assert artist["metadataProfileId"] == lidarr_setup["no_singles_id"]
    albums = lidarr_admin.albums(artist["id"])
    assert albums and all(a["albumType"] != "Single" for a in albums)  # Lidarr's list already honours the profile
    assert lidarr_admin.monitored_album_ids(artist["id"]) == []


# ----------------------------------------------------------------------------------- 8. defaults endpoint


@pytest.fixture
def admin_api(tmp_path, lidarr_target):
    db = Database(":memory:")
    db.update_lidarr_settings({"url": lidarr_target[0], "api_key": lidarr_target[1], "root_folder": ROOT_MAIN})
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id="admin-1", username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, "admin-1", {"auth": "test"})
    yield TestClient(app), {"Authorization": f"Bearer {token}"}
    db.close()


def test_defaults_endpoint_shape_against_real_lidarr(admin_api, lidarr_setup):
    api, headers = admin_api
    resp = api.get("/api/settings/lidarr/defaults", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    root_folders = body.pop("root_folders")
    assert sorted(root_folders) == sorted([ROOT_MAIN, ROOT_NO_SINGLES])
    assert body == {
        "root_folder": ROOT_MAIN,
        "quality_profile": {"id": lidarr_setup["quality_id"], "name": QUALITY_NAME},
        "metadata_profile": {"id": lidarr_setup["with_singles_id"], "name": PROFILE_WITH_SINGLES},
        "monitor": MONITOR_OPTION,
        "new_item_monitor": NEW_ITEM_OPTION,
        "tags": [{"id": lidarr_setup["tag_id"], "label": TAG_LABEL}],
        "source": "rootfolder",
        "singles_enabled": True,
    }
