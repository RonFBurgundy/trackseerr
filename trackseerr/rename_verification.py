"""Verifies rename-preview rows by fingerprinting the audio (AcoustID) and comparing the identified MusicBrainz
recording with the recording of the library track the file is linked to.

No FastAPI imports: the (later) routes call ``start_verification`` and poll ``get_verification_status``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from trackseerr.job_tracker import track_job
from trackseerr.library import FingerprintUnavailable, fingerprint_and_lookup
from trackseerr.redaction import safe_exc
from trackseerr.rename_reasons import (
    ALBUM_TITLE,
    ALBUM_YEAR,
    COLLISION,
    DISC_LAYOUT,
    FILE_TITLE,
    TRACK_NUMBER,
)
from trackseerr.storage_common import clean_library_name
from trackseerr.task_manager import TRIGGER_MANUAL, record_task_run

logger = logging.getLogger(__name__)

VERIFIED = "verified"
MISMATCH = "mismatch"
UNKNOWN = "unknown"

RISKY_REASONS = frozenset({TRACK_NUMBER, FILE_TITLE, ALBUM_TITLE, ALBUM_YEAR, DISC_LAYOUT, COLLISION})
KEY_REJECTED_MESSAGE = "AcoustID rejected the API key"
UNAVAILABLE_MESSAGE = "Audio fingerprinting is unavailable (fpcalc / pyacoustid missing)"


def is_risky(reasons: list[str]) -> bool:
    """True when any reason means the file may be linked to the wrong catalog track."""
    return any(r in RISKY_REASONS for r in reasons)


def _verdict(status: str, score: Optional[float], **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "score": score,
        "actual_track_id": None,
        "actual_album_id": None,
        "actual_title": None,
        "actual_artist": None,
    }
    out.update(extra)
    return out


def _find_library_hit(db: Any, ids: list[str], album_id: Optional[str]) -> Optional[tuple[dict[str, Any], str]]:
    """The library track (and the recording id that found it) for the first matched recording, same album first."""
    scopes: list[Optional[str]] = [album_id, None] if album_id is not None else [None]
    for scope in scopes:
        for rid in ids:
            hit = db.get_library_track_by_mb_recording_id(rid, album_id=scope)
            if hit:
                return hit, rid
    return None


def _same_recording_other_album(hit: dict[str, Any], track: dict[str, Any]) -> bool:
    """True when the library hit is on another album but carries the linked track's (cleaned) title."""
    if hit.get("album_id") == track.get("album_id"):
        return False
    return clean_library_name(str(hit.get("title") or "")) == clean_library_name(str(track.get("title") or ""))


def verdict_for(db: Any, fp_row: Optional[dict[str, Any]], track: dict[str, Any], current_path: str) -> Optional[dict[str, Any]]:
    """Compares a cached fingerprint row with the linked track. None when there is no usable (current) row."""
    if fp_row is None or fp_row.get("file_path") != current_path:
        return None
    matches = fp_row.get("matches") or []
    if fp_row.get("status") != "ok" or not matches:
        return _verdict(UNKNOWN, None)
    scores = {m["recording_id"]: m["score"] for m in matches}
    target = track.get("mb_recording_id")
    if target and target in scores:
        return _verdict(VERIFIED, scores[target])
    found = _find_library_hit(db, list(scores), track.get("album_id"))
    top = matches[0]
    if found:
        hit, rid = found
        if hit["id"] == track["id"]:
            return _verdict(VERIFIED, scores[rid])
        if not target and _same_recording_other_album(hit, track):
            return _verdict(UNKNOWN, None)  # a single and its album release share a recording: not a mismatch
        return _verdict(
            MISMATCH,
            scores[rid],
            actual_track_id=hit["id"],
            actual_album_id=hit.get("album_id"),
            actual_title=hit.get("title"),
        )
    if target:
        return _verdict(MISMATCH, top["score"], actual_title=top.get("title"), actual_artist=top.get("artist"))
    return _verdict(UNKNOWN, None)


def _cache_valid(row: dict[str, Any], path: str, st: os.stat_result) -> bool:
    return (
        row.get("file_path") == path
        and row.get("size_bytes") == st.st_size
        and row.get("mtime_ns") == st.st_mtime_ns
        and row.get("status") in ("ok", "no_result")
    )


def verify_file(db: Any, file_row: dict[str, Any], api_key: str) -> dict[str, Any]:
    """The fingerprint row for a library file: from the cache when the file is unchanged, else freshly looked up.

    A missing file returns ``{"status": "error", "error": "file missing"}`` (not cached). Raises
    FingerprintUnavailable when fingerprinting cannot run at all. A returned row carries ``cached`` (bool).
    """
    path = file_row["file_path"]
    try:
        st = os.stat(path)
    except OSError as exc:
        logger.warning("verify_file: cannot stat %s: %s", path, exc)
        return {"status": "error", "error": "file missing", "cached": False}
    stored = db.get_file_fingerprint(file_row["id"])
    if stored and _cache_valid(stored, path, st):
        return {**stored, "cached": True}
    result = fingerprint_and_lookup(path, api_key)
    row = {
        "file_id": file_row["id"],
        "file_path": path,
        "size_bytes": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        **result,
    }
    db.upsert_file_fingerprint(row)
    return {**row, "cached": False}


