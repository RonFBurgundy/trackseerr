"""Tailored per-user mixes (Discover Weekly, Daily Blend, Artist Radio) built from listen history and Deezer."""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from plex_playlist_sync.library_availability import get_item_availability
from plex_playlist_sync.models import Playlist, RequestStatus, Track
from plex_playlist_sync.redaction import redact_text, safe_exc
from plex_playlist_sync.request_submission import (
    RequestRejected,
    RequestSubmission,
    effective_quota_limits,
    run_submission_followups,
    submit_track_request,
    user_request_lock,
)

logger = logging.getLogger(__name__)

QUOTA_WINDOW_DAYS = 7
FAMILIAR_POOL_SIZE = 200
RELATED_PER_SEED = 4
TRACKS_PER_RELATED = 5
RELATED_FETCH_LIMIT = 10
DAILY_BLEND_MAX_WINDOW_DAYS = 3
MIX_SUFFIX = "TrackSeerr"

DEFAULT_NAMES = {
    "discover_weekly": f"Discover Weekly · {MIX_SUFFIX}",
    "daily_blend": f"Daily Blend · {MIX_SUFFIX}",
}


def default_mix_name(mix_type: str, seed_artist: Optional[str] = None) -> str:
    if mix_type == "artist_radio":
        return f"{(seed_artist or 'Artist').strip()} Radio · {MIX_SUFFIX}"
    return DEFAULT_NAMES.get(mix_type, f"Mix · {MIX_SUFFIX}")


class InsufficientHistoryError(Exception):
    """The user has no usable listen history to seed the mix from."""


@dataclass
class MixTrack:
    artist: str
    title: str
    album: Optional[str] = None
    origin: str = "familiar"  # "familiar" | "discovery"

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist.strip().lower(), self.title.strip().lower())


@dataclass
class TailoredMixResult:
    mix_id: str
    generated_at: str
    total: int = 0
    available: int = 0
    missing: int = 0
    acquisitions_queued: int = 0
    quota_remaining: int = 0
    synced: bool = False
    sync_error: Optional[str] = None
    tracks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def _round_half_up(value: float) -> int:
    return int(value + 0.5)


def _interleave(familiar: list[MixTrack], discovery: list[MixTrack]) -> list[MixTrack]:
    """Deterministic even spread of discovery tracks among familiar ones."""
    total = len(familiar) + len(discovery)
    out: list[MixTrack] = []
    fi = di = 0
    for i in range(total):
        want_disc = _round_half_up((i + 1) * len(discovery) / total) if total else 0
        if (di < want_disc and di < len(discovery)) or fi >= len(familiar):
            out.append(discovery[di])
            di += 1
        else:
            out.append(familiar[fi])
            fi += 1
    return out


def _deezer_id(discovery: Any, artist_name: str) -> Optional[str]:
    found = discovery.search_artist(artist_name)
    if not found:
        return None
    raw = str(found.get("id") or "")
    num = raw.rsplit(":", 1)[-1]
    return num if num.isdigit() else None


def _discovery_by_seed(discovery: Any, seed: str, include_seed: bool) -> list[MixTrack]:
    """Candidate discovery tracks for one seed: related artists' top tracks (plus the seed's own for radio)."""
    artist_id = _deezer_id(discovery, seed)
    if not artist_id:
        logger.debug("Tailored mix: no Deezer artist match for seed '%s'", seed)
        return []
    candidates: list[MixTrack] = []
    sources: list[tuple[str, str]] = [(artist_id, seed)] if include_seed else []
    for rel in discovery.get_related_artists(artist_id, limit=RELATED_FETCH_LIMIT)[:RELATED_PER_SEED]:
        sources.append((str(rel.get("id")), str(rel.get("name") or "")))
    for src_id, _name in sources:
        for t in discovery.get_artist_top_tracks(src_id, limit=TRACKS_PER_RELATED):
            if t.get("title") and t.get("artist"):
                candidates.append(
                    MixTrack(artist=str(t["artist"]), title=str(t["title"]), album=t.get("album"), origin="discovery")
                )
    return candidates


