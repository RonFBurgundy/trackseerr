"""ReDoS hardening: validation, hard timeout via the ``regex`` module, reject-on-timeout semantics, input hardening."""

import time

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import safe_regex
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.decision_engine import parse_term, validate_term
from plex_playlist_sync.safe_regex import (
    Budget,
    BudgetExceeded,
    UnsafeRegexError,
    safe_search,
    validate_pattern,
)
from plex_playlist_sync.storage import Database
from tests.test_quality_engine_v2 import FLAC_MP3, _fmt, _profile, _score, _spec

CF = "/api/settings/custom-formats"
RP = "/api/settings/release-profiles"
QP = "/api/settings/quality-profiles"

CATASTROPHIC = [
    r".*.*.*.*.*x",
    r"\w*\w*\w*\w*\w*\w*x",
    r"(a+){2,10}b",
    r"(?:\w+\s*){1,10}x",
    r"(a|b|c)*\1x",
]


@pytest.fixture
def ctx(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    user = db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    yield client, db, {"Authorization": f"Bearer {token}"}
    db.close()


@pytest.fixture
def unchecked(monkeypatch):
    """Simulates a pattern stored before the tightened validation: skips the shape checks (compile still runs)."""
    monkeypatch.setattr(safe_regex, "_check", lambda items: None)
    monkeypatch.setattr(safe_regex, "_check_backreferences", lambda items: None)
    safe_regex._validated.cache_clear()
    yield
    safe_regex._validated.cache_clear()


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_catastrophic_rejected_at_save(pattern):
    with pytest.raises(UnsafeRegexError):
        validate_pattern(pattern)


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_catastrophic_rejected_by_api(ctx, pattern):
    client, _, h = ctx
    fmt = {"name": "Bad", "specifications": [{"name": "s", "implementation": "ReleaseTitleSpecification", "fields": {"value": pattern}}]}
    assert client.post(CF, json=fmt, headers=h).status_code == 400
    assert client.post(CF + "/import", json=fmt, headers=h).status_code == 400
    assert client.post(RP, json={"name": "R", "required": [f"/{pattern}/"]}, headers=h).status_code == 400


# Patterns the ``regex`` module cannot shortcut: they really do run away without the timeout.
RUNAWAY = [r"(a+)+$", r"(a|aa)+$"]
EVIL_TITLE = "a" * 200 + "!"


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_stored_catastrophic_returns_within_limit(unchecked, pattern):
    start = time.monotonic()
    try:
        safe_search(pattern, EVIL_TITLE)
    except BudgetExceeded:
        pass
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize("pattern", RUNAWAY)
def test_stored_runaway_times_out_within_limit(unchecked, pattern):
    start = time.monotonic()
    with pytest.raises(BudgetExceeded):
        safe_search(pattern, EVIL_TITLE)
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize("pattern", RUNAWAY)
def test_stored_catastrophic_format_rejects_in_engine(unchecked, pattern):
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", pattern))], scores={1: 5})
    start = time.monotonic()
    r = _score(p, "A - B [FLAC] " + "a" * 200 + "!")
    assert time.monotonic() - start < 2.0
    assert not r.is_acceptable
    assert "evaluation_budget_exceeded" in [x["code"] for x in r.breakdown.rejections]


@pytest.mark.parametrize("key", ["required", "ignored"])
def test_stored_catastrophic_release_profile_term_rejects(unchecked, key):
    rp = {"id": 1, "name": "RP", key: ["/(a+)+$/"]}
    p = _profile(FLAC_MP3, "FLAC 16bit", release_profiles=[rp])
    start = time.monotonic()
    r = _score(p, "A - B [FLAC] " + "a" * 200 + "!")
    assert time.monotonic() - start < 2.0
    assert not r.is_acceptable
    assert "evaluation_budget_exceeded" in [x["code"] for x in r.breakdown.rejections]


def test_budget_exhausted_rejects_not_skips():
    assert safe_search("a", "a") is True
    b = Budget(0)
    time.sleep(0.001)
    with pytest.raises(BudgetExceeded):
        safe_search("a", "a", b)
    assert b.exhausted


def test_validation_tightening():
    for bad in [r"a*b*c*d*e", r"(?:ab+){1,50}x", r"(?:(a)\1)+"]:
        with pytest.raises(UnsafeRegexError):
            validate_pattern(bad)
    for good in [r"\b(?:FLAC|Lossless)\b.*\d+.*kbps", r"colou?r", r"(foo|bar)+", r"\d+(?:\.\d+)?"]:
        validate_pattern(good)


def test_dotnet_named_group_with_regex_module():
    validate_pattern(r"(?<g>ab)c")
    assert safe_search(r"(?<g>ab)c", "xxABCxx") is True


def test_input_capped_to_256():
    assert safe_search("needle$", "x" * 300 + "needle") is False
    assert safe_search("^x{256}$", "x" * 400) is True


def test_parse_term_requires_both_slashes():
    assert parse_term("/ac/dc") == ("substring", "/ac/dc")
    assert parse_term("/") == ("substring", "/")
    assert parse_term("//") == ("substring", "//")
    assert parse_term("/flac/") == ("regex", "flac")
    validate_term("/ac/dc")  # a plain substring, not a regex


# ------------------------------------------------------------------ custom-format import / PUT hardening

SPEC = {"name": "s", "implementation": "ReleaseTitleSpecification", "fields": {"value": "flac"}}


@pytest.mark.parametrize("specs", ["abc", {"a": 1}, 5, [1, 2], ["x"], [SPEC, "x"]])
def test_non_list_or_non_object_specifications_400(ctx, specs):
    client, _, h = ctx
    r = client.post(CF + "/import", json={"name": "N", "specifications": specs}, headers=h)
    assert r.status_code == 400
    ok = client.post(CF, json={"name": "P"}, headers=h).json()
    # PUT takes the typed model: bad shapes are a 422 validation error, never a 500.
    assert client.put(f"{CF}/{ok['id']}", json={"name": "P", "specifications": specs}, headers=h).status_code in (400, 422)


def test_import_non_object_item_does_not_500(ctx):
    client, _, h = ctx
    assert client.post(CF + "/import", json=[{"name": "N", "specifications": 3}, "x"], headers=h).status_code == 400
    r = client.post(CF + "/import", json=[{"name": "N", "specifications": 3}, {"name": "Good", "specifications": [SPEC]}], headers=h)
    assert r.status_code == 200 and len(r.json()["errors"]) == 1


def test_name_length_caps(ctx):
    client, _, h = ctx
    assert client.post(CF + "/import", json={"name": "n" * 201, "specifications": [SPEC]}, headers=h).status_code == 400
    long_spec = {**SPEC, "name": "s" * 201}
    assert client.post(CF + "/import", json={"name": "N", "specifications": [long_spec]}, headers=h).status_code == 400
    assert client.post(CF, json={"name": "N", "specifications": [long_spec]}, headers=h).status_code in (400, 422)
    assert client.post(CF + "/import", json={"name": "n" * 200, "specifications": [{**SPEC, "name": "s" * 200}]}, headers=h).status_code == 200


def test_spec_count_cap(ctx):
    client, _, h = ctx
    assert client.post(CF + "/import", json={"name": "Many", "specifications": [SPEC] * 51}, headers=h).status_code == 400
    assert client.post(CF + "/import", json={"name": "Fifty", "specifications": [SPEC] * 50}, headers=h).status_code == 200
    assert client.post(CF, json={"name": "Many2", "specifications": [SPEC] * 51}, headers=h).status_code in (400, 422)


def test_import_concurrent_delete_is_reported_not_asserted(ctx, monkeypatch):
    client, db, h = ctx
    client.post(CF, json={"name": "Dup", "specifications": [SPEC]}, headers=h)
    monkeypatch.setattr(db, "update_custom_format", lambda *a, **k: None)
    r = client.post(CF + "/import", json=[{"name": "Dup", "specifications": [SPEC]}, {"name": "Other", "specifications": [SPEC]}], headers=h)
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["errors"]] == ["Dup"]
    assert [i["name"] for i in r.json()["imported"]] == ["Other"]


# ------------------------------------------------------------------ legacy custom_formats on quality profiles

PROFILE = {"name": "Legacy", "cutoff": "FLAC 16bit", "items": [{"type": "quality", "quality": "FLAC 16bit", "allowed": True}]}


@pytest.mark.parametrize("pattern", CATASTROPHIC + ["(unclosed"])
def test_legacy_custom_format_regex_validated_on_save(ctx, pattern):
    client, _, h = ctx
    r = client.post(QP, json={**PROFILE, "custom_formats": [{"name": "x", "pattern": pattern, "score": 5}]}, headers=h)
    assert r.status_code == 400


def test_legacy_custom_format_safe_regex_saves(ctx):
    client, _, h = ctx
    r = client.post(QP, json={**PROFILE, "custom_formats": [{"name": "x", "pattern": r"\bflac\b", "score": 5}]}, headers=h)
    assert r.status_code == 200, r.text
