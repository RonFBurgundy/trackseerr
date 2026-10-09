"""The "missing tracks" CSV a playlist sync can leave behind, shared by every media-server adapter."""

import csv
import logging
import re
from pathlib import Path
from typing import Any, Iterable

from trackseerr.models import Track
from trackseerr.redaction import safe_exc
from trackseerr.security import safe_data_path

logger = logging.getLogger(__name__)

_UNSAFE_NAME = re.compile(r'[\\/*?:"<>|]')


def write_missing_csv(missing_tracks: Iterable[Track], playlist_name: str, data_dir: str = "/data") -> None:
    """Write missing tracks to ``<data_dir>/<playlist>.csv`` with spreadsheet formula-injection defense."""
    try:
        folder = Path(data_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        clean_name = _UNSAFE_NAME.sub("_", playlist_name).strip() or "missing_playlist"
        target = safe_data_path(f"{clean_name}.csv", base_dir=str(folder))

        def _clean(val: Any) -> str:
            text = str(val if val is not None else "")
            if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "|")):
                return f"'{text}"
            return text

        rows = list(missing_tracks)
        with open(target, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["title", "artist", "album", "url"])
            for t in rows:
                writer.writerow([_clean(t.title), _clean(t.artist), _clean(t.album), _clean(t.url)])
        logger.info("Wrote %d missing track(s) to %s", len(rows), target)
    except (OSError, ValueError) as e:
        logger.warning("Failed to write missing tracks CSV for '%s': %s", playlist_name, safe_exc(e))


def delete_missing_csv(playlist_name: str, data_dir: str = "/data") -> None:
    """Delete a previously written missing-tracks CSV once every track matches."""
    try:
        clean_name = _UNSAFE_NAME.sub("_", playlist_name).strip()
        if not clean_name:
            return
        target = safe_data_path(f"{clean_name}.csv", base_dir=str(data_dir))
        if target.exists():
            target.unlink()
            logger.info("Cleaned up obsolete missing CSV: %s", target)
    except (OSError, ValueError) as e:
        logger.debug("Could not delete missing CSV for '%s': %s", playlist_name, safe_exc(e))
