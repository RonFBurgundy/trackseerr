"""Phase 4 backend: paged library lists, the scrubber group index, sort-key persistence and tier/mode gating.

The critical property, asserted for every list / sort / direction / filter: each group the index returns starts at
exactly the offset where the paged list first shows that group, i.e. the item at ``offset`` carries the group's label
and the item at ``offset - 1`` does not.
"""

import re
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library_paging
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.list_index import (
    NULL_LABEL,
    format_size,
    group_label_for_key,
    group_label_for_name,
    library_sort_key,
)
from trackseerr.storage import Database

# ------------------------------------------------------------------------------------------------ fixtures


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


ARTIST_NAMES = [
    "The Beatles", "Beatles Tribute", "A Tribe Called Quest", "An Horse", "Éric Clapton", "Eric Church", "Ørsted",
    "ábc", "123 Band", "!!!", "\U0001F600 Emoji", "", "[Brackets]", "Zebra", "zzz", "Жук", "The", "A",
    "Muse", "Metallica", "Mötley Crüe", "the who", "THE XX", "Anna", "Cher", "Daft Punk", "Radiohead",
    "Nine Inch Nails", "Ñandú", "Blur",
]
ALBUM_TITLES = ["The Wall", "A Night", "An Album", "Éclat", "OK Computer", "9", "#1 Hits", "Zen", "Abbey Road", ""]


def _seed(db, *, wide_dates=True):
    """30 artists (varied monitored / created_at), 0-4 albums each, 2 tracks per album; some tracks have files."""
    n_albums = 0
    n_tracks = 0
    for i, name in enumerate(ARTIST_NAMES):
        artist = db.upsert_library_artist(
            {
                "id": f"ar-{i:02d}", "name": name, "monitored": i % 3 != 0,
                "created_at": f"20{20 + i % 6}-0{1 + i % 9}-1{i % 10} 10:00:00",
            }
        )
        for j in range(i % 5):
            idx = n_albums
            n_albums += 1
            if wide_dates:
                year = 1990 + (idx * 7) % 35
            else:
                year = 2024 + (idx % 2)
            release = [f"{year}-0{1 + idx % 9}-15", f"{year}-0{1 + idx % 9}", str(year), None, ""][idx % 5]
            album = db.upsert_library_album(
                {
                    "id": f"al-{idx:03d}", "artist_id": artist["id"], "title": ALBUM_TITLES[idx % len(ALBUM_TITLES)],
                    "release_date": release, "year": None if idx % 7 == 0 else year, "monitored": idx % 4 != 0,
                    "created_at": f"2023-0{1 + idx % 9}-0{1 + idx % 9} 08:00:00",
                }
            )
            for k in range(2):
                tid = f"tr-{n_tracks:03d}"
                track = db.upsert_library_track(
                    {
                        "id": tid, "album_id": album["id"], "artist_id": artist["id"],
                        "title": ["Intro", "The Song", "Ünder", "9 Lives", "Zed", "An End", "~tilde", "Outro"][
                            n_tracks % 8
                        ],
                        "track_number": k + 1, "monitored": n_tracks % 5 != 0,
                        "created_at": f"2022-0{1 + n_tracks % 9}-2{n_tracks % 9} 09:00:00",
                    }
                )
                if n_tracks % 6 != 0:  # every sixth track has no file
                    db.upsert_library_file(
                        {
                            "id": f"f-{tid}", "track_id": track["id"], "file_path": f"/m/{tid}.flac",
                            "relative_path": f"{tid}.flac", "codec": "FLAC", "quality_name": "FLAC",
                            "size_bytes": (n_tracks * 37 % 50 + 1) * 700_000 + (n_tracks % 4) * 9_000_000,
                        }
                    )
                n_tracks += 1
    return n_albums, n_tracks


@pytest.fixture
def seeded(test_db):
    _seed(test_db)
    return test_db


# ------------------------------------------------------------------------------------------ the property


def _get(api, headers, path, **params):
    res = api.get(path, headers=headers, params={k: v for k, v in params.items() if v is not None})
    assert res.status_code == 200, (path, params, res.text)
    return res.json()


def _month_mode(groups):
    return any(re.fullmatch(r"[A-Z][a-z]{2} \d{4}", g["label"]) for g in groups)


def _date_label(value, month_mode):
    if not value:
        return NULL_LABEL
    value = str(value)
    if month_mode and len(value) >= 7:
        names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
        return f"{names[int(value[5:7]) - 1]} {value[:4]}"
    return value[:4]


def _assert_index(api, headers, base, params, value_of, kind):
    """Walks every group of ``<base>/index`` against ``<base>`` paged at page_size=1."""
    idx_path = base[: -len("/paged")] + "/index" if base.endswith("/paged") else base + "/index"
    idx = _get(api, headers, idx_path, **params)
    groups = idx["groups"]
    first = _get(api, headers, base, page=1, page_size=1, **params)
    total = first["total"]
    assert idx["total"] == total == sum(g["count"] for g in groups)
    assert idx["sort_key"] == params.get("sort_key", idx["sort_key"])
    running = 0
    for g in groups:
        assert g["offset"] == running and g["count"] > 0
        running += g["count"]
    if kind == "date":
        month = _month_mode(groups)
        label_of = lambda rec: _date_label(value_of(rec), month)  # noqa: E731
    elif kind == "name":
        label_of = lambda rec: group_label_for_name(value_of(rec))  # noqa: E731
    else:
        label_of = None
    for g in groups:
        item = _get(api, headers, base, page=g["offset"] + 1, page_size=1, **params)["records"][0]
        last = _get(api, headers, base, page=g["offset"] + g["count"], page_size=1, **params)["records"][0]
        before = (
            _get(api, headers, base, page=g["offset"], page_size=1, **params)["records"][0] if g["offset"] else None
        )
        if label_of is not None:
            assert label_of(item) == g["label"], (g, item)
            assert label_of(last) == g["label"], (g, last)
            if before is not None:
                assert label_of(before) != g["label"], (g, before)
        else:  # number sorts: values never split across a boundary and move monotonically
            if before is not None:
                assert value_of(before) != value_of(item)
    labels = [g["label"] for g in groups]
    assert len(labels) == len(set(labels))
    return groups


