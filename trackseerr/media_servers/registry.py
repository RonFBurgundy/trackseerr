"""Process-wide registry of configured media servers and their cached adapters.

Holds every configured media server (Plex, Subsonic, and Jellyfin) across the process. Environment-configured servers
are represented as locked rows whose credentials always come from the environment rather than the database. The
installed list of server records is swapped as a single immutable reference so concurrent readers never observe a
half-updated configuration.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from trackseerr.config import (
    MEDIA_SERVER_JELLYFIN,
    MEDIA_SERVER_PLEX,
    MEDIA_SERVER_SOURCE_ENV,
    MEDIA_SERVER_SUBSONIC,
    Config,
)
from trackseerr.media_servers import _saved_url_is_unsafe
from trackseerr.media_servers.base import MediaServer, MediaServerError
from trackseerr.media_servers.jellyfin import JellyfinMediaServer
from trackseerr.media_servers.plex import PlexMediaServer
from trackseerr.media_servers.subsonic import SubsonicMediaServer
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)

ROLE_REFRESH = "refresh"
ROLE_PLAYLISTS = "playlists"
ROLE_USERS = "users"
ROLE_SCROBBLES = "scrobbles"
ROLES = (ROLE_REFRESH, ROLE_PLAYLISTS, ROLE_USERS, ROLE_SCROBBLES)

_ROLE_FLAGS = (
    ("use_refresh", ROLE_REFRESH),
    ("use_playlists", ROLE_PLAYLISTS),
    ("use_users", ROLE_USERS),
    ("use_scrobbles", ROLE_SCROBBLES),
)


@dataclass(frozen=True)
class ServerRecord:
    """Immutable snapshot of a configured media server and its enabled roles."""

    id: str
    kind: str
    name: str
    source: str
    url: str
    username: str = ""
    password: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    music_section: str = ""
    verify_ssl: bool = True
    enabled: bool = True
    roles: frozenset[str] = frozenset(ROLES)
    is_login: bool = False
    path_mapping: Optional[dict[str, Any]] = None

    @property
    def locked(self) -> bool:
        """True when the server is controlled by environment variables."""
        return self.source == MEDIA_SERVER_SOURCE_ENV


def env_records(config: Config) -> list[ServerRecord]:
    """Build ``ServerRecord`` entries for every media server configured via environment variables."""
    result: list[ServerRecord] = []
    if config.plex_url and config.plex_token:
        result.append(
            ServerRecord(
                id=f"env-{MEDIA_SERVER_PLEX}",
                kind=MEDIA_SERVER_PLEX,
                name="Plex",
                source=MEDIA_SERVER_SOURCE_ENV,
                url=config.plex_url,
                api_key=config.plex_token,
                music_section=config.plex_music_section or "",
                verify_ssl=config.plex_verify_ssl,
            )
        )
    if config.media_server_source == MEDIA_SERVER_SOURCE_ENV:
        if config.subsonic_url and (
            config.subsonic_api_key or (config.subsonic_user and config.subsonic_password)
        ):
            result.append(
                ServerRecord(
                    id=f"env-{MEDIA_SERVER_SUBSONIC}",
                    kind=MEDIA_SERVER_SUBSONIC,
                    name="Subsonic",
                    source=MEDIA_SERVER_SOURCE_ENV,
                    url=config.subsonic_url,
                    username=config.subsonic_user,
                    password=config.subsonic_password,
                    api_key=config.subsonic_api_key,
                    verify_ssl=config.plex_verify_ssl,
                )
            )
        if config.jellyfin_url and config.jellyfin_api_key:
            result.append(
                ServerRecord(
                    id=f"env-{MEDIA_SERVER_JELLYFIN}",
                    kind=MEDIA_SERVER_JELLYFIN,
                    name="Jellyfin",
                    source=MEDIA_SERVER_SOURCE_ENV,
                    url=config.jellyfin_url,
                    username=config.jellyfin_user,
                    api_key=config.jellyfin_api_key,
                    verify_ssl=config.plex_verify_ssl,
                )
            )
    return result


def sync_env_rows(config: Config, db: Any) -> None:
    """Ensure ``media_servers`` DB rows match the environment-configured servers without storing secrets."""
    env = env_records(config)
    env_ids = {rec.id for rec in env}

    for rec in env:
        if db.get_media_server_row(rec.id) is None:
            db.insert_media_server_row(
                {"id": rec.id, "kind": rec.kind, "source": MEDIA_SERVER_SOURCE_ENV, "name": rec.name}
            )

    for row in db.list_media_server_rows():
        if row["source"] == MEDIA_SERVER_SOURCE_ENV and row["id"] not in env_ids:
            db.delete_media_server_row(row["id"])

    rows = db.list_media_server_rows()
    plex_id = f"env-{MEDIA_SERVER_PLEX}"
    if not any(row["is_login"] for row in rows) and any(row["id"] == plex_id for row in rows):
        db.set_login_media_server(plex_id)


def _record_from_row(row: dict[str, Any], env_by_id: dict[str, ServerRecord]) -> Optional[ServerRecord]:
    """Convert one DB row into a ``ServerRecord``, overlaying env credentials for env rows."""
    roles = frozenset(role for flag, role in _ROLE_FLAGS if row.get(flag))
    if row["source"] == MEDIA_SERVER_SOURCE_ENV:
        env = env_by_id.get(row["id"])
        if env is None:
            return None
        url, username, password, api_key, music_section, verify_ssl = (
            env.url,
            env.username,
            env.password,
            env.api_key,
            env.music_section,
            env.verify_ssl,
        )
    else:
        url = row["url"]
        username = row["username"]
        password = row["password"]
        api_key = row["api_key"]
        music_section = row["music_section"]
        verify_ssl = bool(row["verify_ssl"])

    return ServerRecord(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        source=row["source"],
        url=url,
        username=username,
        password=password,
        api_key=api_key,
        music_section=music_section,
        verify_ssl=verify_ssl,
        enabled=bool(row["enabled"]),
        roles=roles,
        is_login=bool(row["is_login"]),
        path_mapping=row.get("path_mapping"),
    )


def load_records(config: Config, db: Any) -> list[ServerRecord]:
    """Load ordered ``ServerRecord`` instances from the database and overlay environment credentials."""
    env_by_id = {rec.id: rec for rec in env_records(config)}
    loaded: list[ServerRecord] = []
    for row in db.list_media_server_rows():
        record = _record_from_row(row, env_by_id)
        if record is not None:
            loaded.append(record)
    return loaded


_records_lock = threading.Lock()
_records: tuple[ServerRecord, ...] = ()


def install(records: Sequence[ServerRecord]) -> None:
    """Atomically replace the process-wide installed server record tuple."""
    global _records
    snapshot = tuple(records)
    with _records_lock:
        _records = snapshot


def records() -> tuple[ServerRecord, ...]:
    """Return the currently installed tuple of ``ServerRecord`` instances."""
    with _records_lock:
        return _records


def reload(config: Config, db: Any) -> None:
    """Sync env rows, load records from ``db``, and install them; keep previous list on SQLite error."""
    try:
        sync_env_rows(config, db)
        loaded = load_records(config, db)
    except sqlite3.Error as exc:
        logger.error("Failed to reload media server registry: %s", type(exc).__name__)
        return
    install(loaded)


_adapters_lock = threading.Lock()
_adapters: dict[str, tuple[tuple[Any, ...], MediaServer]] = {}


def _instantiate_adapter(record: ServerRecord) -> Optional[MediaServer]:
    """Create a fresh ``MediaServer`` adapter for ``record``, or None on initialization failure."""
    if record.kind == MEDIA_SERVER_PLEX:
        from trackseerr.clients.plex import PlexClient

        section = record.music_section or None
        try:
            return PlexMediaServer(
                PlexClient(record.url, record.api_key, verify_ssl=record.verify_ssl, music_section=section),
                music_section=section,
            )
        except Exception as exc:  # noqa: BLE001 - PlexServer raises plexapi/requests/ssl errors; cause is logged
            logger.error("Failed to initialize media server: %s", safe_exc(exc))
            return None
    if record.kind == MEDIA_SERVER_SUBSONIC:
        try:
            return SubsonicMediaServer(
                record.url,
                record.username,
                record.password,
                api_key=record.api_key,
                verify_ssl=record.verify_ssl,
            )
        except MediaServerError as exc:
            logger.error("Failed to initialize Subsonic media server: %s", exc.safe_detail)
            return None
    if record.kind == MEDIA_SERVER_JELLYFIN:
        try:
            return JellyfinMediaServer(
                record.url,
                record.api_key,
                record.username,
                verify_ssl=record.verify_ssl,
            )
        except MediaServerError as exc:
            logger.error("Failed to initialize Jellyfin media server: %s", exc.safe_detail)
            return None
    return None


def build_adapter(record: ServerRecord) -> Optional[MediaServer]:
    """Return the cached adapter for ``record``, building a new one if configuration changed.

    One adapter (one pooled HTTP client) is shared per distinct configuration. When the settings change a new adapter
    replaces it; the old one is NOT closed here because a running sync may still be using it. Its HTTP client closes
    when the last holder lets go of it.
    """
    if _saved_url_is_unsafe(record.source, record.kind, record.url):
        return None
    key = (
        record.kind,
        record.url,
        record.username,
        record.password,
        record.api_key,
        record.music_section,
        record.verify_ssl,
    )
    with _adapters_lock:
        cached = _adapters.get(record.id)
        if cached is not None and cached[0] == key:
            return cached[1]
        adapter = _instantiate_adapter(record)
        if adapter is None:
            return None
        _adapters[record.id] = (key, adapter)
        return adapter


def reset_adapters() -> None:
    """Clear the cached adapter instances."""
    with _adapters_lock:
        _adapters.clear()


def media_servers(role: Optional[str] = None) -> list[tuple[ServerRecord, MediaServer]]:
    """Return ``(record, adapter)`` pairs for all enabled servers matching ``role`` whose adapter builds."""
    if role is not None and role not in ROLES:
        raise ValueError(f"Unknown media server role: {role!r}")
    result: list[tuple[ServerRecord, MediaServer]] = []
    for rec in records():
        if not rec.enabled:
            continue
        if role is not None and role not in rec.roles:
            continue
        adapter = build_adapter(rec)
        if adapter is not None:
            result.append((rec, adapter))
    return result


def server_by_id(server_id: str) -> Optional[tuple[ServerRecord, MediaServer]]:
    """Return ``(record, adapter)`` for ``server_id`` (enabled or disabled) if present and buildable."""
    for rec in records():
        if rec.id == server_id:
            adapter = build_adapter(rec)
            return (rec, adapter) if adapter is not None else None
    return None


def login_server() -> Optional[tuple[ServerRecord, MediaServer]]:
    """Return ``(record, adapter)`` for the enabled login server, or None."""
    for rec in records():
        if rec.enabled and rec.is_login:
            adapter = build_adapter(rec)
            return (rec, adapter) if adapter is not None else None
    return None


__all__ = [
    "ROLE_PLAYLISTS",
    "ROLE_REFRESH",
    "ROLE_SCROBBLES",
    "ROLE_USERS",
    "ROLES",
    "ServerRecord",
    "build_adapter",
    "env_records",
    "install",
    "load_records",
    "login_server",
    "media_servers",
    "records",
    "reload",
    "reset_adapters",
    "server_by_id",
    "sync_env_rows",
]
