"""Importing the media server's accounts into the user table (playlist targets).

Shared by the startup discovery, the Settings reconnect and ``POST /api/users/refresh`` so they apply the same rules:
an account whose name belongs to a different user id is skipped (an imported name must never shadow a local account),
imported accounts are not Trackseerr administrators, and one bad row never stops the rest.
"""

import logging
import sqlite3
from collections.abc import Iterable
from typing import Any

from plex_playlist_sync.config import MEDIA_SERVER_PLEX
from plex_playlist_sync.media_servers.base import ServerUser
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)


def import_server_users(db: Any, kind: str, users: Iterable[ServerUser]) -> tuple[int, int]:
    """Upserts ``users`` found on a ``kind`` media server; returns ``(imported, skipped)``.

    Plex Home administrators keep the admin flag Plex reports (the owner signs in through Plex). For every other
    server ``is_admin`` on the media-server side says nothing about Trackseerr, so nobody is auto-granted admin; an
    admin flag already set in Trackseerr is never taken away. Tombstoned (admin-deleted) accounts stay out.
    """
    imported = skipped = 0
    for user in users:
        try:
            if db.is_tombstoned(user.id):
                continue  # deleted by an admin; only an explicit restore lets them back in
            row = db.import_media_server_user(
                user.id,
                user.name,
                user.extra.get("email") or None,
                auth_type=kind,
                grant_admin=kind == MEDIA_SERVER_PLEX and user.is_admin,
            )
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not import %s user '%s': %s", kind, user.name, safe_exc(exc))
            skipped += 1
            continue
        if row is None:
            logger.warning(
                "Skipped %s user '%s': that name already belongs to a different Trackseerr account", kind, user.name
            )
            skipped += 1
        else:
            imported += 1
    return imported, skipped
