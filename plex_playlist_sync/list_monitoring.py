"""Applies a playlist or import-list item to the library at a chosen monitor level.

One entry point, :func:`apply_list_item`, serves both playlists (their missing tracks) and import lists. The mode
says how far to go, and is widened to the item's own kind (an album can't be applied "as a track"):

=========  ======  ======  ======
mode       track   album   artist
=========  ======  ======  ======
track      track   album   artist
album      album   album   artist
artist     artist  artist  artist
none       record only (no library change)
=========  ======  ======  ======

Applying never unmonitors anything and never touches an artist that already exists beyond filling in what the item
needs, so a user who later unmonitors something keeps that choice. Callers record a successful apply and do not
apply the same item again.
"""

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from plex_playlist_sync import lidarr_library
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.library_manager import MODE_LIDARR, ModeChanged, build_lidarr_client, run_for_mode
from plex_playlist_sync.library_monitoring import (
    LIST_MONITOR_MODES,
    NATIVE_MONITOR_OPTIONS,
    validate_list_monitor_mode,
    validate_monitor_option,
)
from plex_playlist_sync.models import LibraryAlbum, LibraryArtist, LibraryTrack
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.request_submission import RequestRejected, submit_track_request
from plex_playlist_sync.storage import Database, clean_library_name

logger = logging.getLogger(__name__)

__all__ = [
    "LIST_MONITOR_MODES",
    "ApplyResult",
    "ListItem",
    "apply_list_item",
    "apply_playlist_missing",
    "apply_playlist_missing_safely",
    "effective_level",
]

LEVELS = ("track", "album", "artist")
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2}
# Lidarr adds albums to a new artist asynchronously after the add call returns.
LIDARR_ALBUM_WAIT_ATTEMPTS = 6
LIDARR_ALBUM_WAIT_SECONDS = 3.0

STATUS_APPLIED = "applied"
STATUS_PENDING = "pending"
STATUS_UNRESOLVED = "unresolved"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class ListItem:
    """An item to apply: an artist, album or track as a name plus whatever MusicBrainz ids are known."""

    kind: str
    artist_name: str = ""
    album_title: str = ""
    track_title: str = ""
    mbid: Optional[str] = None
    artist_mbid: Optional[str] = None


@dataclass
class ApplyResult:
    """Outcome of applying one item.

    ``status`` is ``applied``, ``skipped`` (mode ``none``), ``unresolved`` (no MusicBrainz match: permanent),
    ``pending`` (a transient problem such as Lidarr not ready: try again on the next sync) or ``failed``.
    ``mbid`` is the item's resolved MusicBrainz id when one was found; ``error`` carries a reason or a note.
    """

    status: str
    applied_level: Optional[str] = None
    error: Optional[str] = None
    mbid: Optional[str] = None


class Unresolved(Exception):
    """The item has no usable MusicBrainz match."""


class Transient(Exception):
    """A temporary problem; the item stays pending and is retried on a later sync."""


@dataclass
class _Resolved:
    artist_name: str
    artist_mbid: Optional[str] = None
    album_title: str = ""
    album_mbid: Optional[str] = None


def effective_level(kind: str, mode: str) -> Optional[str]:
    """The level to apply for an item of ``kind`` under ``mode``: the mode widened to the kind, or None for ``none``."""
    mode = validate_list_monitor_mode(mode)
    if kind not in LEVELS:
        raise ValueError(f"Unknown item kind {kind!r}")
    if mode == "none":
        return None
    return max(kind, mode, key=lambda level: _LEVEL_RANK[level])


# --------------------------------------------------------------------------------------------- resolution


