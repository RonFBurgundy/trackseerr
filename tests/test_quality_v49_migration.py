"""Migration v49: quality definitions, custom formats, release profiles and v2 profile items from a v48 database."""

import json
import sqlite3

from plex_playlist_sync.acquisition_coordinator import _to_quality_profile
from plex_playlist_sync.models import QualityProfile, QualityProfileItem
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.storage import SCHEMA_VERSION, Database


def _downgrade_to_v48(path: str) -> None:
    """Rewinds a freshly migrated DB to its v48 shape and plants old-style profiles."""
    conn = sqlite3.connect(path)
    for table in ("quality_definitions", "custom_formats", "release_profiles"):
        conn.execute(f"DROP TABLE {table}")
    for col in ("format_items_json", "min_format_score", "cutoff_format_score", "min_upgrade_format_score"):
        conn.execute(f"ALTER TABLE quality_profiles DROP COLUMN {col}")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 49")
    conn.execute("DELETE FROM quality_profiles")
    items = [  # deliberately not stored in weight order
        {"quality": "MP3 320", "allowed": True, "weight": 800},
        {"quality": "FLAC 16bit", "allowed": True, "weight": 900},
        {"quality": "FLAC 24bit", "allowed": False, "weight": 1000},
        {"quality": "Unknown", "allowed": False, "weight": 100},
    ]
    conn.execute(
        "INSERT INTO quality_profiles (id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json, "
        "min_size_mb, max_size_mb, is_default, min_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("old-1", "Old Style", "FLAC 16bit", json.dumps(items), json.dumps(["cd", "Web"]),
         json.dumps(["live", "bootleg"]), 5.0, 900.0, 1, 950),
    )
    conn.execute(
        "INSERT INTO quality_profiles (id, name, cutoff, items_json) VALUES (?,?,?,?)",
        ("old-2", "Plain", "MP3 320", json.dumps([{"quality": "MP3 320", "allowed": True, "weight": 5}])),
    )
    conn.commit()
    conn.close()


def _migrated(tmp_path):
    path = str(tmp_path / "t.db")
    Database(path).close()
    _downgrade_to_v48(path)
    return Database(path)


def test_schema_version_and_seeds(tmp_path):
    assert SCHEMA_VERSION == 51
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 51
        defs = {d["quality"]: d for d in db.list_quality_definitions()}
        assert (defs["FLAC 24bit"]["min_kbps"], defs["FLAC 24bit"]["preferred_kbps"], defs["FLAC 24bit"]["max_kbps"]) == (0, 2000, 9500)
        assert (defs["MP3 320"]["min_kbps"], defs["MP3 320"]["preferred_kbps"], defs["MP3 320"]["max_kbps"]) == (290, 320, 350)
        assert (defs["MP3 V2"]["min_kbps"], defs["MP3 V2"]["max_kbps"]) == (130, 280)
        names = {f["name"] for f in db.list_custom_formats()}
        assert {"Preferred Groups", "CD", "Lossless", "Hi-Res 24bit", "WEB", "Vinyl", "Mono",
                "Censored/Clean", "Remastered", "Deluxe"} <= names
        groups = db.get_custom_format_by_name("Preferred Groups")
        assert len(groups["specifications"]) == 7
        rp = next(r for r in db.list_release_profiles() if r["name"] == "Reject bad sources")
        assert rp["enabled"] and len(rp["ignored"]) == 5 and rp["required"] == []
        # seeded default scores on the stock profiles
        prof = db.get_quality_profile("profile-lossless")
        scores = {fi["format_id"]: fi["score"] for fi in prof["format_items"]}
        assert scores[groups["id"]] == 100
        assert scores[db.get_custom_format_by_name("Vinyl")["id"]] == -50
        assert prof["min_upgrade_format_score"] == 1
    finally:
        db.close()


