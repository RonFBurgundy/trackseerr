"""Strict-mode backstop for ``/api/mixes`` (wave 2b), admin and non-admin."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plex_playlist_sync.api.dependencies import get_discovery_client, get_media_client
from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401

X = "/api/mixes"


@pytest.fixture
def api(client):
    client.app.dependency_overrides[get_discovery_client] = lambda: object()
    client.app.dependency_overrides[get_media_client] = lambda: None
    return client


def test_crud_roles(api, db, admin, alice):
    mix = ok(api.post(X, json={"mix_type": "artist_radio", "seed_artist": "Radiohead", "quality_profile_id": "ignored"}, headers=alice), 201)
    assert mix["user_id"] == "alice-1" and mix["quality_profile_id"] is None and mix["excluded_genres"] == []
    assert mix["name"] and mix["last_generated_at"] is None
    assert [m["id"] for m in ok(api.get(X, headers=alice))] == [mix["id"]]
    upd = ok(api.put(f"{X}/{mix['id']}", json={"track_count": 40, "enabled": False}, headers=alice))
    assert upd["track_count"] == 40 and upd["enabled"] is False
    admin_mix = ok(api.post(X, json={"mix_type": "artist_radio", "seed_artist": "Blur", "user_id": "alice-1"}, headers=admin), 201)
    assert admin_mix["user_id"] == "alice-1"
    assert len(ok(api.get(f"{X}?user_id=alice-1", headers=admin))) == 2
    assert api.delete(f"{X}/{mix['id']}", headers=alice).status_code == 204


def test_result_and_generate(api, db, alice):
    mix = ok(api.post(X, json={"mix_type": "artist_radio", "seed_artist": "Radiohead"}, headers=alice), 201)
    assert api.get(f"{X}/{mix['id']}/result", headers=alice).status_code == 404
    result = {"mix_id": mix["id"], "generated_at": "2026-10-07T10:00:00+00:00", "total": 1, "available": 1, "missing": 0,
              "acquisitions_queued": 0, "quota_remaining": 5, "synced": False, "sync_error": "No media server connected",
              "tracks": [{"artist": "Radiohead", "title": "Airbag", "album": None, "origin": "familiar", "status": "available"}]}
    db.record_mix_result(mix["id"], json.dumps(result))
    assert ok(api.get(f"{X}/{mix['id']}/result", headers=alice)) == result
    assert ok(api.get(f"{X}", headers=alice))[0]["last_generated_at"]


def test_preview_and_generate(api, db, alice):
    mix = ok(api.post(X, json={"mix_type": "artist_radio", "seed_artist": "Radiohead"}, headers=alice), 201)
    tracks = [SimpleNamespace(artist="Radiohead", title="Airbag", album=None, origin="discovery")]
    with patch("plex_playlist_sync.api.routes.mixes.compile_user_mix", return_value=tracks):
        assert ok(api.post(f"{X}/{mix['id']}/preview", headers=alice)) == {
            "tracks": [{"artist": "Radiohead", "title": "Airbag", "album": None, "origin": "discovery"}]
        }
    with patch("plex_playlist_sync.api.routes.mixes.generate_and_sync") as gen:
        assert ok(api.post(f"{X}/{mix['id']}/generate", headers=alice), 202) == {"status": "queued"}
        gen.assert_called_once()
