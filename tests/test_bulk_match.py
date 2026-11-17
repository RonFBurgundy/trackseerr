"""Tests for bulk MusicBrainz matching: sure matches apply automatically, the rest are stored for review."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.test_compilation_filing import _seed_album
from tests.test_library_api import _auth_headers, app_and_client, seeded_users, test_config, test_db  # noqa: F401
from trackseerr import match_suggestions
from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.api.dependencies import get_mbid_enricher
from trackseerr.match_suggestions import apply_stored_suggestions, auto_match_unlinked
from trackseerr.storage import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_bulk_match.db"))
    clear_negative_cache()
    yield database
    database.close()


def _seed(db: Database, tmp_path: Path, album_id: str = "alb-1", files: int = 2) -> None:
    _seed_album(db, tmp_path, album_id, "Hits", [{"num": n, "title": f"Song {n}"} for n in range(1, files + 1)])


def _cand(gid: str, confidence: int, mapped: int, album_id: str = "alb-1", files: int = 2) -> dict[str, Any]:
    return {
        "id": gid,
        "title": "Hits",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "2001-01-01",
        "artist_credit": None,
        "is_various": False,
        "target_artist": "Bob",
        "mapped": mapped,
        "track_count": files,
        "track_map": [
            {
                "track_id": f"{album_id}-{n}",
                "title": f"Song {n}",
                "has_file": True,
                "release_track_title": f"Song {n}",
                "release_track_number": n,
                "recording_id": f"rec-{gid}-{n}",
            }
            for n in range(1, files + 1)
        ],
        "confidence": confidence,
        "reasons": [],
    }


class _Stub:
    """Replaces suggest_matches / apply_match with canned per-album answers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, results: dict[str, Any], conflict: bool = False):
        self.results = results
        self.applied: list[tuple[str, str, dict[str, str]]] = []
        self.conflict = conflict
        monkeypatch.setattr(match_suggestions, "suggest_matches", self.suggest)
        monkeypatch.setattr(match_suggestions, "apply_match", self.apply)

    def suggest(self, db: Any, enricher: Any, album_id: str, limit: int = 5) -> dict[str, Any]:
        value = self.results[album_id]
        if isinstance(value, Exception):
            raise value
        if value == "down":
            return {"album": {}, "candidates": [], "unavailable": True}
        return {"album": {}, "candidates": value, "unavailable": False}

    def apply(self, db: Any, enricher: Any, album_id: str, group: dict[str, Any], recs: dict[str, str]):
        self.applied.append((album_id, group["id"], recs))
        return {"album_id": album_id, "merged": False, "conflict": self.conflict}


