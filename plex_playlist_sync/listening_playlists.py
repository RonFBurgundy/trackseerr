"""Playlists built from a user's own Last.fm / ListenBrainz listening.

A listening playlist is owned by one user and always fetched with that user's linked scrobbling account, never anyone
else's. Its missing tracks are listed only; automatic requests are a per-playlist opt-in that needs admin or the
``AUTO_REQUEST_PLAYLISTS`` permission, re-checked on every sync.
"""

import hashlib
import logging
import re
from typing import Any, Optional

from plex_playlist_sync.clients.import_lists import lastfm, listenbrainz
from plex_playlist_sync.clients.import_lists.base import ImportListError, ImportListItem
from plex_playlist_sync.item_history import TRIGGER_PLAYLIST, GrabTrigger
from plex_playlist_sync.models import Track
from plex_playlist_sync.playlist_policy import LISTENING_SERVICES, user_may_auto_request
from plex_playlist_sync.request_submission import RequestRejected, submit_track_request

logger = logging.getLogger(__name__)

PROVIDER_LASTFM = "lastfm"
PROVIDER_LISTENBRAINZ = "listenbrainz"
KIND_LOVED = "loved"
KIND_TOP_TRACKS = "top_tracks"
KIND_LB_PLAYLIST = "playlist"
KIND_LB_CREATED_FOR = "created_for"
TRACK_LIMIT = 200

PERIOD_LABELS = {
    "overall": "All time",
    "7day": "Last 7 days",
    "1month": "Last month",
    "3month": "Last 3 months",
    "6month": "Last 6 months",
    "12month": "Last 12 months",
}

_MBID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


class ListeningSourceError(Exception):
    """A listening source could not be resolved or fetched. The message is safe to show to the user."""


def _lastfm_key(db: Any, config: Any) -> str:
    # Deferred import: scrobbling pulls in the Last.fm client stack.
    from plex_playlist_sync.scrobbling import get_lastfm_credentials

    return get_lastfm_credentials(db, config)[0].strip()


def _scrobble_config(db: Any, user_id: str) -> dict[str, Any]:
    return db.get_scrobble_config(str(user_id)) or {}


def _lastfm_username(cfg: dict[str, Any]) -> str:
    return str(cfg.get("lastfm_username") or "").strip()


def _lb_username(cfg: dict[str, Any]) -> str:
    return str(cfg.get("listenbrainz_username") or "").strip()


def _lb_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"token": str(cfg.get("listenbrainz_token") or "").strip()}


def playlist_id_for(user_id: str, provider: str, kind: str, ref: str) -> str:
    """Stable id, so creating the same source twice updates the playlist instead of duplicating it."""
    digest = hashlib.sha256(f"{user_id}|{provider}|{kind}|{ref}".encode()).hexdigest()[:12]
    return f"{'lfm' if provider == PROVIDER_LASTFM else 'lb'}_{digest}"


def lastfm_lists() -> list[dict[str, Any]]:
    lists: list[dict[str, Any]] = [{"kind": KIND_LOVED, "ref": "", "label": "Loved tracks", "date": None}]
    for period in lastfm.PERIODS:
        lists.append(
            {"kind": KIND_TOP_TRACKS, "ref": period, "label": f"Top tracks - {PERIOD_LABELS[period]}", "date": None}
        )
    return lists


def list_sources(db: Any, config: Any, user_id: str) -> dict[str, Any]:
    """What the user can build a playlist from, using only their own linked accounts."""
    cfg = _scrobble_config(db, user_id)
    lf_user = _lastfm_username(cfg)
    lb_user = _lb_username(cfg)

    lastfm_info: dict[str, Any] = {
        "linked": bool(lf_user),
        "username": lf_user or None,
        "available": False,
        "reason": None,
        "lists": [],
    }
    if not lf_user:
        lastfm_info["reason"] = "Last.fm is not linked. Link it in Settings > Scrobbling."
    elif not _lastfm_key(db, config):
        lastfm_info["reason"] = "No Last.fm API key is configured on the server. Ask an admin to set it up."
    else:
        lastfm_info["available"] = True
        lastfm_info["lists"] = lastfm_lists()

    lb_info: dict[str, Any] = {
        "linked": bool(lb_user),
        "username": lb_user or None,
        "available": False,
        "reason": None,
        "lists": [],
    }
    if not lb_user:
        lb_info["reason"] = "ListenBrainz is not linked. Link it in Settings > Scrobbling."
    else:
        lb_cfg = _lb_config(cfg)
        try:
            own = listenbrainz.list_user_playlists(lb_user, lb_cfg)
            created_for = listenbrainz.list_created_for(lb_user, lb_cfg)
        except ImportListError as exc:
            logger.warning("ListenBrainz playlist discovery failed for user %s: %s", user_id, exc)
            lb_info["reason"] = f"ListenBrainz could not be reached ({exc})"
        else:
            lb_info["available"] = True
            newest_per_kind: dict[str, dict[str, Any]] = {}
            for entry in created_for:  # newest first, so the first of each kind wins
                newest_per_kind.setdefault(entry["slug"], entry)
            lb_info["lists"] = [
                {
                    "kind": KIND_LB_CREATED_FOR,
                    "ref": e["mbid"],
                    "label": created_for_name(e["title"]),
                    "date": e["date"] or None,
                }
                for e in newest_per_kind.values()
            ] + [
                {"kind": KIND_LB_PLAYLIST, "ref": e["mbid"], "label": e["title"] or e["mbid"], "date": e["date"] or None}
                for e in own
            ]
    return {PROVIDER_LASTFM: lastfm_info, PROVIDER_LISTENBRAINZ: lb_info}


