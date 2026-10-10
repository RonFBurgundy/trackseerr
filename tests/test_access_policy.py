"""Server-side enforcement of the user access policy (docs/design/two-tier-security.md, "Access policy")."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import internal_auth
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_discovery_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.core_client import CoreClient, ProxyResponse
from trackseerr.config import Config
from trackseerr.storage import Database

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _fresh_nonces():
    internal_auth._nonce_cache.clear()
    yield
    internal_auth._nonce_cache.clear()


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("user-alice", "alice", "al@x.tv", is_admin=False)
    d.upsert_user("user-bob", "bob", "bo@x.tv", is_admin=False)
    yield d
    d.close()


def _config(tmp_path: Path, role: str = "all-in-one") -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET if role != "all-in-one" else None,
        trackseerr_core_url="http://core.internal:5251",
        user_request_quota=10,
    )


def _client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    discovery = MagicMock()
    discovery.get_trending.return_value = [{"id": "deezer:track:1", "item_type": "track", "title": "Song", "artist": "Band", "album": None, "cover_url": None, "preview_url": None, "release_date": None, "status": "none"}]
    app.dependency_overrides[get_discovery_client] = lambda: discovery
    return TestClient(app)


def _session(db: Database, cfg: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(db, tmp_path):
    cfg = _config(tmp_path)
    client = _client(db, cfg)
    return {
        "client": client,
        "db": db,
        "cfg": cfg,
        "alice": _session(db, cfg, "user-alice"),
        "bob": _session(db, cfg, "user-bob"),
        "admin": _session(db, cfg, "admin-1"),
    }


def _issue(db: Database, issue_id: str, user_id: str) -> None:
    db.create_issue(
        {
            "id": issue_id,
            "user_id": user_id,
            "media_title": "Album",
            "artist": "Band",
            "issue_type": "other",
            "problem_details": "broken",
            "status": "open",
        }
    )


# --------------------------------------------------------------------------- admin-only: 403 for users

ADMIN_ONLY = [
    ("GET", "/api/library/stats"),
    ("GET", "/api/library/artists"),
    ("GET", "/api/library/albums"),
    ("GET", "/api/library/tracks"),
    ("GET", "/api/library/scan/status"),
    ("GET", "/api/library/migrate-lidarr/status"),
    ("GET", "/api/library/collections"),
    ("POST", "/api/library/collections"),
    ("GET", "/api/system/events"),
    ("GET", "/api/system/logs"),
    ("GET", "/api/system/logs/stream"),
    ("GET", "/api/queue"),
    ("DELETE", "/api/queue/dl-1"),
    ("GET", "/api/missing"),
    ("GET", "/api/missing/csv"),
    ("GET", "/api/missing/rss"),
    ("GET", "/api/missing/text"),
    ("POST", "/api/missing/match"),
    ("POST", "/api/sync"),
    ("GET", "/api/sync/status"),
    ("POST", "/api/sync/webhook"),
    ("GET", "/api/users"),
    ("PUT", "/api/issues/issue-x"),
    ("DELETE", "/api/issues/issue-x"),
    ("POST", "/api/requests/req-x/retry"),
    ("POST", "/api/settings/download-clients/test"),
    ("POST", "/api/settings/indexers/test"),
]


@pytest.mark.parametrize("method,path", ADMIN_ONLY)
def test_non_admin_gets_403_on_admin_routes(env, method, path):
    kwargs: dict[str, Any] = {}
    if method in ("POST", "PUT"):
        kwargs["json"] = {}
    res = env["client"].request(method, path, headers=env["alice"], **kwargs)
    assert res.status_code == 403, (method, path, res.status_code, res.text)


def test_admin_routes_reject_forwarded_principal_of_an_admin_user(env):
    client = env["client"]
    cfg = Config(**{**env["cfg"].__dict__, "role": "core", "internal_core_secret": SECRET})
    client = _client(env["db"], cfg)
    for path in ("/api/library/stats", "/api/system/logs", "/api/queue", "/api/missing", "/api/users", "/api/sync/status"):
        headers = internal_auth.sign_assertion(SECRET, "GET", path, "admin-1", "root", b"")
        res = client.get(path, headers=headers)
        assert res.status_code == 403, (path, res.status_code)
    path = "/api/issues/issue-x"
    headers = internal_auth.sign_assertion(SECRET, "DELETE", path, "admin-1", "root", b"")
    assert client.delete(path, headers=headers).status_code == 403


def test_missing_feeds_refuse_anonymous_and_accept_api_key_and_admin(env):
    client, db = env["client"], env["db"]
    assert client.get("/api/missing/rss").status_code == 401
    assert client.get("/api/missing/text").status_code == 401
    key = db.get_api_key()
    assert client.get("/api/missing/rss", headers={"X-Api-Key": key}).status_code == 200
    assert client.get("/api/missing/text", headers=env["admin"]).status_code == 200


def test_sync_webhook_refuses_user_and_accepts_api_key(env):
    client, db = env["client"], env["db"]
    assert client.post("/api/sync/webhook", headers=env["alice"]).status_code == 403
    assert client.post("/api/sync/webhook").status_code == 401
    with patch("trackseerr.api.routes.sync.sync_state.execute_sync"):
        res = client.post("/api/sync/webhook", headers={"X-Api-Key": db.get_api_key()})
    assert res.status_code == 200


def test_log_stream_refuses_non_admin_session_token(env):
    token = env["alice"]["Authorization"].split(" ", 1)[1]
    res = env["client"].get("/api/system/logs/stream", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


def test_log_stream_ignores_query_token(env, stream_ok):
    """A session token in the URL (proxy/access-log exposure) is never accepted, even a valid admin one."""
    token = env["admin"]["Authorization"].split(" ", 1)[1]
    assert env["client"].get(f"/api/system/logs/stream?token={token}").status_code == 401


def _tok(headers):
    return headers["Authorization"].split(" ", 1)[1]


@pytest.fixture
def stream_ok():
    """Replaces the endless SSE body so an authorised stream returns immediately."""
    from fastapi.responses import PlainTextResponse

    with patch(
        "trackseerr.api.routes.system.logs.StreamingResponse",
        lambda *a, **k: PlainTextResponse("ok"),
    ):
        yield


def test_log_stream_admin_token_streams(env, stream_ok):
    token = _tok(env["admin"])
    assert env["client"].get("/api/system/logs/stream", headers=env["admin"]).status_code == 200
    env["client"].cookies.set("session_token", token)
    assert env["client"].get("/api/system/logs/stream").status_code == 200


def test_log_stream_refuses_missing_token(env, stream_ok):
    assert env["client"].get("/api/system/logs/stream").status_code == 401


def test_log_stream_refuses_logged_out_session_token(env, stream_ok):
    token = _tok(env["admin"])
    env["db"].delete_session(token)
    assert env["client"].get("/api/system/logs/stream", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_log_stream_refuses_disabled_admin(env, stream_ok):
    token = _tok(env["admin"])
    env["db"].set_disabled("admin-1", True)
    assert env["client"].get("/api/system/logs/stream", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)


def test_log_stream_refuses_session_issued_before_revocation(env, stream_ok):
    token = _tok(env["admin"])
    env["db"].revoke_sessions("admin-1")
    # the session row is kept so only the sessions_revoked_at check can reject it
    env["db"].create_session(token, "admin-1", {"auth": "test"}, issued_at_us=1_000_000)
    assert env["client"].get("/api/system/logs/stream", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --------------------------------------------------------------------------- user-level routes: 200


def test_non_admin_can_use_availability_discovery_and_own_requests(env):
    client = env["client"]
    assert client.get("/api/library/availability?artist_name=Band", headers=env["alice"]).status_code == 200
    res = client.get("/api/discovery/trending", headers=env["alice"])
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert client.get("/api/requests", headers=env["alice"]).status_code == 200


def test_non_admin_lists_only_own_requests_and_cancels_only_own_pending(env):
    client, db = env["client"], env["db"]
    mine = client.post("/api/requests", json={"title": "A", "artist": "X", "item_type": "track"}, headers=env["alice"])
    theirs = client.post("/api/requests", json={"title": "B", "artist": "Y", "item_type": "track"}, headers=env["bob"])
    assert mine.status_code == 201 and theirs.status_code == 201
    listed = client.get("/api/requests", headers=env["alice"]).json()["requests"]
    assert [r["id"] for r in listed] == [mine.json()["id"]]
    # another user's request: 404, and it is untouched
    assert client.delete(f"/api/requests/{theirs.json()['id']}", headers=env["alice"]).status_code == 404
    assert db.get_request(theirs.json()["id"]) is not None
    # own pending request: cancelled
    assert client.delete(f"/api/requests/{mine.json()['id']}", headers=env["alice"]).status_code == 200


def test_non_admin_cannot_cancel_own_non_pending_request(env):
    client, db = env["client"], env["db"]
    created = client.post("/api/requests", json={"title": "A", "artist": "X", "item_type": "track"}, headers=env["alice"])
    rid = created.json()["id"]
    db.update_request_status(rid, "processing")
    assert client.delete(f"/api/requests/{rid}", headers=env["alice"]).status_code == 400


def test_issue_create_and_own_issue_visibility(env):
    client, db = env["client"], env["db"]
    body = {"media_title": "Album", "artist": "Band", "issue_type": "other", "problem_details": "skips"}
    created = client.post("/api/issues", json=body, headers=env["alice"])
    assert created.status_code == 201
    mine = created.json()["id"]
    _issue(db, "issue-bob", "user-bob")

    listed = client.get("/api/issues", headers=env["alice"]).json()
    assert [i["id"] for i in listed] == [mine]
    assert client.get(f"/api/issues/{mine}", headers=env["alice"]).status_code == 200
    assert {i["id"] for i in client.get("/api/issues", headers=env["admin"]).json()} == {mine, "issue-bob"}


def test_non_admin_gets_404_for_another_users_issue(env):
    _issue(env["db"], "issue-bob", "user-bob")
    assert env["client"].get("/api/issues/issue-bob", headers=env["alice"]).status_code == 404
    assert env["client"].get("/api/issues/issue-bob", headers=env["admin"]).status_code == 200


def test_admin_can_edit_and_delete_issues(env):
    _issue(env["db"], "issue-bob", "user-bob")
    res = env["client"].put("/api/issues/issue-bob", json={"status": "resolved"}, headers=env["admin"])
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    assert env["client"].delete("/api/issues/issue-bob", headers=env["admin"]).status_code == 200


def test_non_admin_cannot_edit_or_delete_own_issue(env):
    _issue(env["db"], "issue-alice", "user-alice")
    assert env["client"].put("/api/issues/issue-alice", json={"status": "resolved"}, headers=env["alice"]).status_code == 403
    assert env["client"].delete("/api/issues/issue-alice", headers=env["alice"]).status_code == 403


def test_users_me_stays_open(env):
    assert env["client"].get("/api/users/me", headers=env["alice"]).status_code == 200


# --------------------------------------------------------------------------- playlists


def _playlist(db: Database, pid: str, creator: str, targets: list[str]) -> None:
    db.upsert_playlist(pid, name=pid, creator_id=creator)
    db.set_playlist_targets(pid, targets)


def test_playlist_list_is_scoped_to_creator_or_target(env):
    db, client = env["db"], env["client"]
    _playlist(db, "pl-alice", "user-alice", ["user-alice"])
    _playlist(db, "pl-bob", "user-bob", ["user-bob"])
    _playlist(db, "pl-shared", "admin-1", ["user-alice"])
    ids = {p["id"] for p in client.get("/api/playlists", headers=env["alice"]).json()}
    assert ids == {"pl-alice", "pl-shared"}
    assert {p["id"] for p in client.get("/api/playlists", headers=env["admin"]).json()} == {"pl-alice", "pl-bob", "pl-shared"}


def test_playlist_mutations_on_others_playlists_are_404(env):
    db, client = env["db"], env["client"]
    _playlist(db, "pl-bob", "user-bob", ["user-bob"])
    _playlist(db, "pl-shared", "admin-1", ["user-alice"])
    for pid in ("pl-bob", "pl-shared", "pl-missing"):
        assert client.put(f"/api/playlists/{pid}/targets", json={"user_ids": ["user-alice"]}, headers=env["alice"]).status_code == 404
        assert client.put(f"/api/playlists/{pid}/enabled", json={"enabled": False}, headers=env["alice"]).status_code == 404
        assert client.delete(f"/api/playlists/{pid}", headers=env["alice"]).status_code == 404
    assert db.get_playlist("pl-bob") is not None
    assert db.get_playlist_targets("pl-bob") == ["user-bob"]
    assert db.get_playlist_targets("pl-shared") == ["user-alice"]


def test_playlist_targets_restricted_to_self(env):
    db, client = env["db"], env["client"]
    _playlist(db, "pl-alice", "user-alice", [])
    url = "/api/playlists/pl-alice/targets"
    assert client.put(url, json={"user_ids": ["user-bob"]}, headers=env["alice"]).status_code == 403
    assert client.put(url, json={"user_ids": ["user-alice", "user-bob"]}, headers=env["alice"]).status_code == 403
    assert db.get_playlist_targets("pl-alice") == []
    ok = client.put(url, json={"user_ids": ["user-alice"]}, headers=env["alice"])
    assert ok.status_code == 200 and ok.json()["targets"] == ["user-alice"]
    cleared = client.put(url, json={"user_ids": []}, headers=env["alice"])
    assert cleared.status_code == 200 and cleared.json()["targets"] == []
    # admin may target anyone
    admin = client.put(url, json={"user_ids": ["user-bob"]}, headers=env["admin"])
    assert admin.status_code == 200 and admin.json()["targets"] == ["user-bob"]


def test_user_can_toggle_and_delete_own_playlist(env):
    db, client = env["db"], env["client"]
    _playlist(db, "pl-alice", "user-alice", ["user-alice"])
    assert client.put("/api/playlists/pl-alice/enabled", json={"enabled": False}, headers=env["alice"]).status_code == 200
    assert client.delete("/api/playlists/pl-alice", headers=env["alice"]).status_code == 200
    assert db.get_playlist("pl-alice") is None


def test_user_import_sets_creator_and_ignores_foreign_targets(env):
    db, client = env["db"], env["client"]
    body = {"name": "Mine", "tracks": [{"title": "T", "artist": "A"}], "targets": ["user-bob"]}
    res = client.post("/api/playlists/import", json=body, headers=env["alice"])
    assert res.status_code == 201, res.text
    created = db.list_playlists(user_id="user-alice")
    assert len(created) == 1 and created[0]["creator_id"] == "user-alice"
    assert db.get_playlist_targets(created[0]["id"]) == ["user-alice"]
    assert db.list_playlists(user_id="user-bob") == []


def test_featured_and_presets_open_to_users(env):
    assert env["client"].get("/api/playlists/featured", headers=env["alice"]).status_code == 200


# --------------------------------------------------------------------------- gateway


@pytest.fixture
def gateway(db, tmp_path):
    cfg = _config(tmp_path, "gateway")
    return _client(db, cfg), _session(db, cfg, "user-alice")


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/playlists"),
        ("POST", "/api/playlists/import"),
        ("PUT", "/api/playlists/pl-1/targets"),
        ("PUT", "/api/playlists/pl-1/enabled"),
        ("DELETE", "/api/playlists/pl-1"),
        ("GET", "/api/playlists/featured"),
        ("GET", "/api/issues"),
        ("POST", "/api/issues"),
        ("GET", "/api/issues/issue-1"),
        ("GET", "/api/requests"),
        ("DELETE", "/api/requests/req-1"),
    ],
)
def test_gateway_forwards_user_endpoints_to_core(gateway, method, path):
    client, headers = gateway
    fake = ProxyResponse(200, b"[]", {"Content-Type": "application/json"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        res = client.request(method, path, headers=headers, content=b"{}" if method in ("POST", "PUT") else None)
    assert res.status_code == 200
    args = proxy.call_args.args
    assert args[0] == method and args[1] == path
    assert args[4]["id"] == "user-alice" and args[4]["username"] == "alice"
    assert args[4]["_session_issued_at_us"] > 0


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/api/issues/issue-1"),
        ("DELETE", "/api/issues/issue-1"),
        ("GET", "/api/library/artists"),
        ("GET", "/api/library/stats"),
        ("GET", "/api/queue"),
        ("GET", "/api/system/logs"),
        ("GET", "/api/missing"),
        ("POST", "/api/sync"),
        ("GET", "/api/users"),
        ("POST", "/api/requests/req-1/retry"),
    ],
)
def test_gateway_404s_admin_routes_without_forwarding(gateway, method, path):
    client, headers = gateway
    with patch.object(CoreClient, "proxy") as proxy:
        res = client.request(method, path, headers=headers)
    assert res.status_code == 404
    proxy.assert_not_called()


# --------------------------------------------------------------------------- gateway discovery availability


def test_gateway_discovery_annotates_from_core_availability(gateway):
    client, headers = gateway
    avail = {"in_library": True, "status": "available", "quality": "FLAC"}
    with patch.object(CoreClient, "get_availability", return_value=avail) as get_avail:
        res = client.get("/api/discovery/trending", headers=headers)
    assert res.status_code == 200
    item = res.json()["items"][0]
    assert item["status"] == "available" and item["quality"] == "FLAC"
    kwargs = get_avail.call_args.kwargs
    assert kwargs["artist_name"] == "Band" and kwargs["track_title"] == "Song"
    assert kwargs["user_info"]["id"] == "user-alice"


def test_gateway_discovery_survives_core_outage(gateway):
    import httpx

    client, headers = gateway
    with patch.object(CoreClient, "get_availability", side_effect=httpx.ConnectError("down")):
        res = client.get("/api/discovery/trending", headers=headers)
    assert res.status_code == 200
    assert res.json()["items"][0]["status"] == "none"


def test_core_availability_accepts_service_principal(db, tmp_path):
    cfg = _config(tmp_path, "core")
    client = _client(db, cfg)
    target = "/api/library/availability?artist_name=Band"
    headers = internal_auth.sign_assertion(SECRET, "GET", target, "", "", b"")
    res = client.get(target, headers=headers)
    assert res.status_code == 200
    assert res.json()["in_library"] is False