_state_lock = threading.Lock()
_state: dict[str, Any] = {}


def _fresh_state(total: int = 0, running: bool = False) -> dict[str, Any]:
    return {
        "running": running,
        "total": total,
        "done": 0,
        "verified": 0,
        "mismatched": 0,
        "unknown": 0,
        "errors": 0,
        "cached": 0,
        "message": "",
        "started_at": time.time() if running else None,
        "finished_at": None,
    }


_state.update(_fresh_state())


def get_verification_status() -> dict[str, Any]:
    """A snapshot of the current / last verification run."""
    with _state_lock:
        return dict(_state)


def _bump(**counts: int) -> None:
    with _state_lock:
        for key, n in counts.items():
            _state[key] += n


_VERDICT_COUNTER = {VERIFIED: "verified", MISMATCH: "mismatched", UNKNOWN: "unknown"}


def _verify_one(db: Any, file_id: str, api_key: str) -> Optional[dict[str, Any]]:
    """Verifies one file and updates the counters; returns its fingerprint row (None when it had no file/track).

    FingerprintUnavailable propagates.
    """
    file_row = db.get_library_file(file_id)
    track = db.get_library_track(file_row["track_id"]) if file_row else None
    if not file_row or not track:
        _bump(errors=1, done=1)
        return None
    fp_row = verify_file(db, file_row, api_key)
    if fp_row.get("status") == "error":
        _bump(errors=1, done=1)
        return fp_row
    verdict = verdict_for(db, fp_row, track, file_row["file_path"])
    counter = _VERDICT_COUNTER.get(verdict["status"] if verdict else UNKNOWN, "unknown")
    _bump(**{counter: 1, "done": 1, "cached": 1 if fp_row.get("cached") else 0})
    return fp_row


def _key_rejected(fp_row: Optional[dict[str, Any]]) -> bool:
    """True when a fresh lookup failed because AcoustID rejected the API key."""
    if not fp_row or fp_row.get("cached") or fp_row.get("status") != "error":
        return False
    return "invalid api key" in str(fp_row.get("error") or "").lower()


def run_verification(
    db: Any,
    file_ids: list[str],
    api_key: str,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Verifies the given files one by one (blocking) and returns the final counts."""
    with _state_lock:
        _state.clear()
        _state.update(_fresh_state(total=len(file_ids), running=True))
    try:
        first_lookup_pending = True
        for file_id in file_ids:
            if should_stop and should_stop():
                break
            fp_row = _verify_one(db, file_id, api_key)
            if first_lookup_pending and fp_row and not fp_row.get("cached") and fp_row.get("error") != "file missing":
                first_lookup_pending = False
                if _key_rejected(fp_row):
                    with _state_lock:
                        _state["message"] = KEY_REJECTED_MESSAGE
                    break
    except FingerprintUnavailable:
        with _state_lock:
            _state["message"] = UNAVAILABLE_MESSAGE
    finally:
        with _state_lock:
            _state["running"] = False
            _state["finished_at"] = time.time()
    return get_verification_status()


def _summary(counts: dict[str, Any]) -> str:
    if counts.get("message"):
        return str(counts["message"])
    return f"verified={counts['verified']} mismatched={counts['mismatched']} unknown={counts['unknown']}"


def _thread_main(db: Any, file_ids: list[str], api_key: str) -> None:
    try:
        with track_job("rename_verify", "AcoustID verify") as job, record_task_run(
            db, "rename_verify", TRIGGER_MANUAL
        ) as run:
            counts = run_verification(db, file_ids, api_key) or get_verification_status()
            summary = _summary(counts)
            run.message = summary
            job.update_message(summary)
    except Exception as exc:  # worker thread: log the cause, never leave the state stuck on running
        logger.error("Rename verification failed: %s", safe_exc(exc))
        with _state_lock:
            _state["running"] = False
            _state["finished_at"] = time.time()
            _state["message"] = "Verification failed"


def start_verification(db: Any, file_ids: list[str], api_key: str) -> bool:
    """Starts a background verification run; False when one is already running."""
    with _state_lock:
        if _state.get("running"):
            return False
        _state.clear()
        _state.update(_fresh_state(total=len(file_ids), running=True))
    threading.Thread(target=_thread_main, args=(db, file_ids, api_key), name="rename-verify", daemon=True).start()
    return True
