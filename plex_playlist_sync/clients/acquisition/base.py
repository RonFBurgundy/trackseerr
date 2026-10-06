"""Base acquisition driver interface for native download clients."""

from abc import ABC, abstractmethod
import logging
from typing import Any, Optional

from plex_playlist_sync.models import AcquisitionSearchResult


logger = logging.getLogger(__name__)
_share_limits_unsupported_logged: set[str] = set()


class AcquisitionRetryableError(RuntimeError):
    """A dispatch failed for a transient reason (rate limit, data still loading); the caller may retry later.

    ``reason`` is a short machine code (``rate_limited``, ``albums_pending``) shown to the requester.
    """

    def __init__(self, message: str, reason: str = "retry_later", retry_after: int = 60) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


class AcquisitionUnavailableError(RuntimeError):
    """The release cannot be fetched by this client (e.g. not in Lidarr's metadata profile); retrying soon is futile."""

    def __init__(self, message: str, reason: str = "not_in_metadata_profile") -> None:
        super().__init__(message)
        self.reason = reason


class AcquisitionDriver(ABC):
    """Abstract base class for all acquisition drivers."""

    #: True for torrent clients, whose completed files seed from their original location (import mode applies).
    is_torrent: bool = False

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity and authentication with download client or indexer.

        Returns:
            tuple[bool, str]: (success, status_or_error_message)
        """
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
    ) -> list[AcquisitionSearchResult]:
        """Search client/indexer for music tracks or albums matching the query.

        Returns:
            list[AcquisitionSearchResult]: Formatted search results with metadata.
        """
        raise NotImplementedError

    def fetch_recent(self, limit: int = 100) -> list[AcquisitionSearchResult]:
        """Poll recent releases from indexer feed. Default empty implementation."""
        return []

    @abstractmethod
    def download(self, result: AcquisitionSearchResult) -> str:
        """Submit a download request to the download client.

        Args:
            result: The chosen AcquisitionSearchResult.

        Returns:
            str: Unique download ID or hash assigned by the client.
        """
        raise NotImplementedError

    @abstractmethod
    def get_status(self, download_id: str) -> dict[str, Any]:
        """Query download status, progress, speed, and file location from the client.

        Args:
            download_id: Unique download ID or hash.

        Returns:
            dict[str, Any]: Standardized status dictionary containing:
                - status: str ("queued", "downloading", "completed", "failed")
                - progress: float (0.0 - 100.0)
                - size_bytes: int
                - speed_bps: int
                - eta_seconds: int
                - source_path: Optional[str]
                - error_message: Optional[str]
        """
        raise NotImplementedError

    @abstractmethod
    def cancel(self, download_id: str) -> bool:
        """Cancel and remove a download from the client.

        Args:
            download_id: Unique download ID or hash.

        Returns:
            bool: True if successfully cancelled/removed, False otherwise.
        """
        raise NotImplementedError

    def cleanup_completed(self, download_id: str, delete_files: bool = False) -> bool:
        """Removes a completed download from the client.

        Args:
            download_id: Unique download ID or hash.
            delete_files: If True, delete downloaded files. Default False.

        Returns:
            bool: True if successfully cleaned up, False otherwise.
        """
        return False

    def set_share_limits(
        self, lookup: str, ratio: Optional[float], seed_time_minutes: Optional[int]
    ) -> bool:
        """Pushes seed limits to the client so they hold while TrackSeerr is down. ``None`` = use the client's
        own default, ``0`` = no limit. Default: unsupported (logged once), returns False."""
        name = type(self).__name__
        if name not in _share_limits_unsupported_logged:
            _share_limits_unsupported_logged.add(name)
            logger.info("Download client driver %s doesn't support share limits; TrackSeerr governs seeding", name)
        return False
