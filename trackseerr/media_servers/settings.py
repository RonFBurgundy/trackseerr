"""Media-server choice saved on the Settings page.

Plex stays environment-only (its token also feeds the gateway guard and Plex OAuth flows); Subsonic and Jellyfin can be set by
environment or saved here. The environment always wins: when it configures a media server the saved values are kept
but ignored, and the UI shows the field as locked. Saved values reach the running process through
``config.set_media_server_overlay`` (applied by ``Config.from_env``); registered listeners (the background workers'
client holder) are told to reconnect after every save.
"""

import logging
import threading
from typing import Any, Callable

from trackseerr.config import (
    MEDIA_SERVER_JELLYFIN,
    MEDIA_SERVER_NONE,
    MEDIA_SERVER_SUBSONIC,
    Config,
    set_media_server_overlay,
)

logger = logging.getLogger(__name__)

SECRET_PLACEHOLDER = "********"
SECRET_FIELDS = ("password", "api_key")
_SECRET_FIELDS_BY_TYPE = {MEDIA_SERVER_SUBSONIC: ("password", "api_key"), MEDIA_SERVER_JELLYFIN: ("api_key",)}
SETTABLE_TYPES = (MEDIA_SERVER_SUBSONIC, MEDIA_SERVER_JELLYFIN, MEDIA_SERVER_NONE)  # Plex is environment-only

_listeners: list[Callable[[], None]] = []
_listeners_lock = threading.Lock()


class MediaServerSettingsError(ValueError):
    """The submitted media-server settings are not acceptable (rendered as a 422 with this message)."""


class SecretReuseError(MediaServerSettingsError):
    """A masked (saved) secret was submitted for a different server than the one it was saved for (rendered as 400)."""


def on_change(callback: Callable[[], None]) -> None:
    with _listeners_lock:
        _listeners.append(callback)


def clear_listeners() -> None:
    with _listeners_lock:
        _listeners.clear()


def _notify() -> None:
    with _listeners_lock:
        callbacks = list(_listeners)
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - one listener failing must not undo a saved setting; cause is logged
            logger.error("Media-server change listener failed: %s", type(exc).__name__)
            logger.debug("Media-server listener traceback", exc_info=True)


def load_into_process(db: Any) -> None:
    """Install the saved values (core startup). A database error leaves the environment-only behaviour in place."""
    try:
        stored = db.get_media_server_settings()
    except Exception as exc:  # noqa: BLE001 - sqlite3.Error and a closed handle; startup must not depend on this
        logger.warning("Could not load the saved media-server settings: %s", type(exc).__name__)
        return
    set_media_server_overlay(stored)


def present(config: Config, db: Any) -> dict[str, Any]:
    """The Settings-page view: saved values with secrets masked, plus what is effectively active and why."""
    stored = db.get_media_server_settings()
    return {
        "type": stored["type"] or (config.media_server_type if config.media_server_env_controlled else MEDIA_SERVER_NONE),
        "url": stored["url"],
        "username": stored["username"],
        "password": SECRET_PLACEHOLDER if stored["password"] else "",
        "api_key": SECRET_PLACEHOLDER if stored["api_key"] else "",
        "effective_type": config.media_server_type,
        "locked_by_env": config.media_server_env_controlled,
    }


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _credentials_owner(stored: dict[str, str]) -> str:
    """The server type the saved credentials belong to (``type`` is only the active choice and can be ``none``)."""
    return (stored.get("credentials_type") or stored.get("type") or "").strip().lower()


def merge_secrets(incoming: dict[str, str], stored: dict[str, str]) -> dict[str, str]:
    """``incoming`` with each ``********`` secret replaced by the stored value (empty when there is none).

    A saved secret is only ever resolved for the server it was saved for, so it can never be sent to another host or
    be reinterpreted as another server type's credential. A masked secret submitted with a different server type, a
    different URL or (for a Subsonic password) a different username raises :class:`SecretReuseError`.
    """
    merged = dict(incoming)
    kind = (merged.get("type") or "").strip().lower()
    used = [
        f for f in _SECRET_FIELDS_BY_TYPE.get(kind, ()) if merged.get(f) == SECRET_PLACEHOLDER and stored.get(f)
    ]
    if used:
        if _credentials_owner(stored) != kind:
            raise SecretReuseError("The saved credentials belong to a different media server type: enter them again")
        if _norm_url(merged.get("url", "")) != _norm_url(stored.get("url", "")):
            raise SecretReuseError(
                "The server URL changed: enter the "
                + ("password" if used == ["password"] else "API key" if used == ["api_key"] else "credentials")
                + " again instead of keeping the saved one"
            )
        if "password" in used and (merged.get("username") or "").strip() != (stored.get("username") or "").strip():
            raise SecretReuseError("The username changed: enter the password again instead of keeping the saved one")
    for key in SECRET_FIELDS:
        if merged.get(key) == SECRET_PLACEHOLDER:
            merged[key] = stored.get(key, "")
    return merged


def validate(values: dict[str, str]) -> dict[str, str]:
    """Normalised values or ``MediaServerSettingsError``. Only complete Subsonic settings are accepted."""
    kind = (values.get("type") or "").strip().lower()
    if kind == "plex":
        raise MediaServerSettingsError("Plex is configured with PLEX_URL / PLEX_TOKEN in the environment")
    if kind not in SETTABLE_TYPES:
        raise MediaServerSettingsError(f"Media server type must be one of: {', '.join(SETTABLE_TYPES)}")
    if kind == MEDIA_SERVER_NONE:
        return {"type": kind, "url": "", "username": "", "password": "", "api_key": ""}
    url = (values.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise MediaServerSettingsError("Server URL must start with http:// or https://")
    if kind == MEDIA_SERVER_JELLYFIN:  # ``username`` is the optional default account; there is no password
        api_key = (values.get("api_key") or "").strip()
        if not api_key:
            raise MediaServerSettingsError("Enter the Jellyfin API key")
        return {"type": kind, "url": url, "username": (values.get("username") or "").strip(), "password": "", "api_key": api_key}
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    api_key = (values.get("api_key") or "").strip()
    if not api_key and not (username and password):
        raise MediaServerSettingsError("Enter a username and password, or an API key")
    return {"type": kind, "url": url, "username": username, "password": password, "api_key": api_key}


def save(db: Any, incoming: dict[str, str]) -> dict[str, str]:
    """Validate, persist and activate ``incoming``; returns the stored (unmasked) values."""
    stored = db.get_media_server_settings()
    merged = merge_secrets(incoming, stored)
    clean = validate(merged)
    if clean["type"] == MEDIA_SERVER_NONE:
        # Turning the server off only changes which one is active: what was saved for the previous type stays.
        owner = _credentials_owner(stored)
        keep = owner in _SECRET_FIELDS_BY_TYPE
        clean = {
            "type": MEDIA_SERVER_NONE,
            "credentials_type": owner if keep else "",
            **{k: (stored[k] if keep else "") for k in ("url", "username", "password", "api_key")},
        }
    else:
        clean = {**clean, "credentials_type": clean["type"]}
    saved = db.save_media_server_settings(clean)
    set_media_server_overlay(saved)
    _notify()
    return saved
