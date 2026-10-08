"""Changelog routes mounted at /system/changelog."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from plex_playlist_sync.api.dependencies import get_db, require_admin, require_user
from plex_playlist_sync.api.schemas.changelog import (
    ChangelogResponse,
    ChangelogSeenResponse,
    ChangelogUnseenResponse,
)
from plex_playlist_sync.changelog import (
    get_build_info,
    get_changelog,
    get_latest_release,
)
from plex_playlist_sync.storage import Database

router = APIRouter()


@router.get("", response_model=ChangelogResponse, summary="Get changelog releases and build metadata")
def get_system_changelog(
    limit: Optional[int] = Query(None, ge=1, description="Max releases to return"),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Returns the app version, commit, changelog releases, and latest release for any authenticated user."""
    version, commit = get_build_info()
    releases = get_changelog()
    latest = get_latest_release(releases, version)
    if limit is not None:
        releases = releases[:limit]
    return {
        "version": version,
        "commit": commit,
        "releases": releases,
        "latest": latest,
    }


@router.get("/unseen", response_model=ChangelogUnseenResponse, summary="Check unseen changelog release for admin")
def get_system_changelog_unseen(
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Returns whether to show an upgrade changelog popup for the current admin.

    show is true when the admin's stored last-seen version differs from the current version
    AND the current version has a changelog entry. Fresh installs (last_seen is null) record
    the current version silently and return show: false.
    """
    version, _ = get_build_info()
    user_id = current_user.get("id")
    if not user_id:
        return {"show": False, "release": None}

    last_seen = db.get_last_seen_changelog_version(user_id)
    if last_seen is None:
        db.set_last_seen_changelog_version(user_id, version)
        return {"show": False, "release": None}

    if last_seen != version:
        releases = get_changelog()
        v_clean = version.lstrip("v")
        for rel in releases:
            rel_v = str(rel.get("version", "")).lstrip("v")
            if not rel.get("unreleased") and rel_v == v_clean:
                return {"show": True, "release": rel}

    return {"show": False, "release": None}


@router.post("/seen", response_model=ChangelogSeenResponse, summary="Acknowledge current version changelog for admin")
def post_system_changelog_seen(
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Stores the current version as this admin's last-seen changelog version."""
    version, _ = get_build_info()
    user_id = current_user.get("id")
    if user_id:
        db.set_last_seen_changelog_version(user_id, version)
    return {"success": True}
