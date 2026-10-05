"""Local accounts (Phase 1): hashing, policy, TOTP, invites, login, revocation, gateway paths, secrecy."""

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import internal_auth, local_auth
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.clients.core_client import CoreClient, ProxyResponse
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database

SECRET = "s" * 40
PW = "correct horse battery staple"


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


def _config(tmp_path: Path, role: str = "core", **extra: Any) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET,
        trackseerr_core_url="http://core.internal:5251",
        user_request_quota=10,
        **extra,
    )


def _client(db: Database, cfg: Config, peer: Optional[tuple[str, int]] = None) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app, client=peer) if peer else TestClient(app)


@pytest.fixture
def core(db, tmp_path):
    cfg = _config(tmp_path, "core")
    return _client(db, cfg), cfg


def _mk_local(db: Database, username: str = "bob", password: str = PW, perms: Optional[int] = None) -> dict[str, Any]:
    user, raw = db.create_local_user(username, f"{username}@x.tv", perms, "admin-1")
    assert db.consume_token_set_password(raw, local_auth.hash_password(password)) == user["id"]
    return db.get_user(user["id"])


def _login(client: TestClient, username: str = "bob", password: str = PW, **extra: Any):
    return client.post("/api/auth/local/login", json={"username": username, "password": password, **extra})


def _signed(method: str, target: str, user_id: str = "", user_name: str = "", body: bytes = b"", **kw: Any):
    headers = internal_auth.sign_assertion(SECRET, method, target, user_id, user_name, body, **kw)
    if body:
        headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------- hashing


def test_hash_format_and_verify():
    h = local_auth.hash_password(PW)
    assert re.fullmatch(r"scrypt\$32768\$8\$1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}", h)
    assert local_auth.verify_password(PW, h)
    assert not local_auth.verify_password(PW + "x", h)
    assert local_auth.hash_password(PW) != h  # fresh salt each time
    assert not local_auth.needs_rehash(h)


@pytest.mark.parametrize("bad", ["", "plain", "scrypt$1$2$3$a$b", "scrypt$32768$8$1$!!$!!", "bcrypt$1$2$3$AAAA$AAAA"])
def test_malformed_hashes_never_verify(bad):
    assert local_auth.verify_password(PW, bad) is False
    assert local_auth.verify_password(PW, None) is False


