"""NZBGet Usenet Downloader Acquisition Driver."""

import logging
import posixpath
from typing import Any, Optional

import httpx

from plex_playlist_sync.clients.acquisition.base import (
    AcquisitionDriver,
    AcquisitionRetryableError,
)
from plex_playlist_sync.models import AcquisitionSearchResult, DownloadStatus
from plex_playlist_sync.security import is_safe_service_url

logger = logging.getLogger(__name__)


class NzbgetDriver(AcquisitionDriver):
    """Driver for NZBGet Usenet download client via JSON-RPC."""

    is_torrent = False

    def __init__(
        self,
        host_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        category: str = "music",
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.username = username
        self.password = password
        self.category = category
        self.timeout = timeout
        self._req_id = 0

    @property
    def _endpoint(self) -> str:
        return f"{self.host_url}/jsonrpc"

    @property
    def _auth(self) -> Optional[tuple[str, str]]:
        if self.username or self.password:
            return (self.username or "", self.password or "")
        return None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _rpc(
        self,
        client: httpx.Client,
        method: str,
        params: list[Any],
    ) -> Any:
        """Executes an NZBGet JSON-RPC call using HTTP Basic Auth."""
        payload = {"method": method, "params": params, "id": self._next_id()}
        resp = client.post(self._endpoint, json=payload)

        if resp.status_code in (401, 403):
            raise PermissionError("NZBGet authentication failed (HTTP 401/403)")

        if resp.status_code != 200:
            raise RuntimeError(f"NZBGet RPC failed (HTTP {resp.status_code}): {resp.text[:120]}")

        data = resp.json()
        error = data.get("error")
        if error:
            err_msg = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"NZBGet RPC error in {method}: {err_msg}")

        return data.get("result")

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity and authentication against NZBGet."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0, auth=self._auth) as client:
                version = self._rpc(client, "version", [])
                return True, f"NZBGet {version or 'connected'}"
        except PermissionError:
            return False, "Authentication failed (invalid credentials)"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except (httpx.RequestError, httpx.HTTPError) as e:
            return False, f"Connection error: {str(e)}"
        except Exception as e:
            logger.warning("NZBGet test_connection failed: %s", e)
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """NZBGet is a downloader; search is handled via Newznab indexers."""
        return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Submits an NZB download URL to NZBGet via append RPC."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        target_url = result.download_url
        if not target_url:
            raise ValueError("Missing download_url in search result for NZBGet")
        if not is_safe_service_url(target_url):
            raise ValueError(f"Unsafe download URL: {target_url}")

        nzb_filename = f"{result.artist} - {result.title}.nzb"
        category = self.category or "music"

        # NZBGet append parameter signature:
        # NZBFilename, NZBContent, Category, Priority, AddToTop, AddPaused, DupeKey, DupeScore, DupeMode, PPParameters
        params = [
            nzb_filename,
            target_url,
            category,
            0,  # Priority (0 = normal)
            False,  # AddToTop
            False,  # AddPaused
            "",  # DupeKey
            0,  # DupeScore
            "SCORE",  # DupeMode
            [],  # PPParameters
        ]

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                res = self._rpc(client, "append", params)
                if not res or int(res) <= 0:
                    raise RuntimeError(f"NZBGet refused download: append returned {res}")
                return str(res)
        except PermissionError as exc:
            raise PermissionError(f"NZBGet authentication failed: {exc}") from exc
        except (httpx.RequestError, httpx.HTTPError) as exc:
            raise AcquisitionRetryableError(f"Network error connecting to NZBGet: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"NZBGet download failed: {exc}") from exc

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Inspects status of a download in NZBGet active queue or history."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                # 1. Check active queue (listgroups)
                groups = self._rpc(client, "listgroups", [0])
                if isinstance(groups, list):
                    for group in groups:
                        if not isinstance(group, dict):
                            continue
                        if str(group.get("NZBID")) == download_id:
                            file_size_mb = float(group.get("FileSizeMB", 0))
                            rem_size_mb = float(group.get("RemainingSizeMB", 0))
                            size_bytes = int(file_size_mb * 1024 * 1024)
                            rem_bytes = int(rem_size_mb * 1024 * 1024)
                            speed_bps = int(group.get("DownloadRate", 0))

                            if size_bytes > 0:
                                progress = round(max(0.0, min(100.0, (1.0 - rem_bytes / size_bytes) * 100.0)), 1)
                            else:
                                progress = 0.0

                            status_raw = str(group.get("Status", "")).upper()
                            status_str = (
                                DownloadStatus.QUEUED.value
                                if "PAUSED" in status_raw
                                else DownloadStatus.DOWNLOADING.value
                            )
                            eta = int(rem_bytes / speed_bps) if speed_bps > 0 else 0

                            return {
                                "status": status_str,
                                "progress": progress,
                                "size_bytes": size_bytes,
                                "speed_bps": speed_bps,
                                "eta_seconds": eta,
                                "source_path": None,
                                "error_message": None,
                            }

                # 2. Check history (completed / failed)
                history = self._rpc(client, "history", [False])
                if isinstance(history, list):
                    for item in history:
                        if not isinstance(item, dict):
                            continue
                        if str(item.get("NZBID")) == download_id:
                            file_size_mb = float(item.get("FileSizeMB", 0))
                            size_bytes = int(file_size_mb * 1024 * 1024)
                            h_status = str(item.get("Status", "")).upper()
                            dest_dir = item.get("DestDir")

                            if "SUCCESS" in h_status:
                                return {
                                    "status": DownloadStatus.COMPLETED.value,
                                    "progress": 100.0,
                                    "size_bytes": size_bytes,
                                    "speed_bps": 0,
                                    "eta_seconds": 0,
                                    "source_path": dest_dir,
                                    "error_message": None,
                                }
                            else:
                                fail_msg = str(item.get("Status", "Usenet download failed"))
                                return {
                                    "status": DownloadStatus.FAILED.value,
                                    "progress": 0.0,
                                    "size_bytes": size_bytes,
                                    "speed_bps": 0,
                                    "eta_seconds": 0,
                                    "source_path": None,
                                    "error_message": fail_msg,
                                }

            return {
                "status": DownloadStatus.QUEUED.value,
                "progress": 0.0,
                "size_bytes": 0,
                "speed_bps": 0,
                "eta_seconds": 0,
                "source_path": None,
                "error_message": None,
            }
        except Exception as e:
            logger.warning("Error fetching NZBGet status for %s: %s", download_id, e)
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
        """Deletes a download from NZBGet queue and history."""
        if not is_safe_service_url(self.host_url):
            return False

        nzb_id = int(download_id) if download_id.isdigit() else 0
        if nzb_id <= 0:
            return False

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                try:
                    self._rpc(client, "editqueue", ["GroupDelete", 0, "", [nzb_id]])
                except Exception:
                    pass
                try:
                    self._rpc(client, "editqueue", ["HistoryDelete", 0, "", [nzb_id]])
                except Exception:
                    pass
                return True
        except Exception as e:
            logger.error("Failed to cancel NZBGet download %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes a completed download from NZBGet history."""
        if not is_safe_service_url(self.host_url):
            return False

        nzb_id = int(download_id) if download_id.isdigit() else 0
        if nzb_id <= 0:
            return False

        cmd = "HistoryFinalDelete" if delete_files else "HistoryDelete"
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                try:
                    self._rpc(client, "editqueue", [cmd, 0, "", [nzb_id]])
                    return True
                except Exception:
                    if delete_files:
                        self._rpc(client, "editqueue", ["HistoryDelete", 0, "", [nzb_id]])
                        return True
                    return False
        except Exception as e:
            logger.error("Failed to cleanup completed NZBGet download %s: %s", download_id, e)
            return False

    def get_download_roots(self) -> list[str]:
        """Completed-download folders reported by NZBGet config DestDir and Category DestDir."""
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []

        roots: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                cfg_items = self._rpc(client, "config", [])
                cfg_dict: dict[str, str] = {}
                if isinstance(cfg_items, list):
                    for item in cfg_items:
                        if isinstance(item, dict) and "Name" in item and "Value" in item:
                            cfg_dict[str(item["Name"])] = str(item["Value"]).strip()

                dest_dir = cfg_dict.get("DestDir", "").strip()
                if dest_dir:
                    roots.append(dest_dir)

                if self.category:
                    cat_lower = self.category.strip().lower()
                    # Inspect Category<X>.Name and Category<X>.DestDir
                    for key, val in cfg_dict.items():
                        if key.startswith("Category") and key.endswith(".Name") and val.lower() == cat_lower:
                            prefix = key[:-5]  # e.g. "Category1"
                            cat_dest = cfg_dict.get(f"{prefix}.DestDir", "").strip()
                            if cat_dest:
                                if posixpath.isabs(cat_dest):
                                    roots.append(cat_dest)
                                elif dest_dir:
                                    roots.append(posixpath.normpath(posixpath.join(dest_dir, cat_dest)))

        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Could not read NZBGet download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []

        if not roots:
            self.last_roots_error = "NZBGet reported no DestDir"
        return list(dict.fromkeys(roots))
