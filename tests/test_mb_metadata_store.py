"""Comprehensive unit tests for persistent MusicBrainz metadata store, redirects, and enricher paging."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.mb_metadata_store import (
    MbMetadataStore,
    get_shared_enricher,
    reset_shared_enricher,
)
from trackseerr.storage import SCHEMA_VERSION, Database

pytestmark = pytest.mark.real_mbid_enricher


@pytest.fixture
def test_db(tmp_path: Path):
    """Isolated disk-backed database for testing."""
    db_file = tmp_path / "test_mb_store.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _reset_enricher_fixture():
    """Ensures module singleton enricher is reset before and after each test."""
    reset_shared_enricher()
    yield
    reset_shared_enricher()


def test_migration_creates_tables_and_version(test_db: Database) -> None:
    """Fresh Database reaches SCHEMA_VERSION 72 and creates mb cache and redirect tables and indexes."""
    assert SCHEMA_VERSION == 72
    cur = test_db.conn.cursor()
    cur.execute("SELECT MAX(version) FROM schema_migrations")
    assert cur.fetchone()[0] == 72

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert "mb_metadata_cache" in tables
    assert "mb_id_redirects" in tables

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {r[0] for r in cur.fetchall()}
    assert "idx_mb_cache_expires" in indexes
    assert "idx_mb_cache_kind" in indexes
    assert "idx_mb_redirects_new" in indexes


def test_store_roundtrip_and_expiry(test_db: Database) -> None:
    """Store put/get succeeds when unexpired; ttl 0 or past timestamp is a miss."""
    store = MbMetadataStore(test_db)

    # Valid entry
    store.put("key1", "artist_lookup", {"artist": "Radiohead"}, ttl_seconds=3600.0)
    hit, data = store.get("key1")
    assert hit is True
    assert data == {"artist": "Radiohead"}

    # Entry with TTL 0
    store.put("key2", "artist_lookup", {"artist": "Expired"}, ttl_seconds=0.0)
    hit, data = store.get("key2")
    assert hit is False
    assert data is None

    # Entry with explicit past expires_at
    test_db.conn.execute(
        """INSERT INTO mb_metadata_cache (cache_key, kind, payload_json, fetched_at, expires_at)
           VALUES ('key3', 'test', '{"past": true}', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"""
    )
    test_db.conn.commit()
    hit, data = store.get("key3")
    assert hit is False
    assert data is None

    # Nonexistent key
    hit, data = store.get("nonexistent")
    assert hit is False
    assert data is None


def test_prune_expired_only_removes_expired(test_db: Database) -> None:
    """prune_expired removes rows where expires_at <= now, preserving unexpired ones."""
    store = MbMetadataStore(test_db)

    store.put("active", "kind_a", {"status": "ok"}, ttl_seconds=3600.0)
    store.put("expired1", "kind_a", {"status": "dead"}, ttl_seconds=0.0)
    test_db.conn.execute(
        """INSERT INTO mb_metadata_cache (cache_key, kind, payload_json, fetched_at, expires_at)
           VALUES ('expired2', 'kind_b', '{"dead": true}', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"""
    )
    test_db.conn.commit()

    pruned = store.prune_expired()
    assert pruned == 2

    # Active item is still readable
    hit, data = store.get("active")
    assert hit is True
    assert data == {"status": "ok"}

    # Expired rows are deleted
    row1 = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'expired1'"
    ).fetchone()
    assert row1 is None
    row2 = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'expired2'"
    ).fetchone()
    assert row2 is None


def test_redirect_chain_and_cycle(test_db: Database) -> None:
    """Redirect resolution traverses up to 5 hops, stops on cycles, and leaves unredirected input intact."""
    store = MbMetadataStore(test_db)

    # Chain: a -> b -> c
    store.record_redirect("id-a", "id-b", "artist")
    store.record_redirect("id-b", "id-c", "artist")
    assert store.resolve_redirect("id-a") == "id-c"
    assert store.resolve_redirect("id-b") == "id-c"
    assert store.resolve_redirect("id-c") == "id-c"
    assert store.resolve_redirect("id-unrelated") == "id-unrelated"

    # Cycle: cyc-a -> cyc-b -> cyc-a terminates
    store.record_redirect("cyc-a", "cyc-b", "artist")
    store.record_redirect("cyc-b", "cyc-a", "artist")
    res = store.resolve_redirect("cyc-a")
    assert res in ("cyc-a", "cyc-b")

    # Ignore when old == new
    store.record_redirect("same", "same", "artist")
    row = test_db.conn.execute("SELECT 1 FROM mb_id_redirects WHERE old_id = 'same'").fetchone()
    assert row is None


def test_rg_tracks_persist_across_instances(test_db: Database) -> None:
    """enricher A fetches release group tracks making 1 HTTP call; new enricher B on same db needs 0 calls."""
    store = MbMetadataStore(test_db)
    enricher_a = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "releases": [
            {
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "Airbag",
                                "length": 284000,
                                "recording": {"id": "rec-airbag"},
                            }
                        ],
                    }
                ]
            }
        ]
    }

    with patch.object(enricher_a._session, "get", return_value=mock_resp) as mock_get_a:
        tracks_a = enricher_a.get_release_group_tracks("rg-ok-computer")
        assert len(tracks_a) == 1
        assert tracks_a[0]["title"] == "Airbag"
        assert mock_get_a.call_count == 1

    # New enricher instance sharing the database
    enricher_b = MbidEnricherClient(
        base_url="https://api.brainzmash.org", store=MbMetadataStore(test_db)
    )
    with patch.object(enricher_b._session, "get") as mock_get_b:
        tracks_b = enricher_b.get_release_group_tracks("rg-ok-computer")
        assert tracks_b == tracks_a
        assert mock_get_b.call_count == 0


def test_failure_not_persisted(test_db: Database) -> None:
    """503, requests.Timeout, and non-JSON body are not written to L2 store; subsequent call retries."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    # 1. 503 Service Unavailable
    r503 = MagicMock(status_code=503)
    with patch.object(client._session, "get", return_value=r503):
        assert client.get_artist_details("art-503") is None
    row503 = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'artist_details:art-503'"
    ).fetchone()
    assert row503 is None

    # 2. Timeout
    with patch.object(client._session, "get", side_effect=requests.Timeout("Connection timed out")):
        assert client.lookup_album_mbids("Art", "Alb") is None
    row_to = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'album:art:alb'"
    ).fetchone()
    assert row_to is None

    # 3. Non-JSON body
    r_bad_json = MagicMock(status_code=200)
    r_bad_json.json.side_effect = ValueError("Invalid JSON response")
    with patch.object(client._session, "get", return_value=r_bad_json):
        assert client.lookup_artist_mbid("Bad JSON Artist") is None
    row_bj = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'artist:bad json artist'"
    ).fetchone()
    assert row_bj is None

    # Subsequent fresh client retries and succeeds
    client_fresh = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)
    r_ok = MagicMock(status_code=200)
    r_ok.json.return_value = {"artists": [{"id": "art-success", "name": "Bad JSON Artist"}]}
    with patch.object(client_fresh._session, "get", return_value=r_ok) as mock_retry:
        mbid = client_fresh.lookup_artist_mbid("Bad JSON Artist")
        assert mbid == "art-success"
        assert mock_retry.call_count == 1


