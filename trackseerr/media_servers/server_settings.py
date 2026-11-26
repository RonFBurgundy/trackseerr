"""Validation, secret masking, and persistence for multi-server media-server settings."""

from __future__ import annotations

from typing import Any, Optional

from trackseerr.config import (
    MEDIA_SERVER_JELLYFIN,
    MEDIA_SERVER_PLEX,
    MEDIA_SERVER_SOURCE_ENV,
    MEDIA_SERVER_SOURCE_SETTINGS,
    MEDIA_SERVER_SUBSONIC,
    Config,
)
from trackseerr.media_server_store import MEDIA_SERVER_KINDS
from trackseerr.media_servers import capabilities_for, registry, settings
from trackseerr.media_servers.base import ConnectionTest
from trackseerr.security import MASK_MARKER, is_safe_service_url, mask_secret, resolve_masked_value

SECRET_FIELDS = ("password", "api_key")
LOCKED_EDITABLE = (
    "name",
    "enabled",
    "use_refresh",
    "use_playlists",
    "use_users",
    "use_scrobbles",
    "sort_order",
)
_LOCKED_IMMUTABLE_KEYS = ("kind", "url", "username", "password", "api_key", "music_section", "verify_ssl")


class ServerSettingsError(ValueError):
    """Invalid media-server configuration (mapped to HTTP 422)."""


class ServerLockedError(ServerSettingsError):
    """Attempted to modify or delete an environment-configured server (mapped to HTTP 409)."""


class ServerSecretReuseError(ServerSettingsError):
    """A masked secret was submitted with a changed host/username or for a new server (mapped to HTTP 400)."""


class ServerNotFoundError(ServerSettingsError):
    """Target media server does not exist (mapped to HTTP 404)."""


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def present(row: dict[str, Any], record: Optional[registry.ServerRecord] = None) -> dict[str, Any]:
    """Return a media-server row safe for API responses with secrets masked and capabilities attached."""
    use_record = row["source"] == MEDIA_SERVER_SOURCE_ENV and record is not None and record.id == row["id"]
    url = record.url if use_record and record is not None else row["url"]
    username = record.username if use_record and record is not None else row["username"]
    raw_password = record.password if use_record and record is not None else row.get("password")
    raw_api_key = record.api_key if use_record and record is not None else row.get("api_key")
    music_section = record.music_section if use_record and record is not None else row["music_section"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source": row["source"],
        "name": row["name"],
        "url": url,
        "username": username,
        "password": mask_secret(raw_password) if raw_password else "",
        "api_key": mask_secret(raw_api_key) if raw_api_key else "",
        "music_section": music_section,
        "verify_ssl": bool(row["verify_ssl"]),
        "enabled": bool(row["enabled"]),
        "use_refresh": bool(row["use_refresh"]),
        "use_playlists": bool(row["use_playlists"]),
        "use_users": bool(row["use_users"]),
        "use_scrobbles": bool(row["use_scrobbles"]),
        "is_login": bool(row["is_login"]),
        "sort_order": int(row["sort_order"]),
        "locked": row["source"] == MEDIA_SERVER_SOURCE_ENV,
        "capabilities": capabilities_for(row["kind"]).to_dict(),
    }


