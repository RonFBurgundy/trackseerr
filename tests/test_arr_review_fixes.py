"""Regression tests for the ARR profiles review findings (docs/design/ARR_PROFILES_SPEC.md)."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.backlog_worker import _current_floor
from trackseerr.decision_engine import DurationInfo, resolve_durations, upgrade_floor
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.quality_defaults import (
    V51_NEW_QUALITIES,
    legacy_cutoff_for_entries,
    legacy_items_to_entries,
)
from trackseerr.storage import Database

from tests.test_library_scanner import db, scanner  # noqa: F401
from tests.test_metadata_profiles import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

QP = "/api/settings/quality-profiles"


# ------------------------------------------------------------------ 2 + 3. UI save keeps legacy fields / tags


def _q(quality, allowed):
    return {"type": "quality", "quality": quality, "allowed": allowed}


def _v2_body(**extra):
    return {
        "id": "lp",
        "name": "Legacy",
        "cutoff": "FLAC 16bit",
        "items": [_q("FLAC 16bit", True), _q("MP3 320", True)],
        "upgrade_allowed": True,
        "min_format_score": -100,
        "cutoff_format_score": 0,
        "min_upgrade_format_score": 1,
        **extra,
    }


def test_v2_save_preserves_legacy_fields(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.upsert_quality_profile(
        {
            "id": "lp", "name": "Legacy", "cutoff": "FLAC 16bit", "items": [_q("FLAC 16bit", True)],
            "min_score": 900, "min_size_mb": 5.0, "max_size_mb": 800.0,
            "custom_formats": [{"name": "X", "pattern": "x", "score": 5, "negate": False}],
            "preferred_tags": ["cd"], "ignored_tags": ["live"],
        }
    )
    r = client.post(QP, json=_v2_body(), headers=h)  # the frontend v2 shape: no legacy fields
    assert r.status_code == 200, r.text
    p = test_db.get_quality_profile("lp")
    assert (p["min_score"], p["min_size_mb"], p["max_size_mb"]) == (900, 5.0, 800.0)
    assert p["custom_formats"] == [{"name": "X", "pattern": "x", "score": 5, "negate": False}]
    assert p["preferred_tags"] == ["cd"] and p["ignored_tags"] == ["live"]
    # explicitly sent values still win (including clearing)
    client.post(QP, json=_v2_body(min_score=None, min_size_mb=1.0, preferred_tags=[]), headers=h)
    p = test_db.get_quality_profile("lp")
    assert p["min_score"] is None and p["min_size_mb"] == 1.0 and p["preferred_tags"] == []
    assert p["max_size_mb"] == 800.0 and p["ignored_tags"] == ["live"]


def test_resave_does_not_reapply_legacy_tags(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    body = _v2_body(preferred_tags=["cd"], ignored_tags=["live"])
    assert client.post(QP, json=body, headers=h).status_code == 200
    cd = test_db.get_custom_format_by_name("Preferred: cd")
    prof = test_db.get_quality_profile("lp")
    assert {fi["format_id"]: fi["score"] for fi in prof["format_items"]}[cd["id"]] == 50
    rp = next(r for r in test_db.list_release_profiles() if r["name"] == "Ignored tags: Legacy")

    # user tunes the score and deletes the generated release profile
    tuned = [{**fi, "score": 80} if fi["format_id"] == cd["id"] else fi for fi in prof["format_items"]]
    assert client.post(QP, json=_v2_body(preferred_tags=["cd"], ignored_tags=["live"], format_items=tuned),
                       headers=h).status_code == 200
    test_db.delete_release_profile(rp["id"])

    assert client.post(QP, json=_v2_body(preferred_tags=["cd"], ignored_tags=["live"]), headers=h).status_code == 200
    scores = {fi["format_id"]: fi["score"] for fi in test_db.get_quality_profile("lp")["format_items"]}
    assert scores[cd["id"]] == 80
    assert not any(r["name"] == "Ignored tags: Legacy" for r in test_db.list_release_profiles())

    # a genuinely new tag is folded in; the old one keeps its tuned score
    assert client.post(QP, json=_v2_body(preferred_tags=["cd", "web"], ignored_tags=["live", "bootleg"]),
                       headers=h).status_code == 200
    scores = {fi["format_id"]: fi["score"] for fi in test_db.get_quality_profile("lp")["format_items"]}
    web = test_db.get_custom_format_by_name("Preferred: web")
    assert scores[cd["id"]] == 80 and scores[web["id"]] == 50
    rp2 = next(r for r in test_db.list_release_profiles() if r["name"] == "Ignored tags: Legacy")
    assert len(rp2["ignored"]) == 1  # only the added "bootleg"


# ------------------------------------------------------------------ 4. same-tier upgrade loop


def _hq_profile(db):
    return _to_quality_profile(db.get_quality_profile("profile-high-quality"))


def test_unknown_current_title_only_better_tier_upgrades(test_db):
    prof = _hq_profile(test_db)
    cand_same = evaluate_release(parse_release_title("A - B [MP3 320] [WEB]"), prof)
    cand_plain = evaluate_release(parse_release_title("A - B [MP3 320]"), prof)
    assert cand_same.score > cand_plain.score  # the WEB format gives a same-tier bonus
    floor = _current_floor(test_db, prof, "MP3 320", request_id="req-unknown")
    assert not cand_same.score > floor  # no re-grab loop
    assert not cand_plain.score > floor
    better = evaluate_release(parse_release_title("A - B [FLAC 16bit]"), prof)
    assert better.score > floor  # a better tier still upgrades
    vinyl_flac = evaluate_release(parse_release_title("A - B [FLAC 16bit] [Vinyl]"), prof)
    assert vinyl_flac.score > floor  # even a better tier with a negative format score


def test_known_current_title_same_tier_upgrade_still_works(test_db):
    prof = _hq_profile(test_db)
    test_db.record_download_event("imported", request_id="req-1", release_title="A - B [MP3 320] [Vinyl]")
    floor = _current_floor(test_db, prof, "MP3 320", request_id="req-1")
    cand_web = evaluate_release(parse_release_title("A - B [MP3 320] [WEB]"), prof)
    assert cand_web.score > floor
    cur = evaluate_release(parse_release_title("A - B [MP3 320] [Vinyl]"), prof)
    assert floor == upgrade_floor(cur, prof)
    # the same title as the current file is not an upgrade of itself
    assert not cur.score > floor


def test_known_title_for_other_quality_is_ignored(test_db):
    prof = _hq_profile(test_db)
    test_db.record_download_event("imported", request_id="req-2", release_title="A - B [FLAC 16bit]")
    floor = _current_floor(test_db, prof, "MP3 320", request_id="req-2")  # file was replaced since
    cand_web = evaluate_release(parse_release_title("A - B [MP3 320] [WEB]"), prof)
    assert not cand_web.score > floor


def test_library_scan_treats_cutoff_format_score_as_met(db, scanner, tmp_path: Path):  # noqa: F811
    default = db.get_default_quality_profile()
    db.upsert_quality_profile({**default, "cutoff_format_score": 100000})
    album_dir = tmp_path / "music" / "Miles Davis" / "Kind of Blue"
    album_dir.mkdir(parents=True)
    f = album_dir / "01 - So What.flac"
    f.write_bytes(b"flac")
    meta = {
        "title": "So What", "artist": "Miles Davis", "album": "Kind of Blue", "track_number": 1, "disc_number": 1,
        "codec": "FLAC", "bitrate": 900000, "sample_rate": 44100, "bits_per_sample": 16,
        "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(f.resolve()),
    }
    with patch("trackseerr.library_scanner.inspect_audio_file", return_value=meta):
        assert scanner.scan(db, root_folder=str(tmp_path / "music"))["status"] == "completed"
    row = db.get_library_file_by_path(str(f.resolve()))
    assert row is not None and row["cutoff_met"] is True


# ------------------------------------------------------------------ 5. estimated durations / widened defaults


def test_long_track_album_with_estimated_duration_not_rejected(test_db):
    prof = _hq_profile(test_db)
    # 4 tracks of ~15 min each would be 960 s if estimated at 240 s/track; the real album is 3600 s at 320 kbps
    duration = resolve_durations([None, None, None, None], 4)
    assert duration.estimated
    size = int(320 * 1000 / 8 * 3600)
    res = evaluate_release(parse_release_title("A - B [MP3 320]"), prof, size_bytes=size, duration=duration)
    assert res.is_acceptable, res.rejection_reasons
    assert "estimated" in res.breakdown.kbps["skipped_reason"]
    known = evaluate_release(parse_release_title("A - B [MP3 320]"), prof, size_bytes=size,
                             duration=DurationInfo(960))
    assert not known.is_acceptable  # a known duration is still enforced


def test_default_320_and_v0_max_are_400(test_db):
    defs = {d["quality"]: d for d in test_db.list_quality_definitions()}
    assert defs["MP3 320"]["max_kbps"] == 400 and defs["MP3 V0"]["max_kbps"] == 400
    assert defs["MP3 320"]["default_max_kbps"] == 400



# ------------------------------------------------------------------ 6. v49 weight order vs list-index cutoff


def _items(*rows):
    return [{"quality": q, "allowed": True, "weight": w} for q, w in rows]


def _met_set_old(items, cutoff):
    """Old engine: a quality meets the cutoff when its list index <= the cutoff's list index."""
    idx = [i["quality"] for i in items].index(cutoff)
    return {i["quality"] for i in items[: idx + 1]}


