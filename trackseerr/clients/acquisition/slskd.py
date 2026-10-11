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
        download_dir: Optional[str] = None,
        timeout: float = 10.0,
        search_timeout: float = 12.0,
    ) -> None:
        self.search_timeout = search_timeout
        self.host_url = host_url.rstrip("/")
        self.api_key = (api_key or "").strip()
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

    def _test_failed(self, reason: str, status: Optional[int] = None) -> tuple[bool, str]:
        """Logs one warning for a failed connection test (never the API key) and returns the failure tuple."""
        logger.warning(
            "slskd connection test failed: host=%s status=%s reason=%s",
            self.host_url,
            status if status is not None else "n/a",
            reason,
        )
        return False, reason

    def test_connection(self) -> tuple[bool, str]:
        """Validates slskd connectivity and API credentials."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.host_url}/api/v0/application", headers=self._headers())
                if resp.status_code in (401, 403):
                    return self._test_failed(
                        "Authentication failed: invalid API key or credentials", resp.status_code
                    )
                if resp.status_code != 200:
                    return self._test_failed(f"HTTP {resp.status_code}: {resp.text[:120]}", resp.status_code)
                data = resp.json() if resp.text else {}
        except httpx.TimeoutException:
            return self._test_failed("Connection timed out (5s)")
        except httpx.TransportError as e:
            return self._test_failed(f"Could not reach slskd at {self.host_url}: {type(e).__name__}: {e}")
        except (httpx.HTTPError, ValueError) as e:
            return self._test_failed(f"Connection error: {type(e).__name__}: {e}")

        info = data.get("version") if isinstance(data, dict) else None
        if isinstance(info, dict):
            version = str(info.get("current") or "connected")
        elif isinstance(info, str) and info:
            version = info
        else:
            version = "connected"
        server = data.get("server") if isinstance(data, dict) else None
        if isinstance(server, dict) and server.get("isLoggedIn") is False:
            return True, f"slskd {version} (connected; not logged in to the Soulseek network yet)"
        return True, f"slskd {version}"

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
        search_id = None
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                init_resp = client.post(
                    init_url,
                    headers=self._headers(),
                    json={
                        "searchText": query,
                        "searchTimeout": int(self.search_timeout * 1000),
                        "filterResponses": True,
                    },
                )
                if init_resp.status_code not in (200, 201, 202):
                    logger.warning("slskd search initiation failed (%d): %s", init_resp.status_code, init_resp.text[:100])
                    return []

                search_id = init_resp.json().get("id")
                if not search_id:
                    return []
                logger.info("slskd search started id=%s query=%r timeout=%.0fs", search_id, query, self.search_timeout)

                try:
                    state = self._wait_for_search(client, search_id)
                    resp = client.get(f"{init_url}/{search_id}/responses", headers=self._headers())
                    if resp.status_code != 200:
                        return []
                    user_responses = resp.json()
                    if not isinstance(user_responses, list):
                        return []
                    results = self._parse_responses(user_responses, artist, title, album)
                    files = sum(len(u.get("files", [])) for u in user_responses if isinstance(u, dict))
                    logger.info(
                        "slskd search id=%s query=%r finished state=%s responses=%d files=%d audio_kept=%d in %.1fs",
                        search_id, query, state, len(user_responses), files, len(results), time.monotonic() - started,
                    )
                    return results
                finally:
                    self._delete_search(client, init_url, search_id)
        except (httpx.HTTPError, ValueError) as e:
            logger.error("slskd search failed for query %r: %s: %s", query, type(e).__name__, e)
            return []

    def _wait_for_search(self, client: httpx.Client, search_id: str) -> str:
        """Poll the search until slskd reports it complete or the deadline passes; returns the last state."""
        status_url = f"{self.host_url}/api/v0/searches/{search_id}"
        deadline = time.monotonic() + self.search_timeout + 5.0
        state = "Unknown"
        while time.monotonic() < deadline:
            resp = client.get(status_url, headers=self._headers())
            if resp.status_code != 200:
                logger.warning("slskd search status poll failed (%d) for id=%s", resp.status_code, search_id)
                return state
            data = resp.json()
            data = data if isinstance(data, dict) else {}
            state = str(data.get("state") or state)
            if data.get("isComplete") or "Completed" in state:
                return state
            time.sleep(1.0)
        return state

    def _delete_search(self, client: httpx.Client, init_url: str, search_id: str) -> None:
        """Remove the finished search from slskd; cleanup never raises."""
        try:
            resp = client.delete(f"{init_url}/{search_id}", headers=self._headers())
            if resp.status_code not in (200, 204):
                logger.debug("slskd search delete returned %d for id=%s", resp.status_code, search_id)
        except httpx.HTTPError as e:
            logger.debug("slskd search delete failed for id=%s: %s: %s", search_id, type(e).__name__, e)

    def _parse_responses(
        self,
        payload: list[Any],
        artist: str,
        title: Optional[str],
        album: Optional[str],
    ) -> list[AcquisitionSearchResult]:
        results: list[AcquisitionSearchResult] = []
        for user_resp in payload:
            uname = user_resp.get("username", "")
            for f in user_resp.get("files", []):
                fname = f.get("filename", "")
                ext = f.get("extension", "").lower().lstrip(".")
                if ext not in ("mp3", "flac", "m4a", "aac", "ogg", "opus"):
                    continue

                size = int(f.get("size", 0))
                bitrate = f.get("bitRate")
                quality_desc = f"{ext.upper()}"
                if bitrate:
                    quality_desc += f" {bitrate}kbps"

                base = fname.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                name = base.rsplit(".", 1)[0] if "." in base else base
                unique_dl_id = f"{uname}::{quote(fname)}"
                results.append(
                    AcquisitionSearchResult(
                        download_id=unique_dl_id,
                        title=name,
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

    @staticmethod
    def _find_transfer(
        data: Any, target_user: Optional[str], target_file: Optional[str], download_id: str
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Locate one transfer in slskd's downloads list; returns (transfer, directory)."""
        if not isinstance(data, list):
            return None, None
        for item in data:
            if target_user and item.get("username", "") != target_user:
                continue
            for d in item.get("directories", []):
                for f in d.get("files", []):
                    if (target_file and f.get("filename") == target_file) or f.get("id") == download_id:
                        return f, d.get("directory")
            # Also check flat transfer files if present
            for f in item.get("files", []):
                if (target_file and f.get("filename") == target_file) or f.get("id") == download_id:
                    return f, None
        return None, None

    @staticmethod
    def _map_state(state: str) -> tuple[str, Optional[str]]:
        """Map a slskd transfer state to (DownloadStatus value, error message)."""
        low = str(state or "").strip().lower()
        if low.startswith("completed"):
            if "succeeded" in low:
                return DownloadStatus.COMPLETED.value, None
            suffix = state.split(",", 1)[1].strip() if "," in state else "Failed"
            return DownloadStatus.FAILED.value, f"Soulseek transfer {suffix}"
        if low in ("succeeded", "finished"):
            return DownloadStatus.COMPLETED.value, None
        if any(s in low for s in ("errored", "failed", "aborted", "cancelled", "rejected")):
            return DownloadStatus.FAILED.value, f"Soulseek transfer {low}"
        if any(s in low for s in ("queued", "requested", "initializing", "waiting")):
            return DownloadStatus.QUEUED.value, None
        return DownloadStatus.DOWNLOADING.value, None

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

            transfer, transfer_dir = self._find_transfer(data, target_user, target_file, download_id)

            if not transfer:
                # Not in slskd's list: the worker decides how long to tolerate this.
                return {
                    "status": DownloadStatus.QUEUED.value,
                    "progress": 0.0,
                    "size_bytes": 0,
                    "speed_bps": 0,
                    "eta_seconds": 0,
                    "source_path": None,
                    "error_message": None,
                    "missing": True,
                }

            size = int(transfer.get("size", 0))
            transferred = int(transfer.get("bytesTransferred", 0))
            speed = int(transfer.get("averageSpeed", 0) or transfer.get("speed", 0) or 0)
            progress = (float(transferred) / float(size) * 100.0) if size > 0 else 0.0

            status_str, state_error = self._map_state(str(transfer.get("state", "")))
            if status_str == DownloadStatus.COMPLETED.value:
                progress = 100.0

            source_file = (
                self._local_source_path(transfer, transfer_dir)
                if status_str == DownloadStatus.COMPLETED.value
                else None
            )
            error_message = transfer.get("error")
            if status_str == DownloadStatus.FAILED.value and not error_message:
                error_message = state_error
            return {
                "status": status_str,
                "progress": round(progress, 1),
                "size_bytes": size,
                "speed_bps": speed,
                "eta_seconds": int((size - transferred) / speed) if speed > 0 and size > transferred else 0,
                "source_path": source_file,
                "error_message": error_message,
                "remote_state": transfer.get("state"),
                "transfer_id": transfer.get("id"),
            }
        except (httpx.HTTPError, ValueError) as e:
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
        """Cancels a transfer in slskd (DELETE by transfer id, removing it from the list)."""
        if not is_safe_service_url(self.host_url):
            return False

        target_user = ""
        target_file = ""
        if "::" in download_id:
            parts = download_id.split("::", 1)
            target_user = parts[0]
            target_file = unquote(parts[1])

        base = f"{self.host_url}/api/v0/transfers/downloads/{quote(target_user)}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(base, headers=self._headers())
                if resp.status_code != 200:
                    return False
                transfer, _ = self._find_transfer([resp.json()], target_user, target_file, download_id)
                transfer_id = transfer.get("id") if transfer else None
                if not transfer_id:
                    return False
                resp = client.delete(f"{base}/{quote(str(transfer_id))}", headers=self._headers(), params={"remove": "true"})
                return resp.status_code in (200, 204)
        except (httpx.HTTPError, ValueError) as e:
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
