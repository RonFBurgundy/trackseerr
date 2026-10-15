"""Tests for request-catalog-seed: approved requests become monitored catalog items."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
    MusicRequest,
    RequestStatus,
)
from trackseerr.request_catalog import (
    seed_pending_request_catalog,
    seed_request_catalog,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated Database instance configured for native mode."""
    db = Database(str(tmp_path / "test_req_catalog.db"))
    db.conn.execute(
        "INSERT OR IGNORE INTO download_clients (id, name, driver_type, host_url, enabled, priority) "
        "VALUES ('dl-test', 'Client', 'qbittorrent', 'http://127.0.0.1:8080', 1, 1)"
    )
    db.conn.execute(
        "INSERT OR IGNORE INTO indexers (id, name, indexer_type, host_url, categories, enabled, priority) "
        "VALUES ('idx-test', 'Indexer', 'torznab', 'http://127.0.0.1:9696', '3000', 1, 1)"
    )
    db.upsert_user("user-1", "testuser", "test@example.com", is_admin=True)
    db.conn.commit()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def mock_discovery():
    """Mock DiscoveryClient returning Daft Punk discography and track details."""
    client = MagicMock(spec=DiscoveryClient)

    def fake_get_artist_details(artist_id: str, force: bool = False) -> dict[str, Any]:
        return {
            "id": artist_id,
            "name": "Daft Punk",
            "albums": [
                {
                    "id": "deezer:album:302127",
                    "title": "Discovery",
                    "release_date": "2001-03-12",
                    "record_type": "album",
                },
                {
                    "id": "deezer:album:302128",
                    "title": "Random Access Memories",
                    "release_date": "2013-05-17",
                    "record_type": "album",
                },
            ],
            "singles_eps": [],
            "compilations": [],
        }

    def fake_get_album_details(album_id: str, force: bool = False) -> dict[str, Any] | None:
        if album_id == "deezer:album:302127":
            return {
                "id": "deezer:album:302127",
                "title": "Discovery",
                "artist": "Daft Punk",
                "artist_id": "deezer:artist:27",
                "tracks": [
                    {
                        "id": "deezer:track:3135553",
                        "title": "One More Time",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 320.0,
                    },
                    {
                        "id": "deezer:track:3135554",
                        "title": "Aerodynamic",
                        "track_number": 2,
                        "disc_number": 1,
                        "duration_seconds": 212.0,
                    },
                ],
            }
        if album_id == "deezer:album:302128":
            return {
                "id": "deezer:album:302128",
                "title": "Random Access Memories",
                "artist": "Daft Punk",
                "artist_id": "deezer:artist:27",
                "tracks": [
                    {
                        "id": "deezer:track:3135560",
                        "title": "Give Life Back to Music",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 275.0,
                    },
                    {
                        "id": "deezer:track:3135561",
                        "title": "Get Lucky",
                        "track_number": 8,
                        "disc_number": 1,
                        "duration_seconds": 369.0,
                    },
                ],
            }
        return None

    def fake_get_track_details(track_id: str) -> dict[str, Any] | None:
        if track_id == "deezer:track:3135553":
            return {
                "id": "deezer:track:3135553",
                "title": "One More Time",
                "artist": "Daft Punk",
                "album": "Discovery",
                "artist_discovery_id": "deezer:artist:27",
                "album_discovery_id": "deezer:album:302127",
            }
        return None

    def fake_search_artist(name: str) -> dict[str, Any] | None:
        if "daft" in name.lower():
            return {
                "id": "deezer:artist:27",
                "name": "Daft Punk",
            }
        return None

    client.get_artist_details.side_effect = fake_get_artist_details
    client.get_album_details.side_effect = fake_get_album_details
    client.get_track_details.side_effect = fake_get_track_details
    client.search_artist.side_effect = fake_search_artist
    return client


@pytest.fixture
def mock_enricher():
    """Mock MbidEnricherClient."""
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.source_available.return_value = False
    enricher.stats.return_value = {"network_requests": 0, "cache_hits": 0}
    enricher.lookup_artist_mbid.return_value = ("mbid-daft", 1.0, "deezer:artist:27")
    enricher.get_artist_details.return_value = {"id": "mbid-daft", "name": "Daft Punk"}
    return enricher


def _create_req(
    db: Database,
    *,
    req_id: str,
    artist: str,
    title: str,
    item_type: str = "track",
    album: str | None = None,
    foreign_id: str | None = None,
    status: RequestStatus = RequestStatus.PROCESSING,
) -> dict[str, Any]:
    return db.create_request(
        MusicRequest(
            id=req_id,
            user_id="user-1",
            item_type=item_type,
            title=title,
            artist=artist,
            album=album,
            foreign_id=foreign_id,
            status=status,
        )
    )


