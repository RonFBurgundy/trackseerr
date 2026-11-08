"""qBittorrent WebAPI v2 Acquisition Driver."""

import logging
import ntpath
from pathlib import Path
import posixpath
import re
import time
from typing import Any, Optional

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.redaction import safe_exc
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)

_BTIH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


class QbittorrentDriver(AcquisitionDriver):
    """Driver for qBittorrent Web API v2."""

    is_torrent = True

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
        self.last_login_status: Optional[int] = None

    @staticmethod
    def _sid_cookie(cookies: Any) -> Optional[tuple[str, str]]:
        """Return the (name, value) of the session cookie: ``SID`` (<=4.x) or ``QBT_SID_<port>`` (5.x)."""
        for name, value in cookies.items():
            if name == "SID" or name.startswith("QBT_SID"):
                return name, value
        return None

    def _login(self, client: httpx.Client, raise_transport: bool = False) -> bool:
        """Authenticates with qBittorrent and caches the SID cookie."""
        if not self.username and not self.password:
            return True
        login_url = f"{self.host_url}/api/v2/auth/login"
        try:
            resp = client.post(
                login_url,
                data={"username": self.username or "", "password": self.password or ""},
                headers={"Referer": self.host_url, "Origin": self.host_url},
            )
            self.last_login_status = resp.status_code
            if not 200 <= resp.status_code < 300 or "Fails." in resp.text:
                return False
            sid = self._sid_cookie(resp.cookies) or self._sid_cookie(client.cookies)
            if "Ok." in resp.text or resp.status_code == 204 or sid:
                self._cookie = f"{sid[0]}={sid[1]}" if sid else None
                return True
            return False
        except httpx.HTTPError as e:
            if raise_transport and isinstance(e, httpx.TransportError):
                raise
            logger.warning("qBittorrent login exception: %s", e)
            return False

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity and authentication against qBittorrent; logs one warning on failure."""
        self.last_login_status = None
        ok, msg = self._test_connection()
        if not ok:
            logger.warning(
                "qBittorrent connection test failed for %s (login HTTP status: %s): %s",
                self.host_url,
                self.last_login_status if self.last_login_status is not None else "none",
                msg,
            )
        return ok, msg

    def _test_connection(self) -> tuple[bool, str]:
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0) as client:
                if self.username or self.password:
                    if not self._login(client, raise_transport=True):
                        if self.last_login_status == 403:
                            return False, "qBittorrent banned this IP after too many failed logins; wait or restart qBittorrent"
                        if self.last_login_status == 401:
                            return False, (
                                "qBittorrent rejected the login: check the username/password, and that the port in the "
                                "URL is qBittorrent's own WebUI port (qBittorrent rejects remapped ports unless Host "
                                "header validation is off)"
                            )
                        return False, "Authentication failed (invalid credentials)"
                version_url = f"{self.host_url}/api/v2/app/version"
                headers = {"Cookie": self._cookie} if self._cookie else {}
                resp = client.get(version_url, headers=headers)
                if resp.status_code == 200:
                    return True, f"qBittorrent {resp.text.strip()}"
                return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except httpx.TransportError as e:
            return False, f"Could not reach qBittorrent at {self.host_url}: {type(e).__name__}: {e}"
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

    def _check_add_response(self, resp: httpx.Response) -> None:
        """Validates qBittorrent torrents/add response."""
        status = resp.status_code
        text = resp.text
        if status not in (200, 202) or "Fails." in text:
            raise RuntimeError(f"qBittorrent torrents/add failed (HTTP {status}): {text[:120]}")

        try:
            data = resp.json()
        except ValueError:
            data = None

        if isinstance(data, dict):
            failure_count = data.get("failure_count")
            if isinstance(failure_count, int) and failure_count > 0:
                raise RuntimeError(f"qBittorrent torrents/add failed (HTTP {status}): {text[:120]}")

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
        elif result.extra and result.extra.get("info_hash"):
            torrent_hash = str(result.extra["info_hash"]).lower()
        elif ":" in torrent_hash:
            torrent_hash = torrent_hash.split(":")[0].lower()

        extra = result.extra if isinstance(result.extra, dict) else {}
        file_indices = extra.get("file_indices")

        with httpx.Client(timeout=self.timeout) as client:
            if self._cookie:
                client.headers["Cookie"] = self._cookie
            else:
                self._login(client)
                if self._cookie:
                    client.headers["Cookie"] = self._cookie

            if file_indices:
                return self._download_partial(client, result, target_url, torrent_hash)

            add_url = f"{self.host_url}/api/v2/torrents/add"
            payload = {
                "urls": target_url,
                "category": self.category,
                "paused": "false",
            }
            resp = client.post(add_url, data=payload)
            self._check_add_response(resp)

        return torrent_hash

    def _download_partial(
        self,
        client: httpx.Client,
        result: AcquisitionSearchResult,
        target_url: str,
        torrent_hash: str,
    ) -> str:
        """Downloads only selected files from a torrent pack."""
        add_url = f"{self.host_url}/api/v2/torrents/add"
        payload = {
            "urls": target_url,
            "category": self.category,
            "paused": "true",
            "stopped": "true",
        }
        resp = client.post(add_url, data=payload)
        self._check_add_response(resp)

        files_data = self._wait_for_file_list(client, torrent_hash)
        self._set_file_priorities(client, result, torrent_hash, files_data)
        self._resume_torrent(client, torrent_hash)
        return torrent_hash

    def _wait_for_file_list(
        self, client: httpx.Client, torrent_hash: str, max_wait_seconds: int = 60
    ) -> list[dict[str, Any]]:
        """Polls for torrent file list, deleting torrent and raising on timeout."""
        files_url = f"{self.host_url}/api/v2/torrents/files?hash={torrent_hash}"
        for _ in range(max_wait_seconds):
            try:
                resp = client.get(files_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list):
                        return data
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("Waiting for qBittorrent file list for %s: %s", torrent_hash, safe_exc(exc))
            time.sleep(1)

        self._delete_torrent(client, torrent_hash, delete_files=False)
        raise RuntimeError("qBittorrent did not receive the torrent's file list in time")

    def _delete_torrent(
        self, client: httpx.Client, torrent_hash: str, delete_files: bool = False
    ) -> None:
        """Deletes a torrent from qBittorrent."""
        del_url = f"{self.host_url}/api/v2/torrents/delete"
        payload = {"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"}
        try:
            client.post(del_url, data=payload)
        except Exception as e:
            logger.warning("Failed to delete torrent %s: %s", torrent_hash, e)

    def _set_file_priorities(
        self,
        client: httpx.Client,
        result: AcquisitionSearchResult,
        torrent_hash: str,
        files_data: list[dict[str, Any]],
    ) -> None:
        """Verifies indices and sets priority 0 for other files and 1 for wanted files."""
        extra = result.extra if isinstance(result.extra, dict) else {}
        wanted_indices = list(extra.get("file_indices") or [])
        stored_paths = list(extra.get("file_paths") or [])

        file_by_idx = {
            f.get("index", idx): f
            for idx, f in enumerate(files_data)
        }

        for w_idx, s_path in zip(wanted_indices, stored_paths):
            f = file_by_idx.get(w_idx)
            s_basename = Path(str(s_path).replace("\\", "/")).name
            if not f:
                self._delete_torrent(client, torrent_hash, delete_files=False)
                raise RuntimeError(f"Wanted file index {w_idx} not found in qBittorrent file list")
            f_name = str(f.get("name") or "").replace("\\", "/")
            if not f_name.endswith(s_basename):
                self._delete_torrent(client, torrent_hash, delete_files=False)
                raise RuntimeError(
                    f"Index drift: file {w_idx} name {f_name!r} does not match expected {s_basename!r}"
                )

        wanted_set = set(wanted_indices)
        all_indices = [f.get("index", idx) for idx, f in enumerate(files_data)]
        other_indices = [idx for idx in all_indices if idx not in wanted_set]

        prio_url = f"{self.host_url}/api/v2/torrents/filePrio"
        if other_indices:
            for i in range(0, len(other_indices), 500):
                chunk = other_indices[i : i + 500]
                ids_str = "|".join(str(idx) for idx in chunk)
                client.post(prio_url, data={"hash": torrent_hash, "id": ids_str, "priority": 0})

        wanted_ids_str = "|".join(str(idx) for idx in wanted_indices)
        client.post(prio_url, data={"hash": torrent_hash, "id": wanted_ids_str, "priority": 1})

    def _resume_torrent(self, client: httpx.Client, torrent_hash: str) -> None:
        """Resumes torrent via torrents/start with fallback to torrents/resume for qBittorrent 4."""
        start_url = f"{self.host_url}/api/v2/torrents/start"
        resp = client.post(start_url, data={"hashes": torrent_hash})
        if resp.status_code == 404:
            resume_url = f"{self.host_url}/api/v2/torrents/resume"
            client.post(resume_url, data={"hashes": torrent_hash})

    def _apply_partial_progress(
        self, client: httpx.Client, download_id: str, progress: float, size: int
    ) -> tuple[float, int]:
        """Calculates progress and size based on selected files if partial torrent."""
        files_url = f"{self.host_url}/api/v2/torrents/files?hash={download_id.lower()}"
        try:
            files_resp = client.get(files_url)
            if files_resp.status_code != 200:
                return progress, size
            files = files_resp.json()
            is_files_list = (
                isinstance(files, list)
                and files
                and all(isinstance(f, dict) and "priority" in f for f in files)
            )
            if not is_files_list:
                return progress, size
            has_unwanted = any(f.get("priority") == 0 for f in files)
            selected_files = [f for f in files if (f.get("priority") or 0) > 0]
            if not (has_unwanted and selected_files):
                return progress, size

            sel_size = sum(int(f.get("size") or 0) for f in selected_files)
            sel_done = sum(
                float(f.get("size") or 0) * float(f.get("progress") or 0.0)
                for f in selected_files
            )
            size = sel_size
            if sel_size > 0:
                part_prog = sel_done / sel_size
            else:
                part_prog = sum(float(f.get("progress") or 0.0) for f in selected_files) / len(selected_files)
            progress = min(100.0, round(part_prog * 100.0, 1))
            if all(float(f.get("progress") or 0.0) >= 1.0 for f in selected_files):
                progress = 100.0
        except Exception as exc:
            logger.debug("Failed to check partial torrent files status for %s: %s", download_id, exc)
        return progress, size

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

                # Check if this is a partial torrent via /torrents/files
                progress, size = self._apply_partial_progress(client, download_id, progress, size)

                status_str = DownloadStatus.DOWNLOADING.value
                if progress >= 100.0 or any(s in state for s in ("uploading", "pausedup", "queuedup", "stalledup")):
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
                    "content_path": tor.get("content_path") or "",
                    "save_path": tor.get("save_path") or "",
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

    def _get_json(self, client: httpx.Client, path: str) -> Any:
        """Authenticated GET returning parsed JSON; re-logs in once on 403. Raises RuntimeError on non-200."""
        url = f"{self.host_url}{path}"
        if not self._cookie:
            self._login(client)
        if self._cookie:
            client.headers["Cookie"] = self._cookie
        resp = client.get(url)
        if resp.status_code == 403:
            self._login(client)
            if self._cookie:
                client.headers["Cookie"] = self._cookie
            resp = client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"qBittorrent {path} failed (HTTP {resp.status_code})")
        return resp.json()

    def get_download_roots(self) -> list[str]:
        """Completed-download folders: the default ``save_path`` plus the configured category's ``savePath``.

        ``temp_path`` (incomplete downloads) is deliberately never returned.
        """
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []
        roots: list[str] = []
        save_path = ""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                prefs = self._get_json(client, "/api/v2/app/preferences")
                if isinstance(prefs, dict):
                    save_path = str(prefs.get("save_path") or "").strip()
                    if save_path:
                        roots.append(save_path)
                if self.category and self.category.strip():
                    cats = self._get_json(client, "/api/v2/torrents/categories")
                    cat = cats.get(self.category) if isinstance(cats, dict) else None
                    if isinstance(cat, dict):
                        cat_path = str(cat.get("savePath") or cat.get("save_path") or "").strip()
                        if cat_path:
                            is_abs = cat_path.startswith("/") or (
                                len(cat_path) >= 2
                                and cat_path[0].isalpha()
                                and cat_path[1] == ":"
                                and (len(cat_path) == 2 or cat_path[2] in ("\\", "/"))
                            )
                            if is_abs:
                                roots.append(cat_path)
                            elif not save_path:
                                logger.debug(
                                    "Skipping relative category savePath %r because qBittorrent reported no save_path",
                                    cat_path,
                                )
                            else:
                                is_win = (
                                    len(save_path) >= 2
                                    and save_path[0].isalpha()
                                    and save_path[1] == ":"
                                    and (len(save_path) == 2 or save_path[2] in ("\\", "/"))
                                ) or "\\" in save_path
                                pmod = ntpath if is_win else posixpath
                                roots.append(pmod.normpath(pmod.join(save_path, cat_path)))
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning("Could not read qBittorrent download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []
        if not roots:
            self.last_roots_error = "qBittorrent reported no save_path"
        return list(dict.fromkeys(roots))

    def list_category(self) -> Optional[list[dict[str, Any]]]:
        """Every torrent in this client's configured category (``torrents/info?category=``).

        Returns normalized dicts: hash (lower case), name, size, ratio, seeding_time (seconds), content_path, state.
        An empty category is refused (returns None, "unsupported"): qBittorrent treats ``category=`` as *uncategorized*,
        which would sweep in torrents TrackSeerr never owned. Raises RuntimeError/httpx.HTTPError when the client
        cannot be queried, so the caller can tell "no torrents" from "could not look".
        """
        if not self.category or not self.category.strip():
            return None
        if not is_safe_service_url(self.host_url):
            raise RuntimeError("Prohibited host URL")
        url = f"{self.host_url}/api/v2/torrents/info"
        with httpx.Client(timeout=self.timeout) as client:
            if self._cookie:
                client.headers["Cookie"] = self._cookie
            else:
                self._login(client)
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
            resp = client.get(url, params={"category": self.category})
            if resp.status_code == 403:
                self._login(client)
                if self._cookie:
                    client.headers["Cookie"] = self._cookie
                resp = client.get(url, params={"category": self.category})
            if resp.status_code != 200:
                raise RuntimeError(f"qBittorrent torrents/info failed (HTTP {resp.status_code})")
            items = resp.json()
        if not isinstance(items, list):
            raise RuntimeError("qBittorrent torrents/info returned an unexpected payload")
        out: list[dict[str, Any]] = []
        for tor in items:
            if not isinstance(tor, dict) or not tor.get("hash"):
                continue
            out.append(
                {
                    "hash": str(tor["hash"]).lower(),
                    "name": str(tor.get("name") or ""),
                    "size": int(tor.get("total_size") or tor.get("size") or 0),
                    "ratio": float(tor.get("ratio") or 0.0),
                    "seeding_time": int(tor.get("seeding_time") or tor.get("time_seeded") or 0),
                    "content_path": str(tor.get("content_path") or ""),
                    "state": str(tor.get("state") or ""),
                }
            )
        return out

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
            "shareLimitAction": "Default",
            "shareLimitsMode": "Default",
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
                    logger.warning(
                        "qBittorrent setShareLimits failed for %s (HTTP %s): %s",
                        lookup,
                        resp.status_code,
                        resp.text[:120],
                    )
                    return False
                return True
        except httpx.HTTPError as e:
            logger.warning("qBittorrent setShareLimits failed for %s: %s", lookup, e)
            return False
