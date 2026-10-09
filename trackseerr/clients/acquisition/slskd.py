"""Soulseek / slskd REST API Acquisition Driver."""

import logging
import posixpath
import time
from typing import Any, Optional
from urllib.parse import quote, unquote

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)


class SlskdDriver(AcquisitionDriver):
    """Driver for slskd (Soulseek REST API daemon)."""

    def __init__(
        self,
        host_url: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        download_dir: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.username = username
        self.password = password
        # Explicit override only; otherwise the completed-downloads folder is read from slskd's own options.
        self.download_dir = (download_dir or "").strip() or None
        self.timeout = timeout
        self._downloads_root: Optional[str] = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        return headers

    def _read_downloads_root(self) -> Optional[str]:
        """slskd's completed-downloads folder (``directories.downloads``), cached on the driver.

        ``directories.incomplete`` is never used. Returns None (and sets ``last_roots_error``) when unreadable.
        """
        if self._downloads_root:
            return self._downloads_root
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.host_url}/api/v0/options", headers=self._headers())
                if resp.status_code != 200:
                    raise RuntimeError(f"slskd /options failed (HTTP {resp.status_code})")
                data = resp.json()
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Could not read slskd download folder from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return None
        dirs = data.get("directories") if isinstance(data, dict) else None
        downloads = str(dirs.get("downloads") or "").strip() if isinstance(dirs, dict) else ""
        if not downloads:
            self.last_roots_error = "slskd reported no directories.downloads"
            return None
        self._downloads_root = downloads
        return downloads

    def get_download_roots(self) -> list[str]:
        """slskd's completed-downloads directory (``directories.downloads``); the incomplete folder is never returned."""
        root = self._read_downloads_root()
        roots = [root] if root else []
        if self.download_dir and self.download_dir not in roots:
            roots.append(self.download_dir)
        return roots

    @staticmethod
    def _last_component(remote_path: str) -> str:
        """Last path component of a remote (possibly Windows ``\\``-separated) path."""
        return remote_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    def _local_source_path(self, transfer: dict[str, Any], remote_dir: Optional[str]) -> Optional[str]:
        """Where slskd stored a completed transfer: ``<downloads>/<last remote directory component>/<file name>``."""
        reported = str(transfer.get("localFilename") or "").strip()
        if reported and posixpath.isabs(reported.replace("\\", "/")):
            return reported
        base = self.download_dir or self._read_downloads_root()
        filename = str(transfer.get("filename") or "")
        if not base or not filename:
            return None
        folder_src = remote_dir or filename.replace("\\", "/").rsplit("/", 1)[0]
        folder = self._last_component(folder_src)
        name = self._last_component(filename)
        if not name or name in (".", ".."):
            return None
        parts = [base.rstrip("/") or "/"]
        if folder and folder not in (".", ".."):
            parts.append(folder)
        parts.append(name)
        return posixpath.join(*parts)

    def test_connection(self) -> tuple[bool, str]:
        """Validates slskd connectivity and API credentials."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        url = f"{self.host_url}/api/v0/version"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, headers=self._headers())
                if resp.status_code == 200:
                    data = resp.json() if resp.text else {}
                    version = data.get("version", "connected") if isinstance(data, dict) else "connected"
                    return True, f"slskd {version}"
                if resp.status_code in (401, 403):
                    return False, "Authentication failed: invalid X-API-KEY"
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
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
        """Executes a search query against slskd and formats results."""
        if not is_safe_service_url(self.host_url):
            logger.error("Prohibited slskd host URL: %s", self.host_url)
            return []

        query_parts = [artist.strip()]
        if title:
            query_parts.append(title.strip())
        elif album:
            query_parts.append(album.strip())
        query = " ".join(query_parts)

        init_url = f"{self.host_url}/api/v0/searches"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                init_resp = client.post(
                    init_url,
                    headers=self._headers(),
                    json={"searchText": query},
                )
                if init_resp.status_code not in (200, 201, 202):
                    logger.warning("slskd search initiation failed (%d): %s", init_resp.status_code, init_resp.text[:100])
                    return []

                search_obj = init_resp.json()
                search_id = search_obj.get("id")
                if not search_id:
                    return []

                # Poll brief window for fast responses
                responses_url = f"{self.host_url}/api/v0/searches/{search_id}/responses"
                results: list[AcquisitionSearchResult] = []
                
                # Fetch available responses
                resp = client.get(responses_url, headers=self._headers())
                if resp.status_code != 200:
                    return []
                
                user_responses = resp.json()
                if not isinstance(user_responses, list):
                    return []

                for user_resp in user_responses:
                    uname = user_resp.get("username", "")
                    files = user_resp.get("files", [])
                    for f in files:
                        fname = f.get("filename", "")
                        ext = f.get("extension", "").lower().lstrip(".")
                        if ext not in ("mp3", "flac", "m4a", "aac", "ogg", "opus"):
                            continue

                        size = int(f.get("size", 0))
                        bitrate = f.get("bitRate")
                        quality_desc = f"{ext.upper()}"
                        if bitrate:
                            quality_desc += f" {bitrate}kbps"

                        unique_dl_id = f"{uname}::{quote(fname)}"
                        results.append(
                            AcquisitionSearchResult(
                                download_id=unique_dl_id,
                                title=title or fname.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                                artist=artist,
                                album=album,
                                item_type="track",
                                size_bytes=size,
                                bit_rate=int(bitrate) if bitrate else None,
                                format=ext,
                                quality_str=quality_desc,
                                download_url=unique_dl_id,
                                source="slskd",
                                extra={"username": uname, "filename": fname, "size": size},
                            )
                        )
                return results
        except Exception as e:
            logger.error("Error during slskd search: %s", e)
            return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Enqueues track or folder download via slskd."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        extra = result.extra or {}
        username = extra.get("username")
        filename = extra.get("filename")
        size = extra.get("size", result.size_bytes)

        if not username or not filename:
            # Attempt to parse from download_id
            if "::" in result.download_id:
                parts = result.download_id.split("::", 1)
                username = parts[0]
                filename = unquote(parts[1])
            else:
                raise ValueError("Missing username or filename for slskd transfer")

        url = f"{self.host_url}/api/v0/transfers/downloads/{quote(username)}"
        payload = [{"filename": filename, "size": size}]

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
            if resp.status_code not in (200, 201, 202):
                raise RuntimeError(f"slskd download queue failed ({resp.status_code}): {resp.text[:120]}")

        return f"{username}::{quote(filename)}"

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Inspects transfer status across all slskd downloads."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        target_user = None
        target_file = None
        if "::" in download_id:
            parts = download_id.split("::", 1)
            target_user = parts[0]
            target_file = unquote(parts[1])

        url = f"{self.host_url}/api/v0/transfers/downloads"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    return {
                        "status": DownloadStatus.FAILED.value,
                        "error_message": f"slskd returned HTTP {resp.status_code}",
                    }
                data = resp.json()

            # slskd returns a list of user download groups
            # Each entry: {"username": ..., "directories": [{"files": [...]}]}
            # or a flat list of transfers depending on version
            transfer = None
            transfer_dir: Optional[str] = None
            if isinstance(data, list):
                for item in data:
                    item_user = item.get("username", "")
                    if target_user and item_user != target_user:
                        continue
                    # Check nested files
                    directories = item.get("directories", [])
                    for d in directories:
                        for f in d.get("files", []):
                            if target_file and f.get("filename") == target_file:
                                transfer = f
                                transfer_dir = d.get("directory")
                                break
                            elif f.get("id") == download_id:
                                transfer = f
                                transfer_dir = d.get("directory")
                                break
                        if transfer:
                            break
                    if transfer:
                        break
                    # Also check flat transfer files if present
                    for f in item.get("files", []):
                        if target_file and f.get("filename") == target_file:
                            transfer = f
                            break
                        elif f.get("id") == download_id:
                            transfer = f
                            break
                    if transfer:
                        break

            if not transfer:
                # If download not in active list, check if completed previously or queued
                return {
                    "status": DownloadStatus.QUEUED.value,
                    "progress": 0.0,
                    "size_bytes": 0,
                    "speed_bps": 0,
                    "eta_seconds": 0,
                    "source_path": None,
                    "error_message": None,
                }

            state = str(transfer.get("state", "")).lower()
            size = int(transfer.get("size", 0))
            transferred = int(transfer.get("bytesTransferred", 0))
            speed = int(transfer.get("averageSpeed", 0) or transfer.get("speed", 0) or 0)
            progress = (float(transferred) / float(size) * 100.0) if size > 0 else 0.0

            status_str = DownloadStatus.DOWNLOADING.value
            if any(s in state for s in ("completed", "succeeded", "finished")):
                status_str = DownloadStatus.COMPLETED.value
                progress = 100.0
            elif any(s in state for s in ("queued", "requested", "waiting")):
                status_str = DownloadStatus.QUEUED.value
            elif any(s in state for s in ("errored", "failed", "aborted", "cancelled", "rejected")):
                status_str = DownloadStatus.FAILED.value

            source_file = (
                self._local_source_path(transfer, transfer_dir)
                if status_str == DownloadStatus.COMPLETED.value
                else None
            )
            return {
                "status": status_str,
                "progress": round(progress, 1),
                "size_bytes": size,
                "speed_bps": speed,
                "eta_seconds": int((size - transferred) / speed) if speed > 0 and size > transferred else 0,
                "source_path": source_file,
                "error_message": transfer.get("error"),
            }
        except Exception as e:
            logger.warning("Error fetching slskd status for %s: %s", download_id, e)
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
        """Cancels a transfer in slskd."""
        if not is_safe_service_url(self.host_url):
            return False

        target_user = ""
        target_file = ""
        if "::" in download_id:
            parts = download_id.split("::", 1)
            target_user = parts[0]
            target_file = unquote(parts[1])

        url = f"{self.host_url}/api/v0/transfers/downloads/{quote(target_user)}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request("DELETE", url, headers=self._headers(), json=[{"filename": target_file}])
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.error("Failed to cancel slskd transfer %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes completed transfer from slskd or returns True."""
        if not is_safe_service_url(self.host_url):
            return False

        if "::" not in download_id:
            return True

        parts = download_id.split("::", 1)
        target_user = parts[0]
        target_file = unquote(parts[1])

        url = f"{self.host_url}/api/v0/transfers/downloads/{quote(target_user)}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request("DELETE", url, headers=self._headers(), json=[{"filename": target_file}])
                return resp.status_code in (200, 204, 404)
        except Exception as e:
            logger.warning("Error cleaning up slskd completed transfer %s: %s", download_id, e)
            return True
