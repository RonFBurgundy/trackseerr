"""Two-tier (DMZ) security: signed assertions, forwarded principals, gateway deny-by-default."""

import json
import os
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import internal_auth
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, has_permission
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.core_client import CoreClient, ProxyResponse
from plex_playlist_sync.cli import main
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.storage import Database

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
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    yield d
    d.close()


def _config(tmp_path: Path, role: str = "core", secret: Optional[str] = SECRET) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=secret,
        trackseerr_core_url="http://core.internal:5251",
        user_request_quota=10,
    )


def _client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


@pytest.fixture
def core(db, tmp_path):
    cfg = _config(tmp_path, "core")
    return _client(db, cfg), cfg


def _signed(
    method: str,
    target: str,
    user_id: str = "1001",
    user_name: str = "alice",
    body: bytes = b"",
    secret: str = SECRET,
    **overrides: Any,
) -> dict[str, str]:
    headers = internal_auth.sign_assertion(secret, method, target, user_id, user_name, body, **overrides)
    if body:
        headers["Content-Type"] = "application/json"
    return headers


def _session_headers(user: dict[str, Any], db: Database, cfg: Config) -> dict[str, str]:
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- core verification


def test_valid_signature_yields_nonadmin_user(core):
    client, _ = core
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me"))
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "1001"
    assert body["is_admin"] is False


def test_asserted_admin_is_rejected_403_everywhere(core):
    client, _ = core
    for target in ("/api/users/me", "/api/settings/api-key", "/api/scrobbles/users"):
        res = client.get(target, headers=_signed("GET", target, user_id="admin-1", user_name="root"))
        assert res.status_code == 403, target
        assert res.json()["detail"] == "Admin accounts must use the TrackSeerr Core admin interface"


def test_asserted_user_with_admin_permission_bit_is_rejected(core, db):
    client, _ = core
    db.upsert_user("5005", "bit-admin", None, is_admin=False)
    db.conn.execute("UPDATE users SET permissions = ? WHERE id = '5005'", (int(UserPermission.ADMIN),))
    db.conn.commit()
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me", user_id="5005", user_name="bit-admin"))
    assert res.status_code == 403


def test_signature_works_on_get_current_user_endpoint(core):
    client, _ = core
    res = client.get("/api/auth/me", headers=_signed("GET", "/api/auth/me"))
    assert res.status_code == 200
    assert res.json()["user"]["forwarded"] is True
    assert res.json()["user"]["is_admin"] is False


def test_tampered_body_path_user_or_timestamp_is_401(core):
    client, _ = core
    body = json.dumps({"title": "A", "artist": "B", "item_type": "track"}).encode()
    good = _signed("POST", "/api/requests", body=body)
    tampered_body = json.dumps({"title": "A", "artist": "C", "item_type": "track"}).encode()
    assert client.post("/api/requests", content=tampered_body, headers=good).status_code == 401

    h = _signed("GET", "/api/users/me")
    assert client.get("/api/auth/me", headers=h).status_code == 401  # path differs

    h = _signed("GET", "/api/users/me")
    h["X-TS-User-Id"] = "admin-1"
    assert client.get("/api/users/me", headers=h).status_code == 401

    h = _signed("GET", "/api/users/me")
    h["X-TS-User-Name"] = "mallory"
    assert client.get("/api/users/me", headers=h).status_code == 401

    h = _signed("GET", "/api/users/me")
    h["X-TS-Timestamp"] = str(int(h["X-TS-Timestamp"]) + 1)
    assert client.get("/api/users/me", headers=h).status_code == 401


def test_tampered_query_is_401(core):
    client, _ = core
    h = _signed("GET", "/api/library/availability?artist_name=A")
    assert client.get("/api/library/availability?artist_name=B", headers=h).status_code == 401


