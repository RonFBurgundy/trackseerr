"""Audio file to catalog track reconciliation and matching for Trackseerr."""

import difflib
import logging
from pathlib import Path
from typing import Any, Optional


from trackseerr.library import (
    fingerprint_audio_file,
)
from trackseerr.storage import Database, clean_library_name


logger = logging.getLogger(__name__)


def reconcile_audio_file_to_track(
    meta: dict[str, Any],
    candidate_tracks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Reconciles an audio file's metadata against expected library tracks; returns only the track.

    Thin wrapper over reconcile_audio_file_to_track_scored (see it for the matching hierarchy).
    """
    return reconcile_audio_file_to_track_scored(meta, candidate_tracks)[0]



MATCH_STRONG = "strong"



MATCH_WEAK = "weak"



MATCH_NONE = "none"



def reconcile_audio_file_to_track_scored(  # noqa: C901
    meta: dict[str, Any],
    candidate_tracks: list[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], str]:
    """Reconciles an audio file's metadata against a list of expected library tracks.

    Matching hierarchy:
    1. Exact match on disc_number and track_number (if mutagen extracted valid track number).
    2. Clean title similarity match (clean_library_name(t["title"]) == clean_library_name(meta["title"]) or ratio >= 0.85).
    3. Duration tolerance match (within 5 seconds) if multiple candidates match title.

    Returns (track, strength). Strength is "strong" for a unique disc+track number match, a number match
    disambiguated to one by exact title, or a unique exact clean-title match; "weak" for every fallback pick
    (ambiguous picks, duration tie-breaks, fuzzy matches); "none" when nothing matched.
    """
    if not candidate_tracks:
        return None, MATCH_NONE

    file_track = meta.get("track_number")
    file_disc = meta.get("disc_number") or 1
    has_valid_track_num = isinstance(file_track, int) and file_track > 0

    # 1. Exact match on disc_number and track_number (if mutagen extracted valid track number)
    if has_valid_track_num:
        num_matches = [
            t
            for t in candidate_tracks
            if int(t.get("track_number") or 1) == file_track
            and int(t.get("disc_number") or 1) == int(file_disc)
        ]
        if len(num_matches) == 1:
            return num_matches[0], MATCH_STRONG
        elif len(num_matches) > 1:
            clean_title = clean_library_name(meta.get("title") or "")
            title_matches = [
                t
                for t in num_matches
                if clean_library_name(t.get("title") or "") == clean_title
            ]
            if len(title_matches) == 1:
                return title_matches[0], MATCH_STRONG
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in num_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    ), MATCH_WEAK
            return num_matches[0], MATCH_WEAK

    # 2. Clean title similarity match
    meta_title = meta.get("title") or ""
    clean_meta = clean_library_name(meta_title)
    if not clean_meta and meta.get("file_path"):
        clean_meta = clean_library_name(Path(meta["file_path"]).stem)

    if clean_meta:
        # Exact clean title match
        exact_title_matches = [
            t
            for t in candidate_tracks
            if clean_library_name(t.get("title") or "") == clean_meta
        ]
        if len(exact_title_matches) == 1:
            return exact_title_matches[0], MATCH_STRONG
        elif len(exact_title_matches) > 1:
            # 3. Duration tolerance match (within 5 seconds) if multiple candidates match title
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in exact_title_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if len(dur_matches) == 1:
                    return dur_matches[0], MATCH_WEAK
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    ), MATCH_WEAK
            return exact_title_matches[0], MATCH_WEAK

        # Fuzzy title match with ratio >= 0.85
        fuzzy_candidates: list[tuple[float, dict[str, Any]]] = []
        for t in candidate_tracks:
            clean_t = clean_library_name(t.get("title") or "")
            if not clean_t:
                continue
            ratio = difflib.SequenceMatcher(None, clean_t, clean_meta).ratio()
            if ratio >= 0.85:
                fuzzy_candidates.append((ratio, t))

        if fuzzy_candidates:
            fuzzy_candidates.sort(key=lambda x: x[0], reverse=True)
            top_ratio = fuzzy_candidates[0][0]
            top_matches = [t for r, t in fuzzy_candidates if abs(r - top_ratio) < 0.001]
            if len(top_matches) == 1:
                return top_matches[0], MATCH_WEAK

            # 3. Duration tolerance match if multiple fuzzy candidates
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in top_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if len(dur_matches) == 1:
                    return dur_matches[0], MATCH_WEAK
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    ), MATCH_WEAK
            return top_matches[0], MATCH_WEAK

    return None, MATCH_NONE



FINGERPRINT_MIN_SCORE = 0.80



def _fingerprint_fallback_match(
    file_path: Path,
    media_settings: dict[str, Any],
    remaining_tracks: list[dict[str, Any]],
    tag_track: Optional[dict[str, Any]],
    strength: str,
) -> Optional[dict[str, Any]]:
    """Resolves a weak or missing tag match via AcoustID fingerprinting; returns the tag result when it cannot improve.

    Only runs when the tag match is not strong, fingerprint_on_weak_match is enabled and an AcoustID key is set.
    fingerprint_audio_file never raises, so a lookup failure leaves the tag result untouched.
    """
    if strength == MATCH_STRONG:
        logger.info("Import match for %s decided by tag-strong", file_path.name)
        return tag_track
    api_key = media_settings.get("acoustid_api_key")
    if not (media_settings.get("fingerprint_on_weak_match") and api_key):
        logger.info("Import match for %s decided by tag-weak-kept (fingerprint fallback disabled)", file_path.name)
        return tag_track

    fp = fingerprint_audio_file(file_path, api_key)
    if fp and float(fp.get("score") or 0.0) >= FINGERPRINT_MIN_SCORE:
        rec_id = fp.get("recording_id")
        if rec_id:
            rec_hits = [t for t in remaining_tracks if t.get("mb_recording_id") == rec_id]
            if rec_hits:
                logger.info("Import match for %s decided by fingerprint-recording (%s)", file_path.name, rec_id)
                return rec_hits[0]
        fp_title = clean_library_name(fp.get("title") or "")
        if fp_title:
            title_hits = [t for t in remaining_tracks if clean_library_name(t.get("title") or "") == fp_title]
            if len(title_hits) == 1:
                logger.info("Import match for %s decided by fingerprint-title (%s)", file_path.name, fp_title)
                return title_hits[0]
    logger.info("Import match for %s decided by tag-weak-kept (strength=%s)", file_path.name, strength)
    return tag_track



def resolve_download_expected_tracks(
    db: Database,
    item: dict[str, Any],
    req: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """The catalog album a native download targets and that album's tracks (the tracks the import expects).

    Resolution order: the download's album_id, its track's album, then artist name + album title.
    Returns (None, []) when the download cannot be tied to a catalog album.
    """
    target_album = None
    if item.get("album_id"):
        target_album = db.get_library_album(item["album_id"])
    elif item.get("track_id"):
        req_track = db.get_library_track(item["track_id"])
        if req_track:
            target_album = db.get_library_album(req_track["album_id"])

    if not target_album:
        art_name_cand = item.get("artist") or (req.get("artist") if req else None)
        alb_title_cand = (
            (item.get("title") if item.get("item_type") == "album" else None)
            or (req.get("album") or req.get("title") if req else None)
            or item.get("title")
        )
        if art_name_cand and alb_title_cand:
            art_cand = db.get_library_artist_by_name(art_name_cand)
            if art_cand:
                target_album = db.get_library_album_by_title(art_cand["id"], alb_title_cand)

    expected_tracks: list[dict[str, Any]] = []
    if target_album:
        expected_tracks = db.list_library_tracks(album_id=target_album["id"], limit=1000)
    return target_album, expected_tracks

