"""rTorrent BitTorrent Client Acquisition Driver (XML-RPC over HTTP)."""

import logging
import posixpath
import time
import xmlrpc.client
from typing import Any, Optional
from urllib.parse import unquote
from xml.parsers.expat import ExpatError

import httpx

from trackseerr.bencode import BencodeError, info_hash
from trackseerr.clients.acquisition.base import (
    AcquisitionDriver,
    AcquisitionRetryableError,
    AcquisitionUnavailableError,
)
from trackseerr.clients.acquisition.qbittorrent import _BTIH_RE, _btih_to_hex
from trackseerr.models import AcquisitionSearchResult, DownloadStatus
from trackseerr.redaction import safe_exc
from trackseerr.security import is_safe_service_url

logger = logging.getLogger(__name__)

_MAX_REPLY_BYTES = 20 * 1024 * 1024

_STATUS_FIELDS = (
    "d.name",
    "d.size_bytes",
    "d.completed_bytes",
    "d.down.rate",
    "d.complete",
    "d.state",
    "d.is_active",
    "d.hashing",
    "d.message",
    "d.base_path",
    "d.directory",
    "d.is_multi_file",
    "d.ratio",
    "d.timestamp.finished",
    "d.is_partially_done",
    "d.left_bytes",
    "d.custom=tsr_done",
)

_LIST_FIELDS = (
    "d.hash=",
    "d.name=",
    "d.size_bytes=",
    "d.ratio=",
    "d.timestamp.finished=",
    "d.base_path=",
    "d.directory=",
    "d.custom1=",
    "d.state=",
    "d.complete=",
    "d.is_partially_done=",
    "d.custom=tsr_done",
)