def _met_set_new(entries, cutoff):
    order = [e["quality"] for e in entries]
    return set(order[: order.index(cutoff) + 1])


def test_v49_list_order_differs_from_weights_exact():
    items = _items(("FLAC 16bit", 900), ("FLAC 24bit", 1000), ("MP3 320", 800), ("Unknown", 100))
    entries = legacy_items_to_entries(items)
    assert [e["quality"] for e in entries] == ["FLAC 24bit", "FLAC 16bit", "MP3 320", "Unknown"]  # weight ranking
    cutoff, exact = legacy_cutoff_for_entries(items, "FLAC 24bit")
    assert exact and cutoff == "FLAC 16bit"
    assert _met_set_new(entries, cutoff) == _met_set_old(items, "FLAC 24bit")


def test_v49_unrepresentable_cutoff_is_flagged():
    items = _items(("MP3 320", 800), ("FLAC 24bit", 1000), ("FLAC 16bit", 900))
    cutoff, exact = legacy_cutoff_for_entries(items, "MP3 320")
    assert not exact and cutoff == "MP3 320"
    items = _items(("FLAC 16bit", 900), ("MP3 320", 800), ("FLAC 24bit", 1000), ("Unknown", 1))
    cutoff, exact = legacy_cutoff_for_entries(items, "MP3 320")  # old met {FLAC16, MP3 320}; prefix = {FLAC24} no
    assert not exact


