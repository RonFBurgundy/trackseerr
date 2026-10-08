"""In-memory record of background task executions for ``GET /api/system/queue``.

Every scheduled or manual run of a task wraps itself in :func:`track_job`. The tracker keeps the jobs that are
queued or running plus the 50 most recent finished ones, all behind one lock. Nothing is persisted.
"""

import functools
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Iterator, Optional, TypeVar

from plex_playlist_sync.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

RECENT_LIMIT = 50
STATES = ("queued", "running", "completed", "failed", "cancelled")


class JobHandle:
    """Lets the body of a ``track_job`` block attach a result message or mark itself cancelled."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.message: Optional[str] = None
        self.cancelled = False
        self.failed: Optional[str] = None  # set to a message when the run ended badly without raising
        self.discard = False  # the run turned out to be a no-op that should not appear in the queue view

    def update_message(self, message: str) -> None:
        self.message = message
        job_tracker.update_message(self.job_id, message)


class JobTracker:
    def __init__(self, recent_limit: int = RECENT_LIMIT) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._recent: Deque[dict[str, Any]] = deque(maxlen=recent_limit)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def update_message(self, job_id: str, message: Optional[str]) -> None:
        with self._lock:
            job = self._active.get(job_id)
            if job is not None:
                job["message"] = message

    def start(self, task_id: str, name: str, state: str = "running", message: Optional[str] = None) -> str:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = {
            "id": job_id,
            "task_id": task_id,
            "name": name,
            "state": state,
            "started_at": self._now() if state == "running" else None,
            "finished_at": None,
            "duration_ms": None,
            "message": message,
            "_t0": time.monotonic(),
        }
        with self._lock:
            self._active[job_id] = job
        return job_id

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._active.get(job_id)
            if job is not None and job["state"] == "queued":
                job["state"] = "running"
                job["started_at"] = self._now()
                job["_t0"] = time.monotonic()

    def finish(self, job_id: str, state: str, message: Optional[str] = None) -> None:
        if state not in ("completed", "failed", "cancelled"):
            raise ValueError(f"Invalid terminal job state: {state}")
        with self._lock:
            job = self._active.pop(job_id, None)
            if job is None:
                return
            job["state"] = state
            job["finished_at"] = self._now()
            job["duration_ms"] = int((time.monotonic() - job["_t0"]) * 1000)
            if message is not None:
                job["message"] = message
            self._recent.appendleft(job)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            active = [self._public(j) for j in self._active.values()]
            recent = [self._public(j) for j in self._recent]
        running = sorted((j for j in active if j["state"] == "running"), key=lambda j: j["started_at"] or "")
        queued = [j for j in active if j["state"] == "queued"]
        return {"running": running, "queued": queued, "recent": recent}

    def running_message(self, task_id: str) -> Optional[str]:
        """Message of the oldest running job of ``task_id`` (None when none runs or it has no message yet)."""
        with self._lock:
            jobs = [j for j in self._active.values() if j["task_id"] == task_id and j["state"] == "running"]
        jobs.sort(key=lambda j: j["started_at"] or "")
        return jobs[0]["message"] if jobs else None

    def record_completed(
        self, task_id: str, name: str, duration_ms: int, message: Optional[str] = None, state: str = "completed"
    ) -> None:
        """Adds an already-finished run (for loops that only know afterwards that a tick did real work)."""
        if state not in ("completed", "failed", "cancelled"):
            raise ValueError(f"Invalid terminal job state: {state}")
        finished = datetime.now(timezone.utc)
        started = finished.timestamp() - duration_ms / 1000.0
        job = {
            "id": f"job-{uuid.uuid4().hex[:12]}",
            "task_id": task_id,
            "name": name,
            "state": state,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "finished_at": finished.isoformat(),
            "duration_ms": int(duration_ms),
            "message": message,
        }
        with self._lock:
            self._recent.appendleft(job)

    def discard(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def clear(self) -> None:
        with self._lock:
            self._active.clear()
            self._recent.clear()


job_tracker = JobTracker()


@contextmanager
def track_job(task_id: str, name: str) -> Iterator[JobHandle]:
    """Records one execution. An exception marks the job failed (message is the redacted type) and propagates."""
    handle = JobHandle(job_tracker.start(task_id, name))
    try:
        yield handle
    except Exception as exc:
        job_tracker.finish(handle.job_id, "failed", safe_exc(exc))
        raise
    else:
        if handle.discard:
            job_tracker.discard(handle.job_id)
        elif handle.failed is not None:
            job_tracker.finish(handle.job_id, "failed", handle.failed)
        else:
            job_tracker.finish(handle.job_id, "cancelled" if handle.cancelled else "completed", handle.message)


def summarize_result(result: Any) -> Optional[str]:
    """Short ``key=value`` rendering of a worker's result dict (scalars only), for the job message."""
    if not isinstance(result, dict):
        return None
    parts = [
        f"{k}={redact_text(v) if isinstance(v, str) else v}"
        for k, v in result.items()
        if isinstance(v, (int, float, str)) and not isinstance(v, bool) and v != ""
    ]
    return ", ".join(parts)[:200] or None


F = TypeVar("F", bound=Callable[..., Any])


def tracked(task_id: str, name: str) -> Callable[[F], F]:
    """Decorator recording each call as a job. A result with ``status == "already_running"`` is not recorded."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with track_job(task_id, name) as handle:
                result = fn(*args, **kwargs)
                if isinstance(result, dict) and result.get("status") == "already_running":
                    handle.discard = True
                else:
                    handle.message = summarize_result(result)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator
