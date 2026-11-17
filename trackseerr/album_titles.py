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


def album_titles_match(a: Any, b: Any) -> bool:
    """Compare two album titles after stripping album-specific trailing qualifiers."""
    left = strip_album_junk(a) if isinstance(a, str) else a
    right = strip_album_junk(b) if isinstance(b, str) else b
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