def created_for_name(title: str) -> str:
    """"Weekly Jams for ron, week of 2025-01-06" -> "Weekly Jams"."""
    return (title.split(" for ", 1)[0].strip() or title.strip() or "Created for you")


def describe_source(provider: str, kind: str, ref: str, title: Optional[str] = None) -> str:
    """Playlist name for a source."""
    if provider == PROVIDER_LASTFM:
        if kind == KIND_LOVED:
            return "Loved Tracks - Last.fm"
        return f"Top Tracks ({PERIOD_LABELS.get(ref, ref)}) - Last.fm"
    base = created_for_name(title or "") if kind == KIND_LB_CREATED_FOR else (title or "ListenBrainz playlist")
    return f"{base} - ListenBrainz"


def validate_source(provider: str, kind: str, ref: str) -> tuple[str, str]:
    """Normalises ``(kind, ref)`` for ``provider`` or raises ListeningSourceError."""
    ref = (ref or "").strip()
    if provider == PROVIDER_LASTFM:
        if kind == KIND_LOVED:
            return kind, ""
        if kind == KIND_TOP_TRACKS:
            period = ref or "overall"
            if period not in lastfm.PERIODS:
                raise ListeningSourceError(f"period must be one of: {', '.join(lastfm.PERIODS)}")
            return kind, period
        raise ListeningSourceError("Last.fm kind must be 'loved' or 'top_tracks'")
    if provider == PROVIDER_LISTENBRAINZ:
        if kind == KIND_LB_PLAYLIST:
            match = _MBID_RE.search(ref)
            if not match:
                raise ListeningSourceError("A ListenBrainz playlist id is required")
            return kind, match.group(0).lower()
        if kind == KIND_LB_CREATED_FOR:
            if not ref:
                raise ListeningSourceError("A created-for playlist id is required")
            return kind, ref
        raise ListeningSourceError("ListenBrainz kind must be 'playlist' or 'created_for'")
    raise ListeningSourceError("provider must be 'lastfm' or 'listenbrainz'")


def resolve_created_for_slug(db: Any, user_id: str, ref: str) -> tuple[str, str]:
    """``(slug, title)`` of the created-for playlist ``ref`` names (its mbid or its kind slug)."""
    cfg = _scrobble_config(db, user_id)
    username = _lb_username(cfg)
    if not username:
        raise ListeningSourceError("ListenBrainz is not linked")
    try:
        entries = listenbrainz.list_created_for(username, _lb_config(cfg))
    except ImportListError as exc:
        raise ListeningSourceError(f"ListenBrainz could not be reached ({exc})") from exc
    wanted = ref.strip().lower()
    for entry in entries:
        if entry["mbid"] == wanted or entry["slug"] == wanted:
            return entry["slug"], entry["title"]
    raise ListeningSourceError("That ListenBrainz created-for playlist was not found")


def _to_tracks(items: list[ImportListItem]) -> list[Track]:
    return [
        Track(title=i.track_title, artist=i.artist_name, album=i.album_title)
        for i in items
        if i.kind == "track" and i.track_title and i.artist_name
    ]


