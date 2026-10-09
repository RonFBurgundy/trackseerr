"""Pure release-selection rules for Lidarr song and album requests.

Lidarr's own album list already reflects the user's metadata profile, so every rule here only ever chooses among
albums Lidarr knows about; nothing here adds or edits a profile.

Titles are compared by exact equality of their normalised form. Normalisation folds case, accents, ``&`` and
punctuation and drops qualifiers that do not change which recording it is (remaster years, radio edit, explicit,
featuring credits, ...). Qualifiers that do change it (live, instrumental, remix, acoustic, demo, ...) are kept, so
"Song (Live)" never matches the studio "Song".
"""

import re
import unicodedata
from typing import Any, Iterable, Optional

_FAR_FUTURE = "9999-99-99"
_BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")
_APOSTROPHES = re.compile("['‘’ʼ`´]")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_FEATURING = re.compile(r"^(?:feat|ft|featuring|with)\b\.?\s*\S")
_TRAILING_FEAT = re.compile(r"\s+(?:feat|ft|featuring)\b\.?\s+.*$")
# One non-identity qualifier (already casefolded and squashed to single-spaced words).
_NOISE = re.compile(
    r"^(?:"
    r"(?:\d{4}\s+)?(?:digital\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?"
    r"|radio\s+edit"
    r"|single\s+version"
    r"|album\s+version"
    r"|(?:mono|stereo|explicit|clean|bonus\s+track)(?:\s+version)?"
    r"|deluxe(?:\s+(?:edition|version))?"
    r")$"
)


def _squash(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text).split())


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "")).casefold().replace("&", " and ")
    raw = _APOSTROPHES.sub("", raw)  # "Don't" and "Dont" are one word
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


def _is_noise(qualifier: str) -> bool:
    """True when ``qualifier`` (free text from a bracket or a " - " suffix) does not change the recording."""
    words = _squash(qualifier)
    if not words:
        return True
    if _FEATURING.match(words):
        return True
    parts = [_squash(p) for p in re.split(r"[,/;]", qualifier)]
    return all(p and _NOISE.match(p) for p in parts)


def norm_title(text: Any) -> str:
    """The comparison form of a title (see the module docstring)."""
    folded = _fold(text)

    def _bracket(match: "re.Match[str]") -> str:
        inner = match.group(1)
        return " " if _is_noise(inner) else f" {inner} "

    folded = _BRACKETED.sub(_bracket, folded)
    parts = _DASH_SPLIT.split(folded)
    while len(parts) > 1 and _is_noise(parts[-1]):
        parts.pop()
    folded = " - ".join(parts)
    folded = _TRAILING_FEAT.sub("", folded)
    return _squash(folded)


def titles_match(left: Any, right: Any) -> bool:
    """Exact equality of the normalised titles (two empty titles never match)."""
    a = norm_title(left)
    return bool(a) and a == norm_title(right)


def _album_type(album: dict[str, Any]) -> str:
    return str(album.get("albumType") or "").strip().casefold()


def _release_key(album: dict[str, Any]) -> tuple[str, int]:
    return (str(album.get("releaseDate") or _FAR_FUTURE), int(album.get("id") or 0))


def earliest(albums: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    ordered = sorted(albums, key=_release_key)
    return ordered[0] if ordered else None


def match_named_album(albums: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    """The album Lidarr lists under ``name``: exact normalised title only, never a partial match.

    Among equal matches the one spelled exactly like ``name`` wins, then the earliest release.
    """
    exact = [a for a in albums if titles_match(a.get("title"), name)]
    spelled = [a for a in exact if str(a.get("title") or "").strip().casefold() == str(name or "").strip().casefold()]
    return earliest(spelled or exact)


def albums_containing_song(
    tracks: list[dict[str, Any]], albums: list[dict[str, Any]], song_title: str
) -> list[dict[str, Any]]:
    """Albums (from Lidarr's own list) holding a track whose title matches ``song_title``."""
    album_ids = {
        int(t["albumId"])
        for t in tracks
        if t.get("albumId") is not None and titles_match(t.get("title"), song_title)
    }
    return [a for a in albums if a.get("id") is not None and int(a["id"]) in album_ids]


def select_release_for_song(
    candidates: list[dict[str, Any]], song_title: str, album_hint: str = "", *, prefer_singles: bool = True
) -> Optional[dict[str, Any]]:
    """Picks the release to monitor for a song from ``candidates``, albums that all contain it.

    Order: a Single titled like the song; the album the request named (a tie-breaker hint, honoured only because
    it is among the candidates); the earliest Album; the earliest EP; anything else. ``None`` when there are no
    candidates, in which case the caller falls back to a song search rather than monitoring an unrelated album.

    With ``prefer_singles=False`` the matching Single is no longer tried first: the order is the requested album, the
    earliest Album, the earliest EP, a Single titled like the song, then anything else.
    """
    singles = [a for a in candidates if _album_type(a) == "single" and titles_match(a.get("title"), song_title)]
    if prefer_singles and singles:
        return earliest(singles)
    if album_hint.strip():
        hinted = [a for a in candidates if titles_match(a.get("title"), album_hint)]
        if hinted:
            return earliest(hinted)
    for kind in ("album", "ep"):
        group = [a for a in candidates if _album_type(a) == kind]
        if group:
            return earliest(group)
    if singles:
        return earliest(singles)
    return earliest(candidates)