def _resolve(item: ListItem, level: str, enricher: MbidEnricherClient) -> _Resolved:
    """MusicBrainz ids for the artist (level ``artist``) or artist + release group (level ``album``)."""
    artist_name = item.artist_name.strip()
    if not artist_name:
        raise Unresolved("Item has no artist name")
    artist_mbid = item.mbid if item.kind == "artist" else item.artist_mbid
    if level == "artist":
        artist_mbid = artist_mbid or enricher.lookup_artist_mbid(artist_name)
        if not artist_mbid:
            raise Unresolved(f"No MusicBrainz artist found for {artist_name!r}")
        return _Resolved(artist_name=artist_name, artist_mbid=artist_mbid)

    album_title = item.album_title.strip()
    album_mbid = item.mbid if item.kind == "album" else None
    if not (album_mbid and artist_mbid):
        found: Optional[dict[str, Optional[str]]] = None
        if item.kind == "track":
            track_ids = enricher.lookup_track_mbids(artist_name, album_title, item.track_title.strip())
            if track_ids:
                found = {
                    "mb_release_group_id": track_ids.get("musicbrainz_releasegroupid"),
                    "mb_artist_id": track_ids.get("musicbrainz_artistid"),
                }
        if (not found or not found.get("mb_release_group_id")) and album_title:
            found = enricher.lookup_album_mbids(artist_name, album_title)
        if found:
            album_mbid = album_mbid or found.get("mb_release_group_id")
            artist_mbid = artist_mbid or found.get("mb_artist_id")
    if not album_mbid or not artist_mbid:
        label = album_title or item.track_title.strip() or "item"
        raise Unresolved(f"No MusicBrainz release group found for {artist_name!r} - {label!r}")
    return _Resolved(artist_name=artist_name, artist_mbid=artist_mbid, album_title=album_title, album_mbid=album_mbid)


# --------------------------------------------------------------------------------------------- native mode


def _artist_folder(db: Database, name: str) -> str:
    root = str(db.get_media_management_settings().get("root_folder_path") or "/music")
    return str(Path(root) / name.replace("/", "_").replace("\\", "_"))


def _ensure_native_artist(
    db: Database,
    resolved: _Resolved,
    monitor_option: str,
    quality_profile_id: Optional[str],
) -> tuple[dict[str, Any], bool]:
    """The library artist for ``resolved`` and whether this call created it. An existing artist is left as it is
    (only a missing MusicBrainz id is filled in); a new one gets ``monitor_option`` and is monitored."""
    existing = None
    if resolved.artist_mbid:
        existing = db.get_library_artist_by_mbid(resolved.artist_mbid)
    if existing is None:
        existing = db.get_library_artist_by_name(resolved.artist_name)
    if existing is not None:
        if resolved.artist_mbid and not existing.get("mbid"):
            existing = db.upsert_library_artist(
                {**existing, "mbid": resolved.artist_mbid}, preserve_monitoring=True
            )
        return existing, False
    created = db.upsert_library_artist(
        LibraryArtist(
            id=str(uuid.uuid4()),
            name=resolved.artist_name,
            clean_name=clean_library_name(resolved.artist_name),
            path=_artist_folder(db, resolved.artist_name),
            monitored=True,
            monitor_option=monitor_option,
            quality_profile_id=quality_profile_id,
            mbid=resolved.artist_mbid,
        ),
        preserve_monitoring=True,
    )
    return created, True


def _release_group_meta(enricher: MbidEnricherClient, artist_mbid: Optional[str], rg_id: str) -> dict[str, Any]:
    if not artist_mbid:
        return {}
    for rg in enricher.get_artist_discography(artist_mbid) or []:
        if rg.get("id") == rg_id:
            return rg
    return {}


def _hydrate_album_tracks(
    db: Database, enricher: MbidEnricherClient, artist_id: str, album_id: str, rg_id: str
) -> None:
    """Fills the album's tracklist from MusicBrainz; tracks already present keep their monitored flag."""
    for trk in enricher.get_release_group_tracks(rg_id) or []:
        title = trk.get("title") or "Unknown Track"
        number = int(trk.get("track_number") or 1)
        existing = db.get_library_track_by_title(album_id, title, track_number=number)
        if existing:
            continue
        db.upsert_library_track(
            LibraryTrack(
                id=str(uuid.uuid4()),
                album_id=album_id,
                artist_id=artist_id,
                title=title,
                clean_title=clean_library_name(title),
                track_number=number,
                disc_number=int(trk.get("disc_number") or 1),
                duration_seconds=trk.get("duration_seconds"),
                monitored=True,
                mb_recording_id=trk.get("mb_recording_id"),
            )
        )