def test_track_request_new_artist_monitors_only_that_track(test_db: Database, mock_discovery, mock_enricher) -> None:
    req = _create_req(
        test_db,
        req_id="req-1",
        artist="Daft Punk",
        title="One More Time",
        album="Discovery",
        foreign_id="deezer:track:3135553",
        item_type="track",
    )

    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "seeded"

    # Verify request row has catalog metadata
    row = test_db.conn.execute(
        "SELECT catalog_track_id, catalog_album_id, catalog_seeded_at, catalog_seed_status FROM music_requests WHERE id = ?",
        ("req-1",),
    ).fetchone()
    assert row[3] == "seeded"
    assert row[2] is not None
    assert row[0] is not None  # catalog_track_id
    assert row[1] is not None  # catalog_album_id

    # Verify artist is monitored
    artist = test_db.get_library_artist_by_name("Daft Punk")
    assert artist is not None
    assert artist["monitored"] == 1

    # Verify requested track and its album are monitored
    track = test_db.get_library_track(row[0])
    assert track is not None
    assert track["monitored"] == 1
    assert track["title"] == "One More Time"

    album = test_db.get_library_album(row[1])
    assert album is not None
    assert album["monitored"] == 1
    assert album["title"] == "Discovery"

    # Verify other track on Discovery is NOT monitored
    aerodynamic = test_db.get_library_track_by_title(album["id"], "Aerodynamic")
    assert aerodynamic is not None
    assert aerodynamic["monitored"] == 0

    # Verify other album is NOT monitored
    ram = test_db.get_library_album_by_title(artist["id"], "Random Access Memories")
    assert ram is not None
    assert ram["monitored"] == 0

    # Verify tracks on other album are NOT monitored
    ram_tracks = test_db.list_library_tracks(album_id=ram["id"])
    assert all(t["monitored"] == 0 for t in ram_tracks)


