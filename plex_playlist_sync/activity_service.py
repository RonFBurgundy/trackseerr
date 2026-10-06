"""Activity (queue / history / blocklist) and Wanted (missing / cutoff unmet), served from either manager.

``native`` mode reads TrackSeerr's own tables; ``lidarr`` mode proxies Lidarr API v1 live with Lidarr's own paging and
sort. Both produce the same normalised record shapes (see docs/system-activity-redesign.md, "Phase 3 API contract").

Lidarr records are normalised defensively: a field that is missing or of an unexpected type becomes ``None`` rather
than failing the whole page. Free text that originates in a client (Lidarr/download-client messages) passes through
``redact_text`` before it leaves this module.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plex_playlist_sync.clients.acquisition import get_acquisition_driver
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.item_history import TRIGGER_RETRY, GrabTrigger, emit
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

SOURCE_NATIVE = "native"
SOURCE_LIDARR = "lidarr"

STALL_AFTER = timedelta(minutes=30)

QUEUE_SORT_KEYS = tuple(Database.QUEUE_SORT_KEYS)
HISTORY_SORT_KEYS = tuple(Database.HISTORY_SORT_KEYS)
BLOCKLIST_SORT_KEYS = tuple(Database.BLOCKLIST_SORT_KEYS)
WANTED_SORT_KEYS = tuple(Database.WANTED_SORT_KEYS)
HISTORY_EVENTS = Database.HISTORY_EVENTS

# Our sort keys -> Lidarr ``sortKey`` values.
LIDARR_QUEUE_SORT = {
    "added_at": "added",
    "artist": "artists.sortName",
    "title": "albums.title",
    "progress": "progress",
    "status": "status",
    "size_bytes": "size",
}
LIDARR_HISTORY_SORT = {"date": "date"}
LIDARR_BLOCKLIST_SORT = {"date": "date", "artist": "artists.sortName"}
# ``last_searched_at`` has no Lidarr sort key (``albums.lastSearchTime`` is not a sortable column), so it is absent
# here and the Wanted routes answer it with a 422 in lidarr mode.
LIDARR_WANTED_SORT = {
    "artist": "artists.sortName",
    "album": "albums.title",
    "title": "albums.title",
    "release_date": "albums.releaseDate",
}
# Our history event filter -> Lidarr history ``eventType`` (blocklisted/upgraded have no Lidarr event type).
LIDARR_EVENT_TYPES = {"grabbed": 1, "imported": 3, "failed": 4, "deleted": 5}
# Lidarr history ``eventType`` strings -> our event names; anything else is passed through lower-cased.
LIDARR_EVENT_NAMES = {
    "grabbed": "grabbed",
    "trackfileimported": "imported",
    "downloadimported": "imported",
    "downloadfolderimported": "imported",
    "downloadfailed": "failed",
    "trackfiledeleted": "deleted",
}

_TIMELEFT_RE = re.compile(r"^(?:(\d+)\.)?(\d+):(\d{2}):(\d{2})")


def _now() -> datetime:
    """Current UTC time; a seam so stall detection can be tested with frozen time."""
    return datetime.now(timezone.utc)


def _parse_sqlite_ts(value: Any) -> Optional[datetime]:
    """Parses SQLite ``CURRENT_TIMESTAMP`` (``YYYY-MM-DD HH:MM:SS``, UTC) or an ISO-8601 string."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    """SQLite timestamps as ISO-8601 UTC strings (``...Z``); other strings pass through unchanged."""
    parsed = _parse_sqlite_ts(value)
    if parsed is None:
        return str(value) if value else None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(text: Any) -> Optional[str]:
    return redact_text(str(text)) if text not in (None, "") else None


def _dig(obj: Any, *path: str) -> Any:
    """``obj[a][b]...`` that yields None as soon as a level is not a dict."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _page(mode: str, page: int, page_size: int, total: int, sort_key: str, sort_dir: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": mode,
        "page": page,
        "page_size": page_size,
        "total": total,
        "sort_key": sort_key,
        "sort_dir": sort_dir,
        "records": records,
    }


# ----------------------------------------------------------------------------------------------- native: queue


