"""Pure helpers that simplify artist/title strings into progressively looser search queries.

Discover tracks often carry credit lists and featuring/remix decorations
(``Morgan Page, BT, Ned Shepard & Sultan`` / ``In the Air (Mord Fustang Remix) [feat. Angela McCluskey]``) that match
nothing when joined into one indexer query. ``build_query_ladder`` yields the raw query first and simpler fallbacks
after it; ``matches_request`` guards the fallback rungs against wrong matches.
"""

import re
from typing import Optional

from trackseerr.decision_engine import phrase_score
from trackseerr.lidarr_release import _fold, _is_noise, norm_title

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


def wildcard_word(text: str) -> Optional[str]:
    """Replace the first letter of the longest word (>= 4 letters, ties -> first) with '*'."""
    if not text:
        return None
    candidates = [m for m in re.finditer(r"[^\W_]+", text) if len(m.group(0)) >= 4]
    if not candidates:
        return None
    longest = max(candidates, key=lambda m: len(m.group(0)))
    start = longest.start()
    return f"{text[:start]}*{text[start + 1:]}"


def banned_term_queries(artist: str, title: Optional[str], album: Optional[str]) -> list[str]:
    """Ordered, case-insensitively de-duplicated query texts (max 3) for Soulseek banned terms."""
    lead = primary_artist(artist)
    effective_title = (title or "").strip()
    effective_album = (album or "").strip()

    first_source = effective_title if effective_title else effective_album
    first_text = title_core(first_source) if first_source else ""

    target_text = title_core(effective_title or effective_album)

    candidates: list[str] = []
    if len(first_text) >= 4:
        candidates.append(first_text)

    wild_lead = wildcard_word(lead)
    if wild_lead is not None:
        if target_text:
            candidates.append(f"{wild_lead} {target_text}")
        else:
            candidates.append(wild_lead)

    wild_target = wildcard_word(target_text) if target_text else None
    if wild_target is not None:
        if lead:
            candidates.append(f"{lead} {wild_target}")
        else:
            candidates.append(wild_target)

    queries: list[str] = []
    seen: set[str] = set()
    for q in candidates:
        clean = q.strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(clean)
        if len(queries) == 3:
            break
    return queries


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


_DIR_SPLIT_RE = re.compile(r"[/\\]")
_QUALITY_BRACKET_RE = re.compile(r"\s*\[[^\]]+\]\s*$")
_EXT_RE = re.compile(r"\.[a-zA-Z0-9]{2,5}$")
_TRACK_NO_RE = re.compile(r"(?:^|(?<=[\s\-_.]))(?:\d{1,2}-\d{1,2}|\d{1,2}(?:[._-](?!\d))?|\d{1,2}\b)")
_HEX_HASH_RE = re.compile(r"-[0-9a-fA-F]{6,}$")


def soulseek_file_titles(file_name: str) -> list[str]:
    """Candidate track titles extracted from a Soulseek file result name or path."""
    base = _DIR_SPLIT_RE.split(file_name)[-1].strip()
    base = _QUALITY_BRACKET_RE.sub("", base)
    base = _EXT_RE.sub("", base)
    base = _QUALITY_BRACKET_RE.sub("", base)

    name = base.replace("_", " ")
    if " " not in name:
        name = name.replace("-", " ")

    candidates: list[str] = []
    name_no_hash = _HEX_HASH_RE.sub("", name).strip()
    for s in (name, name_no_hash):
        if " - " in s:
            parts = s.split(" - ")
            candidates.append(parts[-1].strip())
            if len(parts) > 1 and _is_noise(_fold(parts[-1])):
                candidates.append(parts[-2].strip())
                candidates.append(" - ".join(parts[-2:]).strip())
        elif "-" in s:
            parts = s.split("-")
            candidates.append(parts[-1].strip())
        else:
            candidates.append(s.strip())

    m = _TRACK_NO_RE.search(name)
    if m:
        after = name[m.end():].lstrip(" ._–-").strip()
        if after:
            candidates.append(after)
            after_no_hash = _HEX_HASH_RE.sub("", after).strip()
            if after_no_hash != after:
                candidates.append(after_no_hash)
            if " - " in after_no_hash:
                parts = after_no_hash.split(" - ")
                candidates.append(parts[-1].strip())
                if len(parts) > 1 and _is_noise(_fold(parts[-1])):
                    candidates.append(parts[-2].strip())
                    candidates.append(" - ".join(parts[-2:]).strip())
            elif "-" in after_no_hash:
                candidates.append(after_no_hash.split("-")[-1].strip())

    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        c_clean = c.strip()
        if c_clean and c_clean not in seen:
            seen.add(c_clean)
            result.append(c_clean)
    return result


def soulseek_file_matches_track(file_name: str, wanted_title: str) -> bool:
    """True when any candidate from ``soulseek_file_titles`` normalises to ``wanted_title``."""
    wanted_norm = norm_title(wanted_title)
    if not wanted_norm:
        return False
    for candidate in soulseek_file_titles(file_name):
        c_norm = norm_title(candidate)
        if c_norm and c_norm == wanted_norm:
            return True
    return False