def compile_user_mix(
    db: Any, discovery: Any, config: dict[str, Any], now: Optional[datetime] = None
) -> list[MixTrack]:
    """Build the track list for a mix config row. Raises InsufficientHistoryError when there is nothing to seed from."""
    now = now or datetime.now(timezone.utc)
    user_id = str(config["user_id"])
    mix_type = config["mix_type"]
    track_count = int(config.get("track_count") or 30)
    ratio = float(config.get("discovery_ratio") if config.get("discovery_ratio") is not None else 0.7)
    window_days = int(config.get("seed_window_days") or 14)
    if mix_type == "daily_blend":
        window_days = min(window_days, DAILY_BLEND_MAX_WINDOW_DAYS)
    since_iso = (now - timedelta(days=window_days)).isoformat(timespec="seconds")

    if config.get("excluded_genres"):
        logger.debug("Tailored mix %s: Deezer related/top carries no genre data; excluded_genres not applied", config.get("id"))

    # Seeds
    if mix_type == "artist_radio":
        seed_artist = (config.get("seed_artist") or "").strip()
        if not seed_artist:
            raise ValueError("seed_artist is required for artist_radio")
        seeds = [seed_artist]
    else:
        seeds = [row["artist"] for row in db.top_artists(user_id, since_iso, limit=10) if row.get("artist")]
        if not seeds:
            raise InsufficientHistoryError("Not enough listening history to build this mix yet")
    seed_keys = {s.strip().lower() for s in seeds}
    is_radio = mix_type == "artist_radio"

    heard = db.heard_track_keys(user_id)

    # Resolve the related-artist names for radio's familiar filter and gather discovery candidates per seed.
    per_seed: list[list[MixTrack]] = []
    related_names: set[str] = set()
    for seed in seeds:
        candidates = _discovery_by_seed(discovery, seed, include_seed=is_radio)
        if is_radio:
            related_names.update(c.artist.strip().lower() for c in candidates)
        per_seed.append(candidates)

    # Discovery pool: round-robin across seeds, excluding heard / seed artists (discover_weekly) / duplicates.
    seen: set[tuple[str, str]] = set()
    discovery_pool: list[MixTrack] = []
    cursors = [0] * len(per_seed)
    progressed = True
    while progressed:
        progressed = False
        for idx, candidates in enumerate(per_seed):
            while cursors[idx] < len(candidates):
                cand = candidates[cursors[idx]]
                cursors[idx] += 1
                if cand.key in heard or cand.key in seen:
                    continue
                if mix_type == "discover_weekly" and cand.artist.strip().lower() in seed_keys:
                    continue
                seen.add(cand.key)
                discovery_pool.append(cand)
                progressed = True
                break

    # Familiar pool
    familiar_pool: list[MixTrack] = []
    familiar_seen: set[tuple[str, str]] = set()
    allowed_artists = seed_keys | related_names
    for row in db.top_tracks(user_id, since_iso, limit=FAMILIAR_POOL_SIZE):
        artist, title = row.get("artist"), row.get("title")
        if not artist or not title:
            continue
        if is_radio and artist.strip().lower() not in allowed_artists:
            continue
        track = MixTrack(artist=artist, title=title, album=row.get("album"), origin="familiar")
        if track.key in familiar_seen:
            continue
        familiar_seen.add(track.key)
        familiar_pool.append(track)

    # Blend
    n_new = _round_half_up(track_count * ratio)
    chosen_disc = discovery_pool[:n_new]
    chosen_fam = familiar_pool[: track_count - len(chosen_disc)]
    chosen_disc = discovery_pool[: track_count - len(chosen_fam)]
    chosen_fam_set = {t.key for t in chosen_fam}
    chosen_disc = [t for t in chosen_disc if t.key not in chosen_fam_set]
    return _interleave(chosen_fam, chosen_disc)


# ---------------------------------------------------------------------------
# Generate + sync
# ---------------------------------------------------------------------------


def _queue_acquisition(
    db: Any, app_config: Any, config_row: dict[str, Any], user: dict[str, Any], track: MixTrack
) -> Optional[RequestSubmission]:
    """Submit a track request for the mix owner through the shared request policy.

    Notifications and the native grab are deferred: the caller holds ``user_request_lock`` and must call
    ``run_submission_followups`` after releasing it. Returns the submission, or None when policy refused it
    (duplicate); raises ``RequestRejected`` with
    code ``quota`` once the owner's request quota is exhausted. Requests of users without AUTO_APPROVE are PENDING.
    """
    try:
        submission = submit_track_request(
            db,
            app_config,
            user,
            track.title,
            track.artist,
            track.album,
            quality_profile_id=config_row.get("quality_profile_id"),
            source="mix",
            defer_followups=True,
        )
    except RequestRejected as exc:
        if exc.code == "duplicate":
            logger.info("Mix acquisition skipped (duplicate) for %s - %s", track.artist, track.title)
            return None
        raise
    db.add_mix_acquisition(config_row["id"], str(submission.request["id"]))
    return submission


