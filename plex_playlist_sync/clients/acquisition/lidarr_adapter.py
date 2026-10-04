"""Lidarr Acquisition Driver Adapter fulfilling AcquisitionDriver interface."""

import logging
from typing import Any, Optional

import httpx

from plex_playlist_sync import lidarr_library
from plex_playlist_sync.clients.acquisition.base import (
    AcquisitionDriver,
    AcquisitionRetryableError,
    AcquisitionUnavailableError,
)
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.models import AcquisitionSearchResult, DownloadStatus
from plex_playlist_sync.security import is_safe_service_url

logger = logging.getLogger(__name__)


class LidarrAdapter(AcquisitionDriver):
    """Adapts LidarrClient to the unified AcquisitionDriver interface."""

    def __init__(
        self,
        host_url: str,
        api_key: str,
        verify_ssl: bool = True,
        auto_search: bool = True,
        root_folder: Optional[str] = None,
        timeout: float = 10.0,
        prefer_singles: bool = True,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.api_key = api_key.strip()
        self.verify_ssl = verify_ssl
        self.auto_search = auto_search
        self.root_folder = root_folder
        self.timeout = timeout
        self.prefer_singles = prefer_singles
        self.client = LidarrClient(
            base_url=self.host_url,
            api_key=self.api_key,
            verify_ssl=self.verify_ssl,
            auto_search=self.auto_search,
            root_folder=self.root_folder,
            timeout=self.timeout,
            prefer_singles=self.prefer_singles,
        )

    def test_connection(self) -> tuple[bool, str]:
        """Validates connection to Lidarr server."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        res = self.client.test_connection()
        if res.get("online"):
            app_name = res.get("app_name", "Lidarr")
            version = res.get("version", "connected")
            return True, f"{app_name} {version}"
        return False, res.get("error", "Failed to connect to Lidarr")

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """Looks up artist or album in Lidarr."""
        if not is_safe_service_url(self.host_url):
            logger.error("Prohibited Lidarr host URL: %s", self.host_url)
            return []

        # Return synthesized result to allow queueing into Lidarr
        item_title = album or title or f"All Albums by {artist}"
        return [
            AcquisitionSearchResult(
                download_id=f"lidarr::{artist}::{album or title or ''}",
                title=item_title,
                artist=artist,
                album=album,
                item_type="album" if album else "track",
                source="lidarr",
                extra={"artist": artist, "album": album, "title": title},
            )
        ]

    def download(self, result: AcquisitionSearchResult) -> str:
        """Sends artist/album to Lidarr for monitoring and download."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        artist = result.artist
        album = result.album or (result.title if result.item_type == "album" else "")
        title = "" if album else str((result.extra or {}).get("title") or (result.title if result.item_type == "track" else "") or "")
        # A song or album request never adds the whole discography: the client monitors only the release needed.
        res = self.client.add_artist_and_albums(
            artist_name=artist,
            auto_search=self.auto_search,
            wants=[{"album": album, "title": title, "item_type": "album" if album else "track"}],
            album_wait_attempts=1,  # called from request threads: never sleep on Lidarr; albums_pending is retried later
        )
        lidarr_library.invalidate()  # the artist/album lists in Lidarr changed
        status = res.get("status")
        message = str(res.get("message") or "")
        if status == "success":
            return f"lidarr::{artist}::{album or title}"
        if status in ("rate_limited", "albums_pending"):
            raise AcquisitionRetryableError(
                message or "Lidarr is busy", reason=str(status), retry_after=int(res.get("retry_after") or 60)
            )
        if status == "not_in_metadata_profile":
            raise AcquisitionUnavailableError(message or "Not available with your Lidarr metadata profile")
        raise RuntimeError(f"Lidarr addition failed: {message}")

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Polls Lidarr /api/v1/queue to determine progress."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        target_artist = ""
        target_album = ""
        if "lidarr::" in download_id:
            parts = download_id.split("::")
            if len(parts) >= 3:
                target_artist = parts[1].lower()
                target_album = parts[2].lower()

        url = f"{self.host_url}/api/v1/queue"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    records = data.get("records", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for rec in records:
                        rec_artist = (rec.get("artist", {}).get("artistName") or "").lower()
                        rec_album = (rec.get("album", {}).get("title") or "").lower()

                        if target_artist and target_artist in rec_artist:
                            size = int(rec.get("size", 0))
                            sizeleft = int(rec.get("sizeleft", 0))
                            progress = 100.0 if size == 0 else round((float(size - sizeleft) / float(size)) * 100.0, 1)
                            status_val = str(rec.get("status", "")).lower()

                            status_str = DownloadStatus.DOWNLOADING.value
                            if "completed" in status_val or sizeleft == 0:
                                status_str = DownloadStatus.COMPLETED.value
                            elif "queued" in status_val or "delay" in status_val:
                                status_str = DownloadStatus.QUEUED.value

                            return {
                                "status": status_str,
                                "progress": progress,
                                "size_bytes": size,
                                "speed_bps": 0,
                                "eta_seconds": int(rec.get("timeleft_sec") or 0),
                                "source_path": None,
                                "error_message": rec.get("errorMessage"),
                            }

            # If not in queue, it may have already completed / imported
            return {
                "status": DownloadStatus.COMPLETED.value,
                "progress": 100.0,
                "size_bytes": 0,
                "speed_bps": 0,
                "eta_seconds": 0,
                "source_path": None,
                "error_message": None,
            }
        except Exception as e:
            logger.warning("Error inspecting Lidarr queue for %s: %s", download_id, e)
            return {
                "status": DownloadStatus.FAILED.value,
                "progress": 0.0,
                "size_bytes": 0,
                "speed_bps": 0,
                "eta_seconds": 0,
                "source_path": None,
                "error_message": str(e),
            }

    def cancel(self, download_id: str) -> bool:
        """Removes a download from Lidarr queue."""
        if not is_safe_service_url(self.host_url):
            return False

        # Attempt to delete from queue
        headers = {"X-Api-Key": self.api_key}
        url = f"{self.host_url}/api/v1/queue"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    records = resp.json().get("records", [])
                    for rec in records:
                        if rec.get("id"):
                            del_url = f"{self.host_url}/api/v1/queue/{rec['id']}"
                            client.delete(del_url, headers=headers)
            return True
        except Exception as e:
            logger.error("Error cancelling Lidarr queue item %s: %s", download_id, e)
            return False
