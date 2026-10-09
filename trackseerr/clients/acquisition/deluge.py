"""Deluge Web UI Acquisition Driver."""

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


class DelugeDriver(AcquisitionDriver):
    """Driver for Deluge BitTorrent client via Web UI JSON-RPC."""

    is_torrent = True

    def __init__(
        self,
        host_url: str,
        password: Optional[str] = None,
        label: Optional[str] = "trackseerr",
        download_location: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.password = password
        self.label = label
        self.download_location = download_location
        self.timeout = timeout
        self._cookie: Optional[str] = None
        self._req_id = 0

    @property
    def category(self) -> Optional[str]:
        """Category alias mapped to label for AcquisitionDriver consistency."""
        return self.label

    @property
    def _endpoint(self) -> str:
        return f"{self.host_url}/json"

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _login(self, client: httpx.Client) -> bool:
        """Authenticates with Deluge Web UI and stores the session cookie."""
        login_payload = {
            "method": "auth.login",
            "params": [self.password or ""],
            "id": self._next_id(),
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = client.post(self._endpoint, json=login_payload, headers=headers)
            if resp.status_code == 200:
                cookie_header = resp.headers.get("set-cookie")
                if cookie_header:
                    self._cookie = cookie_header
                data = resp.json()
                if data.get("result") is True and not data.get("error"):
                    return True
            return False
        except (httpx.RequestError, httpx.HTTPError):
            raise
        except Exception as e:
            logger.warning("Deluge login exception: %s", e)
            return False

    def _ensure_connected(self, client: httpx.Client) -> None:
        """Ensures the Deluge web service is connected to a deluged daemon."""
        headers = {"Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie

        # 1. Check web.connected
        resp = client.post(
            self._endpoint,
            json={"method": "web.connected", "params": [], "id": self._next_id()},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("result") is True:
                return

        # 2. Get hosts and connect to the first host if not connected
        h_resp = client.post(
            self._endpoint,
            json={"method": "web.get_hosts", "params": [], "id": self._next_id()},
            headers=headers,
        )
        if h_resp.status_code == 200:
            h_data = h_resp.json()
            hosts = h_data.get("result", [])
            if hosts and isinstance(hosts, list):
                first_host_id = hosts[0][0]
                client.post(
                    self._endpoint,
                    json={"method": "web.connect", "params": [first_host_id], "id": self._next_id()},
                    headers=headers,
                )

    def _rpc(
        self,
        client: httpx.Client,
        method: str,
        params: list[Any],
        retry_auth: bool = True,
    ) -> Any:
        """Executes a Deluge JSON-RPC call, re-logging in once on auth error."""
        headers = {"Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie

        payload = {"method": method, "params": params, "id": self._next_id()}
        resp = client.post(self._endpoint, json=payload, headers=headers)

        if resp.status_code in (401, 403) and retry_auth:
            if self._login(client):
                self._ensure_connected(client)
                return self._rpc(client, method, params, retry_auth=False)
            raise PermissionError("Deluge authentication failed")

        if resp.status_code != 200:
            raise RuntimeError(f"Deluge RPC failed (HTTP {resp.status_code}): {resp.text[:120]}")

        data = resp.json()
        error = data.get("error")
        if error:
            err_msg = error.get("message") if isinstance(error, dict) else str(error)
            if "authenticated" in str(err_msg).lower() and retry_auth:
                if self._login(client):
                    self._ensure_connected(client)
                    return self._rpc(client, method, params, retry_auth=False)
            raise RuntimeError(f"Deluge RPC error in {method}: {err_msg}")

        return data.get("result")

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity, authentication, and daemon status in Deluge."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0) as client:
                if not self._login(client):
                    return False, "Authentication failed (invalid credentials)"
                self._ensure_connected(client)
                # Query version from daemon or web
                try:
                    version = self._rpc(client, "daemon.get_version", [], retry_auth=False)
                except (RuntimeError, httpx.HTTPError) as exc:
                    logger.debug(
                        "Deluge daemon.get_version failed (%s), falling back to web.get_version",
                        exc,
                    )
                    version = self._rpc(client, "web.get_version", [], retry_auth=False)
                return True, f"Deluge {version or 'connected'}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except (httpx.RequestError, httpx.HTTPError) as e:
            return False, f"Connection error: {str(e)}"
        except Exception as e:
            logger.warning("Deluge test_connection failed: %s", e)
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """Deluge is a downloader; search is handled via Torznab indexers."""
        return []

    def download(self, result: AcquisitionSearchResult) -> str:
        """Submits magnet link or torrent URL to Deluge."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        target_url = result.magnet_url or result.download_url
        if not target_url:
            raise ValueError("No magnet or download URL provided for torrent")

        torrent_hash = result.download_id
        btih_match = _BTIH_RE.search(target_url)
        if btih_match:
            torrent_hash = btih_match.group(1).lower()

        options: dict[str, Any] = {}
        if self.download_location:
            options["download_location"] = self.download_location

        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    if not self._login(client):
                        raise PermissionError("Deluge authentication failed")
                    self._ensure_connected(client)

                if target_url.startswith("magnet:"):
                    res = self._rpc(client, "core.add_torrent_magnet", [target_url, options])
                else:
                    res = self._rpc(client, "core.add_torrent_url", [target_url, options])

                if res and isinstance(res, str):
                    torrent_hash = res.lower()

                # Label plugin integration: check if enabled
                if self.label:
                    try:
                        plugins = self._rpc(client, "core.get_enabled_plugins", [])
                        if isinstance(plugins, list) and any(str(p).lower() == "label" for p in plugins):
                            try:
                                self._rpc(client, "label.add", [self.label])
                            except (RuntimeError, httpx.HTTPError) as exc:
                                logger.debug("Deluge label.add: label may already exist: %s", exc)
                            try:
                                self._rpc(client, "label.set_torrent", [torrent_hash, self.label])
                            except Exception as e:
                                logger.warning("Could not set label on Deluge torrent %s: %s", torrent_hash, e)
                    except Exception as e:
                        logger.warning("Error inspecting Deluge label plugin: %s", e)

        except PermissionError as exc:
            raise PermissionError(f"Deluge authentication failed: {exc}") from exc
        except (httpx.RequestError, httpx.HTTPError) as exc:
            raise AcquisitionRetryableError(f"Network error connecting to Deluge: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"Deluge add_torrent failed: {exc}") from exc

        return torrent_hash

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Polls Deluge for torrent status, progress, speed, and location metrics."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        keys = [
            "name",
            "state",
            "progress",
            "total_size",
            "download_payload_rate",
            "eta",
            "save_path",
            "ratio",
            "seeding_time",
            "message",
            "time_since_transfer",
        ]

        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    self._login(client)
                    self._ensure_connected(client)

                res = self._rpc(client, "core.get_torrents_status", [{"id": [download_id.lower()]}, keys])
                if not isinstance(res, dict) or download_id.lower() not in res:
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

                tor = res[download_id.lower()]
                state = str(tor.get("state", "")).capitalize()
                raw_progress = float(tor.get("progress", 0.0))
                progress = min(100.0, round(raw_progress, 1))
                size = int(tor.get("total_size") or 0)
                speed = int(tor.get("download_payload_rate") or 0)
                eta = int(tor.get("eta") or 0)
                if eta < 0 or eta > 8640000:
                    eta = 0

                save_path = str(tor.get("save_path") or "").rstrip("/")
                name = str(tor.get("name") or "")
                content_path = posixpath.join(save_path, name) if save_path and name else (save_path or None)
                ratio = float(tor.get("ratio") or 0.0)
                seeding_time = int(tor.get("seeding_time") or tor.get("time_since_transfer") or 0)
                error_msg = str(tor.get("message") or "")

                if state == "Error":
                    status_str = DownloadStatus.FAILED.value
                elif state == "Seeding":
                    status_str = DownloadStatus.COMPLETED.value
                    progress = 100.0
                elif state == "Paused":
                    if progress >= 100.0:
                        status_str = DownloadStatus.COMPLETED.value
                    else:
                        status_str = DownloadStatus.QUEUED.value
                elif state in ("Queued", "Checking", "Allocating"):
                    status_str = DownloadStatus.QUEUED.value
                elif state == "Downloading":
                    status_str = DownloadStatus.DOWNLOADING.value
                else:
                    status_str = DownloadStatus.DOWNLOADING.value if progress < 100.0 else DownloadStatus.COMPLETED.value

                return {
                    "status": status_str,
                    "progress": progress,
                    "size_bytes": size,
                    "speed_bps": speed,
                    "eta_seconds": eta,
                    "source_path": content_path,
                    "content_path": content_path or "",
                    "save_path": save_path,
                    "ratio": ratio,
                    "seeding_time_seconds": seeding_time,
                    "error_message": error_msg if status_str == DownloadStatus.FAILED.value and error_msg else None,
                }
        except Exception as e:
            logger.warning("Error fetching Deluge status for %s: %s", download_id, e)
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
        """Removes torrent and downloaded files from Deluge."""
        if not is_safe_service_url(self.host_url):
            return False

        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    self._login(client)
                    self._ensure_connected(client)
                self._rpc(client, "core.remove_torrent", [download_id.lower(), True])
                return True
        except Exception as e:
            logger.error("Failed to cancel Deluge torrent %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes completed torrent from Deluge with optional file deletion."""
        if not is_safe_service_url(self.host_url):
            return False

        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    self._login(client)
                    self._ensure_connected(client)
                self._rpc(client, "core.remove_torrent", [download_id.lower(), delete_files])
                return True
        except Exception as e:
            logger.error("Failed to cleanup completed Deluge torrent %s: %s", download_id, e)
            return False

    def get_download_roots(self) -> list[str]:
        """Completed-download folders reported by Deluge config download_location."""
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []

        roots: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    self._login(client)
                    self._ensure_connected(client)
                loc = self._rpc(client, "core.get_config_value", ["download_location"])
                if loc and str(loc).strip():
                    roots.append(str(loc).strip())
                if self.download_location and self.download_location.strip():
                    roots.append(self.download_location.strip())
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Could not read Deluge download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []

        if not roots:
            self.last_roots_error = "Deluge reported no download_location"
        return list(dict.fromkeys(roots))

    def list_category(self) -> Optional[list[dict[str, Any]]]:
        """Torrents in TrackSeerr's label in Deluge.

        Returns normalized dicts: hash (lower case), name, size, ratio, seeding_time (seconds), content_path, state.
        An empty label is refused (returns None).
        """
        if not self.label or not self.label.strip():
            return None
        if not is_safe_service_url(self.host_url):
            raise RuntimeError("Prohibited host URL")

        keys = ["name", "total_size", "ratio", "seeding_time", "save_path", "state", "label"]
        with httpx.Client(timeout=self.timeout) as client:
            if not self._cookie:
                self._login(client)
                self._ensure_connected(client)
            res = self._rpc(client, "core.get_torrents_status", [{"label": self.label}, keys])

        if not isinstance(res, dict):
            raise RuntimeError("Deluge get_torrents_status returned an unexpected payload")

        label_target = self.label.strip().lower()
        out: list[dict[str, Any]] = []
        for h, tor in res.items():
            if not isinstance(tor, dict):
                continue
            tor_label = str(tor.get("label") or "").strip().lower()
            if tor_label != label_target:
                continue

            save_path = str(tor.get("save_path") or "").rstrip("/")
            name = str(tor.get("name") or "")
            content_path = posixpath.join(save_path, name) if save_path and name else save_path

            out.append(
                {
                    "hash": str(h).lower(),
                    "name": name,
                    "size": int(tor.get("total_size") or 0),
                    "ratio": float(tor.get("ratio") or 0.0),
                    "seeding_time": int(tor.get("seeding_time") or 0),
                    "content_path": content_path,
                    "state": str(tor.get("state") or ""),
                }
            )
        return out

    def set_share_limits(
        self, lookup: str, ratio: Optional[float], seed_time_minutes: Optional[int]
    ) -> bool:
        """Sets per-torrent share limits via core.set_torrent_options stop_at_ratio/stop_ratio/remove_at_ratio=false.

        Note: Deluge supports stop_at_ratio and stop_ratio options. Seed-time targets are enforced by
        TrackSeerr's own seed cleanup worker.
        """
        if not is_safe_service_url(self.host_url):
            return False

        options: dict[str, Any] = {"remove_at_ratio": False}
        if ratio is None or float(ratio) <= 0:
            options["stop_at_ratio"] = False
        else:
            options["stop_at_ratio"] = True
            options["stop_ratio"] = float(ratio)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not self._cookie:
                    self._login(client)
                    self._ensure_connected(client)
                self._rpc(client, "core.set_torrent_options", [[lookup.lower()], options])
                return True
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Deluge set_share_limits failed for %s: %s", lookup, e)
            return False
