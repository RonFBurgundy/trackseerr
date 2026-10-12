"""Arr-grade token template naming engine, conditional block evaluator,

cross-platform path sanitizer, and path builder for TrackSeerr.
"""

import re
import unicodedata
from pathlib import Path
from typing import Any

# Supported token names
SUPPORTED_TOKENS: list[str] = [
    # Artist
    "Artist CleanName",
    "Artist Disambiguation",
    "Artist Name",
    "Artist NameThe",
    "Artist CleanNameThe",
    "Artist NameFirstCharacter",
    "Artist Genre",
    "Artist MbId",
    # Album
    "Album CleanTitle",
    "Album Disambiguation",
    "Original Release Year",
    "Release Year",
    "Album Title",
    "Album TitleThe",
    "Album CleanTitleThe",
    "Album TitleFirstCharacter",
    "Album Genre",
    "Album MbId",
    "Album Type",
    # Disc / Medium
    "Medium Format",
    "Medium Title",
    "medium:00",
    "medium:0",
    "disc:00",
    "disc:0",
    # Track
    "Track CleanTitle",
    "Track Title",
    "Track TitleThe",
    "Track CleanTitleThe",
    "Track ArtistName",
    "Track ArtistCleanName",
    "Track ArtistNameThe",
    "Original Filename",
    "track:00",
    "track:0",
    # Audio / Quality / MediaInfo
    "Quality Full",
    "MediaInfo AudioCodec",
    "MediaInfo BitDepth",
    "MediaInfo Bitrate",
    "MediaInfo SampleRate",
]

# Legacy split-format building blocks, used only to compose full-path formats from old-style settings.
_LEGACY_ALBUM_FOLDER_FORMAT = "{Album Title} ({Release Year}){[ - Album Type]}"
_LEGACY_TRACK_FILE_FORMAT = "{track:00} - {Track Title}{[ (Quality Full)]}"
_LEGACY_DISC_FOLDER_FORMAT = "{Medium Format} {medium:00}"


def legacy_to_track_formats(settings: dict[str, Any]) -> tuple[str, str]:
    """Composes (standard_track_format, multi_disc_track_format) from the pre-Lidarr-style split settings.

    Legacy settings stored the album folder, disc folder and file name separately; this joins
    them so old data renders identically under the full-path model.
    """
    album = str(settings.get("album_folder_format") or _LEGACY_ALBUM_FOLDER_FORMAT)
    track = str(settings.get("standard_track_format") or _LEGACY_TRACK_FILE_FORMAT)
    disc = str(settings.get("multi_disc_folder_format") or _LEGACY_DISC_FOLDER_FORMAT)
    return f"{album}/{track}", f"{album}/{disc}/{track}"


def _legacy_preset(**legacy: Any) -> dict[str, Any]:
    """Builds a full-path preset from legacy split values, keeping the legacy keys for old clients."""
    std, multi = legacy_to_track_formats(legacy)
    preset = dict(legacy)
    preset["standard_track_format"] = std
    preset["multi_disc_track_format"] = multi
    return preset


# Preset templates
PRESETS: dict[str, dict[str, Any]] = {
    "Trackseerr": {
        "artist_folder_format": "{Artist Name}",
        "standard_track_format": "{Album Title}{ - [{Album Type}]}{ (Release Year)}/{track:00} - {Track Title}",
        "multi_disc_track_format": (
            "{Album Title}{ - [{Album Type}]}{ (Release Year)}/Disc {medium:00}/{track:00} - {Track Title}"
        ),
        "compilation_track_format": "",
        "root_folder_path": "/music",
        "colon_replacement_format": " - ",
        "replace_illegal_characters": True,
    },
    # TRaSH Guides has no official Lidarr naming page; this mirrors the Lidarr defaults the
    # community recommends (artist/album in the file name so loose files stay identifiable).
    "TRaSH Guides": {
        "artist_folder_format": "{Artist Name}{ (Artist Disambiguation)}",
        "standard_track_format": (
            "{Album Title}{ [Album Disambiguation]}{ (Release Year)}/{Artist Name} - {Album Title} - {track:00} - {Track Title}"
        ),
        "multi_disc_track_format": (
            "{Album Title}{ [Album Disambiguation]}{ (Release Year)}/{Medium Format} {medium:00}/"
            "{Artist Name} - {Album Title} - {track:00} - {Track Title}"
        ),
        "compilation_track_format": "",
        "root_folder_path": "/music",
        "colon_replacement_format": " - ",
        "replace_illegal_characters": True,
    },
    # Plex's documented layout: Artist/Album/NN - Title. Multi-disc albums stay in one folder
    # with the disc number prefixed to the track number (101, 102, 201, ...).
    "Plex": {
        "artist_folder_format": "{Artist Name}",
        "standard_track_format": "{Album Title}/{track:00} - {Track Title}",
        "multi_disc_track_format": "{Album Title}/{medium:0}{track:00} - {Track Title}",
        "compilation_track_format": "",
        "root_folder_path": "/music",
        "colon_replacement_format": " - ",
        "replace_illegal_characters": True,
    },
    # Lidarr's out-of-the-box naming (full-path model).
    "Lidarr Default": {
        "artist_folder_format": "{Artist Name}",
        "standard_track_format": (
            "{Album Title} ({Release Year})/{Artist Name} - {Album Title} - {track:00} - {Track Title}"
        ),
        "multi_disc_track_format": (
            "{Album Title} ({Release Year})/{Medium Format} {medium:00}/"
            "{Artist Name} - {Album Title} - {track:00} - {Track Title}"
        ),
        "compilation_track_format": "",
        "root_folder_path": "/music",
        "colon_replacement_format": " - ",
        "replace_illegal_characters": True,
    },
    "Clean Minimal": _legacy_preset(
        artist_folder_format="{Artist CleanName}",
        album_folder_format="{Album CleanTitle} ({Release Year})",
        standard_track_format="{track:00} - {Track CleanTitle}",
        compilation_track_format="{track:00} - {Artist CleanName} - {Track CleanTitle}",
        multi_disc_folder_format="Disc {medium:0}",
        root_folder_path="/music",
        colon_replacement_format="_",
        replace_illegal_characters=True,
    ),
    "Audiophile / Detailed": _legacy_preset(
        artist_folder_format="{Artist Name}",
        album_folder_format="{Album Title} ({Release Year}){[ - Album Type]}",
        standard_track_format="{track:00} - {Track Title} [{MediaInfo AudioCodec} {MediaInfo BitDepth} {MediaInfo SampleRate}]",
        compilation_track_format="{track:00} - {Artist Name} - {Track Title} [{MediaInfo AudioCodec} {MediaInfo BitDepth} {MediaInfo SampleRate}]",
        multi_disc_folder_format="{Medium Format} {medium:00}",
        root_folder_path="/music",
        colon_replacement_format=" - ",
        replace_illegal_characters=True,
    ),
}

