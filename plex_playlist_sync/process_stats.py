"""Cheap resource stats for this process (no psutil): CPU %, resident memory, thread count and uptime.

Reads ``/proc/self/status`` and ``/proc/self/stat`` on Linux; every field is ``None`` where it cannot be read
(non-Linux, restricted /proc) instead of raising. CPU is the share of one core used since the previous sample.
"""

import logging
import os
import threading
import time
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

_PROC_STATUS = "/proc/self/status"
_PROC_STAT = "/proc/self/stat"
_PROC_UPTIME = "/proc/uptime"


class ResourceSnapshot(TypedDict):
    cpu_percent: Optional[float]
    rss_bytes: Optional[int]
    thread_count: Optional[int]
    uptime_seconds: Optional[float]


_lock = threading.Lock()
_import_monotonic = time.monotonic()


def _cpu_seconds() -> Optional[float]:
    try:
        times = os.times()
    except OSError as exc:
        logger.debug("os.times unavailable: %s", exc)
        return None
    return float(times.user + times.system)


# (monotonic time, cpu seconds) of the previous sample; seeded at import so the first call has a window.
_last_sample: tuple[float, Optional[float]] = (_import_monotonic, _cpu_seconds())


def _read_status() -> tuple[Optional[int], Optional[int]]:
    """(rss_bytes, thread_count) from /proc/self/status, each None when absent."""
    rss: Optional[int] = None
    threads: Optional[int] = None
    try:
        with open(_PROC_STATUS, encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        rss = int(parts[1]) * 1024  # reported in kB
                elif line.startswith("Threads:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        threads = int(parts[1])
    except OSError:
        return None, None
    return rss, threads


def _uptime_seconds() -> Optional[float]:
    """Seconds since the process started: /proc start time against system uptime; falls back to time since import."""
    try:
        with open(_PROC_STAT, encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        # comm (field 2) may contain spaces/parentheses: split after the last ')'. starttime is field 22 overall,
        # i.e. index 19 of what follows the comm field.
        fields = stat[stat.rindex(")") + 2 :].split()
        start_ticks = int(fields[19])
        with open(_PROC_UPTIME, encoding="ascii", errors="replace") as handle:
            system_uptime = float(handle.read().split()[0])
        return max(0.0, system_uptime - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, AttributeError):
        return max(0.0, time.monotonic() - _import_monotonic)


def sample() -> ResourceSnapshot:
    """One snapshot. ``cpu_percent`` covers the time since the previous call (any caller), 100 = one full core."""
    global _last_sample
    now = time.monotonic()
    cpu = _cpu_seconds()
    with _lock:
        prev_time, prev_cpu = _last_sample
        _last_sample = (now, cpu)
    cpu_percent: Optional[float] = None
    wall = now - prev_time
    if cpu is not None and prev_cpu is not None and wall > 0:
        cpu_percent = round(max(0.0, (cpu - prev_cpu) / wall * 100.0), 1)
    rss, threads = _read_status()
    uptime = _uptime_seconds()
    return {
        "cpu_percent": cpu_percent,
        "rss_bytes": rss,
        "thread_count": threads,
        "uptime_seconds": round(uptime, 1) if uptime is not None else None,
    }