def test_profiles_migrated_to_ordered_items(tmp_path):
    db = _migrated(tmp_path)
    try:
        p = db.get_quality_profile("old-1")
        # v51 then appends the new codecs, disallowed, after the original entries
        assert [e["quality"] for e in p["items"]][:4] == ["FLAC 24bit", "FLAC 16bit", "MP3 320", "Unknown"]
        assert all(e["type"] == "quality" for e in p["items"])
        allowed = {e["quality"]: e["allowed"] for e in p["items"]}
        assert {q: allowed[q] for q in ("FLAC 24bit", "FLAC 16bit", "MP3 320", "Unknown")} == {
            "FLAC 24bit": False, "FLAC 16bit": True, "MP3 320": True, "Unknown": False}
        assert not any(allowed[q] for q in allowed if q not in ("FLAC 16bit", "MP3 320"))
        assert p["cutoff"] == "FLAC 16bit"
        assert p["min_format_score"] == -100 and p["cutoff_format_score"] == 0 and p["min_upgrade_format_score"] == 1
        # legacy columns remain readable
        assert p["preferred_tags"] == ["cd", "Web"] and p["ignored_tags"] == ["live", "bootleg"]
        assert (p["min_size_mb"], p["max_size_mb"], p["min_score"]) == (5.0, 900.0, 950)
        assert p["is_default"] is True
    finally:
        db.close()


def test_legacy_tags_become_formats_and_scoped_release_profile(tmp_path):
    db = _migrated(tmp_path)
    try:
        cd = db.get_custom_format_by_name("Preferred: cd")
        web = db.get_custom_format_by_name("Preferred: web")
        assert cd and web
        p = db.get_quality_profile("old-1")
        scores = {fi["format_id"]: fi["score"] for fi in p["format_items"]}
        assert scores[cd["id"]] == 50 and scores[web["id"]] == 50
        rp = next(r for r in db.list_release_profiles() if r["name"] == "Ignored tags: Old Style")
        assert rp["quality_profile_ids"] == ["old-1"] and len(rp["ignored"]) == 2
        # the profile without tags gets none and is unaffected by the scoped release profile
        assert not any(r["name"] == "Ignored tags: Plain" for r in db.list_release_profiles())
    finally:
        db.close()


def test_behaviour_preserved_against_legacy_engine(tmp_path):
    db = _migrated(tmp_path)
    try:
        migrated = _to_quality_profile(db.get_quality_profile("old-1"))
        legacy = QualityProfile(
            id="old-1", name="Old Style", cutoff="FLAC 16bit",
            items=[QualityProfileItem("MP3 320", True, 800), QualityProfileItem("FLAC 16bit", True, 900),
                   QualityProfileItem("FLAC 24bit", False, 1000), QualityProfileItem("Unknown", False, 100)],
            preferred_tags=["cd", "Web"], ignored_tags=["live", "bootleg"], min_size_mb=5.0, max_size_mb=900.0,
            min_score=950,
        )
        titles = [
            "A - B (2001) [FLAC 16bit] [CD]",          # accepted (1000? weight 900+100 >= 950)
            "A - B (2001) [FLAC 24bit] [WEB]",         # quality not allowed
            "A - B Live at X [FLAC 16bit]",             # ignored tag
            "A - B [MP3 320] bootleg",                  # ignored tag
            "A - B [MP3 320] [CD]",                     # accepted? 800+50 < 950 -> min_score reject
            "A - B [FLAC 16bit] [WEB]",
        ]
        for t in titles:
            for size in (None, 100 * 1024 * 1024, 2 * 1024**3):
                m = evaluate_release(parse_release_title(t), migrated, size_bytes=size)
                o = evaluate_release(parse_release_title(t), legacy, size_bytes=size)
                assert m.is_acceptable == o.is_acceptable, (t, size, m.rejection_reasons, o.rejection_reasons)
                assert m.meets_cutoff == o.meets_cutoff, (t, size)
                assert m.format_score >= o.format_score  # migrated adds default-format scores on top of +50 tags
        # allowed qualities and ordering preserved
        assert evaluate_release(parse_release_title("A - B [FLAC 24bit]"), migrated).parsed_quality == "FLAC 24bit"
        assert not evaluate_release(parse_release_title("A - B [FLAC 24bit]"), migrated).is_acceptable
    finally:
        db.close()


def test_vinyl_not_hard_rejected_after_migration(tmp_path):
    """Default formats carry soft penalties; migrated profiles' floor keeps old-engine acceptance."""
    db = _migrated(tmp_path)
    try:
        prof = _to_quality_profile(db.get_quality_profile("old-2"))
        r = evaluate_release(parse_release_title("A - B [MP3 320] Vinyl"), prof)
        assert r.is_acceptable and r.format_score == -50
    finally:
        db.close()


def test_migration_idempotent_columns(tmp_path):
    path = str(tmp_path / "again.db")
    Database(path).close()
    db = Database(path)  # reopening re-runs nothing and must not fail
    try:
        assert len(db.list_custom_formats()) >= 10
    finally:
        db.close()
