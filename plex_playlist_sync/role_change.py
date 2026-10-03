"""Detects a deployment-role flip (all-in-one <-> core) on boot and surfaces a one-time checklist.

No data is migrated: nothing in the database is role-specific. We only remember the last role
(``general_settings.last_role``) so the first boot under a new role can tell the admin what to
re-point. See ``docs/dmz-ergonomics.md`` section 6.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

LEGACY_ROLE = "all-in-one"

CHECKLISTS: dict[str, tuple[str, ...]] = {
    "core": (
        "Set APPLICATION_URL to the public gateway URL.",
        "Point your reverse proxy or tunnel at the gateway container, not at this one.",
        "The Plex webhook URL is unchanged (Plex keeps talking to this container on the LAN).",
        "Users sign in once on the gateway.",
    ),
    "all-in-one": (
        "Set APPLICATION_URL to the URL users will now open (this container serves them directly).",
        "Point your reverse proxy or tunnel at this container and stop the gateway container.",
        "The Plex webhook URL is unchanged.",
        "Users sign in once again on this container.",
    ),
}


def checklist_for(to_role: str) -> list[str]:
    return list(CHECKLISTS.get(to_role, ()))


def record_boot_role(db: Database, role: str) -> Optional[dict[str, Any]]:
    """Compares ``role`` with the stored last role; on a flip, logs the checklist once and stores a notice.

    Returns the new notice when a flip was detected, else None. Always leaves ``last_role == role``.
    A gateway only records its role (its database is its own; there is nothing to migrate).
    An empty ``last_role`` (a pre-v30 database or a first boot) is never inferred as a flip: the current
    role is recorded silently. A notice needs a non-empty ``last_role`` that differs from ``role``.
    """
    role = (role or LEGACY_ROLE).lower().strip()
    previous = db.get_last_role()
    if previous == role:
        return None
    notice: Optional[dict[str, Any]] = None
    if role in CHECKLISTS:
        if previous in CHECKLISTS and previous != role:
            notice = {
                "from_role": previous,
                "to_role": role,
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "dismissed": False,
            }
            logger.warning("TrackSeerr is starting as ROLE=%s for the first time (was %s). Checklist:", role, previous)
            for i, item in enumerate(CHECKLISTS[role], start=1):
                logger.warning("  %d. %s", i, item)
            db.set_role_change_notice(notice)
    db.set_last_role(role)
    return notice


def current_notice(db: Database) -> dict[str, Any]:
    """Shape of ``GET /api/admin/role-change-notice``."""
    raw = db.get_role_change_notice()
    if not raw or raw.get("dismissed"):
        return {"active": False, "from_role": None, "to_role": None, "changed_at": None, "checklist": []}
    to_role = str(raw.get("to_role") or "")
    return {
        "active": True,
        "from_role": raw.get("from_role"),
        "to_role": to_role,
        "changed_at": raw.get("changed_at"),
        "checklist": checklist_for(to_role),
    }


def dismiss_notice(db: Database) -> None:
    raw = db.get_role_change_notice()
    if raw and not raw.get("dismissed"):
        raw["dismissed"] = True
        db.set_role_change_notice(raw)
