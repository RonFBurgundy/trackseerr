"""Tests for MusicBrainz persistent caching, forced refreshes, MBID redirects, and worker stats."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.artist_refresh import refresh_single_artist
from plex_playlist_sync.artist_refresh_worker import (
    ArtistRefreshWorker,
    _LAST_REFRESH_PREFIX,
)
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.mb_metadata_store import MbMetadataStore
from plex_playlist_sync.models import LibraryAlbum, LibraryArtist
from plex_playlist_sync.storage import Database
from starlette.testclient import TestClient

pytestmark = pytest.mark.real_mbid_enricher


def _mock_response(status_code: int = 200, json_data: Any = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    if json_data is not None:
        import json
        resp._content = json.dumps(json_data).encode("utf-8")
        resp.headers["content-type"] = "application/json"
    else:
        resp._content = b""
    return resp


@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_cache.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture
def seeded_users(test_db: Database):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    user = test_db.upsert_user("user-1", "regular_user", "user@example.com", is_admin=False)
    return {"admin": admin, "user": user}


@pytest.fixture
def test_config(tmp_path: Path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def test_second_scheduled_refresh_skips_tracklists(test_db: Database):
    """Artist with mbid and 30 album release groups:

    First refresh: 1 discography page + 1 details + 30 track calls.
    Expire only discography row in db, new enricher on same db, refresh again -> 0 track calls.
    """
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-1"
    mbid = "artist-mbid-1"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 1",
            clean_name="Test Artist 1",
            mbid=mbid,
            monitored=True,
            monitor_option="all",
        )
    )

    rgs = [
        {
            "id": f"rg-{i}",
            "title": f"Album {i}",
            "primary-type": "Album",
            "secondary-types": [],
            "first-release-date": "2020-01-01",
        }
        for i in range(30)
    ]

    tracks_calls = []

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {
                "id": mbid,
                "name": "Test Artist 1",
                "country": "US",
                "disambiguation": "Rock band",
                "genres": ["Rock"],
            })
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            return _mock_response(200, {
                "release-groups": rgs,
                "release-group-count": 30,
            })
        if "/ws/2/release" in url and params and "release-group" in params:
            rg_id = params["release-group"]
            tracks_calls.append(rg_id)
            return _mock_response(200, {
                "releases": [
                    {
                        "id": f"rel-{rg_id}",
                        "title": f"Release {rg_id}",
                        "media": [
                            {
                                "position": 1,
                                "tracks": [
                                    {
                                        "number": "1",
                                        "title": f"Track 1 of {rg_id}",
                                        "length": 180000,
                                        "recording": {"id": f"rec-{rg_id}-1"},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            })
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        res1 = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert res1["success"] is True
        assert len(tracks_calls) == 30

    tracks_calls.clear()

    # Expire only the discography row in db
    with test_db._lock:
        test_db.conn.execute(
            "UPDATE mb_metadata_cache SET expires_at = '2000-01-01T00:00:00+00:00' WHERE kind = 'discography'"
        )
        test_db.conn.commit()

    # New enricher on the same db
    client2 = MbidEnricherClient(store=MbMetadataStore(test_db))
    with patch.object(client2._session, "get", side_effect=router):
        res2 = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client2,
            force=False,
        )
        assert res2["success"] is True
        assert len(tracks_calls) == 0


def test_new_release_group_fetches_only_its_tracks(test_db: Database):
    """Second discography has 31 groups (1 new) -> exactly 1 track call."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-2"
    mbid = "artist-mbid-2"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 2",
            clean_name="Test Artist 2",
            mbid=mbid,
            monitored=True,
            monitor_option="all",
        )
    )

    rgs_30 = [
        {
            "id": f"rg-{i}",
            "title": f"Album {i}",
            "primary-type": "Album",
            "secondary-types": [],
            "first-release-date": "2020-01-01",
        }
        for i in range(30)
    ]
    rgs_31 = list(rgs_30) + [
        {
            "id": "rg-new-31",
            "title": "Album 31",
            "primary-type": "Album",
            "secondary-types": [],
            "first-release-date": "2021-01-01",
        }
    ]

    tracks_calls = []

    def router_first(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {
                "id": mbid,
                "name": "Test Artist 2",
            })
        if "/ws/2/release-group" in url:
            return _mock_response(200, {
                "release-groups": rgs_30,
                "release-group-count": 30,
            })
        if "/ws/2/release" in url and params and "release-group" in params:
            rg_id = params["release-group"]
            tracks_calls.append(rg_id)
            return _mock_response(200, {
                "releases": [
                    {
                        "id": f"rel-{rg_id}",
                        "title": f"Release {rg_id}",
                        "media": [
                            {"position": 1, "tracks": [{"number": "1", "title": "Track 1"}]}
                        ],
                    }
                ]
            })
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router_first):
        refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )

    tracks_calls.clear()

    # Expire discography cache
    with test_db._lock:
        test_db.conn.execute(
            "UPDATE mb_metadata_cache SET expires_at = '2000-01-01T00:00:00+00:00' WHERE kind = 'discography'"
        )
        test_db.conn.commit()

    def router_second(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {
                "id": mbid,
                "name": "Test Artist 2",
            })
        if "/ws/2/release-group" in url:
            return _mock_response(200, {
                "release-groups": rgs_31,
                "release-group-count": 31,
            })
        if "/ws/2/release" in url and params and "release-group" in params:
            rg_id = params["release-group"]
            tracks_calls.append(rg_id)
            return _mock_response(200, {
                "releases": [
                    {
                        "id": f"rel-{rg_id}",
                        "title": f"Release {rg_id}",
                        "media": [
                            {"position": 1, "tracks": [{"number": "1", "title": "Track 1"}]}
                        ],
                    }
                ]
            })
        return _mock_response(404, {})

    client2 = MbidEnricherClient(store=MbMetadataStore(test_db))
    with patch.object(client2._session, "get", side_effect=router_second):
        res2 = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client2,
            force=False,
        )
        assert res2["success"] is True
        assert len(tracks_calls) == 1
        assert tracks_calls[0] == "rg-new-31"


