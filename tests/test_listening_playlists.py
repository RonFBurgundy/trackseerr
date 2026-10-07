"""Playlists from a user's own Last.fm / ListenBrainz listening, and the auto-request permission gate (mocked HTTP)."""

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_media_client
from plex_playlist_sync.api.routes.sync import sync_state
from plex_playlist_sync.backlog_worker import effective_playlist_modes
from plex_playlist_sync.clients.import_lists import lastfm, listenbrainz
from plex_playlist_sync.config import Config
from plex_playlist_sync.list_monitoring import apply_playlist_missing
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.request_submission import RequestRejected
from plex_playlist_sync.storage import Database
from tests._rm_helpers import auth_headers

P = "/api/playlists"
PL_OLD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PL_NEW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PL_OWN = "cccccccc-cccc-cccc-cccc-cccccccccccc"
BIT = int(UserPermission.AUTO_REQUEST_PLAYLISTS)
BASE = int(UserPermission.DEFAULT)


class FakeResp:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload


def _jspf(titles: list[tuple[str, str]], title: str = "x") -> dict[str, Any]:
    return {
        "playlist": {
            "title": title,
            "track": [{"title": t, "creator": a, "album": "", "identifier": ""} for t, a in titles],
        }
    }


def _listing(entries: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """entries: (mbid, title, date, source_patch)."""
    return {
        "playlist_count": len(entries),
        "playlists": [
            {
                "playlist": {
                    "identifier": f"https://listenbrainz.org/playlist/{mbid}",
                    "title": title,
                    "date": date,
                    "extension": {
                        "https://musicbrainz.org/doc/jspf#playlist": {
                            "additional_metadata": {"algorithm_metadata": {"source_patch": patch_name}}
                        }
                    },
                }
            }
            for mbid, title, date, patch_name in entries
        ],
    }


class FakeHttp:
    """Routes ``requests.get`` by URL and records every call."""

    def __init__(self) -> None:
        self.created_for: list[tuple[str, str, str, str]] = [
            (PL_OLD, "Weekly Jams for ron, week of 2025-01-06", "2025-01-06T00:00:00", "weekly-jams"),
            (PL_NEW, "Weekly Jams for ron, week of 2025-01-13", "2025-01-13T00:00:00", "weekly-jams"),
            ("dddddddd-dddd-dddd-dddd-dddddddddddd", "Weekly Exploration for ron", "2025-01-12T00:00:00", "weekly-exploration"),
        ]
        self.own: list[tuple[str, str, str, str]] = [(PL_OWN, "Road trip", "2024-05-01T00:00:00", "")]
        self.playlists: dict[str, dict[str, Any]] = {
            PL_OLD: _jspf([("Old Song", "Old Artist")]),
            PL_NEW: _jspf([("New Song", "New Artist"), ("Other Song", "Other Artist")]),
            PL_OWN: _jspf([("Own Song", "Own Artist")]),
        }
        self.lastfm_tracks = [("Loved One", "Band A"), ("Loved Two", "Band B")]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, params: Any = None, headers: Any = None, timeout: Any = None) -> FakeResp:
        self.calls.append((url, dict(params or {})))
        if url.endswith("/playlists/createdfor"):
            return FakeResp(_listing(self.created_for))
        if url.endswith("/playlists"):
            return FakeResp(_listing(self.own))
        if "/playlist/" in url:
            return FakeResp(self.playlists[url.rsplit("/", 1)[1]])
        if "audioscrobbler" in url:
            method = (params or {})["method"]
            root, key = ("lovedtracks", "track") if method == "user.getlovedtracks" else ("toptracks", "track")
            return FakeResp(
                {
                    root: {
                        key: [{"name": n, "mbid": "", "artist": {"name": a}} for n, a in self.lastfm_tracks],
                        "@attr": {"totalPages": "1"},
                    }
                }
            )
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def http():
    fake = FakeHttp()
    with patch("plex_playlist_sync.clients.import_lists.base.requests.get", side_effect=fake):
        yield fake


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(plex_url="", plex_token="", data_dir=str(tmp_path), media_server="none")


