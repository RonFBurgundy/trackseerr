"""MusicBrainz Add-Release seeding and release linking services."""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

MB_RELEASE_ADD_URL = "https://musicbrainz.org/release/add"
_MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _map_primary_type(raw_type: Optional[str]) -> str:
    """Maps raw album/record type to MusicBrainz primary types: album, single, ep, or other."""
    if not raw_type:
        return "other"
    norm = str(raw_type).strip().lower()
    if norm in ("album", "single", "ep"):
        return norm
    return "other"


def _parse_date_parts(raw_date: Any, year: Any) -> list[list[str]]:
    """Extracts year, month, day components from date string or year integer."""
    out: list[list[str]] = []
    if raw_date:
        parts = [p.strip() for p in str(raw_date).split("-") if p.strip()]
        if len(parts) >= 1 and parts[0].isdigit():
            out.append(["events.0.date.year", str(int(parts[0]))])
        if len(parts) >= 2 and parts[1].isdigit():
            out.append(["events.0.date.month", str(int(parts[1]))])
        if len(parts) >= 3 and parts[2].isdigit():
            out.append(["events.0.date.day", str(int(parts[2]))])
    elif year is not None and str(year).isdigit():
        out.append(["events.0.date.year", str(int(year))])
    return out


def _build_medium_fields(tracks: list[dict[str, Any]]) -> list[list[str]]:
    """Groups tracks by disc_number and emits ordered MusicBrainz medium/track form fields."""
    discs: dict[int, list[dict[str, Any]]] = {}
    for t in tracks:
        d_num = int(t.get("disc_number") or 1)
        discs.setdefault(d_num, []).append(t)

    sorted_discs = sorted(discs.keys())
    fields: list[list[str]] = []
    for n, d_num in enumerate(sorted_discs):
        fields.append([f"mediums.{n}.format", "Digital Media"])
        disc_tracks = sorted(discs[d_num], key=lambda tr: int(tr.get("track_number") or 1))
        for m, tr in enumerate(disc_tracks):
            fields.append([f"mediums.{n}.track.{m}.name", str(tr.get("title") or "")])
            fields.append([f"mediums.{n}.track.{m}.number", str(tr.get("track_number", m + 1))])
            dur = tr.get("duration_seconds")
            if dur is not None and float(dur) > 0:
                ms = int(round(float(dur) * 1000))
                fields.append([f"mediums.{n}.track.{m}.length", str(ms)])
            rec_id = tr.get("mb_recording_id")
            if rec_id:
                fields.append([f"mediums.{n}.track.{m}.recording", str(rec_id).strip()])
    return fields


def _build_artist_credit_fields(
    db: Any, album: dict[str, Any], deezer_details: Optional[dict[str, Any]]
) -> list[list[str]]:
    """Builds artist credit form fields from album artist or Deezer details."""
    artist_name = ""
    artist_mbid = None
    if album.get("artist_id"):
        artist = db.get_library_artist(album["artist_id"])
        if artist:
            artist_name = artist.get("name") or ""
            artist_mbid = artist.get("mbid") or None
    if not artist_name and deezer_details:
        artist_name = deezer_details.get("artist") or ""

    fields: list[list[str]] = []
    if artist_name:
        fields.append(["artist_credit.names.0.name", str(artist_name)])
        fields.append(["artist_credit.names.0.artist.name", str(artist_name)])
    if artist_mbid:
        fields.append(["artist_credit.names.0.mbid", str(artist_mbid)])
    return fields


def _build_deezer_metadata_fields(deezer_details: Optional[dict[str, Any]]) -> list[list[str]]:
    """Extracts label and barcode fields when provided by Deezer."""
    if not deezer_details:
        return []
    fields: list[list[str]] = []
    if deezer_details.get("label"):
        fields.append(["labels.0.name", str(deezer_details["label"])])
    if deezer_details.get("upc"):
        fields.append(["barcode", str(deezer_details["upc"])])
    return fields


