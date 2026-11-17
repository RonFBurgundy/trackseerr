"""Shared helpers for torrent clients that download only some files of a torrent."""

import logging
import posixpath
from typing import Any

from trackseerr.bencode import BencodeError, _get_val, decode, torrent_files
from trackseerr.clients.acquisition.base import AcquisitionUnavailableError
from trackseerr.models import AcquisitionSearchResult
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)


def _basename(path: Any) -> str:
    return posixpath.basename(str(path or "").replace("\\", "/"))


def prepare_partial_download(
    result: AcquisitionSearchResult, wanted: list[int], wanted_paths: list[str], timeout: float, client_name: str
) -> tuple[bytes, int]:
    """Fetches the .torrent and checks the wanted files against it.

    Returns ``(torrent_bytes, total_file_count)``; the count includes BEP-47 padding slots so
    it lines up with the torrent-order indices in ``wanted``. Raises ``AcquisitionUnavailableError``
    when only a magnet is available, the file cannot be read, or a wanted file does not match.
    """
    from trackseerr.clients.acquisition.torznab import IndexerUnavailable, fetch_torrent_file

    needs_file = AcquisitionUnavailableError(
        f"{client_name} needs the .torrent file to download part of a torrent",
        reason="release_unavailable",
    )
    url = result.download_url or ""
    if result.magnet_url or not url.startswith("http"):
        raise needs_file
    try:
        fetched = fetch_torrent_file(url, timeout)
    except IndexerUnavailable as exc:
        logger.warning("Could not fetch the .torrent file for a partial download: %s", safe_exc(exc))
        raise needs_file from exc
    if isinstance(fetched, str):
        raise needs_file

    try:
        meta = decode(fetched)
        listed = {f["file_index"]: f["path"] for f in torrent_files(meta)}
        files = _get_val(_get_val(meta, "info") or {}, "files")
    except BencodeError as exc:
        raise AcquisitionUnavailableError(
            "The indexer did not return a valid .torrent file", reason="release_unavailable"
        ) from exc
    total = len(files) if isinstance(files, list) else 1

    for pos, idx in enumerate(wanted):
        if idx < 0 or idx >= total:
            raise AcquisitionUnavailableError(
                f"Wanted file index {idx} not found in the torrent file list", reason="release_unavailable"
            )
        if pos < len(wanted_paths) and _basename(listed.get(idx)) != _basename(wanted_paths[pos]):
            raise AcquisitionUnavailableError(
                f"Index drift: file {idx} path {listed.get(idx)!r} does not match expected {wanted_paths[pos]!r}",
                reason="release_unavailable",
            )
    return fetched, total
