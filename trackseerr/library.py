"""Audio metadata inspector and library management pipeline for TrackSeerr.

Extracts tags, stream metrics, and codecs via Mutagen with cross-platform collision detection.
"""

import base64
import logging
import os
import re
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPOS, TPE1, TPE2, TRCK, TSRC, TXXX, UFID
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from trackseerr.naming import format_quality
from trackseerr.recycle_bin import is_system_dirname, is_system_filename

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2"}


def is_archive_file(path: Path | str) -> bool:
    """Checks if a file has a supported archive extension."""
    name = Path(path).name.lower()
    return (
        name.endswith(".zip")
        or name.endswith(".tar")
        or name.endswith(".tar.gz")
        or name.endswith(".tgz")
        or name.endswith(".tar.bz2")
    )


_FEAT_SPLIT_RE = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring)\b|\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
_LEADING_TRACK_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[-.)_]+\s*|\s+)(?=\S)(.+)$")


_FOLDER_ART_EXTS = (".jpg", ".jpeg", ".png")
_ALBUMART_RE = re.compile(r"^albumart_\{?[0-9a-f-]+\}?_(large|small)\.(?:jpe?g|png)$", re.IGNORECASE)


def find_folder_art(directory: Path | str) -> Optional[Path]:
    """Finds album art in a folder (case-insensitive): cover/folder, then iTunes/WMP AlbumArt (Large preferred)."""
    try:
        entries = [e for e in Path(directory).iterdir() if e.is_file()]
    except OSError:
        return None

    def rank(entry: Path) -> int | None:
        name = entry.name.lower()
        stem, ext = os.path.splitext(name)
        if ext not in _FOLDER_ART_EXTS:
            return None
        if stem == "cover":
            return 0
        if stem == "folder":
            return 1
        match = _ALBUMART_RE.match(name)
        if match:
            return 2 if match.group(1).lower() == "large" else 4
        if stem == "albumartsmall":
            return 4
        if stem == "albumart":
            return 3
        return None

    ranked = sorted(
        ((r, e.name.lower(), e) for e in entries if (r := rank(e)) is not None),
        key=lambda t: (t[0], t[1]),
    )
    return ranked[0][2] if ranked else None


_SPACED_SPLIT_RE = re.compile(r"\s+/\s+|\s*;\s*")
_MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _clean_credits(values: Any) -> list[str]:
    """Normalises a multi-valued tag (str/bytes/list) into a list of non-empty credited names."""
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        values = [values]
    out: list[str] = []
    for raw in values:
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        text = text.strip()
        if text:
            out.append(text)
    return out


def primary_artist(
    credit: str | None,
    known_artist: Callable[[str], bool] | None = None,
    single_entity: bool = False,
) -> str:
    """Primary artist of a track-artist credit.

    Always splits on spaced ' / ', ';' and feat./ft./featuring; '&' is never split ('Simon & Garfunkel' is one act).
    A bare '/' ('AC/DC', 'f/x') splits only when ``known_artist`` says the left part is already a library artist
    and the whole string is not. ``single_entity`` (one MusicBrainz artist ID) keeps a bare '/' intact.
    """
    text = (credit or "").strip()
    if not text:
        return text
    text = _SPACED_SPLIT_RE.split(text, maxsplit=1)[0]
    text = _FEAT_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    if "/" in text and known_artist is not None and not single_entity:
        left = text.split("/", 1)[0].strip()
        if left and known_artist(left) and not known_artist(text):
            return left
    return text


def resolve_album_artist(
    metadata: dict[str, Any],
    fallback: str = "",
    known_artist: Callable[[str], bool] | None = None,
) -> str:
    """Artist an album/track is filed under.

    Precedence: album artist tag; first credit of the multi-valued ARTISTS tag; the track-artist string split by
    ``primary_artist`` (a single MusicBrainz artist ID marks it as one entity, so a bare '/' is kept).
    """
    album_artist = str(metadata.get("album_artist") or "").strip()
    if album_artist:
        return album_artist
    credits = _clean_credits(metadata.get("artists"))
    if credits:
        return credits[0]
    mbid_text = f"{metadata.get('musicbrainz_artistid') or ''} {metadata.get('musicbrainz_albumartistid') or ''}"
    mbids = {m.lower() for m in _MBID_RE.findall(mbid_text)}
    return primary_artist(str(metadata.get("artist") or ""), known_artist, single_entity=len(mbids) == 1) or fallback