def fetch_listening_tracks(db: Any, config: Any, playlist: dict[str, Any]) -> list[Track]:
    """Fetches a listening playlist's current tracks with its OWNER's linked credentials.

    Raises ListeningSourceError when the owner is gone, the account is no longer linked or the provider fails.
    """
    owner_id = playlist.get("creator_id")
    if not owner_id or db.get_user(str(owner_id)) is None:
        raise ListeningSourceError("The playlist owner no longer exists")
    cfg = _scrobble_config(db, str(owner_id))
    provider = str(playlist.get("service") or "")
    kind = str(playlist.get("source_kind") or "")
    ref = str(playlist.get("source_ref") or "")
    try:
        if provider == PROVIDER_LASTFM:
            username = _lastfm_username(cfg)
            if not username:
                raise ListeningSourceError("The owner's Last.fm account is no longer linked")
            api_key = _lastfm_key(db, config)
            if not api_key:
                raise ListeningSourceError("No Last.fm API key is configured on the server")
            items = lastfm.fetch(
                {
                    "username": username,
                    "api_key": api_key,
                    "source": "loved_tracks" if kind == KIND_LOVED else "top_tracks",
                    "period": ref or "overall",
                    "limit": TRACK_LIMIT,
                }
            )
        elif provider == PROVIDER_LISTENBRAINZ:
            username = _lb_username(cfg)
            if not username:
                raise ListeningSourceError("The owner's ListenBrainz account is no longer linked")
            lb_cfg = _lb_config(cfg)
            if kind == KIND_LB_CREATED_FOR:
                newest = listenbrainz.newest_created_for(username, ref, lb_cfg)
                if newest is None:
                    raise ListeningSourceError("ListenBrainz has not generated this playlist yet")
                items = listenbrainz.playlist_items(newest["mbid"], lb_cfg)
            elif kind == KIND_LB_PLAYLIST:
                items = listenbrainz.playlist_items(ref, lb_cfg)
            else:
                raise ListeningSourceError("Unknown ListenBrainz playlist kind")
        else:
            raise ListeningSourceError("Not a listening playlist")
    except ImportListError as exc:
        raise ListeningSourceError(f"{provider} request failed ({exc})") from exc
    tracks = _to_tracks(items)[:TRACK_LIMIT]
    if not tracks:
        # An empty result would clear the playlist on the media server; leave it as it was instead.
        raise ListeningSourceError("The source returned no tracks")
    return tracks


def auto_request_missing(db: Any, config: Any, playlist: dict[str, Any]) -> dict[str, int]:
    """Requests a listening playlist's missing tracks as its owner, when the playlist opted in and the owner may.

    The owner's rights are re-read on every call; a revoked permission silently stops requesting without touching
    the stored opt-in. Quota, duplicate and approval policy are those of ``submit_track_request``; the first quota
    rejection ends the pass (logged once) and the rest are retried on the next sync. Returns apply-style counts.
    """
    counts = {"applied": 0, "unresolved": 0, "pending": 0, "failed": 0}
    if not playlist.get("auto_request"):
        return counts
    playlist_id = str(playlist["id"])
    owner_id = playlist.get("creator_id")
    owner = db.get_user(str(owner_id)) if owner_id else None
    if owner is None or not user_may_auto_request(owner):
        logger.info("Playlist %s: owner may no longer auto-request tracks; skipping requests", playlist_id)
        return counts
    trigger = GrabTrigger(
        TRIGGER_PLAYLIST, ref=playlist_id, label=playlist.get("name"), actor_user_id=str(owner["id"])
    )
    for row in (t for t in db.get_missing_tracks(playlist_id) if not t.get("list_applied_at")):
        track_id = int(row["id"])
        try:
            submit_track_request(
                db,
                config,
                owner,
                str(row.get("title") or ""),
                str(row.get("artist") or ""),
                str(row.get("album") or "") or None,
                source="playlist",
                trigger=trigger,
            )
        except RequestRejected as exc:
            if exc.code == "duplicate":
                # Already requested: remember it so the sync stops asking.
                db.mark_missing_tracks_list_applied([track_id])
                counts["applied"] += 1
                continue
            logger.info("Playlist %s: request quota reached, no further requests this sync (%s)", playlist_id, exc.detail)
            counts["pending"] += 1
            break
        except Exception:  # one bad track must not abort the rest; the cause is logged with its traceback
            logger.exception("Playlist %s: requesting missing track %s failed", playlist_id, track_id)
            counts["failed"] += 1
            continue
        db.mark_missing_tracks_list_applied([track_id])
        counts["applied"] += 1
    return counts


__all__ = [
    "LISTENING_SERVICES",
    "ListeningSourceError",
    "auto_request_missing",
    "created_for_name",
    "describe_source",
    "fetch_listening_tracks",
    "list_sources",
    "playlist_id_for",
    "resolve_created_for_slug",
    "validate_source",
]
