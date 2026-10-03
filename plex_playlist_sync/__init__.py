"""Plex Playlist Sync package."""

import tomllib as _tomllib
from importlib import metadata as _metadata
from pathlib import Path as _Path

_FALLBACK_VERSION = "1.0.0"


def _resolve_version() -> str:
    """Installed metadata first; else the checkout's pyproject.toml (the container runs from /app, not pip-installed)."""
    try:
        return _metadata.version("trackseerr")
    except _metadata.PackageNotFoundError:
        pass
    pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            version = _tomllib.load(fh).get("project", {}).get("version")
    except (OSError, _tomllib.TOMLDecodeError):
        version = None
    return str(version) if version else _FALLBACK_VERSION


__version__ = _resolve_version()
