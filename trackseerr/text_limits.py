"""Bounded input helper for regex and text processing. Protects against ReDoS."""
from __future__ import annotations

MAX_TITLE_CHARS = 512


def cap(text: str | None, limit: int = MAX_TITLE_CHARS) -> str:
    """Bounds regex input against catastrophic backtracking."""
    return (text or "")[:limit]
