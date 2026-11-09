"""SQLite persistence engine for trackseerr with WAL mode and migrations."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Optional, Union
import weakref

logger = logging.getLogger(__name__)


class _ReaderConnection(sqlite3.Connection):
    """Subclass of sqlite3.Connection allowing weak references so reader connections can close when threads end."""


from trackseerr.list_index import fold_search_text

# Mixins
from trackseerr.user_store import UserStoreMixin
from trackseerr.account_store import AccountStoreMixin
from trackseerr.playlist_store import PlaylistStoreMixin
from trackseerr.import_list_store import ImportListStoreMixin
from trackseerr.request_store import RequestStoreMixin
from trackseerr.settings_store import SettingsStoreMixin
from trackseerr.issue_store import IssueStoreMixin
from trackseerr.download_store import DownloadStoreMixin
from trackseerr.activity_store import ActivityStoreMixin
from trackseerr.notification_store import NotificationStoreMixin
from trackseerr.library_artist_store import LibraryArtistStoreMixin
from trackseerr.library_catalog_store import LibraryCatalogStoreMixin
from trackseerr.scrobble_store import ScrobbleStoreMixin
from trackseerr.storage_migrations import MigrationsMixin
from trackseerr.quality_store import QualityCatalogMixin
from trackseerr.delay_store import DelayProfileMixin
from trackseerr.item_history import ItemHistoryMixin
from trackseerr.tag_store import TagMixin
from trackseerr.search_log_store import SearchLogStoreMixin
from trackseerr.track_alias_store import TrackAliasStoreMixin
from trackseerr.fingerprint_store import FingerprintStoreMixin
from trackseerr.slskd_challenge_store import SlskdChallengeStoreMixin
from trackseerr.health_store import HealthStoreMixin
from trackseerr.exclusion_store import ExclusionStoreMixin
from trackseerr.pack_index_store import PackIndexStoreMixin

# Re-exported: callers import these from trackseerr.storage.
from trackseerr.storage_common import (  # noqa: F401
    BASELINE_VERSION,
    LIDARR_BACKOFF_STATUSES,
    LIDARR_ERROR_BACKOFF,
    LIDARR_WEEKLY_RETRY,
    LIDARR_WEEKLY_STATUSES,
    REPLACEMENT_MESSAGE_PREFIX,
    REQUEST_FAST_RETRY_REASONS,
    REQUEST_RETRY_BACKOFF,
    REQUEST_RETRY_TAIL,
    RESERVED_USER_IDS,
    SCHEMA_VERSION,
    SEED_COMPLETE_ACTIONS,
    _BUSY_TIMEOUT_MS,
    _EPOCH,
    _KNOWN_PERMISSION_MASK,
    _LEGACY_LIDARR_OVERRIDE_COLUMNS,
    _NEAR_TITLE_RATIO,
    _RETRY_TS_FORMAT,
    _TRACK_DURATION_TOLERANCE,
    _now_us,
    _opt_float,
    _opt_int,
    _titles_near_equal,
    _us_of,
    _utcnow,
    clean_library_name,
    lidarr_item_due,
    lidarr_retry_delay,
    logger,
    request_retry_delay,
    ts_to_us,
)


class Database(
    UserStoreMixin,
    AccountStoreMixin,
    PlaylistStoreMixin,
    ImportListStoreMixin,
    RequestStoreMixin,
    SettingsStoreMixin,
    IssueStoreMixin,
    DownloadStoreMixin,
    ActivityStoreMixin,
    NotificationStoreMixin,
    LibraryArtistStoreMixin,
    LibraryCatalogStoreMixin,
    ScrobbleStoreMixin,
    MigrationsMixin,
    QualityCatalogMixin,
    DelayProfileMixin,
    ItemHistoryMixin,
    TagMixin,
    TrackAliasStoreMixin,
    FingerprintStoreMixin,
    SlskdChallengeStoreMixin,
    HealthStoreMixin,
    SearchLogStoreMixin,
    ExclusionStoreMixin,
    PackIndexStoreMixin,
):
    """Thread-safe SQLite database wrapper with WAL mode, foreign keys, and migrations."""
    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is not None:
            if str(db_path) == ":memory:":
                self.db_path: Union[str, Path] = ":memory:"
            else:
                self.db_path = Path(db_path)
        else:
            if os.path.isdir("/config"):
                self.db_path = Path("/config/sync_db.sqlite")
            elif os.path.exists("/data/sync_db.sqlite"):
                self.db_path = Path("/data/sync_db.sqlite")
            else:
                self.db_path = (
                    Path("/config/sync_db.sqlite")
                    if os.path.isdir("/config")
                    else Path("/data/sync_db.sqlite")
                )
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._read_local = threading.local()
        self._readers: weakref.WeakSet[_ReaderConnection] = weakref.WeakSet()
        self._readers_lock = threading.Lock()
        self._reader_generation = 0
        self._ensure_connection()
        self._migrate()

    def _ensure_connection(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is not None:
            return conn
        with self._lock:
            if self._conn is None:
                self._conn = self._open_connection()
            return self._conn

    def _open_connection(self) -> sqlite3.Connection:
        if self.db_path != ":memory:":
            assert isinstance(self.db_path, Path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            parent_dir = self.db_path.parent
            if not os.access(parent_dir, os.W_OK):
                uid = os.getuid() if hasattr(os, "getuid") else "N/A"
                gid = os.getgid() if hasattr(os, "getgid") else "N/A"
                raise PermissionError(
                    f"Database directory '{parent_dir}' is not writable (UID {uid}, GID {gid}). "
                    f"Please verify permissions on your appdata volume or configure PUID/PGID."
                )
            if self.db_path.exists() and not os.access(self.db_path, os.W_OK):
                uid = os.getuid() if hasattr(os, "getuid") else "N/A"
                gid = os.getgid() if hasattr(os, "getgid") else "N/A"
                raise PermissionError(
                    f"Database file '{self.db_path}' exists but is not writable (UID {uid}, GID {gid}). "
                    f"Please verify permissions on your appdata volume."
                )
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
            except sqlite3.OperationalError as e:
                uid = os.getuid() if hasattr(os, "getuid") else "N/A"
                gid = os.getgid() if hasattr(os, "getgid") else "N/A"
                raise sqlite3.OperationalError(
                    f"Failed to open SQLite database at '{self.db_path}': {e}. "
                    f"Ensure directory '{parent_dir}' is writable by user UID {uid} / GID {gid}."
                ) from e
        else:
            conn = sqlite3.connect(":memory:", timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)

        self._configure(conn, cache_kib=65536, read_only=False)
        return conn

    def _configure(self, conn: sqlite3.Connection, *, cache_kib: int, read_only: bool) -> None:
        conn.row_factory = sqlite3.Row
        conn.create_function("fold_text", 1, fold_search_text, deterministic=True)
        # busy_timeout first: switching to WAL and every later statement must wait for a competing writer
        # instead of failing at once with "database is locked".
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL;")
        # NORMAL is SQLite's recommended pairing with WAL: no corruption risk, only the last commits can be lost on power loss.
        conn.execute("PRAGMA synchronous=NORMAL;")
        # Negative cache_size sets SQLite page cache in KiB; -65536 gives a 64 MiB cache for hot pages.
        conn.execute(f"PRAGMA cache_size = -{cache_kib};")
        # Keep temporary tables and indices in memory rather than spilling to disk.
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA foreign_keys = ON;")
        if read_only:
            conn.execute("PRAGMA query_only = ON;")

    def _open_reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,
            factory=_ReaderConnection,
        )
        self._configure(conn, cache_kib=16384, read_only=True)
        with self._readers_lock:
            self._readers.add(conn)
        return conn

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a thread-local read-only SQLite connection.

        Contract: read-only SELECTs only; sees every committed write; never takes
        self._lock for file databases.
        """
        if self.db_path == ":memory:":
            with self._lock:
                yield self.conn
            return

        # A caller inside a write must read its own uncommitted rows, so it
        # keeps using the writer connection (it already holds the lock).
        if self._lock._is_owned():
            yield self.conn
            return

        conn = getattr(self._read_local, "conn", None)
        gen = getattr(self._read_local, "generation", None)
        if conn is not None and gen != self._reader_generation:
            try:
                conn.close()
            except sqlite3.Error as e:
                logger.debug("Failed to close stale reader connection: %s", e)
            conn = None
            self._read_local.conn = None
            self._read_local.generation = None

        if conn is None:
            conn = self._open_reader()
            self._read_local.conn = conn
            self._read_local.generation = self._reader_generation

        try:
            yield conn
        except sqlite3.ProgrammingError:
            self._read_local.conn = None
            self._read_local.generation = None
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._ensure_connection()

    def __enter__(self) -> "Database":
        self._ensure_connection()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error as e:
                    logger.debug("Failed to close primary connection: %s", e)
                self._conn = None
        with self._readers_lock:
            # Closing a connection another thread may be executing on crashes the
            # interpreter (SIGSEGV / exit 139). Do NOT iterate and close self._readers.
            # Instead, increment self._reader_generation so every other thread replaces
            # its stale reader on its next reader() call, and readers of finished threads
            # are closed when the thread's locals are collected. Only close the calling
            # thread's own reader connection here.
            self._reader_generation += 1
            calling_conn = getattr(self._read_local, "conn", None)
            if calling_conn is not None:
                try:
                    calling_conn.close()
                except sqlite3.Error as e:
                    logger.debug("Failed to close calling thread reader connection: %s", e)
                self._read_local.conn = None
                self._read_local.generation = None

    def record_nonce(self, nonce: str, now: int, ttl_seconds: int = 120) -> bool:
        """Records a nonce; returns False if it was already seen within ``ttl_seconds`` (a replay)."""
        with self._lock:
            self.conn.execute("DELETE FROM internal_nonces WHERE seen_at < ?", (int(now) - int(ttl_seconds),))
            try:
                self.conn.execute(
                    "INSERT INTO internal_nonces (nonce, seen_at) VALUES (?, ?)", (str(nonce), int(now))
                )
            except sqlite3.IntegrityError:
                self.conn.commit()
                return False
            self.conn.commit()
            return True

    def _migration_v25(self, cur: sqlite3.Cursor) -> None:
        """Per-user scrobbling, listens, Last.fm auth states, tailored mixes and server-level scrobble settings."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_scrobble_configs (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                scrobbling_enabled INTEGER NOT NULL DEFAULT 1,
                lastfm_username TEXT,
                lastfm_session_key TEXT,
                listenbrainz_token TEXT,
                listenbrainz_username TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_listens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                album TEXT,
                rating_key TEXT,
                duration_ms INTEGER,
                played_at TEXT NOT NULL,
                source TEXT NOT NULL,
                lastfm_status TEXT NOT NULL DEFAULT 'skipped',
                listenbrainz_status TEXT NOT NULL DEFAULT 'skipped',
                forward_attempts INTEGER NOT NULL DEFAULT 0,
                last_forward_error TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_listens_lookup ON user_listens(user_id, played_at DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_listens_artist ON user_listens(user_id, artist);")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_listens_pending ON user_listens(lastfm_status, listenbrainz_status);"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lastfm_auth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                forward_url TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tailored_mix_configs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mix_type TEXT NOT NULL,
                name TEXT NOT NULL,
                seed_artist TEXT,
                track_count INTEGER NOT NULL DEFAULT 30,
                discovery_ratio REAL NOT NULL DEFAULT 0.7,
                seed_window_days INTEGER NOT NULL DEFAULT 14,
                excluded_genres_json TEXT NOT NULL DEFAULT '[]',
                auto_acquire_missing INTEGER NOT NULL DEFAULT 0,
                max_weekly_acquisitions INTEGER NOT NULL DEFAULT 10,
                quality_profile_id TEXT REFERENCES quality_profiles(id) ON DELETE SET NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_generated_at TEXT,
                last_result_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tailored_mix_user ON tailored_mix_configs(user_id);")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mix_acquisitions (
                mix_id TEXT NOT NULL REFERENCES tailored_mix_configs(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (mix_id, request_id)
            )
            """
        )
        # Small key/value table for worker cursors (history poll cursor, cached Plex admin username).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scrobble_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("PRAGMA table_info(general_settings);")
        gs_cols = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("lastfm_api_key", "TEXT NOT NULL DEFAULT ''"),
            ("lastfm_api_secret", "TEXT NOT NULL DEFAULT ''"),
            ("plex_webhook_secret", "TEXT NOT NULL DEFAULT ''"),
            ("plex_history_poll_minutes", "INTEGER NOT NULL DEFAULT 15"),
        ):
            if col not in gs_cols:
                cur.execute(f"ALTER TABLE general_settings ADD COLUMN {col} {ddl};")

