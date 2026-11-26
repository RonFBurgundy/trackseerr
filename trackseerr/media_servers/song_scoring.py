"""Shared candidate scoring, title cleaning, and path-tail helpers for Subsonic and Jellyfin adapters."""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from trackseerr.lidarr_release import norm_title
from trackseerr.media_match import (
    artist_credits,
    norm_name,
    split_artist_credits,
    title_rank,
)
from trackseerr.models import Track

DURATION_TOLERANCE_SECONDS = 3.0
_DURATION_PENALTY = 0.15
_W_TITLE, _W_ARTIST, _W_ALBUM = 0.60, 0.35, 0.05

_VERSION_WORDS = frozenset(
    {
        "remix",
        "mix",
        "edit",
        "rework",
        "bootleg",
        "vip",
        "dub",
        "live",
        "acoustic",
        "instrumental",
        "demo",
        "unplugged",
        "karaoke",
        "acapella",
        "cappella",
        "extended",
        "slowed",
        "sped",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_CLEAN_TAG_RE = re.compile(
    r"[\(\[](.*?)(remaster|remix|feat|ft\.|live|deluxe|version|edition|mono|stereo|anniversary)(.*?)[\)\]]",
    re.IGNORECASE,
)
_CLEAN_TRAIL_RE = re.compile(
    r"\s*-\s*(remastered|remaster|live|deluxe|radio edit|mono|stereo).*$",
    re.IGNORECASE,
)
_CLEAN_BRACKET_RE = re.compile(r"[\(\[].*?[\)\]]")


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def clean_title(title: str) -> str:
    """Strip remaster info, feature tags, and bracketed metadata for fallback search."""
    cleaned = _CLEAN_TAG_RE.sub("", title or "")
    cleaned = _CLEAN_TRAIL_RE.sub("", cleaned)
    cleaned = _CLEAN_BRACKET_RE.sub("", cleaned)
    return cleaned.strip()


def _norm_title_for_compare(title: str) -> str:
    folded = norm_title((title or "").replace("$", "s"))
    return folded or norm_name(title)


def version_words(title: str) -> tuple[str, ...]:
    """Sorted multiset of identity-changing version words in the noise-stripped title."""
    normed = norm_title((title or "").replace("$", "s"))
    words = [w for w in _WORD_RE.findall(normed) if w in _VERSION_WORDS]
    return tuple(sorted(words))


def artist_forms(name: str) -> list[str]:
    """Normalized full artist name plus every normalized credited artist."""
    if not name:
        return []
    full = norm_name(name)
    credits = artist_credits(name)
    return list(dict.fromkeys(v for v in (full, *credits) if v))


def first_artist_query(artist: str) -> str:
    """First credited artist normalized for media-server search queries."""
    credits = artist_credits(artist)
    if credits:
        return credits[0]
    return norm_name(artist)


def artist_query_credits(artist: str, limit: int = 4) -> list[str]:
    """Distinct credited artist strings (normalized and raw) suitable for search queries."""
    raw_credits = split_artist_credits(artist)
    out: list[str] = []
    for credit in raw_credits:
        normed = norm_name(credit)
        if normed:
            out.append(normed)
        stripped = re.sub(r"^the\s+", "", credit.strip(), flags=re.IGNORECASE).replace("$", "s").strip()
        if stripped and stripped.lower() != normed:
            out.append(stripped)
    return list(dict.fromkeys(out))[:limit]


def tail_two_components(path: str) -> tuple[str, str] | None:
    """Return the last two path components (album folder, file name) after normalising separators."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        return None
    return (parts[-2], parts[-1])


def score_candidate(
    wanted_title: str,
    wanted_artist: str,
    wanted_album: str,
    wanted_duration: float | None,
    cand_title: str,
    cand_artists: Sequence[str],
    cand_album: str = "",
    cand_duration: float | None = None,
) -> float:
    """Similarity of a library song to the wanted track in ``[0, 1]``.

    A candidate whose title adds or drops an identity-changing version word (``remix``, ``live``, ``acoustic``, ...)
    scores ``0.0`` so a remix or live cut never beats an absent studio version.
    """
    if version_words(wanted_title) != version_words(cand_title):
        return 0.0
    title = _ratio(_norm_title_for_compare(wanted_title), _norm_title_for_compare(cand_title))
    wanted_forms = artist_forms(wanted_artist)
    have_forms = [v for name in cand_artists for v in artist_forms(name)]
    artist = max((_ratio(w, h) for w in wanted_forms for h in have_forms), default=0.0)
    score = _W_TITLE * title + _W_ARTIST * artist
    weight = _W_TITLE + _W_ARTIST
    norm_wanted_album = norm_name(wanted_album)
    norm_cand_album = norm_name(cand_album)
    if norm_wanted_album and norm_cand_album:
        score += _W_ALBUM * _ratio(norm_wanted_album, norm_cand_album)
        weight += _W_ALBUM
    score /= weight
    if (
        wanted_duration
        and cand_duration
        and abs(wanted_duration - cand_duration) > DURATION_TOLERANCE_SECONDS
    ):
        score -= _DURATION_PENALTY
    return max(0.0, min(1.0, score))


def candidate_rank_key(track: Track, cand_title: str, score: float) -> tuple[float, int]:
    """Sort/comparison key preferring higher score, breaking ties by ``title_rank``."""
    return (round(score, 9), -title_rank(cand_title, track.title))


def best_scored_match(
    track: Track,
    candidates: Sequence[Any],
    threshold: float,
    *,
    censored: bool = False,
) -> tuple[Any | None, float, tuple[float, int]]:
    """Return ``(best_song, best_score, best_key)`` across ``candidates`` (``best_song`` may be below threshold)."""
    best: Any | None = None
    best_score = 0.0
    best_key = (0.0, -3)
    for song in candidates:
        wanted_title = song.title if censored else track.title
        if censored and version_words(track.title) != version_words(song.title):
            continue
        score = score_candidate(
            wanted_title,
            track.artist,
            track.album,
            track.duration_seconds,
            song.title,
            song.artists,
            song.album,
            song.duration,
        )
        key = candidate_rank_key(track, song.title, score)
        if key > best_key:
            best, best_score, best_key = song, score, key
    if best is not None and best_score + 1e-9 >= threshold:
        return best, best_score, best_key
    return best, best_score, best_key
