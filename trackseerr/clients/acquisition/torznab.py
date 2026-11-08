"""Torznab / Newznab XML Indexer Client."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.quality import parse_release_title
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)


class IndexerError(RuntimeError):
    """Base exception for Torznab / Newznab indexer failures."""

    def __init__(self, message: str = "", code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class IndexerAuthError(IndexerError):
    """Raised when authentication with the indexer fails (HTTP 401/403 or code 100-102)."""


class IndexerRateLimited(IndexerError):
    """Raised when indexer rate limits or API request quotas are exceeded."""

    def __init__(
        self,
        message: str = "",
        code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after


class IndexerUnavailable(IndexerError):
    """Raised when indexer is unreachable, returns 5xx, invalid XML, or generic errors."""


class IndexerUnsupported(IndexerError):
    """Raised internally when indexer does not support music search (code 202/203)."""


_SEARCH_MODE: dict[str, tuple[str, float]] = {}
_SEARCH_MODE_LOCK = threading.Lock()
_SEARCH_MODE_TTL: float = 86400.0  # 24 hours in seconds


def reset_search_mode_cache() -> None:
    """Clears the search mode capabilities cache."""
    with _SEARCH_MODE_LOCK:
        _SEARCH_MODE.clear()


def _parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parses Retry-After header as seconds (numeric or HTTP date)."""
    if not header_val:
        return None
    val_str = header_val.strip()
    try:
        val = float(val_str)
        return int(val) if val.is_integer() else val
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val_str)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except (ValueError, TypeError):
        return None


def _raise_for_status(resp: httpx.Response) -> None:
    """Raises appropriate IndexerError for non-200 HTTP response codes."""
    if resp.status_code == 200:
        return
    if resp.status_code == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        raise IndexerRateLimited("HTTP 429", code=429, retry_after=retry_after)
    if resp.status_code in (401, 403):
        raise IndexerAuthError(f"HTTP {resp.status_code}", code=resp.status_code)
    raise IndexerUnavailable(f"HTTP {resp.status_code}", code=resp.status_code)


def _raise_for_error_reply(root: ET.Element) -> None:
    """Raises typed IndexerError if root is an <error> element."""
    tag = root.tag.split("}")[-1].lower()
    if tag != "error":
        return
    code_raw = root.get("code")
    code: Optional[int] = None
    if code_raw is not None:
        try:
            code = int(code_raw)
        except (ValueError, TypeError):
            pass
    description = root.get("description", "")
    msg = f"Indexer error {code}: {description}"
    if code in (100, 101, 102):
        raise IndexerAuthError(msg, code=code)
    if code in (500, 501):
        raise IndexerRateLimited(msg, code=code, retry_after=None)
    if code in (202, 203):
        raise IndexerUnsupported(msg, code=code)
    raise IndexerUnavailable(msg, code=code)


def _extract_error_description(xml_text: str) -> Optional[str]:
    """Returns description if xml_text is an <error> element, else None."""
    if "<error" not in xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
        tag = root.tag.split("}")[-1].lower()
        if tag == "error":
            return root.get("description") or f"Indexer error {root.get('code', 'unknown')}"
    except ET.ParseError:
        pass
    return None


VIDEO_TITLE_RE = re.compile(
    r"\b(?:"
    r"x264|x265|h\.?26[45]|hevc|xvid|divx|720p|1080p|2160p|4k|uhd|hdr"
    r"|bluray|blu-ray|bdrip|brrip|dvdrip|dvd5|dvd9|mdvdr|hdtv|webrip|web-dl|uhdtv"
    r")\b",
    re.IGNORECASE,
)


