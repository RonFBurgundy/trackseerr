"""Who may let a playlist acquire music automatically.

Playlists can feed the wanted list on their own: a ``track``-mode playlist has its missing tracks searched by the
backlog worker, and a listening playlist (Last.fm / ListenBrainz) can raise requests for them. Both bypass the manual
request flow, so they need the ``AUTO_REQUEST_PLAYLISTS`` permission (admins hold every permission). Album and artist
modes add to the library without quota or approval and stay admin-only (see ``list_monitoring``).
"""

import logging
from typing import Any, Optional

from trackseerr.models import UserPermission

logger = logging.getLogger(__name__)

# Playlists whose tracks come from a user's own scrobbling account. They never use a monitor mode.
LISTENING_SERVICES = ("lastfm", "listenbrainz")

SMART_SERVICE = "trackseerr"
SMART_KIND = "smart"


def is_listening_playlist(playlist: dict[str, Any]) -> bool:
    return str(playlist.get("service") or "") in LISTENING_SERVICES


def is_smart_collection(playlist: dict[str, Any]) -> bool:
    return (
        str(playlist.get("service") or "") == SMART_SERVICE
        and str(playlist.get("source_kind") or "") == SMART_KIND
    )


def user_may_auto_request(user: Optional[dict[str, Any]]) -> bool:
    """Whether ``user`` is an active admin or holds ``AUTO_REQUEST_PLAYLISTS``."""
    if not user or user.get("disabled"):
        return False
    # Deferred import: api.dependencies pulls in the whole API layer.
    from trackseerr.api.dependencies import has_permission

    return has_permission(user, UserPermission.AUTO_REQUEST_PLAYLISTS)


def creator_may_auto_acquire(db: Any, playlist: dict[str, Any], _cache: Optional[dict[str, bool]] = None) -> bool:
    """Whether the playlist's creator may have it acquire music automatically.

    A playlist with no creator (configured by the operator, or orphaned) is trusted. A creator who no longer exists
    or is disabled is not. ``_cache`` memoises the answer per creator id within one sync pass.
    """
    creator_id = playlist.get("creator_id")
    if not creator_id:
        return True
    key = str(creator_id)
    if _cache is not None and key in _cache:
        return _cache[key]
    allowed = user_may_auto_request(db.get_user(key))
    if _cache is not None:
        _cache[key] = allowed
    return allowed


def initial_monitor_mode_forced(user: dict[str, Any]) -> Optional[str]:
    """The monitor mode a new playlist must start with, or None to keep the column default.

    A user who may not auto-request gets a list-only playlist regardless of the default.
    """
    return None if user_may_auto_request(user) else "none"
