"""Per-user Subsonic playlist sync helper."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional, Sequence

from trackseerr.config import Config
from trackseerr.media_servers.base import MediaServerError, PlaylistSyncOptions
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.models import Playlist, SyncResult
from trackseerr.secret_box import unseal

logger = logging.getLogger(__name__)

_WARNED_TRANSFER_LOCK = threading.Lock()
_WARNED_TRANSFER_SERVERS: set[str] = set()


def _warn_owner_transfer_ignored(server_id: str, server_name: str) -> None:
    """Log once per server when Navidrome ignores a playlist owner transfer."""
    with _WARNED_TRANSFER_LOCK:
        if server_id in _WARNED_TRANSFER_SERVERS:
            return
        _WARNED_TRANSFER_SERVERS.add(server_id)
    logger.warning(
        "Navidrome server '%s' ignored playlist owner transfer; save per-user logins to sync non-admin playlists",
        server_name,
    )


def _sync_own_account(adapter: Any, playlist: Playlist, options: PlaylistSyncOptions) -> list[SyncResult]:
    total = len(playlist.tracks)
    try:
        return list(adapter.sync_playlist(playlist, [""], options))
    except MediaServerError as exc:
        logger.warning("Subsonic sync failed for server account on '%s': %s", playlist.name, exc.safe_detail)
        return [SyncResult(playlist.name, total, 0, total, False, exc.safe_detail, missing=list(playlist.tracks))]


def _sync_user_account(
    config: Config,
    rec: ServerRecord,
    adapter: Any,
    playlist: Playlist,
    name: str,
    account: dict[str, Any],
    options: PlaylistSyncOptions,
) -> list[SyncResult]:
    total = len(playlist.tracks)
    password = unseal(account["secret_sealed"], data_dir=config.state_dir)
    if not password:
        return [
            SyncResult(
                playlist.name,
                total,
                0,
                total,
                False,
                f"The saved {rec.name} login for {name} can't be read any more; save it again on the Account page",
                missing=list(playlist.tracks),
            )
        ]

    user_adapter = None
    try:
        user_adapter = adapter.with_login(account["username"], password)
        return list(user_adapter.sync_playlist(playlist, [""], options))
    except MediaServerError as exc:
        logger.warning("Subsonic sync failed for user '%s' on '%s': %s", name, rec.name, exc.safe_detail)
        return [SyncResult(playlist.name, total, 0, total, False, exc.safe_detail, missing=list(playlist.tracks))]
    finally:
        if user_adapter is not None and hasattr(user_adapter, "close"):
            user_adapter.close()


def _match_for_admin(
    adapter: Any,
    playlist: Playlist,
    options: PlaylistSyncOptions,
    cache: dict[str, tuple[list[Any], list[Any]]],
) -> tuple[list[Any], list[Any]]:
    cached = cache.get("admin")
    if cached is not None:
        return cached
    if hasattr(adapter, "match_and_prepare"):
        result = adapter.match_and_prepare(playlist, options)
    else:
        result = adapter.match_playlist_tracks(playlist.tracks, threshold=options.threshold, db=options.db)
    cache["admin"] = result
    return result


def _cleanup_rejected_copy(
    db: Any,
    adapter: Any,
    playlist_id: str,
    server_id: str,
    user_id: str,
    server_playlist_id: str,
    created_new: bool,
) -> None:
    db.delete_server_playlist_id(playlist_id, server_id, user_id)
    if created_new and hasattr(adapter, "delete_playlist"):
        try:
            adapter.delete_playlist(server_playlist_id)
        except MediaServerError as exc:
            logger.debug("Could not remove untransferred Navidrome playlist copy: %s", exc.safe_detail)


def _sync_navidrome_owner_transfer(
    db: Any,
    rec: ServerRecord,
    adapter: Any,
    nav_admin: Any,
    playlist: Playlist,
    name: str,
    user: dict[str, Any],
    link: dict[str, Any],
    options: PlaylistSyncOptions,
    match_cache: dict[str, tuple[list[Any], list[Any]]],
) -> Optional[list[SyncResult]]:
    """Push a per-user playlist copy with the admin account and transfer ownership; None if transfer was ignored."""
    total = len(playlist.tracks)
    try:
        matched, missing = _match_for_admin(adapter, playlist, options, match_cache)
        if not matched:
            return [
                SyncResult(
                    playlist.name,
                    total,
                    0,
                    len(missing),
                    False,
                    "Zero tracks matched on the Subsonic server",
                    missing=list(missing),
                )
            ]
        mapped_id = db.get_server_playlist_id(playlist.id, rec.id, user["id"])
        server_playlist_id, created_new = adapter.push_playlist_for_id(
            playlist, [m.id for m in matched], options, existing_id=mapped_id
        )
        db.set_server_playlist_id(playlist.id, rec.id, user["id"], server_playlist_id)
        if nav_admin.set_owner(server_playlist_id, link["server_user_id"]):
            return [SyncResult(playlist.name, total, len(matched), len(missing), True, missing=list(missing))]
        _warn_owner_transfer_ignored(rec.id, rec.name)
        _cleanup_rejected_copy(db, adapter, playlist.id, rec.id, user["id"], server_playlist_id, created_new)
        return None
    except MediaServerError as exc:
        logger.warning("Navidrome owner-transfer sync failed for '%s' on '%s': %s", name, rec.name, exc.safe_detail)
        return [SyncResult(playlist.name, total, 0, total, False, exc.safe_detail, missing=list(playlist.tracks))]


def _can_use_own_account(cleaned: str, own_username: str, link: Optional[dict[str, Any]]) -> bool:
    if not own_username or cleaned.lower() != own_username:
        return False
    if link is not None and link.get("source") == "manual":
        linked_name = str(link.get("server_username") or "").strip().lower()
        if linked_name and linked_name != own_username:
            return False
    return True


def _missing_target_result(
    rec: ServerRecord,
    playlist: Playlist,
    name: str,
    *,
    transfer_failed: bool,
) -> SyncResult:
    total = len(playlist.tracks)
    error = (
        f"Could not transfer {rec.name} playlist ownership to {name}"
        if transfer_failed
        else f"No {rec.name} login saved for {name}"
    )
    return SyncResult(
        playlist.name,
        total,
        0,
        total,
        False,
        error,
        missing=list(playlist.tracks),
        skipped=not transfer_failed,
    )


def sync_subsonic_for_users(
    db: Any,
    config: Config,
    rec: ServerRecord,
    adapter: Any,
    playlist: Playlist,
    target_usernames: Sequence[str],
    options: PlaylistSyncOptions,
) -> list[SyncResult]:
    """Push ``playlist`` to Subsonic using saved logins, Navidrome admin owner transfer, or the server's own login."""
    if not target_usernames:
        return _sync_own_account(adapter, playlist, options)

    own_username = str(getattr(adapter, "_user", "") or rec.username or "").strip().lower()
    users_by_name = (
        {str(u.get("username") or "").strip().lower(): u for u in db.list_users() if u.get("username")}
        if db is not None
        else {}
    )
    nav_fn = getattr(adapter, "navidrome_admin", None)
    nav_admin = nav_fn() if callable(nav_fn) else None

    results: list[SyncResult] = []
    own_results: Optional[list[SyncResult]] = None
    match_cache: dict[str, tuple[list[Any], list[Any]]] = {}

    for name in target_usernames:
        cleaned = str(name or "").strip()
        if not cleaned:
            if own_results is None:
                own_results = _sync_own_account(adapter, playlist, options)
            results.extend(own_results)
            continue

        user = users_by_name.get(cleaned.lower())
        account = db.get_server_account(user["id"], rec.id) if (db is not None and user is not None) else None
        if account is not None and account.get("username"):
            results.extend(_sync_user_account(config, rec, adapter, playlist, cleaned, account, options))
            continue

        link = db.get_server_user_link(user["id"], rec.id) if (db is not None and user is not None) else None
        transfer_failed = False
        if nav_admin is not None and link is not None and user is not None:
            transferred = _sync_navidrome_owner_transfer(
                db, rec, adapter, nav_admin, playlist, cleaned, user, link, options, match_cache
            )
            if transferred is not None:
                results.extend(transferred)
                continue
            transfer_failed = True

        if _can_use_own_account(cleaned, own_username, link):
            if own_results is None:
                own_results = _sync_own_account(adapter, playlist, options)
            results.extend(own_results)
            continue

        results.append(_missing_target_result(rec, playlist, cleaned, transfer_failed=transfer_failed))
    return results
