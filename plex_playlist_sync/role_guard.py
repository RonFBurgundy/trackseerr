"""Startup guardrails for the two-tier (DMZ) roles.

``check_role_environment`` is a pure function over an env mapping and an injectable filesystem
probe, so every rule is unit-testable. It never reads or returns a secret value: problems name
variables, never their contents. See ``docs/design/dmz-ergonomics.md`` section 1.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol
from urllib.parse import urlsplit

from plex_playlist_sync.internal_auth import MIN_SECRET_LENGTH

logger = logging.getLogger(__name__)

README_HINT = 'See the README "Two-tier deployment" section.'

# Secrets and credentials that must never reach the internet-facing gateway.
GATEWAY_FORBIDDEN_ENV: tuple[str, ...] = (
    "PLEX_TOKEN",
    "SUBSONIC_PASSWORD",
    "SUBSONIC_API_KEY",
    "JELLYFIN_API_KEY",
    "LASTFM_API_SECRET",
    "LASTFM_API_KEY",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_CLIENT_ID",
    "LIDARR_API_KEY",
    "FEED_TOKEN",
    "ADMIN_PASSWORD",
)

# Download-client / indexer credentials supplied through the environment.
GATEWAY_FORBIDDEN_ENV_PATTERN = re.compile(
    r"^(QBIT|QBITTORRENT|SABNZBD|NZBGET|TRANSMISSION|DELUGE|SLSKD|PROWLARR|JACKETT|INDEXER|DOWNLOAD_CLIENT)_"
    r".*(PASSWORD|API_?KEY|TOKEN|SECRET|USERNAME)"
)

DB_FILENAME = "sync_db.sqlite"

# Problem codes (stable; tests and callers key off them).
CODE_FORBIDDEN_ENV = "forbidden_env"
CODE_DB_PRESENT = "db_present"
CODE_SECRET_WEAK = "secret_weak"
CODE_CORE_URL = "core_url_invalid"
CODE_APP_URL_MISSING = "application_url_missing"
CODE_APP_URL_SELF = "application_url_is_core"
CODE_BIND_ALL = "bind_all_interfaces"


@dataclass(frozen=True)
class Problem:
    """One guardrail finding. ``fatal`` problems stop startup; the rest are logged as warnings."""

    code: str
    message: str
    fatal: bool = True


class RoleFs(Protocol):
    def db_owner(self, directory: str) -> Optional[str]:
        """None when ``directory`` holds no TrackSeerr DB, else "gateway" (acceptable) or another value (refuse)."""

    def db_owner_file(self, path: str) -> Optional[str]:
        """As ``db_owner`` but for an explicit database file path."""


_SQLITE_MAGIC = b"SQLite format 3\x00"

# (table, column or None) pairs whose non-empty content proves a core / all-in-one database.
_ROW_TABLES = (
    "library_artists",
    "library_albums",
    "library_tracks",
    "download_clients",
    "indexers",
    "playlists",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_value(conn: sqlite3.Connection, table: str, column: str, extra: str = "") -> bool:
    if not _table_exists(conn, table) or column not in _columns(conn, table):
        return False
    sql = f"SELECT 1 FROM {table} WHERE TRIM(COALESCE({column}, '')) <> ''{extra} LIMIT 1"
    return conn.execute(sql).fetchone() is not None


def core_like_reasons(conn: sqlite3.Connection) -> list[str]:
    """Names (never values) of the core-only content found in an open database.

    Users and sessions alone are NOT core-like: a gateway upserts users on login.
    """
    reasons: list[str] = []
    for table in _ROW_TABLES:
        if _table_exists(conn, table) and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            reasons.append(f"{table} rows")
    checks = (
        ("lidarr_settings", "api_key", "a Lidarr API key"),
        ("media_server_settings", "password", "a media server password"),
        ("media_server_settings", "api_key", "a media server API key"),
        ("user_scrobble_configs", "lastfm_session_key", "per-user Last.fm credentials"),
        ("user_scrobble_configs", "listenbrainz_token", "per-user ListenBrainz credentials"),
        ("users", "password_hash", "local accounts with passwords"),
        ("general_settings", "lastfm_api_key", "a Last.fm API key"),
        ("general_settings", "lastfm_api_secret", "a Last.fm API secret"),
        ("general_settings", "plex_webhook_secret", "a Plex webhook secret"),
    )
    for table, column, label in checks:
        try:
            if _has_value(conn, table, column):
                reasons.append(label)
        except sqlite3.Error as exc:
            logger.warning("Could not inspect %s.%s while checking the gateway database: %s", table, column, type(exc).__name__)
    return reasons


class RealFs:
    """Looks for a TrackSeerr database on the real filesystem, read-only, and classifies it by content.

    Returns "gateway" for a database the gateway may keep using (its own, or one with no core-only content)
    and "core" for one that looks like a core / all-in-one database.
    """

    def db_owner(self, directory: str) -> Optional[str]:
        return self.db_owner_file(os.path.join(directory, DB_FILENAME))

    def db_owner_file(self, path: str) -> Optional[str]:
        if not os.path.isfile(path):
            return None
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                header = fh.read(len(_SQLITE_MAGIC))
        except OSError as exc:
            logger.warning("Could not read %s while checking the gateway database: %s", path, type(exc).__name__)
            return "gateway"
        if size == 0:
            logger.warning("Empty database file at %s; treating it as not core-like", path)
            return "gateway"
        if header != _SQLITE_MAGIC:
            logger.warning("%s is non-empty but not a SQLite database; treating it as core-like", path)
            return "core"
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error as exc:
            logger.warning("Could not open %s read-only: %s; treating it as not core-like", path, type(exc).__name__)
            return "gateway"
        try:
            try:
                row = conn.execute("SELECT last_role FROM general_settings WHERE id = 1").fetchone()
                if row and str(row[0] or "") == "gateway":
                    return "gateway"
            except sqlite3.Error:
                pass  # pre-v30: no last_role column; fall through to the content check
            try:
                reasons = core_like_reasons(conn)
            except sqlite3.Error as exc:
                logger.warning("Could not inspect %s: %s; treating it as not core-like", path, type(exc).__name__)
                return "gateway"
            if reasons:
                logger.warning("%s looks like a core database (%s)", path, ", ".join(reasons))
                return "core"
            return "gateway"
        finally:
            conn.close()


def _is_set(env: Mapping[str, str], name: str) -> bool:
    return bool(str(env.get(name, "") or "").strip())


def forbidden_env_names(env: Mapping[str, str]) -> list[str]:
    """Names (never values) of gateway-forbidden variables that are set to a non-empty value."""
    found = [n for n in GATEWAY_FORBIDDEN_ENV if _is_set(env, n)]
    found += [n for n in sorted(env) if GATEWAY_FORBIDDEN_ENV_PATTERN.match(n) and _is_set(env, n) and n not in found]
    return found


def _data_dirs(env: Mapping[str, str]) -> list[str]:
    dirs = [env.get("CONFIG_DIR", ""), "/config", env.get("DATA_DIR", ""), "/data"]
    seen: list[str] = []
    for d in dirs:
        d = str(d or "").strip()
        if d and d not in seen:
            seen.append(d)
    return seen


def _url_host_port(url: str) -> tuple[str, Optional[int]]:
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").lower(), parts.port
    except ValueError:
        return "", None


def check_role_environment(
    role: str,
    env: Mapping[str, str],
    fs: Optional[RoleFs] = None,
) -> list[Problem]:
    """Returns every guardrail problem for ``role`` given ``env``. Pure apart from ``fs`` probes."""
    role = (role or "all-in-one").lower().strip()
    fs = fs if fs is not None else RealFs()
    problems: list[Problem] = []

    if role not in ("gateway", "core"):
        return problems  # all-in-one: no new checks

    secret = str(env.get("INTERNAL_CORE_SECRET", "") or "").strip()
    if len(secret) < MIN_SECRET_LENGTH:
        problems.append(
            Problem(
                CODE_SECRET_WEAK,
                f"INTERNAL_CORE_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters "
                "(generate one with: openssl rand -hex 32)",
            )
        )

    if role == "gateway":
        names = forbidden_env_names(env)
        if names:
            problems.append(
                Problem(
                    CODE_FORBIDDEN_ENV,
                    "ROLE=gateway must not hold secrets, but these are set: " + ", ".join(names)
                    + " (remove them from the gateway container)",
                )
            )
        scanned: set[str] = set()
        targets: list[tuple[str, str, bool]] = []
        for directory in _data_dirs(env):
            targets.append((directory, os.path.realpath(os.path.join(directory, DB_FILENAME)), False))
        db_env = str(env.get("DATABASE_PATH", "") or "").strip()
        if db_env and db_env != ":memory:":
            targets.append((db_env, os.path.realpath(db_env), True))
        for label, real, is_file in targets:
            if real in scanned:
                continue
            scanned.add(real)
            owner = fs.db_owner_file(db_env) if is_file else fs.db_owner(label)
            if owner is not None and owner != "gateway":
                problems.append(
                    Problem(
                        CODE_DB_PRESENT,
                        f"ROLE=gateway found a core-like TrackSeerr database at {label}. Remove the /config volume "
                        "from the Requests container; the gateway must not share Core's database",
                    )
                )
        core_url = str(env.get("TRACKSEERR_CORE_URL", "") or "").strip()
        scheme = urlsplit(core_url).scheme.lower() if core_url else ""
        if scheme not in ("http", "https") or not _url_host_port(core_url)[0]:
            problems.append(
                Problem(CODE_CORE_URL, "TRACKSEERR_CORE_URL is missing or not an http(s):// URL")
            )
        if not (_is_set(env, "APPLICATION_URL") or _is_set(env, "APP_URL")):
            problems.append(Problem(CODE_APP_URL_MISSING, "APPLICATION_URL (the public URL) is required"))
        return problems

    # role == "core": warnings only (the weak-secret refusal above already applies)
    app_url = str(env.get("APPLICATION_URL", "") or env.get("APP_URL", "") or "").strip()
    host = str(env.get("HOST", "") or "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(str(env.get("PORT", "") or "5250").strip())
    except ValueError:
        port = 5250
    if app_url:
        a_host, a_port = _url_host_port(app_url)
        if a_port is None:
            a_port = 443 if urlsplit(app_url).scheme.lower() == "https" else 80
        own_hosts = {"localhost", "127.0.0.1", "::1", host.lower()} - {"0.0.0.0", "::"}
        if a_host in own_hosts and a_port == port:
            problems.append(
                Problem(
                    CODE_APP_URL_SELF,
                    "APPLICATION_URL points at core's own listen address/port; it should be the public "
                    "gateway URL. Core must not be exposed publicly.",
                    fatal=False,
                )
            )
    if host in ("0.0.0.0", "::") and not _is_set(env, "CORE_LAN_BIND"):
        problems.append(
            Problem(
                CODE_BIND_ALL,
                "core binds 0.0.0.0 with no CORE_LAN_BIND hint; publish its port on a LAN or loopback "
                "address only (set CORE_LAN_BIND), never to the internet",
                fatal=False,
            )
        )
    return problems