# --------------------------------------------------------------------------------------------- normalizer


class TestSortKey:
    @pytest.mark.parametrize(
        "name,key",
        [
            ("The Beatles", "BEATLES"), ("the who", "WHO"), ("A Tribe Called Quest", "TRIBE CALLED QUEST"),
            ("An Horse", "HORSE"), ("Éric", "ERIC"), ("Ñandú", "NANDU"), ("Ørsted", "ORSTED"), ("Æon", "AEON"),
            ("a-ha", "A-HA"), ("The", "THE"), ("A", "A"), ("Anna", "ANNA"), ("Theory", "THEORY"),
            ("123", "#123"), ("!!!", "#!!!"), ("", "#"), (None, "#"), ("\U0001F600", "#\U0001F600"),
            ("[x]", "#[X]"), ("Ж", "#Ж"), ("  the   who ", "WHO"),
        ],
    )
    def test_key(self, name, key):
        assert library_sort_key(name) == key

    def test_labels(self):
        assert group_label_for_name("The Beatles") == "B"
        assert group_label_for_name("Éric") == "E"
        for odd in ("123", "!!!", "", "\U0001F600", "[x]", "Ж"):
            assert group_label_for_name(odd) == "#"
        assert group_label_for_key("") == "#" and group_label_for_key(None) == "#"

    def test_hash_block_is_contiguous_before_letters(self):
        keys = sorted(library_sort_key(n) for n in ARTIST_NAMES)
        labels = [group_label_for_key(k) for k in keys]
        collapsed = [lab for i, lab in enumerate(labels) if i == 0 or labels[i - 1] != lab]
        assert collapsed == sorted(set(collapsed)) and collapsed[0] == "#"

    def test_format_size(self):
        assert format_size(5 * 1024 * 1024) == "5 MB"
        assert format_size(1536 * 1024) == "1.5 MB"
        assert format_size(900) == "900 B"


# --------------------------------------------------------------------------------------------- migration


class TestMigrationV34:
    def test_registered_and_columns_and_indexes(self, test_db):
        versions = {r[0] for r in test_db.conn.execute("SELECT version FROM schema_migrations")}
        assert 34 in versions
        for table, col in (("library_artists", "sort_name"), ("library_albums", "sort_title"),
                           ("library_tracks", "sort_title")):
            assert col in {r[1] for r in test_db.conn.execute(f"PRAGMA table_info({table})")}
        idx = {r[1] for r in test_db.conn.execute("PRAGMA index_list(library_artists)")}
        assert "idx_lib_artists_sort_name" in idx

    def test_backfill_from_pre_v34_shape_and_idempotent(self, test_db):
        _seed(test_db)
        expected = {
            "artists": dict(test_db.conn.execute("SELECT id, sort_name FROM library_artists").fetchall()),
            "albums": dict(test_db.conn.execute("SELECT id, sort_title FROM library_albums").fetchall()),
            "tracks": dict(test_db.conn.execute("SELECT id, sort_title FROM library_tracks").fetchall()),
        }
        # Reproduce the pre-v34 schema: no indexes on, no column for, the sort keys.
        for idx in ("idx_lib_artists_sort_name", "idx_lib_albums_sort_title", "idx_lib_tracks_sort_title"):
            test_db.conn.execute(f"DROP INDEX {idx}")
        test_db.conn.execute("ALTER TABLE library_artists DROP COLUMN sort_name")
        test_db.conn.execute("ALTER TABLE library_albums DROP COLUMN sort_title")
        test_db.conn.execute("ALTER TABLE library_tracks DROP COLUMN sort_title")
        test_db.conn.commit()

        cur = test_db.conn.cursor()
        test_db._migration_v34(cur)
        test_db.conn.commit()
        got = {
            "artists": dict(test_db.conn.execute("SELECT id, sort_name FROM library_artists").fetchall()),
            "albums": dict(test_db.conn.execute("SELECT id, sort_title FROM library_albums").fetchall()),
            "tracks": dict(test_db.conn.execute("SELECT id, sort_title FROM library_tracks").fetchall()),
        }
        assert got == expected and expected["artists"]["ar-00"] == "BEATLES"

        # A second run is a no-op (and a hand-edited key is not clobbered: only empty defaults are backfilled).
        test_db.conn.execute("UPDATE library_artists SET sort_name = 'KEEP' WHERE id = 'ar-01'")
        test_db._migration_v34(test_db.conn.cursor())
        test_db.conn.commit()
        assert test_db.conn.execute("SELECT sort_name FROM library_artists WHERE id = 'ar-01'").fetchone()[0] == "KEEP"
        assert test_db.conn.execute(
            "SELECT COUNT(*) FROM library_artists WHERE sort_name = ''"
        ).fetchone()[0] == 0

    def test_reopening_a_file_db_does_not_remigrate(self, tmp_path):
        path = tmp_path / "t.db"
        db = Database(path)
        db.upsert_library_artist({"id": "a", "name": "The Cure"})
        db.close()
        db = Database(path)
        assert db.get_library_artist("a")["sort_name"] == "CURE"
        assert db.conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 34").fetchone()[0] == 1
        db.close()