def test_404_negative_persisted_with_short_ttl(test_db: Database) -> None:
    """HTTP 404 response persists as negative cache row with ~1-day TTL."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    r404 = MagicMock(status_code=404)
    with patch.object(client._session, "get", return_value=r404):
        assert client.lookup_artist_mbid("Ghost Band") is None

    row = test_db.conn.execute(
        "SELECT * FROM mb_metadata_cache WHERE cache_key = 'artist:ghost band'"
    ).fetchone()
    assert row is not None
    assert row["payload_json"] == "null"
    exp = datetime.fromisoformat(row["expires_at"])
    fetch = datetime.fromisoformat(row["fetched_at"])
    diff = (exp - fetch).total_seconds()
    # TTL_NEGATIVE is 86400 (1 day)
    assert 86300 <= diff <= 86500


def test_force_bypasses_cache_and_overwrites(test_db: Database) -> None:
    """force=True ignores cached entry, issues HTTP request, and overwrites stored data."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    r1 = MagicMock(status_code=200)
    r1.json.return_value = {"id": "art-force", "name": "Initial Name", "country": "US"}
    with patch.object(client._session, "get", return_value=r1):
        res1 = client.get_artist_details("art-force")
        assert res1 is not None
        assert res1["name"] == "Initial Name"

    # Ordinary call gets cached version without network
    with patch.object(client._session, "get") as mock_get_idle:
        res_cached = client.get_artist_details("art-force")
        assert res_cached is not None
        assert res_cached["name"] == "Initial Name"
        assert mock_get_idle.call_count == 0

    # force=True fetches new data and updates store
    r2 = MagicMock(status_code=200)
    r2.json.return_value = {"id": "art-force", "name": "Updated Name", "country": "US"}
    with patch.object(client._session, "get", return_value=r2) as mock_get_force:
        res_forced = client.get_artist_details("art-force", force=True)
        assert res_forced is not None
        assert res_forced["name"] == "Updated Name"
        assert mock_get_force.call_count == 1

    # Verify L2 updated
    row = test_db.conn.execute(
        "SELECT payload_json FROM mb_metadata_cache WHERE cache_key = 'artist_details:v2:art-force'"
    ).fetchone()
    assert json.loads(row["payload_json"])["name"] == "Updated Name"


