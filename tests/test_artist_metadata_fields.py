"""Unit tests for artist Spotle-style metadata fields (artist_type, member_count, begin_year, end_year, popularity)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.api.schemas.library import LibraryArtistRecord
from trackseerr.artist_refresh import (
    _RefreshState,
    _apply_mb_artist_details,
    _sync_deezer_discography,
)
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.mb_metadata_store import MbMetadataStore, reset_shared_enricher
from trackseerr.models import LibraryArtist
from trackseerr.storage import Database, SCHEMA_VERSION


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_artist_meta.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _reset_enricher():
    reset_shared_enricher()
    yield
    reset_shared_enricher()


def test_fresh_db_has_artist_metadata_columns(test_db: Database) -> None:
    """PRAGMA table_info(library_artists) contains the five columns; schema_migrations max == 72."""
    cur = test_db.conn.cursor()
    cur.execute("SELECT MAX(version) FROM schema_migrations")
    assert cur.fetchone()[0] == 72
    assert SCHEMA_VERSION == 72

    cur.execute("PRAGMA table_info(library_artists)")
    cols = {r[1] for r in cur.fetchall()}
    for col in ("artist_type", "member_count", "begin_year", "end_year", "popularity"):
        assert col in cols, f"Missing column {col} in library_artists"


def test_v71_db_migrates_to_v72(tmp_path: Path) -> None:
    """Database at v71 migrates smoothly to v72 and adds missing columns/indexes."""
    db_file = tmp_path / "v71.db"
    db = Database(str(db_file))
    with db._lock:
        db.conn.execute("DROP INDEX IF EXISTS idx_lib_artists_begin_year")
        db.conn.execute("DROP INDEX IF EXISTS idx_lib_artists_popularity")
        for col in ("artist_type", "member_count", "begin_year", "end_year", "popularity"):
            db.conn.execute(f"ALTER TABLE library_artists DROP COLUMN {col}")
        db.conn.execute("DELETE FROM schema_migrations WHERE version = 72")
        db.conn.execute("INSERT OR REPLACE INTO schema_migrations (version) VALUES (71)")
        db.conn.commit()
    db.close()

    db2 = Database(str(db_file))
    try:
        cur = db2.conn.cursor()
        cur.execute("SELECT MAX(version) FROM schema_migrations")
        assert cur.fetchone()[0] == 72

        cur.execute("PRAGMA table_info(library_artists)")
        cols = {r[1] for r in cur.fetchall()}
        for col in ("artist_type", "member_count", "begin_year", "end_year", "popularity"):
            assert col in cols
    finally:
        db2.close()


def test_pre_baseline_db_is_refused(tmp_path: Path) -> None:
    """Database schema v70 predates the v71 baseline and is refused with RuntimeError."""
    db_file = tmp_path / "pre_baseline.db"
    db = Database(str(db_file))
    with db._lock:
        db.conn.execute("DELETE FROM schema_migrations WHERE version > 70")
        db.conn.execute("INSERT OR REPLACE INTO schema_migrations (version) VALUES (70)")
        db.conn.commit()
    db.close()

    with pytest.raises(RuntimeError, match=r"predates the v71 baseline"):
        Database(str(db_file))


def test_get_artist_details_parses_group_members_and_life_span(test_db: Database) -> None:
    """get_artist_details parses group members count, life span years, and includes artist-rels in inc."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    resp_data = {
        "id": "a1",
        "name": "The Band",
        "type": "Group",
        "life-span": {"begin": "1965-03", "end": "1995"},
        "relations": [
            {
                "type": "member of band",
                "direction": "backward",
                "ended": False,
                "artist": {"id": "m1", "name": "Member 1"},
            },
            {
                "type": "member of band",
                "direction": "backward",
                "ended": False,
                "artist": {"id": "m2", "name": "Member 2"},
            },
            {
                "type": "member of band",
                "direction": "backward",
                "ended": True,
                "artist": {"id": "m3", "name": "Member 3 (ended)"},
            },
            {
                "type": "member of band",
                "direction": "backward",
                "ended": False,
                "artist": {"id": "m1", "name": "Member 1 duplicate"},
            },
            {
                "type": "official homepage",
                "url": {"resource": "https://theband.example.com"},
            },
        ],
    }

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = resp_data

    with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
        res = client.get_artist_details("a1")
        assert res is not None
        assert res["artist_type"] == "group"
        assert res["member_count"] == 2
        assert res["begin_year"] == 1965
        assert res["end_year"] == 1995
        assert res["urls"].get("official homepage") == "https://theband.example.com"

        mock_get.assert_called_once()
        params = mock_get.call_args[1].get("params") or {}
        assert "artist-rels" in params.get("inc", "")


def test_get_artist_details_person_is_solo(test_db: Database) -> None:
    """get_artist_details with type Person returns member_count == 1 and artist_type == person."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    resp_data = {
        "id": "p1",
        "name": "Solo Artist",
        "type": "Person",
        "life-span": {"begin": "1980-05-12", "end": None},
        "relations": [],
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = resp_data

    with patch.object(client._session, "get", return_value=mock_resp):
        res = client.get_artist_details("p1")
        assert res is not None
        assert res["artist_type"] == "person"
        assert res["member_count"] == 1
        assert res["begin_year"] == 1980
        assert res["end_year"] is None


def test_get_artist_details_unknown_type_and_bad_dates(test_db: Database) -> None:
    """get_artist_details handles missing type and unparseable life-span dates gracefully."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    resp_data = {
        "id": "u1",
        "name": "Unknown Artist",
        "life-span": {"begin": "abc"},
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = resp_data

    with patch.object(client._session, "get", return_value=mock_resp):
        res = client.get_artist_details("u1")
        assert res is not None
        assert res["artist_type"] is None
        assert res["member_count"] is None
        assert res["begin_year"] is None


