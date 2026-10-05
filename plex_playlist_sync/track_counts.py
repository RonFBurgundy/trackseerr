"""Shared parsing for provider-reported track counts."""

from __future__ import annotations

from typing import Any, Optional


def positive_int(value: Any) -> Optional[int]:
    """``value`` as a positive int, else None (providers report 0, missing or junk for an unknown track count)."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None
