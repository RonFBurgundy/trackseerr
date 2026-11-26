"""Tests for Navidrome native REST admin client, capabilities detection, and user linking."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trackseerr.api.dependencies import get_db, get_media_client, require_admin, require_media_server
from trackseerr.api.routes.users import router as users_router
from trackseerr.media_servers import (
    MediaServerAuthError,
    NavidromeAdmin,
    ServerUser,
    SubsonicMediaServer,
    import_server_users,
    registry,
)
from trackseerr.storage import Database
from tests.subsonic_fake import FakePlaylist, FakeSubsonic, FakeUser, default_state


class FakeNavidrome(FakeSubsonic):
    """Fake Subsonic + Navidrome native API (``/auth/login``, ``/api/user``, ``/api/playlist/{id}``)."""

    def __init__(
        self,
        songs: list[tuple[str, str, str]] | None = None,
        *,
        password: str = "pw",
        ignore_owner_change: bool = False,
    ) -> None:
        super().__init__(default_state(songs or [], password=password))
        self.user_ids: dict[str, str] = {"admin": "nd-admin", "kid": "nd-kid"}
        self.user_emails: dict[str, str] = {"admin": "admin@nav.local", "kid": "kid@nav.local"}
        self.active_token: str = "tok-1"
        self.token_counter: int = 1
        self.ignore_owner_change = ignore_owner_change
        self.native_calls: list[tuple[str, str]] = []
        self.reject_next_token: bool = False
        self.put_bodies: list[dict[str, Any]] = []
        self.playlist_comments: dict[str, str] = {}
        self.playlist_public: dict[str, bool] = {}

    def add_user(self, uid: str, username: str, *, admin: bool = False, password: str = "pw", email: str = "") -> None:
        self.state.users[username] = FakeUser(username, admin=admin, password=password)
        self.user_ids[username] = uid
        self.user_emails[username] = email

    def _envelope(self, status: str, **payload: Any) -> httpx.Response:  # type: ignore[override]
        body = {
            "subsonic-response": {
                "status": status,
                "version": "1.16.1",
                "type": "navidrome",
                "serverVersion": "0.53.0",
                **payload,
            }
        }
        return httpx.Response(200, json=body)

    def _own(self, user: FakeUser, playlist_id: str) -> FakePlaylist:
        p = self.state.playlists.get(playlist_id)
        if p is None:
            return super()._own(user, playlist_id)
        if user.admin or p.owner == user.username:
            return p
        return super()._own(user, playlist_id)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return self._handle_login(request)
        if path.startswith("/api/"):
            return self._handle_native_api(request)
        return super().handle(request)

    def _handle_login(self, request: httpx.Request) -> httpx.Response:
        self.native_calls.append((request.method, request.url.path))
        data = json.loads(request.content.decode() or "{}")
        user = self.state.users.get(data.get("username", ""))
        if user is None or data.get("password") != user.password:
            return httpx.Response(401, json={"error": "Invalid username or password"})
        self.token_counter += 1
        self.active_token = f"tok-{self.token_counter}"
        return httpx.Response(
            200,
            json={
                "id": self.user_ids.get(user.username, user.username),
                "username": user.username,
                "name": user.username,
                "isAdmin": user.admin,
                "token": self.active_token,
            },
        )

    def _auth_native(self, request: httpx.Request) -> Optional[FakeUser]:
        auth = request.headers.get("x-nd-authorization", "")
        if self.reject_next_token:
            self.reject_next_token = False
            return None
        if auth != f"Bearer {self.active_token}":
            return None
        return self.state.users.get("admin")

    def _handle_native_api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.native_calls.append((request.method, path))
        caller = self._auth_native(request)
        if caller is None:
            return httpx.Response(401, json={"error": "Unauthorized"})
        if path == "/api/user" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": self.user_ids.get(u.username, u.username),
                        "userName": u.username,
                        "isAdmin": u.admin,
                        "email": self.user_emails.get(u.username, ""),
                    }
                    for u in self.state.users.values()
                ],
            )
        if path.startswith("/api/playlist/"):
            pl_id = path[len("/api/playlist/") :]
            pl = self.state.playlists.get(pl_id)
            if pl is None:
                return httpx.Response(404, json={"error": "Not found"})
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": pl.id,
                        "name": pl.name,
                        "comment": self.playlist_comments.get(pl.id, ""),
                        "ownerId": self.user_ids.get(pl.owner, pl.owner),
                        "ownerName": pl.owner,
                        "public": self.playlist_public.get(pl.id, False),
                    },
                )
            if request.method == "PUT":
                body = json.loads(request.content.decode() or "{}")
                self.put_bodies.append(body)
                if not self.ignore_owner_change and "ownerId" in body:
                    target_owner_id = str(body["ownerId"])
                    by_id = {uid: uname for uname, uid in self.user_ids.items()}
                    if target_owner_id in by_id:
                        pl.owner = by_id[target_owner_id]
                return httpx.Response(
                    200,
                    json={
                        "id": pl.id,
                        "name": pl.name,
                        "comment": self.playlist_comments.get(pl.id, ""),
                        "ownerId": self.user_ids.get(pl.owner, pl.owner),
                        "public": self.playlist_public.get(pl.id, False),
                    },
                )
        return httpx.Response(404, json={"error": "Unknown route"})


def test_login_caches_token_and_relogins_once_on_401(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeNavidrome()
    admin = NavidromeAdmin("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        with caplog.at_level(logging.DEBUG):
            tok1 = admin.login()
            tok2 = admin.login()
            assert tok1 == tok2
            assert admin.is_admin is True
            users = admin.list_users()
            assert len(users) == 2
            assert [c for c in fake.native_calls if c[1] == "/auth/login"] == [("POST", "/auth/login")]

            # Force a single 401 -> transparent re-login once
            fake.reject_next_token = True
            users2 = admin.list_users()
            assert len(users2) == 2
            assert len([c for c in fake.native_calls if c[1] == "/auth/login"]) == 2

        joined_logs = "\n".join(r.getMessage() for r in caplog.records)
        assert "pw" not in joined_logs
        assert tok1 not in joined_logs
    finally:
        admin.close()


def test_login_failure_raises_auth_error_without_secret_leak() -> None:
    fake = FakeNavidrome()
    client = NavidromeAdmin(
        "http://nav:4533",
        "admin",
        "super-secret-password",
        transport=fake.transport(),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(MediaServerAuthError) as exc_info:
            client.login()
        assert "super-secret-password" not in exc_info.value.safe_detail
        assert "401" in exc_info.value.safe_detail
    finally:
        client.close()


def test_subsonic_non_admin_and_api_key_return_none() -> None:
    fake = FakeNavidrome()
    fake.state.api_keys["nav-api-key"] = "admin"

    non_admin = SubsonicMediaServer("http://nav:4533", "kid", "pw", transport=fake.transport(), sleep=lambda _s: None)
    api_key_srv = SubsonicMediaServer(
        "http://nav:4533", api_key="nav-api-key", transport=fake.transport(), sleep=lambda _s: None
    )
    try:
        assert non_admin.navidrome_admin() is None
        assert non_admin.capabilities.users is False
        assert api_key_srv.navidrome_admin() is None
        assert api_key_srv.capabilities.users is False
    finally:
        non_admin.close()
        api_key_srv.close()


def test_subsonic_non_navidrome_returns_none() -> None:
    fake = FakeSubsonic(default_state([], password="pw"))
    srv = SubsonicMediaServer("http://sub:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        assert srv.navidrome_admin() is None
        assert srv.capabilities.users is False
    finally:
        srv.close()


def test_subsonic_admin_navidrome_capabilities_and_list_users() -> None:
    fake = FakeNavidrome()
    fake.add_user("nd-alice", "alice", admin=False, email="alice@example.com")
    srv = SubsonicMediaServer("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        admin = srv.navidrome_admin()
        assert admin is not None
        assert srv.navidrome_admin() is admin  # cached
        assert srv.capabilities.users is True
        users = {u.name: u for u in srv.list_users()}
        assert users["admin"].id == "nd-admin" and users["admin"].is_admin is True
        assert users["alice"].id == "nd-alice"
        assert users["alice"].is_admin is False
        assert users["alice"].extra["email"] == "alice@example.com"
    finally:
        srv.close()


@pytest.mark.parametrize("ignore_owner_change,expected", [(False, True), (True, False)])
def test_set_owner_verifies_by_rereading_playlist(ignore_owner_change: bool, expected: bool) -> None:
    fake = FakeNavidrome(ignore_owner_change=ignore_owner_change)
    fake.add_user("nd-alice", "alice")
    fake.state.playlists["pl-9"] = FakePlaylist("pl-9", "Mix", "admin", [])
    admin = NavidromeAdmin("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        assert admin.set_owner("pl-9", "nd-alice") is expected
        expected_owner = "admin" if ignore_owner_change else "alice"
        assert fake.state.playlists["pl-9"].owner == expected_owner
    finally:
        admin.close()


def test_import_navidrome_links_by_username_and_email_without_duplicates(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    try:
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        local_alice = db.create_local_user_with_password("Alice", "hash", 35, email="alice@local.test")
        plex_bob = db.upsert_user("plex-bob", "bob_plex", email="Bob@Shared.Test")

        discovered = [
            ServerUser(id="nd-alice", name="alice", is_admin=False, extra={"email": "other@x"}),
            ServerUser(id="nd-bob", name="robert_nav", is_admin=False, extra={"email": "bob@shared.test"}),
            ServerUser(id="nd-carol", name="carol", is_admin=True, extra={"email": "carol@nav.test"}),
        ]
        imported, skipped = import_server_users(db, "subsonic", discovered, server_id=srv["id"])
        assert (imported, skipped) == (3, 0)

        # No duplicate Trackseerr users created for Alice (name match) or Bob (email match)
        all_users = {u["username"]: u for u in db.list_users()}
        assert set(all_users) == {"alice", "bob_plex", "carol"}
        assert all_users["carol"]["auth_type"] == "subsonic"
        assert all_users["carol"]["is_admin"] is False

        alice_link = db.get_server_user_link(local_alice["id"], srv["id"])
        bob_link = db.get_server_user_link(plex_bob["id"], srv["id"])
        carol_link = db.get_server_user_link("nd-carol", srv["id"])
        assert alice_link == {
            "user_id": local_alice["id"],
            "server_id": srv["id"],
            "server_user_id": "nd-alice",
            "server_username": "alice",
            "source": "imported",
        }
        assert bob_link == {
            "user_id": plex_bob["id"],
            "server_id": srv["id"],
            "server_user_id": "nd-bob",
            "server_username": "robert_nav",
            "source": "imported",
        }
        assert carol_link == {
            "user_id": "nd-carol",
            "server_id": srv["id"],
            "server_user_id": "nd-carol",
            "server_username": "carol",
            "source": "imported",
        }
    finally:
        db.close()


def test_server_user_links_crud_and_manual_precedence(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "links.db"))
    try:
        srv1 = db.insert_media_server_row({"kind": "subsonic", "name": "Nav1", "url": "http://nav1:4533"})
        srv2 = db.insert_media_server_row({"kind": "jellyfin", "name": "JF1", "url": "http://jf1:8096"})
        db.upsert_user("u1", "alice", "a@x")
        db.upsert_user("u2", "bob", "b@x")

        db.link_server_user("u1", srv1["id"], "nd-1", "alice", source="manual")
        # Subsequent automated import must NOT overwrite a manual link
        db.link_server_user("u1", srv1["id"], "nd-other", "alice_other", source="imported")
        assert db.get_server_user_link("u1", srv1["id"])["server_user_id"] == "nd-1"
        assert db.get_server_user_link("u1", srv1["id"])["source"] == "manual"

        db.link_server_user("u2", srv1["id"], "nd-2", "bob", source="imported")
        db.link_server_user("u1", srv2["id"], "jf-1", "alice", source="imported")

        assert len(db.list_server_user_links()) == 3
        assert len(db.list_server_user_links(server_id=srv1["id"])) == 2
        assert len(db.list_server_user_links(user_id="u1")) == 2
        assert len(db.list_server_user_links(server_id=srv1["id"], user_id="u1")) == 1

        assert db.unlink_server_user("u1", srv1["id"]) is True
        assert db.unlink_server_user("u1", srv1["id"]) is False
        assert db.get_server_user_link("u1", srv1["id"]) is None
    finally:
        db.close()


def test_users_refresh_route_links_navidrome_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry._reset_for_tests()
    db = Database(str(tmp_path / "refresh.db"))
    try:
        srv = db.insert_media_server_row(
            {"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533", "username": "admin", "password": "pw"}
        )
        local_ron = db.create_local_user_with_password("ron", "hash", 35, email="ron@home.test")
        fake = FakeNavidrome()
        fake.add_user("nd-ron", "rontube", admin=True, email="ron@home.test")
        fake.add_user("nd-eve", "eve", admin=False, email="eve@home.test")
        adapter = SubsonicMediaServer(
            "http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None
        )
        rec = registry.ServerRecord(
            id=srv["id"],
            kind="subsonic",
            name="Navidrome",
            source="settings",
            url="http://nav:4533",
            username="admin",
            password="pw",
            roles=frozenset({registry.ROLE_USERS}),
        )
        monkeypatch.setattr(registry, "is_installed", lambda: True)
        monkeypatch.setattr(registry, "media_servers", lambda role=None, config=None: [(rec, adapter)])

        app = FastAPI()
        app.include_router(users_router, prefix="/api/users")
        app.dependency_overrides[require_media_server] = lambda: None
        app.dependency_overrides[require_admin] = lambda: {"id": local_ron["id"], "is_admin": True}
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_media_client] = lambda: None

        client = TestClient(app)
        resp = client.post("/api/users/refresh")
        assert resp.status_code == 200
        usernames = {u["username"] for u in resp.json()}
        assert "ron" in usernames and "eve" in usernames and "rontube" not in usernames
        assert db.get_server_user_link(local_ron["id"], srv["id"])["server_user_id"] == "nd-ron"
        assert db.get_server_user_link("nd-eve", srv["id"])["server_username"] == "eve"
        adapter.close()
    finally:
        registry._reset_for_tests()
        db.close()


def test_set_owner_sends_only_editable_playlist_fields() -> None:
    fake = FakeNavidrome()
    fake.add_user("nd-alice", "alice")
    fake.state.playlists["pl-10"] = FakePlaylist("pl-10", "Road Mix", "admin", [])
    fake.playlist_comments["pl-10"] = "Keep this comment"
    fake.playlist_public["pl-10"] = True
    admin = NavidromeAdmin("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        assert admin.set_owner("pl-10", "nd-alice") is True
        assert len(fake.put_bodies) == 1
        put_body = fake.put_bodies[0]
        assert set(put_body.keys()) == {"name", "comment", "public", "ownerId"}
        assert put_body["name"] == "Road Mix"
        assert put_body["comment"] == "Keep this comment"
        assert put_body["public"] is True
        assert put_body["ownerId"] == "nd-alice"
    finally:
        admin.close()


def test_set_owner_includes_rules_only_when_non_empty() -> None:
    fake = FakeNavidrome()
    fake.add_user("nd-alice", "alice")
    fake.state.playlists["pl-11"] = FakePlaylist("pl-11", "Smart Mix", "admin", [])
    admin = NavidromeAdmin("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    smart_rules = {"all": [{"is": {"genre": "Rock"}}]}
    original_get_playlist = admin.get_playlist

    def _get_with_rules(playlist_id: str) -> dict[str, object]:
        payload = original_get_playlist(playlist_id)
        payload["rules"] = smart_rules
        return payload

    admin.get_playlist = _get_with_rules  # type: ignore[method-assign]
    try:
        assert admin.set_owner("pl-11", "nd-alice") is True
        assert len(fake.put_bodies) == 1
        put_body = fake.put_bodies[0]
        assert set(put_body.keys()) == {"name", "comment", "public", "ownerId", "rules"}
        assert put_body["rules"] == smart_rules
    finally:
        admin.close()