# --------------------------------------------------------------------------------------------- write paths


class TestWritePaths:
    def test_artist_insert_and_rename(self, test_db):
        row = test_db.upsert_library_artist({"id": "a1", "name": "The Beatles"})
        assert row["sort_name"] == "BEATLES"
        row = test_db.upsert_library_artist({"id": "a1", "name": "Éric Clapton"})
        assert row["sort_name"] == "ERIC CLAPTON"
        assert test_db.get_library_artist("a1")["sort_name"] == "ERIC CLAPTON"

    def test_album_and_track_insert_and_rename(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "X"})
        alb = test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "The Wall"})
        assert alb["sort_title"] == "WALL"
        assert test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "An Ode"})["sort_title"] == "ODE"
        trk = test_db.upsert_library_track({"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "9 Lives"})
        assert trk["sort_title"] == "#9 LIVES"
        assert test_db.upsert_library_track(
            {"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Ünder"}
        )["sort_title"] == "UNDER"

    def test_partial_updates_leave_keys_alone(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "The Beatles"})
        test_db.set_artist_monitored("a1", False)
        assert test_db.get_library_artist("a1")["sort_name"] == "BEATLES"

    def test_lidarr_importer_populates_keys(self, test_db):
        from unittest.mock import MagicMock

        from trackseerr.clients.lidarr import LidarrClient
        from trackseerr.lidarr_migration import LidarrMigrationJob

        client = MagicMock(spec=LidarrClient)
        client.get_all_artists.return_value = [
            {"id": 1, "artistName": "The Cure", "foreignArtistId": "m-1", "path": "/m/Cure", "monitored": True}
        ]
        client.get_all_albums.return_value = [
            {"id": 2, "artistId": 1, "title": "A Forest", "foreignAlbumId": "m-2", "year": 1980, "monitored": True}
        ]
        client.get_all_tracks.return_value = [
            {"id": 3, "artistId": 1, "albumId": 2, "title": "The Hanging Garden", "trackNumber": 1, "discNumber": 1,
             "duration": 1000, "monitored": True, "foreignTrackId": "m-3"}
        ]
        client.get_all_track_files.return_value = []
        res = LidarrMigrationJob().run_migration(test_db, client, auto_switch_mode=False)
        assert res["status"] == "completed"
        artist = test_db.get_library_artist_by_name("The Cure")
        assert artist["sort_name"] == "CURE"
        album = test_db.get_library_album_by_title(artist["id"], "A Forest")
        assert album["sort_title"] == "FOREST"
        assert test_db.get_library_track_by_title(album["id"], "The Hanging Garden", 1)["sort_title"] == "HANGING GARDEN"

    def test_scanner_style_dict_upserts(self, test_db):
        """The scanner and acquisition worker upsert plain dicts through the same storage methods."""
        a = test_db.upsert_library_artist({"name": "An Artist", "path": "/m/a"})
        b = test_db.upsert_library_album({"artist_id": a["id"], "title": "The Record"})
        t = test_db.upsert_library_track({"album_id": b["id"], "artist_id": a["id"], "title": "A Song"})
        assert (a["sort_name"], b["sort_title"], t["sort_title"]) == ("ARTIST", "RECORD", "SONG")


# ------------------------------------------------------------------------- the paged lists + index property

NAME_SORTS = {
    "artists": ("name", lambda r: r["name"]),
    "albums": ("title", lambda r: r["title"]),
    "tracks": ("title", lambda r: r["title"]),
}


class TestPagedShape:
    def test_artists_shape_and_counts(self, api, admin_h, seeded):
        body = _get(api, admin_h, "/api/library/artists/paged", page_size=200)
        assert set(body) == {"mode", "page", "page_size", "total", "sort_key", "sort_dir", "records"}
        assert body["mode"] == "native" and body["total"] == len(ARTIST_NAMES) == len(body["records"])
        rec = next(r for r in body["records"] if r["id"] == "ar-04")
        assert rec["album_count"] == 4 and rec["track_count"] == 8 and "image_url" in rec
        assert body["sort_key"] == "name" and body["sort_dir"] == "asc"
        legacy = api.get("/api/library/artists?limit=1000", headers=admin_h).json()
        assert {r["id"]: r["album_count"] for r in legacy} == {r["id"]: r["album_count"] for r in body["records"]}

    def test_albums_and_tracks_shape(self, api, admin_h, seeded):
        alb = _get(api, admin_h, "/api/library/albums/paged", page_size=5)["records"][0]
        assert "artist_name" in alb and "track_count" in alb
        trk = _get(api, admin_h, "/api/library/tracks/paged", page_size=5)["records"][0]
        assert {"artist_name", "album_title", "file"} <= set(trk)

    def test_pagination_covers_everything_without_overlap(self, api, admin_h, seeded):
        seen = []
        for page in range(1, 5):
            seen += [r["id"] for r in _get(api, admin_h, "/api/library/artists/paged", page=page, page_size=8,
                                           sort_key="album_count", sort_dir="desc")["records"]]
        assert len(seen) == len(set(seen)) == len(ARTIST_NAMES)

    def test_name_order_ignores_articles_and_accents(self, api, admin_h, seeded):
        names = [r["name"] for r in _get(api, admin_h, "/api/library/artists/paged", page_size=200)["records"]]
        keys = [library_sort_key(n) for n in names]
        assert keys == sorted(keys)
        assert names.index("The Beatles") < names.index("Beatles Tribute") < names.index("Blur")
        assert names.index("Éric Clapton") > names.index("Eric Church") or names.index("Éric Clapton") < names.index("Ørsted")
        assert names[0] in ("", "!!!", "123 Band", "[Brackets]", "\U0001F600 Emoji", "Жук")

    def test_unknown_sort_key_422(self, api, admin_h):
        for kind in ("artists", "albums", "tracks"):
            assert api.get(f"/api/library/{kind}/paged?sort_key=bogus", headers=admin_h).status_code == 422
            assert api.get(f"/api/library/{kind}/index?sort_key=bogus", headers=admin_h).status_code == 422
            assert api.get(f"/api/library/{kind}/paged?sort_dir=sideways", headers=admin_h).status_code == 422
        assert api.get("/api/library/albums/paged?sort_key=name", headers=admin_h).status_code == 422
        assert api.get("/api/library/tracks/paged?page_size=201", headers=admin_h).status_code == 422

    def test_old_endpoints_unchanged(self, api, admin_h, seeded):
        assert len(api.get("/api/library/albums?limit=1000", headers=admin_h).json()) > 0
        assert len(api.get("/api/library/tracks?limit=1000", headers=admin_h).json()) > 0
        album = api.get("/api/library/albums/al-001", headers=admin_h)
        assert album.status_code == 200


class TestSearchAndFilters:
    def test_q_is_server_side_and_escaped(self, api, test_db, admin_h):
        for i, name in enumerate(["100% Pure", "100 Proof", "a_b", "axb", "back\\slash", "Plain"]):
            test_db.upsert_library_artist({"id": f"x{i}", "name": name})
        q = lambda text: [r["name"] for r in _get(api, admin_h, "/api/library/artists/paged", q=text)["records"]]  # noqa: E731
        assert q("100%") == ["100% Pure"]
        assert q("a_b") == ["a_b"]
        assert q("\\") == ["back\\slash"]
        assert q("%") == ["100% Pure"]
        assert sorted(q("pure")) == ["100% Pure"]
        assert _get(api, admin_h, "/api/library/artists/paged", q="%")["total"] == 1

    def test_q_is_accent_and_case_insensitive(self, api, seeded, admin_h):
        def names(kind, field, text):
            return sorted(r[field] for r in _get(api, admin_h, f"/api/library/{kind}/paged", q=text, page_size=200)["records"])

        assert names("artists", "name", "ERIC") == ["Eric Church", "Éric Clapton"]
        assert names("artists", "name", "éric c") == ["Eric Church", "Éric Clapton"]
        assert names("artists", "name", "orsted") == ["Ørsted"]
        assert names("artists", "name", "ØRSTED") == ["Ørsted"]
        assert names("artists", "name", "abc") == ["ábc"]
        assert names("artists", "name", "motley crue") == ["Mötley Crüe"]
        assert names("artists", "name", "nandu") == ["Ñandú"]
        assert {r for r in names("albums", "title", "ECLAT")} == {"Éclat"}
        assert "Ünder" in names("tracks", "title", "under")
        # accented needle against unaccented data, and the index/total see the same filter
        assert names("artists", "name", "ÉRIC C") == ["Eric Church", "Éric Clapton"]
        assert _get(api, admin_h, "/api/library/artists/index", q="ERIC")["total"] == 2

    def test_sql_injection_attempt_is_inert(self, api, seeded, admin_h):
        body = _get(api, admin_h, "/api/library/artists/paged", q="'; DROP TABLE library_artists; --")
        assert body["total"] == 0
        assert _get(api, admin_h, "/api/library/artists/paged")["total"] == len(ARTIST_NAMES)

    def test_monitored_and_id_filters(self, api, admin_h, seeded):
        mon = _get(api, admin_h, "/api/library/artists/paged", monitored_only="true", page_size=200)
        assert mon["total"] == sum(1 for i in range(len(ARTIST_NAMES)) if i % 3 != 0)
        assert all(r["monitored"] for r in mon["records"])
        alb = _get(api, admin_h, "/api/library/albums/paged", artist_id="ar-04", page_size=200)
        assert alb["total"] == 4 and {r["artist_id"] for r in alb["records"]} == {"ar-04"}
        trk = _get(api, admin_h, "/api/library/tracks/paged", album_id="al-001", page_size=200)
        assert trk["total"] == 2 and {r["album_id"] for r in trk["records"]} == {"al-001"}
        trk = _get(api, admin_h, "/api/library/tracks/paged", artist_id="ar-04", page_size=200)
        assert trk["total"] == 8

    def test_album_search_matches_artist_name(self, api, admin_h, seeded):
        body = _get(api, admin_h, "/api/library/albums/paged", q="radiohead", page_size=200)
        assert body["total"] > 0 and {r["artist_name"] for r in body["records"]} == {"Radiohead"}


class TestIndexProperty:
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    @pytest.mark.parametrize("kind", ["artists", "albums", "tracks"])
    def test_name_sorts(self, api, admin_h, seeded, kind, sort_dir):
        key, value_of = NAME_SORTS[kind]
        groups = _assert_index(api, admin_h, f"/api/library/{kind}/paged", {"sort_key": key, "sort_dir": sort_dir},
                               value_of, "name")
        assert groups
        letters = [g["label"] for g in groups]
        assert letters == sorted(letters, reverse=sort_dir == "desc")
        if kind == "artists":
            assert "#" in letters and {"B", "E", "T"} <= set(letters)  # "The Beatles" files under B, not T

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_artist_name_on_related_sorts(self, api, admin_h, seeded, sort_dir):
        _assert_index(api, admin_h, "/api/library/albums/paged", {"sort_key": "artist", "sort_dir": sort_dir},
                      lambda r: r["artist_name"], "name")
        _assert_index(api, admin_h, "/api/library/tracks/paged", {"sort_key": "artist", "sort_dir": sort_dir},
                      lambda r: r["artist_name"], "name")
        _assert_index(api, admin_h, "/api/library/tracks/paged", {"sort_key": "album", "sort_dir": sort_dir},
                      lambda r: r["album_title"], "name")

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    @pytest.mark.parametrize("wide", [True, False])
    def test_album_release_date(self, api, test_db, admin_h, wide, sort_dir):
        _seed(test_db, wide_dates=wide)
        groups = _assert_index(
            api, admin_h, "/api/library/albums/paged", {"sort_key": "release_date", "sort_dir": sort_dir},
            lambda r: r["release_date"] or (str(r["year"]) if r["year"] else None), "date",
        )
        assert _month_mode(groups) is (not wide)
        labels = [g["label"] for g in groups]
        assert NULL_LABEL in labels
        assert labels[0 if sort_dir == "asc" else -1] == NULL_LABEL  # SQLite orders NULLs first ascending, last descending

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    @pytest.mark.parametrize("kind", ["artists", "albums", "tracks"])
    def test_added_at(self, api, admin_h, seeded, kind, sort_dir):
        _assert_index(api, admin_h, f"/api/library/{kind}/paged", {"sort_key": "added_at", "sort_dir": sort_dir},
                      lambda r: r["created_at"], "date")

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_artist_album_count_quantiles(self, api, admin_h, seeded, sort_dir):
        groups = _assert_index(api, admin_h, "/api/library/artists/paged",
                               {"sort_key": "album_count", "sort_dir": sort_dir}, lambda r: r["album_count"], "number")
        assert 1 <= len(groups) <= 10
        assert sum(g["count"] for g in groups) == len(ARTIST_NAMES)
        assert all(re.search(r"albums?$", g["label"]) for g in groups)
        asc_labels = [g["label"] for g in groups][:: 1 if sort_dir == "asc" else -1]
        assert asc_labels[0].startswith("0")

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_track_size_quantiles(self, api, admin_h, seeded, sort_dir):
        params = {"sort_key": "size_bytes", "sort_dir": sort_dir}
        groups = _assert_index(api, admin_h, "/api/library/tracks/paged", params,
                               lambda r: (r["file"] or {}).get("size_bytes"), "number")
        total = _get(api, admin_h, "/api/library/tracks/paged", page_size=1)["total"]
        assert sum(g["count"] for g in groups) == total
        labels = [g["label"] for g in groups][:: 1 if sort_dir == "asc" else -1]
        assert labels[0] == NULL_LABEL  # tracks without a file sort first ascending
        sized = labels[1:]
        assert len(sized) > 2 and sized[0].startswith("<") and sized[-1].startswith("≥")
        assert any(re.fullmatch(r"[\d.]+(\s?[KMG]?B)?–[\d.]+ [KMG]?B|[\d.]+ [KMG]?B–[\d.]+ [KMG]?B", s)
                   for s in sized[1:-1])
        # values are monotone across the whole list, in the requested direction
        sizes = []
        for page in range(1, 4):
            sizes += [(r["file"] or {}).get("size_bytes") for r in
                      _get(api, admin_h, "/api/library/tracks/paged", page=page, page_size=40, **params)["records"]]
        present = [s for s in sizes if s is not None]
        assert present == sorted(present, reverse=sort_dir == "desc")

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_with_filters_and_q(self, api, admin_h, seeded, sort_dir):
        base = {"sort_dir": sort_dir}
        _assert_index(api, admin_h, "/api/library/artists/paged",
                      {**base, "sort_key": "name", "monitored_only": "true"}, lambda r: r["name"], "name")
        _assert_index(api, admin_h, "/api/library/artists/paged", {**base, "sort_key": "name", "q": "e"},
                      lambda r: r["name"], "name")
        _assert_index(api, admin_h, "/api/library/albums/paged",
                      {**base, "sort_key": "title", "monitored_only": "true", "q": "a"}, lambda r: r["title"], "name")
        _assert_index(api, admin_h, "/api/library/albums/paged",
                      {**base, "sort_key": "added_at", "artist_id": "ar-04"}, lambda r: r["created_at"], "date")
        _assert_index(api, admin_h, "/api/library/tracks/paged",
                      {**base, "sort_key": "title", "artist_id": "ar-09", "monitored_only": "true"},
                      lambda r: r["title"], "name")
        _assert_index(api, admin_h, "/api/library/tracks/paged", {**base, "sort_key": "size_bytes", "q": "e"},
                      lambda r: (r["file"] or {}).get("size_bytes"), "number")

    def test_empty_results_have_no_groups(self, api, admin_h, seeded):
        for kind, key in (("artists", "name"), ("albums", "release_date"), ("tracks", "size_bytes")):
            body = _get(api, admin_h, f"/api/library/{kind}/index", sort_key=key, q="zzzzqqqq")
            assert body["total"] == 0 and body["groups"] == []

    def test_index_total_matches_filtered_paged_total(self, api, admin_h, seeded):
        idx = _get(api, admin_h, "/api/library/albums/index", q="a", monitored_only="true")
        paged = _get(api, admin_h, "/api/library/albums/paged", q="a", monitored_only="true")
        assert idx["total"] == paged["total"] > 0

    def test_single_value_and_duplicate_values_do_not_split(self, api, test_db, admin_h):
        for i in range(12):
            test_db.upsert_library_artist({"id": f"s{i}", "name": f"Same {i}"})
        body = _get(api, admin_h, "/api/library/artists/index", sort_key="album_count")
        assert body["groups"] == [{"label": "0 albums", "offset": 0, "count": 12}]


# ------------------------------------------------------------------------------------- wanted + history


def _seed_wanted(db):
    artists = ["The Beatles", "Beatles Tribute", "Éric Clapton", "123 Band", "Zebra", "A Tribe Called Quest", "!!!", "Muse"]
    n = 0
    for i, name in enumerate(artists):
        db.upsert_library_artist({"id": f"wa{i}", "name": name, "monitored": True})
        for j in range(3):
            db.upsert_library_album({
                "id": f"wb{i}-{j}", "artist_id": f"wa{i}", "title": ["The Wall", "Éclat", "9"][j],
                "release_date": [f"2020-0{1 + i % 9}-01", "2021", None][(i + j) % 3], "monitored": True,
                "year": 1999 if j == 2 else None,
            })
            for k in range(2):
                tid = f"wt{n}"
                db.upsert_library_track({
                    "id": tid, "album_id": f"wb{i}-{j}", "artist_id": f"wa{i}",
                    "title": ["Intro", "The Song", "Ünder", "9 Lives"][n % 4], "monitored": True,
                })
                if n % 2:  # odd: has a file below cutoff (cutoff list); even: no file (missing list)
                    db.upsert_library_file({
                        "id": f"wf{n}", "track_id": tid, "file_path": f"/w/{tid}.mp3", "relative_path": f"{tid}.mp3",
                        "codec": "MP3", "quality_name": "MP3-128", "cutoff_met": False,
                    })
                if n % 3 == 0:
                    db.conn.execute("UPDATE library_tracks SET last_searched_at = ? WHERE id = ?",
                                    (f"2026-0{1 + n % 9}-0{1 + n % 7} 10:00:00", tid))
                n += 1
    db.conn.commit()


class TestWantedIndex:
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    @pytest.mark.parametrize("kind", ["missing", "cutoff"])
    @pytest.mark.parametrize(
        "key,kind_of,value_of",
        [
            ("artist", "name", lambda r: r["artist"]),
            ("album", "name", lambda r: r["album"]),
            ("title", "name", lambda r: r["title"]),
            ("release_date", "date", lambda r: r["release_date"]),
            ("last_searched_at", "date", lambda r: r["last_searched_at"]),
        ],
    )
    def test_offsets_match_paged_list(self, api, test_db, admin_h, kind, sort_dir, key, kind_of, value_of):
        _seed_wanted(test_db)
        total = _get(api, admin_h, f"/api/wanted/{kind}", page_size=1)["total"]
        assert total > 0
        if kind_of == "date":
            # last_searched_at is rendered as ISO-8601 with a 'T'; the bucket is the date prefix either way
            groups = _assert_index(api, admin_h, f"/api/wanted/{kind}",
                                   {"sort_key": key, "sort_dir": sort_dir}, value_of, "date")
            assert sum(g["count"] for g in groups) == total
        else:
            _assert_index(api, admin_h, f"/api/wanted/{kind}", {"sort_key": key, "sort_dir": sort_dir},
                          value_of, "name")

    def test_articles_fold_in_wanted_artist_sort(self, api, test_db, admin_h):
        _seed_wanted(test_db)
        recs = _get(api, admin_h, "/api/wanted/missing", page_size=200)["records"]
        keys = [library_sort_key(r["artist"]) for r in recs]
        assert keys == sorted(keys)
        groups = _get(api, admin_h, "/api/wanted/missing/index")["groups"]
        assert [g["label"] for g in groups][0] == "#"

    def test_unknown_sort_key_422(self, api, admin_h):
        assert api.get("/api/wanted/missing/index?sort_key=bogus", headers=admin_h).status_code == 422
        assert api.get("/api/wanted/cutoff/index?sort_dir=up", headers=admin_h).status_code == 422


def _seed_history(db, n=40):
    rows = []
    for i in range(n):
        month = 1 + i % 4
        rows.append((f"h{i}", "grabbed" if i % 3 else "imported", f"2026-0{month}-{10 + i % 15} 12:00:0{i % 10}"))
    for rid, event, created in rows:
        db.conn.execute("INSERT INTO download_history (id, event, created_at) VALUES (?, ?, ?)", (rid, event, created))
    db.conn.commit()


class TestHistoryIndex:
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    @pytest.mark.parametrize("event", [None, "grabbed"])
    def test_offsets_match_paged_list(self, api, test_db, admin_h, sort_dir, event):
        _seed_history(test_db)
        groups = _assert_index(api, admin_h, "/api/activity/history",
                               {"sort_dir": sort_dir, **({"event": event} if event else {})},
                               lambda r: r["date"] and r["date"].replace("T", " "), "date")
        assert all(re.fullmatch(r"[A-Z][a-z]{2} 2026", g["label"]) for g in groups)

    def test_defaults_and_validation(self, api, admin_h):
        body = _get(api, admin_h, "/api/activity/history/index")
        assert body == {"sort_key": "date", "sort_dir": "desc", "total": 0, "groups": []}
        assert api.get("/api/activity/history/index?sort_key=artist", headers=admin_h).status_code == 422
        assert api.get("/api/activity/history/index?event=bogus", headers=admin_h).status_code == 422


# ------------------------------------------------------------------------------- tier, auth, lidarr mode

ENDPOINTS = [f"/api/library/{k}/{s}" for k in ("artists", "albums", "tracks") for s in ("paged", "index")] + [
    "/api/wanted/missing/index",
    "/api/wanted/cutoff/index",
    "/api/activity/history/index",
]


class TestGating:
    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_non_admin_forbidden_anonymous_rejected(self, api, test_db, test_config, users, path):
        alice = _headers(users["alice"], test_db, test_config)
        assert api.get(path, headers=alice).status_code == 403
        assert api.get(path).status_code in (401, 403)

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_gateway_tier_404(self, test_db, tmp_path, users, path):
        cfg = Config(
            plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
            internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
        )
        app = create_app(db=test_db, config=cfg)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: cfg
        client = TestClient(app)
        assert client.get(path, headers=_headers(users["admin"], test_db, cfg)).status_code == 404

    def test_not_in_gateway_allowlists(self):
        from trackseerr.api import tier_middleware as tm

        listed = [p for _m, p in (*tm.GATEWAY_LOCAL_ALLOWLIST, *tm.GATEWAY_FORWARD_SERVICE_ALLOWLIST,
                                  *tm.GATEWAY_FORWARD_ALLOWLIST)]
        for path in ENDPOINTS:
            assert not any(
                p == path or (p.endswith("/**") and path.startswith(p[:-3])) for p in listed
            ), path

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_core_tier_dependency_blocks_gateway_role(self, test_db, tmp_path, path):
        from fastapi import HTTPException

        from trackseerr.api.dependencies import require_core_tier

        cfg = Config(plex_url="http://p", plex_token="t", data_dir=str(tmp_path), role="gateway",
                     internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251")
        with pytest.raises(HTTPException) as exc:
            require_core_tier(cfg)
        assert exc.value.status_code == 403


class TestLidarrMode:
    @pytest.fixture
    def lidarr_mode(self, test_db):
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": "lidarr-secret-key-abcdef123456"})
        test_db.update_media_management_settings({"library_mode": "lidarr"})

    @pytest.mark.parametrize(
        "path", ["/api/wanted/missing/index", "/api/wanted/cutoff/index", "/api/activity/history/index"]
    )
    def test_empty_groups_and_no_lidarr_http(self, api, admin_h, lidarr_mode, path):
        with patch("trackseerr.clients.lidarr.httpx.Client") as client_cls:
            res = api.get(path, headers=admin_h)
        assert res.status_code == 200
        assert res.json()["groups"] == [] and res.json()["total"] == 0
        client_cls.assert_not_called()
        client_cls.return_value.__enter__.return_value.get.assert_not_called()

    def test_lidarr_mode_still_validates_sort_key(self, api, admin_h, lidarr_mode):
        assert api.get("/api/wanted/missing/index?sort_key=bogus", headers=admin_h).status_code == 422

    def test_library_lists_come_from_lidarr_not_native_tables(self, api, admin_h, lidarr_mode, test_db):
        test_db.upsert_library_artist({"id": "a", "name": "The Cure"})
        with patch("trackseerr.clients.lidarr.httpx.Client") as client_cls:
            http = client_cls.return_value.__enter__.return_value
            http.get.return_value.status_code = 200
            http.get.return_value.json.return_value = [{"id": 7, "artistName": "Zebra"}]
            body = _get(api, admin_h, "/api/library/artists/index")
        assert body["mode"] == "lidarr" and body["groups"] == [{"label": "Z", "offset": 0, "count": 1}]


# ------------------------------------------------------------------------------------------ performance


class TestPerformance:
    N = 20_000

    @pytest.fixture
    def big_db(self):
        db = Database(":memory:")
        names = ["The Band %d" % i if i % 7 == 0 else "Éa Artist %d" % i if i % 11 == 0 else "Artist %05d" % i
                 for i in range(self.N)]
        db.conn.executemany(
            "INSERT INTO library_artists (id, name, clean_name, sort_name, monitored) VALUES (?, ?, ?, ?, 1)",
            [(f"id-{i:06d}", n, n.lower(), library_sort_key(n)) for i, n in enumerate(names)],
        )
        db.conn.commit()
        yield db
        db.close()

    def test_plans_use_the_sort_index(self, big_db):
        for direction in ("asc", "desc"):
            (sql, params), _count = library_paging.page_queries("artists", 300, 50, "name", direction)
            plan = " | ".join(r[3] for r in big_db.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            assert "idx_lib_artists_sort_name" in plan, plan
            assert "TEMP B-TREE" not in plan, plan
        # the index scan stays on the stored key too
        plan = " | ".join(
            r[3] for r in big_db.conn.execute(
                "EXPLAIN QUERY PLAN SELECT SUBSTR(a.sort_name, 1, 1) AS k, COUNT(*) FROM library_artists a GROUP BY k"
            ).fetchall()
        )
        assert "idx_lib_artists_sort_name" in plan, plan

    def test_index_and_deep_page_are_fast(self, big_db):
        def best(fn):
            samples = []
            for _ in range(3):
                start = time.perf_counter()
                fn()
                samples.append(time.perf_counter() - start)
            return min(samples)

        index_time = best(lambda: library_paging.index_library(big_db, "artists", "name", "asc"))
        page_time = best(lambda: library_paging.page_library(big_db, "artists", 399, 50, "name", "desc"))
        assert index_time < 0.2, index_time
        assert page_time < 0.2, page_time
        total, groups = library_paging.index_library(big_db, "artists", "name", "asc")
        assert total == self.N and sum(g["count"] for g in groups) == self.N
        assert groups[0]["label"] == "A" or groups[0]["label"] == "#"
        assert sqlite3.sqlite_version_info >= (3, 25)  # window functions back the quantile buckets


# ------------------------------------------------------------------------------------- v35 persisted search


class TestMigrationV35:
    def test_registered_and_columns(self, test_db):
        assert 35 in {r[0] for r in test_db.conn.execute("SELECT version FROM schema_migrations")}
        for table in ("library_artists", "library_albums", "library_tracks"):
            cols = {r[1] for r in test_db.conn.execute(f"PRAGMA table_info({table})")}
            assert {"search_text", "search_clean"} <= cols

    def test_upserts_persist_folded_text(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "Ørsted"})
        test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Éclat!"})
        test_db.upsert_library_track({"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Ünder"})
        q = lambda t, c: test_db.conn.execute(f"SELECT search_text, search_clean FROM {t} WHERE id = ?", (c,)).fetchone()  # noqa: E731
        assert tuple(q("library_artists", "a1")) == ("orsted", "orsted")
        assert q("library_albums", "b1")["search_text"] == "eclat!"
        assert q("library_albums", "b1")["search_clean"] == "eclat"
        assert q("library_tracks", "t1")["search_text"] == "under"
        test_db.upsert_library_track({"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Zoë"})
        assert q("library_tracks", "t1")["search_text"] == "zoe"
        # the folded columns never leak into API rows
        assert "search_text" not in test_db.get_library_track("t1")

    def test_backfill_and_idempotent(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "Mötley Crüe"})
        test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Éclat"})
        test_db.upsert_library_track({"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Ünder"})
        for table in ("library_artists", "library_albums", "library_tracks"):
            test_db.conn.execute(f"ALTER TABLE {table} DROP COLUMN search_text")
            test_db.conn.execute(f"ALTER TABLE {table} DROP COLUMN search_clean")
        test_db.conn.commit()
        test_db._migration_v35(test_db.conn.cursor())
        test_db.conn.commit()
        get = lambda t: test_db.conn.execute(f"SELECT search_text FROM {t}").fetchone()[0]  # noqa: E731
        assert (get("library_artists"), get("library_albums"), get("library_tracks")) == ("motley crue", "eclat", "under")
        test_db.conn.execute("UPDATE library_artists SET search_text = 'keep'")
        test_db._migration_v35(test_db.conn.cursor())
        assert get("library_artists") == "keep"  # only rows with empty folded columns are backfilled


class TestSearchFollowsRenames:
    """Related names are joined, not denormalized, so an artist rename is visible to album and track search."""

    def test_artist_rename_updates_album_and_track_search(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "Old Name"})
        test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Record"})
        test_db.upsert_library_track({"id": "t1", "album_id": "b1", "artist_id": "a1", "title": "Song"})
        hits = lambda kind, q: library_paging.page_library(test_db, kind, 1, 50, library_paging.default_sort_key(kind), "asc", q)[1]  # noqa: E731
        assert (hits("albums", "old name"), hits("tracks", "old name")) == (1, 1)
        test_db.upsert_library_artist({"id": "a1", "name": "Ünique Rename"})
        assert (hits("albums", "old name"), hits("tracks", "old name")) == (0, 0)
        assert (hits("albums", "unique rename"), hits("tracks", "unique rename"), hits("artists", "unique")) == (1, 1, 1)
        # album rename flows to its tracks
        test_db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Brand New"})
        assert hits("tracks", "brand new") == 1 and hits("tracks", "record") == 0

    def test_punctuation_widening_still_applies(self, test_db):
        test_db.upsert_library_artist({"id": "a1", "name": "AC/DC"})
        hits = lambda q: library_paging.page_library(test_db, "artists", 1, 50, "name", "asc", q)[1]  # noqa: E731
        assert hits("ac/dc") == 1 and hits("acdc") == 1 and hits("AC-DC") == 1