def test_old_parameters_verify_then_rehash_on_login(core, db):
    user = _mk_local(db)
    salt = b"0123456789abcdef"
    weak = hashlib.scrypt(PW.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    b64 = lambda b: __import__("base64").b64encode(b).decode().rstrip("=")  # noqa: E731
    stored = f"scrypt$16384$8$1${b64(salt)}${b64(weak)}"
    db.update_password_hash(user["id"], stored)
    assert local_auth.verify_password(PW, stored) and local_auth.needs_rehash(stored)
    client, _ = core
    assert _login(client).status_code == 200
    upgraded = db.get_local_credentials(user["id"])["password_hash"]
    assert upgraded != stored and not local_auth.needs_rehash(upgraded)
    assert local_auth.verify_password(PW, upgraded)


def test_unknown_user_still_runs_scrypt(core, db):
    client, _ = core
    with patch.object(local_auth.hashlib, "scrypt", wraps=hashlib.scrypt) as spy:
        res = _login(client, "nobody-here", PW)
    assert res.status_code == 401
    assert spy.called


# --------------------------------------------------------------------------- password policy


@pytest.mark.parametrize(
    "password,username,expected",
    [
        ("short", "bob", "at least 12"),
        ("x" * 129, "bob", "at most 128"),
        ("my-BOB-is-here-ok", "bob", "username"),
        ("PasswordPassword", "bob", "too common"),
        ("123456789012", "bob", "too common"),
    ],
)
def test_password_policy_rejections(password, username, expected):
    assert expected in (local_auth.validate_password(password, username) or "")


def test_password_policy_accepts_good_password():
    assert local_auth.validate_password(PW, "bob") is None
    assert local_auth.validate_password("x" * 128, "bob") is None


def test_common_password_list_is_bundled():
    words = local_auth.common_passwords()
    assert len(words) == 1000 and all(w == w.lower() for w in words)


# --------------------------------------------------------------------------- TOTP


RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of the RFC 6238 ASCII seed "12345678901234567890"


@pytest.mark.parametrize(
    "t,code8",
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_rfc6238_vectors(t, code8):
    assert local_auth.totp_code(RFC_SECRET, t, digits=8) == code8
    assert local_auth.totp_code(RFC_SECRET, t) == code8[-6:]
    assert local_auth.verify_totp(RFC_SECRET, code8[-6:], None, for_time=t) == t // 30


def test_totp_window_and_replay():
    t = 1111111109
    prev = local_auth.totp_code(RFC_SECRET, t - 30)
    nxt = local_auth.totp_code(RFC_SECRET, t + 30)
    far = local_auth.totp_code(RFC_SECRET, t + 90)
    assert local_auth.verify_totp(RFC_SECRET, prev, None, for_time=t) == t // 30 - 1
    assert local_auth.verify_totp(RFC_SECRET, nxt, None, for_time=t) == t // 30 + 1
    assert local_auth.verify_totp(RFC_SECRET, far, None, for_time=t) is None
    current = local_auth.totp_code(RFC_SECRET, t)
    counter = local_auth.verify_totp(RFC_SECRET, current, None, for_time=t)
    assert local_auth.verify_totp(RFC_SECRET, current, counter, for_time=t) is None  # replay
    assert local_auth.verify_totp(RFC_SECRET, prev, counter, for_time=t) is None  # older step
    assert local_auth.verify_totp(RFC_SECRET, "abcdef", None, for_time=t) is None
    assert local_auth.verify_totp(RFC_SECRET, "12345", None, for_time=t) is None


def test_totp_secret_shape_and_uri():
    s = local_auth.generate_totp_secret()
    assert re.fullmatch(r"[A-Z2-7]{32}", s)
    uri = local_auth.otpauth_uri(s, "bob")
    assert uri.startswith("otpauth://totp/TrackSeerr%3Abob?secret=" + s)


# --------------------------------------------------------------------------- recovery codes & tokens


def test_recovery_codes_format_and_single_use(db):
    user = _mk_local(db)
    codes = local_auth.generate_recovery_codes()
    assert len(codes) == 10 == len(set(codes))
    assert all(re.fullmatch(r"[0-9a-hj-km-np-tv-z]{4}(-[0-9a-hj-km-np-tv-z]{4}){2}", c) for c in codes)
    db.replace_recovery_codes(user["id"], [local_auth.hash_recovery_code(c) for c in codes])
    assert db.count_recovery_codes(user["id"]) == 10
    h = local_auth.hash_recovery_code(codes[0])
    assert db.consume_recovery_code(user["id"], h) is True
    assert db.consume_recovery_code(user["id"], h) is False
    assert db.count_recovery_codes(user["id"]) == 9
    assert local_auth.normalize_recovery_code(codes[1].upper().replace("-", " ")) == codes[1]


def test_tokens_are_stored_hashed_only(db):
    user, raw = db.create_local_user("carol", None, None, "admin-1")
    assert user["id"].startswith("local-") and user["auth_type"] == "local" and user["permissions"] == 34
    rows = db.conn.execute("SELECT * FROM user_invites").fetchall()
    assert len(rows) == 1 and rows[0]["token_hash"] == local_auth.hash_token(raw) != raw
    assert raw not in json.dumps([dict(r) for r in rows])
    with pytest.raises(ValueError):
        db.create_local_user("CAROL", None, None, "admin-1")  # case-insensitive duplicate


# --------------------------------------------------------------------------- usernames / identities


def test_local_username_unique_against_plex_usernames(db):
    with pytest.raises(ValueError, match="already taken"):
        db.create_local_user("Alice", None, None, "admin-1")
    for bad in ("ab", "x" * 33, "has space", "bad!", "gateway_service"):
        with pytest.raises(ValueError):
            db.create_local_user(bad, None, None, "admin-1")
    with pytest.raises(ValueError):
        db.create_local_user("okname", None, 1 << 20, "admin-1")


def test_ensure_user_refuses_tombstoned_and_never_creates_local(db):
    local = _mk_local(db)
    assert db.ensure_user(local["id"], "whatever")["id"] == local["id"]  # existing local row is returned
    with pytest.raises(PermissionError):
        db.ensure_user("local-" + "0" * 24, "ghost")
    assert db.get_user("local-" + "0" * 24) is None
    db.add_tombstone("1001", "admin-1")
    with pytest.raises(PermissionError):
        db.ensure_user("1001", "alice")
    db.add_tombstone("2002", "admin-1")
    with pytest.raises(PermissionError):
        db.ensure_user("2002", "newbie")
    assert db.get_user("2002") is None


# --------------------------------------------------------------------------- invites


def _invite_token(db, name="dave"):
    user, raw = db.create_local_user(name, None, None, "admin-1")
    return user, raw


def test_invite_info_and_accept(core, db):
    client, _ = core
    user, raw = _invite_token(db)
    info = client.get(f"/api/auth/invite/{raw}")
    assert info.status_code == 200
    assert set(info.json()) == {"username", "purpose", "expires_at"}
    assert info.json()["username"] == "dave" and info.json()["purpose"] == "invite"
    assert raw not in info.text and "token_hash" not in info.text
    weak = client.post(f"/api/auth/invite/{raw}", json={"password": "short"})
    assert weak.status_code == 400  # policy failure does not burn the token
    assert client.get(f"/api/auth/invite/{raw}").status_code == 200
    ok = client.post(f"/api/auth/invite/{raw}", json={"password": PW})
    assert ok.status_code == 200
    assert _login(client, "dave").status_code == 200


def test_invite_reused_expired_and_voided(core, db):
    client, _ = core
    user, raw = _invite_token(db)
    assert client.post(f"/api/auth/invite/{raw}", json={"password": PW}).status_code == 200
    assert client.post(f"/api/auth/invite/{raw}", json={"password": PW + "2"}).status_code == 404
    assert client.get(f"/api/auth/invite/{raw}").status_code == 404

    user2, raw2 = _invite_token(db, "erin")
    db.conn.execute("UPDATE user_invites SET expires_at = '2000-01-01T00:00:00+00:00'")
    db.conn.commit()
    assert client.get(f"/api/auth/invite/{raw2}").status_code == 404
    assert client.post(f"/api/auth/invite/{raw2}", json={"password": PW}).status_code == 404

    user3, old = _invite_token(db, "frank")
    new = db.issue_token(user3["id"], "invite", "admin-1")
    assert client.get(f"/api/auth/invite/{old}").status_code == 404
    assert client.get(f"/api/auth/invite/{new}").status_code == 200
    assert client.get("/api/auth/invite/not-a-real-token-at-all-0000").status_code == 404
    assert client.get("/api/auth/invite/short").status_code == 404


def test_invite_per_ip_rate_limit_on_core(core, db):
    client, _ = core
    codes = [client.get("/api/auth/invite/" + "a" * 40).status_code for _ in range(12)]
    assert codes[:10] == [404] * 10 and codes[10] == 429


def test_reset_token_revokes_sessions(core, db):
    client, _ = core
    user = _mk_local(db)
    assert _login(client).status_code == 200
    assert client.get("/api/account").status_code == 200
    raw = db.issue_token(user["id"], "reset", "admin-1")
    assert client.get(f"/api/auth/invite/{raw}").json()["purpose"] == "reset"
    assert client.post(f"/api/auth/invite/{raw}", json={"password": PW + "!new"}).status_code == 200
    assert client.get("/api/account").status_code == 401  # old session revoked
    assert _login(client, password=PW).status_code == 401
    assert _login(client, password=PW + "!new").status_code == 200


# --------------------------------------------------------------------------- login


def test_login_success_sets_cookie_like_plex(core, db):
    client, _ = core
    _mk_local(db)
    res = _login(client, "BOB")
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["username"] == "bob" and body["mfa_enrollment_required"] is False
    cookie = res.headers["set-cookie"].lower()
    assert "session_token=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/me").json()["user"]["username"] == "bob"
    assert db.get_local_credentials(body["user"]["id"])["failed_logins"] == 0


def test_login_secure_cookie_over_https_proxy_header(core, db):
    client, _ = core
    _mk_local(db)
    res = client.post(
        "/api/auth/local/login",
        json={"username": "bob", "password": PW},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "secure" in res.headers["set-cookie"].lower()


def test_wrong_password_and_unknown_user_same_message(core, db):
    client, _ = core
    _mk_local(db)
    wrong = _login(client, "bob", "not the password at all")
    unknown = _login(client, "ghost-user", "not the password at all")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid username or password"}


def test_plex_user_and_unredeemed_invite_cannot_password_login(core, db):
    client, _ = core
    assert _login(client, "alice", PW).status_code == 401
    db.create_local_user("pending", None, None, "admin-1")
    assert _login(client, "pending", PW).status_code == 401


def test_lockout_after_five_failures_then_423_even_with_right_password(core, db):
    client, _ = core
    _mk_local(db)
    for _ in range(5):
        assert _login(client, "bob", "wrong password here").status_code == 401
    res = _login(client, "bob", PW)
    assert res.status_code == 423 and res.json() == {"detail": "Account temporarily locked"}
    # An unknown name locks identically (no existence oracle).
    for _ in range(5):
        _login(client, "nobody", "wrong password here")
    assert _login(client, "nobody", "wrong password here").status_code == 423
    # Lock expires 15 minutes after it was set.
    db.conn.execute("UPDATE login_attempts SET attempted_at = attempted_at - 1000")
    db.conn.commit()
    assert _login(client, "bob", PW).status_code == 200


def test_ip_throttle_429_after_20_failures(core, db):
    client, _ = core
    for i in range(20):
        assert _login(client, f"user{i:02d}", "wrong password here").status_code == 401
    res = _login(client, "user99", "wrong password here")
    assert res.status_code == 429
    assert res.headers.get("retry-after")


def test_login_attempts_pruned_after_an_hour(db):
    now = int(time.time())
    db.record_login_attempt("ip:old", now - 7200)
    db.record_login_attempt("ip:new", now)
    keys = {r["key"] for r in db.conn.execute("SELECT key FROM login_attempts")}
    assert keys == {"ip:new"}


def test_disabled_user_cannot_login_and_generic_message(core, db):
    client, _ = core
    user = _mk_local(db)
    db.set_disabled(user["id"], True)
    res = _login(client)
    assert res.status_code == 401 and res.json() == {"detail": "Invalid username or password"}


# --------------------------------------------------------------------------- MFA


def _enroll(client: TestClient) -> tuple[str, list[str]]:
    setup = client.post("/api/account/mfa/setup", json={"password": PW})
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    confirm = client.post("/api/account/mfa/confirm", json={"code": local_auth.totp_code(secret)})
    assert confirm.status_code == 200
    return secret, confirm.json()["recovery_codes"]


def test_mfa_enrollment_login_flow_and_replay(core, db):
    client, _ = core
    user = _mk_local(db)
    assert _login(client).status_code == 200
    secret, codes = _enroll(client)
    assert len(codes) == 10
    me = client.get("/api/account").json()
    assert me["mfa_enabled"] is True and me["recovery_codes_remaining"] == 10
    assert secret not in client.get("/api/account").text

    fresh = TestClient(client.app)
    needs = _login(fresh)
    assert needs.status_code == 401 and needs.json() == {"detail": "mfa_required"}
    # A code for the next step is newer than the one used at enrollment; it works once.
    nxt = local_auth.totp_code(secret, time.time() + 30)
    ok = _login(fresh, totp_code=nxt)
    assert ok.status_code == 200
    replay = _login(TestClient(client.app), totp_code=nxt)
    assert replay.status_code == 401 and replay.json() == {"detail": "Invalid authentication code"}
    assert db.get_local_credentials(user["id"])["failed_logins"] >= 1


def test_recovery_code_login_is_single_use(core, db):
    client, _ = core
    _mk_local(db)
    _login(client)
    _secret, codes = _enroll(client)
    first = _login(TestClient(client.app), recovery_code=codes[0])
    assert first.status_code == 200
    second = _login(TestClient(client.app), recovery_code=codes[0])
    assert second.status_code == 401
    assert client.get("/api/account").json()["recovery_codes_remaining"] == 9


def test_mfa_disable_and_regenerate_require_password_and_code(core, db, monkeypatch):
    client, _ = core
    _mk_local(db)
    _login(client)
    secret, codes = _enroll(client)
    bad = client.post(
        "/api/account/mfa/recovery-codes",
        json={"password": "wrong password here", "code": local_auth.totp_code(secret, time.time() + 30)},
    )
    assert bad.status_code == 400
    good = client.post(
        "/api/account/mfa/recovery-codes",
        json={"password": PW, "code": local_auth.totp_code(secret, time.time() + 30)},
    )
    assert good.status_code == 200
    new_codes = good.json()["recovery_codes"]
    assert set(new_codes).isdisjoint(codes)
    assert _login(TestClient(client.app), recovery_code=codes[0]).status_code == 401  # old codes dead
    # Enrollment used step N and regeneration step N+1; replay protection needs N+2, which is only
    # inside the +-1 window once the clock has advanced a step.
    base = time.time()
    monkeypatch.setattr(local_auth.time, "time", lambda: base + 30)
    off = client.post(
        "/api/account/mfa/disable",
        json={"password": PW, "code": local_auth.totp_code(secret, base + 60)},
    )
    assert off.status_code == 200
    assert client.get("/api/account").json()["mfa_enabled"] is False


def test_require_mfa_restricts_session_until_enrolled(core, db):
    client, _ = core
    user = _mk_local(db)
    db.conn.execute("UPDATE general_settings SET require_mfa_local = 1")
    db.conn.commit()
    res = _login(client)
    assert res.status_code == 200 and res.json()["mfa_enrollment_required"] is True
    blocked = client.get("/api/users/me")
    assert blocked.status_code == 403 and blocked.json() == {"detail": "mfa_enrollment_required"}
    assert client.get("/api/requests").status_code == 403
    assert client.get("/api/account").status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    acct = client.get("/api/account").json()
    assert acct["mfa_required"] is True and acct["mfa_enabled"] is False
    _enroll(client)
    assert client.get("/api/users/me").status_code == 200
    # An administrator-required MFA cannot be switched off by the user.
    off = client.post("/api/account/mfa/disable", json={"password": PW, "code": "000000"})
    assert off.status_code == 409


def test_mfa_confirm_without_setup_and_wrong_code(core, db):
    client, _ = core
    _mk_local(db)
    _login(client)
    assert client.post("/api/account/mfa/confirm", json={"code": "123456"}).status_code == 400
    client.post("/api/account/mfa/setup", json={"password": PW})
    assert client.post("/api/account/mfa/confirm", json={"code": "000000"}).status_code == 400


def test_plex_user_cannot_use_password_or_mfa_endpoints(core, db):
    client, _ = core
    sbody = json.dumps({"password": PW}).encode()
    headers = _signed("POST", "/api/account/mfa/setup", "1001", "alice", sbody)
    assert client.post("/api/account/mfa/setup", content=sbody, headers=headers).status_code == 400
    body = json.dumps({"current_password": PW, "new_password": PW + "x"}).encode()
    headers = _signed("POST", "/api/account/password", "1001", "alice", body)
    assert client.post("/api/account/password", content=body, headers=headers).status_code == 400


# --------------------------------------------------------------------------- /api/account


def test_account_shape_local_user(core, db):
    client, _ = core
    _mk_local(db)
    _login(client)
    res = client.get("/api/account")
    assert res.status_code == 200
    data = res.json()
    assert set(data) == {
        "id", "username", "auth_type", "mfa_enabled", "mfa_required",
        "recovery_codes_remaining", "quotas", "auto_approve",
    }
    assert data["auth_type"] == "local" and data["mfa_enabled"] is False and data["mfa_required"] is False
    assert data["recovery_codes_remaining"] == 0
    assert data["quotas"] == {
        "tracks": 25, "albums": 10, "discographies": 1, "window_days": 7,
        "used": {"tracks": 0, "albums": 0, "discographies": 0},
    }
    assert data["auto_approve"] == {"tracks": False, "albums": False, "discographies": False}


def test_account_quota_overrides_usage_and_auto_approve(core, db):
    client, _ = core
    user = _mk_local(db, perms=2 | 8 | 64)
    db.conn.execute(
        "UPDATE users SET quota_albums = 3, quota_window_days = 30 WHERE id = ?", (user["id"],)
    )
    db.conn.execute("UPDATE general_settings SET default_quota_tracks = 40")
    for rid, kind, status in (("r1", "album", "pending"), ("r2", "album", "rejected"), ("r3", "track", "approved")):
        db.conn.execute(
            "INSERT INTO music_requests (id, user_id, item_type, title, artist, status) VALUES (?,?,?,?,?,?)",
            (rid, user["id"], kind, "t", "a", status),
        )
    db.conn.commit()
    _login(client)
    data = client.get("/api/account").json()
    assert data["quotas"]["albums"] == 3 and data["quotas"]["tracks"] == 40 and data["quotas"]["window_days"] == 30
    assert data["quotas"]["used"] == {"tracks": 1, "albums": 1, "discographies": 0}
    assert data["auto_approve"] == {"tracks": False, "albums": True, "discographies": True}


def test_account_shape_plex_forwarded_user(core, db):
    client, _ = core
    res = client.get("/api/account", headers=_signed("GET", "/api/account", "1001", "alice"))
    assert res.status_code == 200
    data = res.json()
    assert data["auth_type"] == "plex" and data["mfa_required"] is False and data["id"] == "1001"


def test_change_password_revokes_older_sessions_and_reissues(core, db):
    client, cfg = core
    user = _mk_local(db)
    _login(client)
    other = TestClient(client.app)
    _login(other)
    assert other.get("/api/account").status_code == 200
    bad = client.post("/api/account/password", json={"current_password": "nope nope nope", "new_password": PW + "2"})
    assert bad.status_code == 400
    weak = client.post("/api/account/password", json={"current_password": PW, "new_password": "short"})
    assert weak.status_code == 400
    ok = client.post("/api/account/password", json={"current_password": PW, "new_password": PW + "2"})
    assert ok.status_code == 200 and ok.json() == {"status": "success"}
    assert other.get("/api/account").status_code == 401  # other session revoked
    assert client.get("/api/account").status_code == 200  # current session re-issued
    assert _login(TestClient(client.app), password=PW + "2").status_code == 200


# --------------------------------------------------------------------------- revocation / disabled / tombstone


def test_forwarded_session_older_than_revocation_is_rejected(core, db):
    client, _ = core
    user = _mk_local(db)
    # The session must be issued after the invite was accepted (which itself revokes older sessions).
    issued = int(time.time() * 1_000_000)
    ok = client.get("/api/account", headers=_signed("GET", "/api/account", user["id"], "bob", session_issued_at=issued))
    assert ok.status_code == 200
    time.sleep(0.01)
    db.set_password(user["id"], local_auth.hash_password(PW + "n"))
    stale = client.get("/api/account", headers=_signed("GET", "/api/account", user["id"], "bob", session_issued_at=issued))
    assert stale.status_code == 401 and stale.json() == {"detail": "Session has expired or was revoked"}
    missing = client.get("/api/account", headers=_signed("GET", "/api/account", user["id"], "bob"))
    assert missing.status_code == 401
    newer = int(time.time() * 1_000_000) + 1_000_000
    fresh = client.get("/api/account", headers=_signed("GET", "/api/account", user["id"], "bob", session_issued_at=newer))
    assert fresh.status_code == 200


def test_forwarded_disabled_and_tombstoned_rejected(core, db):
    client, _ = core
    user = _mk_local(db)
    issued = int(time.time() * 1_000_000) + 10_000_000
    hdr = lambda uid, name: _signed("GET", "/api/account", uid, name, session_issued_at=issued)  # noqa: E731
    db.set_disabled(user["id"], True)
    assert client.get("/api/account", headers=hdr(user["id"], "bob")).status_code == 401
    db.set_disabled(user["id"], False)
    db.conn.execute("UPDATE users SET sessions_revoked_at = NULL WHERE id = ?", (user["id"],))
    db.conn.commit()
    assert client.get("/api/account", headers=hdr(user["id"], "bob")).status_code == 200
    db.add_tombstone("1001", "admin-1")
    assert client.get("/api/account", headers=hdr("1001", "alice")).status_code == 401


def test_direct_session_disabled_and_tombstoned_rejected(core, db):
    client, _ = core
    user = _mk_local(db)
    _login(client)
    assert client.get("/api/account").status_code == 200
    db.conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user["id"],))
    db.conn.commit()
    assert client.get("/api/account").status_code == 401
    db.conn.execute("UPDATE users SET disabled = 0 WHERE id = ?", (user["id"],))
    db.conn.commit()
    assert client.get("/api/account").status_code == 200
    db.add_tombstone(user["id"], "admin-1")
    assert client.get("/api/account").status_code == 401


