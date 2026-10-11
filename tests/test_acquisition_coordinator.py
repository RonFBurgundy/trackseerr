"""Unit and integration tests for Automated Search & Grab Coordinator (Phase 3).

Covers:
- search_all_indexers across Torznab, Newznab, and slskd
- Resilient multi-indexer error isolation
- evaluate_and_rank scoring, filtering, and torrent seeder tiebreakers
- find_client_for_protocol priority resolution
- search_and_grab lifecycle for qBittorrent, SABnzbd, and slskd
- Active downloads recording in database
- Graceful Lidarr trickle fallback on requests API
- Remote path mapping translation and path traversal prevention
- POST /api/missing/{track_id}/grab endpoint
"""

import json
import httpx
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr.clients.acquisition.base import AcquisitionUnavailableError
from trackseerr.item_history import GrabTrigger
from trackseerr.acquisition_coordinator import (
    AcquisitionCoordinator,
    NoClientAvailable,
    acquisition_coordinator,
)
from trackseerr.import_files import translate_remote_path
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_lidarr_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
    QualityProfile,
    QualityProfileItem,
    RequestStatus,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        auto_approve_requests=True,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and standard user."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates FastAPI test client."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user.get("username", "user"),
        is_admin=bool(user.get("is_admin")),
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. search_all_indexers Tests
# ---------------------------------------------------------------------------

def test_search_all_indexers_aggregates_sources(test_db):
    """Verifies search_all_indexers queries Torznab, Newznab, and slskd and aggregates results."""
    # Seed Torznab and Newznab indexers
    test_db.create_indexer(
        IndexerConfig(
            id="idx-torznab",
            name="Prowlarr Torznab",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            api_key="key1",
            enabled=True,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-newznab",
            name="NZBGeek Newznab",
            indexer_type="newznab",
            host_url="http://nzbgeek.info",
            api_key="key2",
            enabled=True,
        )
    )
    # Seed slskd client
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-slskd",
            name="slskd Soulseek",
            driver_type="slskd",
            host_url="http://slskd:5030",
            api_key="slskdkey",
            enabled=True,
        )
    )

    torznab_res = [
        AcquisitionSearchResult(
            download_id="t1",
            title="Radiohead - Karma Police [FLAC]",
            artist="Radiohead",
            item_type="track",
            source="torznab",
            protocol="torrent",
        )
    ]
    newznab_res = [
        AcquisitionSearchResult(
            download_id="n1",
            title="Radiohead - Karma Police (1997) [MP3 320]",
            artist="Radiohead",
            item_type="track",
            source="newznab",
            protocol="usenet",
        )
    ]
    slskd_res = [
        AcquisitionSearchResult(
            download_id="s1",
            title="Radiohead - Karma Police.flac",
            artist="Radiohead",
            item_type="track",
            source="slskd",
            protocol="slskd",
        )
    ]

    mock_torznab_driver = MagicMock()
    mock_torznab_driver.search.return_value = torznab_res

    mock_newznab_driver = MagicMock()
    mock_newznab_driver.search.return_value = newznab_res

    mock_slskd_driver = MagicMock()
    mock_slskd_driver.search.return_value = slskd_res

    def mock_get_indexer_driver(cfg):
        return mock_torznab_driver if cfg.get("indexer_type") == "torznab" else mock_newznab_driver

    def mock_get_acquisition_driver(cfg):
        return mock_slskd_driver

    coordinator = AcquisitionCoordinator()

    with patch(
        "trackseerr.acquisition_coordinator.get_indexer_driver",
        side_effect=mock_get_indexer_driver,
    ), patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver",
        side_effect=mock_get_acquisition_driver,
    ):
        results = coordinator.search_all_indexers("Radiohead", "Karma Police", db=test_db)

    assert len(results) == 3
    titles = [r.title for r in results]
    assert "Radiohead - Karma Police [FLAC]" in titles
    assert "Radiohead - Karma Police (1997) [MP3 320]" in titles
    assert "Radiohead - Karma Police.flac" in titles


def _result(title):
    return AcquisitionSearchResult(
        download_id=title, title=title, artist="Morgan Page", item_type="track", source="torznab", protocol="torrent"
    )


_MP_ARTIST = "Morgan Page, BT, Ned Shepard & Sultan"
_MP_TITLE = "In the Air (Mord Fustang Remix) [feat. Angela McCluskey]"


def test_search_with_fallback_stops_at_first_hit():
    hit = _result("Morgan Page - In The Air (Mord Fustang Remix) FLAC")
    with patch.object(AcquisitionCoordinator, "search_all_indexers", side_effect=[[], [hit]]) as m:
        out = AcquisitionCoordinator().search_with_fallback(_MP_ARTIST, _MP_TITLE, None, MagicMock())
    assert out == [hit]
    assert m.call_count == 2
    assert m.call_args_list[1].kwargs["artist"] == "Morgan Page"


def test_search_with_fallback_filters_irrelevant_on_fallback_rungs():
    wrong = _result("Morgan Page - In The Air (Original Mix)")
    good = _result("Morgan Page - In The Air (Mord Fustang Remix)")
    with patch.object(AcquisitionCoordinator, "search_all_indexers", side_effect=[[], [wrong], [good]]) as m:
        out = AcquisitionCoordinator().search_with_fallback(_MP_ARTIST, _MP_TITLE, None, MagicMock())
    assert out == [good]
    assert m.call_count == 3


def test_search_with_fallback_uses_slskd_path_for_artist():
    slskd = AcquisitionSearchResult(
        download_id="u1", title="In the Air (Mord Fustang Remix)", artist="", item_type="track", source="slskd",
        protocol="soulseek",
        extra={"filename": "Music\\Morgan Page\\Remixes\\03 In the Air (Mord Fustang Remix).flac"},
    )
    with patch.object(AcquisitionCoordinator, "search_all_indexers", side_effect=[[], [slskd]]):
        out = AcquisitionCoordinator().search_with_fallback(_MP_ARTIST, _MP_TITLE, None, MagicMock())
    assert out == [slskd]


def test_search_with_fallback_rung1_unfiltered():
    odd = _result("Totally Unrelated Name")
    with patch.object(AcquisitionCoordinator, "search_all_indexers", return_value=[odd]) as m:
        out = AcquisitionCoordinator().search_with_fallback(_MP_ARTIST, _MP_TITLE, None, MagicMock())
    assert out == [odd]
    assert m.call_count == 1


def test_search_with_fallback_exhausted_returns_empty():
    with patch.object(AcquisitionCoordinator, "search_all_indexers", return_value=[]) as m:
        out = AcquisitionCoordinator().search_with_fallback(_MP_ARTIST, _MP_TITLE, None, MagicMock())
    assert out == []
    assert m.call_count == 3


def test_search_and_grab_uses_fallback(test_db):
    with patch.object(AcquisitionCoordinator, "search_with_fallback", return_value=[]) as fb:
        res = AcquisitionCoordinator().search_and_grab(
            "Morgan Page, BT", "In the Air (feat. X)", db=test_db, trigger=GrabTrigger("request")
        )
    fb.assert_called_once()
    assert fb.call_args.args[:3] == ("Morgan Page, BT", "In the Air (feat. X)", None)
    assert res["success"] is False


def test_search_all_indexers_logs_per_source(test_db, caplog):
    test_db.create_indexer(
        IndexerConfig(
            id="idx-t", name="Prowlarr Torznab", indexer_type="torznab",
            host_url="http://prowlarr:9696", api_key="k", enabled=True,
        )
    )
    driver = MagicMock()
    driver.search.return_value = [_result("Radiohead - Karma Police")]
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=driver):
        with caplog.at_level("INFO", logger="trackseerr.acquisition_coordinator"):
            AcquisitionCoordinator().search_all_indexers("Radiohead", "Karma Police", db=test_db)
    assert any("source=Prowlarr Torznab" in r.getMessage() and "-> 1 results" in r.getMessage() for r in caplog.records)


def test_search_all_indexers_error_isolation(test_db):
    """Ensures a failure on one indexer does not abort searches on other indexers."""
    test_db.create_indexer(
        IndexerConfig(
            id="idx-fail",
            name="Failing Indexer",
            indexer_type="torznab",
            host_url="http://offline-indexer:9696",
            enabled=True,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-healthy",
            name="Healthy Indexer",
            indexer_type="torznab",
            host_url="http://healthy-indexer:9696",
            enabled=True,
        )
    )

    healthy_res = [
        AcquisitionSearchResult(
            download_id="h1",
            title="Daft Punk - One More Time [FLAC]",
            artist="Daft Punk",
            item_type="track",
            source="torznab",
            protocol="torrent",
        )
    ]

    mock_fail = MagicMock()
    mock_fail.search.side_effect = RuntimeError("Connection refused")

    mock_healthy = MagicMock()
    mock_healthy.search.return_value = healthy_res

    def mock_get_indexer(cfg):
        return mock_fail if cfg.get("id") == "idx-fail" else mock_healthy

    coordinator = AcquisitionCoordinator()
    with patch(
        "trackseerr.acquisition_coordinator.get_indexer_driver",
        side_effect=mock_get_indexer,
    ):
        results = coordinator.search_all_indexers("Daft Punk", "One More Time", db=test_db)

    assert len(results) == 1
    assert results[0].download_id == "h1"


def _blocklist_setup(test_db):
    test_db.create_indexer(
        IndexerConfig(
            id="idx-bl", name="BL Indexer", indexer_type="torznab", host_url="http://bl:9696", enabled=True
        )
    )
    test_db.add_to_blocklist(source_title="Bad - Release [FLAC]")
    res = [
        AcquisitionSearchResult(
            download_id="b1", title="Bad - Release [FLAC]", artist="Bad", item_type="album",
            source="torznab", protocol="torrent",
        ),
        AcquisitionSearchResult(
            download_id="g1", title="Good - Release [FLAC]", artist="Good", item_type="album",
            source="torznab", protocol="torrent",
        ),
    ]
    driver = MagicMock()
    driver.search.return_value = res
    return driver


def test_search_all_indexers_drops_blocklisted_by_default(test_db):
    driver = _blocklist_setup(test_db)
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=driver):
        results = AcquisitionCoordinator().search_all_indexers("Bad", "Release", db=test_db)
    assert [r.download_id for r in results] == ["g1"]


def test_search_all_indexers_include_blocklisted_marks_extra(test_db):
    driver = _blocklist_setup(test_db)
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=driver):
        results = AcquisitionCoordinator().search_all_indexers(
            "Bad", "Release", db=test_db, include_blocklisted=True
        )
    by_id = {r.download_id: r for r in results}
    assert set(by_id) == {"b1", "g1"}
    assert by_id["b1"].extra["blocklisted"] is True
    assert not (by_id["g1"].extra or {}).get("blocklisted")


def test_search_all_indexers_collects_errors(test_db):
    test_db.create_indexer(
        IndexerConfig(
            id="idx-x", name="Down Indexer", indexer_type="torznab", host_url="http://down:9696", enabled=True
        )
    )
    driver = MagicMock()
    driver.search.side_effect = RuntimeError("boom")
    errors: list = []
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=driver):
        results = AcquisitionCoordinator().search_all_indexers("A", "B", db=test_db, errors=errors)
    assert results == []
    assert errors == [{"source": "Down Indexer", "error": "boom"}]


# ---------------------------------------------------------------------------
# 2. evaluate_and_rank Tests
# ---------------------------------------------------------------------------

def test_evaluate_and_rank_quality_and_seeder_tiebreaker():
    """Validates ranking order, quality rejection filtering, and torrent seeder tiebreaking."""
    profile = QualityProfile(
        id="p1",
        name="Test Profile",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=800),
            QualityProfileItem(quality="MP3 192", allowed=False, weight=500),
            QualityProfileItem(quality="Unknown", allowed=False, weight=100),
        ],
        preferred_tags=["cd", "web"],
        ignored_tags=["live", "bootleg"],
    )

    cands = [
        # Candidate 1: FLAC 16bit, 5 seeders
        AcquisitionSearchResult(
            download_id="c1",
            title="Pink Floyd - Time (1973) [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=5,
        ),
        # Candidate 2: FLAC 16bit, 50 seeders (tiebreaker should place this above Candidate 1)
        AcquisitionSearchResult(
            download_id="c2",
            title="Pink Floyd - Time [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=50,
        ),
        # Candidate 3: FLAC 24bit (should rank highest overall)
        AcquisitionSearchResult(
            download_id="c3",
            title="Pink Floyd - Time [24bit Hi-Res]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=2,
        ),
        # Candidate 4: Live bootleg (rejected by ignored_tags)
        AcquisitionSearchResult(
            download_id="c4",
            title="Pink Floyd - Time Live Bootleg [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=100,
        ),
        # Candidate 5: Disallowed MP3 192 (rejected by profile.items)
        AcquisitionSearchResult(
            download_id="c5",
            title="Pink Floyd - Time [192kbps]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=20,
        ),
    ]

    coordinator = AcquisitionCoordinator()
    ranked = coordinator.evaluate_and_rank(cands, profile)

    assert len(ranked) == 3
    # First place: FLAC 24bit (c3)
    assert ranked[0][0].download_id == "c3"
    # Second place: FLAC 16bit with 50 seeders (c2)
    assert ranked[1][0].download_id == "c2"
    # Third place: FLAC 16bit with 5 seeders (c1)
    assert ranked[2][0].download_id == "c1"


def _slsk_profile():
    return QualityProfile(
        id="p-slsk",
        name="Slsk",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=800),
        ],
    )


def _slsk(cid, title, free=True, queue=0, speed=1000, length=None):
    extra = {"peer": {"has_free_slot": free, "queue_length": queue, "upload_speed": speed}}
    if length is not None:
        extra["length_seconds"] = length
    return AcquisitionSearchResult(
        download_id=cid, title=title, artist="A", source="slskd", protocol="slskd", extra=extra
    )


_FLAC = "03 Song [FLAC 16bit 44.1kHz]"
_MP3 = "03 Song [MP3 320]"


def _ids(ranked):
    return [c.download_id for c, _ in ranked]


def test_slskd_free_slot_beats_queued_peer_same_quality():
    cands = [
        _slsk("queued", _FLAC, free=False, queue=0, speed=9_000_000),
        _slsk("free", _FLAC, free=True, queue=5, speed=10),
    ]
    ranked = AcquisitionCoordinator().evaluate_and_rank(cands, _slsk_profile())
    assert _ids(ranked) == ["free", "queued"]


def test_slskd_shorter_queue_wins_when_no_free_slots():
    cands = [
        _slsk("long", _FLAC, free=False, queue=40),
        _slsk("short", _FLAC, free=False, queue=2),
    ]
    ranked = AcquisitionCoordinator().evaluate_and_rank(cands, _slsk_profile())
    assert _ids(ranked) == ["short", "long"]


def test_slskd_quality_still_beats_peer():
    cands = [
        _slsk("mp3-free", _MP3, free=True, queue=0),
        _slsk("flac-queued", _FLAC, free=False, queue=99),
    ]
    ranked = AcquisitionCoordinator().evaluate_and_rank(cands, _slsk_profile())
    assert _ids(ranked) == ["flac-queued", "mp3-free"]


def test_non_slskd_ranking_unchanged():
    def tor(cid, seeders):
        return AcquisitionSearchResult(
            download_id=cid, title=_FLAC, artist="A", source="torznab", protocol="torrent", seeders=seeders
        )

    ranked = AcquisitionCoordinator().evaluate_and_rank([tor("few", 3), tor("many", 30)], _slsk_profile())
    assert _ids(ranked) == ["many", "few"]


def test_slskd_length_mismatch_rejected(test_db):
    from trackseerr.decision_engine import DurationInfo

    cands = [_slsk("long", _FLAC, length=420), _slsk("ok", _FLAC, length=214)]
    with patch("trackseerr.acquisition_coordinator.resolve_duration", return_value=DurationInfo(210.0)):
        ranked = AcquisitionCoordinator().evaluate_and_rank(
            cands, _slsk_profile(), db=test_db, track_id="t1", item_type="track"
        )
    assert _ids(ranked) == ["ok"]


def test_length_check_skipped_for_estimated_or_album(test_db):
    from trackseerr.decision_engine import DurationInfo

    cands = [_slsk("long", _FLAC, length=420)]
    with patch(
        "trackseerr.acquisition_coordinator.resolve_duration",
        return_value=DurationInfo(210.0, estimated=True, source="estimate"),
    ):
        est = AcquisitionCoordinator().evaluate_and_rank(
            cands, _slsk_profile(), db=test_db, track_id="t1", item_type="track"
        )
    folder = _folder("long-folder", ["x.flac"], track_count=1, total=420)
    with patch("trackseerr.acquisition_coordinator.resolve_duration", return_value=DurationInfo(210.0)):
        alb = AcquisitionCoordinator().evaluate_and_rank(
            [folder], _slsk_profile(), db=test_db, album_id="a1", item_type="album"
        )
    assert _ids(est) == ["long"]
    assert _ids(alb) == ["long-folder"]


def test_length_check_skipped_without_length(test_db):
    from trackseerr.decision_engine import DurationInfo

    with patch("trackseerr.acquisition_coordinator.resolve_duration", return_value=DurationInfo(210.0)):
        ranked = AcquisitionCoordinator().evaluate_and_rank(
            [_slsk("nolen", _FLAC)], _slsk_profile(), db=test_db, track_id="t1", item_type="track"
        )
    assert _ids(ranked) == ["nolen"]


# --- Soulseek album folders ------------------------------------------------

_TITLES = ["Title One", "Title Two", "Title Three", "Title Four", "Title Five"]


