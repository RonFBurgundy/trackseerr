"""Unit tests for the sanitized library availability layer and discovery route status annotation."""

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from trackseerr.api.routes.discovery import annotate_item_statuses
from trackseerr.clients.plex import PlexClient
from trackseerr.library_availability import get_item_availability
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    MusicRequest,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path):
    """Provides an isolated Database instance."""
    db_file = tmp_path / "test_avail.db"
    database = Database(db_file)
    yield database
    database.close()


def _populate_test_artist_and_album(
    db: Database,
    artist_name: str = "Daft Punk",
    album_title: str = "Discovery",
    num_tracks: int = 3,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Helper to seed an artist, album, and tracks."""
    art = db.upsert_library_artist(
        LibraryArtist(
            id=str(uuid.uuid4()),
            name=artist_name,
            foreign_artist_id="mbid-art-daft",
            monitored=True,
        )
    )
    alb = db.upsert_library_album(
        LibraryAlbum(
            id=str(uuid.uuid4()),
            artist_id=art["id"],
            title=album_title,
            foreign_album_id="mbid-alb-disc",
            year=2001,
            total_tracks=num_tracks,
            monitored=True,
        )
    )
    tracks = []
    track_names = ["One More Time", "Aerodynamic", "Digital Love"]
    for i in range(num_tracks):
        name = track_names[i] if i < len(track_names) else f"Track {i+1}"
        trk = db.upsert_library_track(
            LibraryTrack(
                id=str(uuid.uuid4()),
                album_id=alb["id"],
                artist_id=art["id"],
                title=name,
                track_number=i + 1,
                monitored=True,
                foreign_track_id=f"mbid-trk-{i+1}",
            )
        )
        tracks.append(trk)
    return art, alb, tracks


class TestLibraryAvailabilityLayer:
    """Verifies get_item_availability contract, non-leaking boundary, and resolution logic."""

    def test_single_track_meets_cutoff(self, db: Database):
        art, alb, tracks = _populate_test_artist_and_album(db)
        # Add file for track 1 that meets cutoff
        db.upsert_library_file(
            LibraryFile(
                id=str(uuid.uuid4()),
                track_id=tracks[0]["id"],
                file_path="/music/Daft Punk/Discovery/01 One More Time.flac",
                relative_path="01 One More Time.flac",
                codec="FLAC",
                quality_name="FLAC 24bit",
                cutoff_met=True,
            )
        )

        avail = get_item_availability(
            db, artist_name="Daft Punk", track_title="One More Time"
        )
        assert avail["in_library"] is True
        assert avail["status"] == "available"
        assert avail["quality"] == "FLAC 24bit"
        assert avail["file_count"] == 1
        assert avail["track_count"] == 1
        assert avail["monitored"] is True
        # Invariant check: No sensitive disk paths in output
        assert "file_path" not in avail
        assert "/music" not in str(avail)

    def test_single_track_cutoff_unmet(self, db: Database):
        art, alb, tracks = _populate_test_artist_and_album(db)
        # Add file for track 1 that fails cutoff
        db.upsert_library_file(
            LibraryFile(
                id=str(uuid.uuid4()),
                track_id=tracks[0]["id"],
                file_path="/music/Daft Punk/Discovery/01 One More Time.mp3",
                relative_path="01 One More Time.mp3",
                codec="MP3",
                quality_name="MP3 192",
                cutoff_met=False,
            )
        )

        avail = get_item_availability(
            db, artist_name="Daft Punk", track_title="One More Time"
        )
        assert avail["in_library"] is True
        assert avail["status"] == "cutoff_unmet"
        assert avail["quality"] == "MP3 192"

    def test_single_track_catalogued_missing_file(self, db: Database):
        art, alb, tracks = _populate_test_artist_and_album(db)
        # Track 2 has no file
        avail = get_item_availability(
            db, artist_name="Daft Punk", track_title="Aerodynamic"
        )
        assert avail["in_library"] is True
        assert avail["status"] == "missing"
        assert avail["quality"] is None
        assert avail["file_count"] == 0
        assert avail["track_count"] == 1

    def test_album_complete_partial_missing(self, db: Database):
        art, alb, tracks = _populate_test_artist_and_album(db, num_tracks=3)

        # 1. Missing (0 files for 3 tracks)
        avail_missing = get_item_availability(
            db, artist_name="Daft Punk", album_title="Discovery"
        )
        assert avail_missing["in_library"] is True
        assert avail_missing["status"] == "missing"
        assert avail_missing["file_count"] == 0
        assert avail_missing["track_count"] == 3

        # 2. Partial (1 file for 3 tracks)
        db.upsert_library_file(
            LibraryFile(
                id=str(uuid.uuid4()),
                track_id=tracks[0]["id"],
                file_path="/music/Daft Punk/Discovery/01.flac",
                relative_path="01.flac",
                codec="FLAC",
                quality_name="FLAC 16bit",
                cutoff_met=True,
            )
        )
        avail_partial = get_item_availability(
            db, artist_name="Daft Punk", album_title="Discovery"
        )
        assert avail_partial["in_library"] is True
        assert avail_partial["status"] == "partial"
        assert avail_partial["file_count"] == 1
        assert avail_partial["track_count"] == 3
        assert avail_partial["quality"] == "FLAC 16bit"

        # 3. Complete (files for all 3 tracks)
        for i in (1, 2):
            db.upsert_library_file(
                LibraryFile(
                    id=str(uuid.uuid4()),
                    track_id=tracks[i]["id"],
                    file_path=f"/music/Daft Punk/Discovery/0{i+1}.flac",
                    relative_path=f"0{i+1}.flac",
                    codec="FLAC",
                    quality_name="FLAC 16bit",
                    cutoff_met=True,
                )
            )

        avail_complete = get_item_availability(
            db, artist_name="Daft Punk", album_title="Discovery"
        )
        assert avail_complete["in_library"] is True
        assert avail_complete["status"] == "available"
        assert avail_complete["file_count"] == 3
        assert avail_complete["track_count"] == 3

    def test_artist_catalog_presence(self, db: Database):
        db.upsert_library_artist(
            LibraryArtist(
                id=str(uuid.uuid4()),
                name="Justice",
                monitored=True,
            )
        )
        avail = get_item_availability(db, artist_name="Justice")
        assert avail["in_library"] is True
        assert avail["status"] == "available"
        assert avail["monitored"] is True

    def test_item_not_in_library(self, db: Database):
        avail = get_item_availability(
            db, artist_name="The Beatles", album_title="Abbey Road"
        )
        assert avail["in_library"] is False
        assert avail["status"] == "none"
        assert avail["file_count"] == 0

    def test_foreign_id_resolution(self, db: Database):
        art, alb, tracks = _populate_test_artist_and_album(db)
        db.upsert_library_file(
            LibraryFile(
                id=str(uuid.uuid4()),
                track_id=tracks[0]["id"],
                file_path="/music/Daft Punk/Discovery/01.flac",
                relative_path="01.flac",
                codec="FLAC",
                quality_name="FLAC 24bit",
            )
        )

        # Lookup by foreign_track_id
        avail_trk = get_item_availability(db, foreign_id="mbid-trk-1")
        assert avail_trk["in_library"] is True
        assert avail_trk["status"] == "available"
        assert avail_trk["quality"] == "FLAC 24bit"

        # Lookup by foreign_artist_id
        avail_art = get_item_availability(db, foreign_id="mbid-art-daft")
        assert avail_art["in_library"] is True
        assert avail_art["status"] == "available"


class TestDiscoveryRouteAnnotation:
    """Verifies annotate_item_statuses behavior under native vs lidarr library modes."""

    def test_annotate_native_mode_prioritization(self, db: Database):
        db.update_media_management_settings({"library_mode": "native"})
        art, alb, tracks = _populate_test_artist_and_album(db)
        db.upsert_library_file(
            LibraryFile(
                id=str(uuid.uuid4()),
                track_id=tracks[0]["id"],
                file_path="/music/Daft Punk/Discovery/01.flac",
                relative_path="01.flac",
                codec="FLAC",
                quality_name="FLAC 24bit",
            )
        )

        # Seed user for request foreign key
        db.upsert_user(user_id="user-1", username="testuser")

        # Existing request for an item NOT yet in the library catalog
        db.create_request(
            MusicRequest(
                id="req-2",
                user_id="user-1",
                item_type="track",
                title="Robot Rock",
                artist="Daft Punk",
                foreign_id="disc-track-2",
            )
        )

        items = [
            {"id": "disc-track-1", "type": "track", "title": "One More Time", "artist": "Daft Punk"},
            {"id": "disc-track-2", "type": "track", "title": "Robot Rock", "artist": "Daft Punk"},
            {"id": "disc-track-9", "type": "track", "title": "Around the World", "artist": "Daft Punk"},
            {"id": "disc-track-missing", "type": "track", "title": "Aerodynamic", "artist": "Daft Punk"},
        ]

        mock_plex = MagicMock(spec=PlexClient)
        annotated = annotate_item_statuses(items, db=db, plex_client=mock_plex)

        # 1. Item 1 is in native library with file -> available
        assert annotated[0]["status"] == "available"
        assert annotated[0]["quality"] == "FLAC 24bit"

        # 2. Item 2 is not in library, but has pending request -> requested
        assert annotated[1]["status"] == "requested"
        assert annotated[1]["request_id"] == "req-2"

        # 3. Item 3 is neither -> none
        assert annotated[2]["status"] == "none"

        # 4. Item 4 is in catalog but missing file -> missing
        assert annotated[3]["status"] == "missing"

        # In native mode, items in the native library ("One More Time", "Aerodynamic")
        # short-circuit without querying Plex
        searched_queries = [
            c.kwargs.get("query")
            for c in mock_plex.search_library_tracks.call_args_list
            if "query" in c.kwargs
        ]
        assert "one more time" not in searched_queries
        assert "aerodynamic" not in searched_queries

    def test_annotate_lidarr_mode_fallback_to_plex(self, db: Database):
        db.update_media_management_settings({"library_mode": "lidarr"})

        mock_plex = MagicMock(spec=PlexClient)
        mock_plex.search_library_tracks.return_value = [
            {"title": "Harder, Better, Faster, Stronger", "artist": "Daft Punk"}
        ]

        items = [
            {
                "id": "disc-track-3",
                "type": "track",
                "title": "Harder, Better, Faster, Stronger",
                "artist": "Daft Punk",
            }
        ]

        annotated = annotate_item_statuses(items, db=db, plex_client=mock_plex)

        # In Lidarr mode, Plex was queried and item found
        mock_plex.search_library_tracks.assert_called_once()
        assert annotated[0]["status"] == "in_library"
