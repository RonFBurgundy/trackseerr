"""Unit tests for native library storage: schema migration v15, models, CRUD, cascades, and stats."""

import uuid
from typing import Any

import pytest

from plex_playlist_sync.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryMode,
    LibraryTrack,
)
from plex_playlist_sync.storage import Database, clean_library_name


@pytest.fixture
def db(tmp_path):
    """Provides a fresh disk-backed Database for isolated testing with full PRAGMA foreign_keys."""
    db_file = tmp_path / "test_library.db"
    database = Database(db_file)
    yield database
    database.close()


@pytest.fixture
def mem_db():
    """Provides an in-memory Database instance."""
    database = Database(":memory:")
    yield database
    database.close()


class TestLibraryModelsAndHelpers:
    """Tests domain model dataclasses, serialization, and string cleaning utilities."""

    def test_clean_library_name_helper(self):
        assert clean_library_name("Radiohead") == "radiohead"
        assert clean_library_name("AC/DC") == "acdc"
        assert clean_library_name("P!nk") == "pnk"
        assert clean_library_name("The Beatles - Abbey Road (Remastered)") == "the beatles abbey road remastered"
        assert clean_library_name("   Multiple   Spaces   Here  ") == "multiple spaces here"
        assert clean_library_name("") == ""
        assert clean_library_name(None) == ""

    def test_models_to_dict_serialization(self):
        artist = LibraryArtist(
            id="art-1",
            name="Pink Floyd",
            clean_name="pink floyd",
            foreign_artist_id="mbid-123",
            path="/music/Pink Floyd",
            monitored=True,
        )
        art_dict = artist.to_dict()
        assert art_dict["id"] == "art-1"
        assert art_dict["name"] == "Pink Floyd"
        assert art_dict["clean_name"] == "pink floyd"
        assert art_dict["foreign_artist_id"] == "mbid-123"
        assert art_dict["monitored"] is True

        album = LibraryAlbum(
            id="alb-1",
            artist_id="art-1",
            title="The Dark Side of the Moon",
            clean_title="the dark side of the moon",
            year=1973,
            album_type="album",
            monitored=True,
            total_tracks=10,
        )
        alb_dict = album.to_dict()
        assert alb_dict["id"] == "alb-1"
        assert alb_dict["artist_id"] == "art-1"
        assert alb_dict["year"] == 1973
        assert alb_dict["total_tracks"] == 10

        track = LibraryTrack(
            id="trk-1",
            album_id="alb-1",
            artist_id="art-1",
            title="Time",
            clean_title="time",
            track_number=4,
            disc_number=1,
            duration_seconds=413.5,
            monitored=True,
        )
        trk_dict = track.to_dict()
        assert trk_dict["id"] == "trk-1"
        assert trk_dict["track_number"] == 4
        assert trk_dict["disc_number"] == 1
        assert trk_dict["duration_seconds"] == 413.5

        lib_file = LibraryFile(
            id="fil-1",
            track_id="trk-1",
            file_path="/music/Pink Floyd/The Dark Side of the Moon/04 Time.flac",
            relative_path="The Dark Side of the Moon/04 Time.flac",
            codec="flac",
            bitrate=980,
            sample_rate=44100,
            bits_per_sample=16,
            quality_name="FLAC 16bit",
            size_bytes=34567890,
            cutoff_met=True,
        )
        fil_dict = lib_file.to_dict()
        assert fil_dict["id"] == "fil-1"
        assert fil_dict["codec"] == "flac"
        assert fil_dict["size_bytes"] == 34567890
        assert fil_dict["cutoff_met"] is True


