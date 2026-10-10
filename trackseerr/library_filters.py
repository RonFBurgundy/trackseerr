"""Library facet filters, SQL WHERE clause generation, and facet aggregation.

Filter objects represent facet criteria (genres, countries, years, band size, popularity, tags)
that can be applied to native library lists. Designed for reuse across paged lists, index scrubbers,
and smart collections without dependency on FastAPI.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional


def _clean_genre(g: Any) -> str:
    cleaned = str(g).strip().lower()
    if any(ch in cleaned for ch in (",", "%", "_")):
        raise ValueError(f"Invalid character in genre: {g!r}")
    return cleaned


@dataclass(frozen=True)
class LibraryFacetFilter:
    genres: tuple[str, ...] = ()
    exclude_genres: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    album_types: tuple[str, ...] = ()
    artist_types: tuple[str, ...] = ()
    members_min: Optional[int] = None
    members_max: Optional[int] = None
    formed_from: Optional[int] = None
    formed_to: Optional[int] = None
    popularity_min: Optional[int] = None
    popularity_max: Optional[int] = None
    tag_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "genres", tuple(_clean_genre(g) for g in self.genres))
        object.__setattr__(self, "exclude_genres", tuple(_clean_genre(g) for g in self.exclude_genres))
        object.__setattr__(
            self, "countries", tuple(str(c).strip().upper() for c in self.countries if str(c).strip())
        )
        object.__setattr__(
            self, "album_types", tuple(str(t).strip().lower() for t in self.album_types if str(t).strip())
        )
        object.__setattr__(
            self, "artist_types", tuple(str(t).strip().lower() for t in self.artist_types if str(t).strip())
        )
        object.__setattr__(self, "tag_ids", tuple(int(t) for t in self.tag_ids))

    def is_empty(self) -> bool:
        return not (
            self.genres
            or self.exclude_genres
            or self.countries
            or self.year_from is not None
            or self.year_to is not None
            or self.album_types
            or self.artist_types
            or self.members_min is not None
            or self.members_max is not None
            or self.formed_from is not None
            or self.formed_to is not None
            or self.popularity_min is not None
            or self.popularity_max is not None
            or self.tag_ids
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LibraryFacetFilter:
        def _tuple_str(val: Any) -> tuple[str, ...]:
            if not val:
                return ()
            return tuple(str(x) for x in val)

        def _tuple_int(val: Any) -> tuple[int, ...]:
            if not val:
                return ()
            return tuple(int(x) for x in val)

        def _opt_int(val: Any) -> Optional[int]:
            if val is None or val == "":
                return None
            return int(val)

        return cls(
            genres=_tuple_str(d.get("genres")),
            exclude_genres=_tuple_str(d.get("exclude_genres")),
            countries=_tuple_str(d.get("countries")),
            year_from=_opt_int(d.get("year_from")),
            year_to=_opt_int(d.get("year_to")),
            album_types=_tuple_str(d.get("album_types")),
            artist_types=_tuple_str(d.get("artist_types")),
            members_min=_opt_int(d.get("members_min")),
            members_max=_opt_int(d.get("members_max")),
            formed_from=_opt_int(d.get("formed_from")),
            formed_to=_opt_int(d.get("formed_to")),
            popularity_min=_opt_int(d.get("popularity_min")),
            popularity_max=_opt_int(d.get("popularity_max")),
            tag_ids=_tuple_int(d.get("tag_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "genres": list(self.genres),
            "exclude_genres": list(self.exclude_genres),
            "countries": list(self.countries),
            "year_from": self.year_from,
            "year_to": self.year_to,
            "album_types": list(self.album_types),
            "artist_types": list(self.artist_types),
            "members_min": self.members_min,
            "members_max": self.members_max,
            "formed_from": self.formed_from,
            "formed_to": self.formed_to,
            "popularity_min": self.popularity_min,
            "popularity_max": self.popularity_max,
            "tag_ids": list(self.tag_ids),
        }


def GENRE_MATCH_SQL(column: str) -> str:
    return f"(',' || lower(replace(COALESCE({column}, ''), ', ', ',')) || ',') LIKE ?"


def _genre_group(column_exprs: list[str], genres: tuple[str, ...]) -> tuple[str, list[str]]:
    if len(column_exprs) == 1:
        c = " OR ".join([GENRE_MATCH_SQL(column_exprs[0]) for _ in genres])
        params = [f"%,{g},%" for g in genres]
    else:
        inner = " OR ".join([GENRE_MATCH_SQL(col) for col in column_exprs])
        c = " OR ".join([f"({inner})" for _ in genres])
        params = []
        for g in genres:
            for _ in column_exprs:
                params.append(f"%,{g},%")
    return c, params


def _genre_clauses(f: LibraryFacetFilter, kind: str) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    col_exprs = ["a.genres"] if kind == "artists" else ["al.genres", "ar.genres"]

    if f.genres:
        c, p = _genre_group(col_exprs, f.genres)
        clauses.append(f"({c})")
        params.extend(p)

    if f.exclude_genres:
        c, p = _genre_group(col_exprs, f.exclude_genres)
        clauses.append(f"NOT ({c})")
        params.extend(p)

    return clauses, params


def _in_clause(column: str, values: tuple[Any, ...]) -> tuple[list[str], list[Any]]:
    if not values:
        return [], []
    placeholders = ", ".join("?" for _ in values)
    return [f"{column} IN ({placeholders})"], list(values)


def _year_clauses(f: LibraryFacetFilter, kind: str) -> tuple[list[str], list[Any]]:
    if f.year_from is None and f.year_to is None:
        return [], []

    if f.year_from is not None and f.year_to is not None:
        expr = "year BETWEEN ? AND ?"
        params: list[Any] = [f.year_from, f.year_to]
    elif f.year_from is not None:
        expr = "year >= ?"
        params = [f.year_from]
    else:
        expr = "year <= ?"
        params = [f.year_to]

    if kind in ("albums", "tracks"):
        clause = f"al.{expr}"
    else:
        clause = f"EXISTS (SELECT 1 FROM library_albums y WHERE y.artist_id = a.id AND y.{expr})"
    return [clause], params


def _album_type_clauses(f: LibraryFacetFilter, kind: str) -> tuple[list[str], list[Any]]:
    if not f.album_types:
        return [], []
    placeholders = ", ".join("?" for _ in f.album_types)
    if kind in ("albums", "tracks"):
        clause = f"al.album_type IN ({placeholders})"
    else:
        clause = f"EXISTS (SELECT 1 FROM library_albums y WHERE y.artist_id = a.id AND y.album_type IN ({placeholders}))"
    return [clause], list(f.album_types)


def _range_clauses(f: LibraryFacetFilter, artist_alias: str) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for col_name, val_min, val_max in (
        ("member_count", f.members_min, f.members_max),
        ("begin_year", f.formed_from, f.formed_to),
        ("popularity", f.popularity_min, f.popularity_max),
    ):
        col_expr = f"{artist_alias}.{col_name}"
        if val_min is not None and val_max is not None:
            clauses.append(f"{col_expr} IS NOT NULL AND {col_expr} BETWEEN ? AND ?")
            params.extend([val_min, val_max])
        elif val_min is not None:
            clauses.append(f"{col_expr} IS NOT NULL AND {col_expr} >= ?")
            params.append(val_min)
        elif val_max is not None:
            clauses.append(f"{col_expr} IS NOT NULL AND {col_expr} <= ?")
            params.append(val_max)
    return clauses, params


def _tag_clauses(f: LibraryFacetFilter, artist_alias: str) -> tuple[list[str], list[Any]]:
    if not f.tag_ids:
        return [], []
    placeholders = ", ".join("?" for _ in f.tag_ids)
    clause = f"EXISTS (SELECT 1 FROM artist_tags t2 WHERE t2.artist_id = {artist_alias}.id AND t2.tag_id IN ({placeholders}))"
    return [clause], list(f.tag_ids)


def facet_clauses(f: LibraryFacetFilter, kind: str) -> tuple[list[str], list[Any]]:
    if f.is_empty():
        return [], []
    if kind not in ("artists", "albums", "tracks"):
        raise ValueError(f"Unknown library kind: {kind!r}")

    artist_alias = "a" if kind == "artists" else "ar"
    clauses: list[str] = []
    params: list[Any] = []

    for helper_clauses, helper_params in (
        _genre_clauses(f, kind),
        _in_clause(f"{artist_alias}.country", f.countries),
        _year_clauses(f, kind),
        _album_type_clauses(f, kind),
        _in_clause(f"{artist_alias}.artist_type", f.artist_types),
        _range_clauses(f, artist_alias),
        _tag_clauses(f, artist_alias),
    ):
        clauses.extend(helper_clauses)
        params.extend(helper_params)

    return clauses, params



def library_facets(db: Any) -> dict[str, Any]:
    with db._lock:
        # genres: top 200 {value, count} by number of artists carrying the genre
        artist_genres: dict[str, set[str]] = {}
        for aid, gstr in db.conn.execute(
            "SELECT id, genres FROM library_artists WHERE genres IS NOT NULL AND genres != ''"
        ).fetchall():
            if gstr:
                for raw in str(gstr).split(","):
                    g = raw.strip().lower()
                    if g:
                        artist_genres.setdefault(str(aid), set()).add(g)

        for aid, gstr in db.conn.execute(
            "SELECT artist_id, genres FROM library_albums WHERE artist_id IS NOT NULL AND genres IS NOT NULL AND genres != ''"
        ).fetchall():
            if gstr:
                for raw in str(gstr).split(","):
                    g = raw.strip().lower()
                    if g:
                        artist_genres.setdefault(str(aid), set()).add(g)

        genre_counts: dict[str, int] = Counter()
        for genres in artist_genres.values():
            for g in genres:
                genre_counts[g] += 1

        sorted_genres = sorted(genre_counts.items(), key=lambda item: (-item[1], item[0]))[:200]
        genres_facet = [{"value": g, "count": c} for g, c in sorted_genres]

        # countries: {value, count} from artists
        country_rows = db.conn.execute(
            "SELECT UPPER(TRIM(country)), COUNT(*) FROM library_artists "
            "WHERE country IS NOT NULL AND TRIM(country) != '' "
            "GROUP BY UPPER(TRIM(country)) ORDER BY COUNT(*) DESC, UPPER(TRIM(country)) ASC"
        ).fetchall()
        countries_facet = [{"value": row[0], "count": int(row[1])} for row in country_rows]

        # decades: {value: 1970, count} from album years (count albums)
        decade_rows = db.conn.execute(
            "SELECT (year / 10) * 10 AS decade, COUNT(*) FROM library_albums "
            "WHERE year IS NOT NULL AND year > 0 "
            "GROUP BY decade ORDER BY decade ASC"
        ).fetchall()
        decades_facet = [{"value": int(row[0]), "count": int(row[1])} for row in decade_rows]

        # album_types and artist_types {value, count}
        album_type_rows = db.conn.execute(
            "SELECT LOWER(TRIM(album_type)), COUNT(*) FROM library_albums "
            "WHERE album_type IS NOT NULL AND TRIM(album_type) != '' "
            "GROUP BY LOWER(TRIM(album_type)) ORDER BY COUNT(*) DESC, LOWER(TRIM(album_type)) ASC"
        ).fetchall()
        album_types_facet = [{"value": row[0], "count": int(row[1])} for row in album_type_rows]

        artist_type_rows = db.conn.execute(
            "SELECT LOWER(TRIM(artist_type)), COUNT(*) FROM library_artists "
            "WHERE artist_type IS NOT NULL AND TRIM(artist_type) != '' "
            "GROUP BY LOWER(TRIM(artist_type)) ORDER BY COUNT(*) DESC, LOWER(TRIM(artist_type)) ASC"
        ).fetchall()
        artist_types_facet = [{"value": row[0], "count": int(row[1])} for row in artist_type_rows]

        # scalar min/max values
        row_albums = db.conn.execute(
            "SELECT MIN(year), MAX(year) FROM library_albums WHERE year IS NOT NULL AND year > 0"
        ).fetchone()
        year_min = int(row_albums[0]) if row_albums and row_albums[0] is not None else None
        year_max = int(row_albums[1]) if row_albums and row_albums[1] is not None else None

        row_artists = db.conn.execute(
            "SELECT MIN(CASE WHEN begin_year > 0 THEN begin_year END), "
            "MAX(CASE WHEN begin_year > 0 THEN begin_year END), "
            "MAX(member_count), MAX(popularity) FROM library_artists"
        ).fetchone()
        formed_min = int(row_artists[0]) if row_artists and row_artists[0] is not None else None
        formed_max = int(row_artists[1]) if row_artists and row_artists[1] is not None else None
        members_max = int(row_artists[2]) if row_artists and row_artists[2] is not None else None
        popularity_max = int(row_artists[3]) if row_artists and row_artists[3] is not None else None

        return {
            "genres": genres_facet,
            "countries": countries_facet,
            "decades": decades_facet,
            "album_types": album_types_facet,
            "artist_types": artist_types_facet,
            "year_min": year_min,
            "year_max": year_max,
            "formed_min": formed_min,
            "formed_max": formed_max,
            "members_max": members_max,
            "popularity_max": popularity_max,
        }