def resolve_secrets(incoming: dict[str, Any], stored: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Resolve masked secrets in ``incoming`` against ``stored``, rejecting unsafe reuse."""
    resolved = dict(incoming)
    if stored is None:
        for field in SECRET_FIELDS:
            val = resolved.get(field)
            if isinstance(val, str) and MASK_MARKER in val:
                raise ServerSecretReuseError("Masked placeholder submitted for a new server; enter the full value")
        return resolved

    used: list[str] = []
    for field in SECRET_FIELDS:
        submitted = resolved.get(field)
        stored_val = stored.get(field) or ""
        masked_stored = mask_secret(stored_val) if stored_val else ""
        if isinstance(submitted, str) and stored_val and submitted == masked_stored:
            used.append(field)
        try:
            kept = resolve_masked_value(submitted, stored_val, masked_stored, field=field)
        except ValueError as exc:
            raise ServerSecretReuseError(str(exc)) from exc
        resolved[field] = "" if kept is None else kept

    if used:
        if _norm_url(str(resolved.get("url") or "")) != _norm_url(str(stored.get("url") or "")):
            what = "password" if used == ["password"] else "API key" if used == ["api_key"] else "credentials"
            raise ServerSecretReuseError(
                f"The server URL changed: enter the {what} again instead of keeping the saved one"
            )
        if "password" in used and str(resolved.get("username") or "").strip() != str(stored.get("username") or "").strip():
            raise ServerSecretReuseError(
                "The username changed: enter the password again instead of keeping the saved one"
            )
    return resolved


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise media-server settings for any supported kind."""
    kind = str(values.get("kind") or "").strip().lower()
    if kind not in MEDIA_SERVER_KINDS:
        raise ServerSettingsError(f"Media server kind must be one of: {', '.join(MEDIA_SERVER_KINDS)}")

    url = str(values.get("url") or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ServerSettingsError("Server URL must start with http:// or https://")
    if not is_safe_service_url(url):
        raise ServerSettingsError("Prohibited or invalid host URL (SSRF defense)")

    name = str(values.get("name") or "").strip() or kind.title()
    username = str(values.get("username") or "").strip()
    password = str(values.get("password") or "")
    api_key = str(values.get("api_key") or "").strip()
    music_section = str(values.get("music_section") or "").strip()

    if kind == MEDIA_SERVER_JELLYFIN:
        if not api_key:
            raise ServerSettingsError("Enter the Jellyfin API key")
        password = ""
    elif kind == MEDIA_SERVER_PLEX:
        if not api_key:
            raise ServerSettingsError("Enter the Plex token")
        password = ""
    elif kind == MEDIA_SERVER_SUBSONIC:
        if not api_key and not (username and password):
            raise ServerSettingsError("Enter a username and password, or an API key")

    return {
        "kind": kind,
        "name": name,
        "url": url,
        "username": username,
        "password": password,
        "api_key": api_key,
        "music_section": music_section,
        "verify_ssl": bool(values.get("verify_ssl", True)),
        "enabled": bool(values.get("enabled", True)),
        "use_refresh": bool(values.get("use_refresh", True)),
        "use_playlists": bool(values.get("use_playlists", True)),
        "use_users": bool(values.get("use_users", True)),
        "use_scrobbles": bool(values.get("use_scrobbles", True)),
        "sort_order": int(values.get("sort_order") or 0),
    }


def _taken_names(db: Any, *, exclude_id: Optional[str] = None) -> set[str]:
    return {
        str(r.get("name") or "").strip().lower()
        for r in db.list_media_server_rows()
        if exclude_id is None or r["id"] != exclude_id
    }


def _next_available_name(db: Any, kind: str) -> str:
    taken = _taken_names(db)
    base = kind.title()
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} {n}".lower() in taken:
        n += 1
    return f"{base} {n}"


def _ensure_unique_name(db: Any, name: str, *, exclude_id: Optional[str] = None) -> None:
    if name.strip().lower() in _taken_names(db, exclude_id=exclude_id):
        raise ServerSettingsError("Another server already uses that name")


def _save_locked(
    db: Any,
    server_id: str,
    values: dict[str, Any],
    stored: dict[str, Any],
    record: Optional[registry.ServerRecord] = None,
) -> dict[str, Any]:
    presented = present(stored, record=record)
    for key in _LOCKED_IMMUTABLE_KEYS:
        if key not in values or values[key] is None:
            continue
        incoming_val = values[key]
        expected_val = presented[key]
        if key == "verify_ssl":
            changed = bool(incoming_val) != bool(expected_val)
        elif key == "kind":
            changed = str(incoming_val).strip().lower() != str(expected_val).strip().lower()
        elif key == "url":
            changed = _norm_url(str(incoming_val)) != _norm_url(str(expected_val))
        else:
            changed = str(incoming_val).strip() != str(expected_val).strip()
        if changed:
            raise ServerLockedError("This server is set by environment variables; change them there")

    updates = {key: values[key] for key in LOCKED_EDITABLE if key in values and values[key] is not None}
    if "name" in updates:
        cleaned_name = str(updates["name"]).strip()
        if not cleaned_name:
            raise ServerSettingsError("Media server name must not be blank")
        _ensure_unique_name(db, cleaned_name, exclude_id=server_id)
        updates["name"] = cleaned_name
    if not updates:
        return stored
    updated = db.update_media_server_row(server_id, updates)
    if updated is None:
        raise ServerNotFoundError(f"Media server '{server_id}' not found")
    return updated


def save(db: Any, config: Config, values: dict[str, Any]) -> dict[str, Any]:
    """Create or update a media server, reload the process registry, and notify listeners."""
    raw_id = values.get("id")
    server_id = str(raw_id).strip() if raw_id is not None else ""

    if server_id:
        stored = db.get_media_server_row(server_id)
        if stored is None:
            raise ServerNotFoundError(f"Media server '{server_id}' not found")
        if stored["source"] == MEDIA_SERVER_SOURCE_ENV:
            record = next((r for r in registry.records(config) if r.id == server_id), None)
            row = _save_locked(db, server_id, values, stored, record=record)
        else:
            incoming_kind = str(values.get("kind") or "").strip().lower()
            if incoming_kind and incoming_kind != stored["kind"]:
                raise ServerSettingsError("Media server kind cannot be changed")
            resolved = resolve_secrets(values, stored)
            clean = validate({**resolved, "kind": stored["kind"]})
            _ensure_unique_name(db, clean["name"], exclude_id=server_id)
            updated = db.update_media_server_row(server_id, clean)
            if updated is None:
                raise ServerNotFoundError(f"Media server '{server_id}' not found")
            row = updated
    else:
        resolved = resolve_secrets(values, None)
        clean = validate(resolved)
        if str(values.get("name") or "").strip():
            _ensure_unique_name(db, clean["name"])
        else:
            clean["name"] = _next_available_name(db, clean["kind"])
        row = db.insert_media_server_row({**clean, "source": MEDIA_SERVER_SOURCE_SETTINGS})

    registry.reload(config, db)
    settings.notify_changed()
    record = next((r for r in registry.records(config) if r.id == row["id"]), None)
    return present(row, record=record)


def delete(db: Any, config: Config, server_id: str) -> None:
    """Delete a settings-configured media server, reload the registry, and notify listeners."""
    row = db.get_media_server_row(server_id)
    if row is None:
        raise ServerNotFoundError(f"Media server '{server_id}' not found")
    if row["source"] == MEDIA_SERVER_SOURCE_ENV:
        raise ServerLockedError("This server is set by environment variables; change them there")
    db.delete_media_server_row(server_id)
    registry.reload(config, db)
    settings.notify_changed()


def set_login(db: Any, config: Config, server_id: Optional[str]) -> None:
    """Select the unique Plex login server (or clear when ``server_id`` is None), reload, and notify."""
    try:
        db.set_login_media_server(server_id)
    except ValueError as exc:
        raise ServerSettingsError(str(exc)) from exc
    registry.reload(config, db)
    settings.notify_changed()


def test(db: Any, values: dict[str, Any], config: Optional[Config] = None) -> ConnectionTest:
    """Test a media-server connection using a throwaway adapter that is never cached."""
    raw_id = values.get("id")
    server_id = str(raw_id).strip() if raw_id is not None else ""
    stored = db.get_media_server_row(server_id) if server_id else None

    if stored is not None and stored["source"] == MEDIA_SERVER_SOURCE_ENV:
        record = next((r for r in registry.records(config) if r.id == server_id), None)
        if record is None:
            return ConnectionTest(ok=False, message=f"Media server '{server_id}' is not configured in the environment")
        label = record.kind
    else:
        resolved = resolve_secrets(values, stored)
        clean = validate(resolved)
        label = clean["kind"]
        record = registry.ServerRecord(
            id=server_id or "test",
            kind=clean["kind"],
            name=clean["name"],
            source=MEDIA_SERVER_SOURCE_SETTINGS,
            url=clean["url"],
            username=clean["username"],
            password=clean["password"],
            api_key=clean["api_key"],
            music_section=clean["music_section"],
            verify_ssl=clean["verify_ssl"],
        )
    adapter = registry._instantiate_adapter(record)
    if adapter is None:
        return ConnectionTest(ok=False, message=f"Failed to initialize {label} client")
    try:
        return adapter.test_connection()
    finally:
        close_fn = getattr(adapter, "close", None)
        if callable(close_fn):
            close_fn()
