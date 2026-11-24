"""Album-specific title cleaning and comparison helpers."""

from __future__ import annotations

import re
from typing import Any

from trackseerr.lidarr_release import titles_match

_FORMAT_WORDS = r"(?:remaster(?:ed)?|sacd|shm|flac|mp3|320|24\s*bit|16\s*bit|hi-?res|vinyl|rip|web)"

_TRAILING_JUNK = re.compile(
    r"(?:"
    r"\s*[\(\[]\s*\d{4}\s*[\)\]]"
    r"|\s+[-–—]\s*(?:single|ep)"
    r"|\s*[\(\[]\s*(?:single|ep)\s*[\)\]]"
    r"|\s*[\(\[]\s*(?:disc|cd)\s*\d+\s*[\)\]]"
    r"|\s*[\(\[]\s*(?:\d{4}[\s/-]+)*" + _FORMAT_WORDS + r"(?:[\s/-]+(?:\d{4}|" + _FORMAT_WORDS + r"))*\s*[\)\]]"
    r")\s*$",
    re.IGNORECASE,
)

_TRAILING_SINGLE_EP = re.compile(r"\s+[-–—]\s*(?:single|ep)\s*$", re.IGNORECASE)
_TRAILING_YEAR = re.compile(r"\s*[\(\[]\s*(\d{4})\s*[\)\]]\s*$")
_PLACEHOLDER_ALBUM_TITLES = frozenset({"", "unknown album", "unknown"})


def is_placeholder_album_title(title: object) -> bool:
    """Return True when title is empty or a generic placeholder like 'Unknown Album'."""
    cleaned = _TRAILING_YEAR.sub("", str(title or "").strip()).strip()
    return cleaned.casefold() in _PLACEHOLDER_ALBUM_TITLES

_EDITION_WORDS = frozenset(
    {
        "deluxe",
        "super",
        "edition",
        "expanded",
        "anniversary",
        "remaster",
        "remastered",
        "the",
        "special",
        "collector's",
        "collectors",
        "collector’s",
        "legacy",
        "reissue",
        "bonus",
        "version",
        "hi-res",
        "hires",
        "mono",
        "stereo",
        "udsacd",
        "sacd",
        "shm",
        "mfsl",
        "original",
        "album",
        "track",
        "tracks",
    }
)
_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)$")
_YEAR_RE = re.compile(r"^\d{4}$")
_TOKEN_SPLIT_RE = re.compile(r"[\s;,&/]+")
_TRAILING_BRACKET_GROUP_RE = re.compile(r"\s*(?:\(([^()]*)\)|\[([^\[\]]*)\])\s*$")
_TRAILING_DASH_SUFFIX_RE = re.compile(r"\s+[-–—]\s+(.+?)\s*$")


def _is_edition_token(token: str) -> bool:
    if token in _EDITION_WORDS or _ORDINAL_RE.match(token) or _YEAR_RE.match(token):
        return True
    if "-" in token:
        parts = [p for p in token.split("-") if p]
        return bool(parts) and all(_is_edition_token(p) for p in parts)
    return False


def _is_edition_phrase(phrase: str) -> bool:
    tokens = [tok.lower() for tok in _TOKEN_SPLIT_RE.split(phrase.strip()) if tok]
    return bool(tokens) and all(_is_edition_token(tok) for tok in tokens)


def strip_album_junk(title: str) -> str:
    """Repeatedly strip trailing year, single/EP, disc, and format/remaster tags from an album title."""
    if not isinstance(title, str) or not title:
        return ""
    cur = title.strip()
    if not cur:
        return title
    while True:
        nxt = _TRAILING_JUNK.sub("", cur).strip()
        if not nxt or nxt == cur:
            break
        cur = nxt
    return cur


def strip_edition(title: str) -> str:
    """Strip trailing album junk and edition qualifiers without ever returning an empty string."""
    if not isinstance(title, str) or not title:
        return ""
    original_stripped = title.strip()
    if not original_stripped:
        return title
    cur = strip_album_junk(original_stripped)
    while True:
        match = _TRAILING_BRACKET_GROUP_RE.search(cur)
        phrase = (match.group(1) if match.group(1) is not None else match.group(2)) if match else None
        if match is None:
            match = _TRAILING_DASH_SUFFIX_RE.search(cur)
            phrase = match.group(1) if match else None
        if match is None or phrase is None or not _is_edition_phrase(phrase):
            break
        nxt = cur[: match.start()].strip()
        if not nxt:
            break
        cur = nxt
    cur = strip_album_junk(cur)
    return cur or original_stripped


def album_titles_match(a: Any, b: Any) -> bool:
    """Compare two album titles after stripping album-specific trailing qualifiers and edition tags."""
    left = strip_edition(a) if isinstance(a, str) else a
    right = strip_edition(b) if isinstance(b, str) else b
    return titles_match(left, right)


def clean_new_album_title(title: str, year: int | None) -> str:
    """Strip a trailing '(YYYY)'/'[YYYY]' (only when equal to ``year``) and trailing ' - Single'/' - EP'."""
    if not isinstance(title, str) or not title:
        return ""
    target_year: int | None = None
    if year is not None:
        try:
            target_year = int(year)
        except (TypeError, ValueError):
            target_year = None

    cur = title.strip()
    if not cur:
        return title
    while True:
        nxt = _TRAILING_SINGLE_EP.sub("", cur).strip()
        if nxt and nxt != cur:
            cur = nxt
            continue
        if target_year is not None:
            match = _TRAILING_YEAR.search(cur)
            if match and int(match.group(1)) == target_year:
                candidate = cur[: match.start()].strip()
                if candidate:
                    cur = candidate
                    continue
        break
    return cur