def _build_deezer_url_and_note(deezer_id: Optional[str]) -> tuple[list[list[str]], str]:
    """Builds external URL field and edit note text."""
    edit_note = (
        "Seeded from TrackSeerr using Deezer and local file metadata. "
        "Please check track titles and lengths before submitting."
    )
    if not deezer_id:
        return [], edit_note
    deezer_url = f"https://www.deezer.com/album/{deezer_id}"
    return [["urls.0.url", deezer_url]], f"{edit_note}\n{deezer_url}"


def build_release_seed(
    db: Any,
    discovery_client: Any,
    album_id: str,
    *,
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Builds prefilled field parameters for MusicBrainz release editor seeding."""
    album = db.get_library_album(album_id)
    if album is None:
        raise ValueError(f"album not found: {album_id}")

    tracks = db.list_library_tracks(album_id=album_id, limit=2000)
    if not tracks:
        raise ValueError("album has no tracks")

    foreign_album_id = str(album.get("foreign_album_id") or "").strip()
    deezer_details: Optional[dict[str, Any]] = None
    deezer_id: Optional[str] = None
    if foreign_album_id.startswith("deezer:album:") and discovery_client is not None:
        deezer_id = foreign_album_id.removeprefix("deezer:album:").strip()
        deezer_details = discovery_client.get_album_details(foreign_album_id)

    fields: list[list[str]] = []

    # 1. Title / Name
    album_title = album.get("title") or (deezer_details.get("title") if deezer_details else "") or ""
    fields.append(["name", str(album_title)])

    # 2. Release group or primary type
    rg_id = album.get("mb_release_group_id")
    if rg_id:
        fields.append(["release_group", str(rg_id)])
    else:
        raw_type = album.get("album_type") or (deezer_details.get("record_type") if deezer_details else None)
        fields.append(["type", _map_primary_type(raw_type)])

    # 3. Status
    fields.append(["status", "official"])

    # 4. Artist credit
    fields.extend(_build_artist_credit_fields(db, album, deezer_details))

    # 5. Date parts
    rel_date = album.get("release_date") or (deezer_details.get("release_date") if deezer_details else None)
    year = album.get("year")
    fields.extend(_parse_date_parts(rel_date, year))

    # 6. Deezer label and barcode
    fields.extend(_build_deezer_metadata_fields(deezer_details))

    # 7. Mediums and tracks
    fields.extend(_build_medium_fields(tracks))

    # 8 & 9. Deezer URL and edit note
    url_fields, edit_note = _build_deezer_url_and_note(deezer_id)
    fields.extend(url_fields)
    fields.append(["edit_note", edit_note])

    # 10. Redirect URI
    if redirect_uri:
        fields.append(["redirect_uri", str(redirect_uri)])

    return {"action_url": MB_RELEASE_ADD_URL, "fields": fields}


def link_album_release(
    db: Any,
    enricher: Any,
    album_id: str,
    release_mbid: str,
) -> dict[str, Any]:
    """Validates release_mbid, saves it to the album, and backfills release group if missing."""
    if not release_mbid or not isinstance(release_mbid, str):
        raise ValueError("Invalid MusicBrainz release ID")
    match = _MBID_RE.search(release_mbid.strip())
    if not match:
        raise ValueError(f"Invalid MusicBrainz release ID: {release_mbid}")
    clean_mbid = match.group(0).lower()

    album = db.get_library_album(album_id)
    if album is None:
        raise ValueError(f"Album not found: {album_id}")

    db.set_album_mb_release_id(album_id, clean_mbid)

    if not album.get("mb_release_group_id") and enricher is not None:
        rg_id = enricher.lookup_release_group_for_release(clean_mbid)
        if rg_id:
            db.set_album_mb_release_group_id(album_id, rg_id)

    updated = db.get_library_album(album_id)
    return updated if updated is not None else album
