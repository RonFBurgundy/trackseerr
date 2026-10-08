"""Tests for the changelog parser, caching, database migration, and system API routes."""

import logging
import os
import sqlite3
import time
from pathlib import Path

import pytest

import plex_playlist_sync
from plex_playlist_sync.changelog import (
    clear_changelog_cache,
    get_build_info,
    get_changelog,
    get_latest_release,
    parse_changelog,
    strip_inline_markdown,
)
from plex_playlist_sync.storage import SCHEMA_VERSION, Database
from tests.test_system_status import (  # noqa: F401
    app_and_client,
    create_auth_cookies,
    secret_key,
    seeded_users,
    test_config,
    test_db,
)


@pytest.fixture(autouse=True)
def _reset_changelog_cache():
    """Ensure cache is reset between tests."""
    clear_changelog_cache()
    yield
    clear_changelog_cache()


# =============================================================================
# 1. Parser & Markdown Stripping Tests
# =============================================================================


def test_strip_inline_markdown():
    """Inline markdown elements are stripped to clean plain text."""
    assert strip_inline_markdown("**bold**") == "bold"
    assert strip_inline_markdown("__bold__") == "bold"
    assert strip_inline_markdown("*italic*") == "italic"
    assert strip_inline_markdown("_italic_") == "italic"
    assert strip_inline_markdown("***bold and italic***") == "bold and italic"
    assert strip_inline_markdown("`code`") == "code"
    assert strip_inline_markdown("[link text](https://example.com/path)") == "link text"
    assert strip_inline_markdown("![image alt](https://example.com/img.png)") == "image alt"
    assert strip_inline_markdown("~~strikethrough~~") == "strikethrough"
    assert (
        strip_inline_markdown("A **feature** with `x` and [docs](https://trackseerr.tv).")
        == "A feature with x and docs."
    )


def test_parser_on_fixture_markdown(tmp_path):
    """Parses Keep a Changelog fixture: Unreleased + versions, preamble ignored,

    inline markdown stripped, empty sections preserved, and malformed headings tolerated.
    """
    raw_md = """# TrackSeerr Changelog
This is preamble text before any release heading.
- Bullet in preamble that must be ignored.
## Random Heading in preamble

## [Unreleased]

### Added

- Backup and restore: scheduled and on-demand backups with [System page](/system) UI.
- Changelog: a "What's new" section on the System page and **one-time** notice.
- Multi-line item:
  continued on next line with `inline code` and *emphasis*.

### Fixed

- Fixed playlist sync race condition.

### Removed

## [1.2.0] - 2026-10-01

Description paragraph before sections for v1.2.0.

### Added

- Added support for new metadata provider.

## Malformed Heading Without Brackets That Should Be Tolerated

### Ignored Subsection

## [1.1.0]

### Changed

- Updated default retention policy.
"""
    fixture_file = tmp_path / "FIXTURE_CHANGELOG.md"
    fixture_file.write_text(raw_md, encoding="utf-8")

    releases = get_changelog(fixture_file)
    assert len(releases) == 3

    # Release 0: Unreleased
    rel_unreleased = releases[0]
    assert rel_unreleased["version"] == "Unreleased"
    assert rel_unreleased["unreleased"] is True
    assert rel_unreleased["date_note"] is None
    assert len(rel_unreleased["sections"]) == 3

    added_sec = rel_unreleased["sections"][0]
    assert added_sec["title"] == "Added"
    assert len(added_sec["items"]) == 3
    assert added_sec["items"][0] == "Backup and restore: scheduled and on-demand backups with System page UI."
    assert added_sec["items"][1] == "Changelog: a \"What's new\" section on the System page and one-time notice."
    assert (
        added_sec["items"][2]
        == "Multi-line item: continued on next line with inline code and emphasis."
    )

    fixed_sec = rel_unreleased["sections"][1]
    assert fixed_sec["title"] == "Fixed"
    assert fixed_sec["items"] == ["Fixed playlist sync race condition."]

    removed_sec = rel_unreleased["sections"][2]
    assert removed_sec["title"] == "Removed"
    assert removed_sec["items"] == []  # empty section tolerated

    # Release 1: 1.2.0
    rel_120 = releases[1]
    assert rel_120["version"] == "1.2.0"
    assert rel_120["unreleased"] is False
    assert rel_120["date_note"] == "2026-10-01"
    assert len(rel_120["sections"]) == 1
    assert rel_120["sections"][0]["title"] == "Added"
    assert rel_120["sections"][0]["items"] == ["Added support for new metadata provider."]

    # Release 2: 1.1.0 (after malformed heading)
    rel_110 = releases[2]
    assert rel_110["version"] == "1.1.0"
    assert rel_110["unreleased"] is False
    assert rel_110["date_note"] is None
    assert len(rel_110["sections"]) == 1
    assert rel_110["sections"][0]["title"] == "Changed"
    assert rel_110["sections"][0]["items"] == ["Updated default retention policy."]


