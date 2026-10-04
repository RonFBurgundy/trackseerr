"""Shared pieces of the import list providers: the item shape, the error type and a retrying JSON GET."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 4
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 30.0
ITEM_KINDS = ("artist", "album", "track")
MAX_LIMIT = 1000
_DEFAULT_HEADERS = {"User-Agent": "TrackSeerr/1.0 (https://github.com/RonFBurgundy/trackseerr)", "Accept": "application/json"}


class ImportListError(Exception):
    """A provider fetch failed. The message is safe to show to the admin (it never carries credentials)."""


@dataclass
class ImportListItem:
    """One thing an import list asks for.

    ``external_key`` is stable across syncs of the same list (an MBID where the provider supplies one, else a
    normalised name) so a re-sync recognises an item it already applied. ``mbid`` is the item's own MusicBrainz id
    (artist, release group or recording) and ``artist_mbid`` the artist's, when the provider knows them.
    """

    kind: str
    external_key: str
    artist_name: str = ""
    album_title: str = ""
    track_title: str = ""
    mbid: Optional[str] = None
    artist_mbid: Optional[str] = None

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "external_key": self.external_key,
            "mbid": self.mbid,
            "artist_mbid": self.artist_mbid,
            "artist_name": self.artist_name,
            "album_title": self.album_title,
            "track_title": self.track_title,
        }


def name_key(*parts: str) -> str:
    """Stable key from names: casefolded, whitespace-collapsed parts joined with ``|``."""
    return "|".join(" ".join((p or "").split()).casefold() for p in parts)


def clamp_limit(value: Any, default: int) -> int:
    """``value`` as an int in ``[1, MAX_LIMIT]``; ``default`` when blank, ImportListError when not a number."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ImportListError("limit must be a whole number") from exc
    return max(1, min(MAX_LIMIT, number))


def require_text(config: dict[str, Any], key: str, label: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ImportListError(f"{label} is required")
    return value


def get_json(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    ok_statuses: tuple[int, ...] = (200,),
) -> Optional[Any]:
    """GET ``url`` and return the parsed JSON body, retrying 429/5xx and network errors with backoff.

    A status in ``ok_statuses`` other than 200 (for example 204 "no content") returns ``None``. Anything else raises
    ImportListError with a message carrying only the status code, never the URL, which can hold an API key.
    """
    last_error = "request failed"
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(
                url, params=params, headers={**_DEFAULT_HEADERS, **(headers or {})}, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = f"could not reach the provider ({type(exc).__name__})"
            logger.warning("Import list request failed (attempt %d/%d): %s", attempt + 1, MAX_ATTEMPTS, type(exc).__name__)
        else:
            if resp.status_code in _RETRY_STATUSES:
                last_error = f"provider returned HTTP {resp.status_code}"
                logger.warning("Import list provider HTTP %d (attempt %d/%d)", resp.status_code, attempt + 1, MAX_ATTEMPTS)
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(_backoff_seconds(resp, attempt))
                continue
            if resp.status_code in ok_statuses:
                if resp.status_code != 200:
                    return None
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ImportListError("provider returned invalid JSON") from exc
            raise ImportListError(f"provider returned HTTP {resp.status_code}")
        if attempt + 1 < MAX_ATTEMPTS:
            time.sleep(min(_MAX_BACKOFF_SECONDS, 2.0**attempt))
    raise ImportListError(last_error)


def _backoff_seconds(resp: "requests.Response", attempt: int) -> float:
    header = resp.headers.get("Retry-After") if resp.headers else None
    if header:
        try:
            return max(0.0, min(_MAX_BACKOFF_SECONDS, float(header)))
        except ValueError:
            pass
    return min(_MAX_BACKOFF_SECONDS, 2.0**attempt)
