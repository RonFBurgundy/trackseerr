"""Per-item audit trail: the event catalog, grab-trigger provenance and the ``item_events`` persistence mixin.

``item_events`` is append-only and deliberately has no foreign keys: ids are plain TEXT and the names are denormalised,
so an item's history outlives the library rows (which cascade-delete). Recording must never break the caller's real
work, so call sites use :func:`emit`, which logs a failure and swallows it.

This module must not import ``storage`` (storage imports it).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal, Optional, Union

logger = logging.getLogger(__name__)

# --- event catalog ---------------------------------------------------------------------------------------------------
ITEM_EVENTS: frozenset[str] = frozenset(
    {
        "requested",
        "request_approved",
        "request_declined",
        "added_to_library",
        "monitored",
        "unmonitored",
        "searched",
        "grabbed",
        "download_failed",
        "blocklisted",
        "imported",
        "upgraded",
        "file_replaced",
        "file_deleted",
        "file_missing",
        "quarantined",
        "restored",
        "issue_opened",
        "issue_resolved",
        "health_finding",
        "renamed",
        "moved",
        "removed_from_library",
        "tags_changed",
        "retagged",
    }
)

# --- trigger kinds ---------------------------------------------------------------------------------------------------
TRIGGER_REQUEST = "request"
TRIGGER_REQUEST_APPROVED = "request_approved"
TRIGGER_WANTED = "wanted"
TRIGGER_RSS = "rss"
TRIGGER_PLAYLIST = "playlist"
TRIGGER_MIX = "mix"
TRIGGER_IMPORT_LIST = "import_list"
TRIGGER_MANUAL = "manual"
TRIGGER_ISSUE = "issue"
TRIGGER_UPGRADE = "upgrade"
TRIGGER_RETRY = "retry"
TRIGGER_USER = "user"
TRIGGER_SCAN = "scan"
TRIGGER_MANUAL_IMPORT = "manual_import"
TRIGGER_SEED_CLEANUP = "seed_cleanup"
TRIGGER_RECYCLE_CLEANUP = "recycle_cleanup"
TRIGGER_SYSTEM = "system"

TRIGGER_KINDS: frozenset[str] = frozenset(
    {
        TRIGGER_REQUEST, TRIGGER_REQUEST_APPROVED, TRIGGER_WANTED, TRIGGER_RSS, TRIGGER_PLAYLIST, TRIGGER_MIX,
        TRIGGER_IMPORT_LIST, TRIGGER_MANUAL, TRIGGER_ISSUE, TRIGGER_UPGRADE, TRIGGER_RETRY, TRIGGER_USER,
        TRIGGER_SCAN, TRIGGER_MANUAL_IMPORT, TRIGGER_SEED_CLEANUP, TRIGGER_RECYCLE_CLEANUP, TRIGGER_SYSTEM,
    }
)
# Triggers whose label/actor identify a person: redacted for other users in the API.
USER_SCOPED_TRIGGERS: frozenset[str] = frozenset(
    {
        TRIGGER_REQUEST, TRIGGER_REQUEST_APPROVED, TRIGGER_MANUAL, TRIGGER_ISSUE, TRIGGER_USER, TRIGGER_MANUAL_IMPORT,
        TRIGGER_RETRY,  # a retried request carries the requester's username as its label
    }
)


@dataclass(frozen=True)
class GrabTrigger:
    """Why a grab happened: the kind plus optional reference (request id, playlist id...), label and acting user."""

    kind: str
    ref: Optional[str] = None
    label: Optional[str] = None
    actor_user_id: Optional[str] = None

    def to_dict(self) -> dict[str, Optional[str]]:
        return {"kind": self.kind, "ref": self.ref, "label": self.label, "actor_user_id": self.actor_user_id}

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["GrabTrigger"]:
        if not isinstance(raw, dict) or not raw.get("kind"):
            return None
        return cls(
            kind=str(raw["kind"]),
            ref=str(raw["ref"]) if raw.get("ref") is not None else None,
            label=str(raw["label"]) if raw.get("label") is not None else None,
            actor_user_id=str(raw["actor_user_id"]) if raw.get("actor_user_id") is not None else None,
        )


_provenance: contextvars.ContextVar[Optional[GrabTrigger]] = contextvars.ContextVar("item_provenance", default=None)


@contextlib.contextmanager
def provenance(trigger: GrabTrigger) -> Iterator[None]:
    """Default trigger for item events recorded by library mutations made inside the block.

    Library writes (``added_to_library``, ``monitored``, ``removed_from_library``...) happen deep in the storage layer
    where the cause is unknown; the caller that knows it (a route, the scanner, a list worker) wraps its work here.
    Per-thread/context, so concurrent workers never see each other's provenance.
    """
    token = _provenance.set(trigger)
    try:
        yield
    finally:
        _provenance.reset(token)


def set_provenance(trigger: GrabTrigger) -> None:
    """Sets the ambient trigger for the rest of the current context (a request task) with no block to scope it."""
    _provenance.set(trigger)


def current_provenance() -> Optional[GrabTrigger]:
    return _provenance.get()


def trigger_kwargs(trigger: Optional[GrabTrigger]) -> dict[str, Optional[str]]:
    """``record_item_event`` / ``record_download_event`` keyword form of a trigger."""
    if trigger is None:
        return {}
    return {
        "trigger": trigger.kind,
        "trigger_ref": trigger.ref,
        "trigger_label": trigger.label,
        "actor_user_id": trigger.actor_user_id,
    }


def emit(db: Any, event: str, **kwargs: Any) -> Optional[int]:
    """``db.record_item_event`` that can never break the caller: a database failure is logged and swallowed.

    A bad ``event`` name is a programming error and still raises ``ValueError``.
    """
    try:
        return db.record_item_event(event, **kwargs)
    except sqlite3.Error:
        logger.exception("Could not record item event %r", event)
        return None


def emit_named(
    db: Any, event: str, *, artist: Optional[str], album: Optional[str] = None, title: Optional[str] = None,
    **kwargs: Any,
) -> Optional[int]:
    """``emit`` for code that only knows names: resolves library ids by name and skips when nothing matches.

    An event with no id could never be shown on any item, so an unresolvable name is dropped (with a debug log).
    """
    try:
        ids = db.find_library_ids_by_name(artist, album, title)
    except sqlite3.Error:
        logger.exception("Could not resolve library ids for item event %r", event)
        return None
    if not (ids["track_id"] or ids["album_id"] or ids["artist_id"]) and not kwargs.get("request_id"):
        logger.debug("Item event %r for %r / %r has no library item yet; not recorded", event, artist, album or title)
        return None
    if kwargs.get("request_id"):
        # Whatever part of the item is not in the library yet is kept by name under its request; the first event that
        # carries the ids adopts it (see ``record_item_event``). Resolved parts keep the library's own names.
        unresolved = {
            "artist_name": artist if not ids["artist_id"] else None,
            "album_title": album if not ids["album_id"] else None,
            "track_title": title if not ids["track_id"] else None,
        }
        kwargs = {**{k: v for k, v in unresolved.items() if v}, **kwargs}
    return emit(db, event, **{**ids, **kwargs})


def download_trigger_kwargs(db: Any, download_id: Optional[str]) -> dict[str, Optional[str]]:
    """Grab-time provenance of ``download_id`` as item-event kwargs (empty when unknown or the lookup fails)."""
    if not download_id:
        return {}
    try:
        found = db.get_download_trigger(download_id)
    except sqlite3.Error:
        logger.exception("Could not read the grab trigger of download %s", download_id)
        return {}
    return found if isinstance(found, dict) else {}


def request_trigger(
    db: Any, request: dict[str, Any], *, kind: Optional[str] = None, actor_user_id: Optional[str] = None
) -> GrabTrigger:
    """Trigger for a grab made on behalf of ``request``.

    A request raised by a system source (mix, import list, playlist) carries that trigger on its row and keeps it. A
    user request is ``request`` (or ``kind``, e.g. ``request_approved``): label = requester's username, ref = request
    id. The acting user defaults to the requester; an approving admin passes ``actor_user_id`` and the requester stays
    in the label/ref.
    """
    requester_id = str(request.get("user_id") or "") or None
    stored = str(request.get("trigger") or "")
    if stored and stored not in (TRIGGER_REQUEST, TRIGGER_REQUEST_APPROVED):
        return GrabTrigger(
            kind=stored,
            ref=request.get("trigger_ref") or None,
            label=request.get("trigger_label") or None,
            actor_user_id=actor_user_id or requester_id,
        )
    label = request.get("username") or None
    if not label and requester_id:
        try:
            user = db.get_user(requester_id)
        except sqlite3.Error:
            logger.exception("Could not look up requester %s for grab provenance", requester_id)
            user = None
        if isinstance(user, dict):
            label = user.get("username") or None
    return GrabTrigger(
        kind=kind or TRIGGER_REQUEST,
        ref=str(request["id"]) if request.get("id") else None,
        label=label,
        actor_user_id=actor_user_id or requester_id,
    )


# --- persistence -----------------------------------------------------------------------------------------------------
_COLUMNS = (
    "event", "artist_id", "album_id", "track_id", "artist_name", "album_title", "track_title", "trigger",
    "trigger_ref", "trigger_label", "actor_user_id", "request_id", "download_id", "message", "details_json",
)


class ItemHistoryMixin:
    """Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``)."""

    def _migration_v63(self, cur: sqlite3.Cursor) -> None:
        """Append-only ``item_events`` audit trail (no foreign keys) plus grab provenance columns on ``music_requests``
        and ``download_history``. A delayed (parked) grab keeps its trigger inside ``pending_releases.payload_json``."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                artist_id TEXT,
                album_id TEXT,
                track_id TEXT,
                artist_name TEXT NOT NULL DEFAULT '',
                album_title TEXT NOT NULL DEFAULT '',
                track_title TEXT NOT NULL DEFAULT '',
                "trigger" TEXT,
                trigger_ref TEXT,
                trigger_label TEXT,
                actor_user_id TEXT,
                request_id TEXT,
                download_id TEXT,
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_item_events_track ON item_events(track_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_item_events_album ON item_events(album_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_item_events_artist ON item_events(artist_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_item_events_request ON item_events(request_id)")
        for table in ("download_history", "music_requests"):
            have = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            for column in ("trigger", "trigger_ref", "trigger_label"):
                if column not in have:
                    cur.execute(f'ALTER TABLE {table} ADD COLUMN "{column}" TEXT')

    def _resolve_item_context(
        self, track_id: Optional[str], album_id: Optional[str], artist_id: Optional[str]
    ) -> dict[str, Any]:
        """Fills album/artist ids and denormalised names from the library tables for whichever id is given."""
        ctx: dict[str, Any] = {
            "artist_id": artist_id, "album_id": album_id, "artist_name": "", "album_title": "", "track_title": "",
        }
        if track_id:
            row = self.conn.execute(
                "SELECT title, album_id, artist_id FROM library_tracks WHERE id = ?", (str(track_id),)
            ).fetchone()
            if row is not None:
                ctx["track_title"] = row["title"] or ""
                ctx["album_id"] = ctx["album_id"] or row["album_id"]
                ctx["artist_id"] = ctx["artist_id"] or row["artist_id"]
        if ctx["album_id"]:
            row = self.conn.execute(
                "SELECT title, artist_id FROM library_albums WHERE id = ?", (str(ctx["album_id"]),)
            ).fetchone()
            if row is not None:
                ctx["album_title"] = row["title"] or ""
                ctx["artist_id"] = ctx["artist_id"] or row["artist_id"]
        if ctx["artist_id"]:
            row = self.conn.execute(
                "SELECT name FROM library_artists WHERE id = ?", (str(ctx["artist_id"]),)
            ).fetchone()
            if row is not None:
                ctx["artist_name"] = row["name"] or ""
        return ctx

    def record_item_event(
        self,
        event: str,
        *,
        track_id: Optional[str] = None,
        album_id: Optional[str] = None,
        artist_id: Optional[str] = None,
        artist_name: Optional[str] = None,
        album_title: Optional[str] = None,
        track_title: Optional[str] = None,
        trigger: Optional[str] = None,
        trigger_ref: Optional[str] = None,
        trigger_label: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        download_id: Optional[str] = None,
        message: str = "",
        details: Optional[dict[str, Any]] = None,
        dedupe_last: bool = False,
    ) -> int:
        """Appends one ``item_events`` row and returns its id.

        ``dedupe_last`` skips the insert (returning the existing id) when the newest event for the most specific id is
        the same event with the same message: repeated sweeps that find the same thing do not flood the history.

        Parent ids and names are resolved from the library tables when only a track (or album) id is given; explicit
        arguments always win. Raises ``ValueError`` for an event that is not in ``ITEM_EVENTS``.
        """
        if event not in ITEM_EVENTS:
            raise ValueError(f"Unknown item event: {event!r}")
        if trigger is None and trigger_ref is None and trigger_label is None and actor_user_id is None:
            ambient = _provenance.get()
            if ambient is not None:
                trigger, trigger_ref = ambient.kind, ambient.ref
                trigger_label, actor_user_id = ambient.label, ambient.actor_user_id
        with self._lock:
            ctx = self._resolve_item_context(track_id, album_id, artist_id)
            values = self._event_values(
                event, ctx, track_id=track_id, artist_name=artist_name, album_title=album_title,
                track_title=track_title, trigger=trigger, trigger_ref=trigger_ref, trigger_label=trigger_label,
                actor_user_id=actor_user_id, request_id=request_id, download_id=download_id, message=message,
                details=details,
            )
            if dedupe_last:
                for column in ("track_id", "album_id", "artist_id"):
                    if values[column]:
                        last = self.conn.execute(
                            f"SELECT id, event, message FROM item_events WHERE {column} = ? ORDER BY id DESC LIMIT 1",
                            (values[column],),
                        ).fetchone()
                        if last is not None and last["event"] == event and last["message"] == values["message"]:
                            return int(last["id"])
                        break
            with self._item_event_write():
                return self._insert_item_event(values)

    @contextlib.contextmanager
    def _item_event_write(self) -> Iterator[None]:
        """Atomic scope for an event write that never commits (or rolls back) work it did not start.

        With no transaction open the event owns one: commit on success, rollback on a ``sqlite3.Error`` so a failed
        insert cannot ride along with the next unrelated commit. With a caller's transaction open the write is a
        SAVEPOINT: a failure undoes only the event, and committing is left to the caller. The error propagates; the
        recording helpers (``emit`` / ``_safe_item_event``) log and swallow it.
        """
        nested = self.conn.in_transaction
        if nested:
            self.conn.execute("SAVEPOINT item_event")
        try:
            yield
        except sqlite3.Error:
            if nested:
                self.conn.execute("ROLLBACK TO SAVEPOINT item_event")
                self.conn.execute("RELEASE SAVEPOINT item_event")
            else:
                self.conn.rollback()
            raise
        if nested:
            self.conn.execute("RELEASE SAVEPOINT item_event")
        else:
            self.conn.commit()

    @staticmethod
    def _event_values(
        event: str,
        ctx: dict[str, Any],
        *,
        track_id: Optional[str] = None,
        artist_name: Optional[str] = None,
        album_title: Optional[str] = None,
        track_title: Optional[str] = None,
        trigger: Optional[str] = None,
        trigger_ref: Optional[str] = None,
        trigger_label: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        download_id: Optional[str] = None,
        message: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "event": event,
            "artist_id": ctx["artist_id"],
            "album_id": ctx["album_id"],
            "track_id": str(track_id) if track_id else None,
            "artist_name": artist_name if artist_name else ctx["artist_name"],
            "album_title": album_title if album_title else ctx["album_title"],
            "track_title": track_title if track_title else ctx["track_title"],
            "trigger": trigger,
            "trigger_ref": trigger_ref,
            "trigger_label": trigger_label,
            "actor_user_id": actor_user_id,
            "request_id": request_id,
            "download_id": download_id,
            "message": message or "",
            "details_json": json.dumps(details, default=str) if details else None,
        }

    def _insert_item_event(self, values: dict[str, Any]) -> int:
        """Inserts one event row (and lets earlier events of its request adopt the ids); caller owns the commit."""
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        cur = self.conn.execute(
            f"INSERT INTO item_events ({cols}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
            [values[c] for c in _COLUMNS],
        )
        new_id = int(cur.lastrowid)
        request_id = values["request_id"]
        if request_id and (values["track_id"] or values["album_id"] or values["artist_id"]):
            # Earlier events of this request that could not be attached (item not in the library yet) adopt the
            # ids now known. A track-level request event (it carries a track title) takes the track id.
            self.conn.execute(
                "UPDATE item_events SET artist_id = COALESCE(artist_id, ?), album_id = COALESCE(album_id, ?), "
                "track_id = CASE WHEN track_title <> '' THEN COALESCE(track_id, ?) ELSE track_id END "
                "WHERE request_id = ? AND id <> ? AND track_id IS NULL",
                (values["artist_id"], values["album_id"], values["track_id"], request_id, new_id),
            )
        return new_id

    def _resolve_item_contexts(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``_resolve_item_context`` for many events with one chunked query per table (not per event)."""
        chunk = 500

        def fetch(sql: str, ids: set[str]) -> dict[str, Any]:
            found: dict[str, Any] = {}
            wanted = sorted(ids)
            for i in range(0, len(wanted), chunk):
                part = wanted[i : i + chunk]
                marks = ", ".join("?" for _ in part)
                for row in self.conn.execute(sql.format(marks=marks), part).fetchall():
                    found[str(row["id"])] = row
            return found

        tracks = fetch(
            "SELECT id, title, album_id, artist_id FROM library_tracks WHERE id IN ({marks})",
            {str(e["track_id"]) for e in events if e.get("track_id")},
        )
        album_ids = {str(e["album_id"]) for e in events if e.get("album_id")}
        album_ids |= {str(t["album_id"]) for t in tracks.values() if t["album_id"]}
        albums = fetch("SELECT id, title, artist_id FROM library_albums WHERE id IN ({marks})", album_ids)
        artist_ids = {str(e["artist_id"]) for e in events if e.get("artist_id")}
        artist_ids |= {str(t["artist_id"]) for t in tracks.values() if t["artist_id"]}
        artist_ids |= {str(a["artist_id"]) for a in albums.values() if a["artist_id"]}
        artists = fetch("SELECT id, name FROM library_artists WHERE id IN ({marks})", artist_ids)

        contexts: list[dict[str, Any]] = []
        for e in events:
            ctx: dict[str, Any] = {
                "artist_id": e.get("artist_id"), "album_id": e.get("album_id"),
                "artist_name": "", "album_title": "", "track_title": "",
            }
            track = tracks.get(str(e["track_id"])) if e.get("track_id") else None
            if track is not None:
                ctx["track_title"] = track["title"] or ""
                ctx["album_id"] = ctx["album_id"] or track["album_id"]
                ctx["artist_id"] = ctx["artist_id"] or track["artist_id"]
            album = albums.get(str(ctx["album_id"])) if ctx["album_id"] else None
            if album is not None:
                ctx["album_title"] = album["title"] or ""
                ctx["artist_id"] = ctx["artist_id"] or album["artist_id"]
            artist = artists.get(str(ctx["artist_id"])) if ctx["artist_id"] else None
            if artist is not None:
                ctx["artist_name"] = artist["name"] or ""
            contexts.append(ctx)
        return contexts

    def record_item_events_bulk(self, events: list[dict[str, Any]]) -> int:
        """Appends many events in one transaction; returns how many were written.

        Each entry is the keyword arguments of ``record_item_event`` plus an ``event`` key (``dedupe_last`` is not
        supported). Ids and names are resolved with a handful of chunked queries, plain rows go in with one
        ``executemany`` and there is a single commit. Raises ``ValueError`` for an unknown event (nothing written) and
        ``sqlite3.Error`` after rolling back (nothing written).
        """
        if not events:
            return 0
        for entry in events:
            if entry.get("event") not in ITEM_EVENTS:
                raise ValueError(f"Unknown item event: {entry.get('event')!r}")
        ambient = _provenance.get()
        with self._lock:
            contexts = self._resolve_item_contexts(events)
            plain: list[list[Any]] = []
            keyed: list[dict[str, Any]] = []
            for entry, ctx in zip(events, contexts):
                kwargs = {k: v for k, v in entry.items() if k not in ("event", "dedupe_last")}
                if (
                    ambient is not None and all(kwargs.get(k) is None for k in
                                                ("trigger", "trigger_ref", "trigger_label", "actor_user_id"))
                ):
                    kwargs.update(
                        trigger=ambient.kind, trigger_ref=ambient.ref, trigger_label=ambient.label,
                        actor_user_id=ambient.actor_user_id,
                    )
                kwargs.pop("artist_id", None)
                kwargs.pop("album_id", None)
                values = self._event_values(entry["event"], ctx, **kwargs)
                if values["request_id"]:
                    keyed.append(values)  # needs the adoption pass, so it goes through the single-row insert
                else:
                    plain.append([values[c] for c in _COLUMNS])
            with self._item_event_write():
                if plain:
                    cols = ", ".join(f'"{c}"' for c in _COLUMNS)
                    self.conn.executemany(
                        f"INSERT INTO item_events ({cols}) VALUES ({', '.join('?' for _ in _COLUMNS)})", plain
                    )
                for values in keyed:
                    self._insert_item_event(values)
        return len(events)

    def _safe_item_event(self, event: str, **kwargs: Any) -> None:
        """``record_item_event`` for storage-layer hooks: a failure is logged and never breaks the library write."""
        try:
            self.record_item_event(event, **kwargs)
        except sqlite3.Error:
            logger.exception("Could not record item event %r", event)

    def _safe_item_events_bulk(self, events: list[dict[str, Any]]) -> None:
        """``record_item_events_bulk`` for storage-layer hooks: a failure is logged and never breaks the library write."""
        try:
            self.record_item_events_bulk(events)
        except sqlite3.Error:
            logger.exception("Could not record %d item event(s)", len(events))

    def _emit_removed(self, kind: Literal["artist", "album", "track"], entity_id: str) -> None:
        """``removed_from_library`` for a row about to be deleted (names are resolved while the row still exists)."""
        table = {"artist": "library_artists", "album": "library_albums", "track": "library_tracks"}[kind]
        with self._lock:
            try:
                exists = self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)).fetchone() is not None
            except sqlite3.Error:
                logger.exception("Could not check %s %s before removal", kind, entity_id)
                return
            if exists:
                self._safe_item_event(
                    "removed_from_library", message=f"Removed {kind} from the library", **{f"{kind}_id": entity_id}
                )

    def find_library_ids_by_name(
        self, artist: Optional[str], album: Optional[str] = None, title: Optional[str] = None
    ) -> dict[str, Optional[str]]:
        """Best-effort library ids for a request/download that only carries names (all values None when unmatched).

        Resolution is exact on the cleaned names: artist, then album under it, then track (``title``) under that album
        (or under the artist when no album is given). Ambiguity resolves to nothing rather than a guess.
        """
        from plex_playlist_sync.storage import clean_library_name  # local: storage imports this module

        found: dict[str, Optional[str]] = {"artist_id": None, "album_id": None, "track_id": None}
        clean_artist = clean_library_name(artist or "")
        if not clean_artist:
            return found
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM library_artists WHERE clean_name = ? LIMIT 2", (clean_artist,)
            ).fetchall()
            if len(rows) != 1:
                return found
            found["artist_id"] = rows[0]["id"]
            clean_album = clean_library_name(album or "")
            if clean_album:
                rows = self.conn.execute(
                    "SELECT id FROM library_albums WHERE artist_id = ? AND clean_title = ? LIMIT 2",
                    (found["artist_id"], clean_album),
                ).fetchall()
                if len(rows) == 1:
                    found["album_id"] = rows[0]["id"]
            clean_title = clean_library_name(title or "")
            if clean_title:
                sql = "SELECT id, album_id FROM library_tracks WHERE artist_id = ? AND clean_title = ?"
                params: list[Any] = [found["artist_id"], clean_title]
                if found["album_id"]:
                    sql += " AND album_id = ?"
                    params.append(found["album_id"])
                rows = self.conn.execute(sql + " LIMIT 2", params).fetchall()
                if len(rows) == 1:
                    found["track_id"] = rows[0]["id"]
                    found["album_id"] = found["album_id"] or rows[0]["album_id"]
        return found

    def get_download_trigger(self, download_id: Optional[str]) -> dict[str, Optional[str]]:
        """Provenance (trigger/ref/label) stamped on a download's ``grabbed`` history row; empty values when absent."""
        empty: dict[str, Optional[str]] = {"trigger": None, "trigger_ref": None, "trigger_label": None}
        if not download_id:
            return empty
        with self._lock:
            row = self.conn.execute(
                'SELECT "trigger", trigger_ref, trigger_label FROM download_history '
                "WHERE download_id = ? AND event = 'grabbed' AND \"trigger\" IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (str(download_id),),
            ).fetchone()
        if row is None:
            return empty
        return {"trigger": row["trigger"], "trigger_ref": row["trigger_ref"], "trigger_label": row["trigger_label"]}

    def record_download_item_event(
        self,
        event: str,
        download_id: str,
        *,
        message: str = "",
        details: Optional[dict[str, Any]] = None,
        count_failures: bool = False,
        **overrides: Any,
    ) -> Optional[int]:
        """Writes an ``item_events`` row for the item a download is for.

        Item ids, request id and the grab-time trigger come from the download (names are the fallback when the download
        carries no library ids). Never raises on a database failure. ``count_failures`` adds the running count of
        ``download_failed`` events for the item to ``details``.
        """
        try:
            with self._lock:
                row = self.conn.execute(
                    self._QUEUE_SELECT + self._QUEUE_FROM + " WHERE d.id = ?", (str(download_id),)
                ).fetchone()
            if row is None:
                return None
            track_id, album_id = row["track_id"], row["album_id"]
            names = {}
            if not track_id and not album_id:
                names = self.find_library_ids_by_name(
                    row["artist"],
                    row["album_title"] if row["item_type"] != "album" else (row["album_title"] or row["item_title"]),
                    row["item_title"] if row["item_type"] != "album" else None,
                )
                track_id, album_id = names.get("track_id"), names.get("album_id")
            merged = dict(details or {})
            if not (track_id or album_id or names.get("artist_id")) and not row["request_id"]:
                return None  # nothing to attach it to (an unrequested, unlibraried download)
            if count_failures:
                merged["failure_count"] = self.item_event_count(
                    "download_failed", track_id=track_id, album_id=None if track_id else album_id
                ) + 1
            kwargs: dict[str, Any] = {
                "track_id": track_id,
                "album_id": album_id,
                "artist_id": names.get("artist_id"),
                "request_id": row["request_id"],
                "download_id": str(download_id),
                "message": message,
                "details": merged,
                **self.get_download_trigger(download_id),
            }
            if not (track_id or album_id):
                kwargs["artist_name"] = row["artist"] or ""
                kwargs["album_title"] = row["album_title"] or ""
                kwargs["track_title"] = row["item_title"] if row["item_type"] != "album" else ""
            kwargs.update({k: v for k, v in overrides.items() if v is not None})
            return self.record_item_event(event, **kwargs)
        except sqlite3.Error:
            logger.exception("Could not record item event %r for download %s", event, download_id)
            return None

    def find_recycled_item_events(self, path_prefixes: Union[str, Iterable[str]]) -> list[dict[str, Any]]:
        """``file_replaced`` events whose recycled copy lives at or under any of ``path_prefixes`` (bin folders/files).

        One scan of the ``file_replaced`` rows however many prefixes there are: each row's recycled path is checked
        against the prefix set by walking its own ancestors. Each event is returned once, oldest first.
        """
        raw = [path_prefixes] if isinstance(path_prefixes, str) else list(path_prefixes)
        prefixes = {str(p).rstrip("/") for p in raw if str(p).rstrip("/")}
        if not prefixes:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT *, json_extract(details_json, '$.recycled_to') AS _recycled_to FROM item_events "
                "WHERE event = 'file_replaced' AND json_extract(details_json, '$.recycled_to') IS NOT NULL "
                "ORDER BY id"
            ).fetchall()
        matched: list[dict[str, Any]] = []
        for row in rows:
            recycled = str(row["_recycled_to"]).rstrip("/")
            candidate = recycled
            while candidate:
                if candidate in prefixes:
                    event_row = self._item_event_row(row)
                    event_row.pop("_recycled_to", None)
                    matched.append(event_row)
                    break
                candidate = candidate.rpartition("/")[0]
        return matched

    def item_event_count(self, event: str, *, track_id: Optional[str] = None, album_id: Optional[str] = None) -> int:
        """How many times ``event`` happened to a track (or album): used for running failure counts."""
        column, value = ("track_id", track_id) if track_id else ("album_id", album_id)
        if not value:
            return 0
        with self._lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM item_events WHERE event = ? AND {column} = ?", (event, str(value))
            ).fetchone()
        return int(row[0])

    def list_item_events(
        self,
        entity: Literal["artist", "album", "track"],
        entity_id: str,
        *,
        limit: int = 100,
        before_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Newest-first events for an entity (album scope includes its tracks, artist scope everything beneath)."""
        if entity not in ("artist", "album", "track"):
            raise ValueError(f"Unknown history entity: {entity!r}")
        sql = f"SELECT * FROM item_events WHERE {entity}_id = ?"
        params: list[Any] = [str(entity_id)]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item_event_row(r) for r in rows]

    def earliest_item_event(
        self, entity: Literal["artist", "album", "track"], entity_id: str
    ) -> Optional[dict[str, Any]]:
        """The origin of an entity: its earliest added_to_library / requested / grabbed event (whole scope)."""
        if entity not in ("artist", "album", "track"):
            raise ValueError(f"Unknown history entity: {entity!r}")
        with self._lock:
            row = self.conn.execute(
                f"SELECT * FROM item_events WHERE {entity}_id = ? "
                "AND event IN ('added_to_library', 'requested', 'grabbed') ORDER BY id ASC LIMIT 1",
                (str(entity_id),),
            ).fetchone()
        return self._item_event_row(row) if row is not None else None

    def has_item_events(self, entity: Literal["artist", "album", "track"], entity_id: str) -> bool:
        if entity not in ("artist", "album", "track"):
            raise ValueError(f"Unknown history entity: {entity!r}")
        with self._lock:
            return self.conn.execute(
                f"SELECT 1 FROM item_events WHERE {entity}_id = ? LIMIT 1", (str(entity_id),)
            ).fetchone() is not None

    @staticmethod
    def _item_event_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        raw = d.pop("details_json", None)
        try:
            details = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Unreadable details_json on item event %s", d.get("id"))
            details = {}
        d["details"] = details if isinstance(details, dict) else {}
        return d


# --- presentation (API) ----------------------------------------------------------------------------------------------
ANOTHER_USER = "another user"
_SYSTEM_ACTOR = "system"
# Non-admin viewers never see detail keys that normalise (lowercase, letters and digits only) to one of these, nor any
# key containing one of the fragments: indexer/client identity, release identifiers and titles, filesystem locations.
_HIDDEN_DETAIL_KEYS = frozenset(
    {
        "indexer", "indexername", "indexerid", "client", "clientname", "clientid", "protocol", "infohash", "hash",
        "releaseguid", "guid", "downloadurl", "magneturl", "url", "release", "releasetitle", "releasename",
        "path", "paths", "file", "files", "filepath", "filename", "oldpath", "newpath", "originalpath", "recycledto",
        "quarantinedto", "from", "to", "sourcepath", "targetpath", "folder", "directory", "relpath", "relativepath",
    }
)
_HIDDEN_KEY_FRAGMENTS = (
    "path", "indexer", "infohash", "guid", "url", "magnet", "release", "filename", "folder", "directory", "protocol",
    "client",
)
# Values of these keys are short quality labels ("FLAC 16/44") whose "/" is not a path separator.
_QUALITY_KEYS = frozenset({"quality", "codec", "fromquality", "toquality"})
_QUALITY_LABEL = re.compile(r"^[\w .,+()/-]{1,40}$")
# What a non-admin sees as the actor of a system trigger: a fixed per-kind name, never the stored label (which can be
# an indexer, a playlist or a username).
_GENERIC_ACTORS: dict[str, str] = {
    TRIGGER_WANTED: "Wanted search",
    TRIGGER_RSS: "RSS sync",
    TRIGGER_PLAYLIST: "Playlist sync",
    TRIGGER_MIX: "Mix sync",
    TRIGGER_IMPORT_LIST: "Import list sync",
    TRIGGER_UPGRADE: "Quality upgrade",
    TRIGGER_SCAN: "Library scan",
    TRIGGER_SEED_CLEANUP: "Seed cleanup",
    TRIGGER_RECYCLE_CLEANUP: "Recycle bin cleanup",
    TRIGGER_SYSTEM: _SYSTEM_ACTOR,
}
_PRIVATE_LABEL_TRIGGERS = frozenset({TRIGGER_PLAYLIST, TRIGGER_MIX, TRIGGER_IMPORT_LIST})


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _hidden_key(key: Any) -> bool:
    norm = _norm_key(key)
    return norm in _HIDDEN_DETAIL_KEYS or any(fragment in norm for fragment in _HIDDEN_KEY_FRAGMENTS)


def _looks_like_path(value: str) -> bool:
    """Conservative: any string with a path separator counts (a quality label is exempted by its caller)."""
    return "/" in value or "\\" in value


def _safe_quality(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _QUALITY_LABEL.match(value) and "\\" not in value and "//" not in value \
        and not value.startswith("/") else None


def redact_details(value: Any, _key: Any = None) -> Any:
    """Details for a non-admin viewer.

    Drops indexer/client/protocol/hash/guid/url keys, release titles and filesystem keys (matched case-insensitively
    and ignoring separators, so ``indexerName`` and ``relPath`` go too), and every string that contains a path
    separator, at any depth. Short quality labels under quality/codec keys are kept.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if _hidden_key(k):
                continue
            if isinstance(v, str):
                if _norm_key(k) in _QUALITY_KEYS and _safe_quality(v) is not None:
                    out[k] = v
                elif not _looks_like_path(v):
                    out[k] = v
                continue
            out[k] = redact_details(v, k)
        return out
    if isinstance(value, list):
        return [
            redact_details(v) for v in value
            if not (isinstance(v, str) and _looks_like_path(v) and not (_norm_key(_key) in _QUALITY_KEYS and _safe_quality(v)))
        ]
    return value


# Server-generated messages for non-admin viewers. The stored message is free text that embeds file names, release
# titles and error output, so it is never shown to them: it is rebuilt from the event and the few safe details.
_PLAIN_MESSAGES: dict[str, str] = {
    "requested": "Requested",
    "request_approved": "Request approved",
    "request_declined": "Request declined",
    "added_to_library": "Added to the library",
    "monitored": "Monitored",
    "unmonitored": "Unmonitored",
    "searched": "Searched for a release",
    "grabbed": "Release grabbed",
    "download_failed": "Download failed",
    "blocklisted": "Release blocklisted",
    "imported": "Imported",
    "upgraded": "Upgraded",
    "file_replaced": "File replaced",
    "file_deleted": "File deleted from disk",
    "file_missing": "File missing from disk",
    "quarantined": "File quarantined",
    "restored": "Restored",
    "issue_opened": "Issue opened",
    "issue_resolved": "Issue resolved",
    "health_finding": "Library health finding",
    "renamed": "File renamed",
    "moved": "File moved",
    "removed_from_library": "Removed from the library",
    "tags_changed": "Tags changed",
    "retagged": "Files retagged",
}


def public_message(event: str, details: dict[str, Any]) -> str:
    """Path-free, release-free message for ``event`` built from the (raw) ``details`` it was recorded with."""
    base = _PLAIN_MESSAGES.get(event, "Event")
    quality = _safe_quality(details.get("quality"))
    codec = _safe_quality(details.get("codec"))
    if event == "imported":
        codec = codec if codec and not (quality and codec.lower() in quality.lower()) else None
        parts = [p for p in (codec, quality) if p]
        return f"Imported ({' '.join(parts)})" if parts else base
    if event == "upgraded":
        before, after = _safe_quality(details.get("from_quality")), _safe_quality(details.get("to_quality"))
        return f"Upgraded ({before} -> {after})" if before and after else base
    if event == "download_failed" and isinstance(details.get("failure_count"), int):
        return f"{base} (attempt {details['failure_count']})"
    return base


def _iso(value: Any) -> Optional[str]:
    """SQLite ``YYYY-MM-DD HH:MM:SS`` (UTC) to ISO-8601 with a ``Z``; anything else is returned untouched."""
    text = str(value or "")
    if len(text) == 19 and text[10] == " ":
        return text.replace(" ", "T") + "Z"
    return text or None


class HistoryPresenter:
    """Turns ``item_events`` rows into the API shape for one viewer (full for admins, redacted for everyone else)."""

    def __init__(self, db: Any, viewer: dict[str, Any], is_admin: bool) -> None:
        self._db = db
        self._viewer_id = str(viewer.get("id") or "")
        self._viewer_name = str(viewer.get("username") or "")
        self._is_admin = is_admin
        self._names: dict[str, Optional[str]] = {}

    def _username(self, user_id: str) -> Optional[str]:
        if user_id not in self._names:
            try:
                user = self._db.get_user(user_id)
            except sqlite3.Error:
                logger.exception("Could not look up user %s for item history", user_id)
                user = None
            self._names[user_id] = (user or {}).get("username") if isinstance(user, dict) else None
        return self._names[user_id]

    def _is_own(self, event: dict[str, Any]) -> bool:
        actor_id = str(event.get("actor_user_id") or "")
        return bool(actor_id and self._viewer_id and actor_id == self._viewer_id)

    def _actor_display(self, event: dict[str, Any]) -> str:
        kind = str(event.get("trigger") or "")
        actor_id = str(event.get("actor_user_id") or "")
        label = event.get("trigger_label") or None
        if self._is_admin:
            if actor_id:
                return self._username(actor_id) or label or _SYSTEM_ACTOR
            return label or kind or _SYSTEM_ACTOR
        if actor_id:
            if self._is_own(event):
                return self._viewer_name or ANOTHER_USER
            return ANOTHER_USER
        if kind in USER_SCOPED_TRIGGERS and label:  # the label of these is a username
            return self._viewer_name if self._viewer_name and label == self._viewer_name else ANOTHER_USER
        return _GENERIC_ACTORS.get(kind, _SYSTEM_ACTOR)  # never the stored label: it may be an indexer or a list name

    def _trigger_label(self, event: dict[str, Any]) -> Optional[str]:
        label = event.get("trigger_label") or None
        if self._is_admin or not label:
            return label
        kind = str(event.get("trigger") or "")
        if kind in USER_SCOPED_TRIGGERS:
            return self._viewer_name if self._viewer_name and label == self._viewer_name else ANOTHER_USER
        if kind in _PRIVATE_LABEL_TRIGGERS:
            return label if self._is_own(event) else None  # another user's playlist / mix / list name
        return _GENERIC_ACTORS.get(kind)  # rss: the label is an indexer name; the rest are fixed names anyway

    def present(self, event: dict[str, Any]) -> dict[str, Any]:
        details = event.get("details") or {}
        message = event.get("message") or ""
        if not self._is_admin:
            message = public_message(str(event["event"]), details)
            details = redact_details(details)
        return {
            "id": event["id"],
            "event": event["event"],
            "created_at": _iso(event.get("created_at")),
            "track_id": event.get("track_id"),
            "album_id": event.get("album_id"),
            "artist_id": event.get("artist_id"),
            "track_title": event.get("track_title") or "",
            "album_title": event.get("album_title") or "",
            "artist_name": event.get("artist_name") or "",
            "trigger": event.get("trigger"),
            "trigger_label": self._trigger_label(event),
            "actor_display": self._actor_display(event),
            "message": message,
            "details": details,
        }

    def present_origin(self, event: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if event is None:
            return None
        return {
            "trigger": event.get("trigger"),
            "trigger_label": self._trigger_label(event),
            "actor_display": self._actor_display(event),
            "created_at": _iso(event.get("created_at")),
        }
