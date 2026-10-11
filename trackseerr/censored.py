"""Matching helpers for titles whose explicit words are masked with asterisk runs (Spotify embed: "F**k")."""

from __future__ import annotations

import re
from typing import Optional

from trackseerr.storage_common import clean_library_name

CENSOR_RUN = re.compile(r"\*{2,}")


def is_censored(title: str) -> bool:
    """True when the title contains a run of two or more asterisks."""
    return bool(title and CENSOR_RUN.search(title))


def _token_pattern(token: str) -> str:
    """Regex for one whitespace-delimited raw token ('' when nothing of it survives cleaning)."""
    if not CENSOR_RUN.search(token):
        return re.escape(clean_library_name(token))
    parts = [clean_library_name(p) for p in CENSOR_RUN.split(token)]
    literals = [p for p in parts if p]
    if not literals:
        return r"\S+"
    # A mask glued to letters may stand for zero or more characters; a standalone mask is one whole word.
    return r"\S*".join(re.escape(p) for p in parts)


def censored_pattern(title: str) -> Optional["re.Pattern[str]"]:
    """Pattern over the clean_library_name form of an uncensored title, or None when not censored/matchable."""
    if not is_censored(title):
        return None
    pieces = [_token_pattern(tok) for tok in title.split()]
    pieces = [p for p in pieces if p]
    if not any(p != r"\S+" for p in pieces):
        return None
    return re.compile("^" + " ".join(pieces) + "$")


def search_text(title: str) -> str:
    """The longest uncensored stretch of the title, for media-server search queries."""
    segments = [s.strip() for s in CENSOR_RUN.split(title or "")]
    return max(segments, key=len, default="")