PRESET_DESCRIPTIONS: dict[str, str] = {
    "Trackseerr": (
        "Default. Built for a clean, readable library that matches MusicBrainz: names exactly as the artist writes "
        "them (accents and & kept), characters a filesystem can't store are replaced the way Lidarr does (? -> !, "
        "* -> -, / -> +), album type and year on every album folder, Disc NN subfolders. File names stay stable across quality upgrades."
    ),
    "TRaSH Guides": (
        "The community-recommended Lidarr setup: like Lidarr Default plus artist and album disambiguation, so "
        "same-named artists and albums never collide. Repeats artist and album in every file name, so a loose file "
        "is still identifiable."
    ),
    "Plex": (
        "Plex's recommended layout for the best automatic matching: Artist/Album/NN - Title, multi-disc albums in "
        "one folder numbered 101, 102, 201. Short paths with nothing extra in them."
    ),
    "Lidarr Default": (
        "Lidarr's out-of-the-box naming. Pick this if your library was built by Lidarr with its default settings so "
        "nothing gets renamed. Artist and album are repeated in every file name."
    ),
    "Clean Minimal": (
        "Lidarr-style clean names: accents removed, & written as and, punctuation dropped, colons replaced with _. "
        "Good for older devices, car stereos and players that mishandle special characters."
    ),
    "Audiophile / Detailed": (
        "Adds codec, bit depth and sample rate to every file name (e.g. [FLAC 24 96000]). Note: files are renamed "
        "whenever their quality changes, such as after an upgrade."
    ),
}

# Fresh-install defaults are exactly the Trackseerr preset.
_DEFAULT_PRESET = PRESETS["Trackseerr"]
DEFAULT_ARTIST_FOLDER_FORMAT: str = _DEFAULT_PRESET["artist_folder_format"]
DEFAULT_STANDARD_TRACK_FORMAT: str = _DEFAULT_PRESET["standard_track_format"]
DEFAULT_MULTI_DISC_TRACK_FORMAT: str = _DEFAULT_PRESET["multi_disc_track_format"]
DEFAULT_COMPILATION_TRACK_FORMAT: str = _DEFAULT_PRESET["compilation_track_format"]
DEFAULT_COLON_REPLACEMENT: str = _DEFAULT_PRESET["colon_replacement_format"]
DEFAULT_REPLACE_ILLEGAL: bool = _DEFAULT_PRESET["replace_illegal_characters"]
_LEGACY_COMPILATION_TRACK_FORMAT = "{track:00} - {Artist Name} - {Track Title}{[ (Quality Full)]}"

_LEGACY_FORMAT_KEYS = ("album_folder_format", "standard_track_format", "multi_disc_folder_format")


def _uses_legacy_split_formats(settings: dict[str, Any]) -> bool:
    """True for settings that predate the full-path model: legacy keys present, no multi_disc_track_format."""
    return settings.get("multi_disc_track_format") is None and any(k in settings for k in _LEGACY_FORMAT_KEYS)


def strip_leading_articles(name: str | None) -> str:
    """Strips leading English grammatical articles (The, A, An) from a string."""
    if not name:
        return ""
    stripped = re.sub(r"^(?:the|a|an)\s+", "", str(name).strip(), flags=re.IGNORECASE)
    return stripped.strip()


_CLEAN_TITLE_PUNCT = re.compile(
    r"(?<=\s)(,|<|>|/|\\|;|:|'|\"|\||`|~|!|\?|@|\$|%|\^|\*|-|_|=){1}(?=\s)"
    r"|('|:|\?|,)(?=(?:(?:s|m)\s)|\s|$)"
    r"|(\(|\)|\[|\]|\{|\})",
    re.IGNORECASE,
)