def _folder(cid, names, track_count=None, total=None, title="Album (2011) [FLAC 16bit 44.1kHz]"):
    extra = {
        "directory": f"@@u\\Music\\{cid}",
        "filename": f"@@u\\Music\\{cid}",
        "files": [{"filename": f"@@u\\Music\\{cid}\\{n}", "size": 1000, "length_seconds": 200} for n in names],
        "track_count": len(names) if track_count is None else track_count,
        "total_length_seconds": total,
        "peer": {"has_free_slot": True, "queue_length": 0, "upload_speed": 1000},
    }
    return AcquisitionSearchResult(
        download_id=cid, title=title, artist="A", source="slskd", protocol="slskd",
        item_type="album", size_bytes=5000, extra=extra,
    )


def _good_names(n=5):
    return [f"{i + 1:02d} - {_TITLES[i]}.flac" for i in range(n)]


def _seed_tracks(db, titles=_TITLES, discs=1, album_id="alb"):
    db.upsert_library_artist({"id": "A1", "name": "A1"})
    db.upsert_library_album({"id": album_id, "artist_id": "A1", "title": album_id})
    for i, t in enumerate(titles):
        db.upsert_library_track(
            {"id": f"{album_id}-t{i}", "album_id": album_id, "artist_id": "A1", "title": t,
             "track_number": i + 1, "disc_number": 1 + (i % discs), "duration_seconds": 200.0}
        )


def _rank_album(db, cands):
    return AcquisitionCoordinator().evaluate_and_rank(
        cands, _slsk_profile(), db=db, album_id="alb", item_type="album"
    )


def test_album_request_ignores_slskd_track_files(test_db):
    ranked = _rank_album(test_db, [_slsk("file", _FLAC), _folder("dir", _good_names())])
    assert _ids(ranked) == ["dir"]


def test_track_request_ignores_slskd_folders(test_db):
    ranked = AcquisitionCoordinator().evaluate_and_rank(
        [_slsk("file", _FLAC), _folder("dir", _good_names())], _slsk_profile(), db=test_db, track_id="t1", item_type="track"
    )
    assert _ids(ranked) == ["file"]


def test_non_slskd_album_unaffected(test_db):
    _seed_tracks(test_db)
    tor = AcquisitionSearchResult(
        download_id="tor", title="Album [FLAC 16bit 44.1kHz]", artist="A", source="torznab",
        protocol="torrent", item_type="track", seeders=5,
    )
    assert _ids(_rank_album(test_db, [tor])) == ["tor"]


def test_slskd_folder_track_count_short_rejected(test_db):
    _seed_tracks(test_db)
    assert _rank_album(test_db, [_folder("short", _good_names(4))]) == []


def test_slskd_folder_bonus_tracks_tolerated(test_db):
    _seed_tracks(test_db)
    ok = _folder("bonus2", _good_names() + ["06 - Extra A.flac", "07 - Extra B.flac"])
    too_many = _folder("bonus3", _good_names() + ["06 - Extra A.flac", "07 - Extra B.flac", "08 - Extra C.flac"])
    assert _ids(_rank_album(test_db, [ok, too_many])) == ["bonus2"]


def test_slskd_folder_titles_must_match(test_db):
    _seed_tracks(test_db)
    wrong = _folder("wrong", [f"{i:02d} - Completely Different {i}.flac" for i in range(1, 6)])
    ranked = _rank_album(test_db, [wrong, _folder("right", _good_names())])
    assert _ids(ranked) == ["right"]


def test_slskd_folder_multi_disc_rejected(test_db):
    _seed_tracks(test_db, discs=2)
    assert _rank_album(test_db, [_folder("multi", _good_names())]) == []


def test_slskd_folder_total_length_checked(test_db):
    _seed_tracks(test_db)
    from trackseerr.decision_engine import DurationInfo

    cands = [_folder("long", _good_names(), total=1500), _folder("ok", _good_names(), total=1010)]
    with patch("trackseerr.acquisition_coordinator.resolve_duration", return_value=DurationInfo(1000.0)):
        ranked = _rank_album(test_db, cands)
    assert _ids(ranked) == ["ok"]


def test_slskd_folder_no_library_tracks_accepted(test_db):
    ranked = _rank_album(test_db, [_slsk("file", _FLAC), _folder("dir", ["a.flac"])])
    assert _ids(ranked) == ["dir"]


# ---------------------------------------------------------------------------
# 3. find_client_for_protocol Tests
# ---------------------------------------------------------------------------

def test_find_client_for_protocol(test_db):
    """Validates selecting enabled download client by protocol and priority."""
    # Seed qBittorrent priority 2, SABnzbd priority 1, slskd priority 1
    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-low",
            name="qBittorrent Backup",
            driver_type="qbittorrent",
            host_url="http://qbit-backup:8080",
            priority=2,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-primary",
            name="qBittorrent Primary",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            priority=1,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="sab-primary",
            name="SABnzbd Usenet",
            driver_type="sabnzbd",
            host_url="http://sab:8080",
            priority=1,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="slskd-client",
            name="slskd Client",
            driver_type="slskd",
            host_url="http://slskd:5030",
            priority=1,
            enabled=True,
        )
    )

    coordinator = AcquisitionCoordinator()

    # Torrent protocol -> should pick highest priority qbittorrent (qbit-primary)
    torrent_client = coordinator.find_client_for_protocol("torrent", test_db)
    assert torrent_client is not None
    assert torrent_client["id"] == "qbit-primary"

    # Usenet protocol -> sabnzbd
    usenet_client = coordinator.find_client_for_protocol("usenet", test_db)
    assert usenet_client is not None
    assert usenet_client["id"] == "sab-primary"

    # slskd protocol -> slskd
    slskd_client = coordinator.find_client_for_protocol("slskd", test_db)
    assert slskd_client is not None
    assert slskd_client["id"] == "slskd-client"

    # Unsupported protocol
    none_client = coordinator.find_client_for_protocol("ftp", test_db)
    assert none_client is None