def native_stalled(row: dict[str, Any], now: datetime) -> tuple[bool, Optional[str]]:
    """Native stall rule: ``warning`` status, or ``downloading`` with no progress change for 30 minutes."""
    status = str(row.get("status") or "").lower()
    if status == "warning":
        return True, _clean(row.get("error_message")) or "The download client reported a warning"
    if status == "downloading":
        since = _parse_sqlite_ts(row.get("progress_updated_at") or row.get("created_at"))
        if since is not None and now - since >= STALL_AFTER:
            minutes = int((now - since).total_seconds() // 60)
            return True, f"No progress for {minutes} minutes"
    return False, None


def native_seeding(row: dict[str, Any], media_settings: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Seeding progress of a completed torrent still held for its seed rule; None for everything else.

    Targets come from the grab-time snapshot, falling back to the global limits for legacy rows. Ratio and time
    are the last values the worker read from the client (0 until it has polled).
    """
    if str(row.get("status") or "").lower() != "completed" or str(row.get("protocol") or "").lower() != "torrent":
        return None
    ratio_t = row.get("seed_ratio_target")
    time_t = row.get("seed_time_target_minutes")
    if not row.get("seed_rule_source") and media_settings:
        ratio_t = media_settings.get("seed_ratio_limit")
        time_t = media_settings.get("seed_time_limit_minutes")
    action = str((media_settings or {}).get("seed_complete_action") or "keep")
    if action not in ("keep", "remove", "remove_and_delete"):
        action = "keep"
    seeding_minutes = int(row.get("seeding_seconds") or 0) // 60
    time_target = int(time_t) if time_t else None
    removes_in = max(0, time_target - seeding_minutes) if (action != "keep" and time_target) else None
    return {
        "ratio": float(row.get("seed_ratio_current") or 0.0),
        "ratio_target": float(ratio_t) if ratio_t else None,
        "seeding_minutes": seeding_minutes,
        "time_target_minutes": time_target,
        "removes_in_minutes": removes_in,
        "action": action,
    }


def native_queue_record(
    row: dict[str, Any], now: datetime, media_settings: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    stalled, reason = native_stalled(row, now)
    size = int(row.get("size_bytes") or 0)
    progress = max(0.0, min(1.0, float(row.get("progress") or 0.0)))
    messages = [m for m in (_clean(row.get("error_message")),) if m]
    unmatched = Database._parse_unmatched_files(row.get("unmatched_files"))
    return {
        "id": str(row["id"]),
        "source": SOURCE_NATIVE,
        "artist": row.get("artist"),
        "album": row.get("album_title"),
        "title": row.get("item_title") or row.get("title"),
        "release_title": row.get("title"),
        "item_type": row.get("item_type") or "track",
        "quality": row.get("quality"),
        "protocol": row.get("protocol"),
        "indexer": row.get("indexer"),
        "client": row.get("client_name"),
        "status": str(row.get("status") or "").lower(),
        "progress": progress,
        "size_bytes": size,
        "sizeleft_bytes": int(round(size * (1.0 - progress))) if size else 0,
        "eta_seconds": None,
        "added_at": _iso(row.get("created_at")),
        "stalled": stalled,
        "stalled_reason": reason,
        "messages": messages,
        "request_id": row.get("request_id"),
        "download_id": str(row["id"]),
        "needs_manual_import": bool(unmatched),
        "unmatched_count": len(unmatched),
        "seeding": native_seeding(row, media_settings),
    }


def native_queue(db: Database, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    rows, total = db.list_native_queue(page, page_size, sort_key, sort_dir)
    now = _now()
    media_settings = db.get_media_management_settings()
    return _page(SOURCE_NATIVE, page, page_size, total, sort_key, sort_dir, [native_queue_record(r, now, media_settings) for r in rows])


def empty_index(sort_key: str, sort_dir: str) -> dict[str, Any]:
    """The group index of a list that has none (Lidarr mode: Lidarr pages server-side)."""
    return {"sort_key": sort_key, "sort_dir": sort_dir, "total": 0, "groups": []}


def native_wanted_index(db: Database, kind: str, sort_key: str, sort_dir: str) -> dict[str, Any]:
    total, groups = db.wanted_index(kind, sort_key, sort_dir)
    return {"sort_key": sort_key, "sort_dir": sort_dir, "total": total, "groups": groups}


def native_history_index(db: Database, sort_dir: str, event: Optional[str]) -> dict[str, Any]:
    total, groups = db.download_history_index(sort_dir, event=event)
    return {"sort_key": "date", "sort_dir": sort_dir, "total": total, "groups": groups}


# ------------------------------------------------------------------------------------------- native: history etc.


def native_history_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "source": SOURCE_NATIVE,
        "event": row.get("event"),
        "artist": row.get("artist"),
        "album": row.get("album"),
        "title": row.get("title"),
        "release_title": row.get("release_title"),
        "quality": row.get("quality"),
        "indexer": row.get("indexer"),
        "client": row.get("client"),
        "date": _iso(row.get("created_at")),
        "message": _clean(row.get("message")),
        "can_mark_failed": bool(row.get("can_mark_failed")),
    }


def native_history(db: Database, page: int, page_size: int, sort_dir: str, event: Optional[str]) -> dict[str, Any]:
    rows, total = db.list_download_history(page, page_size, sort_dir, event=event)
    return _page(SOURCE_NATIVE, page, page_size, total, "date", sort_dir, [native_history_record(r) for r in rows])


def native_blocklist_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "source": SOURCE_NATIVE,
        "artist": row.get("artist"),
        "album": row.get("album"),
        "title": row.get("album") or row.get("source_title"),
        "release_title": row.get("source_title"),
        "quality": None,
        "indexer": row.get("indexer"),
        "protocol": row.get("protocol"),
        "reason": _clean(row.get("reason")),
        "date": _iso(row.get("created_at")),
    }


def native_blocklist(db: Database, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    rows, total = db.list_blocklist_page(page, page_size, sort_key, sort_dir)
    return _page(SOURCE_NATIVE, page, page_size, total, sort_key, sort_dir, [native_blocklist_record(r) for r in rows])


def native_wanted_record(row: dict[str, Any], kind: str) -> dict[str, Any]:
    rec = {
        "id": str(row["id"]),
        "source": SOURCE_NATIVE,
        "artist": row.get("artist"),
        "album": row.get("album"),
        "album_id": str(row["album_id"]) if row.get("album_id") else None,
        "title": row.get("title"),
        "item_type": "track",
        "release_date": row.get("release_date"),
        "monitored": bool(row.get("monitored", 1)),
        "last_searched_at": _iso(row.get("last_searched_at")),
    }
    if kind == "cutoff":
        rec["current_quality"] = row.get("current_quality")
        rec["cutoff_quality"] = row.get("cutoff_quality")
    return rec


def native_wanted(db: Database, kind: str, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    rows, total = db.list_wanted(kind, page, page_size, sort_key, sort_dir)
    return _page(SOURCE_NATIVE, page, page_size, total, sort_key, sort_dir, [native_wanted_record(r, kind) for r in rows])


# --------------------------------------------------------------------------------------- native: mutating actions


def _stop_at_client(db: Database, row: dict[str, Any]) -> None:
    """Best-effort cancel of a download at its client; a missing/unreachable client is logged, never fatal."""
    client_id = row.get("client_id")
    client_cfg = db.get_download_client(client_id) if client_id else None
    if not client_cfg:
        logger.warning("Cannot remove %s from its client: client %s no longer exists", row.get("id"), client_id)
        return
    try:
        get_acquisition_driver(client_cfg).cancel(row.get("download_hash") or str(row["id"]))
    except Exception as exc:  # driver errors span HTTP, auth and parsing failures; removal still proceeds
        logger.warning("Could not cancel %s at its download client: %s", row.get("id"), redact_text(str(exc)))


def _blocklist_release(db: Database, *, title: str, artist: Optional[str], album: Optional[str], guid: Optional[str],
                       info_hash: Optional[str], protocol: Optional[str], indexer: Optional[str], reason: str,
                       download_id: Optional[str] = None) -> None:
    db.add_to_blocklist(
        source_title=title,
        artist=artist,
        album=album,
        release_guid=guid,
        info_hash=info_hash,
        protocol=protocol,
        indexer=indexer,
        reason=reason,
        download_id=download_id,
    )


def native_delete_queue_item(db: Database, download_id: str, remove_from_client: bool, blocklist: bool) -> Optional[str]:
    """Removes a native queue item; returns a message, or None when it does not exist."""
    row = db.get_native_queue_item(download_id)
    if row is None:
        return None
    if remove_from_client:
        _stop_at_client(db, row)
    if blocklist:
        _blocklist_release(
            db,
            title=str(row.get("title") or ""),
            artist=row.get("artist"),
            album=row.get("album_title"),
            guid=str(row["id"]),
            info_hash=row.get("download_hash"),
            protocol=row.get("protocol"),
            indexer=row.get("indexer"),
            reason="Removed from the queue and blocklisted by an administrator",
            download_id=str(row["id"]),
        )
    db.record_download_event(
        "deleted",
        download_id=str(row["id"]),
        message="Removed from the queue" + (" and blocklisted" if blocklist else ""),
    )
    db.delete_active_download(str(row["id"]))
    return "Removed from the queue" + (" and blocklisted" if blocklist else "")


class HistoryConflict(Exception):
    """The history row is not in a state that allows the requested action (maps to HTTP 409)."""


def _search_again(
    db: Database,
    *,
    artist: Optional[str],
    title: Optional[str],
    album: Optional[str],
    item_type: Optional[str],
    request_id: Optional[str],
    track_id: Optional[str],
    album_id: Optional[str],
    actor_user_id: Optional[str] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Runs one indexer search + grab; returns ``(grabbed, message, raw_result)``."""
    from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator  # local: avoids an import cycle

    if not artist or not title:
        return False, "Not enough information to search again", {}
    res = acquisition_coordinator.search_and_grab(
        artist=artist,
        title=title,
        album=album,
        item_type=item_type or "track",
        request_id=request_id,
        db=db,
        track_id=track_id,
        album_id=album_id,
        trigger=GrabTrigger(TRIGGER_RETRY, actor_user_id=actor_user_id),
    )
    if res.get("success"):
        return True, f"Grabbed '{redact_text(str(res.get('release')))}'", res
    return False, redact_text(str(res.get("message") or "No release found")), res


def native_retry_queue_item(
    db: Database, download_id: str, actor_user_id: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Searches again for the item behind a queue row; None when the row does not exist.

    The stuck row is only dropped once the new search actually grabbed a release. When nothing is found the row is
    left in place (and the client keeps its download) and the result is ``{success: false, message}``.
    """
    row = db.get_native_queue_item(download_id)
    if row is None:
        return None
    grabbed, message, res = _search_again(
        db,
        artist=row.get("artist"),
        title=row.get("item_title") or row.get("title"),
        album=row.get("album_title"),
        item_type=row.get("item_type"),
        request_id=row.get("request_id"),
        track_id=row.get("track_id"),
        album_id=row.get("album_id"),
        actor_user_id=actor_user_id,
    )
    if not grabbed:
        return {"success": False, "message": message}
    new_id = res.get("download_id")
    if new_id and str(new_id) != str(row["id"]):
        # Never cancel at the client when the replacement is the very same torrent/NZB the stuck row points at.
        same_download = bool(row.get("download_hash")) and row.get("download_hash") == res.get("download_hash")
        if not same_download:
            _stop_at_client(db, row)
        db.record_download_event("deleted", download_id=str(row["id"]), message="Replaced by a new grab")
        db.delete_active_download(str(row["id"]))
    return {"success": True, "message": message}


def native_mark_history_failed(
    db: Database, history_id: str, actor_user_id: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Marks a grab as failed: blocklists the release, records ``failed``, then searches for a replacement.

    Returns None when the history row does not exist; raises ``HistoryConflict`` when it cannot be marked (not a
    grab, superseded by a newer grab of the same download, or already failed/blocklisted), so no duplicate
    ``failed``/blocklist rows are written.

    The release is blocklisted whether or not a replacement is found, because that is what "failed" means. The
    result is ``success: true`` either way (the mark itself succeeded); the message says whether a replacement was
    grabbed.
    """
    hist = db.get_download_history_item(history_id)
    if hist is None:
        return None
    if not hist.get("can_mark_failed"):
        raise HistoryConflict("This grab was already marked failed or has been superseded; nothing to mark")
    reason = "Marked as failed from history"
    download_id = hist.get("download_id")
    live = db.get_native_queue_item(download_id) if download_id else None
    if live is not None and live.get("status") not in ("failed", "imported"):
        _stop_at_client(db, live)
        # The status transition itself writes the ``failed`` history row.
        db.update_download_status(str(live["id"]), "failed", error_message=reason)
    else:
        db.record_download_event(
            "failed",
            download_id=download_id,
            request_id=hist.get("request_id"),
            track_id=hist.get("track_id"),
            album_id=hist.get("album_id"),
            item_type=hist.get("item_type"),
            artist=hist.get("artist"),
            album=hist.get("album"),
            title=hist.get("title"),
            release_title=hist.get("release_title"),
            quality=hist.get("quality"),
            indexer=hist.get("indexer"),
            protocol=hist.get("protocol"),
            client=hist.get("client"),
            info_hash=hist.get("info_hash"),
            release_guid=hist.get("release_guid"),
            message=reason,
        )
        emit(
            db, "download_failed", track_id=hist.get("track_id"), album_id=hist.get("album_id"),
            artist_name=hist.get("artist") or "", request_id=hist.get("request_id"), download_id=download_id,
            actor_user_id=actor_user_id, message=reason,
            details={
                "release": hist.get("release_title"), "indexer": hist.get("indexer"),
                "client": hist.get("client"), "protocol": hist.get("protocol"),
            },
        )
    release = hist.get("release_title") or hist.get("title")
    if release:
        _blocklist_release(
            db,
            title=str(release),
            artist=hist.get("artist"),
            album=hist.get("album"),
            guid=hist.get("release_guid"),
            info_hash=hist.get("info_hash"),
            protocol=hist.get("protocol"),
            indexer=hist.get("indexer"),
            reason=reason,
            download_id=str(download_id) if download_id else None,
        )
    grabbed, message, _res = _search_again(
        db,
        artist=hist.get("artist"),
        title=hist.get("title"),
        album=hist.get("album"),
        item_type=hist.get("item_type"),
        request_id=hist.get("request_id"),
        track_id=hist.get("track_id"),
        album_id=hist.get("album_id"),
        actor_user_id=actor_user_id,
    )
    if grabbed:
        return {"success": True, "message": f"Marked as failed and blocklisted. {message}"}
    return {"success": True, "message": "Marked failed; no replacement found yet"}


# ----------------------------------------------------------------------------------------------- lidarr: mapping


def _lidarr_quality(rec: dict[str, Any]) -> Optional[str]:
    name = _dig(rec, "quality", "quality", "name")
    return str(name) if name else None


def _timeleft_seconds(value: Any) -> Optional[int]:
    match = _TIMELEFT_RE.match(str(value or ""))
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def lidarr_queue_record(rec: dict[str, Any]) -> dict[str, Any]:
    size = _num(rec.get("size"))
    left = _num(rec.get("sizeleft"))
    progress = max(0.0, min(1.0, 1.0 - left / size)) if size > 0 else 0.0
    tracked = str(rec.get("trackedDownloadStatus") or "").lower()
    stalled = tracked in ("warning", "error")
    messages: list[str] = []
    if rec.get("errorMessage"):
        messages.append(redact_text(str(rec["errorMessage"])))
    for entry in rec.get("statusMessages") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("title"):
            messages.append(redact_text(str(entry["title"])))
        messages.extend(redact_text(str(m)) for m in (entry.get("messages") or []))
    reason = None
    if stalled:
        reason = messages[0] if messages else f"Lidarr reports a {tracked} for this download"
    return {
        "id": str(rec.get("id")),
        "source": SOURCE_LIDARR,
        "artist": _dig(rec, "artist", "artistName"),
        "album": _dig(rec, "album", "title"),
        "title": _dig(rec, "album", "title") or rec.get("title"),
        "release_title": _clean(rec.get("title")),
        "item_type": "album",
        "quality": _lidarr_quality(rec),
        "protocol": rec.get("protocol"),
        "indexer": rec.get("indexer"),
        "client": rec.get("downloadClient"),
        "status": str(rec.get("status") or "").lower(),
        "progress": progress,
        "size_bytes": int(size),
        "sizeleft_bytes": int(left),
        "eta_seconds": _timeleft_seconds(rec.get("timeleft")),
        "added_at": rec.get("added"),
        "stalled": stalled,
        "stalled_reason": reason,
        "messages": messages,
        "request_id": None,
        "download_id": None,
        "needs_manual_import": False,
        "unmatched_count": 0,
        "seeding": None,
    }


def lidarr_history_record(rec: dict[str, Any]) -> dict[str, Any]:
    raw_event = str(rec.get("eventType") or "").lower()
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    return {
        "id": str(rec.get("id")),
        "source": SOURCE_LIDARR,
        "event": LIDARR_EVENT_NAMES.get(raw_event, raw_event),
        "artist": _dig(rec, "artist", "artistName"),
        "album": _dig(rec, "album", "title"),
        "title": _dig(rec, "album", "title") or rec.get("sourceTitle"),
        "release_title": _clean(rec.get("sourceTitle")),
        "quality": _lidarr_quality(rec),
        "indexer": data.get("indexer"),
        "client": data.get("downloadClientName") or data.get("downloadClient"),
        "date": rec.get("date"),
        "message": _clean(data.get("message") or rec.get("sourceTitle")),
        # Lidarr decides what it accepts; only a grab is something it can mark failed.
        "can_mark_failed": LIDARR_EVENT_NAMES.get(raw_event, raw_event) == "grabbed",
    }


def lidarr_blocklist_record(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(rec.get("id")),
        "source": SOURCE_LIDARR,
        "artist": _dig(rec, "artist", "artistName"),
        "album": None,
        "title": rec.get("sourceTitle"),
        "release_title": _clean(rec.get("sourceTitle")),
        "quality": _lidarr_quality(rec),
        "indexer": rec.get("indexer"),
        "protocol": rec.get("protocol"),
        "reason": _clean(rec.get("message")),
        "date": rec.get("date"),
    }


def lidarr_wanted_record(rec: dict[str, Any], kind: str) -> dict[str, Any]:
    out = {
        "id": str(rec.get("id")),
        "source": SOURCE_LIDARR,
        "artist": _dig(rec, "artist", "artistName"),
        "album": rec.get("title"),
        "album_id": str(rec["id"]) if rec.get("id") is not None else None,
        "title": rec.get("title"),
        "item_type": "album",
        "release_date": rec.get("releaseDate"),
        "monitored": bool(rec.get("monitored", True)),
        "last_searched_at": rec.get("lastSearchTime"),
    }
    if kind == "cutoff":
        out["current_quality"] = None
        out["cutoff_quality"] = None
    return out


def _lidarr_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in payload.get("records", []) if isinstance(r, dict)]


def _lidarr_total(payload: dict[str, Any], fallback: int) -> int:
    try:
        return int(payload.get("totalRecords"))
    except (TypeError, ValueError):
        return fallback


# ------------------------------------------------------------------------------------------- lidarr: list calls


def lidarr_queue(client: LidarrClient, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    payload = client.get_queue(page, page_size, LIDARR_QUEUE_SORT[sort_key], sort_dir)
    records = [lidarr_queue_record(r) for r in _lidarr_records(payload)]
    return _page(SOURCE_LIDARR, page, page_size, _lidarr_total(payload, len(records)), sort_key, sort_dir, records)


def lidarr_history(client: LidarrClient, page: int, page_size: int, sort_dir: str, event: Optional[str]) -> dict[str, Any]:
    if event and event not in LIDARR_EVENT_TYPES:
        # Lidarr has no history event type for this filter (blocklisted / upgraded): nothing to list.
        return _page(SOURCE_LIDARR, page, page_size, 0, "date", sort_dir, [])
    payload = client.get_history(page, page_size, LIDARR_HISTORY_SORT["date"], sort_dir, LIDARR_EVENT_TYPES.get(event or ""))
    records = [lidarr_history_record(r) for r in _lidarr_records(payload)]
    return _page(SOURCE_LIDARR, page, page_size, _lidarr_total(payload, len(records)), "date", sort_dir, records)


def lidarr_blocklist(client: LidarrClient, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    payload = client.get_blocklist(page, page_size, LIDARR_BLOCKLIST_SORT[sort_key], sort_dir)
    records = [lidarr_blocklist_record(r) for r in _lidarr_records(payload)]
    return _page(SOURCE_LIDARR, page, page_size, _lidarr_total(payload, len(records)), sort_key, sort_dir, records)


def lidarr_wanted(client: LidarrClient, kind: str, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
    payload = client.get_wanted(kind, page, page_size, LIDARR_WANTED_SORT[sort_key], sort_dir)
    records = [lidarr_wanted_record(r, kind) for r in _lidarr_records(payload)]
    return _page(SOURCE_LIDARR, page, page_size, _lidarr_total(payload, len(records)), sort_key, sort_dir, records)


def lidarr_album_id_for_queue_item(client: LidarrClient, queue_id: int) -> Optional[int]:
    """Finds the album behind a Lidarr queue item by scanning the queue (Lidarr has no get-one endpoint)."""
    for page in range(1, 11):
        payload = client.get_queue(page, 200, "added", "desc")
        for rec in _lidarr_records(payload):
            if rec.get("id") == queue_id:
                album_id = rec.get("albumId")
                return int(album_id) if isinstance(album_id, int) else None
        if page * 200 >= _lidarr_total(payload, 0):
            break
    return None
