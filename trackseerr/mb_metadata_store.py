"""Persistent SQLite-backed MusicBrainz metadata cache and redirect store."""

from datetime import datetime, timedelta, timezone
import json
import logging
import random
import sqlite3
import threading
from typing import TYPE_CHECKING, Any, Optional

from trackseerr.storage import Database

if TYPE_CHECKING:
    from trackseerr.clients.discovery import DiscoveryClient
    from trackseerr.clients.mbid_enricher import MbidEnricherClient

logger = logging.getLogger(__name__)

# TTL constants in seconds
TTL_LOOKUP = 30 * 86400.0  # kinds: artist_lookup, url_lookup, track_lookup, album_lookup
TTL_ARTIST_DETAILS = 7 * 86400.0
TTL_DISCOGRAPHY = 20 * 3600.0
TTL_RG_TRACKS = 30 * 86400.0
TTL_RG_LOOKUP = 30 * 86400.0
TTL_NEGATIVE = 1 * 86400.0

TTL_DEEZER_ALBUM = 30 * 86400.0
TTL_DEEZER_ARTIST = 7 * 86400.0
TTL_DEEZER_TOP = 1 * 86400.0


def ttl_with_jitter(base: float) -> float:
    """Adds 0-15% random jitter to positive TTLs; negative TTL (TTL_NEGATIVE) and non-positive TTLs are unjittered."""
    base_val = float(base)
    if base_val <= 0.0 or base_val == TTL_NEGATIVE:
        return base_val
    return base_val * (1.0 + random.uniform(0.0, 0.15))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(ttl_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=float(ttl_seconds))).isoformat()


