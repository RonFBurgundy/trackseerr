"""Comprehensive test suite for the Release Calendar and iCal feed.

Covers:
1. Range validation, date formatting, and max-range 120 days constraint (400 Bad Request).
2. Permission guards: non-admin gets 403, unauthenticated gets 401, admin gets 200.
3. Native mode: SQL range filtering, unmonitored filtering, cover art versioning, and status derivation:
   - downloaded (track_file_count >= total_tracks)
   - partial (0 < track_file_count < total_tracks)
   - missing (0 tracks, release_date <= today)
   - upcoming (0 tracks, release_date > today)
4. Lidarr mode: live snapshot fetching, status derivation, range and unmonitored filtering.
5. RFC 5545 iCal feed generation (/api/calendar/feed.ics):
   - Authentication (feed token via query/header/bearer, admin session, 401/403 guards)
   - Content-Type (text/calendar; charset=utf-8)
   - VCALENDAR / VEVENT structure, stable UIDs (album-<id>@trackseerr), all-day DTSTART/DTEND
   - RFC 5545 escaping (commas, semicolons, backslashes, newlines)
   - Line folding at 75 octets with CRLF
6. Artist refresh retention: future releases from MusicBrainz are saved to library_albums with
   future release date and monitored per artist monitor option, appearing on the calendar as upcoming.
"""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from plex_playlist_sync import lidarr_library
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.api.routes.library import refresh_single_artist
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from plex_playlist_sync.storage import Database

FEED_TOKEN = "test-feed-token-secret-xyz"
LIDARR_API_KEY = "lidarr-key-123456"


@pytest.fixture(autouse=True)
def _clear_lidarr_cache():
    """Ensure lidarr_library cache is clear between tests."""
    lidarr_library.invalidate()
    yield
    lidarr_library.invalidate()


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_calendar.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    """Provides a test Config with feed_token set."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        feed_token=FEED_TOKEN,
    )


@pytest.fixture
def seeded_users(test_db: Database):
    """Seeds admin and non-admin users."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    return {"admin": admin, "alice": alice}


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer Authorization header."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(seeded_users, test_db, test_config):
    return _auth_headers(seeded_users["admin"], test_db, test_config)


@pytest.fixture
def alice_headers(seeded_users, test_db, test_config):
    return _auth_headers(seeded_users["alice"], test_db, test_config)


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a FastAPI test client."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


# =========================================================================
# 1. Validation & Constraints Tests
# =========================================================================

