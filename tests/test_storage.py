"""Tests for SQLite persistence engine: migrations, CRUD operations, foreign key cascades, and targeting."""

from datetime import datetime, timezone
import sqlite3
from unittest.mock import patch
import pytest

from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
    Playlist,
    Track,
)
from trackseerr.storage import Database


@pytest.fixture
def mem_db():
    """Fixture providing an in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def disk_db(tmp_path):
    """Fixture providing a disk-backed Database instance."""
    db_file = tmp_path / "test_data" / "playlists.db"
    db = Database(db_file)
    yield db
    db.close()


class TestDatabaseInitAndMigrations:
    def test_in_memory_initialization_and_tables(self, mem_db):
        cur = mem_db.conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name ASC"
        )
        tables = [row["name"] for row in cur.fetchall()]
        assert "users" in tables
        assert "playlists" in tables
        assert "playlist_targets" in tables
        assert "missing_tracks" in tables
        assert "sessions" in tables
        assert "schema_migrations" in tables

    def test_foreign_keys_pragma_enabled(self, mem_db):
        cur = mem_db.conn.cursor()
        cur.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1

    def test_disk_db_wal_mode(self, disk_db):
        cur = disk_db.conn.cursor()
        cur.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"

    def test_context_manager(self, tmp_path):
        db_file = tmp_path / "ctx.db"
        with Database(db_file) as db:
            user = db.upsert_user("u1", "testuser")
            assert user["username"] == "testuser"
        # Connection should be closed on exit
        assert db._conn is None

    def test_migration_idempotent(self, tmp_path):
        db_file = tmp_path / "idem.db"
        db1 = Database(db_file)
        db1.upsert_user("u1", "user1")
        db1.close()

        # Re-open same database file, should not crash or duplicate schema
        db2 = Database(db_file)
        user = db2.get_user("u1")
        assert user is not None
        assert user["username"] == "user1"
        db2.close()

    def test_unwritable_directory_raises_permission_error(self, tmp_path):
        db_file = tmp_path / "protected_dir" / "playlists.db"
        with patch("os.access", return_value=False):
            with pytest.raises(PermissionError) as exc_info:
                Database(db_file)
            assert "not writable" in str(exc_info.value)
            assert "PUID/PGID" in str(exc_info.value)

    def test_unwritable_database_file_raises_permission_error(self, tmp_path):
        db_file = tmp_path / "existing.db"
        db_file.touch()

        def custom_access(path, mode):
            # Directory writable, but db file itself unwritable
            if str(path) == str(db_file):
                return False
            return True

        with patch("os.access", side_effect=custom_access):
            with pytest.raises(PermissionError) as exc_info:
                Database(db_file)
            assert "exists but is not writable" in str(exc_info.value)


class TestUsersCRUD:
    def test_upsert_and_get_user(self, mem_db):
        user = mem_db.upsert_user(
            user_id="user_123",
            username="Alice",
            email="alice@example.com",
            is_admin=True,
        )
        assert user["id"] == "user_123"
        assert user["username"] == "Alice"
        assert user["email"] == "alice@example.com"
        assert user["is_admin"] is True

        retrieved = mem_db.get_user("user_123")
        assert retrieved == user

    def test_update_user_on_conflict(self, mem_db):
        mem_db.upsert_user("u1", "Bob", "bob@old.com", is_admin=False)
        updated = mem_db.upsert_user("u1", "Bob Roberts", "bob@new.com", is_admin=True)

        assert updated["id"] == "u1"
        assert updated["username"] == "Bob Roberts"
        assert updated["email"] == "bob@new.com"
        assert updated["is_admin"] is True

    def test_get_nonexistent_user(self, mem_db):
        assert mem_db.get_user("unknown") is None

    def test_list_users(self, mem_db):
        mem_db.upsert_user("u2", "Zach")
        mem_db.upsert_user("u1", "Alice")
        users = mem_db.list_users()
        assert len(users) == 2
        # Ordered by username ASC
        assert users[0]["username"] == "Alice"
        assert users[1]["username"] == "Zach"

    def test_delete_user(self, mem_db):
        mem_db.upsert_user("u1", "Alice")
        assert mem_db.delete_user("u1") is True
        assert mem_db.get_user("u1") is None
        assert mem_db.delete_user("u1") is False


class TestPlaylistsCRUD:
    def test_upsert_playlist_args(self, mem_db):
        pl = mem_db.upsert_playlist(
            playlist_id="p1",
            name="Top Hits",
            service="spotify",
            description="Billboard Top 100",
            poster_url="https://img.com/poster.jpg",
            enabled=True,
            sync_status="success",
        )
        assert pl["id"] == "p1"
        assert pl["name"] == "Top Hits"
        assert pl["service"] == "spotify"
        assert pl["description"] == "Billboard Top 100"
        assert pl["poster_url"] == "https://img.com/poster.jpg"
        assert pl["enabled"] is True
        assert pl["sync_status"] == "success"

    def test_upsert_playlist_dataclass(self, mem_db):
        model_pl = Playlist(
            id="p2",
            name="Indie Chill",
            description="Relaxing indie songs",
            poster="https://img.com/indie.jpg",
        )
        pl = mem_db.upsert_playlist(model_pl)
        assert pl["id"] == "p2"
        assert pl["name"] == "Indie Chill"
        assert pl["description"] == "Relaxing indie songs"
        assert pl["poster_url"] == "https://img.com/indie.jpg"

    def test_get_nonexistent_playlist(self, mem_db):
        assert mem_db.get_playlist("nonexistent") is None

    def test_delete_playlist(self, mem_db):
        mem_db.upsert_playlist("p1", "Rock")
        assert mem_db.delete_playlist("p1") is True
        assert mem_db.get_playlist("p1") is None
        assert mem_db.delete_playlist("p1") is False


class TestPlaylistTargetsAndFiltering:
    def test_set_and_get_playlist_targets(self, mem_db):
        mem_db.upsert_user("u1", "Alice")
        mem_db.upsert_user("u2", "Bob")
        mem_db.upsert_playlist("p1", "Workout Beats")

        mem_db.set_playlist_targets("p1", ["u1", "u2", "u1"])
        targets = mem_db.get_playlist_targets("p1")
        assert targets == ["u1", "u2"]

    def test_replace_playlist_targets(self, mem_db):
        mem_db.upsert_user("u1", "Alice")
        mem_db.upsert_user("u2", "Bob")
        mem_db.upsert_playlist("p1", "Workout Beats")

        mem_db.set_playlist_targets("p1", ["u1"])
        assert mem_db.get_playlist_targets("p1") == ["u1"]

        mem_db.set_playlist_targets("p1", ["u2"])
        assert mem_db.get_playlist_targets("p1") == ["u2"]

    def test_list_playlists_filtered_by_user(self, mem_db):
        mem_db.upsert_user("u1", "Alice")
        mem_db.upsert_user("u2", "Bob")

        mem_db.upsert_playlist("p1", "Alice Only Playlist")
        mem_db.upsert_playlist("p2", "Bob Only Playlist")
        mem_db.upsert_playlist("p3", "Shared Playlist", enabled=False)

        mem_db.set_playlist_targets("p1", ["u1"])
        mem_db.set_playlist_targets("p2", ["u2"])
        mem_db.set_playlist_targets("p3", ["u1", "u2"])

        # Filter by u1
        u1_playlists = mem_db.list_playlists(user_id="u1")
        assert [p["id"] for p in u1_playlists] == ["p1", "p3"]

        # Filter by u1 with enabled_only=True
        u1_enabled = mem_db.list_playlists(user_id="u1", enabled_only=True)
        assert [p["id"] for p in u1_enabled] == ["p1"]

        # Filter by u2
        u2_playlists = mem_db.list_playlists(user_id="u2")
        assert [p["id"] for p in u2_playlists] == ["p2", "p3"]

        # All playlists
        all_playlists = mem_db.list_playlists()
        assert len(all_playlists) == 3


class TestSyncResultsAndMissingTracks:
    def test_record_sync_result_with_dataclasses(self, mem_db):
        mem_db.upsert_playlist("p1", "Synthwave 80s")
        tracks = [
            Track(title="Nightcall", artist="Kavinsky", album="OutRun", url="https://sp.com/1"),
            Track(title="Tech Noir", artist="Gunship", album="Gunship", url="https://sp.com/2"),
        ]
        mem_db.record_sync_result("p1", "partial", tracks)

        pl = mem_db.get_playlist("p1")
        assert pl["sync_status"] == "partial"
        assert pl["last_synced_at"] is not None

        missing = mem_db.get_missing_tracks("p1")
        assert len(missing) == 2
        assert missing[0]["title"] == "Nightcall"
        assert missing[0]["artist"] == "Kavinsky"
        assert missing[1]["title"] == "Tech Noir"

    def test_record_sync_result_with_dicts(self, mem_db):
        mem_db.upsert_playlist("p1", "Rock")
        tracks = [
            {"title": "Song 1", "artist": "Artist 1", "album": "Album 1", "url": ""},
        ]
        mem_db.record_sync_result("p1", "success", tracks)

        missing = mem_db.get_missing_tracks("p1")
        assert len(missing) == 1
        assert missing[0]["title"] == "Song 1"

    def test_sync_result_overwrites_old_missing_tracks(self, mem_db):
        mem_db.upsert_playlist("p1", "Jazz")
        initial_tracks = [Track(title="Take Five", artist="Dave Brubeck", album="Time Out")]
        mem_db.record_sync_result("p1", "partial", initial_tracks)
        assert len(mem_db.get_missing_tracks("p1")) == 1

        # Successful sync clears missing tracks
        mem_db.record_sync_result("p1", "success", [])
        assert len(mem_db.get_missing_tracks("p1")) == 0

    def test_get_missing_tracks_all(self, mem_db):
        mem_db.upsert_playlist("p1", "P1")
        mem_db.upsert_playlist("p2", "P2")

        mem_db.record_sync_result("p1", "failed", [Track(title="T1", artist="A1", album="")])
        mem_db.record_sync_result("p2", "failed", [Track(title="T2", artist="A2", album="")])

        all_missing = mem_db.get_missing_tracks()
        assert len(all_missing) == 2


class TestSessions:
    def test_create_and_get_session(self, mem_db):
        mem_db.upsert_user("u1", "Alice")
        session = mem_db.create_session(
            session_id="sess_abc123",
            user_id="u1",
            data={"role": "admin", "plex_id": 999},
            expires_at="2026-12-31T23:59:59Z",
        )
        assert session["session_id"] == "sess_abc123"
        assert session["user_id"] == "u1"
        assert session["data"] == {"role": "admin", "plex_id": 999}
        assert session["expires_at"] == "2026-12-31T23:59:59Z"

        retrieved = mem_db.get_session("sess_abc123")
        assert retrieved == session

    def test_create_session_with_datetime(self, mem_db):
        exp = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
        session = mem_db.create_session("sess_dt", expires_at=exp)
        assert session["expires_at"] == exp.isoformat()

    def test_update_session_on_conflict(self, mem_db):
        mem_db.create_session("sess_1", data={"v": 1})
        updated = mem_db.create_session("sess_1", data={"v": 2})
        assert updated["data"] == {"v": 2}

    def test_delete_session(self, mem_db):
        mem_db.create_session("sess_del")
        assert mem_db.delete_session("sess_del") is True
        assert mem_db.get_session("sess_del") is None
        assert mem_db.delete_session("sess_del") is False


class TestForeignKeyCascades:
    def test_user_deletion_cascades_targets_and_sessions(self, mem_db):
        # 1. Setup User, Playlist, Target, Session
        mem_db.upsert_user("u1", "Alice")
        mem_db.upsert_playlist("p1", "Hits")
        mem_db.set_playlist_targets("p1", ["u1"])
        mem_db.create_session("sess_alice", user_id="u1")

        # Verify associations exist
        assert mem_db.get_playlist_targets("p1") == ["u1"]
        assert mem_db.get_session("sess_alice") is not None

        # 2. Delete User
        mem_db.delete_user("u1")

        # 3. Verify user deletion cascaded to playlist_targets and sessions
        assert mem_db.get_playlist_targets("p1") == []
        assert mem_db.get_session("sess_alice") is None

        # Playlist itself should still exist
        assert mem_db.get_playlist("p1") is not None

    def test_playlist_deletion_cascades_targets_and_missing_tracks(self, mem_db):
        # 1. Setup User, Playlist, Target, Missing Tracks
        mem_db.upsert_user("u1", "Alice")
        mem_db.upsert_playlist("p1", "Hits")
        mem_db.set_playlist_targets("p1", ["u1"])
        mem_db.record_sync_result(
            "p1", "partial", [Track(title="Song X", artist="Artist Y", album="Album Z")]
        )

        # Verify associations exist
        assert mem_db.get_playlist_targets("p1") == ["u1"]
        assert len(mem_db.get_missing_tracks("p1")) == 1

        # 2. Delete Playlist
        mem_db.delete_playlist("p1")

        # 3. Verify playlist deletion cascaded to targets and missing tracks
        assert mem_db.get_playlist_targets("p1") == []
        assert mem_db.get_missing_tracks("p1") == []

        # User should still exist
        assert mem_db.get_user("u1") is not None

    def test_foreign_key_violation_on_invalid_user_target(self, mem_db):
        mem_db.upsert_playlist("p1", "Hits")
        # Attempt to target a non-existent user should raise sqlite3.IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            mem_db.set_playlist_targets("p1", ["nonexistent_user"])


class TestAcquisitionStorage:
    def test_migration_v8_tables_and_indexes(self, mem_db):
        cursor = mem_db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "download_clients" in tables
        assert "indexers" in tables
        assert "active_downloads" in tables

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_active_downloads_status" in indexes

    def test_download_clients_crud(self, mem_db):
        client = mem_db.create_download_client(
            DownloadClientConfig(
                id="client-slskd-1",
                name="Soulseek Primary",
                driver_type=DownloadDriverType.SLSKD,
                host_url="http://slskd:5030",
                username="admin",
                password="pwd",
                priority=1,
                enabled=True,
            )
        )
        assert client["id"] is not None
        assert client["name"] == "Soulseek Primary"

        fetched = mem_db.get_download_client(client["id"])
        assert fetched is not None
        assert fetched["driver_type"] == "slskd"

        all_clients = mem_db.list_download_clients()
        assert len(all_clients) == 1

        # Update
        updated = mem_db.update_download_client(client["id"], {"priority": 5, "enabled": False})
        assert updated["priority"] == 5
        assert updated["enabled"] is False

        # Delete
        assert mem_db.delete_download_client(client["id"]) is True
        assert mem_db.get_download_client(client["id"]) is None
        assert len(mem_db.list_download_clients()) == 0

    def test_indexers_crud(self, mem_db):
        indexer = mem_db.create_indexer(
            IndexerConfig(
                id="indexer-torznab-1",
                name="Prowlarr Redacted",
                indexer_type="torznab",
                host_url="http://prowlarr:9696/1/api",
                api_key="secret",
                categories="3000,3010",
                priority=1,
                enabled=True,
            )
        )
        assert indexer["id"] is not None

        fetched = mem_db.get_indexer(indexer["id"])
        assert fetched is not None
        assert fetched["name"] == "Prowlarr Redacted"

        all_indexers = mem_db.list_indexers()
        assert len(all_indexers) == 1

        updated = mem_db.update_indexer(indexer["id"], {"name": "Prowlarr Lossless"})
        assert updated["name"] == "Prowlarr Lossless"

        assert mem_db.delete_indexer(indexer["id"]) is True
        assert mem_db.get_indexer(indexer["id"]) is None

    def test_active_downloads_crud_and_status_update(self, mem_db):
        client = mem_db.create_download_client(
            DownloadClientConfig(
                id="client-sab-1",
                name="SABnzbd",
                driver_type=DownloadDriverType.SABNZBD,
                host_url="http://sabnzbd:8080",
            )
        )

        dl = mem_db.create_active_download(
            ActiveDownload(
                id="dl-active-1",
                title="Daft Punk - Around the World",
                artist="Daft Punk",
                client_id=client["id"],
                download_hash="nzo-12345",
                status=DownloadStatus.DOWNLOADING,
                progress=42.0,
            )
        )
        assert dl["id"] is not None

        fetched = mem_db.get_active_download(dl["id"])
        assert fetched is not None
        assert fetched["progress"] == 42.0

        # Update status
        mem_db.update_download_status(
            dl["id"],
            status=DownloadStatus.COMPLETED.value,
            target_path="/music/Daft Punk/Homework/02 - Around the World.flac",
        )
        mem_db.update_download_progress(dl["id"], progress=100.0)

        completed = mem_db.get_active_download(dl["id"])
        assert completed["status"] == DownloadStatus.COMPLETED.value
        assert completed["progress"] == 100.0
        assert completed["target_path"] == "/music/Daft Punk/Homework/02 - Around the World.flac"

        # List active downloads filtered
        active_list = mem_db.list_active_downloads(statuses=[DownloadStatus.DOWNLOADING.value])
        assert len(active_list) == 0

        # List with completed
        all_list = mem_db.list_active_downloads(statuses=[DownloadStatus.COMPLETED.value])
        assert len(all_list) == 1

        # Delete
        assert mem_db.delete_active_download(dl["id"]) is True
        assert mem_db.get_active_download(dl["id"]) is None


class TestGeneralSettingsStorage:
    """Tests for migration v16 and general_settings CRUD in storage."""

    def test_migration_v16_initializes_general_settings(self, mem_db):
        cur = mem_db.conn.execute("SELECT * FROM general_settings WHERE id = 1")
        row = cur.fetchone()
        assert row is not None
        assert row["id"] == 1
        assert row["application_url"] == ""

    def test_get_general_settings_defaults_and_env(self, mem_db, monkeypatch):
        settings = mem_db.get_general_settings()
        assert settings["id"] == 1
        assert settings["application_url"] == ""

        # Test fallback to env when empty in DB
        monkeypatch.setenv("APPLICATION_URL", "https://app.local.domain")
        settings_env = mem_db.get_general_settings()
        assert settings_env["application_url"] == "https://app.local.domain"

    def test_update_general_settings(self, mem_db):
        updated = mem_db.update_general_settings({"application_url": "https://music.home.arpa/"})
        assert updated["application_url"] == "https://music.home.arpa"

        retrieved = mem_db.get_general_settings()
        assert retrieved["application_url"] == "https://music.home.arpa"
        assert retrieved["updated_at"] is not None





class TestBusyTimeout:
    def test_busy_timeout_and_wal(self, tmp_path):
        db = Database(tmp_path / "t.db")
        assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        db.close()

    def test_writer_waits_for_a_concurrent_writer_instead_of_failing(self, tmp_path):
        import sqlite3
        import threading
        import time as _time

        path = tmp_path / "t.db"
        db = Database(path)
        user = db.upsert_user("u1", "alice", "a@x.tv", is_admin=False)
        other = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")  # hold the write lock

        def release() -> None:
            _time.sleep(0.6)
            other.execute("COMMIT")

        thread = threading.Thread(target=release)
        thread.start()
        start = _time.perf_counter()
        db.record_successful_login(user["id"])  # must block ~0.6s, not raise "database is locked"
        waited = _time.perf_counter() - start
        thread.join()
        other.close()
        assert waited >= 0.4
        db.close()


class TestNoLeakedTransactions:
    """A getter that leaves an implicit transaction open keeps the write lock until another thread commits."""

    def test_settings_getters_do_not_hold_the_write_lock(self, tmp_path):
        import sqlite3

        db = Database(tmp_path / "t.db")
        db.get_instance_id()
        db.get_plex_webhook_secret()
        db.get_plex_webhook_secret()
        db.get_general_settings()
        assert db.conn.in_transaction is False
        other = sqlite3.connect(str(tmp_path / "t.db"), timeout=0.2)
        other.execute("BEGIN IMMEDIATE")  # would raise "database is locked" if the first connection held a txn
        other.execute("ROLLBACK")
        other.close()
        db.close()

    def test_create_app_shares_its_database_with_get_db(self, tmp_path, monkeypatch):
        from trackseerr.api import dependencies
        from trackseerr.api.app import create_app
        from trackseerr.config import Config

        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        db = Database(tmp_path / "sync_db.sqlite")
        create_app(db=db, config=Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path)))
        assert dependencies.get_db() is db
        db.close()


def test_ensure_connection_is_single_under_concurrent_access_after_close(tmp_path):
    """After close(), racing .conn accesses must share one connection, not each open their own."""
    import threading

    db = Database(tmp_path / "race.sqlite")
    real_open = Database._open_connection
    opened = []

    def slow_open(self):
        import time

        conn = real_open(self)
        opened.append(conn)
        time.sleep(0.05)
        return conn

    db.close()
    barrier = threading.Barrier(8)
    seen = []

    def worker():
        barrier.wait()
        seen.append(db.conn)

    with patch.object(Database, "_open_connection", slow_open):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(opened) == 1
    assert len({id(c) for c in seen}) == 1
    db.close()
