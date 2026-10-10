"""Tests for library facet filters, new sorts, and the /facets endpoint."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.api.routes.library._shared import NATIVE_ONLY_DETAIL
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_filters import LibraryFacetFilter
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def users(test_db):
    return {
        "admin": test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False),
    }


@pytest.fixture
def api(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_h(test_db, test_config, users):
    return _headers(users["admin"], test_db, test_config)


def _get(api, headers, path, **params):
    res = api.get(path, headers=headers, params={k: v for k, v in params.items() if v is not None})
    assert res.status_code == 200, (path, params, res.text)
    return res.json()


@pytest.fixture
def seeded_db(test_db):
    """Seeds ~6 artists with diverse genres, countries, years, types, band sizes, popularity and tags."""
    # Artist 1: Post-Rock / shoegaze, GB, group, 4 members, formed 1982, popularity 500
    test_db.upsert_library_artist(
        {"id": "ar-01", "name": "Artist One", "genres": "Post-Rock, shoegaze", "country": "GB", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("group", 4, 1982, 500, "ar-01"),
    )
    # Album 1A: 1985, Ambient, album
    test_db.upsert_library_album(
        {"id": "al-01", "artist_id": "ar-01", "title": "Album 1A", "year": 1985, "genres": "Ambient"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-01"))
    test_db.upsert_library_track({"id": "tr-01", "album_id": "al-01", "artist_id": "ar-01", "title": "Track 1A1"})
    # Album 1B: 1995, Post-Rock, ep (test duplicate artist+album genre count in facets)
    test_db.upsert_library_album(
        {"id": "al-02", "artist_id": "ar-01", "title": "Album 1B", "year": 1995, "genres": "Post-Rock"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("ep", "al-02"))
    test_db.upsert_library_track({"id": "tr-02", "album_id": "al-02", "artist_id": "ar-01", "title": "Track 1B1"})

    # Artist 2: rock, US, person, 1 member, formed 1975, popularity 1200
    test_db.upsert_library_artist(
        {"id": "ar-02", "name": "Artist Two", "genres": "rock", "country": "US", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("person", 1, 1975, 1200, "ar-02"),
    )
    # Album 2A: 1978, Classic Rock, album
    test_db.upsert_library_album(
        {"id": "al-03", "artist_id": "ar-02", "title": "Album 2A", "year": 1978, "genres": "Classic Rock"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-03"))
    test_db.upsert_library_track({"id": "tr-03", "album_id": "al-03", "artist_id": "ar-02", "title": "Track 2A1"})

    # Artist 3: electronic, ambient, DE, group, 2 members, formed 1992, popularity 300
    test_db.upsert_library_artist(
        {"id": "ar-03", "name": "Artist Three", "genres": "electronic, ambient", "country": "DE", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("group", 2, 1992, 300, "ar-03"),
    )
    # Album 3A: 1998, jazz, album (matches tracks via album genre jazz)
    test_db.upsert_library_album(
        {"id": "al-04", "artist_id": "ar-03", "title": "Album 3A", "year": 1998, "genres": "jazz"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-04"))
    test_db.upsert_library_track({"id": "tr-04", "album_id": "al-04", "artist_id": "ar-03", "title": "Track 3A1"})

    # Artist 4: Jazz, Blues, GB, group, 5 members, formed 1968, popularity 800
    test_db.upsert_library_artist(
        {"id": "ar-04", "name": "Artist Four", "genres": "Jazz, Blues", "country": "GB", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("group", 5, 1968, 800, "ar-04"),
    )
    # Album 4A: 1970, Blues, album
    test_db.upsert_library_album(
        {"id": "al-05", "artist_id": "ar-04", "title": "Album 4A", "year": 1970, "genres": "Blues"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-05"))
    test_db.upsert_library_track({"id": "tr-05", "album_id": "al-05", "artist_id": "ar-04", "title": "Track 4A1"})

    # Artist 5: NULL genres, FR, person, 1 member, NULL begin_year, NULL popularity
    test_db.upsert_library_artist(
        {"id": "ar-05", "name": "Artist Five", "genres": None, "country": "FR", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = NULL, popularity = NULL WHERE id = ?",
        ("person", 1, "ar-05"),
    )
    # Album 5A: 2005, single
    test_db.upsert_library_album(
        {"id": "al-06", "artist_id": "ar-05", "title": "Album 5A", "year": 2005, "genres": None}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("single", "al-06"))
    test_db.upsert_library_track({"id": "tr-06", "album_id": "al-06", "artist_id": "ar-05", "title": "Track 5A1"})

    # Artist 6: Metal, GB, group, 4 members, formed 2001, popularity 100
    test_db.upsert_library_artist(
        {"id": "ar-06", "name": "Artist Six", "genres": "Metal", "country": "GB", "monitored": True}
    )
    test_db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("group", 4, 2001, 100, "ar-06"),
    )
    # Album 6A: 2003, Metal, album
    test_db.upsert_library_album(
        {"id": "al-07", "artist_id": "ar-06", "title": "Album 6A", "year": 2003, "genres": "Metal"}
    )
    test_db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-07"))
    test_db.upsert_library_track({"id": "tr-07", "album_id": "al-07", "artist_id": "ar-06", "title": "Track 6A1"})

    test_db.conn.commit()
    return test_db


def test_genre_matches_whole_token_case_insensitive(api, admin_h, seeded_db):
    """Artist genres 'Post-Rock, shoegaze', other 'rock' -> genre=rock returns only the second; genre=POST-ROCK returns only the first."""
    # genre=rock should match ar-02 ("rock"), but NOT ar-01 ("Post-Rock, shoegaze")
    res1 = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "rock"})
    assert res1.status_code == 200
    body1 = res1.json()
    ids1 = [r["id"] for r in body1["records"]]
    assert ids1 == ["ar-02"]

    # genre=POST-ROCK should match ar-01, but NOT ar-02
    res2 = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "POST-ROCK"})
    assert res2.status_code == 200
    body2 = res2.json()
    ids2 = [r["id"] for r in body2["records"]]
    assert ids2 == ["ar-01"]


def test_genre_any_of_and_album_genres(api, admin_h, seeded_db):
    """Tracks list, genre=jazz&genre=shoegaze matches via album genres or artist genres."""
    res = api.get("/api/library/tracks/paged", headers=admin_h, params=[("genre", "jazz"), ("genre", "shoegaze")])
    assert res.status_code == 200
    body = res.json()
    tids = {r["id"] for r in body["records"]}
    # ar-01 has shoegaze -> tr-01, tr-02 match via artist genres
    # ar-03 has al-04 with jazz -> tr-04 matches via album genres
    # ar-04 has jazz -> tr-05 matches via artist genres
    assert tids == {"tr-01", "tr-02", "tr-04", "tr-05"}


def test_exclude_genre(api, admin_h, seeded_db):
    """Excluding a genre drops artists matching that genre."""
    res = api.get("/api/library/artists/paged", headers=admin_h, params={"exclude_genre": "rock"})
    assert res.status_code == 200
    body = res.json()
    ids = [r["id"] for r in body["records"]]
    # ar-02 ("rock") must be excluded; ar-01 ("Post-Rock, shoegaze") must remain
    assert "ar-02" not in ids
    assert "ar-01" in ids


def test_year_range_albums_tracks_and_artists(api, admin_h, seeded_db):
    """year_from=1980&year_to=1989 on albums, tracks, and artists (artist with any 80s album)."""
    # Albums
    res_al = api.get("/api/library/albums/paged", headers=admin_h, params={"year_from": 1980, "year_to": 1989})
    assert res_al.status_code == 200
    al_ids = [r["id"] for r in res_al.json()["records"]]
    assert al_ids == ["al-01"]  # year 1985

    # Tracks
    res_tr = api.get("/api/library/tracks/paged", headers=admin_h, params={"year_from": 1980, "year_to": 1989})
    assert res_tr.status_code == 200
    tr_ids = [r["id"] for r in res_tr.json()["records"]]
    assert tr_ids == ["tr-01"]

    # Artists (artist with any 80s album -> ar-01 has al-01 from 1985)
    res_ar = api.get("/api/library/artists/paged", headers=admin_h, params={"year_from": 1980, "year_to": 1989})
    assert res_ar.status_code == 200
    ar_ids = [r["id"] for r in res_ar.json()["records"]]
    assert ar_ids == ["ar-01"]


def test_country_and_artist_type_and_members(api, admin_h, seeded_db):
    """country=gb&artist_type=group&members_min=4&members_max=4."""
    res = api.get(
        "/api/library/artists/paged",
        headers=admin_h,
        params={"country": "gb", "artist_type": "group", "members_min": 4, "members_max": 4},
    )
    assert res.status_code == 200
    ids = {r["id"] for r in res.json()["records"]}
    # ar-01: GB, group, 4 members -> MATCH
    # ar-04: GB, group, 5 members -> NO MATCH
    # ar-06: GB, group, 4 members -> MATCH
    assert ids == {"ar-01", "ar-06"}


def test_formed_and_popularity_ranges_ignore_nulls(api, admin_h, seeded_db):
    """Artist with NULL begin_year is excluded by formed_from."""
    res1 = api.get("/api/library/artists/paged", headers=admin_h, params={"formed_from": 1980})
    assert res1.status_code == 200
    ids1 = {r["id"] for r in res1.json()["records"]}
    # ar-05 has NULL begin_year -> excluded
    assert "ar-05" not in ids1
    # ar-01 (1982), ar-03 (1992), ar-06 (2001) are included
    assert ids1 == {"ar-01", "ar-03", "ar-06"}

    # Popularity min ignores NULL popularity
    res2 = api.get("/api/library/artists/paged", headers=admin_h, params={"popularity_min": 100})
    assert res2.status_code == 200
    ids2 = {r["id"] for r in res2.json()["records"]}
    assert "ar-05" not in ids2
    assert "ar-06" in ids2


def test_tag_filter(api, admin_h, seeded_db):
    """Tag one artist via artist_tags -> tag=<id> on artists and tracks."""
    seeded_db.conn.execute("INSERT OR IGNORE INTO tags (id, label) VALUES (10, 'indie')")
    seeded_db.conn.execute("INSERT INTO artist_tags (artist_id, tag_id) VALUES ('ar-01', 10)")
    seeded_db.conn.commit()

    res_ar = api.get("/api/library/artists/paged", headers=admin_h, params={"tag": 10})
    assert res_ar.status_code == 200
    assert [r["id"] for r in res_ar.json()["records"]] == ["ar-01"]

    res_tr = api.get("/api/library/tracks/paged", headers=admin_h, params={"tag": 10})
    assert res_tr.status_code == 200
    assert {r["id"] for r in res_tr.json()["records"]} == {"tr-01", "tr-02"}


def test_filters_apply_to_index_total(api, admin_h, seeded_db):
    """/albums/index with a filter -> total equals /albums/paged total for the same filter."""
    params = {"album_type": "album"}
    paged = api.get("/api/library/albums/paged", headers=admin_h, params=params).json()
    indexed = api.get("/api/library/albums/index", headers=admin_h, params=params).json()

    assert indexed["total"] == paged["total"]
    assert indexed["total"] == sum(g["count"] for g in indexed["groups"])


def test_new_sorts(api, admin_h, seeded_db):
    """Artists sort_key=popularity&sort_dir=desc order; tracks sort_key=year; artists sort_key=formed with NULLs does not 500."""
    # Popularity sort
    res_pop = api.get(
        "/api/library/artists/paged", headers=admin_h, params={"sort_key": "popularity", "sort_dir": "desc"}
    )
    assert res_pop.status_code == 200
    pop_ids = [r["id"] for r in res_pop.json()["records"]]
    # 1200 (ar-02), 800 (ar-04), 500 (ar-01), 300 (ar-03), 100 (ar-06), 0/null (ar-05)
    assert pop_ids[:2] == ["ar-02", "ar-04"]

    # Year sort on tracks
    res_yr = api.get(
        "/api/library/tracks/paged", headers=admin_h, params={"sort_key": "year", "sort_dir": "asc"}
    )
    assert res_yr.status_code == 200
    records = res_yr.json()["records"]
    assert len(records) > 0

    # Formed sort on artists with NULL begin_year does not 500
    res_formed = api.get(
        "/api/library/artists/paged", headers=admin_h, params={"sort_key": "formed", "sort_dir": "asc"}
    )
    assert res_formed.status_code == 200
    # Also index does not 500
    res_formed_idx = api.get(
        "/api/library/artists/index", headers=admin_h, params={"sort_key": "formed", "sort_dir": "asc"}
    )
    assert res_formed_idx.status_code == 200


def test_bad_genre_and_too_many_values_422(api, admin_h, seeded_db):
    """genre=a%25b -> 422; 21 genre params -> 422."""
    res1 = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "a%b"})
    assert res1.status_code == 422

    res_comma = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "a,b"})
    assert res_comma.status_code == 422

    res_under = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "a_b"})
    assert res_under.status_code == 422

    # 21 genre parameters
    many = [("genre", f"genre{i}") for i in range(21)]
    res2 = api.get("/api/library/artists/paged", headers=admin_h, params=many)
    assert res2.status_code == 422


def test_facets_endpoint(api, admin_h, users, seeded_db, test_config):
    """Shape + counts (genre counted once per artist even if on artist and album), decades bucketed, non-admin -> 403."""
    res = api.get("/api/library/facets", headers=admin_h)
    assert res.status_code == 200
    data = res.json()

    for key in (
        "genres",
        "countries",
        "decades",
        "album_types",
        "artist_types",
        "year_min",
        "year_max",
        "formed_min",
        "formed_max",
        "members_max",
        "popularity_max",
    ):
        assert key in data, f"Missing {key} in facets response"

    # Genre counted once per artist:
    # ar-01 has 'post-rock' on artist and 'post-rock' on album al-02. It should count 1 artist!
    genre_map = {g["value"]: g["count"] for g in data["genres"]}
    assert genre_map.get("post-rock") == 1

    # Decades bucketed:
    decade_values = {d["value"] for d in data["decades"]}
    # Albums are from 1970, 1978, 1985, 1995, 1998, 2003, 2005
    assert 1970 in decade_values
    assert 1980 in decade_values
    assert 1990 in decade_values
    assert 2000 in decade_values

    # Scalar bounds
    assert data["year_min"] == 1970
    assert data["year_max"] == 2005
    assert data["formed_min"] == 1968
    assert data["formed_max"] == 2001
    assert data["members_max"] == 5
    assert data["popularity_max"] == 1200

    # Non-admin -> 403
    alice_h = _headers(users["alice"], seeded_db, test_config)
    res_alice = api.get("/api/library/facets", headers=alice_h)
    assert res_alice.status_code == 403


def test_lidarr_mode_rejects_facets(api, admin_h, seeded_db):
    """Set library mode to Lidarr -> /artists/paged?genre=rock -> 409."""
    seeded_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": "lidarr-secret-key-abcdef123456"})
    seeded_db.update_media_management_settings({"library_mode": "lidarr"})

    res = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "rock"})
    assert res.status_code == 409
    assert res.json()["detail"] == NATIVE_ONLY_DETAIL

    # Facets endpoint also rejected in Lidarr mode
    res_facets = api.get("/api/library/facets", headers=admin_h)
    assert res_facets.status_code == 409

    # Unfiltered call does not raise 409 (Lidarr branch proceeds)
    # With no actual Lidarr server mocked here it will raise 502/bad gateway or mock, but NOT 409!
    res_unfiltered = api.get("/api/library/artists/paged", headers=admin_h)
    assert res_unfiltered.status_code != 409


def test_facet_filter_dict_roundtrip():
    """LibraryFacetFilter.from_dict(f.to_dict()) == f; from_dict({}) is empty; unknown keys ignored."""
    f = LibraryFacetFilter(
        genres=("rock", "pop"),
        exclude_genres=("jazz",),
        countries=("US", "GB"),
        year_from=1980,
        year_to=1989,
        album_types=("album", "ep"),
        artist_types=("group",),
        members_min=2,
        members_max=6,
        formed_from=1975,
        formed_to=1985,
        popularity_min=100,
        popularity_max=5000,
        tag_ids=(1, 2, 3),
    )
    d = f.to_dict()
    roundtripped = LibraryFacetFilter.from_dict(d)
    assert roundtripped == f

    empty = LibraryFacetFilter.from_dict({})
    assert empty.is_empty()

    with_unknown = LibraryFacetFilter.from_dict({"genres": ["metal"], "extra_unknown_field": "val"})
    assert with_unknown.genres == ("metal",)
    assert not with_unknown.is_empty()


def test_injection_inert(api, admin_h, seeded_db):
    """genre="x') OR 1=1 --" -> 422 or empty result, never all rows."""
    res = api.get("/api/library/artists/paged", headers=admin_h, params={"genre": "x') OR 1=1 --"})
    # Either rejected with 422 or returns 0 records, never all rows
    if res.status_code == 200:
        records = res.json()["records"]
        assert len(records) == 0
    else:
        assert res.status_code == 422