# ---------------------------------------------------------------------------
# 4. search_and_grab Lifecycle Tests
# ---------------------------------------------------------------------------

def test_search_and_grab_qbittorrent(test_db):
    """Tests end-to-end search_and_grab for a torrent candidate with active_download record."""
    # Seed indexer & qBittorrent client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbittorrent:8080",
            enabled=True,
        )
    )

    # Seed user and request to satisfy request_id foreign key constraint
    test_db.upsert_user("admin-1", "admin", "admin@test.com", is_admin=True)
    test_db.create_request(
        MusicRequest(
            id="req-123",
            user_id="admin-1",
            item_type="track",
            title="In Bloom",
            artist="Nirvana",
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="dl-hash-12345",
        title="Nirvana - In Bloom [FLAC]",
        artist="Nirvana",
        item_type="track",
        size_bytes=35 * 1024 * 1024,
        magnet_url="magnet:?xt=urn:btih:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        source="torznab",
        protocol="torrent",
        seeders=42,
    )

    coordinator = AcquisitionCoordinator()

    mock_driver = MagicMock()
    mock_driver.download.return_value = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_driver,
    ):
        result = coordinator.search_and_grab(
            artist="Nirvana",
            title="In Bloom",
            request_id="req-123",
            db=test_db,
            trigger=GrabTrigger("request"),
        )

    assert result["success"] is True
    assert result["download_hash"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert result["client"] == "qBittorrent"
    assert result["release"] == "Nirvana - In Bloom [FLAC]"

    # Verify active_download in database
    dl_id = result["download_id"]
    active_dl = test_db.get_active_download(dl_id)
    assert active_dl is not None
    assert active_dl["request_id"] == "req-123"
    assert active_dl["client_id"] == "client-qbit"
    assert active_dl["status"] == DownloadStatus.QUEUED.value
    assert active_dl["title"] == "Nirvana - In Bloom [FLAC]"
    assert active_dl["size_bytes"] == 35 * 1024 * 1024


def test_search_and_grab_sabnzbd(test_db):
    """Tests search_and_grab dispatching to SABnzbd for usenet releases."""
    test_db.create_indexer(
        IndexerConfig(
            id="idx-nzb",
            name="NZBGeek",
            indexer_type="newznab",
            host_url="http://nzbgeek:5076",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-sab",
            name="SABnzbd",
            driver_type="sabnzbd",
            host_url="http://sabnzbd:8080",
            enabled=True,
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="nzb-100",
        title="Fleetwood Mac - Dreams [FLAC]",
        artist="Fleetwood Mac",
        download_url="http://nzbgeek:5076/getnzb/100.nzb",
        source="newznab",
        protocol="usenet",
        size_bytes=28 * 1024 * 1024,
    )

    coordinator = AcquisitionCoordinator()
    mock_sab = MagicMock()
    mock_sab.download.return_value = "SABnzbd_nzo_abc999"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_sab,
    ):
        res = coordinator.search_and_grab(
            artist="Fleetwood Mac",
            title="Dreams",
            db=test_db,
            trigger=GrabTrigger("request"),
        )

    assert res["success"] is True
    assert res["download_hash"] == "SABnzbd_nzo_abc999"
    assert res["client"] == "SABnzbd"


def test_search_and_grab_slskd(test_db):
    """Tests search_and_grab dispatching to slskd for Soulseek P2P results."""
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-slskd",
            name="slskd",
            driver_type="slskd",
            host_url="http://slskd:5030",
            enabled=True,
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="user1::Music/Song.flac",
        title="Aphex Twin - Xtal [FLAC]",
        artist="Aphex Twin",
        source="slskd",
        protocol="slskd",
        size_bytes=40 * 1024 * 1024,
    )

    coordinator = AcquisitionCoordinator()
    mock_slskd = MagicMock()
    mock_slskd.download.return_value = "user1::Music/Song.flac"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_slskd,
    ):
        res = coordinator.search_and_grab(
            artist="Aphex Twin",
            title="Xtal",
            db=test_db,
            trigger=GrabTrigger("request"),
        )

    assert res["success"] is True
    assert res["download_hash"] == "user1::Music/Song.flac"
    assert res["client"] == "slskd"


def test_search_and_grab_no_acceptable_releases(test_db):
    """Verifies search_and_grab returns failure when all releases are filtered out."""
    candidate_live = AcquisitionSearchResult(
        download_id="live-1",
        title="The Beatles - Hey Jude (Live Bootleg)",
        artist="The Beatles",
        source="torznab",
        protocol="torrent",
    )

    coordinator = AcquisitionCoordinator()
    with patch.object(coordinator, "search_all_indexers", return_value=[candidate_live]):
        res = coordinator.search_and_grab("The Beatles", "Hey Jude", db=test_db, trigger=GrabTrigger("request"))

    assert res["success"] is False
    assert "No acceptable releases found" in res["message"]


def test_search_and_grab_no_client_available(test_db):
    """Verifies graceful failure when no client is enabled for the candidate's protocol."""
    # Usenet candidate, but no SABnzbd client configured
    candidate_nzb = AcquisitionSearchResult(
        download_id="nzb-1",
        title="Led Zeppelin - Kashmir [FLAC]",
        artist="Led Zeppelin",
        source="newznab",
        protocol="usenet",
    )

    coordinator = AcquisitionCoordinator()
    with patch.object(coordinator, "search_all_indexers", return_value=[candidate_nzb]):
        res = coordinator.search_and_grab("Led Zeppelin", "Kashmir", db=test_db, trigger=GrabTrigger("request"))

    assert res["success"] is False
    assert "No enabled download client available for usenet" in res["message"]


# ---------------------------------------------------------------------------
# 5. Remote Path Mapping Tests
# ---------------------------------------------------------------------------