class TestCalendarValidation:
    def test_missing_query_parameters_returns_422(self, app_and_client, admin_headers):
        _, client = app_and_client
        # Missing start and end
        assert client.get("/api/calendar", headers=admin_headers).status_code == 422
        # Missing end
        assert client.get("/api/calendar?start=2026-10-01", headers=admin_headers).status_code == 422
        # Missing start
        assert client.get("/api/calendar?end=2026-10-31", headers=admin_headers).status_code == 422

    def test_invalid_date_format_returns_400(self, app_and_client, admin_headers):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026/10/01&end=2026-10-31", headers=admin_headers)
        assert resp.status_code == 400
        assert "Invalid start date" in resp.json()["detail"]

        resp = client.get("/api/calendar?start=2026-10-01&end=invalid", headers=admin_headers)
        assert resp.status_code == 400
        assert "Invalid end date" in resp.json()["detail"]

    def test_end_before_start_returns_400(self, app_and_client, admin_headers):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026-10-20&end=2026-10-10", headers=admin_headers)
        assert resp.status_code == 400
        assert "end date must be on or after start date" in resp.json()["detail"]

    def test_range_exceeding_120_days_returns_400(self, app_and_client, admin_headers):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026-01-01&end=2026-05-15", headers=admin_headers)
        assert resp.status_code == 400
        assert "120 days" in resp.json()["detail"]

    def test_range_at_120_days_is_accepted(self, app_and_client, admin_headers):
        _, client = app_and_client
        # 120 days: 2026-01-01 to 2026-05-01 is 120 days
        resp = client.get("/api/calendar?start=2026-01-01&end=2026-05-01", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []


# =========================================================================
# 2. Permissions & Security Tests
# =========================================================================

class TestCalendarPermissions:
    def test_unauthenticated_request_returns_401(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026-10-01&end=2026-10-31")
        assert resp.status_code == 401

    def test_non_admin_user_returns_403(self, app_and_client, alice_headers):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026-10-01&end=2026-10-31", headers=alice_headers)
        assert resp.status_code == 403

    def test_admin_user_returns_200(self, app_and_client, admin_headers):
        _, client = app_and_client
        resp = client.get("/api/calendar?start=2026-10-01&end=2026-10-31", headers=admin_headers)
        assert resp.status_code == 200


# =========================================================================
# 3. Native Mode Tests: Range, Unmonitored, and Status Derivation
# =========================================================================

class TestCalendarNativeMode:
    def _seed_data(self, test_db: Database):
        today = datetime.now(timezone.utc).date()
        past_date = (today - timedelta(days=10)).isoformat()
        future_date = (today + timedelta(days=10)).isoformat()
        far_future = (today + timedelta(days=60)).isoformat()

        # Artist 1: Monitored
        test_db.upsert_library_artist(
            LibraryArtist(
                id="art-1",
                name="Radiohead",
                clean_name="radiohead",
                monitored=True,
            )
        )
        # Artist 2: Unmonitored
        test_db.upsert_library_artist(
            LibraryArtist(
                id="art-2",
                name="Coldplay",
                clean_name="coldplay",
                monitored=False,
            )
        )

        # Album 1: Monitored, Artist Monitored, Past date, Downloaded (1 track, 1 file)
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-downloaded",
                artist_id="art-1",
                title="OK Computer",
                clean_title="ok computer",
                release_date=past_date,
                monitored=True,
                total_tracks=1,
            )
        )
        test_db.upsert_library_track(
            LibraryTrack(
                id="trk-1",
                album_id="alb-downloaded",
                artist_id="art-1",
                title="Airbag",
                clean_title="airbag",
                track_number=1,
            )
        )
        test_db.upsert_library_file(
            LibraryFile(
                id="fil-1",
                track_id="trk-1",
                file_path="/music/Radiohead/OK Computer/01 - Airbag.flac",
                relative_path="Radiohead/OK Computer/01 - Airbag.flac",
            )
        )

        # Album 2: Monitored, Artist Monitored, Past date, Partial (2 tracks, 1 file)
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-partial",
                artist_id="art-1",
                title="Kid A",
                clean_title="kid a",
                release_date=past_date,
                monitored=True,
                total_tracks=2,
            )
        )
        test_db.upsert_library_track(
            LibraryTrack(
                id="trk-2",
                album_id="alb-partial",
                artist_id="art-1",
                title="Everything in Its Right Place",
                clean_title="everything in its right place",
                track_number=1,
            )
        )
        test_db.upsert_library_track(
            LibraryTrack(
                id="trk-3",
                album_id="alb-partial",
                artist_id="art-1",
                title="Kid A",
                clean_title="kid a",
                track_number=2,
            )
        )
        test_db.upsert_library_file(
            LibraryFile(
                id="fil-2",
                track_id="trk-2",
                file_path="/music/Radiohead/Kid A/01 - Everything.flac",
                relative_path="Radiohead/Kid A/01 - Everything.flac",
            )
        )

        # Album 3: Monitored, Artist Monitored, Past date, Missing (0 files)
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-missing",
                artist_id="art-1",
                title="Amnesiac",
                clean_title="amnesiac",
                release_date=past_date,
                monitored=True,
                total_tracks=1,
            )
        )

        # Album 4: Monitored, Artist Monitored, Future date, Upcoming (0 files)
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-upcoming",
                artist_id="art-1",
                title="LP 10",
                clean_title="lp 10",
                release_date=future_date,
                monitored=True,
                total_tracks=10,
            )
        )

        # Album 5: Monitored, Artist Monitored, Far Future (out of default range)
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-far-future",
                artist_id="art-1",
                title="LP 11",
                clean_title="lp 11",
                release_date=far_future,
                monitored=True,
                total_tracks=10,
            )
        )

        # Album 6: Unmonitored Album on Monitored Artist, in range
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-unmonitored-album",
                artist_id="art-1",
                title="B-Sides",
                clean_title="b-sides",
                release_date=past_date,
                monitored=False,
                total_tracks=5,
            )
        )

        # Album 7: Monitored Album on Unmonitored Artist, in range
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-unmonitored-artist",
                artist_id="art-2",
                title="Parachutes",
                clean_title="parachutes",
                release_date=past_date,
                monitored=True,
                total_tracks=10,
            )
        )

        return {
            "today": today,
            "past_date": past_date,
            "future_date": future_date,
            "far_future": far_future,
        }

    def test_status_derivation_and_range_filtering(self, app_and_client, test_db, admin_headers):
        dates = self._seed_data(test_db)
        _, client = app_and_client

        start = (dates["today"] - timedelta(days=20)).isoformat()
        end = (dates["today"] + timedelta(days=20)).isoformat()

        # By default unmonitored=false
        resp = client.get(f"/api/calendar?start={start}&end={end}", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()

        # Should include alb-downloaded, alb-partial, alb-missing, alb-upcoming
        # Should NOT include alb-far-future (outside range)
        # Should NOT include alb-unmonitored-album or alb-unmonitored-artist
        ids = {item["id"]: item for item in items}
        assert "alb-downloaded" in ids
        assert "alb-partial" in ids
        assert "alb-missing" in ids
        assert "alb-upcoming" in ids
        assert "alb-far-future" not in ids
        assert "alb-unmonitored-album" not in ids
        assert "alb-unmonitored-artist" not in ids

        # Validate status derivations
        assert ids["alb-downloaded"]["status"] == "downloaded"
        assert ids["alb-downloaded"]["track_file_count"] == 1
        assert ids["alb-downloaded"]["total_track_count"] == 1

        assert ids["alb-partial"]["status"] == "partial"
        assert ids["alb-partial"]["track_file_count"] == 1
        assert ids["alb-partial"]["total_track_count"] == 2

        assert ids["alb-missing"]["status"] == "missing"
        assert ids["alb-missing"]["track_file_count"] == 0

        assert ids["alb-upcoming"]["status"] == "upcoming"
        assert ids["alb-upcoming"]["track_file_count"] == 0

    def test_unmonitored_toggle(self, app_and_client, test_db, admin_headers):
        dates = self._seed_data(test_db)
        _, client = app_and_client

        start = (dates["today"] - timedelta(days=20)).isoformat()
        end = (dates["today"] + timedelta(days=20)).isoformat()

        # With unmonitored=true, unmonitored album and unmonitored artist albums appear
        resp = client.get(f"/api/calendar?start={start}&end={end}&unmonitored=true", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()
        ids = {item["id"]: item for item in items}

        assert "alb-unmonitored-album" in ids
        assert ids["alb-unmonitored-album"]["monitored"] is False
        assert ids["alb-unmonitored-album"]["artist_monitored"] is True

        assert "alb-unmonitored-artist" in ids
        assert ids["alb-unmonitored-artist"]["monitored"] is True
        assert ids["alb-unmonitored-artist"]["artist_monitored"] is False

    def test_year_only_release_date_matching(self, app_and_client, test_db, admin_headers):
        _, client = app_and_client
        test_db.upsert_library_artist(
            LibraryArtist(id="art-y", name="Year Band", clean_name="year band", monitored=True)
        )
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-y2026",
                artist_id="art-y",
                title="Year 2026 Album",
                clean_title="year 2026 album",
                release_date="2026",
                monitored=True,
                total_tracks=10,
            )
        )
        # When querying within 2026, it should match
        resp = client.get("/api/calendar?start=2026-06-01&end=2026-07-01", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()
        assert any(i["id"] == "alb-y2026" for i in items)

        # When querying outside 2026, it should NOT match
        resp = client.get("/api/calendar?start=2025-01-01&end=2025-02-01", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()
        assert not any(i["id"] == "alb-y2026" for i in items)


# =========================================================================
# 4. Lidarr Mode Tests
# =========================================================================

class TestCalendarLidarrMode:
    def test_lidarr_mode_range_and_status(self, app_and_client, test_db, admin_headers):
        _, client = app_and_client
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": LIDARR_API_KEY})
        test_db.update_media_management_settings({"library_mode": "lidarr"})

        today = datetime.now(timezone.utc).date()
        past_date = (today - timedelta(days=5)).isoformat() + "T00:00:00Z"
        future_date = (today + timedelta(days=5)).isoformat() + "T00:00:00Z"
        out_date = (today + timedelta(days=40)).isoformat() + "T00:00:00Z"

        lidarr_artists = [
            {"id": 1, "artistName": "Daft Punk", "monitored": True},
            {"id": 2, "artistName": "Air", "monitored": False},
        ]
        lidarr_albums = [
            {
                "id": 101,
                "title": "Discovery",
                "artistId": 1,
                "artist": {"id": 1, "artistName": "Daft Punk"},
                "releaseDate": past_date,
                "monitored": True,
                "albumType": "Album",
                "statistics": {"trackFileCount": 14, "totalTrackCount": 14},
                "images": [{"coverType": "cover", "url": "/MediaCover/Albums/101/cover.jpg"}],
            },
            {
                "id": 102,
                "title": "Human After All",
                "artistId": 1,
                "artist": {"id": 1, "artistName": "Daft Punk"},
                "releaseDate": past_date,
                "monitored": True,
                "albumType": "Album",
                "statistics": {"trackFileCount": 3, "totalTrackCount": 10},
                "images": [],
            },
            {
                "id": 103,
                "title": "Future Funk",
                "artistId": 1,
                "artist": {"id": 1, "artistName": "Daft Punk"},
                "releaseDate": future_date,
                "monitored": True,
                "albumType": "Album",
                "statistics": {"trackFileCount": 0, "totalTrackCount": 8},
                "images": [],
            },
            {
                "id": 104,
                "title": "Unmonitored Moon",
                "artistId": 2,
                "artist": {"id": 2, "artistName": "Air"},
                "releaseDate": past_date,
                "monitored": False,
                "albumType": "Album",
                "statistics": {"trackFileCount": 0, "totalTrackCount": 10},
                "images": [],
            },
            {
                "id": 105,
                "title": "Far Release",
                "artistId": 1,
                "artist": {"id": 1, "artistName": "Daft Punk"},
                "releaseDate": out_date,
                "monitored": True,
                "albumType": "Album",
                "statistics": {"trackFileCount": 0, "totalTrackCount": 5},
                "images": [],
            },
        ]

        def fake_snapshot(entity: str, _client: Any):
            if entity == "artists":
                return lidarr_artists
            if entity == "albums":
                return lidarr_albums
            return []

        start = (today - timedelta(days=10)).isoformat()
        end = (today + timedelta(days=10)).isoformat()

        with patch("plex_playlist_sync.lidarr_library.snapshot", side_effect=fake_snapshot):
            # 1. unmonitored=false
            resp = client.get(f"/api/calendar?start={start}&end={end}", headers=admin_headers)
            assert resp.status_code == 200
            items = resp.json()
            ids = {str(item["id"]): item for item in items}

            assert "101" in ids
            assert ids["101"]["status"] == "downloaded"
            assert ids["101"]["cover_url"] == "/api/lidarr/MediaCover/Albums/101/cover.jpg"

            assert "102" in ids
            assert ids["102"]["status"] == "partial"

            assert "103" in ids
            assert ids["103"]["status"] == "upcoming"

            # 104 is unmonitored, should not appear
            assert "104" not in ids
            # 105 is out of range, should not appear
            assert "105" not in ids

            # 2. unmonitored=true
            resp_all = client.get(
                f"/api/calendar?start={start}&end={end}&unmonitored=true", headers=admin_headers
            )
            assert resp_all.status_code == 200
            all_ids = {str(item["id"]): item for item in resp_all.json()}
            assert "104" in all_ids
            assert all_ids["104"]["status"] == "missing"


# =========================================================================
# 5. RFC 5545 iCal Feed Tests
# =========================================================================

class TestCalendarIcsFeed:
    def test_feed_auth_mechanisms(self, app_and_client, test_db, admin_headers, alice_headers):
        _, client = app_and_client

        # 1. Unauthenticated -> 401
        assert client.get("/api/calendar/feed.ics").status_code == 401

        # 2. Invalid token query param -> 401
        assert client.get("/api/calendar/feed.ics?token=wrong-token").status_code == 401

        # 3. Valid token query param -> 200
        resp = client.get(f"/api/calendar/feed.ics?token={FEED_TOKEN}")
        assert resp.status_code == 200
        assert "text/calendar" in resp.headers["content-type"]

        # 4. Valid X-Api-Key -> 200
        resp = client.get("/api/calendar/feed.ics", headers={"X-Api-Key": FEED_TOKEN})
        assert resp.status_code == 200

        # 5. Valid Bearer feed token -> 200
        resp = client.get("/api/calendar/feed.ics", headers={"Authorization": f"Bearer {FEED_TOKEN}"})
        assert resp.status_code == 200

        # 6. Admin session header -> 200
        resp = client.get("/api/calendar/feed.ics", headers=admin_headers)
        assert resp.status_code == 200

        # 7. Non-admin session header -> 403
        resp = client.get("/api/calendar/feed.ics", headers=alice_headers)
        assert resp.status_code == 403

    def test_feed_rfc5545_structure_escaping_and_folding(self, app_and_client, test_db):
        _, client = app_and_client

        # Seed an album with characters needing RFC 5545 escaping: commas, semicolons, backslashes, newlines
        test_db.upsert_library_artist(
            LibraryArtist(
                id="art-special",
                name="Special, Artist; with \\ slash",
                clean_name="special artist",
                monitored=True,
            )
        )
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-special-1",
                artist_id="art-special",
                title="A Very Long Album Title: Commas, Semicolons; And Extra Text That Easily Exceeds Seventy-Five Octets For Testing Line Folding",
                clean_title="a very long album title",
                release_date="2026-11-15",
                monitored=True,
                total_tracks=12,
            )
        )

        resp = client.get(f"/api/calendar/feed.ics?token={FEED_TOKEN}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/calendar; charset=utf-8"

        body = resp.text
        # Line endings must be CRLF
        assert "\r\n" in body

        # Header fields
        assert "BEGIN:VCALENDAR\r\n" in body
        assert "VERSION:2.0\r\n" in body
        assert "PRODID:-//Trackseerr//Release Calendar//EN\r\n" in body
        assert "CALSCALE:GREGORIAN\r\n" in body
        assert "METHOD:PUBLISH\r\n" in body
        assert "X-WR-CALNAME:Trackseerr Releases\r\n" in body
        assert "END:VCALENDAR\r\n" in body

        # Event fields
        assert "BEGIN:VEVENT\r\n" in body
        assert "UID:album-alb-special-1@trackseerr\r\n" in body
        assert "DTSTART;VALUE=DATE:20261115\r\n" in body
        assert "DTEND;VALUE=DATE:20261116\r\n" in body
        assert "END:VEVENT\r\n" in body

        # Check line length constraint: RFC 5545 requires lines <= 75 octets (excluding CRLF)
        for line in body.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75, f"Line exceeds 75 octets: {line}"

        # Unfolded content check: characters must be properly escaped
        unfolded = body.replace("\r\n ", "")
        assert "Special\\, Artist\\; with \\\\ slash" in unfolded
        assert "Commas\\, Semicolons\\;" in unfolded

    def test_feed_unmonitored_filter(self, app_and_client, test_db):
        _, client = app_and_client
        test_db.upsert_library_artist(
            LibraryArtist(id="art-m", name="Monitored Artist", clean_name="mon", monitored=True)
        )
        test_db.upsert_library_artist(
            LibraryArtist(id="art-u", name="Unmonitored Artist", clean_name="unmon", monitored=False)
        )
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-m",
                artist_id="art-m",
                title="Mon Album",
                clean_title="mon album",
                release_date="2026-10-01",
                monitored=True,
            )
        )
        test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-u",
                artist_id="art-u",
                title="Unmon Album",
                clean_title="unmon album",
                release_date="2026-10-02",
                monitored=False,
            )
        )

        # Default unmonitored=false: alb-u not present
        resp = client.get(f"/api/calendar/feed.ics?token={FEED_TOKEN}")
        assert "UID:album-alb-m@trackseerr" in resp.text
        assert "UID:album-alb-u@trackseerr" not in resp.text

        # unmonitored=true: alb-u present
        resp_all = client.get(f"/api/calendar/feed.ics?token={FEED_TOKEN}&unmonitored=true")
        assert "UID:album-alb-m@trackseerr" in resp_all.text
        assert "UID:album-alb-u@trackseerr" in resp_all.text


