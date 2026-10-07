"""Strict-mode backstop for ``/api/scrobbles`` (wave 2b), including that secrets never appear."""

import json

from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401

S = "/api/scrobbles"


def test_own_config_and_secret_masking(client, db, alice):
    cfg = ok(client.get(f"{S}/config", headers=alice))
    assert cfg["scrobbling_enabled"] is True and cfg["lastfm_connected"] is False
    db.upsert_scrobble_config("alice-1", lastfm_username="al", lastfm_session_key="SESSION-SECRET",
                              listenbrainz_token="LB-SECRET", listenbrainz_username="al_lb")
    resp = client.get(f"{S}/config", headers=alice)
    assert "SESSION-SECRET" not in resp.text and "LB-SECRET" not in resp.text
    assert resp.json()["lastfm_connected"] is True and resp.json()["listenbrainz_connected"] is True
    updated = ok(client.put(f"{S}/config", json={"scrobbling_enabled": False, "unlink_lastfm": True}, headers=alice))
    assert updated["scrobbling_enabled"] is False and updated["lastfm_connected"] is False


def test_admin_user_configs_and_listens(client, db, admin, alice):
    db.upsert_scrobble_config("alice-1", lastfm_username="al", lastfm_session_key="SESSION-SECRET")
    users = client.get(f"{S}/users", headers=admin)
    assert "SESSION-SECRET" not in users.text and users.status_code == 200
    assert client.get(f"{S}/users", headers=alice).status_code == 403
    cfg = ok(client.put(f"{S}/users/alice-1/config", json={"scrobbling_enabled": True}, headers=admin))
    assert cfg["user_id"] == "alice-1" and cfg["username"] == "alice"
    db.insert_listen("alice-1", artist="Radiohead", title="Airbag", album="OK Computer", rating_key="1", duration_ms=1000, source="plex_webhook")
    mine = ok(client.get(f"{S}/listens", headers=alice))
    assert mine[0]["artist"] == "Radiohead" and mine[0]["lastfm_status"]
    assert len(ok(client.get(f"{S}/listens?user_id=alice-1", headers=admin))) == 1
    assert client.get(f"{S}/listens?user_id=admin-1", headers=alice).status_code == 403


def test_server_config_webhook_and_lastfm(client, db, admin, alice):
    cfg = ok(client.get(f"{S}/server-config", headers=admin))
    assert cfg["lastfm_configured"] is False and cfg["plex_history_poll_minutes"] == 15
    saved = ok(client.put(f"{S}/server-config", json={"lastfm_api_key": "abcd1234efgh5678", "lastfm_api_secret": "SECRETSECRET", "plex_history_poll_minutes": 30}, headers=admin))
    assert saved["lastfm_configured"] is True and saved["plex_history_poll_minutes"] == 30
    assert "SECRETSECRET" not in json.dumps(saved) and "abcd1234efgh5678" not in json.dumps(saved)
    assert client.get(f"{S}/server-config", headers=alice).status_code == 403
    url = ok(client.get(f"{S}/webhook-url", headers=admin))["url"]
    assert "/api/scrobbles/plex?token=" in url
    rotated = ok(client.post(f"{S}/webhook-secret/rotate", headers=admin))["url"]
    assert rotated != url
    assert client.get(f"{S}/webhook-url", headers=alice).status_code == 403
    auth = ok(client.get(f"{S}/lastfm/auth-url", headers=alice))
    assert "audioscrobbler" in auth["url"] or "last.fm" in auth["url"]


def test_plex_webhook_statuses(client, db):
    db.upsert_user("alice-1", "alice", "al@example.com", is_admin=False)
    token = db.get_plex_webhook_secret()
    ignored = client.post(f"{S}/plex?token={token}", json={"event": "library.new", "Metadata": {}})
    assert ok(ignored) == {"status": "ignored"}
    payload = {"event": "media.scrobble", "Account": {"id": 1, "title": "alice"},
               "Metadata": {"type": "track", "title": "Airbag", "grandparentTitle": "Radiohead", "parentTitle": "OK Computer"}}
    assert ok(client.post(f"{S}/plex?token={token}", json=payload))["status"] in ("ok", "ignored")
