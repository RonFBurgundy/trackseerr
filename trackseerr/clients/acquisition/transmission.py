"""Transmission BitTorrent Client Acquisition Driver."""

import logging
import posixpath
import re
from typing import Any, Optional

import httpx

from trackseerr.clients.acquisition.base import (
    AcquisitionDriver,
    AcquisitionRetryableError,
)
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)

_BTIH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


class TransmissionDriver(AcquisitionDriver):
    """Driver for Transmission BitTorrent client via JSON-RPC."""

    is_torrent = True

    def __init__(
        self,
        host_url: str,
        rpc_path: str = "/transmission/rpc",
        username: Optional[str] = None,
        password: Optional[str] = None,
        category: str = "trackseerr",
        download_dir: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        norm_path = rpc_path.strip()
        if not norm_path.startswith("/"):
            norm_path = "/" + norm_path
        self.rpc_path = norm_path
        self.username = username
        self.password = password
        self.category = category
        self.download_dir = download_dir
        self.timeout = timeout
        self._session_id: Optional[str] = None

    @property
    def _endpoint(self) -> str:
        return f"{self.host_url}{self.rpc_path}"

    @property
    def _auth(self) -> Optional[tuple[str, str]]:
        if self.username or self.password:
            return (self.username or "", self.password or "")
        return None

    def _rpc(
        self,
        client: httpx.Client,
        method: str,
        arguments: Optional[dict[str, Any]] = None,
        tag: Optional[int] = None,
    ) -> dict[str, Any]:
        """Executes a Transmission JSON-RPC call with X-Transmission-Session-Id 409 handshake handling."""
        payload: dict[str, Any] = {"method": method, "arguments": arguments or {}}
        if tag is not None:
            payload["tag"] = tag

        headers: dict[str, str] = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        resp = client.post(self._endpoint, json=payload, headers=headers)

        if resp.status_code == 409:
            # 409 Conflict handshake: update token and retry once
            session_id = resp.headers.get("X-Transmission-Session-Id")
            if session_id:
                self._session_id = session_id
                headers["X-Transmission-Session-Id"] = session_id
                resp = client.post(self._endpoint, json=payload, headers=headers)
            else:
                raise RuntimeError("Transmission returned 409 Conflict without X-Transmission-Session-Id header")

        if resp.status_code in (401, 403):
            raise PermissionError(f"Transmission authentication failed (HTTP {resp.status_code})")

        if resp.status_code != 200:
            raise RuntimeError(f"Transmission RPC failed (HTTP {resp.status_code}): {resp.text[:120]}")

        data = resp.json()
        result_msg = data.get("result", "")
        if result_msg != "success":
            raise RuntimeError(f"Transmission RPC error: {result_msg}")

        args = data.get("arguments", {})
        return args if isinstance(args, dict) else {}

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity and authentication against Transmission."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0, auth=self._auth) as client:
                args = self._rpc(client, "session-get", {"fields": ["version"]})
                version = args.get("version", "connected")
                return True, f"Transmission {version}"
        except PermissionError:
            return False, "Authentication failed (invalid credentials)"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except (httpx.RequestError, httpx.HTTPError) as e:
            return False, f"Connection error: {str(e)}"
        except Exception as e:
            logger.warning("Transmission test_connection failed: %s", e)
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """Transmission is a downloader; search is handled via Torznab indexers."""
        return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Submits magnet link or torrent URL to Transmission."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        target_url = result.magnet_url or result.download_url
        if not target_url:
            raise ValueError("No magnet or download URL provided for torrent")

        torrent_hash = result.download_id
        btih_match = _BTIH_RE.search(target_url)
        if btih_match:
            torrent_hash = btih_match.group(1).lower()

        arguments: dict[str, Any] = {"filename": target_url}

        # download-dir from setting or category path
        target_dir = self.download_dir
        if not target_dir and self.category and (self.category.startswith("/") or "\\" in self.category):
            target_dir = self.category

        if target_dir:
            arguments["download-dir"] = target_dir

        # labels (supported in Transmission >= 3.0)
        if self.category:
            arguments["labels"] = [self.category]

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                res = self._rpc(client, "torrent-add", arguments)
                added = res.get("torrent-added") or res.get("torrent-duplicate") or {}
                if isinstance(added, dict) and added.get("hashString"):
                    torrent_hash = str(added["hashString"]).lower()
        except PermissionError as exc:
            raise PermissionError(f"Transmission authentication failed: {exc}") from exc
        except (httpx.RequestError, httpx.HTTPError) as exc:
            raise AcquisitionRetryableError(f"Network error connecting to Transmission: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"Transmission torrent-add failed: {exc}") from exc

        return torrent_hash

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Polls Transmission for torrent status, progress, and speed metrics."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        fields = [
            "id",
            "hashString",
            "name",
            "totalSize",
            "percentDone",
            "rateDownload",
            "eta",
            "status",
            "error",
            "errorString",
            "downloadDir",
            "uploadRatio",
            "secondsSeeding",
        ]

        ids: list[Any] = [int(download_id)] if download_id.isdigit() else [download_id.lower()]

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                res = self._rpc(client, "torrent-get", {"ids": ids, "fields": fields})
                torrents = res.get("torrents", [])
                if not torrents or not isinstance(torrents, list):
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

                tor = torrents[0]
                raw_status = int(tor.get("status", 0))
                error = int(tor.get("error", 0))
                error_string = str(tor.get("errorString") or "").strip()
                percent_done = float(tor.get("percentDone", 0.0))
                progress = min(100.0, round(percent_done * 100.0, 1))
                size = int(tor.get("totalSize") or 0)
                speed = int(tor.get("rateDownload") or 0)
                eta = int(tor.get("eta") or 0)
                if eta < 0 or eta > 8640000:
                    eta = 0

                download_dir = str(tor.get("downloadDir") or "").rstrip("/")
                name = str(tor.get("name") or "")
                content_path = posixpath.join(download_dir, name) if download_dir and name else (download_dir or None)
                ratio = float(tor.get("uploadRatio") or 0.0)
                seeding_time = int(tor.get("secondsSeeding") or 0)

                # Transmission status codes:
                # 0: Stopped (paused)
                # 1: Check wait, 2: Checking
                # 3: Download wait, 4: Downloading
                # 5: Seed wait, 6: Seeding
                if error != 0:
                    status_str = DownloadStatus.FAILED.value
                elif raw_status == 0:
                    if progress >= 100.0:
                        status_str = DownloadStatus.COMPLETED.value
                    else:
                        status_str = DownloadStatus.QUEUED.value
                elif raw_status in (1, 2, 3):
                    status_str = DownloadStatus.QUEUED.value
                elif raw_status == 4:
                    status_str = DownloadStatus.DOWNLOADING.value
                elif raw_status in (5, 6):
                    status_str = DownloadStatus.COMPLETED.value
                    progress = 100.0
                else:
                    status_str = DownloadStatus.QUEUED.value

                return {
                    "status": status_str,
                    "progress": progress,
                    "size_bytes": size,
                    "speed_bps": speed,
                    "eta_seconds": eta,
                    "source_path": content_path,
                    "content_path": content_path or "",
                    "save_path": download_dir,
                    "ratio": ratio,
                    "seeding_time_seconds": seeding_time,
                    "error_message": error_string if status_str == DownloadStatus.FAILED.value and error_string else None,
                }
        except Exception as e:
            logger.warning("Error fetching Transmission status for %s: %s", download_id, e)
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
        """Deletes torrent and downloaded files from Transmission."""
        if not is_safe_service_url(self.host_url):
            return False

        ids: list[Any] = [int(download_id)] if download_id.isdigit() else [download_id.lower()]
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                self._rpc(client, "torrent-remove", {"ids": ids, "delete-local-data": True})
                return True
        except Exception as e:
            logger.error("Failed to cancel Transmission torrent %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes completed torrent from Transmission with optional file deletion."""
        if not is_safe_service_url(self.host_url):
            return False

        ids: list[Any] = [int(download_id)] if download_id.isdigit() else [download_id.lower()]
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                self._rpc(client, "torrent-remove", {"ids": ids, "delete-local-data": delete_files})
                return True
        except Exception as e:
            logger.error("Failed to cleanup completed Transmission torrent %s: %s", download_id, e)
            return False

    def get_download_roots(self) -> list[str]:
        """Completed-download folders reported by Transmission session-get download-dir."""
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []

        roots: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                args = self._rpc(client, "session-get", {"fields": ["download-dir"]})
                dd = str(args.get("download-dir") or "").strip()
                if dd:
                    roots.append(dd)
                if self.download_dir and self.download_dir.strip():
                    roots.append(self.download_dir.strip())
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Could not read Transmission download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []

        if not roots:
            self.last_roots_error = "Transmission reported no download-dir"
        return list(dict.fromkeys(roots))

    def list_category(self) -> Optional[list[dict[str, Any]]]:
        """Torrents in TrackSeerr's category/label in Transmission.

        Returns normalized dicts: hash (lower case), name, size, ratio, seeding_time (seconds), content_path, state.
        An empty category is refused (returns None).
        """
        if not self.category or not self.category.strip():
            return None
        if not is_safe_service_url(self.host_url):
            raise RuntimeError("Prohibited host URL")

        fields = [
            "hashString",
            "name",
            "totalSize",
            "uploadRatio",
            "secondsSeeding",
            "downloadDir",
            "status",
            "labels",
        ]
        with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
            res = self._rpc(client, "torrent-get", {"fields": fields})
            items = res.get("torrents", [])

        if not isinstance(items, list):
            raise RuntimeError("Transmission torrent-get returned an unexpected payload")

        cat_target = self.category.strip().lower()
        out: list[dict[str, Any]] = []
        for tor in items:
            if not isinstance(tor, dict) or not tor.get("hashString"):
                continue
            labels = [str(lbl).strip().lower() for lbl in tor.get("labels", []) if lbl]
            if cat_target not in labels:
                continue

            download_dir = str(tor.get("downloadDir") or "").rstrip("/")
            name = str(tor.get("name") or "")
            content_path = posixpath.join(download_dir, name) if download_dir and name else download_dir

            out.append(
                {
                    "hash": str(tor["hashString"]).lower(),
                    "name": name,
                    "size": int(tor.get("totalSize") or 0),
                    "ratio": float(tor.get("uploadRatio") or 0.0),
                    "seeding_time": int(tor.get("secondsSeeding") or 0),
                    "content_path": content_path,
                    "state": str(tor.get("status") or ""),
                }
            )
        return out

    def set_share_limits(
        self, lookup: str, ratio: Optional[float], seed_time_minutes: Optional[int]
    ) -> bool:
        """Sets per-torrent share limits via torrent-set seedRatioLimit/seedRatioMode and seedIdleLimit/seedIdleMode.

        Note: Transmission supports idle-time limits (seedIdleLimit / seedIdleMode), not total-seed-time limits.
        Seed-time targets are still enforced by TrackSeerr's own seed cleanup worker.
        """
        if not is_safe_service_url(self.host_url):
            return False

        ids: list[Any] = [int(lookup)] if lookup.isdigit() else [lookup.lower()]
        arguments: dict[str, Any] = {"ids": ids}

        # Ratio limit:
        # seedRatioMode: 0 = global, 1 = single torrent limit, 2 = unlimited
        if ratio is None:
            arguments["seedRatioMode"] = 0
        elif float(ratio) <= 0:
            arguments["seedRatioMode"] = 2
        else:
            arguments["seedRatioMode"] = 1
            arguments["seedRatioLimit"] = float(ratio)

        # Idle limit (Transmission idle seeding):
        # seedIdleMode: 0 = global, 1 = single torrent limit, 2 = unlimited
        if seed_time_minutes is None:
            arguments["seedIdleMode"] = 0
        elif int(seed_time_minutes) <= 0:
            arguments["seedIdleMode"] = 2
        else:
            arguments["seedIdleMode"] = 1
            arguments["seedIdleLimit"] = int(seed_time_minutes)

        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                self._rpc(client, "torrent-set", arguments)
                return True
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Transmission set_share_limits failed for %s: %s", lookup, e)
            return False