# =========================================================================
# 6. Future Release Retention in Artist Refresh
# =========================================================================

class TestCalendarArtistRefreshRetention:
    def test_future_release_retained_and_surfaced_on_calendar(
        self, app_and_client, test_db, admin_headers
    ):
        """Validates that artist refresh does not drop future releases from MusicBrainz,

        monitors them according to the artist monitor option, and surfaces them as upcoming.
        """
        _, client = app_and_client

        # 1. Create artist with monitor_option="all"
        artist_id = "art-future-band"
        test_db.upsert_library_artist(
            LibraryArtist(
                id=artist_id,
                name="The Futurists",
                clean_name="the futurists",
                mbid="mbid-futurists",
                monitored=True,
                monitor_option="all",
            )
        )

        # 2. Mock enricher returns an album with a future release date
        future_date = (datetime.now(timezone.utc).date() + timedelta(days=25)).isoformat()
        mock_enricher = MagicMock(spec=MbidEnricherClient)
        mock_enricher.get_artist_details.return_value = {
            "id": "mbid-futurists",
            "country": "US",
            "genres": ["Synthwave"],
            "bio": "Electronic band",
        }
        mock_enricher.get_artist_discography.return_value = [
            {
                "id": "rg-future-lp",
                "title": "Neon Tomorrow",
                "album_type": "album",
                "release_date": future_date,
                "year": int(future_date[:4]),
                "cover_url": "https://example.com/cover.jpg",
                "track_count": 8,
            }
        ]
        mock_enricher.get_release_group_tracks.return_value = []

        with patch("plex_playlist_sync.mediacover.mediacover_service.ensure_artwork", return_value=None):
            res = refresh_single_artist(
                artist_id=artist_id,
                db=test_db,
                enricher=mock_enricher,
            )
            assert res.get("success") is True

        # 3. Verify album was saved in DB with future release_date and monitored=True
        saved_alb = test_db.get_library_album_by_release_group_id("rg-future-lp")
        assert saved_alb is not None
        assert saved_alb["release_date"] == future_date
        assert saved_alb["monitored"] == 1

        # 4. Query calendar API for the window covering future_date
        start = (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat()
        end = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
        resp = client.get(f"/api/calendar?start={start}&end={end}", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        item = items[0]
        assert item["title"] == "Neon Tomorrow"
        assert item["artist_name"] == "The Futurists"
        assert item["release_date"] == future_date
        assert item["status"] == "upcoming"
        assert item["monitored"] is True
