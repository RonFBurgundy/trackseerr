"""AcoustID contribution task and submission lifecycle management.

Submits audio fingerprints and MusicBrainz recording MBIDs of library files
to the AcoustID web service using pyacoustid.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from typing import Any, Callable, Optional

import acoustid

from trackseerr.library import FingerprintUnavailable
from trackseerr.rename_verification import verify_file

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
_DURATION_TOLERANCE_SECONDS = 7.0
_CONFLICTING_MATCH_MIN_SCORE = 0.9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_bitrate_kbps(bitrate: Any) -> Optional[int]:
    """Normalizes bitrate to integer kbps, or None if unknown/invalid."""
    if bitrate is None or isinstance(bitrate, bool):
        return None
    try:
        val = int(bitrate)
    except (ValueError, TypeError):
        return None
    if val <= 0:
        return None
    if val > 2000:
        return round(val / 1000)
    return val


def _classify_candidate(
    candidate: dict[str, Any], fp_res: dict[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    """Classifies a fingerprint result against candidate track metadata.

    Returns (status, reason) if skipped, or (None, None) if eligible to submit.
    """
    fp = fp_res.get("fingerprint")
    if fp_res.get("status") == "error" or not fp:
        return "skipped", "fingerprint_error"

    target_mbid = candidate.get("mb_recording_id")
    matches = fp_res.get("matches") or []
    match_ids = {m.get("recording_id") for m in matches if m.get("recording_id")}

    if target_mbid in match_ids:
        return "skipped", "already_linked"

    if any(
        float(m.get("score") or 0.0) >= _CONFLICTING_MATCH_MIN_SCORE
        for m in matches
    ):
        return "skipped", "conflicting_match"

    track_duration = candidate.get("duration_seconds")
    fp_duration = fp_res.get("duration")
    if (
        track_duration is not None
        and track_duration > 0
        and fp_duration is not None
        and fp_duration > 0
    ):
        diff = abs(float(fp_duration) - float(track_duration))
        if diff > _DURATION_TOLERANCE_SECONDS:
            return "skipped", "duration_mismatch"

    return None, None


def _build_submit_item(
    candidate: dict[str, Any], fp_res: dict[str, Any]
) -> dict[str, Any]:
    """Constructs the pyacoustid payload dict for a single file submission."""
    duration = int(round(float(fp_res["duration"])))
    item: dict[str, Any] = {
        "fingerprint": fp_res["fingerprint"],
        "duration": duration,
        "mbid": candidate["mb_recording_id"],
    }
    kbps = _format_bitrate_kbps(candidate.get("bitrate"))
    if kbps is not None:
        item["bitrate"] = kbps
    codec = str(candidate.get("codec") or "").strip()
    if codec:
        item["fileformat"] = codec
    return item


def _handle_submission_batch_error(
    db: Any,
    batch: list[tuple[dict[str, Any], dict[str, Any]]],
    exc: Exception,
    counts: dict[str, int],
) -> None:
    logger.warning("AcoustID submission batch failed: %s", exc, exc_info=True)
    err_text = str(exc)[:200]
    now_iso = _now_iso()
    for cand, item in batch:
        fp_str = str(item.get("fingerprint") or "")
        fp_hash = hashlib.sha1(fp_str.encode("utf-8")).hexdigest() if fp_str else ""
        db.upsert_acoustid_submission({
            "file_id": cand["id"],
            "recording_mbid": cand["mb_recording_id"],
            "fingerprint_hash": fp_hash,
            "submission_id": None,
            "status": "error",
            "reason": err_text,
            "submitted_at": None,
            "checked_at": now_iso,
        })
        counts["errors"] += 1


def _flush_batch(
    db: Any,
    api_key: str,
    user_key: str,
    batch: list[tuple[dict[str, Any], dict[str, Any]]],
    counts: dict[str, int],
) -> None:
    """Submits a batch of files to AcoustID and updates the submission store."""
    if not batch:
        return
    data = [item for _, item in batch]
    files = [cand for cand, _ in batch]
    try:
        resp = acoustid.submit(api_key, user_key, data)
    except Exception as exc:
        _handle_submission_batch_error(db, batch, exc, counts)
        return

    submissions = resp.get("submissions") or []
    now_iso = _now_iso()
    mapped_indices: set[int] = set()

    for sub in submissions:
        idx = sub.get("index")
        if idx is not None and 0 <= idx < len(files):
            mapped_indices.add(idx)
            cand = files[idx]
            item = data[idx]
            sub_status = sub.get("status") or "pending"
            sub_id = sub.get("id")
            fp_str = str(item.get("fingerprint") or "")
            fp_hash = hashlib.sha1(fp_str.encode("utf-8")).hexdigest() if fp_str else ""

            db.upsert_acoustid_submission({
                "file_id": cand["id"],
                "recording_mbid": cand["mb_recording_id"],
                "fingerprint_hash": fp_hash,
                "submission_id": sub_id,
                "status": sub_status,
                "reason": None,
                "submitted_at": now_iso,
                "checked_at": now_iso,
            })
            if sub_status == "imported":
                counts["imported"] += 1
            else:
                counts["submitted"] += 1

    # Catch any indices not reported in response
    for idx in range(len(files)):
        if idx not in mapped_indices:
            cand = files[idx]
            item = data[idx]
            fp_str = str(item.get("fingerprint") or "")
            fp_hash = hashlib.sha1(fp_str.encode("utf-8")).hexdigest() if fp_str else ""
            db.upsert_acoustid_submission({
                "file_id": cand["id"],
                "recording_mbid": cand["mb_recording_id"],
                "fingerprint_hash": fp_hash,
                "submission_id": None,
                "status": "pending",
                "reason": None,
                "submitted_at": now_iso,
                "checked_at": now_iso,
            })
            counts["submitted"] += 1


def contribute_fingerprints(
    db: Any,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
    limit: int = 500,
) -> dict[str, int]:
    """Finds unsubmitted MBID-tagged library files and submits them to AcoustID."""
    settings = db.get_media_management_settings()
    api_key = str(settings.get("acoustid_api_key") or "").strip()
    user_key = str(settings.get("acoustid_user_key") or "").strip()
    if not api_key or not user_key:
        logger.info(
            "AcoustID contribution skipped: missing AcoustID API key or user key"
        )
        return {"missing_keys": 1}

    counts: dict[str, int] = {
        "submitted": 0,
        "imported": 0,
        "already_linked": 0,
        "conflicting_match": 0,
        "duration_mismatch": 0,
        "fingerprint_error": 0,
        "errors": 0,
    }

    queue: list[tuple[dict[str, Any], dict[str, Any]]] = []
    processed = 0

    for candidate in db.iter_acoustid_candidates():
        if processed >= limit:
            break
        if should_stop is not None and should_stop():
            break

        try:
            fp_res = verify_file(db, candidate, api_key)
        except FingerprintUnavailable as exc:
            logger.warning("AcoustID contribution stopped: fpcalc unavailable: %s", exc)
            counts["fpcalc_unavailable"] = 1
            return counts

        status, reason = _classify_candidate(candidate, fp_res)
        if status == "skipped":
            fp = fp_res.get("fingerprint")
            fp_hash = hashlib.sha1(fp.encode("utf-8")).hexdigest() if fp else ""
            db.upsert_acoustid_submission({
                "file_id": candidate["id"],
                "recording_mbid": candidate["mb_recording_id"],
                "fingerprint_hash": fp_hash,
                "submission_id": None,
                "status": "skipped",
                "reason": reason,
                "submitted_at": None,
                "checked_at": _now_iso(),
            })
            if reason in counts:
                counts[reason] += 1
        else:
            item = _build_submit_item(candidate, fp_res)
            queue.append((candidate, item))
            if len(queue) >= BATCH_SIZE:
                _flush_batch(db, api_key, user_key, queue, counts)
                queue.clear()

        processed += 1

    if queue:
        _flush_batch(db, api_key, user_key, queue, counts)
        queue.clear()

    return counts


def refresh_pending_submissions(
    db: Any,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
    limit: int = 200,
) -> dict[str, int]:
    """Queries AcoustID for pending submissions and marks resolved ones as imported."""
    settings = db.get_media_management_settings()
    api_key = str(settings.get("acoustid_api_key") or "").strip()
    if not api_key:
        logger.info("AcoustID refresh pending skipped: missing AcoustID API key")
        return {"missing_keys": 1}

    counts = {"imported": 0, "errors": 0}
    pending_rows = db.list_acoustid_submissions("pending", limit=limit)
    now_iso = _now_iso()

    for row in pending_rows:
        if should_stop is not None and should_stop():
            break
        sub_id = row.get("submission_id")
        if not sub_id:
            continue
        try:
            res = acoustid.get_submission_status(api_key, sub_id)
        except Exception as exc:
            logger.warning(
                "AcoustID check submission status failed for %s: %s",
                sub_id,
                exc,
                exc_info=True,
            )
            counts["errors"] += 1
            continue

        submissions = res.get("submissions") or []
        for s in submissions:
            st = s.get("status")
            if st == "imported":
                row_update = {**row, "status": "imported", "checked_at": now_iso}
                db.upsert_acoustid_submission(row_update)
                counts["imported"] += 1
            else:
                row_update = {**row, "checked_at": now_iso}
                db.upsert_acoustid_submission(row_update)

    return counts


def run_acoustid_contribution(
    db: Any,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, int]:
    """Main manual task entry point: refreshes pending then contributes."""
    settings = db.get_media_management_settings()
    api_key = str(settings.get("acoustid_api_key") or "").strip()
    user_key = str(settings.get("acoustid_user_key") or "").strip()
    if not api_key or not user_key:
        logger.info(
            "AcoustID contribution skipped: missing AcoustID API key or user key"
        )
        return {"missing_keys": 1}

    refreshed = refresh_pending_submissions(db, should_stop=should_stop)
    if should_stop is not None and should_stop():
        return {
            "submitted": 0,
            "imported": refreshed.get("imported", 0),
            "already_linked": 0,
            "conflicting_match": 0,
            "duration_mismatch": 0,
            "fingerprint_error": 0,
            "errors": refreshed.get("errors", 0),
        }

    contrib = contribute_fingerprints(db, should_stop=should_stop)
    if "missing_keys" in contrib:
        return contrib

    merged = {
        "submitted": contrib.get("submitted", 0),
        "imported": refreshed.get("imported", 0) + contrib.get("imported", 0),
        "already_linked": contrib.get("already_linked", 0),
        "conflicting_match": contrib.get("conflicting_match", 0),
        "duration_mismatch": contrib.get("duration_mismatch", 0),
        "fingerprint_error": contrib.get("fingerprint_error", 0),
        "errors": refreshed.get("errors", 0) + contrib.get("errors", 0),
    }
    if "fpcalc_unavailable" in contrib:
        merged["fpcalc_unavailable"] = contrib["fpcalc_unavailable"]
    return merged
