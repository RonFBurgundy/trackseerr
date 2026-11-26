"""Importing the media server's accounts into the user table (playlist targets).

Shared by the startup discovery, the Settings reconnect and ``POST /api/users/refresh`` so they apply the same rules:
an account whose name belongs to a different user id is skipped (an imported name must never shadow a local account),
imported accounts are not Trackseerr administrators, and one bad row never stops the rest.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from typing import Any, Optional

from trackseerr.config import MEDIA_SERVER_PLEX, MEDIA_SERVER_SUBSONIC
from trackseerr.media_servers.base import ServerUser
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)


def _find_existing_user(db: Any, username: str, email: str) -> Optional[dict[str, Any]]:
    """Find an existing Trackseerr user by case-insensitive ``username`` or non-empty ``email``."""
    name = str(username or "").strip()
    if name:
        by_name = db.get_user_by_username(name)
        if by_name is not None:
            return by_name
    cleaned_email = str(email or "").strip().lower()
    if not cleaned_email:
        return None
    users = sorted(
        db.list_users(),
        key=lambda u: (
            0 if u.get("auth_type") == "local" else 1,
            str(u.get("created_at") or ""),
            str(u.get("id") or ""),
        ),
    )
    for existing in users:
        if str(existing.get("email") or "").strip().lower() == cleaned_email:
            return existing
    return None


def _import_one_user(
    db: Any,
    kind: str,
    user: ServerUser,
    server_id: Optional[str],
    has_server_row: bool,
) -> Optional[bool]:
    """Import or link one ``ServerUser``; returns True if imported/linked, False if skipped, None if tombstoned."""
    if db.is_tombstoned(user.id):
        return None
    email = str(user.extra.get("email") or "").strip()
    if kind == MEDIA_SERVER_SUBSONIC and db.get_user(user.id) is None:
        existing = _find_existing_user(db, user.name, email)
        if existing is not None:
            if has_server_row and server_id is not None:
                db.link_server_user(existing["id"], server_id, user.id, user.name, source="imported")
            return True

    row = db.import_media_server_user(
        user.id,
        user.name,
        email or None,
        auth_type=kind,
        grant_admin=kind == MEDIA_SERVER_PLEX and user.is_admin,
    )
    if row is None:
        logger.warning(
            "Skipped %s user '%s': that name already belongs to a different Trackseerr account", kind, user.name
        )
        return False
    if has_server_row and server_id is not None:
        db.link_server_user(row["id"], server_id, user.id, user.name, source="imported")
    return True


def import_server_users(
    db: Any,
    kind: str,
    users: Iterable[ServerUser],
    *,
    server_id: Optional[str] = None,
) -> tuple[int, int]:
    """Upserts ``users`` found on a ``kind`` media server; returns ``(imported, skipped)``.

    Plex Home administrators keep the admin flag Plex reports (the owner signs in through Plex). For every other
    server ``is_admin`` on the media-server side says nothing about Trackseerr, so nobody is auto-granted admin; an
    admin flag already set in Trackseerr is never taken away. Tombstoned (admin-deleted) accounts stay out.
    """
    has_server_row = bool(
        server_id and hasattr(db, "get_media_server_row") and db.get_media_server_row(server_id) is not None
    )
    imported = skipped = 0
    for user in users:
        try:
            outcome = _import_one_user(db, kind, user, server_id, has_server_row)
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not import %s user '%s': %s", kind, user.name, safe_exc(exc))
            skipped += 1
            continue
        if outcome is True:
            imported += 1
        elif outcome is False:
            skipped += 1
    return imported, skipped
