"""First-run administrator for instances with no media server.

With Plex, the first admin is the Plex server owner signing in. Without a media server there is no such
sign-in, and creating users needs an admin, so a local admin is seeded from the environment.
"""

import logging
import os
import sqlite3

from trackseerr import local_auth
from trackseerr.models import UserPermission
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"


def ensure_bootstrap_admin(db: Database) -> bool:
    """Creates a local admin from ``ADMIN_USERNAME`` (default ``admin``) / ``ADMIN_PASSWORD`` when no enabled admin
    can actually sign in without a media server (a local admin with a password). Returns True only when an account
    was created. Never modifies an existing account, and never runs once a usable admin exists (so the env
    password cannot be used to reset one). ``ADMIN_PASSWORD`` is removed from the process environment afterwards."""
    if db.count_local_login_admins() > 0:
        os.environ.pop("ADMIN_PASSWORD", None)
        return False
    password = os.getenv("ADMIN_PASSWORD", "")
    os.environ.pop("ADMIN_PASSWORD", None)
    username = local_auth.normalize_username(os.getenv("ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME)
    if not password:
        logger.error(
            "No administrator can sign in: no media server is configured and no local admin with a password "
            "exists. To recover: set ADMIN_PASSWORD and restart to create a local admin (ADMIN_USERNAME optionally "
            "chooses its name)."
        )
        return False
    problem = local_auth.validate_password(password, username)
    if problem:
        logger.error("ADMIN_PASSWORD rejected: %s", problem)
        return False
    existing = next((u for u in db.list_users() if str(u.get("username", "")).lower() == username.lower()), None)
    if existing is not None:
        logger.error(
            "Cannot create the local admin '%s': a %s user with that name already exists and is left untouched. "
            "Set ADMIN_USERNAME to a different name and restart.",
            username,
            existing.get("auth_type") or "existing",
        )
        return False
    try:
        db.create_local_user_with_password(
            username,
            local_auth.hash_password(password),
            int(UserPermission.DEFAULT) | int(UserPermission.ADMIN),
        )
    except (ValueError, sqlite3.Error) as exc:
        logger.error("Could not create the first local admin '%s': %s", username, safe_exc(exc))
        return False
    logger.info("Created first local admin '%s' from ADMIN_USERNAME/ADMIN_PASSWORD", username)
    return True