def test_manual_refresh_forces_network(test_db: Database):
    """Warm cache, manual refresh (force=True) refetches track calls."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-3"
    mbid = "artist-mbid-3"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 3",
            clean_name="Test Artist 3",
            mbid=mbid,
            monitored=True,
            monitor_option="all",
        )
    )

    track_calls = []

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 3"})
        if "/ws/2/release-group" in url:
            return _mock_response(200, {
                "release-groups": [
                    {
                        "id": "rg-single",
                        "title": "Album Single",
                        "primary-type": "Album",
                        "secondary-types": [],
                        "first-release-date": "2020-01-01",
                    }
                ],
                "release-group-count": 1,
            })
        if "/ws/2/release" in url:
            track_calls.append("rg-single")
            return _mock_response(200, {
                "releases": [
                    {"id": "rel-single", "media": [{"tracks": [{"title": "Track 1"}]}]}
                ]
            })
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        # 1. Warm cache
        refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert len(track_calls) == 1

        # 2. Scheduled refresh with warm cache -> no track calls
        track_calls.clear()
        refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert len(track_calls) == 0

        # 3. Manual refresh (force=True) -> track calls happen again
        track_calls.clear()
        res_forced = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=True,
        )
        assert res_forced["success"] is True
        assert len(track_calls) == 1


def test_artist_redirect_updates_mbid(test_db: Database):
    """Details response id differs -> library_artists.mbid updated, redirect row recorded."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-4"
    old_mbid = "old-artist-mbid-4"
    new_mbid = "new-artist-mbid-4"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 4",
            clean_name="Test Artist 4",
            mbid=old_mbid,
            monitored=True,
        )
    )

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{old_mbid}" in url or f"/ws/2/artist/{new_mbid}" in url:
            return _mock_response(200, {
                "id": new_mbid,
                "name": "Test Artist 4 Merged",
                "country": "SE",
            })
        if "/ws/2/release-group" in url:
            return _mock_response(200, {"release-groups": [], "release-group-count": 0})
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        res = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert res["success"] is True

    updated_artist = test_db.get_library_artist(artist_id)
    assert updated_artist is not None
    assert updated_artist["mbid"] == new_mbid

    # Verify redirect row recorded in store
    resolved = store.resolve_redirect(old_mbid)
    assert resolved == new_mbid


