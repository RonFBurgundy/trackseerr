"""Software update checker: checks GitHub releases for newer TrackSeerr versions."""

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Tuple

import httpx

from plex_playlist_sync.changelog import get_build_info
from plex_playlist_sync.config import Config
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import Database
from plex_playlist_sync.task_manager import (
    TRIGGER_SCHEDULED,
    record_task_run,
    wait_for_next_cycle,
)

try:
    from packaging import version as pkg_version
except ImportError:
    pkg_version = None

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/RonFBurgundy/trackseerr/releases/latest"
TIMEOUT_SECONDS = 10.0
ACCEPT_HEADER = "application/vnd.github+json"
USER_AGENT_PREFIX = "TrackSeerr"


def _fallback_parse_tuple(ver_str: str) -> Tuple[Tuple[int, ...], int, int]:
    """Fallback numeric comparison when packaging.version is unavailable.

    Returns: (numeric_tuple, prerelease_tier, prerelease_num)
    prerelease_tier: 0 for dev, 1 for alpha, 2 for beta, 3 for rc, 4 for final release.
    """
    clean = ver_str.strip().lstrip("vV").lower()
    # Find numeric chunks from base version (excluding prerelease / build tags)
    main_part = clean.split("-")[0].split("+")[0]
    base_part = re.split(r"(?:[.-]?(?:dev|alpha|a|beta|b|rc))\d*", main_part)[0]
    nums = tuple(int(m) for m in re.findall(r"\d+", base_part))

    tier = 4
    pre_num = 0
    if ".dev" in clean or "dev" in clean:
        tier = 0
        m = re.search(r"dev(\d+)", clean)
        if m:
            pre_num = int(m.group(1))
    elif "alpha" in clean or "a" in clean:
        tier = 1
        m = re.search(r"(?:alpha|a)(\d+)", clean)
        if m:
            pre_num = int(m.group(1))
    elif "beta" in clean or "b" in clean:
        tier = 2
        m = re.search(r"(?:beta|b)(\d+)", clean)
        if m:
            pre_num = int(m.group(1))
    elif "rc" in clean:
        tier = 3
        m = re.search(r"rc(\d+)", clean)
        if m:
            pre_num = int(m.group(1))

    return (nums, tier, pre_num)


def is_newer_version(remote_version: str, current_version: str) -> bool:
    """Returns True if remote_version is strictly newer than current_version.

    Handles leading 'v', PEP 440 prereleases, and local/dev builds.
    """
    rem = remote_version.strip().lstrip("vV")
    cur = current_version.strip().lstrip("vV")
    if not rem or not cur:
        return False

    if pkg_version is not None:
        try:
            return pkg_version.parse(rem) > pkg_version.parse(cur)
        except Exception:
            # Fall back to heuristic parse if packaging raises InvalidVersion
            pass

    try:
        rem_tuple = _fallback_parse_tuple(rem)
        cur_tuple = _fallback_parse_tuple(cur)
        return rem_tuple > cur_tuple
    except Exception:
        return False


