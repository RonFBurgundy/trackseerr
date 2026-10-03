"""Security audit fixes: log redaction, gateway session enforcement, token voiding, lockout atomicity,
attempt-table bounds, throttle reset, legacy PUT guards and MFA setup re-authentication."""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import internal_auth, local_auth, local_login
from plex_playlist_sync.api import dependencies
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.api.routes.system import log_ring_buffer
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.core_client import CoreClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.local_login import LoginError, verify_local_login
from plex_playlist_sync.storage import Database

SECRET = "s" * 40
PW = "correct horse battery staple"
TOKEN = "T0kenSecretValue" + "x" * 27  # 43 chars, url-safe


@pytest.fixture(autouse=True)
def _fresh():
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


def _config(tmp_path: Path, role: str = "core") -> Config:
    return Config(
        application_url="https://music.example.com",
        plex_url="http://127.0.0.1:32400",
        plex_token="t",
        data_dir=str(tmp_path),
        role=role,
        internal_core_secret=SECRET,
        trackseerr_core_url="http://core.internal:5251",
    )


def _client(db: Database, cfg: Config) -> TestClient:
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    return TestClient(app)


def _mk_local(db: Database, username: str = "bob", password: str = PW) -> dict[str, Any]:
    user, raw = db.create_local_user(username, f"{username}@x.tv", None, "admin-1")
    assert db.consume_token_set_password(raw, local_auth.hash_password(password)) == user["id"]
    return db.get_user(user["id"])


def _bearer(db: Database, cfg: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _attempt_rows(db: Database) -> int:
    return int(db.conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0])


# --------------------------------------------------------------------------- 1. log redaction


@pytest.fixture
def real_logging(tmp_path):
    """Runs the real ``setup_logging`` and restores global logging state afterwards."""
    from plex_playlist_sync.cli import RedactLogFilter, setup_logging

    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    cfg = _config(tmp_path, "gateway")
    setup_logging("INFO", cfg)
    log_ring_buffer.buffer.clear()
    yield cfg
    for h in list(root.handlers):
        if h not in before_handlers:
            root.removeHandler(h)
            h.close()
    for h in root.handlers:
        h.filters = [f for f in h.filters if not isinstance(f, RedactLogFilter)]
    root.setLevel(before_level)
    for n, lvl in levels.items():
        logging.getLogger(n).setLevel(lvl)


def _all_log_text(cfg: Config, caplog) -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    parts = [caplog.text, json.dumps(list(log_ring_buffer.buffer))]
    for f in Path(cfg.data_dir).glob("*.log*"):
        parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_gateway_forwarded_invite_token_is_in_no_log_sink(db, real_logging, caplog):
    cfg = real_logging
    caplog.set_level(logging.DEBUG)
    client = _client(db, cfg)
    ok = httpx.Response(200, json={"username": "dave"})
    with patch.object(httpx.HTTPTransport, "handle_request", return_value=ok):
        res = client.get(f"/api/auth/invite/{TOKEN}")
        post = client.post(f"/api/auth/invite/{TOKEN}", json={"password": PW})
    assert res.status_code == 200 and post.status_code == 200
    assert TOKEN not in _all_log_text(cfg, caplog)


def test_httpx_and_httpcore_loggers_are_quiet(real_logging):
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_root_handlers_redact_tokens_and_secret_query_params(real_logging, caplog):
    cfg = real_logging
    secrets = ["SECRETINVITE1", "SECRETINVITE2", "k1", "k2", "k3", "k4", "k5", "k6"]
    logging.getLogger("some.module").info(
        "GET /invite/SECRETINVITE1 /api/auth/invite/SECRETINVITE2?token=k1&apikey=k2&api_key=k3&state=k4&sk=k5&api_sig=k6"
    )
    logging.getLogger("some.module").warning("relay %s", "/api/auth/invite/SECRETINVITE1")
    text = _all_log_text(cfg, caplog)
    for secret in secrets:
        assert secret not in text, secret
    assert "[REDACTED]" in text


# --------------------------------------------------------------------------- 2. gateway session status


