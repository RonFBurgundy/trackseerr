"""Tests for PlaylistStoreMixin media-server excludes and per-server sync state."""

from trackseerr.storage import Database


def _seed_playlist_and_servers(db: Database) -> tuple[str, str, str]:
    db.upsert_playlist("pl_1", "Mix One", service="spotify")
    db.upsert_playlist("pl_2", "Mix Two", service="spotify")
    s1 = db.insert_media_server_row({"kind": "plex", "name": "Plex", "url": "http://plex:32400"})
    s2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
    s3 = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"})
    return s1["id"], s2["id"], s3["id"]


def test_excludes_round_trip_and_replace() -> None:
    db = Database(":memory:")
    try:
        s1, s2, s3 = _seed_playlist_and_servers(db)
        assert db.get_playlist_server_excludes("pl_1") == set()

        db.set_playlist_server_excludes("pl_1", [s1, s2, s1])
        db.set_playlist_server_excludes("pl_2", [s3])
        assert db.get_playlist_server_excludes("pl_1") == {s1, s2}
        assert db.get_playlist_server_excludes("pl_2") == {s3}

        db.set_playlist_server_excludes("pl_1", [s2, s3])
        assert db.get_playlist_server_excludes("pl_1") == {s2, s3}
        assert db.get_playlist_server_excludes("pl_2") == {s3}

        db.set_playlist_server_excludes("pl_1", [])
        assert db.get_playlist_server_excludes("pl_1") == set()
        assert db.get_playlist_server_excludes("pl_2") == {s3}
    finally:
        db.close()


def test_state_upsert_and_list() -> None:
    db = Database(":memory:")
    try:
        s1, s2, _ = _seed_playlist_and_servers(db)
        first_id, second_id = sorted([s1, s2])

        db.record_playlist_server_state("pl_1", second_id, "failed", matched=1, missing=4, error="timeout")
        db.record_playlist_server_state("pl_1", first_id, "success", matched=5, missing=0)

        rows = db.list_playlist_server_state("pl_1")
        assert [r["server_id"] for r in rows] == [first_id, second_id]
        assert rows[0]["status"] == "success"
        assert rows[0]["matched"] == 5
        assert rows[0]["missing"] == 0
        assert rows[0]["error"] == ""
        assert rows[0]["synced_at"]

        assert rows[1]["status"] == "failed"
        assert rows[1]["matched"] == 1
        assert rows[1]["missing"] == 4
        assert rows[1]["error"] == "timeout"

        # Upsert replaces the existing row rather than inserting a duplicate.
        db.record_playlist_server_state("pl_1", second_id, "success", matched=5, missing=0, error="")
        updated = db.list_playlist_server_state("pl_1")
        assert len(updated) == 2
        assert updated[1]["status"] == "success"
        assert updated[1]["matched"] == 5
        assert updated[1]["missing"] == 0
        assert updated[1]["error"] == ""
    finally:
        db.close()


def test_state_error_truncated_to_500_chars() -> None:
    db = Database(":memory:")
    try:
        s1, _, _ = _seed_playlist_and_servers(db)
        long_err = "x" * 750
        db.record_playlist_server_state("pl_1", s1, "failed", error=long_err)
        rows = db.list_playlist_server_state("pl_1")
        assert len(rows) == 1
        assert rows[0]["error"] == "x" * 500
    finally:
        db.close()
