"""Media-server change listeners and Plex account token loader.

Configured media servers live in the ``media_servers`` table (managed through
``trackseerr.media_servers.server_settings``); this module only carries the process-wide change
listeners and loads the saved Plex owner account token at startup.
"""

import logging
import threading
from typing import Any, Callable

from trackseerr.config import set_plex_account_token

logger = logging.getLogger(__name__)

_listeners: list[Callable[[], None]] = []
_listeners_lock = threading.Lock()


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


def notify_changed() -> None:
    """Notify registered media-server change listeners."""
    _notify()


def load_into_process(db: Any) -> None:
    """Install the saved Plex owner account token at startup. A database error leaves it unset."""
    try:
        plex_account = db.get_plex_account()
    except Exception as exc:  # noqa: BLE001 - sqlite3.Error and a closed handle; startup must not depend on this
        logger.warning("Could not load the saved Plex account token: %s", type(exc).__name__)
        return
    set_plex_account_token(str((plex_account or {}).get("token") or ""))