def test_refresh_writes_mb_facts_and_overwrites(test_db: Database) -> None:
    """_apply_mb_artist_details writes and overwrites artist_type, member_count, begin_year, end_year."""
    artist_id = "art-test-facts"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Test Band",
            mbid="mbid-test-facts",
            monitored=True,
        )
    )
    # Set existing member_count to 5
    with test_db._lock:
        test_db.conn.execute("UPDATE library_artists SET member_count = 5 WHERE id = ?", (artist_id,))
        test_db.conn.commit()

    row = test_db.get_library_artist(artist_id)
    assert row["member_count"] == 5

    mock_enricher = MagicMock(spec=MbidEnricherClient)
    mock_enricher.source_available.return_value = True
    mock_enricher.get_artist_details.return_value = {
        "id": "mbid-test-facts",
        "artist_type": "group",
        "member_count": 3,
        "begin_year": 1990,
        "end_year": None,
    }

    mock_discovery = MagicMock(spec=DiscoveryClient)

    st = _RefreshState(
        artist_id=artist_id,
        db=test_db,
        enricher=mock_enricher,
        discovery_client=mock_discovery,
        force=False,
        artist=row,
        artist_name="Test Band",
        metadata_profile=None,
        foreign_artist_id=None,
        mbid="mbid-test-facts",
        source_unavailable=False,
    )
    _apply_mb_artist_details(st)

    updated_row = test_db.get_library_artist(artist_id)
    assert updated_row["artist_type"] == "group"
    assert updated_row["member_count"] == 3
    assert updated_row["begin_year"] == 1990
    assert updated_row["end_year"] is None


def test_refresh_stores_deezer_popularity(test_db: Database) -> None:
    """_sync_deezer_discography stores nb_fan into popularity, and leaves it untouched when nb_fan is 0."""
    artist_id = "art-deezer-pop"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Deezer Band",
            foreign_artist_id="dz-100",
            monitored=True,
        )
    )

    mock_enricher = MagicMock(spec=MbidEnricherClient)
    mock_enricher.source_available.return_value = True
    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.get_artist_details.return_value = {
        "nb_fan": 123456,
        "albums": [],
        "singles_eps": [],
        "compilations": [],
    }

    row = test_db.get_library_artist(artist_id)
    st = _RefreshState(
        artist_id=artist_id,
        db=test_db,
        enricher=mock_enricher,
        discovery_client=mock_discovery,
        force=False,
        artist=row,
        artist_name="Deezer Band",
        metadata_profile=None,
        foreign_artist_id="dz-100",
        mbid=None,
        source_unavailable=False,
    )
    _sync_deezer_discography(st)

    updated_row = test_db.get_library_artist(artist_id)
    assert updated_row["popularity"] == 123456

    # Test with nb_fan == 0 leaving popularity untouched
    artist_id_zero = "art-deezer-zero"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id_zero,
            name="Zero Fan Artist",
            foreign_artist_id="dz-200",
            monitored=True,
        )
    )
    mock_discovery.get_artist_details.return_value = {
        "nb_fan": 0,
        "albums": [],
        "singles_eps": [],
        "compilations": [],
    }
    row_zero = test_db.get_library_artist(artist_id_zero)
    st_zero = _RefreshState(
        artist_id=artist_id_zero,
        db=test_db,
        enricher=mock_enricher,
        discovery_client=mock_discovery,
        force=False,
        artist=row_zero,
        artist_name="Zero Fan Artist",
        metadata_profile=None,
        foreign_artist_id="dz-200",
        mbid=None,
        source_unavailable=False,
    )
    _sync_deezer_discography(st_zero)

    updated_row_zero = test_db.get_library_artist(artist_id_zero)
    assert updated_row_zero["popularity"] is None


def test_artist_record_exposes_new_fields(test_db: Database) -> None:
    """LibraryArtistRecord exposes and round-trips the five new metadata fields."""
    artist_id = "art-record-test"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Record Band",
            country="US",
        )
    )
    with test_db._lock:
        test_db.conn.execute(
            """
            UPDATE library_artists
            SET artist_type = ?, member_count = ?, begin_year = ?, end_year = ?, popularity = ?
            WHERE id = ?
            """,
            ("group", 4, 1980, 2010, 500000, artist_id),
        )
        test_db.conn.commit()

    row = test_db.conn.execute("SELECT * FROM library_artists WHERE id = ?", (artist_id,)).fetchone()
    mapped = test_db._map_library_artist(row)
    rec = LibraryArtistRecord.model_validate(mapped)
    assert rec.artist_type == "group"
    assert rec.member_count == 4
    assert rec.begin_year == 1980
    assert rec.end_year == 2010
    assert rec.popularity == 500000

    # Test None round-trip
    artist_id_empty = "art-record-empty"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id_empty,
            name="Empty Record Band",
        )
    )
    row_empty = test_db.conn.execute("SELECT * FROM library_artists WHERE id = ?", (artist_id_empty,)).fetchone()
    mapped_empty = test_db._map_library_artist(row_empty)
    rec_empty = LibraryArtistRecord.model_validate(mapped_empty)
    assert rec_empty.artist_type is None
    assert rec_empty.member_count is None
    assert rec_empty.begin_year is None
    assert rec_empty.end_year is None
    assert rec_empty.popularity is None
