"""Background sync routes and SSE live log streaming."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
import hashlib
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_deezer_client,
    get_media_client,
    get_spotify_client,
    require_admin,
    verify_feed_access,
)
from trackseerr.api.schemas.sync import SyncStatusResponse, SyncTriggerResponse, SyncWebhookResponse
from trackseerr.clients.deezer import DeezerClient
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import MediaServerError, PlaylistSyncOptions, as_media_server, describe_error, registry
from trackseerr.media_servers.plex import read_playlist_items, refresh_mix_snapshots
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.media_servers.user_logins import sync_subsonic_for_users
from trackseerr.clients.spotify import SpotifyClient
from trackseerr.clients.youtube_music import get_shared_youtube_music_client
from trackseerr.config import MEDIA_SERVER_SUBSONIC, Config
from trackseerr.job_tracker import job_tracker, tracked
from trackseerr.task_manager import TRIGGER_MANUAL, record_task_run
from trackseerr.list_monitoring import apply_playlist_missing_safely
from trackseerr.listening_playlists import fetch_listening_tracks
from trackseerr.playlist_policy import is_listening_playlist, is_smart_collection
from trackseerr.smart_collections import SmartCollectionError, refresh_tracks
from trackseerr.models import Playlist, RequestStatus, SyncResult, Track
from trackseerr.native_match import match_playlist_tracks_native, resolve_playlist_library_hits
from trackseerr.playlist_identity import LibraryHit
from trackseerr.storage import Database
from trackseerr.track_aliases import apply_track_aliases, restore_missing
from trackseerr.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

router = APIRouter()


class BroadcastLogHandler(logging.Handler):
    """Logging handler that thread-safely broadcasts formatted log records to active asyncio queues."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # This handler sits on the package logger, ahead of the root redaction filters: redact here too.
        msg = redact_text(self.format(record))
        with self._lock:
            active_listeners = list(self.listeners)

        for loop, q in active_listeners:
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(self._safe_put, q, msg)
            except Exception:
                pass

    @staticmethod
    def _safe_put(q: asyncio.Queue, msg: str) -> None:
        try:
            q.put_nowait(msg)
        except (asyncio.QueueFull, Exception):
            pass

    def add_listener(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners.append((loop, q))

    def remove_listener(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners = [item for item in self.listeners if item[1] is not q]


def _effective_tracks(db: Database, tracks: list[Track], playlist_name: str) -> tuple[list[Track], dict[int, Track], set[int]]:
    """Applies approved track aliases; a storage failure falls back to the unaliased tracks (logged)."""
    try:
        return apply_track_aliases(db, tracks)
    except sqlite3.Error as e:
        logger.warning("Track aliases not applied for playlist '%s': %s", playlist_name, safe_exc(e))
        return list(tracks), {}, set()


_FULL_RESYNC_AFTER = timedelta(hours=24)  # an unchanged playlist is still fully resynced once a day
_IMPORT_RESCAN_GRACE = timedelta(minutes=30)  # library changes this close to a sync may predate the media-server scan


def _sync_fingerprint(
    tracks: list[Track],
    target_usernames: list[str],
    push: bool,
    monitor_mode: Optional[str],
    server_ids: Sequence[str] = (),
) -> str:
    """Hash of everything that decides a playlist's sync result apart from the library: tracks, targets, push, mode, servers."""
    payload = {
        "tracks": [[t.title, t.artist, t.album, t.url, t.duration_seconds] for t in tracks],
        "targets": sorted(target_usernames),
        "push": push,
        "monitor_mode": monitor_mode or "",
        "servers": sorted(server_ids),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _playlist_servers(
    db: Database, pl: dict[str, Any], fallback_server: Optional[Any]
) -> list[tuple[Optional[ServerRecord], Any]]:
    """Return ``(record, adapter)`` pairs for pushing ``pl``, primary first, minus per-playlist excludes."""
    if registry.is_installed():
        excludes = set(db.get_playlist_server_excludes(pl["id"]))
        pairs: list[tuple[Optional[ServerRecord], Any]] = [
            (rec, adapter)
            for rec, adapter in registry.media_servers(registry.ROLE_PLAYLISTS)
            if rec.id not in excludes
        ]
        primary = registry.primary_record()
        if primary is not None:
            pairs.sort(key=lambda pair: 0 if pair[0] is not None and pair[0].id == primary.id else 1)
        return pairs
    return [(None, fallback_server)] if fallback_server else []


def _server_push_outcome(results: Sequence[SyncResult], total_tracks: int) -> tuple[str, int, list[Track], str]:
    """Derive ``(status, matched_count, missing, error_msg)`` from per-target push results.

    ``status`` is ``"skipped"`` when every target lacked an account on the server, ``"success"`` when every target
    with an account succeeded, and ``"failed"`` otherwise.
    """
    if not results:
        return "failed", 0, [], ""
    non_skipped = [r for r in results if not getattr(r, "skipped", False)]
    if not non_skipped:
        skip_reason = next((r.skip_reason or r.error for r in results if r.skip_reason or r.error), "")
        return "skipped", 0, [], skip_reason
    if all(r.success for r in non_skipped):
        chosen = non_skipped[0]
        missing = list(getattr(chosen, "missing", None) or [])
        matched_count = max(0, total_tracks - len(missing))
        return "success", matched_count, missing, ""
    chosen = next((r for r in non_skipped if r.success), non_skipped[0])
    missing = list(getattr(chosen, "missing", None) or [])
    matched_count = max(0, total_tracks - len(missing))
    error_msg = next((r.error for r in non_skipped if not r.success and r.error), "")
    return "failed", matched_count, missing, error_msg


def _push_results_outcome(results: Sequence[SyncResult], total_tracks: int) -> tuple[bool, int, list[Track]]:
    """Derive ``(success, matched_count, missing)`` from per-target push results."""
    status, matched_count, missing, _ = _server_push_outcome(results, total_tracks)
    return status == "success", matched_count, missing


def _target_server_username(
    db: Database,
    rec: ServerRecord,
    user: Optional[dict[str, Any]],
    target_name: str,
) -> str:
    """Return the lowercase server-side username for ``target_name`` on ``rec``."""
    if user is not None:
        account = db.get_server_account(user["id"], rec.id)
        if account is not None and account.get("username"):
            return str(account["username"]).strip().lower()
        link = db.get_server_user_link(user["id"], rec.id)
        if link is not None and link.get("server_username"):
            return str(link["server_username"]).strip().lower()
    return target_name.strip().lower()


def _resolve_empty_adopted_owner(
    db: Database,
    rec: ServerRecord,
    adapter: Any,
    playlist_id: str,
    source: dict[str, Any],
) -> str:
    """Look up and persist the source playlist owner when ``server_user`` is empty."""
    list_fn = getattr(adapter, "list_source_playlists", None)
    source_pid = str(source.get("server_playlist_id") or "").strip()
    if not callable(list_fn) or not source_pid:
        return ""
    try:
        playlists = list_fn()
    except MediaServerError:
        return ""
    match = next((p for p in playlists if str(p.get("id") or "") == source_pid), None)
    owner = str(match.get("owner") or "").strip() if isinstance(match, dict) else ""
    if not owner:
        return ""
    db.set_server_playlist_source(playlist_id, rec.id, source_pid, owner)
    return owner.lower()


def _filter_adopted_source_owner(
    db: Database,
    rec: Optional[ServerRecord],
    adapter: Any,
    playlist: Playlist,
    target_usernames: list[str],
) -> tuple[list[str], list[SyncResult]]:
    """Drop the source playlist's owner when pushing an adopted playlist back to its source server."""
    if rec is None:
        return target_usernames, []
    source = db.get_server_playlist_source(playlist.id)
    if source is None or source.get("server_id") != rec.id:
        return target_usernames, []

    owner_key = str(source.get("server_user") or "").strip().lower()
    if not owner_key:
        owner_key = _resolve_empty_adopted_owner(db, rec, adapter, playlist.id, source)
    if not owner_key:
        owner_key = str(getattr(adapter, "_user", "") or rec.username or "").strip().lower()
    if not owner_key:
        return target_usernames, []

    users_by_name = {
        str(u.get("username") or "").strip().lower(): u
        for u in db.list_users()
        if u.get("username")
    }
    kept: list[str] = []
    skipped_results: list[SyncResult] = []
    total = len(playlist.tracks)
    for name in target_usernames:
        cleaned = str(name or "").strip()
        user = users_by_name.get(cleaned.lower()) if cleaned else None
        if _target_server_username(db, rec, user, cleaned) == owner_key:
            logger.info(
                "Skipping target '%s' on '%s' for adopted playlist '%s': This is the original playlist",
                cleaned,
                rec.name,
                playlist.name,
            )
            skipped_results.append(
                SyncResult(
                    playlist_name=playlist.name,
                    total_tracks=total,
                    matched_tracks=0,
                    missing_tracks=0,
                    success=False,
                    error="This is the original playlist",
                    skipped=True,
                    skip_reason="This is the original playlist",
                )
            )
        else:
            kept.append(name)
    return kept, skipped_results


def _map_linked_server_usernames(
    db: Database,
    rec: ServerRecord,
    target_usernames: list[str],
) -> list[str]:
    """Map each Trackseerr target username to its linked ``server_username`` on ``rec``, if any."""
    users_by_name = {
        str(u.get("username") or "").strip().lower(): u
        for u in db.list_users()
        if u.get("username")
    }
    mapped: list[str] = []
    for name in target_usernames:
        cleaned = str(name or "").strip()
        user = users_by_name.get(cleaned.lower()) if cleaned else None
        link = db.get_server_user_link(user["id"], rec.id) if user is not None else None
        linked_name = str(link.get("server_username") or "").strip() if link is not None else ""
        mapped.append(linked_name or name)
    return mapped


def _dispatch_server_push(
    db: Database,
    config: Config,
    rec: Optional[ServerRecord],
    adapter: Any,
    model_playlist: Playlist,
    target_usernames: list[str],
    options: PlaylistSyncOptions,
) -> list[SyncResult]:
    """Push ``model_playlist`` to ``adapter``, skipping the source owner on adopted playlists."""
    filtered_targets, skipped_results = _filter_adopted_source_owner(
        db, rec, adapter, model_playlist, target_usernames
    )
    if not filtered_targets:
        return skipped_results
    if rec is not None and rec.kind == MEDIA_SERVER_SUBSONIC:
        pushed = sync_subsonic_for_users(
            db, config, rec, adapter, model_playlist, filtered_targets, options
        )
    else:
        targets = (
            _map_linked_server_usernames(db, rec, filtered_targets)
            if rec is not None
            else filtered_targets
        )
        pushed = list(adapter.sync_playlist(model_playlist, targets, options))
    return [*skipped_results, *pushed]


def _push_to_extra_server(
    db: Database,
    config: Config,
    rec: ServerRecord,
    adapter: Any,
    model_playlist: Playlist,
    target_usernames: list[str],
    skip_item_ids: Optional[set[str]] = None,
    library_hits: Optional[dict[int, LibraryHit]] = None,
    library_missing: Optional[set[int]] = None,
) -> None:
    """Push ``model_playlist`` to an additional playlist server and record only its per-server state."""
    try:
        options = PlaylistSyncOptions.from_config(
            config,
            db=db,
            skip_item_ids=skip_item_ids,
            library_hits=library_hits,
            library_missing=library_missing,
        )
        results = _dispatch_server_push(
            db, config, rec, adapter, model_playlist, target_usernames, options
        )
        server_status, matched_count, missing, error_msg = _server_push_outcome(results, len(model_playlist.tracks))
        db.record_playlist_server_state(
            model_playlist.id,
            rec.id,
            server_status,
            matched=matched_count,
            missing=len(missing),
            error=error_msg,
        )
    except MediaServerError as e:
        logger.error("Error syncing playlist '%s' to %s: %s", model_playlist.name, rec.name, e.safe_detail)
        db.record_playlist_server_state(
            model_playlist.id,
            rec.id,
            "failed",
            error=e.safe_detail,
        )
    except Exception as e:  # one extra server must not stop remaining servers or alter overall status
        detail = describe_error(e)
        logger.error("Error syncing playlist '%s' to %s: %s", model_playlist.name, rec.name, detail)
        logger.debug("Extra server playlist sync traceback", exc_info=True)
        db.record_playlist_server_state(
            model_playlist.id,
            rec.id,
            "failed",
            error=detail,
        )


class SyncState:
    """Manages active sync status, statistics, and log streaming."""

    def __init__(self) -> None:
        self.is_syncing: bool = False
        self.progress: Optional[tuple[int, int, str, Optional[str]]] = None
        self._no_target_logged: set[str] = set()
        self.last_run_at: Optional[str] = None
        self.last_run_stats: dict[str, Any] = {
            "total_playlists": 0,
            "success_count": 0,
            "total_matched": 0,
            "total_missing": 0,
            "skipped_unchanged": 0,
        }
        self.log_handler = BroadcastLogHandler()
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        self.log_handler.setFormatter(formatter)
        logging.getLogger("trackseerr").addHandler(self.log_handler)

    @staticmethod
    def _refresh_adopted_playlist(
        db: Database, plex_client: Optional[PlexClient], pl: dict[str, Any]
    ) -> Optional[set[str]]:
        """Refresh tracks_json of a service='plex' adopted playlist from its source.

        Returns the set of source rating keys that must not be written to (None if unresolved).
        Falls back to the stored snapshot when the source is gone or Plex is unreachable.
        """
        registry = db.get_plex_registry_by_adopted(pl["id"])
        if registry is None:
            logger.warning("Adopted playlist '%s' has no source registry row; using stored snapshot", pl["name"])
            return None
        skip = {str(registry["rating_key"])}
        if plex_client is None:
            return skip
        try:
            items = read_playlist_items(as_media_server(plex_client), registry["plex_user"], registry["rating_key"])
        except MediaServerError as e:
            logger.warning(
                "Source Plex playlist for adopted '%s' unavailable (%s); using stored snapshot", pl["name"], e.safe_detail
            )
            return skip
        snapshot = [
            {
                "title": str(getattr(i, "title", "") or ""),
                "artist": str(getattr(i, "grandparentTitle", "") or ""),
                "album": str(getattr(i, "parentTitle", "") or ""),
            }
            for i in items
        ]
        payload = json.dumps(snapshot)
        db.set_playlist_tracks_json(pl["id"], payload)
        pl["tracks_json"] = payload
        return skip

    @staticmethod
    def _refresh_server_adopted(db: Database, pl: dict[str, Any], source: dict[str, Any]) -> None:
        """Refresh ``tracks_json`` of a Subsonic/Jellyfin adopted playlist from its originating server.

        Falls back to the stored snapshot when the source server is missing, unreachable, or returns an error.
        """
        pair = registry.server_by_id(source["server_id"])
        if pair is None:
            logger.warning(
                "Source server '%s' for adopted playlist '%s' is unavailable; using stored snapshot",
                source["server_id"],
                pl["name"],
            )
            return
        _, adapter = pair
        try:
            tracks = adapter.source_playlist_tracks(
                source["server_playlist_id"],
                user=source.get("server_user") or None,
            )
        except MediaServerError as e:
            logger.warning(
                "Source playlist for adopted '%s' unavailable (%s); using stored snapshot",
                pl["name"],
                e.safe_detail,
            )
            return
        payload = json.dumps(tracks)
        db.set_playlist_tracks_json(pl["id"], payload)
        pl["tracks_json"] = payload

    @staticmethod
    def _tracks_to_json(tracks: list[Track]) -> str:
        return json.dumps([{"title": t.title, "artist": t.artist, "album": t.album, "url": t.url} for t in tracks])

    @staticmethod
    def _stored_tracks(pl: dict[str, Any]) -> list[Track]:
        raw = pl.get("tracks_json")
        if not raw:
            return []
        return [
            Track(
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                album=t.get("album", ""),
                url=t.get("url", ""),
            )
            for t in json.loads(raw)
            if t.get("title")
        ]

    def _load_source_tracks(
        self,
        db: Database,
        config: Config,
        pl: dict[str, Any],
        spotify_client: Optional[SpotifyClient],
        deezer_client: Optional[DeezerClient],
    ) -> list[Track]:
        """The playlist's current tracks: smart/listening rebuild, live Spotify/Deezer fetch (snapshotted), else stored list."""
        pl_id = pl["id"]
        service = pl.get("service", "spotify")
        if is_smart_collection(pl):
            return refresh_tracks(db, pl)
        if is_listening_playlist(pl):
            # Re-fetched every sync with the owner's linked account (a created-for playlist
            # re-resolves to its newest edition); the snapshot keeps the last good list.
            tracks = fetch_listening_tracks(db, config, pl)
            db.set_playlist_tracks_json(pl_id, self._tracks_to_json(tracks))
            return tracks
        client = (
            spotify_client
            if service == "spotify"
            else deezer_client
            if service == "deezer"
            else get_shared_youtube_music_client()
            if service == "youtube_music"
            else None
        )
        if client is not None and not pl_id.startswith("imp_"):
            live = client.get_playlist_tracks(pl_id)
            if live:
                db.set_playlist_tracks_json(pl_id, self._tracks_to_json(live))
                return live
            stored = self._stored_tracks(pl)
            if stored:
                logger.warning(
                    "Live fetch for playlist '%s' returned no tracks; matching against the stored snapshot", pl["name"]
                )
                return stored
            return live or []
        return self._stored_tracks(pl)

    def _resolve_targets(self, db: Database, pl: dict[str, Any]) -> list[str]:
        """Usernames of the playlist's target users; empty means match-only (logged once per playlist)."""
        pl_id = pl["id"]
        usernames: list[str] = []
        for uid in db.get_playlist_targets(pl_id):
            user_row = db.get_user(uid)
            if user_row:
                usernames.append(user_row["username"])
        if usernames:
            self._no_target_logged.discard(pl_id)
            return usernames
        message = "Playlist '%s' has no target users; matching against the library only, not pushing to the media server"
        if pl_id in self._no_target_logged:
            logger.debug(message, pl["name"])
        else:
            self._no_target_logged.add(pl_id)
            logger.info(message, pl["name"])
        return usernames

    @staticmethod
    def _match_natively(
        db: Database,
        config: Config,
        pl: dict[str, Any],
        tracks: list[Track],
        original_by_effective: dict[int, Track],
        ignored_ids: set[int],
        stats: dict[str, int],
        fingerprint: str,
    ) -> None:
        """Matches a playlist against the native library and records its missing tracks; nothing is pushed."""
        pl_id = pl["id"]
        try:
            matched, missing = match_playlist_tracks_native(db, tracks)
            missing = restore_missing(missing, original_by_effective, ignored_ids)
            db.record_sync_result(playlist_id=pl_id, status="success", missing_tracks=missing, fingerprint=fingerprint)
            apply_playlist_missing_safely(db, config, pl_id)
            stats["success_count"] += 1
            stats["total_matched"] += len(matched)
            stats["total_missing"] += len(missing)
        except Exception as e:  # one playlist must not stop the cycle; the root cause is logged
            logger.error("Native library match failed for playlist '%s': %s", pl["name"], safe_exc(e))
            logger.debug("Native match traceback", exc_info=True)
            db.record_sync_result(playlist_id=pl_id, status="failed")

    @staticmethod
    def _push_playlist(
        db: Database,
        config: Config,
        server: Any,
        pl: dict[str, Any],
        model_playlist: Playlist,
        target_usernames: list[str],
        skip_rating_keys: Optional[set[str]],
        original_by_effective: dict[int, Track],
        ignored_ids: set[int],
        stats: dict[str, int],
        fingerprint: str,
        server_name: str = "Plex",
        rec: Optional[ServerRecord] = None,
        library_hits: Optional[dict[int, LibraryHit]] = None,
        library_missing: Optional[set[int]] = None,
    ) -> tuple[str, int, int, str]:
        """Pushes a playlist to the primary media server for its target users and records the outcome."""
        pl_id = pl["id"]
        try:
            options = PlaylistSyncOptions.from_config(
                config,
                db=db,
                skip_item_ids=skip_rating_keys,
                library_hits=library_hits,
                library_missing=library_missing,
            )
            results = _dispatch_server_push(
                db, config, rec, server, model_playlist, target_usernames, options
            )
            server_status, matched_count, missing, error_msg = _server_push_outcome(
                results, len(model_playlist.tracks)
            )
            if server_status == "skipped":
                if library_missing is not None:
                    missing = [t for idx, t in enumerate(model_playlist.tracks) if idx in library_missing]
                else:
                    _, missing = match_playlist_tracks_native(db, model_playlist.tracks)
                missing = restore_missing(missing, original_by_effective, ignored_ids)
                db.record_sync_result(playlist_id=pl_id, status="skipped", missing_tracks=missing)
                apply_playlist_missing_safely(db, config, pl_id)
                return "skipped", 0, 0, error_msg
            success = server_status == "success"
            missing = restore_missing(missing, original_by_effective, ignored_ids)
            db.record_sync_result(
                playlist_id=pl_id,
                status="success" if success else "failed",
                missing_tracks=missing,
                fingerprint=fingerprint,
            )
            apply_playlist_missing_safely(db, config, pl_id)
            if success:
                stats["success_count"] += 1
            stats["total_matched"] += matched_count
            stats["total_missing"] += len(missing)
            return server_status, matched_count, len(missing), error_msg
        except Exception as e:
            detail = describe_error(e)
            logger.error("Error syncing playlist '%s' to %s: %s", pl["name"], server_name, detail)
            logger.debug("Playlist sync traceback", exc_info=True)
            db.record_sync_result(playlist_id=pl_id, status="failed")
            return "failed", 0, 0, detail

    def _push_to_servers(
        self,
        db: Database,
        config: Config,
        servers: list[tuple[Optional[ServerRecord], Any]],
        pl: dict[str, Any],
        model_playlist: Playlist,
        target_usernames: list[str],
        skip_rating_keys: Optional[set[str]],
        original_by_effective: dict[int, Track],
        ignored_ids: set[int],
        stats: dict[str, int],
        fingerprint: str,
    ) -> None:
        """Push to the primary/first server (driving playlist status) and fan out to remaining servers."""
        library_hits, library_missing = resolve_playlist_library_hits(db, model_playlist.tracks)
        first_rec, first_server = servers[0]
        first_skip = skip_rating_keys if (first_rec is None or first_rec.is_login) else None
        server_name = first_rec.name if first_rec is not None else "Plex"
        self._update_progress_server(server_name)
        server_status, matched_count, missing_count, error_msg = self._push_playlist(
            db,
            config,
            first_server,
            pl,
            model_playlist,
            target_usernames,
            first_skip,
            original_by_effective,
            ignored_ids,
            stats,
            fingerprint,
            server_name=server_name,
            rec=first_rec,
            library_hits=library_hits,
            library_missing=library_missing,
        )
        if first_rec is not None:
            db.record_playlist_server_state(
                pl["id"],
                first_rec.id,
                server_status,
                matched=matched_count,
                missing=missing_count,
                error=error_msg,
            )
        for rec, adapter in servers[1:]:
            if rec is not None:
                self._update_progress_server(rec.name)
                extra_skip = skip_rating_keys if rec.is_login else None
                _push_to_extra_server(
                    db,
                    config,
                    rec,
                    adapter,
                    model_playlist,
                    target_usernames,
                    skip_item_ids=extra_skip,
                    library_hits=library_hits,
                    library_missing=library_missing,
                )

    @staticmethod
    def _unchanged_since_last_sync(
        db: Database, pl: dict[str, Any], fingerprint: str, latest_library_change: Optional[str]
    ) -> bool:
        """True when the last sync succeeded, is under a day old, and neither inputs nor the library changed since.

        The library counts as changed while its newest change is within ``_IMPORT_RESCAN_GRACE`` before the last
        sync: a media server may not have scanned a freshly imported file when that sync ran.
        """
        if pl.get("sync_status") != "success":
            return False
        stored, last_synced_at = db.get_playlist_sync_fingerprint(pl["id"])
        if not stored or stored != fingerprint or not last_synced_at:
            return False
        try:
            synced = datetime.strptime(last_synced_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False  # unparseable timestamp: resync rather than trust it
        if datetime.now(timezone.utc) - synced > _FULL_RESYNC_AFTER:
            return False
        if latest_library_change is None:
            return True
        try:
            changed = datetime.strptime(latest_library_change, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return changed <= synced - _IMPORT_RESCAN_GRACE

    def _sync_one(
        self,
        db: Database,
        config: Config,
        plex_client: Optional[PlexClient],
        server: Any,
        spotify_client: Optional[SpotifyClient],
        deezer_client: Optional[DeezerClient],
        pl: dict[str, Any],
        stats: dict[str, int],
        force: bool,
        latest_library_change: Optional[str],
    ) -> None:
        """Syncs one playlist: loads its tracks, skips it when nothing changed, else matches (and pushes) it."""
        pl_id = pl["id"]
        # Target users only matter for pushing to a media server; without targets (or without a
        # media server) the playlist is still matched against the native library.
        servers = _playlist_servers(db, pl, server)
        no_media_server = not servers
        target_usernames = [] if no_media_server else self._resolve_targets(db, pl)
        push = bool(target_usernames)

        skip_rating_keys: Optional[set[str]] = None
        if (source := db.get_server_playlist_source(pl_id)) is not None:
            self._refresh_server_adopted(db, pl, source)
        elif pl.get("service", "spotify") == "plex":
            skip_rating_keys = self._refresh_adopted_playlist(db, plex_client, pl)
        try:
            tracks = self._load_source_tracks(db, config, pl, spotify_client, deezer_client)
        except SmartCollectionError as e:
            logger.info("Smart collection '%s' skipped: %s", pl["name"], e)
            db.record_sync_result(pl_id, status="error")
            return
        except Exception as e:
            logger.error("Failed to fetch tracks for playlist '%s' (%s): %s", pl["name"], pl_id, safe_exc(e))
            db.record_sync_result(pl_id, status="error")
            return

        tracks, original_by_effective, ignored_ids = _effective_tracks(db, tracks, pl["name"])
        server_ids = [rec.id for rec, _ in servers if rec is not None]
        fingerprint = _sync_fingerprint(tracks, target_usernames, push, pl.get("monitor_mode"), server_ids=server_ids)
        if not force and self._unchanged_since_last_sync(db, pl, fingerprint, latest_library_change):
            stats["skipped_unchanged"] += 1
            logger.debug("Playlist '%s' unchanged since last sync; skipped", pl["name"])
            return

        started = time.monotonic()
        matched_before, missing_before = stats["total_matched"], stats["total_missing"]
        model_playlist = Playlist(
            id=pl_id,
            name=pl["name"],
            tracks=tracks,
            description=pl.get("description", ""),
            poster=pl.get("poster_url", ""),
        )
        if no_media_server or not push:
            # Nothing is pushed (no media server, or no target users): the source playlist is matched
            # against the native library so its missing tracks reach monitoring / wanted.
            self._match_natively(db, config, pl, tracks, original_by_effective, ignored_ids, stats, fingerprint)
        elif servers[0][1]:
            self._push_to_servers(
                db, config, servers, pl, model_playlist, target_usernames, skip_rating_keys,
                original_by_effective, ignored_ids, stats, fingerprint,
            )
        else:
            logger.warning("No media server available; recorded simulated sync for '%s'", pl["name"])
            db.record_sync_result(playlist_id=pl_id, status="unconfigured")
            return
        logger.info(
            "Playlist '%s' synced in %.1fs (%d matched, %d missing)",
            pl["name"],
            time.monotonic() - started,
            stats["total_matched"] - matched_before,
            stats["total_missing"] - missing_before,
        )

    def _set_progress(
        self, current: int, total: int, playlist_name: str, server_name: Optional[str]
    ) -> None:
        self.progress = (current, total, playlist_name, server_name)
        msg = f"Syncing '{playlist_name}' to {server_name}" if server_name else f"Matching '{playlist_name}'"
        for job in job_tracker.snapshot()["running"]:
            if job["task_id"] == "playlist_sync":
                job_tracker.update_message(job["id"], msg)

    def _update_progress_server(self, server_name: str) -> None:
        if self.progress is not None:
            current, total, playlist_name, _ = self.progress
            self._set_progress(current, total, playlist_name, server_name)

    @tracked("playlist_sync", "Playlist Sync")
    def execute_sync(
        self,
        db: Database,
        config: Config,
        plex_client: Optional[PlexClient],
        spotify_client: Optional[SpotifyClient],
        deezer_client: Optional[DeezerClient],
        force: bool = False,
    ) -> dict[str, Any]:
        """Runs synchronization across all enabled database playlists.

        Unless ``force``, a playlist whose inputs, targets and library are unchanged since its last
        successful sync (under 24 h ago) is skipped; any library import since then forces a rematch.
        """
        if self.is_syncing:
            return {"status": "already_running"}

        self.is_syncing = True
        logger.info("Starting background playlist synchronization cycle")
        stats = {
            "total_playlists": 0,
            "success_count": 0,
            "total_matched": 0,
            "total_missing": 0,
            "skipped_unchanged": 0,
        }

        try:
            server = as_media_server(plex_client)
            if server:
                try:
                    refreshed = refresh_mix_snapshots(server, db)
                    if refreshed:
                        logger.info("Refreshed %d Plexamp mix snapshot(s)", refreshed)
                except MediaServerError as e:
                    logger.warning("Mix snapshot refresh failed: %s", e.safe_detail)
            playlists = db.list_playlists(enabled_only=True)
            stats["total_playlists"] = len(playlists)
            latest_library_change = db.latest_library_file_change()

            for idx, pl in enumerate(playlists, start=1):
                self._set_progress(idx, len(playlists), pl["name"], None)
                self._sync_one(
                    db, config, plex_client, server, spotify_client, deezer_client,
                    pl, stats, force, latest_library_change,
                )

            self.last_run_stats = stats
            self.last_run_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Background sync complete: %d/%d succeeded, %d matched, %d missing, %d skipped unchanged",
                stats["success_count"],
                stats["total_playlists"],
                stats["total_matched"],
                stats["total_missing"],
                stats["skipped_unchanged"],
            )
            try:
                db.record_event(
                    "sync_completed",
                    f"Playlist sync completed: {stats['total_playlists']} playlists processed ({stats['total_matched']} matched, {stats['total_missing']} missing, {stats['skipped_unchanged']} unchanged)",
                    source="SyncCoordinator",
                    severity="info",
                )
            except Exception as ev_err:
                logger.warning("Failed to record sync_completed event: %s", ev_err)
            return {"status": "success", "stats": stats}
        except Exception as e:
            logger.error("Unexpected error during sync cycle: %s", safe_exc(e))
            logger.debug("Sync cycle traceback", exc_info=True)
            return {"status": "error", "error": safe_exc(e)}
        finally:
            self.progress = None
            self.is_syncing = False


# Shared sync state instance
sync_state = SyncState()


@router.post("", response_model=SyncTriggerResponse, response_model_exclude_unset=True)
def trigger_sync(
    background_tasks: BackgroundTasks,
    _admin: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
) -> dict[str, Any]:
    """Triggers sync in background task (runs async without blocking)."""
    if sync_state.is_syncing:
        return {
            "status": "already_running",
            "message": "Synchronization is already running in background",
        }

    def _run_sync() -> None:
        with record_task_run(db, "playlist_sync", TRIGGER_MANUAL) as run:
            run.apply_result(
                sync_state.execute_sync(
                    db=db,
                    config=config,
                    plex_client=plex_client,
                    spotify_client=spotify_client,
                    deezer_client=deezer_client,
                    force=True,
                )
            )

    background_tasks.add_task(_run_sync)

    return {
        "status": "started",
        "message": "Synchronization triggered successfully in background",
    }


@router.get("/status", response_model=SyncStatusResponse, response_model_exclude_unset=True)
def get_sync_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns current sync status and last run stats."""
    return {
        "is_syncing": sync_state.is_syncing,
        "last_run_at": sync_state.last_run_at,
        "last_run_stats": sync_state.last_run_stats,
    }


@router.get("/stream")
async def stream_sync_logs(
    request: Request,
    limit: Optional[int] = None,
    _admin: dict[str, Any] = Depends(require_admin),
) -> StreamingResponse:
    """Server-Sent Events (SSE) streaming live log lines (administrators only)."""

    async def event_generator():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        sync_state.log_handler.add_listener(loop, q)
        count = 0
        try:
            yield "data: Connected to live sync log stream\n\n"
            count += 1
            if limit is not None and count >= limit:
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {line}\n\n"
                    count += 1
                    if limit is not None and count >= limit:
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sync_state.log_handler.remove_listener(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/webhook", response_model=SyncWebhookResponse, response_model_exclude_unset=True)
async def handle_sync_webhook(  # noqa: C901
    background_tasks: BackgroundTasks,
    request: Request,
    _auth: dict[str, Any] = Depends(verify_feed_access),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
) -> dict[str, Any]:
    """Webhook endpoint for Lidarr, Plex, or external automation triggers.

    When Lidarr completes a track download/import or Plex completes a library scan,
    they can ping this endpoint to trigger immediate playlist sync and re-evaluation.
    Requires the FEED_TOKEN, an API key or an admin session; never a plain user.
    """
    body_preview = ""
    fulfilled_count = 0
    try:
        raw_body = await request.body()
        if raw_body:
            body_preview = raw_body[:200].decode("utf-8", errors="ignore")
            try:
                payload = json.loads(raw_body.decode("utf-8", errors="ignore"))
            except Exception:
                payload = None

            if isinstance(payload, dict):
                artist_name = None
                album_name = None
                track_title = None

                if isinstance(payload.get("artist"), dict):
                    artist_name = payload["artist"].get("name")
                elif isinstance(payload.get("artist"), str):
                    artist_name = payload["artist"]

                if isinstance(payload.get("album"), dict):
                    album_name = payload["album"].get("title")
                elif isinstance(payload.get("album"), str):
                    album_name = payload["album"]
                elif isinstance(payload.get("albums"), list) and payload["albums"]:
                    first_album = payload["albums"][0]
                    album_name = first_album.get("title") if isinstance(first_album, dict) else str(first_album)

                if isinstance(payload.get("track"), dict):
                    track_title = payload["track"].get("title")
                elif isinstance(payload.get("title"), str):
                    track_title = payload["title"]
                elif isinstance(payload.get("trackFiles"), list) and payload["trackFiles"]:
                    first_tf = payload["trackFiles"][0]
                    if isinstance(first_tf, dict) and isinstance(first_tf.get("track"), dict):
                        track_title = first_tf["track"].get("title")

                if artist_name:
                    matching_reqs = db.find_matching_processing_requests(
                        artist=artist_name, album=album_name, title=track_title
                    )
                    for m_req in matching_reqs:
                        db.update_request_status(m_req["id"], RequestStatus.AVAILABLE)
                        fulfilled_count += 1
                        logger.info(
                            "Auto-fulfilled request %s (%s - %s) to AVAILABLE via webhook",
                            m_req["id"],
                            m_req.get("artist"),
                            m_req.get("title"),
                        )
    except Exception as e:
        logger.debug("Error inspecting webhook payload for request fulfillment: %s", e)

    logger.info(
        "Sync webhook received (triggering background sync): %s (auto-fulfilled %d requests)",
        body_preview,
        fulfilled_count,
    )

    if sync_state.is_syncing:
        return {
            "status": "already_running",
            "message": "Synchronization is already in progress",
            "fulfilled_requests": fulfilled_count,
        }

    background_tasks.add_task(
        sync_state.execute_sync,
        db=db,
        config=config,
        plex_client=plex_client,
        spotify_client=spotify_client,
        deezer_client=deezer_client,
    )
    return {
        "status": "triggered",
        "message": "Background synchronization triggered via webhook",
        "fulfilled_requests": fulfilled_count,
    }