def test_clock_skew_over_60s_is_401(core):
    client, _ = core
    # Margins well past the 60s window: int() truncation plus request latency under load eats up to ~1s+,
    # so a +61 timestamp can land at exactly 60s by verification time and be (correctly) accepted.
    old = _signed("GET", "/api/users/me", timestamp=int(time.time()) - 90)
    assert client.get("/api/users/me", headers=old).status_code == 401
    future = _signed("GET", "/api/users/me", timestamp=int(time.time()) + 90)
    assert client.get("/api/users/me", headers=future).status_code == 401
    ok = _signed("GET", "/api/users/me", timestamp=int(time.time()) - 30)
    assert client.get("/api/users/me", headers=ok).status_code == 200


def test_clock_skew_boundary_is_deterministic():
    secret_headers = _signed("GET", "/api/users/me", timestamp=1_000_000)
    kwargs = dict(nonce_cache=internal_auth.NonceCache())
    body_hash = __import__("hashlib").sha256(b"").hexdigest()
    with pytest.raises(internal_auth.InvalidAssertion):
        internal_auth.verify_assertion(SECRET, "GET", "/api/users/me", secret_headers, body_hash, now=1_000_061.0, **kwargs)
    with pytest.raises(internal_auth.InvalidAssertion):
        internal_auth.verify_assertion(SECRET, "GET", "/api/users/me", secret_headers, body_hash, now=999_939.0, **kwargs)


def test_rejected_assertion_never_falls_back_to_session_credentials(core, db):
    """A present-but-invalid gateway assertion is 401 even alongside a valid admin/user Bearer session."""
    client, cfg = core
    for uid in ("admin-1", "1001"):
        sess = _session_headers(db.get_user(uid), db, cfg)
        # sanity: the session alone authenticates
        assert client.get("/api/users/me", headers=sess).status_code == 200
        for bad in (
            _signed("GET", "/api/users/me", timestamp=int(time.time()) - 90),
            _signed("GET", "/api/users/me", secret="x" * 40),
            _signed("GET", "/api/auth/me"),  # signed for a different path
        ):
            res = client.get("/api/users/me", headers={**sess, **bad})
            assert res.status_code == 401, uid
            client.cookies.set("session_token", sess["Authorization"][7:])
            res = client.get("/api/users/me", headers=bad)
            client.cookies.clear()
            assert res.status_code == 401, uid


def test_replayed_nonce_is_401(core):
    client, _ = core
    h = _signed("GET", "/api/users/me")
    assert client.get("/api/users/me", headers=h).status_code == 200
    assert client.get("/api/users/me", headers=h).status_code == 401


def test_unsigned_nonce_does_not_poison_cache(core):
    client, _ = core
    bad = _signed("GET", "/api/users/me", secret="x" * 40, nonce="fixed-nonce")
    assert client.get("/api/users/me", headers=bad).status_code == 401
    good = _signed("GET", "/api/users/me", nonce="fixed-nonce")
    assert client.get("/api/users/me", headers=good).status_code == 200