def test_auto_applies_sure_match(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    db.save_match_suggestion("alb-1", _cand("rg-a", 80, 2), 2, 2)
    stub = _Stub(monkeypatch, {"alb-1": [_cand("rg-a", 95, 2), _cand("rg-b", 70, 2)]})

    counts = auto_match_unlinked(db, object())

    assert counts["applied"] == 1 and counts["albums"] == 1
    assert stub.applied == [("alb-1", "rg-a", {"alb-1-1": "rec-rg-a-1", "alb-1-2": "rec-rg-a-2"})]
    assert db.get_match_suggestion("alb-1") is None


def test_auto_stores_unsure_low_confidence(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    stub = _Stub(monkeypatch, {"alb-1": [_cand("rg-a", 89, 2)]})

    counts = auto_match_unlinked(db, object())

    assert counts["suggested"] == 1 and counts["applied"] == 0 and stub.applied == []
    stored = db.get_match_suggestion("alb-1")
    assert stored is not None and stored["confidence"] == 89 and stored["candidate"]["id"] == "rg-a"
    assert stored["album_title"] == "Hits" and stored["track_count"] == 2 and stored["artist_name"]


def test_auto_stores_when_not_all_files_found(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path, files=3)
    stub = _Stub(monkeypatch, {"alb-1": [_cand("rg-a", 97, 2, files=3)]})

    counts = auto_match_unlinked(db, object())

    assert counts["suggested"] == 1 and stub.applied == []
    stored = db.get_match_suggestion("alb-1")
    assert stored is not None and (stored["files_found"], stored["file_count"]) == (2, 3)


def test_auto_stores_when_runner_up_close(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    stub = _Stub(monkeypatch, {"alb-1": [_cand("rg-a", 96, 2), _cand("rg-b", 87, 2)]})

    counts = auto_match_unlinked(db, object())

    assert counts["suggested"] == 1 and stub.applied == []


def test_auto_counts_no_candidates_and_clears_stale(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    db.save_match_suggestion("alb-1", _cand("rg-a", 60, 1), 1, 2)
    _Stub(monkeypatch, {"alb-1": []})

    counts = auto_match_unlinked(db, object())

    assert counts["no_candidates"] == 1
    assert db.get_match_suggestion("alb-1") is None


def test_auto_stops_after_three_unavailable(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for n in range(5):
        _seed(db, tmp_path, f"alb-{n}")
    _Stub(monkeypatch, {f"alb-{n}": "down" for n in range(5)})

    counts = auto_match_unlinked(db, object())

    assert counts["unavailable"] == 3 and counts["albums"] == 3


def test_auto_conflict_counted(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    _Stub(monkeypatch, {"alb-1": [_cand("rg-a", 95, 2)]}, conflict=True)

    counts = auto_match_unlinked(db, object())

    assert counts["conflicts"] == 1 and counts["applied"] == 0


def test_auto_counts_errors_and_continues(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path, "alb-1")
    _seed(db, tmp_path, "alb-2")
    _Stub(monkeypatch, {"alb-1": sqlite3.OperationalError("locked"), "alb-2": [_cand("rg-a", 50, 1, "alb-2")]})

    counts = auto_match_unlinked(db, object())

    assert counts["errors"] == 1 and counts["suggested"] == 1


def test_apply_stored_applies_and_deletes(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    db.save_match_suggestion("alb-1", _cand("rg-a", 80, 2), 2, 2)
    stub = _Stub(monkeypatch, {})

    results = apply_stored_suggestions(db, object(), ["alb-1"])

    assert results == [{"album_id": "alb-1", "final_album_id": "alb-1", "status": "applied"}]
    assert stub.applied[0][1] == "rg-a"
    assert db.get_match_suggestion("alb-1") is None


def test_apply_stored_missing_suggestion(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed(db, tmp_path)
    stub = _Stub(monkeypatch, {})

    results = apply_stored_suggestions(db, object(), ["alb-1"])

    assert results[0]["status"] == "missing" and stub.applied == []


def test_routes_admin_only_and_paging(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):  # noqa: F811
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice_h = _auth_headers(seeded_users["alice"], test_db, test_config)
    for n, conf in enumerate([70, 85, 60]):
        _seed(test_db, tmp_path, f"alb-{n}")
        test_db.save_match_suggestion(f"alb-{n}", _cand(f"rg-{n}", conf, 2, f"alb-{n}"), 2, 2)
    url = "/api/library/musicbrainz/suggestions"

    assert client.get(url, headers=alice_h).status_code == 403
    first = client.get(url, params={"page": 1, "page_size": 2}, headers=admin_h).json()
    second = client.get(url, params={"page": 2, "page_size": 2}, headers=admin_h).json()
    assert first["total"] == 3 and [i["confidence"] for i in first["items"]] == [85, 70]
    assert [i["confidence"] for i in second["items"]] == [60]
    assert first["items"][0]["candidate"]["id"] == "rg-1" and first["items"][0]["album_title"] == "Hits"

    app.dependency_overrides[get_mbid_enricher] = lambda: object()
    apply_url = url + "/apply"
    assert client.post(apply_url, json={"album_ids": ["alb-0"]}, headers=alice_h).status_code == 403
    assert client.post(apply_url, json={"album_ids": []}, headers=admin_h).status_code == 422
    res = client.post(apply_url, json={"album_ids": ["nope"]}, headers=admin_h)
    assert res.status_code == 200 and res.json()["results"][0]["status"] == "missing"


def test_migration_v100_creates_table(db: Database):
    assert SCHEMA_VERSION >= 100
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(library_match_suggestions)").fetchall()}
    assert cols == {"album_id", "candidate_json", "confidence", "files_found", "file_count", "computed_at"}
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
