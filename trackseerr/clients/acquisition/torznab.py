"""Torznab / Newznab XML Indexer Client."""

import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.quality import parse_release_title
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)


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
        self.host_url = host_url.rstrip("/")
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
                if resp.status_code == 200 and ("<caps" in resp.text or "<rss" in resp.text):
                    return True, f"{self.indexer_type.capitalize()} Indexer Online"
                # Fallback: test music search
                search_url = self._api_url(t="music", q="test")
                s_resp = client.get(search_url)
                if s_resp.status_code == 200 and "<rss" in s_resp.text:
                    return True, f"{self.indexer_type.capitalize()} Indexer Online"
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def _parse_item(
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

        # Parse torznab:attr elements
        seeders = None
        leechers = None
        bitrate = None
        fmt = None
        magnet_url = None

        for elem in item:
            tag_lower = elem.tag.lower()
            if tag_lower.endswith("attr"):
                attr_name = elem.get("name", "").lower()
                attr_val = elem.get("value", "")
                if attr_name == "seeders" and attr_val.isdigit():
                    seeders = int(attr_val)
                elif attr_name in ("peers", "leechers") and attr_val.isdigit():
                    leechers = int(attr_val)
                elif attr_name in ("bitrate", "audiobitrate") and attr_val.isdigit():
                    bitrate = int(attr_val)
                elif attr_name in ("format", "audioformat"):
                    fmt = attr_val.lower()
                elif attr_name == "magneturl":
                    magnet_url = attr_val

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
            extra={"raw_title": item_title},
        )

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

        url = self._api_url(t="music", q=query, cat=self.categories)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    logger.warning("Torznab search failed (HTTP %d): %s", resp.status_code, resp.text[:100])
                    return []
                xml_text = resp.text

            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                return []

            results: list[AcquisitionSearchResult] = []
            for item in channel.findall("item"):
                res = self._parse_item(item, default_artist=artist, default_album=album)
                if res is not None:
                    results.append(res)

            return results
        except (httpx.HTTPError, httpx.TimeoutException, ET.ParseError, ValueError) as e:
            logger.error("Error parsing Torznab search results: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected error in Torznab search: %s", e)
            return []

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
