"""Seed data and shape helpers for the Arr-style quality system (docs/design/ARR_PROFILES_SPEC.md, docs/design/QUALITY_RESEARCH.md).

Pure data and pure functions: no database or engine imports, so ``storage`` (migration v49), the decision engine and
the API can all share one definition of the defaults.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# Canonical quality order, best first (used to order quality definitions and group members in the UI).
# Lossless (FLAC 24, FLAC 16, ALAC, WAV/AIFF) sits above lossy; lossy runs 320 > V0 > V1 > AAC 256 > Opus > OGG >
# AAC other > 192 > V2. New quality profiles list their entries in this order.
QUALITY_ORDER: list[str] = [
    "FLAC 24bit",
    "FLAC 16bit",
    "ALAC",
    "WAV/AIFF",
    "MP3 320",
    "MP3 V0",
    "MP3 V1",
    "AAC 256",
    "Opus",
    "OGG Vorbis",
    "AAC (other)",
    "MP3 192",
    "MP3 V2",
    "Unknown",
]

# Qualities added in migration v51 (appended disallowed to existing profiles).
V51_NEW_QUALITIES: list[str] = ["ALAC", "WAV/AIFF", "MP3 V1", "AAC (other)", "Opus", "OGG Vorbis"]

# What each v51 quality parsed as before v51 (quality.py at c4a8250): ALAC matched the FLAC 16bit pattern, the rest had no
# pattern (or, for AAC below 256 kbps, was demoted) and parsed as Unknown. v51 gives each the old target's allowed flag.
V51_OLD_PARSE_TARGETS: dict[str, str] = {
    "ALAC": "FLAC 16bit",
    "WAV/AIFF": "Unknown",
    "MP3 V1": "Unknown",
    "AAC (other)": "Unknown",
    "Opus": "Unknown",
    "OGG Vorbis": "Unknown",
}

# Seeded defaults that migration v52 widened (quality -> (min, preferred, max) before v52). Only rows still equal to
# these are bumped, so user edits survive.
V52_OLD_DEFAULTS: dict[str, tuple[float, float, float]] = {
    "MP3 320": (290.0, 320.0, 350.0),
    "MP3 V0": (160.0, 245.0, 350.0),
}

# (quality, title, min_kbps, preferred_kbps, max_kbps); None = unbounded.
DEFAULT_QUALITY_DEFINITIONS: list[tuple[str, str, Optional[float], Optional[float], Optional[float]]] = [
    ("FLAC 24bit", "FLAC 24bit", 0.0, 2000.0, 9500.0),
    ("FLAC 16bit", "FLAC 16bit", 0.0, 895.0, 1400.0),
    # Derived (docs/design/QUALITY_RESEARCH.md), not community standards: ALAC compresses 16/44.1 PCM (1411 kbps) to roughly
    # 50-70% (~700-1000 kbps), so 900 preferred, no floor (quiet or sparse material compresses far below that) and a
    # 1600 ceiling that also admits 16/48 and light 24-bit.
    ("ALAC", "ALAC", 0.0, 900.0, 1600.0),
    # Uncompressed PCM is exact arithmetic: 44.1 kHz x 16 bit x 2 ch = 1411 kbps. The floor of 1300 rejects anything
    # that is not full-rate stereo PCM; 5000 allows up to roughly 24/96 stereo (4608 kbps).
    ("WAV/AIFF", "WAV/AIFF", 1300.0, 1411.0, 5000.0),
    ("MP3 320", "MP3 320", 290.0, 320.0, 400.0),
    ("MP3 V0", "MP3 V0", 160.0, 245.0, 400.0),
    # LAME V1 averages ~225 kbps (range ~190-250), between V0 (245) and V2 (190); the ceiling is shared with 320.
    ("MP3 V1", "MP3 V1", 150.0, 225.0, 320.0),
    ("AAC 256", "AAC 256", 200.0, 256.0, 280.0),
    # Opus is transparent around 128-160 kbps, so 160 preferred; 64 is the floor for music, 256 above the useful range.
    ("Opus", "Opus", 64.0, 160.0, 256.0),
    # Vorbis q5-q8 span ~160-256 kbps with 320 as q10; 192 (q6) is a common archive choice.
    ("OGG Vorbis", "OGG Vorbis", 96.0, 192.0, 320.0),
    # Any other AAC (VBR/LC at lower rates up to 320): 192 preferred, sub-96 is unusable for music.
    ("AAC (other)", "AAC (other)", 96.0, 192.0, 320.0),
    ("MP3 192", "MP3 192", 150.0, 192.0, 210.0),
    ("MP3 V2", "MP3 V2", 130.0, 190.0, 280.0),
    ("Unknown", "Unknown", 0.0, 195.0, 350.0),
]

PREFERRED_GROUPS = ["DeVOiD", "PERFECT", "ENRiCH", "BigFLAC", "GalaxyLossless", "MusiCHI", "LoRD"]


def _title_spec(name: str, pattern: str) -> dict[str, Any]:
    return {
        "name": name,
        "implementation": "ReleaseTitleSpecification",
        "negate": False,
        "required": False,
        "fields": {"value": pattern},
    }


# (name, specifications, default score)
DEFAULT_CUSTOM_FORMATS: list[tuple[str, list[dict[str, Any]], int]] = [
    (
        "Preferred Groups",
        [
            {
                "name": g,
                "implementation": "ReleaseGroupSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": rf"\b{g}\b"},
            }
            for g in PREFERRED_GROUPS
        ],
        100,
    ),
    ("CD", [_title_spec("CD", r"\bCD(?:DA|-?Rip)?\b")], 10),
    ("Lossless", [_title_spec("Lossless", r"\b(?:FLAC|Lossless|ALAC|APE|WavPack)\b")], 10),
    ("Hi-Res 24bit", [_title_spec("Hi-Res 24bit", r"24.?bit|Hi.?Res")], 15),
    ("WEB", [_title_spec("WEB", r"\bWEB\b")], 5),
    ("Vinyl", [_title_spec("Vinyl", r"\bVinyl\b")], -50),
    ("Mono", [_title_spec("Mono", r"\bMono\b")], -10),
    ("Censored/Clean", [_title_spec("Censored/Clean", r"\b(?:Censored|Clean)\b")], -15),
    ("Remastered", [_title_spec("Remastered", r"[Rr]emaster(?:ed)?")], 0),
    ("Deluxe", [_title_spec("Deluxe", r"[Dd]eluxe")], 0),
]

DEFAULT_RELEASE_PROFILE: dict[str, Any] = {
    "name": "Reject bad sources",
    "enabled": True,
    "required": [],
    "ignored": [
        r"/transcode(?:d)?/",
        r"/up[-_ ]?(?:conver\w+|sampl\w+)/",
        r"/fake[-_ .]?flac/",
        r"/lossy[-_ ]?(?:master|web|flac)/",
        r"/\bMQA\b/",
    ],
}

# Legacy sort for tags that the old engine matched through ``parse_release_title``'s tag and source detectors.
_LEGACY_TAG_PATTERNS: dict[str, str] = {
    "remaster": r"remaster(?:ed)?",
    "remastered": r"remastered",
    "deluxe": r"deluxe",
    "live": r"live",
    "bootleg": r"bootleg",
    "tribute": r"tribute",
    "instrumental": r"instrumental",
    "karaoke": r"karaoke",
    "clean edit": r"clean(?:\s+edit)?",
    "explicit": r"explicit",
    "bonus": r"bonus",
}
_LEGACY_SOURCE_PATTERNS: dict[str, str] = {
    "web": r"web(?:-?dl|-?rip)?|qobuz|tidal|deezer|itunes|bandcamp|amazon(?:-hd)?",
    "cd": r"cd|cdda|cd-?rip|retail",
    "vinyl": r"vinyl|lp|12-inch|record",
    "sacd": r"sacd|dsd(?:64|128|256)?",
    "cassette": r"cassette|tape",
}


def legacy_tag_regex(tag: str) -> str:
    """Regex that matches a legacy preferred/ignored tag exactly as the pre-v49 engine did.

    The old engine matched a tag when it was a detected tag name, the detected source, or a word in the raw title;
    this folds all three into one alternation so a single ``ReleaseTitleSpecification`` reproduces it.
    """
    clean = tag.strip().lower()
    alts = [rf"\b{re.escape(clean)}\b"]
    if clean in _LEGACY_TAG_PATTERNS:
        alts.append(rf"\b(?:{_LEGACY_TAG_PATTERNS[clean]})\b")
    if clean in _LEGACY_SOURCE_PATTERNS:
        alts.append(rf"\b(?:{_LEGACY_SOURCE_PATTERNS[clean]})\b")
    return "|".join(dict.fromkeys(alts))


def legacy_tag_term(tag: str) -> str:
    """A release-profile ``/regex/`` term reproducing a legacy ignored tag."""
    return f"/{legacy_tag_regex(tag)}/"


def legacy_tag_format_spec(tag: str) -> list[dict[str, Any]]:
    return [_title_spec(f"Preferred: {tag.strip().lower()}", legacy_tag_regex(tag))]


def legacy_format_name(tag: str) -> str:
    return f"Preferred: {tag.strip().lower()}"


# --------------------------------------------------------------------------------------------------------------------
# Quality profile items (v2)
# --------------------------------------------------------------------------------------------------------------------


def is_v2_items(items: Any) -> bool:
    return isinstance(items, list) and any(isinstance(i, dict) and i.get("type") in ("quality", "group") for i in items)


def _legacy_flat(items: list[Any]) -> list[dict[str, Any]]:
    """Legacy weight items in stored list order, each with the effective weight the pre-v49 engine ranked by:
    the item's weight, or ``1000 - index * 100`` (floored at 0) when the weight is 0 or null."""
    flat: list[dict[str, Any]] = []
    for it in items or []:
        if hasattr(it, "to_dict"):
            it = it.to_dict()
        if not isinstance(it, dict) or not it.get("quality"):
            continue
        try:
            weight = int(it.get("weight", 100) or 0)
        except (TypeError, ValueError):
            weight = 100
        idx = len(flat)
        flat.append(
            {
                "quality": str(it["quality"]),
                "allowed": bool(it.get("allowed", True)),
                "weight": weight if weight else max(0, 1000 - idx * 100),
            }
        )
    return flat


