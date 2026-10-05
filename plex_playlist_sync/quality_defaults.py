"""Seed data and shape helpers for the Arr-style quality system (docs/ARR_PROFILES_SPEC.md, QUALITY_RESEARCH.md).

Pure data and pure functions: no database or engine imports, so ``storage`` (migration v49), the decision engine and
the API can all share one definition of the defaults.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# Canonical quality order, best first (used to order quality definitions and group members in the UI).
QUALITY_ORDER: list[str] = [
    "FLAC 24bit",
    "FLAC 16bit",
    "MP3 320",
    "MP3 V0",
    "AAC 256",
    "MP3 192",
    "MP3 V2",
    "Unknown",
]

# (quality, title, min_kbps, preferred_kbps, max_kbps); None = unbounded.
DEFAULT_QUALITY_DEFINITIONS: list[tuple[str, str, Optional[float], Optional[float], Optional[float]]] = [
    ("FLAC 24bit", "FLAC 24bit", 0.0, 2000.0, 9500.0),
    ("FLAC 16bit", "FLAC 16bit", 0.0, 895.0, 1400.0),
    ("MP3 320", "MP3 320", 290.0, 320.0, 350.0),
    ("MP3 V0", "MP3 V0", 160.0, 245.0, 350.0),
    ("AAC 256", "AAC 256", 200.0, 256.0, 280.0),
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


def legacy_items_to_entries(items: list[Any]) -> list[dict[str, Any]]:
    """Weight-based flat items -> ordered v2 entries (weight descending, stable). Allowed flags are preserved."""
    flat: list[dict[str, Any]] = []
    for it in items or []:
        if hasattr(it, "to_dict"):
            it = it.to_dict()
        if not isinstance(it, dict) or not it.get("quality"):
            continue
        try:
            weight = int(it.get("weight", 100))
        except (TypeError, ValueError):
            weight = 100
        flat.append({"quality": str(it["quality"]), "allowed": bool(it.get("allowed", True)), "weight": weight})
    flat.sort(key=lambda i: -i["weight"])
    return [{"type": "quality", "quality": i["quality"], "allowed": i["allowed"]} for i in flat]


def legacy_items_to_entries_with_weight(items: list[Any]) -> list[dict[str, Any]]:
    """Like ``legacy_items_to_entries`` but keeps each weight (used when the profile has a legacy ``min_score``)."""
    entries = legacy_items_to_entries(items)
    weights = {
        str(i["quality"]): int(i.get("weight", 100))
        for i in (x.to_dict() if hasattr(x, "to_dict") else x for x in items or [])
        if isinstance(i, dict) and i.get("quality")
    }
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