def test_discography_pages_until_count(test_db: Database) -> None:
    """Discography fetches all pages when release-group-count=250 over 3 pages with offsets 0/100/200."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    page1 = MagicMock(status_code=200)
    page1.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(100)],
        "release-group-count": 250,
    }
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(100, 200)],
        "release-group-count": 250,
    }
    page3 = MagicMock(status_code=200)
    page3.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(200, 250)],
        "release-group-count": 250,
    }

    with patch.object(client._session, "get", side_effect=[page1, page2, page3]) as mock_get:
        items, complete = client.get_artist_discography_result("art-big", limit=100)
        assert len(items) == 250
        assert complete is True
        assert mock_get.call_count == 3
        offsets = [call.kwargs["params"]["offset"] for call in mock_get.call_args_list]
        assert offsets == [0, 100, 200]

    # Verify cached in L2 store
    row = test_db.conn.execute(
        "SELECT payload_json FROM mb_metadata_cache WHERE cache_key = 'discography:art-big'"
    ).fetchone()
    assert row is not None
    assert len(json.loads(row["payload_json"])) == 250


def test_discography_partial_failure_not_cached(test_db: Database) -> None:
    """When page 2 returns 503, returns partial items, complete=False, and nothing is persisted to L2."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    page1 = MagicMock(status_code=200)
    page1.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(100)],
        "release-group-count": 250,
    }
    page2_fail = MagicMock(status_code=503)

    with patch.object(client._session, "get", side_effect=[page1, page2_fail]):
        items, complete = client.get_artist_discography_result("art-partial", limit=100)
        assert len(items) == 100
        assert complete is False

    row = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'discography:art-partial'"
    ).fetchone()
    assert row is None

    # Next call retries from offset 0
    page2_ok = MagicMock(status_code=200)
    page2_ok.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(100, 200)],
        "release-group-count": 200,
    }
    with patch.object(client._session, "get", side_effect=[page1, page2_ok]) as mock_retry:
        items_full, complete_full = client.get_artist_discography_result("art-partial", limit=100)
        assert len(items_full) == 200
        assert complete_full is True
        assert mock_retry.call_count == 2


def test_discography_page_cap(test_db: Database) -> None:
    """Large count exceeding 25 pages caps at 25 calls and returns complete=False."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    mock_page = MagicMock(status_code=200)
    mock_page.json.return_value = {
        "release-groups": [{"id": f"rg-{i}", "title": f"Album {i}"} for i in range(10)],
        "release-group-count": 100000,
    }

    with patch.object(client._session, "get", return_value=mock_page) as mock_get:
        items, complete = client.get_artist_discography_result("art-huge", limit=10)
        assert mock_get.call_count == 25
        assert complete is False
        assert len(items) == 250

    row = test_db.conn.execute(
        "SELECT 1 FROM mb_metadata_cache WHERE cache_key = 'discography:art-huge'"
    ).fetchone()
    assert row is None


def test_artist_details_records_redirect(test_db: Database) -> None:
    """When API response id differs from requested id, records redirect and future lookups resolve to new id."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "id": "new-artist-uuid",
        "name": "Canonical Band",
        "country": "UK",
    }

    with patch.object(client._session, "get", return_value=resp):
        details = client.get_artist_details("old-artist-uuid")
        assert details is not None
        assert details["name"] == "Canonical Band"

    # Redirect record in DB
    row = test_db.conn.execute(
        "SELECT * FROM mb_id_redirects WHERE old_id = 'old-artist-uuid'"
    ).fetchone()
    assert row is not None
    assert row["new_id"] == "new-artist-uuid"
    assert row["entity_type"] == "artist"

    # Future lookup on old ID resolves to new ID
    new_client = MbidEnricherClient(
        base_url="https://api.brainzmash.org", store=MbMetadataStore(test_db)
    )
    with patch.object(new_client._session, "get") as mock_get:
        details_cached = new_client.get_artist_details("old-artist-uuid")
        assert details_cached is not None
        assert details_cached["name"] == "Canonical Band"
        assert mock_get.call_count == 0