def legacy_items_to_entries(items: list[Any]) -> list[dict[str, Any]]:
    """Weight-based flat items -> ordered v2 entries (effective weight descending, stable). Allowed flags are preserved."""
    flat = _legacy_flat(items)
    flat.sort(key=lambda i: -i["weight"])
    return [{"type": "quality", "quality": i["quality"], "allowed": i["allowed"]} for i in flat]


def legacy_cutoff_for_entries(items: list[Any], cutoff: str) -> tuple[str, bool]:
    """Maps a legacy cutoff onto the weight-ordered v2 entries. Returns ``(cutoff, exact)``.

    The pre-v49 engine ranked by weight but decided "cutoff met" by *list index* (index <= cutoff index). v2 ranks and
    cuts off by the same order, so when the list order differs from the weight order the cutoff is moved to the entry
    that makes the same set of qualities count as met. If that set is not a prefix of the weight order it cannot be
    represented exactly: the longest prefix inside the old set is used (``exact`` is False), which can only trigger
    extra upgrades, never block ones that were allowed. An unknown cutoff is returned unchanged.
    """
    flat = _legacy_flat(items)
    cutoff_idx = next((i for i, f in enumerate(flat) if f["quality"] == cutoff), None)
    if cutoff_idx is None:
        return cutoff, True
    met = {f["quality"] for f in flat[: cutoff_idx + 1]}
    ordered = [f["quality"] for f in sorted(flat, key=lambda i: -i["weight"])]
    prefix = 0
    while prefix < len(ordered) and ordered[prefix] in met:
        prefix += 1
    if prefix == len(met):
        return ordered[prefix - 1], True
    if prefix > 0:
        return ordered[prefix - 1], False
    return next(q for q in ordered if q in met), False


