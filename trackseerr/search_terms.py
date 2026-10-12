"""Pure helpers that simplify artist/title strings into progressively looser search queries.

Discover tracks often carry credit lists and featuring/remix decorations
(``Morgan Page, BT, Ned Shepard & Sultan`` / ``In the Air (Mord Fustang Remix) [feat. Angela McCluskey]``) that match
nothing when joined into one indexer query. ``build_query_ladder`` yields the raw query first and simpler fallbacks
after it; ``matches_request`` guards the fallback rungs against wrong matches.
"""

import re
from typing import Optional

from trackseerr.decision_engine import phrase_score

_VERSION_KEYWORDS = (
    "remix", "mix", "edit", "version", "rework", "bootleg", "vip", "dub", "live", "acoustic", "remaster",
)
_VERSION_RE = re.compile(r"\b(?:" + "|".join(_VERSION_KEYWORDS) + r")\b", re.IGNORECASE)
_GROUP_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_FEAT_GROUP_RE = re.compile(r"\s*[\(\[]\s*(?:(?:feat|ft)\.?|featuring)\s[^\)\]]*[\)\]]", re.IGNORECASE)
_FEAT_TRAIL_RE = re.compile(r"\s+(?:(?:feat|ft)\.?|featuring)\s+.*$", re.IGNORECASE)
_ARTIST_SPLIT_RE = re.compile(
    r"\s*,\s*|\s+(?:&|and|x|vs\.?|(?:feat|ft)\.?|featuring)(?:\s+|$)", re.IGNORECASE
)
_ANY_GROUP_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_TRAILING_DASH_RE = re.compile(r"\s+-\s+.*$")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_RELEVANCE = 80

Rung = tuple[str, Optional[str], Optional[str]]


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def primary_artist(artist: str) -> str:
    """First artist of a credit list (``A, B & C`` -> ``A``); a single artist is returned stripped."""
    text = (artist or "").strip()
    first = _ARTIST_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return first or text


def strip_featuring(title: str) -> str:
    """Remove ``(feat. ...)``/``[ft. ...]`` groups and a trailing un-bracketed ``feat. ...`` clause."""
    text = _FEAT_GROUP_RE.sub("", title or "")
    text = _FEAT_TRAIL_RE.sub("", text)
    return _collapse(text)


def _version_groups(title: str) -> list[str]:
    return [g.strip() for g in _GROUP_RE.findall(title or "") if _VERSION_RE.search(g)]


def version_descriptor(title: str) -> Optional[str]:
    """Text of the first bracket group naming a version (remix, edit, live, ...), or None."""
    groups = _version_groups(strip_featuring(title))
    return groups[0] if groups else None


def title_core(title: str) -> str:
    """Title with featuring credits, every bracket group and any trailing `` - suffix`` removed."""
    text = _ANY_GROUP_RE.sub("", strip_featuring(title))
    text = _TRAILING_DASH_RE.sub("", text)
    return _collapse(text)


def _simplify(text: str) -> str:
    return _collapse(_PUNCT_RE.sub(" ", text))


def _core_with_descriptor(text: str) -> str:
    core = title_core(text)
    descriptor = version_descriptor(text)
    return _simplify(f"{core} {descriptor}" if descriptor else core)


def build_query_ladder(artist: str, title: Optional[str], album: Optional[str]) -> list[Rung]:
    """Ordered, case-insensitively de-duplicated ``(artist, title, album)`` rungs (max 3), raw query first."""
    lead = primary_artist(artist)
    if title:
        candidates: list[Rung] = [
            (artist, title, album),
            (lead, strip_featuring(title), album),
            (lead, _core_with_descriptor(title), album),
        ]
    else:
        album_text = album or ""
        candidates = [
            (artist, title, album),
            (lead, None, strip_featuring(album_text) if album else album),
            (lead, None, _core_with_descriptor(album_text) if album else album),
        ]
    rungs: list[Rung] = []
    seen: set[tuple[str, str, str]] = set()
    for a, t, al in candidates:
        key = ((a or "").lower(), (t or "").lower(), (al or "").lower())
        if key in seen:
            continue
        seen.add(key)
        rungs.append((a, t, al))
    return rungs[:3]


def matches_request(
    candidate_text: str,
    artist: str,
    title: Optional[str],
    album: Optional[str],
    title_text: Optional[str] = None,
) -> bool:
    """Relevance gate for fallback rungs: the candidate must name the artist, contain the title core, and any requested remixer.

    ``candidate_text`` is checked for the artist; the title/remixer checks use ``title_text`` when given (e.g. a
    Soulseek file name, so an album folder in the path cannot satisfy the title) and ``candidate_text`` otherwise.
    """
    candidate_title = title_text if title_text is not None else candidate_text
    if (artist or "").strip() and not (
        phrase_score(primary_artist(artist), candidate_text) >= _MIN_RELEVANCE
        or phrase_score(artist, candidate_text) >= _MIN_RELEVANCE
    ):
        return False
    source = title or album or ""
    target = title_core(source)
    if not target or phrase_score(target, candidate_title) < _MIN_RELEVANCE:
        return False
    descriptor = version_descriptor(source)
    candidate_lower = (candidate_title or "").lower()
    if descriptor is not None:
        words = [w for w in _WORD_RE.findall(descriptor.lower()) if not _VERSION_RE.fullmatch(w)]
        cand_words = set(_WORD_RE.findall(candidate_lower))
        return all(w in cand_words for w in words)
    return not any("remix" in g.lower() for g in _version_groups(candidate_title))