def _trim_track_title(title: str) -> str:
    """Lidarr trims trailing spaces, dots and '?' from track titles ('Voodoo?' -> 'Voodoo'); interior ones stay."""
    return title.strip().rstrip(" .?")


def _remove_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn"))


def clean_name(name: str | None) -> str:
    """Lidarr's FileNameBuilder.CleanTitle: '&' -> 'and', '/' -> space, droppable punctuation and
    brackets removed, diacritics stripped. The leading article is kept; remaining illegal characters
    are left for ``sanitize_component``."""
    if not name:
        return ""
    title = str(name).replace("&", "and")
    title = re.sub(r"[/]", " ", title)
    title = _CLEAN_TITLE_PUNCT.sub("", title)
    title = _remove_diacritics(title)
    return re.sub(r"\s+", " ", title).strip()


def move_leading_article(name: str | None) -> str:
    """Moves a leading article to the end: 'The Beatles' -> 'Beatles, The'."""
    if not name:
        return ""
    m = re.match(r"^(the|a|an)\s+(.+)$", str(name).strip(), flags=re.IGNORECASE)
    if not m:
        return str(name).strip()
    return f"{m.group(2).strip()}, {m.group(1)}"


def format_quality(metadata: dict[str, Any]) -> str:  # noqa: C901
    """Builds a human-readable quality string (e.g. 'FLAC 24bit 96kHz' or 'MP3 320kbps')."""
    if metadata.get("quality_full"):
        return str(metadata["quality_full"]).strip()

    codec = str(metadata.get("codec") or metadata.get("audio_codec") or "").upper().strip()
    bit_depth = metadata.get("bits_per_sample") or metadata.get("bit_depth")
    sample_rate = metadata.get("sample_rate")
    bitrate = metadata.get("bitrate")

    sr_str: str | None = None
    if sample_rate:
        try:
            sr_val = float(sample_rate)
            if sr_val >= 1000:
                if sr_val % 1000 == 0:
                    sr_str = f"{int(sr_val // 1000)}kHz"
                else:
                    sr_str = f"{sr_val / 1000:.1f}kHz"
            else:
                sr_str = f"{sr_val}Hz"
        except (ValueError, TypeError):
            sr_str = str(sample_rate)

    br_str: str | None = None
    if bitrate:
        try:
            br_val = float(bitrate)
            kbps = round(br_val / 1000) if br_val > 1000 else round(br_val)
            br_str = f"{kbps}kbps"
        except (ValueError, TypeError):
            br_str = str(bitrate)
            if not br_str.endswith("kbps"):
                br_str = f"{br_str}kbps"

    depth_str: str | None = None
    if bit_depth:
        depth_val = str(bit_depth).lower().replace("bit", "").strip()
        depth_str = f"{depth_val}bit"

    if codec in ("FLAC", "ALAC", "WAV", "AIFF", "PCM"):
        parts = [codec]
        if depth_str:
            parts.append(depth_str)
        if sr_str:
            parts.append(sr_str)
        return " ".join(parts)
    elif codec:
        parts = [codec]
        if br_str:
            parts.append(br_str)
        elif depth_str and sr_str:
            parts.extend([depth_str, sr_str])
        return " ".join(parts)
    elif br_str:
        return br_str
    return ""