class MbMetadataStore:
    """Thread-safe persistent cache for MusicBrainz API responses and ID redirects."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, cache_key: str) -> tuple[bool, Any]:
        """Looks up a cached entry. Returns (True, payload) on unexpired hit, else (False, None)."""
        key = str(cache_key)
        try:
            with self.db._lock:
                row = self.db.conn.execute(
                    "SELECT payload_json, expires_at FROM mb_metadata_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
            if not row:
                return False, None

            expires_at_str = str(row["expires_at"])
            now_dt = datetime.now(timezone.utc)
            try:
                exp_dt = datetime.fromisoformat(expires_at_str)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt <= now_dt:
                    return False, None
            except (ValueError, TypeError):
                if expires_at_str <= now_dt.isoformat():
                    return False, None

            payload = json.loads(row["payload_json"])
            return True, payload
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("MbMetadataStore: failed reading cache key %r: %s", key, exc)
            return False, None

    def put(self, cache_key: str, kind: str, payload: Any, ttl_seconds: float) -> None:
        """Upserts a cache entry with kind, JSON payload, and computed expiry."""
        key = str(cache_key)
        try:
            payload_json = json.dumps(payload)
            fetched_at = _now_iso()
            expires_at = _expires_iso(ttl_seconds)
            with self.db._lock:
                self.db.conn.execute(
                    """INSERT INTO mb_metadata_cache (cache_key, kind, payload_json, fetched_at, expires_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                           kind = excluded.kind,
                           payload_json = excluded.payload_json,
                           fetched_at = excluded.fetched_at,
                           expires_at = excluded.expires_at""",
                    (key, str(kind), payload_json, fetched_at, expires_at),
                )
                self.db.conn.commit()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            logger.warning("MbMetadataStore: failed writing cache key %r: %s", key, exc)

    def record_redirect(self, old_id: str, new_id: str, entity_type: str) -> None:
        """Records an ID redirect (e.g. merged MusicBrainz entity). Ignored if old == new."""
        old = str(old_id).strip().lower()
        new = str(new_id).strip().lower()
        if not old or not new or old == new:
            return
        seen_at = _now_iso()
        try:
            with self.db._lock:
                self.db.conn.execute(
                    """INSERT INTO mb_id_redirects (old_id, new_id, entity_type, seen_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(old_id) DO UPDATE SET
                           new_id = excluded.new_id,
                           entity_type = excluded.entity_type,
                           seen_at = excluded.seen_at""",
                    (old, new, str(entity_type), seen_at),
                )
                self.db.conn.commit()
        except sqlite3.Error as exc:
            logger.warning("MbMetadataStore: failed recording redirect %r -> %r: %s", old, new, exc)

    def resolve_redirect(self, mbid: str) -> str:
        """Follows redirect chains up to 5 hops, stopping on cycles. Returns input if no redirect."""
        if not mbid or not isinstance(mbid, str):
            return mbid
        clean_mbid = mbid.strip()
        if not clean_mbid:
            return mbid

        curr_id = clean_mbid.lower()
        visited = {curr_id}
        hops = 0
        max_hops = 5
        found_any = False

        try:
            with self.db._lock:
                while hops < max_hops:
                    row = self.db.conn.execute(
                        "SELECT new_id FROM mb_id_redirects WHERE old_id = ?",
                        (curr_id,),
                    ).fetchone()
                    if not row:
                        break
                    next_id = str(row["new_id"]).strip().lower()
                    found_any = True
                    if next_id in visited:
                        break
                    visited.add(next_id)
                    curr_id = next_id
                    hops += 1
        except sqlite3.Error as exc:
            logger.warning("MbMetadataStore: failed resolving redirect for %r: %s", mbid, exc)
            return mbid

        return curr_id if found_any else mbid

    def prune_expired(self) -> int:
        """Deletes expired entries and returns count of removed rows."""
        now_str = _now_iso()
        try:
            with self.db._lock:
                cur = self.db.conn.execute(
                    "DELETE FROM mb_metadata_cache WHERE expires_at <= ?",
                    (now_str,),
                )
                self.db.conn.commit()
                return int(cur.rowcount or 0)
        except sqlite3.Error as exc:
            logger.warning("MbMetadataStore: failed pruning expired cache rows: %s", exc)
            return 0

    def stats(self) -> dict[str, int]:
        """Returns row count grouped by kind."""
        try:
            with self.db._lock:
                rows = self.db.conn.execute(
                    "SELECT kind, COUNT(*) as cnt FROM mb_metadata_cache GROUP BY kind"
                ).fetchall()
                return {str(row["kind"]): int(row["cnt"]) for row in rows}
        except sqlite3.Error as exc:
            logger.warning("MbMetadataStore: failed fetching stats: %s", exc)
            return {}


_shared_lock = threading.Lock()
_shared_enricher: Optional[Any] = None
_shared_db: Optional[Database] = None
_shared_mirror: Optional[str] = None
_shared_discovery: Optional[Any] = None
_shared_discovery_db: Optional[Database] = None


def get_shared_enricher(db: Database) -> "MbidEnricherClient":
    """Module-level singleton enricher client configured with db and mirror settings."""
    global _shared_enricher, _shared_db, _shared_mirror
    from trackseerr.clients.mbid_enricher import MbidEnricherClient

    mirror = db.get_media_management_settings().get("mb_mirror_url") or None
    with _shared_lock:
        if (
            _shared_enricher is None
            or _shared_db is not db
            or _shared_mirror != mirror
        ):
            store = MbMetadataStore(db)
            _shared_enricher = MbidEnricherClient(
                base_url=mirror,
                timeout=10.0,
                store=store,
            )
            _shared_db = db
            _shared_mirror = mirror
        return _shared_enricher


def get_shared_discovery_client(db: Database) -> "DiscoveryClient":
    """Module-level singleton discovery client configured with db persistent store."""
    global _shared_discovery, _shared_discovery_db
    from trackseerr.clients.discovery import DiscoveryClient

    with _shared_lock:
        if (
            _shared_discovery is None
            or _shared_discovery_db is not db
        ):
            store = MbMetadataStore(db)
            _shared_discovery = DiscoveryClient(store=store)
            _shared_discovery_db = db
        return _shared_discovery


def reset_shared_discovery_client() -> None:
    """Resets the module-level singleton discovery client (for testing)."""
    global _shared_discovery, _shared_discovery_db
    with _shared_lock:
        _shared_discovery = None
        _shared_discovery_db = None


def reset_shared_enricher() -> None:
    """Resets the module-level singleton enricher client (for testing)."""
    global _shared_enricher, _shared_db, _shared_mirror
    with _shared_lock:
        _shared_enricher = None
        _shared_db = None
        _shared_mirror = None
    reset_shared_discovery_client()


def reset_shared_clients() -> None:
    """Resets both shared enricher and discovery clients."""
    reset_shared_enricher()
    reset_shared_discovery_client()
