"""Tests for ServerAccountStoreMixin and gateway role_guard checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackseerr.role_guard import core_like_reasons
from trackseerr.storage import Database


def test_crud_server_accounts(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "accounts.db"))
    try:
        db.upsert_user("u1", "alice", "a@x")
        db.upsert_user("u2", "bob", "b@x")
        srv1 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome 1", "url": "http://nav1:4533"})
        srv2 = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome 2", "url": "http://nav2:4533"})

        assert db.get_server_account("u1", srv1["id"]) is None
        assert db.list_server_accounts_for_user("u1") == []

        db.set_server_account("u1", srv1["id"], "alice_sub", "sealed-token-1")
        db.set_server_account("u1", srv2["id"], "alice_sub2", "sealed-token-2")
        db.set_server_account("u2", srv1["id"], "bob_sub", "sealed-bob")

        got = db.get_server_account("u1", srv1["id"])
        assert got is not None
        assert got["username"] == "alice_sub"
        assert got["secret_sealed"] == "sealed-token-1"
        assert got["updated_at"]

        listed = db.list_server_accounts_for_user("u1")
        assert len(listed) == 2
        assert {item["server_id"] for item in listed} == {srv1["id"], srv2["id"]}
        assert all("secret_sealed" not in item for item in listed)

        # Upsert replaces username and secret_sealed
        db.set_server_account("u1", srv1["id"], "alice_new", "sealed-token-new")
        updated = db.get_server_account("u1", srv1["id"])
        assert updated is not None
        assert updated["username"] == "alice_new"
        assert updated["secret_sealed"] == "sealed-token-new"

        assert db.delete_server_account("u1", srv1["id"]) is True
        assert db.delete_server_account("u1", srv1["id"]) is False
        assert db.get_server_account("u1", srv1["id"]) is None
        assert len(db.list_server_accounts_for_user("u2")) == 1
    finally:
        db.close()


def test_set_server_account_rejects_blank_fields(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "blank.db"))
    try:
        db.upsert_user("u1", "alice", "a@x")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        with pytest.raises(ValueError):
            db.set_server_account("u1", srv["id"], "  ", "sealed")
        with pytest.raises(ValueError):
            db.set_server_account("u1", srv["id"], "alice", "   ")
    finally:
        db.close()


def test_role_guard_detects_user_server_accounts(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "guard.db"))
    try:
        assert "per-user media server logins" not in core_like_reasons(db.conn)
        db.upsert_user("u1", "alice", "a@x")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.set_server_account("u1", srv["id"], "alice", "sealed-token")
        assert "per-user media server logins" in core_like_reasons(db.conn)
    finally:
        db.close()
