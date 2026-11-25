"""Tests for MediaServerStoreMixin CRUD, validation, login uniqueness, and path mapping."""

import pytest

from trackseerr.storage import Database


def test_insert_and_list_round_trip() -> None:
    db = Database(":memory:")
    try:
        created = db.insert_media_server_row(
            {
                "kind": "subsonic",
                "name": "Navidrome",
                "url": "http://navidrome:4533",
                "username": "alice",
                "password": "secret",
                "verify_ssl": False,
                "use_scrobbles": 0,
            }
        )
        assert created["id"]
        assert created["kind"] == "subsonic"
        assert created["source"] == "settings"
        assert created["name"] == "Navidrome"
        assert created["url"] == "http://navidrome:4533"
        assert created["username"] == "alice"
        assert created["password"] == "secret"
        assert created["verify_ssl"] is False
        assert created["enabled"] is True
        assert created["use_refresh"] is True
        assert created["use_playlists"] is True
        assert created["use_users"] is True
        assert created["use_scrobbles"] is False
        assert created["is_login"] is False
        assert created["path_mapping"] is None
        assert "path_mapping_json" not in created
        assert created["sort_order"] == 0

        listed = db.list_media_server_rows()
        assert listed == [created]
        assert db.get_media_server_row(created["id"]) == created
    finally:
        db.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "emby", "name": "Emby"},
        {"kind": "", "name": "Empty"},
        {"kind": "plex", "name": ""},
        {"kind": "plex", "name": "   "},
        {"kind": "plex", "name": "Plex", "source": "cli"},
    ],
)
def test_insert_rejects_unknown_kind_and_blank_name(payload: dict) -> None:
    db = Database(":memory:")
    try:
        with pytest.raises(ValueError):
            db.insert_media_server_row(payload)
    finally:
        db.close()


def test_env_row_keeps_given_id() -> None:
    db = Database(":memory:")
    try:
        row = db.insert_media_server_row(
            {"id": "env-plex", "kind": "plex", "source": "env", "name": "Plex"}
        )
        assert row["id"] == "env-plex"
        assert row["source"] == "env"
        assert row["kind"] == "plex"
        assert db.get_media_server_row("env-plex") == row
    finally:
        db.close()


def test_update_changes_only_editable_keys() -> None:
    db = Database(":memory:")
    try:
        row = db.insert_media_server_row(
            {"kind": "plex", "name": "Home Plex", "url": "http://plex:32400"}
        )
        updated = db.update_media_server_row(
            row["id"],
            {
                "name": "  Updated Plex  ",
                "kind": "subsonic",
                "source": "env",
                "is_login": True,
                "unknown_col": "ignored",
                "enabled": False,
            },
        )
        assert updated is not None
        assert updated["name"] == "Updated Plex"
        assert updated["kind"] == "plex"
        assert updated["source"] == "settings"
        assert updated["is_login"] is False
        assert updated["enabled"] is False

        with pytest.raises(ValueError):
            db.update_media_server_row(row["id"], {"name": "   "})

        assert db.update_media_server_row("does-not-exist", {"name": "Nope"}) is None
    finally:
        db.close()


def test_delete() -> None:
    db = Database(":memory:")
    try:
        row = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin"})
        assert db.delete_media_server_row(row["id"]) is True
        assert db.get_media_server_row(row["id"]) is None
        assert db.delete_media_server_row(row["id"]) is False
    finally:
        db.close()


def test_login_server_is_unique_and_plex_only() -> None:
    db = Database(":memory:")
    try:
        plex_a = db.insert_media_server_row({"kind": "plex", "name": "Plex A"})
        plex_b = db.insert_media_server_row({"kind": "plex", "name": "Plex B"})
        sub = db.insert_media_server_row({"kind": "subsonic", "name": "Subsonic"})

        db.set_login_media_server(plex_a["id"])
        assert db.get_media_server_row(plex_a["id"])["is_login"] is True
        assert db.get_media_server_row(plex_b["id"])["is_login"] is False

        db.set_login_media_server(plex_b["id"])
        assert db.get_media_server_row(plex_a["id"])["is_login"] is False
        assert db.get_media_server_row(plex_b["id"])["is_login"] is True

        with pytest.raises(ValueError):
            db.set_login_media_server(sub["id"])
        assert db.get_media_server_row(plex_b["id"])["is_login"] is True
        assert db.get_media_server_row(sub["id"])["is_login"] is False

        with pytest.raises(ValueError):
            db.set_login_media_server("missing-id")
        assert db.get_media_server_row(plex_b["id"])["is_login"] is True

        db.set_login_media_server(None)
        assert db.get_media_server_row(plex_a["id"])["is_login"] is False
        assert db.get_media_server_row(plex_b["id"])["is_login"] is False
    finally:
        db.close()


def test_path_mapping_round_trip_and_bad_json() -> None:
    db = Database(":memory:")
    try:
        row = db.insert_media_server_row({"kind": "plex", "name": "Plex"})
        mapping = {
            "server_prefix": "/music",
            "local_prefix": "/data/media/music",
            "auto": True,
            "server_kind": "plex",
        }
        db.set_media_server_row_path_mapping(row["id"], mapping)
        assert db.get_media_server_row(row["id"])["path_mapping"] == mapping

        db.set_media_server_row_path_mapping(row["id"], None)
        assert db.get_media_server_row(row["id"])["path_mapping"] is None

        db.conn.execute(
            "UPDATE media_servers SET path_mapping_json = ? WHERE id = ?",
            ("{not-valid-json", row["id"]),
        )
        db.conn.commit()
        fetched = db.get_media_server_row(row["id"])
        assert fetched is not None
        assert fetched["path_mapping"] is None
    finally:
        db.close()


def test_list_order_follows_sort_order() -> None:
    db = Database(":memory:")
    try:
        second = db.insert_media_server_row({"kind": "subsonic", "name": "Second", "sort_order": 20})
        first = db.insert_media_server_row({"kind": "plex", "name": "First", "sort_order": 10})
        third = db.insert_media_server_row({"kind": "jellyfin", "name": "Third", "sort_order": 30})
        ids = [r["id"] for r in db.list_media_server_rows()]
        assert ids == [first["id"], second["id"], third["id"]]
    finally:
        db.close()
