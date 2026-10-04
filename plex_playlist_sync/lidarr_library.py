"""The library as Lidarr sees it: live, cached briefly, sorted/filtered/paged/grouped in memory.

Lidarr v1 ``/artist`` and ``/album`` are not paged, so each kind is fetched whole, kept in-process for
``LIST_TTL_SECONDS`` (single-flight: concurrent requests share one Lidarr fetch) and dropped after any mutation we
make. Ordering and scrubber groups are computed by SQLite over an in-memory table built from the filtered rows, using
the very same ``list_index`` group builders as native mode, so group offsets are exact for every sort and direction.

Records come out in the shape native mode returns (ids are the Lidarr numeric ids as strings; image and cover URLs
point at the existing ``/api/library/...`` artwork routes, which proxy Lidarr's media cover).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlsplit

from plex_playlist_sync.clients.lidarr import _COVER_FILE_RE, LidarrClient
from plex_playlist_sync.list_index import SortDef, build_index, fold_search_text, library_sort_key, order_clause
from plex_playlist_sync.storage import clean_library_name

logger = logging.getLogger(__name__)

LIST_TTL_SECONDS = 60.0
MAX_COVER_BYTES = 3 * 1024 * 1024
COVER_DEADLINE_SECONDS = 10.0  # wall clock for one whole upstream cover fetch
COVER_MAX_CONCURRENT = 8  # simultaneous upstream cover fetches
COVER_SLOT_WAIT_SECONDS = 2.0  # how long a request waits for a free slot before the route answers 503
ORDER_CACHE_SIZE = 16  # sorted+filtered orders kept per snapshot
VALIDATOR_CACHE_SIZE = 2048

ARTIST_SORTS: dict[str, SortDef] = {
    "name": SortDef("s_name", "name"),
    "added_at": SortDef("s_added_at", "date"),
    "album_count": SortDef("s_album_count", "count", "albums"),
}
ALBUM_SORTS: dict[str, SortDef] = {
    "title": SortDef("s_title", "name"),
    "artist": SortDef("s_artist", "name"),
    "release_date": SortDef("s_release_date", "date"),
    "added_at": SortDef("s_added_at", "date"),
}
TRACK_SORTS: dict[str, SortDef] = {
    "title": SortDef("s_title", "name"),
    "artist": SortDef("s_artist", "name"),
    "album": SortDef("s_album", "name"),
    "added_at": SortDef("s_added_at", "date"),
    "size_bytes": SortDef("s_size_bytes", "size"),
}
SORTS: dict[str, dict[str, SortDef]] = {"artists": ARTIST_SORTS, "albums": ALBUM_SORTS, "tracks": TRACK_SORTS}


@dataclass(frozen=True)
class Row:
    """One list item: the output record plus everything needed to filter and order it."""

    id: str
    record: dict[str, Any]
    sort: dict[str, Any]
    clean_text: tuple[str, ...]
    raw_text: tuple[str, ...]
    monitored: bool
    artist_id: str = ""
    images: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (coverType, file name)
    folded_text: tuple[str, ...] = field(init=False, default=())  # accent- and case-folded search haystack

    def __post_init__(self) -> None:
        folded = tuple(fold_search_text(t) for t in (*self.clean_text, *self.raw_text))
        object.__setattr__(self, "folded_text", folded)


# ------------------------------------------------------------------------------------------------- mapping


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _images(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for image in raw.get("images") or []:
        if not isinstance(image, dict):
            continue
        kind = str(image.get("coverType") or "").lower()
        name = urlsplit(str(image.get("url") or "")).path.rsplit("/", 1)[-1]
        if kind and _COVER_FILE_RE.fullmatch(name):
            out.append((kind, name))
    return tuple(out)


def pick_image(images: tuple[tuple[str, str], ...], cover_types: tuple[str, ...]) -> Optional[str]:
    """The file name of the first image whose coverType is in ``cover_types`` (in that preference order)."""
    for wanted in cover_types:
        for kind, name in images:
            if kind == wanted:
                return name
    return None


def _monitor_option(raw: dict[str, Any]) -> Optional[str]:
    """Native-style monitor preset Lidarr actually reports for the artist, else None (unknown, never invented).

    Lidarr keeps ``monitorNewItems`` on the artist (and ``addOptions.monitor`` right after an add); only the values
    that mean the same as a native preset are mapped (``all`` and ``none``).
    """
    add_options = raw.get("addOptions") if isinstance(raw.get("addOptions"), dict) else {}
    for source in (raw.get("monitorNewItems"), add_options.get("monitor")):
        value = str(source or "").strip().lower()
        if value in ("all", "none"):
            return value
    return None


def artist_row(raw: dict[str, Any]) -> Row:
    aid = str(_int(raw.get("id")))
    name = str(raw.get("artistName") or "")
    stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
    added = _text(raw.get("added"))
    monitored = bool(raw.get("monitored", True))
    album_count = _int(stats.get("albumCount"))
    images = _images(raw)
    record = {
        "id": aid,
        "name": name,
        "clean_name": clean_library_name(name),
        "path": _text(raw.get("path")),
        "monitored": monitored,
        "monitor_option": _monitor_option(raw),
        "mbid": _text(raw.get("foreignArtistId")),
        "bio": _text(raw.get("overview")),
        "genres": list(raw.get("genres") or []),
        "added_at": added,
        "created_at": added,
        "album_count": album_count,
        "track_count": _int(stats.get("totalTrackCount", stats.get("trackCount"))),
        "track_file_count": _int(stats.get("trackFileCount")),
        "size_bytes": _int(stats.get("sizeOnDisk")),
        "status": _text(raw.get("status")),
        "image_url": f"/api/library/artists/{aid}/image" if pick_image(images, ("poster", "cover")) else None,
        "banner_url": f"/api/library/artists/{aid}/banner" if pick_image(images, ("banner",)) else None,
        "source": "lidarr",
    }
    return Row(
        id=aid,
        record=record,
        sort={"name": library_sort_key(name), "added_at": added, "album_count": album_count},
        clean_text=(record["clean_name"],),
        raw_text=(name,),
        monitored=monitored,
        images=images,
    )


def album_row(raw: dict[str, Any], artist_names: Optional[dict[str, str]] = None) -> Row:
    alid = str(_int(raw.get("id")))
    title = str(raw.get("title") or "")
    artist = raw.get("artist") if isinstance(raw.get("artist"), dict) else {}
    artist_id = str(_int(raw.get("artistId", artist.get("id"))))
    artist_name = str(artist.get("artistName") or (artist_names or {}).get(artist_id) or "")
    stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
    release = _text(raw.get("releaseDate"))
    added = _text(raw.get("added"))
    monitored = bool(raw.get("monitored", True))
    images = _images(raw)
    year = _int(release[:4], 0) if release and release[:4].isdigit() else None
    record = {
        "id": alid,
        "artist_id": artist_id,
        "artist_name": artist_name or "Unknown Artist",
        "title": title,
        "clean_title": clean_library_name(title),
        "release_date": release,
        "year": year,
        "album_type": _text(raw.get("albumType")),
        "mbid": _text(raw.get("foreignAlbumId")),
        "monitored": monitored,
        "added_at": added,
        "created_at": added,
        "total_tracks": _int(stats.get("totalTrackCount", stats.get("trackCount"))),
        "track_count": _int(stats.get("totalTrackCount", stats.get("trackCount"))),
        "track_file_count": _int(stats.get("trackFileCount")),
        "size_bytes": _int(stats.get("sizeOnDisk")),
        "cover_url": f"/api/library/albums/{alid}/cover" if pick_image(images, ("cover",)) else None,
        "source": "lidarr",
    }
    return Row(
        id=alid,
        record=record,
        sort={
            "title": library_sort_key(title),
            "artist": library_sort_key(artist_name),
            "release_date": release,
            "added_at": added,
        },
        clean_text=(record["clean_title"], clean_library_name(artist_name)),
        raw_text=(title, artist_name),
        monitored=monitored,
        artist_id=artist_id,
        images=images,
    )


def track_row(raw: dict[str, Any], artist_name: str, album_title: str) -> Row:
    tid = str(_int(raw.get("id")))
    title = str(raw.get("title") or "")
    track_file = raw.get("trackFile") if isinstance(raw.get("trackFile"), dict) else None
    size = _int(track_file.get("size")) if track_file else None
    quality = ""
    if track_file and isinstance(track_file.get("quality"), dict):
        inner = track_file["quality"].get("quality")
        quality = str(inner.get("name") or "") if isinstance(inner, dict) else ""
    duration = raw.get("duration")
    record = {
        "id": tid,
        "artist_id": str(_int(raw.get("artistId"))),
        "album_id": str(_int(raw.get("albumId"))),
        "title": title,
        "artist_name": artist_name or "Unknown Artist",
        "album_title": album_title or "Unknown Album",
        "track_number": _int(raw.get("trackNumber"), _int(raw.get("absoluteTrackNumber"), 1)) or 1,
        "disc_number": _int(raw.get("mediumNumber"), 1) or 1,
        "duration_seconds": float(duration) / 1000.0 if isinstance(duration, (int, float)) else None,
        "monitored": None,  # Lidarr tracks carry no monitored flag; null, not an invented True
        "has_file": bool(raw.get("hasFile")),
        "file": (
            {"id": str(_int(track_file.get("id"))), "file_path": _text(track_file.get("path")), "size_bytes": size,
             "quality": quality}
            if track_file
            else None
        ),
        "source": "lidarr",
    }
    return Row(
        id=tid,
        record=record,
        sort={
            "title": library_sort_key(title),
            "artist": library_sort_key(artist_name),
            "album": library_sort_key(album_title),
            "added_at": None,
            "size_bytes": size,
        },
        clean_text=(clean_library_name(title), clean_library_name(album_title), clean_library_name(artist_name)),
        raw_text=(title, album_title, artist_name),
        monitored=True,  # no per-track flag in Lidarr: never excluded by the "monitored only" filter
        artist_id=record["artist_id"],
    )


# ----------------------------------------------------------------------------------------- cache (single flight)


class Snapshot:
    """One fetched list plus everything derived from it: an id lookup and the cached filtered/sorted orders."""

    def __init__(self, kind: str, identity: tuple[str, str], fetched_at: float, rows: list[Row]) -> None:
        self.kind = kind
        self.identity = identity
        self.fetched_at = fetched_at
        self.rows = rows
        self.by_id: dict[str, Row] = {r.id: r for r in rows}
        self._orders: "OrderedDict[tuple[Any, ...], Ordered]" = OrderedDict()
        self._order_lock = threading.Lock()

    def ordered(
        self,
        sort_key: str,
        sort_dir: str,
        query: Optional[str],
        monitored_only: bool,
        artist_id: Optional[str],
    ) -> "Ordered":
        """The rows filtered and sorted, built once per distinct (sort, direction, filters) and kept in a small LRU."""
        key = (sort_key, sort_dir, (query or "").strip(), bool(monitored_only), str(artist_id or ""))
        with self._order_lock:
            hit = self._orders.get(key)
            if hit is not None:
                self._orders.move_to_end(key)
                return hit
            filtered = filter_rows(self.rows, query, monitored_only, artist_id)
            built = Ordered(self.kind, sort_key, sort_dir, ordered_rows(self.kind, filtered, sort_key, sort_dir))
            self._orders[key] = built
            while len(self._orders) > ORDER_CACHE_SIZE:
                self._orders.popitem(last=False)
            return built


class Ordered:
    """Filtered rows in final order; pages are slices and the scrubber index is computed once, lazily."""

    def __init__(self, kind: str, sort_key: str, sort_dir: str, rows: list[Row]) -> None:
        self.kind = kind
        self.sort_key = sort_key
        self.sort_dir = sort_dir
        self.rows = rows
        self._index: Optional[tuple[int, list[dict[str, Any]]]] = None
        self._lock = threading.Lock()

    def index(self) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            if self._index is None:
                self._index = index_rows(self.kind, self.rows, self.sort_key, self.sort_dir)
            return self._index


_lock = threading.Lock()  # guards _entries and _generation; never held while calling Lidarr
_fetch_locks: dict[str, threading.Lock] = {"artists": threading.Lock(), "albums": threading.Lock()}
_entries: dict[str, Snapshot] = {}
_generation = 0
_clock: Callable[[], float] = time.monotonic
_cover_validators: "OrderedDict[tuple[str, str, str, str], str]" = OrderedDict()
_cover_slots = threading.BoundedSemaphore(COVER_MAX_CONCURRENT)


def _identity(client: LidarrClient) -> tuple[str, str]:
    """Which Lidarr a cached list belongs to: its base URL and a hash of the API key (never the key itself)."""
    key = str(getattr(client, "api_key", "") or "")
    return str(client.base_url), hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def invalidate() -> None:
    """Drops both cached lists (and keeps any in-flight fetch from storing its now-stale result)."""
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()
        _cover_validators.clear()


def _peek(kind: str, identity: tuple[str, str]) -> Optional[Snapshot]:
    with _lock:
        entry = _entries.get(kind)
        if entry and entry.identity == identity and _clock() - entry.fetched_at < LIST_TTL_SECONDS:
            return entry
    return None


def snapshot_state(kind: str, client: LidarrClient) -> Snapshot:
    """The cached ``Snapshot`` of ``artists`` or ``albums`` (fetched when stale); one concurrent Lidarr fetch."""
    identity = _identity(client)
    state = _peek(kind, identity)
    if state is not None:
        return state
    with _fetch_locks[kind]:
        state = _peek(kind, identity)
        if state is not None:
            return state
        with _lock:
            generation = _generation
        if kind == "artists":
            rows = [artist_row(raw) for raw in client.fetch_artists()]
        else:
            raw_albums = client.fetch_albums()
            names: dict[str, str] = {}
            if any(not isinstance(a.get("artist"), dict) for a in raw_albums):
                names = {r.id: str(r.record["name"]) for r in snapshot("artists", client)}
            rows = [album_row(raw, names) for raw in raw_albums]
        state = Snapshot(kind, identity, _clock(), rows)
        with _lock:
            if generation == _generation:
                _entries[kind] = state
        return state


def snapshot(kind: str, client: LidarrClient) -> list[Row]:
    """The full ``artists`` or ``albums`` list, from cache when fresh; exactly one concurrent Lidarr fetch."""
    return snapshot_state(kind, client).rows


class CoverBusy(Exception):
    """Every upstream cover slot stayed taken for ``COVER_SLOT_WAIT_SECONDS``."""


@contextmanager
def cover_slot() -> Iterator[None]:
    """Bounds concurrent upstream cover fetches; waits briefly, then raises CoverBusy instead of piling up threads."""
    if not _cover_slots.acquire(timeout=COVER_SLOT_WAIT_SECONDS):
        raise CoverBusy("Too many artwork requests in flight")
    try:
        yield
    finally:
        _cover_slots.release()


def cover_etag(identity: tuple[str, str], kind: str, entity_id: int, filename: str, upstream: str) -> str:
    """Weak ETag over the Lidarr entity, file name and upstream ETag/Last-Modified (when it sent one)."""
    digest = hashlib.sha256(f"{kind}|{int(entity_id)}|{filename}|{upstream}".encode("utf-8")).hexdigest()[:24]
    return f'W/"{digest}"'


def known_cover_etag(identity: tuple[str, str], kind: str, entity_id: int, filename: str) -> Optional[str]:
    """The ETag last served for this cover, so a matching If-None-Match can be answered without a Lidarr fetch."""
    with _lock:
        return _cover_validators.get((identity[0] + identity[1], kind, str(entity_id), filename))


def remember_cover_etag(identity: tuple[str, str], kind: str, entity_id: int, filename: str, etag: str) -> None:
    with _lock:
        _cover_validators[(identity[0] + identity[1], kind, str(entity_id), filename)] = etag
        while len(_cover_validators) > VALIDATOR_CACHE_SIZE:
            _cover_validators.popitem(last=False)


def etag_matches(header: Optional[str], etag: str) -> bool:
    """True when an ``If-None-Match`` header names ``etag`` (weak comparison) or ``*``."""
    if not header:
        return False
    bare = etag[2:] if etag.startswith("W/") else etag
    for token in header.split(","):
        token = token.strip()
        if token == "*" or (token[2:] if token.startswith("W/") else token) == bare:
            return True
    return False


# --------------------------------------------------------------------------------------- filter, order, page, group


def _matches(row: Row, query: str) -> bool:
    """Accent- and case-insensitive substring match, same rules as native mode (see library_paging._where)."""
    text = query.strip()
    if not text:
        return True
    needles = [fold_search_text(text)]
    if not any(ch in text for ch in "%_\\"):
        clean = clean_library_name(text)
        if clean:
            needles.append(fold_search_text(clean))
    return any(n in hay for n in needles for hay in row.folded_text)


def filter_rows(
    rows: list[Row], query: Optional[str], monitored_only: bool, artist_id: Optional[str] = None
) -> list[Row]:
    out = rows
    if monitored_only:
        out = [r for r in out if r.monitored]
    if artist_id:
        out = [r for r in out if r.artist_id == str(artist_id)]
    if query and query.strip():
        out = [r for r in out if _matches(r, query)]
    return out


def _table(kind: str, rows: list[Row]) -> sqlite3.Connection:
    sorts = SORTS[kind]
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    columns = ", ".join(sort.expr for sort in sorts.values())
    conn.execute(f"CREATE TABLE t (id TEXT, pos INTEGER, {columns})")
    marks = ", ".join("?" for _ in range(2 + len(sorts)))
    conn.executemany(
        f"INSERT INTO t VALUES ({marks})",
        [(r.id, i, *(r.sort.get(key) for key in sorts)) for i, r in enumerate(rows)],
    )
    return conn


def ordered_rows(kind: str, rows: list[Row], sort_key: str, sort_dir: str) -> list[Row]:
    """``rows`` ordered by ``sort_key``/``sort_dir`` (id breaks ties, as in native mode) via one SQLite sort."""
    sort = SORTS[kind][sort_key]  # KeyError for a key outside the whitelist; routes validate first
    conn = _table(kind, rows)
    try:
        positions = conn.execute(f"SELECT pos FROM t {order_clause(sort, sort_dir, 'id')}").fetchall()
    finally:
        conn.close()
    return [rows[p[0]] for p in positions]


def page_rows(
    kind: str, rows: list[Row], page: int, page_size: int, sort_key: str, sort_dir: str
) -> list[dict[str, Any]]:
    """One page of ``rows`` ordered by ``sort_key``/``sort_dir`` (id breaks ties, as in native mode)."""
    return page_of(ordered_rows(kind, rows, sort_key, sort_dir), page, page_size)


def page_of(ordered: list[Row], page: int, page_size: int) -> list[dict[str, Any]]:
    start = max(int(page) - 1, 0) * int(page_size)
    return [dict(r.record) for r in ordered[start : start + int(page_size)]]


def index_rows(kind: str, rows: list[Row], sort_key: str, sort_dir: str) -> tuple[int, list[dict[str, Any]]]:
    sort = SORTS[kind][sort_key]
    conn = _table(kind, rows)
    try:
        return build_index(conn, "FROM t", [], sort, sort_dir)
    finally:
        conn.close()


def list_page(
    kind: str,
    client: LidarrClient,
    page: int,
    page_size: int,
    sort_key: str,
    sort_dir: str,
    query: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    ordered = snapshot_state(kind, client).ordered(sort_key, sort_dir, query, monitored_only, artist_id)
    return page_of(ordered.rows, page, page_size), len(ordered.rows)


def list_index(
    kind: str,
    client: LidarrClient,
    sort_key: str,
    sort_dir: str,
    query: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str] = None,
) -> tuple[int, list[dict[str, Any]]]:
    total, groups = snapshot_state(kind, client).ordered(sort_key, sort_dir, query, monitored_only, artist_id).index()
    return total, [dict(g) for g in groups]


def album_track_rows(client: LidarrClient, album_id: int) -> list[Row]:
    """Live tracks of one album (the album fetch also validates the id and names the album and artist)."""
    album = client.fetch_album(album_id)
    artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
    artist_name = str(artist.get("artistName") or "")
    title = str(album.get("title") or "")
    return [track_row(raw, artist_name, title) for raw in client.fetch_album_tracks(album_id)]


# -------------------------------------------------------------------------------------------------- details


def artist_detail(client: LidarrClient, artist_id: int) -> dict[str, Any]:
    row = artist_row(client.fetch_artist(artist_id))
    result = dict(row.record)
    names = {row.id: str(row.record["name"])}
    result["albums"] = [dict(album_row(a, names).record) for a in client.fetch_artist_albums(artist_id)]
    return result


def album_detail(client: LidarrClient, album_id: int) -> dict[str, Any]:
    raw = client.fetch_album(album_id)
    result = dict(album_row(raw).record)
    result["tracks"] = [dict(r.record) for r in album_track_rows(client, album_id)]
    return result


# ------------------------------------------------------------------------------------------------ artwork


def cover_file(kind: str, client: LidarrClient, entity_id: int, cover_types: tuple[str, ...]) -> Optional[str]:
    """The Lidarr media cover file name for ``kind`` (``artists``/``albums``) id, or None when it has none.

    Looks at the cached list first; an id the cache has not seen (new since the last fetch) falls back to one live
    record fetch. Raises LidarrNotFound when Lidarr itself has no such id.
    """
    row = snapshot_state(kind, client).by_id.get(str(entity_id))
    if row is not None:
        return pick_image(row.images, cover_types)
    raw = client.fetch_artist(entity_id) if kind == "artists" else client.fetch_album(entity_id)
    return pick_image(_images(raw), cover_types)


# ------------------------------------------------------------------------------------------------ mutations


def set_artist_monitored(client: LidarrClient, artist_id: int, monitored: bool) -> dict[str, Any]:
    try:
        raw = client.set_artist_monitored(artist_id, monitored)
    finally:
        invalidate()
    return dict(artist_row(raw).record)


def set_album_monitored(client: LidarrClient, album_id: int, monitored: bool) -> dict[str, Any]:
    try:
        client.set_albums_monitored([album_id], monitored)
    finally:
        invalidate()
    return dict(album_row(client.fetch_album(album_id)).record)


_PRESET_ALBUM_TYPES: dict[str, frozenset[str]] = {
    "albums": frozenset({"album", "studio"}),
    "singles_eps": frozenset({"single", "ep", "singles", "eps"}),
}


def apply_monitor_preset(client: LidarrClient, artist_id: int, option: str) -> dict[str, Any]:
    """Applies a native-style monitor preset to a Lidarr artist.

    Same subsets as native mode: ``all`` monitors every album, ``albums`` only studio albums (Lidarr ``albumType``
    "Album"), ``singles_eps`` only Single/EP, ``none`` unmonitors everything and the artist. Any other option than
    ``none`` also monitors the artist. Album flags go through ``PUT /album/monitor`` (one call per direction).
    """
    if option not in ("all", "albums", "singles_eps", "none"):
        raise ValueError(f"Unknown monitor preset: {option}")
    try:
        albums = client.fetch_artist_albums(artist_id)
        wanted_types = _PRESET_ALBUM_TYPES.get(option)
        monitor_ids: list[int] = []
        unmonitor_ids: list[int] = []
        for raw in albums:
            album_id = _int(raw.get("id"))
            if not album_id:
                continue
            kind = str(raw.get("albumType") or ("album" if option == "albums" else "")).lower()
            keep = option == "all" or (wanted_types is not None and kind in wanted_types)
            (monitor_ids if keep else unmonitor_ids).append(album_id)
        artist = client.set_artist_monitored(artist_id, option != "none")
        if monitor_ids:
            client.set_albums_monitored(monitor_ids, True)
        if unmonitor_ids:
            client.set_albums_monitored(unmonitor_ids, False)
    finally:
        invalidate()
    record = dict(artist_row(artist).record)
    record["monitor_option"] = option
    return record


def search_artist(client: LidarrClient, artist_id: int) -> dict[str, Any]:
    return client.run_command("ArtistSearch", artistId=int(artist_id))


def search_album(client: LidarrClient, album_id: int) -> dict[str, Any]:
    return client.run_command("AlbumSearch", albumIds=[int(album_id)])


def refresh_artist(client: LidarrClient, artist_id: int) -> dict[str, Any]:
    try:
        return client.run_command("RefreshArtist", artistId=int(artist_id))
    finally:
        invalidate()