def fetch_latest_release(current_version: str) -> Tuple[Optional[dict[str, Any]], Optional[str]]:
    """Performs GET to GitHub releases/latest with 10s timeout and TrackSeerr User-Agent.

    Returns: (release_data_dict_or_None, error_message_or_None)
    """
    headers = {
        "Accept": ACCEPT_HEADER,
        "User-Agent": f"{USER_AGENT_PREFIX}/{current_version}",
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            resp = client.get(GITHUB_RELEASES_URL, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data, None
                return None, "GitHub returned unexpected response shape"
            elif resp.status_code == 404:
                # No releases published yet
                logger.info("GitHub API returned 404: no releases found for %s", GITHUB_RELEASES_URL)
                return None, None
            elif resp.status_code in (403, 429):
                err_msg = f"GitHub API rate limit exceeded (HTTP {resp.status_code})"
                logger.warning("Update check rate limit: %s", err_msg)
                return None, err_msg
            else:
                err_msg = f"GitHub API returned HTTP {resp.status_code}"
                logger.warning("Update check HTTP error: %s", err_msg)
                return None, err_msg
    except httpx.TimeoutException as exc:
        err_msg = f"GitHub request timed out: {safe_exc(exc)}"
        logger.warning("Update check timeout: %s", err_msg)
        return None, err_msg
    except httpx.NetworkError as exc:
        err_msg = f"GitHub network error: {safe_exc(exc)}"
        logger.warning("Update check network error: %s", err_msg)
        return None, err_msg
    except httpx.HTTPError as exc:
        err_msg = f"GitHub HTTP error: {safe_exc(exc)}"
        logger.warning("Update check HTTP error: %s", err_msg)
        return None, err_msg
    except Exception as exc:
        err_msg = f"GitHub check error: {safe_exc(exc)}"
        logger.warning("Update check error: %s", err_msg)
        return None, err_msg


def is_update_check_enabled(db: Database, config: Optional[Config] = None) -> bool:
    """Returns True if update checking is enabled in both environment and database."""
    # 1. Environment check: UPDATE_CHECK=false forces it off
    if config is not None:
        if not getattr(config, "update_check", True):
            return False
    else:
        env_val = os.getenv("UPDATE_CHECK")
        if env_val is not None:
            clean = env_val.strip().lower()
            if clean not in ("1", "true", "yes", "y", "on"):
                return False

    # 2. Database setting
    state = db.get_update_check_state()
    return bool(state.get("enabled", True))


def run_update_check(db: Database, config: Optional[Config] = None) -> dict[str, Any]:
    """Executes update check if enabled, persists the result, and returns full update status."""
    current_version, current_commit = get_build_info()
    enabled = is_update_check_enabled(db, config)

    if not enabled:
        state = db.get_update_check_state()
        latest = state.get("latest_version")
        update_available = bool(latest and is_newer_version(latest, current_version))
        return {
            "current_version": current_version,
            "current_commit": current_commit,
            "latest_version": latest,
            "release_url": state.get("release_url"),
            "published_at": state.get("published_at"),
            "checked_at": state.get("checked_at"),
            "update_available": update_available,
            "enabled": False,
            "error": state.get("error"),
        }

    release_data, error = fetch_latest_release(current_version)
    checked_at = datetime.now(timezone.utc).isoformat()

    if release_data is not None:
        tag_raw = str(release_data.get("tag_name") or release_data.get("name") or "").strip()
        latest_version = tag_raw.lstrip("vV")
        release_url = release_data.get("html_url")
        published_at = release_data.get("published_at")
        db.save_update_check_result(
            latest_version=latest_version,
            release_url=release_url,
            published_at=published_at,
            checked_at=checked_at,
            error=None,
        )
        update_available = is_newer_version(latest_version, current_version)
        return {
            "current_version": current_version,
            "current_commit": current_commit,
            "latest_version": latest_version,
            "release_url": release_url,
            "published_at": published_at,
            "checked_at": checked_at,
            "update_available": update_available,
            "enabled": True,
            "error": None,
        }

    if error is None:
        # 404: No releases yet
        db.save_update_check_result(
            latest_version=None,
            release_url=None,
            published_at=None,
            checked_at=checked_at,
            error=None,
        )
        state = db.get_update_check_state()
        latest = state.get("latest_version")
        update_available = bool(latest and is_newer_version(latest, current_version))
        return {
            "current_version": current_version,
            "current_commit": current_commit,
            "latest_version": latest,
            "release_url": state.get("release_url"),
            "published_at": state.get("published_at"),
            "checked_at": checked_at,
            "update_available": update_available,
            "enabled": True,
            "error": None,
        }

    # Error occurred (rate limit, network, timeout): keep last good result and record error
    db.save_update_check_result(
        latest_version=None,
        release_url=None,
        published_at=None,
        checked_at=checked_at,
        error=error,
    )
    state = db.get_update_check_state()
    latest = state.get("latest_version")
    update_available = bool(latest and is_newer_version(latest, current_version))
    return {
        "current_version": current_version,
        "current_commit": current_commit,
        "latest_version": latest,
        "release_url": state.get("release_url"),
        "published_at": state.get("published_at"),
        "checked_at": checked_at,
        "update_available": update_available,
        "enabled": True,
        "error": error,
    }


def get_update_status(db: Database, config: Optional[Config] = None) -> dict[str, Any]:
    """Returns the current cached update status without making an HTTP request."""
    current_version, current_commit = get_build_info()
    enabled = is_update_check_enabled(db, config)
    state = db.get_update_check_state()
    latest = state.get("latest_version")
    update_available = bool(latest and is_newer_version(latest, current_version))
    return {
        "current_version": current_version,
        "current_commit": current_commit,
        "latest_version": latest,
        "release_url": state.get("release_url"),
        "published_at": state.get("published_at"),
        "checked_at": state.get("checked_at"),
        "update_available": update_available,
        "enabled": enabled,
        "error": state.get("error"),
    }


class UpdateCheckWorker:
    """Background worker for periodic GitHub release checks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        db: Any,
        config: Any,
        initial_delay: float = 60.0,
        interval_seconds: float = 12 * 3600.0,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        with self._lock:
            if self._is_running:
                return False
            self._stop_event.clear()
            self._is_running = True

        cycle_interval = interval_fn or (lambda: float(interval_seconds))

        def _loop() -> None:
            if not self._stop_event.wait(initial_delay):
                while not self._stop_event.is_set():
                    self.run_once(db, config)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="UpdateCheckWorkerThread")
        self._thread.start()
        return True

    def run_once(self, db: Any, config: Any) -> bool:
        """Runs one scheduled update check under record_task_run."""
        try:
            with record_task_run(db, "update_check", TRIGGER_SCHEDULED) as run:
                result = run_update_check(db, config)
                if result.get("error"):
                    run.failed = result["error"]
                    run.message = f"error: {result['error']}"
                elif not result.get("enabled"):
                    run.message = "update check disabled"
                elif result.get("latest_version"):
                    avail_suffix = " (update available)" if result.get("update_available") else ""
                    run.message = f"latest={result['latest_version']}{avail_suffix}"
                else:
                    run.message = "no releases found"
            return True
        except Exception as exc:
            logger.error("UpdateCheckWorker: check failed: %s", safe_exc(exc))
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


update_check_worker = UpdateCheckWorker()