def test_resolve_release_group(test_db: Database) -> None:
    """resolve_release_group records redirect if response id differs; 404 returns None and persists negative."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)

    # 1. Canonical ID differs
    r_diff = MagicMock(status_code=200)
    r_diff.json.return_value = {"id": "rg-canonical-99"}
    with patch.object(client._session, "get", return_value=r_diff):
        canon = client.resolve_release_group("rg-old-11")
        assert canon == "rg-canonical-99"

    row = test_db.conn.execute(
        "SELECT * FROM mb_id_redirects WHERE old_id = 'rg-old-11'"
    ).fetchone()
    assert row is not None
    assert row["new_id"] == "rg-canonical-99"
    assert row["entity_type"] == "release_group"

    # 2. 404
    r_404 = MagicMock(status_code=404)
    with patch.object(client._session, "get", return_value=r_404):
        res = client.resolve_release_group("rg-missing")
        assert res is None

    row_neg = test_db.conn.execute(
        "SELECT * FROM mb_metadata_cache WHERE cache_key = 'rg_lookup:rg-missing'"
    ).fetchone()
    assert row_neg is not None
    assert row_neg["payload_json"] == "null"


def test_stats_counts_requests_and_hits(test_db: Database) -> None:
    """Enricher client tracks network requests and cache hits across L1 and L2."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", store=store)
    expected_breakers = {"api.brainzmash.org": "closed", "musicbrainz.org": "closed"}
    assert client.stats() == {"network_requests": 0, "cache_hits": 0, "breaker_state": expected_breakers}

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"artists": [{"id": "art-1", "name": "Coldplay"}]}

    with patch.object(client._session, "get", return_value=resp):
        res1 = client.lookup_artist_mbid("Coldplay")
        assert res1 == "art-1"
        assert client.stats() == {"network_requests": 1, "cache_hits": 0, "breaker_state": expected_breakers}

    # L1 cache hit
    res2 = client.lookup_artist_mbid("Coldplay")
    assert res2 == "art-1"
    assert client.stats() == {"network_requests": 1, "cache_hits": 1, "breaker_state": expected_breakers}

    # Clear L1 to force L2 store hit
    client._cache.clear()
    res3 = client.lookup_artist_mbid("Coldplay")
    assert res3 == "art-1"
    assert client.stats() == {"network_requests": 1, "cache_hits": 2, "breaker_state": expected_breakers}

    # Verify store.stats() groups by kind
    assert store.stats() == {"artist_lookup": 1}


def test_shared_enricher_singleton_and_rebuild(test_db: Database, tmp_path: Path) -> None:
    """get_shared_enricher returns singleton, honors mb_mirror_url, and rebuilds when settings or db change."""
    test_db.update_media_management_settings({"mb_mirror_url": "https://mirror1.example.org"})

    enricher1 = get_shared_enricher(test_db)
    assert enricher1.base_url == "https://mirror1.example.org"

    # Same instance returned on subsequent call
    enricher2 = get_shared_enricher(test_db)
    assert enricher1 is enricher2

    # Setting changes -> rebuilt
    test_db.update_media_management_settings({"mb_mirror_url": "https://mirror2.example.org"})
    enricher3 = get_shared_enricher(test_db)
    assert enricher3 is not enricher1
    assert enricher3.base_url == "https://mirror2.example.org"

    # Different database object -> rebuilt
    db2 = Database(str(tmp_path / "second.db"))
    try:
        enricher4 = get_shared_enricher(db2)
        assert enricher4 is not enricher3
    finally:
        db2.close()


def test_no_direct_construction():
    """Production code builds metadata clients only via mb_metadata_store (and the client modules themselves)."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "trackseerr"
    allowed = {"mbid_enricher.py", "discovery.py", "mb_metadata_store.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "MbidEnricherClient(" in text or "DiscoveryClient(" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
