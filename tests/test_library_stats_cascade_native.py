"""Native mode: stats payload, artist/album bulk unmonitor cascade and the track bulk-edit endpoint."""

from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from tests.test_monitoring_defaults_bulk_edit import _seed_album, _seed_artist


def _seed(db):
    _seed_artist(db, "A1")
    _seed_artist(db, "A2", monitored=False)
    _seed_album(db, "a1-own", "A1", with_file=True)
    _seed_album(db, "a1-miss", "A1", with_file=False)
    _seed_album(db, "a2-miss", "A2", with_file=False)


def test_native_stats_payload(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed(test_db)
    s = client.get("/api/library/stats", headers=h).json()
    assert s["source"] == "native"
    assert (s["artist_count"], s["monitored_artist_count"], s["unmonitored_artist_count"]) == (2, 1, 1)
    assert s["album_count"] == 3 and s["track_count"] == 3 and s["total_track_count"] == 3
    assert s["track_file_count"] == 1 and s["file_count"] == 1
    assert s["missing_track_count"] == 3 - 1  # monitored tracks without a file
    assert s["continuing_artist_count"] is None and s["ended_artist_count"] is None
    assert "monitored_track_count" in s and "cutoff_unmet_track_count" in s and "total_size_bytes" in s


def test_bulk_unmonitor_artists_with_flag_cascades_to_albums_and_tracks(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed(test_db)
    test_db.bulk_edit_library_artists(["A2"], monitored=True)
    r = client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["A1", "A2"], "monitored": False, "apply_monitor_to_albums": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"artists_updated": 2, "albums_monitored": 0, "albums_unmonitored": 3}
    assert not any(a["monitored"] for a in test_db.list_library_albums())
    assert not any(t["monitored"] for t in test_db.list_library_tracks(limit=100))


def test_bulk_unmonitor_artists_cascades_when_flag_omitted_but_not_when_false(
    app_and_client, test_db, test_config, seeded_users
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed(test_db)
    client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["A1"], "monitored": False, "apply_monitor_to_albums": False},
        headers=h,
    )
    assert test_db.get_library_artist("A1")["monitored"] is False
    assert test_db.get_library_album("a1-own")["monitored"] is True  # explicit opt-out
    client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["A1"], "monitored": False}, headers=h)
    assert test_db.get_library_album("a1-own")["monitored"] is False
    assert test_db.get_library_track("a1-own-t1")["monitored"] is False


def test_bulk_album_unmonitor_cascades_to_tracks(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed(test_db)
    r = client.post("/api/library/albums/bulk-edit", json={"album_ids": ["a1-own", "a1-miss"], "monitored": False}, headers=h)
    assert r.status_code == 200 and r.json() == {"albums_updated": 2}
    assert test_db.get_library_track("a1-own-t1")["monitored"] is False
    assert test_db.get_library_track("a1-miss-t1")["monitored"] is False


def test_track_bulk_edit(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed(test_db)
    r = client.post(
        "/api/library/tracks/bulk-edit",
        json={"track_ids": ["a1-own-t1", "a1-miss-t1", "nope"], "monitored": False},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"tracks_updated": 2}
    assert test_db.get_library_track("a1-own-t1")["monitored"] is False
    assert test_db.get_library_track("a2-miss-t1")["monitored"] is True
    r = client.post("/api/library/tracks/bulk-edit", json={"track_ids": ["a1-own-t1"], "monitored": True}, headers=h)
    assert r.json() == {"tracks_updated": 1}
    assert test_db.get_library_track("a1-own-t1")["monitored"] is True


def test_track_bulk_edit_validation_and_auth(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    assert client.post("/api/library/tracks/bulk-edit", json={"track_ids": [], "monitored": True}, headers=admin).status_code == 400
    assert client.post("/api/library/tracks/bulk-edit", json={"track_ids": ["x"], "monitored": True}).status_code in (401, 403)


def test_native_stats_track_count_only_monitored_albums(app_and_client, test_db, test_config, seeded_users):
    """track_count follows Lidarr (monitored albums only); total_track_count keeps every stored row."""
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist(test_db, "A1")
    _seed_album(test_db, "mon", "A1", with_file=True)
    _seed_album(test_db, "unmon-1", "A1", with_file=False, monitored=False)
    _seed_album(test_db, "unmon-2", "A1", with_file=False, monitored=False)
    # a monitored-flag track on an unmonitored album must not count as missing
    s = client.get("/api/library/stats", headers=h).json()
    assert s["total_track_count"] == 3
    assert s["track_count"] == 1
    assert s["track_file_count"] == 1
    assert s["missing_track_count"] == 0


def test_upsert_track_with_new_id_reuses_existing_row(test_db):
    from tests.test_monitoring_defaults_bulk_edit import _seed_album, _seed_artist

    _seed_artist(test_db, "A1")
    _seed_album(test_db, "al", "A1", with_file=False)
    base = {"album_id": "al", "artist_id": "A1", "title": "Song", "track_number": 3, "foreign_track_id": "f-1"}
    first = test_db.upsert_library_track({**base, "id": "t-a"})
    again = test_db.upsert_library_track({**base, "id": "t-b"})  # refresh path that minted a fresh uuid
    other_title = test_db.upsert_library_track({**base, "id": "t-c", "title": "Other", "foreign_track_id": None,
                                                "track_number": 4})
    assert again["id"] == first["id"] == "t-a"
    assert other_title["id"] == "t-c"
    assert len(test_db.list_library_tracks(album_id="al", limit=100)) == 3  # seeded "t" + Song + Other


def test_dedupe_migration_merges_duplicates_and_keeps_file_links(test_db):
    from tests.test_monitoring_defaults_bulk_edit import _seed_album, _seed_artist

    _seed_artist(test_db, "A1")
    _seed_album(test_db, "al", "A1", with_file=False)
    with test_db._lock:
        for tid, created in (("t-old", "2020-01-01 00:00:00"), ("t-dup1", "2021-01-01 00:00:00"),
                             ("t-dup2", "2022-01-01 00:00:00")):
            test_db.conn.execute(
                "INSERT INTO library_tracks (id, album_id, artist_id, title, clean_title, track_number, "
                "disc_number, monitored, foreign_track_id, created_at) "
                "VALUES (?, 'al', 'A1', 'Song', 'song', 5, 1, ?, 'f-9', ?)",
                (tid, 1 if tid == "t-dup2" else 0, created),
            )
        # the file hangs off a NEWER duplicate; it must survive and win
        test_db.conn.execute(
            "INSERT INTO library_files (id, track_id, file_path, relative_path, codec, quality_name, size_bytes) "
            "VALUES ('f1', 't-dup1', '/m/s.flac', 's.flac', 'FLAC', 'FLAC', 7)"
        )
        test_db.conn.commit()
        cur = test_db.conn.cursor()
        removed = test_db._dedupe_library_tracks(cur)
        test_db.conn.commit()
    assert removed == 2
    rows = test_db.list_library_tracks(album_id="al", limit=100)
    song = [r for r in rows if r["title"] == "Song"]
    assert len(song) == 1 and song[0]["id"] == "t-dup1" and song[0]["monitored"]
    assert test_db.get_library_file_for_track("t-dup1")["id"] == "f1"
    assert test_db.get_library_stats()["track_file_count"] == 1
