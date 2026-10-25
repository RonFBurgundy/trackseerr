"""Tests for quality unavailable notification and tracking."""

from unittest.mock import patch

import pytest

from trackseerr.acquisition_coordinator import (
    AcquisitionCoordinator,
    _to_quality_profile,
)
from trackseerr.item_history import GrabTrigger
from trackseerr.models import (
    AcquisitionSearchResult,
    NotificationEvent,
    QualityProfile,
    QualityProfileItem,
)
from trackseerr.quality_unavailable import (
    disallowed_qualities,
    maybe_notify,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def flac_profile(test_db):
    profile_dict = test_db.get_default_quality_profile()
    assert profile_dict["name"] == "Lossless (FLAC)"
    allowed = {item["quality"] for item in profile_dict["items"] if item.get("allowed")}
    assert "FLAC 16bit" in allowed
    assert "MP3 320" not in allowed
    return _to_quality_profile(profile_dict)


def test_disallowed_qualities_mp3_only(flac_profile):
    """Two [MP3 320] candidates -> ['MP3 320']."""
    c1 = AcquisitionSearchResult(
        download_id="d1",
        title="Daft Punk - Discovery [MP3 320]",
        artist="Daft Punk",
        size_bytes=10 * 1024 * 1024,
    )
    c2 = AcquisitionSearchResult(
        download_id="d2",
        title="Daft Punk - Discovery (320kbps MP3)",
        artist="Daft Punk",
        size_bytes=12 * 1024 * 1024,
    )
    qualities = disallowed_qualities([c1, c2], flac_profile)
    assert qualities == ["MP3 320"]


def test_disallowed_qualities_ignores_other_rejections():
    """A FLAC candidate rejected for size only -> []."""
    profile = QualityProfile(
        id="size-restricted",
        name="Size Restricted FLAC",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=False, weight=800),
        ],
        min_size_mb=10,
        max_size_mb=20,
    )
    # 50 MB FLAC exceeds max_size_mb (rejected with size_above_max)
    c_flac_too_large = AcquisitionSearchResult(
        download_id="d-flac",
        title="Daft Punk - Discovery [FLAC]",
        artist="Daft Punk",
        size_bytes=50 * 1024 * 1024,
    )
    qualities = disallowed_qualities([c_flac_too_large], profile)
    assert qualities == []


def test_no_notice_before_threshold(test_db, flac_profile):
    """4 rung-1 search_log rows for the track -> False, no event, dispatch not called."""
    for _ in range(4):
        test_db.record_search(
            source_name="prowlarr",
            source_type="torznab",
            query_artist="Daft Punk",
            query_title="Voyager",
            rung=1,
            track_id="trk-1",
        )

    candidates = [
        AcquisitionSearchResult(
            download_id="d1",
            title="Daft Punk - Voyager [MP3 320]",
            artist="Daft Punk",
            size_bytes=10 * 1024 * 1024,
        )
    ]

    with patch("trackseerr.quality_unavailable.notification_dispatcher.dispatch") as mock_dispatch:
        notified = maybe_notify(
            test_db,
            candidates=candidates,
            profile=flac_profile,
            profile_name=flac_profile.name,
            artist="Daft Punk",
            title="Voyager",
            track_id="trk-1",
        )
        assert notified is False
        assert test_db.item_event_count("quality_unavailable", track_id="trk-1") == 0
        mock_dispatch.assert_not_called()


def test_notice_after_threshold_once(test_db, flac_profile):
    """5 rows -> True, one quality_unavailable item event, dispatch called once; a second call -> False."""
    for _ in range(5):
        test_db.record_search(
            source_name="prowlarr",
            source_type="torznab",
            query_artist="Daft Punk",
            query_title="Voyager",
            rung=1,
            track_id="trk-2",
        )

    candidates = [
        AcquisitionSearchResult(
            download_id="d1",
            title="Daft Punk - Voyager [MP3 320]",
            artist="Daft Punk",
            size_bytes=10 * 1024 * 1024,
        )
    ]

    with patch("trackseerr.quality_unavailable.notification_dispatcher.dispatch") as mock_dispatch:
        notified = maybe_notify(
            test_db,
            candidates=candidates,
            profile=flac_profile,
            profile_name=flac_profile.name,
            artist="Daft Punk",
            title="Voyager",
            track_id="trk-2",
        )
        assert notified is True
        assert test_db.item_event_count("quality_unavailable", track_id="trk-2") == 1
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args[0][0] == NotificationEvent.QUALITY_UNAVAILABLE
        data = mock_dispatch.call_args[1]["data"]
        assert data["qualities"] == "MP3 320"
        assert data["searches"] == 5

        # Second call: already notified, returns False, no additional dispatch
        notified_again = maybe_notify(
            test_db,
            candidates=candidates,
            profile=flac_profile,
            profile_name=flac_profile.name,
            artist="Daft Punk",
            title="Voyager",
            track_id="trk-2",
        )
        assert notified_again is False
        assert test_db.item_event_count("quality_unavailable", track_id="trk-2") == 1
        assert mock_dispatch.call_count == 1


def test_request_only_item_uses_request_id(test_db, flac_profile):
    """No track/album id, request_id set -> counts and dedupes by request_id."""
    for _ in range(5):
        test_db.record_search(
            source_name="prowlarr",
            source_type="torznab",
            query_artist="Daft Punk",
            query_title="Aerodynamic",
            rung=1,
            request_id="req-123",
        )

    candidates = [
        AcquisitionSearchResult(
            download_id="d1",
            title="Daft Punk - Aerodynamic [MP3 320]",
            artist="Daft Punk",
            size_bytes=10 * 1024 * 1024,
        )
    ]

    with patch("trackseerr.quality_unavailable.notification_dispatcher.dispatch") as mock_dispatch:
        notified = maybe_notify(
            test_db,
            candidates=candidates,
            profile=flac_profile,
            profile_name=flac_profile.name,
            artist="Daft Punk",
            title="Aerodynamic",
            request_id="req-123",
        )
        assert notified is True
        assert test_db.item_event_count("quality_unavailable", request_id="req-123") == 1
        mock_dispatch.assert_called_once()

        # Dedupe by request_id
        notified_again = maybe_notify(
            test_db,
            candidates=candidates,
            profile=flac_profile,
            profile_name=flac_profile.name,
            artist="Daft Punk",
            title="Aerodynamic",
            request_id="req-123",
        )
        assert notified_again is False
        assert test_db.item_event_count("quality_unavailable", request_id="req-123") == 1
        assert mock_dispatch.call_count == 1


def test_never_grabs(test_db):
    """Patch search_with_fallback to return MP3 candidates and grab_candidate with a Mock;

    _search_and_grab returns success False and grab_candidate is not called.
    """
    coordinator = AcquisitionCoordinator()
    candidates = [
        AcquisitionSearchResult(
            download_id="d1",
            title="Daft Punk - Aerodynamic [MP3 320]",
            artist="Daft Punk",
            size_bytes=10 * 1024 * 1024,
            protocol="torrent",
        )
    ]

    with (
        patch.object(coordinator, "search_with_fallback", return_value=candidates),
        patch.object(coordinator, "grab_candidate") as mock_grab,
    ):
        res = coordinator._search_and_grab(
            artist="Daft Punk",
            title="Aerodynamic",
            album=None,
            item_type="track",
            request_id="req-456",
            db=test_db,
            quality_profile_id=None,
            min_score=None,
            track_id="trk-456",
            album_id=None,
            bypass_delay=True,
            replacement_issue_id=None,
            trigger=GrabTrigger(kind="request"),
        )

    assert res["success"] is False
    mock_grab.assert_not_called()