@pytest.fixture
def api(db, config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_media_client] = lambda: None
    return TestClient(app)


def _user(db: Database, uid: str, *, admin: bool = False, perms: int = BASE) -> dict[str, Any]:
    db.upsert_user(uid, uid, f"{uid}@example.com", is_admin=admin)
    if not admin:
        db.update_user_admin_fields(uid, {"permissions": perms})
    return db.get_user(uid)


def _link(db: Database, uid: str, lastfm: bool = True, lb: bool = True) -> None:
    fields: dict[str, Any] = {}
    if lastfm:
        fields.update(lastfm_username="ron_lf", lastfm_session_key="sk")
    if lb:
        fields.update(listenbrainz_username="ron", listenbrainz_token="tok")
    db.upsert_scrobble_config(uid, **fields)
    if lastfm:
        db.set_lastfm_settings(api_key="key", api_secret="secret")


def _headers(db, config, user) -> dict[str, str]:
    return auth_headers(db, config, user)


# ------------------------------------------------------------------ sources


def test_sources_unlinked_say_so(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    body = api.get(f"{P}/listening/sources", headers=alice).json()
    assert body["lastfm"]["linked"] is False and "not linked" in body["lastfm"]["reason"]
    assert body["listenbrainz"]["linked"] is False and body["listenbrainz"]["lists"] == []
    assert body["can_auto_request"] is False
    assert http.calls == []  # nothing is fetched without a linked account


def test_sources_linked_lists_lb_created_for_and_lastfm_periods(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice", perms=BASE | BIT))
    _link(db, "alice")
    body = api.get(f"{P}/listening/sources", headers=alice).json()
    assert body["can_auto_request"] is True
    lf = body["lastfm"]
    assert lf["linked"] and lf["available"] and lf["username"] == "ron_lf"
    assert [(i["kind"], i["ref"]) for i in lf["lists"]][:3] == [("loved", ""), ("top_tracks", "overall"), ("top_tracks", "7day")]
    lb = body["listenbrainz"]["lists"]
    created = {i["label"]: i for i in lb if i["kind"] == "created_for"}
    assert set(created) == {"Weekly Jams", "Weekly Exploration"}  # newest edition of each kind only
    assert created["Weekly Jams"]["ref"] == PL_NEW
    assert [i["ref"] for i in lb if i["kind"] == "playlist"] == [PL_OWN]


def test_sources_lastfm_without_server_key(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    db.upsert_scrobble_config("alice", lastfm_username="ron_lf")
    lf = api.get(f"{P}/listening/sources", headers=alice).json()["lastfm"]
    assert lf["linked"] and not lf["available"] and "API key" in lf["reason"]


def test_lastfm_top_tracks_sends_period_param(http):
    items = lastfm.fetch({"username": "u", "api_key": "k", "source": "top_tracks", "period": "7day", "limit": 5})
    assert [i.track_title for i in items] == ["Loved One", "Loved Two"]
    params = http.calls[0][1]
    assert params["method"] == "user.gettoptracks" and params["period"] == "7day"


def test_listenbrainz_created_for_discovery_and_newest(http):
    entries = listenbrainz.list_created_for("ron", {})
    assert [e["slug"] for e in entries] == ["weekly-jams", "weekly-exploration", "weekly-jams"]  # newest first
    assert listenbrainz.newest_created_for("ron", "weekly-jams", {})["mbid"] == PL_NEW
    assert listenbrainz.newest_created_for("ron", "daily-jams", {}) is None


# ------------------------------------------------------------------ create


def _create(api, headers, **body):
    payload = {"provider": "lastfm", "kind": "loved", "ref": "", "keep_in_sync": True, "auto_request": False}
    payload.update(body)
    return api.post(f"{P}/listening", json=payload, headers=headers)


def test_create_is_list_only_by_default_and_owned_by_the_user(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    res = _create(api, alice)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["track_count"] == 2 and body["missing_count"] == 2 and body["targets"] == ["alice"]
    row = db.get_playlist(body["id"])
    assert row["service"] == "lastfm" and row["monitor_mode"] == "none" and row["auto_request"] is False
    assert (row["source_kind"], row["source_ref"], row["creator_id"], row["enabled"]) == ("loved", "", "alice", True)
    assert len(db.get_missing_tracks(body["id"])) == 2
    listed = api.get(P, headers=alice).json()[0]
    assert listed["auto_request"] is False and listed["source_kind"] == "loved"


def test_create_requires_a_linked_account(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    res = _create(api, alice, provider="listenbrainz", kind="playlist", ref=PL_OWN)
    assert res.status_code == 400 and "no longer linked" in res.json()["detail"]
    assert db.list_playlists() == []


def test_create_uses_only_the_owners_credentials(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _user(db, "bob")
    db.upsert_scrobble_config("bob", lastfm_username="bobs_account")
    db.set_lastfm_settings(api_key="key", api_secret="secret")
    assert _create(api, alice).status_code == 400  # alice has no Last.fm link; bob's is never used
    assert not any(p.get("user") == "bobs_account" for _, p in http.calls)


def test_create_static_snapshot_when_not_kept_in_sync(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    body = _create(api, alice, provider="lastfm", kind="top_tracks", ref="3month", keep_in_sync=False).json()
    row = db.get_playlist(body["id"])
    assert row["enabled"] is False and row["source_ref"] == "3month"
    assert "Top Tracks" in row["name"] and json.loads(row["tracks_json"])[0]["title"] == "Loved One"


def test_create_rejects_unknown_period(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    assert _create(api, alice, kind="top_tracks", ref="forever").status_code == 400


def test_create_auto_request_permission_matrix(api, db, config, http):
    plain = _headers(db, config, _user(db, "plain"))
    holder = _headers(db, config, _user(db, "holder", perms=BASE | BIT))
    boss = _headers(db, config, _user(db, "boss", admin=True))
    for uid in ("plain", "holder", "boss"):
        _link(db, uid)
    assert _create(api, plain, auto_request=True).status_code == 403
    assert db.list_playlists() == []
    for headers, uid in ((holder, "holder"), (boss, "boss")):
        res = _create(api, headers, auto_request=True)
        assert res.status_code == 201, res.text
        assert db.get_playlist(res.json()["id"])["auto_request"] is True
        assert db.get_playlist(res.json()["id"])["monitor_mode"] == "none"


def test_listenbrainz_created_for_stores_kind_and_names_it(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    res = _create(api, alice, provider="listenbrainz", kind="created_for", ref=PL_OLD)
    assert res.status_code == 201, res.text
    row = db.get_playlist(res.json()["id"])
    assert row["service"] == "listenbrainz" and row["source_kind"] == "created_for" and row["source_ref"] == "weekly-jams"
    assert row["name"] == "Weekly Jams - ListenBrainz"
    # the creation fetch already resolves the newest edition, not the one that was clicked
    assert [t["title"] for t in json.loads(row["tracks_json"])] == ["New Song", "Other Song"]


def test_listenbrainz_own_playlist_uses_its_title(api, db, config, http):
    alice = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    res = _create(api, alice, provider="listenbrainz", kind="playlist", ref=PL_OWN)
    assert res.status_code == 201, res.text
    assert db.get_playlist(res.json()["id"])["name"] == "Road trip - ListenBrainz"


# ------------------------------------------------------------------ auto-request toggle and monitor-mode


def _make(api, db, config, uid="alice", perms=BASE):
    headers = _headers(db, config, _user(db, uid, perms=perms))
    _link(db, uid)
    return headers, _create(api, headers).json()["id"]


def test_auto_request_toggle_needs_permission_to_enable_not_to_disable(api, db, config, http):
    headers, pid = _make(api, db, config)
    assert api.put(f"{P}/{pid}/auto-request", json={"auto_request": True}, headers=headers).status_code == 403
    assert api.put(f"{P}/{pid}/auto-request", json={"auto_request": False}, headers=headers).status_code == 200
    db.update_user_admin_fields("alice", {"permissions": BASE | BIT})
    res = api.put(f"{P}/{pid}/auto-request", json={"auto_request": True}, headers=headers)
    assert res.status_code == 200 and res.json() == {"id": pid, "auto_request": True}
    assert db.get_playlist(pid)["auto_request"] is True


def test_auto_request_toggle_hidden_from_other_users_and_non_listening(api, db, config, http):
    _, pid = _make(api, db, config)
    mallory = _headers(db, config, _user(db, "mallory", perms=BASE | BIT))
    assert api.put(f"{P}/{pid}/auto-request", json={"auto_request": True}, headers=mallory).status_code == 404
    db.upsert_playlist("plain-pl", "Plain", creator_id="mallory")
    assert api.put(f"{P}/plain-pl/auto-request", json={"auto_request": True}, headers=mallory).status_code == 400


def test_listening_playlist_rejects_any_monitor_mode(api, db, config, http):
    boss = _headers(db, config, _user(db, "boss", admin=True))
    _link(db, "boss")
    pid = _create(api, boss).json()["id"]
    for mode in ("track", "album", "artist"):
        assert api.put(f"{P}/{pid}/monitor-mode", json={"monitor_mode": mode}, headers=boss).status_code == 400
        assert api.put(f"{P}/{pid}/enabled", json={"monitor_mode": mode}, headers=boss).status_code == 400
    assert api.put(f"{P}/{pid}/monitor-mode", json={"monitor_mode": "none"}, headers=boss).status_code == 200
    assert db.get_playlist(pid)["monitor_mode"] == "none"


# ------------------------------------------------------------------ sync: auto-request


def _listening_row(db, uid="alice", auto=True, perms=BASE | BIT, admin=False) -> str:
    _user(db, uid, admin=admin, perms=perms)
    db.upsert_playlist("lfm_x", "Loved Tracks - Last.fm", service="lastfm", creator_id=uid)
    db.set_playlist_source("lfm_x", "loved", "")
    db.set_playlist_monitor_mode("lfm_x", "none")
    db.set_playlist_auto_request("lfm_x", auto)
    db.record_sync_result(
        "lfm_x", "success", missing_tracks=[{"title": f"T{i}", "artist": f"A{i}", "album": ""} for i in range(3)]
    )
    return "lfm_x"


SUBMIT = "plex_playlist_sync.listening_playlists.submit_track_request"


def test_no_requests_when_auto_request_is_off(db, config):
    pid = _listening_row(db, auto=False)
    with patch(SUBMIT) as submit:
        counts = apply_playlist_missing(db, config, pid)
    submit.assert_not_called()
    assert counts["applied"] == 0 and all(not t["list_applied_at"] for t in db.get_missing_tracks(pid))


def test_requests_each_missing_track_as_the_owner_once(db, config):
    pid = _listening_row(db)
    with patch(SUBMIT) as submit:
        counts = apply_playlist_missing(db, config, pid)
        apply_playlist_missing(db, config, pid)  # a second sync does not ask again
    assert counts["applied"] == 3 and submit.call_count == 3
    for call in submit.call_args_list:
        assert call.args[2]["id"] == "alice" and call.kwargs["trigger"].ref == pid
    assert [c.args[3] for c in submit.call_args_list] == ["T0", "T1", "T2"]


def test_stops_on_first_quota_rejection_and_retries_next_sync(db, config, caplog):
    pid = _listening_row(db)
    effects = [None, RequestRejected("quota", 400, "Request quota reached for tracks (1 per 7 days)")]
    with patch(SUBMIT, side_effect=effects) as submit, caplog.at_level("INFO"):
        counts = apply_playlist_missing(db, config, pid)
    assert submit.call_count == 2 and counts["applied"] == 1 and counts["pending"] == 1
    assert sum("quota reached" in r.message for r in caplog.records) == 1
    done = [t["title"] for t in db.get_missing_tracks(pid) if t["list_applied_at"]]
    assert done == ["T0"]  # the rejected one and the rest stay open for the next sync


def test_duplicate_is_treated_as_requested(db, config):
    pid = _listening_row(db)
    with patch(SUBMIT, side_effect=RequestRejected("duplicate", 409, "dup")) as submit:
        counts = apply_playlist_missing(db, config, pid)
    assert submit.call_count == 3 and counts["applied"] == 3


def test_one_bad_item_does_not_stop_the_rest(db, config):
    pid = _listening_row(db)
    with patch(SUBMIT, side_effect=[RuntimeError("boom"), None, None]) as submit:
        counts = apply_playlist_missing(db, config, pid)
    assert submit.call_count == 3 and counts == {"applied": 2, "unresolved": 0, "pending": 0, "failed": 1}


def test_revoked_permission_stops_requests_but_keeps_the_opt_in(db, config):
    pid = _listening_row(db)
    db.update_user_admin_fields("alice", {"permissions": BASE})
    with patch(SUBMIT) as submit:
        apply_playlist_missing(db, config, pid)
    submit.assert_not_called()
    assert db.get_playlist(pid)["auto_request"] is True
    db.update_user_admin_fields("alice", {"permissions": BASE | BIT})
    with patch(SUBMIT) as submit:
        apply_playlist_missing(db, config, pid)
    assert submit.call_count == 3


def test_admin_owner_always_requests(db, config):
    pid = _listening_row(db, uid="boss", admin=True)
    with patch(SUBMIT) as submit:
        apply_playlist_missing(db, config, pid)
    assert submit.call_count == 3


# ------------------------------------------------------------------ sync cycle


def test_sync_cycle_keeps_listenbrainz_created_for_on_the_newest_edition(api, db, config, http):
    headers = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    pid = _create(api, headers, provider="listenbrainz", kind="created_for", ref=PL_OLD).json()["id"]
    assert [t["title"] for t in json.loads(db.get_playlist(pid)["tracks_json"])] == ["New Song", "Other Song"]
    # LB regenerates the playlist: a newer edition appears
    newest = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    http.created_for.append((newest, "Weekly Jams for ron, week of 2025-01-20", "2025-01-20T00:00:00", "weekly-jams"))
    http.playlists[newest] = _jspf([("Fresh Song", "Fresh Artist")])
    result = sync_state.execute_sync(db=db, config=config, plex_client=None, spotify_client=None, deezer_client=None)
    assert result["status"] == "success"
    assert [t["title"] for t in json.loads(db.get_playlist(pid)["tracks_json"])] == ["Fresh Song"]
    assert [t["title"] for t in db.get_missing_tracks(pid)] == ["Fresh Song"]


def test_sync_cycle_skips_paused_listening_playlists(api, db, config, http):
    headers = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    pid = _create(api, headers, keep_in_sync=False).json()["id"]
    before = len(http.calls)
    sync_state.execute_sync(db=db, config=config, plex_client=None, spotify_client=None, deezer_client=None)
    assert len(http.calls) == before


def test_sync_cycle_fetch_failure_records_error_and_keeps_snapshot(api, db, config, http):
    headers = _headers(db, config, _user(db, "alice"))
    _link(db, "alice")
    pid = _create(api, headers).json()["id"]
    db.upsert_scrobble_config("alice", lastfm_username="")  # the owner unlinked Last.fm
    sync_state.execute_sync(db=db, config=config, plex_client=None, spotify_client=None, deezer_client=None)
    row = db.get_playlist(pid)
    assert row["sync_status"] == "error" and json.loads(row["tracks_json"])[0]["title"] == "Loved One"


# ------------------------------------------------------------------ permission gate on every playlist source


def test_gate_set_track_needs_bit_for_non_admin(api, db, config):
    alice = _headers(db, config, _user(db, "alice"))
    db.upsert_playlist("pl-a", "Mine", creator_id="alice")
    assert api.put(f"{P}/pl-a/monitor-mode", json={"monitor_mode": "track"}, headers=alice).status_code == 403
    assert api.put(f"{P}/pl-a/enabled", json={"monitor_mode": "track"}, headers=alice).status_code == 403
    assert api.put(f"{P}/pl-a/monitor-mode", json={"monitor_mode": "none"}, headers=alice).status_code == 200
    db.update_user_admin_fields("alice", {"permissions": BASE | BIT})
    assert api.put(f"{P}/pl-a/monitor-mode", json={"monitor_mode": "track"}, headers=alice).status_code == 200
    # album/artist stay admin-only even with the bit
    assert api.put(f"{P}/pl-a/monitor-mode", json={"monitor_mode": "album"}, headers=alice).status_code == 403


def test_gate_admin_unaffected(api, db, config):
    boss = _headers(db, config, _user(db, "boss", admin=True))
    db.upsert_playlist("pl-a", "Mine", creator_id="boss")
    for mode in ("track", "album", "artist", "none"):
        assert api.put(f"{P}/pl-a/monitor-mode", json={"monitor_mode": mode}, headers=boss).status_code == 200


@pytest.mark.parametrize("holder, expected", [(False, "none"), (True, "track")])
def test_gate_created_playlists_start_list_only_without_the_bit(api, db, config, holder, expected):
    alice = _headers(db, config, _user(db, "alice", perms=BASE | (BIT if holder else 0)))
    res = api.post(
        f"{P}/import",
        json={"name": "Paste", "service": "spotify", "tracks": [{"title": "Airbag", "artist": "Radiohead"}]},
        headers=alice,
    )
    assert res.status_code == 201, res.text
    assert db.get_playlist(res.json()["id"])["monitor_mode"] == expected
    m3u = "#EXTM3U\n#EXTINF:200,Radiohead - Lucky\n/music/a.flac\n"
    res = api.post(f"{P}/import/m3u", json={"content": m3u, "name": "M3U"}, headers=alice)
    assert db.get_playlist(res.json()["id"])["monitor_mode"] == expected


def test_gate_url_create_starts_list_only_without_the_bit(api, db, config):
    from plex_playlist_sync.api.dependencies import get_deezer_client, get_spotify_client

    api.app.dependency_overrides[get_spotify_client] = lambda: None
    api.app.dependency_overrides[get_deezer_client] = lambda: None
    alice = _headers(db, config, _user(db, "alice"))
    res = api.post(P, json={"url_or_id": "3155776842", "service": "deezer"}, headers=alice)
    assert res.status_code == 201 and res.json()["monitor_mode"] == "none"
    boss = _headers(db, config, _user(db, "boss", admin=True))
    res = api.post(P, json={"url_or_id": "3155776843", "service": "deezer"}, headers=boss)
    assert res.json()["monitor_mode"] == "track"


def test_gate_backlog_ignores_track_mode_of_creator_without_bit_until_granted(db):
    _user(db, "alice")
    _user(db, "boss", admin=True)
    db.upsert_playlist("pl-a", "Alice", creator_id="alice")  # default mode: track
    db.upsert_playlist("pl-b", "Boss", creator_id="boss")
    db.upsert_playlist("pl-c", "Operator")  # no creator: configured by the operator, trusted
    assert effective_playlist_modes(db) == {"pl-a": "none", "pl-b": "track", "pl-c": "track"}
    assert db.get_playlist("pl-a")["monitor_mode"] == "track"  # the stored mode is never rewritten
    db.update_user_admin_fields("alice", {"permissions": BASE | BIT})
    assert effective_playlist_modes(db)["pl-a"] == "track"
    db.update_user_admin_fields("alice", {"permissions": BASE})
    assert effective_playlist_modes(db)["pl-a"] == "none"


def test_gate_apply_logs_and_skips_for_creator_without_bit(db, config, caplog):
    _user(db, "alice")
    db.upsert_playlist("pl-a", "Alice", creator_id="alice")
    with caplog.at_level("INFO"), patch("plex_playlist_sync.list_monitoring.apply_list_item") as apply_item:
        apply_playlist_missing(db, config, "pl-a")
    apply_item.assert_not_called()
    assert any("may not auto-request" in r.message for r in caplog.records)
