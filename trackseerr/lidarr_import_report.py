"""Reconciliation report for a Lidarr import: what Lidarr had versus what TrackSeerr ended up with."""

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trackseerr import __version__
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

MAX_SKIPPED_ITEMS = 1000
KEEP_REPORTS = 5
REPORT_PREFIX = "lidarr-import-"

_COUNTER_KEYS = (
    "albums_monitored",
    "albums_unmonitored",
    "tracks",
    "tracks_monitored",
    "files_linked",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class ImportReport:
    """Collects per-artist counters and skipped items during one import run (single worker thread)."""

    def __init__(self) -> None:
        self.active = False  # set by the job once a run actually starts
        self._artists: dict[Any, dict[str, Any]] = {}
        self._counters: dict[Any, Counter] = {}
        self._trackfile_artist: dict[Any, Any] = {}
        self._artist_map: dict[Any, str] = {}
        self._skipped: list[dict[str, Any]] = []
        self._skip_counts: Counter = Counter()

    def add_artist(self, lidarr_artist: dict[str, Any], trackseerr_artist_id: Optional[str] = None) -> None:
        """Records an artist with Lidarr's own statistics (missing keys count as 0)."""
        lidarr_id = lidarr_artist.get("id")
        stats = lidarr_artist.get("statistics") or {}
        self._artists[lidarr_id] = {
            "lidarr_id": lidarr_id,
            "name": (lidarr_artist.get("artistName") or "Unknown Artist").strip(),
            "monitored": bool(lidarr_artist.get("monitored", True)),
            "lidarr_track_count": _int(stats.get("trackCount")),
            "lidarr_file_count": _int(stats.get("trackFileCount")),
            "lidarr_album_count": _int(stats.get("albumCount")),
        }
        self._counters.setdefault(lidarr_id, Counter())
        if trackseerr_artist_id:
            self._artist_map[lidarr_id] = trackseerr_artist_id

    def album(self, lidarr_artist_id: Any, monitored: bool) -> None:
        self._counters.setdefault(lidarr_artist_id, Counter())["albums_monitored" if monitored else "albums_unmonitored"] += 1

    def track(self, lidarr_artist_id: Any, monitored: bool, lidarr_trackfile_id: Any = None) -> None:
        counter = self._counters.setdefault(lidarr_artist_id, Counter())
        counter["tracks"] += 1
        if monitored:
            counter["tracks_monitored"] += 1
        if lidarr_trackfile_id:
            self._trackfile_artist[lidarr_trackfile_id] = lidarr_artist_id

    def file_linked(self, lidarr_trackfile_id: Any) -> None:
        artist_id = self._trackfile_artist.get(lidarr_trackfile_id)
        if artist_id is not None:
            self._counters.setdefault(artist_id, Counter())["files_linked"] += 1

    def skip(self, kind: str, reason: str, detail: dict[str, Any]) -> None:
        """Records a skipped item; the item list is capped but the per-reason totals are not."""
        self._skip_counts[reason] += 1
        if len(self._skipped) < MAX_SKIPPED_ITEMS:
            self._skipped.append({"kind": kind, "reason": reason, **detail})

    def _missing_by_artist(self, db: Database) -> dict[str, int]:
        """Monitored tracks on monitored albums without a file, grouped by TrackSeerr artist id."""
        try:
            rows = db.conn.execute(
                "SELECT t.artist_id, COUNT(*) FROM library_tracks t JOIN library_albums al ON al.id = t.album_id "
                "WHERE t.monitored = 1 AND al.monitored = 1 "
                "AND NOT EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) GROUP BY t.artist_id"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("LidarrImportReport: could not compute per-artist missing counts: %s", safe_exc(exc))
            return {}
        return {str(row[0]): int(row[1]) for row in rows}

    def _artist_rows(self, db: Database) -> list[dict[str, Any]]:
        missing_by_artist = self._missing_by_artist(db)
        rows: list[dict[str, Any]] = []
        for lidarr_id, info in self._artists.items():
            counter = self._counters.get(lidarr_id, Counter())
            lidarr_missing = max(info["lidarr_track_count"] - info["lidarr_file_count"], 0)
            ts_missing = missing_by_artist.get(self._artist_map.get(lidarr_id, ""), 0)
            rows.append(
                {
                    "lidarr_id": lidarr_id,
                    "name": info["name"],
                    "monitored": info["monitored"],
                    "lidarr_track_count": info["lidarr_track_count"],
                    "lidarr_file_count": info["lidarr_file_count"],
                    "lidarr_missing": lidarr_missing,
                    "imported_albums_monitored": counter["albums_monitored"],
                    "imported_albums_unmonitored": counter["albums_unmonitored"],
                    "imported_tracks": counter["tracks"],
                    "imported_tracks_monitored": counter["tracks_monitored"],
                    "files_linked": counter["files_linked"],
                    "trackseerr_missing": ts_missing,
                    "delta": ts_missing - lidarr_missing,
                }
            )
        rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
        return rows

    def _totals(self, db: Database) -> dict[str, Any]:
        infos = list(self._artists.values())
        stats = db.get_library_stats()
        return {
            "lidarr": {
                "artists": len(infos),
                "monitored_artists": sum(1 for i in infos if i["monitored"]),
                "albums": sum(i["lidarr_album_count"] for i in infos),
                "tracks": sum(i["lidarr_track_count"] for i in infos),
                "files": sum(i["lidarr_file_count"] for i in infos),
                "missing": sum(max(i["lidarr_track_count"] - i["lidarr_file_count"], 0) for i in infos),
            },
            "trackseerr": {
                key: _int(stats.get(key))
                for key in ("artist_count", "album_count", "track_count", "track_file_count", "missing_track_count")
            },
        }

    def finalize(
        self,
        db: Database,
        status: str,
        error: Optional[str],
        started_at: Optional[str],
        auto_switch: bool,
    ) -> dict[str, Any]:
        """Builds the JSON-serialisable report. Never includes the Lidarr API key or base URL."""
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at,
            "status": status,
            "error": error,
            "auto_switch": auto_switch,
            "trackseerr_version": __version__,
            "totals": self._totals(db),
            "artists": self._artist_rows(db),
            "skipped": {"counts": dict(self._skip_counts), "items": list(self._skipped)},
        }


def _reports_dir(config_dir: str) -> Path:
    return Path(config_dir) / "reports"


def write_report(report: dict[str, Any], config_dir: str) -> Optional[Path]:
    """Writes the report under ``<config_dir>/reports`` and prunes to the newest five; None when it cannot be written."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    directory = _reports_dir(config_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{REPORT_PREFIX}{stamp}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        for old in sorted(directory.glob(f"{REPORT_PREFIX}*.json"), key=lambda p: p.name)[:-KEEP_REPORTS]:
            old.unlink(missing_ok=True)
        return path
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("LidarrImportReport: could not write the report: %s", safe_exc(exc))
        return None


def latest_report_path(config_dir: str) -> Optional[Path]:
    """The newest report file, or None."""
    try:
        files = sorted(_reports_dir(config_dir).glob(f"{REPORT_PREFIX}*.json"), key=lambda p: p.name)
    except OSError as exc:
        logger.warning("LidarrImportReport: could not list reports: %s", safe_exc(exc))
        return None
    return files[-1] if files else None