def parse_filename_track(stem: str) -> tuple[str, int | None]:
    """Splits a leading track number off a file stem: '08 Get Lucky' -> ('Get Lucky', 8)."""
    match = _LEADING_TRACK_RE.match(stem or "")
    if not match:
        return (stem or "").strip(), None
    number = int(match.group(1))
    title = match.group(2).strip()
    if number <= 0 or not title:
        return (stem or "").strip(), None
    return title, number


def _parse_int(val: Any) -> int | None:
    """Safely parses an integer or returns None."""
    if val is None:
        return None
    try:
        # If float or string with decimals
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def _parse_num_total(val: Any) -> tuple[int | None, int | None]:
    """Parses a track or disc field which can be '1/12', (1, 12), or 1."""
    if val is None:
        return None, None

    if isinstance(val, (list, tuple)):
        if len(val) >= 2:
            return _parse_int(val[0]), _parse_int(val[1])
        elif len(val) == 1:
            return _parse_num_total(val[0])
        return None, None

    s = str(val).strip()
    if "/" in s:
        parts = s.split("/", 1)
        return _parse_int(parts[0]), _parse_int(parts[1])
    return _parse_int(s), None


def _extract_year(date_val: Any) -> int | None:
    """Extracts 4-digit year from date strings like '2023-05-12' or '2023'."""
    if not date_val:
        return None
    m = re.search(r"\b(\d{4})\b", str(date_val))
    if m:
        return int(m.group(1))
    return None


