import os
import re
from typing import Any, Optional

from .security import sanitize_text

# Regex for #EXTINF:<seconds> [attributes],<title / artist - title>
_EXTINF_PATTERN = re.compile(
    r"^#EXTINF:\s*(-?\d+)?(?:\s+([^\,]*))?\,(.*)$", re.IGNORECASE
)
# Pattern for attributes like artist="Foo", tvg-name="Bar", or tvg-artist="Baz"
_ATTR_PATTERN = re.compile(r'([\w\-]+)=["\']([^"\']*)["\']')

# Audio file extensions to strip from path-based entries
_AUDIO_EXTENSIONS = (
    ".mp3",
    ".flac",
    ".m4a",
    ".alac",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".aiff",
    ".ape",
)


def _norm(text: str) -> str:
    """Comparison key: lowercase alphanumerics only, so 'Daft Punk_ X' and 'Daft Punk; X' compare equal."""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _path_hints(raw_path: str) -> tuple[Optional[str], Optional[str]]:
    """(artist folder, file title) from an Artist/Album/NN Title.ext path; either may be None."""
    clean_entry = raw_path
    for ext in _AUDIO_EXTENSIONS:
        if clean_entry.lower().endswith(ext):
            clean_entry = clean_entry[: -len(ext)]
            break
    parts = [p.strip() for p in clean_entry.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return None, None
    file_title = re.sub(r"^\d+[\s\.\-_]+", "", parts[-1]).strip() or None
    artist_folder = parts[-3] if len(parts) >= 3 else None
    return artist_folder, file_title


def _orient_display_name(left: str, right: str, raw_path: str) -> tuple[str, str, Optional[tuple[str, str]]]:
    """Resolves an 'A - B' EXTINF display name into (artist, title, alternative (artist, title) or None).

    iTunes writes 'Title - Artist'; most other tools write 'Artist - Title'. The path (Artist/Album/NN Title.ext)
    decides when it can: the half equal to the artist folder is the artist, the half equal to the file's title is
    the title. When the path says nothing, 'Artist - Title' is assumed and the swapped reading is returned as the
    alternative so the importer can try both against the library.
    """
    artist_folder, file_title = _path_hints(raw_path)
    left_n, right_n = _norm(left), _norm(right)
    if artist_folder:
        folder_n = _norm(artist_folder)
        if folder_n and folder_n == right_n:
            return right, left, None
        if folder_n and folder_n == left_n:
            return left, right, None
    if file_title:
        title_n = _norm(file_title)
        if title_n and title_n == left_n:
            return right, left, None
        if title_n and title_n == right_n:
            return left, right, None
    return left, right, (right, left)


def parse_m3u(content: str) -> list[dict[str, Any]]:
    """Safely parses an M3U or M3U8 playlist content string into a list of tracks.

    Returns:
        list of dicts with keys: 'title', 'artist', 'album'
    """
    if not isinstance(content, str):
        return []

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    tracks: list[dict[str, Any]] = []

    pending_title: Optional[str] = None
    pending_artist: Optional[str] = None
    pending_album: Optional[str] = None
    pending_split: Optional[tuple[str, str]] = None  # unresolved 'A - B' display name, oriented at the path line

    for line in lines:
        if line.startswith("#EXTM3U") or line.startswith("#PLAYLIST:"):
            continue

        if line.startswith("#EXTINF:"):
            m = _EXTINF_PATTERN.match(line)
            pending_split = None
            if m:
                _duration_str, attr_str, display_name = m.groups()
                display_name = sanitize_text(display_name or "").strip()

                artist: Optional[str] = None
                title: Optional[str] = None
                album: Optional[str] = None

                # Check for explicit artist="..." or title="..." attributes
                if attr_str:
                    for k, v in _ATTR_PATTERN.findall(attr_str):
                        k_lower = k.lower()
                        if k_lower in ("artist", "tvg-artist"):
                            artist = sanitize_text(v)
                        elif k_lower in ("title", "tvg-name", "tvg-title"):
                            title = sanitize_text(v)
                        elif k_lower in ("album", "tvg-album"):
                            album = sanitize_text(v)

                # Parse display name if artist or title not already resolved from attributes
                if display_name:
                    if " - " in display_name:
                        parts = display_name.split(" - ", 1)
                        if not artist and not title:
                            pending_split = (parts[0].strip(), parts[1].strip())
                            title = parts[1].strip()
                            artist = parts[0].strip()
                        else:
                            if not artist:
                                artist = parts[0].strip()
                            if not title:
                                title = parts[1].strip()
                    else:
                        if not title:
                            title = display_name

                pending_title = title
                pending_artist = artist
                pending_album = album
            continue

        # Ignore comments or directives
        if line.startswith("#"):
            continue

        # This line is a file path, URI, or title
        raw_path = line

        # If we had an #EXTINF preceding this line, use those metadata values
        if pending_title:
            t = pending_title
            a = pending_artist or "Unknown Artist"
            al = pending_album or ""
            entry: dict[str, Any] = {"title": t, "artist": a, "album": al}
            if pending_split:
                a, t, alt = _orient_display_name(pending_split[0], pending_split[1], raw_path)
                entry.update({"title": t, "artist": a})
                if alt:
                    entry["alt"] = {"artist": alt[0], "title": alt[1]}
            tracks.append(entry)
            pending_split = None
            pending_title = None
            pending_artist = None
            pending_album = None
            continue

        # Otherwise, parse the raw path or title line directly
        # Strip trailing audio extension
        clean_entry = raw_path
        for ext in _AUDIO_EXTENSIONS:
            if clean_entry.lower().endswith(ext):
                clean_entry = clean_entry[: -len(ext)]
                break

        # Remove path separators (extract filename and potential parent directory)
        norm_path = clean_entry.replace("\\", "/")
        path_parts = [p.strip() for p in norm_path.split("/") if p.strip()]

        if not path_parts:
            continue

        filename = path_parts[-1]

        # Strip track numbers like "01 - " or "1. "
        stripped_filename = re.sub(r"^\d+[\s\.\-_]+", "", filename).strip()

        artist = "Unknown Artist"
        title = stripped_filename
        album = ""

        if " - " in stripped_filename:
            parts = stripped_filename.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        elif len(path_parts) >= 2:
            # Fall back to parent folder as artist if format is Artist/Song
            possible_artist = path_parts[-2]
            if len(path_parts) >= 3:
                # Structure: Artist/Album/Track
                artist = path_parts[-3]
                album = path_parts[-2]
            else:
                artist = possible_artist

        tracks.append(
            {
                "title": sanitize_text(title),
                "artist": sanitize_text(artist),
                "album": sanitize_text(album),
            }
        )

    # If file ended with pending #EXTINF line
    if pending_title:
        tracks.append(
            {
                "title": pending_title,
                "artist": pending_artist or "Unknown Artist",
                "album": pending_album or "",
            }
        )

    return tracks
