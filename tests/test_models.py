from trackseerr.models import Playlist, SyncResult, Track


def test_track_dataclass():
    track = Track(title="Karma Police", artist="Radiohead", album="OK Computer", url="https://spotify.com/track/123")
    assert track.title == "Karma Police"
    assert track.artist == "Radiohead"
    assert track.album == "OK Computer"
    assert "Karma Police" in repr(track)


def test_playlist_dataclass():
    t1 = Track(title="Song A", artist="Artist A", album="Album A")
    playlist = Playlist(id="pl_1", name="Favorites", description="My favorite tracks", poster="http://img.com/p.jpg", tracks=[t1])
    assert playlist.id == "pl_1"
    assert len(playlist.tracks) == 1
    assert "Favorites" in repr(playlist)


def test_sync_result():
    res = SyncResult(
        playlist_name="Daily Mix",
        total_tracks=10,
        matched_tracks=8,
        missing_tracks=2,
        success=True,
    )
    assert res.success is True
    assert res.matched_tracks == 8
    assert res.missing_tracks == 2
