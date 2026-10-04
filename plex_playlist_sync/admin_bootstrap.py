"""First-run administrator for instances with no media server.

With Plex, the first admin is the Plex server owner signing in. Without a media server there is no such
sign-in, and creating users needs an admin, so a local admin is seeded from the environment.
"""

import logging
import os
import sqlite3

from plex_playlist_sync import local_auth
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"


def ensure_bootstrap_admin(db: Database) -> bool:
    """Creates a local admin from ``ADMIN_USERNAME`` (default ``admin``) / ``ADMIN_PASSWORD`` when the database has
    no active admin. Returns True only when an account was created. Never modifies an existing admin, and never
    runs once an admin exists (so the env password cannot be used to reset one)."""
    if db.count_active_admins() > 0:
        return False
    password = os.getenv("ADMIN_PASSWORD", "")
    username = local_auth.normalize_username(os.getenv("ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME)
    if not password:
        logger.warning(
            "No administrator exists and no media server is configured to sign one in. "
            "Set ADMIN_PASSWORD (and optionally ADMIN_USERNAME) and restart to create the first local admin."
        )
        return False
    problem = local_auth.validate_password(password, username)
    if problem:
        logger.error("ADMIN_PASSWORD rejected: %s", problem)
        return False
    try:
        user, _invite = db.create_local_user(
            username, None, int(UserPermission.DEFAULT) | int(UserPermission.ADMIN), created_by="bootstrap"
        )
        db.set_password(user["id"], local_auth.hash_password(password))
    except (ValueError, sqlite3.Error) as exc:
        logger.error("Could not create the first local admin '%s': %s", username, safe_exc(exc))
        return False
    logger.info("Created first local admin '%s' from ADMIN_USERNAME/ADMIN_PASSWORD", username)
    return True