class _Core:
    """Scriptable stand-in for core's session-status endpoint."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.reply: Any = (200, {"valid": True})

    def __call__(self, user_id: str, issued: int):
        self.calls.append((user_id, issued))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def gw(db, tmp_path, monkeypatch):
    cfg = _config(tmp_path, "gateway")
    client = _client(db, cfg)
    uid = "local-" + "b" * 24
    db.mirror_local_user(uid, "bob")
    core = _Core()
    monkeypatch.setattr(CoreClient, "session_status", lambda self, u, i: core(u, i))
    clock = {"t": 1000.0}
    monkeypatch.setattr(dependencies, "_monotonic", lambda: clock["t"])
    dependencies.clear_session_status_cache()
    return {"client": client, "db": db, "cfg": cfg, "uid": uid, "core": core, "clock": clock,
            "headers": _bearer(db, cfg, uid)}


@pytest.mark.parametrize("reason", ["disabled", "deleted", "revoked"])
def test_gateway_refuses_session_core_says_is_invalid_on_local_route(gw, reason):
    gw["core"].reply = (200, {"valid": False, "reason": reason})
    res = gw["client"].get("/api/users/me", headers=gw["headers"])
    assert res.status_code == 401
    token = gw["headers"]["Authorization"].split()[1]
    assert gw["db"].get_session(token) is None  # the gateway session is deleted
    assert gw["client"].get("/api/users/me", headers=gw["headers"]).status_code == 401


def test_gateway_disable_takes_effect_once_the_cache_ttl_lapses(gw):
    client, core, clock = gw["client"], gw["core"], gw["clock"]
    assert client.get("/api/users/me", headers=gw["headers"]).status_code == 200
    core.reply = (200, {"valid": False, "reason": "disabled"})
    clock["t"] += 59
    assert client.get("/api/users/me", headers=gw["headers"]).status_code == 200  # still cached
    clock["t"] += 2
    assert client.get("/api/users/me", headers=gw["headers"]).status_code == 401


def test_gateway_cache_hit_makes_no_core_call(gw):
    client, core = gw["client"], gw["core"]
    for _ in range(3):
        assert client.get("/api/users/me", headers=gw["headers"]).status_code == 200
    assert len(core.calls) == 1
    assert core.calls[0][0] == gw["uid"] and core.calls[0][1] > 0


def test_gateway_fails_closed_when_core_is_down(gw):
    gw["core"].reply = httpx.ConnectError("down")
    res = gw["client"].get("/api/users/me", headers=gw["headers"])
    assert res.status_code == 503 and res.json() == {"detail": "Core unavailable"}
    gw["core"].reply = (500, {"detail": "boom"})
    assert gw["client"].get("/api/users/me", headers=gw["headers"]).status_code == 503
    gw["core"].reply = (200, {"nonsense": True})
    assert gw["client"].get("/api/users/me", headers=gw["headers"]).status_code == 503


def test_gateway_mfa_enrollment_required_is_403_but_account_paths_stay_reachable(gw):
    gw["core"].reply = (200, {"valid": False, "reason": "mfa_enrollment_required"})
    res = gw["client"].get("/api/users/me", headers=gw["headers"])
    assert res.status_code == 403 and res.json()["detail"] == "mfa_enrollment_required"
    assert gw["client"].get("/api/auth/me", headers=gw["headers"]).status_code == 200


def _plex_login(client: TestClient):
    with patch("plex_playlist_sync.api.routes.auth.check_plex_pin", return_value="plex-token"), patch(
        "plex_playlist_sync.api.routes.auth.verify_server_access", return_value=(True, False)
    ), patch(
        "plex_playlist_sync.api.routes.auth.get_plex_user",
        return_value={"id": "5005", "username": "zed", "email": None},
    ):
        return client.post("/api/auth/plex/verify", json={"pin_id": 1, "target_machine_id": "m1"})


@pytest.mark.parametrize("reason", ["disabled", "deleted"])
def test_gateway_plex_login_refuses_users_core_has_disabled_or_removed(gw, reason):
    gw["core"].reply = (200, {"valid": False, "reason": reason})
    res = _plex_login(gw["client"])
    assert res.status_code == 403
    assert res.json() == {"detail": "Forbidden: this account is not permitted to sign in"}
    assert "set-cookie" not in res.headers and gw["db"].get_user("5005") is None
    assert gw["core"].calls and gw["core"].calls[-1][0] == "5005"


def test_gateway_plex_login_allowed_when_core_agrees_and_503_when_core_down(gw):
    assert _plex_login(gw["client"]).status_code == 200
    gw["core"].reply = httpx.ConnectError("down")
    res = _plex_login(gw["client"])
    assert res.status_code == 503 and res.json() == {"detail": "Core unavailable"}


def _signed(method: str, target: str, body: bytes = b"", user_id: str = "", user_name: str = ""):
    headers = internal_auth.sign_assertion(SECRET, method, target, user_id, user_name, body)
    headers["Content-Type"] = "application/json"
    return headers


def _status(client: TestClient, user_id: str, issued: int) -> Any:
    body = json.dumps({"user_id": user_id, "session_issued_at": issued}).encode()
    url = "/api/internal/auth/session-status"
    return client.post(url, content=body, headers=_signed("POST", url, body))


def test_core_session_status_endpoint_verdicts(db, tmp_path):
    client = _client(db, _config(tmp_path, "core"))
    user = _mk_local(db)
    uid = user["id"]
    now_us = int(time.time() * 1_000_000) + 5_000_000
    assert _status(client, uid, now_us).json() == {"valid": True}
    assert _status(client, "never-seen", now_us).json() == {"valid": True}
    db.revoke_sessions(uid)
    assert _status(client, uid, 1).json() == {"valid": False, "reason": "revoked"}
    assert _status(client, uid, now_us).json() == {"valid": True}
    db.update_account_settings({"require_mfa_local": True})
    assert _status(client, uid, now_us).json() == {"valid": False, "reason": "mfa_enrollment_required"}
    db.update_account_settings({"require_mfa_local": False})
    db.set_disabled(uid, True)
    assert _status(client, uid, now_us).json() == {"valid": False, "reason": "disabled"}
    db.add_tombstone(uid)
    assert _status(client, uid, now_us).json() == {"valid": False, "reason": "deleted"}


def test_core_session_status_is_service_principal_only(db, tmp_path):
    cfg = _config(tmp_path, "core")
    client = _client(db, cfg)
    url = "/api/internal/auth/session-status"
    body = json.dumps({"user_id": "1001", "session_issued_at": 1}).encode()
    assert client.post(url, content=body, headers=_bearer(db, cfg, "admin-1")).status_code == 404
    assert client.post(url, content=body).status_code == 404
    user_signed = _signed("POST", url, body, "1001", "alice")
    assert client.post(url, content=body, headers=user_signed).status_code == 404
    gw_cfg = _config(tmp_path, "gateway")
    assert _client(db, gw_cfg).post(url, content=body, headers=_signed("POST", url, body)).status_code in (400, 404)


# --------------------------------------------------------------------------- 3. tokens


def test_reset_voids_an_unused_invite_and_vice_versa(db, tmp_path):
    client = _client(db, _config(tmp_path))
    user, invite = db.create_local_user("dave", None, None, "admin-1")
    reset = db.issue_token(user["id"], "reset", "admin-1")
    assert db.get_valid_token(invite) is None
    assert client.get(f"/api/auth/invite/{invite}").status_code == 404
    assert client.post(f"/api/auth/invite/{invite}", json={"password": PW}).status_code == 404
    assert db.consume_token_set_password(invite, local_auth.hash_password(PW)) is None
    assert db.get_valid_token(reset)["purpose"] == "reset"
    again = db.issue_token(user["id"], "invite", "admin-1")
    assert db.get_valid_token(reset) is None and db.get_valid_token(again) is not None


# --------------------------------------------------------------------------- 4. lockout atomicity


def _counting_verify(counter: dict[str, int], delay: float = 0.02):
    real = local_auth.verify_password

    def wrapped(password: str, stored: str) -> bool:
        counter["n"] += 1
        time.sleep(delay)
        return real(password, stored)

    return wrapped


def test_concurrent_wrong_passwords_get_at_most_five_verifications(db, monkeypatch):
    _mk_local(db)
    counter = {"n": 0}
    monkeypatch.setattr(local_auth, "verify_password", _counting_verify(counter))
    outcomes: list[int] = []
    out_lock = threading.Lock()
    barrier = threading.Barrier(30)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            verify_local_login(
                db, username="bob", password="wrong password here", totp_code=None, recovery_code=None,
                client_ip=f"198.51.100.{i}",
            )
            code = 200
        except LoginError as exc:
            code = exc.status_code
        with out_lock:
            outcomes.append(code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter["n"] <= 5
    assert outcomes.count(401) == 5 and outcomes.count(423) == 25


def test_concurrent_totp_guesses_are_serialized_too(db, monkeypatch):
    user = _mk_local(db)
    secret = local_auth.generate_totp_secret()
    db.enable_totp(user["id"], secret, 1, [local_auth.hash_recovery_code(c) for c in local_auth.generate_recovery_codes()])
    calls = {"n": 0}
    real = local_auth.verify_totp

    def slow_totp(*a: Any, **k: Any):
        calls["n"] += 1
        time.sleep(0.02)
        return real(*a, **k)

    monkeypatch.setattr(local_auth, "verify_totp", slow_totp)
    barrier = threading.Barrier(25)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            verify_local_login(
                db, username="bob", password=PW, totp_code="000000", recovery_code=None,
                client_ip=f"203.0.113.{i}",
            )
        except LoginError:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] <= 5


def test_sixth_in_flight_attempt_from_one_ip_gets_429_immediately(db, monkeypatch):
    for name in ("usr1", "usr2", "usr3", "usr4", "usr5"):
        _mk_local(db, name)
    release = threading.Event()
    entered = {"n": 0}
    guard = threading.Lock()

    def blocking_verify(password: str, stored: str) -> bool:
        with guard:
            entered["n"] += 1
        release.wait(5)
        return False

    monkeypatch.setattr(local_auth, "verify_password", blocking_verify)
    results: list[int] = []

    def attempt(name: str) -> None:
        try:
            verify_local_login(db, username=name, password="wrong password here", totp_code=None,
                               recovery_code=None, client_ip="192.0.2.9")
        except LoginError as exc:
            results.append(exc.status_code)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("usr1", "usr2", "usr3", "usr4")]
    for t in threads:
        t.start()
    deadline = time.time() + 5
    while entered["n"] < 4 and time.time() < deadline:
        time.sleep(0.005)
    assert entered["n"] == 4
    started = time.time()
    with pytest.raises(LoginError) as err:
        verify_local_login(db, username="usr5", password="wrong password here", totp_code=None,
                           recovery_code=None, client_ip="192.0.2.9")
    assert err.value.status_code == 429 and time.time() - started < 1
    release.set()
    for t in threads:
        t.join()
    # Slots are released: the same IP can attempt again.
    with pytest.raises(LoginError) as again:
        verify_local_login(db, username="usr5", password="wrong password here", totp_code=None,
                           recovery_code=None, client_ip="192.0.2.9")
    assert again.value.status_code == 401


# --------------------------------------------------------------------------- 5. attempts table bounds


def test_login_flood_keeps_attempt_rows_bounded(db):
    _mk_local(db)
    for i in range(150):
        try:
            verify_local_login(db, username="bob", password="wrong password here", totp_code=None,
                               recovery_code=None, client_ip="198.51.100.77", now=1_000_000 + i % 5)
        except LoginError:
            pass
    assert _attempt_rows(db) <= local_login.IP_FAILURE_LIMIT + local_login.USER_FAILURE_LIMIT + 1


def test_locked_path_does_not_grow_the_table(db):
    _mk_local(db)
    for _ in range(5):
        with pytest.raises(LoginError):
            verify_local_login(db, username="bob", password="x" * 12, totp_code=None, recovery_code=None,
                               client_ip="198.51.100.1", now=5000)
    before = _attempt_rows(db)
    for i in range(100):
        with pytest.raises(LoginError):
            verify_local_login(db, username="bob", password="x" * 12, totp_code=None, recovery_code=None,
                               client_ip=f"198.51.100.{i % 3 + 1}", now=5000)
    assert before <= 11
    assert _attempt_rows(db) <= 3 * local_login.IP_FAILURE_LIMIT + 6


def test_invite_flood_keeps_attempt_rows_bounded_on_core_and_gateway(db, tmp_path):
    core = _client(db, _config(tmp_path, "core"))
    for _ in range(200):
        core.get("/api/auth/invite/" + "a" * 43)
    assert _attempt_rows(db) <= 11
    db.conn.execute("DELETE FROM login_attempts")
    db.conn.commit()
    gateway = _client(db, _config(tmp_path, "gateway"))
    ok = httpx.Response(404, json={"detail": "no"})
    with patch.object(httpx.HTTPTransport, "handle_request", return_value=ok):
        for _ in range(200):
            gateway.get("/api/auth/invite/" + "a" * 43)
    assert _attempt_rows(db) <= 11


def test_prune_runs_at_most_once_a_minute_and_attempted_at_is_indexed(db):
    ts = 10_000_000
    db.record_login_attempt("k", ts)  # first call prunes
    db.conn.execute("INSERT INTO login_attempts (key, attempted_at) VALUES ('old', ?)", (ts - 7200,))
    db.conn.commit()
    db.record_login_attempt("k", ts + 10)
    assert db.conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key = 'old'").fetchone()[0] == 1
    db.record_login_attempt("k", ts + 61)
    assert db.conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key = 'old'").fetchone()[0] == 0
    indexed = [
        [c[2] for c in db.conn.execute(f"PRAGMA index_info({i[1]})").fetchall()]
        for i in db.conn.execute("PRAGMA index_list(login_attempts)").fetchall()
    ]
    assert ["attempted_at"] in indexed
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 29


def test_record_login_attempt_respects_max_rows(db):
    assert [db.record_login_attempt("k", 100, max_rows=3) for _ in range(5)] == [True, True, True, False, False]
    assert _attempt_rows(db) == 3


# --------------------------------------------------------------------------- 6. throttle reset


def _seed_throttles(db: Database, name: str) -> None:
    for prefix in ("user", "lock"):
        db.record_login_attempt(f"{prefix}:{name}")
    db.record_login_attempt("user:other")


def _throttle_counts(db: Database, name: str) -> tuple[int, int, int]:
    since = int(time.time()) - 900
    return (
        db.login_attempt_stats(f"user:{name}", since)[0],
        db.login_attempt_stats(f"lock:{name}", since)[0],
        db.login_attempt_stats("user:other", since)[0],
    )


def test_accepting_an_invite_or_reset_clears_user_and_lock_rows(db):
    user, raw = db.create_local_user("erin", None, None, "admin-1")
    _seed_throttles(db, "erin")
    assert db.consume_token_set_password(raw, local_auth.hash_password(PW)) == user["id"]
    assert _throttle_counts(db, "erin") == (0, 0, 1)
    _seed_throttles(db, "erin")
    assert _throttle_counts(db, "erin") == (1, 1, 2)
    reset = db.issue_token(user["id"], "reset", "admin-1")
    assert db.consume_token_set_password(reset, local_auth.hash_password(PW + "2")) == user["id"]
    assert _throttle_counts(db, "erin") == (0, 0, 2)


def test_admin_reset_password_clears_lock_rows(db, tmp_path):
    cfg = _config(tmp_path)
    client = _client(db, cfg)
    user = _mk_local(db, "frank")
    _seed_throttles(db, "frank")
    res = client.post(f"/api/admin/users/{user['id']}/reset-password", headers=_bearer(db, cfg, "admin-1"))
    assert res.status_code == 200
    assert _throttle_counts(db, "frank") == (0, 0, 1)


# --------------------------------------------------------------------------- 7. legacy PUT


@pytest.fixture
def admin_env(db, tmp_path):
    cfg = _config(tmp_path)
    return {"client": _client(db, cfg), "admin": _bearer(db, cfg, "admin-1"), "db": db}


@pytest.mark.parametrize("payload", [{"request_limit_quota": 5}, {"request_limit_days": 7}, {"permissions": 34}, {}])
def test_legacy_put_refuses_api_key_entirely(admin_env, payload):
    key = {"X-Api-Key": admin_env["db"].get_api_key()}
    before = admin_env["db"].get_user_quota_overrides("1001")
    res = admin_env["client"].put("/api/users/1001", json=payload, headers=key)
    assert res.status_code == 403
    assert admin_env["db"].get_user_quota_overrides("1001") == before


@pytest.mark.parametrize(
    "payload",
    [{"request_limit_quota": -1}, {"request_limit_quota": 1_000_001}, {"request_limit_days": 0},
     {"request_limit_days": 3651}, {"request_limit_days": -5}],
)
def test_legacy_put_enforces_admin_route_bounds(admin_env, payload):
    res = admin_env["client"].put("/api/users/1001", json=payload, headers=admin_env["admin"])
    assert res.status_code == 422


def test_legacy_put_accepts_edge_values(admin_env):
    res = admin_env["client"].put(
        "/api/users/1001", json={"request_limit_quota": 1_000_000, "request_limit_days": 1}, headers=admin_env["admin"]
    )
    assert res.status_code == 200
    assert admin_env["client"].put(
        "/api/users/1001", json={"request_limit_quota": 0}, headers=admin_env["admin"]
    ).status_code == 200


# --------------------------------------------------------------------------- 8. MFA setup re-auth


@pytest.fixture
def acct(db, tmp_path):
    cfg = _config(tmp_path)
    client = _client(db, cfg)
    _mk_local(db, "gina")
    assert client.post("/api/auth/local/login", json={"username": "gina", "password": PW}).status_code == 200
    return client, db


def test_mfa_setup_requires_the_password(acct):
    client, _ = acct
    assert client.post("/api/account/mfa/setup").status_code == 422
    wrong = client.post("/api/account/mfa/setup", json={"password": "not the password"})
    assert wrong.status_code == 400 and wrong.json() == {"detail": "Invalid password"}
    ok = client.post("/api/account/mfa/setup", json={"password": PW})
    assert ok.status_code == 200 and ok.json()["secret"]


def test_wrong_mfa_setup_passwords_count_toward_the_lockout(acct):
    client, _ = acct
    for _ in range(5):
        assert client.post("/api/account/mfa/setup", json={"password": "nope nope nope"}).status_code == 400
    locked = client.post("/api/account/mfa/setup", json={"password": PW})
    assert locked.status_code == 429


# --------------------------------------------------------------------------- exception-text redaction / logging setup failure


def test_exception_traceback_is_redacted_in_handler_output_and_ring_buffer(real_logging):
    import io

    from plex_playlist_sync.cli import RedactLogFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactLogFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        try:
            raise ValueError("boom /api/auth/invite/SECRETTOKEN123456789")
        except ValueError:
            logging.getLogger("some.module").error("failed", exc_info=True)
    finally:
        root.removeHandler(handler)
    out = stream.getvalue()
    assert "ValueError" in out and "SECRETTOKEN" not in out
    for h in root.handlers:
        h.flush()
    assert "SECRETTOKEN" not in json.dumps(list(log_ring_buffer.buffer))


@pytest.mark.parametrize("role", ["gateway", "core"])
def test_logging_setup_failure_is_fatal_for_tiers(tmp_path, role, monkeypatch, capsys):
    from plex_playlist_sync import cli

    def broken(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(cli, "setup_logging", broken)
    with pytest.raises(OSError):
        create_app(db=Database(":memory:"), config=_config(tmp_path, role))
    assert "disk on fire" in capsys.readouterr().err


def test_logging_setup_failure_warns_but_continues_all_in_one(tmp_path, monkeypatch, capsys):
    from plex_playlist_sync import cli

    def broken(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(cli, "setup_logging", broken)
    cfg = _config(tmp_path, "all-in-one")
    cfg.internal_core_secret = None
    assert create_app(db=Database(":memory:"), config=cfg) is not None
    err = capsys.readouterr().err
    assert "WARNING" in err and "disk on fire" in err