def legacy_items_to_entries_with_weight(items: list[Any]) -> list[dict[str, Any]]:
    """Like ``legacy_items_to_entries`` but keeps each effective weight (used when the profile has a legacy
    ``min_score``, which the old engine compared against weight + format score)."""
    entries = legacy_items_to_entries(items)
    weights = {f["quality"]: f["weight"] for f in reversed(_legacy_flat(items))}
    for e in entries:
        e["weight"] = weights.get(e["quality"], 100)
    return entries


def normalize_entries(items: Any) -> list[dict[str, Any]]:
    """Accepts v2 entries or legacy weight items; returns clean v2 entries (best first)."""
    if not isinstance(items, list):
        return []
    if not is_v2_items(items):
        return legacy_items_to_entries(items)
    out: list[dict[str, Any]] = []
    for it in items:
        if hasattr(it, "to_dict"):
            it = it.to_dict()
        if not isinstance(it, dict):
            continue
        if it.get("type") == "group":
            members = [str(m) for m in (it.get("items") or []) if str(m).strip()]
            out.append(
                {
                    "type": "group",
                    "name": str(it.get("name") or "").strip(),
                    "allowed": bool(it.get("allowed", True)),
                    "items": members,
                }
            )
        elif it.get("quality"):
            entry = {"type": "quality", "quality": str(it["quality"]), "allowed": bool(it.get("allowed", True))}
            if isinstance(it.get("weight"), int):  # only kept for profiles with a legacy ``min_score``
                entry["weight"] = it["weight"]
            out.append(entry)
    return out


def entry_label(entry: dict[str, Any]) -> str:
    return str(entry.get("name") if entry.get("type") == "group" else entry.get("quality"))


def entry_qualities(entry: dict[str, Any]) -> list[str]:
    if entry.get("type") == "group":
        return [str(m) for m in entry.get("items") or []]
    return [str(entry.get("quality"))]


def find_cutoff_index(entries: list[dict[str, Any]], cutoff: str) -> Optional[int]:
    """Index of the entry named ``cutoff`` (a group name, a quality, or a quality inside a group)."""
    for idx, entry in enumerate(entries):
        if entry_label(entry) == cutoff:
            return idx
    for idx, entry in enumerate(entries):
        if cutoff in entry_qualities(entry):
            return idx
    return None


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=False)
