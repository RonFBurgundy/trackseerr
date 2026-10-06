"""qBittorrent WebAPI v2 Acquisition Driver."""

import logging
import re
from typing import Any, Optional

import httpx

from plex_playlist_sync.clients.acquisition.base import AcquisitionDriver
from plex_playlist_sync.models import AcquisitionSearchResult, DownloadStatus
from plex_playlist_sync.security import is_safe_service_url

logger = logging.getLogger(__name__)

_BTIH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


class QbittorrentDriver(AcquisitionDriver):
    """Driver for qBittorrent Web API v2."""

    def __init__(
        self,
        host_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        category: str = "trackseerr",
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.username = username
        self.password = password
        self.category = category
        self.timeout = timeout
        self._cookie: Optional[str] = None

    def _login(self, client: httpx.Client) -> bool:
        """Authenticates with qBittorrent and caches the SID cookie."""
        if not self.username and not self.password:
            return True
        login_url = f"{self.host_url}/api/v2/auth/login"
        try:
            resp = client.post(
                login_url,
                data={"username": self.username or "", "password": self.password or ""},
            )
            if resp.status_code == 200 and "Ok." in resp.text:
                self._cookie = resp.headers.get("set-cookie")
                return True
            return False
        except Exception as e:
            logger.warning("qBittorrent login exception: %s", e)
            return False

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity and authentication against qBittorrent."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0) as client:
                if self.username or self.password:
                    if not self._login(client):
                        return False, "Authentication failed (invalid credentials)"
                version_url = f"{self.host_url}/api/v2/app/version"
                headers = {"Cookie": self._cookie} if self._cookie else {}
                resp = client.get(version_url, headers=headers)
                if resp.status_code == 200:
                    return True, f"qBittorrent {resp.text.strip()}"
                return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """qBittorrent is a downloader; search is handled via Torznab indexers."""
        return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Submits magnet or torrent link to qBittorrent."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        target_url = result.magnet_url or result.download_url
        if not target_url:
            raise ValueError("No magnet or download URL provided for torrent")

        # Determine torrent hash
        torrent_hash = result.download_id
        btih_match = _BTIH_RE.search(target_url)
        if btih_match:
            torrent_hash = btih_match.group(1).lower()

        add_url = f"{self.host_url}/api/v2/torrents/add"
        payload = {
            "urls": target_url,
            "category": self.category,
            "paused": "false",
        }

        with httpx.Client(timeout=self.timeout) as client:
            if self._cookie:
                client.headers["Cookie"] = self._cookie
            else:
                self._login(client)
                if self._cookie:
                    client.headers["Cookie"] = self._cookie

            resp = client.post(add_url, data=payload)
            if resp.status_code != 200 or "Fails." in resp.text:
                raise RuntimeError(f"qBittorrent torrents/add failed (HTTP {resp.status_code}): {resp.text[:120]}")

        return torrent_hash

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Polls qBittorrent for torrent download status and metrics."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        info_url = f"{self.host_url}/api/v2/torrents/info?hashes={download_id.lower()}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
                else:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie

                resp = client.get(info_url)
                if resp.status_code == 403:
                    # Retry login once
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie
                    resp = client.get(info_url)

                if resp.status_code != 200:
                    return {
                        "status": DownloadStatus.FAILED.value,
                        "error_message": f"HTTP {resp.status_code}",
                    }

                items = resp.json()
                if not items or not isinstance(items, list):
                    return {
                        "status": DownloadStatus.QUEUED.value,
                        "progress": 0.0,
                        "size_bytes": 0,
                        "speed_bps": 0,
                        "eta_seconds": 0,
                        "source_path": None,
                        "ratio": 0.0,
                        "seeding_time_seconds": 0,
                        "error_message": None,
                    }

                tor = items[0]
                state = str(tor.get("state", "")).lower()
                progress_raw = float(tor.get("progress", 0.0))
                progress = min(100.0, round(progress_raw * 100.0, 1))
                size = int(tor.get("total_size") or tor.get("size") or 0)
                speed = int(tor.get("dlspeed", 0))
                eta = int(tor.get("eta", 0))
                source_path = tor.get("content_path") or tor.get("save_path")
                ratio = float(tor.get("ratio", 0.0))
                seeding_time = int(tor.get("seeding_time") or tor.get("time_seeded") or 0)

                status_str = DownloadStatus.DOWNLOADING.value
                if any(s in state for s in ("uploading", "pausedup", "queuedup", "stalledup")):
                    status_str = DownloadStatus.COMPLETED.value
                    progress = 100.0
                elif any(s in state for s in ("pauseddl", "queueddl", "allocating", "checking")):
                    status_str = DownloadStatus.QUEUED.value
                elif any(s in state for s in ("error", "missingfiles")):
                    status_str = DownloadStatus.FAILED.value

                return {
                    "status": status_str,
                    "progress": progress,
                    "size_bytes": size,
                    "speed_bps": speed,
                    "eta_seconds": eta if eta < 8640000 else 0,
                    "source_path": source_path,
                    "ratio": ratio,
                    "seeding_time_seconds": seeding_time,
                    "error_message": None,
                }
        except Exception as e:
            logger.warning("Error fetching qBittorrent status for %s: %s", download_id, e)
            return {
                "status": DownloadStatus.FAILED.value,
                "progress": 0.0,
                "size_bytes": 0,
                "speed_bps": 0,
                "eta_seconds": 0,
                "source_path": None,
                "ratio": 0.0,
                "seeding_time_seconds": 0,
                "error_message": str(e),
            }

    def cancel(self, download_id: str) -> bool:
        """Deletes torrent and downloaded files from qBittorrent."""
        if not is_safe_service_url(self.host_url):
            return False

        del_url = f"{self.host_url}/api/v2/torrents/delete"
        payload = {"hashes": download_id.lower(), "deleteFiles": "true"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
                else:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie

                resp = client.post(del_url, data=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.error("Failed to delete qBittorrent torrent %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes completed torrent from qBittorrent with optional file deletion."""
        if not is_safe_service_url(self.host_url):
            return False

        del_url = f"{self.host_url}/api/v2/torrents/delete"
        payload = {"hashes": download_id.lower(), "deleteFiles": str(delete_files).lower()}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
                else:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie

                resp = client.post(del_url, data=payload)
                if resp.status_code == 403:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie
                    resp = client.post(del_url, data=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.error("Failed to cleanup completed qBittorrent torrent %s: %s", download_id, e)
            return False

    def set_share_limits(
        self, lookup: str, ratio: Optional[float], seed_time_minutes: Optional[int]
    ) -> bool:
        """Sets per-torrent share limits via torrents/setShareLimits.

        qBittorrent semantics: -2 = use the global setting, -1 = unlimited. So ``None`` maps to -2 and ``0``
        (no requirement) to -1. The inactive-seeding limit is always left on the global setting.
        """
        if not is_safe_service_url(self.host_url):
            return False
        ratio_limit: float = -2 if ratio is None else (-1 if float(ratio) <= 0 else float(ratio))
        time_limit: int = -2 if seed_time_minutes is None else (-1 if int(seed_time_minutes) <= 0 else int(seed_time_minutes))
        url = f"{self.host_url}/api/v2/torrents/setShareLimits"
        payload = {
            "hashes": lookup.lower(),
            "ratioLimit": ratio_limit,
            "seedingTimeLimit": time_limit,
            "inactiveSeedingTimeLimit": -2,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
                else:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie
                resp = client.post(url, data=payload)
                if resp.status_code == 403:
                    self._login(client)
                    if self._cookie:
                        client.headers["Cookie"] = self._cookie
                    resp = client.post(url, data=payload)
                if resp.status_code != 200:
                    logger.warning("qBittorrent setShareLimits failed for %s (HTTP %s)", lookup, resp.status_code)
                    return False
                return True
        except httpx.HTTPError as e:
            logger.warning("qBittorrent setShareLimits failed for %s: %s", lookup, e)
            return False