class _RtorrentFault(RuntimeError):
    """rTorrent answered an XML-RPC call with a fault (for example an unknown info-hash)."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _queued_status() -> dict[str, Any]:
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


def _failed_status(message: str) -> dict[str, Any]:
    out = _queued_status()
    out["status"] = DownloadStatus.FAILED.value
    out["error_message"] = message
    return out


def _seeding_seconds(finished: int, complete: bool, marked: int = 0) -> int:
    stamp = finished if finished > 0 else marked
    if complete and stamp > 0:
        return max(0, int(time.time()) - stamp)
    return 0


def _content_path(f: dict[str, Any]) -> str:
    base_path = str(f.get("d.base_path") or "").strip()
    if base_path:
        return base_path
    directory = str(f.get("d.directory") or "").rstrip("/")
    if _int(f.get("d.is_multi_file")) == 1:
        return directory
    return posixpath.join(directory, str(f.get("d.name") or ""))


def _status_from_fields(f: dict[str, Any], progress: float) -> tuple[str, float, Optional[str]]:
    """Maps rTorrent state flags to (status, progress, error_message)."""
    state = _int(f.get("d.state"))
    if _int(f.get("d.hashing")) != 0:
        return DownloadStatus.QUEUED.value, progress, None
    if _int(f.get("d.complete")) == 1 or _int(f.get("d.is_partially_done")) == 1:
        return DownloadStatus.COMPLETED.value, 100.0, None
    if state == 1 and _int(f.get("d.is_active")) == 1:
        return DownloadStatus.DOWNLOADING.value, progress, None
    # Tracker warnings land in d.message while a torrent downloads, so only a stopped torrent reports it.
    message = str(f.get("d.message") or "").strip()
    return DownloadStatus.QUEUED.value, progress, (message if state == 0 and message else None)


def _fetch_payload(result: AcquisitionSearchResult, timeout: float) -> tuple[Optional[bytes], str]:
    """Fetches the .torrent locally (a remote seedbox usually cannot reach a home indexer) or falls back to the link."""
    if result.magnet_url:
        return None, result.magnet_url

    download_url = result.download_url or ""
    if download_url.startswith("http"):
        from trackseerr.clients.acquisition.torznab import (
            IndexerUnavailable,
            fetch_torrent_file,
        )

        try:
            fetched = fetch_torrent_file(download_url, timeout)
            if isinstance(fetched, str):
                return None, fetched
            return fetched, download_url
        except IndexerUnavailable as exc:
            logger.warning(
                "Could not fetch the .torrent file, letting rTorrent fetch the link: %s",
                safe_exc(exc),
            )
            return None, download_url

    return None, download_url


def _resolve_hash(torrent_bytes: Optional[bytes], target_url: str, result: AcquisitionSearchResult) -> str:
    if torrent_bytes is not None:
        try:
            return info_hash(torrent_bytes)
        except BencodeError as exc:
            raise AcquisitionUnavailableError(
                "The indexer did not return a valid .torrent file",
                reason="release_unavailable",
            ) from exc

    extra = result.extra if isinstance(result.extra, dict) else {}
    btih_match = _BTIH_RE.search(target_url)
    if btih_match:
        return _btih_to_hex(btih_match.group(1))
    if extra.get("info_hash"):
        return _btih_to_hex(str(extra["info_hash"]))
    raise AcquisitionUnavailableError(
        "Could not download the .torrent file from the indexer",
        reason="release_unavailable",
    )


def _basename(path: Any) -> str:
    return posixpath.basename(str(path or "").replace("\\", "/"))


def _verify_file_paths(rows: list[Any], wanted: list[int], wanted_paths: list[str]) -> None:
    for pos, w_idx in enumerate(wanted):
        if w_idx < 0 or w_idx >= len(rows):
            raise AcquisitionUnavailableError(
                f"Wanted file index {w_idx} not found in rTorrent file list",
                reason="release_unavailable",
            )
        if pos < len(wanted_paths):
            row = rows[w_idx]
            f_path = row[0] if isinstance(row, list) and row else row
            if _basename(f_path) != _basename(wanted_paths[pos]):
                raise AcquisitionUnavailableError(
                    f"Index drift: file {w_idx} path {f_path!r} does not match expected {wanted_paths[pos]!r}",
                    reason="release_unavailable",
                )


class RtorrentDriver(AcquisitionDriver):
    """Driver for rTorrent via its XML-RPC endpoint (SCGI behind an HTTP server such as nginx or ruTorrent)."""

    is_torrent = True

    def __init__(
        self,
        host_url: str,
        rpc_path: str = "/RPC2",
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

    @property
    def _endpoint(self) -> str:
        return f"{self.host_url}{self.rpc_path}"

    @property
    def _auth(self) -> Optional[tuple[str, str]]:
        if self.username or self.password:
            return (self.username or "", self.password or "")
        return None

    def _call(self, client: httpx.Client, method: str, *params: Any) -> Any:
        """Executes one XML-RPC call and returns its value."""
        body = xmlrpc.client.dumps(params, methodname=method, allow_none=False)
        resp = client.post(self._endpoint, content=body, headers={"Content-Type": "text/xml"})

        if resp.status_code in (401, 403):
            raise PermissionError(f"rTorrent authentication failed (HTTP {resp.status_code})")
        resp.raise_for_status()

        text = resp.text
        if len(text) > _MAX_REPLY_BYTES:
            raise RuntimeError("rTorrent reply is too large")
        try:
            result, _method = xmlrpc.client.loads(text)
        except xmlrpc.client.Fault as fault:
            raise _RtorrentFault(f"rTorrent {method} failed: {fault.faultString}") from fault
        except ExpatError as exc:
            raise RuntimeError("rTorrent returned an invalid XML-RPC reply") from exc
        if not result:
            raise RuntimeError("rTorrent returned an empty XML-RPC reply")
        return result[0]

    def _multicall(self, client: httpx.Client, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Runs several calls in one request; a faulted call yields None for its slot."""
        payload = [{"methodName": m, "params": p} for m, p in calls]
        replies = self._call(client, "system.multicall", payload)
        if not isinstance(replies, list):
            raise RuntimeError("rTorrent system.multicall returned an unexpected payload")
        out: list[Any] = []
        for reply in replies:
            if isinstance(reply, list) and reply:
                out.append(reply[0])
            else:
                out.append(None)
        return out

    def test_connection(self) -> tuple[bool, str]:
        """Validates connectivity and authentication against rTorrent."""
        if not is_safe_service_url(self.host_url):
            return False, "Invalid or prohibited host URL (SSRF defense)"

        try:
            with httpx.Client(timeout=5.0, auth=self._auth) as client:
                version = self._call(client, "system.client_version")
                return True, f"Connected to rTorrent {version}"
        except PermissionError:
            return False, "Authentication failed (invalid credentials)"
        except httpx.TimeoutException:
            return False, "Connection timed out (5s)"
        except httpx.HTTPError as e:
            return False, f"Connection error: {str(e)}"
        except RuntimeError as e:
            logger.warning("rTorrent test_connection failed: %s", e)
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """rTorrent is a downloader; search is handled via Torznab indexers."""
        return []

    def _already_loaded(self, client: httpx.Client, torrent_hash: str) -> bool:
        try:
            self._call(client, "d.hash", torrent_hash.upper())
        except _RtorrentFault:
            return False
        return True

    def _load_commands(self) -> list[str]:
        cmds: list[str] = []
        if self.download_dir:
            cmds.append(f"d.directory.set={self.download_dir}")
        if self.category:
            cmds.append(f"d.custom1.set={self.category}")
        return cmds

    def _fetch_file_list(self, client: httpx.Client, thash: str) -> list[Any]:
        for _ in range(10):
            try:
                rows = self._call(client, "f.multicall", thash, "", "f.path=")
                if isinstance(rows, list) and rows:
                    return rows
            except _RtorrentFault:
                pass
            time.sleep(0.5)
        self._call(client, "d.erase", thash)
        raise AcquisitionUnavailableError(
            "rTorrent did not load the torrent's file list in time",
            reason="release_unavailable",
        )

    def _load_partial(
        self,
        client: httpx.Client,
        torrent_bytes: bytes,
        torrent_hash: str,
        wanted: list[int],
        wanted_paths: list[str],
        cmds: list[str],
    ) -> None:
        thash = torrent_hash.upper()
        self._call(client, "load.raw", "", xmlrpc.client.Binary(torrent_bytes), *cmds)
        rows = self._fetch_file_list(client, thash)
        try:
            _verify_file_paths(rows, wanted, wanted_paths)
        except AcquisitionUnavailableError:
            self._call(client, "d.erase", thash)
            raise
        wanted_set = set(wanted)
        for idx in range(len(rows)):
            self._call(client, "f.priority.set", f"{thash}:f{idx}", 1 if idx in wanted_set else 0)
        self._call(client, "d.update_priorities", thash)
        self._call(client, "d.start", thash)

    def download(self, result: AcquisitionSearchResult) -> str:
        """Uploads the .torrent (or hands over the magnet) to rTorrent and returns the lower-case info hash."""
        if not is_safe_service_url(self.host_url):
            raise ValueError("Prohibited host URL")

        extra = result.extra if isinstance(result.extra, dict) else {}
        wanted = list(extra.get("file_indices") or [])
        wanted_paths = list(extra.get("file_paths") or [])

        torrent_bytes, target_url = _fetch_payload(result, self.timeout)
        if torrent_bytes is None and not target_url:
            raise ValueError("No magnet or download URL provided for torrent")
        if wanted and torrent_bytes is None:
            raise AcquisitionUnavailableError(
                "rTorrent needs the .torrent file to download part of a torrent",
                reason="release_unavailable",
            )
        torrent_hash = _resolve_hash(torrent_bytes, target_url, result)

        cmds = self._load_commands()
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                if self._already_loaded(client, torrent_hash):
                    return torrent_hash
                if wanted and torrent_bytes is not None:
                    self._load_partial(client, torrent_bytes, torrent_hash, wanted, wanted_paths, cmds)
                elif torrent_bytes is not None:
                    self._call(client, "load.raw_start", "", xmlrpc.client.Binary(torrent_bytes), *cmds)
                else:
                    self._call(client, "load.start", "", target_url, *cmds)
        except AcquisitionUnavailableError:
            raise
        except PermissionError as exc:
            raise PermissionError(f"rTorrent authentication failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AcquisitionRetryableError(f"Network error connecting to rTorrent: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"rTorrent load failed: {exc}") from exc

        return torrent_hash

    def get_status(self, download_id: str) -> dict[str, Any]:
        """Polls rTorrent for torrent status, progress, and speed metrics."""
        if not is_safe_service_url(self.host_url):
            return {"status": DownloadStatus.FAILED.value, "error_message": "Prohibited host URL"}

        thash = download_id.upper()
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                values = self._multicall(client, [(m, [thash]) for m in _STATUS_FIELDS])
                fields = dict(zip(_STATUS_FIELDS, values))
                if fields.get("d.name") is None:
                    return _queued_status()
                complete = _int(fields.get("d.complete")) == 1 or _int(fields.get("d.is_partially_done")) == 1
                marked = _int(fields.get("d.custom=tsr_done"))
                if complete and _int(fields.get("d.timestamp.finished")) == 0 and marked == 0:
                    try:
                        self._call(client, "d.custom.set", thash, "tsr_done", str(int(time.time())))
                    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                        logger.debug("Failed to set tsr_done for %s: %s", download_id, exc)
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as e:
            logger.warning("Error fetching rTorrent status for %s: %s", download_id, e)
            return _failed_status(str(e))

        return self._build_status(fields, marked=marked)

    @staticmethod
    def _build_status(f: dict[str, Any], marked: int = 0) -> dict[str, Any]:
        size = _int(f.get("d.size_bytes"))
        completed = _int(f.get("d.completed_bytes"))
        rate = _int(f.get("d.down.rate"))
        # A partial download (selected files only) leaves unwanted bytes outstanding, so measure what is wanted.
        left = _int(f["d.left_bytes"]) if f.get("d.left_bytes") is not None else None
        if left is not None and completed + left > 0:
            progress = min(100.0, round(completed / (completed + left) * 100.0, 1))
        else:
            progress = min(100.0, round(completed / size * 100.0, 1)) if size > 0 else 0.0
        remaining = left if left is not None else max(0, size - completed)
        status_str, progress, error_message = _status_from_fields(f, progress)

        content_path = _content_path(f)
        directory = str(f.get("d.directory") or "").rstrip("/")
        save_path = directory if _int(f.get("d.is_multi_file")) == 1 else posixpath.dirname(content_path)
        complete = _int(f.get("d.complete")) == 1 or _int(f.get("d.is_partially_done")) == 1

        return {
            "status": status_str,
            "progress": progress,
            "size_bytes": size,
            "speed_bps": rate,
            "eta_seconds": max(0, remaining) // rate if rate > 0 else 0,
            "source_path": content_path or None,
            "content_path": content_path,
            "save_path": save_path,
            "ratio": _int(f.get("d.ratio")) / 1000.0,
            "seeding_time_seconds": _seeding_seconds(_int(f.get("d.timestamp.finished")), complete, marked),
            "error_message": error_message,
        }

    def _erase(self, download_id: str) -> None:
        with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
            self._call(client, "d.erase", download_id.upper())

    def cancel(self, download_id: str) -> bool:
        """Removes the torrent from rTorrent. XML-RPC cannot delete data, so downloaded files stay on the client."""
        if not is_safe_service_url(self.host_url):
            return False
        try:
            self._erase(download_id)
            return True
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as e:
            logger.error("Failed to cancel rTorrent torrent %s: %s", download_id, e)
            return False

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes a completed torrent from rTorrent (files cannot be deleted over XML-RPC)."""
        if not is_safe_service_url(self.host_url):
            return False
        try:
            self._erase(download_id)
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as e:
            logger.error("Failed to cleanup completed rTorrent torrent %s: %s", download_id, e)
            return False
        if delete_files:
            logger.info(
                "rTorrent cannot delete files over XML-RPC; files for %s were left in place", download_id
            )
        return True

    def get_download_roots(self) -> list[str]:
        """Completed-download folders reported by rTorrent's directory.default."""
        self.last_roots_error = None
        if not is_safe_service_url(self.host_url):
            self.last_roots_error = "Prohibited host URL"
            return []

        roots: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
                dd = str(self._call(client, "directory.default") or "").strip()
                if dd:
                    roots.append(dd)
                if self.download_dir and self.download_dir.strip():
                    roots.append(self.download_dir.strip())
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as e:
            logger.warning("Could not read rTorrent download folders from %s: %s", self.host_url, e)
            self.last_roots_error = str(e) or type(e).__name__
            return []

        if not roots:
            self.last_roots_error = "rTorrent reported no default directory"
        return list(dict.fromkeys(roots))

    def list_category(self) -> Optional[list[dict[str, Any]]]:
        """Torrents in TrackSeerr's label (d.custom1) in rTorrent.

        Returns normalized dicts: hash (lower case), name, size, ratio, seeding_time (seconds), content_path, state.
        An empty category is refused (returns None).
        """
        if not self.category or not self.category.strip():
            return None
        if not is_safe_service_url(self.host_url):
            raise RuntimeError("Prohibited host URL")

        with httpx.Client(timeout=self.timeout, auth=self._auth) as client:
            rows = self._call(client, "d.multicall2", "", "main", *_LIST_FIELDS)

        if not isinstance(rows, list):
            raise RuntimeError("rTorrent d.multicall2 returned an unexpected payload")

        cat_target = self.category.strip().lower()
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < len(_LIST_FIELDS) or not row[0]:
                continue
            f = dict(zip(_LIST_FIELDS, row))
            if unquote(str(f["d.custom1="] or "")).strip().lower() != cat_target:
                continue
            out.append(self._list_row(f))
        return out

    @staticmethod
    def _list_row(f: dict[str, Any]) -> dict[str, Any]:
        complete = _int(f["d.complete="]) == 1 or _int(f["d.is_partially_done="]) == 1
        if complete:
            state = "seeding"
        elif _int(f["d.state="]) == 1:
            state = "downloading"
        else:
            state = "stopped"
        return {
            "hash": str(f["d.hash="]).lower(),
            "name": str(f["d.name="] or ""),
            "size": _int(f["d.size_bytes="]),
            "ratio": _int(f["d.ratio="]) / 1000.0,
            "seeding_time": _seeding_seconds(_int(f["d.timestamp.finished="]), complete, _int(f.get("d.custom=tsr_done"))),
            "content_path": str(f["d.base_path="] or f["d.directory="] or ""),
            "state": state,
        }
