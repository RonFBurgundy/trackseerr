"""Import of an iTunes / Apple Music library export (``iTunes Library.xml`` / ``Library.xml``, Apple plist XML).

Pipeline: :func:`parse_library` turns the upload into a compact structure, :func:`save_import` parks it under the
data dir for 24 h, :func:`suggest_mappings` proposes export-path -> library-path rewrites, and :func:`run_import`
(called from a background thread) matches every track of the chosen playlists to the native library and
creates / updates Trackseerr playlists. Nothing here runs on a request thread except the parse itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import plistlib
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional
from urllib.parse import unquote, urlsplit
from xml.parsers.expat import ExpatError

from trackseerr.job_tracker import track_job
from trackseerr.library import primary_artist, resolve_album_artist
from trackseerr.list_monitoring import apply_playlist_missing_safely
from trackseerr.redaction import safe_exc
from trackseerr.security import sanitize_text
from trackseerr.storage import Database, clean_library_name

logger = logging.getLogger(__name__)

ENV_MAX_MB = "ITUNES_IMPORT_MAX_MB"
DEFAULT_MAX_MB = 100
IMPORT_TTL_SECONDS = 24 * 3600
DURATION_TOLERANCE_S = 3.0
MONITOR_MODES = ("track", "album", "artist", "none")
SERVICE = "itunes"
PLAYLIST_ID_PREFIX = "itn_"
_IMPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DRIVE_PATH_RE = re.compile(r"^/[A-Za-z]:[/\\]")
_HEAD_BYTES = 65536


class ItunesImportError(ValueError):
    """A user-facing problem with the uploaded export (the message is safe to show)."""


def max_import_bytes() -> int:
    """Upload size cap in bytes (``ITUNES_IMPORT_MAX_MB``, default 100)."""
    raw = os.getenv(ENV_MAX_MB, "").strip()
    try:
        mb = float(raw) if raw else float(DEFAULT_MAX_MB)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d MB", ENV_MAX_MB, raw, DEFAULT_MAX_MB)
        mb = float(DEFAULT_MAX_MB)
    return int(max(mb, 1.0) * 1024 * 1024)


# --------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------

def _check_header(head: bytes) -> None:
    """Rejects non-XML input and any DOCTYPE carrying an internal subset or entity declaration."""
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"bplist") or not stripped.startswith(b"<"):
        raise ItunesImportError(
            "This is not an XML library export. Binary iTunes files (.itl, binary plist) cannot be read: "
            "in iTunes or Music choose File > Library > Export Library and upload the XML file it writes."
        )
    # Friendly early error only. The real guard is plistlib/expat itself, which rejects entity declarations
    # (and so billion-laughs / external entities) during parsing regardless of what this header scan sees.
    lowered = head.lower()
    if b"<!entity" in lowered:
        raise ItunesImportError("Rejected: the file declares XML entities, which are not allowed in a library export.")
    start = lowered.find(b"<!doctype")
    if start != -1:
        end = lowered.find(b">", start)
        subset = lowered.find(b"[", start)
        if subset != -1 and (end == -1 or subset < end):
            raise ItunesImportError("Rejected: the file has a DOCTYPE with an internal subset.")


def _parse_stream(stream: BinaryIO) -> dict[str, Any]:
    head = stream.read(_HEAD_BYTES)
    _check_header(head)
    stream.seek(0)
    try:
        root = plistlib.load(stream, fmt=plistlib.FMT_XML)
    except (plistlib.InvalidFileException, ExpatError, ValueError, OverflowError, RecursionError) as exc:
        logger.warning("iTunes export parse failed: %s", safe_exc(exc))
        raise ItunesImportError("The file is not a valid iTunes library XML export.") from exc
    if not isinstance(root, dict) or ("Tracks" not in root and "Playlists" not in root):
        raise ItunesImportError("The file is a plist but not an iTunes library export (no Tracks or Playlists).")
    return _build(root)


def parse_library(source: bytes | BinaryIO | str | Path) -> dict[str, Any]:
    """Parses an XML iTunes library export. Raises :class:`ItunesImportError` on anything unusable."""
    if isinstance(source, (bytes, bytearray)):
        return _parse_stream(io.BytesIO(bytes(source)))
    if isinstance(source, (str, Path)):
        with open(source, "rb") as fh:
            return _parse_stream(fh)
    return _parse_stream(source)


def _stars(rating: Any) -> int:
    try:
        value = int(rating)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, round(value / 20)))


def _entry(track_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    added = raw.get("Date Added")
    return {
        "track_id": track_id,
        "name": sanitize_text(str(raw.get("Name") or "")),
        "artist": sanitize_text(str(raw.get("Artist") or "")),
        "album_artist": sanitize_text(str(raw.get("Album Artist") or "")),
        "album": sanitize_text(str(raw.get("Album") or "")),
        "track_number": int(raw.get("Track Number") or 0),
        "disc_number": int(raw.get("Disc Number") or 0),
        "total_time_ms": int(raw.get("Total Time") or 0),
        "location": str(raw.get("Location") or ""),
        "play_count": int(raw.get("Play Count") or 0),
        "rating": _stars(raw.get("Rating")),
        "date_added": added.isoformat() if isinstance(added, datetime) else None,
        "persistent_id": str(raw.get("Persistent ID") or ""),
    }


def _build(root: dict[str, Any]) -> dict[str, Any]:
    tracks: dict[str, dict[str, Any]] = {}
    for tid, raw in (root.get("Tracks") or {}).items():
        if isinstance(raw, dict):
            try:
                tracks[str(tid)] = _entry(str(tid), raw)
            except (TypeError, ValueError, OverflowError):
                logger.debug("Skipping unreadable iTunes track %s", tid, exc_info=True)

    raw_playlists = [p for p in (root.get("Playlists") or []) if isinstance(p, dict)]
    folders = {
        str(p.get("Playlist Persistent ID")): (str(p.get("Name") or ""), str(p.get("Parent Persistent ID") or ""))
        for p in raw_playlists
        if p.get("Folder") and p.get("Playlist Persistent ID")
    }

    def folder_path(parent_id: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        while parent_id and parent_id in folders and parent_id not in seen:
            seen.add(parent_id)
            name, parent_id = folders[parent_id]
            names.append(sanitize_text(name))
        return list(reversed(names))

    skipped = {"builtin": 0, "folders": 0, "empty": 0}
    playlists: list[dict[str, Any]] = []
    for p in raw_playlists:
        if p.get("Folder"):
            skipped["folders"] += 1
            continue
        if p.get("Master") or p.get("Distinguished Kind") is not None:
            skipped["builtin"] += 1
            continue
        items = [
            str(i["Track ID"])
            for i in (p.get("Playlist Items") or [])
            if isinstance(i, dict) and "Track ID" in i and str(i["Track ID"]) in tracks
        ]
        if not items:
            skipped["empty"] += 1
            continue
        key = str(p.get("Playlist Persistent ID") or p.get("Playlist ID") or "")
        if not key:
            continue
        playlists.append(
            {
                "key": key,
                "name": sanitize_text(str(p.get("Name") or "Untitled")),
                "is_smart": "Smart Info" in p,
                "folder_path": folder_path(str(p.get("Parent Persistent ID") or "")),
                "items": items,
            }
        )
    return {"version": 1, "tracks": tracks, "playlists": playlists, "skipped": skipped}


# --------------------------------------------------------------------------------------------------
# Server-side storage of a parsed export
# --------------------------------------------------------------------------------------------------

def import_dir(data_dir: str) -> Path:
    return Path(data_dir) / "itunes_imports"


def valid_import_id(import_id: str) -> bool:
    return bool(_IMPORT_ID_RE.match(str(import_id)))


def purge_expired(data_dir: str, now: Optional[float] = None) -> int:
    """Deletes stored exports older than 24 h; returns how many were removed."""
    directory = import_dir(data_dir)
    if not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - IMPORT_TTL_SECONDS
    removed = 0
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
                with _JOBS_LOCK:
                    _JOBS.pop(path.stem, None)
        except OSError as exc:
            logger.warning("Could not purge expired iTunes import %s: %s", path.name, exc)
    return removed


def save_import(data_dir: str, parsed: dict[str, Any]) -> str:
    purge_expired(data_dir)
    directory = import_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    import_id = uuid.uuid4().hex
    tmp = directory / f"{import_id}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(parsed, fh, ensure_ascii=False)
    os.replace(tmp, directory / f"{import_id}.json")
    return import_id


def load_import(data_dir: str, import_id: str) -> Optional[dict[str, Any]]:
    """The stored export, or None when the id is unknown, malformed or expired."""
    if not valid_import_id(import_id):
        return None
    purge_expired(data_dir)
    path = import_dir(data_dir) / f"{import_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Stored iTunes import %s is unreadable: %s", import_id, safe_exc(exc))
        return None


# --------------------------------------------------------------------------------------------------
# Locations and path mapping
# --------------------------------------------------------------------------------------------------

def location_to_path(location: str) -> Optional[str]:
    """``file://localhost/C:/Users/a/x.mp3`` -> ``C:/Users/a/x.mp3``; ``file://localhost/Volumes/m/x`` -> ``/Volumes/m/x``.

    Returns None for an empty or non-file URL (cloud / streamed tracks have no Location).
    """
    if not location:
        return None
    parts = urlsplit(location)
    if parts.scheme.lower() != "file":
        return None
    path = unquote(parts.path)
    if _DRIVE_PATH_RE.match(path):
        path = path[1:]
    elif parts.netloc and parts.netloc.lower() != "localhost":
        path = f"//{parts.netloc}{path}"
    return path.replace("\\", "/")


def _pkey(path: str) -> str:
    return unicodedata.normalize("NFC", path.replace("\\", "/")).casefold()


def _norm_prefix(prefix: str) -> str:
    text = prefix.strip().replace("\\", "/")
    return text.rstrip("/") if len(text) > 1 else text


def _split_key(path: str) -> list[str]:
    """Path split on ``/`` with each component NFC+casefolded; a leading ``/`` yields an empty first element."""
    parts = path.split("/")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return [unicodedata.normalize("NFC", c).casefold() for c in parts]


def apply_mappings(path: str, mappings: list[dict[str, str]]) -> Optional[str]:
    """Rewrites ``path`` with the first mapping whose ``from`` is a leading directory prefix; None if none apply.

    Matching is per path component in NFC+casefolded space, and the unmatched remainder is taken from the original
    components, so normalisation or case-folding that changes string length cannot shift the split point.
    """
    norm = path.replace("\\", "/")
    orig_parts = norm.split("/")
    parts = _split_key(norm)
    for mapping in mappings:
        src = _norm_prefix(str(mapping.get("from") or ""))
        dst = _norm_prefix(str(mapping.get("to") or ""))
        if dst == "/":
            dst = ""
        if not src or not (dst or str(mapping.get("to") or "").strip() == "/"):
            continue
        src_parts = _split_key(src)
        n = len(src_parts)
        if len(parts) >= n and parts[:n] == src_parts:
            rest = orig_parts[n:]
            return dst + "".join("/" + c for c in rest)
    return None


def _components(path: str) -> list[str]:
    return [c for c in path.replace("\\", "/").split("/") if c]


def suggest_mappings(
    export_paths: list[str],
    library_paths: list[str],
    sample_size: int = 2000,
    max_suggestions: int = 3,
) -> list[dict[str, Any]]:
    """Proposes ``{from, to, sample_matches}`` by longest common path suffix between export and library paths.

    A sample of library files is walked; for each, the export path sharing the most trailing path components (at
    least 2: folder + file) is found, and what precedes that suffix on each side becomes a candidate (from, to)
    pair. Candidates are voted on, then re-verified by mapping the whole sample back to export paths, so
    ``sample_matches`` is the number of sampled library files that really line up with an exported track.
    Walking the library (not the export) keeps a small library inside a huge export from being missed.
    """
    if not export_paths or not library_paths:
        return []
    by_tail: dict[str, list[str]] = {}
    export_keys: set[str] = set()
    for ep in export_paths:
        comps = _components(ep)
        export_keys.add(_pkey(ep))
        if len(comps) >= 2:
            by_tail.setdefault(_pkey("/".join(comps[-2:])), []).append(ep)

    step = max(1, len(library_paths) // sample_size)
    sample = library_paths[::step][:sample_size]
    votes: Counter[tuple[str, str]] = Counter()
    for lp in sample:
        l_comps = _components(lp)
        if len(l_comps) < 3:
            continue
        best_k, best_exp = 0, None
        for ep in by_tail.get(_pkey("/".join(l_comps[-2:])), []):
            e_comps = _components(ep)
            k = 0
            while k < min(len(e_comps), len(l_comps)) and _pkey(e_comps[-1 - k]) == _pkey(l_comps[-1 - k]):
                k += 1
            if k > best_k:
                best_k, best_exp = k, ep
        if best_exp is None or best_k < 2:
            continue
        e_comps = _components(best_exp)
        # every suffix length from 2 to best_k is a candidate: a case-insensitive match can swallow a folder that
        # the user would rather keep in the mapping ('Music' vs '/music'); verification below decides
        for k in range(2, best_k + 1):
            if k >= len(e_comps) or k >= len(l_comps):
                continue
            e_prefix = "/".join(e_comps[:-k])
            if best_exp.startswith("/"):
                e_prefix = "/" + e_prefix
            l_prefix = "/" + "/".join(l_comps[:-k]) if lp.startswith("/") else "/".join(l_comps[:-k])
            votes[(e_prefix, l_prefix)] += 1

    out: list[dict[str, Any]] = []
    for (src, dst), count in votes.most_common(max_suggestions * 4):
        dst_key = _pkey(dst).rstrip("/")
        verified = 0
        for lp in sample:
            lp_key = _pkey(lp)
            if lp_key.startswith(dst_key + "/") and _pkey(src.rstrip("/") + lp.replace("\\", "/")[len(dst.rstrip("/")):]) in export_keys:
                verified += 1
        if verified:
            out.append({"from": src, "to": dst, "sample_matches": verified, "_votes": count})
    out.sort(key=lambda m: (m["sample_matches"], m["_votes"], len(m["from"])), reverse=True)
    for m in out:
        m.pop("_votes")
    return out[:max_suggestions]


def build_preview(db: Database, parsed: dict[str, Any]) -> dict[str, Any]:
    paths = [p for p in (location_to_path(t["location"]) for t in parsed["tracks"].values()) if p]
    library_paths = [p for p, _tid in db.list_library_file_paths()]
    return {
        "track_count": len(parsed["tracks"]),
        "playlists": [
            {"key": p["key"], "name": p["name"], "is_smart": p["is_smart"], "item_count": len(p["items"])}
            for p in parsed["playlists"]
        ],
        "suggested_mappings": suggest_mappings(paths, library_paths),
        "skipped": parsed["skipped"],
    }


# --------------------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------------------

class TrackMatcher:
    """Matches export tracks to library tracks: file path first, then normalized metadata."""

    def __init__(self, db: Database, mappings: list[dict[str, str]]) -> None:
        self._db = db
        self._mappings = mappings
        self._paths: Optional[dict[str, str]] = None
        self._artists: dict[str, Optional[dict[str, Any]]] = {}
        self._indexes: dict[str, list[dict[str, Any]]] = {}

    def _path_index(self) -> dict[str, str]:
        if self._paths is None:
            self._paths = {_pkey(p): tid for p, tid in self._db.list_library_file_paths()}
        return self._paths

    def _artist(self, name: str) -> Optional[dict[str, Any]]:
        key = clean_library_name(name)
        if not key:
            return None
        if key not in self._artists:
            self._artists[key] = self._db.get_library_artist_by_name(name)
        return self._artists[key]

    def _index(self, artist_id: str) -> list[dict[str, Any]]:
        if artist_id not in self._indexes:
            self._indexes[artist_id] = self._db.list_library_artist_track_index(artist_id)
        return self._indexes[artist_id]

    def match(self, entry: dict[str, Any]) -> Optional[tuple[str, str]]:
        """``(library track id, "path" | "metadata")`` or None."""
        by_path = self._match_path(entry)
        if by_path:
            return by_path, "path"
        by_meta = self._match_metadata(entry)
        if by_meta:
            return by_meta, "metadata"
        return None

    def _match_path(self, entry: dict[str, Any]) -> Optional[str]:
        if not self._mappings:
            return None
        path = location_to_path(entry.get("location") or "")
        if not path:
            return None
        mapped = apply_mappings(path, self._mappings)
        if mapped is None:
            return None
        return self._path_index().get(_pkey(mapped))

    def _artist_rows(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        known = lambda n: self._artist(n) is not None  # noqa: E731
        album_artist, artist = entry.get("album_artist") or "", entry.get("artist") or ""
        names = [
            resolve_album_artist({"album_artist": album_artist, "artist": artist}, known_artist=known),
            album_artist,
            primary_artist(album_artist, known),
            artist,
            primary_artist(artist, known),
        ]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            row = self._artist(name) if name else None
            if row and row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        return rows

    def _match_metadata(self, entry: dict[str, Any]) -> Optional[str]:
        title = clean_library_name(entry.get("name") or "")
        if not title:
            return None
        album_clean = clean_library_name(entry.get("album") or "")
        number = entry.get("track_number") or None
        seconds = (entry.get("total_time_ms") or 0) / 1000.0
        squashed = title.replace(" ", "")
        for artist in self._artist_rows(entry):
            if album_clean:
                album = self._db.get_library_album_by_title(artist["id"], entry["album"])
                if album:
                    track = self._db.get_library_track_by_title(album["id"], entry["name"], number)
                    if track and self._db.get_library_file_for_track(track["id"]):
                        return str(track["id"])
            candidates = [
                t
                for t in self._index(artist["id"])
                if t["has_file"]
                and (t["clean_title"] == title or (squashed and str(t["clean_title"]).replace(" ", "") == squashed))
            ]
            if not candidates:
                continue

            def rank(t: dict[str, Any]) -> tuple[int, int, float]:
                diff = abs(float(t["duration_seconds"]) - seconds) if t["duration_seconds"] and seconds else 9999.0
                return (
                    0 if album_clean and t["album_clean_title"] == album_clean else 1,
                    0 if diff <= DURATION_TOLERANCE_S else 1,
                    diff,
                )

            return str(min(candidates, key=rank)["id"])
        return None


# --------------------------------------------------------------------------------------------------
# Job state
# --------------------------------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

PLAY_STATS_NOTE = (
    "Play counts, ratings and dates added were not imported: the library has no per-track play-count or rating "
    "store (listens are a per-user scrobble log and importing into it would trigger scrobbles)."
)


def playlist_id_for(key: str) -> str:
    return PLAYLIST_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def job_status(import_id: str) -> Optional[dict[str, Any]]:
    with _JOBS_LOCK:
        job = _JOBS.get(import_id)
        if job is None:
            return None
        snapshot = {k: v for k, v in job.items()}
        snapshot["playlists"] = [dict(p) for p in job["playlists"]]
        return snapshot


def start_job(import_id: str, selected: list[dict[str, Any]]) -> Optional[str]:
    """Registers a queued job; None if one is already queued or running for this export."""
    with _JOBS_LOCK:
        current = _JOBS.get(import_id)
        if current and current["state"] in ("queued", "running"):
            return None
        job_id = f"itunes-{uuid.uuid4().hex[:12]}"
        _JOBS[import_id] = {
            "job_id": job_id,
            "state": "queued",
            "total": len(selected),
            "done": 0,
            "error": None,
            "play_stats": None,
            "playlists": [
                {"key": p["key"], "name": p["name"], "state": "pending", "matched": 0, "missing": 0,
                 "created_playlist_id": None}
                for p in selected
            ],
        }
        return job_id


def fail_job(import_id: str, error: str) -> None:
    """Marks a job failed (releasing the export for a new commit) and fails any still-pending playlists."""
    with _JOBS_LOCK:
        job = _JOBS.get(import_id)
        if not job:
            return
        for p in job["playlists"]:
            if p["state"] == "pending":
                p["state"] = "failed"
                p["error"] = error
                job["done"] += 1
        job["state"] = "failed"
        job["error"] = error


def _update(import_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        if import_id in _JOBS:
            _JOBS[import_id].update(fields)


def _update_playlist(import_id: str, index: int, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(import_id)
        if job:
            job["playlists"][index].update(fields)
            if fields.get("state") in ("done", "failed"):
                job["done"] += 1


def display_name(playlist: dict[str, Any], name_prefix: Optional[str], include_folders: bool) -> str:
    name = playlist["name"]
    if include_folders and playlist.get("folder_path"):
        name = " / ".join([*playlist["folder_path"], name])
    return f"{name_prefix or ''}{name}"[:200]


def run_import(
    db: Database,
    config: Any,
    import_id: str,
    parsed: dict[str, Any],
    selected: list[dict[str, Any]],
    *,
    mappings: list[dict[str, str]],
    monitor_mode: Optional[str],
    name_prefix: Optional[str],
    include_folders: bool,
    import_play_stats: bool,
    creator_id: str,
    on_done: Optional[Callable[[], None]] = None,
) -> None:
    """Thread body: matches and persists each selected playlist. Never raises; failures land in the job status."""
    _update(import_id, state="running")
    try:
        with track_job("itunes_import", "iTunes library import") as handle:
            matcher = TrackMatcher(db, mappings)
            cache: dict[str, Optional[tuple[str, str]]] = {}
            totals = Counter()
            for index, playlist in enumerate(selected):
                try:
                    result = _import_playlist(
                        db, config, parsed, playlist, matcher, cache, monitor_mode,
                        display_name(playlist, name_prefix, include_folders), creator_id,
                    )
                except Exception as exc:  # isolate per playlist: any failure marks it failed and the rest continue
                    logger.exception("iTunes import: playlist %r failed", playlist["name"])
                    _update_playlist(import_id, index, state="failed", error=safe_exc(exc))
                    totals["failed"] += 1
                    continue
                totals["matched"] += result["matched"]
                totals["missing"] += result["missing"]
                _update_playlist(import_id, index, state="done", **result)
            play_stats = {"requested": bool(import_play_stats), "applied": False}
            if import_play_stats:
                play_stats["note"] = PLAY_STATS_NOTE
            _update(import_id, play_stats=play_stats)
            handle.message = (
                f"playlists={len(selected)}, matched={totals['matched']}, missing={totals['missing']}, "
                f"failed={totals['failed']}"
            )
        _update(import_id, state="completed")
    except Exception as exc:  # last line of defence for a daemon thread: logged with traceback, surfaced in status
        logger.exception("iTunes import %s crashed", import_id)
        fail_job(import_id, safe_exc(exc))
    finally:
        if on_done:
            on_done()


def _import_playlist(
    db: Database,
    config: Any,
    parsed: dict[str, Any],
    playlist: dict[str, Any],
    matcher: TrackMatcher,
    cache: dict[str, Optional[tuple[str, str]]],
    monitor_mode: Optional[str],
    name: str,
    creator_id: str,
) -> dict[str, Any]:
    tracks_json: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    missing_seen: set[tuple[str, str]] = set()
    matched = 0
    for tid in playlist["items"]:
        entry = parsed["tracks"].get(tid)
        if not entry or not entry["name"]:
            continue
        artist = entry["artist"] or entry["album_artist"]
        tracks_json.append({"title": entry["name"], "artist": artist, "album": entry["album"]})
        if tid not in cache:
            cache[tid] = matcher.match(entry)
        if cache[tid]:
            matched += 1
            continue
        key = (entry["name"].lower(), artist.lower())
        if key not in missing_seen:
            missing_seen.add(key)
            missing.append({"title": entry["name"], "artist": artist, "album": entry["album"]})

    pid = playlist_id_for(playlist["key"])
    existing = db.get_playlist(pid)
    kind = "smart playlist" if playlist["is_smart"] else "playlist"
    # On re-import keep what the user may have changed (name, poster, enabled); only tracks/missing are refreshed.
    db.upsert_playlist(
        playlist_id=pid,
        name=str(existing["name"]) if existing else name,
        service=SERVICE,
        description=f"Imported from an iTunes / Apple Music library export ({kind} snapshot)",
        poster_url=str(existing.get("poster_url") or "") if existing else "",
        enabled=bool(existing["enabled"]) if existing else True,
        creator_id=creator_id,
        tracks_json=json.dumps(tracks_json),
    )
    if not db.get_playlist_targets(pid):
        db.set_playlist_targets(pid, [creator_id])
    mode = monitor_mode or (str(existing.get("monitor_mode") or "none") if existing else "none")
    db.set_playlist_monitor_mode(pid, mode)
    db.record_sync_result(pid, "imported", missing_tracks=missing)
    if mode in ("album", "artist"):
        apply_playlist_missing_safely(db, config, pid)
    return {"matched": matched, "missing": len(missing), "created_playlist_id": pid, "updated": existing is not None}