def test_direct_session_issued_before_revocation_rejected(core, db):
    client, _ = core
    user = _mk_local(db)
    _login(client)
    db.conn.execute(
        "UPDATE users SET sessions_revoked_at = '2999-01-01T00:00:00+00:00' WHERE id = ?", (user["id"],)
    )
    db.conn.commit()
    assert client.get("/api/account").status_code == 401


def test_plex_login_refuses_disabled_and_tombstoned(db, tmp_path):
    cfg = _config(tmp_path, "all-in-one")
    client = _client(db, cfg)
    plex_user = {"id": "1001", "username": "alice", "email": "al@x.tv", "thumb": ""}
    with patch("plex_playlist_sync.api.routes.auth.check_plex_pin", return_value="tok"), patch(
        "plex_playlist_sync.api.routes.auth.verify_server_access", return_value=(True, False)
    ), patch("plex_playlist_sync.api.routes.auth.get_plex_user", return_value=plex_user), patch.dict(
        "os.environ", {"PLEX_MACHINE_IDENTIFIER": "m1"}
    ):
        assert client.post("/api/auth/plex/verify", json={"pin_id": 1}).status_code == 200
        db.conn.execute("UPDATE users SET disabled = 1 WHERE id = '1001'")
        db.conn.commit()
        assert client.post("/api/auth/plex/verify", json={"pin_id": 1}).status_code == 403
        db.conn.execute("UPDATE users SET disabled = 0 WHERE id = '1001'")
        db.add_tombstone("1001", "admin-1")
        assert client.post("/api/auth/plex/verify", json={"pin_id": 1}).status_code == 403