def _apply_album_native(
    db: Database, enricher: MbidEnricherClient, resolved: _Resolved, quality_profile_id: Optional[str]
) -> Optional[str]:
    """Native album level. Returns a note for the caller when the result needs explaining."""
    assert resolved.album_mbid
    # A brand-new artist is monitored with option "none" so a later refresh adds no further albums as monitored.
    artist, created = _ensure_native_artist(db, resolved, "none", quality_profile_id)
    artist_id = str(artist["id"])
    meta = _release_group_meta(enricher, resolved.artist_mbid, resolved.album_mbid)
    title = str(meta.get("title") or resolved.album_title or "Unknown Album")

    album = db.get_library_album_by_release_group_id(resolved.album_mbid) or db.get_library_album_by_title(
        artist_id, title
    )
    if album is None:
        album = db.upsert_library_album(
            LibraryAlbum(
                id=str(uuid.uuid4()),
                artist_id=artist_id,
                title=title,
                clean_title=clean_library_name(title),
                mb_release_group_id=resolved.album_mbid,
                album_type=str(meta.get("album_type") or "album"),
                year=meta.get("year"),
                release_date=meta.get("release_date"),
                cover_url=meta.get("cover_url"),
                monitored=True,
                path=str(Path(artist["path"]) / title) if artist.get("path") else None,
            )
        )
    album_id = str(album["id"])
    _hydrate_album_tracks(db, enricher, artist_id, album_id, resolved.album_mbid)
    db.monitor_library_album_and_tracks(album_id)
    if not created and not artist.get("monitored"):
        return "Artist is unmonitored, so the album will not appear in Wanted until the artist is monitored"
    return None


def _apply_artist_native(
    db: Database,
    enricher: MbidEnricherClient,
    resolved: _Resolved,
    monitor_option: str,
    quality_profile_id: Optional[str],
    *,
    resume: bool = False,
    on_artist_added: Optional[Callable[[], None]] = None,
) -> None:
    """Native artist level. ``resume`` says an earlier attempt of this same item created the artist, so the
    refresh that fills in its albums is still owed even though the artist now exists."""
    artist, created = _ensure_native_artist(db, resolved, monitor_option, quality_profile_id)
    if created and on_artist_added is not None:
        on_artist_added()  # persisted before the refresh so a crash or failure below is retried, not lost
    if not (created or resume):
        return
    # Deferred import: the route module pulls in the whole API layer.
    from plex_playlist_sync.api.routes.library import refresh_single_artist

    result = refresh_single_artist(str(artist["id"]), db, enricher=enricher)
    if not result.get("success", True):
        raise Transient(str(result.get("message") or "Artist refresh failed"))


# --------------------------------------------------------------------------------------------- lidarr mode


def _pick_lidarr_candidate(candidates: list[dict[str, Any]], artist_mbid: Optional[str]) -> Optional[dict[str, Any]]:
    if artist_mbid:
        for c in candidates:
            if str(c.get("foreignArtistId") or "").lower() == artist_mbid.lower():
                return c
    return candidates[0] if candidates else None


def _lidarr_artist(
    client: LidarrClient,
    resolved: _Resolved,
    *,
    whole_artist: bool,
    search: bool = False,
    on_added: Optional[Callable[[], None]] = None,
) -> tuple[int, bool]:
    """Lidarr's id for the artist, adding it with Lidarr's own root-folder defaults if absent. Returns (id, added).

    An artist already in Lidarr is returned untouched. A new one is added whole (``whole_artist``) or unmonitored
    for a release that is monitored afterwards.
    """
    term = f"lidarr:{resolved.artist_mbid}" if resolved.artist_mbid else resolved.artist_name
    candidate = _pick_lidarr_candidate(client.lookup_artist(term), resolved.artist_mbid)
    if candidate is None:
        raise Unresolved(f"Lidarr found no artist for {resolved.artist_name!r}")
    existing_id = int(candidate.get("id") or 0)
    if existing_id:
        return existing_id, False
    added = client.add_artist_with_defaults(candidate, whole_artist=whole_artist, search=search)
    if on_added is not None:
        on_added()  # persisted before the post-add steps so a failure there is retried, not lost
    return int(added.get("id") or 0), True


