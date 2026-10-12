"""Soulseek / slskd REST API Acquisition Driver."""

import logging
import posixpath
import threading
import time
from collections import Counter
from typing import Any, Optional
from urllib.parse import quote, unquote

import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)

# Soulseek punishes search floods: space searches globally across every driver instance.
_SEARCH_LOCK = threading.Lock()
_SEARCH_OVERRUN_SECONDS = 20.0  # extra polling past the soft deadline while slskd still reports the search running
_last_search_at: float = 0.0


class SlskdDriver(AcquisitionDriver):
    """Driver for slskd (Soulseek REST API daemon)."""

    def __init__(
        self,
        host_url: str,
        api_key: Optional[str] = None,
        download_dir: Optional[str] = None,
        timeout: float = 10.0,
        search_timeout: float = 12.0,
        max_peer_queue: int = 50,
        min_upload_speed_kbps: int = 0,
        ignored_users: Optional[list[str]] = None,
        min_search_interval: float = 5.0,
    ) -> None:
        self.max_peer_queue = max_peer_queue
        self.min_upload_speed_kbps = min_upload_speed_kbps
        self.ignored_users = {str(u).strip().lower() for u in (ignored_users or []) if str(u).strip()}
        self.min_search_interval = min_search_interval
        self.search_timeout = search_timeout
        self.host_url = host_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        # Explicit override only; otherwise the completed-downloads folder is read from slskd's own options.
        self.download_dir = (download_dir or "").strip() or None
        self.timeout = timeout
        self._downloads_root: Optional[str] = None
        self.peers_filtered = 0

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
        self._pace_search()

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
                    self.peers_filtered = 0
                    results = self._parse_responses(user_responses, artist, title, album)
                    files = sum(len(u.get("files", [])) for u in user_responses if isinstance(u, dict))
                    logger.info(
                        "slskd search id=%s query=%r finished state=%s responses=%d files=%d audio_kept=%d peers_filtered=%d in %.1fs",
                        search_id, query, state, len(user_responses), files, len(results), self.peers_filtered,
                        time.monotonic() - started,
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
        started = time.monotonic()
        deadline = started + self.search_timeout + 5.0
        hard_deadline = deadline + _SEARCH_OVERRUN_SECONDS
        state = "Unknown"
        overrun_logged = False
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if now >= deadline:
                if not any(s in state for s in ("InProgress", "Requested", "Queued")):
                    break
                if not overrun_logged:
                    overrun_logged = True
                    logger.debug("slskd search id=%s still %s past soft deadline; waiting up to %.0fs more",
                                 search_id, state, _SEARCH_OVERRUN_SECONDS)
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

    def _pace_search(self) -> None:
        """Block until ``min_search_interval`` has passed since the last search (any instance); no HTTP under the lock."""
        global _last_search_at
        if self.min_search_interval <= 0:
            return
        with _SEARCH_LOCK:
            wait = max(0.0, _last_search_at + self.min_search_interval - time.monotonic())
            if wait > 0:
                logger.debug("slskd search paced: waiting %.1fs", wait)
                time.sleep(wait)
            _last_search_at = time.monotonic()

    def _peer_rejected(self, user_resp: dict[str, Any]) -> bool:
        """True when the peer is ignored, has too long a queue, or is slower than the configured minimum."""
        uname = str(user_resp.get("username", "")).strip().lower()
        if uname and uname in self.ignored_users:
            return True
        queue = int(user_resp.get("queueLength") or 0)
        if self.max_peer_queue > 0 and queue > self.max_peer_queue:
            return True
        speed_kbps = int(user_resp.get("uploadSpeed") or 0) * 8 / 1000
        return self.min_upload_speed_kbps > 0 and speed_kbps < self.min_upload_speed_kbps

    @staticmethod
    def _quality_tag(f: dict[str, Any]) -> str:
        """Bracket-tag text that ``parse_release_title`` maps to the file's real quality."""
        ext = str(f.get("extension", "")).lower().lstrip(".")
        bitrate = int(f.get("bitRate") or 0)
        depth = int(f.get("bitDepth") or 0)
        rate = int(f.get("sampleRate") or 0)
        if ext == "flac":
            tag = "FLAC 24bit" if depth >= 24 else "FLAC 16bit"
            return f"{tag} {rate / 1000:g}kHz" if rate else tag
        if ext == "mp3":
            if f.get("isVariableBitRate"):
                if bitrate >= 220:
                    return "MP3 V0"
                return "MP3 V2" if bitrate >= 170 else "MP3"
            return "MP3 320" if bitrate >= 315 else "MP3"
        if ext in ("m4a", "aac"):
            if ext == "m4a" and depth:
                return "ALAC"
            return f"AAC {bitrate}" if bitrate else "AAC"
        if ext == "ogg":
            return "OGG"
        return "OPUS" if ext == "opus" else ""

    def _build_result(
        self,
        user_resp: dict[str, Any],
        f: dict[str, Any],
        ext: str,
        artist: str,
        album: Optional[str],
    ) -> AcquisitionSearchResult:
        uname = user_resp.get("username", "")
        fname = f.get("filename", "")
        size = int(f.get("size", 0))
        bitrate = f.get("bitRate")
        quality_desc = f"{ext.upper()}"
        if bitrate:
            quality_desc += f" {bitrate}kbps"

        base = fname.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        name = base.rsplit(".", 1)[0] if "." in base else base
        tag = self._quality_tag(f)
        unique_dl_id = f"{uname}::{quote(fname)}"
        return AcquisitionSearchResult(
            download_id=unique_dl_id,
            title=f"{name} [{tag}]" if tag else name,
            artist=artist,
            album=album,
            item_type="track",
            size_bytes=size,
            bit_rate=int(bitrate) if bitrate else None,
            format=ext,
            quality_str=quality_desc,
            download_url=unique_dl_id,
            source="slskd",
            extra={
                "username": uname,
                "filename": fname,
                "size": size,
                "bit_depth": f.get("bitDepth"),
                "sample_rate": f.get("sampleRate"),
                "length_seconds": f.get("length"),
                "peer": self._peer_info(user_resp),
            },
        )

    @staticmethod
    def _peer_info(user_resp: dict[str, Any]) -> dict[str, Any]:
        return {
            "has_free_slot": bool(user_resp.get("hasFreeUploadSlot")),
            "queue_length": int(user_resp.get("queueLength") or 0),
            "upload_speed": int(user_resp.get("uploadSpeed") or 0),
        }

    @staticmethod
    def _is_folder_id(download_id: str) -> bool:
        return "::dir::" in (download_id or "")

    @staticmethod
    def _split_folder_id(download_id: str) -> tuple[str, str]:
        """(username, unquoted remote directory) from a folder download id."""
        user, _, rest = download_id.partition("::dir::")
        return user, unquote(rest)

    def _build_folder_result(
        self,
        user_resp: dict[str, Any],
        directory: str,
        files: list[tuple[dict[str, Any], str]],
        artist: str,
        album: Optional[str],
    ) -> AcquisitionSearchResult:
        """One album candidate for a peer's directory; ``files`` is [(file dict, lowercase extension)]."""
        uname = user_resp.get("username", "")
        tags = Counter(self._quality_tag(f) for f, _ in files)
        top_tag = tags.most_common(1)[0][0]
        rep, rep_ext = next((f, e) for f, e in files if self._quality_tag(f) == top_tag)
        ordered = sorted(files, key=lambda fe: str(fe[0].get("filename", "")))
        entries = [
            {
                "filename": f.get("filename", ""),
                "size": int(f.get("size", 0)),
                "length_seconds": f.get("length"),
            }
            for f, _ in ordered
        ]
        lengths = [e["length_seconds"] for e in entries]
        total_length = None if any(v is None for v in lengths) else sum(lengths)
        total_size = sum(e["size"] for e in entries)
        bitrate = rep.get("bitRate")
        quality_desc = rep_ext.upper() + (f" {bitrate}kbps" if bitrate else "")
        name = self._last_component(directory)
        dl_id = f"{uname}::dir::{quote(directory)}"
        return AcquisitionSearchResult(
            download_id=dl_id,
            title=f"{name} [{top_tag}]" if top_tag else name,
            artist=artist,
            album=album,
            item_type="album",
            size_bytes=total_size,
            bit_rate=int(bitrate) if bitrate else None,
            format=rep_ext,
            quality_str=quality_desc,
            download_url=dl_id,
            source="slskd",
            extra={
                "username": uname,
                "directory": directory,
                "filename": directory,
                "files": entries,
                "track_count": len(entries),
                "total_length_seconds": total_length,
                "bit_depth": rep.get("bitDepth"),
                "sample_rate": rep.get("sampleRate"),
                "peer": self._peer_info(user_resp),
            },
        )

    def _parse_responses(
        self,
        payload: list[Any],
        artist: str,
        title: Optional[str],
        album: Optional[str],
    ) -> list[AcquisitionSearchResult]:
        results: list[AcquisitionSearchResult] = []
        for user_resp in payload:
            if self._peer_rejected(user_resp):
                self.peers_filtered += 1
                continue
            by_dir: dict[str, list[tuple[dict[str, Any], str]]] = {}
            for f in user_resp.get("files", []):
                ext = f.get("extension", "").lower().lstrip(".")
                if ext not in ("mp3", "flac", "m4a", "aac", "ogg", "opus"):
                    continue
                results.append(self._build_result(user_resp, f, ext, artist, album))
                fname = str(f.get("filename", ""))
                cut = max(fname.rfind("\\"), fname.rfind("/"))
                if cut > 0:
                    by_dir.setdefault(fname[:cut], []).append((f, ext))
            for directory, dir_files in by_dir.items():
                if len(dir_files) >= 2:
                    results.append(self._build_folder_result(user_resp, directory, dir_files, artist, album))
        return results

    def download(self, result: AcquisitionSearchResult) -> str:
        """Enqueues track or folder download via slskd."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        extra = result.extra or {}
        if self._is_folder_id(result.download_id):
            return self._download_folder(result, extra)
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

    def _download_folder(self, result: AcquisitionSearchResult, extra: dict[str, Any]) -> str:
        """Queue every file of a folder candidate in one slskd request; returns the folder id."""
        files = extra.get("files") or []
        if not files:
            raise ValueError("slskd folder candidate has no files")
        username = extra.get("username") or self._split_folder_id(result.download_id)[0]
        url = f"{self.host_url}/api/v0/transfers/downloads/{quote(username)}"
        payload = [{"filename": f["filename"], "size": f["size"]} for f in files]
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
            if resp.status_code not in (200, 201, 202):
                raise RuntimeError(f"slskd folder queue failed ({resp.status_code}): {resp.text[:120]}")
        return result.download_id

    @staticmethod
    def _find_directory(data: Any, username: str, directory: str) -> Optional[dict[str, Any]]:
        """The downloads-list directory object for one user's remote directory."""
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict) or item.get("username", "") != username:
                continue
            for d in item.get("directories", []):
                if d.get("directory") == directory:
                    return d
        return None

    def _folder_local_path(self, dir_obj: dict[str, Any], files: list[dict[str, Any]]) -> Optional[str]:
        """Local folder holding a completed directory transfer."""
        for f in files:
            reported = str(f.get("localFilename") or "").strip()
            if reported and posixpath.isabs(reported.replace("\\", "/")):
                return posixpath.dirname(reported.replace("\\", "/"))
            break
        base = self.download_dir or self._read_downloads_root()
        folder = self._last_component(str(dir_obj.get("directory") or ""))
        if not base or not folder or folder in (".", ".."):
            return None
        return posixpath.join(base.rstrip("/") or "/", folder)

    def _folder_status(self, dir_obj: dict[str, Any]) -> dict[str, Any]:
        """Aggregate the per-file transfers of one directory into a single status dict."""
        files = [f for f in dir_obj.get("files", []) if isinstance(f, dict)]
        states = [self._map_state(str(f.get("state", ""))) for f in files]
        total_size = sum(int(f.get("size", 0) or 0) for f in files)
        transferred = sum(int(f.get("bytesTransferred", 0) or 0) for f in files)
        speed = sum(int(f.get("averageSpeed", 0) or f.get("speed", 0) or 0) for f in files)
        failed = [(s, e) for s, e in states if s == DownloadStatus.FAILED.value]
        done = sum(1 for s, _ in states if s == DownloadStatus.COMPLETED.value)
        in_flight = any(s == DownloadStatus.DOWNLOADING.value for s, _ in states)
        source_path = None
        error = None
        if files and done == len(files):
            status = DownloadStatus.COMPLETED.value
            source_path = self._folder_local_path(dir_obj, files)
        elif failed and not in_flight:
            status = DownloadStatus.FAILED.value
            first = next((f.get("error") for f in files if f.get("error")), None) or failed[0][1]
            error = f"{len(failed)} of {len(files)} files failed: {first}"
        elif in_flight or done:
            status = DownloadStatus.DOWNLOADING.value
        else:
            status = DownloadStatus.QUEUED.value
        progress = 100.0 if status == DownloadStatus.COMPLETED.value else (
            transferred / total_size * 100.0 if total_size > 0 else 0.0
        )
        return {
            "status": status,
            "progress": round(progress, 1),
            "size_bytes": total_size,
            "speed_bps": speed,
            "eta_seconds": int((total_size - transferred) / speed) if speed > 0 and total_size > transferred else 0,
            "source_path": source_path,
            "error_message": error,
            "remote_state": files[0].get("state") if files else None,
        }

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

    @staticmethod
    def _missing_status() -> dict[str, Any]:
        """Not in slskd's list: the worker decides how long to tolerate this."""
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

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Inspects transfer status across all slskd downloads."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        target_user = None
        target_file = None
        folder = self._split_folder_id(download_id) if self._is_folder_id(download_id) else None
        if not folder and "::" in download_id:
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

            if folder:
                dir_obj = self._find_directory(data, folder[0], folder[1])
                return self._folder_status(dir_obj) if dir_obj else self._missing_status()

            transfer, transfer_dir = self._find_transfer(data, target_user, target_file, download_id)

            if not transfer:
                return self._missing_status()

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

    def _remove_transfers(self, download_id: str, ok_codes: tuple[int, ...]) -> Optional[bool]:
        """DELETE the transfer(s) behind a track or folder id from slskd's list.

        Returns None when nothing matches, else True only if every delete returned one of ``ok_codes``.
        Raises ``httpx.HTTPError`` / ``ValueError`` on transport or decode failures.
        """
        folder = self._split_folder_id(download_id) if self._is_folder_id(download_id) else None
        target_user, target_file = "", ""
        if folder:
            target_user = folder[0]
        elif "::" in download_id:
            target_user, _, raw = download_id.partition("::")
            target_file = unquote(raw)

        base = f"{self.host_url}/api/v0/transfers/downloads/{quote(target_user)}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(base, headers=self._headers())
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                return False
            data = resp.json()
            if folder:
                dir_obj = self._find_directory(data, folder[0], folder[1])
                ids = [f.get("id") for f in (dir_obj or {}).get("files", []) if f.get("id")]
            else:
                transfer, _ = self._find_transfer([data], target_user, target_file, download_id)
                ids = [transfer["id"]] if transfer and transfer.get("id") else []
            if not ids:
                return None
            ok = True
            for tid in ids:
                r = client.delete(f"{base}/{quote(str(tid))}", headers=self._headers(), params={"remove": "true"})
                ok = ok and r.status_code in ok_codes
            return ok

    def cancel(self, download_id: str) -> bool:
        """Cancels a track or folder transfer in slskd (DELETE by transfer id, removing it from the list)."""
        if not is_safe_service_url(self.host_url):
            return False
        try:
            return bool(self._remove_transfers(download_id, (200, 204)))
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Failed to cancel slskd transfer %s: %s: %s", download_id, type(e).__name__, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes finished transfer(s) from slskd's list; True when all gone or nothing was found."""
        if not is_safe_service_url(self.host_url):
            return False
        if "::" not in download_id:
            return True
        if delete_files:
            logger.debug("slskd cannot delete downloaded files via its API; ignoring delete_files for %s", download_id)
        try:
            result = self._remove_transfers(download_id, (200, 204, 404))
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Error cleaning up slskd transfer %s: %s: %s", download_id, type(e).__name__, e)
            return False
        return result is not False
