"""Pure helpers for keeping an album's original release year."""

from datetime import datetime
from typing import Any, Optional

MIN_PLAUSIBLE_YEAR = 1900


def is_placeholder_date(value: Any) -> bool:
    """Return True for placeholder / sentinel date values (< 1000, e.g. year 0, 1, '0001-01-01...')."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value < 1000
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        if len(text) == 4 or not text[4].isdigit():
            return int(text[:4]) < 1000
    elif text.isdigit():
        return int(text) < 1000
    return False


def plausible_year(value: Any) -> Optional[int]:
    """Coerce an int / numeric string / 'YYYY-MM-DD' to a year, or None when implausible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        year = value
    else:
        text = str(value).strip()
        if len(text) < 4 or not text[:4].isdigit():
            return None
        if len(text) > 4 and text[4].isdigit():
            return None
        year = int(text[:4])
    if MIN_PLAUSIBLE_YEAR <= year <= datetime.now().year + 1:
        return year
    return None


def merge_album_year(existing: Any, incoming: Any) -> Optional[int]:
    """Earliest plausible year of the two; an implausible one loses to a plausible one."""
    old = plausible_year(existing)
    new = plausible_year(incoming)
    if old is None:
        return new
    if new is None:
        return old
    return min(old, new)


def merge_release_date(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    """Keep the date with the earlier plausible year; existing wins ties and double-implausible."""
    old_year = plausible_year(existing)
    new_year = plausible_year(incoming)
    if new_year is not None and (old_year is None or new_year < old_year):
        return incoming
    return existing