def test_release_group_redirect_relinks_album(test_db: Database):
    """Stored rg id missing from complete discography, lookup returns new id -> album row updated."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-5"
    mbid = "artist-mbid-5"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 5",
            clean_name="Test Artist 5",
            mbid=mbid,
            monitored=True,
            monitor_option="all",
        )
    )

    old_rg = "rg-old-55"
    new_rg = "rg-new-55"
    album_id = "alb-5"
    test_db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id=artist_id,
            title="Stored Album 5",
            clean_title="Stored Album 5",
            mb_release_group_id=old_rg,
            monitored=True,
        )
    )

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 5"})
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            # Complete discography without new_rg so old_rg relinks to new_rg without collision
            return _mock_response(200, {
                "release-groups": [],
                "release-group-count": 0,
            })
        if f"/ws/2/release-group/{old_rg}" in url:
            # Merged release group returns new id
            return _mock_response(200, {"id": new_rg, "title": "Fresh Album Title"})
        if "/ws/2/release" in url:
            return _mock_response(200, {"releases": []})
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        res = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert res["success"] is True

    # Check album was updated
    alb = test_db.get_library_album(album_id)
    assert alb is not None
    assert alb["mb_release_group_id"] == new_rg


def test_no_redirect_lookups_when_incomplete(test_db: Database):
    """Page 2 fails -> discography_complete is False -> zero /ws/2/release-group/<id> calls."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-6"
    mbid = "artist-mbid-6"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 6",
            clean_name="Test Artist 6",
            mbid=mbid,
            monitored=True,
        )
    )

    old_rg = "rg-missing-6"
    test_db.upsert_library_album(
        LibraryAlbum(
            id="alb-6",
            artist_id=artist_id,
            title="Stored Album 6",
            clean_title="Stored Album 6",
            mb_release_group_id=old_rg,
            monitored=True,
        )
    )

    rg_resolve_calls = []

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 6"})
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            offset = params.get("offset", 0) if params else 0
            if offset == 0:
                # 100 items returned, but total count is 150 -> triggers page 2
                return _mock_response(200, {
                    "release-groups": [
                        {"id": f"rg-page1-{i}", "title": f"Album {i}"}
                        for i in range(100)
                    ],
                    "release-group-count": 150,
                })
            else:
                # Page 2 fails!
                return _mock_response(500, {})
        if f"/ws/2/release-group/{old_rg}" in url:
            rg_resolve_calls.append(old_rg)
            return _mock_response(200, {"id": "rg-new-6"})
        if "/ws/2/release" in url:
            return _mock_response(200, {"releases": []})
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        res = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert res["success"] is True

    assert len(rg_resolve_calls) == 0
    alb = test_db.get_library_album("alb-6")
    assert alb["mb_release_group_id"] == old_rg


def test_rg_lookup_404_leaves_album(test_db: Database):
    """Release group lookup returns 404 (None) -> album remains untouched."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-7"
    mbid = "artist-mbid-7"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 7",
            clean_name="Test Artist 7",
            mbid=mbid,
            monitored=True,
        )
    )

    old_rg = "rg-404"
    test_db.upsert_library_album(
        LibraryAlbum(
            id="alb-7",
            artist_id=artist_id,
            title="Album 7",
            clean_title="Album 7",
            mb_release_group_id=old_rg,
            monitored=True,
        )
    )

    def router(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 7"})
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            return _mock_response(200, {"release-groups": [], "release-group-count": 0})
        if f"/ws/2/release-group/{old_rg}" in url:
            return _mock_response(404, {})
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router):
        res = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )
        assert res["success"] is True

    alb = test_db.get_library_album("alb-7")
    assert alb["mb_release_group_id"] == old_rg


def test_shrinking_discography_keeps_albums(test_db: Database):
    """Second discography lacks 5 groups -> album count and monitored flags unchanged."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-8"
    mbid = "artist-mbid-8"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 8",
            clean_name="Test Artist 8",
            mbid=mbid,
            monitored=True,
            monitor_option="all",
        )
    )

    rgs_10 = [
        {"id": f"rg-keep-{i}", "title": f"Album {i}", "primary-type": "Album", "secondary-types": []}
        for i in range(10)
    ]
    rgs_5 = rgs_10[:5]

    def router_first(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 8"})
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            return _mock_response(200, {"release-groups": rgs_10, "release-group-count": 10})
        if "/ws/2/release" in url:
            return _mock_response(200, {"releases": []})
        return _mock_response(404, {})

    with patch.object(client._session, "get", side_effect=router_first):
        refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client,
            force=False,
        )

    albums_before = test_db.list_library_albums(artist_id=artist_id)
    assert len(albums_before) == 10
    assert all(a["monitored"] == 1 for a in albums_before)

    # Expire discography cache
    with test_db._lock:
        test_db.conn.execute(
            "UPDATE mb_metadata_cache SET expires_at = '2000-01-01T00:00:00+00:00' WHERE kind = 'discography'"
        )
        test_db.conn.commit()

    def router_second(url, params=None, timeout=None):
        if f"/ws/2/artist/{mbid}" in url:
            return _mock_response(200, {"id": mbid, "name": "Test Artist 8"})
        if "/ws/2/release-group" in url and (not params or params.get("artist") == mbid):
            return _mock_response(200, {"release-groups": rgs_5, "release-group-count": 5})
        if "/ws/2/release" in url:
            return _mock_response(200, {"releases": []})
        return _mock_response(404, {})

    client2 = MbidEnricherClient(store=MbMetadataStore(test_db))
    with patch.object(client2._session, "get", side_effect=router_second):
        res2 = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            discovery_client=mock_discovery,
            enricher=client2,
            force=False,
        )
        assert res2["success"] is True

    albums_after = test_db.list_library_albums(artist_id=artist_id)
    assert len(albums_after) == 10
    assert all(a["monitored"] == 1 for a in albums_after)


