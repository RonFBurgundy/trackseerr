"""Changelog parser, caching, and build metadata for TrackSeerr."""

import copy
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

import plex_playlist_sync

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cached_path: Optional[str] = None
_cached_mtime: Optional[float] = None
_cached_releases: list[dict[str, Any]] = []
_warned_paths: set[str] = set()

# Regexes for inline markdown stripping
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_ITALIC_STAR_RE = re.compile(r"\*{3}(.*?)\*{3}")
_BOLD_ITALIC_UNDER_RE = re.compile(r"_{3}(.*?)_{3}")
_BOLD_STAR_RE = re.compile(r"\*{2}(.*?)\*{2}")
_BOLD_UNDER_RE = re.compile(r"_{2}(.*?)_{2}")
_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.*?)~~")

# Heading regexes
# e.g., ## [Unreleased] or ## [1.0.0] - note
_RELEASE_HEADING_RE = re.compile(r"^##\s*\[\s*([^\]]+?)\s*\](?:\s*(?:-|–|—)?\s*(.*))?$")
_SECTION_HEADING_RE = re.compile(r"^###\s+(.+)$")


def strip_inline_markdown(text: str) -> str:
    """Strips markdown links, inline code, bold, italics, and strikethrough to plain text."""
    # 1. Images & Links: ![alt](url) -> alt; [anchor](url) -> anchor
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    # 2. Inline code: `code` -> code
    text = _CODE_RE.sub(r"\1", text)
    # 3. Bold & italics
    text = _BOLD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _BOLD_ITALIC_UNDER_RE.sub(r"\1", text)
    text = _BOLD_STAR_RE.sub(r"\1", text)
    text = _BOLD_UNDER_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    # 4. Strikethrough
    text = _STRIKE_RE.sub(r"\1", text)
    return text.strip()


def parse_changelog(content: str) -> list[dict[str, Any]]:
    """Parses Keep a Changelog markdown into structured release entries.

    Returns:
        [{
            "version": str,
            "date_note": Optional[str],
            "unreleased": bool,
            "sections": [{"title": str, "items": [str]}]
        }]
    """
    lines = content.splitlines()
    releases: list[dict[str, Any]] = []

    in_releases = False
    current_release: Optional[dict[str, Any]] = None
    current_section: Optional[dict[str, Any]] = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # Check for release heading: ## [version] ...
        if stripped.startswith("## "):
            m_rel = _RELEASE_HEADING_RE.match(stripped)
            if m_rel:
                in_releases = True
                ver_raw = m_rel.group(1).strip()
                note_raw = m_rel.group(2).strip() if m_rel.group(2) else None
                is_unreleased = ver_raw.lower() == "unreleased"
                current_release = {
                    "version": ver_raw,
                    "date_note": note_raw if note_raw else None,
                    "unreleased": is_unreleased,
                    "sections": [],
                }
                releases.append(current_release)
                current_section = None
                continue
            elif not in_releases:
                # Malformed heading in preamble is ignored
                continue
            else:
                # Malformed ## heading while inside releases: tolerate and close current release
                current_release = None
                current_section = None
                continue

        # If we have not reached the first release heading yet, ignore preamble
        if not in_releases or current_release is None:
            continue

        # Check for section heading: ### Title
        if stripped.startswith("### "):
            m_sec = _SECTION_HEADING_RE.match(stripped)
            if m_sec:
                title = m_sec.group(1).strip()
                current_section = {"title": title, "items": []}
                current_release["sections"].append(current_section)
                continue
            else:
                # Malformed section heading tolerated
                continue

        # Bullet items: - item or * item
        if stripped.startswith(("- ", "* ")):
            item_text = stripped[2:].strip()
            item_clean = strip_inline_markdown(item_text)
            if current_section is None:
                # Bullet item without section heading: create an untitled section
                current_section = {"title": "", "items": []}
                current_release["sections"].append(current_section)
            current_section["items"].append(item_clean)
            continue

        # Indented continuation lines for multi-line bullet points
        if (raw_line.startswith(("  ", "\t"))) and current_section is not None and current_section["items"]:
            continuation = strip_inline_markdown(stripped)
            if continuation:
                current_section["items"][-1] = f"{current_section['items'][-1]} {continuation}"
            continue

    return releases


def get_changelog_path() -> Path:
    """Locates the CHANGELOG.md file path."""
    env_path = os.environ.get("TRACKSEERR_CHANGELOG_PATH")
    if env_path:
        return Path(env_path)
    return Path(plex_playlist_sync.__file__).resolve().parent.parent / "CHANGELOG.md"


def get_changelog(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Loads and parses the changelog with mtime-based caching.

    Missing or unreadable file results in an empty list and a single logged warning.
    """
    global _cached_path, _cached_mtime, _cached_releases

    target = path if path is not None else get_changelog_path()
    target_str = str(target.resolve()) if target.exists() else str(target)

    try:
        stat_result = target.stat()
    except OSError as err:
        with _cache_lock:
            if target_str not in _warned_paths:
                logger.warning("Changelog file missing or unreadable at %s: %s", target, err)
                _warned_paths.add(target_str)
            if _cached_path == target_str:
                _cached_path = None
                _cached_mtime = None
                _cached_releases = []
        return []

    mtime = stat_result.st_mtime
    with _cache_lock:
        if _cached_path == target_str and _cached_mtime == mtime:
            return copy.deepcopy(_cached_releases)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        with _cache_lock:
            if target_str not in _warned_paths:
                logger.warning("Changelog file unreadable at %s: %s", target, err)
                _warned_paths.add(target_str)
            if _cached_path == target_str:
                _cached_path = None
                _cached_mtime = None
                _cached_releases = []
        return []

    releases = parse_changelog(content)

    with _cache_lock:
        _cached_path = target_str
        _cached_mtime = mtime
        _cached_releases = releases
        return copy.deepcopy(releases)


def clear_changelog_cache() -> None:
    """Clears the changelog cache and warning history (primarily for tests)."""
    global _cached_path, _cached_mtime, _cached_releases, _warned_paths
    with _cache_lock:
        _cached_path = None
        _cached_mtime = None
        _cached_releases = []
        _warned_paths.clear()


def get_build_info() -> tuple[str, Optional[str]]:
    """Returns (version, commit). Version is plex_playlist_sync.__version__; commit is short 7 chars or None."""
    version = plex_playlist_sync.__version__
    commit = os.environ.get("TRACKSEERR_COMMIT", "").strip()
    short_commit = commit[:7] if commit else None
    return version, short_commit


def get_latest_release(releases: list[dict[str, Any]], current_version: str) -> Optional[dict[str, Any]]:
    """Selects the latest release entry.

    Rule:
    1. Matching current version
    2. Else first non-Unreleased entry
    3. Else first entry
    """
    if not releases:
        return None

    # 1. Entry matching the current version
    v_clean = current_version.lstrip("v")
    for rel in releases:
        rel_v = str(rel.get("version", "")).lstrip("v")
        if rel_v == v_clean:
            return rel

    # 2. First non-Unreleased entry
    for rel in releases:
        if not rel.get("unreleased"):
            return rel

    # 3. First entry
    return releases[0]
