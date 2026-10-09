"""Release calendar API and iCalendar (RFC 5545) feed.

Provides release dates and acquisition status for monitored artists in native and Lidarr modes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_lidarr_client,
    require_admin,
    require_core_tier,
    verify_feed_access,
)
from trackseerr.api.routes.activity import require_lidarr
from trackseerr.api.routes.library._shared import _versioned_art_url
from trackseerr.api.schemas.calendar import CalendarItem, CalendarStatus
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.config import Config
from trackseerr.library_manager import MODE_LIDARR, get_library_mode
from trackseerr import lidarr_library
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier)])

MAX_CALENDAR_RANGE_DAYS = 120
DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 30
FEED_PAST_DAYS = 30
FEED_FUTURE_DAYS = 180

_DATE_PART_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def parse_release_date_range(release_date: Any, year: Optional[int] = None) -> Optional[tuple[date, date, date]]:
    """Extracts (representative_date, start_date, end_date) from an album release_date or fallback year."""
    if release_date:
        s = str(release_date).strip()
        if "T" in s:
            s = s.split("T")[0]
        m = _DATE_PART_RE.match(s)
        if m:
            y = int(m.group(1))
            if m.group(2) and m.group(3):
                try:
                    d = date(y, int(m.group(2)), int(m.group(3)))
                    return (d, d, d)
                except ValueError:
                    pass
            elif m.group(2):
                try:
                    mo = int(m.group(2))
                    start_m = date(y, mo, 1)
                    if mo == 12:
                        end_m = date(y, 12, 31)
                    else:
                        end_m = date(y, mo + 1, 1) - timedelta(days=1)
                    return (start_m, start_m, end_m)
                except ValueError:
                    pass
            else:
                try:
                    start_y = date(y, 1, 1)
                    end_y = date(y, 12, 31)
                    return (start_y, start_y, end_y)
                except ValueError:
                    pass
    if year is not None:
        try:
            y = int(year)
            return (date(y, 1, 1), date(y, 1, 1), date(y, 12, 31))
        except (ValueError, TypeError):
            pass
    return None


def parse_release_date(release_date: Any, year: Optional[int] = None) -> Optional[date]:
    """Extracts a valid date from an album release_date or fallback year."""
    bounds = parse_release_date_range(release_date, year)
    return bounds[0] if bounds else None


def derive_calendar_status(
    track_file_count: int,
    total_tracks: int,
    release_date: date,
    today: date,
) -> CalendarStatus:
    """Derives release status ∈ {downloaded, partial, missing, upcoming} from track counts and release date."""
    if track_file_count > 0 and (total_tracks <= 0 or track_file_count >= total_tracks):
        return "downloaded"
    if 0 < track_file_count < total_tracks:
        return "partial"
    if release_date > today:
        return "upcoming"
    return "missing"


def _validate_date_param(param_val: Optional[str], param_name: str) -> Optional[date]:
    if not param_val:
        return None
    try:
        return date.fromisoformat(param_val.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {param_name} date format. Expected YYYY-MM-DD",
        ) from exc


def _resolve_calendar_range(start_str: Optional[str], end_str: Optional[str]) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    start_d = _validate_date_param(start_str, "start") or (today - timedelta(days=DEFAULT_PAST_DAYS))
    end_d = _validate_date_param(end_str, "end") or (today + timedelta(days=DEFAULT_FUTURE_DAYS))

    if end_d < start_d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be on or after start date",
        )
    if (end_d - start_d).days > MAX_CALENDAR_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Date range exceeds maximum of {MAX_CALENDAR_RANGE_DAYS} days",
        )
    return start_d, end_d


def _get_native_calendar(
    db: Database,
    start_date: date,
    end_date: date,
    unmonitored: bool,
    today: date,
) -> list[CalendarItem]:
    with db._lock:
        cur = db.conn.execute(
            """
            SELECT al.id, al.artist_id, ar.name AS artist_name, al.title, al.album_type,
                   al.release_date, al.year, al.monitored AS album_monitored, ar.monitored AS artist_monitored,
                   al.total_tracks, al.cover_url, al.art_version
            FROM library_albums al
            JOIN library_artists ar ON ar.id = al.artist_id
            """
        )
        album_rows = cur.fetchall()

    filtered_albums: list[tuple[dict[str, Any], date]] = []
    for row in album_rows:
        al = dict(row)
        date_info = parse_release_date_range(al.get("release_date"), al.get("year"))
        if not date_info:
            continue
        rdate, min_d, max_d = date_info
        if not (min_d <= end_date and max_d >= start_date):
            continue
        alb_mon = bool(al.get("album_monitored", 1))
        art_mon = bool(al.get("artist_monitored", 1))
        if not unmonitored and (not alb_mon or not art_mon):
            continue
        filtered_albums.append((al, rdate))

    if not filtered_albums:
        return []

    album_ids = [al["id"] for al, _ in filtered_albums]
    placeholders = ",".join("?" for _ in album_ids)
    with db._lock:
        cur = db.conn.execute(
            f"""
            SELECT t.album_id, COUNT(t.id),
                   SUM(CASE WHEN EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) THEN 1 ELSE 0 END)
            FROM library_tracks t
            WHERE t.album_id IN ({placeholders})
            GROUP BY t.album_id
            """,
            album_ids,
        )
        counts_data = {r[0]: (int(r[1]), int(r[2] or 0)) for r in cur.fetchall()}

    items: list[CalendarItem] = []
    for al, rdate in filtered_albums:
        aid = al["id"]
        stored_tracks, file_count = counts_data.get(aid, (0, 0))
        total_tracks = int(al.get("total_tracks") or stored_tracks or 0)
        status_val = derive_calendar_status(file_count, total_tracks, rdate, today)
        cover = _versioned_art_url("album", aid, al.get("art_version"), al.get("cover_url"))

        items.append(
            CalendarItem(
                id=str(aid),
                artist_id=str(al["artist_id"]),
                artist_name=str(al.get("artist_name") or "Unknown Artist"),
                title=str(al.get("title") or "Unknown Album"),
                album_type=str(al.get("album_type") or "album"),
                release_date=rdate.isoformat(),
                monitored=bool(al.get("album_monitored", 1)),
                status=status_val,
                cover_url=cover,
            )
        )

    items.sort(key=lambda item: (item.release_date, item.artist_name.lower(), item.title.lower()))
    return items


def _get_lidarr_calendar(
    client: Optional[LidarrClient],
    start_date: date,
    end_date: date,
    unmonitored: bool,
    today: date,
) -> list[CalendarItem]:
    lidarr = require_lidarr(client)
    album_rows = lidarr_library.snapshot("albums", lidarr)
    raw_artists = lidarr_library.snapshot("artists", lidarr)
    artist_rows: dict[str, Any] = {}
    for r in raw_artists:
        art_id = str(getattr(r, "id", None) or (r.get("id") if isinstance(r, dict) else ""))
        if art_id:
            artist_rows[art_id] = r

    items: list[CalendarItem] = []
    for row in album_rows:
        rec = getattr(row, "record", None) or (row if isinstance(row, dict) else {})
        date_info = parse_release_date_range(rec.get("release_date") or rec.get("releaseDate"), rec.get("year"))
        if not date_info:
            continue
        rdate, min_d, max_d = date_info
        if not (min_d <= end_date and max_d >= start_date):
            continue

        aid = str(rec.get("id") or getattr(row, "id", ""))
        art_id = str(rec.get("artist_id") or rec.get("artistId") or (rec.get("artist") or {}).get("id") or "")
        art_row = artist_rows.get(art_id)
        if art_row is not None:
            if hasattr(art_row, "monitored"):
                art_mon = bool(art_row.monitored)
            elif isinstance(art_row, dict):
                art_mon = bool(art_row.get("monitored", True))
            else:
                art_mon = True
        else:
            art_mon = True

        alb_mon = bool(rec.get("monitored", True))
        if not unmonitored and (not alb_mon or not art_mon):
            continue

        stats = rec.get("statistics") if isinstance(rec.get("statistics"), dict) else {}
        file_count = int(rec.get("track_file_count") or stats.get("trackFileCount") or 0)
        total_tracks = int(
            rec.get("total_tracks")
            or rec.get("track_count")
            or stats.get("totalTrackCount")
            or stats.get("trackCount")
            or 0
        )
        status_val = derive_calendar_status(file_count, total_tracks, rdate, today)

        cover_url = rec.get("cover_url")
        if not cover_url:
            images = rec.get("images") or []
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and img.get("coverType") == "cover" and img.get("url"):
                        raw_u = str(img["url"])
                        cover_url = f"/api/lidarr{raw_u}" if raw_u.startswith("/") else f"/api/lidarr/{raw_u}"
                        break

        items.append(
            CalendarItem(
                id=aid,
                artist_id=art_id,
                artist_name=str(
                    rec.get("artist_name")
                    or (rec.get("artist") or {}).get("artistName")
                    or "Unknown Artist"
                ),
                title=str(rec.get("title") or "Unknown Album"),
                album_type=str(rec.get("album_type") or rec.get("albumType") or "album"),
                release_date=rdate.isoformat(),
                monitored=alb_mon,
                status=status_val,
                cover_url=cover_url,
            )
        )

    items.sort(key=lambda item: (item.release_date, item.artist_name.lower(), item.title.lower()))
    return items


def _collect_calendar_items(
    db: Database,
    client: Optional[LidarrClient],
    start_date: date,
    end_date: date,
    unmonitored: bool,
) -> list[CalendarItem]:
    today = datetime.now(timezone.utc).date()
    if get_library_mode(db) == MODE_LIDARR:
        return _get_lidarr_calendar(client, start_date, end_date, unmonitored, today)
    return _get_native_calendar(db, start_date, end_date, unmonitored, today)


@router.get("", response_model=list[CalendarItem], response_model_exclude_unset=True, summary="Release calendar")
@router.get("/", response_model=list[CalendarItem], response_model_exclude_unset=True, include_in_schema=False)
def get_calendar(
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD), defaults to today-7"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD), defaults to today+30"),
    unmonitored: bool = Query(False, description="Include unmonitored albums and artists"),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[CalendarItem]:
    """Retrieves album releases within the given date range for library artists."""
    start_d, end_d = _resolve_calendar_range(start, end)
    return _collect_calendar_items(db, client, start_d, end_d, unmonitored)


# RFC 5545 Helpers for iCalendar feed generation


def escape_ical_text(value: str) -> str:
    """Escapes backslashes, semicolons, commas and newlines per RFC 5545 section 3.3.11."""
    res = value.replace("\\", "\\\\")
    res = res.replace(";", "\\;")
    res = res.replace(",", "\\,")
    res = res.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return res


def fold_ical_line(line: str) -> str:
    """Folds a single content line at 75 octets per RFC 5545 section 3.1."""
    raw_bytes = line.encode("utf-8")
    if len(raw_bytes) <= 75:
        return line

    chunks: list[bytes] = []
    pos = 0
    while pos < len(raw_bytes):
        limit = 75 if pos == 0 else 74
        end = min(pos + limit, len(raw_bytes))
        while end > pos and (raw_bytes[end : end + 1] and (raw_bytes[end] & 0xC0 == 0x80)):
            end -= 1
        if end == pos:
            end = min(pos + limit, len(raw_bytes))
        chunks.append(raw_bytes[pos:end])
        pos = end

    return b"\r\n ".join(chunks).decode("utf-8")


def generate_ical_feed(items: list[CalendarItem], now_dt: Optional[datetime] = None) -> str:
    """Builds RFC 5545 VCALENDAR payload with one all-day VEVENT per album."""
    now = now_dt or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Trackseerr//Release Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Trackseerr Releases",
    ]

    for item in items:
        # DTSTART;VALUE=DATE requires YYYYMMDD
        clean_date = item.release_date.replace("-", "")
        try:
            d_obj = date.fromisoformat(item.release_date)
            next_day_obj = d_obj + timedelta(days=1)
            end_date_str = next_day_obj.strftime("%Y%m%d")
        except ValueError:
            end_date_str = clean_date
        uid = f"album-{item.id}@trackseerr"
        summary = escape_ical_text(f"{item.artist_name} - {item.title}")
        description = escape_ical_text(f"Status: {item.status} | Type: {item.album_type or 'album'}")

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{clean_date}",
                f"DTEND;VALUE=DATE:{end_date_str}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    folded_lines = [fold_ical_line(line) for line in lines]
    return "\r\n".join(folded_lines) + "\r\n"


@router.get("/feed.ics", summary="Release calendar iCal feed")
def calendar_feed_ics(
    request: Request,
    token: Optional[str] = Query(None, description="Feed token or API key for calendar subscribers"),
    unmonitored: bool = Query(False, description="Include unmonitored albums in feed"),
    user_context: dict[str, Any] = Depends(verify_feed_access),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> Response:
    """Generates an RFC 5545 VCALENDAR iCal feed with upcoming and recent releases."""
    today = datetime.now(timezone.utc).date()
    start_d = today - timedelta(days=FEED_PAST_DAYS)
    end_d = today + timedelta(days=FEED_FUTURE_DAYS)

    items = _collect_calendar_items(db, client, start_d, end_d, unmonitored)
    ics_payload = generate_ical_feed(items)

    return Response(
        content=ics_payload,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="trackseerr-releases.ics"',
            "Cache-Control": "no-cache",
        },
    )