def test_real_repo_changelog():
    """The real repo CHANGELOG.md parses to >= 1 release with items."""
    releases = get_changelog()
    assert len(releases) >= 2

    # Check for release 1.0.0
    v1_rel = next((r for r in releases if r["version"] == "1.0.0"), None)
    assert v1_rel is not None
    assert v1_rel["unreleased"] is False
    assert len(v1_rel["sections"]) >= 1

    first_sec = v1_rel["sections"][0]
    assert first_sec["title"] == "Added"
    assert len(first_sec["items"]) >= 10


# =============================================================================
# 2. Missing, Unreadable Files & mtime Cache Invalidation
# =============================================================================


def test_missing_and_unreadable_file(tmp_path, caplog):
    """Missing or unreadable file returns empty list and logs a single warning."""
    missing_path = tmp_path / "NONEXISTENT_CHANGELOG.md"

    with caplog.at_level(logging.WARNING):
        result1 = get_changelog(missing_path)
    assert result1 == []
    assert any("Changelog file missing or unreadable" in r.message for r in caplog.records)

    # Calling again should return [] without duplicate warning
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        result2 = get_changelog(missing_path)
    assert result2 == []
    assert not any("Changelog file missing or unreadable" in r.message for r in caplog.records)


def test_mtime_cache_invalidation(tmp_path):
    """Parsed output is cached keyed by mtime; modifying file and mtime invalidates cache."""
    test_file = tmp_path / "CACHE_TEST_CHANGELOG.md"
    test_file.write_text(
        "## [1.0.0]\n### Added\n- Feature 1\n",
        encoding="utf-8",
    )

    r1 = get_changelog(test_file)
    assert r1[0]["sections"][0]["items"] == ["Feature 1"]

    # Call again: should return cached copy
    r2 = get_changelog(test_file)
    assert r2 == r1

    # Modify file and update mtime forward
    new_mtime = test_file.stat().st_mtime + 5.0
    test_file.write_text(
        "## [1.0.0]\n### Added\n- Feature 1\n- Feature 2\n",
        encoding="utf-8",
    )
    os.utime(test_file, (new_mtime, new_mtime))

    r3 = get_changelog(test_file)
    assert r3[0]["sections"][0]["items"] == ["Feature 1", "Feature 2"]


# =============================================================================
# 3. Latest Release Selection Rules & Build Info
# =============================================================================


def test_latest_release_selection_rules():
    """Rules: matching current version, else first non-Unreleased entry, else first entry."""
    releases = [
        {"version": "Unreleased", "unreleased": True, "sections": []},
        {"version": "1.2.0", "unreleased": False, "sections": []},
        {"version": "1.1.0", "unreleased": False, "sections": []},
    ]

    # 1. Matching current version
    assert get_latest_release(releases, "1.1.0") == releases[2]
    assert get_latest_release(releases, "v1.1.0") == releases[2]
    assert get_latest_release(releases, "1.2.0") == releases[1]

    # 2. Current version not found -> first non-Unreleased entry
    assert get_latest_release(releases, "2.0.0") == releases[1]

    # 3. Only Unreleased entry exists -> first entry
    only_unreleased = [{"version": "Unreleased", "unreleased": True, "sections": []}]
    assert get_latest_release(only_unreleased, "1.0.0") == only_unreleased[0]

    # 4. Empty list -> None
    assert get_latest_release([], "1.0.0") is None


