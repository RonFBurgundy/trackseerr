"""Strict-mode backstop for the release-profile delete route (wave 2b); formats are covered by test_quality_catalog_api.py."""

from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401


def test_release_profile_delete(client, admin, alice):
    created = ok(client.post("/api/settings/release-profiles", json={"name": "No live", "ignored": ["live"]}, headers=admin))
    pid = created["id"]
    assert client.delete(f"/api/settings/release-profiles/{pid}", headers=alice).status_code == 403
    assert ok(client.delete(f"/api/settings/release-profiles/{pid}", headers=admin)) == {"status": "deleted", "id": pid}
    assert client.delete(f"/api/settings/release-profiles/{pid}", headers=admin).status_code == 404