# --------------------------------------------------------------------------- /api/internal and gateway


def test_internal_is_404_for_everyone_but_service_principal(core, db):
    client, _ = core
    _mk_local(db)
    body = json.dumps({"username": "bob", "password": PW, "client_ip": "198.51.100.7"}).encode()
    url = "/api/internal/auth/local/verify"
    assert client.post(url, content=body, headers={"Content-Type": "application/json"}).status_code == 404
    admin_session = _mk_local(db, "adm", PW, perms=1)
    anon = TestClient(client.app)
    assert _login(anon, "adm").status_code == 200
    assert anon.post(url, content=body, headers={"Content-Type": "application/json"}).status_code == 404
    as_user = client.post(url, content=body, headers=_signed("POST", url, "1001", "alice", body))
    assert as_user.status_code == 404
    forged = _signed("POST", url, "", "", body)
    forged["X-TS-Signature"] = "0" * 64
    assert client.post(url, content=body, headers=forged).status_code == 404
    ok = client.post(url, content=body, headers=_signed("POST", url, "", "", body))
    assert ok.status_code == 200
    data = ok.json()
    assert data["user"]["username"] == "bob" and data["mfa_required"] is False
    assert "password" not in ok.text and "scrypt$" not in ok.text


def test_internal_verify_throttles_by_body_ip_and_returns_generic_errors(core, db):
    client, _ = core
    _mk_local(db)
    url = "/api/internal/auth/local/verify"

    def call(password, ip="198.51.100.7"):
        body = json.dumps({"username": "bob", "password": password, "client_ip": ip}).encode()
        return client.post(url, content=body, headers=_signed("POST", url, "", "", body))

    for _ in range(5):
        assert call("wrong password here").json() == {"detail": "Invalid username or password"}
    assert call(PW).status_code == 423
    for i in range(20):
        body = json.dumps({"username": f"u{i}", "password": "wrong password here", "client_ip": "192.0.2.1"}).encode()
        client.post(url, content=body, headers=_signed("POST", url, "", "", body))
    assert call("x" * 12, ip="192.0.2.1").status_code == 429