def truncate_utf8_bytes(s: str, max_bytes: int = 255) -> str:
    """Truncates a string to at most max_bytes when UTF-8 encoded without splitting multi-byte code points."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    truncated = encoded[:max_bytes]
    decoded = truncated.decode("utf-8", errors="ignore")
    return decoded.rstrip(" .")


def sanitize_component(name: str, colon_replacement: str = " - ", replace_illegal: bool = True) -> str:
    """Sanitizes a single directory or file path component across Windows, Linux, and macOS.

    - Normalizes to Unicode NFC so composed and decomposed accents yield identical bytes.
    - Colon: with ``replace_illegal`` on, the default " - " ("smart", Lidarr's default) turns ': ' into " - " and a
      bare ':' into "-" ('45:33' -> '45-33'); any other colon_replacement replaces ':' and following whitespace.
      With it off, colons are deleted regardless of the colon format (as Lidarr does).
    - With ``replace_illegal`` (Lidarr's "Replace illegal characters") maps '/' and '\\' -> '+', '?' -> '!',
      '*' -> '-' and removes < > " |; without it all of \\ / < > ? * | " are simply removed.
    - Dots inside a title are kept ('Flashdance... What a Feeling'); a component made only of dots ('..') becomes ''.
    - Strips ASCII control characters (0x00-0x1F).
    - Collapses redundant whitespace.
    - Truncates to max 255 bytes on clean UTF-8 code point boundaries.
    - Strips leading spaces and dots, and trailing spaces and dots. Lidarr only trims trailing spaces from file
      names; trailing dots are stripped here as well because they break Windows/SMB (a deliberate difference).
    """
    if not name:
        return ""
    name = unicodedata.normalize("NFC", str(name))

    if not replace_illegal:
        result = name.replace(":", "")
    elif colon_replacement == " - ":
        # Smart (Lidarr default): ': ' -> ' - ', bare ':' -> '-'
        result = re.sub(r":\s+", " - ", name).replace(":", "-")
    else:
        # Replace colons and any immediately following whitespace
        result = re.sub(r":\s*", colon_replacement, name)
    if replace_illegal:
        # Lidarr maps '/' to '+' ('AC/DC' -> 'AC+DC'), '?' -> '!' and '*' -> '-'; this also keeps tag-joined
        # names apart ('Daft Punk/Romanthony' -> 'Daft Punk+Romanthony')
        result = result.replace("/", "+").replace("\\", "+")
        result = result.replace("?", "!").replace("*", "-")
        result = re.sub(r'[<>"|]', "", result)
    else:
        result = re.sub(r'[\\/<>?*|"]', "", result)
    result = re.sub(r"[\x00-\x1f]", "", result)
    result = re.sub(r"\s+", " ", result).strip()
    result = truncate_utf8_bytes(result, 255)
    result = result.lstrip(" .").rstrip(" .")  # also guarantees a component is never '.' or '..'
    return result


def resolve_token(token: str, metadata: dict[str, Any]) -> str | None:  # noqa: C901, PLR0915
    """Resolves a token identifier against metadata."""
    t = token.strip()

    # Artist
    if t == "Artist CleanName":
        val = metadata.get("artist") or metadata.get("artist_name") or metadata.get("album_artist") or ""
        return clean_name(str(val)) or None
    if t == "Artist Name":
        val = metadata.get("artist") or metadata.get("artist_name") or metadata.get("album_artist") or ""
        return str(val) or None
    if t in ("Artist NameThe", "Artist CleanNameThe"):
        val = metadata.get("artist") or metadata.get("artist_name") or metadata.get("album_artist") or ""
        if t == "Artist CleanNameThe":
            val = clean_name(str(val))
        return move_leading_article(str(val)) or None
    if t == "Artist NameFirstCharacter":
        val = metadata.get("artist") or metadata.get("artist_name") or metadata.get("album_artist") or ""
        clean = strip_leading_articles(str(val))
        return clean[0].upper() if clean else None
    if t == "Artist Genre":
        val = metadata.get("artist_genre") or metadata.get("genre")
        return str(val).strip() if val else None
    if t == "Artist MbId":
        val = metadata.get("artist_mbid") or metadata.get("musicbrainz_artistid")
        return str(val).strip() if val else None
    if t == "Artist Disambiguation":
        val = metadata.get("artist_disambiguation")
        return str(val).strip() if val else None

    # Album
    if t == "Album CleanTitle":
        val = metadata.get("album") or metadata.get("album_title") or ""
        return clean_name(str(val)) or None
    if t in ("Album TitleThe", "Album CleanTitleThe"):
        val = metadata.get("album") or metadata.get("album_title") or ""
        if t == "Album CleanTitleThe":
            val = clean_name(str(val))
        return move_leading_article(str(val)) or None
    if t == "Album TitleFirstCharacter":
        val = metadata.get("album") or metadata.get("album_title") or ""
        clean = strip_leading_articles(str(val))
        return clean[0].upper() if clean else None
    if t == "Album Genre":
        val = metadata.get("album_genre") or metadata.get("genre")
        return str(val).strip() if val else None
    if t == "Album MbId":
        val = metadata.get("album_mbid") or metadata.get("musicbrainz_albumid")
        return str(val).strip() if val else None
    if t == "Album Title":
        val = metadata.get("album") or metadata.get("album_title") or ""
        return str(val).strip() or None
    if t == "Release Year":
        year = metadata.get("release_year") or metadata.get("year")
        if not year and metadata.get("release_date"):
            m = re.search(r"\b(\d{4})\b", str(metadata["release_date"]))
            if m:
                year = m.group(1)
        return str(year).strip() if year else None
    if t == "Original Release Year":
        year = metadata.get("original_release_year") or metadata.get("original_year")
        if not year:
            year = metadata.get("release_year") or metadata.get("year")
        if not year and metadata.get("release_date"):
            m = re.search(r"\b(\d{4})\b", str(metadata["release_date"]))
            if m:
                year = m.group(1)
        return str(year).strip() if year else None
    if t == "Album Disambiguation":
        val = metadata.get("album_disambiguation")
        return str(val).strip() if val else None
    if t == "Album Type":
        val = metadata.get("album_type")
        return str(val).strip() if val else None

    # Disc / Medium
    if t in ("medium:00", "disc:00"):
        num = metadata.get("disc_number") or metadata.get("medium_number") or metadata.get("disc") or 1
        try:
            return f"{int(num):02d}"
        except (ValueError, TypeError):
            return "01"
    if t in ("medium:0", "disc:0"):
        num = metadata.get("disc_number") or metadata.get("medium_number") or metadata.get("disc") or 1
        try:
            return f"{int(num):d}"
        except (ValueError, TypeError):
            return "1"
    if t == "Medium Format":
        val = metadata.get("medium_format") or metadata.get("media_format") or "CD"
        return str(val).strip()
    if t == "Medium Title":
        val = metadata.get("medium_title") or metadata.get("disc_title")
        return str(val).strip() if val else None

    # Track
    if t == "track:00":
        num = metadata.get("track_number") or metadata.get("track") or 1
        try:
            return f"{int(num):02d}"
        except (ValueError, TypeError):
            return "01"
    if t == "track:0":
        num = metadata.get("track_number") or metadata.get("track") or 1
        try:
            return f"{int(num):d}"
        except (ValueError, TypeError):
            return "1"
    if t == "Track Title":
        val = metadata.get("title") or metadata.get("track_title")
        return _trim_track_title(str(val)) if val else None
    if t in ("Track TitleThe", "Track CleanTitleThe"):
        val = _trim_track_title(str(metadata.get("title") or metadata.get("track_title") or ""))
        if t == "Track CleanTitleThe":
            val = clean_name(str(val))
        return move_leading_article(str(val)) or None
    if t in ("Track ArtistName", "Track ArtistCleanName", "Track ArtistNameThe"):
        val = metadata.get("track_artist") or metadata.get("artist") or metadata.get("artist_name") or ""
        if t == "Track ArtistCleanName":
            return clean_name(str(val)) or None
        if t == "Track ArtistNameThe":
            return move_leading_article(str(val)) or None
        return str(val).strip() or None
    if t == "Original Filename":
        src = metadata.get("file_path") or metadata.get("filename")
        return Path(str(src)).stem if src else None
    if t == "Track CleanTitle":
        val = metadata.get("title") or metadata.get("track_title") or ""
        return clean_name(str(val)) or None

    # Audio / Quality / MediaInfo
    if t == "Quality Full":
        q = format_quality(metadata)
        return q or None
    if t == "MediaInfo AudioCodec":
        codec = metadata.get("codec") or metadata.get("audio_codec")
        return str(codec).upper().strip() if codec else None
    if t == "MediaInfo Bitrate":
        br = metadata.get("bitrate")
        if br:
            try:
                br_val = float(br)
                kbps = round(br_val / 1000) if br_val > 1000 else round(br_val)
                return f"{kbps}kbps"
            except (ValueError, TypeError):
                s = str(br).strip()
                return s if s.endswith("kbps") else f"{s}kbps"
        return None
    if t == "MediaInfo SampleRate":
        sr = metadata.get("sample_rate")
        if sr:
            try:
                sr_val = float(sr)
                if sr_val >= 1000:
                    if sr_val % 1000 == 0:
                        return f"{int(sr_val // 1000)}kHz"
                    return f"{sr_val / 1000:.1f}kHz"
                return f"{sr_val}Hz"
            except (ValueError, TypeError):
                return str(sr).strip()
        return None
    if t == "MediaInfo BitDepth":
        bd = metadata.get("bits_per_sample") or metadata.get("bit_depth")
        if bd:
            bd_str = str(bd).lower().replace("bit", "").strip()
            return f"{bd_str}bit"
        return None

    # Fallback to direct key in metadata
    val_direct = metadata.get(t)
    if val_direct is not None:
        return str(val_direct).strip() or None
    return None


def _evaluate_conditional_block(block_content: str, metadata: dict[str, Any]) -> str:
    """Evaluates the inner content of a conditional block.

    If all resolved tokens are non-empty, replaces tokens and keeps prefixes/suffixes.
    If the tokens resolve to empty/None, returns empty string.
    """
    found_tokens: list[str] = []
    # Identify known tokens inside block_content
    for token_name in SUPPORTED_TOKENS:
        # Match bare token name or wrapped {token_name}
        pattern = re.compile(rf"(\{{?\b{re.escape(token_name)}\}}?)")
        if pattern.search(block_content):
            found_tokens.append(token_name)

    if not found_tokens:
        # Check for any {key} token
        generic_tokens = re.findall(r"\{([A-Za-z0-9_:]+)\}", block_content)
        found_tokens.extend(generic_tokens)

    if not found_tokens:
        return ""

    # Verify that all tokens in the conditional block resolve to non-empty values
    resolved_values: dict[str, str] = {}
    for tok in found_tokens:
        val = resolve_token(tok, metadata)
        if not val:
            return ""
        resolved_values[tok] = val

    # Strip any internal delimiter brackets, e.g. [Disambiguation: ] -> Disambiguation:
    result = re.sub(r"\[(.*?)\]", r"\1", block_content)
    for tok, val in resolved_values.items():
        # Replace {tok} or bare tok
        result = result.replace(f"{{{tok}}}", val)
        result = re.sub(rf"\b{re.escape(tok)}\b", val, result)
    return result


_PURE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:]+(?: [A-Za-z0-9_:]+)*$")
_BRACE_BLOCK_RE = re.compile(r"\{([^{}]+)\}")
_NESTED_BLOCK_RE = re.compile(r"\{([^{}]*\{[^{}]+\}[^{}]*)\}")
_BARE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_:])("
    + "|".join(re.escape(t) for t in sorted(SUPPORTED_TOKENS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_:])"
)


def _render_nested_block(match: re.Match[str], metadata: dict[str, Any]) -> str:
    """Lidarr-style optional block with inner tokens, e.g. ``{ - [{Album Type}]}``.

    Everything outside the inner ``{Token}``s (prefix, suffix, brackets) is kept literally and the
    whole block is dropped when any inner token resolves empty.
    """
    content = match.group(1)
    for tok in _BRACE_BLOCK_RE.findall(content):
        if not resolve_token(tok, metadata):
            return ""

    def sub(m: re.Match[str]) -> str:
        return resolve_token(m.group(1), metadata) or ""

    return _BRACE_BLOCK_RE.sub(sub, content)


def _render_brace_block(match: re.Match[str], metadata: dict[str, Any]) -> str:
    """Renders ``{Token}`` or a Lidarr optional block with bare tokens such as ``{ (Release Year)}``.

    A plain token (no surrounding whitespace/punctuation) resolves to its value or "". Anything
    else is an optional block: prefix/suffix text is kept literally (brackets included) when all
    bare tokens inside resolve, otherwise the entire block disappears.
    """
    content = match.group(1)
    if _PURE_TOKEN_RE.match(content):
        return resolve_token(content, metadata) or ""

    tokens = _BARE_TOKEN_RE.findall(content)
    if not tokens:
        return ""
    values: dict[str, str] = {}
    for tok in tokens:
        val = resolve_token(tok, metadata)
        if not val:
            return ""
        values[tok] = val
    return _BARE_TOKEN_RE.sub(lambda m: values[m.group(1)], content)


def render_template(
    template: str,
    metadata: dict[str, Any],
    colon_replacement: str = " - ",
) -> str:
    """Renders a token naming template with conditional block evaluation and token replacement.

    Supported syntax:
      * ``{Token}``                  - replaced by the token value (empty when unknown).
      * ``{ (Release Year)}``        - Lidarr optional block: kept with its literal prefix/suffix only
                                       when the token has a value.
      * ``{ - [{Album Type}]}``      - same, with nested ``{Token}`` so brackets are preserved.
      * ``{[ - Album Type]}`` / ``{(Token)}`` - legacy Trackseerr conditional forms.
    """
    if not template:
        return ""

    result = template

    # 1. Legacy {[prefix Token suffix]} conditional blocks (brackets are delimiters, not output)
    def replace_bracket_conditional(match: re.Match[str]) -> str:
        inner = match.group(1)
        return _evaluate_conditional_block(inner, metadata)

    result = re.sub(r"\{\[(.*?)\]\}", replace_bracket_conditional, result)

    # 2. Legacy {(Token)} conditional blocks
    def replace_paren_conditional(match: re.Match[str]) -> str:
        inner = match.group(1)
        eval_res = _evaluate_conditional_block(inner, metadata)
        return f"({eval_res})" if eval_res else ""

    result = re.sub(r"\{\((.*?)\)\}", replace_paren_conditional, result)

    # 3. Lidarr optional blocks with nested {Token}s, e.g. { - [{Album Type}]}
    result = _NESTED_BLOCK_RE.sub(lambda m: _render_nested_block(m, metadata), result)

    # 4. Plain {Token}s and Lidarr optional blocks with bare tokens, e.g. { (Release Year)}
    result = _BRACE_BLOCK_RE.sub(lambda m: _render_brace_block(m, metadata), result)

    # 5. Clean up any remaining double spaces or dangling formatting artifacts
    result = re.sub(r" +", " ", result).strip()
    return result


def split_template_segments(template: str) -> list[str]:
    """Splits a format on '/' (or '\\') that sit outside ``{...}`` blocks, dropping empty segments."""
    segments: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in template:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch in "/\\" and depth == 0:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf))
    return [s for s in segments if s.strip()]


def resolve_track_formats(settings: dict[str, Any]) -> tuple[str, str]:
    """Returns the effective (standard, multi_disc) full-path track formats for a settings dict.

    Settings that predate the Lidarr-style model (no ``multi_disc_track_format`` key) are composed
    from the legacy album/disc/file formats so existing callers render unchanged.
    """
    if _uses_legacy_split_formats(settings):
        return legacy_to_track_formats(settings)

    standard = str(settings.get("standard_track_format") or DEFAULT_STANDARD_TRACK_FORMAT)
    if settings.get("multi_disc_track_format") is None:
        return standard, DEFAULT_MULTI_DISC_TRACK_FORMAT
    multi = str(settings.get("multi_disc_track_format") or "").strip()
    if not multi:
        # No explicit multi-disc format: put a disc folder between the album folder(s) and the file.
        segs = split_template_segments(standard)
        disc = str(settings.get("multi_disc_folder_format") or "Disc {medium:00}")
        multi = "/".join(segs[:-1] + [disc] + segs[-1:])
    return standard, multi


def validate_format(template: str, kind: str = "track") -> list[str]:
    """Returns human-readable warnings for a format string (never raises).

    ``kind`` is ``"artist"`` (single folder) or ``"track"`` (folders + file name).
    """
    warnings: list[str] = []
    if not template or not template.strip():
        return ["Format is empty - the default will be used."]

    depth = 0
    for ch in template:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        warnings.append("Unbalanced braces: every '{' needs a matching '}'.")

    for content in _BRACE_BLOCK_RE.findall(template):
        if content.startswith(("[", "(")):
            continue  # legacy conditional forms
        if _PURE_TOKEN_RE.match(content) and content not in SUPPORTED_TOKENS:
            warnings.append(f"Unknown token '{{{content}}}' renders as empty.")

    if kind == "artist":
        if len(split_template_segments(template)) > 1:
            warnings.append("Artist folder format should be a single folder (no '/').")
        return warnings

    if template.rstrip().endswith(("/", "\\")):
        warnings.append("Trailing '/' ignored - the last segment is the file name.")
    segments = split_template_segments(template)
    if segments:
        name = segments[-1]
        if "Track Title" not in name and "Track CleanTitle" not in name and "track:" not in name:
            warnings.append("File name has no {track:00}/{Track Title} token - tracks may overwrite each other.")
    return warnings


def _detect_compilation(meta: dict[str, Any]) -> bool:
    album_artist = str(meta.get("album_artist") or "").strip()
    return bool(
        meta.get("is_compilation") is True
        or meta.get("compilation") is True
        or album_artist.lower() in ("various artists", "various")
        or str(meta.get("album_type", "")).strip().lower() == "compilation"
    )


def _detect_multi_disc(meta: dict[str, Any]) -> bool:
    total_discs = meta.get("total_discs") or meta.get("disc_total") or meta.get("total_mediums")
    disc_number = meta.get("disc_number") or meta.get("medium_number") or meta.get("disc")
    value = total_discs if total_discs is not None else disc_number
    if value is None:
        return False
    try:
        return int(value) > 1
    except (ValueError, TypeError):
        return False


def _guess_extension(meta: dict[str, Any]) -> str:
    ext = meta.get("extension") or meta.get("ext")
    if not ext and meta.get("file_path"):
        ext = Path(str(meta["file_path"])).suffix
    if not ext and meta.get("filename"):
        ext = Path(str(meta["filename"])).suffix
    if not ext and meta.get("codec"):
        codec_ext_map = {
            "FLAC": ".flac",
            "MP3": ".mp3",
            "AAC": ".m4a",
            "ALAC": ".m4a",
            "M4A": ".m4a",
            "OPUS": ".opus",
            "OGG": ".ogg",
            "VORBIS": ".ogg",
            "WAV": ".wav",
            "AIFF": ".aiff",
        }
        ext = codec_ext_map.get(str(meta["codec"]).upper().strip(), "")
    return str(ext).strip() if ext else ""


def build_path_parts(
    metadata: dict[str, Any],
    settings: dict[str, Any],
    multi_disc: bool | None = None,
) -> dict[str, Any]:
    """Renders the sanitized path pieces: ``artist`` folder, ``folders`` (below the artist) and ``file`` name.

    ``multi_disc`` forces which track format is used (used for previews); ``None`` auto-detects from
    the metadata.
    """
    colon_replacement = str(settings.get("colon_replacement_format") or DEFAULT_COLON_REPLACEMENT)
    replace_illegal = bool(settings.get("replace_illegal_characters", DEFAULT_REPLACE_ILLEGAL))
    artist_format = str(settings.get("artist_folder_format") or DEFAULT_ARTIST_FOLDER_FORMAT)
    compilation_track_format = str(settings.get("compilation_track_format") or "").strip()
    if "compilation_track_format" not in settings and _uses_legacy_split_formats(settings):
        compilation_track_format = _LEGACY_COMPILATION_TRACK_FORMAT  # legacy callers
    standard_format, multi_format = resolve_track_formats(settings)

    meta = dict(metadata)
    album_artist = str(meta.get("album_artist") or "").strip()
    is_compilation = _detect_compilation(meta)

    def render(fmt: str, m: dict[str, Any]) -> str:
        raw = render_template(fmt, m, colon_replacement=colon_replacement)
        raw = re.sub(r"\s*(\(\s*\)|\[\s*\])", "", raw)  # drop empty ()/[] left by missing tokens
        return sanitize_component(raw, colon_replacement=colon_replacement, replace_illegal=replace_illegal)

    # Artist folder
    artist_meta = meta
    if is_compilation and (not meta.get("artist") or album_artist.lower() in ("various artists", "various")):
        artist_meta = dict(meta)
        artist_meta["artist"] = album_artist or "Various Artists"
    artist_component = render(artist_format, artist_meta)
    if not artist_component:
        artist_component = "Various Artists" if is_compilation else "Unknown Artist"

    # Track format -> folders + file name
    use_multi = _detect_multi_disc(meta) if multi_disc is None else multi_disc
    segments = split_template_segments(multi_format if use_multi else standard_format)
    if not segments:
        segments = ["Track"]
    folder_templates, file_template = segments[:-1], segments[-1]

    if is_compilation and compilation_track_format:
        comp_segments = split_template_segments(compilation_track_format)
        if comp_segments:
            file_template = comp_segments[-1]

    folders: list[str] = []
    for idx, folder_tpl in enumerate(folder_templates):
        comp = render(folder_tpl, meta)
        if comp:
            folders.append(comp)
        elif idx == 0:
            folders.append("Unknown Album")

    file_component = render(file_template, meta) or "Track"

    ext = _guess_extension(meta)
    if ext:
        if not ext.startswith("."):
            ext = f".{ext}"
        if not file_component.lower().endswith(ext.lower()):
            file_component = f"{file_component}{ext}"

    return {"artist": artist_component, "folders": folders, "file": file_component}


def build_track_path(metadata: dict[str, Any], settings: dict[str, Any]) -> str:
    """Builds a fully sanitized, cross-platform media track path.

    - Artist folder from ``artist_folder_format``.
    - Album folder(s) and file name from the standard or multi-disc track format (Lidarr-style
      '/'-separated relative path); multi-disc is chosen when total_discs > 1.
    - Path sanitization per component preventing directory traversal.
    - Extension preservation.
    """
    root_folder = str(settings.get("root_folder_path") or "/music").rstrip("/\\")
    parts = build_path_parts(metadata, settings)
    rel_path = "/".join([parts["artist"], *parts["folders"], parts["file"]])
    if root_folder:
        return f"{root_folder}/{rel_path}"
    return rel_path


# Help catalog shown in the UI's "?" modal. Every entry in SUPPORTED_TOKENS must appear here
# (enforced by tests) so the documentation can never drift from the engine.
TOKEN_HELP: list[dict[str, Any]] = [
    {
        "group": "Artist",
        "tokens": [
            ("{Artist Name}", "The artist's name exactly as stored", "Artist Name"),
            ("{Artist CleanName}", "Artist name, Lidarr-clean: & becomes and, accents removed, punctuation Lidarr drops is removed", "Blunts & Blondes -> Blunts and Blondes"),
            ("{Artist NameThe}", "Artist name with the leading article moved to the end", "Beatles, The"),
            ("{Artist CleanNameThe}", "Clean artist name with the leading article moved to the end", "Beatles, The"),
            ("{Artist NameFirstCharacter}", "First letter of the artist (article ignored), upper-case", "B"),
            ("{Artist Disambiguation}", "MusicBrainz disambiguation text (empty when none)", "UK rock band"),
            ("{Artist Genre}", "Primary genre of the artist (empty when unknown)", "Pop"),
            ("{Artist MbId}", "MusicBrainz artist ID", "db92a151-1ac2-438b-bc43-b82e149ddd50"),
        ],
    },
    {
        "group": "Album",
        "tokens": [
            ("{Album Title}", "Album title", "The White Album"),
            ("{Album CleanTitle}", "Album title, Lidarr-clean: & becomes and, accents removed, punctuation Lidarr drops is removed", "Re: Stacks -> Re Stacks"),
            ("{Album TitleThe}", "Album title with the leading article moved to the end", "White Album, The"),
            ("{Album CleanTitleThe}", "Clean album title with the leading article moved to the end", "White Album, The"),
            ("{Album TitleFirstCharacter}", "First letter of the album (article ignored), upper-case", "W"),
            ("{Album Genre}", "Genre of the album (empty when unknown)", "Rock"),
            ("{Album MbId}", "MusicBrainz release group ID", "1b022e01-4da6-387b-8658-8678046e4cef"),
            ("{Album Type}", "Release type (Album, EP, Single, ...)", "EP"),
            ("{Album Disambiguation}", "MusicBrainz release disambiguation (empty when none)", "Remastered"),
            ("{Release Year}", "Year of this release", "1968"),
            ("{Original Release Year}", "Year the album was first released", "1968"),
        ],
    },
    {
        "group": "Disc",
        "tokens": [
            ("{Medium Format}", "Physical medium (CD, Vinyl, Digital Media, ...)", "CD"),
            ("{Medium Title}", "Disc title (empty when none)", "Bonus Disc"),
            ("{medium:00}", "Disc number, 2 digits", "02"),
            ("{medium:0}", "Disc number, no padding", "2"),
            ("{disc:00}", "Same as {medium:00}", "02"),
            ("{disc:0}", "Same as {medium:0}", "2"),
        ],
    },
    {
        "group": "Track",
        "tokens": [
            ("{Track Title}", "Track title", "Revolution 1"),
            ("{Track CleanTitle}", "Track title, Lidarr-clean: & becomes and, accents removed, punctuation Lidarr drops is removed", "Tanerélle -> Tanerelle"),
            ("{Track TitleThe}", "Track title with the leading article moved to the end", "Fool on the Hill, The"),
            ("{Track CleanTitleThe}", "Clean track title with the leading article moved to the end", "Fool on the Hill, The"),
            ("{Track ArtistName}", "Artist of this track (differs from the album artist on compilations)", "The Beatles"),
            ("{Track ArtistCleanName}", "Track artist, Lidarr-clean: & becomes and, accents removed, punctuation Lidarr drops is removed", "Hüsker Dü -> Husker Du"),
            ("{Track ArtistNameThe}", "Track artist with the leading article moved to the end", "Beatles, The"),
            ("{Original Filename}", "File name the file had before renaming (no extension)", "01 revolution 1"),
            ("{track:00}", "Track number, 2 digits", "01"),
            ("{track:0}", "Track number, no padding", "1"),
        ],
    },
    {
        "group": "Audio / Quality",
        "tokens": [
            ("{Quality Full}", "Codec with bit depth / sample rate or bitrate", "FLAC 24bit 96kHz"),
            ("{MediaInfo AudioCodec}", "Audio codec", "FLAC"),
            ("{MediaInfo BitDepth}", "Bit depth", "24bit"),
            ("{MediaInfo SampleRate}", "Sample rate", "96kHz"),
            ("{MediaInfo Bitrate}", "Bitrate", "320kbps"),
        ],
    },
]

SYNTAX_HELP: list[tuple[str, str, str]] = [
    ("/", "Separates folders. The last part is the file name (the extension is added automatically).", "{Album Title}/{track:00} - {Track Title}"),
    ("{ (Release Year)}", "Optional block: text around a token is kept only when the token has a value.", " (1968), or nothing"),
    ("{ - [{Album Type}]}", "Optional block with nested token; brackets are kept literally.", " - [EP], or nothing"),
    ("{Token}", "Plain token: replaced by its value, or nothing when unknown.", "{Album Title}"),
]