def test_translate_remote_path_scenarios():
    """Tests remote path translation and security traversal defense."""
    mappings = [
        {"remote_path": "/downloads/qbittorrent", "local_path": "/downloads"},
        {"remote_path": "/data/music", "local_path": "/mnt/music"},
    ]

    # Valid prefix match
    translated = translate_remote_path("/downloads/qbittorrent/Artist/Album", mappings)
    assert translated == "/downloads/Artist/Album"

    # Exact directory match without trailing slash
    exact = translate_remote_path("/downloads/qbittorrent", mappings)
    assert exact == "/downloads"

    # Path not matching any mapping
    unmatched = translate_remote_path("/var/lib/other/file.flac", mappings)
    assert unmatched == "/var/lib/other/file.flac"

    # None input
    assert translate_remote_path(None, mappings) is None

    # Path traversal attack defense
    traversal = translate_remote_path("/downloads/qbittorrent/../../etc/passwd", mappings)
    assert traversal is None


# ---------------------------------------------------------------------------
# 6. Requests API Native First with Lidarr Fallback
# ---------------------------------------------------------------------------

def test_requests_api_native_grab_precedence(app_and_client, test_db, test_config, seeded_users):
    """Tests requests API triggering native search_and_grab and skipping Lidarr when successful."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    # Seed native indexer and client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            enabled=True,
        )
    )

    with patch(
        "trackseerr.api.routes.requests.acquisition_coordinator.search_and_grab",
        return_value={"success": True, "download_id": "dl-12345", "download_hash": "hash123"},
    ) as mock_grab, patch(
        "trackseerr.lidarr_queue.lidarr_worker.start_trickle"
    ) as mock_lidarr:
        resp = client.post(
            "/api/requests",
            headers=headers,
            json={
                "item_type": "album",
                "title": "OK Computer",
                "artist": "Radiohead",
            },
        )

    assert resp.status_code == 201
    assert mock_grab.called
    assert not mock_lidarr.called


def test_requests_api_no_lidarr_fallback_when_native_unmatched(app_and_client, test_db, test_config, seeded_users):
    """In native mode an unmatched native search never falls back to Lidarr (the interlock is strict)."""
    app, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    # Seed native indexer and client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            enabled=True,
        )
    )

    mock_lidarr_client = MagicMock()
    app.dependency_overrides[get_lidarr_client] = lambda: mock_lidarr_client

    try:
        with patch(
            "trackseerr.api.routes.requests.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "No acceptable releases found"},
        ) as mock_grab, patch(
            "trackseerr.lidarr_queue.lidarr_worker.start_trickle"
        ) as mock_lidarr:
            resp = client.post(
                "/api/requests",
                headers=headers,
                json={
                    "item_type": "album",
                    "title": "Rare B-Sides",
                    "artist": "Radiohead",
                },
            )

        assert resp.status_code == 201
        assert mock_grab.called
        assert not mock_lidarr.called
    finally:
        app.dependency_overrides.pop(get_lidarr_client, None)


# ---------------------------------------------------------------------------
# 7. POST /api/missing/{track_id}/grab Endpoint Tests
# ---------------------------------------------------------------------------

def test_missing_track_grab_endpoint(app_and_client, test_db, test_config, seeded_users):
    """Tests the admin-only POST /api/missing/{track_id}/grab endpoint."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    alice = seeded_users["alice"]

    # Insert playlist first to satisfy foreign key constraint on missing_tracks
    test_db.upsert_playlist("pl-1", "My Playlist", service="spotify")

    # Insert missing track into DB
    with test_db._lock:
        cur = test_db.conn.execute(
            """
            INSERT INTO missing_tracks (playlist_id, title, artist, album, url, lidarr_status)
            VALUES ('pl-1', 'Creep', 'Radiohead', 'Pablo Honey', '', 'unmonitored')
            """
        )
        test_db.conn.commit()
        track_id = cur.lastrowid

    # 1. Non-admin access should be forbidden (403)
    user_headers = _auth_headers(alice, test_db, test_config)
    resp_user = client.post(f"/api/missing/{track_id}/grab", headers=user_headers)
    assert resp_user.status_code == 403

    # 2. Non-existent track ID should return 404
    admin_headers = _auth_headers(admin, test_db, test_config)
    resp_404 = client.post("/api/missing/999999/grab", headers=admin_headers)
    assert resp_404.status_code == 404

    # 3. Successful admin grab dispatch
    with patch(
        "trackseerr.api.routes.missing.acquisition_coordinator.search_and_grab",
        return_value={
            "success": True,
            "download_id": "dl-missing-1",
            "download_hash": "hash-missing-1",
            "release": "Radiohead - Creep [FLAC]",
            "client": "qBittorrent",
            "score": 950,
        },
    ) as mock_grab:
        resp_admin = client.post(f"/api/missing/{track_id}/grab", headers=admin_headers)

    assert resp_admin.status_code == 200
    data = resp_admin.json()
    assert data["success"] is True
    assert data["download_id"] == "dl-missing-1"
    assert data["client"] == "qBittorrent"
    mock_grab.assert_called_once_with(
        artist="Radiohead",
        title="Creep",
        album="Pablo Honey",
        item_type="track",
        db=test_db,
        trigger=GrabTrigger("playlist", ref="pl-1", label="My Playlist", actor_user_id="admin-1"),
    )


# ---------------------------------------------------------------------------
# 8. Download Client Category & Remote Path Mappings Payload Tests
# ---------------------------------------------------------------------------