def test_get_build_info(monkeypatch):
    """Build info returns plex_playlist_sync.__version__ and short commit or None."""
    monkeypatch.delenv("TRACKSEERR_COMMIT", raising=False)
    v, commit = get_build_info()
    assert v == plex_playlist_sync.__version__
    assert commit is None

    monkeypatch.setenv("TRACKSEERR_COMMIT", "abcdef123456789")
    v2, commit2 = get_build_info()
    assert v2 == plex_playlist_sync.__version__
    assert commit2 == "abcdef1"


# =============================================================================
# 4. Database Migration & Accessor Tests
# =============================================================================


def test_database_migration_and_accessors(test_db):
    """Schema version 68 adds last_seen_changelog_version to users table with Database accessors."""
    assert SCHEMA_VERSION >= 68
    cols = {r[1] for r in test_db.conn.execute("PRAGMA table_info(users)").fetchall()}
    assert "last_seen_changelog_version" in cols

    # Accessors on nonexistent user
    assert test_db.get_last_seen_changelog_version("nonexistent") is None

    # Upsert user and test accessor methods
    user = test_db.upsert_user("u-test-1", "testuser", is_admin=True)
    assert test_db.get_last_seen_changelog_version(user["id"]) is None

    test_db.set_last_seen_changelog_version(user["id"], "1.0.0")
    assert test_db.get_last_seen_changelog_version(user["id"]) == "1.0.0"

    test_db.set_last_seen_changelog_version(user["id"], "1.1.0")
    assert test_db.get_last_seen_changelog_version(user["id"]) == "1.1.0"


def test_migration_applies_on_existing_database(tmp_path):
    """Migration v68 cleanly applies to an existing SQLite DB migrating up."""
    db_file = tmp_path / "existing_v67.db"
    # Create a fully migrated DB first
    db = Database(str(db_file))
    db.close()

    # Revert migration 68 and drop the new column
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE users DROP COLUMN last_seen_changelog_version")
    conn.execute("DELETE FROM schema_migrations WHERE version >= ?", (SCHEMA_VERSION,))
    conn.commit()
    conn.close()

    # Opening with Database runs _migrate() applying migration 68
    db = Database(str(db_file))
    try:
        top = db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert top == SCHEMA_VERSION

        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(users)").fetchall()}
        assert "last_seen_changelog_version" in cols

        # Check accessors on newly migrated DB
        user = db.upsert_user("u-migrated", "migrated_admin", is_admin=True)
        assert db.get_last_seen_changelog_version(user["id"]) is None
        db.set_last_seen_changelog_version(user["id"], "1.0.0")
        assert db.get_last_seen_changelog_version(user["id"]) == "1.0.0"
    finally:
        db.close()


# =============================================================================
# 5. Endpoint Auth & Query Param Tests
# =============================================================================


def test_endpoint_auth(app_and_client, seeded_users, secret_key, test_db):
    """GET /system/changelog is accessible to any authenticated user;

    unseen and seen routes require admin privileges.
    """
    _, client = app_and_client
    alice_cookies = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    # 1. GET /api/system/changelog
    assert client.get("/api/system/changelog").status_code == 401
    res_alice = client.get("/api/system/changelog", cookies=alice_cookies)
    assert res_alice.status_code == 200
    assert "releases" in res_alice.json()
    assert "version" in res_alice.json()
    res_admin = client.get("/api/system/changelog", cookies=admin_cookies)
    assert res_admin.status_code == 200

    # 2. GET /api/system/changelog/unseen
    assert client.get("/api/system/changelog/unseen").status_code == 401
    assert client.get("/api/system/changelog/unseen", cookies=alice_cookies).status_code == 403
    assert client.get("/api/system/changelog/unseen", cookies=admin_cookies).status_code == 200

    # 3. POST /api/system/changelog/seen
    assert client.post("/api/system/changelog/seen").status_code == 401
    assert client.post("/api/system/changelog/seen", cookies=alice_cookies).status_code == 403
    assert client.post("/api/system/changelog/seen", cookies=admin_cookies).status_code == 200


