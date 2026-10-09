"""Security regressions for feature routes: playlist takeover, Last.fm secrets, webhook limits, 404-not-403."""

import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes.scrobbles import MAX_WEBHOOK_BYTES, _base_url
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.cli import RedactAccessLogFilter, install_access_log_redaction, redact_sensitive_query
from trackseerr.clients.scrobbler import LastFmClient, LastFmError
from trackseerr.config import Config
from trackseerr.storage import Database

SP_ID = "37i9dQZF1DXcBWIGoYBM5M"
SECRET = "SUPERSECRETTOKEN123"


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="http://plex", plex_token="tok", data_dir=str(tmp_path))


@pytest.fixture
def users(db):
    return {
        "admin": db.upsert_user("u-admin", "ronadmin", "a@x.io", is_admin=True),
        "alice": db.upsert_user("u-alice", "alice", "al@x.io", is_admin=False),
        "bob": db.upsert_user("u-bob", "bob", "b@x.io", is_admin=False),
    }


def hdr(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tc(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


# --- playlist takeover ------------------------------------------------------------------------


def test_sql_upsert_never_overwrites_existing_creator(db, users):
    db.upsert_playlist("pl1", "Mine", creator_id="u-alice")
    db.upsert_playlist("pl1", "Hijack", creator_id="u-bob")
    assert db.get_playlist("pl1")["creator_id"] == "u-alice"
    db.upsert_playlist("legacy", "Old", creator_id=None)
    db.upsert_playlist("legacy", "Old", creator_id="u-bob")
    assert db.get_playlist("legacy")["creator_id"] == "u-bob"


def test_user_cannot_take_over_playlist_via_create(tc, db, config, users):
    ha, hb, hadm = (hdr(users[k], db, config) for k in ("alice", "bob", "admin"))
    assert tc.post("/api/playlists", json={"url_or_id": SP_ID}, headers=ha).status_code == 201
    assert tc.put(f"/api/playlists/{SP_ID}/targets", json={"user_ids": ["u-alice", "u-bob", "u-admin"]}, headers=hadm).status_code == 200

    r = tc.post("/api/playlists", json={"url_or_id": SP_ID, "targets": ["u-bob"]}, headers=hb)
    assert r.status_code == 404
    row = db.get_playlist(SP_ID)
    assert row["creator_id"] == "u-alice"
    assert db.get_playlist_targets(SP_ID) == ["u-admin", "u-alice", "u-bob"]


def test_creator_recreate_keeps_other_targets(tc, db, config, users):
    ha, hadm = hdr(users["alice"], db, config), hdr(users["admin"], db, config)
    tc.post("/api/playlists", json={"url_or_id": SP_ID}, headers=ha)
    tc.put(f"/api/playlists/{SP_ID}/targets", json={"user_ids": ["u-alice", "u-bob"]}, headers=hadm)
    assert tc.post("/api/playlists", json={"url_or_id": SP_ID}, headers=ha).status_code == 201
    assert db.get_playlist_targets(SP_ID) == ["u-alice", "u-bob"]
    assert db.get_playlist(SP_ID)["creator_id"] == "u-alice"


def test_admin_create_keeps_creator_but_sets_targets(tc, db, config, users):
    ha, hadm = hdr(users["alice"], db, config), hdr(users["admin"], db, config)
    tc.post("/api/playlists", json={"url_or_id": SP_ID}, headers=ha)
    r = tc.post("/api/playlists", json={"url_or_id": SP_ID, "targets": ["u-bob"]}, headers=hadm)
    assert r.status_code == 201
    assert db.get_playlist(SP_ID)["creator_id"] == "u-alice"
    assert db.get_playlist_targets(SP_ID) == ["u-bob"]


def test_import_cannot_overwrite_other_users_row(tc, db, config, users):
    name, tracks = "Road Trip", [{"title": "T1"}]
    import_id = "imp_" + hashlib.sha256(f"u-bob_{name}_{len(tracks)}".encode()).hexdigest()[:12]
    db.upsert_playlist(import_id, "Alice's", creator_id="u-alice", tracks_json="[]")
    db.set_playlist_targets(import_id, ["u-alice", "u-admin"])
    r = tc.post("/api/playlists/import", json={"name": name, "tracks": tracks}, headers=hdr(users["bob"], db, config))
    assert r.status_code == 404
    assert db.get_playlist(import_id)["creator_id"] == "u-alice"
    assert db.get_playlist_targets(import_id) == ["u-admin", "u-alice"]


def test_import_by_creator_keeps_other_targets(tc, db, config, users):
    ha = hdr(users["alice"], db, config)
    body = {"name": "Mix", "tracks": [{"title": "T1"}]}
    pid = tc.post("/api/playlists/import", json=body, headers=ha).json()["id"]
    db.set_playlist_targets(pid, ["u-alice", "u-bob"])
    assert tc.post("/api/playlists/import", json=body, headers=ha).status_code == 201
    assert db.get_playlist_targets(pid) == ["u-alice", "u-bob"]


# --- Last.fm secrets ---------------------------------------------------------------------------


class _RaisingSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        raise requests.ConnectionError(f"HTTPSConnectionPool: /2.0/?token={SECRET}&api_sig=SIGSECRET&sk=SKSECRET")

    get = post


def test_lastfm_transport_error_never_leaks_token(caplog):
    session = _RaisingSession()
    client = LastFmClient("KEYSECRET", "shh", session=session)
    caplog.set_level(logging.DEBUG)
    with patch("trackseerr.clients.scrobbler.time.sleep"), pytest.raises(LastFmError) as ei:
        client.exchange_token_for_session(SECRET)
    blob = caplog.text + str(ei.value) + repr(ei.value)
    for secret in (SECRET, "SIGSECRET", "SKSECRET"):
        assert secret not in blob
    assert "ConnectionError" in caplog.text
    assert session.calls[0][0] == "POST" and "params" not in session.calls[0][2]


def test_scrobble_worker_logs_exception_type_only(caplog):
    from trackseerr.scrobble_worker import ScrobbleWorker

    worker = ScrobbleWorker()
    caplog.set_level(logging.DEBUG)
    server = SimpleNamespace(systemAccounts=lambda: (_ for _ in ()).throw(
        requests.ConnectionError(f"http://plex:32400/accounts?X-Plex-Token={SECRET}")))
    plex = SimpleNamespace(server=server, is_admin_username=lambda u: True)
    cfg = SimpleNamespace(plex_url="http://plex", plex_token=SECRET, plex_verify_ssl=True, plex_history_poll_minutes=1)
    db = Database(":memory:")
    try:
        with pytest.raises(Exception):
            worker.run_iteration(db, cfg, plex)
    finally:
        db.close()
    assert SECRET not in caplog.text


# --- Core APPLICATION_URL ----------------------------------------------------------------------


def _req(url="http://core.lan:8000/"):
    return SimpleNamespace(base_url=url)


def test_core_without_application_url_is_503(db):
    cfg = Config(plex_url="http://plex", plex_token="tok", role="core", application_url="")
    with pytest.raises(HTTPException) as ei:
        _base_url(_req(), db, cfg)
    assert ei.value.status_code == 503 and "APPLICATION_URL" in ei.value.detail


def test_core_with_application_url_and_all_in_one_fallback(db):
    core = Config(plex_url="http://plex", plex_token="tok", role="core", application_url="https://pub.example/")
    assert _base_url(_req(), db, core) == "https://pub.example"
    aio = Config(plex_url="http://plex", plex_token="tok", role="all-in-one", application_url="")
    assert _base_url(_req(), db, aio) == "http://core.lan:8000"


# --- webhook size ------------------------------------------------------------------------------


def test_webhook_rejects_oversized_content_length(tc, db):
    url = f"/api/scrobbles/plex?token={db.get_plex_webhook_secret()}"
    r = tc.post(url, content=b"x" * (MAX_WEBHOOK_BYTES + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_webhook_rejects_oversized_streamed_body(tc, db):
    url = f"/api/scrobbles/plex?token={db.get_plex_webhook_secret()}"

    def gen():
        for _ in range(3):
            yield b"x" * (MAX_WEBHOOK_BYTES // 2)  # no Content-Length: chunked

    r = tc.post(url, content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413


# --- access-log redaction ----------------------------------------------------------------------


def test_access_log_filter_redacts_sensitive_query_values():
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/api/scrobbles/plex?token=abc123&x=1&apikey=k1&api_key=k2&state=st&Token=up", "1.1", 200), None,
    )
    assert RedactAccessLogFilter().filter(rec) is True
    out = rec.getMessage()
    for leaked in ("abc123", "k1", "k2", "st&", "=up"):
        assert leaked not in out
    assert "x=1" in out and "token=REDACTED" in out


def test_redact_helper_and_installation_is_idempotent():
    assert redact_sensitive_query("/a?state=zzz") == "/a?state=REDACTED"
    assert redact_sensitive_query("/a?other=1") == "/a?other=1"
    install_access_log_redaction()
    install_access_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, RedactAccessLogFilter)]
    assert len(filters) == 1


# --- 404 not 403 -------------------------------------------------------------------------------


def test_other_users_mix_snapshot_is_404_not_403():
    from trackseerr.api.routes.plex_playlists import _authorized_snapshot

    snap = {"id": "s1", "plex_user": "admin"}
    fake_db = SimpleNamespace(get_mix_snapshot=lambda sid: snap)
    plex = object()
    user = {"id": "u-alice", "username": "alice", "is_admin": False}
    with pytest.raises(HTTPException) as ei:
        _authorized_snapshot(fake_db, user, plex, "s1")
    assert ei.value.status_code == 404


# --- requests route behaviour is unchanged by the shared submission ----------------------------


def test_requests_route_policy_responses(tc, db, config, users):
    ha = hdr(users["alice"], db, config)
    r = tc.post("/api/requests", json={"item_type": "track", "title": "Song", "artist": "Band"}, headers=ha)
    assert r.status_code == 201 and r.json()["status"] == "pending"
    dup = tc.post("/api/requests", json={"item_type": "track", "title": "song", "artist": "band"}, headers=ha)
    assert dup.status_code == 409
    db.update_user_governance("u-alice", request_limit_quota=1)
    over = tc.post("/api/requests", json={"item_type": "track", "title": "Other", "artist": "Band"}, headers=ha)
    assert over.status_code == 400 and "quota" in over.json()["detail"]