class TestSearchPerformance:
    def test_track_search_20k_rows_is_fast(self):
        db = Database(":memory:")
        db.upsert_library_artist({"id": "a1", "name": "Éric Clapton"})
        db.upsert_library_album({"id": "b1", "artist_id": "a1", "title": "Album"})
        rows = []
        for i in range(20_000):
            title = f"Love Night {i}" if i % 50 else f"Éric Song {i}"
            folded = title.lower().replace("é", "e")
            rows.append((f"t{i:06d}", "b1", "a1", title, folded, library_sort_key(title), folded, folded, 1))
        db.conn.executemany(
            "INSERT INTO library_tracks (id, album_id, artist_id, title, clean_title, sort_title, search_text, "
            "search_clean, monitored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        db.conn.commit()
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            records, total = library_paging.page_library(db, "tracks", 1, 50, "title", "asc", "nosuchthing")
            samples.append(time.perf_counter() - start)
        assert total == 0 and records == []
        _records, total = library_paging.page_library(db, "tracks", 1, 50, "title", "asc", "eric song")
        assert total == 400
        assert min(samples) < 0.1, samples
        plan = " ".join(r[3] for r in db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT t.* FROM library_tracks t WHERE t.search_text LIKE '%x%'").fetchall())
        assert "fold_text" not in plan
        db.close()
