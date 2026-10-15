"""Tests for MusicBrainz release seeding, release linking, and unlinked album queries."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.api.dependencies import get_config, get_db, get_discovery_client, get_mbid_enricher
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.mb_seed import MB_RELEASE_ADD_URL, build_release_seed, link_album_release
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.storage import Database
from tests.test_library_api import _auth_headers, app_and_client, seeded_users, test_config, test_db


@pytest.fixture
def db(tmp_path: Path):
    db_file = tmp_path / "test_mb_seed.db"
    database = Database(str(db_file))
    yield database
    database.close()


def _seed_album(
    db: Database,
    album_id: str,
    *,
    title: str = "Test Album",
    artist_id: str = "artist-1",
    artist_name: str = "Test Artist",
    artist_mbid: str | None = None,
    rg_id: str | None = None,
    release_id: str | None = None,
    foreign_album_id: str | None = None,
    release_date: str | None = "2020-01-01",
    year: int | None = 2020,
    album_type: str = "album",
    tracks: list[dict[str, Any]] | None = None,
    tmp_path: Path | None = None,
) -> None:
    db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name=artist_name,
            mbid=artist_mbid,
        )
    )
    db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id=artist_id,
            title=title,
            mb_release_group_id=rg_id,
            mb_release_id=release_id,
            foreign_album_id=foreign_album_id,
            release_date=release_date,
            year=year,
            album_type=album_type,
            total_tracks=len(tracks) if tracks else 0,
        )
    )
    if tracks:
        for t in tracks:
            tid = f"{album_id}-{t.get('disc', 1)}-{t['num']}"
            db.upsert_library_track(
                LibraryTrack(
                    id=tid,
                    album_id=album_id,
                    artist_id=artist_id,
                    title=t["title"],
                    track_number=t["num"],
                    disc_number=t.get("disc", 1),
                    duration_seconds=t.get("duration"),
                    mb_recording_id=t.get("rec_id"),
                )
            )
            if t.get("file", True):
                flac_path = (tmp_path or Path("/tmp")) / f"{tid}.flac"
                flac_path.write_bytes(b"dummy")
                db.upsert_library_file(
                    LibraryFile(
                        id=f"file-{tid}",
                        track_id=tid,
                        file_path=str(flac_path),
                        relative_path=flac_path.name,
                        codec="FLAC",
                    )
                )


def test_seed_fields_from_catalog_and_deezer(db: Database, tmp_path: Path):
    _seed_album(
        db,
        "alb-deezer",
        title="Deezer Album",
        artist_id="art-1",
        artist_name="Artist One",
        artist_mbid="11111111-1111-1111-1111-111111111111",
        foreign_album_id="deezer:album:302127",
        release_date="2021-04-15",
        year=2021,
        album_type="album",
        tracks=[
            {"num": 1, "disc": 1, "title": "Track 1", "duration": 180.0, "file": True},
            {"num": 1, "disc": 2, "title": "Track 2", "duration": 240.5, "file": True},
        ],
        tmp_path=tmp_path,
    )

    discovery = MagicMock(spec=DiscoveryClient)
    discovery.get_album_details.return_value = {
        "label": "Test Records",
        "upc": "123456789012",
        "record_type": "album",
    }

    seed = build_release_seed(
        db,
        discovery,
        "alb-deezer",
        redirect_uri="https://example.com/callback",
    )

    assert seed["action_url"] == MB_RELEASE_ADD_URL
    fields = seed["fields"]

    expected_fields = [
        ["name", "Deezer Album"],
        ["type", "album"],
        ["status", "official"],
        ["artist_credit.names.0.name", "Artist One"],
        ["artist_credit.names.0.artist.name", "Artist One"],
        ["artist_credit.names.0.mbid", "11111111-1111-1111-1111-111111111111"],
        ["events.0.date.year", "2021"],
        ["events.0.date.month", "4"],
        ["events.0.date.day", "15"],
        ["labels.0.name", "Test Records"],
        ["barcode", "123456789012"],
        ["mediums.0.format", "Digital Media"],
        ["mediums.0.track.0.name", "Track 1"],
        ["mediums.0.track.0.number", "1"],
        ["mediums.0.track.0.length", "180000"],
        ["mediums.1.format", "Digital Media"],
        ["mediums.1.track.0.name", "Track 2"],
        ["mediums.1.track.0.number", "1"],
        ["mediums.1.track.0.length", "240500"],
        ["urls.0.url", "https://www.deezer.com/album/302127"],
        [
            "edit_note",
            "Seeded from TrackSeerr using Deezer and local file metadata. "
            "Please check track titles and lengths before submitting.\n"
            "https://www.deezer.com/album/302127",
        ],
        ["redirect_uri", "https://example.com/callback"],
    ]
    assert fields == expected_fields
    discovery.get_album_details.assert_called_once_with("deezer:album:302127")


def test_seed_uses_release_group_and_recording_mbids_when_known(db: Database, tmp_path: Path):
    _seed_album(
        db,
        "alb-rg",
        title="RG Album",
        rg_id="rg-mbid-999",
        tracks=[
            {"num": 1, "title": "Track One", "duration": 120.0, "rec_id": "rec-111"},
            {"num": 2, "title": "Track Two", "duration": 130.0, "rec_id": None},
        ],
        tmp_path=tmp_path,
    )

    seed = build_release_seed(db, None, "alb-rg")
    fields = dict(seed["fields"])

    assert fields["release_group"] == "rg-mbid-999"
    assert "type" not in fields
    assert fields["mediums.0.track.0.recording"] == "rec-111"
    assert "mediums.0.track.1.recording" not in fields


def test_seed_without_deezer_uses_catalog_only(db: Database, tmp_path: Path):
    _seed_album(
        db,
        "alb-spotify",
        title="Spotify Album",
        foreign_album_id="spotify:album:abc12345",
        tracks=[{"num": 1, "title": "T1", "duration": 100.0}],
        tmp_path=tmp_path,
    )

    discovery = MagicMock(spec=DiscoveryClient)
    seed = build_release_seed(db, discovery, "alb-spotify")

    discovery.get_album_details.assert_not_called()
    field_names = [k for k, _ in seed["fields"]]
    assert "labels.0.name" not in field_names
    assert "barcode" not in field_names
    assert "urls.0.url" not in field_names
    edit_note = dict(seed["fields"])["edit_note"]
    assert "https://www.deezer.com" not in edit_note


def test_seed_album_without_tracks_raises(db: Database):
    _seed_album(db, "alb-empty", tracks=[])
    with pytest.raises(ValueError, match="album has no tracks"):
        build_release_seed(db, None, "alb-empty")


def test_unlinked_list_kinds_and_paging(db: Database, tmp_path: Path):
    # alb-1: has files, no RG, no release
    _seed_album(
        db,
        "alb-1",
        title="Alpha",
        artist_id="ar-1",
        artist_name="Artist A",
        rg_id=None,
        release_id=None,
        tracks=[{"num": 1, "title": "T1", "file": True}],
        tmp_path=tmp_path,
    )
    # alb-2: has files, has RG, no release
    _seed_album(
        db,
        "alb-2",
        title="Beta",
        artist_id="ar-2",
        artist_name="Artist B",
        rg_id="rg-2",
        release_id=None,
        tracks=[{"num": 1, "title": "T1", "file": True}],
        tmp_path=tmp_path,
    )
    # alb-3: has files, has release -> already linked
    _seed_album(
        db,
        "alb-3",
        title="Gamma",
        artist_id="ar-3",
        artist_name="Artist C",
        rg_id="rg-3",
        release_id="rel-3",
        tracks=[{"num": 1, "title": "T1", "file": True}],
        tmp_path=tmp_path,
    )
    # alb-4: no files -> excluded
    _seed_album(
        db,
        "alb-4",
        title="Delta",
        artist_id="ar-4",
        artist_name="Artist D",
        rg_id=None,
        release_id=None,
        tracks=[{"num": 1, "title": "T1", "file": False}],
        tmp_path=tmp_path,
    )

    # Kind: no_release_group -> only alb-1
    items, total = db.list_albums_without_mb_release(page=1, page_size=10, kind="no_release_group")
    assert total == 1
    assert [i["id"] for i in items] == ["alb-1"]
    assert items[0]["track_count"] == 1
    assert items[0]["file_count"] == 1

    # Kind: no_release -> only alb-2
    items, total = db.list_albums_without_mb_release(page=1, page_size=10, kind="no_release_group")
    items_nr, total_nr = db.list_albums_without_mb_release(page=1, page_size=10, kind="no_release")
    assert total_nr == 1
    assert [i["id"] for i in items_nr] == ["alb-2"]

    # Kind: None -> alb-1 and alb-2
    items_all, total_all = db.list_albums_without_mb_release(page=1, page_size=10)
    assert total_all == 2
    assert [i["id"] for i in items_all] == ["alb-1", "alb-2"]

    # Test paging
    p1, tot = db.list_albums_without_mb_release(page=1, page_size=1)
    assert tot == 2
    assert len(p1) == 1
    assert p1[0]["id"] == "alb-1"

    p2, tot = db.list_albums_without_mb_release(page=2, page_size=1)
    assert tot == 2
    assert len(p2) == 1
    assert p2[0]["id"] == "alb-2"


def test_link_album_release_accepts_url_and_sets_release_group(db: Database, tmp_path: Path):
    _seed_album(
        db,
        "alb-link",
        rg_id=None,
        release_id=None,
        tracks=[{"num": 1, "title": "T1", "file": True}],
        tmp_path=tmp_path,
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.lookup_release_group_for_release.return_value = "rg-discovered-456"

    updated = link_album_release(
        db,
        enricher,
        "alb-link",
        "https://musicbrainz.org/release/00000000-0000-0000-0000-000000000001",
    )

    assert updated["mb_release_id"] == "00000000-0000-0000-0000-000000000001"
    assert updated["mb_release_group_id"] == "rg-discovered-456"
    enricher.lookup_release_group_for_release.assert_called_once_with("00000000-0000-0000-0000-000000000001")

    row = db.get_library_album("alb-link")
    assert row["mb_release_id"] == "00000000-0000-0000-0000-000000000001"
    assert row["mb_release_group_id"] == "rg-discovered-456"


def test_link_album_release_rejects_bad_mbid(db: Database):
    _seed_album(db, "alb-bad", tracks=[{"num": 1, "title": "T1"}])
    enricher = MagicMock(spec=MbidEnricherClient)
    with pytest.raises(ValueError, match="Invalid MusicBrainz release ID"):
        link_album_release(db, enricher, "alb-bad", "not-a-valid-uuid")


# =========================================================================
# Route tests
# =========================================================================


def test_mb_routes_admin_and_permissions(app_and_client, test_db, test_config, seeded_users, tmp_path: Path):
    app, client = app_and_client
    admin_h = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice_h = _auth_headers(seeded_users["alice"], test_db, test_config)

    _seed_album(
        test_db,
        "route-alb-1",
        title="Route Album",
        foreign_album_id="deezer:album:112233",
        tracks=[{"num": 1, "title": "Song 1", "duration": 200.0, "file": True}],
        tmp_path=tmp_path,
    )

    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.get_album_details.return_value = {"label": "Route Label", "upc": "999888"}
    mock_enricher = MagicMock(spec=MbidEnricherClient)
    mock_enricher.lookup_release_group_for_release.return_value = "rg-routed-1"

    app.dependency_overrides[get_discovery_client] = lambda: mock_discovery
    app.dependency_overrides[get_mbid_enricher] = lambda: mock_enricher

    # 1. GET /api/library/musicbrainz/unlinked
    res_admin = client.get("/api/library/musicbrainz/unlinked", headers=admin_h)
    assert res_admin.status_code == 200
    body = res_admin.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "route-alb-1"

    res_alice = client.get("/api/library/musicbrainz/unlinked", headers=alice_h)
    assert res_alice.status_code == 403

    # 2. GET /api/library/albums/{album_id}/musicbrainz-seed
    res_seed = client.get("/api/library/albums/route-alb-1/musicbrainz-seed", headers=admin_h)
    assert res_seed.status_code == 200
    seed_body = res_seed.json()
    assert seed_body["action_url"] == MB_RELEASE_ADD_URL
    assert len(seed_body["fields"]) > 0

    res_seed_alice = client.get("/api/library/albums/route-alb-1/musicbrainz-seed", headers=alice_h)
    assert res_seed_alice.status_code == 403

    res_seed_404 = client.get("/api/library/albums/nonexistent-alb/musicbrainz-seed", headers=admin_h)
    assert res_seed_404.status_code == 404

    # 3. POST /api/library/albums/{album_id}/musicbrainz-link
    mbid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    res_link = client.post(
        "/api/library/albums/route-alb-1/musicbrainz-link",
        json={"release_mbid": f"https://musicbrainz.org/release/{mbid}"},
        headers=admin_h,
    )
    assert res_link.status_code == 200
    link_body = res_link.json()
    assert link_body["mb_release_id"] == mbid

    res_link_alice = client.post(
        "/api/library/albums/route-alb-1/musicbrainz-link",
        json={"release_mbid": mbid},
        headers=alice_h,
    )
    assert res_link_alice.status_code == 403

    res_link_422 = client.post(
        "/api/library/albums/route-alb-1/musicbrainz-link",
        json={"release_mbid": "invalid-mbid-format"},
        headers=admin_h,
    )
    assert res_link_422.status_code == 422
