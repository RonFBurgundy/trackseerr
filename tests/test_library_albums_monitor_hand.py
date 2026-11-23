"""Tests for monitored_by_hand flag on library_albums."""

from unittest.mock import patch

import pytest

from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)


@pytest.fixture(autouse=True)
def _no_background_hydration():
    with patch("trackseerr.artist_refresh_worker.artist_refresh_worker.refresh_once"):
        yield


def _seed_artist_and_albums(test_db, monitored_list: list[bool]) -> list[str]:
    artist = test_db.upsert_library_artist(
        {
            "name": "Test Artist",
            "monitored": True,
            "foreign_artist_id": "mb-artist-hand",
        }
    )
    artist_id = artist["id"]
    album_ids: list[str] = []
    for idx, mon in enumerate(monitored_list):
        alb = test_db.upsert_library_album(
            {
                "artist_id": artist_id,
                "title": f"Album {idx}",
                "monitored": mon,
                "foreign_album_id": f"mb-alb-hand-{idx}",
            }
        )
        album_ids.append(alb["id"])
    return album_ids


def test_put_monitored_marks_by_hand(app_and_client, test_db, seeded_users, test_config):
    _app, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    [alb_id] = _seed_artist_and_albums(test_db, [False])

    resp = client.put(
        f"/api/library/albums/{alb_id}/monitored",
        json={"monitored": True, "cascade_tracks": False},
        headers=headers,
    )
    assert resp.status_code == 200
    assert test_db.get_library_album(alb_id)["monitored_by_hand"] is True

    resp = client.put(
        f"/api/library/albums/{alb_id}/monitored",
        json={"monitored": False, "cascade_tracks": False},
        headers=headers,
    )
    assert resp.status_code == 200
    assert test_db.get_library_album(alb_id)["monitored_by_hand"] is False


def test_bulk_edit_marks_by_hand(app_and_client, test_db, seeded_users, test_config):
    _app, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    alb_ids = _seed_artist_and_albums(test_db, [True, False])

    resp = client.post(
        "/api/library/albums/bulk-edit",
        json={"album_ids": alb_ids, "monitored": True},
        headers=headers,
    )
    assert resp.status_code == 200
    for alb_id in alb_ids:
        assert test_db.get_library_album(alb_id)["monitored_by_hand"] is True


def test_auto_monitor_does_not_mark_by_hand(test_db):
    [alb_id] = _seed_artist_and_albums(test_db, [False])
    assert test_db.set_album_monitored(alb_id, True, cascade_tracks=False) is True
    alb = test_db.get_library_album(alb_id)
    assert alb["monitored"] is True
    assert alb["monitored_by_hand"] is False
