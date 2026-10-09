"""Process boot progress: a thread-safe readiness state plus timed ``[boot] step:`` log lines.

The CLI marks the process *starting* before the HTTP server binds and *ready* once the work that
gates serving (for a gateway: the core protocol handshake) has finished. The API layer reads the
state to answer ``/api/health`` and to return 503 "starting" on every other ``/api`` route.

A bare ``create_app()`` (tests, ``uvicorn trackseerr.api.app:app``) never calls
:meth:`BootState.begin`, so the default state is *ready* and nothing changes for those callers.

Step names are fixed literals chosen by this code base; they are exposed unauthenticated through
the health endpoint, so never put hostnames, URLs, paths or identifiers in them.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("trackseerr.boot")

STATUS_STARTING = "starting"
STATUS_READY = "ok"


class BootState:
    """Tracks whether the process finished its gating startup work and what it is doing right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = True
        self._step = ""
        self._t0 = time.monotonic()

    def begin(self, step: str = "initializing") -> None:
        """Enters the *starting* state (the HTTP server may bind, but gated routes answer 503)."""
        with self._lock:
            self._ready = False
            self._step = step
            self._t0 = time.monotonic()

    def set_step(self, step: str) -> None:
        with self._lock:
            self._step = step

    def mark_ready(self) -> None:
        with self._lock:
            self._ready = True
            self._step = ""

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def step(self) -> str:
        with self._lock:
            return self._step

    def elapsed(self) -> float:
        with self._lock:
            return time.monotonic() - self._t0

    def snapshot(self, *, detailed: bool = True) -> dict[str, object]:
        """Public, non-sensitive view: ``{"status": "starting", "step": ...}`` or ``{"status": "ok"}``.

        ``detailed=False`` (the internet-facing gateway tier) publishes the generic step ``"starting"``
        instead of internal step names, which would reveal the existence and state of a core behind it.
        """
        with self._lock:
            if self._ready:
                return {"status": STATUS_READY}
            return {"status": STATUS_STARTING, "step": self._step if detailed else STATUS_STARTING}

    @contextmanager
    def step_timer(self, name: str, *, publish: bool = True) -> Iterator[None]:
        """Logs ``[boot] step: <name>`` and, on exit, the elapsed time; ``publish`` also sets the public step."""
        if publish:
            self.set_step(name)
        logger.info("[boot] step: %s", name)
        started = time.monotonic()
        try:
            yield
        except BaseException:
            logger.warning("[boot] step: %s failed after %.2fs", name, time.monotonic() - started)
            raise
        logger.info("[boot] step: %s done in %.2fs", name, time.monotonic() - started)


boot_state = BootState()
