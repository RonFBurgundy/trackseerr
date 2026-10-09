"""Unit and integration tests for Quality Profiles engine, title parser, evaluator, and REST API."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import (
    AudioQuality,
    EvaluationResult,
    ParsedRelease,
    QualityProfile,
    QualityProfileItem,
)
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance with migration v10 applied."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer header for a given user."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# 1. Release Title Parser Tests
# =============================================================================


def test_parse_release_title_flac_24bit():
    parsed = parse_release_title("Daft Punk - Discovery (2001) [FLAC 24-96] [WEB]")
    assert parsed.quality == AudioQuality.FLAC_24BIT.value
    assert parsed.source == "WEB"
    assert parsed.year == 2001
    assert parsed.artist == "Daft Punk"
    assert "Discovery" in (parsed.album or "")


def test_parse_release_title_flac_16bit():
    parsed = parse_release_title("Radiohead - OK Computer (1997) [FLAC] [CD] [Remastered]")
    assert parsed.quality == AudioQuality.FLAC_16BIT.value
    assert parsed.source == "CD"
    assert parsed.year == 1997
    assert parsed.artist == "Radiohead"
    assert "remaster" in parsed.tags or "remastered" in parsed.tags


def test_parse_release_title_mp3_320():
    parsed = parse_release_title("The Beatles - Abbey Road (1969) [MP3 320kbps] [Vinyl]")
    assert parsed.quality == AudioQuality.MP3_320.value
    assert parsed.source == "Vinyl"
    assert parsed.year == 1969
    assert parsed.bitrate_kbps == 320


def test_parse_release_title_mp3_v0():
    parsed = parse_release_title("Pink Floyd - Animals (1977) [V0] [CD]")
    assert parsed.quality == AudioQuality.MP3_V0.value
    assert parsed.source == "CD"
    assert parsed.year == 1977


def test_parse_release_title_aac_256():
    parsed = parse_release_title("Taylor Swift - 1989 (2014) [AAC 256kbps] [WEB]")
    assert parsed.quality == AudioQuality.AAC_256.value
    assert parsed.source == "WEB"
    assert parsed.year == 2014
    assert parsed.bitrate_kbps == 256


def test_parse_release_title_mp3_192():
    parsed = parse_release_title("Coldplay - Parachutes (2000) [192kbps] [CD]")
    assert parsed.quality == AudioQuality.MP3_192.value
    assert parsed.bitrate_kbps == 192


def test_parse_release_title_mp3_v2():
    parsed = parse_release_title("Muse - Origin of Symmetry (2001) [V2] [CD]")
    assert parsed.quality == AudioQuality.MP3_V2.value


def test_parse_release_title_tags_and_sources():
    parsed = parse_release_title(
        "Nirvana - MTV Unplugged in New York (1994) [SACD] [FLAC 24bit] [Deluxe] [Live]"
    )
    assert parsed.quality == AudioQuality.FLAC_24BIT.value
    assert parsed.source == "SACD"
    assert "deluxe" in parsed.tags
    assert "live" in parsed.tags


def test_parse_release_title_scene_format():
    parsed = parse_release_title("Daft_Punk-Discovery-2001-FLAC-WEB")
    assert parsed.artist == "Daft Punk"
    assert "Discovery" in (parsed.album or "")
    assert parsed.quality == AudioQuality.FLAC_16BIT.value
    assert parsed.source == "WEB"


# =============================================================================
# 2. Release Evaluator Tests
# =============================================================================


def test_evaluate_release_acceptable_and_score():
    profile = QualityProfile(
        id="test-profile",
        name="Lossless FLAC",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=False, weight=800),
        ],
        preferred_tags=["cd", "remaster"],
        ignored_tags=["live", "bootleg"],
    )

    release = ParsedRelease(
        raw_title="Radiohead - OK Computer [FLAC] [CD] [Remastered]",
        artist="Radiohead",
        album="OK Computer",
        quality="FLAC 16bit",
        source="CD",
        tags=["remaster", "remastered"],
    )

    eval_result = evaluate_release(release, profile)
    assert eval_result.is_acceptable is True
    assert eval_result.meets_cutoff is True
    # Base 900 + 50 (cd) + 50 (remaster) = 1000
    assert eval_result.score >= 1000
    assert len(eval_result.rejection_reasons) == 0


def test_evaluate_release_disallowed_quality():
    profile = QualityProfile(
        id="test-profile",
        name="Lossless FLAC",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=False, weight=800),
        ],
    )

    release = ParsedRelease(
        raw_title="Artist - Song [MP3 320]",
        quality="MP3 320",
    )

    eval_result = evaluate_release(release, profile)
    assert eval_result.is_acceptable is False
    assert any("not allowed" in r for r in eval_result.rejection_reasons)


def test_evaluate_release_ignored_tags():
    profile = QualityProfile(
        id="test-profile",
        name="Lossless FLAC",
        cutoff="FLAC 16bit",
        items=[QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900)],
        ignored_tags=["live", "bootleg"],
    )

    release = ParsedRelease(
        raw_title="Nirvana - Live at Reading [FLAC]",
        quality="FLAC 16bit",
        tags=["live"],
    )

    eval_result = evaluate_release(release, profile)
    assert eval_result.is_acceptable is False
    assert any("Contains rejected keyword 'live'" in r for r in eval_result.rejection_reasons)


def test_evaluate_release_size_bounds():
    profile = QualityProfile(
        id="test-profile",
        name="Size Restricted",
        cutoff="FLAC 16bit",
        items=[QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900)],
        min_size_mb=10.0,
        max_size_mb=100.0,
    )

    release = ParsedRelease(
        raw_title="Artist - Album [FLAC]",
        quality="FLAC 16bit",
    )

    # 5 MB -> below minimum
    under_size = 5 * 1024 * 1024
    res_under = evaluate_release(release, profile, size_bytes=under_size)
    assert res_under.is_acceptable is False
    assert any("below minimum" in r for r in res_under.rejection_reasons)

    # 150 MB -> exceeds maximum
    over_size = 150 * 1024 * 1024
    res_over = evaluate_release(release, profile, size_bytes=over_size)
    assert res_over.is_acceptable is False
    assert any("exceeds maximum" in r for r in res_over.rejection_reasons)

    # 50 MB -> within bounds
    valid_size = 50 * 1024 * 1024
    res_valid = evaluate_release(release, profile, size_bytes=valid_size)
    assert res_valid.is_acceptable is True


def test_evaluate_release_cutoff():
    profile = QualityProfile(
        id="test-profile",
        name="Tiered Profile",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=800),
        ],
    )

    # Above cutoff (FLAC 24bit)
    rel_24 = ParsedRelease(raw_title="Album 24bit", quality="FLAC 24bit")
    res_24 = evaluate_release(rel_24, profile)
    assert res_24.meets_cutoff is True

    # At cutoff (FLAC 16bit)
    rel_16 = ParsedRelease(raw_title="Album 16bit", quality="FLAC 16bit")
    res_16 = evaluate_release(rel_16, profile)
    assert res_16.meets_cutoff is True

    # Below cutoff (MP3 320)
    rel_320 = ParsedRelease(raw_title="Album MP3 320", quality="MP3 320")
    res_320 = evaluate_release(rel_320, profile)
    assert res_320.meets_cutoff is False
    assert res_320.is_acceptable is True


# =============================================================================
# 3. Database Storage CRUD Tests
# =============================================================================


def test_migration_v10_seeds_defaults(test_db):
    profiles = test_db.list_quality_profiles()
    assert len(profiles) >= 3

    names = {p["name"] for p in profiles}
    assert "Lossless (FLAC)" in names
    assert "High Quality (Any)" in names
    assert "Standard MP3" in names

    default_profile = test_db.get_default_quality_profile()
    assert default_profile["name"] == "Lossless (FLAC)"
    assert default_profile["is_default"] is True


def test_upsert_and_get_quality_profile(test_db):
    new_profile = QualityProfile(
        id="custom-profile",
        name="Custom Audiophile",
        cutoff="FLAC 24bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=False, weight=500),
        ],
        preferred_tags=["vinyl", "sacd"],
        ignored_tags=["bootleg"],
        min_size_mb=20.0,
        max_size_mb=500.0,
        is_default=False,
    )

    saved = test_db.upsert_quality_profile(new_profile)
    assert saved["id"] == "custom-profile"
    assert saved["name"] == "Custom Audiophile"
    assert saved["min_size_mb"] == 20.0

    retrieved = test_db.get_quality_profile("custom-profile")
    assert retrieved is not None
    assert retrieved["cutoff"] == "FLAC 24bit"
    assert len(retrieved["items"]) == 2
    assert retrieved["preferred_tags"] == ["vinyl", "sacd"]


def test_upsert_switches_default_profile(test_db):
    # Upsert a new default profile
    p = QualityProfile(
        id="new-default",
        name="New Default Profile",
        cutoff="FLAC 16bit",
        items=[QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900)],
        is_default=True,
    )
    test_db.upsert_quality_profile(p)

    def_p = test_db.get_default_quality_profile()
    assert def_p["id"] == "new-default"

    # Verify old default profile is no longer default
    old_default = test_db.get_quality_profile("profile-lossless")
    assert old_default is not None
    assert old_default["is_default"] is False


def test_delete_quality_profile_protection(test_db):
    default_p = test_db.get_default_quality_profile()

    # Attempting to delete default profile must raise ValueError
    with pytest.raises(ValueError, match="Cannot delete the default quality profile"):
        test_db.delete_quality_profile(default_p["id"])

    # Create a non-default profile and delete it successfully
    non_def = QualityProfile(
        id="to-delete",
        name="Temporary Profile",
        cutoff="MP3 320",
        items=[QualityProfileItem(quality="MP3 320", allowed=True, weight=800)],
        is_default=False,
    )
    test_db.upsert_quality_profile(non_def)
    assert test_db.get_quality_profile("to-delete") is not None

    deleted = test_db.delete_quality_profile("to-delete")
    assert deleted is True
    assert test_db.get_quality_profile("to-delete") is None


# =============================================================================
# 4. REST API Endpoint Tests
# =============================================================================


def test_api_unauthenticated_and_rbac(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client

    # 1. Unauthenticated -> 401
    resp = client.get("/api/settings/quality-profiles")
    assert resp.status_code == 401

    # 2. Non-admin user -> 403
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    resp = client.get("/api/settings/quality-profiles", headers=alice_headers)
    assert resp.status_code == 403

    # 3. Admin user -> 200
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    resp = client.get("/api/settings/quality-profiles", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 3


def test_api_get_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    resp = client.get("/api/settings/quality-profiles/profile-lossless", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Lossless (FLAC)"

    resp_404 = client.get("/api/settings/quality-profiles/non-existent-id", headers=admin_headers)
    assert resp_404.status_code == 404


def test_api_create_and_delete_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    payload = {
        "id": "api-profile-1",
        "name": "API Created Profile",
        "cutoff": "MP3 320",
        "items": [
            {"quality": "MP3 320", "allowed": True, "weight": 800},
            {"quality": "MP3 192", "allowed": False, "weight": 500},
        ],
        "preferred_tags": ["web"],
        "ignored_tags": ["live"],
        "min_size_mb": 5.0,
        "max_size_mb": 200.0,
        "is_default": False,
    }

    # Create profile
    resp = client.post("/api/settings/quality-profiles", json=payload, headers=admin_headers)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["name"] == "API Created Profile"
    assert res_data["cutoff"] == "MP3 320"

    # Attempt to delete default profile -> 400
    del_default_resp = client.delete(
        "/api/settings/quality-profiles/profile-lossless", headers=admin_headers
    )
    assert del_default_resp.status_code == 400
    assert "Cannot delete the default quality profile" in del_default_resp.json()["detail"]

    # Delete newly created profile -> 200
    del_resp = client.delete("/api/settings/quality-profiles/api-profile-1", headers=admin_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"


def test_api_evaluate_title(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Evaluate title with default profile
    payload = {
        "title": "Daft Punk - Discovery (2001) [FLAC 24-96] [WEB]",
        "size_bytes": 500 * 1024 * 1024,
    }
    resp = client.post(
        "/api/settings/quality-profiles/evaluate", json=payload, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "parsed" in body
    assert "evaluation" in body

    assert body["parsed"]["quality"] == "FLAC 24bit"
    assert body["parsed"]["source"] == "WEB"
    assert body["evaluation"]["is_acceptable"] is True
    assert body["evaluation"]["meets_cutoff"] is True
    assert body["evaluation"]["score"] > 0


def test_api_evaluate_title_rejection_and_custom_profile(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Title containing ignored tag (e.g. "live" or "bootleg")
    payload = {
        "title": "Nirvana - Live at Reading (1992) [FLAC] [CD]",
    }
    resp = client.post(
        "/api/settings/quality-profiles/evaluate", json=payload, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluation"]["is_acceptable"] is False
    # v49 folds ignored tags into a per-profile release profile; the rejection now names it.
    assert any("ignores term" in r and "live" in r for r in body["evaluation"]["rejection_reasons"])
    assert body["breakdown"]["rejections"][0]["code"] == "release_profile_ignored"

    # 2. Evaluation against explicit custom profile
    payload_custom = {
        "title": "The Beatles - Abbey Road (1969) [MP3 320] [CD]",
        "profile_id": "profile-standard-mp3",
    }
    resp_custom = client.post(
        "/api/settings/quality-profiles/evaluate", json=payload_custom, headers=admin_headers
    )
    assert resp_custom.status_code == 200
    body_custom = resp_custom.json()
    assert body_custom["evaluation"]["is_acceptable"] is True
    assert body_custom["evaluation"]["meets_cutoff"] is True


# =============================================================================
# 5. Frontend HTML & JavaScript Integration Tests
# =============================================================================


def test_frontend_quality_profiles_ui(app_and_client):
    _, client = app_and_client

    # Verify root index.html serves quality profiles sub-tab and modal
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "settingsSubTab = 'profiles'" in html
    assert "settingsSubTab === 'profiles'" in html
    assert "Quality Profiles" in html
    assert "Live Release Title Tester" in html
    assert "isProfileModalOpen" in html
    assert "openAddProfileModal()" in html
    assert "openEditProfileModal" in html
    assert "testReleaseTitle()" in html

    # Verify static/app.js serves quality profiles state and methods
    resp_js = client.get("/static/app.js")
    assert resp_js.status_code == 200
    js = resp_js.text

    assert "qualityProfiles:" in js
    assert "isProfileModalOpen:" in js
    assert "loadQualityProfiles()" in js
    assert "openAddProfileModal()" in js
    assert "openEditProfileModal" in js
    assert "saveQualityProfile()" in js
    assert "deleteQualityProfile" in js
    assert "testReleaseTitle()" in js
    assert "/api/settings/quality-profiles" in js

