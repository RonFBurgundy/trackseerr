"""MusicBrainz enrichment of a scanned Discovery library (live network, <=1 req/s, skipped if unreachable).

Endpoint: ``TRACKSEERR_MB_URL`` (default https://musicbrainz.org). The in-app default mirror
(api.brainzmash.cc) is deliberately not used so the tests do not depend on a third-party mirror.
Cover Art Archive is never contacted (mediacover is stubbed). Tags are normalised on the copies so the
artist-split bug in test_scan.py does not mask enrichment behaviour.
"""

from __future__ import annotations

import os
import re
import time
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient

from .conftest import copy_media, run_scan, set_id3, snapshot

pytestmark = [pytest.mark.local_media, pytest.mark.network]

MB_URL = os.environ.get("TRACKSEERR_MB_URL", "https://musicbrainz.org")
DAFT_PUNK_MBID = "056e4f3e-d505-4dad-8ec1-d04f521cbb56"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SET = [
    "01 One More Time.mp3",
    "01 One More Time 2.mp3",
    "02 Aerodynamic.mp3",
    "06 Night Vision.mp3",
    "06 Nightvision.mp3",
    "13 Face to Face.mp3",
]


@pytest.fixture(scope="module")
def mb() -> MbidEnricherClient:
    client = MbidEnricherClient(base_url=MB_URL, timeout=20.0, min_interval=1.1)
    for attempt in range(4):
        try:
            resp = client._request(f"{client.base_url}/ws/2/artist", params={"query": "artist:Daft Punk", "fmt": "json", "limit": 1})
        except Exception as exc:  # pragma: no cover - network dependent
            pytest.skip(f"MusicBrainz unreachable: {exc}")
        if resp is not None and resp.status_code == 200:
            return client
        time.sleep(2 * (attempt + 1))
    pytest.skip("MusicBrainz unavailable (non-200 after retries)")


@pytest.fixture(scope="module")
def discography(mb):
    """Primes the client's cache with the artist MBID, discography and Discovery tracklist.

    MusicBrainz intermittently answers 503 and the client caches a failed lookup as an empty result, so each
    attempt starts from a clean cache; the module skips if the data never arrives.
    """
    for attempt in range(5):
        mb._cache.clear()
        mbid = mb.lookup_artist_mbid("Daft Punk")
        rgs = mb.get_artist_discography(mbid, limit=100) if mbid else []
        rg = next((r for r in rgs if r["title"] == "Discovery" and r["album_type"] == "album"), None)
        tracks = mb.get_release_group_tracks(rg["id"]) if rg else []
        if mbid and rgs and tracks:
            return rgs
        time.sleep(3 * (attempt + 1))
    pytest.skip("MusicBrainz did not return artist/discography/tracklist (busy or rate limited)")


def test_artist_name_resolves_to_daft_punk_mbid(mb, discography):
    assert mb.lookup_artist_mbid("Daft Punk") == DAFT_PUNK_MBID


def test_discography_contains_discovery_release_group(discography):
    discovery = [rg for rg in discography if rg["title"] == "Discovery" and rg["album_type"] == "album"]
    assert len(discovery) == 1
    assert UUID_RE.match(discovery[0]["id"]) and discovery[0]["year"] == 2001


def test_release_group_tracklist_has_14_tracks(mb, discography):
    rg = next(r for r in discography if r["title"] == "Discovery")
    tracks = mb.get_release_group_tracks(rg["id"])
    assert [t["track_number"] for t in tracks] == list(range(1, 15))
    titles = {t["title"] for t in tracks}
    assert {"One More Time", "Aerodynamic", "Face to Face", "Too Long"} <= titles
    assert all(UUID_RE.match(t["mb_recording_id"] or "") for t in tracks)


@pytest.fixture(scope="module")
def refreshed(mb, discography, tmp_path_factory, discovery_src):
    """One scan + one real refresh_single_artist run shared by the assertions below."""
    from plex_playlist_sync.artist_refresh import refresh_single_artist
    from plex_playlist_sync.storage import Database

    base = tmp_path_factory.mktemp("mb")
    root = base / "music"
    for p in copy_media(discovery_src, SET, root / "Daft Punk" / "Discovery"):
        set_id3(p, TPE1="Daft Punk")
    db = Database(str(base / "mb.db"))
    run_scan(db, root)
    (artist,) = db.list_library_artists()
    with patch("plex_playlist_sync.artist_refresh.mediacover_service") as cover:
        cover.ensure_artwork.return_value = None
        result = refresh_single_artist(artist["id"], db, discovery_client=MagicMock(), enricher=mb)
    yield db, artist["id"], result
    db.close()


def test_refresh_resolves_artist_and_release_group(refreshed):
    db, artist_id, result = refreshed
    assert result.get("success", True) is not False, result
    assert db.get_library_artist(artist_id)["mbid"] == DAFT_PUNK_MBID
    album = db.get_library_album_by_title(artist_id, "Discovery")
    assert album["mb_release_group_id"] and UUID_RE.match(album["mb_release_group_id"])


def test_refresh_hydrates_full_14_track_list_with_recording_mbids(refreshed):
    db, artist_id, _ = refreshed
    album = db.get_library_album_by_title(artist_id, "Discovery")
    tracks = db.list_library_tracks(album_id=album["id"], limit=100)
    assert sorted(t["track_number"] for t in tracks) == list(range(1, 15))
    assert all(UUID_RE.match(t["mb_recording_id"] or "") for t in tracks)


def test_refresh_keeps_duplicates_and_title_variants_on_one_track(refreshed):
    db, artist_id, _ = refreshed
    snap = snapshot(db)
    album = db.get_library_album_by_title(artist_id, "Discovery")
    by_no = {t["track_number"]: t for t in snap["tracks"] if t["album_id"] == album["id"]}
    assert len(snap["files_by_track"][by_no[1]["id"]]) == 2     # One More Time + ' 2'
    assert len(snap["files_by_track"][by_no[6]["id"]]) == 2     # Night Vision + Nightvision
    assert len(snap["files"]) == len(SET)


def test_refresh_creates_other_discography_albums_unmonitored_under_existing_option(refreshed):
    db, artist_id, _ = refreshed
    others = [a for a in db.list_library_albums(artist_id=artist_id, limit=500) if a["title"] != "Discovery"]
    assert others, "discography should add the rest of Daft Punk's release groups"
    assert not any(a["monitored"] for a in others)
    assert db.get_library_album_by_title(artist_id, "Discovery")["monitored"]


def test_refresh_monitors_only_tracks_with_files_under_existing_option(refreshed):
    db, artist_id, _ = refreshed
    album = db.get_library_album_by_title(artist_id, "Discovery")
    snap = snapshot(db)
    owned = {t_id for t_id, files in snap["files_by_track"].items() if files}
    tracks = db.list_library_tracks(album_id=album["id"], limit=100)
    missing = [t for t in tracks if t["id"] not in owned]
    assert len(missing) == 14 - 4, "owned tracks are 1, 2, 6, 13"
    assert all(t["monitored"] for t in tracks if t["id"] in owned)
    assert not any(t["monitored"] for t in missing)