def test_get_changelog_limit_query_param(app_and_client, seeded_users, secret_key, test_db):
    """Optional ?limit= restricts number of returned releases while preserving latest."""
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["alice"], secret_key)

    resp_all = client.get("/api/system/changelog", cookies=cookies)
    assert resp_all.status_code == 200
    all_releases = resp_all.json()["releases"]
    assert len(all_releases) >= 2

    resp_limit1 = client.get("/api/system/changelog?limit=1", cookies=cookies)
    assert resp_limit1.status_code == 200
    body = resp_limit1.json()
    assert len(body["releases"]) == 1
    assert body["latest"] is not None


# =============================================================================
# 6. Unseen / Seen Flow and Per-User Isolation Tests
# =============================================================================


def test_unseen_seen_flow_and_user_isolation(app_and_client, secret_key, test_db, tmp_path, monkeypatch):
    """Tests the null->silent-record rule, upgrade detection, seen acknowledgement,

    and per-user isolation between multiple admins.
    """
    _, client = app_and_client

    # Create test changelog with versions 1.0.0 and 0.9.0
    test_md = """## [1.0.0] - first TrackSeerr release
### Added
- Feature 1.0.0

## [0.9.0] - initial beta
### Added
- Feature 0.9.0
"""
    changelog_path = tmp_path / "TEST_CHANGELOG.md"
    changelog_path.write_text(test_md, encoding="utf-8")
    monkeypatch.setenv("TRACKSEERR_CHANGELOG_PATH", str(changelog_path))

    # Seed two admin users
    admin1 = test_db.upsert_user("admin-1", "admin1", is_admin=True)
    admin2 = test_db.upsert_user("admin-2", "admin2", is_admin=True)
    admin1_cookies = create_auth_cookies(test_db, admin1, secret_key)
    admin2_cookies = create_auth_cookies(test_db, admin2, secret_key)

    # Rule: Fresh installs (last_seen is null) record current version silently and return show: false
    assert test_db.get_last_seen_changelog_version(admin1["id"]) is None
    resp1 = client.get("/api/system/changelog/unseen", cookies=admin1_cookies)
    assert resp1.status_code == 200
    assert resp1.json() == {"show": False, "release": None}
    assert test_db.get_last_seen_changelog_version(admin1["id"]) == plex_playlist_sync.__version__

    # Calling again remains show: false
    resp1_again = client.get("/api/system/changelog/unseen", cookies=admin1_cookies)
    assert resp1_again.json() == {"show": False, "release": None}

    # Simulate upgrade: admin 1's last-seen was an older version (0.9.0)
    test_db.set_last_seen_changelog_version(admin1["id"], "0.9.0")
    resp_upgrade = client.get("/api/system/changelog/unseen", cookies=admin1_cookies)
    assert resp_upgrade.status_code == 200
    data = resp_upgrade.json()
    assert data["show"] is True
    assert data["release"]["version"] == "1.0.0"

    # Admin 1 calls POST /system/changelog/seen
    resp_seen = client.post("/api/system/changelog/seen", cookies=admin1_cookies)
    assert resp_seen.status_code == 200
    assert resp_seen.json()["success"] is True
    assert test_db.get_last_seen_changelog_version(admin1["id"]) == plex_playlist_sync.__version__

    # Next check for Admin 1 returns show: false
    resp_after_seen = client.get("/api/system/changelog/unseen", cookies=admin1_cookies)
    assert resp_after_seen.json() == {"show": False, "release": None}

    # Admin 2 isolation: Admin 2 was unaffected by Admin 1's actions
    assert test_db.get_last_seen_changelog_version(admin2["id"]) is None
    test_db.set_last_seen_changelog_version(admin2["id"], "0.9.0")
    # Admin 2 sees the popup because their last-seen was 0.9.0
    resp_admin2 = client.get("/api/system/changelog/unseen", cookies=admin2_cookies)
    assert resp_admin2.json()["show"] is True

    # Rule: If current version does NOT have a changelog entry, show is false
    monkeypatch.setattr(plex_playlist_sync, "__version__", "9.9.9")
    test_db.set_last_seen_changelog_version(admin1["id"], "1.0.0")
    resp_no_entry = client.get("/api/system/changelog/unseen", cookies=admin1_cookies)
    assert resp_no_entry.json() == {"show": False, "release": None}
