"""Pure helpers for matching tracks and artist credits across music services."""

import re
import unicodedata
from difflib import SequenceMatcher

from trackseerr.search_terms import title_core, version_descriptor

_DOLLAR_RE = re.compile(r"\$")
_NON_ALPHANUM_RE = re.compile(r"[^a-z0-9]+")
_LEADING_THE_RE = re.compile(r"^the\s+")
_SPLIT_CREDITS_RE = re.compile(
    r"\s*(?:[;,/&]|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\band\b|\bx\b)\s*",
    re.IGNORECASE,
)


def norm_name(text: str) -> str:
    """NFKD-fold accents to ASCII, lowercase, $->s, strip leading 'the ', collapse non-alphanumeric to spaces."""
    if not text:
        return ""
    # NFKD-fold accents to ASCII
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lowered = folded.lower()
    # $ -> s
    dollar_subbed = _DOLLAR_RE.sub("s", lowered).strip()
    # strip leading "the "
    no_the = _LEADING_THE_RE.sub("", dollar_subbed)
    # replace every non-alphanumeric run with one space, collapse/strip
    cleaned = _NON_ALPHANUM_RE.sub(" ", no_the)
    return " ".join(cleaned.split())


def artist_credits(text: str) -> list[str]:
    """Split artist credit string on common delimiters and normalize parts."""
    if not text:
        return []
    parts = _SPLIT_CREDITS_RE.split(text)
    credits: list[str] = []
    for part in parts:
        normed = norm_name(part)
        if normed:
            credits.append(normed)
    return credits


def artist_matches(candidate: str, target: str, threshold: float = 0.9) -> bool:
    """True when ratio >= threshold or any credit of target equals any credit of candidate."""
    if not candidate or not target:
        return False
    norm_cand = norm_name(candidate)
    norm_tgt = norm_name(target)
    if not norm_cand or not norm_tgt:
        return False

    ratio = SequenceMatcher(None, norm_cand, norm_tgt).ratio()
    if ratio >= threshold:
        return True

    target_credits = artist_credits(target)
    candidate_credits = artist_credits(candidate)
    for tc in target_credits:
        if tc in candidate_credits:
            return True

    return False


def title_rank(candidate_title: str, target_title: str) -> int:
    """Rank candidate title: 0 for exact normalized match, 1 for title_core match, 2 otherwise."""
    norm_cand = norm_name(candidate_title)
    norm_tgt = norm_name(target_title)
    if norm_cand and norm_tgt and norm_cand == norm_tgt:
        return 0

    # If target didn't specify a version/remix descriptor, but candidate is a remix/version,
    # it shouldn't rank as core match (rank 1)
    cand_desc = version_descriptor(candidate_title)
    tgt_desc = version_descriptor(target_title)
    if cand_desc != tgt_desc:
        return 2

    core_cand = norm_name(title_core(candidate_title))
    core_tgt = norm_name(title_core(target_title))
    if core_cand and core_tgt and core_cand == core_tgt:
        return 1

    return 2