def generate_and_sync(
    db: Any,
    plex_client: Any,
    discovery: Any,
    config_row: dict[str, Any],
    app_config: Any,
) -> TailoredMixResult:
    """Compile a mix, split by library availability, optionally queue acquisitions, sync to Plex, persist the result."""
    now = datetime.now(timezone.utc)
    mix_id = config_row["id"]
    user_id = str(config_row["user_id"])
    tracks = compile_user_mix(db, discovery, config_row, now=now)

    # The owner row comes from the DB, never from the request context.
    user = db.get_user(user_id)
    max_weekly = int(config_row.get("max_weekly_acquisitions") or 0)
    if user is not None and not user.get("is_admin"):
        max_weekly = min(max_weekly, effective_quota_limits(db, user_id)["tracks"])
    auto_acquire = bool(config_row.get("auto_acquire_missing")) and user is not None
    since_iso = (now - timedelta(days=QUOTA_WINDOW_DAYS)).isoformat(timespec="seconds")
    used = db.count_mix_acquisitions_since(user_id, since_iso)

    out_tracks: list[dict[str, Any]] = []
    available_tracks: list[MixTrack] = []
    queued = 0
    for t in tracks:
        info = get_item_availability(db, artist_name=t.artist, track_title=t.title)
        if info.get("status") in ("available", "cutoff_unmet"):
            status_label = "available"
            available_tracks.append(t)
        else:
            status_label = "missing"
            if auto_acquire and user is not None and used + queued < max_weekly:
                try:
                    submission = None
                    with user_request_lock(user_id):
                        # Re-count under the per-user lock so concurrent generations cannot overshoot the cap.
                        if db.count_mix_acquisitions_since(user_id, since_iso) < max_weekly:
                            submission = _queue_acquisition(db, app_config, config_row, user, t)
                    if submission is not None:
                        # Network follow-ups (notifications, native grab) run outside the lock.
                        run_submission_followups(db, user, submission, source="mix")
                        if submission.grabbed:
                            db.update_request_status(str(submission.request["id"]), RequestStatus.PROCESSING)
                        queued += 1
                        status_label = "queued"
                except RequestRejected as exc:
                    logger.info("Mix %s: request quota reached, no further acquisitions (%s)", mix_id, exc.detail)
                    auto_acquire = False
                except (ValueError, RuntimeError) as exc:
                    logger.error("Could not queue acquisition for %s - %s: %s", t.artist, t.title, exc)
        out_tracks.append(
            {"artist": t.artist, "title": t.title, "album": t.album, "origin": t.origin, "status": status_label}
        )

    result = TailoredMixResult(
        mix_id=mix_id,
        generated_at=now.isoformat(timespec="seconds"),
        total=len(tracks),
        available=len(available_tracks),
        missing=len(tracks) - len(available_tracks),
        acquisitions_queued=queued,
        quota_remaining=max(0, max_weekly - db.count_mix_acquisitions_since(user_id, since_iso)),
        tracks=out_tracks,
    )

    if plex_client is None:
        result.sync_error = "Plex is not configured"
    elif not user:
        result.sync_error = "Mix owner not found"
    elif not available_tracks:
        result.sync_error = "No tracks from this mix are in the library yet"
    else:
        playlist = Playlist(
            id=f"tailored-mix-{mix_id}",
            name=config_row["name"],
            description="Generated by TrackSeerr",
            tracks=[Track(title=t.title, artist=t.artist, album=t.album or "") for t in available_tracks],
        )
        try:
            sync_results = plex_client.sync_playlist_to_users(
                playlist=playlist,
                target_usernames=[user["username"]],
                append=app_config.append_instead_of_sync,
                add_description=app_config.add_playlist_description,
                add_poster=app_config.add_playlist_poster,
                write_missing_as_csv=app_config.write_missing_as_csv,
                data_dir=app_config.data_dir,
                threshold=app_config.search_similarity_threshold,
                db=db,
            )
            result.synced = any(r.success for r in sync_results)
            errors = [r.error for r in sync_results if getattr(r, "error", "")]
            if errors:
                result.sync_error = redact_text(str(errors[0]))
            elif not result.synced:
                result.sync_error = "Plex sync did not complete"
        except (NotFound, BadRequest, Unauthorized, requests.RequestException) as exc:
            # Plex/requests exception text can embed the X-Plex-Token URL: safe_exc redacts or drops it.
            logger.warning("Tailored mix %s Plex sync failed (%s)", mix_id, safe_exc(exc))
            logger.debug("Tailored mix Plex sync traceback", exc_info=True)
            result.sync_error = f"Plex sync failed ({safe_exc(exc)})"

    db.record_mix_result(mix_id, json.dumps(result.to_dict()))
    return result


__all__ = [
    "InsufficientHistoryError",
    "MixTrack",
    "TailoredMixResult",
    "compile_user_mix",
    "default_mix_name",
    "generate_and_sync",
]
