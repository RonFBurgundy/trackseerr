"""Per-user Subsonic playlist sync helper."""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from trackseerr.config import Config
from trackseerr.media_servers.base import MediaServerError, PlaylistSyncOptions
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.models import Playlist, SyncResult
from trackseerr.secret_box import unseal

logger = logging.getLogger(__name__)


def _sync_own_account(adapter: Any, playlist: Playlist, options: PlaylistSyncOptions) -> list[SyncResult]:
    total = len(playlist.tracks)
    try:
        return list(adapter.sync_playlist(playlist, [""], options))
    except MediaServerError as exc:
        logger.warning("Subsonic sync failed for server account on '%s': %s", playlist.name, exc.safe_detail)
        return [SyncResult(playlist.name, total, 0, total, False, exc.safe_detail)]


def _sync_user_account(
    db: Any,
    config: Config,
    rec: ServerRecord,
    adapter: Any,
    playlist: Playlist,
    name: str,
    user: Optional[dict[str, Any]],
    options: PlaylistSyncOptions,
) -> list[SyncResult]:
    total = len(playlist.tracks)
    account = db.get_server_account(user["id"], rec.id) if user is not None else None
    if account is None or not account.get("username"):
        return [SyncResult(playlist.name, total, 0, total, False, f"No {rec.name} login saved for {name}")]
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
            )
        ]

    user_adapter = None
    try:
        user_adapter = adapter.with_login(account["username"], password)
        return list(user_adapter.sync_playlist(playlist, [""], options))
    except MediaServerError as exc:
        logger.warning("Subsonic sync failed for user '%s' on '%s': %s", name, rec.name, exc.safe_detail)
        return [SyncResult(playlist.name, total, 0, total, False, exc.safe_detail)]
    finally:
        if user_adapter is not None and hasattr(user_adapter, "close"):
            user_adapter.close()


def sync_subsonic_for_users(
    db: Any,
    config: Config,
    rec: ServerRecord,
    adapter: Any,
    playlist: Playlist,
    target_usernames: Sequence[str],
    options: PlaylistSyncOptions,
) -> list[SyncResult]:
    """Push ``playlist`` to Subsonic using each target user's own saved login (or the server's own)."""
    if not target_usernames:
        return _sync_own_account(adapter, playlist, options)

    own_username = str(getattr(adapter, "_user", "") or rec.username or "").strip().lower()
    users_by_name = {
        str(u.get("username") or "").strip().lower(): u
        for u in db.list_users()
        if u.get("username")
    }

    results: list[SyncResult] = []
    own_results: Optional[list[SyncResult]] = None
    for name in target_usernames:
        cleaned = str(name or "").strip()
        if not cleaned or (own_username and cleaned.lower() == own_username):
            if own_results is None:
                own_results = _sync_own_account(adapter, playlist, options)
            results.extend(own_results)
            continue

        results.extend(
            _sync_user_account(
                db,
                config,
                rec,
                adapter,
                playlist,
                cleaned,
                users_by_name.get(cleaned.lower()),
                options,
            )
        )
    return results