class TestLibraryStorageMigrationAndCRUD:
    """Comprehensive tests for migration v15, CRUD operations, cascading, and stats."""

    def test_migration_v15_schema(self, db: Database):
        """Verifies all 4 tables, columns, indexes, and library_mode in settings."""
        cur = db.conn.cursor()

        # 1. Verify tables exist
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        assert "library_artists" in tables
        assert "library_albums" in tables
        assert "library_tracks" in tables
        assert "library_files" in tables

        # 2. Verify columns of library_artists
        cur.execute("PRAGMA table_info(library_artists)")
        artist_cols = {row["name"] for row in cur.fetchall()}
        expected_artist_cols = {
            "id",
            "name",
            "clean_name",
            "foreign_artist_id",
            "path",
            "monitored",
            "quality_profile_id",
            "metadata_json",
            "created_at",
            "updated_at",
        }
        assert expected_artist_cols.issubset(artist_cols)

        # 3. Verify columns of library_albums
        cur.execute("PRAGMA table_info(library_albums)")
        album_cols = {row["name"] for row in cur.fetchall()}
        expected_album_cols = {
            "id",
            "artist_id",
            "title",
            "clean_title",
            "foreign_album_id",
            "release_date",
            "year",
            "album_type",
            "monitored",
            "path",
            "cover_url",
            "total_tracks",
            "created_at",
            "updated_at",
        }
        assert expected_album_cols.issubset(album_cols)

        # 4. Verify columns of library_tracks
        cur.execute("PRAGMA table_info(library_tracks)")
        track_cols = {row["name"] for row in cur.fetchall()}
        expected_track_cols = {
            "id",
            "album_id",
            "artist_id",
            "title",
            "clean_title",
            "track_number",
            "disc_number",
            "duration_seconds",
            "monitored",
            "foreign_track_id",
            "created_at",
            "updated_at",
        }
        assert expected_track_cols.issubset(track_cols)

        # 5. Verify columns of library_files
        cur.execute("PRAGMA table_info(library_files)")
        file_cols = {row["name"] for row in cur.fetchall()}
        expected_file_cols = {
            "id",
            "track_id",
            "file_path",
            "relative_path",
            "codec",
            "bitrate",
            "sample_rate",
            "bits_per_sample",
            "quality_name",
            "size_bytes",
            "cutoff_met",
            "date_added",
            "updated_at",
        }
        assert expected_file_cols.issubset(file_cols)

        # 6. Verify indexes
        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cur.fetchall()}
        expected_indexes = {
            "idx_lib_artists_clean_name",
            "idx_lib_artists_foreign",
            "idx_lib_albums_artist",
            "idx_lib_albums_clean_title",
            "idx_lib_albums_foreign",
            "idx_lib_tracks_album",
            "idx_lib_tracks_artist",
            "idx_lib_tracks_clean_title",
            "idx_lib_files_track",
            "idx_lib_files_path",
            "idx_lib_files_cutoff",
        }
        assert expected_indexes.issubset(indexes)

        # 7. Verify library_mode column in media_management_settings
        settings = db.get_media_management_settings()
        assert "library_mode" in settings
        assert settings["library_mode"] == "native"

    def test_library_artist_crud_and_clean_name(self, db: Database):
        """Tests inserting, fetching by ID and name, updating, listing with pagination and search query."""
        # Upsert with model
        artist_obj = LibraryArtist(
            id=str(uuid.uuid4()),
            name="Guns N' Roses",
            clean_name="",  # Should be computed by storage
            path="/music/Guns N' Roses",
            monitored=True,
        )
        created = db.upsert_library_artist(artist_obj)
        artist_id = created["id"]
        assert created["name"] == "Guns N' Roses"
        assert created["clean_name"] == "guns n roses"
        assert created["path"] == "/music/Guns N' Roses"
        assert created["monitored"] is True

        # Fetch by ID
        fetched = db.get_library_artist(artist_id)
        assert fetched is not None
        assert fetched["id"] == artist_id
        assert fetched["name"] == "Guns N' Roses"

        # Fetch by name (both original casing and clean name)
        fetched_by_name = db.get_library_artist_by_name("Guns N' Roses")
        assert fetched_by_name is not None
        assert fetched_by_name["id"] == artist_id

        fetched_by_clean = db.get_library_artist_by_name("guns n roses")
        assert fetched_by_clean is not None
        assert fetched_by_clean["id"] == artist_id

        # Update artist
        created["path"] = "/music/Guns N Roses Updated"
        created["monitored"] = False
        updated = db.upsert_library_artist(created)
        assert updated["path"] == "/music/Guns N Roses Updated"
        assert updated["monitored"] is False

        # Upsert multiple artists for listing & search
        db.upsert_library_artist({"name": "The Beatles", "monitored": True})
        db.upsert_library_artist({"name": "Led Zeppelin", "monitored": True})
        db.upsert_library_artist({"name": "Pink Floyd", "monitored": False})

        # List all
        all_artists = db.list_library_artists(limit=10)
        assert len(all_artists) == 4

        # List monitored only
        monitored_artists = db.list_library_artists(monitored_only=True)
        assert len(monitored_artists) == 2
        names = {a["name"] for a in monitored_artists}
        assert "The Beatles" in names
        assert "Led Zeppelin" in names

        # Pagination: limit & offset
        page1 = db.list_library_artists(limit=2, offset=0)
        page2 = db.list_library_artists(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["id"] != page2[0]["id"]

        # Search query
        search_beatles = db.list_library_artists(query="beatles")
        assert len(search_beatles) == 1
        assert search_beatles[0]["name"] == "The Beatles"

        search_clean = db.list_library_artists(query="guns n roses")
        assert len(search_clean) == 1
        assert search_clean[0]["name"] == "Guns N' Roses"

        search_none = db.list_library_artists(query="NonexistentBand123")
        assert len(search_none) == 0

        # Delete artist
        deleted = db.delete_library_artist(artist_id)
        assert deleted is True
        assert db.get_library_artist(artist_id) is None
        assert db.delete_library_artist("non-existent-id") is False

    def test_cascade_deletion(self, db: Database):
        """Confirms deleting an artist automatically deletes child albums, child tracks, and child files via foreign key cascade."""
        # 1. Create artist -> album -> track -> file
        artist = db.upsert_library_artist({"name": "Radiohead"})
        artist_id = artist["id"]

        album = db.upsert_library_album(
            {"artist_id": artist_id, "title": "OK Computer", "year": 1997}
        )
        album_id = album["id"]

        track = db.upsert_library_track(
            {
                "album_id": album_id,
                "artist_id": artist_id,
                "title": "Paranoid Android",
                "track_number": 2,
            }
        )
        track_id = track["id"]

        file_rec = db.upsert_library_file(
            {
                "track_id": track_id,
                "file_path": "/music/Radiohead/OK Computer/02 Paranoid Android.flac",
                "relative_path": "OK Computer/02 Paranoid Android.flac",
                "codec": "flac",
                "size_bytes": 45000000,
            }
        )
        file_id = file_rec["id"]

        # Verify all entities exist
        assert db.get_library_artist(artist_id) is not None
        assert db.get_library_album(album_id) is not None
        assert db.get_library_track(track_id) is not None
        assert db.get_library_file(file_id) is not None

        # 2. Test track deletion cascades to file, but retains album & artist
        db.delete_library_track(track_id)
        assert db.get_library_track(track_id) is None
        assert db.get_library_file(file_id) is None
        assert db.get_library_album(album_id) is not None
        assert db.get_library_artist(artist_id) is not None

        # Re-insert track and file
        track2 = db.upsert_library_track(
            {
                "album_id": album_id,
                "artist_id": artist_id,
                "title": "Karma Police",
                "track_number": 6,
            }
        )
        file2 = db.upsert_library_file(
            {
                "track_id": track2["id"],
                "file_path": "/music/Radiohead/OK Computer/06 Karma Police.flac",
                "relative_path": "OK Computer/06 Karma Police.flac",
                "codec": "flac",
            }
        )
        assert db.get_library_track(track2["id"]) is not None
        assert db.get_library_file(file2["id"]) is not None

        # 3. Test album deletion cascades to track and file, but retains artist
        db.delete_library_album(album_id)
        assert db.get_library_album(album_id) is None
        assert db.get_library_track(track2["id"]) is None
        assert db.get_library_file(file2["id"]) is None
        assert db.get_library_artist(artist_id) is not None

        # Re-insert album, track, and file
        album3 = db.upsert_library_album(
            {"artist_id": artist_id, "title": "Kid A", "year": 2000}
        )
        track3 = db.upsert_library_track(
            {
                "album_id": album3["id"],
                "artist_id": artist_id,
                "title": "Idioteque",
                "track_number": 8,
            }
        )
        file3 = db.upsert_library_file(
            {
                "track_id": track3["id"],
                "file_path": "/music/Radiohead/Kid A/08 Idioteque.flac",
                "relative_path": "Kid A/08 Idioteque.flac",
                "codec": "flac",
            }
        )

        # 4. Test artist deletion cascades to all child albums, tracks, and files
        db.delete_library_artist(artist_id)
        assert db.get_library_artist(artist_id) is None
        assert db.get_library_album(album3["id"]) is None
        assert db.get_library_track(track3["id"]) is None
        assert db.get_library_file(file3["id"]) is None

    def test_monitored_hierarchy_cascading(self, db: Database):
        """Tests set_artist_monitored(..., cascade_children=True) updates albums and tracks; tests independent track and album monitoring."""
        # Create hierarchy: 1 artist -> 2 albums -> 2 tracks each
        artist = db.upsert_library_artist({"name": "Queen", "monitored": True})
        artist_id = artist["id"]

        album1 = db.upsert_library_album(
            {"artist_id": artist_id, "title": "A Night at the Opera", "monitored": True}
        )
        album2 = db.upsert_library_album(
            {"artist_id": artist_id, "title": "News of the World", "monitored": True}
        )

        t1 = db.upsert_library_track(
            {"album_id": album1["id"], "artist_id": artist_id, "title": "Bohemian Rhapsody", "monitored": True}
        )
        t2 = db.upsert_library_track(
            {"album_id": album1["id"], "artist_id": artist_id, "title": "You're My Best Friend", "monitored": True}
        )
        t3 = db.upsert_library_track(
            {"album_id": album2["id"], "artist_id": artist_id, "title": "We Will Rock You", "monitored": True}
        )
        t4 = db.upsert_library_track(
            {"album_id": album2["id"], "artist_id": artist_id, "title": "We Are the Champions", "monitored": True}
        )

        # 1. Cascade artist unmonitoring
        res = db.set_artist_monitored(artist_id, False, cascade_children=True)
        assert res is True
        assert db.get_library_artist(artist_id)["monitored"] is False
        assert db.get_library_album(album1["id"])["monitored"] is False
        assert db.get_library_album(album2["id"])["monitored"] is False
        assert db.get_library_track(t1["id"])["monitored"] is False
        assert db.get_library_track(t2["id"])["monitored"] is False
        assert db.get_library_track(t3["id"])["monitored"] is False
        assert db.get_library_track(t4["id"])["monitored"] is False

        # 2. Non-cascading artist monitoring
        res = db.set_artist_monitored(artist_id, True, cascade_children=False)
        assert res is True
        assert db.get_library_artist(artist_id)["monitored"] is True
        # Children remain unmonitored
        assert db.get_library_album(album1["id"])["monitored"] is False
        assert db.get_library_album(album2["id"])["monitored"] is False
        assert db.get_library_track(t1["id"])["monitored"] is False

        # 3. Cascading album monitoring on album 1 only
        res = db.set_album_monitored(album1["id"], True, cascade_tracks=True)
        assert res is True
        assert db.get_library_album(album1["id"])["monitored"] is True
        assert db.get_library_track(t1["id"])["monitored"] is True
        assert db.get_library_track(t2["id"])["monitored"] is True
        # Album 2 and its tracks remain False
        assert db.get_library_album(album2["id"])["monitored"] is False
        assert db.get_library_track(t3["id"])["monitored"] is False
        assert db.get_library_track(t4["id"])["monitored"] is False

        # 4. Non-cascading album unmonitoring on album 1
        res = db.set_album_monitored(album1["id"], False, cascade_tracks=False)
        assert res is True
        assert db.get_library_album(album1["id"])["monitored"] is False
        # Tracks remain True
        assert db.get_library_track(t1["id"])["monitored"] is True
        assert db.get_library_track(t2["id"])["monitored"] is True

        # 5. Independent track monitoring
        res = db.set_track_monitored(t1["id"], False)
        assert res is True
        assert db.get_library_track(t1["id"])["monitored"] is False
        assert db.get_library_track(t2["id"])["monitored"] is True

        # Nonexistent IDs return False
        assert db.set_artist_monitored("nonexistent-artist", True) is False
        assert db.set_album_monitored("nonexistent-album", True) is False
        assert db.set_track_monitored("nonexistent-track", True) is False

    def test_library_file_crud_and_stats(self, db: Database):
        """Tests inserting files, linking to tracks, calculating library stats (sizes, counts, cutoff unmet)."""
        # Initial stats on empty library
        initial_stats = db.get_library_stats()
        assert initial_stats == {
            "source": "native",
            "artist_count": 0,
            "unmonitored_artist_count": 0,
            "continuing_artist_count": None,
            "ended_artist_count": None,
            "album_count": 0,
            "track_count": 0,
            "total_track_count": 0,
            "track_file_count": 0,
            "missing_track_count": 0,
            "file_count": 0,
            "total_size_bytes": 0,
            "monitored_artist_count": 0,
            "monitored_track_count": 0,
            "cutoff_unmet_track_count": 0,
        }

        # Create library hierarchy
        artist = db.upsert_library_artist({"name": "Daft Punk", "monitored": True})
        album = db.upsert_library_album(
            {"artist_id": artist["id"], "title": "Discovery", "year": 2001, "monitored": True}
        )
        t1 = db.upsert_library_track(
            {"album_id": album["id"], "artist_id": artist["id"], "title": "One More Time", "monitored": True}
        )
        t2 = db.upsert_library_track(
            {"album_id": album["id"], "artist_id": artist["id"], "title": "Aerodynamic", "monitored": True}
        )
        t3 = db.upsert_library_track(
            {"album_id": album["id"], "artist_id": artist["id"], "title": "Harder, Better, Faster, Stronger", "monitored": False}
        )

        # Insert file for track 1: FLAC meeting cutoff
        f1_data = LibraryFile(
            id=str(uuid.uuid4()),
            track_id=t1["id"],
            file_path="/music/Daft Punk/Discovery/01 One More Time.flac",
            relative_path="Discovery/01 One More Time.flac",
            codec="flac",
            bitrate=1024,
            sample_rate=44100,
            bits_per_sample=16,
            quality_name="FLAC 16bit",
            size_bytes=35000000,
            cutoff_met=True,
        )
        f1 = db.upsert_library_file(f1_data)
        assert f1["file_path"] == f1_data.file_path
        assert f1["cutoff_met"] is True
        assert f1["size_bytes"] == 35000000

        # Insert file for track 2: MP3 not meeting cutoff
        f2_data = {
            "track_id": t2["id"],
            "file_path": "/music/Daft Punk/Discovery/02 Aerodynamic.mp3",
            "relative_path": "Discovery/02 Aerodynamic.mp3",
            "codec": "mp3",
            "bitrate": 192,
            "quality_name": "MP3 192",
            "size_bytes": 5000000,
            "cutoff_met": False,
        }
        f2 = db.upsert_library_file(f2_data)
        assert f2["cutoff_met"] is False

        # Get file by ID
        fetched_f1 = db.get_library_file(f1["id"])
        assert fetched_f1 is not None
        assert fetched_f1["id"] == f1["id"]

        # Get file for track
        fetched_f2_by_trk = db.get_library_file_for_track(t2["id"])
        assert fetched_f2_by_trk is not None
        assert fetched_f2_by_trk["id"] == f2["id"]

        # List files
        file_list = db.list_library_files(limit=10)
        assert len(file_list) == 2

        # Check aggregate stats
        stats = db.get_library_stats()
        assert stats["artist_count"] == 1
        assert stats["album_count"] == 1
        assert stats["track_count"] == 3
        assert stats["file_count"] == 2
        assert stats["total_size_bytes"] == 40000000  # 35M + 5M
        assert stats["monitored_artist_count"] == 1
        assert stats["monitored_track_count"] == 2  # t1 and t2 monitored, t3 unmonitored
        assert stats["cutoff_unmet_track_count"] == 1  # t2 has cutoff_met = False

        # Upsert file (updating metadata and size)
        f2["size_bytes"] = 6000000
        f2["bitrate"] = 256
        f2_updated = db.upsert_library_file(f2)
        assert f2_updated["size_bytes"] == 6000000
        assert f2_updated["bitrate"] == 256

        # Check updated size stat
        stats_after_update = db.get_library_stats()
        assert stats_after_update["total_size_bytes"] == 41000000

        # Delete file
        deleted = db.delete_library_file(f2["id"])
        assert deleted is True
        assert db.get_library_file(f2["id"]) is None
        assert db.get_library_file_for_track(t2["id"]) is None

        # Stats after file deletion
        stats_after_delete = db.get_library_stats()
        assert stats_after_delete["file_count"] == 1
        assert stats_after_delete["total_size_bytes"] == 35000000
        assert stats_after_delete["cutoff_unmet_track_count"] == 0

    def test_library_mode_settings_persistence(self, db: Database):
        """Tests reading/updating library_mode between 'native' and 'lidarr'."""
        # Check initial default
        settings = db.get_media_management_settings()
        assert settings["library_mode"] == LibraryMode.NATIVE.value
        assert settings["library_mode"] == "native"

        # Update to lidarr mode
        updated = db.update_media_management_settings({"library_mode": LibraryMode.LIDARR.value})
        assert updated["library_mode"] == "lidarr"

        # Re-read from fresh query
        reloaded = db.get_media_management_settings()
        assert reloaded["library_mode"] == "lidarr"

        # Switch back to native mode
        updated_back = db.update_media_management_settings({"library_mode": "native"})
        assert updated_back["library_mode"] == "native"
        assert db.get_media_management_settings()["library_mode"] == "native"