def test_source_unavailable_not_stamped_and_sweep_aborts(test_db: Database):
    """Enricher circuit breaker open -> no artist_refresh:last: kv rows written;

    result has aborted_source_unavailable.
    """
    import time
    from plex_playlist_sync.clients.mbid_enricher import _extract_host

    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(store=store)

    # Trip breaker on all usable hosts so source_available() returns False
    now = time.monotonic()
    for h in [client.base_url, "musicbrainz.org"]:
        breaker = client._get_breaker(_extract_host(h))
        with breaker._lock:
            breaker._state = "open"
            breaker._consecutive_failures = 10
            breaker._last_failure_time = now

    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    artist_id = "art-9"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist 9",
            clean_name="Test Artist 9",
            mbid="artist-mbid-9",
            monitored=True,
        )
    )

    worker = ArtistRefreshWorker()
    worker.pace_delay = 0.0
    res = worker.refresh_once(
        db=test_db,
        enricher=client,
        discovery_client=mock_discovery,
        artist_ids=[artist_id],
    )

    assert res.get("aborted_source_unavailable") is True
    assert res.get("remaining_artists") == 1

    # Check that no last-refresh marker was written
    kv_val = test_db.get_kv(_LAST_REFRESH_PREFIX + artist_id)
    assert kv_val is None


def test_manual_refresh_forces_deezer(test_db: Database):
    """Deezer album details are refetched on force=True."""
    mock_enricher = MagicMock(spec=MbidEnricherClient)
    mock_enricher.source_available.return_value = False  # Skip MusicBrainz discography
    mock_enricher.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}
    mock_discovery.get_artist_details.return_value = {
        "id": "dz-art-1",
        "albums": [{"id": "dz-alb-1", "title": "Deezer Album 1"}],
    }
    mock_discovery.get_album_details.return_value = {
        "id": "dz-alb-1",
        "title": "Deezer Album 1",
        "tracks": [{"id": "dz-trk-1", "title": "Deezer Track 1"}],
    }

    artist_id = "art-10"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Deezer Artist",
            clean_name="Deezer Artist",
            foreign_artist_id="dz-art-1",
            monitored=True,
            monitor_option="all",
        )
    )

    # 1. First refresh with force=False
    refresh_single_artist(
        artist_id=artist_id,
        db=test_db,
        discovery_client=mock_discovery,
        enricher=mock_enricher,
        force=False,
    )
    mock_discovery.get_album_details.assert_called_with("dz-alb-1", force=False)

    # 2. Second refresh with force=True
    refresh_single_artist(
        artist_id=artist_id,
        db=test_db,
        discovery_client=mock_discovery,
        enricher=mock_enricher,
        force=True,
    )
    mock_discovery.get_album_details.assert_called_with("dz-alb-1", force=True)


def test_refresh_once_reports_stats_and_prunes(test_db: Database):
    """worker refresh_once result has network_requests, cache_hits, cache_pruned;

    expired row is removed.
    """
    store = MbMetadataStore(test_db)
    # Insert an expired row
    with test_db._lock:
        test_db.conn.execute(
            """INSERT INTO mb_metadata_cache (cache_key, kind, payload_json, fetched_at, expires_at)
               VALUES ('test:expired', 'test', '{}', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"""
        )
        test_db.conn.commit()

    client = MbidEnricherClient(store=store)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.stats.return_value = {"network_requests": 2, "cache_hits": 5}

    worker = ArtistRefreshWorker()
    worker.pace_delay = 0.0

    res = worker.refresh_once(
        db=test_db,
        discovery_client=mock_discovery,
        enricher=client,
        artist_ids=[],
    )

    assert res["success"] is True
    assert "network_requests" in res
    assert "cache_hits" in res
    assert "cache_pruned" in res
    assert res["cache_pruned"] >= 1

    # Check row is gone
    hit, _ = store.get("test:expired")
    assert hit is False


def test_discography_fetch_uses_result_with_force(test_db: Database):
    artist_id = "art-test-force"
    mbid = "artist-mbid-test-force"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Artist Force",
            clean_name="Test Artist Force",
            mbid=mbid,
            monitored=True,
        )
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": mbid}
    enricher.get_artist_discography_result.return_value = ([], False)
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.search_artist.return_value = None
    mock_discovery.search.return_value = []
    mock_discovery.stats.return_value = {"network_requests": 0, "cache_hits": 0}

    res = refresh_single_artist(
        artist_id=artist_id,
        db=test_db,
        discovery_client=mock_discovery,
        enricher=enricher,
        force=True,
    )
    assert res["success"] is True

    calls = enricher.get_artist_discography_result.call_args_list
    assert calls[0] == call(mbid, force=True)
    assert all(c == call(mbid, force=False) for c in calls[1:])
    enricher.get_artist_discography.assert_not_called()

