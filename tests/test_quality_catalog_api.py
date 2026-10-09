"""API: quality definitions, custom formats (import/export), release profiles, v2 quality profiles, preview."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

QD = "/api/settings/quality-definitions"
CF = "/api/settings/custom-formats"
RP = "/api/settings/release-profiles"
QP = "/api/settings/quality-profiles"

SERVARR = {
    "name": "Preferred Groups X",
    "includeCustomFormatWhenRenaming": False,
    "specifications": [
        {"name": "DeVOiD", "implementation": "ReleaseGroupSpecification", "negate": False, "required": False,
         "fields": {"value": "\\bDeVOiD\\b"}},
        {"name": "PERFECT", "implementation": "ReleaseGroupSpecification", "negate": False, "required": False,
         "fields": {"value": "\\bPERFECT\\b"}},
    ],
}


@pytest.fixture
def ctx(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)

    def headers(user):
        token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret)
        db.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    admin = headers(db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True))
    alice = headers(db.upsert_user("alice-1", "alice", "al@x.tv", is_admin=False))
    yield client, db, admin, alice
    db.close()


@pytest.mark.parametrize("path", [QD, CF, RP, QP])
def test_authz_unauth_and_non_admin(ctx, path):
    client, _, admin, alice = ctx
    assert client.get(path).status_code == 401
    assert client.get(path, headers=alice).status_code == 403
    assert client.get(path, headers=admin).status_code == 200


def test_gateway_tier_blocked(ctx, tmp_path):
    client, db, admin, _ = ctx
    cfg = Config(plex_url="http://x", plex_token="t", data_dir=str(tmp_path), role="gateway")
    client.app.dependency_overrides[get_config] = lambda: cfg
    for path in (QD, CF, RP, QP):
        # 403 from require_core_tier (or 404 when the gateway app does not serve library-management routes at all)
        assert client.get(path, headers=admin).status_code in (403, 404)


# ------------------------------------------------------------------ definitions


def test_definitions_list_update_reset(ctx):
    client, _, admin, _ = ctx
    rows = client.get(QD, headers=admin).json()
    assert [r["quality"] for r in rows][:2] == ["FLAC 24bit", "FLAC 16bit"]
    assert rows[0]["is_default"] is True and rows[0]["default_max_kbps"] == 9500
    r = client.put(f"{QD}/MP3 320", json={"min_kbps": 200, "preferred_kbps": 320, "max_kbps": 400}, headers=admin)
    assert r.status_code == 200 and r.json()["min_kbps"] == 200 and r.json()["is_default"] is False
    # reset one
    r = client.post(f"{QD}/MP3 320/reset", headers=admin)
    assert r.json()["min_kbps"] == 290 and r.json()["is_default"] is True
    # unbounded = null
    r = client.put(f"{QD}/FLAC 16bit", json={"min_kbps": 0, "preferred_kbps": 895, "max_kbps": None}, headers=admin)
    assert r.status_code == 200 and r.json()["max_kbps"] is None
    # reset all
    client.put(f"{QD}/MP3 V0", json={"min_kbps": 1, "preferred_kbps": 2, "max_kbps": 3}, headers=admin)
    rows = client.post(f"{QD}/reset", headers=admin).json()
    assert all(r["is_default"] for r in rows)


def test_definitions_validation(ctx):
    client, _, admin, _ = ctx
    assert client.put(f"{QD}/Nope", json={}, headers=admin).status_code == 404
    assert client.post(f"{QD}/Nope/reset", headers=admin).status_code == 404
    assert client.put(f"{QD}/MP3 320", json={"min_kbps": 400, "preferred_kbps": 320}, headers=admin).status_code == 400
    assert client.put(f"{QD}/MP3 320", json={"min_kbps": 100, "max_kbps": 50}, headers=admin).status_code == 400
    assert client.put(f"{QD}/MP3 320", json={"min_kbps": -1}, headers=admin).status_code == 422


# ------------------------------------------------------------------ custom formats


def test_custom_format_crud(ctx):
    client, _, admin, _ = ctx
    body = {"name": "My Fmt", "specifications": [
        {"name": "t", "implementation": "ReleaseTitleSpecification", "fields": {"value": r"\bremix\b"}, "negate": True}]}
    r = client.post(CF, json=body, headers=admin)
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    assert r.json()["specifications"][0]["negate"] is True and r.json()["unsupported"] is False
    assert client.post(CF, json=body, headers=admin).status_code == 400  # duplicate name
    body["name"] = "My Fmt 2"
    assert client.put(f"{CF}/{fid}", json=body, headers=admin).json()["name"] == "My Fmt 2"
    assert client.get(f"{CF}/{fid}", headers=admin).status_code == 200
    assert client.delete(f"{CF}/{fid}", headers=admin).json()["status"] == "deleted"
    assert client.get(f"{CF}/{fid}", headers=admin).status_code == 404
    assert client.delete(f"{CF}/{fid}", headers=admin).status_code == 404
    assert client.put(f"{CF}/9999", json=body, headers=admin).status_code == 404


def test_custom_format_validation_400(ctx):
    client, _, admin, _ = ctx
    bad_regex = {"name": "Bad", "specifications": [
        {"implementation": "ReleaseTitleSpecification", "fields": {"value": "(a+)+$"}}]}
    r = client.post(CF, json=bad_regex, headers=admin)
    assert r.status_code == 400 and "nested quantifier" in str(r.json()["detail"])
    long_regex = {"name": "Long", "specifications": [
        {"implementation": "ReleaseTitleSpecification", "fields": {"value": "a" * 501}}]}
    assert client.post(CF, json=long_regex, headers=admin).status_code == 400
    bad_size = {"name": "Sz", "specifications": [
        {"implementation": "SizeSpecification", "fields": {"min": 5, "max": 1}}]}
    assert client.post(CF, json=bad_size, headers=admin).status_code == 400
    assert client.post(CF, json={"name": ""}, headers=admin).status_code == 422


def test_delete_format_prunes_profile_scores(ctx):
    client, db, admin, _ = ctx
    fid = db.get_custom_format_by_name("Vinyl")["id"]
    assert any(fi["format_id"] == fid for fi in db.get_quality_profile("profile-lossless")["format_items"])
    client.delete(f"{CF}/{fid}", headers=admin)
    assert not any(fi["format_id"] == fid for fi in db.get_quality_profile("profile-lossless")["format_items"])


def test_import_export_round_trip_single_and_list(ctx):
    client, _, admin, _ = ctx
    r = client.post(f"{CF}/import", json=SERVARR, headers=admin)
    assert r.status_code == 200 and r.json()["imported"][0]["action"] == "created"
    fid = r.json()["imported"][0]["id"]
    exported = client.get(f"{CF}/{fid}/export", headers=admin).json()
    assert exported == SERVARR
    # re-import replaces by name
    again = client.post(f"{CF}/import", json=[SERVARR], headers=admin).json()
    assert again["imported"][0]["action"] == "updated" and again["imported"][0]["id"] == fid


def test_import_unknown_implementation_flagged_and_partial_errors(ctx):
    client, _, admin, _ = ctx
    video = {"name": "Res 1080", "specifications": [
        {"name": "1080p", "implementation": "ResolutionSpecification", "negate": False, "required": True,
         "fields": {"value": 1080}}]}
    broken = {"name": "Broken", "specifications": [
        {"implementation": "ReleaseGroupSpecification", "fields": {"value": "(x+)+"}}]}
    r = client.post(f"{CF}/import", json=[video, broken, SERVARR], headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert len(body["imported"]) == 2 and body["errors"][0]["name"] == "Broken"
    res = next(i for i in body["imported"] if i["name"] == "Res 1080")
    assert res["unsupported"] is True and res["specifications"][0]["unsupported"] is True
    # an unsupported format never scores
    ev = client.post(f"{QP}/evaluate", json={"title": "A - B [FLAC] 1080p"}, headers=admin).json()
    assert "Res 1080" not in [m["name"] for m in ev["breakdown"]["matched_formats"]]
    # nothing importable -> 400
    assert client.post(f"{CF}/import", json=broken, headers=admin).status_code == 400
    assert client.post(f"{CF}/import", json=[], headers=admin).status_code == 400
    assert client.post(f"{CF}/import", json="nope", headers=admin).status_code == 400


def test_import_accepts_api_style_fields_array(ctx):
    client, _, admin, _ = ctx
    api_style = {**SERVARR, "name": "Arr Style", "specifications": [
        {**s, "fields": [{"name": "value", "value": s["fields"]["value"]}]} for s in SERVARR["specifications"]]}
    r = client.post(f"{CF}/import", json=api_style, headers=admin)
    fid = r.json()["imported"][0]["id"]
    assert client.get(f"{CF}/{fid}/export", headers=admin).json()["specifications"][0]["fields"] == {"value": "\\bDeVOiD\\b"}
    assert client.get(f"{CF}/9999/export", headers=admin).status_code == 404


# ------------------------------------------------------------------ release profiles


def test_release_profile_crud_and_validation(ctx):
    client, _, admin, _ = ctx
    seeded = client.get(RP, headers=admin).json()
    assert seeded[0]["name"] == "Reject bad sources" and seeded[0]["enabled"] is True
    body = {"name": "Needs CD", "required": ["cd", "/web(?:-dl)?/"], "ignored": ["bootleg"], "indexer_ids": ["3"]}
    r = client.post(RP, json=body, headers=admin)
    assert r.status_code == 200
    rid = r.json()["id"]
    assert r.json()["required"] == ["cd", "/web(?:-dl)?/"] and r.json()["enabled"] is True
    assert client.post(RP, json=body, headers=admin).status_code == 400  # duplicate name
    assert client.post(RP, json={"name": "x", "ignored": ["/(a+)+$/"]}, headers=admin).status_code == 400
    assert client.post(RP, json={"name": "y", "required": ["/(unclosed/"]}, headers=admin).status_code == 400
    r = client.put(f"{RP}/{rid}", json={**body, "enabled": False, "ignored": []}, headers=admin)
    assert r.json()["enabled"] is False and r.json()["ignored"] == []
    assert client.get(f"{RP}/{rid}", headers=admin).status_code == 200
    assert client.delete(f"{RP}/{rid}", headers=admin).status_code == 200
    assert client.get(f"{RP}/{rid}", headers=admin).status_code == 404
    assert client.put(f"{RP}/{rid}", json=body, headers=admin).status_code == 404


# ------------------------------------------------------------------ quality profiles v2


V2 = {
    "name": "V2 Profile",
    "cutoff": "Lossless",
    "items": [
        {"type": "group", "name": "Lossless", "allowed": True, "items": ["FLAC 24bit", "FLAC 16bit"]},
        {"type": "quality", "quality": "MP3 320", "allowed": True},
        {"type": "quality", "quality": "MP3 V0", "allowed": False},
    ],
    "upgrade_allowed": True,
    "min_format_score": 0,
    "cutoff_format_score": 10,
    "min_upgrade_format_score": 3,
}


def test_profile_v2_create_roundtrip(ctx):
    client, db, admin, _ = ctx
    cd = db.get_custom_format_by_name("CD")["id"]
    r = client.post(QP, json={**V2, "format_items": [{"format_id": cd, "score": 25}]}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0] == {"type": "group", "name": "Lossless", "allowed": True, "items": ["FLAC 24bit", "FLAC 16bit"],
                                "quality": None, "weight": None}
    assert body["cutoff"] == "Lossless" and body["cutoff_format_score"] == 10 and body["min_upgrade_format_score"] == 3
    assert body["format_items"] == [{"format_id": cd, "score": 25}]
    got = client.get(f"{QP}/{body['id']}", headers=admin).json()
    assert got["items"] == body["items"]
    # update keeps format items when omitted
    upd = client.post(QP, json={**V2, "id": body["id"], "format_items": None, "name": "Renamed"}, headers=admin).json()
    assert upd["name"] == "Renamed" and upd["format_items"] == [{"format_id": cd, "score": 25}]


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda p: p.update(cutoff="Nope"), "Cutoff"),
        (lambda p: p["items"].append({"type": "quality", "quality": "MP3 320"}), "Duplicate entry name"),
        (lambda p: p["items"].append({"type": "quality", "quality": "WMA"}), "Unknown quality"),
        (lambda p: p["items"].append({"type": "group", "name": "", "items": ["MP3 192"]}), "name"),
        (lambda p: p["items"].append({"type": "group", "name": "G", "items": []}), "no qualities"),
        (lambda p: p["items"].append({"type": "bogus", "quality": "MP3 192"}), "Unknown item type"),
        (lambda p: p.update(format_items=[{"format_id": 99999, "score": 1}]), "does not exist"),
    ],
)
def test_profile_v2_validation_400(ctx, mutate, fragment):
    client, _, admin, _ = ctx
    payload = {**V2, "items": [dict(i) for i in V2["items"]]}
    mutate(payload)
    r = client.post(QP, json=payload, headers=admin)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_profile_empty_items_allowed(ctx):
    client, _, admin, _ = ctx
    r = client.post(QP, json={"name": "Empty", "cutoff": "FLAC", "items": []}, headers=admin)
    assert r.status_code == 200 and r.json()["items"] == []


def test_profile_numeric_bounds_422(ctx):
    client, _, admin, _ = ctx
    assert client.post(QP, json={**V2, "min_upgrade_format_score": 0}, headers=admin).status_code == 422
    assert client.post(QP, json={**V2, "name": ""}, headers=admin).status_code == 422


def test_profile_duplicate_name_400(ctx):
    client, _, admin, _ = ctx
    assert client.post(QP, json={**V2, "name": "Lossless (FLAC)"}, headers=admin).status_code == 400


def test_profile_copy_and_set_default(ctx):
    client, _, admin, _ = ctx
    r = client.post(f"{QP}/profile-lossless/copy", headers=admin)
    assert r.status_code == 200 and r.json()["name"] == "Lossless (FLAC) (Copy)" and r.json()["is_default"] is False
    assert r.json()["format_items"] and r.json()["id"] != "profile-lossless"
    r2 = client.post(f"{QP}/profile-lossless/copy", json={"name": "Mine"}, headers=admin)
    assert r2.json()["name"] == "Mine"
    assert client.post(f"{QP}/profile-lossless/copy", json={"name": "Mine"}, headers=admin).status_code == 400
    assert client.post(f"{QP}/profile-lossless/copy", headers=admin).json()["name"] == "Lossless (FLAC) (Copy) 2"
    assert client.post(f"{QP}/nope/copy", headers=admin).status_code == 404
    new_default = r2.json()["id"]
    assert client.post(f"{QP}/{new_default}/default", headers=admin).json()["is_default"] is True
    profiles = client.get(QP, headers=admin).json()
    assert [p["id"] for p in profiles if p["is_default"]] == [new_default]
    assert client.post(f"{QP}/nope/default", headers=admin).status_code == 404


def test_legacy_payload_with_tags_still_works(ctx):
    client, _, admin, _ = ctx
    payload = {
        "id": "legacy-api", "name": "Legacy API", "cutoff": "MP3 320",
        "items": [{"quality": "MP3 192", "allowed": True, "weight": 500}, {"quality": "MP3 320", "allowed": True, "weight": 800}],
        "preferred_tags": ["web"], "ignored_tags": ["live"],
    }
    r = client.post(QP, json=payload, headers=admin)
    assert r.status_code == 200, r.text
    assert [i["quality"] for i in r.json()["items"]] == ["MP3 320", "MP3 192"]  # ordered by weight
    ev = client.post(f"{QP}/evaluate", json={"title": "A - B Live [MP3 320] [WEB]", "profile_id": "legacy-api"}, headers=admin).json()
    assert ev["evaluation"]["is_acceptable"] is False
    ok = client.post(f"{QP}/evaluate", json={"title": "A - B [MP3 320] [WEB]", "profile_id": "legacy-api"}, headers=admin).json()
    assert ok["evaluation"]["is_acceptable"] is True
    assert "Preferred: web" in [m["name"] for m in ok["breakdown"]["matched_formats"]]


# ------------------------------------------------------------------ preview


def test_evaluate_returns_full_breakdown(ctx):
    client, db, admin, _ = ctx
    r = client.post(
        f"{QP}/evaluate",
        json={"title": "Artist-Album-2020-FLAC-DeVOiD", "profile_id": "profile-lossless", "protocol": "newznab",
              "indexer_id": "4", "indexer_name": "NZB", "size_bytes": 300 * 1024 * 1024},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    b = r.json()["breakdown"]
    assert r.json()["parsed"]["release_group"] == "DeVOiD"
    assert b["protocol"] == "usenet" and b["quality"] == "FLAC 16bit"
    names = {m["name"]: m["score"] for m in b["matched_formats"]}
    assert names["Preferred Groups"] == 100 and names["Lossless"] == 10
    assert b["format_score"] == sum(names.values()) and b["total_score"] == r.json()["evaluation"]["score"]
    assert b["kbps"]["skipped_reason"] == "duration unknown"


def test_evaluate_with_album_duration_and_upgrade(ctx):
    client, db, admin, _ = ctx
    artist = db.upsert_library_artist({"name": "A"})
    album = db.upsert_library_album({"artist_id": artist["id"], "title": "B", "total_tracks": 2})
    for n in (1, 2):
        db.upsert_library_track({"album_id": album["id"], "artist_id": artist["id"], "title": f"T{n}",
                                 "track_number": n, "duration_seconds": 1200})
    size = int(100 * 1000 / 8 * 2400)  # 100 kbps over 2400 s
    r = client.post(f"{QP}/evaluate", json={"title": "A - B [MP3 320]", "profile_id": "profile-standard-mp3",
                                            "size_bytes": size, "album_id": album["id"]}, headers=admin).json()
    assert r["evaluation"]["is_acceptable"] is False
    assert r["breakdown"]["rejections"][0]["code"] == "kbps_below_min"
    assert r["breakdown"]["kbps"]["measured"] == pytest.approx(100, abs=1)
    up = client.post(f"{QP}/evaluate", json={"title": "A - B [FLAC 16bit]", "profile_id": "profile-high-quality",
                                             "current_title": "A - B [MP3 320]"}, headers=admin).json()
    assert up["upgrade"]["is_upgrade"] is True


def test_evaluate_404_and_validation(ctx):
    client, _, admin, _ = ctx
    assert client.post(f"{QP}/evaluate", json={"title": "x", "profile_id": "nope"}, headers=admin).status_code == 404
    assert client.post(f"{QP}/evaluate", json={"title": ""}, headers=admin).status_code == 422
    assert client.post(f"{QP}/evaluate", json={"title": "x", "size_bytes": -1}, headers=admin).status_code == 422
