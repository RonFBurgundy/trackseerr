"""Strict-mode backstop for the typed blocklist routes of ``/api/acquisition`` (wave 2b).

Search, grab and pending are covered by test_interactive_search.py and test_delay_profiles.py.
"""

from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401


def test_blocklist_list_and_remove(client, db, admin, alice):
    db.add_to_blocklist(
        "Radiohead - OK Computer [FLAC]", artist="Radiohead", album="OK Computer", release_guid="guid-1",
        info_hash="ABCDEF", protocol="torrent", indexer="Redacted", reason="Failed import",
    )
    db.add_to_blocklist("Bare title")
    rows = ok(client.get("/api/acquisition/blocklist", headers=admin))
    assert len(rows) == 2
    full = next(r for r in rows if r["artist"] == "Radiohead")
    assert full["info_hash"] == "abcdef" and full["protocol"] == "torrent" and full["created_at"]
    bare = next(r for r in rows if r["source_title"] == "Bare title")
    assert bare["artist"] is None and bare["reason"] is None
    assert client.get("/api/acquisition/blocklist", headers=alice).status_code == 403
    body = ok(client.delete(f"/api/acquisition/blocklist/{full['id']}", headers=admin))
    assert body["success"] is True and full["id"] in body["message"]
    assert client.delete(f"/api/acquisition/blocklist/{full['id']}", headers=admin).status_code == 404