class TorznabDriver(AcquisitionDriver):
    """Client for Torznab / Newznab XML indexers (Prowlarr, Jackett, NZBGeek, etc.)."""

    def __init__(
        self,
        host_url: str,
        api_key: Optional[str] = None,
        categories: str = "3000,3010,3020,3030,3040",
        indexer_type: str = "torznab",
        timeout: float = 10.0,
    ) -> None:
        url = host_url.rstrip("/")
        if url.lower().endswith("/api"):
            url = url[:-4].rstrip("/")
        self.host_url = url
        self.api_key = (api_key or "").strip()
        self.categories = categories
        self.indexer_type = indexer_type
        self.timeout = timeout

    def _api_url(self, **params: Any) -> str:
        base = f"{self.host_url}/api?"
        query_items = []
        if self.api_key:
            query_items.append(f"apikey={self.api_key}")
        for k, v in params.items():
            if v is not None:
                query_items.append(f"{k}={quote(str(v))}")
        return base + "&".join(query_items)

    def test_connection(self) -> tuple[bool, str]:
        """Tests caps or basic search connectivity against the indexer."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        url = self._api_url(t="caps")
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                err_desc = _extract_error_description(resp.text)
                if err_desc:
                    return False, err_desc
                if resp.status_code == 200 and ("<caps" in resp.text or "<rss" in resp.text):
                    return True, f"{self.indexer_type.capitalize()} Indexer Online"
                # Fallback: test music search
                search_url = self._api_url(t="music", q="test")
                s_resp = client.get(search_url)
                s_err_desc = _extract_error_description(s_resp.text)
                if s_err_desc:
                    return False, s_err_desc
                if s_resp.status_code == 200 and "<rss" in s_resp.text:
                    return True, f"{self.indexer_type.capitalize()} Indexer Online"
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def _parse_item(  # noqa: C901
        self,
        item: ET.Element,
        default_artist: str = "",
        default_album: Optional[str] = None,
    ) -> Optional[AcquisitionSearchResult]:
        """Parses an XML <item> node into an AcquisitionSearchResult."""
        item_title = item.findtext("title", "")
        if not item_title:
            return None

        guid = item.findtext("guid", "")
        link = item.findtext("link", "")
        size_elem = item.findtext("size")
        size_bytes = int(size_elem) if (size_elem and size_elem.isdigit()) else 0

        # Check enclosure
        enclosure = item.find("enclosure")
        download_url = enclosure.get("url") if enclosure is not None else link
        if enclosure is not None and not size_bytes:
            enc_len = enclosure.get("length")
            if enc_len and enc_len.isdigit():
                size_bytes = int(enc_len)

        # Parse torznab:attr elements and categories
        seeders = None
        leechers = None
        bitrate = None
        fmt = None
        magnet_url = None
        categories: list[int | str] = []

        for cat_elem in item.findall("category"):
            if cat_elem.text and cat_elem.text.strip():
                txt = cat_elem.text.strip()
                categories.append(int(txt) if txt.isdigit() else txt)

        for elem in item:
            tag_lower = elem.tag.lower()
            if tag_lower.endswith("attr"):
                attr_name = elem.get("name", "").lower()
                attr_val = elem.get("value", "")
                if attr_name == "category" and attr_val.strip():
                    txt = attr_val.strip()
                    categories.append(int(txt) if txt.isdigit() else txt)
                elif attr_name == "seeders" and attr_val.isdigit():
                    seeders = int(attr_val)
                elif attr_name in ("peers", "leechers") and attr_val.isdigit():
                    leechers = int(attr_val)
                elif attr_name in ("bitrate", "audiobitrate") and attr_val.isdigit():
                    bitrate = int(attr_val)
                elif attr_name in ("format", "audioformat"):
                    fmt = attr_val.lower()
                elif attr_name == "magneturl":
                    magnet_url = attr_val

        # Video filter: drop item if category is 3020 or in 2000-2999 / 5000-5999,
        # or if the title matches video regex.
        if VIDEO_TITLE_RE.search(item_title):
            return None

        for cat in categories:
            val = int(cat) if isinstance(cat, int) or (isinstance(cat, str) and cat.isdigit()) else None
            if val is not None and (val == 3020 or 2000 <= val <= 2999 or 5000 <= val <= 5999):
                return None

        if download_url and download_url.startswith("magnet:") and not magnet_url:
            magnet_url = download_url
        elif magnet_url and not download_url:
            download_url = magnet_url

        # Quality string deduction
        quality_str = None
        lower_title = item_title.lower()
        if "flac" in lower_title:
            quality_str = "FLAC"
        elif "320" in lower_title or (bitrate and bitrate >= 320):
            quality_str = "MP3 320kbps"
        elif "v0" in lower_title:
            quality_str = "MP3 V0"

        artist = default_artist
        album = default_album
        if not artist:
            parsed = parse_release_title(item_title)
            artist = parsed.artist or ""
            if not album:
                album = parsed.album

        return AcquisitionSearchResult(
            download_id=guid or link or item_title,
            title=item_title,
            artist=artist,
            album=album,
            item_type="album" if album else "track",
            size_bytes=size_bytes,
            bit_rate=bitrate,
            format=fmt,
            quality_str=quality_str,
            seeders=seeders,
            leechers=leechers,
            download_url=download_url,
            magnet_url=magnet_url,
            source=self.indexer_type,
            extra={"raw_title": item_title, "categories": categories},
        )

    def _get_search_mode(self) -> str:
        now = time.monotonic()
        with _SEARCH_MODE_LOCK:
            if self.host_url in _SEARCH_MODE:
                mode, ts = _SEARCH_MODE[self.host_url]
                if now - ts < _SEARCH_MODE_TTL:
                    return mode

        url = self._api_url(t="caps")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                logger.debug("Caps probe HTTP %d; defaulting to music", resp.status_code)
                return "music"
            root = ET.fromstring(resp.text)
            tag = root.tag.split("}")[-1].lower()
            if tag == "error":
                logger.debug("Caps probe returned error reply; defaulting to music")
                return "music"
            if tag != "caps":
                logger.debug("Caps probe returned non-caps XML <%s>; defaulting to music", tag)
                return "music"

            searching = root.find("searching")
            music_search = searching.find("music-search") if searching is not None else None
            if music_search is not None and music_search.get("available", "").lower() == "yes":
                mode = "music"
            else:
                mode = "search"

            with _SEARCH_MODE_LOCK:
                _SEARCH_MODE[self.host_url] = (mode, time.monotonic())
            return mode
        except (httpx.HTTPError, httpx.TimeoutException, ET.ParseError) as e:
            logger.debug("Caps probe failed (%s); defaulting to music", e)
            return "music"

    def _query(
        self,
        t: str,
        q: str,
        artist: str = "",
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        url = self._api_url(t=t, q=q, cat=self.categories)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            raise IndexerUnavailable(str(e)) from e

        _raise_for_status(resp)

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise IndexerUnavailable("Unreadable reply from indexer") from e

        _raise_for_error_reply(root)

        channel = root.find("channel")
        if channel is None:
            return []

        results: list[AcquisitionSearchResult] = []
        for item in channel.findall("item"):
            res = self._parse_item(item, default_artist=artist, default_album=album)
            if res is not None:
                results.append(res)

        return results

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """Queries indexer for music releases and parses Torznab XML attributes."""
        if not is_safe_service_url(self.host_url):
            logger.error("Prohibited indexer host URL: %s", self.host_url)
            return []

        query_parts = [artist.strip()]
        if title:
            query_parts.append(title.strip())
        elif album:
            query_parts.append(album.strip())
        query = " ".join(query_parts)

        mode = self._get_search_mode()
        if mode == "music":
            try:
                return self._query(t="music", q=query, artist=artist, album=album)
            except IndexerUnsupported:
                with _SEARCH_MODE_LOCK:
                    _SEARCH_MODE[self.host_url] = ("search", time.monotonic())
                return self._query(t="search", q=query, artist=artist, album=album)

        return self._query(t="search", q=query, artist=artist, album=album)

    def fetch_recent(self, limit: int = 100) -> list[AcquisitionSearchResult]:
        """Polls recent indexer releases without search queries."""
        if not is_safe_service_url(self.host_url):
            logger.error("Prohibited indexer host URL: %s", self.host_url)
            return []

        def _fetch_channel_items(t_val: str) -> list[AcquisitionSearchResult]:
            url = self._api_url(t=t_val, cat=self.categories, limit=limit)
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url)
                    if resp.status_code != 200:
                        logger.debug("Torznab fetch_recent (t=%s) returned HTTP %d", t_val, resp.status_code)
                        return []
                    xml_text = resp.text
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                logger.debug("Torznab fetch_recent network error (t=%s): %s", t_val, e)
                return []

            try:
                root = ET.fromstring(xml_text)
                tag = root.tag.split("}")[-1].lower()
                if tag == "error":
                    code = root.get("code")
                    desc = root.get("description", "")
                    logger.warning("Torznab fetch_recent error reply: code=%s description=%s", code, desc)
                    return []
                channel = root.find("channel")
                if channel is None:
                    return []
                items = channel.findall("item")
                results: list[AcquisitionSearchResult] = []
                for item in items:
                    parsed_res = self._parse_item(item)
                    if parsed_res is not None:
                        results.append(parsed_res)
                return results
            except (ET.ParseError, ValueError) as e:
                logger.debug("Torznab fetch_recent XML parse error (t=%s): %s", t_val, e)
                return []

        try:
            # 1. Try t="music"
            results = _fetch_channel_items("music")
            if results:
                return results

            # 2. Fall back to t="search" if t="music" returns empty channel or non-200
            return _fetch_channel_items("search")
        except Exception as e:
            logger.error("Unexpected error in fetch_recent: %s", e)
            return []

    def fetch_page(
        self, offset: int = 0, limit: int = 100
    ) -> list[AcquisitionSearchResult]:
        """Paginates indexer releases using t=search without query."""
        if not is_safe_service_url(self.host_url):
            logger.error("Prohibited indexer host URL: %s", self.host_url)
            return []

        url = self._api_url(t="search", cat=self.categories, offset=offset, limit=limit)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            raise IndexerUnavailable(str(e)) from e

        _raise_for_status(resp)

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise IndexerUnavailable("Unreadable reply from indexer") from e

        _raise_for_error_reply(root)

        channel = root.find("channel")
        if channel is None:
            return []

        results: list[AcquisitionSearchResult] = []
        for item in channel.findall("item"):
            res = self._parse_item(item)
            if res is not None:
                results.append(res)

        return results

    def fetch_torrent(
        self, url: str, max_bytes: int = 10 * 1024 * 1024
    ) -> bytes | str:
        """Fetches a torrent file by following up to 3 redirects manually.

        Returns bytes of the torrent file, or a magnet URL string if redirected to magnet.
        Never logs the URL as it carries API keys / passkeys.
        """
        current_url = url
        for _hop in range(4):
            if current_url.startswith("magnet:"):
                return current_url

            if not is_safe_service_url(current_url):
                raise IndexerUnavailable("Prohibited torrent URL (SSRF defense)")

            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    with client.stream("GET", current_url) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = resp.headers.get("Location")
                            if not loc:
                                raise IndexerUnavailable(
                                    f"HTTP {resp.status_code} redirect without Location header"
                                )
                            if loc.startswith("magnet:"):
                                return loc
                            current_url = str(httpx.URL(current_url).join(loc))
                            continue

                        _raise_for_status(resp)

                        cl = resp.headers.get("Content-Length")
                        if cl and cl.isdigit() and int(cl) > max_bytes:
                            raise IndexerUnavailable(
                                f"Torrent file exceeds size limit of {max_bytes} bytes"
                            )

                        chunks: list[bytes] = []
                        total_bytes = 0
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            total_bytes += len(chunk)
                            if total_bytes > max_bytes:
                                raise IndexerUnavailable(
                                    f"Torrent file exceeds size limit of {max_bytes} bytes"
                                )
                            chunks.append(chunk)

                        return b"".join(chunks)

            except (httpx.HTTPError, httpx.TimeoutException) as e:
                raise IndexerUnavailable(str(e)) from e

        raise IndexerUnavailable("Too many redirects fetching torrent")

    def download(self, result: AcquisitionSearchResult) -> str:
        """Torznab is an indexer; downloads are dispatched to a download client."""
        return result.magnet_url or result.download_url or result.download_id

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Torznab is an indexer; does not hold active transfers."""
        return {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "size_bytes": 0,
            "speed_bps": 0,
            "eta_seconds": 0,
            "source_path": None,
            "error_message": None,
        }

    def cancel(self, download_id: str) -> bool:
        """Torznab is an indexer; cannot cancel transfers."""
        return True


__all__ = [
    "IndexerAuthError",
    "IndexerError",
    "IndexerRateLimited",
    "IndexerUnavailable",
    "IndexerUnsupported",
    "TorznabDriver",
    "VIDEO_TITLE_RE",
    "reset_search_mode_cache",
]