def test_v49_zero_and_equal_weights():
    # weight 0 falls back to 1000 - index * 100, exactly like the old engine's ranking
    items = _items(("MP3 320", 0), ("FLAC 16bit", 950), ("Unknown", 0))
    assert [e["quality"] for e in legacy_items_to_entries(items)] == ["MP3 320", "FLAC 16bit", "Unknown"]
    assert legacy_cutoff_for_entries(items, "MP3 320") == ("MP3 320", True)
    # equal weights keep the stored order, and the list-index cutoff maps to the same entry
    items = _items(("FLAC 16bit", 100), ("MP3 320", 100), ("Unknown", 100))
    entries = legacy_items_to_entries(items)
    assert [e["quality"] for e in entries] == ["FLAC 16bit", "MP3 320", "Unknown"]
    assert legacy_cutoff_for_entries(items, "MP3 320") == ("MP3 320", True)
    assert legacy_cutoff_for_entries(items, "Nope") == ("Nope", True)



# ------------------------------------------------------------------ 7. deprecated release-profile aliases


def test_release_profile_aliases(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    old = client.get("/api/library/release-profiles", headers=h)
    new = client.get("/api/library/metadata-profiles", headers=h)
    assert old.status_code == 200 and old.json() == new.json()
    body = {"name": "Alias", "primary_types": ["album"], "secondary_types": ["studio"]}
    r = client.post("/api/library/release-profiles", json=body, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.put(f"/api/library/release-profiles/{pid}", json={**body, "name": "Alias2"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Alias2"
    test_db.upsert_library_artist({"id": "ar", "name": "ar", "mbid": "mb-ar", "monitored": True})
    p = client.get("/api/library/artists/ar/release-profile-preview", params={"profile_id": pid}, headers=h)
    q = client.get("/api/library/artists/ar/metadata-profile-preview", params={"profile_id": pid}, headers=h)
    assert p.status_code == 200 and p.json() == q.json()
    assert client.delete(f"/api/library/release-profiles/{pid}", headers=h).status_code == 200
    assert client.delete(f"/api/library/release-profiles/{pid}", headers=h).status_code == 404
    paths = client.app.openapi()["paths"]
    for path, method in (
        ("/api/library/release-profiles", "get"),
        ("/api/library/release-profiles", "post"),
        ("/api/library/release-profiles/{profile_id}", "put"),
        ("/api/library/release-profiles/{profile_id}", "delete"),
        ("/api/library/artists/{artist_id}/release-profile-preview", "get"),
    ):
        assert paths[path][method]["deprecated"] is True
    assert not paths["/api/library/metadata-profiles"]["get"].get("deprecated")