@pytest.fixture
def gateway(db, tmp_path):
    cfg = _config(tmp_path, "gateway")
    return _client(db, cfg), cfg


def test_gateway_login_via_mocked_core_verify(gateway, db):
    client, _ = gateway
    reply = (200, {"user": {"id": "local-" + "a" * 24, "username": "bob"}, "mfa_required": False,
                   "mfa_enrollment_required": False, "session_floor_us": 0})
    with patch.object(CoreClient, "local_verify", return_value=reply) as verify:
        res = _login(client, "bob", PW)
    assert res.status_code == 200
    sent = verify.call_args.args[0]
    assert sent["username"] == "bob" and sent["password"] == PW and "client_ip" in sent
    assert "session_token=" in res.headers["set-cookie"] and "httponly" in res.headers["set-cookie"].lower()
    assert res.json() == {"user": {"id": "local-" + "a" * 24, "username": "bob", "is_admin": False},
                          "mfa_enrollment_required": False}
    mirrored = db.get_user("local-" + "a" * 24)
    assert mirrored["is_admin"] is False and db.get_local_credentials(mirrored["id"])["password_hash"] is None
    assert client.get("/api/auth/me").json()["user"]["username"] == "bob"


@pytest.mark.parametrize(
    "code,detail",
    [(401, "Invalid username or password"), (401, "mfa_required"), (401, "Invalid authentication code"),
     (423, "Account temporarily locked"), (429, "Too many attempts. Please try again later.")],
)
def test_gateway_relays_core_login_errors_verbatim(gateway, code, detail):
    client, _ = gateway
    with patch.object(CoreClient, "local_verify", return_value=(code, {"detail": detail})):
        res = _login(client)
    assert res.status_code == code and res.json() == {"detail": detail}
    assert "set-cookie" not in res.headers