def inspect_audio_file(file_path: str | Path) -> dict[str, Any]:  # noqa: C901, PLR0915
    """Inspects an audio file using Mutagen to extract tags and stream properties.

    Supports FLAC, MP3 (ID3), M4A/AAC (MP4), and Ogg/Opus.
    Returns:
        title, artist, album, album_artist, year, track_number, total_tracks,
        disc_number, total_discs, codec, bitrate, sample_rate, bits_per_sample,
        duration, quality_full, extension, file_path.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    audio = mutagen.File(str(path))
    if audio is None:
        raise ValueError(f"Unsupported audio file format or corrupted file: {path}")

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    raw_date: str | None = None
    track_number: int | None = None
    total_tracks: int | None = None
    disc_number: int | None = None
    total_discs: int | None = None
    codec = "UNKNOWN"
    bitrate: int | None = None
    sample_rate: int | None = None
    bits_per_sample: int | None = None
    duration: float = 0.0
    musicbrainz_artistid: str | None = None
    musicbrainz_albumartistid: str | None = None
    artists: list[str] = []
    musicbrainz_albumid: str | None = None
    musicbrainz_releasegroupid: str | None = None
    musicbrainz_trackid: str | None = None
    isrc: str | None = None

    tags = getattr(audio, "tags", None)
    info = getattr(audio, "info", None)

    if info is not None:
        duration = float(getattr(info, "length", 0.0))
        sample_rate = _parse_int(getattr(info, "sample_rate", None))
        bitrate = _parse_int(getattr(info, "bitrate", None))
        bits_per_sample = _parse_int(getattr(info, "bits_per_sample", None))

    # 1. FLAC
    if isinstance(audio, FLAC):
        codec = "FLAC"
        if tags:
            title = tags.get("title", [None])[0]
            artist = tags.get("artist", [None])[0]
            album = tags.get("album", [None])[0]
            album_artist = tags.get("albumartist", [None])[0] or tags.get("album_artist", [None])[0]
            raw_date = tags.get("date", [None])[0]
            year = _extract_year(raw_date)
            track_number, total_tracks = _parse_num_total(tags.get("tracknumber", [None])[0])
            if total_tracks is None:
                total_tracks = _parse_int(tags.get("tracktotal", [None])[0] or tags.get("totaltracks", [None])[0])
            disc_number, total_discs = _parse_num_total(tags.get("discnumber", [None])[0])
            if total_discs is None:
                total_discs = _parse_int(tags.get("disctotal", [None])[0] or tags.get("totaldiscs", [None])[0])
            musicbrainz_artistid = tags.get("musicbrainz_artistid", [None])[0]
            musicbrainz_albumartistid = tags.get("musicbrainz_albumartistid", [None])[0]
            artists = _clean_credits(tags.get("artists"))
            musicbrainz_albumid = tags.get("musicbrainz_albumid", [None])[0]
            musicbrainz_releasegroupid = tags.get("musicbrainz_releasegroupid", [None])[0]
            musicbrainz_trackid = tags.get("musicbrainz_trackid", [None])[0]
            isrc = tags.get("isrc", [None])[0]

    # 2. MP3 (ID3)
    elif isinstance(audio, MP3):
        codec = "MP3"
        if bits_per_sample is None:
            bits_per_sample = 16
        if tags:
            def id3_val(key: str) -> str | None:
                frame = tags.get(key)
                if frame and hasattr(frame, "text") and frame.text:
                    return str(frame.text[0])
                return None

            title = id3_val("TIT2")
            artist = id3_val("TPE1")
            album = id3_val("TALB")
            album_artist = id3_val("TPE2")
            raw_date = id3_val("TDRC") or id3_val("TYER")
            year = _extract_year(raw_date)
            track_number, total_tracks = _parse_num_total(id3_val("TRCK"))
            disc_number, total_discs = _parse_num_total(id3_val("TPOS"))
            musicbrainz_artistid = id3_val("TXXX:MusicBrainz Artist Id")
            musicbrainz_albumartistid = id3_val("TXXX:MusicBrainz Album Artist Id")
            artists_frame = tags.get("TXXX:ARTISTS")
            artists = _clean_credits(getattr(artists_frame, "text", None))
            musicbrainz_albumid = id3_val("TXXX:MusicBrainz Album Id")
            musicbrainz_releasegroupid = id3_val("TXXX:MusicBrainz Release Group Id")
            ufid = tags.get("UFID:http://musicbrainz.org")
            if ufid and hasattr(ufid, "data") and ufid.data:
                try:
                    musicbrainz_trackid = ufid.data.decode("ascii")
                except Exception:
                    musicbrainz_trackid = str(ufid.data)
            if not musicbrainz_trackid:
                musicbrainz_trackid = id3_val("TXXX:MusicBrainz Track Id") or id3_val("TXXX:MusicBrainz Recording Id")
            isrc = id3_val("TSRC") or id3_val("TXXX:ISRC")

    # 3. MP4 / M4A / AAC / ALAC
    elif isinstance(audio, MP4):
        codec = "ALAC" if getattr(info, "codec", "").lower() == "alac" else "AAC"
        if tags:
            def mp4_val(key: str) -> str | None:
                v = tags.get(key)
                if v and isinstance(v, list) and v:
                    raw = v[0]
                    if isinstance(raw, (bytes, bytearray)):
                        return raw.decode("utf-8", errors="ignore")
                    return str(raw)
                return None

            title = mp4_val("\xa9nam")
            artist = mp4_val("\xa9ART")
            album = mp4_val("\xa9alb")
            album_artist = mp4_val("aART")
            raw_date = mp4_val("\xa9day")
            year = _extract_year(raw_date)

            trkn = tags.get("trkn")
            if trkn and isinstance(trkn, list) and trkn:
                track_number, total_tracks = _parse_num_total(trkn[0])

            disk = tags.get("disk")
            if disk and isinstance(disk, list) and disk:
                disc_number, total_discs = _parse_num_total(disk[0])

            musicbrainz_artistid = mp4_val("----:com.apple.iTunes:MusicBrainz Artist Id")
            musicbrainz_albumartistid = mp4_val("----:com.apple.iTunes:MusicBrainz Album Artist Id")
            artists = _clean_credits(tags.get("----:com.apple.iTunes:ARTISTS"))
            musicbrainz_albumid = mp4_val("----:com.apple.iTunes:MusicBrainz Album Id")
            musicbrainz_releasegroupid = mp4_val("----:com.apple.iTunes:MusicBrainz Release Group Id")
            musicbrainz_trackid = mp4_val("----:com.apple.iTunes:MusicBrainz Track Id")
            isrc = mp4_val("----:com.apple.iTunes:ISRC")

    # 4. Ogg Opus or Ogg Vorbis
    elif isinstance(audio, (OggOpus, OggVorbis)):
        codec = "Opus" if isinstance(audio, OggOpus) else "Vorbis"
        if tags:
            title = tags.get("title", [None])[0]
            artist = tags.get("artist", [None])[0]
            album = tags.get("album", [None])[0]
            album_artist = tags.get("albumartist", [None])[0] or tags.get("album_artist", [None])[0]
            raw_date = tags.get("date", [None])[0]
            year = _extract_year(raw_date)
            track_number, total_tracks = _parse_num_total(tags.get("tracknumber", [None])[0])
            disc_number, total_discs = _parse_num_total(tags.get("discnumber", [None])[0])
            musicbrainz_artistid = tags.get("musicbrainz_artistid", [None])[0]
            musicbrainz_albumartistid = tags.get("musicbrainz_albumartistid", [None])[0]
            artists = _clean_credits(tags.get("artists"))
            musicbrainz_albumid = tags.get("musicbrainz_albumid", [None])[0]
            musicbrainz_releasegroupid = tags.get("musicbrainz_releasegroupid", [None])[0]
            musicbrainz_trackid = tags.get("musicbrainz_trackid", [None])[0]
            isrc = tags.get("isrc", [None])[0]

    # 5. Generic Mutagen File fallback
    else:
        suffix = path.suffix.lower()
        if suffix in (".flac",):
            codec = "FLAC"
        elif suffix in (".mp3",):
            codec = "MP3"
        elif suffix in (".m4a", ".aac"):
            codec = "AAC"
        elif suffix in (".opus",):
            codec = "Opus"
        elif suffix in (".ogg",):
            codec = "Vorbis"
        elif suffix in (".wav",):
            codec = "WAV"

        if tags:
            title = str(tags.get("title", [""])[0]) or None
            artist = str(tags.get("artist", [""])[0]) or None
            album = str(tags.get("album", [""])[0]) or None
            album_artist = str(tags.get("albumartist", [""])[0]) or None
            raw_date = str(tags.get("date", [""])[0]) or None
            year = _extract_year(raw_date)
            track_number, total_tracks = _parse_num_total(tags.get("tracknumber", [""])[0])
            disc_number, total_discs = _parse_num_total(tags.get("discnumber", [""])[0])
            musicbrainz_artistid = str(tags.get("musicbrainz_artistid", [""])[0]) or None
            musicbrainz_albumartistid = str(tags.get("musicbrainz_albumartistid", [""])[0]) or None
            artists = _clean_credits(tags.get("artists"))
            musicbrainz_albumid = str(tags.get("musicbrainz_albumid", [""])[0]) or None
            musicbrainz_releasegroupid = str(tags.get("musicbrainz_releasegroupid", [""])[0]) or None
            musicbrainz_trackid = str(tags.get("musicbrainz_trackid", [""])[0]) or None
            isrc = str(tags.get("isrc", [""])[0]) or None

    metadata: dict[str, Any] = {
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "date": raw_date or (str(year) if year is not None else None),
        "year": year,
        "release_year": year,
        "track_number": track_number or None,
        "total_tracks": total_tracks,
        "disc_number": disc_number or 1,
        "total_discs": total_discs or 1,
        "codec": codec,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "duration": round(duration, 2),
        "extension": path.suffix.lower(),
        "file_path": str(path),
        "musicbrainz_artistid": musicbrainz_artistid,
        "musicbrainz_albumartistid": musicbrainz_albumartistid,
        "artists": artists,
        "musicbrainz_albumid": musicbrainz_albumid,
        "musicbrainz_releasegroupid": musicbrainz_releasegroupid,
        "musicbrainz_trackid": musicbrainz_trackid,
        "isrc": isrc,
    }

    metadata["quality_full"] = format_quality(metadata)
    return metadata


def detect_path_collision(
    destination_path: str | Path,
    existing_paths: set[str] | None = None,
) -> bool:
    """Checks if destination_path already exists on the filesystem or in an in-memory set."""
    p_str = str(destination_path)
    if existing_paths and p_str in existing_paths:
        return True
    try:
        return Path(p_str).exists()
    except (OSError, ValueError):
        return False


def resolve_collision(
    destination_path: str | Path,
    existing_paths: set[str] | None = None,
) -> Path:
    """Appends an incrementing counter (e.g. 'Song (1).flac') if a collision is detected."""
    dest = Path(destination_path)
    if not detect_path_collision(dest, existing_paths):
        return dest

    parent = dest.parent
    stem = dest.stem
    suffix = dest.suffix

    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not detect_path_collision(candidate, existing_paths):
            return candidate
        counter += 1


def build_tags_to_write(
    metadata: Optional[dict[str, Any]] = None,
    req: Optional[dict[str, Any]] = None,
    audio_files_count: int = 1,
    *,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    title: Optional[str] = None,
    date: Optional[str | int] = None,
    track_number: Optional[str | int] = None,
    total_tracks: Optional[str | int] = None,
    disc_number: Optional[str | int] = None,
    total_discs: Optional[str | int] = None,
    musicbrainz_artistid: Optional[str] = None,
    musicbrainz_albumid: Optional[str] = None,
    musicbrainz_releasegroupid: Optional[str] = None,
    musicbrainz_trackid: Optional[str] = None,
    isrc: Optional[str] = None,
) -> dict[str, Any]:
    """Constructs the canonical tag dictionary for writing audio tags.

    Shared by both the importer (acquisition_worker) and bulk library retag.
    """
    meta = metadata or {}
    tags: dict[str, Any] = {
        "artist": artist if artist is not None else ((req.get("artist") if req else None) or meta.get("artist")),
        "album": album if album is not None else ((req.get("album") or req.get("title") if req else None) or meta.get("album")),
        "title": title if title is not None else (meta.get("title") if audio_files_count > 1 else ((req.get("title") if req else None) or meta.get("title"))),
        "date": date if date is not None else ((req.get("release_date") if req else None) or meta.get("year")),
        "tracknumber": track_number if track_number is not None else meta.get("track_number"),
        "totaltracks": total_tracks if total_tracks is not None else meta.get("total_tracks"),
        "discnumber": disc_number if disc_number is not None else meta.get("disc_number"),
        "totaldiscs": total_discs if total_discs is not None else meta.get("total_discs"),
    }
    if musicbrainz_artistid is not None:
        tags["musicbrainz_artistid"] = musicbrainz_artistid
    elif meta.get("musicbrainz_artistid"):
        tags["musicbrainz_artistid"] = meta.get("musicbrainz_artistid")

    if musicbrainz_albumid is not None:
        tags["musicbrainz_albumid"] = musicbrainz_albumid
    elif meta.get("musicbrainz_albumid"):
        tags["musicbrainz_albumid"] = meta.get("musicbrainz_albumid")

    if musicbrainz_releasegroupid is not None:
        tags["musicbrainz_releasegroupid"] = musicbrainz_releasegroupid
    elif meta.get("musicbrainz_releasegroupid"):
        tags["musicbrainz_releasegroupid"] = meta.get("musicbrainz_releasegroupid")

    if musicbrainz_trackid is not None:
        tags["musicbrainz_trackid"] = musicbrainz_trackid
    elif meta.get("musicbrainz_trackid"):
        tags["musicbrainz_trackid"] = meta.get("musicbrainz_trackid")

    if isrc is not None:
        tags["isrc"] = isrc
    elif meta.get("isrc"):
        tags["isrc"] = meta.get("isrc")

    return tags


def write_audio_tags(  # noqa: C901, PLR0915
    file_path: str | Path,
    tags: dict[str, Any],
    cover_art_bytes: bytes | None = None,
) -> bool:
    """Writes normalized audio metadata tags and optional cover artwork to an audio file.

    Supports FLAC, MP3 (ID3v2.4), M4A/AAC/MP4, and Ogg/Opus containers.
    Returns True on success, or False if the file is invalid or tagging encounters an error.
    """
    try:
        path = Path(file_path).resolve()
        if not path.is_file():
            logger.warning("Tag writing target is not a regular file: %s", file_path)
            return False

        suffix = path.suffix.lower()

        # Extract normalized tag values with sensible aliases
        t_title = tags.get("title")
        t_artist = tags.get("artist")
        t_album = tags.get("album")
        t_album_artist = tags.get("albumartist") or tags.get("album_artist") or tags.get("artist")
        t_date = tags.get("date") or tags.get("release_date") or tags.get("year")
        t_track = tags.get("tracknumber") or tags.get("track_number")
        t_total_tracks = tags.get("totaltracks") or tags.get("total_tracks")
        t_disc = tags.get("discnumber") or tags.get("disc_number")
        t_total_discs = tags.get("totaldiscs") or tags.get("total_discs")
        mb_artist = tags.get("musicbrainz_artistid")
        mb_album = tags.get("musicbrainz_albumid")
        mb_releasegroup = tags.get("musicbrainz_releasegroupid")
        mb_track = tags.get("musicbrainz_trackid")
        tag_isrc = tags.get("isrc")

        # 1. FLAC
        if suffix == ".flac":
            audio = FLAC(str(path))
            if audio.tags is None:
                audio.add_tags()

            if t_title is not None:
                audio["title"] = [str(t_title)]
            if t_artist is not None:
                audio["artist"] = [str(t_artist)]
            if t_album is not None:
                audio["album"] = [str(t_album)]
            if t_album_artist is not None:
                audio["albumartist"] = [str(t_album_artist)]
            if t_date is not None:
                audio["date"] = [str(t_date)]
            if t_track is not None:
                audio["tracknumber"] = [str(t_track)]
            if t_total_tracks is not None:
                audio["totaltracks"] = [str(t_total_tracks)]
            if t_disc is not None:
                audio["discnumber"] = [str(t_disc)]
            if t_total_discs is not None:
                audio["totaldiscs"] = [str(t_total_discs)]
            if mb_artist is not None:
                audio["musicbrainz_artistid"] = [str(mb_artist)]
            if mb_album is not None:
                audio["musicbrainz_albumid"] = [str(mb_album)]
            if mb_releasegroup is not None:
                audio["musicbrainz_releasegroupid"] = [str(mb_releasegroup)]
            if mb_track is not None:
                audio["musicbrainz_trackid"] = [str(mb_track)]
            if tag_isrc is not None:
                audio["isrc"] = [str(tag_isrc)]

            if cover_art_bytes:
                pic = Picture()
                pic.type = 3  # Cover (front)
                pic.mime = "image/png" if cover_art_bytes.startswith(b"\x89PNG") else "image/jpeg"
                pic.data = cover_art_bytes
                audio.clear_pictures()
                audio.add_picture(pic)

            audio.save()
            return True

        # 2. MP3
        elif suffix == ".mp3":
            audio = MP3(str(path))
            if audio.tags is None:
                audio.add_tags()

            if t_title is not None:
                audio.tags.setall("TIT2", [TIT2(encoding=3, text=[str(t_title)])])
            if t_artist is not None:
                audio.tags.setall("TPE1", [TPE1(encoding=3, text=[str(t_artist)])])
            if t_album is not None:
                audio.tags.setall("TALB", [TALB(encoding=3, text=[str(t_album)])])
            if t_album_artist is not None:
                audio.tags.setall("TPE2", [TPE2(encoding=3, text=[str(t_album_artist)])])
            if t_date is not None:
                audio.tags.setall("TDRC", [TDRC(encoding=3, text=[str(t_date)])])
            if t_track is not None:
                track_val = f"{t_track}/{t_total_tracks}" if t_total_tracks else str(t_track)
                audio.tags.setall("TRCK", [TRCK(encoding=3, text=[track_val])])
            if t_disc is not None:
                disc_val = f"{t_disc}/{t_total_discs}" if t_total_discs else str(t_disc)
                audio.tags.setall("TPOS", [TPOS(encoding=3, text=[disc_val])])
            if mb_artist is not None:
                audio.tags.setall(
                    "TXXX:MusicBrainz Artist Id",
                    [TXXX(encoding=3, desc="MusicBrainz Artist Id", text=[str(mb_artist)])],
                )
            if mb_album is not None:
                audio.tags.setall(
                    "TXXX:MusicBrainz Album Id",
                    [TXXX(encoding=3, desc="MusicBrainz Album Id", text=[str(mb_album)])],
                )
            if mb_releasegroup is not None:
                audio.tags.setall(
                    "TXXX:MusicBrainz Release Group Id",
                    [TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=[str(mb_releasegroup)])],
                )
            if mb_track is not None:
                audio.tags.setall(
                    "UFID:http://musicbrainz.org",
                    [UFID(owner="http://musicbrainz.org", data=str(mb_track).encode("ascii"))],
                )
            if tag_isrc is not None:
                audio.tags.setall("TSRC", [TSRC(encoding=3, text=[str(tag_isrc)])])

            if cover_art_bytes:
                mime = "image/png" if cover_art_bytes.startswith(b"\x89PNG") else "image/jpeg"
                audio.tags.setall(
                    "APIC",
                    [APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_art_bytes)],
                )

            audio.save(v2_version=4)
            return True

        # 3. MP4 / M4A / AAC
        elif suffix in (".m4a", ".aac", ".mp4"):
            audio = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()

            if t_title is not None:
                audio["\xa9nam"] = [str(t_title)]
            if t_artist is not None:
                audio["\xa9ART"] = [str(t_artist)]
            if t_album is not None:
                audio["\xa9alb"] = [str(t_album)]
            if t_album_artist is not None:
                audio["aART"] = [str(t_album_artist)]
            if t_date is not None:
                audio["\xa9day"] = [str(t_date)]

            if t_track is not None:
                try:
                    trkn_num = int(t_track)
                    trkn_total = int(t_total_tracks) if t_total_tracks else 0
                    audio["trkn"] = [(trkn_num, trkn_total)]
                except (ValueError, TypeError):
                    pass

            if t_disc is not None:
                try:
                    disc_num = int(t_disc)
                    disc_total = int(t_total_discs) if t_total_discs else 0
                    audio["disk"] = [(disc_num, disc_total)]
                except (ValueError, TypeError):
                    pass

            if mb_artist is not None:
                audio["----:com.apple.iTunes:MusicBrainz Artist Id"] = [str(mb_artist).encode("utf-8")]
            if mb_album is not None:
                audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [str(mb_album).encode("utf-8")]
            if mb_releasegroup is not None:
                audio["----:com.apple.iTunes:MusicBrainz Release Group Id"] = [str(mb_releasegroup).encode("utf-8")]
            if mb_track is not None:
                audio["----:com.apple.iTunes:MusicBrainz Track Id"] = [str(mb_track).encode("utf-8")]
            if tag_isrc is not None:
                audio["----:com.apple.iTunes:ISRC"] = [str(tag_isrc).encode("utf-8")]

            if cover_art_bytes:
                img_fmt = (
                    MP4Cover.FORMAT_PNG
                    if cover_art_bytes.startswith(b"\x89PNG")
                    else MP4Cover.FORMAT_JPEG
                )
                audio["covr"] = [MP4Cover(cover_art_bytes, imageformat=img_fmt)]

            audio.save()
            return True

        # 4. Ogg Vorbis or Ogg Opus
        elif suffix in (".ogg", ".opus"):
            if suffix == ".opus":
                audio = OggOpus(str(path))
            else:
                audio = OggVorbis(str(path))

            if audio.tags is None:
                audio.add_tags()

            if t_title is not None:
                audio["title"] = [str(t_title)]
            if t_artist is not None:
                audio["artist"] = [str(t_artist)]
            if t_album is not None:
                audio["album"] = [str(t_album)]
            if t_album_artist is not None:
                audio["albumartist"] = [str(t_album_artist)]
            if t_date is not None:
                audio["date"] = [str(t_date)]
            if t_track is not None:
                audio["tracknumber"] = [str(t_track)]
            if t_total_tracks is not None:
                audio["totaltracks"] = [str(t_total_tracks)]
            if t_disc is not None:
                audio["discnumber"] = [str(t_disc)]
            if t_total_discs is not None:
                audio["totaldiscs"] = [str(t_total_discs)]
            if mb_artist is not None:
                audio["musicbrainz_artistid"] = [str(mb_artist)]
            if mb_album is not None:
                audio["musicbrainz_albumid"] = [str(mb_album)]
            if mb_releasegroup is not None:
                audio["musicbrainz_releasegroupid"] = [str(mb_releasegroup)]
            if mb_track is not None:
                audio["musicbrainz_trackid"] = [str(mb_track)]
            if tag_isrc is not None:
                audio["isrc"] = [str(tag_isrc)]

            if cover_art_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/png" if cover_art_bytes.startswith(b"\x89PNG") else "image/jpeg"
                pic.data = cover_art_bytes
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

            audio.save()
            return True

        else:
            logger.warning("Unsupported audio container for tag writing: %s", suffix)
            return False

    except (mutagen.MutagenError, OSError) as e:
        logger.warning("Error writing audio tags to %s: %s", file_path, e)
        return False
    except Exception as e:
        logger.warning("Unexpected error writing audio tags to %s: %s", file_path, e)
        return False


def embed_album_artwork(file_path: str | Path, image_data: bytes) -> bool:
    """Embeds cover artwork directly into an audio file without changing existing tags.

    Supports FLAC, MP3, M4A/AAC/MP4, and Ogg/Opus containers.
    Returns True on success, or False if embedding encounters an error.
    """
    if not image_data:
        logger.warning("Cannot embed empty image data into %s", file_path)
        return False
    return write_audio_tags(file_path=file_path, tags={}, cover_art_bytes=image_data)


MAX_ARCHIVE_UNCOMPRESSED_BYTES = 10 * 1024**3
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_RATIO = 200
ARCHIVE_RATIO_MIN_COMPRESSED = 1024 * 1024


class ArchiveLimitError(ValueError):
    """An archive exceeds the bomb limits or contains a forbidden member type (symlink/hardlink)."""


def _check_archive_limits(count: int, total: int, kind: str) -> None:
    if count > MAX_ARCHIVE_MEMBERS:
        raise ArchiveLimitError(f"{kind} archive has {count} members (limit {MAX_ARCHIVE_MEMBERS})")
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ArchiveLimitError(
            f"{kind} archive uncompressed size {total} bytes exceeds limit {MAX_ARCHIVE_UNCOMPRESSED_BYTES}"
        )


def extract_archive(archive_path: Path | str, target_dir: Path | str) -> list[Path]:  # noqa: C901
    """Extracts an archive (.zip, .tar, .tar.gz, .tgz, .tar.bz2) safely into target_dir.

    Validates that target_dir exists and protects against path traversal attacks.
    Returns a sorted list of discovered audio files matching AUDIO_EXTENSIONS.
    """
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Archive file not found: {archive}")

    target = Path(target_dir).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Target directory does not exist: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Target path is not a directory: {target}")

    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            infos = zf.infolist()
            _check_archive_limits(len(infos), sum(m.file_size for m in infos), "zip")
            for member in infos:
                if member.compress_size > ARCHIVE_RATIO_MIN_COMPRESSED and member.file_size > member.compress_size * MAX_ARCHIVE_RATIO:
                    raise ArchiveLimitError(
                        f"zip member {member.filename} compression ratio exceeds {MAX_ARCHIVE_RATIO}:1"
                    )
                if stat.S_ISLNK((member.external_attr >> 16) & 0xFFFF):
                    raise ArchiveLimitError(f"zip archive contains a symlink member: {member.filename}")
                norm_name = member.filename.replace("\\", "/")
                member_target = (target / norm_name).resolve()
                if not member_target.is_relative_to(target):
                    raise ValueError(f"Path traversal detected in zip archive: {member.filename}")
            zf.extractall(target)
    elif (
        name.endswith(".tar.gz")
        or name.endswith(".tgz")
        or name.endswith(".tar.bz2")
        or name.endswith(".tar")
    ):
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            _check_archive_limits(len(members), sum(m.size for m in members if m.isreg()), "tar")
            for member in members:
                norm_name = member.name.replace("\\", "/")
                member_target = (target / norm_name).resolve()
                if not member_target.is_relative_to(target):
                    raise ValueError(f"Path traversal detected in tar archive: {member.name}")
            try:
                tf.extractall(target, filter="data")
            except (tarfile.FilterError, tarfile.TarError) as e:
                raise ValueError(f"Unsafe tar archive extraction failed: {e}") from e
    else:
        raise ValueError(f"Unsupported archive format: {archive.name}")

    extracted_audio: list[Path] = []
    for root, dirs, files in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not is_system_dirname(d)]
        for f in files:
            if is_system_filename(f):
                continue
            f_path = Path(root) / f
            if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                extracted_audio.append(f_path)

    return sorted(extracted_audio)


_fingerprint_warned = False

# AcoustID allows ~3 requests/second; keep calls at least this far apart (tests monkeypatch to 0).
ACOUSTID_MIN_INTERVAL_SECONDS = 0.34
_acoustid_rate_lock = threading.Lock()
_acoustid_last_call = 0.0


def _acoustid_rate_limit() -> None:
    """Blocks until at least ACOUSTID_MIN_INTERVAL_SECONDS has passed since the previous AcoustID call."""
    global _acoustid_last_call
    with _acoustid_rate_lock:
        wait = ACOUSTID_MIN_INTERVAL_SECONDS - (time.monotonic() - _acoustid_last_call)
        if wait > 0:
            time.sleep(wait)
        _acoustid_last_call = time.monotonic()


def _warn_fingerprint_unavailable(reason: str) -> None:
    """Logs once per process why audio fingerprinting cannot run, instead of failing silently on every call."""
    global _fingerprint_warned
    if _fingerprint_warned:
        return
    _fingerprint_warned = True
    logger.warning("Audio fingerprinting is unavailable: %s. AcoustID lookups will return no match.", reason)


def fingerprint_audio_file(
    file_path: str | Path,
    api_key: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Calculates Chromaprint fingerprint and looks up match via AcoustID API on demand.

    Safely handles missing acoustid package or fpcalc binary without raising.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        logger.warning("fingerprint_audio_file: File not found: %s", path)
        return None

    try:
        import acoustid
    except ImportError as exc:
        _warn_fingerprint_unavailable("the pyacoustid package is not installed (%s)" % exc)
        return None

    if not api_key:
        logger.debug("fingerprint_audio_file: No acoustid_api_key configured.")
        return None

    try:
        # force_fpcalc: pyacoustid otherwise prefers its audioread decoder when the chromaprint library is present,
        # and audioread has no backend in the slim image (no ffmpeg/gstreamer), so every fingerprint would fail.
        _acoustid_rate_limit()
        results = acoustid.match(api_key, str(path), force_fpcalc=True)
        for score, recording_id, title, artist in results:
            return {
                "score": float(score),
                "recording_id": str(recording_id),
                "title": str(title) if title else None,
                "artist": str(artist) if artist else None,
            }
        return None
    except Exception as exc:
        if type(exc).__name__ == "NoBackendError":
            # pyacoustid raises this when neither the chromaprint library nor the fpcalc binary is available.
            _warn_fingerprint_unavailable("the chromaprint 'fpcalc' binary / library was not found (install chromaprint)")
            return None
        logger.warning("fingerprint_audio_file: AcoustID match failed for %s: %s", path, exc)
        return None