def test_download_client_category_and_mappings_payload(app_and_client, test_db, test_config, seeded_users):
    """Tests saving category and remote_path_mappings via download clients API."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    payload = {
        "name": "qBittorrent Mapped",
        "driver_type": "qbittorrent",
        "host_url": "http://qbittorrent:8080",
        "category": "music",
        "remote_path_mappings": [
            {"remote_path": "/downloads/torrents", "local_path": "/local/downloads"}
        ],
    }
    resp = client.post("/api/settings/download-clients", headers=headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] == "music"
    assert len(data["remote_path_mappings"]) == 1
    assert data["remote_path_mappings"][0]["remote_path"] == "/downloads/torrents"
    assert data["remote_path_mappings"][0]["local_path"] == "/local/downloads"

    # Verify stored in DB extra_settings_json
    client_row = test_db.get_download_client(data["id"])
    assert client_row is not None
    extra = json.loads(client_row["extra_settings_json"])
    assert extra["category"] == "music"
    assert extra["remote_path_mappings"] == [
        {"remote_path": "/downloads/torrents", "local_path": "/local/downloads"}
    ]



def test_find_blocklist_entry_matches_hash_guid_title(test_db):
    test_db.add_to_blocklist(source_title="Hash Rel", info_hash="ABCDEF0123", reason="r-hash", indexer="i1")
    test_db.add_to_blocklist(source_title="Guid Rel", release_guid="guid-1", reason="r-guid")
    test_db.add_to_blocklist(source_title="Artist - Title [FLAC]", reason="r-title")
    by_hash = test_db.find_blocklist_entry(info_hash="abcdef0123")
    assert by_hash["reason"] == "r-hash" and by_hash["indexer"] == "i1" and by_hash["created_at"]
    assert test_db.find_blocklist_entry(release_guid="guid-1")["reason"] == "r-guid"
    assert test_db.find_blocklist_entry(release_title="artist - title [flac]")["reason"] == "r-title"
    assert test_db.find_blocklist_entry(release_title="Nope", release_guid="x", info_hash="y") is None
    assert test_db.is_blocklisted(release_guid="guid-1") is True
    assert test_db.is_blocklisted(release_title="Nope") is False


# ---------------------------------------------------------------------------
# Indexer / client priority, round-robin and failover
# ---------------------------------------------------------------------------

def _mk_client(db, cid, driver_type="qbittorrent", priority=1):
    db.create_download_client(
        DownloadClientConfig(
            id=cid, name=cid, driver_type=driver_type, host_url=f"http://{cid}:8080", priority=priority, enabled=True
        )
    )


def _torrent_candidate():
    return AcquisitionSearchResult(
        download_id="t1", title="A - B [FLAC]", artist="A", item_type="track", source="torznab", protocol="torrent",
        magnet_url="magnet:?xt=urn:btih:" + "a" * 40,
    )


def test_search_all_indexers_stamps_indexer_priority(test_db):
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1", name="Pri Indexer", indexer_type="torznab", host_url="http://prowlarr:9696",
            api_key="k", enabled=True, priority=7,
        )
    )
    _mk_client(test_db, "slskd-1", "slskd", priority=3)
    torrent = AcquisitionSearchResult(
        download_id="t1", title="A - B", artist="A", item_type="track", source="torznab", protocol="torrent"
    )
    soulseek = AcquisitionSearchResult(
        download_id="s1", title="A - B.flac", artist="A", item_type="track", source="slskd", protocol="slskd"
    )
    idx_driver, sl_driver = MagicMock(), MagicMock()
    idx_driver.search.return_value = [torrent]
    sl_driver.search.return_value = [soulseek]
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=idx_driver), patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=sl_driver
    ):
        results = AcquisitionCoordinator().search_all_indexers("A", "B", db=test_db)
    by_id = {r.download_id: r for r in results}
    assert by_id["t1"].extra["indexer_priority"] == 7
    assert by_id["s1"].extra["indexer_priority"] == 3
    assert by_id["s1"].extra["indexer_name"] == "slskd-1"


def test_clients_for_protocol_orders_by_priority(test_db):
    _mk_client(test_db, "low", priority=5)
    _mk_client(test_db, "high", priority=1)
    _mk_client(test_db, "mid", priority=3)
    _mk_client(test_db, "sab", "sabnzbd", priority=1)
    ids = [c["id"] for c in AcquisitionCoordinator().clients_for_protocol("torrent", test_db)]
    assert ids == ["high", "mid", "low"]
    assert AcquisitionCoordinator().clients_for_protocol("ftp", test_db) == []


def test_clients_for_protocol_round_robin_equal_priority(test_db):
    _mk_client(test_db, "a", priority=1)
    _mk_client(test_db, "b", priority=1)
    _mk_client(test_db, "c", priority=2)
    coordinator = AcquisitionCoordinator()
    heads = [coordinator.clients_for_protocol("torrent", test_db)[0]["id"] for _ in range(4)]
    assert heads[0] != heads[1] and heads[0] == heads[2] and heads[1] == heads[3]
    assert {heads[0], heads[1]} == {"a", "b"}
    assert coordinator.clients_for_protocol("torrent", test_db)[-1]["id"] == "c"


def test_clients_for_protocol_backed_off_client_goes_last(test_db):
    _mk_client(test_db, "a", priority=1)
    _mk_client(test_db, "b", priority=2)
    coordinator = AcquisitionCoordinator()
    coordinator._mark_client_failed({"id": "a", "name": "a"}, "boom")
    assert [c["id"] for c in coordinator.clients_for_protocol("torrent", test_db)] == ["b", "a"]
    coordinator._client_backoff["a"] = 0.0  # deadline passed
    assert [c["id"] for c in coordinator.clients_for_protocol("torrent", test_db)] == ["a", "b"]


def _drivers(**by_client):
    return lambda cfg: by_client[cfg["id"]]


def test_dispatch_failover_to_next_client(test_db):
    _mk_client(test_db, "first", priority=1)
    _mk_client(test_db, "second", priority=2)
    bad, good = MagicMock(), MagicMock()
    bad.download.side_effect = httpx.ConnectError("refused")
    good.download.return_value = "hash-2"
    coordinator = AcquisitionCoordinator()
    client, dl_hash = coordinator.dispatch_with_failover(test_db, _torrent_candidate(), _drivers(first=bad, second=good))
    assert client["id"] == "second" and dl_hash == "hash-2"
    assert not coordinator._client_available("first")
    # next grab tries the healthy client first
    assert coordinator.clients_for_protocol("torrent", test_db)[0]["id"] == "second"


def test_dispatch_unavailable_does_not_failover(test_db):
    _mk_client(test_db, "first", priority=1)
    _mk_client(test_db, "second", priority=2)
    gone, other = MagicMock(), MagicMock()
    gone.download.side_effect = AcquisitionUnavailableError("gone")
    coordinator = AcquisitionCoordinator()
    with pytest.raises(AcquisitionUnavailableError):
        coordinator.dispatch_with_failover(test_db, _torrent_candidate(), _drivers(first=gone, second=other))
    other.download.assert_not_called()
    assert coordinator._client_available("first")


def test_dispatch_all_fail_returns_last_error(test_db):
    _mk_client(test_db, "first", priority=1)
    _mk_client(test_db, "second", priority=2)
    one, two = MagicMock(), MagicMock()
    one.download.side_effect = httpx.ConnectError("one down")
    two.download.side_effect = RuntimeError("two down")
    coordinator = AcquisitionCoordinator()
    with pytest.raises(RuntimeError, match="two down"):
        coordinator.dispatch_with_failover(test_db, _torrent_candidate(), _drivers(first=one, second=two))
    # grab_candidate reports it as a failed dispatch rather than raising
    with patch("trackseerr.acquisition_coordinator.get_acquisition_driver", side_effect=_drivers(first=one, second=two)):
        res = coordinator.grab_candidate(
            test_db, _torrent_candidate(), parsed_quality="FLAC", score=1, artist="A", album=None, item_type="track",
            request_id=None, track_id=None, album_id=None, trigger=GrabTrigger("request"),
        )
    assert res["success"] is False and "two down" in res["message"]


def test_dispatch_no_client_raises(test_db):
    with pytest.raises(NoClientAvailable):
        AcquisitionCoordinator().dispatch_with_failover(test_db, _torrent_candidate())


# ---------------------------------------------------------------------------- search_log recording


def _seed_two_sources(db):
    db.create_indexer(
        IndexerConfig(id="idx-t", name="Prowlarr Torznab", indexer_type="torznab",
                      host_url="http://prowlarr:9696", api_key="k", enabled=True)
    )
    db.create_download_client(
        DownloadClientConfig(id="cli-s", name="slskd Soulseek", driver_type="slskd",
                             host_url="http://slskd:5030", api_key="k", enabled=True)
    )


def _patched_drivers(torz_search, slsk_search):
    torz, slsk = MagicMock(), MagicMock()
    torz.search.side_effect = torz_search
    slsk.search.side_effect = slsk_search
    return (
        patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=torz),
        patch("trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=slsk),
    )


def test_search_all_indexers_records_each_source_with_context(test_db):
    from trackseerr.acquisition_coordinator import SearchContext

    _seed_two_sources(test_db)
    p1, p2 = _patched_drivers(lambda **kw: [_result("a")], lambda **kw: [_result("b"), _result("c")])
    ctx = SearchContext(trigger_kind="manual", trigger_ref="ref1", trigger_label="lbl", request_id="rq",
                        track_id="tr", album_id="al", rung=2)
    with p1, p2:
        AcquisitionCoordinator().search_all_indexers("Radiohead", "Karma Police", db=test_db, context=ctx)
    rows, total = test_db.list_search_log(1, 50)
    assert total == 2
    by_type = {r["source_type"]: r for r in rows}
    assert by_type["torznab"]["source_name"] == "Prowlarr Torznab" and by_type["torznab"]["result_count"] == 1
    assert by_type["slskd"]["result_count"] == 2
    for r in rows:
        assert (r["trigger_kind"], r["trigger_ref"], r["trigger_label"]) == ("manual", "ref1", "lbl")
        assert (r["request_id"], r["track_id"], r["album_id"], r["rung"]) == ("rq", "tr", "al", 2)
        assert r["query_artist"] == "Radiohead" and r["query_title"] == "Karma Police" and r["error"] is None


def test_search_all_indexers_records_error_row(test_db):
    _seed_two_sources(test_db)

    def boom(**kw):
        raise httpx.ConnectError("connection refused")

    p1, p2 = _patched_drivers(boom, lambda **kw: [])
    with p1, p2:
        AcquisitionCoordinator().search_all_indexers("A", "T", db=test_db)
    rows, _ = test_db.list_search_log(1, 50)
    err = next(r for r in rows if r["source_type"] == "torznab")
    assert err["result_count"] == 0 and "connection refused" in err["error"]
    assert next(r for r in rows if r["source_type"] == "slskd")["error"] is None


def test_search_with_fallback_records_rung_numbers(test_db):
    from trackseerr.acquisition_coordinator import SearchContext

    test_db.create_indexer(
        IndexerConfig(id="idx-t", name="Prowlarr Torznab", indexer_type="torznab",
                      host_url="http://prowlarr:9696", api_key="k", enabled=True)
    )
    driver = MagicMock()
    driver.search.return_value = []
    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=driver):
        AcquisitionCoordinator().search_with_fallback(
            "Radiohead", "Karma Police (Remastered 2009)", "OK Computer (Deluxe)", test_db,
            context=SearchContext(trigger_kind="manual"),
        )
    rows, total = test_db.list_search_log(1, 50)
    rungs = sorted(r["rung"] for r in rows)
    assert total >= 2 and rungs[0] == 1 and rungs == list(range(1, total + 1))
    assert all(r["trigger_kind"] == "manual" for r in rows)


def test_search_recording_failure_does_not_break_search(test_db, caplog):
    import sqlite3

    _seed_two_sources(test_db)
    p1, p2 = _patched_drivers(lambda **kw: [_result("a")], lambda **kw: [_result("b")])
    with p1, p2, patch.object(test_db, "record_search", side_effect=sqlite3.OperationalError("locked")):
        with caplog.at_level("WARNING", logger="trackseerr.acquisition_coordinator"):
            results = AcquisitionCoordinator().search_all_indexers("A", "T", db=test_db)
    assert len(results) == 2
    assert any("Could not record search" in r.getMessage() for r in caplog.records)