def _wait_for_lidarr_albums(client: LidarrClient, artist_id: int) -> list[dict[str, Any]]:
    for attempt in range(LIDARR_ALBUM_WAIT_ATTEMPTS):
        albums = client.fetch_artist_albums(artist_id)
        if albums:
            return albums
        if attempt + 1 < LIDARR_ALBUM_WAIT_ATTEMPTS:
            time.sleep(LIDARR_ALBUM_WAIT_SECONDS)
    raise Transient("Lidarr has not loaded this artist's albums yet")


def _norm_title(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _apply_album_lidarr(db: Database, client: LidarrClient, resolved: _Resolved) -> None:
    artist_id, _ = _lidarr_artist(client, resolved, whole_artist=False)
    albums = _wait_for_lidarr_albums(client, artist_id)
    target = next(
        (a for a in albums if str(a.get("foreignAlbumId") or "").lower() == str(resolved.album_mbid).lower()), None
    ) or next((a for a in albums if resolved.album_title and _norm_title(a.get("title")) == _norm_title(resolved.album_title)), None)
    if target is None:
        raise Unresolved(f"Lidarr has no album matching {resolved.album_title or resolved.album_mbid!r}")
    album_id = int(target["id"])
    if target.get("monitored"):
        return  # already monitored (and presumably searched) by the user or an earlier run
    try:
        client.set_albums_monitored([album_id], True)
        if not client.fetch_album(album_id).get("monitored"):
            raise LidarrApiError("Lidarr did not keep the album monitored")
        if db.get_lidarr_settings().get("auto_search", True):
            client.run_command("AlbumSearch", albumIds=[album_id])
    finally:
        lidarr_library.invalidate()


def _apply_artist_lidarr(
    db: Database,
    client: LidarrClient,
    resolved: _Resolved,
    *,
    on_artist_added: Optional[Callable[[], None]] = None,
) -> None:
    """Lidarr artist level: a new artist is added with every root-folder default of Lidarr (the list's own artist
    monitor option does not apply in Lidarr mode) and searched when auto-search is on, all in the one POST. An artist
    that already exists is never touched. Nothing is owed after the add, so a retried item just finds the artist."""
    search = bool(db.get_lidarr_settings().get("auto_search", True))
    try:
        _lidarr_artist(client, resolved, whole_artist=True, search=search, on_added=on_artist_added)
    finally:
        lidarr_library.invalidate()


# --------------------------------------------------------------------------------------------- entry point


def _item_from(item: Any) -> ListItem:
    if isinstance(item, ListItem):
        return item
    return ListItem(
        kind=str(item.get("kind") or ""),
        artist_name=str(item.get("artist_name") or ""),
        album_title=str(item.get("album_title") or ""),
        track_title=str(item.get("track_title") or ""),
        mbid=item.get("mbid"),
        artist_mbid=item.get("artist_mbid"),
    )


def _default_artist_option(db: Database, artist_monitor_option: Optional[str]) -> str:
    if artist_monitor_option:
        return validate_monitor_option(artist_monitor_option)
    fallback = str(db.get_media_management_settings().get("add_monitor_option") or "all")
    return fallback if fallback in NATIVE_MONITOR_OPTIONS else "all"


ALREADY_REQUESTED_NOTE = "already requested"


def _submit_track(
    db: Database, config: Any, item: ListItem, quality_profile_id: Optional[str], requested_by: dict[str, Any]
) -> Optional[str]:
    """Track level: a normal request, attributed to ``requested_by`` but approved and quota-exempt like a system
    request (the user row is copied with admin rights for the policy check only; nothing is persisted on the user)."""
    if not item.track_title.strip():
        raise Unresolved("Track has no title")
    if db.has_open_track_request(item.artist_name, item.track_title) or db.library_track_has_file(
        item.artist_name, item.track_title
    ):
        return ALREADY_REQUESTED_NOTE
    system_user = {**requested_by, "is_admin": True, "forwarded": False}
    try:
        submit_track_request(
            db,
            config,
            system_user,
            item.track_title,
            item.artist_name,
            item.album_title or None,
            quality_profile_id=quality_profile_id,
            source="list",
            foreign_id=item.mbid,
        )
    except RequestRejected as exc:
        # Admin-level submissions are never quota-limited and skip the duplicate check, but stay defensive.
        logger.info("List track request not created (%s): %s", exc.code, exc.detail)
        if exc.code != "duplicate":
            raise
    return None


def apply_list_item(
    db: Database,
    config: Any,
    item: Any,
    mode: str,
    *,
    artist_monitor_option: Optional[str] = None,
    quality_profile_id: Optional[str] = None,
    requested_by: Optional[dict[str, Any]] = None,
    enricher: Optional[MbidEnricherClient] = None,
    lidarr_client: Optional[LidarrClient] = None,
    artist_added: bool = False,
    on_artist_added: Optional[Callable[[], None]] = None,
) -> ApplyResult:
    """Applies ``item`` (a :class:`ListItem` or a dict with the same fields) at the level ``mode`` widens to.

    ``artist_added`` is the persisted fact that an earlier attempt of this item added its artist; the artist
    post-add step (album load + monitor preset, or the native refresh) is then finished on this attempt.
    ``on_artist_added`` is called the moment this attempt adds the artist, so the caller can persist that fact.

    Never raises for a per-item problem; the outcome is the returned :class:`ApplyResult`. Raises ValueError for an
    invalid ``mode`` or ``artist_monitor_option``.
    """
    list_item = _item_from(item)
    level = effective_level(list_item.kind, mode)
    option = _default_artist_option(db, artist_monitor_option)
    if level is None:
        return ApplyResult(STATUS_SKIPPED)

    try:
        if level == "track":
            if requested_by is None:
                return ApplyResult(STATUS_FAILED, error="No user to attribute the request to")
            note = _submit_track(db, config, list_item, quality_profile_id, requested_by)
            return ApplyResult(STATUS_APPLIED, "track", error=note, mbid=list_item.mbid)

        if enricher is None:
            enricher = MbidEnricherClient(base_url=db.get_media_management_settings().get("mb_mirror_url"), timeout=10.0)
        resolved = _resolve(list_item, level, enricher)
        item_mbid = resolved.artist_mbid if level == "artist" else resolved.album_mbid
        notes: list[str] = []

        def _native() -> None:
            if level == "album":
                note = _apply_album_native(db, enricher, resolved, quality_profile_id)
                if note:
                    notes.append(note)
            else:
                _apply_artist_native(
                    db,
                    enricher,
                    resolved,
                    option,
                    quality_profile_id,
                    resume=artist_added,
                    on_artist_added=on_artist_added,
                )

        def _lidarr() -> None:
            client = lidarr_client or build_lidarr_client(db, config)
            if client is None:
                raise Transient("Lidarr is the library manager but is not configured")
            if level == "album":
                _apply_album_lidarr(db, client, resolved)
            else:
                _apply_artist_lidarr(db, client, resolved, on_artist_added=on_artist_added)

        run_for_mode(db, native=_native, lidarr=_lidarr)
        return ApplyResult(STATUS_APPLIED, level, error=notes[0] if notes else None, mbid=item_mbid)
    except Unresolved as exc:
        return ApplyResult(STATUS_UNRESOLVED, error=str(exc))
    except (Transient, ModeChanged) as exc:
        return ApplyResult(STATUS_PENDING, error=str(exc))
    except RequestRejected as exc:
        return ApplyResult(STATUS_FAILED, error=exc.detail)
    except (LidarrApiError, httpx.HTTPError, sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Applying list item %r failed: %s", list_item.artist_name, safe_exc(exc))
        return ApplyResult(STATUS_FAILED, error=safe_exc(exc, (LidarrApiError,)))


# --------------------------------------------------------------------------------------------- playlists


def list_actor(db: Database, preferred_user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The user list-driven requests are attributed to: ``preferred_user_id`` if it exists, else the oldest admin."""
    if preferred_user_id:
        user = db.get_user(str(preferred_user_id))
        if user is not None:
            return user
    admins = [u for u in db.list_users() if u.get("is_admin") and not u.get("disabled")]
    if not admins:
        return None
    oldest = min(admins, key=lambda u: (str(u.get("created_at") or ""), str(u["id"])))
    return db.get_user(str(oldest["id"])) or oldest


def _creator_may_use_mode(db: Database, playlist: dict[str, Any]) -> bool:
    """Album and artist modes add to the library without quota or approval, so only an admin's playlist may use
    them. A playlist with no creator (configured by the operator) is trusted; a creator who no longer exists or
    is no longer an admin is not."""
    creator_id = playlist.get("creator_id")
    if not creator_id:
        return True
    creator = db.get_user(str(creator_id))
    return bool(creator and creator.get("is_admin") and not creator.get("disabled"))


def apply_playlist_missing(
    db: Database, config: Any, playlist_id: str, *, enricher: Optional[MbidEnricherClient] = None
) -> dict[str, int]:
    """Applies a playlist's not-yet-applied missing tracks at the playlist's monitor mode (album or artist).

    ``track`` mode is the existing backlog behaviour and ``none`` records nothing, so both return at once. So does
    album/artist mode on a playlist whose creator is not (or no longer) an admin: it is treated as ``track``.
    A track that resolves is stamped ``list_applied_at`` right after it applies (never applied again, and the
    backlog skips it as a lone track). One that does not resolve stays unstamped: it is retried on the next sync
    and meanwhile searched as a lone track. An unexpected error on one track is logged and counted as failed
    without stopping the rest.
    """
    counts = {"applied": 0, "unresolved": 0, "pending": 0, "failed": 0}
    playlist = db.get_playlist(playlist_id)
    if playlist is None:
        return counts
    mode = str(playlist.get("monitor_mode") or "track")
    if mode in ("track", "none"):
        return counts
    if not _creator_may_use_mode(db, playlist):
        logger.info("Playlist %s: creator is not an admin, treating monitor mode %r as 'track'", playlist_id, mode)
        return counts
    actor = list_actor(db, playlist.get("creator_id"))
    todo = [t for t in db.get_missing_tracks(playlist_id) if not t.get("list_applied_at")]
    for row in todo:
        track_id = int(row["id"])
        try:
            result = apply_list_item(
                db,
                config,
                ListItem(
                    kind="track",
                    artist_name=str(row.get("artist") or ""),
                    album_title=str(row.get("album") or ""),
                    track_title=str(row.get("title") or ""),
                ),
                mode,
                requested_by=actor,
                enricher=enricher,
                artist_added=bool(row.get("artist_added_by_item")),
                on_artist_added=lambda tid=track_id: db.mark_missing_track_artist_added(tid),
            )
        except Exception:  # one bad track must not abort the rest; the cause is logged with its traceback
            logger.exception("Playlist %s: applying missing track %s failed", playlist_id, track_id)
            counts["failed"] += 1
            continue
        counts[result.status if result.status in counts else "failed"] += 1
        if result.status == STATUS_APPLIED:
            db.mark_missing_tracks_list_applied([track_id])
    return counts


def apply_playlist_missing_safely(db: Database, config: Any, playlist_id: str) -> None:
    """``apply_playlist_missing`` for sync code paths: a failure is logged and never aborts the sync."""
    try:
        apply_playlist_missing(db, config, playlist_id)
    except Exception as exc:  # the sync already succeeded; the cause is logged and the next sync retries
        logger.warning("Applying monitor mode for playlist %s failed: %s", playlist_id, safe_exc(exc))
        logger.debug("Playlist monitor-mode traceback", exc_info=True)
