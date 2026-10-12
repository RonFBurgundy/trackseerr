"""Per-track artist credits: MusicBrainz artist-credit parsing and remixer detection from titles (pure, no DB)."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FEAT_RE = re.compile(r"\b(feat|ft|featuring)\b\.?", re.IGNORECASE)
_TRAILING_GROUP_RE = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]\s*$")
_REMIX_WORDS = frozenset({"remix", "bootleg", "flip", "edit", "rework", "mix", "refix", "re-edit", "dub"})
_GENERIC_WORDS = frozenset(
    {
        "extended", "radio", "club", "original", "album", "single", "main", "vocal", "instrumental",
        "dub", "short", "long", "clean", "dirty",
    }
)
_NAME_SPLIT_RE = re.compile(r"\s+&\s+|\s+x\s+|,\s+", re.IGNORECASE)


def credits_from_mb_artist_credit(artist_credit: Any) -> list[dict[str, Any]]:
    """Turns a MusicBrainz ``artist-credit`` array into credit dicts (no ``position``; the store assigns it).

    The first entry is ``primary``. Once a join phrase containing feat/ft./featuring has been seen, every later
    entry is ``featured`` ("A feat. B & C" features both B and C); otherwise entries are ``primary``.
    """
    if not isinstance(artist_credit, list):
        return []
    credits: list[dict[str, Any]] = []
    featured = False
    for entry in artist_credit:
        if not isinstance(entry, dict):
            continue
        artist = entry.get("artist") if isinstance(entry.get("artist"), dict) else {}
        name = str(entry.get("name") or artist.get("name") or "").strip()
        if not name:
            continue
        join_phrase = str(entry.get("joinphrase") or "")
        mbid = str(artist.get("id") or "").strip() or None
        credits.append(
            {
                "artist_name": name,
                "artist_mbid": mbid,
                "role": "featured" if featured else "primary",
                "join_phrase": join_phrase,
                "source": "musicbrainz",
            }
        )
        if _FEAT_RE.search(join_phrase):
            featured = True
    return credits


def remixers_from_title(title: str) -> list[str]:
    """Remixer names from a trailing "(X Remix)" style bracket group, or [] when unsure (never guesses)."""
    match = _TRAILING_GROUP_RE.search(title or "")
    if not match:
        return []
    group = match.group(1).strip()
    words = group.split()
    if len(words) < 2 or words[-1].lower().strip(".") not in _REMIX_WORDS:
        return []
    names_text = group[: len(group) - len(words[-1])].strip()
    lowered = [w.lower().strip(".,") for w in names_text.split()]
    if not lowered or "vip" in lowered or any(w in _GENERIC_WORDS for w in lowered):
        return []
    return [part.strip() for part in _NAME_SPLIT_RE.split(names_text) if part.strip()]


def build_track_credits(
    mb_artist_credit: Any,
    title: str,
    tag_artist: Optional[str] = None,
    tag_artist_mbid: Optional[str] = None,
) -> list[dict[str, Any]]:
    """MusicBrainz credit (else the tag artist) followed by remixers parsed from the title."""
    credits = credits_from_mb_artist_credit(mb_artist_credit)
    if not credits and tag_artist and tag_artist.strip():
        credits.append(
            {
                "artist_name": tag_artist.strip(),
                "artist_mbid": (tag_artist_mbid or "").strip() or None,
                "role": "primary",
                "join_phrase": "",
                "source": "tags",
            }
        )
    known = {str(c["artist_name"]).casefold() for c in credits}
    for name in remixers_from_title(title):
        if name.casefold() in known:
            continue
        known.add(name.casefold())
        credits.append(
            {"artist_name": name, "artist_mbid": None, "role": "remixer", "join_phrase": "", "source": "title"}
        )
    return credits


def store_track_credits(db: Any, track_id: str, track: dict[str, Any]) -> None:
    """Persists the credits of a fetched MusicBrainz track when it carries an artist-credit; logs DB failures."""
    if not track.get("artist_credit"):
        return
    credits = build_track_credits(track.get("artist_credit"), str(track.get("title") or ""))
    if not credits:
        return
    try:
        db.replace_track_artists(track_id, credits)
    except sqlite3.Error as exc:
        logger.warning("Storing artist credits for track %s failed: %s", track_id, exc)
