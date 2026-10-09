"""Artist tags (Lidarr parity): the ``tags`` registry, ``artist_tags`` assignments and tag-scoped profile matching.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). Migration v64.

Storage model
-------------
* ``tags(id, label UNIQUE NOCASE, created_at)`` is the registry; ``artist_tags(artist_id, tag_id)`` assigns tags to
  library artists and cascades away with either side.
* Delay profiles, release profiles and import lists reference tags **by label**: their existing ``tags_json`` columns
  keep storing a JSON list of normalised label strings (so their API contracts are unchanged). Saving any of them
  registers the labels it uses in ``tags``; renaming a tag rewrites the label in every ``tags_json`` and deleting a tag
  removes it from them. Artists reference tags by **id** (``artist_tags``), and the artist API speaks ids.
* Labels are normalised (:func:`normalize_label`): trimmed, lower-cased, ``[a-z0-9-_ &]`` only, 1-40 characters.

Matching (Lidarr semantics)
---------------------------
* Delay profiles: the first non-default profile whose tags intersect the artist's tags, else the default profile.
* Release profiles: a profile with tags applies only to artists carrying at least one of them; an untagged profile
  applies to everyone. An artist of unknown identity carries no tags.
* Import lists: the list's tags are added to every artist the list adds to the library.

Migration v64 moves the makeshift ``library_artists.metadata_json["tags"]`` values into ``artist_tags`` (removing the
key) and registers every label found in delay/release profile ``tags_json`` (rewritten in normalised form).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

TAG_LABEL_MAX = 40
_VALID_LABEL = re.compile(r"^[a-z0-9_ &-]+$")
_INVALID_CHARS = re.compile(r"[^a-z0-9_ &-]+")
_CHUNK = 500
USAGE_ARTIST_LIMIT = 200

# (table, label column holder) pairs whose ``tags_json`` stores label lists.
_LABEL_TABLES: tuple[tuple[str, str], ...] = (
    ("delay_profiles", "delay_profiles"),
    ("release_profiles", "release_profiles"),
    ("import_lists", "import_lists"),
)


class InvalidTagLabel(ValueError):
    """The label is not 1-40 characters of ``[a-z0-9-_ &]`` after trimming and lower-casing."""


class DuplicateTag(ValueError):
    """A tag with this label already exists."""


class UnknownTag(ValueError):
    """A tag id does not exist."""


def normalize_label(raw: Any) -> str:
    """Canonical tag label; raises :class:`InvalidTagLabel` (a ``ValueError``) with a user-facing message."""
    if not isinstance(raw, str):
        raise InvalidTagLabel("Tag label must be text")
    label = raw.strip().lower()
    if not label:
        raise InvalidTagLabel("Tag label must not be empty")
    if len(label) > TAG_LABEL_MAX:
        raise InvalidTagLabel(f"Tag label must be at most {TAG_LABEL_MAX} characters")
    if not _VALID_LABEL.match(label):
        raise InvalidTagLabel("Tag label may only contain letters a-z, digits, spaces, ampersands, hyphens and underscores")
    return label


def normalize_labels(raw: Iterable[Any]) -> list[str]:
    """Normalises every label (raising on the first invalid one) and drops duplicates, keeping the first order."""
    out: list[str] = []
    for item in raw:
        label = normalize_label(item)
        if label not in out:
            out.append(label)
    return out


def _lenient_label(raw: Any) -> Optional[str]:
    """Best-effort normalisation for legacy data during migration: invalid characters become ``-``; None if empty."""
    if not isinstance(raw, str):
        return None
    label = _INVALID_CHARS.sub("-", raw.strip().lower()).strip()[:TAG_LABEL_MAX].strip()
    return label or None


def _loads_labels(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _chunks(items: list[Any], size: int = _CHUNK) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class TagMixin:
    # ------------------------------------------------------------------------------------------------------------
    # Migration v64
    # ------------------------------------------------------------------------------------------------------------

    def _migration_v64(self, cur: sqlite3.Cursor) -> None:  # noqa: C901
        """Tags registry, artist assignments and ``import_lists.tags_json``; folds legacy tag data in. Idempotent."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS artist_tags (
                artist_id TEXT NOT NULL REFERENCES library_artists(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (artist_id, tag_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_artist_tags_tag ON artist_tags(tag_id)")
        cols = {r[1] for r in cur.execute("PRAGMA table_info(import_lists)").fetchall()}
        if "tags_json" not in cols:
            cur.execute("ALTER TABLE import_lists ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")

        # Profile tag strings -> registry (and the profile columns rewritten with normalised labels).
        for table, _ in _LABEL_TABLES:
            for row in cur.execute(f"SELECT id, tags_json FROM {table}").fetchall():
                old = _loads_labels(row[1])
                clean: list[str] = []
                for raw in old:
                    label = _lenient_label(raw)
                    if label and label not in clean:
                        clean.append(label)
                    elif label:
                        logger.info("Migration v64: legacy tag %r in %s #%s collapsed into existing %r", raw, table, row[0], label)
                for label in clean:
                    cur.execute("INSERT OR IGNORE INTO tags (label) VALUES (?)", (label,))
                if clean != old:
                    cur.execute(f"UPDATE {table} SET tags_json = ? WHERE id = ?", (json.dumps(clean), row[0]))

        # The makeshift ``metadata_json["tags"]`` on artists -> artist_tags.
        rows = cur.execute(
            "SELECT id, metadata_json FROM library_artists WHERE metadata_json IS NOT NULL AND metadata_json LIKE '%\"tags\"%'"
        ).fetchall()
        for artist_id, raw_meta in rows:
            try:
                meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Artist %s has unreadable metadata_json; its tags were not migrated", artist_id)
                continue
            if not isinstance(meta, dict) or "tags" not in meta:
                continue
            tags = meta.pop("tags")
            if not isinstance(tags, list):
                logger.warning(
                    "Artist %s metadata_json['tags'] is %s, not a list; dropping it without migrating", artist_id, type(tags).__name__
                )
                tags = []
            seen: set[str] = set()
            for raw in tags:
                label = _lenient_label(raw)
                if not label:
                    continue
                if label in seen:
                    logger.info("Migration v64: legacy tag %r on artist %s collapsed into existing %r", raw, artist_id, label)
                seen.add(label)
                cur.execute("INSERT OR IGNORE INTO tags (label) VALUES (?)", (label,))
                tag_id = cur.execute("SELECT id FROM tags WHERE label = ?", (label,)).fetchone()[0]
                cur.execute("INSERT OR IGNORE INTO artist_tags (artist_id, tag_id) VALUES (?, ?)", (artist_id, tag_id))
            cur.execute("UPDATE library_artists SET metadata_json = ? WHERE id = ?", (json.dumps(meta), artist_id))

    # ------------------------------------------------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _tag_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": int(row["id"]), "label": row["label"], "created_at": row["created_at"]}

    def _label_users(self, table: str, label: str) -> list[dict[str, Any]]:
        """``[{id, name}]`` of the rows of ``table`` whose ``tags_json`` holds ``label`` (caller holds the lock)."""
        rows = self.conn.execute(f"SELECT id, name, tags_json FROM {table} ORDER BY id").fetchall()
        return [
            {"id": r["id"], "name": r["name"]}
            for r in rows
            if label in {str(t).strip().lower() for t in _loads_labels(r["tags_json"])}
        ]

    def list_tags(self) -> list[dict[str, Any]]:
        """Every tag (by label) with ``artist_count``, ``delay_profile_count``, ``release_profile_count`` and
        ``import_list_count``."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT t.id, t.label, t.created_at, (SELECT COUNT(*) FROM artist_tags a WHERE a.tag_id = t.id) AS n "
                "FROM tags t ORDER BY t.label COLLATE NOCASE"
            ).fetchall()
            usage: dict[str, dict[str, int]] = {}
            for table, key in (
                ("delay_profiles", "delay_profile_count"),
                ("release_profiles", "release_profile_count"),
                ("import_lists", "import_list_count"),
            ):
                for r in self.conn.execute(f"SELECT tags_json FROM {table}").fetchall():
                    for label in {str(t).strip().lower() for t in _loads_labels(r[0])}:
                        usage.setdefault(label, {}).setdefault(key, 0)
                        usage[label][key] += 1
        out: list[dict[str, Any]] = []
        for r in rows:
            u = usage.get(str(r["label"]).lower(), {})
            out.append(
                {
                    **self._tag_row(r),
                    "artist_count": int(r["n"]),
                    "delay_profile_count": u.get("delay_profile_count", 0),
                    "release_profile_count": u.get("release_profile_count", 0),
                    "import_list_count": u.get("import_list_count", 0),
                }
            )
        return out

    def get_tag(self, tag_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
        return self._tag_row(row) if row else None

    def get_tag_by_label(self, label: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tags WHERE label = ?", (normalize_label(label),)).fetchone()
        return self._tag_row(row) if row else None

    def create_tag(self, label: str) -> dict[str, Any]:
        """Creates a tag. Raises :class:`InvalidTagLabel` or :class:`DuplicateTag`."""
        clean = normalize_label(label)
        with self._lock:
            try:
                cur = self.conn.execute("INSERT INTO tags (label) VALUES (?)", (clean,))
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise DuplicateTag(f"Tag {clean!r} already exists") from exc
            row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (int(cur.lastrowid or 0),)).fetchone()
        return self._tag_row(row)

    def register_tag_labels(self, labels: Iterable[Any]) -> list[str]:
        """Normalises ``labels`` (raising :class:`InvalidTagLabel`), creates the missing tags and returns the
        de-duplicated labels. Used when a profile or import list is saved."""
        clean = normalize_labels(labels)
        if clean:
            with self._lock:
                self.conn.executemany("INSERT OR IGNORE INTO tags (label) VALUES (?)", [(c,) for c in clean])
                self.conn.commit()
        return clean

    def _register_tag_labels(self, clean: list[str]) -> None:
        """Creates the missing tags for already-normalised ``clean`` labels WITHOUT committing. The caller holds the
        lock and owns the transaction, so registration lands (or rolls back) together with the profile / list write."""
        if clean:
            self.conn.executemany("INSERT OR IGNORE INTO tags (label) VALUES (?)", [(c,) for c in clean])

    def get_or_create_tag_id(self, label: str) -> int:
        clean = normalize_label(label)
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO tags (label) VALUES (?)", (clean,))
            self.conn.commit()
            return int(self.conn.execute("SELECT id FROM tags WHERE label = ?", (clean,)).fetchone()[0])

    def _rewrite_label(self, old: str, new: Optional[str]) -> None:
        """Replaces (or, with ``new=None``, removes) ``old`` in every profile / import list ``tags_json``.
        Caller holds the lock and commits."""
        for table, _ in _LABEL_TABLES:
            for row in self.conn.execute(f"SELECT id, tags_json FROM {table}").fetchall():
                labels = _loads_labels(row["tags_json"])
                if old not in {t.strip().lower() for t in labels}:
                    continue
                out: list[str] = []
                for t in labels:
                    mapped = new if t.strip().lower() == old else t
                    if mapped is not None and mapped not in out:
                        out.append(mapped)
                self.conn.execute(f"UPDATE {table} SET tags_json = ? WHERE id = ?", (json.dumps(out), row["id"]))

    def rename_tag(self, tag_id: int, label: str) -> Optional[dict[str, Any]]:
        """Renames a tag everywhere (artists follow by id; profile / list labels are rewritten).

        None when the tag does not exist; :class:`InvalidTagLabel` / :class:`DuplicateTag` on a bad or taken label.
        """
        clean = normalize_label(label)
        with self._lock:
            row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
            if row is None:
                return None
            old = str(row["label"]).lower()
            try:
                self.conn.execute("UPDATE tags SET label = ? WHERE id = ?", (clean, int(tag_id)))
                self._rewrite_label(old, clean)
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise DuplicateTag(f"Tag {clean!r} already exists") from exc
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("rename_tag(%s) failed; rolled back", tag_id)
                raise
        return self.get_tag(tag_id)

    def delete_tag(self, tag_id: int) -> bool:
        """Deletes a tag: removed from every artist (cascade), delay/release profile and import list. False if absent."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
            if row is None:
                return False
            label = str(row["label"]).lower()
            affected = [
                str(r[0]) for r in self.conn.execute("SELECT artist_id FROM artist_tags WHERE tag_id = ?", (int(tag_id),))
            ]
            try:
                self._rewrite_label(label, None)
                self.conn.execute("DELETE FROM tags WHERE id = ?", (int(tag_id),))
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("delete_tag(%s) failed; rolled back", tag_id)
                raise
        self._emit_tag_events({a: ([], [label]) for a in affected})
        return True

    def get_tag_usage(self, tag_id: int) -> Optional[dict[str, Any]]:
        """Where a tag is used: artists (capped at ``USAGE_ARTIST_LIMIT``, with the full count), delay profiles,
        release profiles and import lists. None when the tag does not exist."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
            if row is None:
                return None
            label = str(row["label"]).lower()
            count = int(self.conn.execute("SELECT COUNT(*) FROM artist_tags WHERE tag_id = ?", (int(tag_id),)).fetchone()[0])
            artists = self.conn.execute(
                "SELECT a.id, a.name FROM artist_tags t JOIN library_artists a ON a.id = t.artist_id "
                "WHERE t.tag_id = ? ORDER BY a.name COLLATE NOCASE LIMIT ?",
                (int(tag_id), USAGE_ARTIST_LIMIT),
            ).fetchall()
            return {
                "tag": self._tag_row(row),
                "artist_count": count,
                "artists": [{"id": str(a["id"]), "name": a["name"]} for a in artists],
                "delay_profiles": self._label_users("delay_profiles", label),
                "release_profiles": self._label_users("release_profiles", label),
                "import_lists": self._label_users("import_lists", label),
            }

    # ------------------------------------------------------------------------------------------------------------
    # Artist assignments
    # ------------------------------------------------------------------------------------------------------------

    def get_artist_tag_ids(self, artist_id: str) -> list[int]:
        """Tag ids of an artist, ordered by label."""
        return self.get_artist_tags_map([str(artist_id)]).get(str(artist_id), [])

    def get_artist_tags_map(self, artist_ids: Iterable[str]) -> dict[str, list[int]]:
        """``{artist_id: [tag ids ordered by label]}`` for the artists that carry tags."""
        ids = list(dict.fromkeys(str(i) for i in artist_ids))
        out: dict[str, list[int]] = {}
        with self._lock:
            for chunk in _chunks(ids):
                marks = ", ".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT at.artist_id, at.tag_id FROM artist_tags at JOIN tags t ON t.id = at.tag_id "
                    f"WHERE at.artist_id IN ({marks}) ORDER BY t.label COLLATE NOCASE",
                    chunk,
                ).fetchall()
                for r in rows:
                    out.setdefault(str(r[0]), []).append(int(r[1]))
        return out

    def get_artist_tag_labels(self, artist_id: Optional[str] = None, artist_name: Optional[str] = None) -> list[str]:
        """Labels of an artist by id, falling back to the library artist matching ``artist_name`` (empty if unknown)."""
        with self._lock:
            if not artist_id and artist_name and artist_name.strip():
                found = self.get_library_artist_by_name(artist_name)  # type: ignore[attr-defined]
                artist_id = str(found["id"]) if found else None
            if not artist_id:
                return []
            rows = self.conn.execute(
                "SELECT t.label FROM artist_tags at JOIN tags t ON t.id = at.tag_id WHERE at.artist_id = ? "
                "ORDER BY t.label COLLATE NOCASE",
                (str(artist_id),),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def _require_tag_ids(self, tag_ids: Iterable[Any]) -> dict[int, str]:
        """``{id: label}`` for ``tag_ids``; raises :class:`UnknownTag` naming the missing ids. Caller holds the lock."""
        wanted = list(dict.fromkeys(int(i) for i in tag_ids))
        found: dict[int, str] = {}
        for chunk in _chunks(wanted):
            marks = ", ".join("?" for _ in chunk)
            for r in self.conn.execute(f"SELECT id, label FROM tags WHERE id IN ({marks})", chunk):
                found[int(r[0])] = str(r[1])
        missing = [i for i in wanted if i not in found]
        if missing:
            raise UnknownTag(f"Unknown tag id(s): {', '.join(str(i) for i in missing)}")
        return found

    def set_artist_tags(self, artist_id: str, tag_ids: Iterable[int]) -> Optional[list[int]]:
        """Replaces an artist's tags. Returns the resulting ids, None if the artist does not exist, and raises
        :class:`UnknownTag` for an id that is not a tag (nothing is changed)."""
        wanted = list(dict.fromkeys(int(i) for i in tag_ids))
        aid = str(artist_id)
        with self._lock:
            if self.conn.execute("SELECT 1 FROM library_artists WHERE id = ?", (aid,)).fetchone() is None:
                return None
            labels = self._require_tag_ids(wanted)
            before = {
                int(r[0]): str(r[1])
                for r in self.conn.execute(
                    "SELECT at.tag_id, t.label FROM artist_tags at JOIN tags t ON t.id = at.tag_id WHERE at.artist_id = ?",
                    (aid,),
                )
            }
            try:
                self.conn.execute("DELETE FROM artist_tags WHERE artist_id = ?", (aid,))
                self.conn.executemany(
                    "INSERT INTO artist_tags (artist_id, tag_id) VALUES (?, ?)", [(aid, t) for t in wanted]
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("set_artist_tags(%s) failed; rolled back", aid)
                raise
        added = sorted(labels[t] for t in wanted if t not in before)
        removed = sorted(lab for t, lab in before.items() if t not in labels)
        self._emit_tag_events({aid: (added, removed)})
        return self.get_artist_tag_ids(aid)

    def add_artist_tags_by_label(self, artist_id: str, labels: Iterable[Any]) -> list[str]:
        """Adds (creating missing tags) ``labels`` to one artist, e.g. an import list's tags on a newly added artist.
        Returns the labels that were newly assigned."""
        clean = normalize_labels(labels)
        if not clean:
            return []
        aid = str(artist_id)
        with self._lock:
            if self.conn.execute("SELECT 1 FROM library_artists WHERE id = ?", (aid,)).fetchone() is None:
                return []
            self.conn.executemany("INSERT OR IGNORE INTO tags (label) VALUES (?)", [(c,) for c in clean])
            added: list[str] = []
            for label in clean:
                tag_id = int(self.conn.execute("SELECT id FROM tags WHERE label = ?", (label,)).fetchone()[0])
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO artist_tags (artist_id, tag_id) VALUES (?, ?)", (aid, tag_id)
                )
                if cur.rowcount > 0:
                    added.append(label)
            self.conn.commit()
        if added:
            self._emit_tag_events({aid: (added, [])})
        return added

    def _apply_artist_tag_changes(
        self,
        artist_ids: Optional[list[str]],
        add: list[int],
        remove: list[int],
        labels: dict[int, str],
    ) -> tuple[int, int, dict[str, tuple[list[str], list[str]]]]:
        """Adds/removes tags for ``artist_ids`` (None = every artist) without committing.

        Returns ``(assignments_added, assignments_removed, {artist_id: (added labels, removed labels)})``. The caller
        holds the lock, owns the transaction and emits the events after committing.
        """
        where, params = "", []
        if artist_ids is not None:
            where = f" WHERE artist_id IN ({', '.join('?' for _ in artist_ids)})"
            params = list(artist_ids)
        touched = sorted(set(add) | set(remove))
        tag_marks = ", ".join("?" for _ in touched)
        select_pairs = (
            f"SELECT artist_id, tag_id FROM artist_tags{where}{' AND' if where else ' WHERE'} tag_id IN ({tag_marks})"
        )
        before = {(str(r[0]), int(r[1])) for r in self.conn.execute(select_pairs, [*params, *touched])}
        for tag_id in add:
            if artist_ids is None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO artist_tags (artist_id, tag_id) SELECT id, ? FROM library_artists", (tag_id,)
                )
            else:
                marks = ", ".join("?" for _ in artist_ids)
                self.conn.execute(
                    f"INSERT OR IGNORE INTO artist_tags (artist_id, tag_id) "
                    f"SELECT id, ? FROM library_artists WHERE id IN ({marks})",
                    [tag_id, *artist_ids],
                )
        if remove:
            rm_marks = ", ".join("?" for _ in remove)
            self.conn.execute(
                f"DELETE FROM artist_tags{where}{' AND' if where else ' WHERE'} tag_id IN ({rm_marks})",
                [*params, *remove],
            )
        after = {(str(r[0]), int(r[1])) for r in self.conn.execute(select_pairs, [*params, *touched])}
        per_artist: dict[str, tuple[list[str], list[str]]] = {}
        for artist, tag in sorted(after - before):
            per_artist.setdefault(artist, ([], []))[0].append(labels[tag])
        for artist, tag in sorted(before - after):
            per_artist.setdefault(artist, ([], []))[1].append(labels[tag])
        return len(after - before), len(before - after), per_artist

    def bulk_edit_artist_tags(
        self,
        artist_ids: Optional[list[str]],
        add: Iterable[int] = (),
        remove: Iterable[int] = (),
    ) -> dict[str, int]:
        """Adds and removes tags for many artists (None = all) in one transaction.

        Returns ``artists_updated`` (artists whose tag set changed), ``tags_added`` and ``tags_removed``
        (assignment counts). Raises :class:`UnknownTag` for a bad id and ``ValueError`` when a tag is in both lists.
        """
        add_ids, remove_ids = list(dict.fromkeys(int(i) for i in add)), list(dict.fromkeys(int(i) for i in remove))
        if set(add_ids) & set(remove_ids):
            raise ValueError("A tag cannot be both added and removed")
        if not add_ids and not remove_ids:
            raise ValueError("No tag changes requested")
        chunks: list[Optional[list[str]]] = (
            [None] if artist_ids is None else list(_chunks(list(dict.fromkeys(str(i) for i in artist_ids))))
        )
        result = {"artists_updated": 0, "tags_added": 0, "tags_removed": 0}
        events: dict[str, tuple[list[str], list[str]]] = {}
        with self._lock:
            labels = self._require_tag_ids([*add_ids, *remove_ids])
            try:
                for chunk in chunks:
                    added, removed, per_artist = self._apply_artist_tag_changes(chunk, add_ids, remove_ids, labels)
                    result["tags_added"] += added
                    result["tags_removed"] += removed
                    result["artists_updated"] += len(per_artist)
                    events.update(per_artist)
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("bulk_edit_artist_tags failed; transaction rolled back")
                raise
        self._emit_tag_events(events)
        return result

    # ------------------------------------------------------------------------------------------------------------
    # Item history
    # ------------------------------------------------------------------------------------------------------------

    def _emit_tag_events(self, changes: dict[str, tuple[list[str], list[str]]]) -> None:
        """One ``tags_changed`` item event per artist; a failure is logged by the helper and never breaks the edit."""
        events = []
        for artist_id, (added, removed) in changes.items():
            if not added and not removed:
                continue
            parts = [f"+{t}" for t in added] + [f"-{t}" for t in removed]
            events.append(
                {
                    "event": "tags_changed",
                    "artist_id": artist_id,
                    "message": "Tags changed: " + ", ".join(parts),
                    "details": {"added": added, "removed": removed},
                }
            )
        if events:
            self._safe_item_events_bulk(events)  # type: ignore[attr-defined]