def test_nonce_cache_expires_and_is_thread_safe():
    cache = internal_auth.NonceCache(ttl_seconds=10)
    assert cache.check_and_add("n", now=100.0) is True
    assert cache.check_and_add("n", now=105.0) is False
    assert cache.check_and_add("n", now=111.0) is True

    import threading

    results: list[bool] = []
    shared = internal_auth.NonceCache()
    threads = [threading.Thread(target=lambda: results.append(shared.check_and_add("same"))) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_core_without_secret_refuses_to_start(db, tmp_path):
    with pytest.raises(RuntimeError):
        _client(db, _config(tmp_path, "core", secret=None))


def test_wrong_secret_is_401(core):
    client, _ = core
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me", secret="z" * 40))
    assert res.status_code == 401


def test_unknown_user_upserted_nonadmin_and_existing_admin_untouched(core, db):
    client, _ = core
    assert db.get_user("4242") is None
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me", user_id="4242", user_name="newbie"))
    assert res.status_code == 200
    row = db.get_user("4242")
    assert row["username"] == "newbie"
    assert not row["is_admin"]
    assert int(row["permissions"]) == int(UserPermission.DEFAULT)

    # asserting the existing admin (rejected 403) must not touch the row
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me", user_id="admin-1", user_name="evil"))
    assert res.status_code == 403
    admin = db.get_user("admin-1")
    assert admin["is_admin"]
    assert int(admin["permissions"]) & int(UserPermission.ADMIN)
    assert admin["username"] == "root"
    assert admin["email"] == "a@x.tv"


def test_service_principal_has_no_permissions(core):
    client, _ = core
    h = _signed("GET", "/api/auth/me", user_id="", user_name="")
    res = client.get("/api/auth/me", headers=h)
    assert res.status_code == 200
    user = res.json()["user"]
    assert user["id"] == "gateway_service"
    assert user["permissions"] == 0
    assert user["is_admin"] is False
    for perm in UserPermission:
        assert has_permission(user, perm) is False
    # cannot create requests
    body = json.dumps({"title": "T", "artist": "A", "item_type": "track"}).encode()
    res = client.post("/api/requests", content=body, headers=_signed("POST", "/api/requests", user_id="", user_name="", body=body))
    assert res.status_code == 403


def test_has_permission_refuses_admin_for_forwarded():
    forwarded = {"id": "u", "is_admin": True, "permissions": int(UserPermission.ADMIN | UserPermission.REQUEST), "forwarded": True}
    assert has_permission(forwarded, UserPermission.ADMIN) is False
    assert has_permission(forwarded, UserPermission.MANAGE_REQUESTS) is False
    assert has_permission(forwarded, UserPermission.REQUEST) is True


def test_legacy_internal_token_gets_no_admin(core):
    client, _ = core
    assert client.get("/api/queue", headers={"X-Internal-Token": SECRET}).status_code == 401
    assert client.get("/api/queue", headers={"Authorization": f"Bearer {SECRET}"}).status_code == 401
    assert client.get("/api/queue", headers={"X-Api-Key": SECRET}).status_code == 401


def test_gateway_role_never_accepts_signatures(db, tmp_path):
    client = _client(db, _config(tmp_path, "gateway"))
    res = client.get("/api/users/me", headers=_signed("GET", "/api/users/me"))
    assert res.status_code == 400  # inbound X-TS-* headers are refused outright on a gateway


def test_forwarded_request_attributed_to_real_user_and_quota_enforced(core, db):
    client, _ = core
    with db._lock:
        db.conn.execute("UPDATE users SET quota_tracks = 1 WHERE id = '1001'")
        db.conn.commit()

    def post(title: str):
        body = json.dumps({"title": title, "artist": "Band", "item_type": "track"}).encode()
        return client.post("/api/requests", content=body, headers=_signed("POST", "/api/requests", body=body))

    first = post("One")
    assert first.status_code == 201
    assert first.json()["user_id"] == "1001"
    assert first.json()["status"] == "pending"  # not auto-approved as an admin
    second = post("Two")
    assert second.status_code == 400
    assert "quota" in second.json()["detail"].lower()


def test_forwarded_delete_is_owner_scoped(core, db):
    client, _ = core
    body = json.dumps({"title": "One", "artist": "Band", "item_type": "track"}).encode()
    created = client.post("/api/requests", content=body, headers=_signed("POST", "/api/requests", body=body)).json()
    db.upsert_user("1002", "bob")
    path = f"/api/requests/{created['id']}"
    assert client.delete(path, headers=_signed("DELETE", path, user_id="1002", user_name="bob")).status_code == 404
    assert client.delete(path, headers=_signed("DELETE", path)).status_code == 200


# --------------------------------------------------------------------------- scrobbles on core


def test_lastfm_callback_accepts_forwarded_principal_and_checks_state_owner(core, db):
    client, _ = core
    own = db.create_lastfm_auth_state("1001", None)
    target = f"/api/scrobbles/lastfm/callback?state={own}"
    res = client.get(target, headers=_signed("GET", target), follow_redirects=False)
    assert res.status_code == 303
    assert "scrobble_error=state" not in res.headers["location"]  # state accepted (Last.fm itself not configured)

    foreign = db.create_lastfm_auth_state("1001", None)
    target = f"/api/scrobbles/lastfm/callback?state={foreign}"
    res = client.get(target, headers=_signed("GET", target, user_id="1002", user_name="bob"), follow_redirects=False)
    assert res.status_code == 303
    assert "scrobble_error=state" in res.headers["location"]


def test_webhook_url_uses_request_base_url_not_application_url(db, tmp_path):
    cfg = _config(tmp_path, "core")
    cfg.application_url = "https://public.example.com"
    client = _client(db, cfg)
    admin = db.get_user("admin-1")
    for method, path in (("get", "/api/scrobbles/webhook-url"), ("post", "/api/scrobbles/webhook-secret/rotate")):
        res = getattr(client, method)(path, headers=_session_headers(admin, db, cfg))
        assert res.status_code == 200
        assert res.json()["url"].startswith("http://testserver/api/scrobbles/plex?token=")


def test_scrobble_admin_routes_require_admin_for_forwarded(core):
    client, _ = core
    for method, path in (
        ("GET", "/api/scrobbles/users"),
        ("GET", "/api/scrobbles/server-config"),
        ("GET", "/api/scrobbles/webhook-url"),
        ("POST", "/api/scrobbles/webhook-secret/rotate"),
    ):
        res = client.request(method, path, headers=_signed(method, path, user_id="admin-1", user_name="root"))
        assert res.status_code == 403, path


# --------------------------------------------------------------------------- tier field


@pytest.mark.parametrize("role,expected", [("gateway", "gateway"), ("core", "core"), ("all-in-one", "all-in-one"), ("weird", "all-in-one")])
def test_me_reports_tier(db, tmp_path, role, expected):
    cfg = _config(tmp_path, role)
    client = _client(db, cfg)
    headers = _session_headers(db.get_user("1001"), db, cfg)
    assert client.get("/api/users/me", headers=headers).json()["tier"] == expected
    assert client.get("/api/auth/me", headers=headers).json()["tier"] == expected


# --------------------------------------------------------------------------- CoreClient


def test_core_client_signs_and_drops_legacy_headers():
    cc = CoreClient("http://core:5251", secret=SECRET)
    with patch("httpx.Client") as cls:
        inst = MagicMock()
        cls.return_value.__enter__.return_value = inst
        inst.request.return_value = MagicMock(status_code=200, content=b"{}", headers={"content-type": "application/json"})
        cc.proxy("GET", "/api/mixes/x y", "a=1&b=2", b"", {"id": "u1", "username": "bob"})
        assert cls.call_args.kwargs["follow_redirects"] is False
        method, url = inst.request.call_args.args
        headers = inst.request.call_args.kwargs["headers"]
    assert method == "GET"
    assert url == "http://core:5251/api/mixes/x%20y?a=1&b=2"
    assert "X-Internal-Token" not in headers and "X-Api-Key" not in headers
    assert SECRET not in headers.values()
    verified = internal_auth.verify_assertion(
        SECRET, "GET", "/api/mixes/x%20y?a=1&b=2", headers, internal_auth.body_sha256_hex(b"")
    )
    assert verified.user_id == "u1" and verified.user_name == "bob"


def test_core_client_proxy_returns_status_headers_body():
    cc = CoreClient("http://core:5251", secret=SECRET)
    with patch("httpx.Client") as cls:
        inst = MagicMock()
        cls.return_value.__enter__.return_value = inst
        inst.request.return_value = MagicMock(
            status_code=303, content=b"", headers={"location": "/?connected=lastfm", "set-cookie": "x=1"}
        )
        out = cc.proxy("GET", "/api/scrobbles/lastfm/callback", "state=s", b"", {"id": "u1", "username": "bob"})
    assert out.status_code == 303
    assert out.headers == {"Location": "/?connected=lastfm"}


def test_non_ascii_username_roundtrips():
    h = internal_auth.sign_assertion(SECRET, "GET", "/x", "u1", "Zoë Ünï")
    v = internal_auth.verify_assertion(SECRET, "GET", "/x", h, internal_auth.body_sha256_hex(b""))
    assert v.user_name == "Zoë Ünï"


# --------------------------------------------------------------------------- gateway middleware


@pytest.fixture
def gateway(db, tmp_path):
    cfg = _config(tmp_path, "gateway")
    client = _client(db, cfg)
    return client, cfg, _session_headers(db.get_user("1001"), db, cfg)


def test_gateway_local_allowlisted_route_works(gateway):
    client, _, headers = gateway
    assert client.get("/api/health").status_code == 200
    res = client.get("/api/users/me", headers=headers)
    assert res.status_code == 200
    assert res.json()["tier"] == "gateway"
    # request list reads core, not the gateway's ephemeral DB: it is forwarded (see test_access_policy.py)


def test_gateway_forwards_with_session_user(gateway):
    client, _, headers = gateway
    fake = ProxyResponse(200, b'{"playlists": []}', {"Content-Type": "application/json"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        res = client.get("/api/plex-playlists?kind=regular", headers=headers)
    assert res.status_code == 200
    assert res.json() == {"playlists": []}
    args = proxy.call_args.args
    assert args[0] == "GET"
    assert args[1] == "/api/plex-playlists"
    assert args[2] == "kind=regular"
    assert args[4]["id"] == "1001" and args[4]["username"] == "alice"
    assert isinstance(args[4]["_session_issued_at_us"], int) and args[4]["_session_issued_at_us"] > 0


def test_gateway_forward_passes_body_and_all_methods(gateway):
    client, _, headers = gateway
    fake = ProxyResponse(201, b'{"ok": true}', {"Content-Type": "application/json"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        res = client.post("/api/mixes/abc/save", json={"x": 1}, headers=headers)
    assert res.status_code == 201
    assert proxy.call_args.args[0] == "POST"
    assert json.loads(proxy.call_args.args[3]) == {"x": 1}
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        client.put("/api/scrobbles/config", json={"scrobbling_enabled": True}, headers=headers)
        assert proxy.call_args.args[1] == "/api/scrobbles/config"


def test_gateway_forward_requires_session(gateway):
    client, _, _ = gateway
    with patch.object(CoreClient, "proxy") as proxy:
        res = client.get("/api/plex-playlists")
    assert res.status_code == 401
    proxy.assert_not_called()


def test_gateway_lastfm_callback_location_passthrough(gateway):
    client, _, headers = gateway
    fake = ProxyResponse(303, b"", {"Location": "/?connected=lastfm"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        res = client.get("/api/scrobbles/lastfm/callback?state=abc&token=tok", headers=headers, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/?connected=lastfm"
    assert proxy.call_args.args[2] == "state=abc&token=tok"


def test_gateway_hides_admin_and_webhook_routes(gateway, db, tmp_path):
    client, cfg, _ = gateway
    admin_headers = _session_headers(db.get_user("admin-1"), db, cfg)
    with patch.object(CoreClient, "proxy") as proxy:
        for method, path in (
            ("GET", "/api/settings/api-key"),
            ("GET", "/api/queue"),
            ("POST", "/api/library/scan"),
            ("GET", "/api/scrobbles/users"),
            ("GET", "/api/scrobbles/server-config"),
            ("GET", "/api/scrobbles/webhook-url"),
            ("POST", "/api/scrobbles/webhook-secret/rotate"),
            ("POST", "/api/scrobbles/plex?token=whatever"),
            ("POST", "/api/requests/abc/approve"),
            ("GET", "/api/docs"),
            ("GET", "/api/openapi.json"),
            ("DELETE", "/api/scrobbles/config"),
            ("GET", "/api/plex-playlists/../settings"),
        ):
            res = client.request(method, path, headers=admin_headers)
            assert res.status_code == 404, (method, path)
    proxy.assert_not_called()


def test_gateway_oversized_body_is_413(gateway):
    client, _, headers = gateway
    with patch.object(CoreClient, "proxy") as proxy:
        res = client.post("/api/mixes", content=b"x" * (1024 * 1024 + 1), headers=headers)
    assert res.status_code == 413
    proxy.assert_not_called()


def test_gateway_core_unreachable_is_502(gateway):
    import httpx

    client, _, headers = gateway
    with patch.object(CoreClient, "proxy", side_effect=httpx.ConnectError("down")):
        res = client.get("/api/mixes", headers=headers)
    assert res.status_code == 502


def test_gateway_allowlist_includes_mixes_and_excludes_webhook():
    from plex_playlist_sync.api import tier_middleware as tm

    assert tm._allowed(tm.GATEWAY_FORWARD_ALLOWLIST, "DELETE", "/api/mixes/a/b")
    assert not tm._allowed(tm.GATEWAY_FORWARD_ALLOWLIST, "POST", "/api/scrobbles/plex")
    assert not tm._allowed(tm.GATEWAY_LOCAL_ALLOWLIST, "POST", "/api/scrobbles/plex")


def test_non_gateway_roles_are_not_filtered(db, tmp_path):
    client = _client(db, _config(tmp_path, "all-in-one"))
    headers = _session_headers(db.get_user("admin-1"), db, _config(tmp_path, "all-in-one"))
    assert client.get("/api/queue", headers=headers).status_code == 200


# --------------------------------------------------------------------------- CLI fail-fast


@pytest.mark.parametrize("role", ["gateway", "core"])
@pytest.mark.parametrize("secret", [None, "short-secret"])
def test_cli_fails_fast_without_strong_secret(role, secret, caplog):
    env = {"ROLE": role, "PLEX_URL": "http://p", "PLEX_TOKEN": "t"}
    if secret:
        env["INTERNAL_CORE_SECRET"] = secret
    with patch.dict(os.environ, env, clear=True):
        with caplog.at_level("ERROR"):
            assert main() == 1
    assert "INTERNAL_CORE_SECRET" in caplog.text
    if secret:
        assert secret not in caplog.text


def test_cli_secret_not_required_for_all_in_one():
    with patch.dict(os.environ, {"ROLE": "all-in-one", "MEDIA_SERVER": "plex"}, clear=True):
        # fails later on the explicit-Plex credential check, not on the secret
        assert main() == 1


# --------------------------------------------------------------------------- hardening regressions


def _me(client, user_id, user_name):
    return client.get("/api/users/me", headers=_signed("GET", "/api/users/me", user_id=user_id, user_name=user_name))


@pytest.mark.parametrize("bad_id", ["1", "0", "api_key_user", "gateway_service", "internal_gateway", "abc", "12x", "-5"])
def test_reserved_and_non_numeric_ids_are_401(core, db, bad_id):
    client, _ = core
    assert _me(client, bad_id, "mallory").status_code == 401
    assert db.get_user(bad_id) is None


def test_username_clash_with_other_id_is_401_case_insensitive(core, db):
    client, _ = core
    assert _me(client, "7777", "ALICE").status_code == 401
    assert db.get_user("7777") is None


def test_forwarded_principal_cannot_rename_itself(core, db):
    client, _ = core
    res = _me(client, "1001", "totally-new-name")
    assert res.status_code == 200
    assert res.json()["username"] == "alice"
    assert db.get_user("1001")["username"] == "alice"


def test_nonce_replay_rejected_after_memory_cache_cleared(core):
    client, _ = core
    h = _signed("GET", "/api/users/me")
    assert client.get("/api/users/me", headers=h).status_code == 200
    internal_auth._nonce_cache.clear()  # simulates a process restart; only the DB remembers
    assert client.get("/api/users/me", headers=h).status_code == 401


def test_record_nonce_duplicate_fails_and_old_entries_pruned(db):
    assert db.record_nonce("n1", 1000) is True
    assert db.record_nonce("n1", 1010) is False
    assert db.record_nonce("n1", 1000 + 121) is True  # pruned after 120s
    count = db.conn.execute("SELECT COUNT(*) FROM internal_nonces").fetchone()[0]
    assert count == 1


def test_weak_secret_refused_for_sign_and_verify():
    with pytest.raises(ValueError):
        internal_auth.sign_assertion("short", "GET", "/x", "1001", "alice")
    good = internal_auth.sign_assertion(SECRET, "GET", "/x", "1001", "alice")
    with pytest.raises(internal_auth.InvalidAssertion):
        internal_auth.verify_assertion("short", "GET", "/x", good, internal_auth.body_sha256_hex(b""))


@pytest.mark.parametrize("role", ["core", "gateway"])
@pytest.mark.parametrize("secret", [None, "", "x" * 31])
def test_create_app_refuses_weak_secret(db, tmp_path, role, secret):
    with pytest.raises(RuntimeError):
        create_app(db=db, config=_config(tmp_path, role, secret=secret))


def test_create_app_all_in_one_needs_no_secret(db, tmp_path):
    assert create_app(db=db, config=_config(tmp_path, "all-in-one", secret=None)) is not None


def test_create_app_without_config_reads_env(monkeypatch):
    monkeypatch.setenv("ROLE", "core")
    monkeypatch.delenv("INTERNAL_CORE_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        create_app()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/?connected=lastfm", "/?connected=lastfm"),
        ("//evil.example/x", None),
        ("https://evil.example/x", None),
        ("http://core.internal:5251/?a=1", "/?a=1"),
        ("http://core.internal:5251.evil.example/", None),
        ("javascript:alert(1)", None),
        ("/\\evil.example", None),
    ],
)
def test_redirect_location_filtering(value, expected):
    from plex_playlist_sync.api.tier_middleware import _safe_location

    assert _safe_location(value, "http://core.internal:5251") == expected


def test_gateway_drops_external_location(gateway):
    client, _, headers = gateway
    fake = ProxyResponse(303, b"", {"Location": "https://evil.example/phish"})
    with patch.object(CoreClient, "proxy", return_value=fake):
        res = client.get("/api/scrobbles/lastfm/callback?state=a&token=b", headers=headers, follow_redirects=False)
    assert res.status_code == 303
    assert "location" not in res.headers


def test_gateway_rejects_inbound_ts_headers_400(gateway):
    client, _, headers = gateway
    with patch.object(CoreClient, "proxy") as proxy:
        res = client.get("/api/mixes", headers={**headers, "X-TS-User-Id": "1"})
    assert res.status_code == 400
    proxy.assert_not_called()


def test_gateway_unsignable_user_id_is_400(gateway):
    client, _, headers = gateway
    with patch.object(CoreClient, "proxy", side_effect=ValueError("user_id must not contain line breaks")):
        res = client.get("/api/mixes", headers=headers)
    assert res.status_code == 400


def test_core_rejects_oversize_content_length_413(core):
    client, _ = core
    body = b"x" * (1024 * 1024 + 1)
    res = client.post("/api/requests", content=body, headers=_signed("POST", "/api/requests", body=body))
    assert res.status_code == 413


def test_core_caps_chunked_body_413(core):
    client, _ = core

    def gen():
        for _ in range(2):
            yield b"x" * (700 * 1024)

    h = _signed("POST", "/api/requests", body=b"x")
    res = client.post("/api/requests", content=gen(), headers=h)
    assert res.status_code == 413


def test_feed_non_ascii_token_is_401_not_500(db, tmp_path):
    cfg = _config(tmp_path, "all-in-one", secret=None)
    cfg.feed_token = "feedtoken-ascii"
    client = _client(db, cfg)
    res = client.get("/api/missing/rss?token=caf%C3%A9")
    assert res.status_code == 401
    assert res.status_code != 500


def test_non_ascii_api_key_is_401_not_500(core):
    client, _ = core
    res = client.get("/api/queue", headers={"X-Api-Key": "café".encode("utf-8")})
    assert res.status_code == 401


def test_api_docs_disabled_by_default_and_enabled_by_env(db, tmp_path, monkeypatch):
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    c = _client(db, _config(tmp_path, "all-in-one", secret=None))
    assert "openapi" not in c.get("/api/openapi.json").text[:300]
    assert "swagger" not in c.get("/api/docs").text.lower()
    assert "redoc" not in c.get("/api/redoc").text.lower()
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    c2 = _client(db, _config(tmp_path, "all-in-one", secret=None))
    assert "openapi" in c2.get("/api/openapi.json").json()


def test_ensure_user_rejects_non_ascii_digits(db):
    with pytest.raises(PermissionError):
        db.ensure_user("\u0663\u0663", "mallory")
