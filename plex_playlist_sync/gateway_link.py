"""Gateway-side link to core: startup handshake and periodic heartbeat; core-side heartbeat store.

See ``docs/dmz-ergonomics.md`` section 2. Nothing here logs or sends the shared secret; every call is
signed by ``CoreClient`` as the service principal.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from plex_playlist_sync import __version__
from plex_playlist_sync.clients.core_client import CoreClient
from plex_playlist_sync.internal_auth import PROTOCOL_VERSION
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)

HANDSHAKE_DEADLINE_SECONDS = 60.0
HANDSHAKE_RETRY_SECONDS = 30.0
HEARTBEAT_INTERVAL_SECONDS = 60.0
STALE_AFTER_SECONDS = 180.0
GATEWAY_STATUS_KEY = "gateway_status"

HANDSHAKE_OK = "ok"
HANDSHAKE_UNREACHABLE = "unreachable"


class ProtocolMismatch(Exception):
    """The gateway and core speak different internal protocol versions; the gateway must not start."""


def protocol_mismatch_message(core_protocol: Any) -> str:
    return (
        f"Gateway protocol {PROTOCOL_VERSION} != core protocol {core_protocol} "
        "— run the same TrackSeerr version on both containers"
    )


@dataclass
class HandshakeResult:
    state: str  # HANDSHAKE_OK | HANDSHAKE_UNREACHABLE
    core_version: Optional[str] = None


def _try_hello(client: CoreClient) -> tuple[Optional[dict[str, Any]], str]:
    """One hello attempt. Returns (body, reason); body is None on any failure."""
    try:
        status, body = client.hello()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return None, f"core unreachable: {safe_exc(exc)}"
    if status == 404:
        return None, (
            "core refused the handshake (wrong INTERNAL_CORE_SECRET, or core is older than this gateway)"
        )
    if status != 200 or "protocol" not in body:
        return None, f"core answered the handshake with HTTP {status}"
    return body, ""


def _evaluate_hello(body: dict[str, Any]) -> HandshakeResult:
    core_protocol = body.get("protocol")
    if core_protocol != PROTOCOL_VERSION:
        raise ProtocolMismatch(protocol_mismatch_message(core_protocol))
    core_version = str(body.get("version") or "")
    if core_version and core_version != __version__:
        logger.warning(
            "Gateway version %s differs from core version %s (same protocol, continuing); "
            "run the same TrackSeerr version on both containers.",
            __version__,
            core_version,
        )
    return HandshakeResult(HANDSHAKE_OK, core_version or None)


def perform_handshake(
    client: CoreClient,
    *,
    deadline_seconds: float = HANDSHAKE_DEADLINE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] = lambda: False,
) -> HandshakeResult:
    """Calls core ``hello`` with exponential backoff (1s, 2s, 4s ... capped at 10s) for up to ``deadline_seconds``.

    Raises :class:`ProtocolMismatch` on a protocol difference. Returns ``unreachable`` when core never
    answered in time (the gateway then starts and fails closed).
    """
    start = monotonic()
    delay = 1.0
    last_reason = ""
    while True:
        body, reason = _try_hello(client)
        if body is not None:
            return _evaluate_hello(body)
        last_reason = reason
        elapsed = monotonic() - start
        if elapsed >= deadline_seconds or should_stop():
            break
        logger.info("Waiting for core (%s); retrying in %.0fs", reason, delay)
        sleep(min(delay, max(0.0, deadline_seconds - elapsed)))
        delay = min(delay * 2, 10.0)
    logger.error(
        "Core did not complete the handshake within %.0fs (%s); starting anyway, failing closed (503) "
        "until it is reachable.",
        deadline_seconds,
        last_reason,
    )
    return HandshakeResult(HANDSHAKE_UNREACHABLE)


class GatewayLinkWorker:
    """Heartbeat thread: re-tries the handshake every 30 s until it succeeds, then beats every 60 s."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.gateway_id = secrets.token_hex(8)
        self.started_at = time.time()
        self.handshaken = False

    def start(
        self,
        client: CoreClient,
        session_counter: Callable[[], int],
        *,
        handshaken: bool,
        interval: float = HEARTBEAT_INTERVAL_SECONDS,
        retry_interval: float = HANDSHAKE_RETRY_SECONDS,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.handshaken = handshaken
        self._thread = threading.Thread(
            target=self._run,
            args=(client, session_counter, interval, retry_interval),
            daemon=True,
            name="GatewayLinkWorker",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def payload(self, active_sessions: int) -> dict[str, Any]:
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "gateway_id": self.gateway_id,
            "started_at": self.started_at,
            "active_sessions": max(0, int(active_sessions)),
        }

    def tick(self, client: CoreClient, session_counter: Callable[[], int]) -> float:
        """One iteration; returns how long to wait before the next one (kept separate for testing)."""
        if not self.handshaken:
            body, reason = _try_hello(client)
            if body is None:
                logger.warning("Core handshake still failing: %s", reason)
                return -1.0
            try:
                _evaluate_hello(body)
            except ProtocolMismatch as exc:
                logger.error("%s", exc)
                return -1.0
            self.handshaken = True
            logger.info("Core handshake succeeded")
        try:
            status = client.heartbeat(self.payload(session_counter()))
            if status != 200:
                logger.warning("Core rejected the gateway heartbeat (HTTP %s)", status)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            logger.warning("Gateway heartbeat failed: %s", safe_exc(exc))
        return 0.0

    def _run(
        self,
        client: CoreClient,
        session_counter: Callable[[], int],
        interval: float,
        retry_interval: float,
    ) -> None:
        logger.info("Gateway link worker started (heartbeat every %.0fs)", interval)
        while not self._stop.is_set():
            try:
                outcome = self.tick(client, session_counter)
            except Exception as exc:  # worker must survive any single failure; cause is logged
                logger.error("Gateway link worker error: %s", safe_exc(exc))
                logger.debug("Gateway link traceback", exc_info=True)
                outcome = -1.0
            self._stop.wait(retry_interval if outcome < 0 else interval)


gateway_link_worker = GatewayLinkWorker()


# ---------------------------------------------------------------------------
# Core side: heartbeat store (memory + durable kv row) and status computation
# ---------------------------------------------------------------------------

_memory: "weakref.WeakKeyDictionary[Any, dict[str, Any]]" = weakref.WeakKeyDictionary()
_memory_lock = threading.Lock()


def clear_memory() -> None:
    with _memory_lock:
        _memory.clear()


def record_heartbeat(db: Any, payload: dict[str, Any], now: Optional[float] = None) -> None:
    record = {
        "received_at": float(time.time() if now is None else now),
        "version": str(payload.get("version") or ""),
        "protocol": int(payload.get("protocol")),
        "gateway_id": str(payload.get("gateway_id") or ""),
        "started_at": payload.get("started_at"),
        "active_sessions": int(payload.get("active_sessions") or 0),
    }
    with _memory_lock:
        _memory[db] = record
    db.set_kv(GATEWAY_STATUS_KEY, json.dumps(record))


def _load_record(db: Any) -> Optional[dict[str, Any]]:
    with _memory_lock:
        cached = _memory.get(db)
    if cached is not None:
        return cached
    raw = db.get_kv(GATEWAY_STATUS_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "received_at" in data else None


def compute_gateway_status(
    db: Any,
    *,
    role: str,
    public_url: str,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """The exact payload of ``GET /api/admin/gateway-status``."""
    url = public_url or None
    if role != "core":
        return {
            "configured": False,
            "public_url": url,
            "last_seen_at": None,
            "version": None,
            "protocol": None,
            "version_match": None,
            "active_sessions": None,
            "state": "not_used",
        }
    record = _load_record(db)
    if record is None:
        return {
            "configured": bool(url),
            "public_url": url,
            "last_seen_at": None,
            "version": None,
            "protocol": None,
            "version_match": None,
            "active_sessions": None,
            "state": "never_seen",
        }
    current = time.time() if now is None else now
    seen = float(record["received_at"])
    return {
        "configured": bool(url),
        "public_url": url,
        "last_seen_at": datetime.fromtimestamp(seen, tz=timezone.utc).isoformat(),
        "version": record.get("version") or None,
        "protocol": record.get("protocol"),
        "version_match": record.get("version") == __version__ and record.get("protocol") == PROTOCOL_VERSION,
        "active_sessions": record.get("active_sessions"),
        "state": "stale" if current - seen > STALE_AFTER_SECONDS else "online",
    }
