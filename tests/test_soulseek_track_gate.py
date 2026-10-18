"""Tests for Soulseek track relevance gating."""

from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.models import AcquisitionSearchResult


def test_relevant_drops_wrong_soulseek_file_for_track_request():
    coord = AcquisitionCoordinator()
    artist = "Pink Floyd"
    wanted_title = "Time"
    album = "The Dark Side of the Moon"

    wrong_slskd = AcquisitionSearchResult(
        download_id="slskd-1",
        title="03 - All of the Time",
        artist=artist,
        album=album,
        item_type="track",
        protocol="slskd",
        source="slskd",
        extra={"filename": f"{artist}/{album}/03 - All of the Time.flac"},
    )
    correct_slskd = AcquisitionSearchResult(
        download_id="slskd-2",
        title="03 - Time",
        artist=artist,
        album=album,
        item_type="track",
        protocol="slskd",
        source="slskd",
        extra={"filename": f"{artist}/{album}/03 - Time.flac"},
    )
    torrent_result = AcquisitionSearchResult(
        download_id="torrent-1",
        title="Pink Floyd - The Dark Side of the Moon [FLAC]",
        artist=artist,
        album=album,
        item_type="album",
        protocol="torrent",
        source="torznab",
    )

    results = [wrong_slskd, correct_slskd, torrent_result]
    kept = coord._relevant(results, number=1, artist=artist, title=wanted_title, album=album)

    assert kept == [correct_slskd, torrent_result]