def test_gateway_login_hides_unexpected_core_errors(gateway):
    client, _ = gateway
    with patch.object(CoreClient, "local_verify", return_value=(500, {"detail": "boom: internal path /etc"})):
        res = _login(client)
    assert res.status_code == 502 and "boom" not in res.text


def test_gateway_login_core_unreachable(gateway):
    import httpx

    client, _ = gateway
    with patch.object(CoreClient, "local_verify", side_effect=httpx.ConnectError("down")):
        assert _login(client).status_code == 502


def test_gateway_never_exposes_internal_or_admin(gateway):
    client, _ = gateway
    with patch.object(CoreClient, "proxy") as proxy:
        for method, path in (
            ("POST", "/api/internal/auth/local/verify"),
            ("GET", "/api/internal/anything"),
            ("GET", "/api/admin/users"),
            ("POST", "/api/admin/users"),
        ):
            assert client.request(method, path, content=b"{}").status_code == 404
    proxy.assert_not_called()


def test_gateway_forwards_invite_as_service_principal_and_rate_limits(gateway):
    client, _ = gateway
    fake = ProxyResponse(200, b'{"username": "dave"}', {"Content-Type": "application/json"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        res = client.get("/api/auth/invite/" + "t" * 43)
        assert res.status_code == 200
        args = proxy.call_args.args
        assert args[0] == "GET" and args[1].startswith("/api/auth/invite/") and args[4] is None
        post = client.post("/api/auth/invite/" + "t" * 43, json={"password": PW})
        assert post.status_code == 200 and proxy.call_args.args[0] == "POST"
        codes = [client.get("/api/auth/invite/" + "t" * 43).status_code for _ in range(10)]
    assert codes[-1] == 429 and codes[0] == 200


def test_gateway_forwards_account_as_user_with_issued_at(gateway, db):
    client, _ = gateway
    from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key

    db.mirror_local_user("local-" + "b" * 24, "bob")
    cfg = client.app.dependency_overrides[get_config]()
    token = create_session_token(
        user_id="local-" + "b" * 24, username="bob", is_admin=False,
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(token, "local-" + "b" * 24)
    fake = ProxyResponse(200, b'{"id": "x"}', {"Content-Type": "application/json"})
    with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
        for method, path in (("GET", "/api/account"), ("POST", "/api/account/mfa/setup"),
                             ("POST", "/api/account/mfa/confirm"), ("POST", "/api/account/mfa/disable")):
            res = client.request(method, path, headers={"Authorization": f"Bearer {token}"}, content=b"{}")
            assert res.status_code == 200
            user_info = proxy.call_args.args[4]
            assert user_info["id"] == "local-" + "b" * 24 and user_info["_session_issued_at_us"] > 0
    assert client.get("/api/account").status_code == 401  # no session, no forward


def test_gateway_reissues_session_after_password_change(gateway, db):
    client, cfg = gateway
    from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key

    uid = "local-" + "c" * 24
    db.mirror_local_user(uid, "carl")
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    tokens = []
    for _ in range(2):
        t = create_session_token(user_id=uid, username="carl", is_admin=False, secret_key=key)
        db.create_session(t, uid)
        tokens.append(t)
        time.sleep(0.01)
    floor = int(time.time() * 1_000_000) + 2_000_000
    core_reply = ProxyResponse(
        200, json.dumps({"status": "success", "reissue_session": True, "session_floor_us": floor}).encode(),
        {"Content-Type": "application/json"},
    )
    with patch.object(CoreClient, "proxy", return_value=core_reply):
        res = client.post("/api/account/password", headers={"Authorization": f"Bearer {tokens[0]}"}, content=b"{}")
    assert res.status_code == 200 and res.json() == {"status": "success"}
    assert "reissue_session" not in res.text and "session_floor_us" not in res.text
    assert "session_token=" in res.headers["set-cookie"]
    assert db.get_session(tokens[0]) is None and db.get_session(tokens[1]) is None
    new_token = res.cookies.get("session_token")
    assert new_token and db.get_session(new_token) is not None
    from plex_playlist_sync.storage import ts_to_us

    assert ts_to_us(db.get_session(new_token)["created_at"]) > floor


def test_core_client_signs_session_issued_at():
    cc = CoreClient("http://core:5251", secret=SECRET)
    h = cc._headers("GET", "/x", b"", {"id": "u", "username": "n", "_session_issued_at_us": 1700000000123456})
    assert h["X-TS-Session-Issued-At"] == "1700000000123456"
    v = internal_auth.verify_assertion(SECRET, "GET", "/x", h, internal_auth.body_sha256_hex(b""))
    assert v.session_issued_at == 1700000000123456
    tampered = dict(h, **{"X-TS-Session-Issued-At": "1"})
    with pytest.raises(internal_auth.InvalidAssertion):
        internal_auth.verify_assertion(SECRET, "GET", "/x", tampered, internal_auth.body_sha256_hex(b""))
    svc = internal_auth.sign_assertion(SECRET, "GET", "/x")
    assert internal_auth.verify_assertion(SECRET, "GET", "/y".replace("y", "x"), svc, internal_auth.body_sha256_hex(b"")).session_issued_at is None


# --------------------------------------------------------------------------- TRUSTED_PROXIES


def test_parse_trusted_proxies_skips_garbage():
    nets = local_auth.parse_trusted_proxies("10.0.0.0/8, 192.168.1.5 ,not-an-ip,,::1")
    assert len(nets) == 3
    assert local_auth.parse_trusted_proxies(None) == ()
    assert local_auth.parse_trusted_proxies("") == ()


def test_resolve_client_ip_rules():
    trusted = local_auth.parse_trusted_proxies("10.0.0.0/8")
    assert local_auth.resolve_client_ip("203.0.113.5", "1.2.3.4", trusted) == "203.0.113.5"  # untrusted peer
    assert local_auth.resolve_client_ip("10.0.0.2", "1.2.3.4", ()) == "10.0.0.2"  # nothing trusted
    assert local_auth.resolve_client_ip("10.0.0.2", "198.51.100.9", trusted) == "198.51.100.9"
    # Spoofed leading entries are ignored: walk right-to-left past trusted hops.
    assert local_auth.resolve_client_ip("10.0.0.2", "6.6.6.6, 198.51.100.9, 10.0.0.9", trusted) == "198.51.100.9"
    assert local_auth.resolve_client_ip("10.0.0.2", "garbage", trusted) == "10.0.0.2"
    assert local_auth.resolve_client_ip("10.0.0.2", None, trusted) == "10.0.0.2"
    assert local_auth.resolve_client_ip("::ffff:10.0.0.2", "198.51.100.9", trusted) == "198.51.100.9"


def test_login_throttle_uses_forwarded_ip_only_via_trusted_proxy(db, tmp_path):
    cfg = _config(tmp_path, "all-in-one", trusted_proxies="10.0.0.0/8")
    client = _client(db, cfg, peer=("10.0.0.5", 5000))
    for i in range(20):
        _login(client, f"user{i:02d}", "wrong password here", ) if False else client.post(
            "/api/auth/local/login",
            json={"username": f"user{i:02d}", "password": "wrong password here"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
    blocked = client.post("/api/auth/local/login", json={"username": "x1", "password": "wrong password here"},
                          headers={"X-Forwarded-For": "203.0.113.9"})
    other = client.post("/api/auth/local/login", json={"username": "x2", "password": "wrong password here"},
                        headers={"X-Forwarded-For": "203.0.113.77"})
    assert blocked.status_code == 429 and other.status_code == 401

    untrusted = _config(tmp_path, "all-in-one")
    db2 = Database(":memory:")
    c2 = _client(db2, untrusted, peer=("198.51.100.1", 5000))
    for i in range(20):
        c2.post("/api/auth/local/login", json={"username": f"u{i:02d}", "password": "wrong password here"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
    spoof = c2.post("/api/auth/local/login", json={"username": "z", "password": "wrong password here"},
                    headers={"X-Forwarded-For": "203.0.113.200"})
    assert spoof.status_code == 429  # spoofed XFF did not evade the per-peer throttle
    db2.close()


# --------------------------------------------------------------------------- secrecy


def test_no_secret_ever_appears_in_responses(core, db):
    client, _ = core
    user = _mk_local(db)
    texts = []
    res = _login(client)
    texts.append(res.text)
    secret, codes = _enroll(client)
    texts.append(client.get("/api/account").text)
    texts.append(client.get("/api/auth/me").text)
    texts.append(client.get("/api/users/me").text)
    creds = db.get_local_credentials(user["id"])
    token_hashes = [r["token_hash"] for r in db.conn.execute("SELECT token_hash FROM user_invites")]
    code_hashes = [r["code_hash"] for r in db.conn.execute("SELECT code_hash FROM user_recovery_codes")]
    forbidden = [creds["password_hash"], "scrypt$", creds["totp_secret"], "password_hash", "totp_secret",
                 "totp_last_counter", *token_hashes, *code_hashes, PW]
    for text in texts:
        for needle in forbidden:
            assert needle not in text, needle
    # The pending secret is shown once by setup only; the confirm response carries only recovery codes.
    again = client.post("/api/account/mfa/setup", json={"password": PW})
    assert again.status_code == 409


def test_mfa_secret_not_returned_by_confirm_response(core, db):
    client, _ = core
    _mk_local(db)
    _login(client)
    setup = client.post("/api/account/mfa/setup", json={"password": PW}).json()
    confirm = client.post("/api/account/mfa/confirm", json={"code": local_auth.totp_code(setup["secret"])})
    assert setup["secret"] not in confirm.text


def test_access_log_redacts_invite_tokens():
    from plex_playlist_sync.cli import redact_sensitive_query

    line = 'GET /api/auth/invite/' + "a" * 43 + ' HTTP/1.1'
    assert "a" * 43 not in redact_sensitive_query(line)
    assert "a" * 43 not in redact_sensitive_query("GET /invite/" + "a" * 43)


def test_migration_v27_schema_and_defaults(db):
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(users)")}
    assert {"auth_type", "password_hash", "password_changed_at", "disabled", "sessions_revoked_at",
            "last_login_at", "totp_secret", "totp_last_counter", "failed_logins", "locked_until",
            "quota_tracks", "quota_albums", "quota_discographies", "quota_window_days"} <= cols
    assert db.get_account_settings() == {
        "require_mfa_local": False, "default_quota_tracks": 25, "default_quota_albums": 10,
        "default_quota_discographies": 1, "default_quota_window_days": 7,
    }
    top = db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert top == 41
    assert db.get_user("1001")["auth_type"] == "plex"


def test_create_local_user_returns_user_and_one_time_token(db):
    user, raw = db.create_local_user("newbie", "n@x.tv", 2 | 4, "admin-1")
    assert user["username"] == "newbie" and user["permissions"] == 6 and user["is_admin"] is False
    assert db.get_valid_token(raw)["username"] == "newbie"
    assert db.get_local_credentials(user["id"])["password_hash"] is None


# --------------------------------------------------------------------------- invite SPA route and log redaction


def test_access_log_filter_replaces_invite_token_with_marker():
    import logging as _logging

    from plex_playlist_sync.cli import RedactAccessLogFilter

    tok = "Zx9_-" * 9
    for path, expected in (
        (f"/invite/{tok}", "/invite/[REDACTED]"),
        (f"/api/auth/invite/{tok}", "/api/auth/invite/[REDACTED]"),
        ("/invite/short", "/invite/[REDACTED]"),
    ):
        record = _logging.LogRecord(
            "uvicorn.access", _logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", path, "1.1", 200), None,
        )
        assert RedactAccessLogFilter().filter(record)
        text = record.getMessage()
        assert expected in text and tok not in text


@pytest.mark.parametrize("tier", ["core", "gateway"])
def test_invite_spa_route_serves_index_only_for_exact_shape(tier, db, tmp_path):
    cfg = _config(tmp_path, tier)
    client = _client(db, cfg)
    tok = "a1B2" * 11
    res = client.get(f"/invite/{tok}")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    assert res.headers["cache-control"] == "no-store"
    assert client.get("/").text == res.text
    for bad in (f"/invite/{tok}/extra", "/invite/", "/invite/short", f"/invite/{tok}.", "/invite/" + "a" * 129,
                f"/invite/{tok}%2f..", "/invite/a%00" + "b" * 20, "/invites/" + tok, "/other/" + tok):
        assert client.get(bad).status_code in (404, 405), bad
    assert client.post(f"/invite/{tok}").status_code == 405


def test_invite_token_never_logged_by_routes(core, db, caplog):
    import logging as _logging

    client, _ = core
    user, raw = db.create_local_user("dave", "d@x.tv", None, "admin-1")
    with caplog.at_level(_logging.DEBUG):
        client.get(f"/api/auth/invite/{raw}")
        client.post(f"/api/auth/invite/{raw}", json={"password": PW})
        client.get(f"/invite/{raw}")
        client.get("/api/auth/invite/" + "q" * 43)
    # httpx logs the TestClient's own outgoing URL; only the server-side loggers matter here.
    server = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "asyncio")))
    assert raw not in server and PW not in server


def test_no_credential_ever_logged(core, db, caplog):
    import logging as _logging

    client, _ = core
    user = _mk_local(db)
    with caplog.at_level(_logging.DEBUG):
        _login(client, password="wrong password here")
        _login(client, "ghost", "ghost password here")
        _login(client)
        secret, codes = _enroll(client)
        _login(TestClient(client.app), recovery_code=codes[0])
        client.post("/api/account/password", json={"current_password": PW, "new_password": PW + "2"})
        creds = db.get_local_credentials(user["id"])
    server = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "asyncio")))
    for needle in (PW, "wrong password here", "ghost password here", secret, creds["password_hash"], *codes):
        assert needle and needle not in server, needle
