"""Scrobbling core: Plex identity resolution, credential lookup and listen forwarding."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from trackseerr.clients.scrobbler import (
    LastFmClient,
    LastFmError,
    ListenBrainzClient,
    ListenBrainzError,
)
from trackseerr.config import Config
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

PLEX_ADMIN_USERNAME_KEY = "plex_admin_username"
OWNER_LOCAL_ACCOUNT_ID = "1"


def resolve_plex_user(
    db: Database,
    account_id: Any,
    account_title: Optional[str],
    admin_username: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Map a Plex account (webhook or history) to a TrackSeerr user, or None.

    Rules: (1) account id 1 is the server owner and maps to the admin user matching ``admin_username``
    (else the first admin); (2) ``str(account_id) == users.id``; (3) case-insensitive title/username match;
    (4) otherwise None. Never creates users and never calls Plex (pass the cached ``admin_username``).
    """
    users = db.list_users()
    aid = str(account_id).strip() if account_id is not None else ""
    # The owner rule is evaluated first so a users.id of "1" can never capture the server owner.
    if aid == OWNER_LOCAL_ACCOUNT_ID:
        admins = [u for u in users if u.get("is_admin")]
        if admin_username:
            for u in admins:
                if str(u["username"]).lower() == admin_username.lower():
                    return u
        if admins:
            return admins[0]
    if aid:
        for u in users:
            if str(u["id"]) == aid:
                return u
    title = (account_title or "").strip().lower()
    if title:
        for u in users:
            if str(u["username"]).strip().lower() == title:
                return u
    logger.info(
        "listen from unknown Plex account %s; user has not logged into TrackSeerr",
        account_title or aid or "<unknown>",
    )
    return None


def get_cached_admin_username(db: Database) -> Optional[str]:
    return db.get_scrobble_state(PLEX_ADMIN_USERNAME_KEY) or None


def get_lastfm_credentials(db: Database, config: Optional[Config]) -> tuple[str, str, bool]:
    """Return ``(api_key, api_secret, from_env)``. Env vars (via Config) override DB values."""
    env_key = (getattr(config, "lastfm_api_key", None) or "").strip() if config else ""
    env_secret = (getattr(config, "lastfm_api_secret", None) or "").strip() if config else ""
    if env_key and env_secret:
        return env_key, env_secret, True
    stored = db.get_lastfm_settings()
    return stored["lastfm_api_key"], stored["lastfm_api_secret"], False


def build_lastfm_client(db: Database, config: Optional[Config]) -> Optional[LastFmClient]:
    key, secret, _ = get_lastfm_credentials(db, config)
    if not key or not secret:
        return None
    return LastFmClient(key, secret)


def _epoch(played_at: str) -> int:
    dt = datetime.fromisoformat(played_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def forward_listen(
    db: Database,
    listen_id: int,
    config: Optional[Config] = None,
    lastfm: Optional[LastFmClient] = None,
    listenbrainz: Optional[ListenBrainzClient] = None,
) -> dict[str, str]:
    """Forward one stored listen to every service still ``pending``/``failed``.

    Returns the resulting ``{service: status}`` map. Never raises for service errors; they are
    logged and recorded on the listen. Last.fm error 9 clears the user's session key.
    """
    listen = db.get_listen(listen_id)
    if listen is None:
        logger.warning("forward_listen: listen %s not found", listen_id)
        return {}
    outcome: dict[str, str] = {
        "lastfm": listen["lastfm_status"],
        "listenbrainz": listen["listenbrainz_status"],
    }
    cfg = db.get_scrobble_config(listen["user_id"])
    enabled = bool(cfg and cfg["scrobbling_enabled"])
    timestamp = _epoch(listen["played_at"])

    if outcome["lastfm"] in ("pending", "failed"):
        session_key = cfg["lastfm_session_key"] if cfg else None
        if not enabled or not session_key:
            db.mark_forward_result(listen_id, "lastfm", "skipped")
            outcome["lastfm"] = "skipped"
        else:
            client = lastfm or build_lastfm_client(db, config)
            if client is None:
                logger.warning("forward_listen: Last.fm not configured; listen %s stays queued", listen_id)
            else:
                try:
                    client.scrobble(
                        listen["artist"], listen["title"], timestamp, session_key, album=listen["album"]
                    )
                except LastFmError as exc:
                    if exc.invalid_session:
                        logger.warning(
                            "Last.fm session for user %s is invalid (error 9); clearing session key",
                            listen["user_id"],
                        )
                        db.upsert_scrobble_config(
                            listen["user_id"], lastfm_session_key=None, lastfm_username=None
                        )
                        db.mark_forward_result(listen_id, "lastfm", "skipped", str(exc))
                        outcome["lastfm"] = "skipped"
                    else:
                        logger.warning("Last.fm scrobble failed for listen %s: %s", listen_id, exc)
                        db.mark_forward_result(listen_id, "lastfm", "failed", str(exc))
                        outcome["lastfm"] = "failed"
                else:
                    db.mark_forward_result(listen_id, "lastfm", "sent")
                    outcome["lastfm"] = "sent"

    if outcome["listenbrainz"] in ("pending", "failed"):
        token = cfg["listenbrainz_token"] if cfg else None
        if not enabled or not token:
            db.mark_forward_result(listen_id, "listenbrainz", "skipped")
            outcome["listenbrainz"] = "skipped"
        else:
            lb = listenbrainz or ListenBrainzClient()
            try:
                lb.submit_listen(
                    token, listen["artist"], listen["title"], release=listen["album"], timestamp=timestamp
                )
            except ListenBrainzError as exc:
                logger.warning("ListenBrainz submit failed for listen %s: %s", listen_id, exc)
                db.mark_forward_result(listen_id, "listenbrainz", "failed", str(exc))
                outcome["listenbrainz"] = "failed"
            else:
                db.mark_forward_result(listen_id, "listenbrainz", "sent")
                outcome["listenbrainz"] = "sent"
    return outcome


def send_now_playing(
    db: Database,
    user_id: str,
    artist: str,
    title: str,
    album: Optional[str] = None,
    duration_ms: Optional[int] = None,
    config: Optional[Config] = None,
    lastfm: Optional[LastFmClient] = None,
) -> bool:
    """Best-effort Last.fm now-playing update; returns True when sent. Failures are logged, never raised."""
    cfg = db.get_scrobble_config(user_id)
    if not cfg or not cfg["scrobbling_enabled"] or not cfg["lastfm_session_key"]:
        return False
    client = lastfm or build_lastfm_client(db, config)
    if client is None:
        return False
    try:
        client.now_playing(
            artist, title, cfg["lastfm_session_key"], album=album,
            duration=int(duration_ms / 1000) if duration_ms else None,
        )
    except LastFmError as exc:
        logger.warning("Last.fm now-playing failed for user %s: %s", user_id, exc)
        if exc.invalid_session:
            db.upsert_scrobble_config(user_id, lastfm_session_key=None, lastfm_username=None)
        return False
    return True
