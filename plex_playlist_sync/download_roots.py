"""Allowed download roots: the import containment boundary derived from the download clients themselves.

Like Sonarr/Lidarr, the download client's own API is the source of truth for where finished files land. A
client-reported source path is acceptable only when, after ``Path.resolve()``, it sits under one of the allowed roots
(each client's reported completed-download folders translated through its remote path mappings, plus the legacy
``staging_folder_path`` when set) and it does not overlap the library root or the app config directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from plex_playlist_sync.clients.acquisition import get_acquisition_driver
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)

#: How long a client's reported roots are reused before the client API is asked again.
ROOTS_TTL_SECONDS = 300.0
#: A failed lookup is retried sooner so a briefly unreachable client recovers quickly.
ROOTS_ERROR_TTL_SECONDS = 30.0

_cache: dict[str, tuple[float, str, list[str], Optional[str]]] = {}
_cache_lock = threading.Lock()


def clear_roots_cache() -> None:
    """Drops every cached client lookup (used after client config changes and by tests)."""
    with _cache_lock:
        _cache.clear()


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _client_extra(cfg: dict[str, Any]) -> dict[str, Any]:
    extra = cfg.get("extra_settings_json")
    if not extra and isinstance(cfg.get("extra_settings"), dict):
        return dict(cfg["extra_settings"])
    try:
        data = json.loads(extra) if isinstance(extra, str) else extra
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _client_mappings(cfg: dict[str, Any]) -> list[dict[str, str]]:
    maps = _client_extra(cfg).get("remote_path_mappings", [])
    return [m for m in maps if isinstance(m, dict)] if isinstance(maps, list) else []


def _fingerprint(cfg: dict[str, Any]) -> str:
    blob = json.dumps(
        [cfg.get("driver_type"), cfg.get("host_url"), cfg.get("username"), cfg.get("api_key"), _client_extra(cfg)],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fetch_client_roots(
    cfg: dict[str, Any],
    *,
    force: bool = False,
    driver_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> tuple[list[str], Optional[str]]:
    """The client's completed-download roots translated to local paths, and an error string (None on success).

    Results are cached per client id (``ROOTS_TTL_SECONDS``; failures ``ROOTS_ERROR_TTL_SECONDS``), keyed to the
    client's connection settings so an edited client is re-read immediately.
    """
    from plex_playlist_sync.acquisition_worker import translate_remote_path

    client_id = str(cfg.get("id") or cfg.get("name") or "")
    fp = _fingerprint(cfg)
    now = time.monotonic()
    raw_roots: list[str]
    error: Optional[str]
    with _cache_lock:
        hit = _cache.get(client_id) if client_id else None
    if hit and not force and hit[1] == fp and hit[0] > now:
        raw_roots, error = hit[2], hit[3]
    else:
        try:
            driver = (driver_factory or get_acquisition_driver)(cfg)
            raw_roots = [str(r) for r in driver.get_download_roots() if str(r or "").strip()]
            error = (getattr(driver, "last_roots_error", None) or None) if not raw_roots else None
            error = str(error) if error else None
        except (ValueError, TypeError, OSError) as exc:
            logger.warning("Could not build driver to read download folders for client %s: %s", client_id, safe_exc(exc))
            raw_roots, error = [], f"Could not read download folder: {safe_exc(exc)}"
        ttl = ROOTS_TTL_SECONDS if raw_roots or not error else ROOTS_ERROR_TTL_SECONDS
        if client_id:
            with _cache_lock:
                _cache[client_id] = (now + ttl, fp, raw_roots, error)

    mappings = _client_mappings(cfg)
    mapped: list[str] = []
    for root in raw_roots:
        local = translate_remote_path(root, mappings)
        if local and local not in mapped:
            mapped.append(local)
    return mapped, error


def _config_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = (os.getenv("CONFIG_DIR") or "").strip()
    if env_dir:
        dirs.append(Path(env_dir).resolve())
    if os.path.isdir("/config"):
        dirs.append(Path("/config").resolve())
    return list(dict.fromkeys(dirs))


@dataclass
class AllowedRoots:
    """Resolved allowed roots plus the paths a download may never overlap."""

    roots: list[Path] = field(default_factory=list)
    library_root: Optional[Path] = None
    config_dirs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def matching_root(self, path: Path) -> Optional[Path]:
        """The most specific allowed root containing the already-resolved ``path``."""
        hits = [r for r in self.roots if _under(path, r)]
        return max(hits, key=lambda r: len(r.parts)) if hits else None

    def check(self, candidate: str | Path) -> tuple[bool, str]:
        """``(accepted, reason)`` for a client-reported local path; ``reason`` explains a rejection."""
        try:
            path = Path(candidate).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return False, f"path could not be resolved ({exc})"
        if self.library_root is not None and (_under(path, self.library_root) or _under(self.library_root, path)):
            return False, "path overlaps the library root"
        root = self.matching_root(path)
        if root is None:
            return False, "path is outside every download folder reported by the download client"
        for cfg_dir in self.config_dirs:
            if _under(path, cfg_dir) and not _under(root, cfg_dir):
                return False, "path is inside the application config directory"
        return True, ""

    def is_allowed(self, candidate: str | Path) -> bool:
        return self.check(candidate)[0]

    def usable_roots(self) -> list[Path]:
        """Roots safe to scan or write under: any root that is the library or inside it is dropped."""
        if self.library_root is None:
            return list(self.roots)
        return [r for r in self.roots if not _under(r, self.library_root)]


def _legacy_staging(media_settings: dict[str, Any]) -> Optional[str]:
    raw = str(media_settings.get("staging_folder_path") or "").strip()
    return raw or None


def build_allowed_roots(
    media_settings: dict[str, Any],
    clients: Iterable[dict[str, Any]],
    *,
    extra_roots: Iterable[str] = (),
    driver: Any = None,
) -> AllowedRoots:
    """Allowed roots for the given clients (each translated through its own mappings) plus legacy staging."""
    out = AllowedRoots(config_dirs=_config_dirs())
    library_raw = str(media_settings.get("root_folder_path") or "").strip()
    if library_raw:
        out.library_root = Path(library_raw).resolve()
    seen: list[str] = []
    for cfg in clients:
        if str(cfg.get("driver_type") or "").lower() == "lidarr":
            continue
        roots, error = fetch_client_roots(cfg, driver_factory=(lambda _cfg: driver) if driver is not None else None)
        if error:
            out.errors.append(f"Could not read download folder from {cfg.get('name') or cfg.get('id')}: {error}")
        seen.extend(roots)
    legacy = _legacy_staging(media_settings)
    if legacy:
        seen.append(legacy)
    seen.extend(str(r) for r in extra_roots if str(r or "").strip())
    for raw in seen:
        resolved = Path(raw).resolve()
        if resolved not in out.roots:
            out.roots.append(resolved)
    return out


def allowed_roots_for_client(
    db: Any, media_settings: dict[str, Any], client_cfg: dict[str, Any], driver: Any = None
) -> AllowedRoots:
    """Allowed roots for one download client's completed items (reusing the caller's driver instance if given)."""
    return build_allowed_roots(media_settings, [client_cfg], driver=driver)


def allowed_roots_for_all_clients(db: Any, media_settings: Optional[dict[str, Any]] = None) -> AllowedRoots:
    """Allowed roots across every enabled client (manual import, library health)."""
    mm = media_settings if media_settings is not None else db.get_media_management_settings()
    try:
        clients = db.list_download_clients(enabled_only=True)
    except sqlite3.Error as exc:
        logger.warning("Could not list download clients for allowed roots: %s", safe_exc(exc))
        clients = []
    return build_allowed_roots(mm, clients)


def describe_client_roots(db: Any, *, force: bool = False) -> list[dict[str, Any]]:
    """``[{client_id, name, roots, error}]`` for every enabled non-Lidarr client, for the UI."""
    out: list[dict[str, Any]] = []
    for cfg in db.list_download_clients(enabled_only=True):
        if str(cfg.get("driver_type") or "").lower() == "lidarr":
            continue
        roots, error = fetch_client_roots(cfg, force=force)
        out.append(
            {
                "client_id": str(cfg.get("id") or ""),
                "name": str(cfg.get("name") or cfg.get("id") or ""),
                "roots": roots,
                "error": error,
            }
        )
    return out
