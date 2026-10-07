"""Tests for per-user scrobbling: storage, Last.fm/ListenBrainz clients, identity, webhook, API and worker."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.scrobbler import (
    LastFmClient,
    LastFmError,
    ListenBrainzClient,
    ListenBrainzError,
    lastfm_signature,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import UserScrobbleConfig
from plex_playlist_sync.scrobble_worker import ScrobbleWorker
from plex_playlist_sync.scrobbling import forward_listen, get_lastfm_credentials, resolve_plex_user
from plex_playlist_sync.storage import Database

SESSION_SECRET = "SECRETSESSIONKEY123"
LB_SECRET = "SECRETLBTOKEN456"


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Scripted requests.Session: pops one response (or exception) per call and records the calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, kind, url, **kwargs):
        self.calls.append((kind, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


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
        "alice": db.upsert_user("1001", "Alice", "al@x.io", is_admin=False),
        "bob": db.upsert_user("u-bob", "bob", "b@x.io", is_admin=False),
    }


def auth_headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}, token


def make_client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


@pytest.fixture
def lfm_config(tmp_path):
    return Config(
        plex_url="http://plex",
        plex_token="tok",
        data_dir=str(tmp_path),
        lastfm_api_key="envkey",
        lastfm_api_secret="envsecret",
    )


def multipart_body(payload: dict, boundary="XBOUNDARYX"):
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload"\r\n\r\n'
        f"{json.dumps(payload)}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="thumb"; filename="a.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + b"\xff\xd8\x00\x01binary\r\n" + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def scrobble_payload(account_id=1, title="ronadmin", **meta):
    metadata = {
        "type": "track",
        "title": "Song",
        "grandparentTitle": "Artist",
        "parentTitle": "Album",
        "ratingKey": "555",
        "duration": 200000,
    }
    metadata.update(meta)
    return {"event": "media.scrobble", "Account": {"id": account_id, "title": title}, "Metadata": metadata}


# ---------------------------------------------------------------------------
# Migration v25
# ---------------------------------------------------------------------------


def test_migration_v25_tables_and_columns(db):
    def cols(table):
        return {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 25
    assert {
        "user_id", "scrobbling_enabled", "lastfm_username", "lastfm_session_key",
        "listenbrainz_token", "listenbrainz_username", "created_at", "updated_at",
    } == cols("user_scrobble_configs")
    assert {
        "id", "user_id", "artist", "title", "album", "rating_key", "duration_ms", "played_at", "source",
        "lastfm_status", "listenbrainz_status", "forward_attempts", "last_forward_error",
    } == cols("user_listens")
    assert {"state", "user_id", "forward_url", "created_at"} == cols("lastfm_auth_states")
    assert {
        "id", "user_id", "mix_type", "name", "seed_artist", "track_count", "discovery_ratio",
        "seed_window_days", "excluded_genres_json", "auto_acquire_missing", "max_weekly_acquisitions",
        "quality_profile_id", "enabled", "last_generated_at", "last_result_json", "created_at", "updated_at",
    } == cols("tailored_mix_configs")
    assert {"mix_id", "request_id", "created_at"} == cols("mix_acquisitions")
    assert {
        "lastfm_api_key", "lastfm_api_secret", "plex_webhook_secret", "plex_history_poll_minutes"
    } <= cols("general_settings")
    names = {r[1] for r in db.conn.execute("PRAGMA index_list(user_listens)").fetchall()}
    assert {"idx_user_listens_lookup", "idx_user_listens_artist", "idx_user_listens_pending"} <= names


def test_webhook_secret_autogenerated_persisted_and_rotated(db):
    first = db.get_plex_webhook_secret()
    assert len(first) >= 32
    assert db.get_plex_webhook_secret() == first
    rotated = db.rotate_plex_webhook_secret()
    assert rotated != first and db.get_plex_webhook_secret() == rotated
    assert db.get_plex_history_poll_minutes() == 15


# ---------------------------------------------------------------------------
# Storage: config, listens, dedup, stats
# ---------------------------------------------------------------------------


def test_scrobble_config_upsert_and_unknown_field(db, users):
    assert db.get_scrobble_config("1001") is None
    cfg = db.upsert_scrobble_config("1001", lastfm_username="al", lastfm_session_key="sk")
    assert cfg["scrobbling_enabled"] is True and cfg["lastfm_session_key"] == "sk"
    cfg = db.upsert_scrobble_config("1001", scrobbling_enabled=False)
    assert cfg["scrobbling_enabled"] is False and cfg["lastfm_username"] == "al"
    with pytest.raises(ValueError):
        db.upsert_scrobble_config("1001", bogus=1)


def test_insert_listen_statuses_follow_credentials(db, users):
    plain = db.insert_listen("1001", "A", "T1", rating_key="1")
    assert db.get_listen(plain)["lastfm_status"] == "skipped"
    db.upsert_scrobble_config("1001", lastfm_session_key="sk", listenbrainz_token="lb")
    both = db.insert_listen("1001", "A", "T2", rating_key="2")
    row = db.get_listen(both)
    assert row["lastfm_status"] == "pending" and row["listenbrainz_status"] == "pending"
    db.upsert_scrobble_config("1001", scrobbling_enabled=False)
    off = db.get_listen(db.insert_listen("1001", "A", "T3", rating_key="3"))
    assert off["lastfm_status"] == "skipped" and off["listenbrainz_status"] == "skipped"


def test_dedup_with_rating_key_within_ten_minutes(db, users):
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert db.insert_listen("1001", "A", "T", rating_key="9", played_at=t0, source="plex_webhook") is not None
    assert db.insert_listen("1001", "A", "T", rating_key="9", played_at=t0 + timedelta(minutes=9), source="plex_history") is None
    assert db.insert_listen("1001", "A", "T", rating_key="9", played_at=t0 - timedelta(minutes=9)) is None
    assert db.insert_listen("1001", "A", "T", rating_key="9", played_at=t0 + timedelta(minutes=11)) is not None
    # other user / other rating key are independent
    assert db.insert_listen("u-bob", "A", "T", rating_key="9", played_at=t0) is not None
    assert db.insert_listen("1001", "A", "T", rating_key="10", played_at=t0) is not None


def test_dedup_without_rating_key_uses_lowercased_artist_title(db, users):
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert db.insert_listen("1001", "Artist", "Song", played_at=t0) is not None
    assert db.insert_listen("1001", "ARTIST", "song", played_at=t0 + timedelta(minutes=5)) is None
    assert db.insert_listen("1001", "Artist", "Other", played_at=t0 + timedelta(minutes=5)) is not None
    assert db.insert_listen("1001", "Artist", "Song", played_at=t0 + timedelta(minutes=30)) is not None


def test_listen_queries_and_stats(db, users):
    now = datetime.now(timezone.utc)
    for i in range(3):
        db.insert_listen("1001", "Alpha", "Song A", album="LP", played_at=now - timedelta(hours=i + 1), source="plex_history")
    db.insert_listen("1001", "Beta", "Song B", played_at=now - timedelta(days=30))
    since = (now - timedelta(days=7)).isoformat()
    assert db.top_artists("1001", since, 10) == [{"artist": "Alpha", "plays": 3}]
    tracks = db.top_tracks("1001", since, 10)
    assert tracks == [{"artist": "Alpha", "title": "Song A", "album": "LP", "plays": 3}]
    assert db.heard_track_keys("1001") == {("alpha", "song a"), ("beta", "song b")}
    listed = db.list_listens("1001", limit=2, offset=0)
    assert len(listed) == 2 and listed[0]["played_at"] >= listed[1]["played_at"]
    assert len(db.list_listens("1001", limit=10, offset=2)) == 2


def test_pending_forwards_age_attempts_and_marking(db, users):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk")
    now = datetime.now(timezone.utc)
    fresh = db.insert_listen("1001", "A", "Fresh", rating_key="1", played_at=now)
    old = db.insert_listen("1001", "A", "Old", rating_key="2", played_at=now - timedelta(days=20))
    exhausted = db.insert_listen("1001", "A", "Tired", rating_key="3", played_at=now - timedelta(hours=1))
    for _ in range(5):
        db.mark_forward_result(exhausted, "lastfm", "failed", "boom")
    ids = {r["id"] for r in db.list_pending_forwards()}
    assert ids == {fresh}
    assert old not in ids
    db.mark_forward_result(fresh, "lastfm", "sent")
    assert db.list_pending_forwards() == []
    row = db.get_listen(exhausted)
    assert row["forward_attempts"] == 5 and row["last_forward_error"] == "boom"
    with pytest.raises(ValueError):
        db.mark_forward_result(fresh, "spotify", "sent")


def test_auth_state_single_use_and_expiry(db, users):
    s = db.create_lastfm_auth_state("1001", "/settings")
    assert db.consume_lastfm_auth_state(s) == {"user_id": "1001", "forward_url": "/settings"}
    assert db.consume_lastfm_auth_state(s) is None
    old = db.create_lastfm_auth_state("1001", None)
    db.conn.execute(
        "UPDATE lastfm_auth_states SET created_at = datetime('now', '-11 minutes') WHERE state = ?", (old,)
    )
    assert db.consume_lastfm_auth_state(old) is None
    assert db.consume_lastfm_auth_state("") is None


def test_mix_config_and_acquisition_storage(db, users):
    mix = db.create_mix_config("1001", "discover_weekly", "DW", excluded_genres=["pop"], seed_artist="ignored")
    assert mix["seed_artist"] is None and mix["excluded_genres"] == ["pop"]
    assert mix["auto_acquire_missing"] is False and mix["enabled"] is True
    with pytest.raises(ValueError):
        db.create_mix_config("1001", "artist_radio", "R")
    with pytest.raises(ValueError):
        db.create_mix_config("1001", "daily_blend", "X", track_count=3)
    radio = db.create_mix_config("u-bob", "artist_radio", "Radio", seed_artist="Daft Punk")
    assert [m["id"] for m in db.list_mix_configs("1001")] == [mix["id"]]
    assert len(db.list_mix_configs()) == 2
    upd = db.update_mix_config(mix["id"], track_count=40, auto_acquire_missing=True)
    assert upd["track_count"] == 40 and upd["auto_acquire_missing"] is True
    assert db.update_mix_config("nope", track_count=40) is None
    db.record_mix_result(mix["id"], json.dumps({"total": 1}))
    got = db.get_mix_config(mix["id"])
    assert got["last_generated_at"] and json.loads(got["last_result_json"]) == {"total": 1}
    other = db.create_mix_config("1001", "daily_blend", "DB")
    db.add_mix_acquisition(mix["id"], "r1")
    db.add_mix_acquisition(other["id"], "r2")
    db.add_mix_acquisition(other["id"], "r2")
    db.add_mix_acquisition(radio["id"], "r3")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    assert db.count_mix_acquisitions_since("1001", week) == 2
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert db.count_mix_acquisitions_since("1001", future) == 0
    assert db.delete_mix_config(mix["id"]) is True and db.get_mix_config(mix["id"]) is None
    assert db.count_mix_acquisitions_since("1001", week) == 1


def test_user_scrobble_config_model_never_emits_secrets():
    cfg = UserScrobbleConfig(
        user_id="1", username="a", lastfm_username="la", lastfm_session_key=SESSION_SECRET,
        listenbrainz_token=LB_SECRET, listenbrainz_username="lb",
    )
    out = cfg.to_dict()
    assert out["lastfm_connected"] is True and out["listenbrainz_connected"] is True
    blob = json.dumps(out) + repr(cfg)
    assert SESSION_SECRET not in blob and LB_SECRET not in blob


# ---------------------------------------------------------------------------
# Last.fm client
# ---------------------------------------------------------------------------


def test_lastfm_signature_known_vector():
    params = {"method": "auth.getSession", "api_key": "xxx", "token": "yyy", "format": "json", "callback": "http://x"}
    expected = hashlib.md5(b"api_keyxxxmethodauth.getSessiontokenyyyzzz").hexdigest()
    assert lastfm_signature(params, "zzz") == expected
    # general rule: sorted key+value pairs, e.g. scrobble
    p2 = {"track": "T", "artist": "A", "timestamp": 100, "sk": "S", "api_key": "K", "method": "track.scrobble"}
    manual = "api_keyKartistAmethodtrack.scrobbleskSTimestamp100trackT".replace("Timestamp", "timestamp")
    assert lastfm_signature(p2, "sec") == hashlib.md5((manual + "sec").encode()).hexdigest()


def test_lastfm_auth_url_is_https_and_encodes_callback():
    url = LastFmClient("key", "sec").build_auth_url("https://ts.example/api/scrobbles/lastfm/callback?state=a b")
    assert url == (
        "https://www.last.fm/api/auth/?api_key=key&cb="
        "https%3A%2F%2Fts.example%2Fapi%2Fscrobbles%2Flastfm%2Fcallback%3Fstate%3Da+b"
    )


def test_lastfm_session_exchange_success_signs_request():
    session = FakeSession(FakeResponse(200, {"session": {"name": "ron", "key": "SK", "subscriber": 0}}))
    client = LastFmClient("xxx", "zzz", session=session)
    assert client.exchange_token_for_session("yyy") == ("ron", "SK")
    kind, url, kw = session.calls[0]
    assert url == "https://ws.audioscrobbler.com/2.0/"
    assert kind == "POST" and "params" not in kw  # token must not travel in the URL
    params = kw["data"]
    assert params["api_sig"] == hashlib.md5(b"api_keyxxxmethodauth.getSessiontokenyyyzzz").hexdigest()
    assert params["format"] == "json"


def test_lastfm_error_payload_with_http_200_raises():
    session = FakeSession(FakeResponse(200, {"error": 4, "message": "Invalid authentication token"}))
    with pytest.raises(LastFmError) as ei:
        LastFmClient("k", "s", session=session).exchange_token_for_session("bad")
    assert ei.value.code == 4 and ei.value.permanent


def test_lastfm_scrobble_posts_signed_form():
    session = FakeSession(FakeResponse(200, {"scrobbles": {}}))
    LastFmClient("K", "sec", session=session).scrobble("A", "T", 1700000000, "S", album="Al")
    kind, _, kw = session.calls[0]
    data = kw["data"]
    assert kind == "POST" and data["method"] == "track.scrobble" and data["sk"] == "S" and data["album"] == "Al"
    signed = {k: v for k, v in data.items() if k not in ("api_sig", "format")}
    assert data["api_sig"] == lastfm_signature(signed, "sec")


def test_lastfm_retries_on_503_then_succeeds():
    session = FakeSession(FakeResponse(503), FakeResponse(429), FakeResponse(200, {"scrobbles": {}}))
    with patch("plex_playlist_sync.clients.scrobbler.time.sleep") as slept:
        LastFmClient("K", "s", session=session).scrobble("A", "T", 1, "S")
    assert len(session.calls) == 3 and slept.call_count == 2


def test_lastfm_gives_up_after_three_attempts():
    session = FakeSession(FakeResponse(503), requests.ConnectionError("down"), FakeResponse(502))
    with patch("plex_playlist_sync.clients.scrobbler.time.sleep"):
        with pytest.raises(LastFmError) as ei:
            LastFmClient("K", "s", session=session).scrobble("A", "T", 1, "S")
    assert ei.value.code == 0 and not ei.value.permanent and len(session.calls) == 3


# ---------------------------------------------------------------------------
# ListenBrainz client
# ---------------------------------------------------------------------------


def test_listenbrainz_submit_listen():
    session = FakeSession(FakeResponse(200, {"status": "ok"}))
    ListenBrainzClient(session=session).submit_listen("TOK", "A", "T", release="R", timestamp=1700000000)
    method, url, kw = session.calls[0]
    assert method == "POST" and url == "https://api.listenbrainz.org/1/submit-listens"
    assert kw["headers"]["Authorization"] == "Token TOK"
    assert kw["json"]["listen_type"] == "single"
    entry = kw["json"]["payload"][0]
    assert entry["listened_at"] == 1700000000
    assert entry["track_metadata"] == {"artist_name": "A", "track_name": "T", "release_name": "R"}


def test_listenbrainz_submit_rejected_and_retry():
    session = FakeSession(FakeResponse(401, {"error": "Invalid authorization token."}))
    with pytest.raises(ListenBrainzError) as ei:
        ListenBrainzClient(session=session).submit_listen("bad", "A", "T", timestamp=1)
    assert ei.value.status == 401 and ei.value.permanent
    session = FakeSession(FakeResponse(503), FakeResponse(200, {"status": "ok"}))
    with patch("plex_playlist_sync.clients.scrobbler.time.sleep"):
        ListenBrainzClient(session=session).submit_listen("t", "A", "T", timestamp=1)
    assert len(session.calls) == 2


def test_listenbrainz_validate_token():
    ok = FakeSession(FakeResponse(200, {"valid": True, "user_name": "ronlb"}))
    assert ListenBrainzClient(session=ok).validate_token("t") == "ronlb"
    assert ok.calls[0][1] == "https://api.listenbrainz.org/1/validate-token"
    bad = FakeSession(FakeResponse(200, {"valid": False, "message": "nope"}))
    assert ListenBrainzClient(session=bad).validate_token("t") is None
    unauth = FakeSession(FakeResponse(401, {"code": 401}))
    assert ListenBrainzClient(session=unauth).validate_token("t") is None


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def test_identity_rule_1_account_id_matches_user_id(db, users):
    assert resolve_plex_user(db, 1001, "whatever")["id"] == "1001"
    assert resolve_plex_user(db, "1001", None)["id"] == "1001"


def test_identity_rule_2_owner_id_one_maps_to_named_admin(db, users):
    other_admin = db.upsert_user("u-admin2", "zzzz", None, is_admin=True)
    assert other_admin["is_admin"]
    assert resolve_plex_user(db, 1, "owner", admin_username="ZZZZ")["id"] == "u-admin2"
    assert resolve_plex_user(db, "1", "owner", admin_username="ronadmin")["id"] == "u-admin"


def test_identity_rule_2_falls_back_to_first_admin(db, users):
    assert resolve_plex_user(db, 1, "owner", admin_username=None)["is_admin"] is True
    assert resolve_plex_user(db, 1, "owner", admin_username="ghost")["is_admin"] is True


def test_owner_rule_beats_identity_rule_when_user_id_is_1(db, users):
    db.upsert_user("1", "owner-user", None, is_admin=False)
    resolved = resolve_plex_user(db, 1, "x", admin_username="ronadmin")
    assert resolved["id"] != "1"
    assert resolved["is_admin"] is True


def test_identity_rule_3_title_matches_username_case_insensitive(db, users):
    assert resolve_plex_user(db, 999, "BOB")["id"] == "u-bob"
    assert resolve_plex_user(db, 999, "alice")["id"] == "1001"


def test_identity_rule_4_unknown_dropped_and_logged(db, users, caplog):
    with caplog.at_level("INFO"):
        assert resolve_plex_user(db, 777, "stranger") is None
    assert "listen from unknown Plex account stranger; user has not logged into TrackSeerr" in caplog.text
    assert len(db.list_users()) == 3


# ---------------------------------------------------------------------------
# forward_listen
# ---------------------------------------------------------------------------


def test_forward_listen_sends_to_both_services(db, users):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk", listenbrainz_token="lb")
    lid = db.insert_listen("1001", "A", "T", album="Al", rating_key="1")
    lf, lb = MagicMock(), MagicMock()
    out = forward_listen(db, lid, lastfm=lf, listenbrainz=lb)
    assert out == {"lastfm": "sent", "listenbrainz": "sent"}
    assert lf.scrobble.call_args.args[:2] == ("A", "T") and lf.scrobble.call_args.args[3] == "sk"
    assert lb.submit_listen.call_args.args[0] == "lb"
    assert db.list_pending_forwards() == []


def test_forward_listen_lastfm_error_9_clears_session_key(db, users, caplog):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk", lastfm_username="al")
    lid = db.insert_listen("1001", "A", "T", rating_key="1")
    lf = MagicMock()
    lf.scrobble.side_effect = LastFmError(9, "Invalid session key")
    with caplog.at_level("WARNING"):
        out = forward_listen(db, lid, lastfm=lf)
    cfg = db.get_scrobble_config("1001")
    assert cfg["lastfm_session_key"] is None and cfg["lastfm_username"] is None
    assert out["lastfm"] == "skipped"
    assert "session" in caplog.text.lower()


def test_forward_listen_transient_failure_marks_failed_and_retry_succeeds(db, users):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk")
    lid = db.insert_listen("1001", "A", "T", rating_key="1")
    lf = MagicMock()
    lf.scrobble.side_effect = LastFmError(0, "HTTP 503")
    assert forward_listen(db, lid, lastfm=lf)["lastfm"] == "failed"
    row = db.get_listen(lid)
    assert row["forward_attempts"] == 1 and "503" in row["last_forward_error"]
    lf.scrobble.side_effect = None
    assert forward_listen(db, lid, lastfm=lf)["lastfm"] == "sent"


def test_forward_listen_listenbrainz_failure(db, users):
    db.upsert_scrobble_config("1001", listenbrainz_token="lb")
    lid = db.insert_listen("1001", "A", "T", rating_key="1")
    lb = MagicMock()
    lb.submit_listen.side_effect = ListenBrainzError(503, "down")
    assert forward_listen(db, lid, listenbrainz=lb)["listenbrainz"] == "failed"


def test_forward_listen_skips_when_disabled_after_queueing(db, users):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk")
    lid = db.insert_listen("1001", "A", "T", rating_key="1")
    db.upsert_scrobble_config("1001", scrobbling_enabled=False)
    lf = MagicMock()
    assert forward_listen(db, lid, lastfm=lf)["lastfm"] == "skipped"
    lf.scrobble.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def webhook_url(db):
    return f"/api/scrobbles/plex?token={db.get_plex_webhook_secret()}"


def test_webhook_bad_or_missing_token_401(db, config, users):
    tc = make_client(db, config)
    assert tc.post("/api/scrobbles/plex?token=wrong", json=scrobble_payload()).status_code == 401
    assert tc.post("/api/scrobbles/plex", json=scrobble_payload()).status_code == 401
    assert db.list_listens("u-admin") == []


def test_webhook_multipart_scrobble_inserts_and_queues_forwarding(db, config, users):
    db.upsert_scrobble_config("u-admin", lastfm_session_key="sk")
    tc = make_client(db, config)
    body, ctype = multipart_body(scrobble_payload(account_id=1, title="ronadmin"))
    with patch("plex_playlist_sync.api.routes.scrobbles.forward_listen") as fwd:
        r = tc.post(webhook_url(db), content=body, headers={"content-type": ctype})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    listens = db.list_listens("u-admin")
    assert len(listens) == 1
    row = listens[0]
    assert (row["artist"], row["title"], row["album"], row["rating_key"]) == ("Artist", "Song", "Album", "555")
    assert row["source"] == "plex_webhook" and row["duration_ms"] == 200000
    assert row["lastfm_status"] == "pending"
    assert fwd.call_args.args[1] == row["id"]
    # same play again within 10 min is deduped
    with patch("plex_playlist_sync.api.routes.scrobbles.forward_listen") as fwd2:
        r2 = tc.post(webhook_url(db), content=body, headers={"content-type": ctype})
    assert r2.json() == {"status": "ignored"} and fwd2.call_count == 0 and len(db.list_listens("u-admin")) == 1


def test_webhook_json_payload_works(db, config, users):
    tc = make_client(db, config)
    with patch("plex_playlist_sync.api.routes.scrobbles.forward_listen") as fwd:
        r = tc.post(webhook_url(db), json=scrobble_payload(account_id=1001, title="Alice", ratingKey="77"))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert len(db.list_listens("1001")) == 1 and fwd.call_count == 1


def test_webhook_uses_originalTitle_for_track_artist(db, config, users):
    tc = make_client(db, config)
    with patch("plex_playlist_sync.api.routes.scrobbles.forward_listen"):
        tc.post(webhook_url(db), json=scrobble_payload(account_id=1001, grandparentTitle="Various Artists", originalTitle="Real Artist"))
    assert db.list_listens("1001")[0]["artist"] == "Real Artist"


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "media.pause", "Account": {"id": 1001, "title": "Alice"}, "Metadata": {"type": "track", "title": "S", "grandparentTitle": "A"}},
        {"event": "media.scrobble", "Account": {"id": 1001, "title": "Alice"}, "Metadata": {"type": "movie", "title": "M"}},
        {"event": "library.new", "Metadata": {"type": "track"}},
    ],
)
def test_webhook_ignores_non_track_and_other_events(db, config, users, payload):
    tc = make_client(db, config)
    r = tc.post(webhook_url(db), json=payload)
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert db.list_listens("1001") == []


def test_webhook_unknown_account_dropped(db, config, users):
    tc = make_client(db, config)
    r = tc.post(webhook_url(db), json=scrobble_payload(account_id=424242, title="Nobody"))
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert len(db.list_users()) == 3
    for u in ("u-admin", "1001", "u-bob"):
        assert db.list_listens(u) == []


def test_webhook_play_event_triggers_now_playing_not_storage(db, config, users):
    tc = make_client(db, config)
    payload = scrobble_payload(account_id=1001, title="Alice")
    payload["event"] = "media.play"
    with patch("plex_playlist_sync.api.routes.scrobbles.send_now_playing") as np_:
        r = tc.post(webhook_url(db), json=payload)
    assert r.json() == {"status": "ok"} and np_.call_count == 1
    assert db.list_listens("1001") == []


def test_webhook_malformed_payload_400(db, config, users):
    tc = make_client(db, config)
    r = tc.post(webhook_url(db), content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_webhook_needs_no_session(db, config, users):
    tc = make_client(db, config)
    r = tc.post(webhook_url(db), json=scrobble_payload(account_id=1001, title="Alice"))
    assert r.status_code == 200  # no Authorization header or cookie anywhere


# ---------------------------------------------------------------------------
# Webhook URL management and server config
# ---------------------------------------------------------------------------


def test_webhook_url_and_rotation_admin_only(db, config, users):
    tc = make_client(db, config)
    admin, _ = auth_headers(users["admin"], db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    db.update_general_settings({"application_url": "https://ts.example"})
    url = tc.get("/api/scrobbles/webhook-url", headers=admin).json()["url"]
    # built from the request's own base URL (admin's LAN URL to core), never application_url
    assert url == f"http://testserver/api/scrobbles/plex?token={db.get_plex_webhook_secret()}"
    new_url = tc.post("/api/scrobbles/webhook-secret/rotate", headers=admin).json()["url"]
    assert new_url != url and new_url.endswith(db.get_plex_webhook_secret())
    # old token now rejected
    assert tc.post(url.replace("http://testserver", ""), json=scrobble_payload()).status_code == 401


def test_non_admin_gets_403_on_admin_routes(db, config, users):
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    assert tc.get("/api/scrobbles/webhook-url", headers=alice).status_code == 403
    assert tc.post("/api/scrobbles/webhook-secret/rotate", headers=alice).status_code == 403
    assert tc.get("/api/scrobbles/users", headers=alice).status_code == 403
    assert tc.put("/api/scrobbles/users/u-bob/config", json={"scrobbling_enabled": False}, headers=alice).status_code == 403
    assert tc.get("/api/scrobbles/server-config", headers=alice).status_code == 403
    assert tc.put("/api/scrobbles/server-config", json={"plex_history_poll_minutes": 5}, headers=alice).status_code == 403
    assert tc.get("/api/scrobbles/listens?user_id=u-bob", headers=alice).status_code == 403


def test_requires_authentication(db, config, users):
    tc = make_client(db, config)
    assert tc.get("/api/scrobbles/config").status_code == 401
    assert tc.get("/api/scrobbles/lastfm/auth-url").status_code == 401


def test_server_config_db_stored_and_secret_never_returned(db, config, users):
    tc = make_client(db, config)
    admin, _ = auth_headers(users["admin"], db, config)
    r = tc.get("/api/scrobbles/server-config", headers=admin).json()
    assert r == {
        "lastfm_configured": False, "lastfm_api_key_masked": "", "lastfm_from_env": False,
        "plex_history_poll_minutes": 15,
    }
    put = tc.put(
        "/api/scrobbles/server-config",
        json={"lastfm_api_key": "abcdef1234567890", "lastfm_api_secret": "TOPSECRETVALUE", "plex_history_poll_minutes": 5},
        headers=admin,
    )
    assert put.status_code == 200 and put.json()["lastfm_configured"] is True
    assert put.json()["plex_history_poll_minutes"] == 5
    assert "TOPSECRETVALUE" not in put.text and "abcdef1234567890" not in put.text
    assert "TOPSECRETVALUE" not in tc.get("/api/scrobbles/server-config", headers=admin).text
    assert db.get_lastfm_settings() == {"lastfm_api_key": "abcdef1234567890", "lastfm_api_secret": "TOPSECRETVALUE"}


def test_env_overrides_db_settings(db, lfm_config, users):
    db.set_lastfm_settings(api_key="dbkey", api_secret="dbsecret")
    assert get_lastfm_credentials(db, lfm_config) == ("envkey", "envsecret", True)
    plain = Config(plex_url="", plex_token="")
    assert get_lastfm_credentials(db, plain) == ("dbkey", "dbsecret", False)
    tc = make_client(db, lfm_config)
    admin, _ = auth_headers(users["admin"], db, lfm_config)
    cfg = tc.get("/api/scrobbles/server-config", headers=admin).json()
    assert cfg["lastfm_from_env"] is True and cfg["lastfm_api_key_masked"] == "\u2022" * 6
    assert "envsecret" not in json.dumps(cfg)
    r = tc.put("/api/scrobbles/server-config", json={"lastfm_api_key": "new"}, headers=admin)
    assert r.status_code == 409
    assert db.get_lastfm_settings()["lastfm_api_key"] == "dbkey"


def test_config_from_env_reads_lastfm_vars(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", " k ")
    monkeypatch.setenv("LASTFM_API_SECRET", "s")
    cfg = Config.from_env()
    assert (cfg.lastfm_api_key, cfg.lastfm_api_secret) == ("k", "s")
    monkeypatch.delenv("LASTFM_API_KEY")
    monkeypatch.delenv("LASTFM_API_SECRET")
    cfg = Config.from_env()
    assert cfg.lastfm_api_key is None and cfg.lastfm_api_secret is None


# ---------------------------------------------------------------------------
# Last.fm connect flow
# ---------------------------------------------------------------------------


def test_auth_url_503_when_not_configured(db, config, users):
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    r = tc.get("/api/scrobbles/lastfm/auth-url", headers=alice)
    assert r.status_code == 503
    assert r.json()["detail"] == "Last.fm is not configured by the server admin"


def test_auth_url_builds_https_url_and_persists_state(db, lfm_config, users):
    db.update_general_settings({"application_url": "https://ts.example"})
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    url = tc.get("/api/scrobbles/lastfm/auth-url?forward_url=/settings%3Ftab%3Dscrobbling", headers=alice).json()["url"]
    assert url.startswith("https://www.last.fm/api/auth/?api_key=envkey&cb=")
    state = db.conn.execute("SELECT state, user_id, forward_url FROM lastfm_auth_states").fetchone()
    assert state["user_id"] == "1001" and state["forward_url"] == "/settings?tab=scrobbling"
    assert f"state%3D{state['state']}" in url
    assert "https%3A%2F%2Fts.example%2Fapi%2Fscrobbles%2Flastfm%2Fcallback" in url


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example", "javascript:alert(1)", "/\\evil"])
def test_auth_url_drops_unsafe_forward_url(db, lfm_config, users, bad):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    assert tc.get("/api/scrobbles/lastfm/auth-url", params={"forward_url": bad}, headers=alice).status_code == 200
    row = db.conn.execute("SELECT forward_url FROM lastfm_auth_states").fetchone()
    assert row["forward_url"] is None


def callback(tc, state, lf_token="LFTOKENabc123", glue=False):
    qs = f"state={state}?token={lf_token}" if glue else f"state={state}&token={lf_token}"
    return tc.get(f"/api/scrobbles/lastfm/callback?{qs}", follow_redirects=False)


def complete(tc, headers, state, token="LFTOKENabc123"):
    return tc.post("/api/scrobbles/lastfm/complete", json={"state": state, "token": token}, headers=headers)


def _linked(db, uid):
    cfg = db.get_scrobble_config(uid)
    return bool(cfg and cfg.get("lastfm_session_key"))


# --- auth-url origin selection ---------------------------------------------------------------------------------


def _cb_origin(url):
    from urllib.parse import parse_qs, urlsplit

    cb = parse_qs(urlsplit(url).query)["cb"][0]
    parts = urlsplit(cb)
    return f"{parts.scheme}://{parts.netloc}"


def test_auth_url_uses_browser_origin_when_it_matches_host(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    r = tc.get("/api/scrobbles/lastfm/auth-url", params={"origin": "http://testserver"}, headers=alice)
    assert _cb_origin(r.json()["url"]) == "http://testserver"


def test_auth_url_accepts_application_url_origin(db, lfm_config, users):
    db.update_general_settings({"application_url": "https://ts.example"})
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    r = tc.get("/api/scrobbles/lastfm/auth-url", params={"origin": "https://ts.example"}, headers=alice)
    assert _cb_origin(r.json()["url"]) == "https://ts.example"


@pytest.mark.parametrize(
    "spoof", ["https://evil.example", "http://testserver.evil.example", "https://user@testserver", "javascript:alert(1)", "http://testserver/path"]
)
def test_auth_url_rejects_spoofed_origin_and_falls_back(db, lfm_config, users, spoof):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    r = tc.get("/api/scrobbles/lastfm/auth-url", params={"origin": spoof}, headers=alice)
    assert r.status_code == 200
    assert _cb_origin(r.json()["url"]) == "http://testserver"  # _base_url fallback


def test_auth_url_ignores_forwarded_host_from_untrusted_peer(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    r = tc.get(
        "/api/scrobbles/lastfm/auth-url",
        params={"origin": "https://spoof.example"},
        headers={**alice, "X-Forwarded-Host": "spoof.example", "X-Forwarded-Proto": "https"},
    )
    assert _cb_origin(r.json()["url"]) == "http://testserver"


# --- callback: shape check and bounce only ----------------------------------------------------------------------


def test_callback_redirects_to_spa_with_params_and_links_nothing(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    with patch.object(LastFmClient, "exchange_token_for_session") as ex:
        r = callback(tc, state)
    assert r.status_code == 303
    assert r.headers["location"] == f"/?lastfm_state={state}&lastfm_token=LFTOKENabc123"
    assert ex.call_count == 0 and not _linked(db, "1001")
    assert db.take_lastfm_auth_state(state)[1] == "ok"  # the callback did not consume it


def test_callback_tolerates_token_glued_onto_state(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    r = callback(tc, state, glue=True)
    assert r.headers["location"] == f"/?lastfm_state={state}&lastfm_token=LFTOKENabc123"


@pytest.mark.parametrize("qs", ["state=&token=abcdefgh1", "state=short&token=abcdefgh1", "state=abcdefgh1234&token=", "state=abc%3Cdef%3E123&token=abcdefgh1"])
def test_callback_malformed_shape_goes_to_state_error(db, lfm_config, users, qs):
    tc = make_client(db, lfm_config)
    r = tc.get(f"/api/scrobbles/lastfm/callback?{qs}", follow_redirects=False)
    assert r.headers["location"] == "/?scrobble_error=state"


# --- complete: bound to the signed-in session -----------------------------------------------------------------------


def test_complete_as_state_owner_links_account(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    with patch.object(LastFmClient, "exchange_token_for_session", return_value=("al_fm", "NEWSK")) as ex:
        r = complete(tc, alice, state)
    assert r.status_code == 200 and r.json() == {"connected": True, "username": "al_fm"}
    ex.assert_called_once_with("LFTOKENabc123")
    cfg = db.get_scrobble_config("1001")
    assert cfg["lastfm_username"] == "al_fm" and cfg["lastfm_session_key"] == "NEWSK"


def test_complete_works_with_session_cookie_too(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    _, tok = auth_headers(users["alice"], db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    tc.cookies.set("session_token", tok)
    with patch.object(LastFmClient, "exchange_token_for_session", return_value=("al_fm", "SK")):
        r = tc.post("/api/scrobbles/lastfm/complete", json={"state": state, "token": "LFTOKENabc123"})
    assert r.status_code == 200


def test_complete_as_different_user_cannot_bind_and_state_is_consumed(db, lfm_config, users, caplog):
    """Account-linking CSRF: a victim holding the attacker's state must not link to the attacker's account."""
    tc = make_client(db, lfm_config)
    bob, _ = auth_headers(users["bob"], db, lfm_config)  # the victim
    state = db.create_lastfm_auth_state("1001", None)  # minted by the attacker (alice)
    with patch.object(LastFmClient, "exchange_token_for_session") as ex, caplog.at_level("WARNING"):
        r = complete(tc, bob, state, token="VICTIMTOKEN123")
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "state_user"
    assert ex.call_count == 0
    assert not _linked(db, "1001") and not _linked(db, "u-bob")
    assert "VICTIMTOKEN123" not in caplog.text
    assert db.take_lastfm_auth_state(state) == (None, "unknown")  # consumed


def test_complete_without_auth_is_401_and_links_nothing(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    with patch.object(LastFmClient, "exchange_token_for_session") as ex:
        r = tc.post("/api/scrobbles/lastfm/complete", json={"state": state, "token": "LFTOKENabc123"})
    assert r.status_code == 401 and ex.call_count == 0 and not _linked(db, "1001")
    assert db.take_lastfm_auth_state(state)[1] == "ok"  # an unauthenticated probe does not burn the state


def test_complete_rejects_reused_state(db, lfm_config, users, caplog):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    with patch.object(LastFmClient, "exchange_token_for_session", return_value=("u", "k")):
        assert complete(tc, alice, state).status_code == 200
        with caplog.at_level("WARNING"):
            r = complete(tc, alice, state)
    assert r.status_code == 400 and r.json()["detail"]["reason"] == "state"
    assert "unknown state" in caplog.text


def test_complete_rejects_expired_state(db, lfm_config, users, caplog):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    db.conn.execute("UPDATE lastfm_auth_states SET created_at = datetime('now', '-11 minutes')")
    with patch.object(LastFmClient, "exchange_token_for_session") as ex, caplog.at_level("WARNING"):
        r = complete(tc, alice, state, token="SECRETTOKEN1")
    assert r.status_code == 400 and r.json()["detail"]["reason"] == "state_expired"
    assert ex.call_count == 0 and "expired state" in caplog.text and "SECRETTOKEN1" not in caplog.text
    assert not _linked(db, "1001")


def test_complete_unknown_state(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    r = complete(tc, alice, "nope-nope-nope")
    assert r.status_code == 400 and r.json()["detail"]["reason"] == "state"


def test_complete_exchange_failure_is_502(db, lfm_config, users):
    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    state = db.create_lastfm_auth_state("1001", None)
    with patch.object(LastFmClient, "exchange_token_for_session", side_effect=LastFmError(4, "bad token")):
        r = complete(tc, alice, state)
    assert r.status_code == 502 and r.json()["detail"]["reason"] == "lastfm"
    assert not _linked(db, "1001")


@pytest.mark.parametrize("fmt", ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S"])
def test_state_freshness_understands_stored_timestamp_shapes(db, users, fmt):
    """created_at in ISO-T/Z/offset shapes must not look stale (SQLite datetime() returns NULL for some)."""
    fresh = db.create_lastfm_auth_state("1001", None)
    db.conn.execute("UPDATE lastfm_auth_states SET created_at = ?", (datetime.now(timezone.utc).strftime(fmt),))
    record, why = db.take_lastfm_auth_state(fresh)
    assert why == "ok" and record["user_id"] == "1001"
    old = db.create_lastfm_auth_state("1001", None)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime(fmt)
    db.conn.execute("UPDATE lastfm_auth_states SET created_at = ?", (stale,))
    assert db.take_lastfm_auth_state(old) == (None, "expired")


def test_full_flow_browser_origin_callback_then_complete(db, lfm_config, users):
    from urllib.parse import parse_qs, unquote, urlsplit

    tc = make_client(db, lfm_config)
    alice, _ = auth_headers(users["alice"], db, lfm_config)
    url = tc.get("/api/scrobbles/lastfm/auth-url", params={"origin": "http://testserver"}, headers=alice).json()["url"]
    cb = parse_qs(urlsplit(url).query)["cb"][0]
    r = tc.get(unquote(cb.split("http://testserver", 1)[1]) + "?token=LFTOKENabc123", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    with patch.object(LastFmClient, "exchange_token_for_session", return_value=("al_fm", "SK")):
        done = complete(tc, alice, q["lastfm_state"][0], q["lastfm_token"][0])
    assert done.status_code == 200 and _linked(db, "1001")


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------


def test_config_and_users_never_leak_secrets(db, config, users):
    db.upsert_scrobble_config("1001", lastfm_username="al_fm", lastfm_session_key=SESSION_SECRET,
                              listenbrainz_token=LB_SECRET, listenbrainz_username="al_lb")
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    admin, _ = auth_headers(users["admin"], db, config)
    mine = tc.get("/api/scrobbles/config", headers=alice)
    everyone = tc.get("/api/scrobbles/users", headers=admin)
    for resp in (mine, everyone):
        assert resp.status_code == 200
        assert SESSION_SECRET not in resp.text and LB_SECRET not in resp.text
        assert "session_key" not in resp.text and "listenbrainz_token" not in resp.text
    body = mine.json()
    assert body == {
        "user_id": "1001", "username": "Alice", "scrobbling_enabled": True, "lastfm_connected": True,
        "lastfm_username": "al_fm", "listenbrainz_connected": True, "listenbrainz_username": "al_lb",
        "updated_at": body["updated_at"],
    }
    rows = {r["user_id"]: r for r in everyone.json()}
    assert set(rows) == {"u-admin", "1001", "u-bob"}
    assert rows["u-bob"]["lastfm_connected"] is False and rows["u-bob"]["scrobbling_enabled"] is True
    put = tc.put("/api/scrobbles/config", json={"scrobbling_enabled": False}, headers=alice)
    assert SESSION_SECRET not in put.text and LB_SECRET not in put.text
    listens = tc.get("/api/scrobbles/listens", headers=alice)
    assert listens.status_code == 200


def test_put_config_listenbrainz_valid_invalid_and_unlink(db, config, users):
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    with patch.object(ListenBrainzClient, "validate_token", return_value=None):
        bad = tc.put("/api/scrobbles/config", json={"listenbrainz_token": "nope"}, headers=alice)
    assert bad.status_code == 400 and db.get_scrobble_config("1001") is None
    with patch.object(ListenBrainzClient, "validate_token", return_value="al_lb"):
        ok = tc.put("/api/scrobbles/config", json={"listenbrainz_token": "good"}, headers=alice)
    assert ok.status_code == 200 and ok.json()["listenbrainz_connected"] is True
    assert ok.json()["listenbrainz_username"] == "al_lb" and "good" not in ok.text
    assert db.get_scrobble_config("1001")["listenbrainz_token"] == "good"
    cleared = tc.put("/api/scrobbles/config", json={"listenbrainz_token": None}, headers=alice)
    assert cleared.json()["listenbrainz_connected"] is False
    assert db.get_scrobble_config("1001")["listenbrainz_token"] is None
    with patch.object(ListenBrainzClient, "validate_token", side_effect=ListenBrainzError(0, "down")):
        down = tc.put("/api/scrobbles/config", json={"listenbrainz_token": "x"}, headers=alice)
    assert down.status_code == 502


def test_put_config_unlink_lastfm_and_toggle(db, config, users):
    db.upsert_scrobble_config("1001", lastfm_username="al", lastfm_session_key="sk")
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    r = tc.put("/api/scrobbles/config", json={"unlink_lastfm": True, "scrobbling_enabled": False}, headers=alice)
    body = r.json()
    assert body["lastfm_connected"] is False and body["lastfm_username"] is None and body["scrobbling_enabled"] is False


def test_admin_can_edit_any_users_config(db, config, users):
    tc = make_client(db, config)
    admin, _ = auth_headers(users["admin"], db, config)
    r = tc.put(
        "/api/scrobbles/users/u-bob/config",
        json={"lastfm_username": "bob_fm", "lastfm_session_key": "BOBSK", "scrobbling_enabled": False},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["lastfm_connected"] is True and r.json()["username"] == "bob"
    assert "BOBSK" not in r.text
    assert db.get_scrobble_config("u-bob")["lastfm_session_key"] == "BOBSK"
    assert tc.put("/api/scrobbles/users/ghost/config", json={}, headers=admin).status_code == 404
    # regular users cannot set the session key on themselves via /config (field ignored)
    alice, _ = auth_headers(users["alice"], db, config)
    tc.put("/api/scrobbles/config", json={"lastfm_session_key": "FORGED"}, headers=alice)
    assert (db.get_scrobble_config("1001") or {}).get("lastfm_session_key") is None


def test_listens_endpoint_shape_and_admin_user_param(db, config, users):
    db.insert_listen("1001", "A", "T", album="Al", rating_key="1", source="plex_history")
    tc = make_client(db, config)
    alice, _ = auth_headers(users["alice"], db, config)
    admin, _ = auth_headers(users["admin"], db, config)
    rows = tc.get("/api/scrobbles/listens?limit=10", headers=alice).json()
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "artist", "title", "album", "played_at", "source", "lastfm_status", "listenbrainz_status"}
    assert tc.get("/api/scrobbles/listens", headers=admin).json() == []
    assert len(tc.get("/api/scrobbles/listens?user_id=1001", headers=admin).json()) == 1


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def history_entry(account_id, artist, title, viewed_at, rating_key, etype="track", album="Album"):
    return SimpleNamespace(
        type=etype, accountID=account_id, grandparentTitle=artist, originalTitle=None, title=title,
        parentTitle=album, ratingKey=rating_key, duration=180000, viewedAt=viewed_at,
    )


def fake_plex(entries, accounts):
    server = MagicMock()
    server.history.return_value = entries
    server.systemAccounts.return_value = [SimpleNamespace(id=i, name=n) for i, n in accounts.items()]
    return SimpleNamespace(server=server, _get_admin_username=lambda: "ronadmin")


def test_worker_history_poll_inserts_through_resolver(db, config, users):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    db.upsert_scrobble_config("u-admin", lastfm_session_key="sk")
    entries = [
        history_entry(1, "Owner Artist", "Owner Song", now - timedelta(minutes=3), 11),
        history_entry(1001, "Alice Artist", "Alice Song", now - timedelta(minutes=2), 12),
        history_entry(55, "Bob Artist", "By Title", now - timedelta(minutes=2), 13),
        history_entry(999, "Ghost", "Dropped", now - timedelta(minutes=1), 14),
        history_entry(1, "Movie", "Not Music", now - timedelta(minutes=1), 15, etype="movie"),
    ]
    plex = fake_plex(entries, {1: "ronadmin", 1001: "Alice", 55: "BOB", 999: "ghost"})
    worker = ScrobbleWorker()
    with patch("plex_playlist_sync.scrobble_worker.forward_listen"):
        result = worker.run_iteration(db, config, plex, now=now, force=True)
    assert result["ingested"] == 3
    owner = db.list_listens("u-admin")
    assert len(owner) == 1 and owner[0]["source"] == "plex_history" and owner[0]["lastfm_status"] == "pending"
    assert db.list_listens("1001")[0]["title"] == "Alice Song"
    assert db.list_listens("u-bob")[0]["title"] == "By Title"
    assert db.get_scrobble_state("plex_history_last_poll") == now.isoformat(timespec="seconds")
    assert db.get_scrobble_state("plex_admin_username") == "ronadmin"
    assert plex.server.history.call_args.kwargs["mindate"] <= now
    # a second poll with the same entries only dedups
    again = worker.poll_history_once(db, plex, now=now + timedelta(minutes=16))
    assert again == [] and len(db.list_listens("1001")) == 1
    assert worker.get_status()["listens_ingested"] == 3


def test_worker_history_poll_respects_interval_and_disable(db, config, users):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    plex = fake_plex([], {})
    worker = ScrobbleWorker()
    worker.run_iteration(db, config, plex, now=now, force=True)
    assert plex.server.history.call_count == 1
    worker.run_iteration(db, config, plex, now=now + timedelta(minutes=5))
    assert plex.server.history.call_count == 1  # not due (15 min)
    worker.run_iteration(db, config, plex, now=now + timedelta(minutes=16))
    assert plex.server.history.call_count == 2
    db.set_plex_history_poll_minutes(0)
    worker.run_iteration(db, config, plex, now=now + timedelta(hours=5))
    assert plex.server.history.call_count == 2


def test_worker_retry_pass_forwards_pending(db, config, users):
    db.upsert_scrobble_config("1001", lastfm_session_key="sk", listenbrainz_token="lb")
    lid = db.insert_listen("1001", "A", "T", rating_key="1")
    exhausted = db.insert_listen("1001", "A", "Done", rating_key="2")
    for _ in range(5):
        db.mark_forward_result(exhausted, "lastfm", "failed", "x")
    lf, lb = MagicMock(), MagicMock()
    worker = ScrobbleWorker()
    with patch("plex_playlist_sync.scrobbling.build_lastfm_client", return_value=lf), \
         patch("plex_playlist_sync.scrobbling.ListenBrainzClient", return_value=lb):
        result = worker.run_iteration(db, config, plex_client=None, force=True)
    assert result == {"ingested": 0, "retried": 1}
    row = db.get_listen(lid)
    assert row["lastfm_status"] == "sent" and row["listenbrainz_status"] == "sent"
    assert lf.scrobble.call_count == 1 and lb.submit_listen.call_count == 1
    assert db.list_pending_forwards() == []


def test_worker_history_poll_plex_error_is_counted_not_fatal(db, config, users):
    from plexapi.exceptions import BadRequest

    plex = fake_plex([], {})
    plex.server.history.side_effect = BadRequest("nope")
    worker = ScrobbleWorker()
    result = worker.run_iteration(db, config, plex, force=True)
    assert result["ingested"] == 0 and worker.get_status()["errors"] == 1


def test_worker_start_stop_lifecycle(db, config):
    worker = ScrobbleWorker()
    assert worker.start(db, config, tick_seconds=1, plex_factory=lambda c: None) is True
    assert worker.is_running()
    assert worker.start(db, config) is False
    worker.stop()
    assert not worker.is_running()