def test_track_request_counts_in_wanted_missing(test_db: Database, mock_discovery, mock_enricher) -> None:
    stats_before = test_db.get_library_stats()
    assert stats_before["missing_track_count"] == 0

    req = _create_req(
        test_db,
        req_id="req-missing",
        artist="Daft Punk",
        title="One More Time",
        album="Discovery",
        foreign_id="deezer:track:3135553",
        item_type="track",
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "seeded"

    stats_after = test_db.get_library_stats()
    assert stats_after["missing_track_count"] == 1

    wanted_tracks, total = test_db.list_wanted("missing", 1, 50, "title", "asc")
    assert total == 1
    assert len(wanted_tracks) == 1
    assert wanted_tracks[0]["title"] == "One More Time"


def test_album_request_monitors_album_and_all_tracks(test_db: Database, mock_discovery, mock_enricher) -> None:
    req = _create_req(
        test_db,
        req_id="req-album",
        artist="Daft Punk",
        title="Discovery",
        album="Discovery",
        foreign_id="deezer:album:302127",
        item_type="album",
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "seeded"

    row = test_db.conn.execute(
        "SELECT catalog_track_id, catalog_album_id, catalog_seeded_at, catalog_seed_status FROM music_requests WHERE id = ?",
        ("req-album",),
    ).fetchone()
    assert row[3] == "seeded"
    assert row[1] is not None
    assert row[0] is None  # track_id is None for album request

    # Discovery has 2 tracks; both should be monitored
    album = test_db.get_library_album(row[1])
    assert album["monitored"] == 1
    tracks = test_db.list_library_tracks(album_id=album["id"])
    assert len(tracks) == 2
    assert all(t["monitored"] == 1 for t in tracks)

    # Missing track count should be 2
    stats = test_db.get_library_stats()
    assert stats["missing_track_count"] == 2

    # RAM should still be unmonitored
    artist = test_db.get_library_artist_by_name("Daft Punk")
    ram = test_db.get_library_album_by_title(artist["id"], "Random Access Memories")
    assert ram["monitored"] == 0


def test_album_request_existing_unmonitored_artist(test_db: Database, mock_discovery, mock_enricher) -> None:
    # Pre-create artist and two albums with monitored=0
    artist = test_db.upsert_library_artist(
        LibraryArtist(
            id="art-existing",
            name="Daft Punk",
            clean_name="daft punk",
            monitored=False,
            path="/music/Daft Punk",
        )
    )
    alb1 = test_db.upsert_library_album(
        LibraryAlbum(
            id="alb-1",
            artist_id="art-existing",
            title="Discovery",
            clean_title="discovery",
            monitored=False,
            foreign_album_id="deezer:album:302127",
        )
    )
    alb2 = test_db.upsert_library_album(
        LibraryAlbum(
            id="alb-2",
            artist_id="art-existing",
            title="Homework",
            clean_title="homework",
            monitored=False,
            foreign_album_id="deezer:album:99999",
        )
    )
    test_db.upsert_library_track(
        LibraryTrack(
            id="trk-1",
            artist_id="art-existing",
            album_id="alb-1",
            title="One More Time",
            clean_title="one more time",
            monitored=False,
        )
    )
    test_db.upsert_library_track(
        LibraryTrack(
            id="trk-2",
            artist_id="art-existing",
            album_id="alb-2",
            title="Da Funk",
            clean_title="da funk",
            monitored=False,
        )
    )

    req = _create_req(
        test_db,
        req_id="req-existing-art",
        artist="Daft Punk",
        title="Discovery",
        album="Discovery",
        foreign_id="deezer:album:302127",
        item_type="album",
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "seeded"

    # Artist must be monitored now
    assert test_db.get_library_artist("art-existing")["monitored"] == 1
    # Album 1 (Discovery) must be monitored and its track monitored
    assert test_db.get_library_album("alb-1")["monitored"] == 1
    assert test_db.get_library_track("trk-1")["monitored"] == 1
    # Album 2 (Homework) must still be UNMONITORED
    assert test_db.get_library_album("alb-2")["monitored"] == 0
    assert test_db.get_library_track("trk-2")["monitored"] == 0


def test_unmatched_track_records_unmatched(test_db: Database, mock_discovery, mock_enricher) -> None:
    req = _create_req(
        test_db,
        req_id="req-unmatched",
        artist="Daft Punk",
        title="Song That Does Not Exist",
        album="Discovery",
        foreign_id="deezer:track:99999999",
        item_type="track",
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "unmatched"

    row = test_db.conn.execute(
        "SELECT catalog_seed_status, catalog_seeded_at, catalog_track_id FROM music_requests WHERE id = ?",
        ("req-unmatched",),
    ).fetchone()
    assert row[0] == "unmatched"
    assert row[1] is not None
    assert row[2] is None


def test_unresolvable_artist_records_no_artist(test_db: Database, mock_discovery, mock_enricher) -> None:
    req = _create_req(
        test_db,
        req_id="req-no-artist",
        artist="Unresolvable Ghost Artist",
        title="Ghost Song",
        item_type="track",
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "no_artist"

    row = test_db.conn.execute(
        "SELECT catalog_seed_status, catalog_seeded_at FROM music_requests WHERE id = ?",
        ("req-no-artist",),
    ).fetchone()
    assert row[0] == "no_artist"
    assert row[1] is not None


def test_pending_request_not_seeded(test_db: Database, mock_discovery, mock_enricher) -> None:
    _create_req(
        test_db,
        req_id="req-pending",
        artist="Daft Punk",
        title="One More Time",
        status=RequestStatus.PENDING,
    )
    counts = seed_pending_request_catalog(test_db)
    assert counts == {}

    row = test_db.conn.execute(
        "SELECT catalog_seeded_at FROM music_requests WHERE id = ?",
        ("req-pending",),
    ).fetchone()
    assert row[0] is None


def test_seed_pending_honors_should_stop(test_db: Database, mock_discovery, mock_enricher) -> None:
    _create_req(
        test_db,
        req_id="req-stop-1",
        artist="Daft Punk",
        title="One More Time",
        foreign_id="deezer:track:3135553",
        status=RequestStatus.PROCESSING,
    )
    _create_req(
        test_db,
        req_id="req-stop-2",
        artist="Daft Punk",
        title="One More Time",
        foreign_id="deezer:track:3135553",
        status=RequestStatus.PROCESSING,
    )

    counts = seed_pending_request_catalog(test_db, should_stop=lambda: True)
    assert counts == {}

    rows = test_db.conn.execute(
        "SELECT catalog_seeded_at FROM music_requests WHERE id IN ('req-stop-1', 'req-stop-2')"
    ).fetchall()
    assert all(r[0] is None for r in rows)


def test_seed_pending_skips_already_seeded(test_db: Database, mock_discovery, mock_enricher) -> None:
    _create_req(
        test_db,
        req_id="req-skip-1",
        artist="Daft Punk",
        title="One More Time",
        foreign_id="deezer:track:3135553",
        status=RequestStatus.PROCESSING,
    )
    _create_req(
        test_db,
        req_id="req-skip-2",
        artist="Daft Punk",
        title="One More Time",
        foreign_id="deezer:track:3135553",
        status=RequestStatus.PROCESSING,
    )

    # Manually mark req-skip-1 as already seeded
    test_db.set_request_catalog("req-skip-1", status="seeded", track_id="trk-pre")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("trackseerr.request_catalog.get_shared_discovery_client", lambda db: mock_discovery)
        mp.setattr("trackseerr.request_catalog.get_shared_enricher", lambda db: mock_enricher)
        counts = seed_pending_request_catalog(test_db)

    # Only req-skip-2 should have been seeded in this run
    assert counts.get("seeded") == 1
    row2 = test_db.conn.execute(
        "SELECT catalog_seed_status FROM music_requests WHERE id = ?", ("req-skip-2",)
    ).fetchone()
    assert row2[0] == "seeded"


def test_lidarr_mode_skipped(test_db: Database, mock_discovery, mock_enricher) -> None:
    test_db.update_media_management_settings({"library_mode": "lidarr"})
    req = _create_req(
        test_db,
        req_id="req-lidarr",
        artist="Daft Punk",
        title="One More Time",
        foreign_id="deezer:track:3135553",
        status=RequestStatus.PROCESSING,
    )
    result = seed_request_catalog(test_db, req, discovery_client=mock_discovery, enricher=mock_enricher)
    assert result == "skipped"

    row = test_db.conn.execute(
        "SELECT catalog_seed_status, catalog_seeded_at FROM music_requests WHERE id = ?",
        ("req-lidarr",),
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] is not None
