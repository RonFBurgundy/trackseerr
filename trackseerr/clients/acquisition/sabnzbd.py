"""SABnzbd Usenet Downloader Acquisition Driver."""

import logging
import posixpath
from typing import Any, Optional
from urllib.parse import quote

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)


class SabnzbdDriver(AcquisitionDriver):
    """Driver for SABnzbd Usenet download client."""

    def __init__(
        self,
        host_url: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        category: str = "music",
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.username = username
        self.password = password
        self.category = category
        self.timeout = timeout

    def _api_url(self, mode: str, **params: Any) -> str:
        base = f"{self.host_url}/api?output=json&mode={mode}"
        if self.api_key:
            base += f"&apikey={self.api_key}"
        for k, v in params.items():
            if v is not None:
                base += f"&{k}={quote(str(v))}"
        return base

    def test_connection(self) -> tuple[bool, str]:
        """Validates SABnzbd connectivity and API key."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        url = self._api_url("version")
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    version = data.get("version", "connected")
                    return True, f"SABnzbd {version}"
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def get_download_roots(self) -> list[str]:
        """Completed-download folders: ``misc.complete_dir`` plus the configured category's absolute dir.

        SABnzbd normally reports ``complete_dir`` absolute; a relative value is resolved against the parent of
        ``download_dir`` (SAB's own folder base, complete beside incomplete). ``download_dir`` itself (incomplete) is never returned.
        """
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []
        roots: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(self._api_url("get_config", section="misc"))
                if resp.status_code != 200:
                    raise RuntimeError(f"SABnzbd get_config misc failed (HTTP {resp.status_code})")
                misc = (resp.json().get("config") or {}).get("misc") or {}
                complete = str(misc.get("complete_dir") or "").strip()
                download = str(misc.get("download_dir") or "").strip()
                if complete and not posixpath.isabs(complete) and download and posixpath.isabs(download):
                    # Relative to SAB's folder base: assume the usual layout where complete sits beside incomplete.
                    complete = posixpath.join(posixpath.dirname(download.rstrip("/")), posixpath.basename(complete.rstrip("/")))
                if complete and posixpath.isabs(complete):
                    roots.append(complete)
                if self.category and roots:
                    cresp = client.get(self._api_url("get_config", section="categories"))
                    if cresp.status_code == 200:
                        cats = (cresp.json().get("config") or {}).get("categories") or []
                        for cat in cats if isinstance(cats, list) else []:
                            if not isinstance(cat, dict) or str(cat.get("name") or "").lower() != self.category.lower():
                                continue
                            cdir = str(cat.get("dir") or "").strip()
                            if cdir and posixpath.isabs(cdir):
                                roots.append(cdir)
                            elif cdir:
                                roots.append(posixpath.normpath(posixpath.join(roots[0], cdir)))
                    else:
                        logger.info("SABnzbd categories lookup returned HTTP %s; using complete_dir only", cresp.status_code)
        except (httpx.HTTPError, RuntimeError, ValueError, AttributeError) as e:
            logger.warning("Could not read SABnzbd download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []
        if not roots:
            self.last_roots_error = "SABnzbd reported no usable complete_dir"
        return list(dict.fromkeys(roots))

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """SABnzbd is a downloader; search is handled by Newznab/Torznab indexers."""
        return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Enqueues NZB download via SABnzbd addurl."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        target_url = result.download_url
        if not target_url:
            raise ValueError("Missing download_url in search result for SABnzbd")
        if not is_safe_service_url(target_url):
            raise ValueError(f"Unsafe download URL: {target_url}")

        url = self._api_url(
            "addurl",
            name=target_url,
            nzbname=f"{result.artist} - {result.title}",
            cat=self.category,
        )

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"SABnzbd addurl failed (HTTP {resp.status_code}): {resp.text[:120]}")

            data = resp.json()
            if not data.get("status"):
                raise RuntimeError(f"SABnzbd refused download: {data}")

            nzo_ids = data.get("nzo_ids", [])
            if nzo_ids:
                return str(nzo_ids[0])
            return str(data.get("nzo_id", result.download_id))

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Inspects status of a download in SABnzbd queue or history."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                # 1. Check active queue
                queue_url = self._api_url("queue")
                q_resp = client.get(queue_url)
                if q_resp.status_code == 200:
                    q_data = q_resp.json().get("queue", {})
                    slots = q_data.get("slots", [])
                    for slot in slots:
                        if slot.get("nzo_id") == download_id:
                            percentage = float(slot.get("percentage") or 0.0)
                            kbpersec = float(slot.get("kbpersec") or 0.0)
                            mb_total = float(slot.get("mb") or 0.0)
                            size_bytes = int(mb_total * 1024 * 1024)
                            speed_bps = int(kbpersec * 1024)
                            slot_status = str(slot.get("status", "")).lower()

                            status_str = DownloadStatus.DOWNLOADING.value
                            if "paused" in slot_status:
                                status_str = DownloadStatus.QUEUED.value

                            return {
                                "status": status_str,
                                "progress": round(percentage, 1),
                                "size_bytes": size_bytes,
                                "speed_bps": speed_bps,
                                "eta_seconds": int(slot.get("timeleft_sec") or 0),
                                "source_path": None,
                                "error_message": None,
                            }

                # 2. Check history (completed / failed)
                hist_url = self._api_url("history", nzo_ids=download_id)
                h_resp = client.get(hist_url)
                if h_resp.status_code == 200:
                    h_data = h_resp.json().get("history", {})
                    h_slots = h_data.get("slots", [])
                    for slot in h_slots:
                        if slot.get("nzo_id") == download_id:
                            h_status = str(slot.get("status", "")).upper()
                            size_bytes = int(slot.get("bytes", 0))
                            storage_path = slot.get("storage")
                            if h_status == "COMPLETED":
                                return {
                                    "status": DownloadStatus.COMPLETED.value,
                                    "progress": 100.0,
                                    "size_bytes": size_bytes,
                                    "speed_bps": 0,
                                    "eta_seconds": 0,
                                    "source_path": storage_path,
                                    "error_message": None,
                                }
                            elif h_status == "FAILED":
                                return {
                                    "status": DownloadStatus.FAILED.value,
                                    "progress": 0.0,
                                    "size_bytes": size_bytes,
                                    "speed_bps": 0,
                                    "eta_seconds": 0,
                                    "source_path": None,
                                    "error_message": slot.get("fail_message", "Usenet download failed"),
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
            logger.warning("Error fetching SABnzbd status for %s: %s", download_id, e)
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
        """Deletes a download from SABnzbd queue and history."""
        if not is_safe_service_url(self.host_url):
            return False

        try:
            with httpx.Client(timeout=self.timeout) as client:
                # Attempt delete from queue with del_files=1
                q_del_url = self._api_url("queue", name="delete", value=download_id, del_files=1)
                client.get(q_del_url)
                # Also attempt history delete
                h_del_url = self._api_url("history", name="delete", value=download_id, del_files=1)
                client.get(h_del_url)
                return True
        except Exception as e:
            logger.error("Failed to cancel SABnzbd download %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes a completed download from SABnzbd history."""
        if not is_safe_service_url(self.host_url):
            return False

        del_files_val = 1 if delete_files else 0
        url = self._api_url("history", name="delete", val=download_id, del_files=del_files_val)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return bool(data.get("status", True))
                return False
        except Exception as e:
            logger.error("Failed to cleanup completed SABnzbd download %s: %s", download_id, e)
            return False
