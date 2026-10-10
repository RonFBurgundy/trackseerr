"""Smart collection domain models, rule evaluation, and track refresh.

Rule-based library playlists that sync like any other playlist (auto-update or one-time).
No FastAPI imports in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from trackseerr.library_filters import LibraryFacetFilter, facet_clauses
from trackseerr.models import Track
from trackseerr.playlist_policy import SMART_KIND, SMART_SERVICE, is_smart_collection

logger = logging.getLogger(__name__)

SORTS = ("random", "year_desc", "year_asc", "popularity_desc", "added_desc", "artist")
MAX_LIMIT = 1000

SORT_MAP = {
    "random": "RANDOM()",
    "year_desc": "al.year DESC, al.sort_title, t.disc_number, t.track_number",
    "year_asc": "al.year ASC, al.sort_title, t.disc_number, t.track_number",
    "popularity_desc": "COALESCE(ar.popularity, 0) DESC, ar.sort_name, al.sort_title, t.disc_number, t.track_number",
    "added_desc": "t.created_at DESC",
    "artist": "ar.sort_name, al.year, al.sort_title, t.disc_number, t.track_number",
}


class SmartCollectionError(ValueError):
    """Raised when smart collection rules are invalid or evaluation fails."""


@dataclass(frozen=True)
class SmartRules:
    filter: LibraryFacetFilter
    sort: str = "random"
    limit: int = 100

    @classmethod
    def from_json(cls, raw: str) -> SmartRules:
        try:
            d = json.loads(raw)
        except Exception as exc:
            raise SmartCollectionError(f"Invalid JSON for smart rules: {exc}") from exc

        if not isinstance(d, dict):
            raise SmartCollectionError("Smart rules JSON must be an object")

        sort = d.get("sort", "random")
        if sort not in SORTS:
            raise SmartCollectionError(f"Invalid sort '{sort}'; must be one of {SORTS}")

        limit_raw = d.get("limit", 100)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError) as exc:
            raise SmartCollectionError(f"Invalid limit: {limit_raw!r}") from exc

        if not (1 <= limit <= MAX_LIMIT):
            raise SmartCollectionError(f"Limit must be between 1 and {MAX_LIMIT}")

        filter_data = d.get("filter")
        if filter_data is None:
            facet_filter = LibraryFacetFilter()
        elif isinstance(filter_data, dict):
            try:
                facet_filter = LibraryFacetFilter.from_dict(filter_data)
            except Exception as exc:
                raise SmartCollectionError(f"Invalid filter in smart rules: {exc}") from exc
        else:
            raise SmartCollectionError("Filter in smart rules must be an object")

        return cls(filter=facet_filter, sort=sort, limit=limit)

    def to_json(self) -> str:
        return json.dumps(
            {
                "filter": self.filter.to_dict(),
                "sort": self.sort,
                "limit": self.limit,
            }
        )


def _build_where_clause(rules: SmartRules) -> tuple[str, list[Any]]:
    clauses, params = facet_clauses(rules.filter, "tracks")
    where_parts = ["EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id)"]
    if clauses:
        where_parts.extend(clauses)
    where_sql = " AND ".join(where_parts)
    return where_sql, params


def evaluate(db: Any, rules: SmartRules) -> list[dict[str, Any]]:
    """Evaluates smart rules against the library and returns track dictionaries."""
    sort_expr = SORT_MAP.get(rules.sort)
    if not sort_expr:
        raise SmartCollectionError(f"Invalid sort '{rules.sort}'")

    where_sql, params = _build_where_clause(rules)
    query = (
        "SELECT t.title, ar.name AS artist, al.title AS album, al.year "
        "FROM library_tracks t "
        "JOIN library_albums al ON al.id = t.album_id "
        "JOIN library_artists ar ON ar.id = t.artist_id "
        f"WHERE {where_sql} "
        f"ORDER BY {sort_expr} "
        "LIMIT ?"
    )
    query_params = list(params) + [int(rules.limit)]

    with db._lock:
        cur = db.conn.execute(query, query_params)
        rows = cur.fetchall()
        return [
            {
                "title": row[0] or "",
                "artist": row[1] or "",
                "album": row[2] or "",
                "year": row[3],
            }
            for row in rows
        ]


def evaluate_tracks(db: Any, rules: SmartRules) -> list[Track]:
    """Evaluates smart rules and returns a list of Track domain objects."""
    results = evaluate(db, rules)
    return [
        Track(title=r["title"], artist=r["artist"], album=r["album"])
        for r in results
    ]


def count(db: Any, rules: SmartRules) -> int:
    """Counts total matching tracks ignoring limit."""
    where_sql, params = _build_where_clause(rules)
    query = (
        "SELECT COUNT(*) "
        "FROM library_tracks t "
        "JOIN library_albums al ON al.id = t.album_id "
        "JOIN library_artists ar ON ar.id = t.artist_id "
        f"WHERE {where_sql}"
    )
    with db._lock:
        cur = db.conn.execute(query, list(params))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def refresh_tracks(db: Any, playlist: dict[str, Any]) -> list[Track]:
    """Refreshes tracks for a smart collection and updates stored snapshot."""
    raw_rules = playlist.get("source_ref") or ""
    rules = SmartRules.from_json(raw_rules)
    tracks = evaluate_tracks(db, rules)
    if not tracks:
        raise SmartCollectionError("The collection matched no tracks")

    playlist_id = playlist["id"]
    tracks_json = json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in tracks])
    db.set_playlist_tracks_json(playlist_id, tracks_json)
    return tracks
