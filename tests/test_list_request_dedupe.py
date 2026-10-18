"""Tests for list and playlist request deduplication and library matching."""

from pathlib import Path
from unittest.mock import patch

import pytest

from trackseerr.config import Config
from trackseerr.item_history import GrabTrigger
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    RequestStatus,
)
from trackseerr.request_submission import RequestRejected, submit_track_request
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "dedupe.db"))
    d.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    d.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path: Path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="token",
        data_dir=str(tmp_path),
        auto_approve_requests=True,
    )


def test_system_trigger_rejects_duplicate_active_request_for_admin(db: Database, cfg: Config):
    """Admin user with playlist GrabTrigger: first submit succeeds; second identical submit raises RequestRejected('duplicate'); no second grab."""
    admin = db.get_user("admin-1")
    trigger = GrabTrigger(kind="playlist", ref="pl-1", label="Test Playlist")

    with patch("trackseerr.request_submission.native_is_configured", return_value=True), patch(
        "trackseerr.request_submission.acquisition_coordinator.search_and_grab"
    ) as mock_grab:
        sub1 = submit_track_request(
            db,
            cfg,
            admin,
            title="Song A",
            artist="Artist X",
            source="list",
            trigger=trigger,
        )
        assert sub1.request["status"] == RequestStatus.PROCESSING
        assert mock_grab.call_count == 1

        with pytest.raises(RequestRejected) as exc_info:
            submit_track_request(
                db,
                cfg,
                admin,
                title="Song A",
                artist="Artist X",
                source="list",
                trigger=trigger,
            )
        assert exc_info.value.code == "duplicate"
        assert exc_info.value.status_code == 409
        assert mock_grab.call_count == 1


def test_admin_manual_request_without_trigger_unchanged(db: Database, cfg: Config):
    """Admin user without trigger: duplicate active requests are allowed (current behaviour)."""
    admin = db.get_user("admin-1")

    with patch("trackseerr.request_submission.native_is_configured", return_value=True), patch(
        "trackseerr.request_submission.acquisition_coordinator.search_and_grab"
    ) as mock_grab:
        sub1 = submit_track_request(
            db,
            cfg,
            admin,
            title="Song A",
            artist="Artist X",
            source="api",
            trigger=None,
        )
        assert sub1.request["status"] == RequestStatus.PROCESSING

        sub2 = submit_track_request(
            db,
            cfg,
            admin,
            title="Song A",
            artist="Artist X",
            source="api",
            trigger=None,
        )
        assert sub2.request["status"] == RequestStatus.PROCESSING
        assert mock_grab.call_count == 2


def test_duplicate_allowed_after_first_request_available(db: Database, cfg: Config):
    """First request set to 'available' -> new playlist submit is not rejected as duplicate (code != 'duplicate')."""
    admin = db.get_user("admin-1")
    trigger = GrabTrigger(kind="playlist", ref="pl-1", label="Test Playlist")

    with patch("trackseerr.request_submission.native_is_configured", return_value=True), patch(
        "trackseerr.request_submission.acquisition_coordinator.search_and_grab"
    ):
        sub1 = submit_track_request(
            db,
            cfg,
            admin,
            title="Song A",
            artist="Artist X",
            source="list",
            trigger=trigger,
        )
        # Update request status to AVAILABLE
        db.update_request_status(sub1.request["id"], RequestStatus.AVAILABLE)

        # A new playlist submit should not raise duplicate error
        try:
            submit_track_request(
                db,
                cfg,
                admin,
                title="Song A",
                artist="Artist X",
                source="list",
                trigger=trigger,
            )
        except RequestRejected as exc:
            assert exc.code != "duplicate"


def test_library_has_matches_other_release_title(db: Database, cfg: Config):
    """Artist owns 'Play in the Sunshine' (file, cutoff met) -> submitting 'Play In The Sunshine - 2020 Remaster' raises in_library."""
    admin = db.get_user("admin-1")

    # Seed artist, album, track, and library file with cutoff_met
    aid = "art-prince"
    alb_id = "alb-sott"
    trk_id = "trk-pits"
    file_id = "fil-pits"

    db.upsert_library_artist(LibraryArtist(id=aid, name="Prince", clean_name="prince", monitored=True))
    db.upsert_library_album(
        LibraryAlbum(id=alb_id, artist_id=aid, title="Sign o' the Times", clean_title="sign o the times", monitored=True)
    )
    db.upsert_library_track(
        LibraryTrack(
            id=trk_id,
            album_id=alb_id,
            artist_id=aid,
            title="Play in the Sunshine",
            clean_title="play in the sunshine",
            track_number=2,
            monitored=True,
        )
    )
    db.upsert_library_file(
        LibraryFile(
            id=file_id,
            track_id=trk_id,
            file_path="/music/Prince/Sign o the Times/02 Play in the Sunshine.flac",
            relative_path="Prince/Sign o the Times/02 Play in the Sunshine.flac",
            codec="flac",
            quality_name="Lossless",
            size_bytes=25000000,
            cutoff_met=True,
        )
    )

    with patch("trackseerr.request_submission.native_is_configured", return_value=True), patch(
        "trackseerr.request_submission.acquisition_coordinator.search_and_grab"
    ):
        with pytest.raises(RequestRejected) as exc_info:
            submit_track_request(
                db,
                cfg,
                admin,
                title="Play In The Sunshine - 2020 Remaster",
                artist="Prince",
                source="list",
            )
        assert exc_info.value.code == "in_library"
        assert exc_info.value.status_code == 409
        assert "Already in your library" in exc_info.value.detail
